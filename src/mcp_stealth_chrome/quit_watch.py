"""The server's side of the per-browser quit watcher (`_quit_watcher.py`).

Starts the watcher detached, keeps its two small state files, and reads its verdict:

- `<key>.tool-closed`, written by the server before tab_close empties a browser on purpose, so
  the watcher does not take the empty browser for a Quit. The watcher removes it once the
  browser has a page again, and when it exits.
- `<key>.quit`, written by the watcher just before it ends a browser whose last window went.
  The server reads it to say "closed because its last window was closed" rather than "crashed",
  and removes it then. A watcher whose server is gone removes it itself.

Both live in a private folder (0700) in the per-user temp dir, files 0600. The key holds the
Chrome PID and its DevTools port, and the verdict names the profile, so a PID handed out again
can never match an old verdict.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import _quit_watcher as _watcher

# Read once at import: a server started before a reinstall keeps starting its own watcher,
# never a newer file that may expect other arguments.
_SCRIPT = (Path(__file__).with_name("_quit_watcher.py")).read_text(encoding="utf-8")
_STALE_AFTER = 24 * 3600


def watch_dir() -> Path:
    override = os.environ.get("MCP_STEALTH_QUIT_WATCH_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "mcp-stealth-quit-watch"


# On the browser object: when its Chrome process started, read once. With the PID and the
# profile this tells one Chrome from a later one on the same PID, profile and port.
START_ATTR = "_mcp_pid_start"


def pid_start(browser: Any, pid: int) -> Optional[str]:
    """The recorded start time of this browser's process, read now when not yet recorded."""
    start = getattr(browser, START_ATTR, None)
    if not start:
        start = _watcher.process_start(pid)
        if start:
            with contextlib.suppress(Exception):
                setattr(browser, START_ATTR, start)
    return start


def _identity(browser: Any) -> Optional[tuple[int, str, int, str]]:
    """(pid, host, port, user_data_dir) of a browser this server started, or None."""
    process = getattr(browser, "_process", None)
    pid = getattr(process, "pid", None) or getattr(browser, "_process_pid", None)
    config = getattr(browser, "config", None)
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    data_dir = getattr(config, "user_data_dir", None)
    if not (isinstance(pid, int) and pid > 0 and host and port and data_dir):
        return None
    return pid, str(host), int(port), str(data_dir)


def _paths(pid: int, port: int, start: Optional[str] = None) -> tuple[Path, Path]:
    # The generation is in the name: a later Chrome on the same PID and port never shares the
    # files of an earlier one's watcher.
    base = watch_dir() / f"chrome-{pid}-{port}-{_watcher.generation(start)}"
    return base.with_suffix(".tool-closed"), base.with_suffix(".quit")


def _browser_paths(browser: Any) -> Optional[tuple[Path, Path]]:
    ident = _identity(browser)
    if ident is None:
        return None
    return _paths(ident[0], ident[2], getattr(browser, START_ATTR, None))


def _interpreter() -> str:
    """The base interpreter, outside any venv: a reinstall rebuilds the tool's venv."""
    base = getattr(sys, "_base_executable", None)
    return base if base and os.path.exists(base) else sys.executable


def _prune(folder: Path) -> None:
    now = time.time()
    with contextlib.suppress(OSError):
        for entry in folder.iterdir():
            if entry.suffix in (".quit", ".tool-closed") and not entry.is_symlink():
                with contextlib.suppress(OSError):
                    if now - entry.lstat().st_mtime > _STALE_AFTER:
                        entry.unlink()


def start(browser: Any, grace: float, close_wait: float, *, max_orphan_seconds: Optional[float] = None,
          pages_matter: bool = True, keep_alive_record: Optional[str] = None
          ) -> Optional[subprocess.Popen]:
    """Start the watcher for a headed browser. None when it cannot run here; the server's own
    sweep then covers that browser.

    For a browser kept alive (keep_alive): `max_orphan_seconds` ends it once it has had no
    server that long, `pages_matter=False` (headless) leaves out the window logic, and
    `keep_alive_record` is removed when Chrome is gone. A watcher already running for this
    browser is ended first, so one browser never has two."""
    if os.name != "posix" or os.environ.get("MCP_STEALTH_QUIT_WATCHER", "1").lower() in (
            "0", "false", "no", "off"):
        return None
    ident = _identity(browser)
    if ident is None:
        return None
    pid, host, port, data_dir = ident
    folder = watch_dir()
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or folder.stat().st_uid != os.getuid():
            return None
        os.chmod(folder, 0o700)
    except OSError:
        return None
    _prune(folder)
    previous = watcher(browser)
    if previous is not None:
        with contextlib.suppress(Exception):
            if previous.poll() is None:
                previous.terminate()
                previous.wait(timeout=2)
    start = pid_start(browser, pid)
    tool_closed, quit_file = _paths(pid, port, start)
    for stale in (tool_closed, quit_file):
        with contextlib.suppress(OSError):
            stale.unlink()
    cfg = {"pid": pid, "host": host, "port": port, "user_data_dir": data_dir,
           "grace": float(grace), "close_wait": float(close_wait), "interval": 0.5,
           "tool_closed_file": str(tool_closed), "quit_file": str(quit_file),
           "server_pid": os.getpid(), "pid_start": start}
    if max_orphan_seconds:
        cfg["max_orphan_seconds"] = float(max_orphan_seconds)
    if not pages_matter:
        cfg["pages_matter"] = False
    if keep_alive_record:
        cfg["keep_alive_record"] = str(keep_alive_record)
    try:
        proc = subprocess.Popen(
            [_interpreter(), "-I", "-", "mcp-stealth-quit-watcher", json.dumps(cfg)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, cwd="/")
        assert proc.stdin is not None
        proc.stdin.write(_SCRIPT.encode("utf-8"))
        proc.stdin.close()
    except Exception:
        return None
    with contextlib.suppress(Exception):
        browser._quit_watcher = proc
    return proc


def watcher(browser: Any) -> Optional[subprocess.Popen]:
    return getattr(browser, "_quit_watcher", None)


def running(browser: Any) -> bool:
    proc = watcher(browser)
    return proc is not None and proc.poll() is None


def stop(browser: Any) -> None:
    """End this browser's watcher before a tool closes the browser itself, so the two never
    both close it. The Popen is this server's own unreaped child, so its PID cannot have been
    reused."""
    proc = watcher(browser)
    if proc is None:
        return
    with contextlib.suppress(Exception):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
    for path in _browser_paths(browser) or ():
        with contextlib.suppress(OSError):
            path.unlink()


def reap(browser: Any) -> None:
    """Collect a finished watcher so it does not stay a zombie while this server runs."""
    proc = watcher(browser)
    if proc is not None:
        with contextlib.suppress(Exception):
            proc.poll()


def mark_tool_closed(browser: Any) -> None:
    """A tool is about to empty the browser on purpose: tell the watcher that is no Quit."""
    paths = _browser_paths(browser)
    if paths is None or not running(browser):
        return
    tool_closed, _ = paths
    with contextlib.suppress(OSError):
        fd = os.open(tool_closed, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"at": time.time()}, handle)


def _verdict(browser: Any) -> Optional[dict]:
    """The watcher's verdict file for exactly this browser and process generation, or None."""
    ident = _identity(browser)
    if ident is None:
        return None
    pid, _host, port, data_dir = ident
    _, quit_file = _paths(pid, port, getattr(browser, START_ATTR, None))
    try:
        verdict = json.loads(quit_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(verdict, dict) or verdict.get("pid") != pid \
            or verdict.get("user_data_dir") != data_dir \
            or verdict.get("pid_start") != getattr(browser, START_ATTR, None):
        return None
    return verdict


def not_ended_note(browser: Any) -> Optional[str]:
    """Why the watcher left this browser running after its last window went, or None."""
    verdict = _verdict(browser)
    if not verdict or not verdict.get("not_ended"):
        return None
    return (f"its last window was closed, but the quit watcher stopped without ending Chrome PID "
            f"{verdict['pid']}: {verdict['not_ended']}. browser_close tries again and reports what it "
            f"could verify.")


def quit_time(browser: Any) -> Optional[float]:
    """When the watcher ended this browser because its last window went, or None."""
    verdict = _verdict(browser)
    if verdict is None or verdict.get("not_ended"):
        return None
    at = verdict.get("at")
    return float(at) if isinstance(at, (int, float)) else time.time()


def forget(browser: Any) -> None:
    """Drop the verdict once the server has recorded it."""
    for path in _browser_paths(browser) or ():
        with contextlib.suppress(OSError):
            path.unlink()


def closed_note(instance_id: str, at: float) -> str:
    return (f"instance {instance_id!r} was closed at {time.strftime('%H:%M:%S', time.localtime(at))} "
            f"because its last window was closed (Quit, or closing every window).")


def unmark_tool_closed(browser: Any) -> None:
    """Take back mark_tool_closed: the tab_close that wrote it did not close its tab."""
    ident = _identity(browser)
    if ident is None:
        return
    tool_closed, _ = _paths(ident[0], ident[2])
    with contextlib.suppress(OSError):
        tool_closed.unlink()
