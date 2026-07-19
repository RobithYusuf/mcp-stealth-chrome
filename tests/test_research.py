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


if __name__ == "__main__":
    for fn in (test_block_detection, test_bing_decode, test_ddg_decode,
               test_html_to_markdown, test_truncation,
               test_proxy_resolution, test_browser_serp_config):
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            sys.exit(1)
    print("\nALL PASS")
