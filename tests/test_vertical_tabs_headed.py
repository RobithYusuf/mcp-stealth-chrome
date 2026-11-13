"""vertical_tabs takes effect in a visible window: the tab strip is where the launch put it.

Opens real windows, so it only runs with MCP_HEADED=1 on macOS, and needs the os-cursor extra
(Quartz, for the window list): `uv run --extra os-cursor pytest ...`. The page is painted one
colour and the window is captured with `screencapture -l <window id>`, which works while the
window sits behind others (as a headed launch opens it). Where the page colour starts and stops
says where the tab strip is. Geometry read from the page cannot say it on Brave: farbling
changes screenX and outerWidth.

Every server gets MCP_STEALTH_DATA_DIR in a temporary folder, and the browser is found by its
--user-data-dir there, so nothing else on the machine is captured or touched.
"""
import functools
import json
import http.server
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import brave, state

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens visible windows")
FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
STRIP = 300  # image pixels; measured 442 (Brave) and 480 (Chrome) on a 2x display


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _browser_pid(profile: pathlib.Path) -> int:
    for pid, command in state.processes_naming(profile) or []:
        if "--type=" not in command:
            return pid
    raise AssertionError(f"no browser runs on {profile}")


def _capture(pid: int, path: pathlib.Path) -> None:
    import Quartz
    windows = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
               if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0]
    assert windows, f"PID {pid} has no window"
    biggest = max(windows, key=lambda w: w["kCGWindowBounds"]["Width"] * w["kCGWindowBounds"]["Height"])
    subprocess.run(["screencapture", "-x", "-o", "-l", str(biggest["kCGWindowNumber"]), str(path)], check=True)


def _page_edges(path: pathlib.Path) -> dict:
    import cv2
    import numpy as np
    image = cv2.imread(str(path))
    # Magenta after the display's colour profile (#ff00ff reads about #ee2df5).
    mask = (image[:, :, 0] > 200) & (image[:, :, 1] < 90) & (image[:, :, 2] > 200)
    ys, xs = np.nonzero(mask)
    assert xs.size, "the page colour is not in the capture"
    height, width = mask.shape
    return {"left": int(xs.min()), "right": int(width - 1 - xs.max()), "top": int(ys.min())}


@pytest.mark.parametrize("browser,mode,side", [
    ("brave", None, None), ("brave", "left", "left"), ("brave", "right", "right"), ("brave", "off", None),
    ("chrome", None, None), ("chrome", "left", "left"),
])
def test_the_tab_strip_is_where_vertical_tabs_put_it(browser, mode, side):
    pytest.importorskip("Quartz")
    if browser == "brave" and brave.find_brave() is None:
        pytest.skip("Brave is not installed")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = pathlib.Path(tempfile.mkdtemp(prefix="mcp-vertical-tabs-"))
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data)})
    try:
        launched = client.call("browser_launch", _timeout=90, headless=False, browser=browser,
                               url=f"http://127.0.0.1:{srv.server_port}/protocol.html",
                               vertical_tabs=mode, auto_verify=False, extra_args=SAFE_ARGS)
        assert not launched.failed, launched.text
        client.evaluate("document.body.innerHTML = ''; document.documentElement.style.background = "
                        "document.body.style.background = '#ff00ff'; 'painted'")
        time.sleep(0.8)
        profile = data / ("brave-profile" if browser == "brave" else "profile")
        shot = data / "window.png"
        _capture(_browser_pid(profile), shot)
        edges = _page_edges(shot)
        print(f"\n{browser} vertical_tabs={mode}: {edges}")
        if side == "left":
            assert edges["left"] > STRIP and edges["right"] < 10, edges
        elif side == "right":
            assert edges["right"] > STRIP and edges["left"] < 10, edges
        else:
            assert edges["left"] < 10 and edges["right"] < 10, edges
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        srv.shutdown()
        for pid, _command in state.processes_naming(data) or []:
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(data, ignore_errors=True)


def _flat(tree: dict, prefix: str = "") -> list:
    keys = []
    for key, value in tree.items():
        keys.extend(_flat(value, f"{prefix}{key}.") if isinstance(value, dict) else [f"{prefix}{key}"])
    return keys


def _side(edges: dict):
    if edges["left"] > STRIP and edges["right"] < 10:
        return "left"
    if edges["right"] > STRIP and edges["left"] < 10:
        return "right"
    return "off" if edges["left"] < 10 and edges["right"] < 10 else f"unclear {edges}"


@pytest.mark.parametrize("browser", ["brave", "chrome"])
def test_a_matured_profile_takes_each_change_and_keeps_it(browser):
    """A profile the browser has opened and closed before carries Secure Preferences with MACs
    over its tracked settings. Each change is written before launch, is what the window shows,
    survives the close, and a launch with vertical_tabs=None keeps the last one."""
    pytest.importorskip("Quartz")
    if browser == "brave" and brave.find_brave() is None:
        pytest.skip("Brave is not installed")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = pathlib.Path(tempfile.mkdtemp(prefix="mcp-vertical-tabs-matured-"))
    profile = data / ("brave-profile" if browser == "brave" else "profile")
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data)})
    steps = ([None, "left", "right", None, "off", None] if browser == "brave"
             else [None, "left", None, "off", None])
    seen = []
    try:
        for step, mode in enumerate(steps):
            launched = client.call("browser_launch", _timeout=90, headless=False, browser=browser,
                                   url=f"http://127.0.0.1:{srv.server_port}/protocol.html",
                                   vertical_tabs=mode, auto_verify=False, extra_args=SAFE_ARGS)
            assert not launched.failed, launched.text
            client.evaluate("document.body.innerHTML = ''; document.documentElement.style.background = "
                            "document.body.style.background = '#ff00ff'; 'painted'")
            time.sleep(0.8)
            shot = data / f"window-{step}.png"
            _capture(_browser_pid(profile), shot)
            side = _side(_page_edges(shot))
            assert not client.call("browser_close", _timeout=40).failed
            stored = brave.vertical_tabs_read_back(profile, [], browser)
            seen.append((mode, side, stored))
            if step == 0:
                secure = profile / "Default" / "Secure Preferences"
                assert secure.exists(), "the first launch made Secure Preferences"
            macs = json.loads((profile / "Default" / "Secure Preferences").read_text()) \
                .get("protection", {}).get("macs", {})
            tracked = sorted(k for k in _flat(macs) if "tab" in k.lower())
            seen[-1] += (tracked,)
        print(f"\n{browser} (requested, shown, stored after close): {seen}")
        expected = {"left": "left", "right": "right", "off": "off"}
        last = "off"
        for mode, side, stored, _tracked in seen[1:]:
            want = expected[mode] if mode else last
            assert side == want, seen
            assert (stored or "off") == want, seen
            last = want
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        srv.shutdown()
        for pid, _command in state.processes_naming(data) or []:
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(data, ignore_errors=True)
