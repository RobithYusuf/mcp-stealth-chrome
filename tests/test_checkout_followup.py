"""Five gaps found by driving a real hosted-fields checkout with 0.23.0.

The unit tests run always. The browser tests need MCP_E2E=1: headless, an ephemeral profile,
loopback only, with a card frame served from another site as a payment page's frames are.
"""
import http.server
import json
import os
import threading
import urllib.parse

import pytest

from mcp_stealth_chrome import helpers, snapshot

LONG = "eyJ" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 40


def test_a_long_url_in_a_sentence_is_cut_and_says_how_to_get_it_whole():
    url = "https://pay.test/hosted-card-fields/abc/card-number?config=" + LONG
    shown = helpers.short_url(url)
    assert shown.startswith(url[:200]) and len(shown) < 280
    assert f"{len(url) - 200} more chars" in shown and "list_frames(max_url_len=0)" in shown
    assert helpers.short_url("https://a.test/x") == "https://a.test/x"
    assert helpers.short_url(None) == ""


def test_sibling_frames_of_one_widget_are_told_apart_on_the_frames_line():
    base = "https://api-services.test.pxp.io/sessions/api/v1/hosted-card-fields/acfe2541-9f08/"
    number = snapshot._frame_hint(base + "card-number?fieldName=card-number&config=" + LONG)
    cvc = snapshot._frame_hint(base + "card-cvc?fieldName=card-cvc&config=" + LONG)
    assert number != cvc
    assert number.endswith("card-number") and cvc.endswith("card-cvc")
    assert number.startswith("api-services.test.pxp.io/")
    assert snapshot._frame_hint("https://a.test/short") == "https://a.test/short"
    # No path to tell by: the plain cut, never an empty hint.
    assert snapshot._frame_hint("https://a.test/?" + LONG) == ("https://a.test/?" + LONG)[:60]


class _WindowTab:
    """Answers the three Browser.* window commands the way Chrome does, and keeps what was set."""

    def __init__(self):
        self.bounds = {"left": 0, "top": 0, "width": 500, "height": 400}
        from nodriver.cdp import target as cdp_target
        self.target = type("T", (), {"target_id": cdp_target.TargetID("T1")})()

    async def send(self, command):
        asked = next(command)
        if asked["method"] == "Browser.setWindowBounds":
            self.bounds = {**self.bounds, **asked["params"]["bounds"]}
            reply = {}
        elif asked["method"] == "Browser.getWindowForTarget":
            reply = {"windowId": 7, "bounds": dict(self.bounds)}
        else:
            reply = {"bounds": dict(self.bounds)}
        try:
            command.send(reply)
        except StopIteration as done:
            return done.value


def _restore(tmp_path, monkeypatch, size):
    import asyncio

    from mcp_stealth_chrome import server
    saved = tmp_path / "main.json"
    saved.write_text(json.dumps({"left": 100, "top": 120, "width": 1280, "height": 800,
                                 "window_state": "normal"}))
    monkeypatch.setattr(server, "_window_state_path", lambda _id: saved)
    tab = _WindowTab()
    result = asyncio.run(server._restore_window_bounds("main", tab, size=size))
    return result, tab.bounds


def test_a_window_size_asked_for_on_this_launch_is_not_undone_by_the_saved_one(tmp_path, monkeypatch):
    result, bounds = _restore(tmp_path, monkeypatch, (1400, 950))
    assert result["restored"], result
    # The saved position is kept, the size is the caller's.
    assert (bounds["left"], bounds["top"]) == (100, 120)
    assert (bounds["width"], bounds["height"]) == (1400, 950), bounds
    assert result["size_from"] == "this launch"


def test_without_a_size_on_the_launch_the_saved_size_comes_back(tmp_path, monkeypatch):
    result, bounds = _restore(tmp_path, monkeypatch, None)
    assert result["restored"] and "size_from" not in result
    assert (bounds["width"], bounds["height"]) == (1280, 800), bounds


# ── real Chrome ────────────────────────────────────────────────────────────

e2e = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")


def _pages(cross: str) -> dict:
    return {
        "/plain.html": "<!doctype html><title>Plain</title><p>hello</p><button>Go</button>",
        # What a single-page app looks like before its bundle has run: a quiet, empty DOM.
        "/late.html": ("<!doctype html><title>Late</title><div id=root></div><script>"
                       "setTimeout(() => { document.getElementById('root').innerHTML ="
                       " '<h1>Shop</h1><button>Buy</button>'; }, 4000);</script>"),
        "/pay.html": ("<!doctype html><title>Pay</title><body style='margin:0'>"
                      "<button id=pay>Pay now</button>"
                      f'<iframe id="closed" style="width:300px;height:60px;border:0" '
                      f'src="{cross}/hosted-card-fields/abc/card-number?config={LONG}"></iframe>'
                      f'<iframe id="open" style="width:300px;height:60px;border:0" '
                      f'src="{cross}/hosted-card-fields/abc/card-holder-name?config={LONG}"></iframe>'
                      "</body>"),
        "/hosted-card-fields/abc/card-number": (
            "<!doctype html><title>Card</title><body style='margin:0'><div id=host></div><script>"
            "const r = document.getElementById('host').attachShadow({mode: 'closed'});"
            "r.innerHTML = '<input name=card style=\"width:280px;height:40px\">';</script></body>"),
        "/hosted-card-fields/abc/card-holder-name": (
            "<!doctype html><title>Name</title><body style='margin:0'>"
            "<input name=holder style='width:280px;height:40px'></body>"),
    }


class _Fixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        page = _pages(f"http://127.0.0.1:{self.server.server_port}").get(path)
        data = (page or "").encode()
        self.send_response(200 if page is not None else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def mcp(base):
    from _protocol import SAFE_ARGS, Client
    client = Client()
    reply = client.call("browser_launch", _timeout=90, url=f"{base}/plain.html", headless=True,
                        persistent=False, extra_args=SAFE_ARGS)
    assert not reply.failed, reply.text
    yield client
    client.call("close_all_instances", _timeout=40)
    client.close()


@e2e
def test_an_app_that_has_not_rendered_is_not_reported_as_an_empty_page(mcp, base):
    assert not mcp.call("navigate", url=f"{base}/late.html").failed
    summary = json.loads(mcp.call("describe_page", wait_stable=True, max_wait=1.5,
                                  stable_ms=300).text)
    assert "nothing is rendered yet" in summary.get("note", ""), summary
    assert "wait_for" in summary["note"]
    snap = mcp.call("browser_snapshot").text
    assert "Nothing matched on this page at all" in snap and "wait_for" in snap, snap
    assert not mcp.call("wait_for", text="Shop", timeout=10).failed
    summary = json.loads(mcp.call("describe_page").text)
    assert "note" not in summary, summary
    assert "Nothing matched" not in mcp.call("browser_snapshot", diff_from_last=False).text


@e2e
def test_a_page_with_content_gets_no_empty_note(mcp, base):
    assert not mcp.call("navigate", url=f"{base}/plain.html").failed
    assert "note" not in json.loads(mcp.call("describe_page").text)
    assert "Nothing matched" not in mcp.call("browser_snapshot", diff_from_last=False).text


@e2e
def test_a_frame_whose_field_sits_in_a_closed_shadow_root_is_named(mcp, base):
    assert not mcp.call("navigate", url=f"{base}/pay.html").failed
    assert not mcp.call("wait_for", frame_url="card-holder-name", timeout=10).failed
    snap = mcp.call("browser_snapshot", diff_from_last=False).text
    frames = next(line for line in snap.splitlines() if line.startswith("Frames:"))
    assert "card-number" in frames and "card-holder-name" in frames, frames
    blank = next((line for line in snap.splitlines()
                  if line.startswith("Frames with nothing readable")), "")
    assert "closed shadow root" in blank and "mouse_click_xy" in blank, snap
    # Only the frame that really gave nothing is named: the open one's input has a ref.
    named = blank.split(":", 1)[1].split(".", 1)[0]
    assert "f1" in named and "f2" not in named, blank
    assert 'frame=f2' in snap, snap


@e2e
def test_frame_tools_do_not_print_a_thousand_character_url(mcp, base):
    assert not mcp.call("navigate", url=f"{base}/pay.html").failed
    waited = mcp.call("wait_for", frame_url="card-number", timeout=10)
    assert not waited.failed, waited.text
    assert "more chars; list_frames(max_url_len=0)" in waited.text and LONG not in waited.text
    ran = mcp.call("frame_evaluate", frame_url_pattern="card-number", expression="1 + 1")
    assert not ran.failed, ran.text
    assert ran.text.startswith("2") and LONG not in ran.text, ran.text
    assert "more chars; list_frames(max_url_len=0)" in ran.text
    # The whole URL is still there for whoever asks.
    assert LONG in mcp.call("list_frames", max_url_len=0).text
