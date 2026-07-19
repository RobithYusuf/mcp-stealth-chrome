"""Section 27 — Self-sufficient web-research kit (search + fetch + extract).

Goal: let this MCP server replace firecrawl / tavily / (most of) exa for everyday
research, WITHOUT any external search API and WITHOUT always spinning up a browser.

Design principles
-----------------
1. **HTTP-first, browser-fallback (dual-mode).** The fast path is curl_cffi
   (TLS-perfect JA3) — no Chrome process, fully headless/API-mode, low memory.
   The stealth browser is escalated to ONLY when a target actually blocks the
   HTTP request (Cloudflare/anti-bot). This is what makes it lighter *and* able
   to reach anti-bot sites that the pure-API services fail on.
2. **Zero new dependencies.** HTML→markdown uses the stdlib html.parser, so the
   package stays light (matches the "lebih ringan" goal). No readability/bs4.
3. **LLM-ready output.** Clean markdown + citations, token-trimmed.

Tools
  web_search(query, engine, count)         — SERP scrape (DuckDuckGo/Bing), no browser
  scrape_markdown(url, use_browser, ...)    — one URL → clean markdown
  smart_fetch(url, force_browser, ...)      — HTTP-first, auto-escalate to browser on block
  deep_research(query, max_sources, ...)    — search → fetch top-N → markdown + citations
  search_page(text, ...)                    — grep the current browser DOM (zero-LLM)

What it deliberately does NOT try to be: Exa's neural/semantic index. For that,
pass engine="exa" (BYO EXA_API_KEY) — we call it as one backend, we don't rebuild it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from html.parser import HTMLParser
from html import unescape
from typing import Literal, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from .._app import mcp
from ..helpers import err, ok
from ..state import BrowserState

# ---------------------------------------------------------------------------
# Anti-bot block detection (decides HTTP → browser escalation)
# ---------------------------------------------------------------------------

_BLOCK_STATUS = {401, 403, 405, 406, 409, 421, 429, 503}
_BLOCK_MARKERS = (
    "just a moment",              # Cloudflare interstitial
    "cf-chl", "challenge-platform",
    "checking your browser",
    "attention required",         # Cloudflare block page
    "captcha-delivery",           # DataDome
    "please verify you are a human",
    "access denied", "access to this page has been denied",
    "enable javascript and cookies to continue",
    "verifying you are human",
    "px-captcha",                 # PerimeterX
    "incapsula incident",         # Imperva
)


def _looks_blocked(status: Optional[int], text: str) -> bool:
    """True if an HTTP response smells like an anti-bot wall (→ escalate to browser)."""
    if status is None:
        return True
    if status in _BLOCK_STATUS:
        return True
    low = (text or "")[:6000].lower()
    if any(m in low for m in _BLOCK_MARKERS):
        return True
    # A near-empty body on a 200 is often a JS-challenge shell.
    if status == 200 and len(text or "") < 512 and ("<html" in low or low.strip() == ""):
        return True
    return False


# Polite pacing between SERP hits. Rapid-fire scraping trips soft-blocks: the
# engine starts returning decoy/degraded results (e.g. dictionary pages for an
# AI query) or resets the connection. A ~1s gap keeps queries under the radar.
_SERP_MIN_GAP = 1.0
_last_serp_at = [0.0]


async def _serp_throttle() -> None:
    gap = _SERP_MIN_GAP - (time.monotonic() - _last_serp_at[0])
    if gap > 0:
        await asyncio.sleep(gap)
    _last_serp_at[0] = time.monotonic()


# ---------------------------------------------------------------------------
# curl_cffi HTTP fetch (the browserless fast path)
# ---------------------------------------------------------------------------

# JS-challenge / anti-bot interstitials that a REAL browser clears on its own
# after a few seconds (Cloudflare "Just a moment", Reddit "network security"
# js_challenge, etc). curl_cffi can't run the JS, but the stealth browser can —
# we just have to WAIT for the challenge to resolve instead of reading too early.
_CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "challenge-platform",
    "blocked by network security", "verifying you are human", "verify you are human",
    "cf-chl", "jschl", "js_challenge", "please wait", "enable javascript and cookies",
    "ddos protection by", "attention required",
)


async def _browser_render(tab, url: str, max_wait: float = 16.0,
                          settle: float = 1.0) -> str:
    """Navigate then WAIT OUT any JS challenge until real content renders.

    Polls page HTML; treats a tiny body or a known challenge marker as
    "still challenging" and keeps waiting up to max_wait. Returns the final HTML
    (real content if the challenge cleared, else the last challenge/block page so
    _looks_blocked can flag a genuine hard block).
    """
    try:
        await tab.get(url)
    except Exception:
        return ""
    start = time.monotonic()
    deadline = start + max_wait
    last = ""
    reloaded = False
    while time.monotonic() < deadline:
        await asyncio.sleep(settle)
        try:
            html = await tab.get_content()
        except Exception:
            html = ""
        # Scan the WHOLE body: on Reddit's block page the marker sits far below
        # ~190KB of leading CSS, so a head-only slice would miss it and return
        # the block page as if it were real content.
        low = (html or "").lower()
        challenging = (len(html or "") < 1500) or any(m in low for m in _CHALLENGE_MARKERS)
        if html and not challenging:
            return html
        last = html or last
        # One reload partway through: the JS challenge sets a clearance cookie on
        # the first hit, so a reload on the SAME session usually passes (verified
        # on Reddit — the manual "2nd fetch works" behaviour, now automatic).
        if not reloaded and (time.monotonic() - start) > max_wait * 0.35:
            reloaded = True
            try:
                await tab.reload()
            except Exception:
                try:
                    await tab.get(url)
                except Exception:
                    pass
    return last


def _default_proxy(explicit: Optional[str]) -> Optional[str]:
    """Resolve a proxy: explicit arg > RESEARCH_PROXY > HTTPS_PROXY > ALL_PROXY env.

    Routing through a residential/mobile proxy is the scenario that actually
    penetrates IP-level blocks (Reddit, rate-limited SERPs, datacenter-IP bans)
    — no amount of fingerprint/stealth tuning beats a bad source IP. Set once via
    `export RESEARCH_PROXY=http://user:pass@host:port` and every fetch uses it.
    """
    return (explicit or os.environ.get("RESEARCH_PROXY")
            or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or None)


async def _fetch_http(
    url: str,
    impersonate: str = "chrome",
    timeout: float = 25.0,
    method: str = "GET",
    data=None,
    headers: Optional[dict] = None,
    proxy: Optional[str] = None,
) -> tuple[Optional[int], str, dict]:
    """Fetch via curl_cffi. Returns (status, text, headers). status=None on error.

    proxy: explicit override; otherwise falls back to RESEARCH_PROXY/HTTPS_PROXY env.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None, "", {}
    hdrs = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    proxy = _default_proxy(proxy)
    req_kw: dict = {"data": data, "headers": hdrs, "timeout": timeout,
                    "allow_redirects": True}
    if proxy:
        req_kw["proxies"] = {"http": proxy, "https": proxy}
    try:
        async with AsyncSession(impersonate=impersonate) as s:
            resp = await s.request(method.upper(), url, **req_kw)
            try:
                text = resp.text
            except Exception:
                text = ""
            return resp.status_code, text, dict(resp.headers)
    except Exception:
        return None, "", {}


# ---------------------------------------------------------------------------
# HTML → Markdown (stdlib only — keeps the package light)
# ---------------------------------------------------------------------------

_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "head", "nav", "footer", "aside",
    "form", "iframe", "template", "button", "select", "canvas", "figure",
}
_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
_BLOCK = {"p", "div", "section", "article", "header", "main", "tr", "blockquote",
          "table", "ul", "ol", "hr"}


class _MarkdownExtractor(HTMLParser):
    """Minimal, defensive HTML→markdown walker. Not a full converter — enough to
    give an LLM clean, readable text with headings, lists, and resolved links."""

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title: Optional[str] = None
        self._in_title = False
        self._in_link = False
        self._href: Optional[str] = None
        self._link_text: list[str] = []
        self._in_pre = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADING:
            self.parts.append("\n\n" + _HEADING[tag])
        elif tag == "a":
            d = dict(attrs)
            self._href = d.get("href")
            self._in_link = True
            self._link_text = []
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n\n---\n\n")
        elif tag == "pre":
            self._in_pre = True
            self.parts.append("\n\n```\n")
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADING:
            self.parts.append("\n")
        elif tag == "pre":
            self._in_pre = False
            self.parts.append("\n```\n")
        elif tag == "a" and self._in_link:
            text = "".join(self._link_text).strip()
            href = self._href or ""
            if href.startswith("//"):
                href = "https:" + href
            elif href and not href.startswith(("http", "mailto:")) and self.base_url:
                try:
                    href = urljoin(self.base_url, href)
                except Exception:
                    pass
            if text and href.startswith("http"):
                self.parts.append(f"[{text}]({href})")
            elif text:
                self.parts.append(text)
            self._in_link = False
            self._href = None
            self._link_text = []

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self._in_title:
            self.title = (self.title or "") + data
            return
        if self._in_link:
            self._link_text.append(data)
            return
        self.parts.append(data)


# Content containers to prefer over the whole page, MOST-SPECIFIC first — drops
# nav/sidebar/footer AND site-chrome nested in a broad <main> (e.g. Wikipedia's
# language list lives in main#content but NOT in #mw-content-text).
_MAIN_PATTERNS = [
    re.compile(r'<(article)\b[^>]*>', re.I),
    re.compile(r'<(\w+)\b[^>]*\bid=["\'](?:mw-content-text|main-content|'
               r'maincontent|primary|content-main|article-body|post-content)["\']', re.I),
    re.compile(r'<(main)\b[^>]*>', re.I),
    re.compile(r'<(\w+)\b[^>]*\brole=["\']main["\']', re.I),
    re.compile(r'<(\w+)\b[^>]*\bid=["\'](?:content|main)["\']', re.I),
]


def _slice_main(html: str) -> Optional[str]:
    """Inner HTML of the primary content container, trying the most-specific
    article-body landmark first, so nav/sidebar/footer boilerplate is dropped.
    None if no landmark is found (caller falls back to the whole document)."""
    for pat in _MAIN_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        tag = m.group(1).lower()
        gt = html.find(">", m.start())
        if gt == -1:
            continue
        start = gt + 1
        depth = 1
        for mm in re.finditer(rf"<(/?){tag}\b", html[start:], re.I):
            depth += -1 if mm.group(1) else 1
            if depth == 0:
                inner = html[start:start + mm.start()]
                if len(inner) > 200:
                    return inner
                break
    return None


def _html_to_markdown(html: str, base_url: str = "", max_chars: int = 20000) -> str:
    """Convert HTML to trimmed markdown. Best-effort, never raises."""
    if not html:
        return ""
    # Title from the full document (a sliced main region won't contain <title>).
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip() if tm else ""
    # Prefer the primary content container to drop nav/sidebar/footer noise.
    target = _slice_main(html) or html
    try:
        p = _MarkdownExtractor(base_url=base_url)
        p.feed(target)
        raw = "".join(p.parts)
        if not title:
            title = (p.title or "").strip()
    except Exception:
        # Fall back to a crude tag strip so we always return *something*.
        raw = re.sub(r"<[^>]+>", " ", target)
    raw = unescape(raw)
    # Line-wise normalize: collapse intra-line whitespace, rstrip, squeeze blanks.
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if title and not text.lstrip().startswith("#"):
        text = f"# {title}\n\n{text}"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + f"\n\n[truncated at {max_chars} chars]"
    return text


# ---------------------------------------------------------------------------
# SERP scraping (DuckDuckGo + Bing) — no browser, no API key
# ---------------------------------------------------------------------------

def _decode_ddg_href(href: str) -> str:
    """DuckDuckGo wraps result links in a /l/?uddg=<encoded> redirect. Unwrap it."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs and qs["uddg"]:
                return qs["uddg"][0]
        except Exception:
            pass
    return href


class _DDGParser(HTMLParser):
    """Parse duckduckgo html/lite SERP into [{title,url,snippet}]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._cap_title = False
        self._cap_snip = False
        self._cur: Optional[dict] = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": _decode_ddg_href(d.get("href", "")), "snippet": ""}
            self._cap_title = True
            self._buf = []
        elif ("result__snippet" in cls) and self._cur is not None:
            self._cap_snip = True
            self._buf = []

    def handle_endtag(self, tag):
        if self._cap_title and tag == "a":
            self._cur["title"] = "".join(self._buf).strip()
            self._cap_title = False
            if self._cur.get("url", "").startswith("http"):
                self.results.append(self._cur)
            # snippet (if any) attaches to this same dict which is already in list
        elif self._cap_snip and tag in ("a", "td", "div", "span"):
            if self.results:
                self.results[-1]["snippet"] = "".join(self._buf).strip()
            self._cap_snip = False

    def handle_data(self, data):
        if self._cap_title or self._cap_snip:
            self._buf.append(data)


async def _ddg_search(query: str, count: int, proxy: Optional[str] = None) -> list[dict]:
    """DuckDuckGo SERP via the html endpoint, with the lite endpoint as fallback."""
    from urllib.parse import quote_plus
    out: list[dict] = []
    await _serp_throttle()
    # 1) html.duckduckgo.com (POST form) — richest markup
    status, text, _ = await _fetch_http(
        "https://html.duckduckgo.com/html/", method="POST",
        data={"q": query, "kl": "us-en"},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://html.duckduckgo.com/"},
        proxy=proxy,
    )
    if status == 200 and text:
        p = _DDGParser()
        try:
            p.feed(text)
            out = p.results
        except Exception:
            out = []
    # 2) fallback: lite endpoint (GET) if html gave nothing
    if not out:
        status, text, _ = await _fetch_http(
            f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}", proxy=proxy,
        )
        if status == 200 and text:
            p = _DDGParser()
            try:
                p.feed(text)
                out = p.results
            except Exception:
                out = []
    # Dedup by URL, keep order
    seen, deduped = set(), []
    for r in out:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            deduped.append(r)
    return deduped[:count]


def _decode_bing_href(href: str) -> str:
    """Bing wraps result links in /ck/a?...&u=a1<base64url>. Decode to the real URL."""
    href = (href or "").replace("&amp;", "&")
    if "/ck/a" not in href:
        return href
    try:
        u = (parse_qs(urlparse(href).query).get("u") or [""])[0]
        if u.startswith("a1"):
            b = u[2:] + "=" * (-len(u[2:]) % 4)
            return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
    except Exception:
        pass
    return href


async def _bing_search(query: str, count: int, proxy: Optional[str] = None) -> list[dict]:
    """Bing SERP scrape (block-split regex — robust to nested markup).

    mkt=en-US is REQUIRED: without it, cookieless results are near-random
    (verified: 'capital of France' -> 'Capital of Indonesia'). Real destination
    URLs are hidden behind /ck/a redirects, decoded via _decode_bing_href.
    """
    from urllib.parse import quote_plus
    await _serp_throttle()
    status, text, _ = await _fetch_http(
        f"https://www.bing.com/search?q={quote_plus(query)}"
        f"&mkt=en-US&setlang=en&count={max(count, 10)}&form=QBLH",
        headers={"Referer": "https://www.bing.com/"},
        proxy=proxy,
    )
    if status != 200 or not text:
        return []
    # Genuine empty (not a decoy) — bail so the caller reports 0 honestly.
    if "There are no results for" in text or 'class="b_no"' in text:
        return []
    out, seen = [], set()
    for blk in text.split('class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
            continue
        url = _decode_bing_href(m.group(1))
        title = unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = ""
        sm = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        if sm:
            snippet = unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip()
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if url.startswith("http") and host and "bing.com" not in host and url not in seen:
            seen.add(url)
            out.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(out) >= count:
            break
    return out


async def _exa_search(query: str, count: int, proxy: Optional[str] = None) -> list[dict]:
    """Optional neural search via Exa (BYO EXA_API_KEY). The one thing a browser
    can't replicate — offered as a backend, not reimplemented."""
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return []
    try:
        from curl_cffi.requests import AsyncSession
        proxy = _default_proxy(proxy)
        post_kw: dict = {
            "headers": {"x-api-key": key, "Content-Type": "application/json"},
            "data": json.dumps({"query": query, "numResults": count}),
            "timeout": 25.0,
        }
        if proxy:
            post_kw["proxies"] = {"http": proxy, "https": proxy}
        async with AsyncSession() as s:
            resp = await s.post("https://api.exa.ai/search", **post_kw)
            data = resp.json()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("text") or "")[:300]}
            for r in (data.get("results") or [])
        ][:count]
    except Exception:
        return []


async def _search(query: str, engine: str, count: int,
                  proxy: Optional[str] = None) -> list[dict]:
    engine = (engine or "bing").lower()
    if engine in ("exa", "neural"):
        return await _exa_search(query, count, proxy)
    if engine in ("duckduckgo", "ddg"):
        res = await _ddg_search(query, count, proxy)
        return res or await _bing_search(query, count, proxy)
    # default: bing (most reliable browserless engine), fall back to duckduckgo
    res = await _bing_search(query, count, proxy)
    return res or await _ddg_search(query, count, proxy)


# ---------------------------------------------------------------------------
# Browser-driven SERP (escalation rung: real Chrome fingerprint + live DOM)
# ---------------------------------------------------------------------------
# Parses the RENDERED DOM via tab.evaluate (not get_content HTML) because SERPs
# inject results with JS after load — regex on the pre-hydration HTML misses them.

_BROWSER_SERP = {
    "bing": {
        "url": "https://www.bing.com/search?q={q}&mkt=en-US&setlang=en",
        "js": r"""(() => JSON.stringify(
            Array.from(document.querySelectorAll('#b_results > li.b_algo')).slice(0, %d)
              .map(li => { const a = li.querySelector('h2 a');
                           const p = li.querySelector('.b_caption p') || li.querySelector('p');
                           return a ? {title: a.innerText, url: a.href,
                                       snippet: p ? p.innerText.slice(0,300) : ''} : null; })
              .filter(Boolean)))()""",
    },
    "duckduckgo": {
        "url": "https://duckduckgo.com/?q={q}&ia=web",
        "js": r"""(() => JSON.stringify(
            Array.from(document.querySelectorAll('article[data-testid=\"result\"]')).slice(0, %d)
              .map(x => { const a = x.querySelector('a[data-testid=\"result-title-a\"]');
                          const s = x.querySelector('[data-result=\"snippet\"]');
                          return a ? {title: a.innerText, url: a.href,
                                      snippet: s ? s.innerText.slice(0,300) : ''} : null; })
              .filter(Boolean)))()""",
    },
}


async def _browser_search(query: str, engine: str, count: int) -> list[dict]:
    """Run the SERP inside the live stealth browser and read results from the
    rendered DOM. Requires a running browser (browser_launch). Beats curl_cffi
    on soft-blocked / TLS-broken engines because it uses a real Chrome session.
    """
    from urllib.parse import quote_plus
    if not BrowserState.is_up():
        return []
    eng = engine if engine in _BROWSER_SERP else "bing"
    cfg = _BROWSER_SERP[eng]
    js = cfg["js"] % max(count, 8)
    try:
        tab = BrowserState.active_tab()
        await tab.get(cfg["url"].format(q=quote_plus(query)))
    except Exception:
        return []
    # Poll until results render (SPA) or ~9s elapse.
    for _ in range(18):
        await asyncio.sleep(0.5)
        try:
            raw = await tab.evaluate(js, return_by_value=True)
            raw = raw.value if hasattr(raw, "value") else raw
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            data = None
        if isinstance(data, list) and data:
            out, seen = [], set()
            for r in data:
                u = (r or {}).get("url", "")
                if eng == "bing":
                    u = _decode_bing_href(u)   # rendered anchors still point at /ck/a
                try:
                    host = urlparse(u).netloc
                except Exception:
                    host = ""
                if u.startswith("http") and host and "bing.com" not in host and u not in seen:
                    seen.add(u)
                    out.append({"title": (r.get("title") or "").strip(),
                                "url": u, "snippet": (r.get("snippet") or "").strip()})
            if out:
                return out[:count]
    return []


# Words too generic to judge topical relevance by.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "is", "are",
    "best", "top", "how", "what", "why", "vs", "with", "review", "reviews",
    "2023", "2024", "2025", "2026", "guide", "worth", "it", "your", "you",
}


def _looks_decoy(query: str, results: list) -> bool:
    """True if SERP results look like soft-block DECOYS: they exist but share
    almost no keyword with the query (e.g. 'best AI coding agent' -> WhatsApp
    results). Lets web_search reject silent garbage instead of returning it."""
    if not results:
        return False
    qwords = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())
              if w not in _STOPWORDS}
    if len(qwords) < 2:
        return False  # too few meaningful words to judge reliably
    hits = 0
    for r in results:
        blob = f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower()
        if any(w in blob for w in qwords):
            hits += 1
    return hits < max(1, len(results) * 0.34)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_search(
    query: str,
    engine: Literal["bing", "duckduckgo", "exa"] = "bing",
    count: int = 8,
    use_browser: Literal["auto", "off", "on"] = "auto",
    proxy: Optional[str] = None,
) -> str:
    """⭐ Web search WITHOUT a browser or paid API — replaces tavily/exa discovery.

    Escalation ladder ("use every scenario"):
      1. curl_cffi SERP (browserless, headless/API-mode, light) — Bing default
         (mkt=en-US), DuckDuckGo fallback.
      2. If that is soft-blocked/empty AND a browser is running, escalate to a
         browser-driven SERP (real Chrome fingerprint + live DOM) — set
         use_browser="on" to force it, "off" to disable.
      3. engine="exa" → Exa neural search (BYO EXA_API_KEY) — reliable + semantic.

    proxy: route through a residential/mobile proxy (or set RESEARCH_PROXY env) —
    the only thing that beats IP-level blocks (rate-limited SERPs, datacenter bans).

    Returns {query, engine, count, via:"http"|"browser", results:[{title,url,snippet}]}.
    """
    q = (query or "").strip()
    if not q:
        return err("empty query")
    count = max(1, min(int(count), 25))
    t0 = time.monotonic()

    via = "http"
    results = await _search(q, engine, count, proxy=proxy)
    if _looks_decoy(q, results):
        results = []  # soft-block decoy (off-topic) — discard, treat as no-results
    # Escalate to the browser SERP when HTTP is degraded (auto) or forced (on).
    if ((use_browser == "on") or (use_browser == "auto" and not results)) \
            and BrowserState.is_up():
        br = await _browser_search(q, engine, count)
        if br and not _looks_decoy(q, br):
            results, via = br, "browser"

    if not results:
        return ok(json.dumps({
            "query": q, "engine": engine, "count": 0, "results": [],
            "degraded": True,
            "note": "no results — the SERP is likely rate-limiting/IP-blocking this "
                    "host (rapid queries trigger soft-blocks that return 0 or decoy "
                    "results). Options: wait & retry; browser_launch then retry "
                    "(auto-escalates to a real-Chrome SERP); pass proxy=... or set "
                    "RESEARCH_PROXY (residential IP beats datacenter bans); or "
                    "engine='exa' (BYO EXA_API_KEY) for reliable/semantic search.",
        }))
    return ok(json.dumps({
        "query": q,
        "engine": engine,
        "count": len(results),
        "via": via,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "browserless": via == "http",
        "results": results,
    }, ensure_ascii=False))


@mcp.tool()
async def scrape_markdown(
    url: str,
    use_browser: bool = False,
    impersonate: str = "chrome",
    max_chars: int = 20000,
    proxy: Optional[str] = None,
) -> str:
    """⭐ Fetch one URL → clean LLM-ready markdown (firecrawl-scrape equivalent).

    Default path is curl_cffi (no browser — fast, light, headless). Set
    use_browser=True to render via the stealth browser (needs browser_launch)
    for JS-heavy or anti-bot pages.

    Returns {url, path, status, chars, markdown}.
    """
    u = (url or "").strip()
    if not u.startswith("http"):
        return err("url must start with http/https")
    t0 = time.monotonic()

    if use_browser:
        if not BrowserState.is_up():
            return err("use_browser=True but no browser — call browser_launch first")
        try:
            tab = BrowserState.active_tab()
            html = await _browser_render(tab, u)   # waits out JS challenges
        except Exception as e:
            return err(f"browser fetch failed: {type(e).__name__}: {e}")
        md = _html_to_markdown(html, base_url=u, max_chars=max_chars)
        return ok(json.dumps({
            "url": u, "path": "browser", "status": 200,
            "chars": len(md), "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "markdown": md,
        }, ensure_ascii=False))

    status, text, _ = await _fetch_http(u, impersonate=impersonate, proxy=proxy)
    if status is None:
        return err(f"fetch failed for {u} (network error)")
    md = _html_to_markdown(text, base_url=u, max_chars=max_chars)
    blocked = _looks_blocked(status, text)
    return ok(json.dumps({
        "url": u, "path": "http", "status": status,
        "blocked": blocked, "chars": len(md),
        "raw_html_chars": len(text),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "hint": "looks anti-bot blocked — retry with use_browser=True" if blocked else None,
        "markdown": md,
    }, ensure_ascii=False))


@mcp.tool()
async def smart_fetch(
    url: str,
    force_browser: bool = False,
    impersonate: str = "chrome",
    max_chars: int = 20000,
    proxy: Optional[str] = None,
) -> str:
    """⭐ Dual-mode fetch: curl_cffi first, auto-escalate to the stealth browser
    ONLY when the HTTP request is anti-bot blocked. The efficiency core — high
    volume stays browserless; Chrome fires only when truly needed.

    Returns {url, path:"http"|"browser"|"http_blocked_no_browser", status, chars, markdown}.
    """
    u = (url or "").strip()
    if not u.startswith("http"):
        return err("url must start with http/https")
    t0 = time.monotonic()

    status, text, _ = None, "", {}
    if not force_browser:
        status, text, _ = await _fetch_http(u, impersonate=impersonate, proxy=proxy)
        if status is not None and not _looks_blocked(status, text):
            md = _html_to_markdown(text, base_url=u, max_chars=max_chars)
            # A 200 with almost no extractable text is usually a JS/SPA shell
            # (e.g. www.reddit.com) — fall through to render it in the browser.
            # If no browser is up, return the HTTP result rather than nothing.
            if len(md) >= 200 or not BrowserState.is_up():
                return ok(json.dumps({
                    "url": u, "path": "http", "status": status, "chars": len(md),
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "markdown": md,
                }, ensure_ascii=False))

    # Escalate to browser (blocked, empty SPA shell, or forced)
    if BrowserState.is_up():
        try:
            tab = BrowserState.active_tab()
            html = await _browser_render(tab, u)   # waits out JS challenges (Reddit/CF)
        except Exception as e:
            return err(f"browser escalation failed: {type(e).__name__}: {e}")
        md = _html_to_markdown(html, base_url=u, max_chars=max_chars)
        return ok(json.dumps({
            "url": u, "path": "browser", "status": 200, "chars": len(md),
            "escalated_from_http_status": status,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "markdown": md,
        }, ensure_ascii=False))

    # Blocked and no browser to escalate to
    md = _html_to_markdown(text, base_url=u, max_chars=max_chars)
    return ok(json.dumps({
        "url": u, "path": "http_blocked_no_browser", "status": status,
        "chars": len(md),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "hint": "HTTP path blocked — call browser_launch then smart_fetch again to escalate",
        "markdown": md,
    }, ensure_ascii=False))


@mcp.tool()
async def deep_research(
    query: str,
    max_sources: int = 5,
    engine: Literal["bing", "duckduckgo", "exa"] = "bing",
    per_source_chars: int = 3000,
    proxy: Optional[str] = None,
) -> str:
    """⭐ One-call research: search → fetch top-N sources → clean markdown + citations.

    Replaces tavily/firecrawl "research". Fully browserless fast path (curl_cffi);
    sources that are anti-bot blocked are flagged so the caller can re-fetch them
    with smart_fetch(force_browser=True). The host LLM synthesizes from `sources`.

    Returns {query, engine, count, sources:[{title, url, snippet, blocked, content}]}.
    """
    q = (query or "").strip()
    if not q:
        return err("empty query")
    max_sources = max(1, min(int(max_sources), 10))
    t0 = time.monotonic()

    results = await _search(q, engine, count=max_sources * 2, proxy=proxy)
    if not results:
        return ok(json.dumps({"query": q, "engine": engine, "count": 0, "sources": [],
                              "note": "search returned nothing — see web_search note "
                                      "(retry/proxy/browser_launch/engine='exa')"}))
    picked = results[:max_sources]

    async def _one(r: dict) -> dict:
        status, text, _ = await _fetch_http(r.get("url", ""), proxy=proxy)
        blocked = _looks_blocked(status, text)
        content = "" if blocked else _html_to_markdown(
            text, base_url=r.get("url", ""), max_chars=per_source_chars)
        return {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "status": status,
            "blocked": blocked,
            "content": content,
        }

    sources = await asyncio.gather(*[_one(r) for r in picked])
    ok_n = sum(1 for s in sources if not s["blocked"])
    return ok(json.dumps({
        "query": q,
        "engine": engine,
        "count": len(sources),
        "fetched_ok": ok_n,
        "blocked": len(sources) - ok_n,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "browserless": True,
        "sources": sources,
    }, ensure_ascii=False))


@mcp.tool()
async def search_page(text: str, regex: bool = False, max_matches: int = 20,
                      context_chars: int = 120) -> str:
    """Grep the CURRENT page's visible text — zero LLM cost, instant.

    Use before spending vision/LLM tokens: find where a term appears (with
    surrounding context) on the already-loaded page. Needs a live browser.

    Returns {query, matches:[{index, context}], total}.
    """
    if not BrowserState.is_up():
        return err("no browser — call browser_launch first")
    q = text or ""
    if not q:
        return err("empty search text")
    try:
        tab = BrowserState.active_tab()
        raw = await tab.evaluate(
            "document.body ? document.body.innerText : ''", return_by_value=True)
        body = raw.value if hasattr(raw, "value") else raw
        body = str(body or "")
    except Exception as e:
        return err(f"could not read page text: {e}")

    matches = []
    try:
        if regex:
            it = re.finditer(q, body, re.IGNORECASE)
        else:
            it = re.finditer(re.escape(q), body, re.IGNORECASE)
        for m in it:
            start = max(0, m.start() - context_chars)
            end = min(len(body), m.end() + context_chars)
            ctx = re.sub(r"\s+", " ", body[start:end]).strip()
            matches.append({"index": m.start(), "context": f"…{ctx}…"})
            if len(matches) >= max_matches:
                break
    except re.error as e:
        return err(f"bad regex: {e}")
    return ok(json.dumps({
        "query": q, "regex": regex, "total": len(matches), "matches": matches,
    }, ensure_ascii=False))
