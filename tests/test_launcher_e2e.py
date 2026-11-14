"""The Dock launcher on real macOS with Brave Beta: a cold click, a warm click with a link, two opens
at once, a later server reattaching, Quit, and a cold click after a crash whose lock now names a
reused PID (what a restart leaves).

Needs MCP_E2E=1, MCP_HEADED=1, macOS, Brave and Brave Beta installed, and an unlocked screen. Every
browser runs with --use-mock-keychain: Brave Beta is not on the ACL of the person's "Brave Safe
Storage" Keychain item, and the real Keychain asks them for their password. Data, records, watcher
files and the app live in one temporary folder; the app is opened with `open -g` and the launcher
does not activate the browser (MCP_STEALTH_LAUNCHER_ACTIVATE=0), so no window takes the focus.
Every signal goes to a process whose command line names that folder.
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from _protocol import LOOPBACK_ONLY, Client
from mcp_stealth_chrome import brave_beta, launcher, state

BETA_EXE = str(brave_beta.BETA_APP / brave_beta.BETA_BINARY)
BRAVE_APP = pathlib.Path("/Applications/Brave Browser.app")
YEAR_SECONDS = 24 * 365 * 3600


def _screen_locked() -> bool:
    out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, text=True).stdout
    at = out.find("CGSSessionScreenIsLocked")
    return at >= 0 and "<true/>" in out[at:at + 80]


pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E") or not os.environ.get("MCP_HEADED"),
                       reason="opens visible browsers: MCP_E2E=1 MCP_HEADED=1"),
    pytest.mark.skipif(sys.platform != "darwin", reason="the Dock launcher is macOS only"),
    pytest.mark.skipif(not os.path.isfile(BETA_EXE) or not BRAVE_APP.is_dir(),
                       reason="needs Brave and Brave Beta in /Applications"),
    pytest.mark.skipif(sys.platform == "darwin" and _screen_locked(), reason="the screen is locked"),
]

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _bundle_root_state(app: pathlib.Path) -> tuple:
    names = sorted(p.name for p in app.iterdir())
    xattrs = subprocess.run(["xattr", str(app)], capture_output=True, text=True).stdout
    return names, xattrs


@pytest.fixture(scope="module")
def world():
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-launch-", dir="/tmp"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env = {"MCP_STEALTH_DATA_DIR": str(root / "data"), "MCP_STEALTH_KEEP_ALIVE_DIR": str(root / "keep-alive"),
           "MCP_STEALTH_QUIT_WATCH_DIR": str(root / "quit-watch"), "MCP_STEALTH_TABS_DIR": str(root / "tabs"),
           launcher.ENV_EXTRA_ARGS: json.dumps(["--use-mock-keychain", LOOPBACK_ONLY]),
           launcher.ENV_ACTIVATE: "0"}
    (root / "quit-watch").mkdir(mode=0o700)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    brave_before = _bundle_root_state(BRAVE_APP)
    cli = str(pathlib.Path(sys.executable).parent / "mcp-stealth-chrome")
    app = launcher.build_app("brave", root / "apps", cli=cli)
    launcher.acknowledge("brave")
    ns = type("World", (), {})()
    ns.root, ns.env, ns.app, ns.cli = root, env, app, cli
    ns.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ns.profile = root / "data" / "brave-profile"
    try:
        yield ns
    finally:
        httpd.shutdown()
        for pid, _command in state.processes_naming(root) or []:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        launcher.remove_app("brave", root / "apps")
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        assert _bundle_root_state(BRAVE_APP) == brave_before, "the person's Brave was changed"
        shutil.rmtree(root, ignore_errors=True)


def _record(world) -> dict:
    files = list((world.root / "keep-alive").glob("*.json"))
    return json.loads(files[0].read_text()) if files else {}


def _wait(predicate, within: float = 60.0, step: float = 0.2):
    deadline = time.time() + within
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def _mains(world) -> list:
    return [(pid, c) for pid, c in state.processes_naming(world.profile) or [] if launcher._is_main(c)]


def _pages(port: int) -> list:
    body = launcher._watcher.http_get("127.0.0.1", port, "/json/list") or b"[]"
    return [t["url"] for t in json.loads(body) if t.get("type") == "page"]


def _log_lines(world) -> list:
    path = world.root / "data" / "launcher.log"
    return path.read_text().splitlines() if path.exists() else []


def _applet_running(world) -> bool:
    out = subprocess.run(["pgrep", "-f", str(world.app / "Contents" / "MacOS")],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def _click(world, *urls: str) -> None:
    """What a Dock click (no URL) or a link from another app (a URL) sends: `open -a`, kept in
    the background so it takes no focus."""
    subprocess.run(["open", "-g", "-a", str(world.app), *urls], check=True, timeout=30)


def _sample_dock(seconds: float, out: list) -> threading.Thread:
    def run():
        end = time.time() + seconds
        while time.time() < end:
            listing = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True).stdout
            bundle = None
            for line in listing.splitlines():
                if "bundleID=" in line:
                    bundle = line.split("bundleID=", 1)[1].strip().strip('"')
                elif " type=" in line and bundle in (launcher.BUNDLE_IDS["brave"], "com.brave.Browser.beta"):
                    kind = line.split(" type=", 1)[1].split()[0].strip('"')
                    pid = int(line.split("pid = ", 1)[1].split()[0])
                    out.append((bundle, kind, pid))
                    bundle = None
            time.sleep(0.05)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _step_1_a_cold_click_starts_the_agent_brave_and_only_it_shows_in_the_dock(world):
    seen: list = []
    sampler = _sample_dock(12, seen)
    started = time.monotonic()
    _click(world)
    record = _wait(lambda: _record(world).get("state") == "kept" and _record(world))
    took = time.monotonic() - started
    sampler.join()
    assert record, _log_lines(world)
    assert record["opened_by"] == "launcher" and record["max_hours"] == 24 * 365
    mains = _mains(world)
    assert [pid for pid, _c in mains] == [record["pid"]]
    assert mains[0][1].startswith(BETA_EXE + " "), mains[0][1]
    launcher_kinds = {kind for bundle, kind, _pid in seen if bundle == launcher.BUNDLE_IDS["brave"]}
    beta_kinds = {kind for bundle, kind, pid in seen if bundle == "com.brave.Browser.beta" and pid == record["pid"]}
    print(f"\ncold click: kept after {took:.1f} s; launcher seen as {launcher_kinds}, Brave Beta as {beta_kinds}")
    assert launcher_kinds <= {"UIElement"}, "the launcher must not show a Dock icon of its own"
    assert beta_kinds == {"Foreground"}
    assert subprocess.run(["codesign", "--verify", "--deep", str(brave_beta.BETA_APP)]).returncode == 0
    world.pid, world.port = record["pid"], record["port"]
    # The applet finishes its own click before the next one: a link sent while it was still
    # running that click was lost now and then (measured: 1 run in 4), see the report.
    assert _wait(lambda: not _applet_running(world), 60), "the launcher app did not exit"


def _step_2_a_link_opens_in_a_new_tab_of_the_running_browser(world):
    url = f"{world.base}/protocol.html?link=1"
    _click(world, url)
    assert _wait(lambda: url in _pages(world.port), 30), (_pages(world.port), _log_lines(world)[-2:])
    assert [pid for pid, _c in _mains(world)] == [world.pid]
    assert "already running" in _log_lines(world)[-1]


def _step_3_two_opens_at_once_start_no_second_browser(world):
    urls = [f"{world.base}/protocol.html?twin={i}" for i in (1, 2)]
    procs = [subprocess.Popen([world.cli, "open", "--browser", "brave", "--dock-icon", "--url", u],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for u in urls]
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), results
    assert [pid for pid, _c in _mains(world)] == [world.pid]
    assert _wait(lambda: all(u in _pages(world.port) for u in urls), 20)


def _step_4_a_later_server_reattaches_and_the_cap_stays_the_persons(world):
    client = Client(env=dict(world.env, MCP_STEALTH_DOCK_ICON="0"))
    try:
        reply = client.call("browser_launch", _timeout=90, url=f"{world.base}/protocol.html?agent=1",
                            browser="brave", keep_alive=True)
        assert not reply.failed and reply.text.startswith("Reattached"), reply.text
        assert f"pid {world.pid}" in reply.text
    finally:
        client.close()
    record = _wait(lambda: _record(world).get("state") == "kept" and _record(world), 20)
    assert record["pid"] == world.pid and record["opened_by"] == "launcher"
    watcher = launcher._watcher.process_command(record["watcher_pid"]) or ""
    assert f'"max_orphan_seconds": {float(YEAR_SECONDS)}' in watcher, watcher[:400]


def _step_5_quit_ends_it_and_its_record(world):
    script = ('ObjC.import("AppKit");function run(a){var x=$.NSRunningApplication.'
              'runningApplicationWithProcessIdentifier(parseInt(a[0]));return x.isNil()?"none":(x.terminate?"sent":"no")}')
    assert subprocess.run(["osascript", "-l", "JavaScript", "-e", script, str(world.pid)],
                          capture_output=True, text=True).stdout.strip() == "sent"
    assert _wait(lambda: not _mains(world), 20), _mains(world)
    assert _wait(lambda: not _record(world), 10), _record(world)


def test_1_to_5_cold_click_link_twins_reattach_quit(world):
    """One test: between the steps the browser runs with no starter (the click's process has
    exited, as it does for a person), which the suite's leftover net ends after every test."""
    _step_1_a_cold_click_starts_the_agent_brave_and_only_it_shows_in_the_dock(world)
    _step_2_a_link_opens_in_a_new_tab_of_the_running_browser(world)
    _step_3_two_opens_at_once_start_no_second_browser(world)
    _step_4_a_later_server_reattaches_and_the_cap_stays_the_persons(world)
    _step_5_quit_ends_it_and_its_record(world)


def test_6_a_cold_click_after_a_crash_with_a_reused_pid_in_the_lock(world):
    url = f"{world.base}/protocol.html?after=crash"
    done = subprocess.run([world.cli, "open", "--browser", "brave", "--dock-icon"], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    record = _record(world)
    # What a restart leaves: no browser and no watcher, the record and the lock still there.
    for pid in (record["watcher_pid"], record["pid"]):
        assert str(world.root) in (launcher._watcher.process_command(pid) or "")
        os.kill(pid, signal.SIGKILL)
    assert _wait(lambda: not _mains(world), 20)
    stand_in = subprocess.Popen(["/bin/sleep", "300"])
    try:
        lock = world.profile / "SingletonLock"
        if lock.is_symlink():
            lock.unlink()
        lock.symlink_to(f"{socket.gethostname()}-{stand_in.pid}")
        assert state.chrome_lock_state(world.profile) == ("held", stand_in.pid)
        _click(world, url)
        record = _wait(lambda: (_record(world).get("state") == "kept"
                                and _record(world).get("pid") != record["pid"] and _record(world)), 60)
        assert record, _log_lines(world)[-3:]
        assert _wait(lambda: url in _pages(record["port"]), 20)
        assert stand_in.poll() is None, "the process the stale lock named was left alone"
        assert [pid for pid, _c in _mains(world)] == [record["pid"]], _mains(world)
    finally:
        stand_in.terminate()
    script = ('ObjC.import("AppKit");function run(a){var x=$.NSRunningApplication.'
              'runningApplicationWithProcessIdentifier(parseInt(a[0]));return x.isNil()?"none":(x.terminate?"sent":"no")}')
    subprocess.run(["osascript", "-l", "JavaScript", "-e", script, str(record["pid"])], capture_output=True)
    assert _wait(lambda: not _mains(world), 20)
