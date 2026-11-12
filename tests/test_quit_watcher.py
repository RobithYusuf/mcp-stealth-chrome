"""The detached quit watcher, against a stand-in DevTools endpoint and stand-in "Chrome" processes
this test starts itself. No real Chrome, and no process the test did not start is touched.

A headed Chrome started with --no-startup-window stays running after Quit. The watcher ends it
from outside the server, so it has to tell a Quit from everything else on its own: a page
still open, an endpoint that does not answer, a tool that emptied the browser on purpose, and
a PID that now belongs to another program.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import _quit_watcher as qw
from mcp_stealth_chrome import quit_watch

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the watcher uses ps and POSIX signals")


def _fake_chrome(profile: str, ignore_term: bool = False) -> subprocess.Popen:
    """A process whose command line names a profile the way Chrome's does."""
    body = ("import signal,time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "time.sleep(120)")
    return subprocess.Popen([sys.executable, "-c", body, f"--user-data-dir={profile}"])


class _DevTools:
    """/json/list with a settable page count, /json/version, and a websocket that records
    Browser.close and ends the stand-in Chrome, as the real one does."""

    def __init__(self, pages):
        self.pages = pages
        self.closes = 0
        self.on_close = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path == "/json/list":
                    count = outer.pages if not callable(outer.pages) else outer.pages()
                    body = json.dumps([{"type": "page", "id": str(i)} for i in range(count)]
                                      + [{"type": "service_worker", "id": "sw"}]).encode()
                elif self.path == "/json/version":
                    port = self.server.server_address[1]
                    body = json.dumps({"webSocketDebuggerUrl":
                                       f"ws://127.0.0.1:{port}/devtools/browser/x"}).encode()
                elif self.path.startswith("/devtools/browser/"):
                    key = self.headers["Sec-WebSocket-Key"]
                    accept = base64.b64encode(hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    head = self.rfile.read(2)
                    length = head[1] & 0x7F
                    mask = self.rfile.read(4)
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(length)))
                    if json.loads(data).get("method") == "Browser.close":
                        outer.closes += 1
                        if outer.on_close:
                            outer.on_close()
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                # Chrome keeps the connection open whatever the client asks: so does this.
                self.rfile.readline()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def devtools():
    servers = []

    def make(pages):
        server = _DevTools(pages)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def procs():
    started: list[subprocess.Popen] = []
    yield started
    for proc in started:  # only processes this test started
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)


def _cfg(tmp_path, pid, port, profile, **over):
    cfg = {"pid": pid, "host": "127.0.0.1", "port": port, "user_data_dir": profile,
           "grace": 0.3, "close_wait": 1.0, "interval": 0.05,
           "tool_closed_file": str(tmp_path / "w.tool-closed"), "quit_file": str(tmp_path / "w.quit"),
           "server_pid": os.getppid()}
    cfg.update(over)
    return cfg


def _bounded(seconds):
    """A sleep that stops the watch loop after `seconds`, for cases that must never act."""
    deadline = time.monotonic() + seconds

    def sleep(t):
        if time.monotonic() > deadline:
            raise TimeoutError("watched long enough")
        time.sleep(t)
    return sleep


def test_no_page_for_the_grace_closes_the_browser_and_writes_the_verdict(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(0)
    tools.on_close = chrome.terminate
    verdicts = []
    real_end = qw.end_browser

    def end(cfg):
        verdicts.append(json.loads((tmp_path / "w.quit").read_text()))
        return real_end(cfg)

    qw.end_browser = end
    try:
        outcome = qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(10))
    finally:
        qw.end_browser = real_end
    assert outcome == "quit: closed", outcome
    assert tools.closes == 1
    assert verdicts and verdicts[0]["pid"] == chrome.pid and verdicts[0]["user_data_dir"] == profile
    assert chrome.wait(timeout=5) is not None


def test_a_browser_that_ignores_close_gets_its_one_pid_signalled(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(0)
    outcome = qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(10))
    assert outcome == "quit: signalled", outcome
    assert tools.closes == 1
    assert chrome.wait(timeout=5) is not None


def test_a_page_still_open_is_left_alone(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(1)
    with pytest.raises(TimeoutError):
        qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(1.5))
    assert tools.closes == 0 and chrome.poll() is None


def test_an_unanswered_endpoint_never_counts_as_no_windows(tmp_path, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    with contextlib.closing(__import__("socket").socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]  # nothing listens here once closed
    with pytest.raises(TimeoutError):
        qw.watch(_cfg(tmp_path, chrome.pid, dead_port, profile), sleep=_bounded(1.5))
    assert chrome.poll() is None and not (tmp_path / "w.quit").exists()


def test_a_browser_a_tool_emptied_is_not_a_quit_while_its_server_runs(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(0)
    (tmp_path / "w.tool-closed").write_text("{}")
    with pytest.raises(TimeoutError):
        qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(1.5))
    assert tools.closes == 0 and chrome.poll() is None


def test_the_tool_closed_mark_ends_when_the_browser_has_a_page_again(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    seen = iter([1, 0])
    tools = devtools(lambda: next(seen, 0))
    tools.on_close = chrome.terminate
    (tmp_path / "w.tool-closed").write_text("{}")
    outcome = qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(10))
    assert outcome == "quit: closed" and not (tmp_path / "w.tool-closed").exists()


def test_the_tool_closed_mark_does_not_outlive_the_server(tmp_path, devtools, procs):
    """Nobody can use a browser whose server is gone: an empty one is ended like a Quit."""
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(0)
    tools.on_close = chrome.terminate
    (tmp_path / "w.tool-closed").write_text("{}")
    cfg = _cfg(tmp_path, chrome.pid, tools.port, profile, server_pid=-1)
    assert qw.watch(cfg, sleep=_bounded(10)) == "quit: closed"


def test_a_reused_pid_is_never_signalled(tmp_path, devtools, procs):
    """The PID now runs another program: its command line does not name the profile."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    procs.append(other)
    tools = devtools(0)
    outcome = qw.watch(_cfg(tmp_path, other.pid, tools.port, str(tmp_path / "profile")),
                       sleep=_bounded(10))
    assert outcome == "pid reused"
    assert tools.closes == 0 and other.poll() is None


def test_end_browser_rechecks_the_command_line_before_each_signal(tmp_path, devtools, procs, monkeypatch):
    """Between Browser.close and the signal the number can change hands. A PID whose command
    line no longer names the profile is not signalled."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    procs.append(other)
    tools = devtools(0)
    profile = str(tmp_path / "profile")
    monkeypatch.setattr(qw, "wait_gone", lambda *_a: False)  # Browser.close "did not work"
    sent = []
    monkeypatch.setattr(qw, "send_signal", lambda pid, sig: sent.append((pid, sig)))
    assert qw.end_browser(_cfg(tmp_path, other.pid, tools.port, profile)) == "closed"
    assert sent == [] and other.poll() is None


def test_a_gone_browser_ends_the_watcher(tmp_path, devtools, procs):
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(1)
    chrome.kill()
    chrome.wait(timeout=5)
    assert qw.watch(_cfg(tmp_path, chrome.pid, tools.port, profile), sleep=_bounded(5)) == "gone"


def test_the_detached_watcher_runs_from_stdin_and_ends_a_windowless_browser(tmp_path, devtools, procs,
                                                                           monkeypatch):
    """Started as the server starts it: base interpreter, -I, the source on stdin, its own
    session. It reads nothing from the package after it starts."""
    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCH_DIR", str(tmp_path / "watch"))
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(1)
    tools.on_close = chrome.terminate
    browser = SimpleNamespace(_process=chrome, _process_pid=chrome.pid,
                              config=SimpleNamespace(host="127.0.0.1", port=tools.port,
                                                     user_data_dir=profile))
    proc = quit_watch.start(browser, grace=0.5, close_wait=2.0)
    assert proc is not None
    procs.append(proc)
    assert os.getsid(proc.pid) == proc.pid  # its own session: a group signal to the server misses it
    assert (tmp_path / "watch").stat().st_mode & 0o777 == 0o700
    time.sleep(1.0)
    assert proc.poll() is None and chrome.poll() is None  # a page is open
    quit_watch.mark_tool_closed(browser)
    marker = tmp_path / "watch" / f"chrome-{chrome.pid}-{tools.port}.tool-closed"
    assert marker.stat().st_mode & 0o777 == 0o600
    tools.pages = 0
    time.sleep(1.5)
    assert chrome.poll() is None, "a browser a tool emptied was ended"
    marker.unlink()
    started = time.monotonic()
    assert chrome.wait(timeout=6) is not None
    assert time.monotonic() - started < 0.5 + 2.0
    assert proc.wait(timeout=5) == 0
    assert quit_watch.quit_time(browser) is not None  # the verdict waits for this server
    quit_watch.forget(browser)


def test_stopping_the_watcher_before_a_tool_close(tmp_path, devtools, procs, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCH_DIR", str(tmp_path / "watch"))
    profile = str(tmp_path / "profile")
    chrome = _fake_chrome(profile)
    procs.append(chrome)
    tools = devtools(1)
    browser = SimpleNamespace(_process=chrome, _process_pid=chrome.pid,
                              config=SimpleNamespace(host="127.0.0.1", port=tools.port,
                                                     user_data_dir=profile))
    proc = quit_watch.start(browser, grace=0.2, close_wait=1.0)
    procs.append(proc)
    quit_watch.stop(browser)
    assert proc.poll() is not None
    tools.pages = 0
    time.sleep(1.0)
    assert tools.closes == 0 and chrome.poll() is None


def test_a_verdict_for_another_profile_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCH_DIR", str(tmp_path))
    browser = SimpleNamespace(_process=None, _process_pid=4242,
                              config=SimpleNamespace(host="127.0.0.1", port=9, user_data_dir="/a"))
    (tmp_path / "chrome-4242-9.quit").write_text(json.dumps({"pid": 4242, "user_data_dir": "/b", "at": 1}))
    assert quit_watch.quit_time(browser) is None
    (tmp_path / "chrome-4242-9.quit").write_text(json.dumps({"pid": 4242, "user_data_dir": "/a", "at": 1}))
    assert quit_watch.quit_time(browser) == 1.0


def test_the_watcher_imports_nothing_from_the_package():
    source = quit_watch._SCRIPT
    assert "mcp_stealth_chrome" not in source.replace('"mcp-stealth-quit-watcher"', "")
    assert "import " not in source.split("def ", 1)[1], "imports after start would read the disk"
