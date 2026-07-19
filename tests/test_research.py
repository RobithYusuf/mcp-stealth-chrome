"""Offline unit tests for the web-research kit (tools/research.py) — no network.

Run:  uv run python tests/test_research.py
Covers the deterministic pieces: anti-bot block detection, SERP redirect
decoders (Bing /ck/a, DuckDuckGo uddg), and the stdlib HTML→markdown converter.
Live-network behaviour (web_search/deep_research) is exercised manually, not here.
"""
import base64
import sys
from urllib.parse import quote


def test_block_detection():
    from mcp_stealth_chrome.tools.research import _looks_blocked
    assert _looks_blocked(403, "") is True
    assert _looks_blocked(429, "x") is True
    assert _looks_blocked(None, "x") is True
    assert _looks_blocked(200, "<html>Just a moment...</html>") is True
    assert _looks_blocked(200, "<html>" + "x" * 3000 + "</html>") is False
    print("  block detection: 5 cases OK")


def test_bing_decode():
    from mcp_stealth_chrome.tools.research import _decode_bing_href
    real = "https://en.wikipedia.org/wiki/Paris"
    enc = "a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&&p=abc&u={enc}&ntb=1"
    got = _decode_bing_href(href)
    assert got == real, got
    # plain URL passes through unchanged
    assert _decode_bing_href("https://example.com/x") == "https://example.com/x"
    print("  bing /ck/a decode OK")


def test_ddg_decode():
    from mcp_stealth_chrome.tools.research import _decode_ddg_href
    real = "https://example.org/page?a=1"
    href = f"//duckduckgo.com/l/?uddg={quote(real, safe='')}&rut=x"
    got = _decode_ddg_href(href)
    assert got == real, got
    print("  ddg uddg decode OK")


def test_html_to_markdown():
    from mcp_stealth_chrome.tools.research import _html_to_markdown
    html = (
        "<html><head><title>My Page</title>"
        "<script>var x=1;evil()</script><style>.a{color:red}</style></head>"
        "<body><h1>Hello</h1><p>World <a href='/rel'>link</a></p>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )
    md = _html_to_markdown(html, base_url="https://site.test/dir/", max_chars=5000)
    assert "evil()" not in md and "color:red" not in md, f"script/style not stripped: {md!r}"
    assert "# Hello" in md, md
    assert "[link](https://site.test/rel)" in md, md   # relative link resolved via base_url
    assert "- one" in md and "- two" in md, md
    assert len(md) < len(html), "markdown should be smaller than source HTML"
    print("  html→markdown: strip + headings + resolved links + lists OK")


def test_truncation():
    from mcp_stealth_chrome.tools.research import _html_to_markdown
    big = "<p>" + ("word " * 5000) + "</p>"
    md = _html_to_markdown(big, max_chars=200)
    assert len(md) <= 260 and "truncated" in md, f"len={len(md)}"
    print("  markdown truncation honored")


def test_proxy_resolution():
    import os
    from mcp_stealth_chrome.tools.research import _default_proxy
    assert _default_proxy("http://explicit:1") == "http://explicit:1"
    saved = os.environ.get("RESEARCH_PROXY")
    try:
        os.environ["RESEARCH_PROXY"] = "http://envproxy:8080"
        assert _default_proxy(None) == "http://envproxy:8080"
        assert _default_proxy("http://win:1") == "http://win:1"  # explicit overrides env
    finally:
        if saved is None:
            os.environ.pop("RESEARCH_PROXY", None)
        else:
            os.environ["RESEARCH_PROXY"] = saved
    print("  proxy resolution (explicit > env) OK")


def test_browser_serp_config():
    from mcp_stealth_chrome.tools.research import _BROWSER_SERP
    for eng in ("bing", "duckduckgo"):
        cfg = _BROWSER_SERP[eng]
        assert "{q}" in cfg["url"], eng
        assert "%d" in cfg["js"] and "querySelectorAll" in cfg["js"], eng
    print("  browser-SERP live-DOM config present (bing + duckduckgo)")


def test_challenge_markers():
    import inspect
    from mcp_stealth_chrome.tools.research import _CHALLENGE_MARKERS, _browser_render
    assert "blocked by network security" in _CHALLENGE_MARKERS  # Reddit js_challenge
    assert "just a moment" in _CHALLENGE_MARKERS                # Cloudflare
    assert inspect.iscoroutinefunction(_browser_render)
    print("  JS-challenge markers + _browser_render present")


def test_slice_main():
    from mcp_stealth_chrome.tools.research import _slice_main, _html_to_markdown
    html = ("<html><head><title>T</title></head><body>"
            "<nav>Home About Login Language list AAA BBB CCC</nav>"
            "<main><h1>Real Title</h1><p>" + ("Actual body content here. " * 20) +
            "</p></main><footer>copyright junk</footer></body></html>")
    inner = _slice_main(html)
    assert inner is not None and "Actual body content" in inner, inner
    assert "Login" not in inner and "copyright junk" not in inner, "boilerplate leaked"
    md = _html_to_markdown(html, max_chars=2000)
    assert "Actual body content" in md and "Login" not in md, md
    # no main container -> returns None, falls back to whole doc
    assert _slice_main("<div>hello world plain page no landmarks</div>") is None
    print("  main-content slice drops nav/footer, keeps article")


def test_decoy_detection():
    from mcp_stealth_chrome.tools.research import _looks_decoy
    q = "best AI coding agent 2026"
    decoy = [{"title": "WhatsApp Web", "url": "https://web.whatsapp.com/", "snippet": "messaging"},
             {"title": "WhatsApp", "url": "https://www.whatsapp.com/", "snippet": "chat"}]
    real = [{"title": "Top AI coding agents compared", "url": "https://x.com/agents", "snippet": "Cursor, Claude Code..."},
            {"title": "Best AI agent for coding in 2026", "url": "https://y.com/", "snippet": "review"}]
    assert _looks_decoy(q, decoy) is True, "should flag off-topic decoys"
    assert _looks_decoy(q, real) is False, "should accept on-topic results"
    assert _looks_decoy(q, []) is False
    print("  SERP decoy detection: rejects off-topic, keeps relevant")


if __name__ == "__main__":
    for fn in (test_block_detection, test_bing_decode, test_ddg_decode,
               test_html_to_markdown, test_truncation,
               test_proxy_resolution, test_browser_serp_config,
               test_challenge_markers, test_slice_main, test_decoy_detection):
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            sys.exit(1)
    print("\nALL PASS")
