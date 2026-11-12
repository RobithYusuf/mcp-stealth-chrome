"""Moving a profile's cookies to Chrome 158's encryption without losing them.

Chrome 158 on macOS encrypts with a different key in any profile outside its default folder
(Chromium CL 8603400, feature `KeychainKeyProviderUserDataDirProtection`): the old `v10` key is
registered as unavailable, and cookies it cannot read are deleted. Every profile this server
opens is outside the default folder. Launched with
`--disable-features=KeychainKeyProviderUserDataDirProtection`, the same Chrome reads `v10` again
and does not delete anything, so each profile is moved once:

1. the cookie databases are copied aside (a fallback should the flag stop working),
2. Chrome 158 opens the profile headless with the flag and every cookie is read over CDP,
3. those are written to a backup file (mode 600) that `storage_state_load` accepts,
4. that Chrome closes with Browser.close, and a marker in the profile records the move as pending,
5. the requested launch starts without the flag and the cookies are set again,
6. they are read back and compared; the marker then records the move as done.

A profile with a `v10` cookie is never opened by Chrome 158 without the flag before step 4 is
complete: a failure before it means the requested launch runs with the flag, and the next
launch tries again. After step 4 the backup holds every cookie, and a pending marker makes
the next launch set them again. Only cookies move. Saved passwords and other data Chrome
encrypted with `v10` stay unreadable to Chrome 158 in that profile.

This module only reads databases and writes files; starting Chrome is the server's part.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import profile_version

FEATURE = "KeychainKeyProviderUserDataDirProtection"
FIRST_MAJOR = 158
MARKER = ".mcp-stealth-v11-migration.json"
BACKUP_PREFIX = "pre-v11-migration"
_COMPANIONS = ("", "-journal", "-wal", "-shm")
# Chrome's time: microseconds since 1601-01-01.
_CHROME_EPOCH_OFFSET = 11_644_473_600


@dataclass
class Plan:
    """What a launch has to do. kind: none | migrate | resume | flag.

    `flag` means: open with the feature turned off and do not move anything this time, with
    `reason` saying why. `resume` means a previous move got as far as its backup."""
    kind: str
    reason: str = ""
    subprofile: str = "Default"
    rows: int = 0
    v10: int = 0
    marker: dict = field(default_factory=dict)


def applies(executable: Optional[str]) -> bool:
    """macOS with a Chrome of major 158 or newer. The feature exists only on macOS, and only
    from 158: the framework of Chrome 157.0.8103.39 holds no string naming it, the one of
    158.0.8127.18 holds two. The version is read from Info.plist, which costs nothing; reading
    the framework for the string takes 1 to 2.4 s."""
    if sys.platform != "darwin":
        return False
    version = profile_version.binary_version(executable)
    return bool(version) and version[0] >= FIRST_MAJOR


def default_data_dirs() -> list[Path]:
    """The folders where Chrome keeps the default key (`IsUsingDefaultDataDirectory`)."""
    root = Path.home() / "Library" / "Application Support" / "Google"
    return [root / name for name in ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome Canary")]


def subprofile_from_args(args: list[str]) -> str:
    chosen = "Default"
    for arg in args or ():
        if (arg or "").startswith("--profile-directory="):
            chosen = arg.split("=", 1)[1] or chosen
    return chosen


def cookie_databases(profile: Path, subprofile: str) -> list[Path]:
    folder = profile / subprofile
    return [p for p in (folder / "Network" / "Cookies", folder / "Cookies")
            if p.is_file() and not p.is_symlink()]


def count_rows(databases: list[Path]) -> tuple[int, int]:
    """(cookies Chrome will load, of which `v10`), read-only and immutable. Raises when a
    database cannot be read: unknown is not "none"."""
    now = int((time.time() + _CHROME_EPOCH_OFFSET) * 1_000_000)
    loaded = v10 = 0
    for database in databases:
        with contextlib.closing(sqlite3.connect(
                f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True)) as db:
            for persistent, expires, prefix in db.execute(
                    "SELECT is_persistent, expires_utc, substr(encrypted_value, 1, 3) FROM cookies"):
                is_v10 = prefix == b"v10"
                v10 += is_v10
                loaded += bool(persistent) and (not expires or expires > now)
    return loaded, v10


def read_marker(profile: Path) -> dict:
    try:
        data = json.loads((profile / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(profile: Path, data: dict) -> None:
    write_private(profile / MARKER, json.dumps(data, indent=2))


def plan(profile: Optional[Path], args: list[str], executable: Optional[str]) -> Plan:
    """Decide before Chrome starts. Never raises: anything unreadable means the flag."""
    if profile is None or not applies(executable):
        return Plan("none")
    profile = Path(profile)
    try:
        if not profile.is_dir():
            return Plan("none")
        real = os.path.realpath(profile)
        if any(real == os.path.realpath(d) for d in default_data_dirs()):
            return Plan("none")  # Chrome keeps the v10 key in its own folder
        subprofile = subprofile_from_args(args)
        marker = read_marker(profile)
        major = profile_version.binary_version(executable)[0]
        if marker.get("state") == "pending" and Path(str(marker.get("backup", ""))).is_file():
            return Plan("resume", subprofile=subprofile, marker=marker)
        rows, v10 = count_rows(cookie_databases(profile, subprofile))
        if not v10:
            return Plan("none", subprofile=subprofile, rows=rows)
        if marker.get("state") == "done" and marker.get("major") == major:
            # Moved before by this same Chrome, and v10 is back: this Chrome evidently writes
            # v10 here (the protection was turned off remotely). Moving again would loop.
            return Plan("flag", "this Chrome wrote v10 cookies again after they were moved, so "
                        "the profile is opened with the protection turned off", subprofile, rows, v10, marker)
        return Plan("migrate", subprofile=subprofile, rows=rows, v10=v10, marker=marker)
    except Exception as exc:
        return Plan("flag", f"its cookie database could not be read ({type(exc).__name__}: {exc}), "
                    "so it is opened with the protection turned off and nothing was moved")


def label(profile: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", Path(profile).name).strip(".-") or "profile"


def backup_databases(databases: list[Path], profile: Path, target: Path) -> list[str]:
    """Copy each cookie database and its companions to `target`, keeping the layout."""
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied = []
    for database in databases:
        for suffix in _COMPANIONS:
            source = Path(str(database) + suffix)
            if source.is_file() and not source.is_symlink():
                destination = target / source.relative_to(profile)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                destination.chmod(0o600)
                copied.append(str(source.relative_to(profile)))
    return copied


def restore_databases(target: Path, profile: Path, copied: list[str]) -> None:
    """Put the copied databases back, with Chrome closed. Companions that did not exist then
    are removed, so a journal written since cannot be replayed onto the old file."""
    bases = set()
    for name in copied:
        for suffix in _COMPANIONS[1:]:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        bases.add(name)
    for base in bases:
        for suffix in _COMPANIONS:
            live = profile / (base + suffix)
            saved = target / (base + suffix)
            if saved.is_file():
                shutil.copy2(saved, live)
            elif live.is_file() and suffix:
                live.unlink()


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def key(cookie: dict) -> tuple:
    partition = cookie.get("partitionKey") or {}
    if isinstance(partition, dict):
        partition = partition.get("topLevelSite", "")
    return (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", "/"), str(partition or ""))


def backup_document(cookies: list[dict], profile: Path, executable: str) -> dict:
    """A storage-state file `storage_state_load` accepts, plus Chrome's own records so the move
    can set every attribute back."""
    return {
        "cookies": [{"name": c.get("name"), "value": c.get("value"), "domain": c.get("domain"),
                     "path": c.get("path", "/"), "expires": c.get("expires", -1),
                     "secure": c.get("secure", False), "http_only": c.get("httpOnly", False)}
                    for c in cookies],
        "origins": {},
        "saved_at": time.time(),
        "pre_v11_migration": {"profile": str(profile), "chrome": executable, "count": len(cookies)},
        "cdp_cookies": cookies,
    }


def cookie_params(cookie: dict) -> dict:
    """Chrome's Network.Cookie as the Network.CookieParam that sets it again."""
    params: dict[str, Any] = {k: cookie[k] for k in ("name", "value", "domain", "path", "secure",
                                                     "httpOnly", "priority", "sourceScheme",
                                                     "sourcePort") if k in cookie}
    if cookie.get("sameSite"):
        params["sameSite"] = cookie["sameSite"]
    expires = cookie.get("expires")
    if not cookie.get("session") and isinstance(expires, (int, float)) and expires > 0:
        params["expires"] = expires
    if cookie.get("partitionKey"):
        params["partitionKey"] = cookie["partitionKey"]
    return params
