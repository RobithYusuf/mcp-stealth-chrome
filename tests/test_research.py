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
    for eng in ("bing", "duckduckgo", "brave", "google"):
        cfg = _BROWSER_SERP[eng]
        assert "{q}" in cfg["url"], eng
        assert "%d" in cfg["js"] and "querySelectorAll" in cfg["js"], eng
        assert cfg["js"].count("%d") == 1, f"{eng}: exactly one %d for slice count"
    # google: reliable fallback SERP, honors site:, anchors on <h3>
    assert "google.com/search" in _BROWSER_SERP["google"]["url"]
    assert "h3" in _BROWSER_SERP["google"]["js"]
    # Brave relies on the stable data-type="web" attribute (Svelte hashes the rest)
    assert 'data-type=\\"web\\"' in _BROWSER_SERP["brave"]["js"]
    assert "search.brave.com" in _BROWSER_SERP["brave"]["url"]
    print("  browser-SERP live-DOM config present (bing + duckduckgo + brave)")


def test_brave_browser_only():
    import asyncio
    from mcp_stealth_chrome.tools.research import _search
    # brave is a browser-only rich SERP: the HTTP _search layer must yield []
    # so web_search / deep_research escalate to the live-DOM browser SERP.
    assert asyncio.run(_search("anything at all", "brave", 5)) == []
    print("  brave routed browser-only (HTTP _search returns [])")


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


def test_reddit_digest_parse():
    from mcp_stealth_chrome.tools.research import _parse_reddit_digest, _parse_reddit_num
    assert _parse_reddit_num("3.5K") == 3500
    assert _parse_reddit_num("1,234") == 1234
    assert _parse_reddit_num("548") == 548
    md = (
        "##\n[Best coding agent thread](https://www.reddit.com/r/X/comments/abc/best/)\n"
        "[r/X](https://www.reddit.com/r/X/)\n· 3mo ago\n"
        "[Best coding agent thread](https://www.reddit.com/r/X/comments/abc/best/)\n"
        "3.5K votes·548 comments\n---\n"
        "##\n[Second post](https://www.reddit.com/r/X/comments/def/second/)\n"
        "· 1d ago\n[Second post](https://www.reddit.com/r/X/comments/def/second/)\n"
        "2K votes·41 comments\n"
    )
    posts = _parse_reddit_digest(md, 10)
    assert len(posts) == 2, posts
    p = posts[0]
    assert (p["title"] == "Best coding agent thread" and p["votes"] == 3500
            and p["comments"] == 548 and p["age"] == "3mo ago"), p
    assert posts[1]["votes"] == 2000 and posts[1]["comments"] == 41, posts[1]
    print("  reddit digest parse: dedup + votes/comments/age OK")


def test_adapters_parse():
    import json as _j
    from mcp_stealth_chrome.tools.research import _gh_parse, _yt_id, _extract_js_object
    assert _gh_parse("Panniantong/Agent-Reach") == ("Panniantong", "Agent-Reach")
    assert _gh_parse("https://github.com/openai/whisper") == ("openai", "whisper")
    assert _gh_parse("git@github.com:a/b.git") == ("a", "b")
    assert _yt_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
    assert _yt_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert _yt_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert _yt_id("not-a-video-url") is None
    # string-aware brace matching (YT player response has braces inside strings)
    obj = _extract_js_object('ytInitialPlayerResponse = {"a":1,"b":{"c":"}"}}; junk', "ytInitialPlayerResponse")
    assert _j.loads(obj) == {"a": 1, "b": {"c": "}"}}, obj
    print("  adapter parsers: gh-repo + yt-id + balanced js-object OK")


def test_social_research_config():
    import inspect
    from mcp_stealth_chrome.tools.research import (
        _SOCIAL_SITES, _SOCIAL_LOGIN_WALLED, _SOCIAL_NATIVE,
        _social_native_text, social_research,
    )
    # every login-walled + native platform must have a site domain
    for p in _SOCIAL_LOGIN_WALLED | set(_SOCIAL_NATIVE):
        assert p in _SOCIAL_SITES, p
    # native URLs are query templates
    for p, url in _SOCIAL_NATIVE.items():
        assert "{q}" in url and p in url.split("//", 1)[-1] or "reddit" in url, (p, url)
    assert _SOCIAL_LOGIN_WALLED <= set(_SOCIAL_SITES)
    assert "reddit" not in _SOCIAL_LOGIN_WALLED  # reddit is public
    assert inspect.iscoroutinefunction(_social_native_text)
    # tool is registered + wrapped
    from mcp_stealth_chrome.server import mcp
    assert "social_research" in mcp._tool_manager._tools
    print("  social_research config: sites/native/login-walled coherent + registered")


if __name__ == "__main__":
    for fn in (test_block_detection, test_bing_decode, test_ddg_decode,
               test_html_to_markdown, test_truncation,
               test_proxy_resolution, test_browser_serp_config,
               test_brave_browser_only,
               test_challenge_markers, test_slice_main, test_decoy_detection,
               test_reddit_digest_parse, test_adapters_parse,
               test_social_research_config):
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            sys.exit(1)
    print("\nALL PASS")
