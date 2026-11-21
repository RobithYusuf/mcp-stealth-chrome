"""Chrome profile integration: list_chrome_profiles and clone_chrome_profile.

Moved out of server.py unchanged. clone_chrome_profile writes into, and with overwrite=True removes,
a folder under the person's data folder, so every name a test redirects on the server module
(PROFILES_ROOT, ensure_dirs, chrome_user_data_root(s), chrome_lock_holder_pid,
is_chrome_profile_locked) is read here as _srv.<name> at call time and never imported for itself:
an import of its own would keep the real folder while the test patched server's copy. The SQLite
copy helpers (_is_sqlite, _sqlite_snapshot) stay in server.py, which uses them too.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from .. import brave as brave_mode
from .._app import mcp
from ..helpers import err, ok
from ..state import BrowserState
from .. import server as _srv

# Let user start from their existing Chrome profile (with all logins, cookies,
# extensions) instead of a fresh one. Three patterns:
#
#   1. list_chrome_profiles()                           — detect what's on system
#   2. clone_chrome_profile(source, instance_id)        — safe: copy to isolated dir
#   3. spawn_browser(profile_dir=<chrome path>, ...)    — direct: uses profile as-is
#                                                         (requires Chrome desktop closed)


@mcp.tool()
async def list_chrome_profiles(browser: Optional[str] = None) -> str:
    """List profiles across EVERY Chrome-family browser on this system.

    Chrome, Chromium, Edge, Brave, Vivaldi, Opera and Arc are all surveyed —
    people commonly keep their real logged-in sessions in Brave or Edge while a
    near-empty Chrome sits alongside, and reporting only the first browser found
    would hide exactly the profiles worth cloning.

    Each profile reports a `cookies_kb` figure: a profile with kilobytes of
    cookies has real sessions, one with ~0 has never been used for anything.

    Args:
        browser: restrict to one browser id (e.g. "brave", "chrome", "edge").
            Omit to survey them all.

    Reads each browser's 'Local State' JSON read-only — nothing is modified.
    """
    roots = _srv.chrome_user_data_roots()
    if browser:
        wanted = browser.strip().lower()
        roots = [(n, p) for n, p in roots if n == wanted]
        if not roots:
            available = ", ".join(n for n, _ in _srv.chrome_user_data_roots()) or "none"
            return err(f"no user-data root for browser {browser!r}. Found: {available}")
    if not roots:
        return err(
            "No Chrome-family browser profile directory found. "
            "Install Chrome/Chromium/Edge/Brave and launch it once to create profiles."
        )

    out: list[dict] = []
    browsers: list[dict] = []
    parse_errors: list[str] = []
    for bname, root in roots:
        try:
            data = json.loads((root / "Local State").read_text())
        except Exception as e:
            parse_errors.append(f"{bname}: {e}")
            continue
        info_cache = data.get("profile", {}).get("info_cache", {})
        profiles_order = list(data.get("profile", {}).get("profiles_order", []))
        root_running = _srv.is_chrome_profile_locked(root)
        seen = set(profiles_order)
        # Ensure we also include profiles not in profiles_order
        for k in info_cache.keys():
            if k not in seen:
                profiles_order.append(k)
        count_before = len(out)
        for name in profiles_order:
            info = info_cache.get(name, {})
            pdir = root / name
            cookie_candidates = (pdir / "Cookies", pdir / "Network" / "Cookies")
            try:
                cookies_kb = max(
                    (path.stat().st_size // 1024 for path in cookie_candidates
                     if path.exists()),
                    default=0,
                )
            except OSError:
                cookies_kb = 0
            out.append({
                "browser": bname,
                "profile_dir_name": name,
                "display_name": info.get("name", name),
                "email": info.get("user_name", ""),
                "path": str(pdir),
                "exists": pdir.exists(),
                "cookies_kb": cookies_kb,
                # Chromium locks the user-data root, not each Default/Profile N
                # child. Checking pdir always reported false while Chrome ran.
                "in_use": root_running if pdir.exists() else False,
                "last_active_time": info.get("last_active_time", 0),
            })
        browsers.append({
            "browser": bname,
            "root": str(root),
            "running": root_running,
            "profile_count": len(out) - count_before,
        })
    # Richest sessions first — that is what someone scanning this list wants.
    out.sort(key=lambda p: p.get("cookies_kb", 0), reverse=True)
    payload = {
        "browsers": browsers,
        "profile_count": len(out),
        "profiles": out,
        "usage_hint": (
            "clone_chrome_profile(browser='brave', source_profile='Default', "
            "target_instance_id='my_clone') → then spawn_browser(instance_id='my_clone', "
            "browser='brave') (a Brave clone runs in Brave; any other in Chrome). "
            "Pick the profile with the most cookies_kb; close its browser first, "
            "or explicitly use allow_running=true for a verified live snapshot."
        ),
    }
    if parse_errors:
        payload["parse_errors"] = parse_errors
    return ok(json.dumps(payload, indent=2, default=str))


@mcp.tool()
async def clone_chrome_profile(
    source_profile: str = "Default",
    target_instance_id: str = "chrome_clone",
    skip_cache: bool = True,
    overwrite: bool = False,
    browser: Optional[str] = None,
    allow_running: bool = False,
    keep_site_data: bool = False,
) -> str:
    """Clone a profile from ANY Chrome-family browser into an isolated MCP location.

    SAFE: reads the source profile without modification, copies to
    ~/.mcp-stealth/profiles/<target_instance_id>/Default/, or for a Brave profile to
    ~/.mcp-stealth/brave-profiles/<target_instance_id>/Default/, which launches in Brave
    (spawn_browser(..., browser='brave')) and keeps its logins: Brave's cookies and saved
    passwords are encrypted with Brave's own key, which Chrome cannot read.

    The source browser MUST be closed (we check SingletonLock).
    Preserves: cookies, history, bookmarks, saved passwords, extensions state.
    Skips (if skip_cache=True): Cache, Code Cache, GPUCache, Media Cache (regenerable), and
    also the site data folders IndexedDB, Service Worker and File System (saves 500MB+). Those
    are not caches: web apps that keep their login or offline data there (WhatsApp Web,
    Telegram Web, many PWAs) ask to sign in again. The result lists them under not_copied.

    Args:
        source_profile: profile dir name ("Default", "Profile 1", ...).
                        Use list_chrome_profiles() to see options.
        target_instance_id: Name for the cloned instance (becomes folder name).
        skip_cache: Exclude cache dirs for fast + smaller copy (default True).
        overwrite: Delete target if exists before copying (default False).
        keep_site_data: copy IndexedDB, Service Worker and File System even with
            skip_cache=True, for a profile that will be used every day. Larger copy. With
            allow_running these LevelDB folders are copied as they lie, unchecked.
        allow_running: clone even while the browser is open. Each SQLite
            database is copied with its -wal/-shm companions and then
            integrity-checked; any that fail come back under `sqlite_suspect`
            instead of being trusted silently. Without this flag the call refuses
            while the profile is locked.
        browser: which browser to clone FROM — "brave", "chrome", "edge",
            "chromium", "vivaldi", "opera", "arc". Omit to use the first root
            found, which is NOT necessarily the one holding your sessions:
            an empty Chrome often shadows the Brave/Edge profile you actually
            log in with. Pass this explicitly when in doubt.

    After clone, launch with:
        spawn_browser(instance_id='<target_instance_id>')
        spawn_browser(instance_id='<target_instance_id>', browser='brave') for a Brave profile
    """
    import shutil

    try:
        _srv._validate_instance_id(target_instance_id)
    except ValueError as e:
        return err(str(e))

    root = _srv.chrome_user_data_root(browser)
    if root is None:
        found = ", ".join(n for n, _ in _srv.chrome_user_data_roots()) or "none"
        if browser:
            return err(f"no user-data root for browser {browser!r}. Found: {found}")
        return err("No Chrome-family profile root found. Install Chrome/Brave/Edge first.")
    try:
        source_path = _srv._direct_child(root, source_profile, "source_profile")
    except ValueError as e:
        return err(str(e))
    if not source_path.exists():
        return err(
            f"Source profile not found: {source_path}\n"
            f"Run list_chrome_profiles() to see available profiles "
            f"(pass browser= to pick a different browser)."
        )
    running = _srv.is_chrome_profile_locked(source_path) or _srv.is_chrome_profile_locked(root)
    if running and not allow_running:
        label = browser or root.name
        return err(
            f"{label} is currently using this profile (lock file present).\n"
            f"Either close it FULLY (Cmd+Q on macOS, not just the window), or pass "
            f"allow_running=true — every SQLite database (cookies, logins, history) "
            f"is copied with its WAL companions and integrity-checked. Suspect "
            f"copies are reported explicitly. The source profile is only ever "
            f"opened read-only.\n"
            f"Lock: {source_path / 'SingletonLock'}"
        )

    _srv.ensure_dirs()
    source_name = next((n for n, p in _srv.chrome_user_data_roots() if p == root), browser or "")
    from_brave = (source_name or "").lower().startswith(brave_mode.BRAVE)
    target_root = (brave_mode.profiles_root(_srv._brave_data_root()) if from_brave
                   else _srv.PROFILES_ROOT) / target_instance_id
    target_default = target_root / "Default"

    if target_root.exists():
        if not overwrite:
            return err(
                f"Target instance already exists: {target_root}\n"
                f"Pass overwrite=true to replace, or use different target_instance_id."
            )
        # overwrite means "replace a stale copy", never "delete a browser out from under
        # whoever is using it", in this session or another.
        foreign = BrowserState.foreign_instance(target_instance_id)
        if foreign:
            return err(f"not overwriting: {foreign}")
        from ..state import find_chrome_pids_by_profile
        holder = _srv.chrome_lock_holder_pid(target_root)
        running = find_chrome_pids_by_profile(target_root)
        # No process listing on Windows, so nothing there can show the profile is free.
        if holder is not None or running or _srv.is_chrome_profile_locked(target_root) or sys.platform == "win32":
            return err(
                f"not overwriting {target_root}: it is in use "
                f"(lock holder {holder}, Chrome PIDs {running}). Close that browser first."
            )
        try:
            shutil.rmtree(target_root)
        except Exception as e:
            return err(f"failed to remove existing target: {e}")

    target_default.mkdir(parents=True, exist_ok=True)

    # Cache-like directories to skip (regenerable, big, Chrome rebuilds them)
    cache_dirs = {
        "cache", "code cache", "gpucache", "dawnwebgpucache", "dawngraphitecache",
        "graphitedawncache", "grshadercache", "media cache", "service worker",
        "indexeddb", "file system", "downloadedupdates", "downloads",
        "safe browsing", "componentupdater", "extensions_crx_cache",
        "component_crx_cache", "gpupersistentcache", "shared dictionary",
    }
    # Not caches: what a site stored for itself. Skipped with the caches by default (size), so the
    # result names each one and what leaving it out means; keep_site_data copies them.
    site_data_dirs = {"indexeddb", "service worker", "file system"}
    if keep_site_data:
        cache_dirs = cache_dirs - site_data_dirs
    not_copied: dict[str, float] = {}
    # Files that might cause issues if copied (locks, logs)
    skip_files = {
        "singletonlock", "singletoncookie", "singletonsocket",
        "lock", "lockfile",
    }

    copied_count = 0
    skipped_cache_bytes = 0
    errors: list[str] = []
    snapshotted: list[str] = []
    suspect_dbs: list[str] = []

    for item in source_path.iterdir():
        name_lower = item.name.lower()
        try:
            if item.is_file():
                if skip_cache and name_lower in skip_files:
                    continue
                # Also skip -journal WAL sidecars
                if name_lower.endswith("-journal"):
                    continue
                # Cookies/Login Data/History are live SQLite. A plain byte copy of
                # a database the browser is still writing can land mid-checkpoint
                # and produce a torn file, so snapshot those through SQLite itself.
                if _srv._is_sqlite(item):
                    if _srv._sqlite_snapshot(item, target_default / item.name):
                        snapshotted.append(item.name)
                    else:
                        suspect_dbs.append(item.name)
                    copied_count += 1
                    continue
                shutil.copy2(item, target_default / item.name)
                copied_count += 1
            elif item.is_dir():
                if skip_cache and name_lower in cache_dirs:
                    size = 0
                    try:
                        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    except Exception:
                        pass
                    if name_lower in site_data_dirs:
                        not_copied[item.name] = round(size / 1024 / 1024, 1)
                    else:
                        skipped_cache_bytes += size
                    continue
                shutil.copytree(
                    item, target_default / item.name,
                    dirs_exist_ok=True,
                    ignore_dangling_symlinks=True,
                )
                copied_count += 1
        except Exception as e:
            errors.append(f"{item.name}: {type(e).__name__}")
            continue

    # Copy Local State (shared across profiles, needed for Chrome to recognize profile)
    local_state_src = root / "Local State"
    if local_state_src.exists():
        try:
            shutil.copy2(local_state_src, target_root / "Local State")
        except Exception as e:
            errors.append(f"Local State: {e}")

    # Compute target size
    try:
        total_size = sum(f.stat().st_size for f in target_root.rglob("*") if f.is_file())
    except Exception:
        total_size = 0

    result = {
        "source_browser": browser or root.name,
        "source_running": running,
        "sqlite_verified": snapshotted,
        "sqlite_suspect": suspect_dbs,
        "source": str(source_path),
        "target": str(target_root),
        "copied_items": copied_count,
        "target_size_mb": round(total_size / 1024 / 1024, 1),
        "cache_skipped_mb": round(skipped_cache_bytes / 1024 / 1024, 1),
        "errors": errors[:10],  # cap error list
        "verification_scope": (
            "Top-level SQLite files are integrity-checked. Databases nested in "
            "subdirectories are copied without a check, and a live source is a "
            "structural copy, not an atomic point-in-time snapshot — a "
            "transaction committed mid-copy can be missing."
        ),
    }
    if not_copied:
        # Left out with the caches: say so, or a login lost with them looks like a broken clone.
        result["not_copied"] = {name: f"{mb} MB" for name, mb in sorted(not_copied.items())}
        result["not_copied_means"] = (
            "Site data left out with the caches (skip_cache=True): IndexedDB holds what web apps "
            "keep for themselves, often their login (WhatsApp Web, Telegram Web, many PWAs); "
            "Service Worker holds their offline copies and push registrations; File System their "
            "files. Those sites ask to sign in again in the clone. For a profile used every day, "
            "clone again with keep_site_data=True (overwrite=True replaces this copy)."
        )
    elif keep_site_data and skip_cache:
        result["site_data"] = ("IndexedDB, Service Worker and File System were copied "
                               "(keep_site_data=True)" + (", as they lay while the browser ran, "
                               "unchecked" if running else ""))
    # A clone with a corrupt cookie jar looks identical to a good one until a
    # login silently fails hours later, so a suspect database is an error here
    # rather than a field somebody has to notice. The copy is left in place to
    # be inspected; naming it as the next step would endorse it.
    if suspect_dbs or errors:
        result["usable"] = False
        result["recovery"] = (
            "Re-run with the source browser CLOSED for a clean copy, or delete "
            f"{target_root} and retry. The partial copy was kept for inspection."
        )
        return err(json.dumps(result, indent=2, default=str))
    result["usable"] = True
    result["next_step"] = (
        f"spawn_browser(instance_id='{target_instance_id}', "
        f"url='https://example.com', headless=False"
        f"{', browser=' + repr(brave_mode.BRAVE) if from_brave else ''})"
    )
    return ok(json.dumps(result, indent=2, default=str))
