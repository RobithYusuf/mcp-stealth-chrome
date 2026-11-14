"""Brave as the browser of a launch: browser_launch(browser="brave"), spawn_browser(browser=...).

Brave driven over CDP stays Brave (measured on Brave 157.1.99.55 with nodriver): Shields blocks
ads and trackers, farbling gives each site and each session its own canvas and audio values, the
page sees navigator.brave, and userAgentData names Brave. So this module only finds Brave, keeps
its profiles apart from Chrome's, and writes what has to be in a profile before the browser
starts. It imports nothing from the server.

A profile belongs to the browser that made it. Brave keeps its cookies and saved passwords under
its own Keychain item ("Brave Safe Storage" on macOS), Chrome under "Chrome Safe Storage", and
each writes settings the other does not know. `binding_refusal` refuses a Chrome profile to Brave
and a Brave profile to Chrome, before anything writes to it. Which browser made a profile is read
from its `Local State`: Brave writes `brave` and `brave_shields` there at its first start.

Vertical tabs are a profile setting, written into the sub-profile's Preferences before launch:
Brave `brave.tabs.vertical_tabs_enabled` and `brave.tabs.vertical_tabs_on_right`, Chrome
`vertical_tabs.enabled`. Chrome has no right-hand side, so `right` on Chrome is refused.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import state

ENV_PATH = "MCP_STEALTH_BRAVE_PATH"
CHROME, BRAVE = "chrome", "brave"
UNKNOWN = "unknown"
_PRODUCT_ATTR = "_mcp_stealth_product"
BROWSERS = (CHROME, BRAVE)
# None leaves the profile's own setting alone; off turns vertical tabs off again.
VERTICAL_TABS = ("left", "right", "off")

_NAMES = {CHROME: "Google Chrome", BRAVE: "Brave"}
_BRAVE_EXECUTABLES = {"brave browser", "brave browser beta", "brave", "brave.exe", "brave-browser",
                      "brave-browser-stable"}


def name(browser: str) -> str:
    return _NAMES.get(browser, browser)


def candidates() -> list[str]:
    """Where Brave's release channel installs itself on this OS."""
    if sys.platform == "darwin":
        app = "Brave Browser.app/Contents/MacOS/Brave Browser"
        return [f"/Applications/{app}", str(Path.home() / "Applications" / app)]
    if sys.platform == "win32":
        tail = Path("BraveSoftware") / "Brave-Browser" / "Application" / "brave.exe"
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        return [str(Path(root) / tail) for root in roots if root]
    # Brave's own binary first: /usr/bin/brave-browser and /opt/.../brave-browser are shell
    # scripts, so the PID recorded for them is not the browser holding the profile, and
    # keep_alive could not reattach to it.
    return ["/opt/brave.com/brave/brave", "/usr/bin/brave-browser", "/usr/bin/brave-browser-stable",
            "/usr/bin/brave", "/opt/brave.com/brave/brave-browser", "/snap/bin/brave"]


def find_brave() -> Optional[str]:
    """Brave's executable, or None. MCP_STEALTH_BRAVE_PATH names one in another place."""
    chosen = os.environ.get(ENV_PATH, "").strip()
    for path in ([chosen] if chosen else candidates()):
        if path and Path(path).is_file():
            return path
    return None


def install_hint() -> str:
    if sys.platform == "darwin":
        return "Install it from https://brave.com/download/ or with: brew install --cask brave-browser"
    if sys.platform == "win32":
        return "Install it from https://brave.com/download/ or with: winget install Brave.Brave"
    return ("Install it from https://brave.com/linux/ (the brave-browser package from Brave's "
            "APT or RPM repository)")


def is_brave_executable(path: Any) -> bool:
    return bool(path) and Path(str(path)).name.lower() in _BRAVE_EXECUTABLES


def mark(browser: Any, product: str) -> None:
    """Record which browser this is on the nodriver Browser. A launch records what it chose,
    whatever the executable is called; attach records what it measured, or "unknown"."""
    with contextlib.suppress(Exception):
        setattr(browser, _PRODUCT_ATTR, product)


def product_of(handle: Any) -> str:
    """"brave", "chrome" or "unknown" for a nodriver Browser or Tab: what was recorded with
    mark(), else the executable it was started with."""
    browser = getattr(handle, "browser", None) or handle
    recorded = getattr(browser, _PRODUCT_ATTR, None)
    if recorded in (BRAVE, CHROME, UNKNOWN):
        return recorded
    config = getattr(browser, "config", None)
    return BRAVE if is_brave_executable(getattr(config, "browser_executable_path", None)) else CHROME


def brand(product: str) -> str:
    """The brand the browser lists in navigator.userAgentData."""
    return "Brave" if product == BRAVE else "Google Chrome"


def check_request(browser: Optional[str], vertical_tabs: Optional[str]) -> tuple[str, str, str]:
    """(product, executable, error) for a launch's browser= and vertical_tabs=.

    executable is "" for Chrome, which the launch finds as it always has."""
    product = (browser or CHROME).strip().lower()
    if product not in BROWSERS:
        return product, "", f"browser must be 'chrome' or 'brave', not {browser!r}"
    if vertical_tabs is not None and vertical_tabs not in VERTICAL_TABS:
        return product, "", (f"vertical_tabs must be None (leave the profile's setting), 'left', "
                             f"'right' or 'off', not {vertical_tabs!r}")
    if product == CHROME and vertical_tabs == "right":
        return product, "", ("vertical_tabs='right' is refused on Chrome: Google Chrome only puts "
                             "vertical tabs on the left. Use vertical_tabs='left', or "
                             "browser='brave', which has both sides. Nothing was started.")
    if product == CHROME:
        return product, "", ""
    executable = find_brave()
    if executable is None:
        return product, "", f"Brave is not installed here. {install_hint()}"
    return product, executable, ""


# ── The profile flags in extra_args ─────────────────────────────────────────

# Chromium takes a switch after "--" or "-" (and "/" on Windows), and the LAST copy of a switch
# wins. nodriver puts its own --user-data-dir first, so one in extra_args is the profile the
# browser really opens: it has to be the profile every check looks at.
_SWITCH = re.compile(r"^(?:--?%s)(user-data-dir|profile-directory)(=(.*))?$"
                     % ("|/" if sys.platform == "win32" else ""), re.IGNORECASE | re.DOTALL)


def valid_profile_directory(value: str) -> bool:
    """One folder name inside the user-data folder: no path, no "." or ".."."""
    return bool(value) and value not in (".", "..") and "/" not in value and "\\" not in value \
        and not Path(value).is_absolute() and ":" not in value


def split_profile_args(args: Optional[list]) -> tuple[Optional[str], list, str]:
    """(the --user-data-dir in args or None, args without it, problem).

    A --profile-directory stays in args but must name one folder, and copies must agree; so
    must copies of --user-data-dir. A switch with no value is refused rather than guessed."""
    root: Optional[str] = None
    sub: Optional[str] = None
    rest: list = []
    for arg in args or []:
        found = _SWITCH.match(str(arg or ""))
        if not found:
            rest.append(arg)
            continue
        name, value = found.group(1).lower(), (found.group(3) or "").strip()
        if not value:
            return None, list(args or []), f"{arg!r} in extra_args needs a value (--{name}=...)"
        if name == "user-data-dir":
            if root is not None and root != value:
                return None, list(args or []), (f"extra_args names two different --user-data-dir "
                                                f"folders ({root!r} and {value!r})")
            root = value
            continue
        if not valid_profile_directory(value):
            return None, list(args or []), (f"--profile-directory must be one folder name inside the "
                                            f"profile, not {value!r}")
        if sub is not None and sub != value:
            return None, list(args or []), (f"extra_args names two different --profile-directory "
                                            f"values ({sub!r} and {value!r})")
        sub = value
        rest.append(arg)
    return root, rest, ""


# ── Profiles ─────────────────────────────────────────────────────────────────

def main_profile(data_root: Path) -> Path:
    """The "main" instance's Brave profile, beside Chrome's ~/.mcp-stealth/profile."""
    return Path(data_root) / "brave-profile"


def profiles_root(data_root: Path) -> Path:
    """Named instances' Brave profiles, beside Chrome's ~/.mcp-stealth/profiles."""
    return Path(data_root) / "brave-profiles"


def instance_profile(data_root: Path, instance_id: str) -> Path:
    return main_profile(data_root) if instance_id == "main" else profiles_root(data_root) / instance_id


def fallback_profile(data_root: Path, instance_id: str) -> Path:
    """A Brave profile of this process's own when the instance's is held by another browser."""
    if instance_id == "main":
        return Path(data_root) / f"brave-profile-pid{os.getpid()}"
    return profiles_root(data_root) / f"{instance_id}-pid{os.getpid()}"


def profile_browser(user_data_dir: Optional[Path]) -> Optional[str]:
    """Which browser made this user-data folder: "brave", "chrome" (any other Chromium), None
    when nothing is in it yet, or "unknown" when it holds something and the maker cannot be told.

    Brave writes `brave` and `brave_shields` into Local State at its first start, and into every
    profile's Preferences; Chrome never does. Local State and every sub-profile's Preferences are
    the evidence, and all of it has to be readable and agree. One unreadable file, two that
    disagree, or a folder that is not empty with none of them is "unknown", which neither browser
    opens: unknown is not a yes.
    """
    if user_data_dir is None:
        return None
    folder = Path(user_data_dir)
    try:
        if not folder.exists():
            return None
        if folder.is_symlink() or not folder.is_dir():
            return UNKNOWN
        with os.scandir(folder) as entries:
            if next(entries, None) is None:
                return None
    except OSError:
        return UNKNOWN
    seen: set[str] = set()

    def judge(path: Path) -> None:
        if path.is_symlink():
            seen.add(UNKNOWN)  # a link, dangling or not, is not read: its target is not this profile
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            seen.add(UNKNOWN)  # unreadable is not evidence for either browser
            return
        seen.add(BRAVE if ("brave" in data or "brave_shields" in data) else CHROME)

    local_state = folder / "Local State"
    if local_state.exists() or local_state.is_symlink():
        judge(local_state)
    try:
        subs = [sub for sub in folder.iterdir() if os.path.lexists(sub / "Preferences")]
    except OSError:
        return UNKNOWN
    for sub in subs:
        if sub.is_symlink():
            return UNKNOWN
        judge(sub / "Preferences")
    return seen.pop() if len(seen) == 1 and UNKNOWN not in seen else UNKNOWN


def binding_refusal(user_data_dir: Optional[Path], product: str) -> str:
    """Why this browser must not open this profile, or "" when it may.

    Brave and Chrome encrypt cookies and saved passwords with different keys, so either one
    opening the other's profile finds them unreadable, and Chromium drops cookies it cannot
    decrypt (measured: Chrome on a Brave profile left 0 of 3 cookie rows). A profile nothing has
    written yet belongs to whichever browser opens it first. One whose maker cannot be told is
    refused to both.
    """
    maker = profile_browser(user_data_dir)
    if maker is None or maker == product:
        return ""
    if maker == UNKNOWN:
        return (f"refused to open {user_data_dir} with {name(product)}: it holds data, but which "
                f"browser made it cannot be told (its Local State and Preferences are missing, "
                f"unreadable or disagree). Brave and Chrome cannot read each other's cookies and "
                f"drop what they cannot read, so neither opens it. Nothing was started and the "
                f"profile was not changed.")
    maker_name = "Brave" if maker == BRAVE else "Google Chrome (or another Chromium browser)"
    return (f"refused to open {user_data_dir} with {name(product)}: it was made by {maker_name}. "
            f"Brave and Chrome keep cookies and saved passwords under different keys, so "
            f"{name(product)} could not read them and would drop the cookies. Nothing was started "
            f"and the profile was not changed. Launch it with browser='{maker}', or use another "
            f"instance_id or persistent=False for a profile of its own.")


# ── Vertical tabs ────────────────────────────────────────────────────────────

def subprofile(browser_args: list) -> str:
    """The sub-profile the browser opens: the last --profile-directory, as Chromium reads it."""
    chosen = "Default"
    for arg in browser_args or ():
        found = _SWITCH.match(str(arg or ""))
        if found and found.group(1).lower() == "profile-directory" and (found.group(3) or "").strip():
            chosen = found.group(3).strip()
    return chosen


def _preferences_target(user_data_dir: Path, browser_args: list) -> Path:
    """The Preferences file to write, proved to sit inside the profile. Raises ValueError."""
    root = Path(user_data_dir)
    sub = subprofile(browser_args)
    if not valid_profile_directory(sub):
        raise ValueError(f"--profile-directory must be one folder name inside the profile, not {sub!r}")
    folder = root / sub
    prefs = folder / "Preferences"
    for path in (folder, prefs):
        if path.is_symlink():
            raise ValueError(f"{path} is a link, so where the setting would go is not known")
    real_root = root.resolve()
    if prefs.resolve().parent.parent != real_root:
        raise ValueError(f"{prefs} is not inside {real_root}")
    return prefs


def _refuse_unless_free(user_data_dir: Path) -> None:
    """Only a profile positively seen free is written: no lock (an unreadable lock counts as
    one) and no process naming it, with the process list actually read."""
    root = Path(user_data_dir)
    lock, _pid = state.chrome_lock_state(root)
    if lock != "free":  # "held", or "unknown": a lock that cannot be read or looked at
        raise ValueError(f"{root} is in use (its lock is held or cannot be read), so its "
                         "settings were not changed")
    holders = state.processes_naming(root)
    if holders is None:
        raise ValueError(f"whether a browser holds {root} cannot be told (the process list "
                         "could not be read), so its settings were not changed")
    if holders:
        raise ValueError(f"{root} is in use by PID {holders[0][0]}, so its settings were not changed")


def vertical_tab_prefs(product: str, mode: str) -> dict:
    """The Preferences keys that put tabs on the side, as nested dicts."""
    enabled = mode in ("left", "right")
    if product == BRAVE:
        return {"brave": {"tabs": {"vertical_tabs_enabled": enabled,
                                   "vertical_tabs_on_right": mode == "right"}}}
    return {"vertical_tabs": {"enabled": enabled}}


def _merge(into: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict):
            child = into.get(key)
            if not isinstance(child, dict):
                child = into[key] = {}
            _merge(child, value)
        else:
            into[key] = value


def write_vertical_tabs(user_data_dir: Path, browser_args: list, product: str,
                        mode: Optional[str]) -> str:
    """Write the vertical-tab setting into the launched sub-profile's Preferences, before the
    browser starts. Returns the note for the launch result. Raises when the file exists and
    cannot be parsed: writing a new one over it would wipe the profile's settings."""
    if mode is None:
        return ""
    prefs = _preferences_target(Path(user_data_dir), browser_args)
    folder = prefs.parent
    _refuse_unless_free(Path(user_data_dir))
    data: dict = {}
    if prefs.is_file():
        data = json.loads(prefs.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{prefs} does not hold a settings object")
    _merge(data, vertical_tab_prefs(product, mode))
    folder.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".Preferences-", dir=str(folder))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, prefs)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    side = {"left": "on the left", "right": "on the right", "off": "off"}[mode]
    return f" [vertical tabs: {side}]"


def vertical_tabs_read_back(user_data_dir: Path, browser_args: list, product: str) -> Optional[str]:
    """What the profile's Preferences say now: left, right, off, or None when unset."""
    try:
        data = json.loads((Path(user_data_dir) / subprofile(browser_args) / "Preferences")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if product == BRAVE:
        tabs = ((data.get("brave") or {}).get("tabs") or {})
        if "vertical_tabs_enabled" not in tabs:
            return None
        if not tabs.get("vertical_tabs_enabled"):
            return "off"
        return "right" if tabs.get("vertical_tabs_on_right") else "left"
    vertical = data.get("vertical_tabs") or {}
    if "enabled" not in vertical:
        return None
    return "left" if vertical.get("enabled") else "off"
