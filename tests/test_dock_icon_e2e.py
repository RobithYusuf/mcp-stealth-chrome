"""A headed launch really runs from the copy with the project's Dock icon, and still works.

Opens real windows, behind the app in use, so it only runs with MCP_HEADED=1 on macOS. The copy
is built in a folder made for this test, never in ~/.mcp-stealth/app.
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

from _protocol import DOCK_COPY_ON, SAFE_ARGS, Client, wait_for_dock_copy
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


def test_the_first_launch_does_not_wait_and_later_ones_run_from_the_copy(capsys):
    """A server that starts with no copy builds one in the background. A launch arriving
    meanwhile gets Chrome itself at once and says so; once the copy is ready, launches use it."""
    import shutil
    import tempfile

    # Empty, on Chrome's volume, and under the home folder: LaunchServices does not list an
    # app that lives in a temp folder, and whether the copy is listed is part of this test.
    folder = tempfile.mkdtemp(prefix=".mcp-stealth-app-e2e-", dir=pathlib.Path.home())
    copy = pathlib.Path(folder).resolve() / dock_icon.APP_NAME
    installed_before = sorted(os.listdir(INSTALLED))
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)  # a free port from the system
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    before = set(_headed_test_chromes())
    client = Client(env={**DOCK_COPY_ON, dock_icon.ENV_APP_DIR: folder})
    server_started = time.monotonic()
    pids = []
    try:
        # Nothing was launched yet: the build was started by the server starting.
        assert client.call("server_status").json()["dock_icon"]["state"] == "building"

        started = time.monotonic()
        first = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                            headless=False, persistent=False, extra_args=SAFE_ARGS)
        first_took = time.monotonic() - started
        assert not first.failed, first.text
        assert "[Dock icon: Chrome's own this time, the copy is still being prepared]" in first.text
        ours = {pid: cmd for pid, cmd in _headed_test_chromes().items() if pid not in before}
        assert len(ours) == 1, ours
        own_pid, command = next(iter(ours.items()))
        pids.append(own_pid)
        assert command.startswith(str(INSTALLED) + "/"), command
        assert client.call("server_status").json()["dock_icon"]["state"] == "building", \
            "the launch finished before the copy did, so it cannot have waited for it"
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")

        settled = wait_for_dock_copy(client)
        build_took = time.monotonic() - server_started
        assert settled["state"] == "ready", settled

        started = time.monotonic()
        launched = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                               headless=False, persistent=False, extra_args=SAFE_ARGS)
        second_took = time.monotonic() - started
        assert not launched.failed, launched.text
        assert "Dock icon" not in launched.text, launched.text  # a fallback would be named here
        ours = {pid: cmd for pid, cmd in _headed_test_chromes().items() if pid not in before}
        assert len(ours) == 1, ours
        pid, command = next(iter(ours.items()))
        pids.append(pid)
        assert pathlib.Path(command.split(" --", 1)[0]).resolve() == copy / "Contents/MacOS/Google Chrome"
        assert _bundle_path(pid) == str(copy)
        excluded = subprocess.run(["tmutil", "isexcluded", str(copy)], capture_output=True, text=True).stdout
        assert excluded.startswith("[Excluded]"), excluded  # a home folder is backed up unless marked
        with capsys.disabled():
            print(f"\n[dock icon e2e] first launch (Chrome itself, copy building): {first_took:.1f}s; "
                  f"copy ready {build_took:.1f}s after the server started; "
                  f"next launch (from the copy): {second_took:.1f}s")

        # Two apps now share Chrome's bundle id. Links and `open -a "Google Chrome"` must
        # still go to the person's own.
        try:
            import AppKit
        except ImportError:
            AppKit = None

        def listed_for_chrome() -> bool:
            urls = AppKit.NSWorkspace.sharedWorkspace().URLsForApplicationsWithBundleIdentifier_(
                "com.google.Chrome")
            return str(copy) in [os.path.realpath(url.path()) for url in urls]

        if AppKit is not None:
            resolved = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(
                "com.google.Chrome")
            assert resolved is not None and os.path.realpath(resolved.path()) == str(INSTALLED)
            assert listed_for_chrome(), "running registers the copy; if not, the check below proves nothing"

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

        # With nothing running from it, the copy is no longer listed beside the person's Chrome,
        # and that does not get in the way of the next launch or of where the Dock looks.
        if AppKit is not None:
            deadline = time.monotonic() + 15
            while listed_for_chrome() and time.monotonic() < deadline:
                time.sleep(0.25)
            assert not listed_for_chrome(), "the copy is still registered after its browsers closed"
        again = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                            headless=False, persistent=False, extra_args=SAFE_ARGS)
        assert not again.failed and "Dock icon" not in again.text, again.text
        third = {p: c for p, c in _headed_test_chromes().items() if p not in before and p not in pids}
        assert len(third) == 1, third
        pids.append(next(iter(third)))
        assert _bundle_path(pids[-1]) == str(copy)
        assert client.evaluate("document.title") == "Protocol fixture"
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        httpd.shutdown()
        for leftover in pids:  # only what this test started
            if _alive(leftover):
                os.kill(leftover, 15)
        shutil.rmtree(folder, ignore_errors=True)
    assert sorted(os.listdir(INSTALLED)) == installed_before, "the installed Chrome was touched"
