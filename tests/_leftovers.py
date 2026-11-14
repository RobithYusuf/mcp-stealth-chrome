"""Browsers a test run left running: found by the run's own flag, ended by PID and identity.

Every Chrome the suite starts carries --mcp-stealth-test-run=<token> (run_marker), the token
being this run's own. Nothing here looks at an application name: the person's Chrome, and the
browsers another test run on the same machine started, never carry this token.

Each signal goes through a check right before it: the PID still runs a browser main process with
this run's flag and, where the platform gives one, the start time it had when it was found.

Unknown is never gone. A process list that cannot be read, or a PID whose command line or start
time cannot be, is reported as a failure naming what could not be read, and such a process is
never signalled. Read as "nothing there", a browser of the run could stay with the run passing.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

from mcp_stealth_chrome import _quit_watcher, run_marker, state

SAME, GONE, DIFFERENT, UNKNOWN = "same", "gone", "different", "unknown"
UNREADABLE_LIST = "the process list could not be read, so whether browsers of this run are left is unknown"


@dataclass(frozen=True)
class Browser:
    pid: int
    parent: int
    start: Optional[str]
    command: str

    def describe(self) -> str:
        return f"PID {self.pid} (parent {self.parent}): {self.command[:300]}"


def alive(run: str) -> Optional[dict[int, Browser]]:
    """This run's live browser main processes by PID; None when the process list is unreadable."""
    table = state.process_table()
    if table is None:
        return None
    rows = {pid: (parent, command) for pid, parent, command in table
            if command and run_marker.is_marked(command, run)}
    starts = _quit_watcher.process_starts(list(rows)) or {}
    return {pid: Browser(pid, parent, starts.get(pid), command) for pid, (parent, command) in rows.items()}


def starter_gone(browser: Browser, table_pids: set[int]) -> bool:
    """Its parent is gone (reparented to init or launchd, or no longer listed): nothing that
    started it is left to end it."""
    return browser.parent <= 1 or browser.parent not in table_pids


def table_pids() -> Optional[set[int]]:
    """Every listed PID; None when the list cannot be read."""
    table = state.process_table()
    return None if table is None else {pid for pid, _parent, _command in table}


def state_of(browser: Browser, run: str, watcher: Any = None) -> tuple[str, str]:
    """(SAME | GONE | DIFFERENT | UNKNOWN, what could not be read when UNKNOWN)."""
    watcher = watcher or _quit_watcher
    if sys.platform == "win32" and watcher is _quit_watcher:
        # No start time here; the exact command line, read again, is the identity.
        table = state.process_table()
        if table is None:
            return UNKNOWN, "the process list"
        for pid, _parent, command in table:
            if pid == browser.pid:
                return (SAME, "") if command == browser.command else (DIFFERENT, "")
        return GONE, ""
    command = watcher.process_command(browser.pid)
    if command is None:
        return GONE, ""
    if command == "":
        return UNKNOWN, f"the command line of PID {browser.pid}"
    if not run_marker.is_marked(command, run):
        return DIFFERENT, ""
    if not browser.start:
        return UNKNOWN, f"the start time of PID {browser.pid}"
    now = watcher.process_start(browser.pid, browser.start)
    if now is None:
        if watcher.process_command(browser.pid) is None:
            return GONE, ""
        return UNKNOWN, f"the start time of PID {browser.pid}"
    return (SAME, "") if now == browser.start else (DIFFERENT, "")


def _signal(browser: Browser, run: str, sig: int) -> bool:
    """Only a process proved to be this browser, checked right before the signal."""
    if sys.platform == "win32":
        if state_of(browser, run)[0] != SAME:
            return False
        try:
            os.kill(browser.pid, sig)  # TerminateProcess
            return True
        except OSError:
            return False
    if not browser.start:
        return False
    return _quit_watcher.signal_if_same(browser.pid, sig, browser.start,
                                        lambda command: run_marker.is_marked(command, run))


def wait_gone(browsers: list[Browser], run: str, within: float) -> list[tuple[Browser, str, str]]:
    """(browser, SAME or UNKNOWN, what was unreadable) for those not known to be gone after
    `within` seconds; polled, so it returns as soon as every one is gone (or now another process)."""
    deadline = time.monotonic() + within
    left = [(b, *state_of(b, run)) for b in browsers]
    while True:
        left = [(b, status, what) for b, status, what in left if status in (SAME, UNKNOWN)]
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)
        left = [(b, *state_of(b, run)) for b, _status, _what in left]


def end(left: list[tuple[Browser, str, str]], run: str) -> list[tuple[Browser, str, str]]:
    """SIGTERM, then SIGKILL 5 s later, to those proved to be this run's browsers. Returns those
    not ended: still SAME, or UNKNOWN (never signalled). Helpers go with their browser."""
    proved = [b for b, status, _what in left if status == SAME]
    for browser in proved:
        _signal(browser, run, signal.SIGTERM)
    still = wait_gone(proved, run, 5.0)
    for browser, status, _what in still:
        if status == SAME:
            _signal(browser, run, getattr(signal, "SIGKILL", signal.SIGTERM))
    return [*wait_gone([b for b, _s, _w in still], run, 5.0),
            *[(b, status, what) for b, status, what in left if status == UNKNOWN]]


def sweep(run: str, where: str, only_orphans: bool, grace: float) -> list[str]:
    """End what this run left and say so: one line per browser, or per thing that could not be
    read. Empty means nothing of this run was left, positively."""
    now = alive(run)
    if now is None:
        return [f"{where}: {UNREADABLE_LIST}"]
    candidates = list(now.values())
    if only_orphans and candidates:
        listed = table_pids()
        if listed is None:
            return [f"{where}: {UNREADABLE_LIST}; nothing was ended on a guess"]
        candidates = [b for b in candidates if starter_gone(b, listed)]
    left = wait_gone(candidates, run, grace)
    if not left:
        return []
    stuck = {b.pid: (status, what) for b, status, what in end(left, run)}
    lines = []
    for browser, _status, _what in left:
        status, what = stuck.get(browser.pid, (GONE, ""))
        if status == UNKNOWN:
            tail = f" [IDENTITY UNKNOWN: {what} could not be read, so it was not signalled]"
        elif status == SAME:
            tail = " [COULD NOT BE ENDED]"
        else:
            tail = " [ended by the suite]"
        lines.append(f"{where}: {browser.describe()}{tail}")
    return lines
