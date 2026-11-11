"""A headed stealth Chrome with its own Dock icon, so it is not mistaken for the person's Chrome.

macOS takes the Dock icon from the app bundle a process runs out of. So a headed launch runs
from a copy of the installed Chrome, `~/.mcp-stealth/app/Stealth Chrome.app`, that carries the
project icon. The copy is an APFS clone: made in well under a second, sharing its blocks with
the original. The icon goes on as a custom file icon (an `Icon\\r` file and a FinderInfo flag
at the bundle root), which leaves `Contents/` byte for byte what Google signed. Info.plist is
never edited: that would break the signature, and with it the Keychain access that decrypts
cookies. The Dock tooltip therefore still says "Google Chrome".

Nothing here may stop a launch. Whatever goes wrong, `prepare` answers with a note and the
caller starts the installed Chrome. The installed Chrome is only ever read.
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
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENV_SWITCH = "MCP_STEALTH_DOCK_ICON"
ENV_APP_DIR = "MCP_STEALTH_APP_DIR"
APP_NAME = "Stealth Chrome.app"
MARKER_NAME = "Stealth Chrome.json"
ICON_PATH = Path(__file__).parent / "assets" / "dock_icon.png"
ICON_FILE = "Icon\r"
_BUILD_PREFIX = ".building-"
_LOCK_WAIT = 90.0
_WARM_UP_TIMEOUT = 60.0

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


@dataclass(frozen=True)
class DockChrome:
    """`executable` is the copy's binary, or None to launch the installed Chrome.
    `note` says why, when the copy was wanted and could not be used."""
    executable: Optional[str] = None
    note: str = ""


class _Unusable(Exception):
    """The copy cannot be made or used; the message is the note the caller reports."""


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in ("0", "false", "no", "off")


def app_dir() -> Path:
    override = os.environ.get(ENV_APP_DIR, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".mcp-stealth" / "app"


def prepare(chrome_binary: Optional[str], *, headless: bool, root: Optional[Path] = None) -> DockChrome:
    """The executable a headed launch should run, building or refreshing the copy if needed.

    Headless has no Dock icon and other systems have no Dock, so both answer with nothing to
    report, as does a person who turned this off. Never raises.
    """
    if headless or sys.platform != "darwin" or not enabled() or not chrome_binary:
        return DockChrome()
    try:
        return DockChrome(executable=str(_ensure_copy(Path(str(chrome_binary)), root or app_dir())))
    except _Unusable as why:
        return DockChrome(note=str(why))
    except Exception as exc:  # a launch must never fail over an icon
        return DockChrome(note=f"{type(exc).__name__}: {exc}")


def _ensure_copy(binary: Path, root: Path) -> Path:
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
    copy_binary = target / binary.relative_to(source)
    key = (str(source_real), version, str(root_real), _icon_digest())
    if key in _failed:
        raise _Unusable(_failed[key])

    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    with _locked(root):
        stale = _stale_reason(target, copy_binary, root / MARKER_NAME, source_real, version)
        if not stale:
            return copy_binary
        # Whoever builds holds the lock, so a build folder found here was abandoned.
        for leftover in root.glob(f"{_BUILD_PREFIX}*"):
            shutil.rmtree(leftover, ignore_errors=True)
        if target.exists() and _running_from(target):
            # Replacing the bundle under a running Chrome breaks it: new renderers start from
            # that path. Not remembered in _failed, because it clears when that browser closes.
            raise _Unusable(
                f"the copy is out of date ({stale}) and a browser is still running from it; "
                "it is rebuilt on the first headed launch after that browser closes")
        try:
            _build(source, target, root, version, source_real, binary.relative_to(source))
        except _Unusable as why:
            _failed[key] = str(why)
            raise
    return copy_binary


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
        _verify_signature(fresh)
        _warm_up(fresh / relative)
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
