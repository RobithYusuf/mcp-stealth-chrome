"""Tools merged into others, and three that reported less than they did.

humanize_click is click(humanize=True) or mouse_click_xy(humanize=True); localstorage_clear,
sessionstorage_set and sessionstorage_clear are localstorage_set(area=, clear=). The old names
were kept for one release and removed in 0.25.0. click_at_image says whether the page changed, extract_structured
returns absolute links, and fingerprint_rotate warns when it claims another OS.

The unit tests run always; the browser tests need MCP_E2E=1 (headless, ephemeral, loopback).
"""
import http.server
import json
import os
import sys
import threading
import urllib.parse

import pytest

from mcp_stealth_chrome.tools import stealth


def test_an_identity_of_another_os_is_named_and_one_of_this_os_is_not(monkeypatch):
    win = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/157.0.0.0"
    mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/157.0.0.0"
    monkeypatch.setattr(stealth.sys, "platform", "darwin")
    note = stealth._os_mismatch_note(win, None)
    assert "claims windows while Chrome runs on macos" in note and "GPU" in note
    assert stealth._os_mismatch_note(mac, None) == ""
    assert stealth._os_mismatch_note(None, "Win32") != ""
    assert stealth._os_mismatch_note(None, None) == ""  # language or timezone only
    monkeypatch.setattr(stealth.sys, "platform", "win32")
    assert stealth._os_mismatch_note(win, None) == ""
    assert "claims macos" in stealth._os_mismatch_note(mac, "MacIntel")


e2e = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

PAGE = """<!doctype html><title>Merged</title><style>body{margin:0}
#cv{position:absolute;left:20px;top:20px} #dead{position:absolute;left:20px;top:140px}</style>
<canvas id=cv width=300 height=80></canvas><canvas id=dead width=300 height=80></canvas>
<p id=n style="position:absolute;top:240px">0</p>
<ul id=list><li class=card><a class=t href="/p/1">One</a></li><li class=card><a class=t href="p/2">Two</a></li></ul>
<button id=b style="position:absolute;left:20px;top:280px" onclick="n.textContent=+n.textContent+1">Add</button>
<script>
function paint(id){const g=document.getElementById(id).getContext('2d');g.fillStyle='#eee';g.fillRect(0,0,300,80);
g.fillStyle='#e8590c';g.fillRect(170,15,110,50);g.fillStyle='#fff';g.font='bold 20px sans-serif';g.fillText('BUY',205,48);}
paint('cv');paint('dead');
cv.addEventListener('click',e=>{n.textContent=+n.textContent+1});
</script>"""


class _Page(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def mcp():
    from _protocol import SAFE_ARGS, Client
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client()
    reply = client.call("browser_launch", _timeout=90, url=f"http://127.0.0.1:{srv.server_port}/x/index.html",
                        headless=True, persistent=False, extra_args=SAFE_ARGS)
    assert not reply.failed, reply.text
    yield client
    client.call("close_all_instances", _timeout=40)
    client.close()
    srv.shutdown()


def _n(mcp):
    return int(mcp.call("get_text", selector="#n").text)


@e2e
def test_click_at_image_says_whether_the_page_changed(mcp, tmp_path):
    shot = mcp.call("screenshot", filename="merged-template.png", output_mode="path",
                    region={"x": 190, "y": 35, "width": 110, "height": 50})
    assert not shot.failed, shot.text
    template = shot.text.splitlines()[0]
    try:
        before = _n(mcp)
        hit = mcp.call("click_at_image", template_path=template, threshold=0.95)
        assert not hit.failed, hit.text
        # Two identical buttons: whichever it matched, the result must say what happened.
        changed = _n(mcp) != before
        if changed:
            assert "page changed" in hit.text, hit.text
        else:
            assert "NO page change" in hit.text, hit.text
    finally:
        os.remove(template)


@e2e
def test_extract_structured_returns_absolute_links_and_raw_on_request(mcp):
    rows = json.loads(mcp.call("extract_structured", container_selector=".card", fields=[
        {"name": "link", "selector": ".t", "attribute": "href"},
        {"name": "raw", "selector": ".t", "attribute": "raw:href"}]).text)
    assert rows[0]["link"].endswith("/p/1") and rows[0]["link"].startswith("http://127.0.0.1:")
    assert rows[1]["link"].endswith("/x/p/2")  # relative to the page, as a browser resolves it
    assert [r["raw"] for r in rows] == ["/p/1", "p/2"]


@e2e
def test_the_merged_click_clicks_and_the_old_name_is_gone(mcp):
    start = _n(mcp)
    curved = mcp.call("mouse_click_xy", x=50, y=290, humanize=True)
    assert not curved.failed and "curved approach" in curved.text and "button" in curved.text, curved.text
    by_selector = mcp.call("click", selector="#b", humanize=True)
    assert not by_selector.failed and "page changed" in by_selector.text, by_selector.text
    old = mcp.call("humanize_click", x=50, y=290)
    assert old.failed and "humanize_click" in old.text, old.text
    assert _n(mcp) == start + 2  # the refused old name clicked nothing


@e2e
def test_one_storage_setter_for_both_stores_and_the_old_names_are_gone(mcp):
    assert "it now holds 1 entry" in mcp.call("localstorage_set", key="a", value="1").text
    assert "it now holds 2 entries" in mcp.call("localstorage_set", key="b", value="2").text
    cleared = mcp.call("localstorage_set", clear=True, key="c", value="3")
    assert "cleared and set 'c'" in cleared.text and "1 entry" in cleared.text, cleared.text
    assert json.loads(mcp.call("localstorage_get").text) == {"c": "3"}
    assert "sessionStorage" in mcp.call("localstorage_set", key="s", value="9", area="session").text
    assert json.loads(mcp.call("sessionstorage_get").text) == {"s": "9"}
    assert "0 entries" in mcp.call("localstorage_set", area="session", clear=True).text
    assert "0 entries" in mcp.call("localstorage_set", clear=True).text
    # The old names were removed in 0.25.0.
    for old in ("sessionstorage_set", "sessionstorage_clear", "localstorage_clear"):
        reply = mcp.call(old)
        assert reply.failed and old in reply.text, (old, reply.text)
    # What it refuses changes nothing.
    for bad in ({"area": "cookie", "clear": True}, {}, {"key": "k"}):
        reply = mcp.call("localstorage_set", **bad)
        assert reply.failed and "nothing was changed" in reply.text, (bad, reply.text)


@e2e
def test_fingerprint_rotate_warns_about_another_os_on_a_real_page(mcp):
    other = "Linux x86_64" if sys.platform != "linux" else "Win32"
    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
          if sys.platform != "linux" else
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
    reply = mcp.call("fingerprint_rotate", user_agent=ua, platform=other)
    assert not reply.failed and "WARNING" in reply.text and "GPU" in reply.text, reply.text
    calm = mcp.call("fingerprint_rotate", timezone="Asia/Jakarta")
    assert not calm.failed and "WARNING" not in calm.text, calm.text
