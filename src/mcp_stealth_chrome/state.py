"""Multi-instance browser state with backward-compatible singleton facade.

Design: BrowserState keeps class-level attributes for the CURRENT instance
(identical to original singleton). Other instances are stored in `instances`
dict as snapshots. Switching instances swaps the snapshot into class attrs.

All existing code that does `BrowserState.tabs` etc. keeps working unchanged —
it always reads/writes whichever instance is currently active.
"""
from __future__ import annotations

import json
import os
import re
import time
import weakref
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from nodriver import Browser, Tab

HOME = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or ".")
PROFILES_ROOT = HOME / ".mcp-stealth" / "profiles"
PROFILE_DIR = HOME / ".mcp-stealth" / "profile"  # legacy default ("main" instance)
SCREENSHOT_DIR = HOME / ".mcp-stealth" / "screenshots"
EXPORT_DIR = HOME / ".mcp-stealth" / "exports"
STORAGE_STATE_DIR = HOME / ".mcp-stealth" / "storage-states"
IDENTITIES_DIR = HOME / ".mcp-stealth" / "identities"
PRIVACY_PROFILE_ROOT = HOME / ".mcp-stealth" / "privacy-profiles"
# Where a window was last placed, per instance. Kept in our own file rather than
# relying on Chrome's Preferences, because wipe_window_state() deliberately
# clears Chrome's copy to cure the corrupt-placement / invisible-window bug.
WINDOW_STATE_DIR = HOME / ".mcp-stealth" / "window-state"

DEFAULT_IDLE_TIMEOUT = int(os.environ.get("BROWSER_IDLE_TIMEOUT", "600"))  # 10 min
IDLE_REAPER_INTERVAL = int(os.environ.get("BROWSER_IDLE_REAPER_INTERVAL", "60"))


def find_chrome_binary() -> Optional[str]:
    """Locate Chrome/Chromium on this system. Returns path or None."""
    import sys
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif sys.platform.startswith("linux"):
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/bin/microsoft-edge",
            "/usr/bin/brave-browser",
        ]
    elif sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            str(Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(pfx86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(localappdata) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(pfx86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(pf) / "Chromium" / "Application" / "chromium.exe"),
            str(Path(localappdata) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"),
            str(Path(localappdata) / "Vivaldi" / "Application" / "vivaldi.exe"),
        ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


CHROME_INSTALL_HINT = {
    "darwin": (
        "Install Chrome from https://www.google.com/chrome/\n"
        "  or via Homebrew: brew install --cask google-chrome"
    ),
    "linux": (
        "Install Chrome:\n"
        "  Ubuntu/Debian: sudo apt install -y google-chrome-stable  (add Google's APT repo first)\n"
        "  Or Chromium:  sudo apt install -y chromium-browser\n"
        "  Fedora:       sudo dnf install -y chromium"
    ),
    "win32": (
        "Install Chrome from https://www.google.com/chrome/\n"
        "  or via winget: winget install Google.Chrome"
    ),
}


def chrome_install_hint() -> str:
    import sys
    for key, hint in CHROME_INSTALL_HINT.items():
        if sys.platform.startswith(key) or sys.platform == key:
            return hint
    return "Install Chrome or Chromium from https://www.google.com/chrome/"


def chrome_user_data_roots() -> list[tuple[str, Path]]:
    """Every Chromium-family user-data root on this machine, as (browser, path).

    Returning ALL of them matters: people routinely keep their real logged-in
    sessions in Brave or Edge while a near-empty Chrome sits alongside. Reporting
    only the first hit makes the tool look like it surveyed the machine when it
    actually hid the profiles the user cares about.
    """
    import sys
    home = Path.home()
    candidates: list[tuple[str, Path]] = []
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        candidates = [
            ("chrome", support / "Google" / "Chrome"),
            ("chrome-beta", support / "Google" / "Chrome Beta"),
            ("chrome-canary", support / "Google" / "Chrome Canary"),
            ("chromium", support / "Chromium"),
            ("edge", support / "Microsoft Edge"),
            ("brave", support / "BraveSoftware" / "Brave-Browser"),
            ("brave-beta", support / "BraveSoftware" / "Brave-Browser-Beta"),
            ("vivaldi", support / "Vivaldi"),
            ("opera", support / "com.operasoftware.Opera"),
            ("arc", support / "Arc" / "User Data"),
        ]
    elif sys.platform.startswith("linux"):
        cfg = home / ".config"
        candidates = [
            ("chrome", cfg / "google-chrome"),
            ("chromium", cfg / "chromium"),
            ("edge", cfg / "microsoft-edge"),
            ("brave", cfg / "BraveSoftware" / "Brave-Browser"),
            ("vivaldi", cfg / "vivaldi"),
            ("opera", cfg / "opera"),
        ]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates = [
            ("chrome", local / "Google" / "Chrome" / "User Data"),
            ("chromium", local / "Chromium" / "User Data"),
            ("edge", local / "Microsoft" / "Edge" / "User Data"),
            ("brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("vivaldi", local / "Vivaldi" / "User Data"),
            ("opera", roaming / "Opera Software" / "Opera Stable"),
        ]
    return [(name, path) for name, path in candidates
            if path.exists() and (path / "Local State").exists()]


def chrome_user_data_root(browser: Optional[str] = None) -> Optional[Path]:
    """One user-data root: the named browser, else the first one found.

    Kept for callers that only need a default; prefer chrome_user_data_roots()
    when the answer should cover the whole machine.
    """
    roots = chrome_user_data_roots()
    if browser:
        wanted = browser.strip().lower()
        for name, path in roots:
            if name == wanted:
                return path
        return None
    return roots[0][1] if roots else None


def _read_singleton_pid(profile_path: Path) -> Optional[int]:
    """Chrome's SingletonLock is a symlink whose target is `hostname-PID`.
    Returns the PID it points to, or None if unreadable / not a symlink."""
    lock = profile_path / "SingletonLock"
    try:
        if not lock.is_symlink():
            return None
        target = os.readlink(lock)  # e.g. "hostname-12345"
        pid_str = target.rsplit("-", 1)[-1]
        return int(pid_str) if pid_str.isdigit() else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (POSIX: signal 0; Windows: tasklist)."""
    if pid <= 0:
        return False
    import sys
    if sys.platform == "win32":
        try:
            import subprocess
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=2,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return True  # assume alive when uncertain
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    except OSError:
        return False


def is_chrome_profile_locked(profile_path: Path) -> bool:
    """True only if SingletonLock exists AND points to a live PID.
    Stale locks (Chrome crashed / orphaned) return False so we treat the
    profile as free to use."""
    lock = profile_path / "SingletonLock"
    try:
        if not (lock.exists() or lock.is_symlink()):
            return False
    except OSError:
        return False
    pid = _read_singleton_pid(profile_path)
    if pid is None:
        # Lock exists but unreadable / not a symlink — assume live to be safe
        return True
    return _pid_alive(pid)


def chrome_lock_holder_pid(profile_path: Path) -> Optional[int]:
    """Return the PID currently holding this profile's SingletonLock, or None
    if free / stale / unreadable. Used for actionable launch-failure messages.

    Reads the symlink once (is_chrome_profile_locked + _read_singleton_pid
    both re-read it otherwise) and returns the PID only when it is alive."""
    pid = _read_singleton_pid(profile_path)
    if pid is None:
        return None
    return pid if _pid_alive(pid) else None


def find_external_chrome_pids() -> list[int]:
    """Find PIDs of running Chrome processes NOT spawned by this MCP server.
    Returns up to 10 PIDs. Empty list on error or non-POSIX without `pgrep`."""
    import sys
    if sys.platform == "win32":
        try:
            import subprocess
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=2,
            )
            pids = []
            for line in (r.stdout or "").splitlines()[:10]:
                parts = line.split(",")
                if len(parts) >= 2:
                    pid_str = parts[1].strip('"')
                    if pid_str.isdigit():
                        pids.append(int(pid_str))
            return pids
        except Exception:
            return []
    try:
        import subprocess
        # NB: must EXCLUDE Chromes this MCP launched (docstring contract) — a
        # bare pgrep 'chrome' matches our own subprocess and substring-only
        # false hits (e.g. chrome-devtools-mcp). Parse `ps` and drop any
        # process whose argv references an MCP-managed profile.
        mcp_root = str(HOME / ".mcp-stealth")
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=3,
        )
        pids: list[int] = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if "chrome" not in low and "chromium" not in low:
                continue
            if f"--user-data-dir={mcp_root}" in line:
                continue  # spawned by this MCP server — not "external"
            head = line.split(None, 1)[0]
            if head.isdigit():
                pids.append(int(head))
            if len(pids) >= 10:
                break
        return pids
    except Exception:
        return []


def _update_prefs(prefs_file: Path, mutate) -> bool:
    """Load Default/Preferences JSON, call mutate(data) -> bool(changed),
    rewrite only if it reports a change. Best-effort: never raises, and never
    rewrites on a parse failure (Chrome rebuilds Preferences on next launch)."""
    if not prefs_file.exists():
        return False
    try:
        data = json.loads(prefs_file.read_text())
        if mutate(data):
            prefs_file.write_text(json.dumps(data))
            return True
    except Exception:
        pass
    return False


def wipe_window_state(profile_path: Path | str | None = None) -> dict:
    """Selectively wipe Chrome's window/session-restore state from a profile.

    PRESERVES: cookies, login data, history, bookmarks, autofill, IndexedDB,
    LocalStorage, extensions, site settings.

    WIPES:
      • `Default/Preferences` keys: `browser.window_placement`,
        `session.startup_urls`, `session.restore_on_startup_migrated`
      • `Default/Sessions/*` (tab/window restore data)
      • `Default/Current Session`, `Default/Current Tabs`,
        `Default/Last Session`, `Default/Last Tabs` (live session blobs)

    Why: macOS sleep/wake cycles can corrupt the window-placement record
    such that next launch has outerWidth/Height = 0 and visibilityState =
    'hidden'. Disabling restore via launch flags doesn't help because
    Chrome still reads the corrupt placement record. Wiping these files
    forces Chrome to bring up a fresh window at default position/size on
    the next launch, while keeping login + cookies intact.

    Always best-effort — never raises. Returns a dict {prefs:bool, sessions:int, files:int}
    summarising what got cleaned.
    """
    pdir = Path(profile_path) if profile_path else PROFILE_DIR
    result = {"prefs": False, "sessions": 0, "files": 0}
    default_dir = pdir / "Default"
    if not default_dir.exists():
        return result

    # 1. Trim corrupt keys from Preferences (JSON file).
    def _trim(data: dict) -> bool:
        changed = False
        browser = data.get("browser") or {}
        for key in ("window_placement", "last_window_state",
                     "last_window_screen_placement"):
            if key in browser:
                del browser[key]
                changed = True
        if changed:
            data["browser"] = browser
        session = data.get("session") or {}
        for key in ("startup_urls", "restore_on_startup_migrated"):
            if key in session:
                del session[key]
                changed = True
        if changed:
            data["session"] = session
        return changed

    result["prefs"] = _update_prefs(default_dir / "Preferences", _trim)

    # 2. Remove Sessions/ contents (directory itself stays).
    sessions_dir = default_dir / "Sessions"
    if sessions_dir.exists():
        try:
            for child in sessions_dir.iterdir():
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                        result["sessions"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    # 3. Remove top-level current/last session+tabs blobs.
    for fname in ("Current Session", "Current Tabs",
                   "Last Session", "Last Tabs"):
        f = default_dir / fname
        try:
            if f.exists() or f.is_symlink():
                f.unlink()
                result["files"] += 1
        except Exception:
            pass
    return result


def find_chrome_pids_by_profile(profile_path: Path) -> list[int]:
    """Find PIDs of Chrome processes launched against a specific user-data-dir.
    Used by browser_recover to kill zombie Chromes whose CDP path is wedged
    but the OS process is still alive. Matches ANY argv containing
    `--user-data-dir=<resolved_profile>` (covers nodriver-spawned helpers too).
    Returns [] on Windows / pgrep failure.
    """
    import sys
    if sys.platform == "win32":
        return []  # WMIC argv parsing is fragile; skip until needed
    try:
        import subprocess
        target = str(profile_path)
        # `ps -ax -o pid=,command=` is portable across macOS+Linux. Pattern
        # match in Python instead of pgrep -f because pgrep escapes regex
        # characters in profile paths inconsistently across platforms.
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=3,
        )
        # The argument must END at the path. A substring match makes
        # ~/.mcp-stealth/profile also claim profiles/* and profile-pid*, so closing
        # one browser would signal every other browser and server on the machine.
        needle = re.compile(re.escape(f"--user-data-dir={target}") + r"""(?=\s|$|["'])""")
        pids: list[int] = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or not needle.search(line):
                continue
            head = line.split(None, 1)[0]
            if head.isdigit():
                pids.append(int(head))
        return pids
    except Exception:
        return []


def per_process_profile() -> Path:
    """Per-PID profile path. Used when the shared default profile is held by
    another live MCP server process — lets parallel Claude sessions coexist
    instead of hanging on Chrome's SingletonLock."""
    return HOME / ".mcp-stealth" / f"profile-pid{os.getpid()}"


# Profile this process has committed to. Deliberately a module global so it
# survives BrowserState.reset() — see resolve_default_profile for why.
_PROFILE_AFFINITY: Optional[Path] = None


def set_profile_affinity(p: Optional[Path]) -> None:
    """Pin this process to a profile dir (None clears the pin)."""
    global _PROFILE_AFFINITY
    _PROFILE_AFFINITY = p


def get_profile_affinity() -> Optional[Path]:
    return _PROFILE_AFFINITY


def resolve_default_profile(persistent: bool) -> Path:
    """Return the profile path to use for the `main` instance.

    Falls back to a per-PID profile if the shared default is held live by another
    process — and then STAYS on it (affinity).

    The affinity matters more than it looks. Without it: launch #1 finds the
    default locked and lands on profile-pid<N>, where you then log in. Something
    goes wrong, browser_recover kills the Chrome that held the default, freeing
    it. Launch #2 now picks the *default* profile — a different cookie jar — so
    every session you built up appears to have been "wiped", while it actually
    sits untouched in profile-pid<N>. Sticking to the chosen profile keeps the
    logins that are by far the most expensive state to rebuild.
    """
    if not persistent:
        return PROFILE_DIR
    pinned = _PROFILE_AFFINITY
    if pinned is not None and not is_chrome_profile_locked(pinned):
        pinned.mkdir(parents=True, exist_ok=True)
        return pinned
    if is_chrome_profile_locked(PROFILE_DIR):
        # Another live Chrome (likely a parallel MCP session) owns this profile.
        # Use a per-process profile so we never collide on the lock.
        p = per_process_profile()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return PROFILE_DIR


def ensure_dirs() -> None:
    for d in (
        PROFILE_DIR,
        PROFILES_ROOT,
        SCREENSHOT_DIR,
        EXPORT_DIR,
        STORAGE_STATE_DIR,
        IDENTITIES_DIR,
        WINDOW_STATE_DIR,
        PRIVACY_PROFILE_ROOT,
    ):
        d.mkdir(parents=True, exist_ok=True)


def clean_profile_state(profile_dir: Path | str | None = None) -> None:
    """Prevent 'Restore pages?' dialog by marking previous exit as clean.
    Only removes Singleton* locks if they're STALE (PID dead) — never yanks
    a lock from a live Chrome instance."""
    pdir = Path(profile_dir) if profile_dir else PROFILE_DIR

    def _mark_clean(data: dict) -> bool:
        profile = data.get("profile", {})
        if profile.get("exit_type") != "Normal":
            profile["exit_type"] = "Normal"
            profile["exited_cleanly"] = True
            data["profile"] = profile
            return True
        return False

    _update_prefs(pdir / "Default" / "Preferences", _mark_clean)
    # Don't blindly nuke locks — if a sibling MCP process owns them, that
    # would corrupt its session. Only remove when stale.
    if is_chrome_profile_locked(pdir):
        return
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = pdir / lock_name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except Exception:
            pass


@dataclass
class InstanceSnapshot:
    """Stored state for a non-active instance."""
    instance_id: str
    browser: Optional[Browser] = None
    tabs: list[Tab] = field(default_factory=list)
    active_tab_index: int = 0
    profile_dir: Optional[Path] = None
    desc: str = ""
    headless: bool = False
    privacy: bool = False
    fingerprint: dict[str, str] = field(default_factory=dict)
    proxy: Optional[str] = None
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    last_active: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    console_logs: list[dict] = field(default_factory=list)
    network_logs: list[dict] = field(default_factory=list)
    network_index: dict[str, dict] = field(default_factory=dict)
    page_errors: list[str] = field(default_factory=list)
    capture_console: bool = False
    capture_network: bool = False

    def touch(self) -> None:
        self.last_active = time.time()

    def is_idle_expired(self) -> bool:
        if self.idle_timeout <= 0:
            return False
        return (time.time() - self.last_active) > self.idle_timeout

    def is_running(self) -> bool:
        if self.browser is None or not self.tabs:
            return False
        if getattr(self.browser, "_process", None) is None:
            connection = getattr(self.browser, "connection", None)
            return connection is not None and not getattr(connection, "closed", True)
        return not getattr(self.browser, "stopped", True)


def _default_field(name: str) -> Any:
    return getattr(_SessionState(), name)


@dataclass
class _SessionState:
    """The current instance and stored instances of one MCP session."""

    browser: Optional[Browser] = None
    tabs: list[Tab] = field(default_factory=list)
    active_tab_index: int = 0
    console_logs: list[dict] = field(default_factory=list)
    network_logs: list[dict] = field(default_factory=list)
    # Per-request map: request_id → full entry with headers/body. Populated by
    # network_start when capture_bodies=True. Flat network_logs above remains the
    # legacy event-stream view.
    network_index: dict[str, dict] = field(default_factory=dict)
    page_errors: list[str] = field(default_factory=list)
    capture_console: bool = False
    capture_network: bool = False

    # Multi-instance: other instances stored here, plus metadata for current
    current_instance_id: str = "main"
    current_profile_dir: Optional[Path] = None
    current_desc: str = ""
    current_headless: bool = False
    current_privacy: bool = False
    current_fingerprint: dict[str, str] = field(default_factory=dict)
    current_proxy: Optional[str] = None
    current_idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    current_last_active: float = field(default_factory=time.time)
    current_created_at: float = field(default_factory=time.time)

    # Last mouse position — enables realistic cursor continuation (no teleports).
    # Updated by tools that move the mouse.
    last_mouse_xy: dict[str, Optional[int]] = field(
        default_factory=lambda: {"x": None, "y": None}
    )

    instances: dict[str, InstanceSnapshot] = field(default_factory=dict)  # does NOT include current


_SESSION_FIELDS = frozenset(_SessionState.__dataclass_fields__)
PRIMARY_SESSION = "primary"
# Instances released or orphaned by a session wait here until another session
# claims them or the idle reaper closes them. Never bound to a client.
POOL_SESSION = "pool"
_SESSIONS: dict[str, _SessionState] = {
    PRIMARY_SESSION: _SessionState(),
    POOL_SESSION: _SessionState(current_instance_id="(pool)"),
}
_current_session: ContextVar[str] = ContextVar("mcp_stealth_session", default=PRIMARY_SESSION)
# Connections without an Mcp-Session-Id (stdio, in-memory) are told apart by object.
_SESSION_KEYS: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
# Mcp-Session-Id -> session key. A GC finalizer cannot tell when these end: every
# task started during a tool call (nodriver's CDP listener among them) copies the
# request context, and that keeps the session object alive as long as the browser.
_HTTP_SESSIONS: dict[str, str] = {}
_primary_taken = False
_session_counter = 1
# Set in HTTP mode: answers whether an Mcp-Session-Id still exists on the server.
_session_is_live: Optional[Callable[[str], bool]] = None


class _SessionRouted(type):
    """Routes BrowserState.tabs and friends to the calling session's state.

    A stdio server has exactly one session, the primary, so every existing caller
    of BrowserState.tabs behaves as it always has. Over HTTP each Mcp-Session-Id
    binds its own state, and the ~100 call sites stay untouched and isolated.
    """

    def __getattr__(cls, name: str) -> Any:
        if name in _SESSION_FIELDS:
            return getattr(_SESSIONS[_current_session.get()], name)
        raise AttributeError(name)

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in _SESSION_FIELDS:
            setattr(_SESSIONS[_current_session.get()], name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in _SESSION_FIELDS:
            setattr(_SESSIONS[_current_session.get()], name, _default_field(name))
        else:
            super().__delattr__(name)


class BrowserState(metaclass=_SessionRouted):
    """Class-level singleton holding CURRENT instance + dict of others.

    `browser`, `tabs`, etc. always reflect the active instance. Other instances
    are stored in `instances` dict as snapshots — switching swaps snapshots in.
    """

    _reaper_task = None  # asyncio.Task

    # ── Sessions ───────────────────────────────────────────────────────────

    @classmethod
    def session_key(cls) -> str:
        return _current_session.get()

    @classmethod
    def bind_session(cls, session: Any, http_session_id: Optional[str] = None) -> Token:
        """Route this call to the state of an MCP session, creating it on first use.

        The first live session takes the primary state, which is what makes a lone
        client identical to the pre-session behaviour, "main" instance included.
        """
        key = _HTTP_SESSIONS.get(http_session_id) if http_session_id else _SESSION_KEYS.get(session)
        if key is None:
            key = _new_session_key()
            if http_session_id:
                _HTTP_SESSIONS[http_session_id] = key
            else:
                _SESSION_KEYS[session] = key
        return _current_session.set(key)

    @classmethod
    def http_mode(cls) -> bool:
        return _session_is_live is not None

    @classmethod
    def collect_departed_sessions(cls) -> list[str]:
        """Move the browsers of ended HTTP sessions to the pool. Returns those keys."""
        if _session_is_live is None:
            return []
        departed = []
        for http_id, key in list(_HTTP_SESSIONS.items()):
            if not _session_is_live(http_id):
                _HTTP_SESSIONS.pop(http_id, None)
                _session_gone(key)
                departed.append(key)
        return departed

    @classmethod
    def unbind_session(cls, token: Token) -> None:
        _current_session.reset(token)

    @classmethod
    def in_session(cls, key: str) -> Token:
        """Act as a session from a background task (the idle reaper, shutdown)."""
        return _current_session.set(key)

    @classmethod
    def session_keys(cls) -> list[str]:
        return list(_SESSIONS)

    @classmethod
    def owner_of(cls, instance_id: str) -> Optional[str]:
        """Which session holds an instance id. None when nobody does."""
        for key, state in list(_SESSIONS.items()):
            if instance_id in state.instances:
                return key
            if key != POOL_SESSION and state.current_instance_id == instance_id:
                return key
        return None

    @classmethod
    def foreign_instance(cls, instance_id: str) -> Optional[str]:
        """Refusal text when instance_id belongs to another session, else None."""
        owner = cls.owner_of(instance_id)
        if owner in (None, cls.session_key(), POOL_SESSION):
            return None
        return (
            f"instance {instance_id!r} belongs to another MCP session ({owner}) and "
            f"cannot be used from this one. That session can hand it over with "
            f"instance_release({instance_id!r})."
        )

    @classmethod
    def release_instance(cls, instance_id: str) -> None:
        """Hand a stored instance to the pool, where another session can claim it."""
        snap = cls.instances.pop(instance_id)
        _SESSIONS[POOL_SESSION].instances[instance_id] = snap

    @classmethod
    def claim_released(cls, instance_id: str) -> bool:
        """Take an instance from the pool into this session."""
        pool = _SESSIONS[POOL_SESSION].instances
        if instance_id not in pool:
            return False
        cls.instances[instance_id] = pool.pop(instance_id)
        return True

    @classmethod
    def released_ids(cls) -> list[str]:
        return sorted(_SESSIONS[POOL_SESSION].instances)

    @classmethod
    def other_session_instances(cls) -> dict[str, list[str]]:
        """Names held elsewhere, so a caller can see why a name is refused."""
        mine = cls.session_key()
        out: dict[str, list[str]] = {}
        for key, state in list(_SESSIONS.items()):
            if key in (mine, POOL_SESSION):
                continue
            names = list(state.instances)
            if state.browser is not None:
                names.append(state.current_instance_id)
            if names:
                out[key] = sorted(names)
        return out

    # ── Legacy API (backward compat) ───────────────────────────────────────

    @classmethod
    def _browser_alive(cls) -> bool:
        """Return True if the browser connection is still live.

        nodriver's `stopped` property checks `_process.returncode`, which is
        always True (i.e. "stopped") when _process is None — that happens for
        attached browsers (no subprocess was spawned). Use `connection.closed`
        for those instead.
        """
        b = cls.browser
        if b is None:
            return False
        # Attached browser: _process is None, so use CDP websocket liveness.
        if getattr(b, "_process", None) is None:
            conn = getattr(b, "connection", None)
            if conn is None:
                return False
            return not getattr(conn, "closed", True)
        # Launched browser: use the standard stopped flag.
        return not getattr(b, "stopped", True)

    @classmethod
    def is_up(cls) -> bool:
        if cls.browser is None or len(cls.tabs) == 0:
            return False
        # Check if browser process died (websocket dead, Chrome crashed, user closed window).
        # Without this check, stale references cause confusing "HTTP 500" on every subsequent call.
        if not cls._browser_alive():
            return False
        return True

    @classmethod
    def active_tab(cls) -> Tab:
        if cls.browser is not None and not cls._browser_alive():
            raise RuntimeError(
                "Browser died (Chrome closed or CDP websocket lost). Handle retained for "
                "verified cleanup: call browser_recover() then browser_launch()."
            )
        if not cls.is_up():
            raise RuntimeError("Browser not running. Call browser_launch first.")
        cls.current_last_active = time.time()
        if cls.active_tab_index >= len(cls.tabs):
            cls.active_tab_index = 0
        return cls.tabs[cls.active_tab_index]

    @classmethod
    def reset(cls) -> None:
        """Reset ONLY current instance (legacy behavior)."""
        cls.browser = None
        cls.tabs = []
        cls.active_tab_index = 0
        cls.console_logs = []
        cls.network_logs = []
        cls.network_index = {}
        cls.page_errors = []
        cls.capture_console = False
        cls.capture_network = False

    # ── Multi-instance API ─────────────────────────────────────────────────

    @classmethod
    def snapshot_current(cls) -> InstanceSnapshot:
        """Freeze current instance state into a snapshot."""
        return InstanceSnapshot(
            instance_id=cls.current_instance_id,
            browser=cls.browser,
            tabs=list(cls.tabs),
            active_tab_index=cls.active_tab_index,
            profile_dir=cls.current_profile_dir,
            desc=cls.current_desc,
            headless=cls.current_headless,
            privacy=cls.current_privacy,
            fingerprint=dict(cls.current_fingerprint),
            proxy=cls.current_proxy,
            idle_timeout=cls.current_idle_timeout,
            last_active=cls.current_last_active,
            created_at=cls.current_created_at,
            # Capture buffers are handed over BY REFERENCE, not copied. A CDP
            # handler armed on this instance keeps a direct reference to these
            # objects, so copying here would silently orphan it: the instance's
            # later events would land in a list nobody reads, while the copy the
            # user does read stops growing. Identity is the contract.
            console_logs=cls.console_logs,
            network_logs=cls.network_logs,
            network_index=cls.network_index,
            page_errors=cls.page_errors,
            capture_console=cls.capture_console,
            capture_network=cls.capture_network,
        )

    @classmethod
    def restore_from(cls, snap: InstanceSnapshot) -> None:
        """Load snapshot into current class-level state."""
        cls.current_instance_id = snap.instance_id
        cls.browser = snap.browser
        cls.tabs = list(snap.tabs)
        cls.active_tab_index = snap.active_tab_index
        cls.current_profile_dir = snap.profile_dir
        cls.current_desc = snap.desc
        cls.current_headless = snap.headless
        cls.current_privacy = snap.privacy
        cls.current_fingerprint = dict(snap.fingerprint)
        cls.current_proxy = snap.proxy
        cls.current_idle_timeout = snap.idle_timeout
        cls.current_last_active = snap.last_active
        cls.current_created_at = snap.created_at
        # Restored by reference for the same reason they are snapshotted by
        # reference — see save_current(). Rebinding to a copy here would make a
        # switch away and back silently lose everything captured in between.
        cls.console_logs = snap.console_logs
        cls.network_logs = snap.network_logs
        cls.network_index = snap.network_index
        cls.page_errors = snap.page_errors
        cls.capture_console = snap.capture_console
        cls.capture_network = snap.capture_network

    @classmethod
    def switch_to(cls, instance_id: str) -> InstanceSnapshot:
        """Make instance_id the current one. Auto-creates if absent."""
        if instance_id == cls.current_instance_id:
            return cls.snapshot_current()
        # Save current to dict
        if cls.browser is not None:
            cls.instances[cls.current_instance_id] = cls.snapshot_current()
        # Load target (or create blank)
        if instance_id in cls.instances:
            snap = cls.instances.pop(instance_id)
        else:
            snap = InstanceSnapshot(instance_id=instance_id)
        cls.reset()  # wipe class-level state first
        cls.restore_from(snap)
        return snap

    @classmethod
    def list_snapshots(cls) -> list[InstanceSnapshot]:
        """All instances including current."""
        all_snaps = list(cls.instances.values())
        # Add current as snapshot
        all_snaps.append(cls.snapshot_current())
        return all_snaps

    @classmethod
    def remove_instance(cls, instance_id: str) -> bool:
        """Remove an instance from dict (not current)."""
        if instance_id == cls.current_instance_id:
            return False
        return cls.instances.pop(instance_id, None) is not None


def set_session_liveness(check: Optional[Callable[[str], bool]]) -> None:
    """Install the HTTP transport's answer to "does this Mcp-Session-Id still exist"."""
    global _session_is_live
    _session_is_live = check


def _new_session_key() -> str:
    """Key for a new client. A new primary gets back the browser the last one left."""
    global _primary_taken, _session_counter
    if not _primary_taken:
        _primary_taken = True
        snap = _SESSIONS[POOL_SESSION].instances.pop(_SESSIONS[PRIMARY_SESSION].current_instance_id, None)
        if snap is not None:
            token = _current_session.set(PRIMARY_SESSION)
            try:
                BrowserState.restore_from(snap)
            finally:
                _current_session.reset(token)
        return PRIMARY_SESSION
    _session_counter += 1
    key = f"session-{_session_counter}"
    _SESSIONS[key] = _SessionState(current_instance_id=key)
    return key


def _pool_put(instance_id: str, snap: InstanceSnapshot) -> None:
    """Add to the pool without ever overwriting: that would lose a live Chrome's handle."""
    pool = _SESSIONS[POOL_SESSION].instances
    name, n = instance_id, 1
    while name in pool:
        n += 1
        name = f"{instance_id}-orphan{n}"
    snap.instance_id = name
    pool[name] = snap


def _session_gone(key: str) -> None:
    """A client went away: its browsers go to the pool rather than becoming orphans.

    In the pool they stay reachable for the idle reaper, shutdown, and any session
    that wants to claim them.
    """
    global _primary_taken
    state = _SESSIONS.get(key)
    if state is None:
        return
    for name, snap in list(state.instances.items()):
        _pool_put(name, snap)
    state.instances = {}
    token = _current_session.set(key)
    try:
        if state.browser is not None:
            _pool_put(state.current_instance_id, BrowserState.snapshot_current())
    finally:
        _current_session.reset(token)
    if key == PRIMARY_SESSION:
        _SESSIONS[PRIMARY_SESSION] = _SessionState()
        _primary_taken = False
    else:
        _SESSIONS.pop(key, None)
