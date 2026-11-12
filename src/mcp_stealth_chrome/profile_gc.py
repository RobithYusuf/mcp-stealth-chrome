"""Remove the profiles this server leaves behind, and nothing that could hold a login.

Two kinds pile up in the data folder (~/.mcp-stealth unless MCP_STEALTH_DATA_DIR says
otherwise):

- privacy-profiles/<name>: one per privacy launch, removed at close, left behind when a
  server dies first.
- profile-pid<N>: the fallback a server takes when another holds the shared profile.
  Nothing removes it, and someone may have logged in there.

One pass runs on a background thread when the server starts. A folder goes only when every
check says it is safe: an exact name directly in its root, not a link, not locked, no live
process naming it, not held by this server, untouched for GC_MIN_AGE, and for a fallback an
owner that is gone and nothing a person would miss. Anything unreadable counts as data.
What the pass did is kept for server_status, so nothing is removed in silence.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import state as _state

ENV_SWITCH = "MCP_STEALTH_PROFILE_GC"
# A folder newer than this can belong to a launch still starting: mkdtemp comes before Chrome
# writes its lock.
GC_MIN_AGE = float(os.environ.get("MCP_STEALTH_PROFILE_GC_MIN_AGE", "600"))
_START_DELAY = float(os.environ.get("MCP_STEALTH_PROFILE_GC_DELAY", "3"))

FALLBACK_NAME = re.compile(r"^profile-pid([0-9]+)$")


def is_server_command(command: str) -> bool:
    """Whether a command line runs this server, judged by how it starts, not by its paths.

    `.../mcp-stealth-chrome/.venv/bin/python other.py` only lives in a folder of that name:
    counting it made a recycled PID look like a server still running.
    """
    for token in command.split():
        if os.path.basename(token.rstrip("/")) == "mcp-stealth-chrome":
            return True
        if "mcp_stealth_chrome" in token and "/" not in token:
            return True  # -m mcp_stealth_chrome, or code importing it
    return False

_LOCK = threading.Lock()
_LAST: dict[str, Any] = {"state": "not run"}


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in ("0", "false", "no", "off")


def status() -> dict:
    """What the last pass did, for server_status. Starts nothing."""
    with _LOCK:
        return json.loads(json.dumps(_LAST, default=str))


# ── facts about a folder ───────────────────────────────────────────────────────────────────

def _processes() -> Optional[list[tuple[int, str]]]:
    """Every process with its full command line, or None when the list cannot be read."""
    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    rows = []
    for line in out.splitlines():
        head, _, command = line.strip().partition(" ")
        if head.isdigit():
            rows.append((int(head), command))
    return rows


def _names_path(command: str, path: Path) -> bool:
    """Whether a command line refers to this folder, as itself or as a parent of a file."""
    for text in {str(path), os.path.realpath(path)}:
        if re.search(re.escape(text) + r"""(?=$|[\s"'/=,])""", command):
            return True
    return False


def _holders(path: Path, processes: list[tuple[int, str]]) -> list[int]:
    return [pid for pid, command in processes if pid != os.getpid() and _names_path(command, path)]


def _newest_change(path: Path) -> float:
    """The latest change to the folder or anything one or two levels inside it."""
    newest = 0.0
    try:
        newest = os.lstat(path).st_mtime
        for entry in os.scandir(path):
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            if entry.is_dir(follow_symlinks=False):
                for inner in os.scandir(entry.path):
                    newest = max(newest, inner.stat(follow_symlinks=False).st_mtime)
    except OSError:
        return time.time()  # unreadable reads as just changed, so it is left alone
    return newest


def _tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def _is_direct_child(path: Path, root: Path) -> bool:
    """A real directory sitting directly in root, reached without any link on the way."""
    try:
        if path.is_symlink() or not path.is_dir() or root.is_symlink():
            return False
        return path.resolve().parent == root.resolve() and path.parent == root
    except OSError:
        return False


def _recorded_profiles() -> set[str]:
    """Every profile folder this server still holds, in any session or the pool."""
    held: set[str] = set()
    affinity = _state.get_profile_affinity()
    if affinity is not None:
        held.add(os.path.realpath(affinity))
    for session in list(getattr(_state, "_SESSIONS", {}).values()):
        current = getattr(session, "current_profile_dir", None)
        if current:
            held.add(os.path.realpath(current))
        for snap in list(getattr(session, "instances", {}).values()):
            if getattr(snap, "profile_dir", None):
                held.add(os.path.realpath(snap.profile_dir))
    return held


def _owner_gone(pid: int, processes: Optional[list[tuple[int, str]]]) -> Optional[bool]:
    """True when the server that made profile-pid<pid> has ended, None when it cannot be told.

    The number can belong to another program by now. It still counts as gone only when that
    program's command line can be read and is not this server.
    """
    if pid == os.getpid():
        return False
    if not _state._pid_alive(pid):
        return True
    if processes is None:
        return None
    command = next((c for p, c in processes if p == pid), None)
    if command is None:
        return None
    return not is_server_command(command)


# ── what a fallback profile holds ──────────────────────────────────────────────────────────

def _sqlite_count(db: Path, sql: str) -> int:
    """Rows in a Chrome database, read without taking its lock. Raises when unreadable."""
    if not db.exists():
        return 0
    if db.is_symlink():
        raise OSError(f"{db.name} is a link")
    uri = f"file:{quote(str(db))}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _sqlite_count_optional(db: Path, table: str) -> int:
    """Rows in a table that some Chrome versions do not have. Raises when the file is unreadable."""
    if not db.exists():
        return 0
    if db.is_symlink():
        raise OSError(f"{db.name} is a link")
    con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            return 0
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        con.close()


def _top_domains(db: Path, limit: int = 5) -> list[str]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        rows = con.execute(
            "SELECT host_key, COUNT(*) AS n FROM cookies GROUP BY host_key ORDER BY n DESC LIMIT ?",
            (limit,)).fetchall()
        return [str(host).lstrip(".") for host, _n in rows]
    finally:
        con.close()


def _leveldb_has_data(folder: Path) -> bool:
    """A LevelDB store holds something once a table file or a non-empty write log exists."""
    if not folder.exists():
        return False
    for entry in folder.rglob("*"):
        if entry.is_symlink():
            return True  # not ours to judge
        if entry.suffix == ".ldb" or (entry.suffix == ".log" and entry.stat().st_size > 0):
            return True
    return False


def _session_files(sub: Path) -> int:
    """Saved tabs and windows: non-empty files under Sessions/, and the older single files."""
    count = 0
    folder = sub / "Sessions"
    if folder.is_symlink():
        raise OSError("Sessions is a link")
    if folder.exists():
        count += sum(1 for f in folder.iterdir() if f.is_file() and f.stat().st_size > 0)
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        f = sub / name
        if f.exists() and f.stat().st_size > 0:
            count += 1
    return count


def _bookmark_count(path: Path) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    pending = list((data.get("roots") or {}).values())
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        if node.get("type") == "url":
            count += 1
        pending.extend(node.get("children") or [])
    return count


def _subprofiles(profile: Path) -> list[Path]:
    found = []
    for entry in sorted(profile.iterdir()):
        if entry.is_dir() and not entry.is_symlink() and (
                entry.name == "Default" or entry.name.startswith("Profile ")):
            found.append(entry)
    return found


def contents(profile: Path) -> dict:
    """What a person could lose with this profile, without a single secret value.

    `readable` is False when any part could not be read, and then `has_data` is True:
    a database that cannot be opened may well hold a login.
    """
    out: dict[str, Any] = {"cookies": 0, "cookie_domains": [], "logins": 0, "history": 0, "autofill": 0,
                           "top_sites": 0, "sessions": 0,
                           "local_storage": False, "indexeddb": False, "extensions": 0,
                           "bookmarks": 0, "readable": True, "unreadable": []}
    try:
        subs = _subprofiles(profile)
    except OSError as exc:
        return {**out, "readable": False, "unreadable": [f"listing: {exc}"], "has_data": True}
    for sub in subs:
        label = sub.name
        for db in (sub / "Network" / "Cookies", sub / "Cookies"):
            try:
                out["cookies"] += _sqlite_count(db, "SELECT COUNT(*) FROM cookies")
                for host in _top_domains(db):
                    if host not in out["cookie_domains"]:
                        out["cookie_domains"].append(host)
            except Exception as exc:
                out["readable"] = False
                out["unreadable"].append(f"{label}/{db.relative_to(sub)}: {type(exc).__name__}")
        for name in ("Login Data", "Login Data For Account"):
            try:
                out["logins"] += _sqlite_count(sub / name, "SELECT COUNT(*) FROM logins")
            except Exception as exc:
                out["readable"] = False
                out["unreadable"].append(f"{label}/{name}: {type(exc).__name__}")
        try:
            out["history"] += _sqlite_count(sub / "History", "SELECT COUNT(*) FROM urls")
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/History: {type(exc).__name__}")
        try:
            # Autofill entries, saved addresses and cards. A table a Chrome version lacks is no row.
            out["autofill"] += sum(_sqlite_count_optional(sub / "Web Data", table)
                                   for table in ("autofill", "local_addresses", "credit_cards",
                                                 "autofill_profiles", "masked_credit_cards"))
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/Web Data: {type(exc).__name__}")
        try:
            out["local_storage"] = out["local_storage"] or _leveldb_has_data(
                sub / "Local Storage" / "leveldb")
            idb = sub / "IndexedDB"
            out["indexeddb"] = out["indexeddb"] or (idb.exists() and any(idb.iterdir()))
            ext = sub / "Extensions"
            out["extensions"] += sum(1 for e in ext.iterdir() if not e.name.startswith(".")) if ext.exists() else 0
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/storage: {type(exc).__name__}")
        try:
            out["top_sites"] += _sqlite_count_optional(sub / "Top Sites", "top_sites")
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/Top Sites: {type(exc).__name__}")
        try:
            out["sessions"] += _session_files(sub)
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/Sessions: {type(exc).__name__}")
        try:
            out["bookmarks"] += _bookmark_count(sub / "Bookmarks")
        except Exception as exc:
            out["readable"] = False
            out["unreadable"].append(f"{label}/Bookmarks: {type(exc).__name__}")
    out["cookie_domains"] = out["cookie_domains"][:5]
    out["has_data"] = (not out["readable"] or out["cookies"] > 0 or out["logins"] > 0
                       or out["history"] > 0 or out["autofill"] > 0
                       or out["top_sites"] > 0 or out["sessions"] > 0
                       or out["local_storage"] or out["indexeddb"] or out["extensions"] > 0
                       or out["bookmarks"] > 0)
    return out


# ── the decision ───────────────────────────────────────────────────────────────────────────

def _common_refusal(path: Path, root: Path, processes, held: set[str], now: float) -> Optional[str]:
    if not _is_direct_child(path, root):
        return "not a real folder directly in its root"
    if os.path.realpath(path) in held:
        return "held by this server"
    if _state.is_chrome_profile_locked(path) or _state.chrome_lock_holder_pid(path) is not None:
        return "locked by a running Chrome"
    if processes is None:
        return "the process list could not be read"
    holders = _holders(path, processes)
    if holders:
        return f"named by running process(es) {holders[:5]}"
    if now - _newest_change(path) < GC_MIN_AGE:
        return f"changed in the last {int(GC_MIN_AGE)} s"
    return None


def fallback_refusal(path: Path, root: Path, processes=None, held: Optional[set[str]] = None,
                     now: Optional[float] = None, inspected: Optional[dict] = None) -> tuple[Optional[str], dict]:
    """(why profile-pid<N> must stay, or None; what it holds). `root` is the data folder."""
    match = FALLBACK_NAME.match(path.name)
    if not match:
        return "not named profile-pid<number>", {}
    processes = _processes() if processes is None else processes
    held = _recorded_profiles() if held is None else held
    now = time.time() if now is None else now
    why = _common_refusal(path, root, processes, held, now)
    if why:
        return why, {}
    gone = _owner_gone(int(match.group(1)), processes)
    if gone is None:
        return "whether its owner still runs could not be told", {}
    if not gone:
        return f"its server, PID {match.group(1)}, is still running", {}
    found = inspected if inspected is not None else contents(path)
    if found.get("has_data"):
        return "holds data a person could need", found
    return None, found


def privacy_refusal(path: Path, root: Path, processes=None, held: Optional[set[str]] = None,
                    now: Optional[float] = None) -> Optional[str]:
    """Why privacy-profiles/<name> must stay, or None. `root` is the data folder."""
    if path.name.startswith("."):
        return "a hidden name"
    processes = _processes() if processes is None else processes
    held = _recorded_profiles() if held is None else held
    now = time.time() if now is None else now
    return _common_refusal(path, root / "privacy-profiles", processes, held, now)


def _remove(path: Path, root: Path) -> Optional[str]:
    """Delete one folder after checking once more that it is what it was. None or the error."""
    if not _is_direct_child(path, root):
        return "changed into something that is not a plain folder"
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if not path.exists() else "still there after removal"


# Under a test, a pass on this folder is refused whatever root it was handed. The person's
# real folder is reached only by main() in a production server, which passes data_root().
_PROTECTED_UNDER_TEST = Path(os.path.expanduser("~")) / ".mcp-stealth"


def _under_test() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def run_once(root: Path) -> dict:
    """One pass over both kinds in the data folder `root`, which the caller names.

    Returns, and keeps for status(), what it did.
    """
    started = time.time()
    root = Path(root)
    report: dict[str, Any] = {
        "state": "running", "root": str(root), "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "removed": [], "kept_with_data": [], "skipped_in_use": [], "skipped": [], "errors": [],
    }
    if _under_test() and os.path.realpath(root) == os.path.realpath(_PROTECTED_UNDER_TEST):
        # Belt and braces: a test always hands its own folder, never the person's.
        report.update(state="refused", errors=["refused: the person's own data folder under a test"])
        with _LOCK:
            _LAST.clear()
            _LAST.update(report)
        return report
    with _LOCK:
        _LAST.clear()
        _LAST.update(report)
    try:
        processes = _processes()
        held = _recorded_profiles()
        privacy_root = root / "privacy-profiles"
        if privacy_root.is_dir() and not privacy_root.is_symlink():
            for path in sorted(privacy_root.iterdir()):
                why = privacy_refusal(path, root, processes, held, started)
                _settle(report, "privacy-profiles/" + path.name, path, privacy_root, why, {})
        if root.is_dir() and not root.is_symlink():
            for path in sorted(root.iterdir()):
                if not FALLBACK_NAME.match(path.name):
                    continue
                why, found = fallback_refusal(path, root, processes, held, started)
                _settle(report, path.name, path, root, why, found)
        fallback_left = [r for r in report["kept_with_data"] + report["skipped_in_use"] + report["skipped"]
                         if FALLBACK_NAME.match(r["name"])]
        with_data = [r for r in report["kept_with_data"] if FALLBACK_NAME.match(r["name"])]
        report["fallback_profiles_left"] = {
            "count": len(fallback_left),
            "with_data": len(with_data),
            "with_data_bytes": sum(r.get("bytes", 0) for r in with_data),
            "note": "profile_report lists each with what it holds; profile_cleanup "
                    "(remove_fallback, confirm=True) removes the ones you name",
        }
        report["state"] = "done"
    except Exception as exc:  # never takes the server down
        report["state"] = "failed"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["seconds"] = round(time.time() - started, 2)
    report["summary"] = _summary(report)
    with _LOCK:
        _LAST.clear()
        _LAST.update(report)
    return report


def _settle(report: dict, name: str, path: Path, root: Path, why: Optional[str], found: dict) -> None:
    if why is None:
        size = _tree_size(path)
        error = _remove(path, root)
        if error:
            report["errors"].append(f"{name}: {error}")
        else:
            report["removed"].append({"name": name, "bytes": size})
        return
    row: dict[str, Any] = {"name": name, "why": why}
    if found.get("has_data"):
        row.update(bytes=_tree_size(path), cookies=found.get("cookies"), logins=found.get("logins"),
                   readable=found.get("readable"))
        report["kept_with_data"].append(row)
    elif why.startswith(("locked", "named by", "held by", "its server", "changed in")):
        report["skipped_in_use"].append(row)
    else:
        report["skipped"].append(row)


def _summary(report: dict) -> str:
    freed = sum(r["bytes"] for r in report["removed"])
    kept = sum(r.get("bytes", 0) for r in report["kept_with_data"])
    return (f"removed {len(report['removed'])} ({freed / 2**20:.0f} MB); kept "
            f"{len(report['kept_with_data'])} with data ({kept / 2**20:.0f} MB); "
            f"{len(report['skipped_in_use'])} in use; {len(report['skipped'])} other; "
            f"{len(report['errors'])} error(s)")


def start_in_background(root: Path) -> None:
    """Run one pass over `root` a moment after startup, on a daemon thread. Never raises."""
    if not enabled():
        with _LOCK:
            _LAST.clear()
            _LAST.update({"state": "off", "how_to_enable": f"unset {ENV_SWITCH}"})
        return

    def work():
        time.sleep(max(0.0, _START_DELAY))
        try:
            run_once(root)
        except Exception as exc:
            with _LOCK:
                _LAST.update({"state": "failed", "errors": [f"{type(exc).__name__}: {exc}"]})

    threading.Thread(target=work, name="profile-gc", daemon=True).start()
