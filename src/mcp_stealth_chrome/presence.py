"""agent_overlay: who holds a headed browser the agent drives, shown over its window (macOS).

A separate process (`_agent_overlay.py`, PyObjC from the os-cursor extra) draws a turning light
around the browser window while the agent acts, a bar saying what it does, and a lock over the
page so a stray click or scroll never reaches it. Only the bar's Take over button hands the
browser to the person, and only Resume agent hands it back. The agent's cursor can be shown
from the bar; it starts off unless the agent_cursor setting is on. Nothing is drawn inside the page.

This side writes what the agent does into `agent.json` and reads who holds the browser from
`control.json`, both in a 0700 folder of its own under $TMPDIR. While the person holds it, every
tool that would act on or read the page is refused (`gate`): they may be typing a password. A key
the person presses while the agent works pauses the agent's input for a moment (`before_input`)
and the tool's result says so, since their keys reach the page.

No third-party import here: the overlay is only started when AppKit and Quartz can be found.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

ATTR = "_mcp_agent_overlay"
POLITE = 1.5          # the agent's input waits this long after the person's last key
POLITE_MAX = 10.0     # and never longer than this in one go
IDLE_AFTER = 20.0     # with no tool call for this long the agent reads as idle and the lock goes
                      # (its steps came at most 14.7 s apart in a measured run, median 4.3 s)
HIDE_AFTER = 90.0     # with no tool call for this long the overlay fades out (not while a
                      # request for help or the person's hold shows: their buttons stay)
POINTER_EVERY = 1 / 30
READY_WAIT = 3.0      # a new overlay must say it is up within this, or the result says so
RESTARTS = 3          # an overlay that dies while its browser lives is started again this often
                      # (every attempt counts, one whose start failed too)
REVIVE_BACKOFF = 3.0  # after a failed start, no new attempt for this long: a start that keeps
                      # failing was tried on every tool call and every 0.25 s of wait_for_human
_monotonic = time.monotonic  # the backoff's clock; tests replace it
TRUE = {"1", "true", "yes", "on"}
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Tools that work without the browser the person holds, and so could carry on what they took over to
# stop: each result says they hold it.
OUTSIDE = frozenset({"spawn_browser", "switch_instance", "http_request", "http_request_retry"})

# Tools that touch neither the page nor the browser the person holds. Everything else is refused
# while they hold it, reading included: a person who takes over may be typing a password.
WHILE_PERSON_HOLDS = frozenset({
    "server_status", "list_instances", "instance_describe", "wait_for_human", "switch_instance",
    "spawn_browser", "http_request", "http_request_status", "http_request_cancel",
    "http_request_retry", "list_chrome_profiles", "profile_report", "identity_show",
})

STATUS = {
    "navigate": "opening a page", "navigate_and_snapshot": "opening a page",
    "click": "clicking", "click_text": "clicking", "click_role": "clicking",
    "click_and_wait": "clicking", "mouse_click_xy": "clicking", "click_element_offset": "clicking",
    "type_text": "typing", "humanize_type": "typing", "paste_text": "pasting text",
    "fill": "filling in a field", "fill_form": "filling in a form", "smart_fill": "filling in a form",
    "press_key": "pressing a key", "scroll": "scrolling", "scroll_to": "scrolling",
    "hover": "pointing", "select_option": "choosing an option", "check": "ticking a box",
    "uncheck": "unticking a box", "upload_file": "attaching a file", "drag_and_drop": "dragging",
    "screenshot": "looking at the page", "browser_snapshot": "reading the page",
    "accessibility_snapshot": "reading the page", "get_text": "reading the page",
    "describe_page": "reading the page", "scrape_page": "reading the page",
    "wait_for": "waiting for the page", "wait_for_url": "waiting for the page",
    "wait_for_network_idle": "waiting for the page", "tab_new": "opening a tab",
    "tab_select": "switching tabs", "tab_close": "closing a tab", "go_back": "going back",
    "go_forward": "going forward", "reload": "reloading the page", "evaluate": "running a script",
}

# What the bar may add to the status. Typed text never: it may be a password.
_DETAIL_SELECTOR = ("click", "hover", "fill", "select_option", "check", "uncheck", "scroll_to")
_DETAIL_URL = ("navigate", "navigate_and_snapshot", "tab_new")

_LIVE: list["Overlay"] = []
_NOTES: dict[str, list[str]] = {}  # per session: what the next result should say


class PersonHasBrowser(RuntimeError):
    """The person pressed Take over: input must stop."""


def state_root() -> Path:
    return Path(os.environ.get("MCP_STEALTH_OVERLAY_DIR")
                or os.path.join(tempfile.gettempdir(), "mcp-stealth-overlay"))


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read(path: Path) -> Optional[dict]:
    """A state file, never through a link, only a regular file. None when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    except OSError:
        return None
    try:
        import stat as _stat
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if fd != -1:
            os.close(fd)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) is no probe on Windows: 0 is CTRL_C_EVENT, sent through
        # GenerateConsoleCtrlEvent, and it interrupted the test run that called it.
        from .state import _pid_alive as alive
        return alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, not ours to signal


def _same_chrome(pid: int, start: Optional[str]) -> Optional[bool]:
    """Is the process on this PID still the Chrome the overlay was started for? A PID is handed
    out again, so its start time decides; None when that cannot be read."""
    if not start:
        return _pid_alive(pid)
    from . import _quit_watcher as _watcher
    now = _watcher.process_start(pid, like=start)
    if now is None:
        return False if not _pid_alive(pid) else None
    return now == start


def _call_args() -> dict:
    from . import _app
    return _app.CALL_ARGUMENTS.get() or {}


def wanted(arg: Optional[bool]) -> bool:
    if arg is not None:
        return bool(arg)
    from . import settings
    return bool(settings.get("agent_overlay"))


def unavailable() -> Optional[str]:
    """Why the overlay cannot run here, or None. Nothing is imported to find out."""
    if sys.platform != "darwin":
        return "macOS only"
    try:
        missing = [m for m in ("AppKit", "Quartz") if importlib.util.find_spec(m) is None]
    except (ImportError, ValueError):
        missing = ["AppKit"]
    if missing:
        return ("PyObjC is not installed: reinstall with the os-cursor extra, "
                "uv tool install --force 'mcp-stealth-chrome[os-cursor]'")
    return None


def _script() -> str:
    return (Path(__file__).with_name("_agent_overlay.py")).read_text(encoding="utf-8")


LIVE_SETTINGS = ("overlay_bar", "overlay_light", "overlay_lock", "overlay_hide_after", "agent_cursor")


def overlay_config(folder: Path, pid: int, cursor: bool, start: Optional[str] = None) -> dict:
    """What the overlay process starts with. It reads the settings file again while it runs, so
    a choice from its menu or `mcp-stealth-chrome settings` applies at once; a setting the
    environment fixed is passed in `fixed`, so the file never overrides it there either."""
    from . import settings
    data = settings.read_file()
    resolved = {key: settings.resolve(key, data) for key in LIVE_SETTINGS}
    fixed = {key: r["value"] for key, r in resolved.items() if r["source"].startswith("env")}
    hide_after = float(settings.resolve("overlay_hide_after", data)["value"])
    try:
        idle_after = float(os.environ.get("MCP_STEALTH_OVERLAY_IDLE_AFTER", "") or IDLE_AFTER)
    except ValueError:
        idle_after = IDLE_AFTER
    return {"state": str(folder), "pid": pid, "start": start, "server": os.getpid(), "cursor": cursor,
            "hide_after": hide_after if hide_after > 0 else HIDE_AFTER,
            "idle_after": idle_after if idle_after > 0 else IDLE_AFTER, "style": style(),
            "settings_file": str(settings.path()), "fixed": fixed}


def _spawn(folder: Path, pid: int, cursor: bool, start: Optional[str] = None) -> subprocess.Popen:
    cfg = overlay_config(folder, pid, cursor, start)
    log_fd = os.open(folder / "overlay.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | NOFOLLOW, 0o600)
    with os.fdopen(log_fd, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-", "mcp-stealth-agent-overlay", json.dumps(cfg)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
    try:
        assert proc.stdin is not None
        proc.stdin.write(_script().encode("utf-8"))
        proc.stdin.close()
    except BaseException:
        # Started but never given its code: end it, or it outlives every handle on it.
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=2.0)
        raise
    return proc


def _last_log_line(folder: Path) -> str:
    try:
        lines = (folder / "overlay.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "no log"
    return lines[-1][:200] if lines else "nothing logged"


class Overlay:
    """This server's side of one overlay: the files it shares with the overlay process."""

    def __init__(self, browser: Any, pid: int, folder: Path, proc: subprocess.Popen, cursor: bool,
                 start: Optional[str] = None):
        self.browser = browser
        self.pid = pid
        self.start = start
        self.folder = folder
        self.proc = proc
        self.cursor = cursor
        self.restarts = 0       # attempts to start it again, failed ones included
        self.revive_after = 0.0  # no attempt before this (_monotonic), after a failed one
        self.agent_file = folder / "agent.json"
        self.control_file = folder / "control.json"
        self.agent: dict = {"pid": pid, "active_at": 0.0, "busy": False, "status": "", "help": None,
                            "request_id": None, "waiting": None, "cursor": None, "click_at": 0.0,
                            "viewport": None, "window": None}
        self.control: dict = {}
        self.control_stamp: Any = None
        self.pointer_at = 0.0
        self.window_read: tuple = (None, 0.0)  # (tab, when)
        self.hide_after = HIDE_AFTER
        self.follow_task: Any = None
        self.in_flight = 0  # tool calls running on this browser
        self.used_seen = 0.0  # the person's use of the page already passed on
        self.write()

    def write(self, **kw: Any) -> None:
        self.agent.update(kw)
        with contextlib.suppress(OSError):
            _write(self.agent_file, self.agent)

    def touch(self, **kw: Any) -> None:
        self.write(active_at=time.time(), **kw)

    def read_control(self) -> dict:
        try:
            st = os.lstat(self.control_file)
        except OSError:
            return self.control
        stamp = (st.st_ino, st.st_mtime_ns)
        if stamp != self.control_stamp:
            data = _read(self.control_file)
            if data is not None:
                self.control, self.control_stamp = data, stamp
        return self.control

    def held(self) -> bool:
        """The person holds the browser, by the last word of the overlay, alive or not."""
        return self.read_control().get("holder") == "person"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def ready(self) -> bool:
        return self.alive() and self.read_control().get("overlay_pid") == self.proc.pid

    def revive(self) -> bool:
        """Start the overlay again when it died while its browser still runs: it keeps who held
        the browser. True when one runs (`recover`)."""
        return self.recover() == "running"

    def recover(self) -> str:
        """"running" (it runs, started again if its process had exited), "waiting" (not now: a
        start failed less than REVIVE_BACKOFF ago, or Chrome's identity cannot be read) or "ended"
        (its browser is gone, or RESTARTS attempts are spent, a failed start counting as one, so
        it stays ended)."""
        if self.alive():
            return "running"
        if self.restarts >= RESTARTS or not self.folder.is_dir():
            return "ended"
        if _monotonic() < self.revive_after:
            return "waiting"
        same = _same_chrome(self.pid, self.start)
        if same is False:
            return "ended"  # its browser is gone, or its PID now runs something else
        if same is None:
            self.revive_after = _monotonic() + REVIVE_BACKOFF
            return "waiting"
        self.restarts += 1
        try:
            self.proc = _spawn(self.folder, self.pid, self.cursor, self.start)
        except Exception:
            self.revive_after = _monotonic() + REVIVE_BACKOFF
            return "ended" if self.restarts >= RESTARTS else "waiting"
        return "running"

    def stop(self) -> None:
        """Ask it to end, then make sure: the server ignores SIGTERM and children inherit that,
        so the overlay resets it, and a kill follows a terminate that was not enough. The folder
        goes once the process is gone."""
        self.write(ended=True)
        for step in (None, "terminate", "kill"):
            if self.proc.poll() is not None:
                break
            if step:
                with contextlib.suppress(Exception):
                    getattr(self.proc, step)()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=1.0)
        if self.proc.poll() is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(self.folder)
        with contextlib.suppress(ValueError):
            _LIVE.remove(self)


def _handle(browser: Any) -> Optional[Overlay]:
    """The browser's overlay, running or not."""
    found = getattr(browser, ATTR, None) if browser is not None else None
    return found if isinstance(found, Overlay) else None


def of(browser: Any) -> Optional[Overlay]:
    """The browser's overlay when it runs."""
    found = _handle(browser)
    return found if found is not None and found.alive() else None


def of_tab(tab: Any) -> Optional[Overlay]:
    return of(getattr(tab, "browser", None))


def _prune(root: Path) -> None:
    """Folders of overlays whose browser is gone (a server killed before it cleaned up). The
    name carries the PID and its generation (start time), so a reused PID does not keep one."""
    from . import _quit_watcher as _watcher
    with contextlib.suppress(OSError):
        for entry in root.iterdir():
            parts = entry.name.split("-")
            if not (len(parts) >= 4 and parts[0] == "chrome" and parts[1].isdigit()):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            pid = int(parts[1])
            if _pid_alive(pid):
                now = _watcher.process_start(pid)
                if now is None or _watcher.generation(now) == parts[2]:
                    continue  # the same browser, or not known: kept
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)


async def attach(browser: Any, pid: Optional[int], *, ask: Optional[bool], headless: bool,
                 persons_browser: bool = False) -> str:
    """Start the overlay for a browser when asked; the launch result's note, or "".
    False stops one already running (a browser reused or reattached)."""
    existing = _handle(browser)
    if ask is False:
        if existing is not None:
            stop(browser)
            return " [agent_overlay: off, the overlay on this browser was stopped]"
        return ""
    if not wanted(ask):
        return ""
    if existing is not None:
        if existing.alive():
            return " [agent_overlay: on]"
        stop(browser)  # died: start a fresh one
    why = None
    if headless:
        why = "the browser is headless, so there is no window to show it on"
    elif persons_browser:
        why = ("the person opened this browser from the launcher: a lock over the window would "
               "keep them from their own tabs")
    elif not pid:
        why = "this server did not start this browser, so it does not know its process"
    else:
        why = unavailable()
    if why:
        return f" [agent_overlay: OFF, {why}]"
    root = state_root()
    if root.is_symlink():
        return f" [agent_overlay: OFF, its state folder {root} is a link]"
    from . import settings
    cursor = bool(settings.get("agent_cursor"))
    from . import _quit_watcher as _watcher
    from . import quit_watch
    start = await asyncio.to_thread(quit_watch.pid_start, browser, pid)
    folder = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        await asyncio.to_thread(_prune, root)
        folder = Path(tempfile.mkdtemp(prefix=f"chrome-{pid}-{_watcher.generation(start)}-", dir=root))
        proc = _spawn(folder, pid, cursor, start)
    except Exception as exc:
        if folder is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(folder)
        return f" [agent_overlay: OFF, it could not start ({type(exc).__name__}: {exc})]"
    overlay = Overlay(browser, pid, folder, proc, cursor, start)
    setattr(browser, ATTR, overlay)
    _LIVE.append(overlay)
    # Up when it says so: a process that started is not yet an overlay on the screen.
    end = time.time() + READY_WAIT
    while time.time() < end and overlay.alive() and not overlay.ready():
        await asyncio.sleep(0.05)
    if not overlay.ready():
        # Not up is not on: a process that never drew its windows protects nothing.
        why = (f"the overlay ended as it started: {_last_log_line(folder)}" if not overlay.alive()
               else f"the overlay did not confirm it was up within {READY_WAIT:.0f}s")
        stop(browser)
        return f" [agent_overlay: OFF, {why}]"
    from . import settings
    with contextlib.suppress(Exception):
        overlay.hide_after = float(settings.get("overlay_hide_after"))
    with contextlib.suppress(RuntimeError):
        overlay.follow_task = asyncio.get_running_loop().create_task(_follow_window(overlay))
    return on_note(style())


def on_note(look: dict) -> str:
    """What the launch result says is over the window: what the person's settings really show.
    With the bar hidden it said "a bar with Take over" all the same (PROD, 21 Nov 2026)."""
    parts = []
    if look.get("light"):
        parts.append("a light around the window while the agent acts")
    bar = look.get("bar")
    if bar == "off":
        parts.append("no bar (overlay_bar is off in the person's settings: it shows only when the agent "
                     "asks for help, so there is no Take over button)")
    elif bar == "hover":
        parts.append("a bar with Take over while the pointer is over the window")
    else:
        parts.append("a bar with Take over")
    if look.get("lock"):
        parts.append("a lock over the page while the agent works")
    held = ("" if bar == "off" else
            "; while the person holds the browser the tools that act on or read the page are refused")
    return f" [agent_overlay: on: {', '.join(parts)}{held}]"


def stop(browser: Any) -> None:
    overlay = _handle(browser)
    if overlay is not None:
        overlay.stop()
        with contextlib.suppress(Exception):
            delattr(browser, ATTR)


def stop_all() -> None:
    """At exit, with no event loop."""
    for overlay in list(_LIVE):
        with contextlib.suppress(Exception):
            overlay.stop()


def page_size(metrics: Any, window_width: Any) -> tuple:
    """The page's width and height in screen points (what window bounds use), or (None, None).

    Not the CSS layout viewport: CSS pixels shrink with the page's zoom, and at 125% the lock
    over the page came out 800x442 at an offset over a 1000x553 page, leaving 100 pt on each
    side and 111 at the top unlocked (measured, Chrome 157). The legacy layoutViewport is in
    device pixels, which zoom does not change (2000x1106 at 100% and at 125% on a 2x display);
    the display's scale is that width over the window's, rounded, since a Chrome window has no
    side border and a classic scrollbar only takes a few pixels off it."""
    device = metrics[0] if metrics else None
    dw, dh = getattr(device, "client_width", None), getattr(device, "client_height", None)
    if dw and dh and window_width:
        scale = max(1, round(float(dw) / float(window_width)))
        return float(dw) / scale, float(dh) / scale
    view = metrics[3] if metrics and len(metrics) > 3 else (metrics[1] if metrics else None)
    return getattr(view, "client_width", None), getattr(view, "client_height", None)


def page_place(width: Any, height: Any, inner_w: Any, inner_h: Any,
               override: Optional[dict] = None) -> Optional[list]:
    """[left, top, width, height] of the page inside its window, in points, or None.

    With no device metrics override the page fills the window's width under the toolbar, so it
    is centred and bottom-aligned (right on Chrome 157: 1280x900 and 500x900 windows, page at
    (0, 87)). Under one (emulate_device) Chrome draws the page at the top-left of the content
    area, 1 CSS px to 1 pt, clipped to it: iphone-15 (393x852) in a 500x900 window sat at
    (0, 87, 393, 813), where the centred guess put the lock at (53.5, 48, 393, 852), 39 pt over
    the toolbar, whose tabs and address bar must stay usable (measured). That corner is the
    insets measured before the override began (`override["insets"]`)."""
    if override and override.get("insets") and width and height:
        left, top = override["insets"]
        return [left, top, max(0.0, min(float(override["width"]), float(width) - left)),
                max(0.0, min(float(override["height"]), float(height) - top))]
    # No override, or one with no insets measured before it (no overlay was attached when
    # emulate_device set it, so nothing read where the content area starts): the centred
    # guess, as before, which is right only without an override.
    if width and height and inner_w and inner_h:
        return [max(0.0, (width - inner_w) / 2), max(0.0, height - inner_h), inner_w, inner_h]
    return None


# Device metrics overrides emulate_device set, by the tab's CDP target id (helpers.target_key, as
# every per-tab cache is keyed): {"width", "height": the emulated size in CSS px, "insets": the
# page's (left, top) in its window measured just before the first override, or None}. Under an
# override the layout metrics give the emulated size and nothing of where Chrome draws it.
_OVERRIDES: dict[str, dict] = {}


async def _read_window(tab: Any) -> tuple:
    """The CDP bounds of the window showing the tab, and the page's layout metrics."""
    from nodriver.cdp import browser as cdp_browser
    from nodriver.cdp import page as cdp_page
    target_id = getattr(getattr(tab, "target", None), "target_id", None)
    _wid, bounds = await asyncio.wait_for(
        tab.send(cdp_browser.get_window_for_target(target_id=target_id)), 1.0)
    metrics = await asyncio.wait_for(tab.send(cdp_page.get_layout_metrics()), 1.0)
    return bounds, metrics


async def before_device_override(tab: Any) -> Optional[tuple]:
    """The page's left and top insets in its window, read now, before emulate_device sets a
    device metrics override on the tab. With one already set (another preset over the first),
    the insets read before that one: the page cannot be measured under an override. None when
    no overlay is attached (no read for nothing) or the read fails."""
    from .helpers import target_key
    known = _OVERRIDES.get(target_key(tab))
    if known is not None:
        return known["insets"]
    if _handle(getattr(tab, "browser", None)) is None:
        return None
    with contextlib.suppress(Exception):
        bounds, metrics = await _read_window(tab)
        width, height = getattr(bounds, "width", None), getattr(bounds, "height", None)
        place = page_place(width, height, *page_size(metrics, width))
        if place is not None:
            return float(place[0]), float(place[1])
    return None


def device_override(tab: Any, size: Optional[tuple], insets: Optional[tuple] = None) -> None:
    """emulate_device set a device metrics override of `size` (CSS px) on the tab, with the insets
    before_device_override read; None: it cleared the override (the desktop preset)."""
    from .helpers import target_key
    key = target_key(tab)
    if size is None:
        _OVERRIDES.pop(key, None)
    else:
        _OVERRIDES[key] = {"width": float(size[0]), "height": float(size[1]), "insets": insets}


async def refresh_window(overlay: Overlay, tab: Any) -> None:
    """Which window shows the agent's tab, and where its page sits inside it."""
    from .helpers import target_key
    bounds, metrics = await _read_window(tab)
    width, height = getattr(bounds, "width", None), getattr(bounds, "height", None)
    inner_w, inner_h = page_size(metrics, width)
    window = [getattr(bounds, "left", None), getattr(bounds, "top", None), width, height]
    override = _OVERRIDES.get(target_key(tab))
    viewport = page_place(width, height, inner_w, inner_h, override)
    # A full-screen Chrome is several windows on macOS (the toolbar strip apart from the content),
    # so the overlay cannot tell full screen from the bounds alone; the browser says it here.
    state = getattr(bounds, "window_state", None)
    state = getattr(state, "value", state)
    css = metrics[3] if len(metrics) > 3 else None
    css_w = getattr(css, "client_width", None)
    if override and override.get("insets"):
        # The page's width in points is the emulated one, as placed above: page_size rounds a
        # display scale from layout metrics that then report the emulated size.
        inner_w = override["width"]
    # Page coordinates (the agent's pointer) are CSS pixels: on screen they are this many points.
    zoom = round(float(inner_w) / float(css_w), 4) if inner_w and css_w else 1.0
    measured = {"window": window if None not in window else None, "viewport": viewport,
                "window_state": str(state) if state else None, "zoom": zoom}
    if any(overlay.agent.get(key) != value for key, value in measured.items()):
        overlay.write(**measured)  # unchanged: the overlay's file stays as it is


FOLLOW_EVERY = 1.0  # seconds between window reads while the overlay shows and no tool runs


async def _follow_window(overlay: Overlay) -> None:
    """Read the agent's window and page again about once a second while the overlay shows.
    Tool calls read them too, but between two calls the person can enter full screen, show the
    bookmarks bar or zoom, and the lock over the page must not stay where the page was. The
    overlay follows moves and resizes itself, from the window's live frame. Nothing is read
    while the person holds the browser (the lock is off then) or once the overlay has faded."""
    while overlay in _LIVE:
        await asyncio.sleep(FOLLOW_EVERY)
        tab, read_at = overlay.window_read
        now = time.time()
        if tab is None or now - read_at < FOLLOW_EVERY:
            continue
        if now - float(overlay.agent.get("active_at") or 0) > overlay.hide_after or overlay.held():
            continue
        overlay.window_read = (tab, now)
        with contextlib.suppress(Exception):
            await refresh_window(overlay, tab)


# The overlay a tool call marked busy, for that call's after_tool: set in before_tool, which runs
# in the call's own context, so the same call finds it whatever instance is current by then.
_CALL_OVERLAY: contextvars.ContextVar = contextvars.ContextVar("overlay_call", default=None)


async def before_tool(name: str, browser: Any, tab: Any) -> None:
    overlay = _handle(browser)
    if overlay is None or not overlay.revive():
        return
    from . import _app
    # Busy until after_tool: a call longer than the overlay's quiet times (a wait_for of two
    # minutes) read as idle, and HIDE_AFTER took the light, the bar and the lock away mid-call.
    overlay.in_flight += 1
    _CALL_OVERLAY.set(overlay)
    _pass_on(overlay, name, float(overlay.agent.get("active_at") or 0))
    overlay.touch(status=describe(name, _app.CALL_ARGUMENTS.get()), busy=True)
    last_tab, last_at = overlay.window_read
    if tab is not None and (tab is not last_tab or time.time() - last_at > 1.0):
        overlay.window_read = (tab, time.time())
        with contextlib.suppress(Exception):
            await refresh_window(overlay, tab)


def _clock(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


def _pass_on(overlay: "Overlay", name: str, last_active: float) -> None:
    """What the agent's call must know from the person: what they did in the page while the agent
    was idle (the lock is off then), the note they left, and that they hold the browser when the
    call works without it."""
    control = overlay.read_control()
    used_at = float(control.get("person_used_at") or 0)
    if used_at > overlay.used_seen:
        overlay.used_seen = used_at
        if used_at > last_active:
            words = [w for w in control.get("person_used") or [] if w in ("clicked", "scrolled", "typed")]
            did = (", ".join(words[:-1]) + " and " + words[-1]) if len(words) > 1 else (words[0] if words else "used")
            _note(f"while the agent was idle the person {did} in the browser (last at {_clock(used_at)}); the "
                  "page may differ from what the agent read before, so read it again before acting on it.")
    if name in OUTSIDE and control.get("holder") == "person":
        overlay.write(outside_at=time.time(), outside_tool=name)
        since = float(control.get("since") or 0)
        _note(f"the person holds this browser (Take over in its bar at {_clock(since)}). {name} does not "
              "touch it, but they took over to stop the agent: go on only with what they asked for, or "
              "ask them, or call wait_for_human.")


def _done_pressed(overlay: Optional["Overlay"]) -> Optional[str]:
    """The person pressed Done on a request for help that no wait_for_human took: say so in this
    result and close the request (wait_for_human closes its own before its result gets here).
    Without it an agent that read the page instead of waiting never learned of the Done."""
    if overlay is None or not overlay.agent.get("help"):
        return None
    control = overlay.read_control()
    if control.get("answered_id") != overlay.agent.get("request_id"):
        return None
    done_at = float(control.get("help_done") or 0)
    if done_at < float(overlay.agent.get("asked_at") or 0) or done_at <= 0:
        return None
    overlay.write(help=None)
    return (f"the person pressed Done on the request for help at {_clock(done_at)}; "
            "wait_for_human would now return at once.")


def after_tool(browser: Any, tab: Any = None) -> None:
    """The call has ended: not busy any more (once no other call of it runs), and the window and
    page read again now, since the call may have opened, moved to or resized the agent's tab
    (browser_launch itself: the lock had no page to cover until the next tool call)."""
    marked = _CALL_OVERLAY.get()
    if marked is not None:
        _CALL_OVERLAY.set(None)
        marked.in_flight = max(0, marked.in_flight - 1)
        marked.touch(busy=marked.in_flight > 0)
    overlay = of(browser)
    if overlay is None:
        return
    if overlay is not marked:
        overlay.touch(busy=overlay.in_flight > 0)
    if tab is not None:
        overlay.window_read = (tab, time.time())
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            task = loop.create_task(_refresh_quietly(overlay, tab))
            _REFRESHES.add(task)
            task.add_done_callback(_REFRESHES.discard)


_REFRESHES: set = set()  # refreshes started after a call, kept until done


async def _refresh_quietly(overlay: "Overlay", tab: Any) -> None:
    with contextlib.suppress(Exception):
        await refresh_window(overlay, tab)


def refusal(name: str, browser: Any) -> Optional[str]:
    """Error text when the person holds this browser and the tool would act on or read it.
    Fails closed: an overlay that died while they held it still holds it."""
    overlay = _handle(browser)
    if overlay is None or name in WHILE_PERSON_HOLDS:
        return None
    if not overlay.alive():
        overlay.revive()
    if not overlay.held():
        return None
    if not overlay.alive():
        if name == "browser_launch" and _call_args().get("agent_overlay") is False:
            return None  # the way out once nothing can show Resume agent
        return ("Error: the person took over this browser, and its overlay has stopped and could "
                f"not be started again ({_last_log_line(overlay.folder)}), so they cannot press "
                f"Resume agent. {name} did nothing. Ask the person; once they say they are done, "
                "browser_launch(agent_overlay=False) releases the browser.")
    since = overlay.read_control().get("since")
    when = time.strftime(" at %H:%M:%S", time.localtime(since)) if since else ""
    overlay.write(told_at=time.time(), told_tool=name)  # the bar tells the person it was told
    return ("Error: the person took over this browser (Take over in the bar over its window"
            f"{when}). {name} did nothing. The agent may neither act on nor read the page until "
            "they press Resume agent: call wait_for_human to wait for that, or ask them.")


def describe(name: str, args: Optional[dict]) -> str:
    """The bar's status for a tool call: what it does and, where it helps the person follow it,
    on what (a host, a button's text, a selector). Never what is typed."""
    from urllib.parse import urlsplit
    args = args or {}
    text = STATUS.get(name, name.replace("_", " "))
    detail = None
    if name in _DETAIL_URL and args.get("url"):
        with contextlib.suppress(Exception):
            detail = urlsplit(str(args["url"])).hostname
    elif name == "click_text" and args.get("text"):
        detail = f"\u201c{str(args['text'])[:40]}\u201d"
    elif name == "click_role" and args.get("name"):
        detail = f"\u201c{str(args['name'])[:40]}\u201d"
    elif name == "press_key" and args.get("key"):
        detail = str(args["key"])[:20]
    elif name in _DETAIL_SELECTOR and args.get("selector"):
        detail = str(args["selector"])[:40]
    if detail:
        text = {"opening a page": "opening", "pressing a key": "pressing",
                "filling in a field": "filling in", "ticking a box": "ticking",
                "unticking a box": "unticking", "choosing an option": "choosing an option in",
                }.get(text, text) + f" {detail}"
    return text[:1].upper() + text[1:]


def _note(text: str) -> None:
    from .state import BrowserState
    with contextlib.suppress(Exception):
        notes = _NOTES.setdefault(BrowserState.session_key(), [])
        if text not in notes:
            notes.append(text)


def result_note(session_key: str, result: str, browser: Any = None) -> str:
    """The notes for this call's result, and a Done no wait took: every result passes here, also
    those of tools that skip the before-hooks (wait_for_human, web_search)."""
    notes = list(_NOTES.pop(session_key, None) or [])
    overlay = _handle(browser) if browser is not None else None
    done = _done_pressed(overlay)
    if done:
        notes.append(done)
    if not notes:
        return result
    return f"{result} [agent_overlay: {' '.join(notes)}]"


async def before_input(tab: Any) -> None:
    """Before input reaches the page: refuse once the person holds the browser, and let a
    person who is typing finish first."""
    overlay = _handle(getattr(tab, "browser", None))
    if overlay is None:
        return
    if not overlay.alive():
        overlay.revive()
    control = overlay.read_control()
    if control.get("holder") == "person":
        overlay.write(told_at=time.time(), told_tool="input")
        raise PersonHasBrowser(
            "the person pressed Take over in the bar over the browser window; the agent's "
            "input stopped there. Call wait_for_human to wait for Resume agent.")
    last = float(control.get("person_last_input") or 0)
    if time.time() - last >= POLITE or not overlay.alive():
        return
    started = time.time()
    overlay.write(waiting="polite")
    try:
        while time.time() - started < POLITE_MAX:
            control = overlay.read_control()
            if control.get("holder") == "person":
                overlay.write(told_at=time.time(), told_tool="input")
                raise PersonHasBrowser(
                    "the person pressed Take over in the bar over the browser window; the "
                    "agent's input stopped there. Call wait_for_human to wait for Resume agent.")
            if time.time() - float(control.get("person_last_input") or 0) >= POLITE:
                break
            await asyncio.sleep(0.1)
    finally:
        overlay.touch(waiting=None)
    _note(f"the person typed in the browser while this ran, so the agent paused "
          f"{time.time() - started:.1f}s; their keys reached the page too, so check what it holds.")


def note_pointer(tab: Any, x: float, y: float, pressed: bool = False) -> None:
    """Where the agent points, in the page's coordinates, for the cursor the bar can show."""
    overlay = of_tab(tab)
    if overlay is None:
        return
    now = time.time()
    if not pressed and now - overlay.pointer_at < POINTER_EVERY:
        return
    overlay.pointer_at = now
    if pressed:
        overlay.touch(cursor=[x, y], click_at=now)
    else:
        overlay.touch(cursor=[x, y])


def ask(browser: Any, objective: str, request_id: Optional[str] = None) -> Optional[float]:
    """Show the person a request for help (amber, with Done), including its correlation id."""
    overlay = of(browser)
    if overlay is None:
        return None
    asked = time.time()
    overlay.touch(help=objective[:200], request_id=request_id, asked_at=asked)
    return asked


def shows_request(browser: Any, shown_at: Optional[float], request_id: Optional[str] = None) -> bool:
    """Whether the bar still shows the same request, not a newer replacement."""
    overlay = of(browser)
    return (overlay is not None and shown_at is not None
            and float(overlay.agent.get("asked_at") or 0) == float(shown_at)
            and (request_id is None or overlay.agent.get("request_id") == request_id))




def show_again(browser: Any, objective: str, request_id: Optional[str] = None) -> Optional[float]:
    """Show a request again, retaining its correlation id."""
    overlay = _handle(browser)
    if overlay is None or not (overlay.alive() or overlay.revive()):
        return None
    return ask(browser, objective, request_id=request_id)


def keep_running(browser: Any) -> Optional[str]:
    """The browser's overlay, started again first if its process exited (`Overlay.recover`; the
    new process reads agent.json and control.json, so the request and the holder stay):
    "running", "waiting" (it may come back, not yet), "ended", or None with no overlay."""
    overlay = _handle(browser)
    return None if overlay is None else overlay.recover()


def bar_state(browser: Any, shown_at: Optional[float], request_id: Optional[str] = None) -> str:
    """Where a specific request's bar stands: shown, replaced, or gone."""
    overlay = _handle(browser)
    if overlay is None or shown_at is None or not (overlay.alive() or overlay.revive()):
        return "gone"
    if (float(overlay.agent.get("asked_at") or 0) != float(shown_at)
            or (request_id is not None and overlay.agent.get("request_id") != request_id)):
        return "replaced"
    return "shown"


def answered(browser: Any, since: float, request_id: Optional[str] = None) -> Optional[str]:
    """What the person did with the specific request's bar since `since`."""
    overlay = _handle(browser)
    if overlay is None or (request_id is not None and overlay.agent.get("request_id") != request_id):
        return None
    control = overlay.read_control()
    if control.get("answered_id") != request_id:
        return None
    if float(control.get("help_done") or 0) >= since:
        return "done"
    if control.get("holder") == "agent" and control.get("reason") == "button" \
            and float(control.get("since") or 0) >= since:
        return "resumed"
    return None


def waiting_for_person(browser: Any, waiting: bool) -> None:
    """While wait_for_human runs, the bar says the agent is waiting for them."""
    overlay = _handle(browser)
    if overlay is not None:
        overlay.write(waiting_human=waiting)


def holds(browser: Any) -> bool:
    overlay = _handle(browser)
    return overlay is not None and overlay.held()


def end_help(browser: Any, request_id: Optional[str] = None) -> None:
    overlay = of(browser)
    if overlay is not None and (request_id is None or overlay.agent.get("request_id") == request_id):
        overlay.touch(help=None, request_id=None)


BAR_MODES = ("full", "compact", "hover", "off")


def style() -> dict:
    """How the overlay looks: settings.py's overlay_bar (full, compact, hover, off), overlay_light
    and overlay_lock, from the environment, the settings file or the default.

    The bar's button is the only way to take the browser over, so a request for help (Done) and
    the person's hold (Resume agent) always show it, and with the bar off the lock is off too:
    a lock with no Take over in reach would leave the person shut out of the page."""
    from . import settings
    bar = settings.get("overlay_bar")
    if bar not in BAR_MODES:
        bar = "full"
    lock = bool(settings.get("overlay_lock")) and bar != "off"
    return {"bar": bar, "light": bool(settings.get("overlay_light")), "lock": lock}


def status(browser: Any) -> dict:
    overlay = _handle(browser)
    if overlay is None:
        return {"on": False}
    control = overlay.read_control()
    return {"on": overlay.alive(), "overlay_pid": overlay.proc.pid, "ready": overlay.ready(),
            "restarts": overlay.restarts, "holder": control.get("holder", "agent"),
            "cursor_shown": bool(control.get("show_cursor"))}
