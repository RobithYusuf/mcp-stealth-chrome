"""The tabs a browser had when this server closed it, so `restore_tabs=True` can reopen them.

Chrome's own session restore stays off: every persistent launch wipes its session files
(state.wipe_window_state) and passes --disable-restore-session-state, because restored window
placement is what opened windows invisible. So the server keeps its own list: one file per
instance, `<instance>-tabs.json` in the window-state folder, holding one entry per profile that
instance used, so a launch on a fallback profile never replaces the list of the real one:

    {"version": 1, "instance_id": "main",
     "profiles": {"/abs/profile": {"profile": "/abs/profile", "saved_at": 1731500000.0,
                                   "reason": "browser_close", "tabs": [{"url": "..."}],
                                   "active": 1}}}

URLs can carry tokens. The file is written 0600 through a temporary file and a rename, and no
tool shows a URL from it without `shown_url`.

An empty list never replaces a record. A Quit closes every window before anything can read
them, a read that fails says nothing, and a browser holding only about:blank or chrome:// pages
has nothing worth reopening: in each case the earlier record stays. `forget` is the way to
clear one on purpose.

Standard library and this package's helpers only, and no import of server.py: the stdin-EOF
path runs after the event loop has closed.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import state as _state
from .helpers import _mask_pairs, confined_output_path, mask_url

def env_number(name: str, default: float) -> float:
    """A number from the environment; a value that is not one leaves the default, so a typo
    cannot stop the server from starting."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


ENV_DIR = "MCP_STEALTH_TABS_DIR"
VERSION = 1
# Profiles kept per instance file. Entries whose profile folder is gone are dropped first.
MAX_PROFILES = 8
# Tabs kept per record. More than this is noted, not silently dropped.
MAX_TABS = max(1, int(env_number("MCP_STEALTH_RESTORE_TABS_MAX", 30)))
# Only these are reopened. chrome://, devtools://, chrome-extension://, about:, data:, blob:
# and javascript: are never written, and never opened from a file someone edited either.
_KEEP_SCHEMES = ("http://", "https://", "file://")
_INSTANCE_ID = re.compile(r"[A-Za-z0-9._-]+")


def record_dir() -> Path:
    """The folder holding the records, read at call time."""
    override = os.environ.get(ENV_DIR)
    return Path(override).expanduser() if override else _state.WINDOW_STATE_DIR


def record_path(instance_id: str) -> Path:
    if not instance_id or instance_id in {".", ".."} or not _INSTANCE_ID.fullmatch(instance_id):
        raise ValueError(f"not an instance id: {instance_id!r}")
    return confined_output_path(record_dir(), f"{instance_id}-tabs.json")


def profile_key(profile: Any) -> str:
    return str(Path(str(profile)).expanduser().resolve())


def keep_url(url: Any) -> bool:
    return isinstance(url, str) and url.lower().startswith(_KEEP_SCHEMES)


def shown_url(url: Any) -> Any:
    """A URL fit for tool output: secret query and fragment pairs hidden.

    mask_url covers the query. An OAuth redirect carries its token in the fragment
    (#access_token=...), and a reopened tab can sit on exactly that page.
    """
    url = mask_url(url)
    if isinstance(url, str) and "#" in url:
        head, _, fragment = url.partition("#")
        masked = _mask_pairs(fragment) if "=" in fragment else None
        if masked:
            return f"{head}#{masked}"
    return url


def ordered_pages(pages: Iterable[tuple[str, str]], known: list[str]) -> list[tuple[str, str]]:
    """Page targets in the server's tab order, then any it did not know in Chrome's order.

    Chrome lists them in neither tab nor creation order: measured on Chrome 157, four tabs
    opened A, B, C, D came back C, B, D, A from Target.getTargets and C, D, B, A from
    /json/list. The server's own list is the order tab_list shows and the agent used.
    """
    rank = {key: index for index, key in enumerate(known)}
    listed = list(enumerate(pages))
    listed.sort(key=lambda item: (rank.get(str(item[1][0]), len(rank)), item[0]))
    return [page for _, page in listed]


def make_entry(pages: Iterable[tuple[str, str]], known: list[str], active_key: Optional[str],
               profile: Any, reason: str, now: Optional[float] = None) -> Optional[dict]:
    """The record for one profile, or None when no page is worth reopening."""
    kept = [(str(tid), url) for tid, url in ordered_pages(pages, known) if keep_url(url)]
    if not kept:
        return None
    entry: dict[str, Any] = {
        "profile": profile_key(profile),
        "saved_at": time.time() if now is None else now,
        "reason": reason,
        "tabs": [{"url": url} for _tid, url in kept[:MAX_TABS]],
        "active": next((i for i, (tid, _u) in enumerate(kept[:MAX_TABS]) if tid == active_key), None),
    }
    if len(kept) > MAX_TABS:
        entry["not_kept"] = len(kept) - MAX_TABS
    return entry


def _same_tabs(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a) and bool(b) and a.get("tabs") == b.get("tabs") and a.get("active") == b.get("active")


def _read(path: Path) -> dict:
    """The document in path. Raises when it exists and cannot be used."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(
            data.get("profiles"), dict):
        raise ValueError("not a tab record this version writes")
    return data


def _write_private(path: Path, data: dict) -> None:
    """Replace path with data, readable by its owner only, never half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _prune(profiles: dict, keep: str) -> None:
    for key in [k for k in profiles if k != keep and not Path(k).is_dir()]:
        profiles.pop(key, None)
    while len(profiles) > MAX_PROFILES:
        oldest = min((k for k in profiles if k != keep),
                     key=lambda k: profiles[k].get("saved_at") or 0, default=None)
        if oldest is None:
            break
        profiles.pop(oldest, None)


def save(instance_id: str, profile: Any, pages: Optional[Iterable[tuple[str, str]]],
         known: list[str], active_key: Optional[str], reason: str, *, force: bool) -> str:
    """Write what a browser has open. Returns what happened, in words; never raises.

    force=False (the background writer) leaves an unchanged list alone. A close passes True, so
    the time and reason say when it was last seen.
    """
    try:
        path = record_path(instance_id)
    except ValueError as exc:
        return f"not recorded: {exc}"
    entry = make_entry(pages or [], known, active_key, profile, reason) if pages is not None else None
    if entry is None:
        why = "the tabs could not be read" if pages is None else "no page worth reopening"
        return f"not recorded: {why}; an earlier record is kept"
    try:
        doc = _read(path)
    except FileNotFoundError:
        doc = {"version": VERSION, "instance_id": instance_id, "profiles": {}}
    except Exception:
        # Ours, and unusable as it is: replacing it loses nothing restore could have used.
        doc = {"version": VERSION, "instance_id": instance_id, "profiles": {}}
    key = entry["profile"]
    if not force and _same_tabs(doc["profiles"].get(key), entry):
        return "unchanged"
    doc["profiles"][key] = entry
    _prune(doc["profiles"], key)
    try:
        _write_private(path, doc)
    except Exception as exc:
        return f"not recorded: {type(exc).__name__}: {exc}"
    return f"recorded {len(entry['tabs'])} tab(s)"


def load(instance_id: str, profile: Any) -> tuple[Optional[dict], str]:
    """The record for this instance and profile, or None and why there is none."""
    try:
        path = record_path(instance_id)
    except ValueError as exc:
        return None, str(exc)
    try:
        doc = _read(path)
    except FileNotFoundError:
        return None, f"no tabs were saved for instance {instance_id!r}"
    except Exception as exc:
        return None, f"the saved tab list could not be read ({type(exc).__name__}), so nothing was reopened"
    entry = doc["profiles"].get(profile_key(profile))
    if not isinstance(entry, dict) or not isinstance(entry.get("tabs"), list):
        others = len(doc["profiles"])
        return None, (f"no tabs were saved for this profile"
                      + (f" (instance {instance_id!r} has a list for {others} other profile(s))"
                         if others else ""))
    return entry, ""


def _drop(path: Path, keys: set[str], drop_unreadable: bool = False) -> int:
    try:
        doc = _read(path)
    except FileNotFoundError:
        return 0
    except Exception:
        if not drop_unreadable:
            return 0
        # Asked to forget this instance's list, and nothing can restore from this file.
        with contextlib.suppress(OSError):
            path.unlink()
            return 1
        return 0
    removed = [k for k in list(doc["profiles"]) if k in keys]
    if not removed:
        return 0
    for key in removed:
        doc["profiles"].pop(key, None)
    if doc["profiles"]:
        _write_private(path, doc)
    else:
        path.unlink()
    return len(removed)


def forget(instance_id: str, profile: Any) -> bool:
    """Remove the record of one instance and profile. True when there was one."""
    try:
        return _drop(record_path(instance_id), {profile_key(profile)}, drop_unreadable=True) > 0
    except (OSError, ValueError):
        return False


def forget_instance(instance_id: str) -> bool:
    """Remove an instance's whole record file, every profile in it. True when there was one."""
    try:
        path = record_path(instance_id)
        if path.is_symlink() or not path.is_file():
            return False
        path.unlink()
        return True
    except (OSError, ValueError):
        return False


def _record_files() -> list[Path]:
    folder = record_dir()
    try:
        return sorted(p for p in folder.glob("*-tabs.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return []


def forget_profile(profile: Any) -> int:
    """Remove every instance's record for a profile. Returns how many were removed."""
    key = profile_key(profile)
    removed = 0
    for path in _record_files():
        with contextlib.suppress(OSError):
            removed += _drop(path, {key})
    return removed


def saved_for_profile(profile: Any) -> int:
    """How many tabs are saved for a profile, over every instance."""
    key = profile_key(profile)
    count = 0
    for path in _record_files():
        with contextlib.suppress(Exception):
            entry = _read(path)["profiles"].get(key)
            if isinstance(entry, dict):
                count += len(entry.get("tabs") or [])
    return count


def pages_over_http(host: Any, port: Any, timeout: float) -> Optional[list[tuple[str, str]]]:
    """Page targets from the DevTools HTTP endpoint, without an event loop. None when unread.

    The stdin-EOF shutdown runs after the server's loop has closed, so CDP over the websocket
    is out of reach there. The quit watcher's reader reads the body to its Content-Length,
    since Chrome keeps the connection open whatever it is asked.
    """
    from ._quit_watcher import http_get
    if not host or not port:
        return None
    body = http_get(host, port, "/json/list", timeout=timeout)
    if body is None:
        return None
    try:
        targets = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(targets, list):
        return None
    return [(str(t.get("id")), str(t.get("url") or "")) for t in targets
            if isinstance(t, dict) and t.get("type") == "page"]


def age_text(saved_at: Any, now: Optional[float] = None) -> str:
    try:
        seconds = max(0, int((time.time() if now is None else now) - float(saved_at)))
    except (TypeError, ValueError):
        return "at an unknown time"
    for unit, size in (("day", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            value = seconds // size
            return f"{value} {unit}{'s' if unit == 'day' and value != 1 else ''} ago"
    return f"{seconds} s ago"
