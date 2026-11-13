"""See and reclaim the disk ~/.mcp-stealth uses, without risking anyone's logins.

Fallback profiles (profile-pid<N>) are what a server process creates when another
one holds the shared profile. Nothing removes them when that process exits, so they
accumulate. Removal is confined to exactly that kind: a named profile can hold real
logins, and only ever has its regenerable caches cleared.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Literal

from .. import profile_gc as _gc
from .. import state as _state
from .. import tab_record as _tabs
from .._app import mcp
from ..helpers import err, ok

_FALLBACK_NAME = re.compile(r"^profile-pid(\d+)$")

# Regenerable by Chrome. Cookies, Login Data, Local Storage, IndexedDB, Sessions,
# Preferences and Web Data are deliberately absent: those are the logged-in state.
# So is Service Worker/*: CacheStorage is what sites store on purpose for offline use.
_CACHE_TOP = (
    "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "component_crx_cache",
    "optimization_guide_model_store", "OptGuideOnDeviceModel", "Crashpad",
)
_CACHE_PER_PROFILE = (
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
)


def _root() -> Path:
    return _state.data_root()


def _tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += info.st_size
    return total


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _cache_dirs(profile: Path) -> list[Path]:
    found = [profile / name for name in _CACHE_TOP]
    for sub in profile.iterdir():
        if sub.is_dir() and not sub.is_symlink() and (
            sub.name == "Default" or sub.name.startswith("Profile ")
        ):
            found.extend(sub / name for name in _CACHE_PER_PROFILE)
    root = profile.resolve()
    # Resolved, so a planted symlink anywhere on the way cannot aim this outside.
    return [p for p in found if p.is_dir() and not p.is_symlink() and root in p.resolve().parents]


def _last_used(profile: Path) -> float:
    """Newest change among the files Chrome touches in every session.

    Files only. A directory's time also moves when clear_cache removes something from
    it, which would make every cleaned profile look as if it was just used.
    """
    newest = 0.0
    for candidate in (profile / "Local State",
                      profile / "Default" / "Preferences",
                      profile / "Default" / "Network" / "Cookies",
                      profile / "Default" / "Cookies"):
        try:
            newest = max(newest, candidate.stat().st_mtime)
        except OSError:
            continue
    return newest


def _in_use(profile: Path) -> dict:
    """Anything short of positive evidence that it is free counts as in use.

    An unreadable lock is a lock. On Windows no process listing is available here, so
    nothing can show that a directory is free at all.
    """
    holder = _state.chrome_lock_holder_pid(profile)
    pids = _state.find_chrome_pids_by_profile(profile)
    locked = _state.is_chrome_profile_locked(profile)
    unprovable = sys.platform == "win32"
    return {
        "in_use": holder is not None or bool(pids) or locked or unprovable,
        "holder_pid": holder,
        "chrome_pids": pids,
        **({"note": "in-use status cannot be determined on Windows"} if unprovable else {}),
    }


def _describe(name: str, profile: Path, kind: str) -> dict:
    size = _tree_size(profile)
    cache = sum(_tree_size(p) for p in _cache_dirs(profile))
    usage = _in_use(profile)
    last = _last_used(profile)
    row = {
        "name": name,
        "kind": kind,
        "size": _human(size),
        "size_bytes": size,
        "cache": _human(cache),
        "cache_bytes": cache,
        "last_used": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else None,
        "idle_days": round((time.time() - last) / 86400, 1) if last else None,
        **usage,
    }
    match = _FALLBACK_NAME.match(name)
    if match:
        owner = int(match.group(1))
        row["owner_pid"] = owner
        row["owner_alive"] = _state._pid_alive(owner)
        # What would go with it, without a single secret value: counts and cookie domains.
        found = _gc.contents(profile)
        row["contents"] = {k: found[k] for k in (
            "cookies", "cookie_domains", "logins", "history", "autofill", "top_sites", "sessions",
            "local_storage", "indexeddb", "extensions", "bookmarks", "readable", "has_data")}
        why, _ = _gc.fallback_refusal(profile, _root(), inspected=found)
        row["automatic_cleanup"] = "removes it at the next server start" if why is None else f"keeps it: {why}"
    row["removable"] = _removal_refusal(name, profile, row) is None
    return row


def _removal_refusal(name: str, profile: Path, row: dict | None = None) -> str | None:
    """Why this directory may not be deleted, or None. The one place that decides."""
    match = _FALLBACK_NAME.match(name)
    if not match:
        return "not a fallback profile (only profile-pid<N> can be removed)"
    if profile.is_symlink() or not profile.is_dir():
        return "not a real directory"
    if profile.parent.resolve() != _root().resolve():
        return "outside ~/.mcp-stealth"
    info = row if row is not None else _in_use(profile)
    if info.get("in_use"):
        return f"in use (lock holder {info.get('holder_pid')}, chrome {info.get('chrome_pids')})"
    pid = int(match.group(1))
    if pid == os.getpid():
        return "belongs to this server process"
    if _state._pid_alive(pid):
        return f"owner process {pid} is still running"
    return None


def _named_profiles() -> list[tuple[str, Path, str]]:
    root = _root()
    found: list[tuple[str, Path, str]] = []
    main = root / "profile"
    # A symlink here would aim cleanup outside the tree this tool manages.
    if main.is_dir() and not main.is_symlink():
        found.append(("profile", main, "main"))
    named = root / "profiles"
    if named.is_dir() and not named.is_symlink():
        found.extend(
            (f"profiles/{p.name}", p, "named")
            for p in sorted(named.iterdir()) if p.is_dir() and not p.is_symlink()
        )
    if root.is_dir():
        found.extend(
            (p.name, p, "fallback")
            for p in sorted(root.iterdir())
            if p.is_dir() and not p.is_symlink() and _FALLBACK_NAME.match(p.name)
        )
    return found


def _report() -> dict:
    rows = [_describe(name, path, kind) for name, path, kind in _named_profiles()]
    rows.sort(key=lambda r: -r["size_bytes"])
    total = sum(r["size_bytes"] for r in rows)
    cache = sum(r["cache_bytes"] for r in rows)
    removable = sum(r["size_bytes"] for r in rows if r["removable"])
    return {
        "root": str(_root()),
        "total": _human(total),
        "cache_total": _human(cache),
        "removable_fallback_total": _human(removable),
        "profiles": rows,
    }


@mcp.tool()
async def profile_report() -> str:
    """Size, cache size, last use and in-use status of every profile. Changes nothing.

    `kind` is main, named or fallback. A fallback (profile-pid<N>) was created when
    another server process held the shared profile, and is `removable` only once its
    owner has exited and no browser has it open. Each fallback also lists `contents`
    (cookie count and top domains, logins, storage, extensions, bookmarks; no values)
    and what the automatic cleanup at server start does with it: an empty one goes,
    one holding any of that stays until you remove it here. Pass names from here to
    profile_cleanup. Walks every file, so a large tree takes a moment.
    """
    try:
        return ok(json.dumps(await asyncio.to_thread(_report), indent=2))
    except Exception as e:
        return err(str(e))


def _target(name: str) -> tuple[Path, str] | None:
    for known, path, kind in _named_profiles():
        if known == name:
            return path, kind
    return None


def _clean_cache(name: str, confirm: bool) -> dict:
    found = _target(name)
    if found is None:
        return {"name": name, "ok": False, "error": "no such profile"}
    profile, _kind = found
    usage = _in_use(profile)
    if usage["in_use"]:
        return {"name": name, "ok": False, "error": f"in use, not touched: {usage}"}
    dirs = _cache_dirs(profile)
    sizes = {p: _tree_size(p) for p in dirs}
    if not confirm:
        return {"name": name, "ok": True, "would_free": _human(sum(sizes.values())),
                "would_clear": [str(p.relative_to(profile)) for p in dirs]}
    # Asked again immediately before deleting: measuring a large profile takes a while.
    usage = _in_use(profile)
    if usage["in_use"]:
        return {"name": name, "ok": False, "error": f"in use, not touched: {usage}"}
    freed = 0
    failed = []
    for path, size in sizes.items():
        try:
            shutil.rmtree(path)
            freed += size
        except OSError as exc:
            failed.append(f"{path.relative_to(profile)}: {exc}")
    return {"name": name, "ok": not failed, "freed": _human(freed),
            "cleared": len(dirs) - len(failed), **({"failed": failed} if failed else {})}


def _remove_fallback(name: str, confirm: bool) -> dict:
    found = _target(name)
    if found is None:
        return {"name": name, "ok": False, "error": "no such profile"}
    profile, _kind = found
    refusal = _removal_refusal(name, profile)
    if refusal:
        return {"name": name, "ok": False, "error": f"refused: {refusal}"}
    size = _tree_size(profile)
    if not confirm:
        found = _gc.contents(profile)
        return {"name": name, "ok": True, "would_free": _human(size),
                "would_lose": {**{k: found[k] for k in ("cookies", "cookie_domains", "logins",
                                                       "history", "autofill", "top_sites",
                                                       "sessions", "local_storage",
                                                       "indexeddb", "extensions", "bookmarks",
                                                       "readable")},
                               "saved_tabs": _tabs.saved_for_profile(profile)}}
    # Asked again immediately before deleting: the report may be minutes old.
    refusal = _removal_refusal(name, profile)
    if refusal:
        return {"name": name, "ok": False, "error": f"refused: {refusal}"}
    try:
        shutil.rmtree(profile)
    except OSError as exc:
        return {"name": name, "ok": False, "error": str(exc),
                "still_exists": profile.exists()}
    # Its saved tab list goes with it: the URLs belong to that profile's sessions.
    return {"name": name, "ok": True, "removed": True, "freed": _human(size),
            "saved_tab_lists_removed": _tabs.forget_profile(profile)}


@mcp.tool()
async def profile_cleanup(
    action: Literal["clear_cache", "remove_fallback"],
    names: list[str],
    confirm: bool = False,
) -> str:
    """Reclaim profile disk space. A preview unless confirm=True. Irreversible when it runs.

    Args:
        action:
            clear_cache: delete regenerable caches from a profile that is not in
                use. Cookies, logins, local storage and preferences stay.
            remove_fallback: delete a whole profile-pid<N> directory. Refused for
                main or named profiles, or while the directory or its owner is live.
                Any login made inside that profile goes with it, and so does the tab
                list saved for it (restore_tabs).
        names: exact names from profile_report. There is no "all", so nothing gets
            deleted that was not named.
        confirm: False (default) reports what would happen and deletes nothing.

    Only pass confirm=True when the person explicitly asked for this deletion.
    """
    if not isinstance(names, list) or not names:
        return err("names must be a non-empty list from profile_report")
    worker = _clean_cache if action == "clear_cache" else _remove_fallback
    try:
        results = [await asyncio.to_thread(worker, str(n), bool(confirm)) for n in names]
    except Exception as e:
        return err(str(e))
    payload = {"action": action, "dry_run": not confirm, "results": results}
    text = json.dumps(payload, indent=2)
    if all(not r.get("ok") for r in results):
        return err(text)
    return ok(text)
