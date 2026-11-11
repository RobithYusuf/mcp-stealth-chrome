"""A headed stealth Chrome with its own Dock icon, so it is not mistaken for the person's Chrome.

Opt-in, with MCP_STEALTH_DOCK_ICON=1. macOS takes the Dock icon from the app bundle a process
runs out of. So, when asked for, a headed launch runs
from a copy of the installed Chrome, `~/.mcp-stealth/app/Stealth Chrome.app`, that carries the
project icon. The copy is an APFS clone: made in well under a second, sharing its blocks with
the original. The icon goes on as a custom file icon (an `Icon\\r` file and a FinderInfo flag
at the bundle root), which leaves `Contents/` byte for byte what Google signed. Info.plist is
never edited: that would break the signature, and with it the Keychain access that decrypts
cookies. The Dock tooltip therefore still says "Google Chrome".

Nothing here may stop a launch, or make one wait. The copy is built on a background thread,
started when the server starts and again whenever a launch finds the copy missing or out of
date. `prepare` only looks: a copy that is ready is used, and otherwise it answers with a note
and the caller starts the installed Chrome this time. The installed Chrome is only ever read.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import functools
import hashlib
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ENV_SWITCH = "MCP_STEALTH_DOCK_ICON"
ENV_APP_DIR = "MCP_STEALTH_APP_DIR"
APP_NAME = "Stealth Chrome.app"
MARKER_NAME = "Stealth Chrome.json"
ICON_PATH = Path(__file__).parent / "assets" / "dock_icon.png"
ICON_FILE = "Icon\r"
_BUILD_PREFIX = ".building-"
_LOCK_WAIT = 90.0
_WARM_UP_TIMEOUT = 60.0
_BACKUP_EXCLUDE = "com.apple.metadata:com_apple_backup_excludeItem"
_LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
               "LaunchServices.framework/Support/lsregister")

_SET_ICON_PY = (
    "import sys\n"
    "from AppKit import NSImage, NSWorkspace\n"
    "image = NSImage.alloc().initWithContentsOfFile_(sys.argv[1])\n"
    "done = image is not None and NSWorkspace.sharedWorkspace()"
    ".setIcon_forFile_options_(image, sys.argv[2], 0)\n"
    "sys.exit(0 if done else 3)\n"
)
# The same AppKit call through the system's own scripting bridge, for an install without pyobjc.
_SET_ICON_JXA = (
    'ObjC.import("AppKit");'
    "function run(argv){"
    "var image=$.NSImage.alloc.initWithContentsOfFile(argv[0]);"
    'if(image.isNil())return "noimage";'
    'return $.NSWorkspace.sharedWorkspace.setIconForFileOptions(image,argv[1],0)?"ok":"refused";}'
)

# A build that failed is not tried again by this process: an unsigned Chromium would otherwise
# cost a clone and a signature check on every headed launch.
_failed: dict[tuple, str] = {}


# One build at a time in this process. The file lock does the same job between processes.
_guard = threading.Lock()
_builder: Optional[threading.Thread] = None
# Why the last background build did not produce a copy, for server_status.
_last_note = ""

PENDING_NOTE = "the copy is still being prepared"


@dataclass(frozen=True)
class DockChrome:
    """`executable` is the copy's binary, or None to launch the installed Chrome.
    `note` says why, when the copy was wanted and could not be used. `pending` means a build
    is under way, so a later launch can expect the copy."""
    executable: Optional[str] = None
    note: str = ""
    pending: bool = False


@dataclass(frozen=True)
class _Plan:
    binary: Path
    source: Path
    source_real: Path
    root: Path
    version: tuple
    target: Path
    copy_binary: Path
    key: tuple


class _Unusable(Exception):
    """The copy cannot be made or used; the message is the note the caller reports."""


def enabled() -> bool:
    """Opt-in. It puts a second Chrome bundle on the person's disk and in LaunchServices, so
    it happens only when they ask: unset, or any other value, is off."""
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("1", "true", "yes", "on")


def app_dir() -> Path:
    override = os.environ.get(ENV_APP_DIR, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".mcp-stealth" / "app"


def prepare(chrome_binary: Optional[str], *, headless: bool, root: Optional[Path] = None) -> DockChrome:
    """The executable a headed launch should run. Looks, starts a build if one is needed, and
    answers at once: it never waits for a build or for the lock.

    Headless has no Dock icon and other systems have no Dock, so both answer with nothing to
    report, as does anyone who has not turned this on. Never raises.
    """
    if headless or sys.platform != "darwin" or not enabled() or not chrome_binary:
        return DockChrome()
    try:
        plan = _plan(Path(str(chrome_binary)), root or app_dir())
        if plan.key in _failed:
            return DockChrome(note=_failed[plan.key])
        state, detail = _peek(plan)
        if state == "ready":
            return DockChrome(executable=str(plan.copy_binary))
        if state == "building":
            return DockChrome(note=PENDING_NOTE, pending=True)
        if plan.target.exists() and _running_from(plan.target):
            return DockChrome(note=_in_use_note(detail))
        _start(lambda: _ensure_copy(plan))
        return DockChrome(note=PENDING_NOTE, pending=True)
    except _Unusable as why:
        return DockChrome(note=str(why))
    except Exception as exc:  # a launch must never fail over an icon
        return DockChrome(note=f"{type(exc).__name__}: {exc}")


def start_in_background(find_chrome: Callable[[], Optional[str]], root: Optional[Path] = None) -> bool:
    """Bring the copy up to date on a thread, so the first headed launch finds it ready.

    Called when the server starts. Finding Chrome and checking the copy both happen on that
    thread, so this costs the caller nothing. True when a thread was started.
    """
    if sys.platform != "darwin":
        return False
    if not enabled():
        # Turned off, or never on. A copy left from before would sit there for good, and after
        # a Chrome update it holds 1.5 GB of its own. No folder, the usual case, means no thread.
        if not (root or app_dir()).is_dir():
            return False
        return _start(lambda: discard_copy(root))

    def plan() -> _Plan:
        binary = find_chrome()
        if not binary:
            raise _Unusable("Chrome was not found")
        return _plan(Path(str(binary)), root or app_dir())

    return _start(lambda: _ensure_copy(plan()))


def discard_copy(root: Optional[Path] = None) -> str:
    """Remove the copy and what this module made beside it, when nothing runs from it.

    Says what it did. It only ever works in the configured app folder, never follows a link,
    and removes four things by name and nothing else: `Stealth Chrome.app`, its marker, the
    lock file and `.building-*` folders. Anything else found there is left, with the folder.
    """
    root = Path(os.path.abspath(root or app_dir()))
    if root != Path(os.path.abspath(app_dir())):
        return f"refused: {root} is not the configured app folder"
    home = Path(os.path.abspath(Path.home()))
    if root == home or root in home.parents or root.parent == root:
        return f"refused: {root} is not a folder of its own"
    if root.is_symlink() or not root.is_dir():
        return "nothing to remove"
    lock = root / ".lock"
    if lock.is_symlink() or not lock.is_file():
        # Every folder this module built in has its lock file. Without it the folder is
        # someone else's, whatever it happens to contain.
        return f"refused: {root} was not made by this server"
    with open(lock, "r") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "left alone: a server is building there"
        target = root / APP_NAME
        if target.is_symlink():
            return f"refused: {target} is a link"
        if target.is_dir():
            if _running_from(target):
                return "left alone: a browser is running from the copy"
            _unregister(target)
            shutil.rmtree(target)
        for leftover in _leftovers(root):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
        marker = root / MARKER_NAME
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()
        lock.unlink()
    with contextlib.suppress(OSError):
        root.rmdir()  # only if nothing else is in it
    return "removed"


def status(chrome_binary: Optional[str], root: Optional[Path] = None) -> dict:
    """Where the copy stands, for server_status. Reads only: no build, no wait, no folder made."""
    report = _status(chrome_binary, root)
    with contextlib.suppress(Exception):
        disk = _disk_use(Path(os.path.abspath(root or app_dir())) / APP_NAME)
        if disk is not None:
            report["disk"] = disk
    return report


def _status(chrome_binary: Optional[str], root: Optional[Path]) -> dict:
    try:
        if not enabled():
            return {"state": "off", "detail": f"set {ENV_SWITCH}=1 to turn it on (macOS)"}
        if sys.platform != "darwin":
            return {"state": "unavailable", "detail": "only on macOS"}
        if not chrome_binary:
            return {"state": "unavailable", "detail": "Chrome was not found"}
        plan = _plan(Path(str(chrome_binary)), root or app_dir())
        if plan.key in _failed:
            return {"state": "unavailable", "detail": _failed[plan.key]}
        state, detail = _peek(plan)
        if state == "ready":
            detail = f"{plan.target} (Chrome {plan.version[0]})"
        elif state == "stale" and _last_note:
            detail = f"{detail}; the last build did not finish: {_last_note}"
        return {"state": state, "detail": detail}
    except _Unusable as why:
        return {"state": "unavailable", "detail": str(why)}
    except Exception as exc:
        return {"state": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_ushort),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


def _disk_use(app: Path) -> Optional[dict]:
    """What the copy really takes, which `du` cannot say for a clone.

    `own_mb` is what removing it would free: APFS's private size, the blocks it shares with
    nothing. `shown_mb` is what `du` and Finder show. A fresh copy owns next to nothing; one
    left behind by a Chrome update owns all of it. About 30 ms for Chrome's 700 files. None
    when there is no copy or the sizes cannot be read.
    """
    if sys.platform != "darwin" or app.is_symlink() or not app.is_dir():
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    # ATTR_CMN_RETURNED_ATTRS, then ATTR_CMNEXT_PRIVATESIZE in the fork attributes.
    wanted = _AttrList(5, 0, 0x80000000, 0, 0, 0, 0x00000008)
    buffer = ctypes.create_string_buffer(64)
    own = shown = 0
    for base, _dirs, names in os.walk(app):
        for name in names:
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            # FSOPT_NOFOLLOW | FSOPT_ATTR_CMN_EXTENDED
            if libc.getattrlist(os.fsencode(path), ctypes.byref(wanted), buffer, 64, 0x1 | 0x20) != 0:
                return None
            if not struct.unpack_from("5I", buffer, 4)[4] & 0x00000008:
                return None
            own += struct.unpack_from("q", buffer, 24)[0]
            shown += os.lstat(path).st_blocks * 512
    return {"own_mb": round(own / 1e6, 1), "shown_mb": round(shown / 1e6, 1)}


def building() -> bool:
    """Whether this process has a build thread running."""
    return _builder is not None and _builder.is_alive()


def wait(timeout: Optional[float] = None) -> bool:
    """Wait for this process's build thread. For tests and tools; a launch never calls it."""
    thread = _builder
    if thread is not None:
        thread.join(timeout)
    return not building()


def _start(job: Callable[[], object]) -> bool:
    """Run a build, or a removal, on the one background thread. False when it is busy."""
    global _builder
    with _guard:
        if building():
            return False
        _builder = threading.Thread(target=_run, args=(job,), name="dock-icon-copy", daemon=True)
        _builder.start()
        return True


def _run(job: Callable[[], object]) -> None:
    """The background thread. Whatever happens stays here: the reason is kept for the next
    launch (`_failed`) or for server_status (`_last_note`)."""
    global _last_note
    try:
        job()
        _last_note = ""
    except _Unusable as why:
        _last_note = str(why)
    except BaseException as exc:
        _last_note = f"{type(exc).__name__}: {exc}"


def _plan(binary: Path, root: Path) -> _Plan:
    source = next((p for p in (binary, *binary.parents) if p.suffix == ".app"), None)
    if source is None or not binary.is_file():
        raise _Unusable(f"{binary} is not inside an app bundle")
    root = Path(os.path.abspath(root))
    source_real, root_real = source.resolve(), root.resolve()
    if root_real == source_real or source_real in root_real.parents or root_real in source_real.parents:
        # Either the copy would land inside Chrome, or Chrome is the copy itself.
        raise _Unusable(f"{root} and {source} overlap")
    version = _bundle_version(source)
    if version is None:
        raise _Unusable(f"could not read the version of {source}")
    target = root / APP_NAME
    return _Plan(binary=binary, source=source, source_real=source_real, root=root, version=version,
                 target=target, copy_binary=target / binary.relative_to(source),
                 key=(str(source_real), version, str(root_real), _icon_digest()))


def _stale(plan: _Plan) -> str:
    return _stale_reason(plan.target, plan.copy_binary, plan.root / MARKER_NAME,
                         plan.source_real, plan.version)


def _peek(plan: _Plan) -> tuple[str, str]:
    """("ready" | "building" | "stale", detail) without waiting, building or creating anything.

    A builder holds the lock exclusively for the whole build, here or in another server. A
    reader asks for it shared and without blocking, so readers never hold each other up and a
    refusal means exactly that someone is building.
    """
    if building():
        return "building", "this server is preparing the copy"
    try:
        handle = open(plan.root / ".lock", "r")
    except OSError:
        handle = None  # no folder or no lock file yet: nobody has ever built here
    try:
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return "building", "another server is preparing the copy"
        stale = _stale(plan)
        return ("stale", stale) if stale else ("ready", "")
    finally:
        if handle is not None:
            handle.close()


def _in_use_note(stale: str) -> str:
    return (f"the copy is out of date ({stale}) and a browser is still running from it; "
            "it is rebuilt on the first headed launch after that browser closes")


def _ensure_copy(plan: _Plan) -> Path:
    """Build or refresh the copy, waiting for the lock. Only ever runs on the build thread."""
    if plan.key in _failed:
        raise _Unusable(_failed[plan.key])
    if not _leftovers(plan.root) and not _stale(plan):
        # Nothing to do, so the lock is left alone: taking it, even briefly, makes a launch
        # in another server read "being prepared" and fall back to Chrome itself.
        return plan.copy_binary
    if not plan.root.exists():
        plan.root.mkdir(parents=True, mode=0o700)
    with _locked(plan.root):
        # Whoever builds holds the lock, so a build folder found here was abandoned: a server
        # that died mid-build. It can hold a whole previous copy, which after a Chrome update
        # shares no blocks with anything, so it goes even when the copy itself is fine.
        for leftover in _leftovers(plan.root):
            shutil.rmtree(leftover, ignore_errors=True)
        stale = _stale(plan)
        if not stale:
            return plan.copy_binary
        if plan.target.exists() and _running_from(plan.target):
            # Replacing the bundle under a running Chrome breaks it: new renderers start from
            # that path. Not remembered in _failed, because it clears when that browser closes.
            raise _Unusable(_in_use_note(stale))
        try:
            _build(plan.source, plan.target, plan.root, plan.version, plan.source_real,
                   plan.binary.relative_to(plan.source))
        except _Unusable as why:
            _failed[plan.key] = str(why)
            raise
    return plan.copy_binary


def _leftovers(root: Path) -> list[Path]:
    try:
        return list(root.glob(f"{_BUILD_PREFIX}*"))
    except OSError:
        return []


def forget_when_idle(executable: Optional[str]) -> None:
    """Take the copy out of LaunchServices once nothing runs from it. Call after a browser
    started from it has ended. Best effort, never raises; may take a moment, so off the loop.

    Running the copy registers it: from then on it is listed beside the person's Chrome for
    Chrome's bundle id, in Open With and as a handler for links. The bundle id still resolves
    to the installed Chrome, but there is no reason to stay listed. The next run registers it
    again, which is why this is repeated after every run and not done once.
    """
    with contextlib.suppress(Exception):
        if sys.platform != "darwin" or not executable:
            return
        binary = Path(str(executable))
        app = next((p for p in binary.parents if p.suffix == ".app"), None)
        if app is None or app.name != APP_NAME or not (app.parent / MARKER_NAME).is_file():
            return  # not a copy made here: the person's own apps are never unregistered
        if _running_from(app):
            return
        _unregister(app)
        _refresh_idle_copy(app, binary)


def _refresh_idle_copy(app: Path, binary: Path) -> None:
    """The last browser running from the copy has gone. If Chrome was updated meanwhile, the
    copy could not be replaced under it and now holds its 1.5 GB alone, so it is rebuilt now
    and not at whatever launch comes next. With the feature off it is simply removed."""
    if not enabled():
        _start(lambda: discard_copy(app.parent))
        return
    recorded = json.loads((app.parent / MARKER_NAME).read_text(encoding="utf-8"))
    plan = _plan(Path(recorded["source"]) / binary.relative_to(app), app.parent)
    if plan.key not in _failed and _stale(plan):
        _start(lambda: _ensure_copy(plan))


def _unregister(app: Path) -> None:
    with contextlib.suppress(Exception):
        subprocess.run([_LSREGISTER, "-u", str(app)], capture_output=True, timeout=20)


def _exclude_from_backups(app: Path) -> None:
    """Mark the copy as not worth backing up, the way `tmutil addexclusion` does.

    It can be rebuilt in seconds, and a backup does not know it is a clone. The mark is an
    extended attribute that stays with the bundle; tmutil writes the same one, but takes
    11 seconds over it. Best effort: an unmarked copy is still a good copy.
    """
    with contextlib.suppress(Exception):
        value = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY).hex()
        subprocess.run(["/usr/bin/xattr", "-wx", _BACKUP_EXCLUDE, value, str(app)],
                       capture_output=True, timeout=10)


def _bundle_version(app: Path) -> Optional[tuple]:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
        short = info["CFBundleShortVersionString"]
    except Exception:
        return None
    return (str(short), str(info.get("CFBundleVersion", "")))


@functools.lru_cache(maxsize=1)
def _icon_digest() -> str:
    try:
        return hashlib.sha256(ICON_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def _has_icon(app: Path) -> bool:
    return _resource_fork_size(app / ICON_FILE) > 0


def _resource_fork_size(path: Path) -> int:
    """A custom icon lives in the resource fork of `Icon\\r`; the file's data fork is empty."""
    try:
        return os.stat(str(path) + "/..namedfork/rsrc").st_size
    except OSError:
        return 0


def _stale_reason(target: Path, copy_binary: Path, marker: Path, source: Path, version: tuple) -> str:
    """Why the copy has to be rebuilt, or "" when it can be used as it is."""
    if not copy_binary.is_file():
        return "there is no copy yet"
    have = _bundle_version(target)
    if have != version:
        return f"it is Chrome {have[0] if have else 'of an unknown version'}, installed is {version[0]}"
    if not _has_icon(target):
        return "its icon is missing"
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "it was never finished"
    # The marker is written last, after the signature check passed, so its presence is what
    # says the build completed. It also catches a new icon and a different source browser.
    if not isinstance(recorded, dict) or recorded.get("verified") is not True:
        return "it was never finished"
    if recorded.get("source") != str(source):
        return "it was made from another browser"
    if recorded.get("icon") != _icon_digest():
        return "the icon changed"
    return ""


def _running_from(app: Path) -> bool:
    # Both spellings: /var is a link to /private/var, and ps shows the one used at launch.
    prefixes = tuple({str(app) + "/", os.path.realpath(app) + "/"})
    try:
        listing = subprocess.run(["ps", "-ax", "-ww", "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True  # unknown is not "nobody": leave the bundle alone
    return any(line.strip().startswith(prefixes) for line in listing.splitlines())


@contextlib.contextmanager
def _locked(root: Path):
    """One builder at a time across server processes."""
    handle = open(root / ".lock", "a+")
    try:
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _Unusable("another process has been preparing the copy for too long")
                time.sleep(0.2)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _build(source: Path, target: Path, root: Path, version: tuple, source_real: Path,
           relative: Path) -> None:
    """Clone, set the icon, check the signature, run it once, and only then move it into place."""
    if not ICON_PATH.is_file():
        raise _Unusable("the icon file is missing from this install")
    work = root / f"{_BUILD_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    try:
        _require_clone_support(source / "Contents" / "Info.plist", work / "probe")
        fresh = work / APP_NAME
        _clone(source, fresh)
        if _bundle_version(fresh) != version:
            raise _Unusable("Chrome changed while it was being copied")
        _set_icon(fresh, ICON_PATH)
        if not _has_icon(fresh):
            raise _Unusable("the icon did not take on the copy")
        _exclude_from_backups(fresh)
        _verify_signature(fresh)
        _warm_up(fresh / relative)
        # Running it registered this build folder with LaunchServices, a path about to vanish.
        _unregister(fresh)
        marker = root / MARKER_NAME
        with contextlib.suppress(FileNotFoundError):
            marker.unlink()
        if target.exists() or target.is_symlink():
            os.rename(target, work / "previous")
        os.rename(fresh, target)
        pending = work / MARKER_NAME
        pending.write_text(json.dumps({
            "source": str(source_real), "version": list(version),
            "icon": _icon_digest(), "verified": True,
        }), encoding="utf-8")
        os.replace(pending, marker)
    except _Unusable:
        raise
    except Exception as exc:
        raise _Unusable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _require_clone_support(sample: Path, probe: Path) -> None:
    """`cp -c` quietly falls back to a full copy where it cannot clone, which here would be
    most of a gigabyte. So one small file is cloned first, with the call that cannot fall back."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.clonefile(os.fsencode(sample), os.fsencode(probe), 0)
    except Exception as exc:
        raise _Unusable(f"cloning is not available here ({type(exc).__name__})") from exc
    if result != 0:
        code = ctypes.get_errno()
        raise _Unusable(
            f"{probe.parent.parent} cannot hold a clone of Chrome "
            f"({errno.errorcode.get(code, code)}: it has to be on the same APFS volume)")


def _clone(source: Path, destination: Path) -> None:
    try:
        done = subprocess.run(["/bin/cp", "-c", "-R", str(source), str(destination)],
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:
        raise _Unusable(f"cp -c failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise _Unusable(f"cp -c failed: {(done.stderr or '').strip()[:200] or done.returncode}")


def _set_icon(app: Path, icon: Path) -> None:
    """NSWorkspace.setIcon, in another process so AppKit stays out of the server."""
    tried = []
    try:
        done = subprocess.run([sys.executable, "-c", _SET_ICON_PY, str(icon), str(app)],
                              capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            return
        tried.append("AppKit is not installed" if "No module named" in (done.stderr or "")
                     else f"AppKit refused (exit {done.returncode})")
    except Exception as exc:
        tried.append(f"python: {type(exc).__name__}")
    try:
        done = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _SET_ICON_JXA,
                               str(icon), str(app)], capture_output=True, text=True, timeout=60)
        if done.returncode == 0 and done.stdout.strip() == "ok":
            return
        tried.append(f"osascript answered {(done.stdout or done.stderr or '').strip()[:120]!r}")
    except Exception as exc:
        tried.append(f"osascript: {type(exc).__name__}")
    raise _Unusable("the icon could not be set: " + "; ".join(tried))


def _warm_up(binary: Path) -> None:
    """Run the new copy once, so its slow first start is paid here and not by the launch.

    macOS takes about 11 seconds over the first start of a freshly cloned Chrome, and under a
    second after that. Left to the launch, that sits at the edge of what `_start_chrome` waits
    for. `--version` is enough, and it carries over the rename into place. Best effort: a copy
    that was not warmed is still a good copy, only slow once.
    """
    with contextlib.suppress(Exception):
        subprocess.run([str(binary), "--version"], capture_output=True, timeout=_WARM_UP_TIMEOUT)


def _verify_signature(app: Path) -> None:
    """A copy whose signature no longer verifies is never used: Keychain access hangs on it."""
    try:
        done = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", str(app)],
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:
        raise _Unusable(f"codesign could not run: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise _Unusable(
            "the copy's signature did not verify: "
            + ((done.stderr or '').strip().splitlines() or ["no detail"])[-1][:200])
