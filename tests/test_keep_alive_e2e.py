"""keep_alive on real Chrome: a browser left running when its server's client goes, and a later
server reattaching to it.

Needs MCP_E2E=1, POSIX (keep_alive is refused on Windows). Every server gets MCP_STEALTH_DATA_DIR
and MCP_STEALTH_KEEP_ALIVE_DIR in a temporary folder, so the persistent profile and its record
never touch ~/.mcp-stealth. Closing the client is stdin EOF, what an agent going to sleep does.
The Quit case also needs MCP_HEADED=1 on macOS.

Every signal this file sends goes to a process it started: a Chrome whose command line names this
test's temporary folder.
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import state

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome"),
    pytest.mark.skipif(os.name != "posix", reason="keep_alive is refused on Windows (no quit watcher)"),
]
HEADED = pytest.mark.skipif(not os.environ.get("MCP_HEADED") or sys.platform != "darwin",
                            reason="opens visible windows")

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class World:
    def __init__(self, base: str, root: pathlib.Path):
        self.urls = [f"{base}/protocol.html?tab=1", f"{base}/protocol.html?tab=2",
                     f"{base}/protocol.html?tab=3"]
        self.root = root
        self.data = root / "data"
        self.profile = self.data / "profile"
        self.records = root / "keep-alive"

    def env(self, **extra) -> dict:
        # "" for MCP_STEALTH_TABS_DIR: the saved tab lists go to <data>/window-state.
        return {"MCP_STEALTH_DATA_DIR": str(self.data), "MCP_STEALTH_KEEP_ALIVE_DIR": str(self.records),
                "MCP_STEALTH_TABS_DIR": "", **extra}

    def record(self) -> dict:
        files = list(self.records.glob("*.json")) if self.records.exists() else []
        assert len(files) == 1, files
        if sys.platform != "win32":
            assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        return json.loads(files[0].read_text())

    def no_record(self) -> bool:
        return not self.records.exists() or not list(self.records.glob("*.json"))


@pytest.fixture
def world():
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-keep-alive-"))
    try:
        yield World(f"http://127.0.0.1:{httpd.server_address[1]}", root)
    finally:
        httpd.shutdown()
        _end_processes_of(root)
        shutil.rmtree(root, ignore_errors=True)


def _end_processes_of(root: pathlib.Path) -> None:
    """Only processes naming this test's own temporary folder (Chrome, and its watcher)."""
    for pid, _command in state.processes_naming(root) or []:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _alive(pid: int) -> bool:
    return state._pid_alive(pid) and "Z" not in subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout


def _wait_gone(pid: int, within: float = 30) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return False


def _launch(client: Client, url: str, headless: bool = True, **kw):
    return client.call("browser_launch", _timeout=90, url=url, headless=headless, persistent=True,
                       extra_args=SAFE_ARGS, **kw)


def _tabs(client: Client) -> tuple[list, int]:
    lines = client.call("tab_list").text.splitlines()
    active = int(lines[0].split(":", 1)[1])
    return [line.rsplit(" | ", 1)[1] for line in lines[1:]], active


def _open_three_and_type(client: Client, world: World, headless: bool = True, **kw) -> int:
    launched = _launch(client, world.urls[0], headless=headless, keep_alive=True, **kw)
    assert not launched.failed, launched.text
    assert "keep_alive" in launched.text and "ignored" not in launched.text, launched.text
    for url in world.urls[1:]:
        assert not client.call("tab_new", url=url).failed
    assert not client.call("tab_select", index=1).failed
    filled = client.call("fill", selector="#name", value="Kept Value")
    assert not filled.failed, filled.text
    assert _tabs(client) == (world.urls, 1)
    record = world.record()
    assert record["state"] == "held" and record["server_pid"] != os.getpid()
    return record["pid"]


def test_a_kept_browser_survives_eof_and_the_next_server_reattaches(world):
    client = Client(env=world.env())
    try:
        pid = _open_three_and_type(client, world)
    finally:
        client.close()  # stdin EOF: the server exits
    time.sleep(2)
    assert _alive(pid), "the kept browser ended with its server"
    record = world.record()
    assert record["state"] == "kept" and record["pid"] == pid
    assert _alive(record["watcher_pid"]), "the kept browser has no watcher after its server went"
    saved = json.loads((world.data / "window-state" / "main-tabs.json").read_text())
    entry = saved["profiles"][str(world.profile.resolve())]
    assert entry["reason"] == "server exit" and [t["url"] for t in entry["tabs"]] == world.urls, \
        "a kept browser's tabs were not recorded at exit"

    # A new server, a new agent: same profile, default keep_alive.
    client = Client(env=world.env())
    try:
        out = _launch(client, "about:blank")
        assert not out.failed, out.text
        assert f"Reattached to the running browser (pid {pid}, 3 tab(s))" in out.text, out.text
        assert "cookie move did not run" in out.text
        assert _tabs(client) == (world.urls, 1), "tabs or the selected one changed"
        value = client.evaluate("document.querySelector('#name').value")
        assert "Kept Value" in value, value
        # It works as any browser does.
        assert not client.call("tab_new", url=world.urls[0] + "&after=1").failed
        assert world.record()["server_pid"] != record["server_pid"]
        assert not _alive(record["watcher_pid"]), "the old watcher still runs beside the new one"
    finally:
        client.close()  # keep_alive left unset on a reattach: it stays kept
    time.sleep(2)
    assert _alive(pid), "a reattach without keep_alive dropped what the first launch asked for"

    # keep_alive=False on a reattach: closes with this server.
    client = Client(env=world.env())
    try:
        out = _launch(client, "about:blank", keep_alive=False)
        assert f"(pid {pid}, 4 tab(s))" in out.text and "now closes when this server exits" in out.text, out.text
        assert world.no_record()
    finally:
        client.close()
    assert _wait_gone(pid, 15), "a reattached browser without keep_alive outlived its server"


def test_spawn_browser_keeps_and_reattaches_a_named_instance(world):
    def spawn(client, **kw):
        out = client.call("spawn_browser", _timeout=90, instance_id="acct", headless=True,
                          extra_args=SAFE_ARGS, **kw)
        assert not out.failed, out.text
        return out

    client = Client(env=world.env())
    try:
        out = spawn(client, url=world.urls[0], keep_alive=True)
        assert "keep_alive" in out.text, out.text
        pid = world.record()["pid"]
        assert world.record()["instance_id"] == "acct"
    finally:
        client.close()
    time.sleep(2)
    assert _alive(pid)
    client = Client(env=world.env())
    try:
        out = spawn(client, url="about:blank")
        assert f"instance 'acct' reattached to the running browser (pid {pid}, 1 tab(s))" in out.text, out.text
        assert not client.call("switch_instance", instance_id="acct").failed
        assert _tabs(client) == (world.urls[:1], 0)
        assert not client.call("close_instance", instance_id="acct", _timeout=40).failed
        assert _wait_gone(pid, 10)
        assert world.no_record()
    finally:
        client.close()


def test_browser_close_after_a_reattach_ends_it_and_the_record(world):
    client = Client(env=world.env())
    try:
        pid = _open_three_and_type(client, world)
    finally:
        client.close()
    client = Client(env=world.env())
    try:
        out = _launch(client, world.urls[2] + "&new=1")
        assert "Reattached" in out.text and "opened in a new tab" in out.text, out.text
        tabs, active = _tabs(client)
        assert tabs[:3] == world.urls and tabs[3].endswith("&new=1") and active == 3
        closed = client.call("browser_close")
        assert not closed.failed, closed.text
        assert _wait_gone(pid, 10)
        assert world.no_record()
    finally:
        client.close()


def test_without_keep_alive_eof_still_closes(world):
    client = Client(env=world.env())
    try:
        assert not _launch(client, world.urls[0]).failed
        pid = state.chrome_lock_holder_pid(world.profile)
        assert pid
    finally:
        client.close()
    assert _wait_gone(pid, 15)
    assert world.no_record()


@pytest.mark.parametrize("kind", ["persistent=False", "privacy=True"])
def test_keep_alive_is_ignored_without_a_persistent_profile(world, kind):
    args = {"persistent": False} if kind == "persistent=False" else {"privacy": True}
    def chromes():
        return {pid for pid, _pp, cmd in state.process_table() or []
                if "--type=" not in cmd and "EXCLUDE 127.0.0.1" in cmd
                and ("uc_" in cmd or str(world.data) in cmd)}

    before = chromes()
    client = Client(env=world.env())
    try:
        out = client.call("browser_launch", _timeout=90, url=world.urls[0], headless=True,
                          extra_args=SAFE_ARGS, keep_alive=True, **args)
        assert not out.failed, out.text
        assert "keep_alive ignored" in out.text, out.text
        assert world.no_record()
        rows = chromes() - before  # this server's browser only, not one another test is ending
        assert rows
    finally:
        client.close()
    for pid in rows:
        assert _wait_gone(pid, 15), "an ignored keep_alive left the browser running"


def test_the_time_cap_ends_a_kept_browser_nobody_reattached(world):
    client = Client(env=world.env(MCP_STEALTH_KEEP_ALIVE_MAX_HOURS="0.001"))  # 3.6 s
    try:
        launched = _launch(client, world.urls[0], keep_alive=True)
        assert "0.001 h" in launched.text, launched.text
        pid = world.record()["pid"]
        time.sleep(5)
        assert _alive(pid), "the cap counted while a server still held the browser"
    finally:
        client.close()
    assert _wait_gone(pid, 20), "the time cap did not end the kept browser"
    deadline = time.time() + 5
    while time.time() < deadline and not world.no_record():
        time.sleep(0.2)
    assert world.no_record()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_foreign_chrome_on_the_profile_is_never_attached(world, monkeypatch):
    """A Chrome this server did not start, on the profile, with a record forged to point at it:
    no reattach, and the launch falls back as before, naming why."""
    chrome = state.find_chrome_binary()
    port = _free_port()
    world.profile.mkdir(parents=True)
    proc = subprocess.Popen([chrome, f"--user-data-dir={world.profile}", f"--remote-debugging-port={port}",
                             "--headless=new", "--no-first-run", *SAFE_ARGS,
                             *(["--no-sandbox"] if sys.platform.startswith("linux") else []), "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        version = None
        while time.time() < deadline and version is None:
            try:
                version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1))
            except OSError:
                time.sleep(0.2)
        assert version, "the stand-in Chrome did not open its port"
        while time.time() < deadline and state.chrome_lock_holder_pid(world.profile) != proc.pid:
            time.sleep(0.2)
        from mcp_stealth_chrome import keep_alive
        monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(world.records))
        assert keep_alive.write(world.profile, pid=proc.pid, host="127.0.0.1", port=port,
                                user_data_dir=str(world.profile), instance_id="main",
                                chrome_version=version["Browser"], headless=True,
                                server_pid=999999, watcher_pid=None, state="kept") is None
        client = Client(env=world.env())
        try:
            out = _launch(client, world.urls[0])
            assert not out.failed, out.text
            assert "Reattached" not in out.text, out.text
            assert "was not reattached" in out.text and "was not started by this server" in out.text, out.text
            assert "FALLBACK profile" in out.text, out.text
        finally:
            client.call("browser_close", _timeout=40)
            client.close()
        assert proc.poll() is None, "the foreign Chrome was closed"
    finally:
        proc.terminate()
        proc.wait(10)


def test_a_browser_a_live_server_holds_is_not_taken(world):
    holder = Client(env=world.env())
    try:
        pid = _open_three_and_type(holder, world)
        other = Client(env=world.env())
        try:
            out = _launch(other, world.urls[0])
            assert "Reattached" not in out.text, out.text
            assert "is still running and holds it" in out.text, out.text
        finally:
            other.call("browser_close", _timeout=40)
            other.close()
        assert _alive(pid)
        assert _tabs(holder) == (world.urls, 1)
    finally:
        holder.call("browser_close", _timeout=40)
        holder.close()
    assert _wait_gone(pid, 10)


@HEADED
def test_quit_still_ends_a_kept_headed_browser_after_its_server_went(world):
    """Kept, reattached by a second server (whose watcher replaces the first), left again, and
    then quit by the person: the watcher of a server that is gone still ends it."""
    import AppKit
    from test_headed_focus import _Frontmost

    front = _Frontmost()
    try:
        client = Client(env=world.env())
        try:
            pid = _open_three_and_type(client, world, headless=False)
        finally:
            client.close()
        time.sleep(2)
        assert _alive(pid)
        first_watcher = world.record()["watcher_pid"]
        client = Client(env=world.env())
        try:
            start = len(front.samples)
            out = _launch(client, "about:blank", headless=False)
            assert f"Reattached to the running browser (pid {pid}, 3 tab(s))" in out.text, out.text
            assert _tabs(client) == (world.urls, 1)
            assert "Kept Value" in client.evaluate("document.querySelector('#name').value")
            time.sleep(0.5)
            assert front.ms_of({pid}, start) == 0, "the reattach brought Chrome to the front"
            assert not _alive(first_watcher)
        finally:
            client.close()
    finally:
        front.stop()
    time.sleep(2)
    assert _alive(pid)
    assert _alive(world.record()["watcher_pid"])
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    assert app is not None and app.terminate()
    assert _wait_gone(pid, 20), "Quit did not end the kept browser"
    deadline = time.time() + 5
    while time.time() < deadline and not world.no_record():
        time.sleep(0.2)
    assert world.no_record()


def test_keep_alive_is_said_and_applied_on_the_running_browser(world):
    """Review F6: a new browser says it closes with its server, and browser_launch on the browser
    already running applies an explicit keep_alive and says what holds."""
    client = Client(env=world.env())
    try:
        out = _launch(client, world.urls[0])
        assert "keep_alive=False: this browser closes when this server exits" in out.text, out.text
        pid = state.chrome_lock_holder_pid(world.profile)
        out = _launch(client, world.urls[1], keep_alive=True)
        assert "reusing the running browser" in out.text and "keeps running after this server exits" in out.text, out.text
        assert world.record()["pid"] == pid
        out = _launch(client, world.urls[1])
        assert "keep_alive: on" in out.text, out.text
    finally:
        client.close()
    time.sleep(2)
    assert _alive(pid), "keep_alive applied on the running browser did not keep it"
    client = Client(env=world.env())
    try:
        out = _launch(client, "about:blank", keep_alive=False)
        assert "Reattached" in out.text and "now closes" in out.text, out.text
    finally:
        client.close()
    assert _wait_gone(pid, 15)


def test_two_servers_reattaching_at_once_never_share_the_browser(world):
    """Review F1: both find the kept browser free; exactly one takes it, the other is refused."""
    import concurrent.futures

    client = Client(env=world.env())
    try:
        pid = _open_three_and_type(client, world)
    finally:
        client.close()
    first, second = Client(env=world.env()), Client(env=world.env())
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outs = list(pool.map(lambda c: _launch(c, "about:blank"), (first, second)))
        taken = [o for o in outs if "Reattached" in o.text]
        assert len(taken) == 1, [o.text[:300] for o in outs]
        other = next(o for o in outs if "Reattached" not in o.text)
        assert ("another server is reattaching" in other.text
                or "is still running and holds it" in other.text), other.text
    finally:
        for c in (first, second):
            c.call("browser_close", _timeout=40)
            c.close()
    assert _wait_gone(pid, 15)
