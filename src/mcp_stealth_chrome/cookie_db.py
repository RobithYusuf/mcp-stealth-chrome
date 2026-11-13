"""Reading a profile's cookie databases for a decision that must not be wrong.

Chrome keeps cookies in SQLite with a rollback journal (measured on Chrome 157 and Beta 159:
the header says rollback, `Cookies-journal` sits beside the file, no `-wal`). A Chrome that is
killed in the middle of a write leaves a hot journal: Beta 159, killed 3 s after setting two
cookies, left a 12,824-byte `Cookies-journal`. The file then holds pages that were never
committed, and the committed state is only what SQLite rebuilds from the journal.

`mode=ro&immutable=1` reads the file as it lies and ignores the journal (and a `-wal`): a
transaction that rewrote 399 of 400 `v10` rows and was killed before its commit read as 399
`v11` rows. `mode=ro` without immutable refuses a hot journal ("attempt to write a readonly
database"), because rolling it back writes. So a decision reads a private copy: the database
and its `-journal` and `-wal` are copied into a temporary folder of this process (0700), the
copy is opened normally so SQLite rolls the journal back or replays the WAL there, and
`PRAGMA integrity_check` must answer ok. The `-shm` is left behind: SQLite rebuilds it from
the WAL. Nothing in the profile is written.

A copy taken while something writes the database can be torn, so each file's size, change
time and inode are read before and after the copy, and a difference is an error. Every error
means unknown, which callers must not read as "no cookies".
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

COMPANIONS = ("-journal", "-wal")
_ALL_SUFFIXES = ("", "-journal", "-wal", "-shm")


class Unreadable(Exception):
    """The database could not be read with certainty."""


class Changed(Unreadable):
    """The database was written while it was being copied."""


def all_databases(profile: Path) -> list[Path]:
    """Where every sub-profile of `profile` keeps (or would keep) its cookies, whether or not the
    file exists yet: `<sub>/Cookies` on older profiles, `<sub>/Network/Cookies` on newer ones.
    Raises Unreadable on a sub-profile or `Network` folder that is a link: what it points at
    is outside the profile, and a decision must not read or change it as if it were inside."""
    found = []
    with os.scandir(profile) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.is_symlink():
                if os.path.isdir(entry.path):
                    raise Unreadable(f"{entry.path} is a link")
                continue
            if entry.is_dir(follow_symlinks=False):
                sub = Path(entry.path)
                if (sub / "Network").is_symlink():
                    raise Unreadable(f"{sub / 'Network'} is a link")
                found += [sub / "Cookies", sub / "Network" / "Cookies"]
    return found


def fingerprint(databases: list[Path]) -> tuple:
    """Size, change time and inode of each database and its companions; absent files count,
    so a database that appears is a change too."""
    out = []
    for database in databases:
        for suffix in _ALL_SUFFIXES:
            path = Path(str(database) + suffix)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                out.append((str(path), None))
                continue
            out.append((str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_mode))
    return tuple(out)


def read(database: Path, query: Callable[[sqlite3.Connection], Any]) -> Any:
    """`query(connection)` over the committed state of `database`, read from a private copy.
    Raises Unreadable."""
    database = Path(database)
    before = fingerprint([database])
    folder = Path(tempfile.mkdtemp(prefix="mcp-stealth-cookies-"))
    try:
        for suffix in ("", *COMPANIONS):
            source = Path(str(database) + suffix)
            if source.is_symlink():
                raise Unreadable(f"{source} is a link")
            if source.is_file():
                target = folder / ("Cookies" + suffix)
                shutil.copyfile(source, target)
                os.chmod(target, 0o600)
        if fingerprint([database]) != before:
            raise Changed(f"{database} changed while it was being read")
        copy = folder / "Cookies"
        if not copy.is_file():
            raise Unreadable(f"{database} is not there")
        try:
            with contextlib.closing(sqlite3.connect(copy, timeout=1)) as db:
                checked = db.execute("PRAGMA integrity_check").fetchall()
                if checked != [("ok",)]:
                    raise Unreadable(f"{database} failed its integrity check: {checked[:3]}")
                return query(db)
        except sqlite3.Error as exc:
            raise Unreadable(f"{database}: {type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        raise Unreadable(f"{database}: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def stable_read(databases: list[Path], query: Callable[[sqlite3.Connection], Any],
                watch: Optional[list[Path]] = None, attempts: int = 3) -> tuple[dict, tuple]:
    """{database: query result} for each of `databases` that exists, and the fingerprint of
    `watch` (default: the same databases), unchanged from before the first read to after the
    last. A write in between is retried a few times, then is an error. Raises Unreadable."""
    watch = list(databases) if watch is None else watch
    for _ in range(attempts):
        before = fingerprint(watch)
        try:
            found = {db: read(db, query) for db in databases if db.is_file() or db.is_symlink()}
        except Changed:
            continue
        after = fingerprint(watch)
        if after == before:
            return found, after
    raise Unreadable("the cookie databases kept changing while they were being read")
