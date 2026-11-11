"""A headed launch really runs from the copy with the project's Dock icon, and still works.

Opens real windows, behind the app in use, so it only runs with MCP_HEADED=1 on macOS. The copy
is built in the folder conftest made for this run, never in ~/.mcp-stealth/app.
"""
import functools
import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import dock_icon

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens visible windows")

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
INSTALLED = pathlib.Path("/Applications/Google Chrome.app")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _headed_test_chromes() -> dict:
    listing = subprocess.run(["ps", "axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    found = {}
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "/Google Chrome " in command and "--type=" not in command \
                and "EXCLUDE 127.0.0.1" in command and "--headless" not in command:
            found[int(pid)] = command.strip()
    return found


def _alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and "Z" not in state


def _bundle_path(pid: int) -> str:
    """Where the Dock thinks this process came from, which is where it takes the icon from."""
    asn = subprocess.run(["lsappinfo", "find", f"pid={pid}"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "bundlepath", asn],
                          capture_output=True, text=True).stdout
    return os.path.realpath(info.split('="', 1)[1].rsplit('"', 1)[0]) if '="' in info else info


def test_headed_browsers_run_from_the_copy_and_work(_dock_icon_copy_in_a_temp_folder):
    copy = pathlib.Path(_dock_icon_copy_in_a_temp_folder).resolve() / dock_icon.APP_NAME
    installed_before = sorted(os.listdir(INSTALLED))
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)  # a free port from the system
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    before = set(_headed_test_chromes())
    client = Client(env={dock_icon.ENV_SWITCH: "1"})
    pids = []
    try:
        launched = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                               headless=False, persistent=False, extra_args=SAFE_ARGS)
        assert not launched.failed, launched.text
        assert "Dock icon" not in launched.text, launched.text  # a fallback would be named here
        ours = {pid: cmd for pid, cmd in _headed_test_chromes().items() if pid not in before}
        assert len(ours) == 1, ours
        pid, command = next(iter(ours.items()))
        pids.append(pid)
        assert pathlib.Path(command.split(" --", 1)[0]).resolve() == copy / "Contents/MacOS/Google Chrome"
        assert _bundle_path(pid) == str(copy)

        # Two apps now share Chrome's bundle id. Links and `open -a "Google Chrome"` must
        # still go to the person's own.
        try:
            import AppKit
        except ImportError:
            AppKit = None
        if AppKit is not None:
            resolved = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(
                "com.google.Chrome")
            assert resolved is not None and os.path.realpath(resolved.path()) == str(INSTALLED)

        # It is the same Chrome: pages load, scripts run, the identity is the real browser's.
        assert client.evaluate("document.title") == "Protocol fixture"
        assert not client.call("navigate", url=f"{base}/keys.html").failed
        assert client.evaluate("location.pathname + ':' + (6 * 7)") == "/keys.html:42"
        audit = json.loads(client.call("fingerprint_audit", _timeout=60).text)
        assert audit["verdict"] == "clean", audit
        version = dock_icon._bundle_version(INSTALLED)[0]
        assert f"Chrome/{version.split('.')[0]}." in client.evaluate("navigator.userAgent")

        # spawn_browser goes through the other launcher.
        spawned = client.call("spawn_browser", _timeout=120, instance_id="docky", url=f"{base}/keys.html",
                              headless=False, persistent=False, extra_args=SAFE_ARGS)
        assert not spawned.failed and "Dock icon" not in spawned.text, spawned.text
        second = {p: c for p, c in _headed_test_chromes().items() if p not in before and p != pid}
        assert len(second) == 1, second
        pids.append(next(iter(second)))
        assert os.path.realpath(next(iter(second.values())).split(" --", 1)[0]).startswith(str(copy) + "/")
        closed_spawn = client.call("close_instance", _timeout=40, instance_id="docky")
        assert not closed_spawn.failed, closed_spawn.text

        closed = client.call("browser_close", _timeout=40)
        assert closed.text.startswith("Browser closed"), closed.text
        assert "could be checked" not in closed.text and "STILL RUNNING" not in closed.text, closed.text
        for ended in pids:
            assert not _alive(ended), f"PID {ended} outlived its close"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        httpd.shutdown()
        for leftover in pids:  # only what this test started
            if _alive(leftover):
                os.kill(leftover, 15)
    assert sorted(os.listdir(INSTALLED)) == installed_before, "the installed Chrome was touched"
