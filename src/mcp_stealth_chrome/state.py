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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nodriver import Browser, Tab

HOME = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or ".")
PROFILES_ROOT = HOME / ".mcp-stealth" / "profiles"
PROFILE_DIR = HOME / ".mcp-stealth" / "profile"  # legacy default ("main" instance)
SCREENSHOT_DIR = HOME / ".mcp-stealth" / "screenshots"
EXPORT_DIR = HOME / ".mcp-stealth" / "exports"
STORAGE_STATE_DIR = HOME / ".mcp-stealth" / "storage-states"

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
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pfx86}\Google\Chrome\Application\chrome.exe",
            rf"{localappdata}\Google\Chrome\Application\chrome.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Chromium\Application\chromium.exe",
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


def chrome_user_data_root() -> Optional[Path]:
    """Find where Chrome stores user profiles. Returns None if no Chrome installed."""
    import sys
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [
            home / "Library" / "Application Support" / "Google" / "Chrome",
            home / "Library" / "Application Support" / "Chromium",
            home / "Library" / "Application Support" / "Microsoft Edge",
            home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
        ]
    elif sys.platform.startswith("linux"):
        candidates = [
            home / ".config" / "google-chrome",
            home / ".config" / "chromium",
            home / ".config" / "microsoft-edge",
            home / ".config" / "BraveSoftware" / "Brave-Browser",
        ]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates = [
            local / "Google" / "Chrome" / "User Data",
            local / "Chromium" / "User Data",
            local / "Microsoft" / "Edge" / "User Data",
            local / "BraveSoftware" / "Brave-Browser" / "User Data",
        ]
    for c in candidates:
        if c.exists() and (c / "Local State").exists():
            return c
    return None


def is_chrome_profile_locked(profile_path: Path) -> bool:
    """Check if Chrome is currently using a profile (via SingletonLock)."""
    lock = profile_path / "SingletonLock"
    try:
        return lock.exists() or lock.is_symlink()
    except OSError:
        return False


def ensure_dirs() -> None:
    for d in (PROFILE_DIR, PROFILES_ROOT, SCREENSHOT_DIR, EXPORT_DIR, STORAGE_STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def clean_profile_state(profile_dir: Path | str | None = None) -> None:
    """Prevent 'Restore pages?' dialog by marking previous exit as clean."""
    pdir = Path(profile_dir) if profile_dir else PROFILE_DIR
    prefs = pdir / "Default" / "Preferences"
    if prefs.exists():
        try:
            data = json.loads(prefs.read_text())
            profile = data.get("profile", {})
            if profile.get("exit_type") != "Normal":
                profile["exit_type"] = "Normal"
                profile["exited_cleanly"] = True
                data["profile"] = profile
                prefs.write_text(json.dumps(data))
        except Exception:
            pass
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
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    last_active: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    console_logs: list[dict] = field(default_factory=list)
    network_logs: list[dict] = field(default_factory=list)
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
        return self.browser is not None and len(self.tabs) > 0


class BrowserState:
    """Class-level singleton holding CURRENT instance + dict of others.

    `browser`, `tabs`, etc. always reflect the active instance. Other instances
    are stored in `instances` dict as snapshots — switching swaps snapshots in.
    """

    # Current instance state (class-level — existing code writes here directly)
    browser: Optional[Browser] = None
    tabs: list[Tab] = []
    active_tab_index: int = 0
    console_logs: list[dict] = []
    network_logs: list[dict] = []
    page_errors: list[str] = []
    capture_console: bool = False
    capture_network: bool = False

    # Multi-instance: other instances stored here, plus metadata for current
    current_instance_id: str = "main"
    current_profile_dir: Optional[Path] = None
    current_idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    current_last_active: float = time.time()
    current_created_at: float = time.time()

    # Last mouse position — enables realistic cursor continuation (no teleports).
    # Updated by tools that move the mouse.
    last_mouse_xy: dict[str, Optional[int]] = {"x": None, "y": None}

    instances: dict[str, InstanceSnapshot] = {}  # does NOT include current
    _reaper_task = None  # asyncio.Task

    # ── Legacy API (backward compat) ───────────────────────────────────────

    @classmethod
    def is_up(cls) -> bool:
        return cls.browser is not None and len(cls.tabs) > 0

    @classmethod
    def active_tab(cls) -> Tab:
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
            idle_timeout=cls.current_idle_timeout,
            last_active=cls.current_last_active,
            created_at=cls.current_created_at,
            console_logs=list(cls.console_logs),
            network_logs=list(cls.network_logs),
            page_errors=list(cls.page_errors),
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
        cls.current_idle_timeout = snap.idle_timeout
        cls.current_last_active = snap.last_active
        cls.current_created_at = snap.created_at
        cls.console_logs = list(snap.console_logs)
        cls.network_logs = list(snap.network_logs)
        cls.page_errors = list(snap.page_errors)
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
