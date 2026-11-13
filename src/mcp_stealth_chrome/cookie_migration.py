"""Moving a profile's cookies to Chrome 158's encryption without losing them.

Chrome 158 on macOS encrypts with a different key in any profile outside its default folder
(Chromium CL 8603400, feature `KeychainKeyProviderUserDataDirProtection`): the old `v10` key is
registered as unavailable, and cookies it cannot read are deleted. Every profile this server
opens is outside the default folder. Launched with
`--disable-features=KeychainKeyProviderUserDataDirProtection` (the flag), the same Chrome reads
`v10` again, but then cannot read `v11` and deletes that instead (Beta 159: 3 of 3), and a
store holding both kinds is deleted whole either way (5 v10 and 2 v11 of one host: 0 left,
flag or not). So the flag is used only for a profile that holds no `v11`, and each profile with
`v10` cookies is moved once, in an order where every point a crash can stop at recovers:

Preparing writes nothing in the profile.
1. The launched sub-profile's cookie databases and their journals are copied to
   storage-states (`*-db`), with the source unchanged across the copy, the copy passing
   `integrity_check`, and every file and folder fsynced.
2. A headless Chrome 158 with the flag reads every `v10` cookie from a temporary user-data
   folder holding only a copy of those databases with the `v10` rows (the Keychain key does
   not depend on the folder), and is closed and verified gone. The profile is never opened.
3. The cookies go to a backup file (0600, written to a private temporary file, fsynced,
   renamed, folder fsynced) that `storage_state_load` accepts, read back and compared.
4. The profile's cookie databases must still be as the decision read them.
The launch then checks the profile once more (lock, fingerprint) and only then commits:
5. A marker in the profile records the move as `pending` with the backup, its digest and
   count, written the same durable way.
6. Beside `v11` cookies, the `v10` rows are deleted in one transaction per database that
   first checks every one of them is in the backup.
7. The requested launch starts without the flag, sets the backed-up cookies again, reads them
   back and compares names, places, partitions and value digests; all equal marks `done`.

A crash before 5 leaves the profile as it was. From 5 on, the pending marker and its backup
make the next launch resume: it checks the backup against the digest and against every `v10`
row still in the profile, takes those rows out from beside `v11` ones, and sets the cookies
again. Only cookies move. Saved passwords and other data Chrome encrypted with `v10` stay
unreadable to Chrome 158 in that profile.

This module only reads databases and writes files; starting Chrome is the server's part.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import cookie_db, profile_version

FEATURE = "KeychainKeyProviderUserDataDirProtection"
FIRST_MAJOR = 158
MARKER = ".mcp-stealth-v11-migration.json"
# 2: the marker names its profile folder and sub-profile and records the backup's digest and
# count. A pending marker without them was written by v0.16.1 or earlier and is not trusted.
MARKER_VERSION = 2
READER_PREFIX = "mcp-stealth-reader-"
BACKUP_PREFIX = "pre-v11-migration"
_COMPANIONS = ("", "-journal", "-wal")
# A backup holds every cookie value in plain text. Once its move is done and every cookie was
# read back, it is kept this long (MCP_STEALTH_MIGRATION_BACKUP_DAYS) and then removed.
KEEP_DAYS_ENV = "MCP_STEALTH_MIGRATION_BACKUP_DAYS"
KEEP_DAYS = 14
_PROTECTED_UNDER_TEST = Path(os.path.expanduser("~")) / ".mcp-stealth"
# Chrome's time: microseconds since 1601-01-01.
_CHROME_EPOCH_OFFSET = 11_644_473_600
# Chrome caps a cookie's lifetime at 400 days from when it is set.
_MAX_LIFETIME = 400 * 86400


class Changed(Exception):
    """The profile's cookies changed since the decision; nothing may proceed on it."""


@dataclass
class Plan:
    """What a launch has to do. kind: none | migrate | resume | flag | refuse.

    `flag`: open with the feature turned off and move nothing this time; only ever for a
    profile read to hold no `v11` cookie. `refuse`: neither way is safe, or what the profile
    holds is not known; nothing starts and nothing is written. `resume`: a move got as far as
    its pending marker, and its backup holds every `v10` cookie still in the profile.

    `fingerprint` is the state of the launched sub-profile's cookie databases the decision was
    made on (cookie_db.fingerprint); the launch compares it again just before committing.
    `v10_keys` counts each loaded `v10` cookie by key(): name, host, path, partition."""
    kind: str
    reason: str = ""
    subprofile: str = "Default"
    rows: int = 0
    v10: int = 0
    marker: dict = field(default_factory=dict)
    v11: int = 0
    v10_keys: dict = field(default_factory=dict)
    fingerprint: Optional[tuple] = None


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


def candidate_databases(profile: Path, subprofile: str) -> list[Path]:
    """Where the sub-profile keeps its cookies, whether or not the files exist yet. Raises
    cookie_db.Unreadable when a folder on the way is a link, or the name leaves the profile:
    reading or changing a database outside the profile is never meant."""
    profile = Path(profile)
    if not subprofile or subprofile in (".", "..") or "/" in subprofile or "\\" in subprofile:
        raise cookie_db.Unreadable(f"{subprofile!r} is not a sub-profile name")
    folder = profile / subprofile
    for part in (profile, folder, folder / "Network"):
        if part.is_symlink():
            raise cookie_db.Unreadable(f"{part} is a link")
    return [folder / "Network" / "Cookies", folder / "Cookies"]


def cookie_databases(profile: Path, subprofile: str) -> list[Path]:
    return [p for p in candidate_databases(profile, subprofile) if p.is_file() or p.is_symlink()]


@dataclass
class Census:
    loaded: int = 0          # persistent and unexpired: what Chrome loads
    v10: int = 0
    v11: int = 0
    v10_keys: dict = field(default_factory=dict)   # key() of each loaded v10 cookie -> count


def _rows(db) -> list:
    """(persistent, expires, prefix, name, host, path, partition) of every row. Columns differ
    between Chrome versions; a missing one reads as a cookie Chrome loads, never as no cookie."""
    have = {row[1] for row in db.execute("PRAGMA table_info(cookies)")}
    if "encrypted_value" not in have:
        raise cookie_db.Unreadable("its cookies table has no encrypted_value column")
    defaults = {"is_persistent": "1", "expires_utc": "0", "name": "''", "host_key": "''",
                "path": "'/'", "top_frame_site_key": "''"}
    pick = {c: (c if c in have else d) for c, d in defaults.items()}
    picked = [pick["is_persistent"], pick["expires_utc"], "substr(encrypted_value, 1, 3)",
              pick["name"], pick["host_key"], pick["path"], pick["top_frame_site_key"]]
    return db.execute(f"SELECT {', '.join(picked)} FROM cookies").fetchall()


def _row_key(name, host, path, partition) -> tuple:
    return (name or "", host or "", path or "/", partition or "")


def census(databases: list[Path], watch: Optional[list[Path]] = None) -> tuple[Census, tuple]:
    """What the databases hold in their committed state (cookie_db.read: a private copy with
    the journal rolled back), and the fingerprint of `watch` they were read under. Raises
    cookie_db.Unreadable: unknown is not "none"."""
    found, fingerprint = cookie_db.stable_read(databases, _rows, watch=watch)
    now = int((time.time() + _CHROME_EPOCH_OFFSET) * 1_000_000)
    result = Census()
    keys: Counter = Counter()
    for rows in found.values():
        for persistent, expires, prefix, name, host, path, partition in rows:
            loaded = bool(persistent) and (not expires or expires > now)
            result.loaded += loaded
            if prefix == b"v10":
                result.v10 += 1
                if loaded:
                    keys[_row_key(name, host, path, partition)] += 1
            elif prefix == b"v11":
                result.v11 += 1
    result.v10_keys = dict(keys)
    return result, fingerprint


def count_rows(databases: list[Path]) -> tuple[int, int]:
    """(cookies Chrome will load, of which `v10`). Raises when a database cannot be read."""
    found, _ = census(databases)
    return found.loaded, found.v10


def fingerprint_of(profile: Path, subprofile: str) -> tuple:
    return cookie_db.fingerprint(candidate_databases(Path(profile), subprofile))


def missing_from(needed: dict, have: Counter) -> list:
    """The keys `needed` counts more often than `have` does."""
    return sorted(k for k, n in needed.items() if have.get(k, 0) < n)


def file_digest(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def verified_backup(path: Path, marker: Optional[dict] = None) -> Optional[list]:
    """The cookies of a backup, or None when it cannot be trusted: unreadable, a link, or not
    the file the marker recorded (digest, count)."""
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return None
        if marker and marker.get("digest") and file_digest(path) != marker["digest"]:
            return None
        cookies = json.loads(path.read_text(encoding="utf-8")).get("cdp_cookies")
        if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
            return None
        if marker and isinstance(marker.get("count"), int) and marker["count"] != len(cookies):
            return None
        return cookies
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def read_marker(profile: Path) -> dict:
    """The marker, or {} when there is none or it cannot be trusted (a link is never followed)."""
    path = Path(profile) / MARKER
    try:
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(profile: Path, data: dict) -> None:
    write_durable(Path(profile) / MARKER, json.dumps(data, indent=2))


def write_durable(path: Path, text: str) -> None:
    """Write a private file that survives a crash or a power cut once this returns.

    A temporary file is created beside it (0600, O_EXCL, O_NOFOLLOW: a link is never followed),
    written, fsynced, renamed over `path` (a link at `path` is replaced, not written through),
    and the folder is fsynced so the rename itself is kept. A reader sees the old file or the
    whole new one."""
    path = Path(path)
    folder = path.parent
    if folder.is_symlink():
        raise OSError(f"{folder} is a link")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for _ in range(20):
        tmp = folder / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not create a temporary file beside {path}")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    fsync_folder(folder)


def fsync_folder(folder: Path) -> None:
    """Keep a folder's entries (a rename, a new file) across a power cut. POSIX only: Windows
    cannot open a folder this way and keeps the entry once the file is closed."""
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def plan(profile: Optional[Path], args: list[str], executable: Optional[str]) -> Plan:
    """Decide before Chrome starts. Never raises and never writes: what cannot be read means
    `refuse`."""
    if profile is None or not applies(executable):
        return Plan("none")
    profile = Path(profile)
    subprofile = subprofile_from_args(args)
    try:
        if not profile.is_dir():
            return Plan("none")
        real = os.path.realpath(profile)
        if any(real == os.path.realpath(d) for d in default_data_dirs()):
            return Plan("none")  # Chrome keeps the v10 key in its own folder
        marker = read_marker(profile)
        major = profile_version.binary_version(executable)[0]
        watch = candidate_databases(profile, subprofile)
        found, fingerprint = census([p for p in watch if p.is_file() or p.is_symlink()], watch=watch)
    except Exception as exc:
        return Plan("refuse", f"its cookie database could not be read with certainty ({type(exc).__name__}: "
                    f"{exc}). Chrome 158 deletes the cookies it cannot read whichever way it opens the "
                    "profile (v10 ones without the protection turned off, v11 ones with it)",
                    subprofile=subprofile)
    seen = dict(subprofile=subprofile, rows=found.loaded, v10=found.v10, v11=found.v11,
                v10_keys=found.v10_keys, marker=marker, fingerprint=fingerprint)
    if marker.get("state") == "pending":
        why = _untrusted_pending(marker, profile, subprofile)
        if why:
            return Plan("refuse", why, **seen)
        cookies = verified_backup(Path(str(marker["backup"])), marker)
        if cookies is None:
            # Part of the profile may already be stripped: a new move would read only what is
            # left, so the way back is the database copy, never a new move.
            return Plan("refuse", "a move begun earlier is pending, but its backup is missing or is "
                        "not the file the marker recorded. " + recovery(marker, profile), **seen)
        missing = missing_from(found.v10_keys, Counter(key(c) for c in cookies))
        if not missing:
            return Plan("resume", **seen)
        # A cookie written since: move again, carrying over what only this backup holds.
        return Plan("migrate", "the move begun earlier is made again, because its backup lacks "
                    + ", ".join(f"{n}@{h}" for n, h, *_ in missing[:5])
                    + (" and more" if len(missing) > 5 else ""), **seen)
    if not found.v10:
        return Plan("none", **seen)
    if (marker.get("state") == "done" and marker.get("major") == major and not found.v11
            and marker.get("subprofile", subprofile) == subprofile):
        # Moved before by this same Chrome, and v10 is back: this Chrome evidently writes
        # v10 here (the protection was turned off remotely). Moving again would loop. With a
        # v11 cookie beside them the flag would delete it, so such a profile is moved again.
        return Plan("flag", "this Chrome wrote v10 cookies again after they were moved, so "
                    "the profile is opened with the protection turned off", **seen)
    return Plan("migrate", **seen)


def _untrusted_pending(marker: dict, profile: Path, subprofile: str) -> str:
    """Why a pending marker must not be resumed here, or "". It must be this version's, for
    this profile folder and for the sub-profile being launched: a move of Default resumed for
    Profile 1 would set Default's cookies there and delete Profile 1's."""
    if (marker.get("version") != MARKER_VERSION or not marker.get("backup") or not marker.get("digest")
            or not isinstance(marker.get("count"), int) or not marker.get("subprofile")
            or not marker.get("profile")):
        return ("a move begun by an older version of this server is pending, and its backup cannot be "
                "checked (no digest, count or sub-profile). " + recovery(marker, profile))
    if marker["profile"] != os.path.realpath(profile):
        return (f"its pending marker was written for another profile folder ({marker['profile']}), so it "
                "is not this profile's. " + recovery(marker, profile))
    if marker["subprofile"] != subprofile:
        return (f"a move of its sub-profile {marker['subprofile']!r} is pending, and must finish before "
                f"another sub-profile is opened: launch with profile_directory={marker['subprofile']!r} first")
    return ""


def recovery(marker: dict, profile: Path) -> str:
    """How to go back to the cookies as they were before a pending move, in words."""
    copy = marker.get("database_copy")
    sound = False
    if copy and Path(str(copy)).is_dir() and not Path(str(copy)).is_symlink():
        try:
            databases = [p for p in Path(str(copy)).rglob("Cookies") if p.is_file() and not p.is_symlink()]
            for database in databases:
                cookie_db.read(database, lambda db: None)
            sound = bool(databases)
        except Exception:
            sound = False
    if not sound:
        return (f"The database copy taken before the move ({copy or 'not recorded'}) is missing or damaged, "
                "so a backup of the whole profile is the way back. Nothing was changed.")
    return (f"The database copy taken before the move, {copy}, passes its integrity check. To go back to "
            f"the cookies as they were: close every Chrome that has this profile open, copy the files in that "
            f"folder over the same paths in {profile}, remove {Path(profile) / MARKER}, and launch again. "
            "Nothing was changed.")


def label(profile: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", Path(profile).name).strip(".-") or "profile"


def copy_databases(profile: Path, subprofile: str, target: Path) -> list[str]:
    """Copy the sub-profile's cookie databases and their journals to `target` (a new private
    folder), keeping the layout. The sources must be unchanged across the copy, each copy must
    pass integrity_check (read through cookie_db, which leaves the copy as it is), and every
    file and folder is fsynced before this returns. Raises on anything less."""
    databases = cookie_databases(profile, subprofile)
    before = cookie_db.fingerprint(databases)
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied, folders = [], {target}
    for database in databases:
        for suffix in _COMPANIONS:
            source = Path(str(database) + suffix)
            if source.is_symlink():
                raise cookie_db.Unreadable(f"{source} is a link")
            if not source.is_file():
                continue
            destination = target / source.relative_to(profile)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            folders.update(p for p in destination.parents if target in (p, *p.parents))
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            copied.append(str(source.relative_to(profile)))
    if cookie_db.fingerprint(databases) != before:
        raise cookie_db.Changed("the cookie databases changed while they were copied")
    for database in databases:
        cookie_db.read(target / database.relative_to(profile), lambda db: None)
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        fsync_folder(folder)
    fsync_folder(target.parent)
    return copied


def reader_folder(copy: Path, subprofile: str, keep: bytes) -> Path:
    """A temporary user-data folder for the reading Chrome: the copied databases with only the
    rows encrypted as `keep`, so the profile itself is never opened. Chrome 158 reads nothing
    from a store holding both kinds. The caller removes it."""
    folder = Path(tempfile.mkdtemp(prefix=READER_PREFIX))
    try:
        source = Path(copy) / subprofile
        if source.is_dir():
            shutil.copytree(source, folder / subprofile)
        for database in cookie_databases(folder, subprofile):
            with contextlib.closing(sqlite3.connect(database, timeout=5)) as db, db:
                db.execute("DELETE FROM cookies WHERE substr(encrypted_value, 1, 3) != ?", (keep,))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder


def row_digests(db, prefix: bytes) -> Counter:
    """A digest of every row encrypted as `prefix`, by every column but last_access_utc (which a
    read moves), mapped to its key for messages. Two rows alike in key but not in value,
    expiry or flags differ here."""
    columns = [row[1] for row in db.execute("PRAGMA table_info(cookies)") if row[1] != "last_access_utc"]
    found: Counter = Counter()
    for row in db.execute(f"SELECT {', '.join(columns)} FROM cookies WHERE substr(encrypted_value, 1, 3) = ?",
                          (prefix,)):
        named = dict(zip(columns, row))
        digest = hashlib.sha256(json.dumps(
            [v.hex() if isinstance(v, (bytes, bytearray)) else v for v in row], default=str).encode()).hexdigest()
        found[(digest, _row_key(named.get("name"), named.get("host_key"), named.get("path"),
                                named.get("top_frame_site_key")))] += 1
    return found


def snapshot_digests(copy: Path, subprofile: str, prefix: bytes) -> Counter:
    """row_digests of the database copy a backup was read from. Raises when it cannot be read."""
    found: Counter = Counter()
    for database in cookie_databases(Path(copy), subprofile):
        found += cookie_db.read(database, lambda db: row_digests(db, prefix))
    return found


def strip(profile: Path, subprofile: str, prefix: bytes, allowed: Optional[dict] = None,
          snapshot: Optional[Counter] = None) -> int:
    """Delete the rows encrypted as `prefix` from the sub-profile's cookie databases, with no
    Chrome running. Each database in one transaction that first takes the write lock and
    checks, when `allowed` is given, that every loaded row it deletes is counted there (the
    backup): a row written since the backup raises Changed and nothing in that database is
    deleted. Chrome 158+ reads nothing from a store holding both kinds and deletes every row,
    so a mixed profile has its `v10` rows taken out once they are all in the backup. A crash
    between two databases leaves the pending marker, and the next launch resumes."""
    removed = 0
    now = int((time.time() + _CHROME_EPOCH_OFFSET) * 1_000_000)
    databases = cookie_databases(profile, subprofile)

    def check(db) -> None:
        if allowed is None:
            return
        rows = [r for r in _rows(db) if r[2] == prefix]
        there = Counter(_row_key(*r[3:]) for r in rows if bool(r[0]) and (not r[1] or r[1] > now))
        missing = missing_from(dict(there), Counter(allowed))
        if missing:
            raise Changed("cookies arrived after the backup: " + ", ".join(
                f"{n}@{h}" for n, h, *_ in missing[:5]))
        if snapshot is not None:
            changed = row_digests(db, prefix) - snapshot
            if changed:
                raise Changed("a cookie changed since the backup: " + ", ".join(
                    f"{k[0]}@{k[1]}" for _d, k in list(changed)[:5]))

    # Every database is checked before any is changed, so a row written since the backup stops
    # the strip before it starts; each is checked again inside its own transaction.
    for database in databases:
        if database.is_symlink():
            raise cookie_db.Unreadable(f"{database} is a link")
        cookie_db.read(database, check)
    for database in databases:
        with contextlib.closing(sqlite3.connect(database, timeout=5, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                check(db)
                removed += db.execute("DELETE FROM cookies WHERE substr(encrypted_value, 1, 3) = ?",
                                      (prefix,)).rowcount
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
        _committed(database)
    return removed


def _committed(database: Path) -> None:
    """Called after each database's transaction; tests stop the process here."""


def key(cookie: dict) -> tuple:
    partition = cookie.get("partitionKey") or {}
    if isinstance(partition, dict):
        partition = partition.get("topLevelSite", "")
    return (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", "/"), str(partition or ""))


def value_digest(cookie: dict) -> str:
    """What must come back unchanged besides the key: the value and every attribute cookie_params
    sets back (flags, priority, source scheme and port, the whole partition), hashed so no value
    is ever printed or compared in the clear."""
    partition = cookie.get("partitionKey") or {}
    picked = [cookie.get("value", ""), bool(cookie.get("secure")), bool(cookie.get("httpOnly")),
              cookie.get("sameSite") or "", cookie.get("priority") or "", cookie.get("sourceScheme") or "",
              cookie.get("sourcePort"), json.dumps(partition, sort_keys=True)]
    return hashlib.sha256(json.dumps(picked).encode("utf-8")).hexdigest()


def same_expiry(saved: dict, back: dict, now: Optional[float] = None) -> bool:
    """Expiry within a second, or capped by Chrome's 400-day limit, or both session cookies."""
    a, b = saved.get("expires"), back.get("expires")
    if saved.get("session") or not isinstance(a, (int, float)) or a <= 0:
        return bool(back.get("session")) or not isinstance(b, (int, float)) or b <= 0
    if not isinstance(b, (int, float)) or b <= 0:
        return False
    now = time.time() if now is None else now
    return abs(a - b) <= 1 or (b < a and b >= now + _MAX_LIFETIME - 86400)


def compare(saved: list, back: list) -> tuple[list, list]:
    """(missing keys, keys whose value, flags or expiry differ), comparing digests only."""
    found: dict = {}
    for cookie in back:
        found.setdefault(key(cookie), []).append(cookie)
    missing, differ = [], []
    for cookie in saved:
        candidates = found.get(key(cookie)) or []
        if not candidates:
            missing.append(key(cookie))
            continue
        digest = value_digest(cookie)
        match = next((c for c in candidates if value_digest(c) == digest and same_expiry(cookie, c)), None)
        if match is None:
            differ.append(key(cookie))
        else:
            candidates.remove(match)
    return missing, differ


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


def retention_days() -> float:
    try:
        return max(0.0, float(os.environ.get(KEEP_DAYS_ENV, KEEP_DAYS)))
    except ValueError:
        return float(KEEP_DAYS)


def expire_backups(folder: Path, *, now: Optional[float] = None, keep_days: Optional[float] = None) -> dict:
    """Remove the backups (`pre-v11-migration-*.json` and its `-db` copy) of moves that are done.

    A backup goes only when the profile it names has a `done` marker, which is written once
    every cookie was set again and read back, and that was more than `keep_days` ago. Anything
    else stays: a pending move needs its backup, and a profile that is gone or whose marker
    cannot be read proves nothing. Only regular files and folders directly in `folder` with
    this module's prefix are touched, never through a link. What stays is made 0600 (folders
    0700). Under a test the person's own folder is refused, whatever the caller hands in.
    """
    folder = Path(folder)
    now = time.time() if now is None else now
    days = retention_days() if keep_days is None else keep_days
    report: dict[str, Any] = {"state": "done", "removed": [], "kept": 0, "errors": []}
    real = Path(os.path.realpath(folder))
    protected = Path(os.path.realpath(_PROTECTED_UNDER_TEST))
    if os.environ.get("PYTEST_CURRENT_TEST") and protected in (real, *real.parents):
        report["state"] = "refused"
        return report
    try:
        if folder.is_symlink() or not folder.is_dir():
            return report
        entries = sorted(folder.iterdir())
    except OSError as exc:
        report["errors"].append(f"{folder}: {exc.strerror or exc}")
        return report
    for path in entries:
        if not (path.name.startswith(BACKUP_PREFIX + "-") and path.suffix == ".json"):
            continue
        copy = folder / f"{path.stem}-db"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if _done_long_enough(path, now, days * 86400):
                path.unlink()
                if copy.is_dir() and not copy.is_symlink():
                    shutil.rmtree(copy)
                report["removed"].append(path)
                continue
            report["kept"] += 1
            _make_private(path, copy)
        except OSError as exc:
            report["errors"].append(f"{path.name}: {exc.strerror or exc}")
    return report


def _done_long_enough(backup: Path, now: float, keep_seconds: float) -> bool:
    """Whether a backup may go: its profile's move is done and verified (the marker says done:
    every cookie was read back), and it is this move's backup or one it superseded; or no marker
    refers to anything (a backup written before a crash that came before its marker). In each
    case it is older than the retention. While a move is pending no backup goes. A profile
    that is gone, or whose marker cannot be read, proves nothing and keeps it."""
    try:
        document = json.loads(backup.read_text(encoding="utf-8"))
        profile = Path(str(document["pre_v11_migration"]["profile"]))
        saved_at = float(document.get("saved_at") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    marker_file = profile / MARKER
    if not profile.is_dir() or marker_file.is_symlink():
        return False
    if not marker_file.exists():
        return now - saved_at >= keep_seconds  # nothing refers to it
    marker = read_marker(profile)
    if not marker:
        return False  # there, and unreadable
    finished = marker.get("finished_at")
    if marker.get("state") != "done" or not isinstance(finished, (int, float)):
        # A move still pending may need any of its backups: a retry carries cookies over from
        # the one before, and when the newest is damaged the older one is the only copy.
        return False
    if marker.get("backup") and marker["backup"] != str(backup):
        return now - saved_at >= keep_seconds  # superseded by a move that is done
    return saved_at <= finished and now - finished >= keep_seconds


def _named_by_a_process(path: Path) -> Optional[bool]:
    from . import state
    found = state.processes_naming(path)
    return None if found is None else bool(found)


def remove_reader_leftovers(folder: Optional[Path] = None, *, older_than: float = 3600) -> list:
    """Remove the temporary folders a reading Chrome used (`mcp-stealth-reader-*`) that a killed
    server left behind: older than an hour and named by no running process. Only folders with
    that prefix directly in `folder` (the temporary folder), never through a link. They hold an
    encrypted copy of cookie databases, no plain text."""
    folder = Path(folder or tempfile.gettempdir())
    removed = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            if (not entry.name.startswith(READER_PREFIX) or entry.is_symlink() or not entry.is_dir()
                    or time.time() - entry.stat().st_mtime < older_than):
                continue
            if _named_by_a_process(entry) is not False:
                continue  # in use, or unknown
            shutil.rmtree(entry)
            removed.append(entry)
        except OSError:
            continue
    return removed


def _make_private(backup: Path, copy: Path) -> None:
    with contextlib.suppress(OSError):
        if os.stat(backup).st_mode & 0o077:
            backup.chmod(0o600)
    if copy.is_dir() and not copy.is_symlink():
        for root, dirs, files in os.walk(copy):
            with contextlib.suppress(OSError):
                os.chmod(root, 0o700)
            for name in files:
                with contextlib.suppress(OSError):
                    target = os.path.join(root, name)
                    if not os.path.islink(target):
                        os.chmod(target, 0o600)
