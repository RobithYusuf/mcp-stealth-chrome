"""The suite's net for browsers it left running (conftest, _leftovers, run_marker).

Stand-ins only: Python processes whose command line carries a run flag the way a marked Chrome's
does, all started here and ended here. A real browser is checked in test_a_launched_chrome_is_marked.
"""
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import _leftovers
from _protocol import REPO
from mcp_stealth_chrome import run_marker, state

TOKEN = "0123456789abcdef"
NAP = "import time; time.sleep(60)"


def _orphan(*flags: str) -> int:
    """A stand-in whose starter has exited, as a Chrome left by a killed server is."""
    launcher = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(f"""
            import subprocess, sys
            p = subprocess.Popen([sys.executable, "-c", {NAP!r}, *sys.argv[1:]],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(p.pid)
        """), *flags], capture_output=True, text=True, timeout=30)
    return int(launcher.stdout.strip())


def _gone(pid: int, within: float = 10) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if not state._pid_alive(pid) or "Z" in subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def stand_ins():
    started: list[int] = []
    yield started
    for pid in started:
        if state._pid_alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_the_flag_is_added_only_while_a_run_sets_it(monkeypatch):
    class Config:
        def __init__(self):
            self.browser_args = []

        def add_argument(self, arg):
            self.browser_args.append(arg)

    monkeypatch.delenv(run_marker.ENV, raising=False)
    config = Config()
    run_marker.mark(config)
    assert config.browser_args == []
    monkeypatch.setenv(run_marker.ENV, "../not-a-token")
    run_marker.mark(config)
    assert config.browser_args == []
    monkeypatch.setenv(run_marker.ENV, TOKEN)
    run_marker.mark(config)
    run_marker.mark(config)  # a retry of the same launch adds it once
    assert config.browser_args == [f"--mcp-stealth-test-run={TOKEN}"]


def test_only_this_runs_browser_main_processes_match():
    flag = f"--mcp-stealth-test-run={TOKEN}"
    assert run_marker.is_marked(f"/x/Google Chrome --a {flag} about:blank", TOKEN)
    assert run_marker.is_marked(f"chrome {flag}", TOKEN)
    assert not run_marker.is_marked(f"chrome {flag}0", TOKEN)  # another run's longer token
    assert not run_marker.is_marked(f"chrome {flag[:-1]}", TOKEN)
    assert not run_marker.is_marked(f"chrome --type=renderer {flag}", TOKEN)
    assert not run_marker.is_marked("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", TOKEN)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins are reparented the POSIX way")
def test_an_orphan_of_this_run_is_found_and_ended_and_nothing_else(stand_ins):
    ours = _orphan(f"--mcp-stealth-test-run={TOKEN}")
    other_run = _orphan("--mcp-stealth-test-run=fedcba9876543210")
    helper = _orphan("--type=renderer", f"--mcp-stealth-test-run={TOKEN}")
    unmarked = _orphan("--user-data-dir=/tmp/x")
    stand_ins.extend([ours, other_run, helper, unmarked])
    found = _leftovers.alive(TOKEN)
    assert set(found) == {ours}, found
    assert _leftovers.starter_gone(found[ours], _leftovers.table_pids())
    assert _leftovers.state_of(found[ours], TOKEN)[0] == _leftovers.SAME
    assert _leftovers.end([(found[ours], _leftovers.SAME, "")], TOKEN) == []
    assert _gone(ours)
    for pid in (other_run, helper, unmarked):
        assert state._pid_alive(pid), f"{pid} was not this run's browser and was signalled"


@pytest.mark.skipif(sys.platform == "win32", reason="start times are POSIX here")
def test_a_pid_that_now_runs_another_process_is_not_signalled(stand_ins):
    pid = _orphan(f"--mcp-stealth-test-run={TOKEN}")
    stand_ins.append(pid)
    found = _leftovers.alive(TOKEN)[pid]
    reused = _leftovers.Browser(found.pid, found.parent, "Mon Jan  1 00:00:00 2001", found.command)
    # Not this run's browser any more: nothing to end, and it is not signalled.
    assert _leftovers.state_of(reused, TOKEN)[0] == _leftovers.DIFFERENT
    assert _leftovers.wait_gone([reused], TOKEN, 0) == []
    assert _leftovers._signal(reused, TOKEN, signal.SIGTERM) is False
    assert state._pid_alive(pid) and not _gone(pid, 1)


def _sub_run(tmp_path: pathlib.Path, body: str, e2e: bool) -> subprocess.CompletedProcess:
    """pytest on a copy of this suite's net, in tmp_path, running one test file."""
    for name in ("conftest.py", "_leftovers.py", "_protocol.py"):
        shutil.copy(pathlib.Path(__file__).parent / name, tmp_path / name)
    (tmp_path / "test_case.py").write_text(textwrap.dedent(body))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "MCP_TEST_LEFTOVER_GRACE": "1"}
    env.pop(run_marker.ENV, None)
    env.pop("MCP_E2E", None)
    if e2e:
        env["MCP_E2E"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "-p", "no:randomly", str(tmp_path / "test_case.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)


LEAKING = """
    import os, subprocess, sys, textwrap
    from mcp_stealth_chrome import run_marker

    def test_leaks():
        launcher = subprocess.run([sys.executable, "-c", textwrap.dedent('''
            import subprocess, sys
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  sys.argv[1]], start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(p.pid)
        '''), run_marker.arg()], capture_output=True, text=True)
        with open("leaked.pid", "w") as f:
            f.write(launcher.stdout.strip())
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins are reparented the POSIX way")
@pytest.mark.parametrize("e2e", [True, False], ids=["after-the-test", "at-the-end"])
def test_a_run_that_leaves_a_browser_fails_and_names_it(tmp_path, e2e):
    result = _sub_run(tmp_path, LEAKING, e2e)
    pid = int((tmp_path / "leaked.pid").read_text())
    try:
        out = result.stdout + result.stderr
        assert result.returncode != 0, out
        assert "browsers left running" in out and f"PID {pid}" in out, out
        where = "test_case.py::test_leaks" if e2e else "end of the run"
        assert f"{where}: PID {pid}" in out, out
        assert "[ended by the suite]" in out, out
        assert _gone(pid)
    finally:
        if state._pid_alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_clean_run_passes(tmp_path):
    result = _sub_run(tmp_path, "def test_nothing():\n    pass\n", e2e=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "browsers left running" not in result.stdout


@pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")
def test_a_launched_chrome_is_marked(tmp_path):
    """The real thing: a server's Chrome carries the run's flag and is found by it."""
    from _protocol import SAFE_ARGS, Client
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(tmp_path / "data")})
    try:
        out = client.call("browser_launch", _timeout=90, headless=True, persistent=False,
                          extra_args=SAFE_ARGS)
        assert not out.failed, out.text
        found = _leftovers.alive(run_marker.token())
        assert found, "no browser of this run carries its flag"
        helpers = [c for _p, _pp, c in state.process_table() or []
                   if "--type=" in c and run_marker.arg() in c]
        assert helpers == [], "helpers are not matched, only browser main processes"
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


# ── unknown is never gone: review of v019-fixes ─────────────────────────────────────────────
# An unreadable process list read as "no browsers", and a PID whose start time could not be
# read read as gone: a browser of the run could stay with the run passing. Unknown now fails
# the run, naming what could not be read, and is never signalled.


def test_an_unreadable_process_list_is_a_failure_not_a_clean_run(monkeypatch):
    monkeypatch.setattr(state, "process_table", lambda: None)
    entries = _leftovers.sweep(TOKEN, "here", only_orphans=False, grace=0)
    assert len(entries) == 1 and "here:" in entries[0]
    assert "process list could not be read" in entries[0], entries


@pytest.mark.skipif(sys.platform == "win32", reason="start times are POSIX here")
def test_a_browser_whose_identity_cannot_be_read_is_reported_and_never_signalled(stand_ins,
                                                                               monkeypatch):
    pid = _orphan(f"--mcp-stealth-test-run={TOKEN}")
    stand_ins.append(pid)
    from mcp_stealth_chrome import _quit_watcher
    # The start time cannot be read, at the scan and after it.
    monkeypatch.setattr(_quit_watcher, "process_starts", lambda pids, legacy=False: None)
    entries = _leftovers.sweep(TOKEN, "here", only_orphans=False, grace=0.3)
    assert len(entries) == 1, entries
    assert f"PID {pid}" in entries[0] and "IDENTITY UNKNOWN" in entries[0], entries
    assert f"the start time of PID {pid}" in entries[0], entries
    assert state._pid_alive(pid) and not _gone(pid, 1), "a process of unknown identity was signalled"


def test_unknown_is_not_gone():
    browser = _leftovers.Browser(424242, 1, None, f"chrome --mcp-stealth-test-run={TOKEN}")
    assert _leftovers.state_of(browser, TOKEN)[0] in (_leftovers.GONE, _leftovers.UNKNOWN)
    import types
    fake = types.SimpleNamespace(process_command=lambda pid: "", process_start=lambda *a: None)
    status, what = _leftovers.state_of(browser, TOKEN, watcher=fake)
    assert status == _leftovers.UNKNOWN and f"the command line of PID {browser.pid}" in what


def test_an_unreadable_parent_list_ends_nothing_and_fails(monkeypatch):
    """Which starters are gone cannot be told: nothing is ended on a guess, and it is a failure."""
    browser = _leftovers.Browser(424242, 99, "x", f"chrome --mcp-stealth-test-run={TOKEN}")
    monkeypatch.setattr(_leftovers, "alive", lambda run: {browser.pid: browser})
    monkeypatch.setattr(_leftovers, "table_pids", lambda: None)
    ended = []
    monkeypatch.setattr(_leftovers, "end", lambda left, run: ended.append(left) or [])
    entries = _leftovers.sweep(TOKEN, "here", only_orphans=True, grace=0)
    assert ended == [] and "process list could not be read" in entries[0], entries


UNREADABLE_AT_THE_END = """
    from mcp_stealth_chrome import state

    def test_the_list_goes_unreadable():
        state.process_table = lambda: None  # stays so for the end-of-run check
"""


def test_a_run_whose_process_list_is_unreadable_at_the_end_fails(tmp_path):
    result = _sub_run(tmp_path, UNREADABLE_AT_THE_END, e2e=False)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "end of the run" in out and "process list could not be read" in out, out


def test_end_never_signals_a_process_of_unknown_identity(monkeypatch):
    """Each layer on its own: end() signals only what is proved SAME..."""
    sent = []
    monkeypatch.setattr(_leftovers, "_signal", lambda b, run, sig: sent.append(b.pid) or True)
    monkeypatch.setattr(_leftovers, "state_of", lambda b, run, watcher=None: (_leftovers.UNKNOWN, "x"))
    browser = _leftovers.Browser(424242, 1, None, f"chrome --mcp-stealth-test-run={TOKEN}")
    left = _leftovers.end([(browser, _leftovers.UNKNOWN, "the start time of PID 424242")], TOKEN)
    assert sent == [] and left == [(browser, _leftovers.UNKNOWN, "the start time of PID 424242")]


@pytest.mark.skipif(sys.platform == "win32", reason="start times are POSIX here")
def test_signal_refuses_a_browser_found_without_a_start_time(stand_ins):
    """...and _signal() refuses one whose start time was never read, even when asked."""
    pid = _orphan(f"--mcp-stealth-test-run={TOKEN}")
    stand_ins.append(pid)
    found = _leftovers.alive(TOKEN)[pid]
    unread = _leftovers.Browser(found.pid, found.parent, None, found.command)
    assert _leftovers._signal(unread, TOKEN, signal.SIGTERM) is False
    assert not _gone(pid, 1)
