"""One launch at a time per profile, across every process of this project.

`browser_launch`, `spawn_browser` and the launcher's `open` reserve the profiles a launch may
open before they look at who holds them, and keep the reservation until the browser holds the
profile's own lock and its keep_alive record is written. Chrome takes SingletonLock about half a
second after it starts, and a check of that lock is no reservation: a click racing a server's
launch used to see the profile free in both and start two browsers on it.

A file lock per profile, beside the keep_alive records, released by the system when its holder
dies: flock on POSIX, a one-byte msvcrt.locking lock on Windows. Reentrant within a process,
because `open` holds it and then calls browser_launch in the same process.

A lock file that cannot be made or opened (a keep-alive folder that is read-only) does not fail
the launch: it goes on unreserved, as every launch did before reservations, and says so (note()).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import keep_alive as _keep_alive

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

_guard = threading.Lock()
_held: dict[str, list] = {}  # profile key -> [fd, count]
# What the launch holding a reservation should say about it: why one could not be taken.
_NOTE: contextvars.ContextVar[str] = contextvars.ContextVar("launch_lock_note", default="")


class Unavailable(Exception):
    """The lock file could not be made or opened; the launch goes on without a reservation."""


def note() -> str:
    """For the result of the launch now holding a reservation: "" or why it has none."""
    return _NOTE.get()


def lock_path(profile: Any) -> Path:
    key = _keep_alive.profile_key(profile)
    return _keep_alive.record_dir() / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.launch.lock"


def _unavailable_note(key: str, exc: BaseException) -> str:
    why = getattr(exc, "strerror", None) or exc
    return (f" [no launch reservation: {lock_path(key)} could not be used ({type(exc).__name__}: "
            f"{why}); this launch went on without one, as launches did before, so a launch of this "
            f"profile by another process at the same moment was not held off]")


def _lock(fd: int) -> bool:
    """Take the lock on an open file without waiting. False when another holds it."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _try(key: str) -> bool:
    with _guard:
        if key in _held:
            _held[key][1] += 1
            return True
        path = lock_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise Unavailable(_unavailable_note(key, exc)) from exc
        if not _lock(fd):
            os.close(fd)
            return False
        _held[key] = [fd, 1]
        return True


def _release(key: str) -> None:
    with _guard:
        entry = _held.get(key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            del _held[key]
            with contextlib.suppress(OSError):
                os.close(entry[0])  # releases the flock or the msvcrt lock with the handle


def _keys(paths: Iterable[Any]) -> list[str]:
    if fcntl is None and msvcrt is None:
        return []
    return sorted({_keep_alive.profile_key(p) for p in paths if p})  # one order: no deadlock


def _busy(key: str) -> str:
    return (f"another launch on {key} is in progress (a click on the launcher, or another "
            f"server); nothing was started")


@contextlib.contextmanager
def hold(paths: Iterable[Any], wait: float):
    """Reserve these profiles, waiting up to `wait` seconds. Raises TimeoutError."""
    got: list[str] = []
    token = _NOTE.set("")
    try:
        for key in _keys(paths):
            deadline = time.monotonic() + wait
            try:
                while not _try(key):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(_busy(key))
                    time.sleep(0.1)
            except Unavailable as why:
                _NOTE.set(_NOTE.get() + str(why))
                continue
            got.append(key)
        yield
    finally:
        for key in got:
            _release(key)
        _NOTE.reset(token)


@contextlib.asynccontextmanager
async def reserve(paths: Iterable[Any], wait: float):
    """hold() for the event loop. Yields "" once reserved, or why it could not be. A profile whose
    lock file cannot be used is not reserved; note() says so for the launch's result."""
    got: list[str] = []
    problem = ""
    token = _NOTE.set("")
    try:
        for key in _keys(paths):
            deadline = time.monotonic() + wait
            try:
                while not _try(key):
                    if time.monotonic() >= deadline:
                        problem = _busy(key)
                        break
                    await asyncio.sleep(0.1)
            except Unavailable as why:
                _NOTE.set(_NOTE.get() + str(why))
                continue
            if problem:
                break
            got.append(key)
        yield problem
    finally:
        for key in got:
            _release(key)
        _NOTE.reset(token)
