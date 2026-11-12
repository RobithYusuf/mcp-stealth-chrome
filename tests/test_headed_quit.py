"""Quit on a headed browser ends it. Opens a real window, so it only runs with MCP_HEADED=1 on macOS.

Chrome started with --no-startup-window, which keeps a headed launch from taking the keyboard,
closes its windows on Quit and keeps running, with its Dock icon. A detached watcher per browser
ends it after the grace, whether the server that launched it is alive, gone, or running code
whose package was replaced on disk.

Every signal this file sends goes to a process it started itself: its own server (found as a
child of this test process) or the Chrome that server launched for it (found by a command line
carrying this suite's flags, new since the test began).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _protocol import REPO, SAFE_ARGS, Client

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens a visible window")

GRACE = float(os.environ.get("MCP_WINDOWLESS_CLOSE_SECONDS", "3"))
_RESULTS = os.environ.get("MCP_QUIT_RESULTS")  # a JSON-lines file the report reads


def _record(**row):
    if _RESULTS:
        with open(_RESULTS, "a") as out:
            out.write(json.dumps(row) + "\n")


def _ps() -> list[tuple[int, int, str]]:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True,
                             text=True).stdout
    rows = []
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _test_chrome_pids() -> set:
    return {pid for pid, _ppid, cmd in _ps()
            if ("/Google Chrome " in cmd or "/Google Chrome Beta " in cmd) and "--type=" not in cmd
            and "EXCLUDE 127.0.0.1" in cmd and "--headless" not in cmd}


def _command(pid: int) -> str:
    return next((cmd for p, _pp, cmd in _ps() if p == pid), "")


def _profile_of(pid: int) -> str:
    return next(arg.split("=", 1)[1] for arg in _command(pid).split(" --")
                if arg.startswith("user-data-dir="))


def _server_pids() -> set:
    me = os.getpid()
    return {pid for pid, ppid, cmd in _ps()
            if ppid == me and "mcp_stealth_chrome.server import main" in cmd}


def _watcher_pids(chrome_pid: int) -> set:
    return {pid for pid, _pp, cmd in _ps()
            if "mcp-stealth-quit-watcher" in cmd and f'"pid": {chrome_pid},' in cmd}


def _alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and "Z" not in state


def _start(client: Client, instance: str = "main") -> int:
    before = _test_chrome_pids()
    if instance == "main":
        launched = client.call("browser_launch", _timeout=60, url="about:blank", headless=False,
                               persistent=False, extra_args=SAFE_ARGS)
    else:
        launched = client.call("spawn_browser", _timeout=60, instance_id=instance, url="about:blank",
                               headless=False, privacy=True, extra_args=SAFE_ARGS)
    assert not launched.failed, launched.text
    ours = _test_chrome_pids() - before
    assert len(ours) == 1, ours
    pid = ours.pop()
    deadline = time.time() + 5
    while not _watcher_pids(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert _watcher_pids(pid), "no quit watcher was started for the headed browser"
    return pid


def _quit_and_time(pid: int) -> tuple[float, bool]:
    """Quit as Cmd+Q and the Dock's Quit do. Returns (seconds until the process ended, whether
    macOS still lists the app afterwards)."""
    import AppKit
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    assert app is not None
    started = time.monotonic()
    assert app.terminate()
    while time.monotonic() - started < 20 and _alive(pid):
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    AppKit.NSRunLoop.currentRunLoop().runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.5))
    listed = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) is not None
    return elapsed, listed


def _assert_quit_ended(pid: int, case: str) -> float:
    profile = _profile_of(pid)
    exe = _command(pid).split(" --", 1)[0]
    elapsed, listed = _quit_and_time(pid)
    _record(case=case, chrome_pid=pid, seconds=round(elapsed, 2), app_listed_after=listed,
            limit=GRACE + 2, executable=exe)
    assert not _alive(pid), f"{case}: Chrome still running after Quit"
    assert elapsed <= GRACE + 2, f"{case}: took {elapsed:.2f} s"
    assert not listed, f"{case}: macOS still lists the app (Dock icon)"
    deadline = time.time() + 3
    while _watcher_pids(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _watcher_pids(pid), f"{case}: the watcher outlived its browser"
    # Nothing starts a browser on that profile again on its own.
    time.sleep(8)
    again = [p for p, _pp, cmd in _ps() if f"--user-data-dir={profile}" in cmd]
    _record(case=case + ": relaunched within 8 s", pids=again)
    assert not again, f"{case}: something started Chrome on {profile} again: {again}"
    return elapsed


def _end(pid: int) -> None:
    """Cleanup for a failed run: only the Chrome this test started."""
    if _alive(pid) and "EXCLUDE 127.0.0.1" in _command(pid):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("instance", ["main", "spawned"])
def test_quit_ends_the_browser_and_the_next_call_says_why(instance):
    pytest.importorskip("AppKit")
    client = Client()
    pid = None
    try:
        pid = _start(client, "main" if instance == "main" else "quitme")
        if instance != "main":
            client.call("switch_instance", instance_id="quitme")
        _assert_quit_ended(pid, f"server alive ({instance})")
        reply = client.call("get_url")
        _record(case=f"server alive ({instance}): next call", text=reply.text[:200])
        assert reply.failed and "last window" in reply.text, reply.text
        assert "browser_recover" not in reply.text, reply.text
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        if pid:
            _end(pid)
    time.sleep(1)


def test_quit_ends_the_browser_after_its_server_is_gone():
    """The stdio server exits on a client reconnect; its headed Chrome stayed with no watcher."""
    pytest.importorskip("AppKit")
    before = _server_pids()
    client = Client()
    pid = None
    try:
        server = _server_pids() - before
        assert len(server) == 1, server
        server_pid = server.pop()
        pid = _start(client)
        os.kill(server_pid, signal.SIGKILL)  # this test's own server; it ignores SIGTERM
        deadline = time.time() + 5
        while _alive(server_pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(server_pid)
        assert _alive(pid), "Chrome ended with its server; the Quit is what is under test"
        _assert_quit_ended(pid, "server process ended")
    finally:
        with __import__("contextlib").suppress(Exception):
            client.close()
        if pid:
            _end(pid)


def test_quit_ends_the_browser_after_its_package_was_replaced(tmp_path):
    """A server started before `uv tool install --force` keeps running old code while its package
    is replaced on disk; its windowless Chrome stayed for hours."""
    pytest.importorskip("AppKit")
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    venv = Path(tempfile.mkdtemp(prefix="mcp-reinstall-"))
    base = getattr(sys, "_base_executable", sys.executable)

    def install():
        subprocess.run([uv, "venv", "-q", "--python", base, str(venv)], check=True)
        subprocess.run([uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(REPO)],
                       check=True)

    install()
    client = Client(env={"PYTHONPATH": ""}, python=str(venv / "bin" / "python"))
    pid = None
    try:
        status = client.call("server_status").json()
        assert status["version"], status
        pid = _start(client)
        # What a reinstall does to the tool venv: removed and built again.
        shutil.rmtree(venv)
        install()
        _assert_quit_ended(pid, "server package replaced")
        reply = client.call("get_url")
        _record(case="server package replaced: next call", text=reply.text[:200])
        assert reply.failed and "last window" in reply.text, reply.text
    finally:
        with __import__("contextlib").suppress(Exception):
            client.call("close_all_instances", _timeout=40)
        client.close()
        if pid:
            _end(pid)
        shutil.rmtree(venv, ignore_errors=True)


def test_tab_close_of_the_last_tab_is_not_a_quit():
    client = Client()
    pid = None
    try:
        pid = _start(client)
        closed = client.call("tab_close")
        assert not closed.failed, closed.text
        time.sleep(GRACE + 4)
        _record(case="tab_close last tab", alive_after=_alive(pid), waited=GRACE + 4,
                watcher_alive=bool(_watcher_pids(pid)))
        assert _alive(pid), "a browser a tool emptied was ended"
        assert _watcher_pids(pid), "the watcher stopped watching"
        # It is still this server's browser: a new tab works, and a close is still a close.
        opened = client.call("tab_new", url="about:blank")
        assert not opened.failed, opened.text
        done = client.call("browser_close", _timeout=40)
        assert not done.failed, done.text
        assert not _alive(pid)
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        if pid:
            _end(pid)


def test_the_watcher_never_ends_another_chrome(tmp_path):
    """A second browser of the same server, and a Chrome the test starts on its own, survive the
    first one's Quit."""
    pytest.importorskip("AppKit")
    client = Client()
    first = second = None
    loose = None
    loose_profile = tempfile.mkdtemp(prefix="mcp-quit-other-")
    try:
        first = _start(client)
        second = _start(client, "bystander")
        chrome = next(cmd.split(" --", 1)[0] for p, _pp, cmd in _ps() if p == first)
        loose = subprocess.Popen([chrome, f"--user-data-dir={loose_profile}", "--headless=new",
                                  "--use-mock-keychain", "--no-first-run", "about:blank"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        _assert_quit_ended(first, "first of two")
        _record(case="bystanders after the Quit", second_alive=_alive(second),
                loose_alive=loose.poll() is None)
        assert _alive(second), "the other browser of this server was ended"
        assert loose.poll() is None, "a Chrome the server never started was ended"
        switched = client.call("switch_instance", instance_id="bystander")
        assert not switched.failed, switched.text
        assert not client.call("get_url").failed
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        for pid in (first, second):
            if pid:
                _end(pid)
        if loose is not None:
            loose.kill()
            loose.wait(timeout=10)
        shutil.rmtree(loose_profile, ignore_errors=True)
