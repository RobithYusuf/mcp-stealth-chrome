"""A visible Brave with the project icon: Brave Beta, as Chrome Beta is for Chrome.

Opt-in with the same switch, MCP_STEALTH_DOCK_ICON=1, on macOS, for a headed launch with
browser="brave". The person's own Brave is `/Applications/Brave Browser.app`; the agent's runs
from `/Applications/Brave Browser Beta.app`, which carries the project icon as a custom Finder
icon (`Icon\\r` and the FinderInfo flag at the bundle root, dock_icon.put_icon_on). Nothing
under `Contents/` is written, so `codesign --verify --deep` still passes. Brave Beta has its own
bundle id (com.brave.Browser.beta) and its own Sparkle settings domain, which is where Brave on
macOS keeps what its updater knows (`SULastCheckTime`, `LastRunAppBundlePath`): running Beta
never touches com.brave.Browser.

The profile rule is Chrome Beta's (profile_version.beta_verdict): Beta, a major ahead, only
opens a profile it cannot damage. Brave 158 drops the `v10` cookies Brave 157 writes, exactly
as Chrome 158 does, and a profile Beta opened is refused to the older Brave afterwards. The one
exception is asked for by the person: the launcher's `--move-to-beta` (ALLOW_PROFILE_MOVE) lets
Beta take a profile Brave made, through the cookie move that keeps every cookie.

Never fails a launch: Beta missing, an icon that cannot be put on, or a profile Beta must not
open all mean a note in the launch result, and Brave itself or Beta with its own icon.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from . import brave as brave_mode
from . import dock_icon, profile_version

BETA_APP = Path("/Applications/Brave Browser Beta.app")
BETA_BINARY = Path("Contents/MacOS/Brave Browser Beta")
BRAVE_APP = dock_icon.BRAVE_APP
NAMES = ("Brave", "Brave Beta")

# Set only by the launcher's --move-to-beta, in its own one-shot process: never reachable from
# a tool call. Lets Brave Beta take a profile Brave made (the cookie move keeps its cookies).
ALLOW_PROFILE_MOVE = False

# Measured: "Brave Safe Storage" in the login Keychain lists only Brave Browser.app, so the first
# Brave Beta start that needs it asks the person (SecurityAgent). Unanswered, Beta waits; denied, it
# cannot read the profile's cookies and drops them.
KEYCHAIN_NOTE = ("the first time, macOS may ask whether Brave Browser Beta may use 'Brave Safe "
                 "Storage' in your Keychain: Always Allow keeps its logins, Deny makes it drop the "
                 "cookies it cannot read")

PENDING_NOTE = ("the project icon is being put back on Brave Beta (an update removes it), so "
                "this window may show Beta's own icon")


def beta_binary() -> Optional[Path]:
    binary = BETA_APP / BETA_BINARY
    return binary if binary.is_file() and os.access(binary, os.X_OK) else None


def wanted(*, headless: bool) -> bool:
    """Whether this launch would run from Brave Beta if it were installed."""
    return not headless and sys.platform == "darwin" and dock_icon.enabled()


def chosen(*, headless: bool) -> Optional[str]:
    """Brave Beta's binary when a headed Brave launch should run from it, else None. Reads only."""
    if not wanted(headless=headless):
        return None
    binary = beta_binary()
    return str(binary) if binary is not None else None


def is_beta_executable(path: Optional[str]) -> bool:
    return bool(path) and str(path).startswith(str(BETA_APP) + "/")


def missing_note() -> str:
    return f"Brave Browser Beta is not installed in {BETA_APP.parent}, so this is Brave"


def verdict(profile: Optional[Path], *, brave: Optional[str], beta: str,
            extra_args=()) -> tuple[bool, str]:
    """(Brave Beta may open this profile, note). Chrome Beta's rule, plus the person's own
    --move-to-beta for a profile Brave made."""
    allowed, note = profile_version.beta_verdict(profile, chrome=brave, beta=beta,
                                                 extra_args=extra_args, names=NAMES)
    if allowed or not ALLOW_PROFILE_MOVE or profile is None:
        return allowed, note
    if any((arg or "").startswith("--user-data-dir=") for arg in (extra_args or ())):
        return allowed, note
    # The move is taken only on positive evidence on every point; anything unknown stays refused.
    folder = Path(profile)
    try:
        real_folder = folder.is_dir() and not folder.is_symlink()
    except OSError:
        real_folder = False
    if not real_folder:
        return False, f"{note}; --move-to-beta was not applied: {folder} is not a folder of its own"
    if brave_mode.profile_browser(folder) != brave_mode.BRAVE:
        return False, f"{note}; --move-to-beta was not applied: Brave is not known to have made it"
    recorded = profile_version.last_opened_by(folder)
    running = profile_version.binary_version(beta)
    if recorded is None or running is None or recorded > running:
        return False, (f"{note}; --move-to-beta was not applied: the version that last opened it "
                       f"and Brave Beta's own must both be known, and Beta not older")
    return True, ("moved to Brave Beta because the launcher was asked to (--move-to-beta): its "
                  "v10 cookies go through the cookie move, and Brave older than Beta is refused "
                  "this profile from now on")


def choose(profile: Optional[Path], *, headless: bool, brave: str,
           extra_args=()) -> tuple[str, bool, str]:
    """(executable, runs from Beta, note) for a Brave launch, before anything writes to the
    profile. Off, headless or not macOS: Brave and no note."""
    if not wanted(headless=headless):
        return brave, False, ""
    beta = chosen(headless=headless)
    if beta is None:
        return brave, False, missing_note()
    try:
        allowed, note = verdict(profile, brave=brave, beta=beta, extra_args=extra_args)
    except Exception as exc:
        allowed, note = False, f"the profile could not be checked ({type(exc).__name__}: {exc})"
    return (beta if allowed else brave), allowed, note


def prepare(*, headless: bool) -> dock_icon.DockChrome:
    """Brave Beta for this headed launch, answered at once: the icon check is a stat and an xattr
    read, and a missing icon goes back on in a background thread. Never raises."""
    try:
        binary = chosen(headless=headless)
        if binary is None:
            return dock_icon.DockChrome(note=missing_note() if wanted(headless=headless) else "")
        version = (dock_icon._bundle_version(BETA_APP) or ("",))[0]
        if dock_icon._icon_current(BETA_APP):
            return dock_icon.DockChrome(executable=binary, version=version)
        if not dock_icon.ICON_WRITES:  # before any write, the dangling flag's clearing included
            return dock_icon.DockChrome(executable=binary, version=version,
                                        note=dock_icon.NO_WRITES_NOTE)
        dock_icon._clear_dangling_flag(BETA_APP)
        key = dock_icon._icon_key(BETA_APP)
        if key in dock_icon._icon_failed:
            return dock_icon.DockChrome(executable=binary, version=version,
                                        note=f"Brave Beta shows its own icon: {dock_icon._icon_failed[key]}")
        dock_icon._start(lambda: dock_icon.put_icon_on(BETA_APP))
        return dock_icon.DockChrome(executable=binary, version=version, note=PENDING_NOTE)
    except Exception as exc:
        return dock_icon.DockChrome(note=f"{type(exc).__name__}: {exc}")


def status() -> dict:
    """For server_status: off, unavailable, beta-missing, beta-ready or icon-missing. Reads only."""
    try:
        if sys.platform != "darwin":
            return {"state": "unavailable", "detail": "only on macOS"}
        binary = beta_binary()
        version = (dock_icon._bundle_version(BETA_APP) or ("",))[0] if binary else ""
        if not dock_icon.enabled():
            return {"state": "off", "detail": (
                f"off. {dock_icon.ENV_SWITCH}=1 runs visible Brave from Brave Beta"
                + (f" {version} with the project icon" if binary else
                   f"; Brave Beta is not installed in {BETA_APP.parent}"))}
        if binary is None:
            return {"state": "beta-missing", "detail": missing_note()}
        if dock_icon._icon_current(BETA_APP):
            return {"state": "beta-ready",
                    "detail": f"visible Brave runs from {BETA_APP} ({version}) with the project icon"}
        failed = dock_icon._icon_failed.get(dock_icon._icon_key(BETA_APP))
        return {"state": "icon-missing", "detail": (
            f"Brave Beta {version} is used with its own icon: {failed}" if failed else
            f"Brave Beta {version} has no project icon yet; it is put on at the next visible launch")}
    except Exception as exc:
        return {"state": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}
