"""A headed stealth browser with its own Dock icon, so it is not mistaken for the person's Chrome.

Opt-in, with MCP_STEALTH_DOCK_ICON=1, on macOS. The Dock takes its icon from the app bundle a
process runs out of, and the person's own browser is Google Chrome. So a headed launch runs
from Google Chrome Beta instead, `/Applications/Google Chrome Beta.app`, which carries the
project icon as a custom Finder icon: an `Icon\\r` file and a FinderInfo flag at the bundle
root. `Contents/` stays byte for byte what Google signed, so `codesign --verify --deep` passes
and the Keychain still lets it read "Chrome Safe Storage" (that item trusts every Chrome
channel). Beta has its own bundle id and its own entry with Google's updater, which is why it
replaces the Chrome copy withdrawn in 0.13.1: running that copy re-registered Chrome's own
updater entry at the copy.

An update of Beta replaces the bundle and takes the icon with it. `prepare` notices that with
one stat call and puts the icon back on a background thread; a launch never waits for it.
`/Applications/Google Chrome.app` is never written to. Headless has no Dock icon and keeps
Google Chrome.

What is left of the copy is its removal: a server that starts deletes an old
`~/.mcp-stealth/app/Stealth Chrome.app` once nothing runs from it (`discard_copy`).
"""
from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # Windows. Importing it unguarded stopped the server from starting there.
    fcntl = None

ENV_SWITCH = "MCP_STEALTH_DOCK_ICON"
ENV_APP_DIR = "MCP_STEALTH_APP_DIR"
# Where Google installs it. The person's own Chrome, which this module never writes to.
BETA_APP = Path("/Applications/Google Chrome Beta.app")
BETA_BINARY = Path("Contents/MacOS/Google Chrome Beta")
CHROME_APP = Path("/Applications/Google Chrome.app")
# The person's own browsers, never written to whatever a path resolves to, and the only bundle
# names the icon may go on: the Beta channels, each with its own bundle id and updater entry.
BRAVE_APP = Path("/Applications/Brave Browser.app")
ICON_TARGETS = ("Google Chrome Beta.app", "Brave Browser Beta.app")
# The Chrome copy made by 0.13.0, only ever removed now.
APP_NAME = "Stealth Chrome.app"
MARKER_NAME = "Stealth Chrome.json"
ICON_PATH = Path(__file__).parent / "assets" / "dock_icon.png"
ICON_FILE = "Icon\r"
# Which icon was put on, kept on the `Icon\r` file itself: outside Contents/, gone with it.
ICON_MARK = "org.mcp-stealth.icon"
_BUILD_PREFIX = ".building-"
_ICON_TIMEOUT = 60.0
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

# One background job at a time in this process: putting the icon on, or removing the old copy.
_guard = threading.Lock()
_worker: Optional[threading.Thread] = None
# Why the last background job did not finish, for server_status and the next launch.
_last_note = ""
# An icon that could not be put on is not tried again for the same Beta and icon: a Beta the
# person cannot write to would otherwise cost a subprocess on every headed launch.
_icon_failed: dict[tuple, str] = {}

# False in a process the Dock launcher app started: macOS App Management blocks an app from
# modifying another app's bundle (it showed "Stealth Brave was prevented from modifying apps on
# your Mac"), so the launcher never tries; a server or the command line puts the icon back on.
ICON_WRITES = True
NO_WRITES_NOTE = ("the project icon is not on it now (an update removes it); the launcher does not "
                  "modify other apps, so the next launch by a server or `mcp-stealth-chrome open` "
                  "puts it back on")

PENDING_NOTE = ("the project icon is being put back on Chrome Beta (an update removes it), "
                "so this window may show Beta's own icon")


@dataclass(frozen=True)
class DockChrome:
    """`executable` is Chrome Beta's binary, or None to launch Google Chrome as usual.
    `note` is what the launch result should say: why Beta was not used, or that its icon is
    on its way."""
    executable: Optional[str] = None
    note: str = ""
    version: str = ""


def asked_for() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Opt-in: unset, or any value but 1/true/yes/on, is off, and off changes nothing."""
    return asked_for()


def app_dir() -> Path:
    override = os.environ.get(ENV_APP_DIR, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".mcp-stealth" / "app"


def beta_binary() -> Optional[Path]:
    binary = BETA_APP / BETA_BINARY
    return binary if binary.is_file() and os.access(binary, os.X_OK) else None


def prepare(chrome_binary: Optional[str] = None, *, headless: bool,
            profile_refusal: str = "") -> DockChrome:
    """The executable a headed launch should run, answered at once. Never raises, never waits.

    Headless has no Dock icon and other systems have no Dock, so both get nothing, as does
    anyone who has not turned this on. Beta missing means Google Chrome with a note, and so
    does `profile_refusal`: why this launch's profile must not be opened by Beta (see
    profile_version.beta_verdict). The icon check is one stat call; a missing icon is put back
    on a thread while Beta starts anyway.
    """
    if headless or sys.platform != "darwin" or not enabled():
        return DockChrome()
    if profile_refusal:
        return DockChrome(note=f"Google Chrome, because {profile_refusal}")
    try:
        binary = chosen(headless=headless)
        if binary is None:
            return DockChrome(note=missing_note())
        version = (_bundle_version(BETA_APP) or ("",))[0]
        if _icon_current(BETA_APP):
            return DockChrome(executable=binary, version=version)
        if not ICON_WRITES:  # before any write, the dangling flag's clearing included
            return DockChrome(executable=binary, version=version, note=NO_WRITES_NOTE)
        # The custom-icon flag without its `Icon\r` shows a folder in the Dock, so this launch
        # gets Beta's own icon instead while the project's goes back on.
        _clear_dangling_flag(BETA_APP)
        key = _icon_key(BETA_APP)
        if key in _icon_failed:
            return DockChrome(executable=binary, version=version,
                              note=f"Chrome Beta shows its own icon: {_icon_failed[key]}")
        _start(lambda: put_icon_on(BETA_APP))
        return DockChrome(executable=binary, version=version, note=PENDING_NOTE)
    except Exception as exc:  # a launch must never fail over an icon
        return DockChrome(note=f"{type(exc).__name__}: {exc}")


def chosen(*, headless: bool) -> Optional[str]:
    """Chrome Beta's binary when this launch would run from it, else None. Reads only."""
    if headless or sys.platform != "darwin" or not enabled():
        return None
    binary = beta_binary()
    return str(binary) if binary is not None else None


def missing_note() -> str:
    return f"Google Chrome Beta is not installed in {BETA_APP.parent}, so this is Google Chrome"


def start_in_background(root: Optional[Path] = None) -> bool:
    """Called when the server starts, on a thread so it costs the caller nothing: removes an
    old Chrome copy when nothing runs from it, and puts the icon on Beta when that is turned
    on and an update took it off. True when a thread was started."""
    if sys.platform != "darwin":
        return False
    old_copy = (root or app_dir()).is_dir()
    wants_icon = enabled() and beta_binary() is not None and not _icon_current(BETA_APP)
    from . import brave_beta  # here: it imports this module
    wants_brave = (enabled() and brave_beta.beta_binary() is not None
                   and not _icon_current(brave_beta.BETA_APP))
    if not old_copy and not wants_icon and not wants_brave:
        return False

    def job() -> None:
        notes = []
        if old_copy:
            outcome = discard_copy(root)
            if outcome != "removed" and outcome != "nothing to remove":
                notes.append(f"old Chrome copy {outcome}")
        for wanted, app in ((wants_icon, BETA_APP), (wants_brave, brave_beta.BETA_APP)):
            if wanted:
                try:
                    put_icon_on(app)
                except _Note as why:
                    notes.append(f"{app.name}: {why}")
        if notes:
            raise _Note("; ".join(notes))

    return _start(job)


class _Note(Exception):
    """A background job that finished without doing all it set out to; the message is kept."""


def put_icon_on(app: Path) -> None:
    """Set the project icon as the custom Finder icon of `app`. Only ever Chrome Beta or
    Brave Beta (brave_beta.py), never a bundle that resolves to the person's Chrome or Brave.

    NSWorkspace writes `Icon\\r` and the FinderInfo flag at the bundle root and nothing under
    `Contents/`. Which icon went on is recorded on `Icon\\r`, so a new project icon is noticed.
    A failure is remembered for this Beta and icon, and raised for the thread to keep.
    """
    target = Path(os.path.abspath(app))
    if (target.name not in ICON_TARGETS
            or any(os.path.realpath(target) == os.path.realpath(mine) for mine in (CHROME_APP, BRAVE_APP))):
        raise _Note(f"refused: the icon only goes on {' or '.join(sorted(ICON_TARGETS))}, "
                    f"not {target}")
    key = _icon_key(target)
    try:
        if not ICON_PATH.is_file():
            raise _Note("the icon file is missing from this install")
        _set_icon(target, ICON_PATH)
        if not _has_icon(target):
            raise _Note("the icon did not take")
        _write_mark(target / ICON_FILE, _icon_digest())
    except _Note as why:
        _icon_failed[key] = str(why)
        raise
    except Exception as exc:
        _icon_failed[key] = f"{type(exc).__name__}: {exc}"
        raise _Note(_icon_failed[key]) from exc


def status(root: Optional[Path] = None) -> dict:
    """Where the Dock icon stands, for server_status. Reads only: nothing is started or made.

    state: off, unavailable (not macOS), beta-missing, beta-ready, or icon-missing. `detail`
    says what that means here; `old_copy` appears while a Chrome copy from 0.13 is still on disk.
    """
    try:
        report = _status()
    except Exception as exc:
        report = {"state": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}
    with contextlib.suppress(Exception):
        copy = Path(os.path.abspath(root or app_dir())) / APP_NAME
        if copy.is_dir() and not copy.is_symlink():
            report["old_copy"] = (f"{copy} is still there: removed when a server starts and "
                                  "no browser runs from it")
    if _last_note:
        report["last_note"] = _last_note
    return report


def _status() -> dict:
    if sys.platform != "darwin":
        return {"state": "unavailable", "detail": "only on macOS"}
    binary = beta_binary()
    version = (_bundle_version(BETA_APP) or ("",))[0] if binary else ""
    if not enabled():
        if binary is None:
            return {"state": "off", "detail": (
                f"off. {ENV_SWITCH}=1 runs visible browsers from Google Chrome Beta with the "
                f"project icon; Beta is not installed in {BETA_APP.parent}, so it would have "
                "no effect here")}
        return {"state": "off", "detail": (
            f"off. {ENV_SWITCH}=1 runs visible browsers from Google Chrome Beta {version} "
            "with the project icon")}
    if binary is None:
        return {"state": "beta-missing", "detail": missing_note()}
    if _icon_current(BETA_APP):
        return {"state": "beta-ready",
                "detail": f"visible browsers run from {BETA_APP} ({version}) with the project icon"}
    failed = _icon_failed.get(_icon_key(BETA_APP))
    detail = (f"Chrome Beta {version} is used with its own icon: {failed}" if failed else
              f"Chrome Beta {version} has no project icon yet; it is put on at the next "
              "visible launch")
    return {"state": "icon-missing", "detail": detail}


def building() -> bool:
    """Whether this process has a background job running."""
    return _worker is not None and _worker.is_alive()


def wait(timeout: Optional[float] = None) -> bool:
    """Wait for this process's background job. For tests and tools; a launch never calls it."""
    thread = _worker
    if thread is not None:
        thread.join(timeout)
    return not building()


def _start(job: Callable[[], object]) -> bool:
    global _worker
    with _guard:
        if building():
            return False
        _worker = threading.Thread(target=_run, args=(job,), name="dock-icon", daemon=True)
        _worker.start()
        return True


def _run(job: Callable[[], object]) -> None:
    """The background thread. Whatever happens stays here, kept in `_last_note`."""
    global _last_note
    try:
        job()
        _last_note = ""
    except _Note as why:
        _last_note = str(why)
    except BaseException as exc:
        _last_note = f"{type(exc).__name__}: {exc}"


def _icon_current(app: Path) -> bool:
    """The project's icon is on `app`: about 0.1 ms, cheap enough for every launch."""
    if not _has_icon(app):
        return False
    return _read_mark(app / ICON_FILE) == _icon_digest()


_XATTR_NOFOLLOW = 0x0001


@functools.lru_cache(maxsize=1)
def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.setxattr.restype = ctypes.c_int
    return libc


def _read_mark(path: Path) -> str:
    """The icon digest recorded on `Icon\\r`, or "". Python's os.getxattr is Linux only."""
    try:
        buffer = ctypes.create_string_buffer(128)
        size = _libc().getxattr(os.fsencode(path), ICON_MARK.encode(), buffer, 128, 0,
                                _XATTR_NOFOLLOW)
    except Exception:
        return ""
    return buffer.raw[:size].decode(errors="replace") if size > 0 else ""


_FINDER_INFO = b"com.apple.FinderInfo"
_HAS_CUSTOM_ICON = 0x04  # kHasCustomIcon (0x0400) in the big-endian Finder flags at byte 8


def _clear_dangling_flag(app: Path) -> bool:
    """Clear the custom-icon flag on `app` when its `Icon\\r` is gone. Two system calls, no
    subprocess, so it may run on the launch path. True when it cleared something."""
    if _has_icon(app):
        return False
    try:
        buffer = ctypes.create_string_buffer(32)
        path = os.fsencode(app)
        if _libc().getxattr(path, _FINDER_INFO, buffer, 32, 0, _XATTR_NOFOLLOW) != 32:
            return False
        info = bytearray(buffer.raw)
        if not info[8] & _HAS_CUSTOM_ICON:
            return False
        info[8] &= ~_HAS_CUSTOM_ICON & 0xFF
        data = bytes(info)
        return _libc().setxattr(path, _FINDER_INFO, data, 32, 0, _XATTR_NOFOLLOW) == 0
    except Exception:
        return False


def _write_mark(path: Path, digest: str) -> None:
    data = digest.encode()
    if _libc().setxattr(os.fsencode(path), ICON_MARK.encode(), data, len(data), 0,
                        _XATTR_NOFOLLOW) != 0:
        code = ctypes.get_errno()
        raise _Note(f"the icon is on, but recording it failed: {os.strerror(code)}")


def _icon_key(app: Path) -> tuple:
    try:
        stamp = os.stat(app / "Contents" / "Info.plist").st_mtime_ns
    except OSError:
        stamp = 0
    return (str(app), stamp, _icon_digest())


def _has_icon(app: Path) -> bool:
    return _resource_fork_size(app / ICON_FILE) > 0


def _resource_fork_size(path: Path) -> int:
    """A custom icon lives in the resource fork of `Icon\\r`; the file's data fork is empty."""
    try:
        return os.stat(str(path) + "/..namedfork/rsrc").st_size
    except OSError:
        return 0


@functools.lru_cache(maxsize=1)
def _icon_digest() -> str:
    try:
        return hashlib.sha256(ICON_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def _bundle_version(app: Path) -> Optional[tuple]:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
        short = info["CFBundleShortVersionString"]
    except Exception:
        return None
    return (str(short), str(info.get("CFBundleVersion", "")))


def _set_icon(app: Path, icon: Path) -> None:
    """NSWorkspace.setIcon, in another process so AppKit stays out of the server. About 0.3 s."""
    tried = []
    try:
        done = subprocess.run([sys.executable, "-c", _SET_ICON_PY, str(icon), str(app)],
                              capture_output=True, text=True, timeout=_ICON_TIMEOUT)
        if done.returncode == 0:
            return
        tried.append("AppKit is not installed" if "No module named" in (done.stderr or "")
                     else f"AppKit refused (exit {done.returncode})")
    except Exception as exc:
        tried.append(f"python: {type(exc).__name__}")
    try:
        done = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _SET_ICON_JXA,
                               str(icon), str(app)], capture_output=True, text=True,
                              timeout=_ICON_TIMEOUT)
        if done.returncode == 0 and done.stdout.strip() == "ok":
            return
        tried.append(f"osascript answered {(done.stdout or done.stderr or '').strip()[:120]!r}")
    except Exception as exc:
        tried.append(f"osascript: {type(exc).__name__}")
    raise _Note("the icon could not be set: " + "; ".join(tried))


# ── the Chrome copy from 0.13.0: removal only ────────────────────────────────

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
    if fcntl is None:
        # The 0.13.0 copy was only ever built on macOS, and without a lock this cannot tell
        # whether another server is building there.
        return "left alone: no file lock on this platform"
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


def _leftovers(root: Path) -> list[Path]:
    try:
        return list(root.glob(f"{_BUILD_PREFIX}*"))
    except OSError:
        return []


def _unregister(app: Path) -> None:
    """Take the copy out of LaunchServices before it is deleted, so it is not left listed
    for Chrome's bundle id."""
    with contextlib.suppress(Exception):
        subprocess.run([_LSREGISTER, "-u", str(app)], capture_output=True, timeout=20)


def _running_from(app: Path) -> bool:
    # Both spellings: /var is a link to /private/var, and ps shows the one used at launch.
    prefixes = tuple({str(app) + "/", os.path.realpath(app) + "/"})
    try:
        listing = subprocess.run(["ps", "-ax", "-ww", "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True  # unknown is not "nobody": leave the bundle alone
    return any(line.strip().startswith(prefixes) for line in listing.splitlines())
