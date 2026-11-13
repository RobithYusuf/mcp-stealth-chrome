"""restore_tabs on real Chrome: the tabs come back after each way a browser can end.

Needs MCP_E2E=1. Every server gets MCP_STEALTH_DATA_DIR in a temporary folder, so the default
persistent profile and its saved tab list (<data>/window-state/main-tabs.json) live there and
never in ~/.mcp-stealth. The headed cases (Quit, and stdin EOF with a visible window) also need
MCP_HEADED=1 on macOS, and check that no window took the keyboard.

Every signal this file sends goes to a process it started: its own server (a child of this test
process) or a Chrome whose command line names this test's temporary profile.
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import state

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")
HEADED = pytest.mark.skipif(not os.environ.get("MCP_HEADED") or sys.platform != "darwin",
                            reason="opens visible windows")

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
SECRET = "s3cretTok"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class World:
    def __init__(self, base: str, root: pathlib.Path):
        self.urls = [f"{base}/protocol.html?tab=1", f"{base}/protocol.html?tab=2",
                     f"{base}/protocol.html?tab=3&access_token={SECRET}"]
        self.data = root / "data"
        self.profile = self.data / "profile"
        self.record = self.data / "window-state" / "main-tabs.json"

    def env(self, **extra) -> dict:
        # "" for MCP_STEALTH_TABS_DIR: the default folder, <data>/window-state, as a person has it.
        return {"MCP_STEALTH_DATA_DIR": str(self.data), "MCP_STEALTH_TABS_DIR": "", **extra}

    def entry(self) -> dict:
        doc = json.loads(self.record.read_text())
        assert list(doc["profiles"]) == [str(self.profile.resolve())]
        return doc["profiles"][str(self.profile.resolve())]


@pytest.fixture
def world():
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-restore-tabs-"))
    try:
        yield World(f"http://127.0.0.1:{httpd.server_address[1]}", root)
    finally:
        httpd.shutdown()
        _end_chrome_of(root)
        shutil.rmtree(root, ignore_errors=True)


def _end_chrome_of(root: pathlib.Path) -> None:
    """Only processes naming this test's own temporary folder."""
    for pid, _command in state.processes_naming(root) or []:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _wait_profile_free(world: World, within: float = 30) -> None:
    deadline = time.time() + within
    while time.time() < deadline:
        if not state.processes_naming(world.profile):
            return
        time.sleep(0.2)
    raise AssertionError(f"Chrome still holds {world.profile}: {state.processes_naming(world.profile)}")


def _launch(client: Client, url: str, headless: bool = True, **kw):
    return client.call("browser_launch", _timeout=90, url=url, headless=headless, persistent=True,
                       extra_args=SAFE_ARGS, **kw)


def _open_three(client: Client, world: World, headless: bool = True, select: int = 1) -> None:
    launched = _launch(client, world.urls[0], headless=headless)
    assert not launched.failed, launched.text
    for url in world.urls[1:]:
        assert not client.call("tab_new", url=url).failed
    assert not client.call("tab_select", index=select).failed
    assert _tabs(client) == (world.urls, select)


def _tabs(client: Client) -> tuple[list, int]:
    lines = client.call("tab_list").text.splitlines()
    active = int(lines[0].split(":", 1)[1])
    return [line.rsplit(" | ", 1)[1] for line in lines[1:]], active


def _assert_restored(client: Client, world: World, select: int, headless: bool = True):
    out = _launch(client, "about:blank", headless=headless, restore_tabs=True)
    assert not out.failed, out.text
    assert "reopened 3 of 3 saved tab(s)" in out.text, out.text
    assert f"tab {select} is selected" in out.text
    assert SECRET not in out.text, "a token from a saved URL reached the tool output"
    assert "access_token=***" in out.text
    assert _tabs(client) == (world.urls, select)
    return out


def _private(path: pathlib.Path) -> None:
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_browser_close_saves_the_tabs_and_restore_reopens_them(world):
    client = Client(env=world.env())
    try:
        _open_three(client, world, select=1)
        closed = client.call("browser_close")
        assert not closed.failed and "Recorded 3 tab(s)" in closed.text, closed.text
        entry = world.entry()
        assert [t["url"] for t in entry["tabs"]] == world.urls
        assert entry["active"] == 1 and entry["reason"] == "browser_close"
        _private(world.record)
        _assert_restored(client, world, select=1)
        # Launching without restore_tabs keeps the list, and an empty browser does not replace it.
        assert not client.call("browser_close").failed
        assert not _launch(client, "about:blank").failed
        assert _tabs(client)[0] == ["about:blank"]
        assert not client.call("browser_close").failed
        assert [t["url"] for t in world.entry()["tabs"]] == world.urls
        # forget_tabs removes it.
        assert not _launch(client, world.urls[0]).failed
        forgot = client.call("browser_close", forget_tabs=True)
        assert "saved tab list was removed" in forgot.text, forgot.text
        assert not world.record.exists()
        out = _launch(client, "about:blank", restore_tabs=True)
        assert "nothing to reopen: no tabs were saved" in out.text, out.text
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_stdin_eof_saves_the_tabs(world):
    """What an agent session going to sleep does: the client goes, stdin closes, the server exits.
    The background writer is off, so past the launch's own record the list can only come from
    the exit path."""
    client = Client(env=world.env(MCP_STEALTH_TAB_RECORD_DEBOUNCE="0", MCP_STEALTH_TAB_RECORD_PERIOD="0"))
    try:
        _open_three(client, world, select=2)
        time.sleep(1)
        launched = world.entry()
        assert launched["reason"] == "at launch", "something other than the launch wrote the list"
        assert [t["url"] for t in launched["tabs"]] == world.urls[:1]
    finally:
        client.close()
    entry = world.entry()
    assert entry["reason"] == "server exit"
    assert [t["url"] for t in entry["tabs"]] == world.urls and entry["active"] == 2
    _private(world.record)
    _wait_profile_free(world)
    client = Client(env=world.env())
    try:
        _assert_restored(client, world, select=2)
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def _server_pids() -> set:
    """Server processes below this test process. On Windows a venv's python.exe is a launcher
    that starts the interpreter as its child, so a server can be two processes deep."""
    rows = state.process_table() or []
    below, grew = {os.getpid()}, True
    while grew:
        grew = False
        for pid, ppid, _command in rows:
            if ppid in below and pid not in below:
                below.add(pid)
                grew = True
    return {pid for pid, _ppid, command in rows
            if pid in below and "mcp_stealth_chrome.server import main" in command}


def test_a_killed_server_leaves_the_last_list_it_saw(world):
    before = _server_pids()
    client = Client(env=world.env(MCP_STEALTH_TAB_RECORD_DEBOUNCE="0.5", MCP_STEALTH_TAB_RECORD_PERIOD="0"))
    ours = _server_pids() - before
    assert ours, "the server this test started was not found"
    try:
        _open_three(client, world, select=0)
        deadline = time.time() + 10  # the launch writes its own record first
        while time.time() < deadline and not (world.record.exists()
                                              and world.entry()["reason"] == "while running"):
            time.sleep(0.2)
        entry = world.entry()
        assert entry["reason"] == "while running"
        assert [t["url"] for t in entry["tabs"]] == world.urls and entry["active"] == 0
        for pid in ours:  # SIGTERM is TerminateProcess on Windows
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        time.sleep(0.5)
        _end_chrome_of(world.profile)  # orphaned by the kill; it names this test's profile
        _wait_profile_free(world)
    finally:
        client.close()
    client = Client(env=world.env())
    try:
        _assert_restored(client, world, select=0)
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_named_instances_close_instance_close_all_and_idle_timeout(world):
    """A stored instance: close_instance, close_all_instances and the idle reaper each save its
    tabs, and spawn_browser(restore_tabs=True) brings them back. Past the launch's own record,
    only the closes can write."""
    record = world.data / "window-state" / "acct-tabs.json"
    acct = world.data / "profiles" / "acct"
    client = Client(env=world.env(MCP_STEALTH_TAB_RECORD_DEBOUNCE="0", MCP_STEALTH_TAB_RECORD_PERIOD="0",
                                  BROWSER_IDLE_REAPER_INTERVAL="1"))

    def spawn(**kw):
        kw.setdefault("url", world.urls[0])
        out = client.call("spawn_browser", _timeout=90, instance_id="acct", headless=True,
                          extra_args=SAFE_ARGS, **kw)
        assert not out.failed, out.text
        return out

    def open_second_and_leave():
        assert not client.call("switch_instance", instance_id="acct").failed
        assert not client.call("tab_new", url=world.urls[1]).failed
        assert not client.call("tab_select", index=0).failed
        assert not client.call("switch_instance", instance_id="main").failed  # acct is stored now

    def entry():
        doc = json.loads(record.read_text())
        return doc["profiles"][str(acct.resolve())]

    try:
        spawn()
        open_second_and_leave()
        assert entry()["reason"] == "at launch" and [t["url"] for t in entry()["tabs"]] == world.urls[:1]
        assert not client.call("close_instance", instance_id="acct").failed
        assert entry()["reason"] == "close_instance"
        assert [t["url"] for t in entry()["tabs"]] == world.urls[:2] and entry()["active"] == 0
        _private(record)
        out = spawn(restore_tabs=True, url="about:blank")
        assert "reopened 2 of 2 saved tab(s)" in out.text and "tab 0 is selected" in out.text, out.text
        assert not client.call("switch_instance", instance_id="acct").failed
        assert _tabs(client) == (world.urls[:2], 0)
        assert not client.call("tab_select", index=1).failed
        assert not client.call("switch_instance", instance_id="main").failed
        assert not client.call("close_all_instances", _timeout=40).failed
        assert entry()["reason"] == "close_all_instances" and entry()["active"] == 1
        spawn(idle_timeout_seconds=2)
        open_second_and_leave()
        deadline = time.time() + 20
        while time.time() < deadline and entry()["reason"] != "idle timeout":
            time.sleep(0.5)
        assert entry()["reason"] == "idle timeout" and entry()["active"] == 0
        # Saved before the close starts; the instance goes once Chrome is seen gone.
        deadline = time.time() + 15
        while time.time() < deadline and '"acct"' in client.call("list_instances").text:
            time.sleep(0.5)
        assert '"acct"' not in client.call("list_instances").text
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


@pytest.mark.parametrize("kind", ["persistent=False", "privacy=True"])
def test_ephemeral_and_privacy_launches_never_write_a_list(world, kind):
    client = Client(env=world.env(MCP_STEALTH_TAB_RECORD_DEBOUNCE="0.3", MCP_STEALTH_TAB_RECORD_PERIOD="0.5"))
    args = {"persistent": False} if kind == "persistent=False" else {"privacy": True}
    try:
        launched = client.call("browser_launch", _timeout=90, url=world.urls[0], headless=True,
                               extra_args=SAFE_ARGS, **args)
        assert not launched.failed, launched.text
        assert not client.call("tab_new", url=world.urls[1]).failed
        time.sleep(2.5)
        assert not client.call("browser_close").failed
        assert not client.call("browser_launch", _timeout=90, url=world.urls[0], headless=True,
                               extra_args=SAFE_ARGS, **args).failed
        out = client.call("browser_launch", _timeout=90, url="about:blank", headless=True,
                          extra_args=SAFE_ARGS, restore_tabs=True, **args)
        assert "restore_tabs NOT applied" in out.text, "reuse path must say so"
    finally:
        client.close()  # stdin EOF with the browser still open
    folder = world.record.parent
    assert not folder.exists() or not list(folder.glob("*-tabs.json")), list(folder.iterdir())


# ── headed: Quit and stdin EOF with a visible window, no focus taken ─────


def _chrome_pids() -> set:
    return {pid for pid, _pp, cmd in state.process_table() or []
            if ("/Google Chrome " in cmd or "/Google Chrome Beta " in cmd) and "--type=" not in cmd
            and "EXCLUDE 127.0.0.1" in cmd and "--headless" not in cmd}


def _quit(pid: int) -> None:
    import AppKit
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    assert app is not None and app.terminate()


@HEADED
def test_headed_quit_and_sleep_keep_the_tabs_and_restore_takes_no_focus(world):
    from test_headed_focus import _Frontmost

    front = _Frontmost()
    client = Client(env=world.env(MCP_STEALTH_TAB_RECORD_DEBOUNCE="0.5"))
    try:
        before = _chrome_pids()
        _open_three(client, world, headless=False, select=1)
        chrome = _chrome_pids() - before
        assert len(chrome) == 1, chrome
        deadline = time.time() + 10  # the launch writes its own record first
        while time.time() < deadline and not (world.record.exists()
                                              and world.entry()["reason"] == "while running"):
            time.sleep(0.2)
        assert world.entry()["active"] == 1
        saved = world.record.read_text()
        # Quit, as Cmd+Q does: every window closes before anything could read them.
        _quit(next(iter(chrome)))
        _wait_profile_free(world)
        assert world.record.read_text() == saved, "the Quit replaced the saved list"
        time.sleep(2)
        start = len(front.samples)
        before = _chrome_pids()
        _assert_restored(client, world, select=1, headless=False)
        ours = _chrome_pids() - before
        time.sleep(0.5)
        assert ours and front.ms_of(ours, start) == 0, "a restored window took the keyboard"
        # Sleep: the client goes away with the visible browser open.
        assert not client.call("tab_select", index=2).failed
    finally:
        client.close()
    assert world.entry()["reason"] == "server exit" and world.entry()["active"] == 2
    _wait_profile_free(world)
    client = Client(env=world.env())
    try:
        start = len(front.samples)
        before = _chrome_pids()
        _assert_restored(client, world, select=2, headless=False)
        ours = _chrome_pids() - before
        time.sleep(0.5)
        assert ours and front.ms_of(ours, start) == 0, "a restored window took the keyboard"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        front.stop()
