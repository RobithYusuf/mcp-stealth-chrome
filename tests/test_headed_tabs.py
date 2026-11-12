"""tab_new in its own window and in the same window, on a visible Chrome. Opens real windows, so it
only runs with MCP_HEADED=1 on macOS. Counts this browser's PID alone, as test_headed_focus does.
"""
import functools
import http.server
import json
import os
import sys
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from test_headed_focus import FIXTURES, _Frontmost, _main_chrome_pids, _Quiet

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens visible windows")

# What the page itself sees: visibility, and whether requestAnimationFrame runs.
PAGE = ("new Promise(done => { let n = 0; const tick = () => { n++; if (n < 200) requestAnimationFrame(tick); };"
        " requestAnimationFrame(tick); setTimeout(() => done(JSON.stringify("
        "{state: document.visibilityState, hidden: document.hidden, frames: n})), 300); })")


def _claim(text):
    if "reads as visible" in text:
        return "visible"
    if "reads as hidden" in text:
        return "hidden"
    return None


def _page(client):
    return json.loads(client.evaluate(PAGE))


@pytest.fixture(scope="module")
def headed():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://localhost:{srv.server_port}"
    before = _main_chrome_pids()
    front = _Frontmost()
    client = Client()
    reply = client.call("browser_launch", _timeout=60, url=f"{base}/protocol.html", headless=False,
                        persistent=False, extra_args=SAFE_ARGS)
    assert not reply.failed, reply.text
    ours = _main_chrome_pids() - before
    assert ours
    try:
        yield client, base, front, ours
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        front.stop()
        srv.shutdown()
        time.sleep(1)
        assert not (_main_chrome_pids() & ours), "the browser outlived its close"


def _tabs(client):
    reply = client.call("tab_list")
    assert not reply.failed, reply.text
    return sum(1 for line in reply.text.splitlines() if line[1:2] == "[")


def test_a_tab_in_the_same_window_stays_behind_and_reports_what_the_page_reads(headed):
    """A tab in the same window used to be either in front (Chrome took the keyboard) or behind
    and hidden from its page. Focus emulation keeps it painting without activating anything."""
    client, base, front, ours = headed
    count = _tabs(client)
    start = len(front.samples)
    reply = client.call("tab_new", url=f"{base}/protocol.html", new_window=False)
    assert not reply.failed and "in the same window" in reply.text, reply.text
    time.sleep(0.3)
    assert front.ms_of(ours, start) == 0
    seen = _page(client)
    assert _claim(reply.text) == seen["state"], (reply.text, seen)
    assert seen == {"state": "visible", "hidden": False, "frames": seen["frames"]} and seen["frames"] > 5, seen
    assert _tabs(client) == count + 1
    # The emulation belongs to the tab's session, so a later navigation keeps it.
    assert not client.call("navigate", url=f"{base}/old_tools.html").failed
    assert _page(client)["state"] == "visible"
    assert front.ms_of(ours, start) == 0


def test_selecting_tabs_in_a_shared_window_neither_takes_focus_nor_hides_a_page(headed):
    client, _base, front, ours = headed
    start = len(front.samples)
    for index in (0, _tabs(client) - 1, 0):
        reply = client.call("tab_select", index=index)
        assert not reply.failed and "hidden" not in reply.text, reply.text
        assert _page(client)["state"] == "visible"
    time.sleep(0.3)
    assert front.ms_of(ours, start) == 0


def test_the_default_still_opens_its_own_window_behind(headed):
    client, base, front, ours = headed
    count = _tabs(client)
    start = len(front.samples)
    reply = client.call("tab_new", url=f"{base}/protocol.html")
    assert not reply.failed and "in its own window, behind the app in use" in reply.text, reply.text
    time.sleep(0.3)
    assert front.ms_of(ours, start) == 0
    assert _page(client)["state"] == "visible"
    assert _tabs(client) == count + 1


def test_closing_tabs_of_a_shared_window_keeps_the_browser_and_the_focus(headed):
    """The quit watcher counts pages. Closing tabs one by one, the first one included, must not
    read as the person quitting, and Chrome showing the next tab must not come to the front."""
    client, base, front, ours = headed
    client.call("tab_select", index=0)
    assert not client.call("tab_new", url=f"{base}/old_tools.html", new_window=False).failed
    count = _tabs(client)
    start = len(front.samples)
    assert not client.call("tab_close", index=0).failed
    assert not client.call("tab_close").failed
    assert _tabs(client) == count - 2
    time.sleep(5)  # past the quit watcher's grace
    assert client.call("server_status").json()["browser_running"] is True
    assert front.ms_of(ours, start) == 0
