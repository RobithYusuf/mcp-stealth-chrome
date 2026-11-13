"""Does a headed Chrome ever become the frontmost app? Opens real windows, so it only runs with
MCP_HEADED=1 on macOS. Counts this browser's PID alone, so the person's own apps do not skew it.
"""
import functools
import http.server
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import brave

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens visible windows")

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _main_chrome_pids() -> set:
    listing = subprocess.run(["ps", "axo", "pid,args"], capture_output=True, text=True).stdout
    return {int(line.split()[0]) for line in listing.splitlines()
            if ("/Google Chrome " in line or "/Google Chrome Beta " in line or "/Brave Browser " in line)
            and "--type=" not in line
            and "EXCLUDE 127.0.0.1" in line and "--headless" not in line}


class _Frontmost:
    def __init__(self):
        self.samples, self._stop, self._cache = [], False, {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _pid(self):
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
        if asn not in self._cache:
            out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True).stdout
            self._cache[asn] = int(out.split("=", 1)[1]) if "=" in out else None
        return self._cache[asn]

    def _run(self):
        while not self._stop:
            self.samples.append((time.monotonic(), self._pid()))
            time.sleep(0.005)

    def ms_of(self, pids, start: int) -> float:
        win = self.samples[start:]
        return sum((b[0] - a[0]) * 1000 for a, b in zip(win, win[1:]) if a[1] in pids)

    def stop(self):
        self._stop = True
        self._thread.join(2)


@pytest.mark.parametrize("browser, block_ads", [("chrome", False), ("brave", False), ("chrome", True)],
                         ids=["chrome", "brave", "chrome-block_ads"])
def test_headed_browser_stays_behind_the_app_in_use(browser, block_ads):
    """block_ads holds every new target until its Fetch domain is on; that must not raise it."""
    if browser == "brave" and brave.find_brave() is None:
        pytest.skip("Brave is not installed")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://localhost:{srv.server_port}"
    before = _main_chrome_pids()
    front = _Frontmost()
    client = Client()
    try:
        client.call("browser_launch", _timeout=60, url=f"{base}/protocol.html", headless=False,
                    persistent=False, extra_args=SAFE_ARGS, browser=browser, block_ads=block_ads)
        ours = _main_chrome_pids() - before
        assert ours
        assert front.ms_of(ours, 0) == 0
        assert client.evaluate("document.visibilityState") == "visible"
        for name, kw in (("navigate", {"url": f"{base}/protocol.html"}), ("click", {"selector": "#inc"}),
                         ("fill", {"selector": "#name", "value": "abc"}), ("hover", {"selector": "#hoverbox"}),
                         ("tab_new", {"url": f"{base}/protocol.html"}), ("tab_select", {"index": 0}),
                         ("set_viewport_size", {"width": 900, "height": 650})):
            start = len(front.samples)
            assert not client.call(name, **kw).failed, name
            time.sleep(0.3)
            assert front.ms_of(ours, start) == 0, name
        # window.open activates Chrome and nothing prevents it: it must come straight back.
        start = len(front.samples)
        client.call("click", selector="#popup")
        time.sleep(1.5)
        assert front.ms_of(ours, start) < 500
        assert front.samples[-1][1] not in ours
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        front.stop()
        srv.shutdown()
    time.sleep(1)
    assert not (_main_chrome_pids() & ours), "the browser outlived its close"
