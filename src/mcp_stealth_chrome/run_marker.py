"""A mark on every Chrome a test run starts, so the run can find and end exactly those.

A headed Chrome a block_ads test started stayed running for 8 hours (parent PID 1, a uc_* temp
profile, no CDP client) and showed in the person's Dock. Nothing could tell it from the person's
own Chrome, or from one another worker's test run started on the same machine, except by
guessing from its name. So tests/conftest.py sets MCP_STEALTH_TEST_RUN to a token of its own,
every Chrome this server spawns while it is set carries --mcp-stealth-test-run=<token> (Chrome
ignores a switch it does not know, and pages cannot read the command line), and the suite finds
its browsers by that exact flag on the command line. Unset, nothing is added.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

ENV = "MCP_STEALTH_TEST_RUN"
FLAG = "--mcp-stealth-test-run"
# Hex only: nodriver's add_argument refuses an argument holding "lang", "headless" or "data-dir".
_TOKEN = re.compile(r"^[0-9a-f]{8,64}$")


def token() -> Optional[str]:
    value = os.environ.get(ENV, "")
    return value if _TOKEN.match(value) else None


def arg(run: Optional[str] = None) -> Optional[str]:
    run = run or token()
    return f"{FLAG}={run}" if run else None


def mark(config: Any) -> None:
    """Add the run's flag to a nodriver Config about to spawn a Chrome. Never raises."""
    flag = arg()
    if flag is None:
        return
    try:
        if flag not in (getattr(config, "browser_args", None) or []):
            config.add_argument(flag)
    except Exception:
        pass


def is_marked(command: str, run: str) -> bool:
    """A browser main process (helpers carry --type=) whose command line has exactly this run's
    flag: the value ends at the end of the line or at whitespace."""
    return ("--type=" not in command
            and re.search(re.escape(f"{FLAG}={run}") + r"(?=\s|$|\")", command) is not None)


def marked(run: str, table: Optional[list] = None) -> Optional[list[tuple[int, str]]]:
    """(pid, command) of every live browser main process of this run; None when the process list
    cannot be read, which is not the same as none running."""
    if table is None:
        from .state import process_table
        table = process_table()
    if table is None:
        return None
    own = os.getpid()
    return [(pid, command) for pid, _ppid, command in table
            if pid != own and command and is_marked(command, run)]
