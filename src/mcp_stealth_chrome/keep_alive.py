"""Browsers left running when their server goes, and how a later server finds them again.

`browser_launch(keep_alive=True)` and `spawn_browser(keep_alive=True)` mark one browser to stay
open when this server exits (its client went to sleep, reconnected, or closed: stdin EOF). A
small record describes it, one file per profile in `keep-alive/` under the data folder
(`MCP_STEALTH_KEEP_ALIVE_DIR`), written 0600 through a temporary file and a rename:

    {"version": 1, "profile": "/abs/profile", "pid": 4242, "pid_start": "Fri Nov 13 10:00:00 2026",
     "host": "127.0.0.1", "port": 53111,
     "user_data_dir": "/profile/as/given", "instance_id": "main", "chrome_version": "Chrome/157.0.8103.39", "headless": false,
     "fingerprint": {...}, "server_pid": 777, "watcher_pid": 778, "watcher_start": "...",
     "state": "held",
     "launched_at": ..., "kept_at": null, "tab_order": [...], "active": "...",
     "max_hours": 24.0}

It is written when the browser starts (state "held"), so a server that is killed rather than
closed still leaves it findable, and again at exit (state "kept", with the tab order).

A later launch on the same profile reattaches only when `check` finds positive evidence that the
Chrome is the one the record names and that nobody holds it: the PID holds the profile's
SingletonLock, runs a browser whose command line names exactly that --user-data-dir, the
recorded DevTools port and flags only this server passes, started at the recorded time,
DevTools answers on that port with the same Chrome version and names that same PID as its
browser process (SystemInfo.getProcessInfo), and the server that wrote the record is gone.
The check is taken under a per-profile flock held until the record names the new server, and
repeated over the connection once made. No proxy is ever written here. Anything else is not ours to take: the person's own Chrome, or a browser a
live server still drives.

A kept browser has a quit watcher (`_quit_watcher.py`) that outlives the server. It still ends
the browser on Quit when headed, and ends it once it has had no server for `max_hours`
(`MCP_STEALTH_KEEP_ALIVE_MAX_HOURS`, 24 by default). The watcher removes the record when Chrome
is gone.

Standard library and this package's state helpers only, no import of server.py: `mark_kept`
runs at stdin EOF, after the event loop has closed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import signal
import time
from pathlib import Path
from typing import Any, Optional

from . import _quit_watcher as _watcher
from . import state as _state

ENV_DIR = "MCP_STEALTH_KEEP_ALIVE_DIR"
ENV_MAX_HOURS = "MCP_STEALTH_KEEP_ALIVE_MAX_HOURS"
VERSION = 1
DEFAULT_MAX_HOURS = 24.0
# Flags every launch of this server passes (browser_launch and spawn_browser alike). A Chrome
# without them was not started here.
OUR_FLAGS = ("--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
             "--disable-restore-session-state")
ATTR = "_mcp_keep_alive"


def supported() -> Optional[str]:
    """None when keep_alive can work here, or why it cannot."""
    if os.name != "posix":
        return ("keep_alive is not available on Windows: no quit watcher runs there, so a "
                "browser left running could not be ended by Quit or by the time cap")
    if os.environ.get("MCP_STEALTH_QUIT_WATCHER", "1").lower() in ("0", "false", "no", "off"):
        return ("keep_alive needs the quit watcher, which MCP_STEALTH_QUIT_WATCHER turns off: "
                "a browser left running could not be ended by Quit or by the time cap")
    return None


# A year. The cap is a promise that a forgotten browser ends; "inf" would quietly withdraw it.
MAX_HOURS_LIMIT = 24.0 * 365


def _max_hours_read() -> tuple[float, Optional[str]]:
    raw = os.environ.get(ENV_MAX_HOURS)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_HOURS, None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_MAX_HOURS, (f"{ENV_MAX_HOURS}={raw!r} is not a positive finite number of hours, "
                                   f"so the default {DEFAULT_MAX_HOURS:g} h applies")
    if value > MAX_HOURS_LIMIT:
        return MAX_HOURS_LIMIT, (f"{ENV_MAX_HOURS}={raw!r} is longer than a year, so it was cut to "
                                 f"{MAX_HOURS_LIMIT:g} h")
    return value, None


def max_hours() -> float:
    """Hours a kept browser may run with no server: positive and finite, a year at most."""
    return _max_hours_read()[0]


def max_hours_problem() -> Optional[str]:
    """Why the configured cap was not used as given, or None."""
    return _max_hours_read()[1]


def proxy_from_command(command: Optional[str]) -> Optional[str]:
    """The --proxy-server a running Chrome was given. The record never holds it: a proxy URL can
    carry a password, and the running Chrome's own command line already says it."""
    found = re.search(r"--proxy-server=(\S+)", command or "")
    return found.group(1) if found else None


def record_dir() -> Path:
    override = os.environ.get(ENV_DIR)
    return Path(override).expanduser() if override else _state.data_root() / "keep-alive"


def profile_key(profile: Any) -> str:
    return os.path.realpath(os.path.expanduser(str(profile)))


def record_path(profile: Any) -> Path:
    digest = hashlib.sha256(profile_key(profile).encode("utf-8")).hexdigest()[:32]
    return record_dir() / f"{digest}.json"


def lock_path(profile: Any) -> Path:
    return record_path(profile).with_suffix(".lock")


def _take_lock(profile: Any) -> Optional[int]:
    """A lock on reattaching to this profile, across processes, or None when another holds it.

    flock is released by the kernel when its holder exits, so a server that died mid-reattach
    leaves nothing to clean up. POSIX only, as keep_alive is."""
    import fcntl
    path = lock_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(verdict: Optional[dict]) -> None:
    """Let the next server try. Safe to call on any verdict, more than once."""
    fd = (verdict or {}).pop("lock_fd", None)
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write(path: Path, data: dict) -> None:
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise OSError(f"{folder} is a link")
    tmp = folder / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read(profile: Any) -> Optional[dict]:
    """The record for this profile, or None. A file that is a link, belongs to someone else,
    or names another profile is not a record."""
    path = record_path(profile)
    try:
        info = os.lstat(path)
        if not os.path.isfile(path) or os.path.islink(path):
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION \
            or data.get("profile") != profile_key(profile):
        return None
    if not all(isinstance(data.get(k), int) and data[k] > 0 for k in ("pid", "port")):
        return None
    return data


def write(profile: Any, **fields: Any) -> Optional[str]:
    """Write or update the record for a profile. None, or why it could not be written."""
    current = read(profile) or {}
    data = {**current, **fields, "version": VERSION, "profile": profile_key(profile)}
    data.pop("proxy", None)  # never at rest: it can carry a password (proxy_from_command)
    try:
        _write(record_path(profile), data)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def remove(profile: Any, pid: Optional[int] = None) -> bool:
    """Remove the record, only when it names `pid` if one is given."""
    record = read(profile)
    if record is None:
        return False
    if pid is not None and record.get("pid") != pid:
        return False
    with contextlib.suppress(OSError):
        record_path(profile).unlink()
        return True
    return False


def _alive(pid: Any) -> bool:
    return isinstance(pid, int) and pid > 0 and _state._pid_alive(pid)


def still_running(record: dict) -> Optional[bool]:
    """Whether the Chrome a record names still runs as that browser: True, False on positive
    evidence that it does not (gone, its PID now runs something else, the lock is another's),
    None when that cannot be told (its command line could not be read)."""
    pid = record.get("pid")
    given = str(record.get("user_data_dir") or "")
    if not _alive(pid) or not given:
        return False
    command = _watcher.process_command(pid)
    if command is None:
        return False
    if _state.chrome_lock_holder_pid(Path(profile_key(given))) != pid:
        return False
    if command == "":
        return None
    if not _state._is_browser_main_process(command) or not _watcher.names_profile(command, given):
        return False
    started = record.get("pid_start")
    if started and _watcher.process_start(pid, started) not in (None, started):
        return False
    return True


def live_profiles() -> set[str]:
    """Profiles whose record names a Chrome that still runs, as real paths (for profile_gc).

    A PID being alive proves nothing: it can be handed to another program. A record whose
    Chrome is positively gone is removed, so a stale one cannot hold a profile forever; one
    that cannot be told still holds it, and the lock and process checks that follow stand."""
    found: set[str] = set()
    folder = record_dir()
    with contextlib.suppress(OSError):
        for entry in folder.iterdir():
            if entry.suffix != ".json" or entry.is_symlink():
                continue
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(entry.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not data.get("profile"):
                    continue
                verdict = still_running(data)
                if verdict is False:
                    remove(data["profile"], data.get("pid"))
                    continue
                found.add(str(data["profile"]))
    return found


def _browser_pid(host: str, port: int) -> Optional[int]:
    """The PID of the browser process answering on this DevTools port (SystemInfo.getProcessInfo)."""
    result = _watcher.browser_call(host, port, "SystemInfo.getProcessInfo")
    for row in (result or {}).get("processInfo") or []:
        if isinstance(row, dict) and row.get("type") == "browser" and isinstance(row.get("id"), int):
            return row["id"]
    return None


def _json_version(host: str, port: int) -> Optional[dict]:
    body = _watcher.http_get(host, port, "/json/version", timeout=2.0)
    if body is None:
        return None
    with contextlib.suppress(ValueError):
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    return None


def _server_holding(record: dict) -> Optional[int]:
    """The PID of a live server of ours that wrote this record, or None when it is gone."""
    from .profile_gc import is_server_command
    pid = record.get("server_pid")
    if not _alive(pid):
        return None
    command = _watcher.process_command(pid)
    if command is None:
        return None
    if command == "":
        return pid  # could not look: a live process may be that server, so it holds it
    return pid if is_server_command(command) else None


def check(profile: Any, hold: bool = False) -> dict:
    """Whether a later launch may reattach to the browser kept on this profile.

    {"ok": True, "record": ...} only on positive evidence on every point; otherwise
    {"ok": False, "reason": ..., "record": ... or None, "holder": pid or None}.

    hold=True first takes the profile's reattach lock and, on an ok verdict, keeps it in
    "lock_fd" until release_lock: two servers that both found the browser free would
    otherwise both connect. The one that gets the lock writes itself into the record before
    letting go, so the other then reads a live server holding it.
    """
    if not hold:
        return _check(profile)
    try:
        fd = _take_lock(profile)
    except OSError as exc:
        return {"ok": False, "record": read(profile), "holder": None,
                "reason": f"the reattach lock could not be taken ({type(exc).__name__}: {exc})"}
    if fd is None:
        return {"ok": False, "record": read(profile), "holder": None,
                "reason": "another server is reattaching to it right now"}
    verdict = _check(profile)
    if verdict.get("ok"):
        verdict["lock_fd"] = fd
    else:
        with contextlib.suppress(OSError):
            os.close(fd)
    return verdict


def identity_problem(record: dict) -> Optional[str]:
    """Why the process a record names is not, right now, the Chrome it recorded; None when it is.
    Lock, command line (exact profile, port and flags) and start time; no network."""
    pid, port = record["pid"], record["port"]
    given = str(record.get("user_data_dir") or "")
    key = profile_key(given) if given else ""
    if not _alive(pid):
        return f"the kept Chrome (PID {pid}) is no longer running"
    holder = _state.chrome_lock_holder_pid(Path(key))
    if holder != pid:
        return (f"the profile lock is held by PID {holder}, not by the kept Chrome PID {pid}"
                if holder else f"PID {pid} does not hold the profile lock")
    command = _watcher.process_command(pid)
    if not command:
        return f"the command line of PID {pid} could not be read"
    if not _state._is_browser_main_process(command):
        return f"PID {pid} is not a browser's main process"
    if not _watcher.names_profile(command, given):
        return f"PID {pid} was not started by this server (its command line lacks --user-data-dir={given})"
    if not _watcher.has_flag(command, "--remote-debugging-port", port):
        return f"PID {pid} was not started by this server (its command line lacks --remote-debugging-port={port})"
    for flag in OUR_FLAGS:
        if not re.search(re.escape(flag) + r"(?=\s|$)", command):
            return f"PID {pid} was not started by this server (its command line lacks {flag})"
    if not record.get("pid_start"):
        return "the record names no process start time, so PID reuse could not be ruled out"
    now = _watcher.process_start(pid, record["pid_start"])
    if now is None:
        return f"the start time of PID {pid} could not be read"
    if now != record["pid_start"]:
        return (f"PID {pid} now belongs to a process started {now}, not the kept Chrome started "
                f"{record['pid_start']}")
    return None


def _check(profile: Any) -> dict:
    key = profile_key(profile)
    record = read(key)
    if record is None:
        return {"ok": False, "reason": "no keep_alive record for this profile", "record": None}
    pid, port, host = record["pid"], record["port"], str(record.get("host") or "127.0.0.1")
    out: dict[str, Any] = {"ok": False, "record": record, "holder": None}
    # The --user-data-dir exactly as Chrome was given it: /var/folders and /private/var/folders
    # are one folder on macOS, but only one of them is on the command line.
    given = str(record.get("user_data_dir") or "")
    if not given or profile_key(given) != key:
        out["reason"] = "the record does not name this profile's folder"
        return out
    if os.name != "posix":
        out["reason"] = supported()
        return out
    if not _alive(pid):
        remove(key, pid)
        out["reason"] = f"the kept Chrome (PID {pid}) is no longer running"
        return out
    out["holder"] = _state.chrome_lock_holder_pid(Path(key))
    problem = identity_problem(record)
    if problem:
        out["reason"] = problem
        return out
    command = _watcher.process_command(pid)
    version = _json_version(host, port)
    if version is None:
        out["reason"] = f"DevTools did not answer on {host}:{port}"
        return out
    if not record.get("chrome_version"):
        out["reason"] = "the record names no Chrome version, so the browser on that port cannot be matched to it"
        return out
    if version.get("Browser") != record["chrome_version"]:
        out["reason"] = (f"DevTools on port {port} answers as {version.get('Browser')!r}, "
                         f"not the recorded {record['chrome_version']!r}")
        return out
    # The port could belong to some other Chrome: the browser answering there must say it is
    # this very PID.
    if _browser_pid(host, port) != pid:
        out["reason"] = f"the browser answering on port {port} is not PID {pid}"
        return out
    server = _server_holding(record)
    if server == os.getpid():
        out["reason"] = "this server already holds that browser"
        return out
    if server is not None:
        out["reason"] = f"the server PID {server} that started it is still running and holds it"
        out["server_pid"] = server
        return out
    out["ok"] = True
    out["reason"] = "ours, and no server holds it"
    out["proxy"] = proxy_from_command(command)
    return out


def end_old_watcher(record: dict) -> str:
    """End the quit watcher a previous server started for this Chrome, so only the new one acts.

    Only a process whose command line is that watcher for this very Chrome PID and port."""
    pid = record.get("watcher_pid")
    if not _alive(pid):
        return "none running"

    def its_watcher(command: str) -> bool:
        return (_watcher.NAME in command and f'"pid": {record["pid"]},' in command
                and f'"port": {record["port"]},' in command)

    if not its_watcher(_watcher.process_command(pid) or ""):
        return f"PID {pid} is not its watcher, left alone"
    # SIGTERM first; a watcher started by an older server inherited its ignored SIGTERM, so
    # SIGKILL after. Its only cleanup is two state files the new watcher clears on start.
    # Each signal only to the process with the recorded start time, checked right before.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _watcher.signal_if_same(pid, sig, record.get("watcher_start"), its_watcher):
            return ("ended" if _watcher.process_command(pid) is None
                    else f"PID {pid} could not be proved to be its watcher, left alone")
        for _ in range(30):
            if not _alive(pid) or _watcher.process_command(pid) is None:
                return "ended"
            time.sleep(0.05)
    return "still running"


def mark_kept(profile: Any, pid: int, tab_order: list[str], active: Optional[str]) -> Optional[str]:
    """At exit: the browser stays, and a later server may take it. None, or why not written."""
    record = read(profile)
    if record is None or record.get("pid") != pid:
        return "no record for this browser"
    return write(profile, state="kept", kept_at=time.time(), tab_order=list(tab_order),
                 active=active)


def release(browser: Any) -> None:
    """Leave a browser this process started running when the process exits.

    nodriver registers every browser for an exit hook that stops it, and asyncio's subprocess
    transport kills a child still running when it is closed or collected. Both are cut here;
    the process itself is not touched.
    """
    with contextlib.suppress(Exception):
        import nodriver
        nodriver.core.util.get_registered_instances().discard(browser)
    process = getattr(browser, "_process", None)
    transport = getattr(process, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport._proc = None  # close() then has no child to kill
    # From here nodriver's stop() has nothing to signal either.
    with contextlib.suppress(Exception):
        browser._process = None


def close_at_exit(host: str, port: int, pid: int, profile: str, start: Optional[str],
                  wait: float = 5.0) -> str:
    """End a browser this server drives but did not spawn (a reattached one), with no event loop:
    Browser.close over the websocket, then SIGTERM to that one PID only while it is provably the
    same process (profile, browser main process, start time), checked right before."""
    if _watcher.gone(pid, profile, start):
        return "closed"
    _watcher.request_browser_close(host, port)
    if _watcher.wait_gone(pid, profile, wait, start):
        return "closed"
    if _watcher.signal_if_same(pid, signal.SIGTERM, start, _watcher.browser_of(profile)):
        if _watcher.wait_gone(pid, profile, 2.0, start):
            return "signalled"
    return "still running" if not _watcher.gone(pid, profile, start) else "closed"


def describe(record: dict) -> str:
    when = record.get("kept_at") or record.get("launched_at")
    age = ""
    if isinstance(when, (int, float)):
        minutes = max(0, int((time.time() - when) / 60))
        age = f", left running {minutes} min ago" if record.get("kept_at") else ""
    return f"pid {record['pid']}, port {record['port']}{age}"
