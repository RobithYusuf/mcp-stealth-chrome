"""Refuse to open a profile with a Chrome older than the one that last opened it.

Chrome migrates a profile's databases forward when a newer version opens it and never back.
An older Chrome opening that profile meets schemas it does not know, which can lose or damage
data such as cookies and logins. This matters here because a visible browser may run from
Chrome Beta (MCP_STEALTH_DOCK_ICON=1), a version ahead of Google Chrome, while headless and
the switch turned off use Google Chrome.

What last opened a profile is read, never written: Chrome writes its full version to
`Last Version` in the user-data folder at every start, and to Local State as
`user_experience_metrics.stability.stats_version` ("158.0.8127.18-64"). The newer of the two
counts. A profile with neither, or a Chrome whose version cannot be read, is not refused.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")


def parse(text: Optional[str]) -> Optional[tuple[int, int, int, int]]:
    found = _VERSION.search(text or "")
    return tuple(int(part) for part in found.groups()) if found else None  # type: ignore[return-value]


def text(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def last_opened_by(user_data_dir: Path) -> Optional[tuple[int, int, int, int]]:
    """The newest Chrome version recorded in the profile, or None when it records none."""
    found = []
    try:
        found.append(parse((user_data_dir / "Last Version").read_text(encoding="utf-8")[:64]))
    except (OSError, ValueError):
        pass
    try:
        local_state = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
        found.append(parse(str(local_state["user_experience_metrics"]["stability"]["stats_version"])))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = [v for v in found if v]
    return max(found) if found else None


def binary_version(executable: Optional[str]) -> Optional[tuple[int, int, int, int]]:
    """The version of the Chrome at `executable`, without starting it where that can be avoided.

    macOS reads the app bundle's Info.plist and Windows the version folder beside chrome.exe.
    Linux asks `--version`, once per binary and modification time.
    """
    if not executable:
        return None
    path = Path(str(executable))
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    return _binary_version(str(path), stamp)


@functools.lru_cache(maxsize=16)
def _binary_version(executable: str, _stamp: int) -> Optional[tuple[int, int, int, int]]:
    path = Path(executable)
    bundle = next((p for p in path.parents if p.suffix == ".app"), None)
    if bundle is not None:
        try:
            with open(bundle / "Contents" / "Info.plist", "rb") as handle:
                return parse(str(plistlib.load(handle).get("CFBundleShortVersionString", "")))
        except Exception:
            return None
    if sys.platform == "win32":
        with_versions = [parse(p.name) for p in path.parent.iterdir() if p.is_dir()]
        found = [v for v in with_versions if v]
        return max(found) if found else None
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return parse(done.stdout)


def refusal(user_data_dir: Optional[Path], executable: Optional[str], *, beta: bool = False,
            name: Optional[str] = None) -> str:
    """Why `executable` must not open `user_data_dir`, or "" when it may.

    `beta` says this launch runs from Chrome Beta, which only changes the advice. `name` is the
    browser as the message should call it (Brave, Brave Beta); Chrome's names by default.
    """
    if user_data_dir is None:
        return ""
    folder = Path(user_data_dir)
    if not folder.is_dir():
        return ""
    recorded = last_opened_by(folder)
    running = binary_version(executable)
    if recorded is None or running is None or running >= recorded:
        return ""
    brave = bool(name) and name.startswith("Brave")
    name = name or ("Chrome Beta" if beta else "Google Chrome")
    if brave:
        advice = (
            f"Launch it with a Brave of version {text(recorded)} or newer. If Brave Beta opened it "
            "(MCP_STEALTH_DOCK_ICON=1 runs visible Brave from Brave Beta), open it the same way, "
            "visible and with the switch on, or wait until Brave reaches that version. "
            "persistent=False, or another instance_id, starts on a profile of its own.")
        return (
            f"refused to open {folder}: it was last opened by version {text(recorded)}, and this "
            f"launch would use {name} {text(running)}, which is older. An older browser can damage "
            f"a profile a newer one has upgraded. Nothing was started and the profile was not "
            f"changed. {advice}")
    advice = (
        f"Launch it with a Chrome of version {text(recorded)} or newer. If Chrome Beta opened it "
        "(MCP_STEALTH_DOCK_ICON=1 runs visible browsers from Beta), open it the same way, "
        "visible and with the switch on, or wait until Google Chrome reaches that version. "
        "persistent=False, or another instance_id, starts on a profile of its own.")
    return (
        f"refused to open {folder}: it was last opened by Chrome {text(recorded)}, and this "
        f"launch would use {name} {text(running)}, which is older. An older Chrome can damage a "
        f"profile a newer one has upgraded. Nothing was started and the profile was not changed. "
        f"{advice}")



def upgrade_note(user_data_dir: Optional[Path], executable: Optional[str]) -> str:
    """When this launch moves a profile to a newer major version, what that means from now on.

    The step is one-way, and the next launch with an older Chrome is refused, so the result
    says so the first time rather than leaving it to that refusal.
    """
    if user_data_dir is None or not Path(user_data_dir).is_dir():
        return ""
    recorded = last_opened_by(Path(user_data_dir))
    running = binary_version(executable)
    if recorded is None or running is None or running[0] <= recorded[0]:
        return ""
    return (f" [profile: last opened by Chrome {text(recorded)}; this launch upgrades it to "
            f"{text(running)}, and an older Chrome will be refused it from now on]")


def beta_verdict(user_data_dir: Optional[Path], *, chrome: Optional[str], beta: Optional[str],
                 extra_args=(), names: tuple[str, str] = ("Google Chrome", "Chrome Beta")
                 ) -> tuple[bool, str]:
    """Whether Chrome Beta may open this profile, and what the launch result should say.

    Chrome 158, in any profile outside its default folder, does not read the `v10` cookies
    Chrome 157 writes, and deletes them (Chromium CL 8603400, `KeychainKeyProviderUserDataDir
    Protection`): a copy of a profile with 1172 cookies had none left after Beta opened it. So Beta only gets a
    profile nothing else has written to: none at all (a temporary one), a folder that does not
    exist yet or is empty, or one only Beta can open, because Beta wrote it last and it holds
    no `v10` cookie. Anything in doubt is a no, and Google Chrome opens it.

    `names` is (release, beta) as the note calls them; Brave and Brave Beta follow the same
    rule (brave_beta.py), since Brave 158 drops `v10` cookies too.
    """
    release_name, beta_name = names
    family = release_name.split()[-1] if release_name.startswith("Google") else release_name
    if any((arg or "").startswith("--user-data-dir=") for arg in (extra_args or ())):
        return False, (f"this launch names its own --user-data-dir, which may hold data from "
                       f"{release_name}, and {beta_name} would delete its cookies")
    if user_data_dir is None:
        return True, ""
    folder = Path(user_data_dir)
    try:
        if folder.is_symlink():
            return False, f"{folder} is a link, so what it holds is not known"
        if not folder.exists():
            return True, ""
        if not folder.is_dir():
            return False, f"{folder} is not a folder"
        with os.scandir(folder) as entries:
            if next(entries, None) is None:
                return True, ""
    except OSError as exc:
        return False, f"the profile folder could not be read ({exc.strerror or exc})"
    recorded = last_opened_by(folder)
    chrome_version, beta_version = binary_version(chrome), binary_version(beta)
    if (recorded and chrome_version and beta_version and recorded[0] > chrome_version[0]
            and recorded <= beta_version and _no_v10_cookies(folder)):
        return True, (f"this profile was last opened by {family} {text(recorded)}, newer than "
                      f"{release_name} {text(chrome_version)}, so it stays on {beta_name}")
    who = f"{family} {text(recorded)}" if recorded else f"another {family}"
    return False, (f"this profile already holds data from {who}, and {beta_name} would delete "
                   f"its cookies (it cannot read the v10 cookies {release_name} writes)")


def _no_v10_cookies(folder: Path) -> bool:
    """True only when every cookie database in the profile was read and none holds a `v10`
    value. Each is read in its committed state from a private copy with its journal rolled
    back (cookie_db.read), so nothing in the profile is written, and a hot journal or a
    write-ahead log cannot hide a row."""
    from . import cookie_db

    def has_v10(db) -> bool:
        return db.execute("SELECT 1 FROM cookies WHERE substr(encrypted_value, 1, 3) = "
                          "CAST('v10' AS BLOB) LIMIT 1").fetchone() is not None

    try:
        found, _ = cookie_db.stable_read(cookie_db.all_databases(folder), has_v10)
    except Exception:
        return False
    return not any(found.values())
