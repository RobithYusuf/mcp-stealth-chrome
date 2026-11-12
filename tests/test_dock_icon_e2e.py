"""A headed launch with MCP_STEALTH_DOCK_ICON=1 really runs from Chrome Beta with the project icon,
and still works. Opens real windows, behind the app in use, so it only runs with MCP_HEADED=1 on
macOS with Google Chrome Beta installed. It sets the icon on the real
/Applications/Google Chrome Beta.app, which is what the switch does, and takes it off once to
show an update's effect is undone. /Applications/Google Chrome.app is only read.
"""
import functools
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from _protocol import DOCK_ICON_ON, SAFE_ARGS, Client
from mcp_stealth_chrome import dock_icon

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_HEADED") or sys.platform != "darwin",
                       reason="opens visible windows"),
    pytest.mark.skipif(dock_icon.beta_binary() is None, reason="Google Chrome Beta is not installed"),
]

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
BETA = dock_icon.BETA_APP
CHROME = dock_icon.CHROME_APP

# The Dock icon of a running process as a PNG, through AppKit from the system's own JXA bridge.
_RUNNING_ICON_JXA = (
    'ObjC.import("AppKit");'
    "function run(argv){"
    "var app=$.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0]));"
    'if(app.isNil())return "noapp";'
    "var tiff=app.icon.TIFFRepresentation;"
    "var rep=$.NSBitmapImageRep.imageRepWithData(tiff);"
    "var png=rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG,$());"
    'return png.writeToFileAtomically(argv[1],true)?"ok":"nowrite";}'
)
_FILE_ICON_JXA = (
    'ObjC.import("AppKit");'
    "function run(argv){"
    "var image=$.NSImage.alloc.initWithContentsOfFile(argv[0]);"
    "var rep=$.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation);"
    "var png=rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG,$());"
    'return png.writeToFileAtomically(argv[1],true)?"ok":"nowrite";}'
)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _headed_test_chromes() -> dict:
    listing = subprocess.run(["ps", "axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    found = {}
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if ("/Google Chrome " in command or "/Google Chrome Beta " in command) \
                and "--type=" not in command and "EXCLUDE 127.0.0.1" in command \
                and "--headless" not in command:
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


def _png(script: str, source: str, folder: str, name: str):
    out = os.path.join(folder, name)
    done = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, source, out],
                          capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "ok", (done.stdout, done.stderr)
    return out


def _difference(a: str, b: str) -> float:
    """Mean absolute difference of two images at 64x64 over RGBA, 0 (same) to 255."""
    import cv2
    import numpy

    def load(path):
        image = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if image.dtype == numpy.uint16:  # AppKit writes 16 bits per channel for some icons
            image = (image / 257).astype(numpy.uint8)
        if image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
        return cv2.resize(image, (64, 64), interpolation=cv2.INTER_AREA).astype(numpy.float32)

    return float(numpy.abs(load(a) - load(b)).mean())


def _dock_icon_of(pid: int, folder: str) -> dict:
    """How close the running process's icon is to the project icon and to Beta's own."""
    running = _png(_RUNNING_ICON_JXA, str(pid), folder, f"running-{pid}.png")
    beta_own = _png(_FILE_ICON_JXA, str(BETA / "Contents/Resources/app.icns"), folder, "beta-own.png")
    return {"to_project": round(_difference(running, str(dock_icon.ICON_PATH)), 1),
            "to_beta_own": round(_difference(running, beta_own), 1)}


def _chrome_untouched() -> tuple:
    names = sorted(os.listdir(CHROME))
    attrs = subprocess.run(["xattr", str(CHROME)], capture_output=True, text=True).stdout
    return names, attrs


def test_headed_launches_run_from_beta_with_the_project_icon(capsys):
    chrome_before = _chrome_untouched()
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    before = set(_headed_test_chromes())
    images = tempfile.mkdtemp(prefix="dock-icon-e2e-")
    client = Client(env=DOCK_ICON_ON)
    pids, measured = [], {}
    try:
        status = client.call("server_status").json()["dock_icon"]
        assert status["state"] in ("beta-ready", "icon-missing"), status

        # An update of Beta removes the icon. The launch after it does not wait, and puts it back.
        (BETA / dock_icon.ICON_FILE).unlink(missing_ok=True)
        assert client.call("server_status").json()["dock_icon"]["state"] == "icon-missing"
        started = time.monotonic()
        first = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                            headless=False, persistent=False, extra_args=SAFE_ARGS)
        measured["launch_after_icon_removed_s"] = round(time.monotonic() - started, 2)
        assert not first.failed, first.text
        assert "[Dock icon: Chrome Beta " in first.text and dock_icon.PENDING_NOTE in first.text
        deadline = time.monotonic() + 15
        while client.call("server_status").json()["dock_icon"]["state"] != "beta-ready":
            assert time.monotonic() < deadline, "the icon did not come back"
            time.sleep(0.25)
        assert dock_icon._icon_current(BETA)
        ours = {p: c for p, c in _headed_test_chromes().items() if p not in before}
        assert len(ours) == 1, ours
        pid, command = next(iter(ours.items()))
        pids.append(pid)
        assert command.startswith(str(BETA) + "/Contents/MacOS/Google Chrome Beta "), command
        assert _bundle_path(pid) == str(BETA)
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")

        # With the icon in place: Beta, its own Dock entry, the project icon, and the same browser.
        for round_ in range(3):
            launched = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                                   headless=False, persistent=False, extra_args=SAFE_ARGS)
            assert not launched.failed, launched.text
            version = dock_icon._bundle_version(BETA)[0]
            assert f"[Dock icon: Chrome Beta {version}]" in launched.text, launched.text
            ours = {p: c for p, c in _headed_test_chromes().items() if p not in before and p not in pids}
            assert len(ours) == 1, ours
            pid = next(iter(ours))
            pids.append(pid)
            assert _bundle_path(pid) == str(BETA)
            time.sleep(1.0)  # the Dock reads the icon as the app registers
            measured[f"icon_round_{round_}"] = _dock_icon_of(pid, images)
            assert measured[f"icon_round_{round_}"]["to_project"] < \
                measured[f"icon_round_{round_}"]["to_beta_own"], measured
            if round_ == 0:
                assert client.evaluate("document.title") == "Protocol fixture"
                assert client.evaluate("location.pathname + ':' + (6 * 7)") == "/protocol.html:42"
                ua = client.evaluate("navigator.userAgent")
                assert f"Chrome/{version.split('.')[0]}." in ua, ua
                brands = json.loads(client.evaluate(
                    "JSON.stringify(navigator.userAgentData.brands.map(b => b.brand))"))
                assert "Google Chrome" in brands, brands
                audit = json.loads(client.call("fingerprint_audit", _timeout=60).text)
                measured["audit"] = {"verdict": audit["verdict"], "warnings": audit.get("warnings")}
                assert audit["verdict"] == "clean", audit
            closed = client.call("browser_close", _timeout=40)
            assert closed.text.startswith("Browser closed"), closed.text
            assert "STILL RUNNING" not in closed.text, closed.text

        # spawn_browser goes through the other launcher.
        spawned = client.call("spawn_browser", _timeout=120, instance_id="docky", url=f"{base}/keys.html",
                              headless=False, persistent=False, extra_args=SAFE_ARGS)
        assert not spawned.failed and "[Dock icon: Chrome Beta " in spawned.text, spawned.text
        ours = {p: c for p, c in _headed_test_chromes().items() if p not in before and p not in pids}
        assert len(ours) == 1, ours
        pids.append(next(iter(ours)))
        assert _bundle_path(pids[-1]) == str(BETA)
        assert not client.call("close_instance", _timeout=40, instance_id="docky").failed

        # Headless keeps Google Chrome.
        headless = client.call("browser_launch", _timeout=120, url=f"{base}/protocol.html",
                               headless=True, persistent=False, extra_args=SAFE_ARGS)
        assert not headless.failed and "Dock icon" not in headless.text, headless.text
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        for ended in pids:
            assert not _alive(ended), f"PID {ended} outlived its close"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        httpd.shutdown()
        for leftover in pids:  # only what this test started
            if _alive(leftover):
                os.kill(leftover, 15)
        with capsys.disabled():
            print(f"\n[dock icon e2e] {json.dumps(measured)}")
    assert _chrome_untouched() == chrome_before, "Google Chrome.app was touched"
    done = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", str(BETA)],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr


def test_with_the_switch_off_a_headed_launch_is_google_chrome():
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    before = set(_headed_test_chromes())
    client = Client(env={"MCP_STEALTH_DOCK_ICON": "0"})
    pids = []
    try:
        assert client.call("server_status").json()["dock_icon"]["state"] == "off"
        launched = client.call("browser_launch", _timeout=120,
                               url=f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html",
                               headless=False, persistent=False, extra_args=SAFE_ARGS)
        assert not launched.failed and "Dock icon" not in launched.text, launched.text
        ours = {p: c for p, c in _headed_test_chromes().items() if p not in before}
        assert len(ours) == 1, ours
        pid, command = next(iter(ours.items()))
        pids.append(pid)
        assert command.startswith(str(CHROME) + "/Contents/MacOS/Google Chrome "), command
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        httpd.shutdown()
        for leftover in pids:
            if _alive(leftover):
                os.kill(leftover, 15)


# Chrome writes its cookie store to disk every 30 seconds; a close sooner than that can lose
# cookies set just before it, so the test waits for the write.
_COOKIE_FLUSH = 32


def _cookie_rows(profile: pathlib.Path) -> list:
    """(name, prefix) of every cookie Chrome stored, read without writing anything."""
    import sqlite3
    from urllib.parse import quote
    database = profile / "Default" / "Cookies"
    with sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True) as db:
        return sorted(db.execute("SELECT name, substr(encrypted_value, 1, 3) FROM cookies").fetchall())


def test_a_profile_with_chrome_cookies_stays_on_google_chrome_and_keeps_them():
    """Chrome Beta 158 deletes the v10 cookies Chrome 157 writes. A profile Chrome has used is
    therefore opened by Google Chrome even with the switch on, and loses nothing; a new one is
    Beta's, stays Beta's, and Google Chrome is refused it rather than let it damage it."""
    from _protocol import LOOPBACK_ONLY
    import shutil

    real_keychain = [LOOPBACK_ONLY]  # cookies as a person's profile holds them, not mock-encrypted
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page = f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html"
    used = pathlib.Path(tempfile.mkdtemp(prefix="dock-used-profile-"))
    fresh = pathlib.Path(tempfile.mkdtemp(prefix="dock-fresh-profile-"))
    before = set(_headed_test_chromes())
    off, on = Client(env={"MCP_STEALTH_DOCK_ICON": "0"}), Client(env=DOCK_ICON_ON)
    try:
        # Google Chrome makes the profile and stores three cookies in it.
        made = off.call("browser_launch", _timeout=120, url=page, headless=True,
                        user_data_dir=str(used), extra_args=real_keychain)
        assert not made.failed, made.text
        off.evaluate("['a','b','c'].forEach(n => document.cookie = n + '=1; max-age=86400; path=/'), 'ok'")
        time.sleep(_COOKIE_FLUSH)
        assert off.call("browser_close", _timeout=40).text.startswith("Browser closed")
        rows = _cookie_rows(used)
        assert [name for name, _ in rows] == ["a", "b", "c"] and {p for _, p in rows} == {b"v10"}, rows

        # The switch is on and the launch is visible, but the profile is Chrome's.
        launched = on.call("browser_launch", _timeout=120, url=page, headless=False,
                           user_data_dir=str(used), extra_args=real_keychain)
        assert not launched.failed, launched.text
        assert "[Dock icon: Google Chrome, because this profile already holds data from Chrome " \
               in launched.text, launched.text
        ours = {p: c for p, c in _headed_test_chromes().items() if p not in before}
        assert len(ours) == 1, ours
        pid, command = next(iter(ours.items()))
        assert command.startswith(str(CHROME) + "/Contents/MacOS/Google Chrome "), command
        assert sorted(on.evaluate("document.cookie").split("; ")) == ["a=1", "b=1", "c=1"]
        assert on.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert _cookie_rows(used) == rows, "the cookies Chrome stored are all still there"

        # A new profile is Beta's, and stays Beta's.
        for round_ in range(2):
            launched = on.call("browser_launch", _timeout=120, url=page, headless=False,
                               user_data_dir=str(fresh), extra_args=real_keychain)
            assert not launched.failed, launched.text
            assert "[Dock icon: Chrome Beta " in launched.text, launched.text
            if round_ == 0:
                on.evaluate("document.cookie = 'beta=1; max-age=86400; path=/', 'ok'")
                time.sleep(_COOKIE_FLUSH)
            else:
                assert "stays on Chrome Beta" in launched.text, launched.text
                assert on.evaluate("document.cookie") == "beta=1"
            ours = {p: c for p, c in _headed_test_chromes().items() if p not in before}
            assert len(ours) == 1 and next(iter(ours.values())).startswith(str(BETA) + "/"), ours
            assert on.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert {p for _, p in _cookie_rows(fresh)} == {b"v11"}

        # Google Chrome is refused that profile rather than allowed to open it.
        snapshot = {p: p.stat().st_mtime_ns for p in fresh.rglob("*") if p.is_file()}
        refused = off.call("browser_launch", _timeout=60, url=page, headless=True,
                           user_data_dir=str(fresh), extra_args=real_keychain)
        assert refused.failed and "refused to open" in refused.text, refused.text
        assert {p: p.stat().st_mtime_ns for p in fresh.rglob("*") if p.is_file()} == snapshot
    finally:
        for client in (off, on):
            client.call("close_all_instances", _timeout=40)
            client.close()
        httpd.shutdown()
        for leftover in set(_headed_test_chromes()) - before:
            os.kill(leftover, 15)
        shutil.rmtree(used, ignore_errors=True)
        shutil.rmtree(fresh, ignore_errors=True)
