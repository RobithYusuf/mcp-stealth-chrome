"""agent_overlay: who holds a headed browser the agent drives, shown over its window (macOS).

A separate process (`_agent_overlay.py`, PyObjC from the os-cursor extra) draws a turning light
around the browser window while the agent acts, a bar saying what it does, and a lock over the
page so a stray click or scroll never reaches it. Only the bar's Take over button hands the
browser to the person, and only Resume agent hands it back. The agent's cursor can be shown
from the bar; it is off unless MCP_STEALTH_AGENT_CURSOR=1. Nothing is drawn inside the page.

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
HIDE_AFTER = 90.0     # with no tool call for this long the overlay fades out
POINTER_EVERY = 1 / 30
TRUE = {"1", "true", "yes", "on"}

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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def wanted(arg: Optional[bool]) -> bool:
    if arg is not None:
        return bool(arg)
    return os.environ.get("MCP_STEALTH_AGENT_OVERLAY", "").strip().lower() in TRUE


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


class Overlay:
    """This server's side of one overlay: the files it shares with the overlay process."""

    def __init__(self, browser: Any, pid: int, folder: Path, proc: subprocess.Popen):
        self.browser = browser
        self.pid = pid
        self.folder = folder
        self.proc = proc
        self.agent_file = folder / "agent.json"
        self.control_file = folder / "control.json"
        self.agent: dict = {"pid": pid, "active_at": 0.0, "status": "", "help": None,
                            "waiting": None, "cursor": None, "click_at": 0.0,
                            "viewport": None, "window": None}
        self.control: dict = {}
        self.control_stamp: Any = None
        self.pointer_at = 0.0
        self.window_read: tuple = (None, 0.0)  # (tab, when)
        self.write()

    def write(self, **kw: Any) -> None:
        self.agent.update(kw)
        with contextlib.suppress(OSError):
            _write(self.agent_file, self.agent)

    def touch(self, **kw: Any) -> None:
        self.write(active_at=time.time(), **kw)

    def read_control(self) -> dict:
        try:
            st = os.stat(self.control_file)
        except OSError:
            return self.control
        stamp = (st.st_ino, st.st_mtime_ns)
        if stamp != self.control_stamp:
            with contextlib.suppress(OSError, ValueError):
                self.control = json.loads(self.control_file.read_text(encoding="utf-8"))
                self.control_stamp = stamp
        return self.control

    def person_holds(self) -> bool:
        return self.alive() and self.read_control().get("holder") == "person"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        self.write(ended=True)
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=1.0)
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
        with contextlib.suppress(OSError):
            shutil.rmtree(self.folder)
        with contextlib.suppress(ValueError):
            _LIVE.remove(self)


def of(browser: Any) -> Optional[Overlay]:
    found = getattr(browser, ATTR, None) if browser is not None else None
    return found if isinstance(found, Overlay) and found.alive() else None


def of_tab(tab: Any) -> Optional[Overlay]:
    return of(getattr(tab, "browser", None))


def attach(browser: Any, pid: Optional[int], *, ask: Optional[bool], headless: bool,
           persons_browser: bool = False) -> str:
    """Start the overlay for a browser when asked; the launch result's note, or ""."""
    if not wanted(ask):
        return ""
    if of(browser) is not None:
        return " [agent_overlay: on]"
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
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        folder = Path(tempfile.mkdtemp(prefix=f"chrome-{pid}-", dir=root))
        cfg = {"state": str(folder), "pid": pid, "server": os.getpid(),
               "cursor": os.environ.get("MCP_STEALTH_AGENT_CURSOR", "").strip().lower() in TRUE,
               "hide_after": HIDE_AFTER}
        with open(folder / "overlay.log", "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-", "mcp-stealth-agent-overlay", json.dumps(cfg)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True)
        assert proc.stdin is not None
        proc.stdin.write(_script().encode("utf-8"))
        proc.stdin.close()
    except Exception as exc:
        return f" [agent_overlay: OFF, it could not start ({type(exc).__name__}: {exc})]"
    overlay = Overlay(browser, pid, folder, proc)
    setattr(browser, ATTR, overlay)
    _LIVE.append(overlay)
    return (" [agent_overlay: on: a light around the window while the agent acts and a bar "
            "with Take over; while the person holds the browser the tools that act on or read "
            "the page are refused]")


def stop(browser: Any) -> None:
    overlay = getattr(browser, ATTR, None) if browser is not None else None
    if isinstance(overlay, Overlay):
        overlay.stop()
        with contextlib.suppress(Exception):
            delattr(browser, ATTR)


def stop_all() -> None:
    """At exit, with no event loop."""
    for overlay in list(_LIVE):
        with contextlib.suppress(Exception):
            overlay.stop()


async def refresh_window(overlay: Overlay, tab: Any) -> None:
    """Which window shows the agent's tab, and where its page sits inside it."""
    from nodriver.cdp import browser as cdp_browser
    from nodriver.cdp import page as cdp_page
    target_id = getattr(getattr(tab, "target", None), "target_id", None)
    _wid, bounds = await asyncio.wait_for(
        tab.send(cdp_browser.get_window_for_target(target_id=target_id)), 1.0)
    metrics = await asyncio.wait_for(tab.send(cdp_page.get_layout_metrics()), 1.0)
    view = metrics[3] if len(metrics) > 3 else metrics[1]  # css layout viewport
    width, height = getattr(bounds, "width", None), getattr(bounds, "height", None)
    inner_w, inner_h = getattr(view, "client_width", None), getattr(view, "client_height", None)
    window = [getattr(bounds, "left", None), getattr(bounds, "top", None), width, height]
    viewport = None
    if width and height and inner_w and inner_h:
        viewport = [max(0.0, (width - inner_w) / 2), max(0.0, height - inner_h), inner_w, inner_h]
    overlay.write(window=window if None not in window else None, viewport=viewport)


async def before_tool(name: str, browser: Any, tab: Any) -> None:
    overlay = of(browser)
    if overlay is None:
        return
    from . import _app
    overlay.touch(status=describe(name, _app.CALL_ARGUMENTS.get()))
    last_tab, last_at = overlay.window_read
    if tab is not None and (tab is not last_tab or time.time() - last_at > 1.0):
        overlay.window_read = (tab, time.time())
        with contextlib.suppress(Exception):
            await refresh_window(overlay, tab)


def after_tool(browser: Any) -> None:
    overlay = of(browser)
    if overlay is not None:
        overlay.touch()


def refusal(name: str, browser: Any) -> Optional[str]:
    """Error text when the person holds this browser and the tool would act on or read it."""
    overlay = of(browser)
    if overlay is None or name in WHILE_PERSON_HOLDS or not overlay.person_holds():
        return None
    since = overlay.read_control().get("since")
    when = time.strftime(" at %H:%M:%S", time.localtime(since)) if since else ""
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


def result_note(session_key: str, result: str) -> str:
    notes = _NOTES.pop(session_key, None)
    if not notes:
        return result
    return f"{result} [agent_overlay: {' '.join(notes)}]"


async def before_input(tab: Any) -> None:
    """Before input reaches the page: refuse once the person holds the browser, and let a
    person who is typing finish first."""
    overlay = of_tab(tab)
    if overlay is None:
        return
    control = overlay.read_control()
    if control.get("holder") == "person":
        raise PersonHasBrowser(
            "the person pressed Take over in the bar over the browser window; the agent's "
            "input stopped there. Call wait_for_human to wait for Resume agent.")
    last = float(control.get("person_last_input") or 0)
    if time.time() - last >= POLITE:
        return
    started = time.time()
    overlay.write(waiting="polite")
    try:
        while time.time() - started < POLITE_MAX:
            control = overlay.read_control()
            if control.get("holder") == "person":
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


def ask(browser: Any, objective: str) -> Optional[float]:
    """Show the person a request for help (amber, with Done). The time it was asked, or None."""
    overlay = of(browser)
    if overlay is None:
        return None
    asked = time.time()
    overlay.touch(help=objective[:200], asked_at=asked)
    return asked


def answered(browser: Any, since: float) -> Optional[str]:
    """What the person did with the bar since `since`: "done", "resumed", or None."""
    overlay = of(browser)
    if overlay is None:
        return None
    control = overlay.read_control()
    if float(control.get("help_done") or 0) >= since:
        return "done"
    if control.get("holder") == "agent" and control.get("reason") == "button" \
            and float(control.get("since") or 0) >= since:
        return "resumed"
    return None


def holds(browser: Any) -> bool:
    overlay = of(browser)
    return overlay is not None and overlay.person_holds()


def end_help(browser: Any) -> None:
    overlay = of(browser)
    if overlay is not None:
        overlay.touch(help=None)


def status(browser: Any) -> dict:
    overlay = of(browser)
    if overlay is None:
        return {"on": False}
    control = overlay.read_control()
    return {"on": True, "overlay_pid": overlay.proc.pid, "holder": control.get("holder", "agent"),
            "cursor_shown": bool(control.get("show_cursor"))}
