"""brave_lean: an agent's Brave without the parts of Brave an agent has no use for.

browser="brave" turns these off by default on a profile this server made in an empty folder,
marked with PROVENANCE when it does; a profile the caller named, a clone or an older one keeps its
settings unless brave_lean=True asks (browser_launch / spawn_browser). brave_lean=False keeps Brave
as it ships and puts back what lean wrote. Shields, farbling, Brave's identity (navigator.brave, the brands), vertical
tabs, Safe Browsing, the component updater and the filter lists stay exactly as they are.

Brave's enterprise policies (BraveRewardsDisabled, BraveWalletDisabled, ...) are not used: they
are machine-wide managed preferences and would change the person's own Brave too. Chromium reads
policy from the command line (--policy=) only on Android builds that are not stable or beta
(components/policy/core/common/command_line_policy_provider.cc); on Brave 157 for macOS that switch
left brave://rewards offering Rewards. So everything here is per process or per profile:

- Features, per process: one --disable-features, built by the server's _disable_features_arg so
  nodriver's own list survives. Each name was read from brave-core v1.99.55 (Brave 157) and its
  effect seen on the running browser through brave://settings (loadTimeData): AIChat made
  isLeoAssistantAllowed false and brave://leo-ai an error page, BraveVPN isBraveVPNEnabled false,
  Speedreader isSpeedreaderAllowed false. Playlist is already off by default on the desktop
  (isPlaylistFeatureEnabled false either way); naming it keeps it off should a variations study
  turn it on. BraveRewards is not named: it exists on Android only (rewards_util.cc), so on the
  desktop it would do nothing.
- Local State, per profile folder: P3A, the daily usage ping and Tor windows. Read back from the
  running browser through brave://settings (getP3AEnabled, getStatsUsagePingEnabled,
  brave_tor.isTorEnabled), and in the net-log: a fresh profile idle for 90 s contacted
  usage-ping.brave.com, star-randsrv.bsg.brave.com and collector.bsg.brave.com without lean and none
  of them with it.
- Preferences of the launched sub-profile: toolbar and sidebar buttons, new tab page widgets and
  sponsored images, the wallet's page provider, Brave News, Web Discovery's prompt, the Wayback
  Machine prompt and the default-browser prompt. Brave 157 migrates some older names at start
  (show_branded_background_image became brave.brave_ads.sponsored.enabled, hide_all_widgets is
  split per widget), so the current names are written.

Written only into a profile proved free (no lock, the process list read and naming nothing on
it), never through a link, into one folder name inside the profile, through a temporary file and
os.replace, the same rules as vertical tabs. A file that does not parse is not replaced. Never
into Brave's own default user-data folders (the person's Brave): there the features still go off
for that one process, and nothing is written.

What was there before lean is kept in `.mcp-stealth-brave-lean.json` in the profile folder, so
brave_lean=False puts back every value that still holds what lean wrote, and leaves anything that
was changed since. A key Brave protects with a MAC (Secure Preferences) would be reset by Brave,
so it is not written and the result says so; a key that no longer holds what lean wrote at the
next launch is named as changed since.

Rewards, Brave Ads and the wallet itself (brave://wallet) can only be removed by policy. Rewards
and Ads are off until a person opts in (brave.rewards.enabled, brave.brave_ads.enabled false on a
new profile), so lean only hides their buttons and cards, and says so. Brave Talk is a website
(talk.brave.com); lean hides its new tab page card. First-run and What's New pages need nothing:
nodriver passes --no-first-run, and a headed launch starts with --no-startup-window.

Imports nothing from the server.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import brave as brave_mode
from . import launch_lock
from . import state
from .helpers import confined_output_path

MARKER = ".mcp-stealth-brave-lean.json"
# Written when this server makes a Brave profile in an empty folder: lean is the default there only.
# A hint, never proof: the proof is the record in the server's own data folder (REGISTRY_ENV), which
# binds the folder's real path, device and inode, and which a copy, a moved folder or a mark
# written by hand does not match.
PROVENANCE = ".mcp-stealth-brave-agent-profile.json"
REGISTRY_ENV = "MCP_STEALTH_BRAVE_LEAN_DIR"
ATTR = "_mcp_brave_lean"  # the launch Outcome, kept on the nodriver Config
_ABSENT = {"__absent__": True}
_BLOCKED = {"__blocked__": True}  # never stored: a parent that is not settings

# Feature names Brave 157 checks on the desktop, each proved on the running browser.
FEATURES = ("AIChat", "BraveVPN", "Speedreader", "Playlist")

# Local State keys (dotted path -> value).
LOCAL_STATE = {
    "brave.p3a.enabled": False,
    "brave.p3a.notice_acknowledged": True,
    "brave.stats.reporting_enabled": False,
    "tor.tor_disabled": True,
}

# The launched sub-profile's Preferences (dotted path -> value). Wallet enums are
# brave_wallet.mojom.DefaultWallet: 1 is None (no window.ethereum, window.solana or window.cardano).
# sidebar_show_option 3 is ShowSidebarOption::kShowNever.
PREFERENCES = {
    "brave.ai_chat.show_toolbar_button": False,
    "brave.ai_chat.context_menu_enabled": False,
    "brave.ai_chat.autocomplete_provider_enabled": False,
    "brave.brave_vpn.show_button": False,
    "brave.rewards.show_brave_rewards_button_in_location_bar": False,
    "brave.brave_ads.sponsored.enabled": False,
    "brave.wallet.show_wallet_icon_on_toolbar": False,
    "brave.wallet.default_wallet2": 1,
    "brave.wallet.default_solana_wallet": 1,
    "brave.wallet.default_cardano_wallet": 1,
    "brave.today.should_show_toolbar_button": False,
    "brave.today.opted_in": False,
    "brave.new_tab_page.show_stats": False,
    "brave.new_tab_page.show_rewards": False,
    "brave.new_tab_page.show_brave_news": False,
    "brave.new_tab_page.show_brave_vpn": False,
    "brave.new_tab_page.show_together": False,
    "brave.playlist.enabled": False,
    "brave.web_discovery_enabled": False,
    # The Web Discovery infobar (shown with Brave Search as the engine) counts as dismissed.
    "brave.web_discovery.cta_state": {"id": "v1", "count": 5, "dismissed": True,
                                      "last_displayed": "13300000000000000"},
    "brave.wayback_machine_enabled": False,
    "brave.sidebar.sidebar_show_option": 3,
    "brave.show_side_panel_button": False,
    "brave.default_browser_prompt_enabled": False,
}

# For the result and server_status: what each part of lean turns off, and how.
PARTS = (
    ("Leo AI chat", "feature AIChat, toolbar button and context menu"),
    ("Brave VPN", "feature BraveVPN, toolbar button and new tab card"),
    ("Speedreader and its prompts", "feature Speedreader"),
    ("Playlist", "feature Playlist and its setting"),
    ("P3A", "Local State brave.p3a.enabled"),
    ("daily usage ping", "Local State brave.stats.reporting_enabled"),
    ("Tor windows", "Local State tor.tor_disabled"),
    ("Brave News", "toolbar button and new tab feed"),
    ("wallet page provider and toolbar icon", "default wallets set to None"),
    ("new tab widgets and sponsored images", "stats, Rewards, News, VPN and Talk cards"),
    ("sidebar", "never shown, no side panel button"),
    ("Web Discovery and its prompt", "brave.web_discovery_enabled"),
    ("Wayback Machine prompt", "brave.wayback_machine_enabled"),
    ("default-browser prompt", "brave.default_browser_prompt_enabled"),
)
POLICY_ONLY = ("Rewards and Brave Ads stay as Brave ships them, off until a person opts in "
               "(only policy removes them); lean hides their button and cards",
               "the wallet page (brave://wallet) stays; only policy removes it")


@dataclass
class Outcome:
    """What a launch did about lean, for its result and server_status. `mode` is "on", "off"
    (brave_lean=False: put back) or "left" (the default on a profile lean does not claim)."""
    enabled: bool
    features: tuple = ()
    written: dict = field(default_factory=dict)      # file label -> [keys]
    not_written: list = field(default_factory=list)  # (what, why)
    changed_since: list = field(default_factory=list)  # keys no longer holding lean's value
    restored: list = field(default_factory=list)
    left: list = field(default_factory=list)          # keys changed since lean, left alone
    mode: str = ""
    why: str = ""
    recorded: bool = False  # a record of an earlier lean is in the profile

    def summary(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode, "why": self.why,
                "features_disabled": list(self.features),
                "written": self.written, "not_written": [f"{w}: {why}" for w, why in self.not_written],
                "changed_since_last_launch": self.changed_since, "restored": self.restored,
                "left_as_found": self.left}

    def _problems(self) -> str:
        return "".join(f"; NOT applied: {what} ({why})" for what, why in self.not_written)

    def note(self) -> str:
        if self.mode == "unreserved":
            return f" [brave_lean not applied: {self.why}]"
        if self.mode == "left":
            text = (f"brave_lean off: {self.why}; lean changes a profile's settings by default only "
                    f"when this server made it empty, so pass brave_lean=True to apply it")
            if self.written:
                text += f"; written before it stopped: {', '.join(self.written)}"
            if self.recorded:
                text += "; settings an earlier brave_lean=True wrote stay, brave_lean=False puts them back"
            return f" [{text}{self._problems()}]"
        if not self.enabled:
            head = " [brave_lean=False: Brave as it ships"
            if self.restored or self.left:
                head += f"; put back {len(self.restored)} setting(s) an earlier lean launch changed"
                if self.left:
                    head += f", left {len(self.left)} changed since as they are ({', '.join(self.left)})"
            return head + self._problems() + "]"
        parts = [f"brave_lean on ({self.why or 'asked for'}), turned off: Leo AI, VPN, Speedreader, "
                 f"Playlist (features)"]
        if "Local State" in self.written:
            parts.append("P3A, usage ping, Tor windows (Local State)")
        if any(label != "Local State" for label in self.written):
            parts.append("toolbar and sidebar buttons, new tab widgets and sponsored images, "
                         "News, wallet provider, Web Discovery, Wayback and default-browser "
                         "prompts (profile)")
        text = "; ".join(parts) + self._problems()
        if self.changed_since:
            text += (f"; Brave or a person changed {', '.join(self.changed_since)} since the last "
                     f"lean launch, set again")
        text += ("; kept as Brave ships: Rewards and Ads (off until opted in), the wallet page, "
                 "which only policy removes")
        return f" [{text}]"


def person_brave_folders() -> list[Path]:
    """Brave's own default user-data folders on this OS: the person's Brave, never written."""
    names = ("Brave-Browser", "Brave-Browser-Beta", "Brave-Browser-Nightly")
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "BraveSoftware"
        return [base / n for n in names]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return [Path(local) / "BraveSoftware" / n / "User Data" for n in names] if local else []
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [config / "BraveSoftware" / n for n in names]


def _is_person_brave(root: Path) -> bool:
    try:
        real = os.path.realpath(root)
        return any(os.path.realpath(p) == real for p in person_brave_folders())
    except OSError:
        return True  # unknown is not a no


def _get(data: dict, dotted: str) -> Any:
    """The value at a dotted path, _ABSENT when it is not there, or _BLOCKED when a parent on
    the way holds something other than settings (a string, a number, a list)."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return _BLOCKED
        if part not in node:
            return _ABSENT
        node = node[part]
    return node


def _set(data: dict, dotted: str, value: Any) -> None:
    """Set (or with _ABSENT remove) a dotted path. Callers check _get first: a parent that is
    not a dict is never replaced, and removing builds no parents."""
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if child is None and value != _ABSENT:
            child = node[part] = {}
        if not isinstance(child, dict):
            return
        node = child
    if value == _ABSENT:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


def _read_json(path: Path) -> dict:
    """{} for a file that does not exist; raises for one that does not hold a JSON object."""
    if path.is_symlink():
        raise ValueError(f"{path} is a link, so what it holds is not this profile's")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a settings object")
    return data


def _write_json(path: Path, data: dict) -> None:
    """Through a temporary file beside it, fsync and os.replace: a reader sees the old file or
    the whole new one."""
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(folder))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _tracked(root: Path, sub: str) -> set:
    """Preferences keys Brave protects with a MAC (it resets a value whose MAC does not match):
    those listed under protection.macs in Secure Preferences or Preferences."""
    found: set = set()

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            if key.endswith("_encrypted_hash"):
                continue
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            else:
                found.add(f"{prefix}{key}")
    for name in ("Secure Preferences", "Preferences"):
        with contextlib.suppress(Exception):
            macs = (_read_json(root / sub / name).get("protection") or {}).get("macs") or {}
            if isinstance(macs, dict):
                walk(macs, "")
    return found


def _is_tracked(key: str, tracked: set) -> bool:
    return any(t == key or t.startswith(key + ".") for t in tracked)


def _targets(root: Path, browser_args: list) -> dict:
    """{label: (path, wanted)} for the files lean writes, each proved to sit inside the profile.
    Raises ValueError."""
    prefs = brave_mode._preferences_target(root, browser_args)
    local_state = root / "Local State"
    if local_state.is_symlink():
        raise ValueError(f"{local_state} is a link, so where the setting would go is not known")
    if os.path.realpath(local_state.parent) != os.path.realpath(root):
        raise ValueError(f"{local_state} is not inside {root}")
    sub = prefs.parent.name
    return {"Local State": (local_state, LOCAL_STATE), f"{sub}/Preferences": (prefs, PREFERENCES)}


def _marker_path(root: Path) -> Path:
    path = root / MARKER
    if path.is_symlink():
        raise ValueError(f"{path} is a link")
    return path


def _root_identity(root: Path):
    """(device, inode) of the profile folder, or None while it does not exist."""
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _guard(root: Path, seen) -> None:
    """Before each write: the folder is still not a link and still the one that was checked."""
    if root.is_symlink():
        raise ValueError(f"{root} became a link")
    now = _root_identity(root)
    if seen is not None and now != seen:
        raise ValueError(f"{root} was replaced while lean was writing")


def _is_empty(root: Path) -> bool:
    if not root.exists():
        return True
    with os.scandir(root) as entries:
        return next(entries, None) is None


_NOT_MADE = "was not made empty by this server (a clone, or a profile from before this server marked its own)"
_MISMATCH = ("carries the mark of a profile this server made, but this server's record of it does not "
             "match (a copy, a moved folder or a mark written by hand)")


def _checkpoint(root: Path, seen) -> None:
    """Right before each write or removal: the folder is the one that was checked, and no browser
    took it since (its lock read again; a lock that cannot be read counts as held)."""
    _guard(root, seen)
    if state.chrome_lock_state(root)[0] != "free":
        raise ValueError(f"{root} is in use (its lock is held or cannot be read), so lean stopped")


def _identity_of(root: Path) -> dict:
    st = os.lstat(root)
    return {"root": os.path.realpath(root), "dev": st.st_dev, "ino": st.st_ino}


def _registry_path(registry: Path, root: Path) -> Path:
    name = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest() + ".json"
    return confined_output_path(Path(registry), name)


_UNTRUSTED = ("has a record in {path} that is damaged, of a version this server does not know, or "
              "of another folder at this path, so it is not trusted; a launch never rewrites it")


def _valid_identity(data: Any) -> bool:
    """A record or mark this version wrote: version 1 and the folder's real path, device, inode."""
    return (isinstance(data, dict) and data.get("version") == 1
            and isinstance(data.get("root"), str) and bool(data.get("root"))
            and all(isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
                    for k in ("dev", "ino")))


def _why_not_ours(root: Path, registry: Optional[Path]) -> str:
    """"" when this folder is one this server made empty, proved by its record outside the
    profile matching the folder now; else why not. Raises when the mark cannot be read."""
    hint = root / PROVENANCE
    if not os.path.lexists(hint):
        return f"{root} {_NOT_MADE}"
    if hint.is_symlink() or not hint.is_file():
        return f"{root} {_MISMATCH}"
    mark = _read_json(hint)
    if registry is None:
        return f"{root} cannot be proved to be ours: this server keeps no record of the profiles it made"
    path = _registry_path(registry, root)
    if path.is_symlink() or not path.is_file():
        return f"{root} {_MISMATCH}"
    record = _read_json(path)
    if not _valid_identity(record) or not _valid_identity(mark):
        return f"{root} " + _UNTRUSTED.format(path=path)
    now = _identity_of(root)
    if any(record.get(k) != v or mark.get(k) != v for k, v in now.items()):
        return f"{root} {_MISMATCH}"
    return ""


def _why_not_claimable(root: Path, registry: Path) -> str:
    """"" when an empty folder may be claimed: there is no record for it at all. A record that
    exists, whatever it holds (damaged, unknown version, another folder at this path), keeps the
    default off and is left as it is."""
    path = _registry_path(registry, root)
    return f"{root} " + _UNTRUSTED.format(path=path) if os.path.lexists(path) else ""


def _claim(root: Path, registry: Path, seen) -> None:
    """Mark an empty folder as made by this server: first the record in the server's data
    folder (the proof), then the hint inside the profile."""
    folder = Path(registry)
    if folder.is_symlink():
        raise ValueError(f"{folder} is a link")
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    _checkpoint(root, seen)
    if not _is_empty(root):
        raise ValueError(f"{root} stopped being empty before it was marked as made by this server")
    identity = {"version": 1, **_identity_of(root)}
    path = _registry_path(folder, root)
    if os.path.lexists(path):  # appeared since the decision: never rewritten
        raise ValueError(f"{root} " + _UNTRUSTED.format(path=path))
    _write_json(path, identity)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    _checkpoint(root, seen)
    _write_json(root / PROVENANCE, identity)
    with contextlib.suppress(OSError):
        os.chmod(root / PROVENANCE, 0o600)
    _drop_gone(folder)


_RECORD_NAME = re.compile(r"^[0-9a-f]{64}\.json$")


def _drop_gone(folder: Path) -> None:
    """Records of folders that no longer exist (each persistent=False launch claims one), so the
    record folder does not grow for ever. Only this module's own file names, never a link, and
    only when the recorded path is positively gone; anything unreadable stays."""
    with contextlib.suppress(OSError):
        for entry in os.scandir(folder):
            if not _RECORD_NAME.match(entry.name) or entry.is_symlink() or not entry.is_file():
                continue
            with contextlib.suppress(Exception):
                recorded = _read_json(Path(entry.path)).get("root")
                if isinstance(recorded, str) and recorded and not os.path.lexists(recorded):
                    os.unlink(entry.path)


class _NoReservation(ValueError):
    """Lean holds no launch reservation for this profile: it writes nothing and adds no flag."""


@contextlib.contextmanager
def _reserved(root: Path, out: Outcome):
    """The launch reservation of this profile, without waiting: reentrant when the launch calling
    lean already holds it, and a refusal when another launch (a click on the launcher, another
    server) does. A reservation file that cannot be used is said, as launches say it."""
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(launch_lock.hold([root], 0))
    except TimeoutError as exc:
        raise _NoReservation(f"another launch holds the reservation of {root} ({exc})") from None
    with stack:
        if launch_lock.note():
            # launch_lock lets a launch go on unreserved; lean never writes without one.
            raise _NoReservation(f"no launch reservation could be held for {root} "
                                 f"({launch_lock.note().strip(' []')})")
        yield


def _valid_record(marker: dict, marker_path: Path) -> dict:
    """The record of what lean wrote, {label: {key: {"before", "wrote"}}}; raises when its shape
    is not that, rather than act on a record it cannot read."""
    if not marker:
        return {}
    if marker.get("version") != 1:
        raise ValueError(f"{marker_path} is not a version this server knows")
    record = marker.get("files", {})
    ok = isinstance(record, dict) and all(
        isinstance(keys, dict) and all(isinstance(e, dict) and "wrote" in e and "before" in e
                                       for e in keys.values())
        for keys in record.values())
    if not ok:
        raise ValueError(f"the lean record {marker_path} has an unexpected shape")
    return record


def apply(user_data_dir: Optional[Path], browser_args: list, asked: Optional[bool],
          server_chosen: bool = False, registry: Optional[Path] = None) -> Outcome:
    """Before the browser starts: write lean's settings (on), put back what lean wrote (asked
    False), or leave the profile alone. Never raises: what could not be done is in the Outcome.
    The features are the caller's to pass on the command line when `enabled`.

    asked None is the default, and it is lean only on a profile this server made empty: one the
    launch chose itself (server_chosen) that is empty now, which is then recorded in `registry`
    and marked (PROVENANCE), or whose record there still matches it. A clone, a copy, a moved
    folder, an older profile or a folder the caller named keeps its settings unless
    brave_lean=True asks. Under the default, anything uncertain applies nothing, not even the
    features, and says why."""
    out = Outcome(enabled=asked is not False, mode="off" if asked is False else "on",
                  why="asked for" if asked else "")
    if user_data_dir is None:
        out.features = FEATURES if out.enabled else ()
        if out.enabled:
            out.not_written.append(("profile settings", "the launch has no profile folder"))
        return out
    root = Path(user_data_dir)
    claim, decided = False, asked is not None
    out.features = FEATURES if decided and out.enabled else ()
    try:
        if _is_person_brave(root):
            out.features = FEATURES if out.enabled else ()
            if out.enabled:
                out.not_written.append(("profile settings", f"{root} is the person's own Brave folder, "
                                        "which lean never writes; the features are off for this "
                                        "process only"))
            return out
        if root.is_symlink():
            raise ValueError(f"{root} is a link, so which folder would be written is not known")
        # Inside the profile's launch reservation from the first look to the last write: no launch
        # path of this project can start a browser on it meanwhile (see launch_lock).
        with _reserved(root, out):
            marker_path = _marker_path(root)
            out.recorded = marker_path.exists()
            if asked is None:
                if not server_chosen:
                    out.why = f"{root} is a folder you named (user_data_dir or --user-data-dir)"
                elif _is_empty(root):
                    out.why, claim = "default for a profile this server made empty", True
                    if registry is None:
                        out.why = (f"{root} cannot be proved to be ours: this server keeps no record "
                                   f"of the profiles it made")
                    else:
                        out.why = _why_not_claimable(root, registry) or out.why
                else:
                    out.why = _why_not_ours(root, registry) or "default for a profile this server made empty"
                decided = True
                if not out.why.startswith("default"):
                    out.enabled, out.mode = False, "left"
                    return out
            out.features = FEATURES if out.enabled else ()
            if asked is False and not out.recorded:
                return out  # nothing lean ever wrote here
            targets = _targets(root, browser_args)
            brave_mode._refuse_unless_free(root)
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise ValueError(f"{root} is a link, so which folder would be written is not known")
            seen = _root_identity(root)  # from here on every write checks this folder is still it
            record = _valid_record(_read_json(marker_path), marker_path)
            if claim:
                _claim(root, registry, seen)
            if out.enabled:
                _write_lean(root, targets, record, marker_path, out, seen)
            else:
                _take_back(root, record, marker_path, out, seen)
    except _NoReservation as exc:
        out.enabled, out.mode, out.features = False, "unreserved", ()
        out.why = (f"{exc}, so lean wrote nothing and added no flag; the browser starts as "
                   f"Brave ships it")
    except Exception as exc:
        what = "profile settings" if out.enabled or asked is None else "putting back lean's earlier settings"
        if asked is None:  # the default applies nothing it is not sure of, not even the features
            out.why = (f"whether this server made {root} could not be told" if not decided
                       else "lean could not be applied with certainty")
            out.enabled, out.mode, out.features = False, "left", ()
        out.not_written.append((what, f"{type(exc).__name__}: {exc}"))
    return out


def _write_lean(root: Path, targets: dict, record: dict, marker_path: Path, out: Outcome,
                seen) -> None:
    sub = next(label for label in targets if label != "Local State").split("/")[0]
    tracked = _tracked(root, sub)
    loaded = {label: _read_json(path) for label, (path, _w) in targets.items()}
    plan: dict = {}
    for label, (path, wanted) in targets.items():
        data = loaded[label]
        seen_keys = record.get(label) or {}
        for key, value in wanted.items():
            if label != "Local State" and _is_tracked(key, tracked):
                out.not_written.append((key, "Brave protects it with a MAC and would reset it"))
                continue
            now = _get(data, key)
            if now == _BLOCKED:
                out.not_written.append((key, "a parent of it holds something other than settings, "
                                             "which lean does not replace"))
                continue
            entry = seen_keys.get(key)
            still_ours = entry is not None and now in (entry["wrote"], value)
            if entry is not None and not still_ours:
                out.changed_since.append(key)
            # What off puts back: the value from before lean, or the one set since lean last wrote.
            before = entry["before"] if still_ours else now
            plan.setdefault(label, {})[key] = {"before": before, "wrote": value}
    # The record first: a crash after it leaves values that still match `before`, which a
    # later brave_lean=False leaves alone.
    _checkpoint(root, seen)
    _write_json(marker_path, {"version": 1, "files": {**record, **{
        label: {**(record.get(label) or {}), **keys} for label, keys in plan.items()}}})
    with contextlib.suppress(OSError):
        os.chmod(marker_path, 0o600)
    labels = list(plan)
    for index, label in enumerate(labels):
        keys = plan[label]
        path = targets[label][0]
        data = loaded[label]
        for key, entry in keys.items():
            _set(data, key, entry["wrote"])
        try:
            _checkpoint(root, seen)
        except ValueError as exc:
            raise ValueError(f"{exc}; written: {', '.join(out.written) or 'only the lean record'}; "
                             f"not written: {', '.join(labels[index:])}") from None
        _write_json(path, data)
        out.written[label] = sorted(keys)


def _recorded_path(root: Path, label: str) -> Path:
    """The file a record label names, proved to sit inside the profile. Raises ValueError."""
    if label == "Local State":
        path = root / "Local State"
        if path.is_symlink():
            raise ValueError(f"{path} is a link")
        return path
    sub, _, name = label.partition("/")
    if name != "Preferences":
        raise ValueError(f"the lean record names {label!r}, which is not a settings file")
    return brave_mode._preferences_target(root, [f"--profile-directory={sub}"])


def _take_back(root: Path, record: dict, marker_path: Path, out: Outcome, seen) -> None:
    """Every file lean wrote, whichever sub-profile launches now. A key still holding what lean
    wrote gets its earlier value back; one changed since is left and named. A file that cannot
    be read keeps its part of the record, so a later launch can put it back."""
    remaining: dict = {}
    for label, keys in record.items():
        try:
            path = _recorded_path(root, label)
            data = _read_json(path)
            changed = False
            for key, entry in keys.items():
                now = _get(data, key)
                if now == entry["wrote"]:
                    _set(data, key, entry["before"])
                    out.restored.append(key)
                    changed = True
                elif now != entry["before"]:
                    out.left.append(key)
            if changed:
                _checkpoint(root, seen)
                _write_json(path, data)
        except Exception as exc:
            remaining[label] = keys
            out.not_written.append((f"putting back {label}", f"{type(exc).__name__}: {exc}"))
    _checkpoint(root, seen)
    if remaining:
        _write_json(marker_path, {"version": 1, "files": remaining})
    else:
        marker_path.unlink()


def read_back(user_data_dir: Path, browser_args: list) -> dict:
    """What the profile's files say now for each lean key: {key: value or None when unset}."""
    root = Path(user_data_dir)
    result: dict = {}
    for label, (path, wanted) in _targets(root, browser_args).items():
        try:
            data = _read_json(path)
        except Exception:
            data = {}
        for key in wanted:
            value = _get(data, key)
            result[key] = None if value == _ABSENT else value
    return result


def status() -> dict:
    """For server_status: what lean does and what it cannot."""
    return {"default": "on for browser='brave' (brave_lean=False to keep Brave as it ships)",
            "features_disabled": list(FEATURES),
            "local_state": sorted(LOCAL_STATE), "profile": sorted(PREFERENCES),
            "turns_off": [f"{what} ({how})" for what, how in PARTS],
            "not_possible_without_policy": list(POLICY_ONLY),
            "kept": ["Shields", "farbling", "navigator.brave and the Brave brands", "vertical tabs",
                     "Safe Browsing", "component updates and filter lists"]}
