"""mcp-stealth-chrome — FastMCP server entry + all tool implementations.

Architecture parallels mcp-camoufox (Node/Firefox sister package):
- single-file tool registry for easy maintenance
- same tool names, same parameters, same ref system
- nodriver CDP direct + async throughout
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

import nodriver
from nodriver import Browser, Config, Tab
from nodriver.cdp import dom as cdp_dom, runtime as cdp_runtime

from . import __version__
from . import patches as _patches
_patches.apply_all()
from .key_outcome import element_unread
from .helpers import (
    OPEN_DIALOGS,
    dialog_note,
    err,
    get_title,
    get_url,
    is_secret_name,
    mask_body,
    mask_headers,
    mask_request_entry,
    mask_url,
    masked_value,
    ok,
    PageReadError,
    parse_json,
    select_tab,
    target_key,
    transport_lost,
)
from .identity import (
    chrome_version,
    default_accept_language,
    key_event_params,
    navigator_languages,
    navigator_platform,
    os_family,
    strip_headless,
    ua_metadata_fields,
)
from .refs import (
    apply_isolated,
    close_connection,
    ensure_in_root_view,
    evaluate_isolated,
    evaluate_parts,
    forget_refs,
    new_object_group,
    outer_blocker,
    release_group,
    resolve_ref,
)
from . import dock_icon
from . import profile_gc
from . import profile_version
from . import cookie_migration
from . import quit_watch
from . import tab_record
from .state import data_root as state_data_root
from .workflow import WorkflowError, run_workflow
from .humanize import (
    CURRENT_GESTURE, Gesture, humanized_approach, humanized_move, humanized_mouse_press,
    humanized_scroll, move_pointer,
)
from .state import (
    PRIMARY_SESSION,
    SESSION_GONE_HOOKS,
    DEFAULT_IDLE_TIMEOUT,
    IDENTITIES_DIR,
    IDLE_REAPER_INTERVAL,
    PROFILE_DIR,
    PROFILES_ROOT,
    PRIVACY_PROFILE_ROOT,
    STORAGE_STATE_DIR,
    BrowserState,
    InstanceSnapshot,
    chrome_install_hint,
    chrome_lock_holder_pid,
    chrome_user_data_root,
    chrome_user_data_roots,
    clean_profile_state,
    ensure_dirs,
    find_chrome_binary,
    find_external_chrome_pids,
    is_chrome_profile_locked,
    per_process_profile,
    resolve_default_profile,
)

# Hard ceiling on `nodriver.start()` — without this a locked profile or hung
# Chrome subprocess hangs the entire MCP session. Override via env var.
BROWSER_LAUNCH_TIMEOUT = int(os.environ.get("BROWSER_LAUNCH_TIMEOUT", "45"))
BROWSER_NAV_TIMEOUT = int(os.environ.get("BROWSER_NAV_TIMEOUT", "20"))
# Per-tool CDP action ceiling. Pages with stuck JS / blocked service workers
# can keep CDP commands waiting indefinitely otherwise — this ensures each
# tool call either succeeds or returns a clean timeout error within N seconds.
TOOL_ACTION_TIMEOUT = int(os.environ.get("TOOL_ACTION_TIMEOUT", "30"))
# Capture buffers grow for as long as capture is armed, and a long session on a
# chatty site will happily consume the process. These caps make them ring
# buffers: the newest events are the ones worth keeping, and dropping the oldest
# is preferable to an unbounded climb nobody notices until it matters.
CAPTURE_MAX_EVENTS = int(os.environ.get("CAPTURE_MAX_EVENTS", "5000"))
CAPTURE_MAX_INDEXED = int(os.environ.get("CAPTURE_MAX_INDEXED", "1000"))
# Named storage snapshots are keyed by a caller-supplied name, so a loop that
# varies the name would otherwise retain every snapshot for the process lifetime.
_STORAGE_SNAPSHOT_LIMIT = int(os.environ.get("STORAGE_SNAPSHOT_LIMIT", "50"))


def _trim_capture(buffer: list) -> None:
    """Keep a capture list bounded, newest-first-out-last."""
    overflow = len(buffer) - CAPTURE_MAX_EVENTS
    if overflow > 0:
        del buffer[:overflow]


def _trim_index(index: dict) -> None:
    """Keep the request index bounded; dicts preserve insertion order."""
    overflow = len(index) - CAPTURE_MAX_INDEXED
    if overflow > 0:
        for key in list(index)[:overflow]:
            index.pop(key, None)


def _window_state_path(instance_id: str) -> Path:
    """Where this instance's last window placement is remembered."""
    from .state import WINDOW_STATE_DIR
    return _direct_child(WINDOW_STATE_DIR, f"{instance_id}.json", "instance_id")


async def _read_window_bounds(tab) -> Optional[dict]:
    """Current window rect via CDP, or None if it cannot be read."""
    try:
        from nodriver.cdp import browser as cdp_browser
        target_id = getattr(getattr(tab, "target", None), "target_id", None)
        window_id, bounds = await _wait(
            tab.send(cdp_browser.get_window_for_target(target_id=target_id)),
            timeout=5, what="get_window_for_target")
        state = getattr(bounds, "window_state", None)
        return {
            "left": getattr(bounds, "left", None),
            "top": getattr(bounds, "top", None),
            "width": getattr(bounds, "width", None),
            "height": getattr(bounds, "height", None),
            "window_state": getattr(state, "value", None) or (str(state) if state else "normal"),
            "window_id": window_id,
        }
    except Exception:
        return None


async def _save_window_bounds(instance_id: str, tab) -> bool:
    """Remember where this window currently sits. Best-effort."""
    rect = await _read_window_bounds(tab)
    if not rect or not rect.get("width") or not rect.get("height"):
        return False
    try:
        ensure_dirs()
        path = _window_state_path(instance_id)
        payload = {k: rect[k] for k in ("left", "top", "width", "height", "window_state")}
        payload["saved_at"] = int(time.time())
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


async def _restore_window_bounds(instance_id: str, tab) -> dict:
    """Put the window back where it was, and report what actually happened.

    The monitor it was on may be gone. Rather than querying the display layout —
    which needs a platform API and can still be stale — the saved rect is applied
    and then read BACK: Chrome clamps a window to a connected display, so a
    read-back that does not match is itself the signal that the screen changed.
    That makes the check self-validating and impossible to get wrong by guessing.
    """
    result = {"restored": False, "reason": None, "requested": None, "actual": None}
    try:
        path = _window_state_path(instance_id)
        if not path.exists():
            result["reason"] = "no saved placement"
            return result
        saved = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        result["reason"] = f"unreadable saved placement: {type(e).__name__}"
        return result

    width, height = saved.get("width") or 0, saved.get("height") or 0
    if width < 200 or height < 200:
        # The exact corruption wipe_window_state() exists to cure: a 0x0 or
        # tiny rect produces a window that is technically open and invisible.
        result["reason"] = f"refused implausible size {width}x{height}"
        return result
    if (saved.get("window_state") or "normal") != "normal":
        # Minimised/fullscreen are states, not placements; restoring them would
        # hide the window or take over the screen unasked.
        result["reason"] = f"saved state was {saved.get('window_state')}, not restoring"
        return result

    try:
        from nodriver.cdp import browser as cdp_browser
        target_id = getattr(getattr(tab, "target", None), "target_id", None)
        window_id, _ = await _wait(
            tab.send(cdp_browser.get_window_for_target(target_id=target_id)),
            timeout=5, what="get_window_for_target")
        want = cdp_browser.Bounds(
            left=int(saved["left"]), top=int(saved["top"]),
            width=int(width), height=int(height))
        result["requested"] = {"left": want.left, "top": want.top,
                               "width": want.width, "height": want.height}
        await _wait(tab.send(cdp_browser.set_window_bounds(window_id=window_id, bounds=want)),
                    timeout=5, what="set_window_bounds")
        got = await _wait(tab.send(cdp_browser.get_window_bounds(window_id=window_id)),
                          timeout=5, what="get_window_bounds")
        actual = {"left": got.left, "top": got.top, "width": got.width, "height": got.height}
        result["actual"] = actual
        moved = abs((actual["left"] or 0) - want.left) > 40 or abs((actual["top"] or 0) - want.top) > 40
        result["restored"] = True
        result["reason"] = (
            "saved monitor is gone — Chrome clamped the window onto a connected "
            "display, so it is visible rather than off-screen"
            if moved else "restored to the saved position"
        )
        result["clamped"] = moved
    except Exception as e:
        result["reason"] = f"could not apply placement: {type(e).__name__}: {e}"
    return result


async def _debug_port_closed(browser: Any, grace: float) -> bool:
    """True once nothing is listening on an attached browser's debugging port."""
    config = getattr(browser, "config", None)
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    if not host or not port:
        return False
    deadline = time.monotonic() + max(grace, 3.0)
    while True:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
            writer.close()
        except (OSError, asyncio.TimeoutError):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


def _process_exists(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) is no probe on Windows: 0 is CTRL_C_EVENT, sent to the process group
        # through GenerateConsoleCtrlEvent. It read every Chrome as still running after close.
        from .state import _pid_alive
        return _pid_alive(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


async def _discard_temp_profile(browser: Any) -> None:
    """Remove nodriver's temp profile once its Chrome is dead.

    nodriver leaves it for an exit hook, so a long-lived server gains one per launch, and
    at exit 0.48.1 stops after the first because it prints to the closed MCP stdout.
    """
    config = getattr(browser, "config", None)
    if config is None:
        return
    for _ in range(3):
        _remove_temp_profile(config)
        if not Path(str(getattr(config, "user_data_dir", "") or "")).exists():
            return
        await asyncio.sleep(0.2)  # a helper still exiting can write one last file


def _process_command(pid: int) -> Optional[str]:
    """The command line of a running PID, or None when it cannot be read."""
    if sys.platform == "win32":
        # No ps there. The runner's is Git Bash's, which ran 1.1 s to its 3 s timeout here, on
        # the event loop, at every close; a person's Windows has none, so nothing was checked.
        from .state import process_table
        for row_pid, _ppid, command in process_table() or []:
            if row_pid == pid:
                return command or None
        return None
    try:
        r = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    return (r.stdout or "").strip() or None


def _pid_recycled(pid: int, browser: Any) -> bool:
    """Whether a recorded PID now belongs to another program.

    A handle can outlive its Chrome after a crash, and the OS hands the number out again.
    Signalling it then kills something innocent. True only on positive evidence: a live
    process whose command line does not name this browser's profile.
    """
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    if not data_dir:
        return False
    command = _process_command(pid)
    return command is not None and f"--user-data-dir={data_dir}" not in command


def _descendant_pids(pid: int, data_dir: Optional[str] = None,
                     table: Optional[list[tuple[int, int, str]]] = None) -> list[int]:
    """Everything this browser started under a PID, found while the parent links still exist.

    A launcher script that does not exec is the recorded PID, with Chrome as its child.
    Ending only the script leaves Chrome running, reparented to init, and holding its profile.

    With data_dir, only processes whose command line names that profile count. Chrome also
    starts programs that are not part of the browser: on macOS it wakes GoogleUpdater, the
    machine-wide updater, which outlives the browser by a few seconds. Counted as family, it
    made a clean close read as "did not end the process" and was sent SIGTERM mid-update.

    Windows has no ps; the table comes from a Toolhelp snapshot there. Without it the helpers
    were never waited for, and a privacy profile was deleted while they still held its files.
    `table` is a process_table() the caller already read, to compare against the same moment.
    """
    from .state import process_table
    rows = process_table() if table is None else table
    if rows is None:
        return []
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for child, parent, command in rows:
        if child != parent:
            children.setdefault(parent, []).append(child)
        commands[child] = command
    found: list[int] = []
    # Windows keeps a parent's number after it exits and hands the number out again, so the
    # links can form a cycle there.
    seen = {pid}
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child in seen:
                continue
            seen.add(child)
            pending.append(child)
            if not data_dir or str(data_dir) in commands.get(child, ""):
                found.append(child)
    return found


async def _all_gone(pids: list[int], within: float) -> bool:
    """Whether every PID ended inside `within` seconds, checked as they go rather than after."""
    for _ in range(int(max(0.0, within) / 0.05)):
        if not any(_process_exists(target) for target in pids):
            return True
        await asyncio.sleep(0.05)
    return not any(_process_exists(target) for target in pids)


# How long a Chrome asked to close is given to write its state and exit before it is signalled.
# Measured exits are well under a second; the wait ends as soon as the process is gone.
_CLOSE_WAIT = float(os.environ.get("MCP_STEALTH_CLOSE_WAIT", "5"))


async def _request_browser_close(browser: Any) -> bool:
    """Send Browser.close over CDP. True when Chrome acknowledged it; never raises."""
    from nodriver.cdp import browser as cdp_browser
    connection = getattr(browser, "connection", None)
    if connection is None or getattr(connection, "closed", False):
        return False
    try:
        await asyncio.wait_for(connection.send(cdp_browser.close()), timeout=2)
        return True
    except Exception:
        # The reply can be lost as Chrome closes the socket on its way out; whether it is
        # really going is decided by watching the process, not by this answer.
        return True if _socket_dropped(connection) else False


def _socket_dropped(connection: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(getattr(connection, "closed", False))
    return False


async def _ensure_browser_dead(browser, profile_dir=None, grace: float = 1.5) -> dict:
    """Stop a browser and VERIFY the OS process actually died.

    `browser.stop()` talks over CDP. When the websocket is already gone — a
    reload, a wedged renderer, a socket that closed without a close frame — it
    can return without raising while Chrome keeps running. Trusting it then
    reports a clean close, drops the handle, and leaves an orphan nobody owns:
    the process outlives the only thing that knew about it.

    So the process is confirmed dead out-of-band, and signalled directly if it
    is not. The PID nodriver recorded at launch is the precise target; the
    profile-dir scan is the fallback for attached or restored browsers where no
    PID was ever captured.

    Returns {stopped, pid, killed, still_alive, detail} — never raises.
    """
    import signal as _signal

    # `verified` is the load-bearing field: True only when death was actually
    # confirmed. Absence of evidence (no PID, no profile to scan) is NOT proof,
    # so it stays False and the caller must not claim a clean close.
    report = {"stopped": False, "verified": False, "pid": None, "killed": [],
              "still_alive": [], "detail": ""}
    # A close by a tool: the quit watcher must not also act on this browser meanwhile.
    if browser is not None:
        await asyncio.to_thread(quit_watch.stop, browser)
    pid = getattr(browser, "_process_pid", None) if browser else None
    report["pid"] = pid
    recycled = isinstance(pid, int) and pid > 0 and _pid_recycled(pid, browser)
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    family = (_descendant_pids(pid, str(data_dir) if data_dir else None)
              if isinstance(pid, int) and pid > 0 and not recycled else [])
    # Attached: this server did not start that Chrome and has no process to signal, and
    # from nodriver 0.50 stop() only closes the socket. Ask it to quit, then look for
    # evidence, or an attached browser reads as closed while it keeps running.
    attached = (browser is not None and getattr(browser, "_process", None) is None
                and not pid and getattr(getattr(browser, "config", None), "port", None) is not None)
    if attached:
        from nodriver.cdp import browser as cdp_browser
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                (getattr(browser, "connection", None) or browser).send(cdp_browser.close()),
                timeout=3,
            )
    elif isinstance(pid, int) and pid > 0 and not recycled:
        # A browser this server started. nodriver's stop() disconnects and sends SIGTERM, and
        # Chrome then exits without writing what it holds in memory: its cookie store is
        # written about every 30 s, so a login made just before the close was lost. Asked
        # with Browser.close, Chrome shuts down as on Quit and writes it first. The signals
        # below stay the fallback for a Chrome that does not answer or does not exit.
        report["close_requested"] = await _request_browser_close(browser)
        if report["close_requested"]:
            report["closed_itself"] = await _all_gone([pid, *family], _CLOSE_WAIT)
    try:
        if browser:
            browser.stop()
        report["stopped"] = True
    except Exception as e:
        report["detail"] = f"stop() raised: {type(e).__name__}: {e}"
    if attached:
        if await _debug_port_closed(browser, grace):
            report["verified"] = True
            # Connecting made nodriver create a temp profile this Chrome never used.
            await _discard_temp_profile(browser)
        else:
            report["stopped"] = False
            report["detail"] = (
                "the attached Chrome is still answering on its debugging port after "
                "being asked to close"
            )
        return report

    if recycled:
        report["verified"] = True
        report["detail"] = f"PID {pid} now belongs to another program, so it was not signalled"
        await _discard_temp_profile(browser)
        return report
    # Collect every PID that could still be ours.
    candidates: list[int] = [pid] if isinstance(pid, int) and pid > 0 else []
    candidates += [child for child in family if child not in candidates]
    # The profile scan is only a fallback for when no PID was captured. With a PID the
    # target is known, and a scan can only add processes that are not ours to signal.
    if profile_dir is not None and not candidates:
        try:
            from .state import find_chrome_pids_by_profile
            for extra in find_chrome_pids_by_profile(profile_dir):
                if extra not in candidates:
                    candidates.append(extra)
        except Exception:
            pass
    if not candidates:
        # Nothing to check against — cannot prove the process ended.
        return report

    # Let a graceful stop finish before escalating. Chrome is gone about 0.1 s after stop(),
    # so this returns then instead of sitting out the whole grace on every close.
    await _all_gone(candidates, grace)
    for target in candidates:
        if not _process_exists(target):
            continue                    # already gone, or not ours to signal
        try:
            os.kill(target, _signal.SIGTERM)
            report["killed"].append(target)
        except Exception:
            continue
    if report["killed"]:
        await _all_gone(report["killed"], 1.0)
        for target in list(report["killed"]):
            if not _process_exists(target):
                continue
            try:
                # Windows has no SIGKILL; os.kill with any other signal is TerminateProcess there.
                os.kill(target, getattr(_signal, "SIGKILL", _signal.SIGTERM))
            except Exception:
                report["still_alive"].append(target)
        # SIGKILL takes a moment, and the verdict below decides whether the handle is kept.
        for _ in range(20):
            if not any(_process_exists(target) for target in report["killed"]):
                break
            await asyncio.sleep(0.1)
    for target in candidates:
        if _process_exists(target) and target not in report["still_alive"]:
            report["still_alive"].append(target)
    report["verified"] = not report["still_alive"]
    if report["verified"]:
        await _discard_temp_profile(browser)
    return report


def _kill_note(report: dict) -> str:
    """Human-readable tail for a close result — empty when nothing was needed."""
    bits = []
    if report.get("killed"):
        bits.append(
            f"CDP stop did not end the process; signalled PID(s) "
            f"{report['killed']} directly")
    if report.get("still_alive"):
        bits.append(
            f"WARNING: PID(s) {report['still_alive']} are STILL RUNNING and are "
            f"now orphaned — kill them manually")
    # Only where the close is being reported as done; a failed stop already says so itself.
    if report.get("stopped") and not report.get("verified") and not report.get("still_alive"):
        bits.append("stop was requested, but no process could be checked to confirm it ended")
    return (" (" + "; ".join(bits) + ")") if bits else ""


def _close_incomplete(report: dict) -> bool:
    """True when a close must not be reported as done and the handle must be kept.

    stop() returning cleanly while a PID survives both signals is the case that matters:
    dropping the handle then leaves a live Chrome nothing can reach, and a privacy profile
    would be deleted from under it.
    """
    return bool(report.get("still_alive")) or not (report.get("stopped") or report.get("verified"))


def _close_failure(report: dict) -> str:
    alive = (f"PID(s) {report['still_alive']} still running"
             if report.get("still_alive") else "could not confirm the process ended")
    return f"{report['detail']}; {alive}" if report.get("detail") else alive


def _target_key(tab) -> str:
    """Stable identity for a tab's CDP target.

    id(tab) was used before — a Python object address, which changes when the Tab
    object is recreated and can even be recycled onto a different tab after GC.
    The CDP target id is the thing that actually survives navigation.

    Shared by every per-tab cache (console capture arming, snapshot deltas,
    pending human handoffs), which is why it lives here rather than inside any
    one of them.
    """
    return target_key(tab)


async def _wait(coro, timeout: Optional[float] = None, what: str = "operation"):
    """Await a CDP operation with an honest timeout and typed transport errors."""
    limit = TOOL_ACTION_TIMEOUT if timeout is None else timeout
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"{what} timed out after {limit}s — page JS may be stuck or blocked. "
            "Try reload, or close+launch."
        ) from e
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "inspected target navigated" in low or "target navigated" in low:
            raise RuntimeError(
                f"{what} interrupted by target navigation; the expression/action outcome "
                "is unknown. Inspect the new page before retrying."
            ) from e
        if "target closed" in low or "target was closed" in low:
            raise RuntimeError(
                f"{what} target closed; the action outcome is unknown. "
                "Call browser_recover() then browser_launch()."
            ) from e
        if transport_lost(msg):
            raise RuntimeError(
                f"{what} failed: CDP transport lost. Recover with: browser_recover() "
                "then browser_launch(). Your profile and logins are preserved; "
                "the action outcome is unknown."
            ) from e
        raise

# Serialize concurrent launches inside ONE MCP process. Cross-process collision
# is handled separately by `resolve_default_profile()` (per-PID fallback).
class _PerSessionLock:
    """One launch at a time per MCP session.

    It guards a session against racing itself into two Chromes on one profile.
    Sessions launch different profiles, and one must not fail because another
    happens to be starting a browser.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        return self._locks.setdefault(BrowserState.session_key(), asyncio.Lock())

    def locked(self) -> bool:
        return self._lock().locked()

    async def __aenter__(self) -> None:
        await self._lock().acquire()

    async def __aexit__(self, *_exc: Any) -> None:
        self._lock().release()


_LAUNCH_LOCK = _PerSessionLock()


async def _page_facts_for_report(tab: Any) -> tuple[str, str]:
    """(url, title) for a report line, saying so when they could not be read."""
    try:
        return await get_url(tab, strict=True), await get_title(tab, strict=True)
    except PageReadError as exc:
        return f"(unreadable: {exc})", "(unreadable)"


async def _first_live_tab(browser: Any, attempts: int = 8) -> Any:
    """A page target that actually answers.

    Browser.main_tab returns the first page target nodriver has on record, and Chrome can
    replace that one during startup (restoring a session, a profile from another
    browser). Connecting to it then fails with "server rejected WebSocket connection:
    HTTP 404", which aborted the launch of a perfectly healthy browser.
    """
    from nodriver.cdp import runtime as cdp_rt, target as cdp_target

    for _ in range(attempts):
        with contextlib.suppress(Exception):
            await browser.update_targets()
            # A Browser has .connection up to 0.48 and is itself a Connection from 0.50.
            infos = await (getattr(browser, "connection", None) or browser).send(
                cdp_target.get_targets())
            live = {str(t.target_id) for t in infos if t.type_ == "page"}
            for candidate in browser.tabs:
                if str(candidate.target.target_id) not in live:
                    continue
                try:
                    await asyncio.wait_for(candidate.send(cdp_rt.evaluate("1")), timeout=3)
                    return candidate
                except Exception:
                    continue
        await asyncio.sleep(0.25)
    return await asyncio.wait_for(browser.get("about:blank", new_tab=True),
                                  timeout=BROWSER_NAV_TIMEOUT)
_SPAWNING: dict[str, str] = {}


_FINGERPRINT_FIELDS = ("user_agent", "accept_language", "platform", "timezone")


def _validate_instance_id(instance_id: str) -> str:
    """Validate IDs that may become directory names or persistent filenames."""
    if (
        not instance_id
        or instance_id in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9._-]+", instance_id)
    ):
        raise ValueError(
            "instance_id must be a non-empty name containing only letters, "
            "numbers, dot, underscore, and dash (not '.' or '..')"
        )
    return instance_id


def _direct_child(root: Path, name: str, label: str) -> Path:
    """Return a direct child path for a user-supplied profile directory name."""
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
    ):
        raise ValueError(f"{label} must be one directory name, not a path")
    return root / name


def _normalize_fingerprint(value: Optional[dict]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("fingerprint must be an object")
    unknown = set(value) - set(_FINGERPRINT_FIELDS)
    if unknown:
        raise ValueError(f"unsupported fingerprint field(s): {sorted(unknown)}")
    normalized = {
        key: str(value[key]).strip()
        for key in _FINGERPRINT_FIELDS
        if value.get(key) is not None and str(value[key]).strip()
    }
    if not normalized:
        raise ValueError("fingerprint must contain at least one override")
    return normalized


def _mask_proxy_password(proxy: Any) -> Any:
    """user:password@host becomes user:***@host, for anything printed."""
    if not isinstance(proxy, str):
        return proxy
    return re.sub(r"(?<=://)([^:/@\s|]+):[^@\s|]+@", r"\1:***@", proxy)


def _identity_for_report(identity: dict, reveal: bool = False) -> dict:
    return identity if reveal else {**identity, "proxy": _mask_proxy_password(identity.get("proxy"))}


def _identity_path(instance_id: str) -> Path:
    """Resolve an identity file without allowing instance IDs to escape its root."""
    _validate_instance_id(instance_id)
    return IDENTITIES_DIR / f"{instance_id}.json"


def _load_identity(instance_id: str) -> Optional[dict]:
    path = _identity_path(instance_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("fingerprint"), dict):
        raise ValueError(f"invalid identity file: {path}")
    return data


def _privacy_proxy(proxy: Optional[str]) -> Optional[str]:
    """Choose a proxy pool member and expand a per-session token when supplied.

    A literal endpoint is still useful when the provider rotates upstream IPs.
    For client-side rotation callers can pass ``endpoint-a|endpoint-b`` or use a
    provider URL containing ``{session}``.
    """
    if not proxy:
        return None
    candidates = [part.strip() for part in proxy.split("|") if part.strip()]
    chosen = random.choice(candidates) if candidates else proxy
    return chosen.replace("{session}", f"{random.getrandbits(64):016x}")


def _launch_identity(
    instance_id: str,
    *,
    proxy: Optional[str],
    user_agent: Optional[str],
    fingerprint: Optional[dict],
    privacy: bool,
) -> tuple[Optional[str], dict[str, str], str]:
    """Resolve one launch's identity: caller's fields over a pin over the real browser.

    With nothing specified the browser presents itself as it is, in privacy mode
    too. Privacy means a disposable profile, not a made-up machine: a random OS,
    Chrome version and timezone on top of a real engine contradict each other, and
    that contradiction is what stands out.
    """
    _validate_instance_id(instance_id)
    pinned = None if privacy else _load_identity(instance_id)
    if privacy:
        effective_fp = {}
        effective_proxy = _privacy_proxy(proxy)
    else:
        effective_fp = dict((pinned or {}).get("fingerprint") or {})
        effective_proxy = proxy if proxy is not None else (pinned or {}).get("proxy")
    source = "pinned" if effective_fp else "native"
    if fingerprint is not None:
        effective_fp.update(_normalize_fingerprint(fingerprint))
        source = "caller"
    if user_agent:
        effective_fp["user_agent"] = user_agent
        source = "caller"
    return effective_proxy, effective_fp, source


def _ua_metadata_param(hints: dict[str, Any]) -> Any:
    """identity.ua_metadata_fields output as the CDP object setUserAgentOverride takes."""
    from nodriver.cdp import emulation as cdp_emulation

    make = cdp_emulation.UserAgentBrandVersion
    return cdp_emulation.UserAgentMetadata(**{
        **hints,
        "brands": [make(**b) for b in hints["brands"]],
        "full_version_list": [make(**b) for b in hints["full_version_list"]],
    })


async def _apply_fingerprint_to_tab(tab: Tab, fingerprint: dict[str, str]) -> None:
    """Apply a resolved identity to a tab, keeping its fields consistent.

    Fields left out follow the real browser. Network.setUserAgentOverride requires a
    UA even to change only the language, and without metadata it empties
    navigator.userAgentData.brands, so the UA is always sent and the client hints
    are always derived from it. A UA with no navigator.platform gets the matching
    one. In headless mode the override also runs with nothing specified, because the
    real UA and brands say HeadlessChrome.
    """
    from nodriver.cdp import emulation as cdp_emulation
    from nodriver.cdp import network as cdp_network

    requested_ua = fingerprint.get("user_agent") or ""
    language = fingerprint.get("accept_language") or ""
    platform = fingerprint.get("platform") or ""
    from nodriver.cdp import browser as cdp_browser

    version = await tab.send(cdp_browser.get_version())
    real_version = version[1].rsplit("/", 1)[-1]
    real = version[3]
    native = strip_headless(real)
    needs_cleanup = not requested_ua and native != real
    if requested_ua or language or platform or needs_cleanup:
        user_agent = requested_ua or native or ""
        if not platform and requested_ua:
            platform = navigator_platform(os_family(user_agent)) or ""
        kwargs: dict[str, Any] = {"user_agent": user_agent}
        if language:
            # Chrome takes the bare list and generates the weighted header from it.
            tags = navigator_languages(language)
            kwargs["accept_language"] = ",".join(tags)
        if platform:
            kwargs["platform"] = platform
        hints = ua_metadata_fields(user_agent, platform, real_version)
        if hints:
            kwargs["user_agent_metadata"] = _ua_metadata_param(hints)
        await tab.send(cdp_network.set_user_agent_override(**kwargs))
        if language:
            # Weights Chrome would not produce itself can only be sent as an explicit
            # header. navigator.languages keeps the bare list either way.
            wanted = ",".join(p.strip() for p in language.split(",") if p.strip())
            if wanted != default_accept_language(tags):
                await tab.send(cdp_network.enable())
                await tab.send(cdp_network.set_extra_http_headers(
                    cdp_network.Headers({"Accept-Language": wanted})))
    if fingerprint.get("timezone"):
        await tab.send(cdp_emulation.set_timezone_override(
            timezone_id=fingerprint["timezone"]
        ))


def _new_privacy_profile(instance_id: str) -> Path:
    PRIVACY_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", instance_id).strip(".-") or "browser"
    return Path(tempfile.mkdtemp(prefix=f"{safe}-", dir=PRIVACY_PROFILE_ROOT))


# How long removing a privacy profile keeps trying once its browser is verified gone. Windows
# refuses to delete a file while any handle to it is open, and a deleted file stays in its
# folder until the last one closes. On a CI runner, a profile whose Chrome had just been
# ended still could not be removed a second later and could be some seconds after that.
# POSIX deletes open files, so there the three tries made before are kept.
_PRIVACY_REMOVE_WAIT = float(os.environ.get(
    "MCP_STEALTH_PROFILE_REMOVE_WAIT", "15" if sys.platform == "win32" else "0"))
_PRIVACY_REMOVE_DELAYS = (0.1, 0.25, 0.5)
# Why the last removal of a privacy profile failed, by the path the caller passed.
_PRIVACY_REMOVE_FAILURES: dict[str, str] = {}


def _removal_failure(profile_dir: Optional[Path]) -> str:
    """'; <why>' for an error about a privacy profile that could not be removed, else ''."""
    reason = _PRIVACY_REMOVE_FAILURES.get(str(profile_dir)) if profile_dir is not None else None
    return f"; {reason}" if reason else ""


def _short_command(command: str, limit: int = 120) -> str:
    return command if len(command) <= limit else command[:limit - 3] + "..."


def _profile_holders(profile_dir: Path, remaining: list[str], already: set[int]) -> str:
    """Who holds a profile that could not be removed, in words, leaving out PIDs `already` named."""
    from .state import _win_file_holders, process_table, processes_naming
    users = [(pid, command) for pid, command in processes_naming(profile_dir) or []
             if pid not in already]
    named = [f"PID {pid} ({_short_command(command)})" for pid, command in users]
    # Restart Manager exists only on Windows, the one system where an open file blocks this;
    # elsewhere it answers None. It gives no name for a process that is exiting.
    holders = _win_file_holders(remaining[:300]) or []
    commands = {pid: command for pid, _ppid, command in (process_table() or [])} if holders else {}
    for pid, app in holders:
        if pid not in already and not any(n.startswith(f"PID {pid} ") for n in named):
            label = app or _short_command(commands.get(pid, "")) or "a program whose name could not be read"
            named.append(f"PID {pid} ({label})")
    if named:
        return "held open by " + ", ".join(named)
    return "" if already else "no process holding it could be named"


def _left_in(target: Path) -> list[str]:
    left: list[str] = []
    with contextlib.suppress(OSError):
        for folder, _dirs, files in os.walk(target):
            left.extend(os.path.join(folder, name) for name in files)
    return left


async def _remove_privacy_profile(profile_dir: Optional[Path]) -> bool:
    """Remove a privacy profile once its browser is gone, with bounded retries.

    Called after _ensure_browser_dead verified the browser and its helpers ended. Nothing is
    deleted while a process still names the profile on its command line: Chrome writes back
    what is deleted under it. When the folder cannot be removed in time, the reason and any
    holder are kept for the error (_removal_failure).
    """
    if profile_dir is None:
        return True
    root = PRIVACY_PROFILE_ROOT.resolve()
    try:
        target = profile_dir.resolve()
        if target == root or root not in target.parents:
            return False
    except Exception:
        return False
    from .state import processes_naming
    _PRIVACY_REMOVE_FAILURES.pop(str(profile_dir), None)
    # Browser.stop() terminates the parent synchronously, but Chrome helpers can
    # write their final Preferences files a beat later. Deleting immediately can
    # therefore appear successful and then be recreated behind us.
    await asyncio.sleep(0.15)
    deadline = time.monotonic() + _PRIVACY_REMOVE_WAIT
    reason = ""
    users: list[tuple[int, str]] = []
    attempt = 0
    while True:
        delay = _PRIVACY_REMOVE_DELAYS[min(attempt, len(_PRIVACY_REMOVE_DELAYS) - 1)]
        attempt += 1
        # The path as Chrome was given it: resolving can change its spelling (a Windows 8.3
        # short name becomes the long one), and processes_naming tries both.
        users = await asyncio.to_thread(processes_naming, profile_dir) or []
        if users:
            reason = "a process still runs with it: " + ", ".join(
                f"PID {pid} ({_short_command(command)})" for pid, command in users)
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.to_thread(shutil.rmtree, target)
            except FileNotFoundError:
                pass
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(delay)
                if not target.exists():
                    return True
                reason = "it was written again after being removed"
        if not target.exists():
            return True
        if attempt >= len(_PRIVACY_REMOVE_DELAYS) and time.monotonic() >= deadline:
            break
    holders = await asyncio.to_thread(
        _profile_holders, profile_dir, _left_in(target), {pid for pid, _command in users})
    _PRIVACY_REMOVE_FAILURES[str(profile_dir)] = "; ".join(p for p in (reason, holders) if p)
    return False


async def _safe_stop_browser(browser: Optional[Browser]) -> None:
    """Best-effort shutdown — used in cleanup paths so a half-launched Chrome
    doesn't leak its profile lock."""
    if browser is None:
        return
    try:
        browser.stop()
    except Exception:
        pass


# ── Auto-verify for Cloudflare/Turnstile challenges ─────────────────────────
# Triggers naturally after navigation. Max 2 click attempts then gives up
# silently — we never block the caller longer than ~6 seconds for verification.

_TURNSTILE_FIND_JS = """
(() => {
  // Strategy: prefer containers that ACTUALLY hold the rendered widget
  // (response-input + visible iframe) over generic .turnstile-class
  // wrappers that may just be layout cells.
  const inp = document.querySelector('input[name="cf-turnstile-response"]');
  const responseAncestors = new Set();
  if (inp) {
    let el = inp.parentElement;
    while (el && el !== document.body) {
      responseAncestors.add(el);
      el = el.parentElement;
    }
  }
  const primary = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    '[data-testid*="challenge-widget"]',
    '[data-testid*="turnstile"]',
    // [data-sitekey] alone matches reCAPTCHA/hCaptcha too — scope to CF
    // sitekey format (always starts with "0x") to avoid false positives.
    '[data-sitekey^="0x"]',
    '.cf-turnstile',
  ];
  const secondary = [
    '.turnstile',
    '[id*="turnstile" i]',
    '[id*="cf-chl"]',
    '[class*="turnstile" i]',
  ];
  // Standard Turnstile widget renders at ~300×65 (compact) or larger. We
  // prefer matches whose dimensions look like an actual widget (not a tiny
  // empty cell, not a giant page-wide layout wrapper).
  const isWidgetSized = (r) =>
    r.width >= 200 && r.width <= 800 && r.height >= 50 && r.height <= 250;
  const tryPick = (sels, tier) => {
    let bestWidget = null;       // matches isWidgetSized
    let bestContaining = null;   // contains response input
    let bestOther = null;        // any other valid hit
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width < 50 || r.height < 20) continue;
        const containsAnyInput = [...document.querySelectorAll('input[name="cf-turnstile-response"]')].some(i => el.contains(i));
        const area = r.width * r.height;
        const widgetSized = isWidgetSized(r);
        const entry = { tier, found: sel, containsInput: containsAnyInput,
          widgetSized, area,
          left: Math.round(r.left), top: Math.round(r.top),
          width: Math.round(r.width), height: Math.round(r.height) };
        if (widgetSized && containsAnyInput) {
          if (!bestWidget || area > bestWidget.area) bestWidget = entry;
        } else if (containsAnyInput) {
          if (!bestContaining || area < bestContaining.area) bestContaining = entry;
        } else {
          if (!bestOther || area > bestOther.area) bestOther = entry;
        }
      }
    }
    return bestWidget || bestContaining || bestOther;
  };
  // After picking the best container, account for CSS padding so the click
  // lands on actual widget content, not in dead padding space.
  const annotate = (entry, sel) => {
    if (!entry) return entry;
    const el = [...document.querySelectorAll(sel)].find(e => {
      const r = e.getBoundingClientRect();
      return Math.round(r.left) === entry.left && Math.round(r.top) === entry.top;
    });
    if (el) {
      const cs = getComputedStyle(el);
      entry.padLeft = parseFloat(cs.paddingLeft) || 0;
      entry.padTop = parseFloat(cs.paddingTop) || 0;
    }
    return entry;
  };
  let hit = tryPick(primary, 'primary') || tryPick(secondary, 'secondary');
  if (hit) return JSON.stringify(annotate(hit, hit.found));
  // Last resort: walk up from response-input to first sized ancestor
  if (inp) {
    let el = inp.parentElement;
    while (el && el !== document.body) {
      const r = el.getBoundingClientRect();
      if (r.width >= 80 && r.height >= 30) {
        const cs = getComputedStyle(el);
        return JSON.stringify({ tier: 'response-input-ancestor',
          found: 'input[name="cf-turnstile-response"]→ancestor',
          left: Math.round(r.left), top: Math.round(r.top),
          width: Math.round(r.width), height: Math.round(r.height),
          padLeft: parseFloat(cs.paddingLeft) || 0,
          padTop: parseFloat(cs.paddingTop) || 0 });
      }
      el = el.parentElement;
    }
  }
  return 'not_found';
})()
"""

_CF_CHALLENGE_PROBE_INITIAL_JS = """
(() => {
  const responseInputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
  for (const inp of responseInputs) {
    if (inp.value && inp.value.length > 5) return false;  // already solved
  }
  const txt = (document.body && document.body.innerText || '').toLowerCase();
  const phrases = ['performing security verification', 'just a moment',
    'checking your browser', 'verify you are human', 'verifying you are human'];
  const cfText = phrases.some(p => txt.includes(p));
  // Turnstile-specific markers ONLY. We deliberately exclude the bare
  // [data-sitekey] selector — it matches reCAPTCHA / hCaptcha hosts too,
  // and clicking those checkboxes opens unsolvable image grids. The CF
  // Turnstile sitekey format always starts with "0x", so we keep scoped
  // [data-sitekey^="0x"] which is unambiguous.
  const cfDom = !!document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], ' +
    '.cf-turnstile, .turnstile, [class*="turnstile" i], [id*="turnstile" i], ' +
    '[data-sitekey^="0x"], input[name="cf-turnstile-response"], ' +
    'script[src*="challenges.cloudflare.com"]'
  );
  return cfText || cfDom;
})()
"""

_CF_CHALLENGE_PROBE_ACTIVE_JS = """
(() => {
  // Stricter "still active" check used BETWEEN click attempts. Excludes the
  // loader script (which persists after solve) and host-container CSS classes
  // (which also persist after solve, just dormant). True only when the visible
  // challenge UI is actually present.
  const inps = document.querySelectorAll('input[name="cf-turnstile-response"]');
  if (inps.length > 0) {
    let anyEmpty = false;
    for (const inp of inps) {
      if (!inp.value || inp.value.length <= 5) { anyEmpty = true; break; }
    }
    return anyEmpty;  // a token on every input → solved; an empty one → still waiting
  }
  const txt = ((document.title || '') + ' ' + (document.body && document.body.innerText || '')).toLowerCase();
  const phrases = ['performing security verification', 'just a moment',
    'checking your browser', 'verify you are human', 'verifying you are human'];
  if (phrases.some(p => txt.includes(p))) return true;
  return !!document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
  );
})()
"""


# One look right after load that also says whether a challenge could still be on its way.
# "none" is the common case and costs nothing more; anything else keeps the old short wait.
_CF_FIRST_LOOK_JS = """
(() => {
  const present = %s;
  if (present) return 'challenge';
  if (document.readyState !== 'complete') return 'maybe';
  const marks = ['challenges.cloudflare.com', 'turnstile'];
  for (const s of document.scripts) {
    const text = s.src ? '' : (s.textContent || '');
    if (marks.some(m => text.includes(m))) return 'maybe';
  }
  if (document.querySelector('link[href*="challenges.cloudflare.com"]')) return 'maybe';
  try {
    if (performance.getEntriesByType('resource').some(e => e.name.includes('challenges.cloudflare.com'))) return 'maybe';
  } catch (e) {}
  return 'none';
})()
""" % _CF_CHALLENGE_PROBE_INITIAL_JS.strip()


async def _cf_first_look(tab: Tab) -> Optional[str]:
    """'challenge', 'maybe' or 'none' for a freshly loaded page; None when it could not be asked."""
    try:
        # In the isolated world: this runs on every navigation, and in the page's world a
        # replaced querySelector would be handed the Turnstile selectors each time.
        v = await asyncio.wait_for(evaluate_isolated(tab, _CF_FIRST_LOOK_JS), timeout=3.0)
        return v if v in ("challenge", "maybe", "none") else None
    except Exception:
        return None


async def _cf_challenge_state(tab: Tab, *, active: bool = False) -> Optional[bool]:
    """_has_cf_challenge, with None when the page could not be asked at all."""
    js = _CF_CHALLENGE_PROBE_ACTIVE_JS if active else _CF_CHALLENGE_PROBE_INITIAL_JS
    try:
        return bool(await asyncio.wait_for(evaluate_isolated(tab, js), timeout=3.0))
    except Exception:
        return None


async def _has_cf_challenge(tab: Tab, *, active: bool = False) -> bool:
    """Detect a Cloudflare/Turnstile challenge.

    active=False (default): broad detection used BEFORE attempting any click.
        Matches loader script and host containers so we don't miss a
        widget that hasn't fully rendered yet.
    active=True: strict detection used BETWEEN click attempts. Returns True
        only when visible challenge UI is still on the page (not just the
        post-solve dormant markers)."""
    return bool(await _cf_challenge_state(tab, active=active))


# Below this the best template match is just whatever on the page looks least unlike it.
_CF_TEMPLATE_THRESHOLD = 0.7


async def _cf_template_click(tab: Tab, template_image: Optional[str] = None) -> tuple[bool, str]:
    """Click the Turnstile checkbox found by template match: (clicked, what happened).

    Not Tab.verify_cf: it clicks the best match however poor, so on a page without a
    widget it reported success after clicking whatever scored highest.
    """
    import cv2
    import numpy as np
    from nodriver.core import util as nodriver_util
    if template_image:
        template = cv2.imread(str(Path(template_image).expanduser()))
        if template is None:
            return False, f"could not read template image {template_image}"
    else:
        template = cv2.imdecode(np.frombuffer(nodriver_util.get_cf_template(), np.uint8), cv2.IMREAD_COLOR)
    shot = base64.b64decode(await tab.send(cdp_page_capture_png()))
    page = cv2.imdecode(np.frombuffer(shot, np.uint8), cv2.IMREAD_COLOR)
    if page.shape[0] < template.shape[0] or page.shape[1] < template.shape[1]:
        return False, "the page is smaller than the template"
    _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(page, template, cv2.TM_CCOEFF_NORMED))
    if score < _CF_TEMPLATE_THRESHOLD:
        return False, f"no Turnstile checkbox on the page (best template match {score:.2f})"
    raw = await tab.evaluate("window.devicePixelRatio", return_by_value=True)
    ratio = float(raw.value if hasattr(raw, "value") else raw) or 1.0
    x = (loc[0] + template.shape[1] / 2) / ratio
    y = (loc[1] + template.shape[0] / 2) / ratio
    await tab.mouse_click(x, y)
    return True, f"clicked at ({int(x)},{int(y)}) by template match {score:.2f}"


def cdp_page_capture_png():
    from nodriver.cdp import page as cdp_page
    return cdp_page.capture_screenshot(format_="png")


_CF_OBSERVE_JS = """
JSON.stringify({
  title: document.title,
  token: (document.querySelector('input[name="cf-turnstile-response"]') || {}).value || ''
})
"""


# Measured on one macOS machine and one IP: CDP clicks failed 3 of 3 on a full-page
# challenge and solve_turnstile_oscursor passed 3 of 3. So a failed click names it.
_OSCURSOR_HINT = (
    "next step for a full-page challenge: solve_turnstile_oscursor, which clicks with the "
    "real OS cursor in a separate Chrome and copies the cookies back. It needs the main "
    "browser on this page, takes over the mouse during the call (up to 50 s by default), and "
    "needs the [os-cursor] "
    "extra plus macOS Accessibility and Screen Recording permission (Windows and Linux "
    "not field-tested)")


async def _cf_after_click(tab: Tab, wait: float = 3.0) -> str:
    """What the page shows after a challenge click. A click landing is not a challenge passed."""
    deadline = time.monotonic() + wait
    title, token_len, still = "", 0, None
    while True:
        await asyncio.sleep(0.5)
        with contextlib.suppress(Exception):
            raw = await asyncio.wait_for(tab.evaluate(_CF_OBSERVE_JS, return_by_value=True), timeout=3.0)
            seen = parse_json(raw, {}) or {}
            title, token_len = str(seen.get("title", "")), len(str(seen.get("token", "")))
        still = await _cf_challenge_state(tab, active=True)
        if token_len and not still:
            break
        if time.monotonic() >= deadline:
            break
    interstitial = "just a moment" in title.lower()
    token = f"token filled ({token_len} chars)" if token_len else "token empty"
    if token_len and not interstitial and not still:
        verdict = "challenge passed as far as the page shows"
    elif still or interstitial:
        verdict = f"challenge still present: NOT passed; {_OSCURSOR_HINT}"
    else:
        verdict = "no sign either way: check the page before relying on it"
    return f"observed after {wait:g}s: {token}, title {title[:60]!r}, {verdict}"


async def _attempt_turnstile_click(tab: Tab, offset_x: int = 30) -> Optional[tuple[int, int]]:
    """Find Turnstile widget + dispatch a CDP-level click at its checkbox.
    Returns (x, y) clicked or None. CDP click works for out-of-process
    iframes where DOM-level events don't propagate.

    Click target = container.left + padding + offset_x, container.top +
    padding + half of inner-height. CSS padding is honored so clicks on
    padded host containers (.turnstile { padding: 48px 64px; }) land
    inside the widget content rather than in dead padding space."""
    try:
        raw = await asyncio.wait_for(
            tab.evaluate(_TURNSTILE_FIND_JS, return_by_value=True), timeout=3.0
        )
    except Exception:
        return None
    data = parse_json(raw, None)
    if not isinstance(data, dict):
        return None
    pad_left = int(data.get("padLeft", 0))
    pad_top = int(data.get("padTop", 0))
    inner_left = data["left"] + pad_left
    inner_top = data["top"] + pad_top
    inner_height = max(20, data["height"] - 2 * pad_top)
    target_x = inner_left + offset_x
    target_y = inner_top + inner_height // 2
    # Randomize the approach vector: a fixed (+180, -80) start traces the same
    # line every call, and interactive Turnstile scores that as scripted.
    start_x = target_x + random.randint(120, 240) * random.choice((-1, 1))
    start_y = target_y + random.randint(60, 140) * random.choice((-1, 1))
    try:
        await humanized_move(tab, start_x, start_y, target_x, target_y)
        # humanized_mouse_press settles on the point and holds the button for a
        # human duration — tab.mouse_click releases with a 0 ms hold, which the
        # widget scores as synthetic and the server then re-issues (cf_chl_rc_ni).
        await humanized_mouse_press(tab, target_x, target_y)
        return (target_x, target_y)
    except Exception:
        return None


async def _auto_verify_cf(tab: Tab, max_attempts: int = 2, budget: float = 24.0) -> str:
    """Run on the tab right after load. Detects CF challenge + attempts click.
    Caps at max_attempts and self-bounds to `budget` seconds so it always
    finishes inside the caller's wait_for. Returns a short suffix to append to
    the caller's status line, or '' if no challenge was seen.

    Strategy:
      1. Brief wait so the Turnstile iframe has time to render.
      2. DOM-based click via response-input ancestor (works on full-page
         interstitials).
      3. If still on challenge, OpenCV template match via tab.verify_cf —
         covers shadow-DOM / out-of-process iframe widgets where the
         visible checkbox isn't reachable from response-input parents.
    """
    # 1. Let widget initialize. Some pages load the Turnstile script async
    #    and the checkbox iframe needs ~1.5-2s to render before any click
    #    target (DOM or pixel) is reachable.
    #    That wait is only paid when something points at a challenge: it used to be 0.6 s on
    #    every navigation, which made a plain page five times slower to open than it is.
    first = await _cf_first_look(tab)
    if first == "none":
        return ""
    if first != "challenge":
        await asyncio.sleep(0.6)
        if not await _has_cf_challenge(tab):
            return ""
    # Challenge present — give the widget another beat to paint its checkbox
    # so OpenCV template match has something to find.
    await asyncio.sleep(2.0)

    # After a click the widget POSTs behavioral telemetry and waits for the
    # server's verdict; that round trip runs several seconds, so a fixed short
    # check declares "still present" before the verdict lands and fires a second
    # click that re-submits and lowers the score. Poll instead, up to ~8s, but
    # never past the overall budget so the caller's wait_for cannot cancel us
    # mid-press (a cancelled mousePressed leaves no mouseReleased).
    overall_deadline = time.monotonic() + budget

    async def _cleared_within(seconds: float) -> bool:
        deadline = min(time.monotonic() + seconds, overall_deadline)
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if not await _has_cf_challenge(tab, active=True):
                return True
        return False

    actions: list[str] = []
    for _ in range(max(1, max_attempts)):
        # Reserve ~2s for a click+settle before starting another attempt; a
        # verdict poll that never began cannot have cleared the challenge.
        if time.monotonic() > overall_deadline - 2.0:
            break
        # 2. DOM tier
        clicked = await _attempt_turnstile_click(tab)
        if clicked is not None:
            actions.append(f"DOM@{clicked}")
            if await _cleared_within(8.0):
                break

        # 3. OpenCV template tier — handles shadow-DOM / cross-origin iframes
        if time.monotonic() > overall_deadline - 2.0:
            break
        try:
            clicked_tpl, _what = await asyncio.wait_for(_cf_template_click(tab), timeout=4.0)
            if clicked_tpl:
                actions.append("template")
                if await _cleared_within(8.0):
                    break
        except Exception:
            pass

    if not actions:
        return f" [auto-verify: CF detected but no clickable widget found; {_OSCURSOR_HINT}]"
    # The caller is about to read this page, so whether the challenge is still on it matters
    # more than which clicks were tried.
    still = await _has_cf_challenge(tab, active=True)
    if not still:
        return f" [auto-verify: {' → '.join(actions)}; challenge gone]"
    # A full-page interactive interstitial ("Just a moment…", Turnstile cType
    # 'interactive') grades the pointer input itself: CDP-injected clicks, however
    # well-timed, score below a real HID click and the server re-issues the
    # challenge (cf_chl_rc_ni). Say so and point at the paths that do work rather
    # than implying another click would help.
    return (f" [auto-verify: {' → '.join(actions)}; challenge still present: CDP clicks did "
            f"not pass it; {_OSCURSOR_HINT}. Other options: storage_state_load with a warm "
            f"session that holds a valid cf_clearance, or solve_captcha]")

from ._app import mcp  # single FastMCP instance shared with tools/* submodules
from . import image_output as _image_output  # noqa: F401  -- registers native image inspection tools
from .tools import turnstile_oscursor as _turnstile_oscursor  # registers the native-cursor tool


# ── Utility: ensure active tab after operations that may shift tabs ────────

async def _refresh_tabs() -> bool:
    """Sync BrowserState.tabs with the browser, keeping the active tab selected.

    nodriver up to 0.48 tracked new and closed targets by itself. From 0.50 it only learns
    of them on update_targets, so without this a popup stays invisible and a closed tab
    stays listed, and the active one could silently become a different tab.

    True when the browser answered. False means no browser, or a list that may be stale.
    """
    browser = BrowserState.browser
    if not browser:
        return False
    index = BrowserState.active_tab_index
    current = BrowserState.tabs[index] if 0 <= index < len(BrowserState.tabs) else None
    answered = False
    with contextlib.suppress(Exception):
        await asyncio.wait_for(browser.update_targets(), timeout=5)
        answered = True
    if BrowserState.browser is not browser:
        return False  # closed or switched while it was asked; its tabs are not this state's
    BrowserState.tabs = list(browser.tabs)
    if current is not None:
        wanted = _target_key(current)
        for position, tab in enumerate(BrowserState.tabs):
            if _target_key(tab) == wanted:
                BrowserState.active_tab_index = position
                break
    return answered


async def _tab_list_problem() -> Optional[str]:
    """Refresh the tab list for a tool that answers from it. An error text when it cannot.

    A browser closed under the caller (idle timeout, last window, crash) left an empty list,
    and tab_select answered "tab 0 out of range" as if the index were the mistake.
    """
    refreshed = await _refresh_tabs()
    if BrowserState.browser is None or not refreshed:
        try:
            BrowserState.active_tab()  # raises with the reason the browser went, when known
        except Exception as exc:
            return str(exc)
        if BrowserState.browser is None:
            return "browser not running"
        return ("the browser did not answer when asked for its tabs, so the tab list may be "
                "stale and nothing was changed; if it keeps happening, browser_recover")
    if not BrowserState.tabs:
        return "the browser has no open tab"
    return None


# ══════════════════════════════════════════════════════════════════════════
# 1. LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════

# ── Deterministic profile selection ───────────────────────────────────────
# A launch that quietly lands on a different profile than the caller believes
# hands them a different cookie jar: the logins are all still there, in a
# directory nobody named, and the session reads as wiped. So the profile in use
# is always reported (actual_profile + session_state_source), a conflict is
# resolved by a policy the caller picked, and nothing that already exists on
# disk is ever deleted or overwritten to make room.

# Regenerable, large, and rebuilt by Chrome on first run — skipping them keeps a
# conflict clone fast enough to be worth doing at launch time.
_CLONE_SKIP_DIRS = {
    "cache", "code cache", "gpucache", "dawnwebgpucache", "dawngraphitecache",
    "graphitedawncache", "grshadercache", "media cache", "service worker",
    "component_crx_cache", "extensions_crx_cache", "gpupersistentcache",
    "shared dictionary", "safe browsing", "componentupdater", "downloadedupdates",
}
_CLONE_SKIP_FILES = {
    "singletonlock", "singletoncookie", "singletonsocket", "lock", "lockfile",
}


def _safe_profile_clone(source: Path, instance_id: str) -> tuple[Optional[Path], dict]:
    """Copy a busy profile into a FRESH directory so a launch can proceed.

    The source is only ever read: this is the directory holding the developer's
    real logins, so the clone lands in a new timestamped path and nothing
    existing is deleted or overwritten. SQLite databases go through
    `_sqlite_snapshot`, which copies the -wal/-shm companions and integrity-checks
    the copy — a torn cookie jar is indistinguishable from a good one until a
    login fails hours later, so a suspect copy is reported and the launch is
    refused rather than started on top of it.

    Returns (clone_path, report); clone_path is None when the copy is not
    trustworthy, and the partial copy is left in place for inspection.
    """
    report: dict[str, Any] = {
        "source": str(source),
        "files": 0,
        "sqlite_verified": [],
        "sqlite_suspect": [],
        "errors": [],
    }
    target = PROFILES_ROOT / f"{instance_id}-clone-{int(time.time())}"
    if target.exists():
        report["errors"].append(f"clone target already exists: {target}")
        return None, report
    report["target"] = str(target)
    for root, dirs, files in os.walk(source):
        rel = Path(root).relative_to(source)
        dirs[:] = [d for d in dirs if d.lower() not in _CLONE_SKIP_DIRS]
        dest_dir = target / rel
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            report["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
            dirs[:] = []
            continue
        for name in files:
            lowered = name.lower()
            if lowered in _CLONE_SKIP_FILES or lowered.endswith("-journal"):
                continue
            if lowered.endswith(("-wal", "-shm")):
                continue  # _sqlite_snapshot copies these beside their database
            src_file = Path(root) / name
            dst_file = dest_dir / name
            try:
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                if _is_sqlite(src_file):
                    where = str(rel / name) if str(rel) != "." else name
                    if _sqlite_snapshot(src_file, dst_file):
                        report["sqlite_verified"].append(where)
                    else:
                        report["sqlite_suspect"].append(where)
                else:
                    shutil.copy2(src_file, dst_file)
                report["files"] += 1
            except FileNotFoundError:
                continue  # removed by the live browser since the listing, so not in this copy
            except Exception as exc:
                report["errors"].append(f"{name}: {type(exc).__name__}")
    # A file that exists and could not be copied leaves a clone missing part of the profile,
    # which reads as a working copy until the session that needed it fails.
    if report["sqlite_suspect"] or report["errors"]:
        return None, report
    try:
        clean_profile_state(target)
    except Exception:
        pass
    return target, report


def _resolve_launch_profile(
    instance_id: str,
    *,
    persistent: bool,
    profile_conflict: str,
) -> tuple[Optional[Path], dict]:
    """Pick browser_launch's user-data dir and say where its session state came from.

    Deterministic in two ways the old flow was not. A non-"main" instance gets
    its own profile instead of silently sharing main's cookie jar. And a
    conflict over a locked profile is resolved by the policy the caller chose
    rather than by an unannounced fallback:

      fallback (default, legacy behaviour) — use a per-process profile and say
          loudly that the expected logins are NOT in it
      fail     — refuse, naming the PID holding the profile
      clone    — launch from a read-only, integrity-checked copy of it

    "main" keeps the legacy path plus process affinity: that profile holds the
    real logins, and migrating it looks exactly like a wipe.
    """
    from .state import get_profile_affinity

    info: dict[str, Any] = {"profile_conflict_policy": profile_conflict}
    if profile_conflict not in ("fallback", "fail", "clone"):
        info["error"] = (
            f"profile_conflict must be 'fallback', 'fail' or 'clone', not {profile_conflict!r}"
        )
        return None, info
    if not persistent:
        info["actual_profile"] = None
        info["session_state_source"] = (
            "ephemeral — Chrome temp dir, discarded on close (no saved logins)"
        )
        return None, info

    desired = PROFILE_DIR if instance_id == "main" else PROFILES_ROOT / instance_id
    info["desired_profile"] = str(desired)

    pinned = get_profile_affinity()
    if instance_id == "main" and pinned is not None and not is_chrome_profile_locked(pinned):
        pinned.mkdir(parents=True, exist_ok=True)
        info["actual_profile"] = str(pinned)
        info["session_state_source"] = (
            f"persistent profile {pinned.name}" if pinned == desired
            else f"profile affinity — this process settled on {pinned.name} earlier "
                 f"and stays there so its logins remain visible"
        )
        return pinned, info

    holder = chrome_lock_holder_pid(desired) if desired.exists() else None
    if holder is None:
        desired.mkdir(parents=True, exist_ok=True)
        info["actual_profile"] = str(desired)
        info["session_state_source"] = f"persistent profile {desired.name}"
        return desired, info

    info["conflict_with_pid"] = holder
    if profile_conflict == "fail":
        info["error"] = (
            f"profile {desired} is held by Chrome PID {holder} (profile_conflict='fail'). "
            f"Close that browser, or relaunch with profile_conflict='clone' to run from "
            f"a verified read-only copy, or profile_conflict='fallback' for a separate "
            f"empty profile (its logins will NOT be the ones in {desired.name})."
        )
        return None, info
    if profile_conflict == "clone":
        clone, clone_report = _safe_profile_clone(desired, instance_id)
        info["clone"] = clone_report
        if clone is None:
            info["error"] = (
                f"could not take a trustworthy copy of {desired}: "
                f"suspect={clone_report.get('sqlite_suspect')} "
                f"errors={clone_report.get('errors')[:3]}. The partial copy at "
                f"{clone_report.get('target')} was kept for inspection and nothing in "
                f"the source profile was touched."
            )
            return None, info
        info["actual_profile"] = str(clone)
        info["session_state_source"] = (
            f"read-only clone of {desired} taken while Chrome PID {holder} held it — "
            f"logins are a point-in-time copy; new logins here do NOT reach the original"
        )
        return clone, info

    fallback = (
        per_process_profile() if instance_id == "main"
        else PROFILES_ROOT / f"{instance_id}-pid{os.getpid()}"
    )
    fallback.mkdir(parents=True, exist_ok=True)
    info["actual_profile"] = str(fallback)
    info["session_state_source"] = (
        f"FALLBACK profile {fallback.name} — {desired} is held by Chrome PID {holder}. "
        f"Logins saved in {desired.name} are NOT visible here. Use "
        f"profile_conflict='clone' to carry them over, or 'fail' to refuse instead."
    )
    return fallback, info


# Only flags that reduce work without changing what a script can measure about the
# browser, apart from images. Rendering and GPU flags stay out: they change canvas
# and WebGL output, which is exactly what fingerprinters read. --disable-features is
# also out, because Chrome honours the last one and nodriver passes its own.
LIGHT_MODE_FLAGS = (
    "--blink-settings=imagesEnabled=false",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-notifications",
    "--autoplay-policy=user-gesture-required",
    "--mute-audio",
    # Gemini in Chrome, Google Network Speech and Hangouts run as component extensions, about
    # 0.27 GB together on one page. Light mode only: Network Speech supplies the "Google ..."
    # speechSynthesis voices every Google Chrome lists, and pages read that list.
    "--disable-component-extensions-with-background-pages",
)


# A headed Chrome started the normal way opens its first window in front and takes the
# keyboard: measured frontmost within 300 ms, so whatever the person was typing went into
# Chrome. Giving focus back afterwards cannot undo that. With no startup window the app
# never activates, and the first window is created in the background over CDP. A window
# behind others would then count as occluded and report visibilityState hidden.
_BACKGROUND_WINDOW_FLAGS = ("--no-startup-window", "--disable-backgrounding-occluded-windows")

# Every launch, every mode. A new profile otherwise installs Chrome's default apps (Google Docs
# Offline, Chrome Web Store Payments), each with its own renderer: measured on a temporary
# profile, 12 processes and about 1.1 GB became 10 processes and about 0.9 GB, images still
# loading. persistent=False makes a new profile on every launch, so it paid this each time.
# --disable-background-networking saved the same but also stops Safe Browsing and variations
# updates, which leaves a long-lived profile drifting from an ordinary Chrome, so it stays light-only.
_LEAN_FLAGS = ("--disable-default-apps",)


# Headless Chrome reports an 800x600 screen whatever the window size, so the default
# 1280x800 window is larger than its own screen, which no real machine shows.
_FALLBACK_SCREENS = ((1920, 1080), (2560, 1440), (3840, 2160))


def _real_screen() -> Optional[dict]:
    """Size and work-area insets of the main display in CSS pixels, or None when unreadable."""
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSScreen
        screen = NSScreen.mainScreen()
        if screen is None:
            return None
        frame, visible = screen.frame(), screen.visibleFrame()
        width, height = int(frame.size.width), int(frame.size.height)
        bottom = int(visible.origin.y - frame.origin.y)
        top = height - bottom - int(visible.size.height)
        left = int(visible.origin.x - frame.origin.x)
        right = width - left - int(visible.size.width)
        if width <= 0 or height <= 0 or min(top, bottom, left, right) < 0:
            return None
        return {"width": width, "height": height, "top": top, "bottom": bottom,
                "left": left, "right": right}
    except Exception:
        return None


def _headless_screen_arg(window_width: int, window_height: int,
                         args: Optional[list[str]] = None) -> Optional[str]:
    """--screen-info giving headless Chrome a screen its window fits in.

    The machine's own main display when it is readable and large enough, since identity
    defaults to the real machine; otherwise the smallest common size that holds the window.
    Only the size and the work area are set: devicePixelRatio and colorDepth stay as
    headless reports them. None when the caller passed --screen-info.
    """
    if any((a or "").startswith("--screen-info") for a in (args or [])):
        return None
    screen = _real_screen()
    if screen is None or screen["width"] < window_width or screen["height"] < window_height:
        width, height = next(
            (size for size in _FALLBACK_SCREENS if size[0] >= window_width and size[1] >= window_height),
            (max(window_width, 1920), max(window_height, 1080)))
        # The menu bar on macOS and the taskbar on Windows; a Linux screen is left whole.
        screen = {"width": width, "height": height, "left": 0, "right": 0,
                  "top": 25 if sys.platform == "darwin" else 0,
                  "bottom": 48 if sys.platform == "win32" else 0}
    parts = [f"{screen['width']}x{screen['height']}"]
    parts += [f"workArea{side.capitalize()}={screen[side]}"
              for side in ("left", "right", "top", "bottom") if screen[side]]
    return "--screen-info={" + " ".join(parts) + "}"


def _disable_features_arg(config: Any, extra: tuple[str, ...]) -> str:
    """One --disable-features carrying nodriver's list and ours.

    Chrome honours only the last --disable-features. nodriver places its own list
    (IsolateOrigins,site-per-process, plus more on some versions) before the caller's
    arguments, so passing a second flag silently dropped nodriver's list: testing_mode ran
    with site isolation back on. Read what this nodriver version passes, then append.
    """
    features: list[str] = []
    for arg in config():
        if arg.startswith("--disable-features="):
            features += [f for f in arg.split("=", 1)[1].split(",") if f]
    features += list(extra)
    return "--disable-features=" + ",".join(dict.fromkeys(features))


async def _open_background_window(browser: Any, width: Optional[int] = None,
                                  height: Optional[int] = None) -> Any:
    """A new page in its own window behind the person's app, without activating Chrome."""
    from nodriver.cdp import target as cdp_target
    # A Browser has .connection up to 0.48 and is itself a Connection from 0.50.
    target_id = await (getattr(browser, "connection", None) or browser).send(cdp_target.create_target(
        "about:blank", new_window=True, background=True, width=width, height=height,
    ))
    await browser.update_targets()
    for tab in browser.tabs:
        if str(tab.target.target_id) == str(target_id):
            return tab
    raise RuntimeError("Chrome did not list the window it was asked to open")


async def _open_background_tab(browser: Any) -> Any:
    """A new page as a tab behind the active one, in a window Chrome picks, without activating Chrome.

    Chrome has no way to name the window: it uses the browser window it considers current.
    Callers read back where the tab landed (_window_of) instead of assuming.
    """
    from nodriver.cdp import target as cdp_target
    target_id = await (getattr(browser, "connection", None) or browser).send(cdp_target.create_target(
        "about:blank", background=True,
    ))
    await browser.update_targets()
    for tab in browser.tabs:
        if str(tab.target.target_id) == str(target_id):
            return tab
    raise RuntimeError("Chrome did not list the tab it was asked to open")


async def _keep_painting_behind(tab: Any) -> Optional[str]:
    """Let a tab behind another one in its window keep reading as visible. None, or why not.

    A background tab is document.hidden and runs no requestAnimationFrame, which pages notice
    and which stops lazy content. Focus emulation makes the page behave as the focused one
    (visible, painting, document.hasFocus() true) without activating the tab or Chrome:
    measured 0 ms frontmost, where Target.activateTarget and Page.bringToFront both made
    Chrome the front app for about 2 s. It belongs to the tab's own CDP session.
    """
    from nodriver.cdp import emulation as cdp_emulation
    try:
        await _wait(tab.send(cdp_emulation.set_focus_emulation_enabled(True)), timeout=5,
                    what="Emulation.setFocusEmulationEnabled")
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


async def _window_of(browser: Any, tab: Any) -> Optional[int]:
    """The id of the window a tab is in, or None when Chrome would not say."""
    from nodriver.cdp import browser as cdp_browser
    with contextlib.suppress(Exception):
        window_id, _bounds = await _wait(
            (getattr(browser, "connection", None) or browser).send(
                cdp_browser.get_window_for_target(tab.target.target_id)),
            timeout=3, what="Browser.getWindowForTarget")
        return int(window_id)
    return None


async def _page_visibility(tab: Any) -> Optional[str]:
    """What the page itself reads as document.visibilityState, or None when unreadable."""
    with contextlib.suppress(Exception):
        state = await _wait(evaluate_isolated(tab, "document.visibilityState"), timeout=3,
                            what="visibility read")
        return state if state in ("visible", "hidden") else None
    return None


def _frontmost_pid() -> Optional[int]:
    if sys.platform != "darwin":
        return None
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                               timeout=2, check=False).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "pid", front], capture_output=True,
                              text=True, timeout=2, check=False).stdout
    except Exception:
        return None
    found = re.search(r'"pid"=(\d+)', info)
    return int(found.group(1)) if found else None


async def _win_focus_back_from_popup(browser_pid: int, app: str, seconds: float = 1.5) -> None:
    """window.open activates Chrome, and no flag or CDP call prevents that. So after input on
    a headed browser, watch briefly and give focus straight back if this Chrome took it.

    Only this browser's own process counts, so a person switching to any other app, their own
    Chrome included, is left alone. Keys typed in the moment before it returns still reach
    Chrome: this narrows that window and does not close it.
    """
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if await asyncio.to_thread(_frontmost_pid) == browser_pid:
                await asyncio.to_thread(subprocess.run, ["open", "-a", app],
                                        capture_output=True, timeout=3, check=False)
                return
            await asyncio.sleep(0.02)
    except Exception:
        pass


def _frontmost_app() -> Optional[str]:
    """The foreground app's bundle path on macOS. None anywhere else, or if it can't be read."""
    if sys.platform != "darwin":
        return None
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                               timeout=2, check=False).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "bundlepath", front], capture_output=True,
                              text=True, timeout=2, check=False).stdout
    except Exception:
        return None
    found = re.search(r'"LSBundlePath"="([^"]+)"', info)
    return found.group(1) if found else None


async def _give_focus_back(app: Optional[str]) -> None:
    """A headed Chrome takes the foreground when its window opens, away from whatever the
    person was using. Best effort: a launch must never fail over this."""
    if not app:
        return
    try:
        await asyncio.sleep(0.3)  # the window activates a moment after CDP answers
        if await asyncio.to_thread(_frontmost_app) not in (None, app):
            await asyncio.to_thread(subprocess.run, ["open", "-a", app],
                                    capture_output=True, timeout=3, check=False)
    except Exception:
        pass


@dataclass(frozen=True)
class _LaunchChoice:
    """Which Chrome a launch runs, decided before anything writes to its profile.

    `beta` means Chrome Beta for the Dock icon. `note` is the profile's part of the launch
    result: why it stays on Google Chrome, or why it stays on Beta."""
    executable: Optional[str]
    beta: bool = False
    note: str = ""


def _launch_choice(profile_path: Optional[Path], headless: bool,
                   extra_args: Optional[list] = None) -> _LaunchChoice:
    """Chrome Beta only for a profile it cannot damage: see profile_version.beta_verdict.
    Must run before clean_profile_state or the window-state wipe, which write to the profile
    and would make a new one look used."""
    chrome = _default_chrome()
    beta = dock_icon.chosen(headless=headless)
    if not beta:
        return _LaunchChoice(chrome)
    try:
        allowed, note = profile_version.beta_verdict(profile_path, chrome=chrome, beta=beta,
                                                     extra_args=extra_args)
    except Exception as exc:
        allowed, note = False, f"the profile could not be checked ({_exc_text(exc)})"
    return _LaunchChoice(beta if allowed else chrome, beta=allowed, note=note)


async def _use_dock_icon_browser(config: Config, headless: bool, choice: _LaunchChoice) -> str:
    """Point a headed launch at Chrome Beta, which carries the project's Dock icon, when
    MCP_STEALTH_DOCK_ICON=1 asks for it (macOS) and the profile allows it.

    Returns what to add to the launch result: nothing when the switch is off or the launch is
    headless, otherwise which browser it is and why. Never waits: an icon an update removed
    goes back on in the background while Beta starts.
    """
    refusal = "" if choice.beta else choice.note
    try:
        picked = await asyncio.to_thread(
            dock_icon.prepare, str(config.browser_executable_path or ""), headless=headless,
            profile_refusal=refusal)
    except Exception as exc:
        return f" [Dock icon: Google Chrome, because {_exc_text(exc)}]"
    if picked.executable:
        config.browser_executable_path = picked.executable
        version = f" {picked.version}" if picked.version else ""
        tails = [t for t in (picked.note, choice.note) if t]
        tail = "".join(f"; {t}" for t in tails)
        return f" [Dock icon: Chrome Beta{version}{tail}]"
    return f" [Dock icon: {picked.note}]" if picked.note else ""


def _profile_version_refusal(profile_path: Optional[Path], executable: Optional[str]) -> str:
    """Why `executable` must not open this profile, or "": see profile_version. A profile last
    opened by Chrome Beta is refused to an older Google Chrome rather than damaged by it."""
    try:
        executable = str(executable or "")
        return profile_version.refusal(profile_path, executable,
                                       beta=executable.startswith(str(dock_icon.BETA_APP) + "/"))
    except Exception:
        return ""


def _default_chrome() -> Optional[str]:
    """The Chrome a launch runs without the Dock icon: nodriver's own choice, as Config makes it."""
    with contextlib.suppress(Exception):
        from nodriver.core.config import find_chrome_executable
        return str(find_chrome_executable())
    return find_chrome_binary()


def _cdp_call(method: str, params: Optional[dict] = None):
    """A raw CDP command for nodriver's connection.send, answering Chrome's result as sent.
    Used where nodriver's typed objects lag Chrome's fields, as cookies do."""
    result = yield {"method": method, "params": params or {}}
    return result


@dataclass
class _CookieMove:
    """A profile's cookies on their way to Chrome 158's encryption; see cookie_migration.

    `flag`: launch with the protection turned off. `backup`: after the launch, set the
    cookies in this file again and check them. `note` goes into the launch result."""
    plan: Any
    flag: bool = False
    backup: Optional[Path] = None
    note: str = ""


def _cookie_flag_arg(config: Config) -> str:
    return _disable_features_arg(config, (cookie_migration.FEATURE,))


async def _prepare_cookie_move(profile_path: Optional[Path], config: Config) -> _CookieMove:
    """Before Chrome 158 opens a profile holding v10 cookies: read them with the protection
    off, keep them in a backup, and only then let the launch go without it. Never raises;
    whatever goes wrong before the backup is complete means the flag for this launch."""
    executable = str(config.browser_executable_path or "")
    plan = await asyncio.to_thread(cookie_migration.plan, profile_path, list(config.browser_args or []),
                                   executable)
    if plan.kind == "none":
        return _CookieMove(plan)
    if plan.kind == "flag":
        return _CookieMove(plan, flag=True, note=f" [cookies: {plan.reason}]")
    if plan.kind == "resume":
        return _CookieMove(plan, backup=Path(str(plan.marker["backup"])))
    try:
        return await _move_cookies_out(Path(profile_path), config, plan, executable)
    except Exception as exc:
        return _CookieMove(plan, flag=True, note=_cookie_move_failed(_exc_text(exc)))


def _cookie_move_failed(why: str) -> str:
    return (f" [cookies NOT moved to Chrome 158 encryption: {why}. This launch opens the profile with "
            f"the protection turned off, so nothing is lost, and the next launch tries again]")


async def _move_cookies_out(profile: Path, config: Config, plan: Any, executable: str) -> _CookieMove:
    """Steps 1 to 4 of cookie_migration: copy the databases, read every cookie with the flag,
    write the backup, close that Chrome, and mark the move pending."""
    ensure_dirs()
    name = f"{cookie_migration.BACKUP_PREFIX}-{cookie_migration.label(profile)}-{time.strftime('%Y%m%d-%H%M%S')}"
    db_copy = STORAGE_STATE_DIR / f"{name}-db"
    databases = cookie_migration.cookie_databases(profile, plan.subprofile)
    copied = await asyncio.to_thread(cookie_migration.backup_databases, databases, profile, db_copy)
    reader_config = Config(
        user_data_dir=str(profile), headless=True,
        browser_args=[f"--profile-directory={plan.subprofile}", "--no-default-browser-check",
                      "--disable-restore-session-state", "--hide-crash-restore-bubble", *_LEAN_FLAGS],
    )
    reader_config.browser_executable_path = executable
    reader_config.add_argument(_cookie_flag_arg(reader_config))
    reader = None
    cookies: list = []
    problem = ""
    try:
        reader = await _start_chrome(reader_config)
        answer = await asyncio.wait_for(
            (getattr(reader, "connection", None) or reader).send(_cdp_call("Storage.getCookies")),
            timeout=30)
        cookies = list((answer or {}).get("cookies") or [])
    except Exception as exc:
        problem = f"reading them with the protection turned off failed ({_exc_text(exc)})"
    closed = await _ensure_browser_dead(reader, profile) if reader is not None else {"verified": True}
    if not closed.get("verified"):
        return _CookieMove(plan, flag=True, note=_cookie_move_failed(
            f"the Chrome that read them did not end ({_kill_note(closed) or 'not verified'})"))
    if not problem and len(cookies) < plan.rows:
        problem = (f"Chrome read {len(cookies)} of the {plan.rows} cookies in the profile with the "
                   "protection turned off")
    if problem:
        # The flag may have stopped working, in which case Chrome deleted what it could not
        # read: the databases go back as they were.
        await asyncio.to_thread(cookie_migration.restore_databases, db_copy, profile, copied)
        return _CookieMove(plan, flag=True, note=_cookie_move_failed(
            f"{problem}; the cookie databases were put back from {db_copy}"))
    backup = STORAGE_STATE_DIR / f"{name}.json"
    document = cookie_migration.backup_document(cookies, profile, executable)
    await asyncio.to_thread(cookie_migration.write_private, backup, json.dumps(document))
    # Only now may this Chrome open the profile without the flag: every cookie is in the backup.
    cookie_migration.write_marker(profile, {
        "state": "pending", "backup": str(backup), "database_copy": str(db_copy),
        "count": len(cookies), "major": profile_version.binary_version(executable)[0],
        "started_at": time.time()})
    return _CookieMove(plan, backup=backup)


async def _finish_cookie_move(browser: Any, profile: Path, move: _CookieMove) -> tuple[bool, str]:
    """Steps 5 and 6: set the backed-up cookies in the Chrome just started without the flag,
    read them back and compare. (True, note) once all are there and the move is done;
    (False, text) otherwise, with the marker left pending so the next launch sets them again."""
    if move.backup is None:
        return True, move.note
    connection = getattr(browser, "connection", None) or browser
    try:
        document = json.loads(move.backup.read_text(encoding="utf-8"))
        cookies = list(document.get("cdp_cookies") or [])
        params = [cookie_migration.cookie_params(c) for c in cookies]
        refused = []
        for start in range(0, len(params), 200):
            chunk = params[start:start + 200]
            try:
                await connection.send(_cdp_call("Storage.setCookies", {"cookies": chunk}))
            except Exception:
                for one in chunk:  # one bad cookie fails the whole call; find which
                    try:
                        await connection.send(_cdp_call("Storage.setCookies", {"cookies": [one]}))
                    except Exception as exc:
                        refused.append(f"{one.get('name')}@{one.get('domain')}: {_exc_text(exc)}")
        answer = await connection.send(_cdp_call("Storage.getCookies"))
        back = {cookie_migration.key(c) for c in (answer or {}).get("cookies") or []}
    except Exception as exc:
        return False, (f"the cookies could not be set again ({_exc_text(exc)}); every one of them is in "
                       f"{move.backup}, storage_state_load({str(move.backup)!r}) sets them, and the "
                       "next launch of this profile tries again")
    missing = [k for k in (cookie_migration.key(c) for c in cookies) if k not in back]
    if missing:
        sample = ", ".join(f"{n}@{d}" for n, d, *_ in missing[:5])
        return False, (f"{len(cookies) - len(missing)} of {len(cookies)} cookies came back after the move to "
                       f"Chrome 158 encryption; missing {sample}{' and more' if len(missing) > 5 else ''}"
                       f"{'; refused: ' + refused[0] if refused else ''}. All {len(cookies)} are in "
                       f"{move.backup}: storage_state_load({str(move.backup)!r}) sets them, and the next "
                       "launch of this profile tries again")
    marker = cookie_migration.read_marker(profile)
    cookie_migration.write_marker(profile, {**marker, "state": "done", "finished_at": time.time()})
    return True, (f" [cookies: migrated {len(cookies)} cookies to Chrome 158 encryption; "
                  f"backup at {move.backup}]")


async def _dock_icon_status() -> dict:
    try:
        return await asyncio.wait_for(asyncio.to_thread(dock_icon.status), timeout=3)
    except Exception as exc:
        return {"state": "unavailable", "detail": _exc_text(exc)}


# ── Saved tabs (restore_tabs) ─────────────────────────────────────────────
# Chrome's own session restore stays off (wipe_window_state, --disable-restore-session-state):
# restored window placement is what opened windows invisible. So the server keeps the list
# itself (tab_record). It is written when this server closes a browser it started, by a
# background writer a moment after tool calls and every _TAB_RECORD_PERIOD seconds, so a killed
# server still leaves the last list it saw, and at stdin EOF, which is what an agent session
# going to sleep looks like, from the DevTools HTTP endpoint since the event loop has closed by
# then. Only for a profile that outlives the close: never persistent=False, privacy or attached.
_TAB_RECORD_READ_TIMEOUT = 1.0
_TAB_RECORD_DEBOUNCE = tab_record.env_number("MCP_STEALTH_TAB_RECORD_DEBOUNCE", 2)
_TAB_RECORD_PERIOD = tab_record.env_number("MCP_STEALTH_TAB_RECORD_PERIOD", 30)
_RESTORE_TABS_MAX_DAYS = tab_record.env_number("MCP_STEALTH_RESTORE_TABS_MAX_DAYS", 30)
# A browser being closed: the background writer leaves it alone for this long, so the windows
# closing one by one are never written over the list the close just recorded.
_TAB_RECORD_CLOSING_SECONDS = 30.0
_CLOSING_ATTR = "_mcp_stealth_closing_at"
_TAB_RECORD_DIRTY: dict[tuple[str, str], float] = {}  # (session, instance) -> last tool call
_tab_record_task: Optional[asyncio.Task] = None


def _tab_record_scope(instance_id: str, profile: Any, privacy: bool) -> Optional[Path]:
    """The profile whose tabs are kept, or None.

    persistent=False has no profile of its own here and privacy promises that nothing is kept.
    An attached browser was not started by this server.
    """
    if privacy or profile is None or instance_id in _ATTACHED_BROWSERS:
        return None
    return Path(str(profile))


def _instance_view(instance_id: str) -> Optional[tuple]:
    """(browser, tab keys in tab order, active key, profile, privacy) in the bound session."""
    if instance_id == BrowserState.current_instance_id:
        browser, tabs, index = (BrowserState.browser, list(BrowserState.tabs),
                                BrowserState.active_tab_index)
        profile, privacy = BrowserState.current_profile_dir, BrowserState.current_privacy
    else:
        snap = BrowserState.instances.get(instance_id)
        if snap is None:
            return None
        browser, tabs, index = snap.browser, list(snap.tabs), snap.active_tab_index
        profile, privacy = snap.profile_dir, snap.privacy
    if browser is None:
        return None
    keys = [_target_key(t) for t in tabs]
    return browser, keys, (keys[index] if 0 <= index < len(keys) else None), profile, privacy


def _mark_closing(browser: Any) -> None:
    """From here on the background writer leaves this browser to the close."""
    with contextlib.suppress(Exception):
        setattr(browser, _CLOSING_ATTR, time.monotonic())


def _closing(browser: Any) -> bool:
    since = getattr(browser, _CLOSING_ATTR, None)
    return since is not None and time.monotonic() - since < _TAB_RECORD_CLOSING_SECONDS


async def _page_targets(browser: Any) -> list[tuple[str, str]]:
    """(target id, url) of every page, in the order Chrome lists them."""
    from nodriver.cdp import target as cdp_target
    infos = await (getattr(browser, "connection", None) or browser).send(cdp_target.get_targets())
    return [(str(info.target_id), info.url or "") for info in infos
            if getattr(info, "type_", "") == "page"]


async def _record_tabs_before_close(instance_id: str, browser: Any, tabs: list, active_index: int,
                                    profile: Any, privacy: bool, reason: str) -> str:
    """Write what a browser this server is closing has open. Never raises; a read that does not
    answer within _TAB_RECORD_READ_TIMEOUT leaves the earlier record as it was."""
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None or browser is None:
        return "not recorded: this profile keeps no tabs"
    _mark_closing(browser)
    keys = [_target_key(t) for t in tabs]
    active = keys[active_index] if 0 <= active_index < len(keys) else None
    try:
        pages = await asyncio.wait_for(_page_targets(browser), timeout=_TAB_RECORD_READ_TIMEOUT)
    except Exception:
        pages = None
    return tab_record.save(instance_id, scope, pages, keys, active, reason, force=True)


def _record_tabs_at_exit(instance_id: str, browser: Any, tabs: list, active_index: int,
                         profile: Any, privacy: bool) -> str:
    """The stdin-EOF form of _record_tabs_before_close: no event loop, so /json/list over HTTP."""
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None or browser is None or getattr(browser, "stopped", False) is True:
        return "not recorded"
    config = getattr(browser, "config", None)
    pages = tab_record.pages_over_http(getattr(config, "host", None), getattr(config, "port", None),
                                       timeout=_TAB_RECORD_READ_TIMEOUT)
    keys = [_target_key(t) for t in tabs]
    active = keys[active_index] if 0 <= active_index < len(keys) else None
    return tab_record.save(instance_id, scope, pages, keys, active, "server exit", force=True)


def _mark_tabs_changed(_tool: str) -> None:
    """After every tool call, in the caller's session: record this instance's tabs a moment later.

    Only a dictionary write here; the read and the write happen in the background writer.
    """
    if _TAB_RECORD_DEBOUNCE <= 0 or BrowserState.browser is None:
        return
    iid = BrowserState.current_instance_id
    if _tab_record_scope(iid, BrowserState.current_profile_dir, BrowserState.current_privacy) is None:
        return
    _TAB_RECORD_DIRTY[(BrowserState.session_key(), iid)] = time.monotonic()
    _ensure_tab_recorder_running()


def _ensure_tab_recorder_running() -> None:
    global _tab_record_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _tab_record_task
    if task is not None and not task.done() and task.get_loop() is loop:
        return
    _tab_record_task = loop.create_task(_tab_record_loop())


async def _record_tabs_in_background(key: str, instance_id: str) -> str:
    token = BrowserState.in_session(key)
    try:
        view = _instance_view(instance_id)
    finally:
        BrowserState.unbind_session(token)
    if view is None:
        return "gone"
    browser, keys, active, profile, privacy = view
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None or _closing(browser):
        return "skipped"
    try:
        first = await asyncio.wait_for(_page_targets(browser), timeout=_TAB_RECORD_READ_TIMEOUT)
        await asyncio.sleep(0.3)
        second = await asyncio.wait_for(_page_targets(browser), timeout=_TAB_RECORD_READ_TIMEOUT)
    except Exception:
        return "unread"
    if _closing(browser):
        return "skipped"
    if sorted(first) != sorted(second):
        # Pages going or arriving between two reads 0.3 s apart: a Quit closing its windows, or
        # a navigation under way. A half-closed list must not replace the record; look again.
        _TAB_RECORD_DIRTY.setdefault((key, instance_id), time.monotonic())
        return "changing"
    return tab_record.save(instance_id, scope, second, keys, active, "while running", force=False)


def _recordable_instances() -> list[tuple[str, str]]:
    found = []
    for key in BrowserState.session_keys():
        token = BrowserState.in_session(key)
        try:
            for iid in [BrowserState.current_instance_id, *BrowserState.instances]:
                view = _instance_view(iid)
                if view is not None and _tab_record_scope(iid, view[3], view[4]) is not None:
                    found.append((key, iid))
        finally:
            BrowserState.unbind_session(token)
    return found


async def _tab_record_loop() -> None:
    last_sweep = time.monotonic()
    while True:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        now = time.monotonic()
        due = [k for k, at in list(_TAB_RECORD_DIRTY.items()) if now - at >= _TAB_RECORD_DEBOUNCE]
        for k in due:
            _TAB_RECORD_DIRTY.pop(k, None)
        if _TAB_RECORD_PERIOD > 0 and now - last_sweep >= _TAB_RECORD_PERIOD:
            # Pages change without a tool call too: a person navigating a visible window, or a
            # page redirecting itself.
            last_sweep = now
            with contextlib.suppress(Exception):
                due += [k for k in _recordable_instances() if k not in due]
        for key, iid in due:
            try:
                await _record_tabs_in_background(key, iid)
            except asyncio.CancelledError:
                return
            except Exception:
                continue


from . import _app as _app_module  # noqa: E402

_app_module.AFTER_TOOL_HOOKS[:] = [
    hook for hook in _app_module.AFTER_TOOL_HOOKS
    if getattr(hook, "__name__", "") != "_mark_tabs_changed"] + [_mark_tabs_changed]


async def _load_saved_tab(tab: Any, url: str) -> dict:
    doc_state, disarm = await _arm_document_watch(tab)
    try:
        await asyncio.wait_for(tab.get(url), timeout=BROWSER_NAV_TIMEOUT)
        await _wait_load_complete(tab, 10)
        return await _observe_navigation(tab, url, disarm())
    finally:
        disarm()


async def _reopen_saved_tabs(
    instance_id: str, browser: Any, main_tab: Any, profile: Any, privacy: bool, headless: bool,
    fingerprint: dict, launch_url: Optional[str], window_width: Optional[int] = None,
    window_height: Optional[int] = None,
) -> tuple[list, int, dict, str]:
    """Reopen the tabs saved for this instance and profile. Never raises.

    Returns every page in tab order (main_tab first), the index to select, the report and a
    one-line summary. With no launch url (about:blank) the first saved page loads in main_tab and
    the saved selection is selected again. With a launch url, that page stays first and
    selected, the saved pages follow in their order, and one equal to it is not opened twice.
    Each further page opens as tab_new opens one: its own background window when headed, a tab
    when headless, so no window takes the keyboard.
    """
    report: dict[str, Any] = {"reopened": [], "failed": []}
    tabs, select = [main_tab], 0

    def done(summary: str) -> tuple[list, int, dict, str]:
        report["summary"] = summary
        return tabs, select, report, f" [restore_tabs: {summary}]"

    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None:
        kind = ("a privacy profile" if privacy else "an attached browser"
                if instance_id in _ATTACHED_BROWSERS else "a persistent=False launch")
        return done(f"nothing to reopen: {kind} keeps no tabs")
    entry, why = await asyncio.to_thread(tab_record.load, instance_id, scope)
    if entry is None:
        return done(f"nothing to reopen: {why}")
    age = tab_record.age_text(entry.get("saved_at"))
    report["saved"] = f"{age}, at {entry.get('reason') or 'an unknown moment'}"
    try:
        days = (time.time() - float(entry.get("saved_at"))) / 86400
    except (TypeError, ValueError):
        days = None
    if days is None or (_RESTORE_TABS_MAX_DAYS > 0 and days > _RESTORE_TABS_MAX_DAYS):
        return done(f"nothing reopened: the saved list is {age}, older than "
                    f"{_RESTORE_TABS_MAX_DAYS:g} days (MCP_STEALTH_RESTORE_TABS_MAX_DAYS)")
    saved = [t.get("url") if isinstance(t, dict) else None for t in entry["tabs"]]
    active = entry.get("active") if isinstance(entry.get("active"), int) else None
    use_main = not launch_url or launch_url == "about:blank"
    todo: list[tuple[int, str]] = []
    for position, url in enumerate(saved):
        if not tab_record.keep_url(url):
            report["failed"].append({"saved_index": position, "url": tab_record.shown_url(url),
                                     "error": "not reopened: only http, https and file pages are"})
        elif not use_main and url == launch_url:
            report.setdefault("skipped", []).append(
                {"saved_index": position, "url": tab_record.shown_url(url),
                 "note": "already open as the launch url"})
        else:
            todo.append((position, url))
    slots: list[dict] = []
    for n, (position, url) in enumerate(todo):
        slot = {"saved_index": position, "url": url, "tab": None, "error": None}
        slots.append(slot)
        if use_main and n == 0:
            slot["tab"] = main_tab
            continue
        try:
            opened = (await _wait(browser.get("about:blank", new_tab=True), timeout=10,
                                  what="open a tab") if headless
                      else await _wait(_open_background_window(browser, window_width, window_height),
                                       timeout=10, what="open a window"))
            await _apply_fingerprint_to_tab(opened, fingerprint)
            slot["tab"] = opened
        except Exception as exc:
            slot["error"] = f"could not open a tab: {_exc_text(exc)}"
    loads = [s for s in slots if s["tab"] is not None]
    results = await asyncio.gather(*(_load_saved_tab(s["tab"], s["url"]) for s in loads),
                                   return_exceptions=True)
    for slot, result in zip(loads, results):
        slot["observed"] = result
    # The order tab_list will show: the browser's own list, as _refresh_tabs reads it.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(browser.update_targets(), timeout=5)
    tabs = list(browser.tabs)
    listed = {_target_key(t) for t in tabs}
    for tab in [main_tab, *(s["tab"] for s in slots if s["tab"] is not None)]:
        if _target_key(tab) not in listed:
            listed.add(_target_key(tab))
            tabs.append(tab)
    position_of = {_target_key(t): i for i, t in enumerate(tabs)}
    for slot in slots:
        row: dict[str, Any] = {"saved_index": slot["saved_index"],
                               "url": tab_record.shown_url(slot["url"])}
        observed = slot.get("observed")
        if slot["tab"] is not None:
            row["tab"] = position_of.get(_target_key(slot["tab"]))
        if slot["error"]:
            row["error"] = slot["error"]
        elif isinstance(observed, BaseException):
            row["error"] = f"the page did not load: {_exc_text(observed)}"
        elif observed.get("load_failure"):
            row["error"] = f"the page did not load ({observed['load_failure']})"
            row["final_url"] = tab_record.shown_url(observed.get("final_url"))
        else:
            row.update(final_url=tab_record.shown_url(observed.get("final_url")),
                       status=observed.get("status"), ready_state=observed.get("ready_state"))
        report["failed" if "error" in row else "reopened"].append(row)
    report["reopened"].sort(key=lambda r: r["saved_index"])
    report["failed"].sort(key=lambda r: r["saved_index"])
    note = ""
    if use_main and active is not None:
        chosen = next((s for s in slots if s["saved_index"] == active), None)
        if chosen is not None and chosen["tab"] is not None:
            select = position_of.get(_target_key(chosen["tab"]), 0)
        else:
            note = "; the tab that was selected could not be reopened, so tab 0 is"
    if headless and select:
        with contextlib.suppress(Exception):
            await _wait(tabs[select].activate(), timeout=5, what="select the saved tab")
    report["selected"] = select
    if entry.get("not_kept"):
        report["not_kept"] = entry["not_kept"]
    total = len(saved)
    summary = f"reopened {len(report['reopened'])} of {total} saved tab(s)"
    if report["failed"]:
        summary += f", {len(report['failed'])} failed"
    if report.get("skipped"):
        summary += f", {len(report['skipped'])} already open as the launch url"
    summary += f" (saved {report['saved']}); tab {select} is selected{note}"
    return done(summary)


@mcp.tool()
async def browser_launch(
    url: str = "about:blank",
    headless: bool = False,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    window_width: int = 1280,
    window_height: int = 800,
    persistent: Optional[bool] = None,
    lang: str = "en-US",
    extra_args: Optional[list[str]] = None,
    storage_state_path: Optional[str] = None,
    testing_mode: bool = False,
    auto_verify: bool = True,
    user_data_dir: Optional[str] = None,
    profile_directory: Optional[str] = None,
    fingerprint: Optional[dict] = None,
    privacy: bool = False,
    profile_conflict: str = "fallback",
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
    profile_mode: Literal["default", "light"] = "default",
    restore_tabs: bool = False,
) -> str:
    """Launch stealth Chrome via nodriver. Creates persistent profile by default.

    Reports which profile it actually used (actual_profile, session_state_source)
    and the observed postconditions of the initial load (final_url,
    redirect_chain, status, ready_state) — reporting the requested URL after a
    redirect is what left callers reading a page they never asked for.

    Args:
        url: initial URL to load
        headless: run without UI (many sites detect headless — prefer False). A visible
            window opens behind the app in use and does not take the keyboard. A click that
            calls window.open still brings Chrome forward for about 130 ms before focus returns.
            With MCP_STEALTH_DOCK_ICON=1 in the server's environment, on macOS it runs from a
            copy of Chrome with its own Dock icon. The copy is made in the background and a
            launch never waits for it: when it is not ready this launch uses Chrome itself and
            the result says so. Off by default.
            When the person quits it (Cmd+Q, Dock > Quit) or closes every window, the server
            ends that Chrome within a few seconds, and the next call says it was closed and
            why; call browser_launch again to continue. tab_close on the last tab does not.
        proxy: "http://user:pass@host:port" or "socks5://host:port"
        user_agent: override UA string
        window_width, window_height: viewport size
        persistent: reuse the profile at ~/.mcp-stealth/profile (for the "main"
            instance; other instances get ~/.mcp-stealth/profiles/<id>).
            Left unset it defaults to True — EXCEPT under testing_mode, which
            defaults to an ephemeral throwaway profile. Pass persistent=True
            explicitly to keep state during a testing_mode run.
        lang: browser language
        extra_args: additional Chromium flags
        storage_state_path: load cookies/localStorage from JSON before first nav
        profile_mode: "light" saves memory and network by not loading images and
            turning off background Chrome services. For reading ordinary pages.
            Pages can notice images failing to load (img.naturalWidth stays 0), and
            layout or vision tools see no pictures, so keep "default" on bot-protected
            sites or when looking at the page matters.
        testing_mode: 2-5× faster startup+nav for perf/regression testing —
            disables image loading, background throttling dampers, translate,
            notifications, media autoplay. WARNING: reduces stealth — not for
            anti-bot work (sites can detect missing images as automation signal).
        auto_verify: if True (default), automatically detect Cloudflare /
            Turnstile challenges after the initial load and dispatch a
            CDP-level click on the checkbox. Caps at 2 attempts ~6s total —
            never loops. Set False to opt out.
        user_data_dir: launch Chrome against an EXISTING user profile root
            (e.g. "~/Library/Application Support/Google/Chrome"). Overrides
            persistent + the default MCP profile. The target Chrome instance
            (if any) MUST be closed first — locked profiles are detected
            upfront and refused with the lock-holder PID. Supports ~ expansion.
        profile_directory: when paired with user_data_dir, picks a sub-profile
            inside it (e.g. "Default", "Profile 21"). Without this, Chrome
            uses "Default". Helpful to drive a specific persona without
            cloning the profile. Use list_chrome_profiles to enumerate.
        fingerprint: explicit CDP overrides (user_agent, accept_language,
            platform, timezone). Overrides a pinned identity for this launch.
        privacy: start from an empty profile that is deleted when the browser closes,
            so nothing from this browser's history, logins or cookies is present or
            kept. The identity is still the real browser and machine unless fingerprint
            overrides it. A proxy written as a pool (a|b) picks one member. Incompatible
            with user_data_dir.
            To use a logged-in account, leave this off: the default keeps logins between
            launches, and persistent=False with privacy=False tries something without
            saving a login.
        profile_conflict: what to do when the profile is already held by another
            Chrome. "fallback" (default, legacy behaviour) uses a separate
            per-process profile and says so; "fail" refuses with the holding
            PID; "clone" launches from an integrity-checked read-only copy of
            it. Nothing on disk is ever deleted or overwritten either way.
        expect_url, expect_status: validate the initial load — see navigate().
            An unmeasured value fails the check rather than passing it.
        restore_tabs: reopen the tabs this instance had open on this profile when this server
            last closed it (browser_close, close_instance, the idle timeout, Quit, or the
            server's own exit when its client goes away). Pages open behind the app in use, as
            tab_new opens them, and the tab that was selected is selected again. Without a url
            the first saved page loads in the first tab; with a url, that page stays first and
            selected and the saved pages follow. The result lists each page as reopened (final
            URL, status) or failed (why), with secret URL parameters masked, and says plainly
            when there is no saved list. It never fails the launch. Persistent profiles only:
            persistent=False and privacy=True keep no tabs. The list is kept in
            ~/.mcp-stealth/window-state/<instance>-tabs.json, readable by you only;
            browser_close(forget_tabs=True) removes it.
    """
    # Local testing wants a clean room by default: a testing_mode run that
    # silently accumulated state in the real login profile was both a surprise
    # and a slow way to make tests depend on each other.
    if persistent is None:
        persistent = not testing_mode
        persistent_source = (
            "default (testing_mode → ephemeral; pass persistent=true to keep state)"
            if testing_mode else "default"
        )
    else:
        persistent_source = "explicit"
    # No early "already running" return here. It short-circuited the reuse path
    # below — the one that probes CDP and honours the caller's url — so a launch
    # against a live browser answered "already running" and left the caller on
    # whatever page happened to be open, believing the navigation had happened.
    if (not BrowserState.is_up() and BrowserState.browser is not None
            and BrowserState.current_instance_id not in _ATTACHED_BROWSERS):
        # is_up() false does not mean Chrome stopped: closing the last tab empties tabs while
        # the process runs and holds its profile. Launching over it loses the only handle,
        # and a privacy profile deleted now is one that Chrome simply writes back.
        _stale_dir, _stale_privacy = BrowserState.current_profile_dir, BrowserState.current_privacy
        _reap = await _ensure_browser_dead(BrowserState.browser, _stale_dir)
        if _close_incomplete(_reap):
            return err(f"the previous browser could not be ended: {_close_failure(_reap)}. "
                       f"Its handle and profile were kept.")
        _stale_tabs = list(BrowserState.tabs)
        BrowserState.reset()
        _clear_tab_scoped_state(_stale_tabs)
        if _stale_privacy:
            BrowserState.current_profile_dir, BrowserState.current_privacy = _stale_dir, True
    if not BrowserState.is_up() and BrowserState.current_privacy and BrowserState.current_profile_dir:
        stale_profile = BrowserState.current_profile_dir
        if not await _remove_privacy_profile(stale_profile):
            return err(f"could not remove stale privacy profile before relaunch: {stale_profile}"
                       f"{_removal_failure(stale_profile)}")
        BrowserState.current_privacy = False
        BrowserState.current_headless = False
        BrowserState.current_fingerprint = {}
        BrowserState.current_proxy = None

    if _LAUNCH_LOCK.locked():
        return err("another launch is already in progress — wait for it to finish")

    async with _LAUNCH_LOCK:
        # Re-check inside the lock (race with another concurrent call).
        # Reusing a live browser is not just an optimisation: starting a second
        # Chrome against the same profile is what produces "CDP unreachable",
        # and the half-started process is what gets orphaned.
        if BrowserState.is_up():
            _answered = False
            try:
                # is_up() only proves a handle exists. The socket can be gone
                # while Chrome still runs, so make CDP actually answer.
                _tab = BrowserState.active_tab()
                await _wait(_tab.evaluate("1", return_by_value=True),
                            timeout=5, what="reuse probe")
                _answered = True
                reuse_head = f"reusing the running browser ({len(BrowserState.tabs)} tab(s))"
                if restore_tabs:
                    reuse_head += ("; restore_tabs NOT applied, the running browser keeps its own "
                                   "tabs and nothing was reopened")
                if profile_mode != "default":
                    reuse_head += (f"; profile_mode={profile_mode!r} NOT applied, it only "
                                   f"takes effect on a new launch")
                _doc_state, _disarm = await _arm_document_watch(_tab)
                try:
                    if url and url != "about:blank":
                        # Honour the caller's url. Returning "already running" while
                        # silently ignoring it left them on the wrong page believing
                        # the navigation had happened.
                        await _wait(_tab.get(url), what="reuse navigate")
                        try:
                            await _wait(_tab.wait(), timeout=10, what="reuse settle")
                        except Exception:
                            pass
                    # The reuse path is a navigation too: its redirects were
                    # exactly as invisible as a fresh launch's.
                    _observed = await _observe_navigation(_tab, url, _disarm())
                finally:
                    _disarm()
                _observed["profile"] = {
                    "actual_profile": str(BrowserState.current_profile_dir)
                    if BrowserState.current_profile_dir else None,
                    "session_state_source": "reused running browser — profile unchanged",
                }
                return _nav_result(
                    reuse_head, _observed,
                    expect_url=expect_url, expect_status=expect_status,
                )
            except Exception as e:
                # The browser answered, so what failed was the navigation: a bad or slow
                # URL. Ending it here would lose every tab over a typo.
                if _answered and not transport_lost(str(e)):
                    return err(
                        f"the running browser could not load {url}: {_exc_text(e)}. "
                        f"It was left open, with its tabs."
                    )
                # The handle is stale. Relaunching on top of a half-dead Chrome
                # is exactly the collision to avoid, so end it first.
                _stale_privacy = (BrowserState.current_profile_dir
                                  if BrowserState.current_privacy else None)
                _reap = await _ensure_browser_dead(
                    BrowserState.browser, BrowserState.current_profile_dir)
                if _reap.get("still_alive"):
                    return err(
                        f"a previous browser is unreachable ({type(e).__name__}: {e}) and its "
                        f"PID(s) {_reap['still_alive']} could not be ended. Kill them, then "
                        f"retry — relaunching now would collide with them."
                    )
                BrowserState.reset()
                if _stale_privacy is not None and not await _remove_privacy_profile(_stale_privacy):
                    BrowserState.current_profile_dir, BrowserState.current_privacy = _stale_privacy, True
                    return err(f"could not remove stale privacy profile before relaunch: {_stale_privacy}"
                               f"{_removal_failure(_stale_privacy)}")

        ensure_dirs()

        # Build effective extra_args (we may inject --profile-directory into them).
        merged_extra_args = list(extra_args or [])
        if privacy and (user_data_dir or profile_directory or any(
            (arg or "").startswith("--user-data-dir=") for arg in merged_extra_args
        )):
            return err("privacy mode cannot reuse user_data_dir or profile_directory")
        if privacy:
            persistent = False
        try:
            effective_proxy, effective_fingerprint, identity_source = _launch_identity(
                BrowserState.current_instance_id,
                proxy=proxy,
                user_agent=user_agent,
                fingerprint=fingerprint,
                privacy=privacy,
            )
        except Exception as e:
            return err(f"identity setup failed: {e}")

        # If the user passed user_data_dir explicitly, that takes precedence over
        # both `persistent` and any --user-data-dir already in extra_args.
        explicit_udd: Optional[Path] = None
        # Decided before anything writes to the profile; see _launch_choice.
        launch_choice: Optional[_LaunchChoice] = None
        if user_data_dir:
            explicit_udd = Path(user_data_dir).expanduser()
            if not explicit_udd.exists():
                return err(
                    f"user_data_dir does not exist: {explicit_udd}. "
                    "Check the path. Common defaults: "
                    "~/Library/Application Support/Google/Chrome (macOS), "
                    "~/.config/google-chrome (Linux), "
                    "%LOCALAPPDATA%/Google/Chrome/User Data (Windows). "
                    "Use list_chrome_profiles to discover."
                )
            holder_pid = chrome_lock_holder_pid(explicit_udd)
            if holder_pid is not None:
                return err(
                    f"user_data_dir {explicit_udd} is currently in use by Chrome PID {holder_pid}. "
                    "Close that Chrome window first (a single Chrome process locks the whole "
                    "user-data-dir, regardless of profile_directory). "
                    "Or use clone_chrome_profile to take a snapshot copy of the profile, "
                    "or attach_to_chrome(port=...) if Chrome was started with --remote-debugging-port."
                )
            # Validate sub-profile if specified
            if profile_directory:
                try:
                    pd_path = _direct_child(
                        explicit_udd, profile_directory, "profile_directory"
                    )
                except ValueError as e:
                    return err(str(e))
                if not pd_path.exists():
                    avail = [p.name for p in explicit_udd.iterdir()
                             if p.is_dir() and (p / "Preferences").exists()][:15]
                    return err(
                        f"profile_directory {profile_directory!r} not found inside {explicit_udd}. "
                        f"Available: {avail}"
                    )
                merged_extra_args.append(f"--profile-directory={profile_directory}")
            else:
                merged_extra_args.extend(_profile_directory_args(explicit_udd, merged_extra_args))
            launch_choice = _launch_choice(explicit_udd, headless, merged_extra_args)
            refused = _profile_version_refusal(explicit_udd, launch_choice.executable)
            if refused:
                return err(refused)
            persistent = False  # we're managing user_data_dir ourselves; skip default profile flow
            profile_path = explicit_udd
            used_fallback = False
        else:
            # Validate user-supplied --user-data-dir BEFORE launch (saves a 30s timeout).
            # Many users pass a Chrome profile path in extra_args that's currently
            # held by their daily Chrome — fail fast with actionable advice.
            custom_udd: Optional[Path] = None
            for arg in merged_extra_args:
                if arg.startswith("--user-data-dir="):
                    custom_udd = Path(arg.split("=", 1)[1]).expanduser()
                    break
            if custom_udd is not None:
                holder_pid = chrome_lock_holder_pid(custom_udd) if custom_udd.exists() else None
                if holder_pid is not None:
                    return err(
                        f"Profile directory is locked by another Chrome process: "
                        f"{custom_udd} (PID {holder_pid}). Close that Chrome window first, "
                        f"OR use clone_chrome_profile to make a snapshot copy, "
                        f"OR use the user_data_dir parameter (with same fail-fast lock check), "
                        f"OR omit --user-data-dir to use the default MCP profile, "
                        f"OR use attach_to_chrome(port=...) if Chrome was started with debug port."
                    )

            # Each instance has one deterministic profile, and a conflict over it is
            # settled by the caller's profile_conflict. Only "main" takes part in
            # process affinity: a second HTTP session must not repoint it, or the
            # primary's next launch lands on a different cookie jar and its logins
            # look wiped.
            resolved_profile, profile_info = _resolve_launch_profile(
                BrowserState.current_instance_id,
                persistent=bool(persistent),
                profile_conflict=profile_conflict,
            )
            if profile_info.get("error"):
                return err(profile_info["error"])
            # None for an ephemeral launch. Recording the shared profile here instead made
            # browser_close scan it and signal whichever Chrome really was using it: the
            # browser of another session, with its logins.
            profile_path = resolved_profile
            used_fallback = bool(persistent) and "conflict_with_pid" in profile_info
            if persistent and BrowserState.current_instance_id == "main":
                from .state import set_profile_affinity
                set_profile_affinity(profile_path)

            # Friendly upfront check on the default profile too (after resolve_default_profile
            # has potentially picked a fallback). If somehow the resolved one is still locked
            # by a live process, refuse with actionable error rather than hanging.
            if persistent:
                holder_pid = chrome_lock_holder_pid(profile_path)
                if holder_pid is not None:
                    return err(
                        f"Default MCP profile is locked by PID {holder_pid}. "
                        f"This usually means another mcp-stealth-chrome instance is running. "
                        f"Run `pkill -f mcp-stealth-chrome` then retry, or use persistent=False "
                        f"for an ephemeral throwaway profile."
                    )
                # Before the window-state wipe below writes to it: a refused profile stays as it was.
                launch_choice = _launch_choice(profile_path, headless, merged_extra_args)
                refused = _profile_version_refusal(profile_path, launch_choice.executable)
                if refused:
                    return err(refused)
                clean_profile_state(profile_path)  # only clears stale locks
                # Selectively wipe corrupt window-placement / session-restore
                # state. Idempotent + cheap (a few file writes), preserves
                # cookies/login. Saves us from "window 0×0, hidden" bugs that
                # survive macOS sleep/wake cycles. See state.wipe_window_state.
                try:
                    from .state import wipe_window_state as _wws
                    _wws(profile_path)
                except Exception:
                    pass

        # Determine final user_data_dir for Config (None = nodriver picks a temp dir).
        privacy_profile: Optional[Path] = None
        if privacy:
            privacy_profile = _new_privacy_profile(BrowserState.current_instance_id)
            profile_path = privacy_profile
            final_udd = str(privacy_profile)
        elif explicit_udd is not None:
            final_udd: Optional[str] = str(explicit_udd)
        elif persistent:
            final_udd = str(profile_path)
        else:
            final_udd = None

        # nodriver 0.50 never passes Config.lang on, and macOS Chrome ignores --lang. The
        # override applied to the tab below is what actually changes it.
        if lang and lang != "en-US" and not effective_fingerprint.get("accept_language"):
            effective_fingerprint = {**effective_fingerprint, "accept_language": lang}
        config = Config(
            user_data_dir=final_udd,
            headless=headless,
            lang=(effective_fingerprint.get("accept_language", "").split(",")[0] or lang),
            browser_args=list(merged_extra_args),
        )
        # Extra flags to suppress any first-run / restore / notification interrupts
        config.add_argument("--hide-crash-restore-bubble")
        config.add_argument("--disable-session-crashed-bubble")
        config.add_argument("--disable-restore-session-state")
        config.add_argument("--no-default-browser-check")
        for _flag in _LEAN_FLAGS:
            config.add_argument(_flag)
        if effective_fingerprint.get("user_agent"):
            config.add_argument(f"--user-agent={effective_fingerprint['user_agent']}")
        if effective_proxy:
            config.add_argument(f"--proxy-server={effective_proxy}")
        config.add_argument(f"--window-size={window_width},{window_height}")
        if headless:
            _screen_arg = _headless_screen_arg(window_width, window_height, merged_extra_args)
            if _screen_arg:
                config.add_argument(_screen_arg)
        # Force a deterministic on-screen position. Without this, Chrome
        # restores its last saved position from the persistent profile —
        # which after a macOS sleep/wake cycle is often off-screen or 0×0,
        # making the window invisible and CDP Browser.getWindowForTarget
        # return -32000 "window not found". Override unless the user
        # already supplied their own --window-position via extra_args.
        if not any((a or "").startswith("--window-position=")
                    for a in merged_extra_args):
            config.add_argument("--window-position=100,100")
        if profile_mode == "light":
            for _flag in LIGHT_MODE_FLAGS:
                config.add_argument(_flag)
        if testing_mode:
            for _flag in (
                "--blink-settings=imagesEnabled=false",
                _disable_features_arg(config, ("Translate", "BackForwardCache", "AcceptCHFrame")),
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-ipc-flooding-protection",
                "--disable-notifications",
                "--autoplay-policy=user-gesture-required",
                "--mute-audio",
            ):
                config.add_argument(_flag)

        browser: Optional[Browser] = None
        if launch_choice is None:
            launch_choice = _launch_choice(profile_path, headless, merged_extra_args)
        dock_note = await _use_dock_icon_browser(config, headless, launch_choice)
        refused = _profile_version_refusal(profile_path, config.browser_executable_path)
        if refused:
            await _remove_privacy_profile(privacy_profile)
            return err(refused)
        with contextlib.suppress(Exception):
            dock_note += profile_version.upgrade_note(profile_path, config.browser_executable_path)
        cookie_move = await _prepare_cookie_move(profile_path, config)
        if cookie_move.flag:
            config.add_argument(_cookie_flag_arg(config))
        if not cookie_move.backup:
            dock_note += cookie_move.note
        # Read before Chrome starts, while the person's app is still the frontmost one.
        previous_app = None if headless else await asyncio.to_thread(_frontmost_app)
        if not headless:
            for _flag in _BACKGROUND_WINDOW_FLAGS:
                config.add_argument(_flag)
        try:
            browser = await _start_chrome(config)
            if not headless:
                await _open_background_window(browser, window_width, window_height)
        except asyncio.TimeoutError:
            await _safe_stop_browser(browser)
            await _remove_privacy_profile(privacy_profile)
            return err(
                f"launch timed out after {BROWSER_LAUNCH_TIMEOUT}s — "
                f"profile {profile_path} may be locked by another Chrome, or "
                f"Chrome is hung. Kill any stale Chrome processes and retry."
            )
        except asyncio.CancelledError:
            await _safe_stop_browser(browser)
            await _remove_privacy_profile(privacy_profile)
            raise
        except Exception as e:
            # "CDP unreachable" means the process very likely STARTED and only
            # the websocket failed — so _safe_stop_browser, which speaks CDP,
            # cannot end it. Reaping our own PID here is the difference between
            # a failed launch and a failed launch plus an orphan nobody owns.
            reap = getattr(e, "reap_report", {})
            await _remove_privacy_profile(privacy_profile)
            # Diagnose common nodriver "Failed to connect to browser" — usually
            # means Chrome started but its CDP websocket couldn't be reached
            # (profile locked, port collision, AV interference).
            msg = str(e)
            hint = ""
            if "Failed to connect" in msg or "websocket" in msg.lower() or "connection refused" in msg.lower():
                holder = chrome_lock_holder_pid(profile_path) if persistent else None
                ext = find_external_chrome_pids()
                parts = [f"launch failed (Chrome started but CDP unreachable): {msg}"]
                if reap.get("killed"):
                    parts.append(
                        f"→ Reaped the Chrome this launch started (PID(s) {reap['killed']}), "
                        f"so it is not left behind.")
                if reap.get("still_alive"):
                    parts.append(
                        f"→ WARNING: PID(s) {reap['still_alive']} survived and are orphaned.")
                if holder is not None:
                    parts.append(f"→ Profile {profile_path} is locked by PID {holder}.")
                elif ext:
                    parts.append(f"→ Other Chrome processes running: PIDs {ext[:5]}{'...' if len(ext)>5 else ''}.")
                parts.append(
                    "Retried once. Chrome did not open its debugging port in time, which "
                    "usually means the machine is busy or Chrome is updating, so trying "
                    "again shortly often works."
                    + (" If this profile is in use, persistent=False or "
                       "clone_chrome_profile gives an isolated one." if persistent else "")
                )
                hint = " ".join(parts)
            return err(hint or f"launch failed: {e}")

        BrowserState.browser = browser

        if cookie_move.backup is not None:
            moved, moved_text = await _finish_cookie_move(browser, Path(str(profile_path)), cookie_move)
            if not moved:
                return err(f"the browser is running, but its cookies were not all moved: {moved_text}")
            dock_note += moved_text

        if storage_state_path:
            try:
                await _apply_storage_state(BrowserState.browser, storage_state_path)
            except asyncio.CancelledError:
                await _safe_stop_browser(BrowserState.browser)
                BrowserState.reset()
                await _remove_privacy_profile(privacy_profile)
                raise
            except Exception as e:
                await _safe_stop_browser(BrowserState.browser)
                BrowserState.reset()
                await _remove_privacy_profile(privacy_profile)
                return err(f"storage_state load failed: {e}")

        try:
            await asyncio.sleep(0.5)
            main = await _first_live_tab(BrowserState.browser)
            await _apply_fingerprint_to_tab(main, effective_fingerprint)
            if profile_mode == "light" or testing_mode:
                # The first document after startup still loads images despite the flag;
                # every later one obeys it.
                await asyncio.wait_for(main.get("about:blank"), timeout=BROWSER_NAV_TIMEOUT)
            _doc_state, _disarm = await _arm_document_watch(main)
            try:
                await asyncio.wait_for(main.get(url), timeout=BROWSER_NAV_TIMEOUT)
                await _wait_load_complete(main, 3)  # best effort; ready_state is reported
                # Saying "Loaded /admin" after a redirect to /login leaves the caller
                # reading a page it never asked for, and expect_* went unchecked.
                _launch_observed = await _observe_navigation(main, url, _disarm())
            finally:
                _disarm()
        except asyncio.TimeoutError:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            await _remove_privacy_profile(privacy_profile)
            return err(
                f"initial nav timed out after {BROWSER_NAV_TIMEOUT}s — Chrome "
                f"started but couldn't load {url}. Check network/proxy."
            )
        except asyncio.CancelledError:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            await _remove_privacy_profile(privacy_profile)
            raise
        except Exception as e:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            await _remove_privacy_profile(privacy_profile)
            return err(f"initial nav failed: {e}")
        BrowserState.tabs = [main]
        BrowserState.active_tab_index = 0
        BrowserState.current_profile_dir = profile_path
        BrowserState.current_headless = headless
        BrowserState.current_privacy = privacy
        BrowserState.current_fingerprint = dict(effective_fingerprint)
        BrowserState.current_proxy = effective_proxy
        # A stdio browser_launch is not idle-reaped: it is what the person is looking at,
        # and the server exits with its client anyway. The timeout has to be cleared and not
        # merely the reaper left off, because any spawn_browser starts the reaper, which
        # then closed this browser too. Over HTTP a client can vanish, so the reaper is what
        # eventually ends its browser.
        BrowserState.current_idle_timeout = DEFAULT_IDLE_TIMEOUT if BrowserState.http_mode() else 0
        if BrowserState.http_mode():
            _ensure_idle_reaper_running()
        BrowserState.window_closed.pop(BrowserState.current_instance_id, None)
        if not headless:
            _watch_windows(browser)
        suffix = (
            f" [{profile_info.get('session_state_source')}]"
            if used_fallback else ""
        )

        # Post-launch health check — degenerate window detection.
        # After a macOS sleep/wake cycle (or some persistent-profile state
        # corruption) Chrome can come up with outerWidth==0, position
        # off-screen, or visibilityState=='hidden'. Tools work but every
        # mouse_click_xy / set_viewport_size fails with cryptic CDP errors.
        # Detect once + try a single bring_to_front; report rather than fix
        # silently so the caller knows when to call browser_recover().
        health_suffix = ""
        if not headless:
            try:
                probe = await _wait(main.evaluate(
                    "JSON.stringify({w:window.outerWidth,h:window.outerHeight,"
                    "v:document.visibilityState,sx:window.screenX,sy:window.screenY})"
                ), timeout=2.0, what="health probe")
                hv = json.loads(probe) if isinstance(probe, str) else (probe or {})
                if hv.get("w", 1) == 0 or hv.get("h", 1) == 0 or hv.get("v") == "hidden":
                    # One nudge — restore the window without raising it, and re-probe. Taking
                    # the foreground to check on a window is what this launch must not do.
                    with contextlib.suppress(Exception):
                        _window_id, _bounds = await main.get_window()
                        await main.set_window_state(state="normal")
                    try:
                        probe2 = await _wait(main.evaluate(
                            "JSON.stringify({w:window.outerWidth,h:window.outerHeight,"
                            "v:document.visibilityState})"
                        ), timeout=2.0, what="health re-probe")
                        hv2 = json.loads(probe2) if isinstance(probe2, str) else (probe2 or {})
                    except Exception:
                        hv2 = hv
                    if hv2.get("w", 1) == 0 or hv2.get("h", 1) == 0 or hv2.get("v") == "hidden":
                        health_suffix = (
                            f" [⚠ window degenerate (w={hv2.get('w')},h={hv2.get('h')},"
                            f"v={hv2.get('v')}) — likely sleep/wake corruption. "
                            f"Run browser_recover() then browser_launch() to recover.]"
                        )
            except Exception:
                pass  # health probe is best-effort; never block launch

        verify_suffix = ""
        if auto_verify:
            try:
                verify_suffix = await asyncio.wait_for(_auto_verify_cf(main), timeout=30.0)
            except Exception:
                verify_suffix = ""

        # Put the window back where it was last time. Headless has no window to
        # place, and an explicit --window-position means the caller has already
        # decided — neither should be overridden.
        place_suffix = ""
        if not headless and not any(
            (a or "").startswith("--window-position=") for a in merged_extra_args
        ):
            try:
                placed = await _restore_window_bounds(BrowserState.current_instance_id, main)
                if placed.get("restored"):
                    act = placed.get("actual") or {}
                    place_suffix = (
                        f" [window {'clamped to a connected display' if placed.get('clamped') else 'restored'}"
                        f" at ({act.get('left')},{act.get('top')}) {act.get('width')}x{act.get('height')}]"
                    )
            except Exception:
                pass  # placement is a convenience; never fail a launch over it

        restore_suffix = ""
        if restore_tabs:
            try:
                _all_tabs, _select, _report, restore_suffix = await _reopen_saved_tabs(
                    BrowserState.current_instance_id, browser, main, profile_path, privacy,
                    headless, effective_fingerprint, url, window_width, window_height)
                BrowserState.tabs = _all_tabs
                BrowserState.active_tab_index = _select
                _launch_observed = {**_launch_observed, "restore_tabs": _report}
            except Exception as e:
                restore_suffix = f" [restore_tabs: failed: {_exc_text(e)}]"

        await _give_focus_back(previous_app)
        return _nav_result(
            f"Browser launched (headless={headless}, persistent={persistent}, "
            f"privacy={privacy}, identity={identity_source}). Loaded",
            _launch_observed,
            expect_url=expect_url, expect_status=expect_status,
            suffix=f"{suffix}{verify_suffix}{health_suffix}{place_suffix}{restore_suffix}{dock_note}",
        )


def _forget_tabs_note(instance_id: str, profile: Any, privacy: bool) -> str:
    """Remove the saved tab list of an instance and profile, for forget_tabs=True."""
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None:
        return " No tab list is kept for this profile."
    if tab_record.forget(instance_id, scope):
        return " Its saved tab list was removed."
    return " It had no saved tab list."


@mcp.tool()
async def browser_close(forget_tabs: bool = False) -> str:
    """Close the browser and free the profile lock.

    The open tabs are saved first (persistent profiles only), so browser_launch(restore_tabs=True)
    can reopen them. forget_tabs=True closes without saving and removes the saved list.
    """
    # The handle, not is_up(): closing the last tab makes is_up() false while Chrome
    # still runs and holds the profile, and that is not "not running".
    if BrowserState.browser is None:
        stale_privacy_profile = (
            BrowserState.current_profile_dir if BrowserState.current_privacy else None
        )
        if stale_privacy_profile is not None:
            removed = await _remove_privacy_profile(stale_privacy_profile)
            if not removed:
                return err(
                    "Browser was not running and stale privacy profile cleanup failed: "
                    f"{stale_privacy_profile}{_removal_failure(stale_privacy_profile)}"
                )
        BrowserState.current_headless = False
        BrowserState.current_privacy = False
        BrowserState.current_fingerprint = {}
        BrowserState.current_proxy = None
        forgot = ""
        if forget_tabs:
            gone = tab_record.forget_instance(BrowserState.current_instance_id)
            forgot = (f" Removed the saved tab list of instance {BrowserState.current_instance_id!r}."
                      if gone else " There was no saved tab list.")
        if stale_privacy_profile is not None:
            return ok(f"Browser was not running; stale privacy profile removed.{forgot}")
        return ok(f"Browser was not running.{forgot}")
    was_up = BrowserState.is_up()
    privacy_profile = (
        BrowserState.current_profile_dir if BrowserState.current_privacy else None
    )
    closed_profile = BrowserState.current_profile_dir
    closed_tabs = list(BrowserState.tabs)
    # Remember the placement while the window still exists, so the next launch
    # can return to the monitor it was actually on.
    if not BrowserState.current_headless and closed_tabs:
        try:
            await _save_window_bounds(BrowserState.current_instance_id, closed_tabs[0])
        except Exception:
            pass
    tabs_note = ""
    if forget_tabs:
        _mark_closing(BrowserState.browser)
    else:
        saved = await _record_tabs_before_close(
            BrowserState.current_instance_id, BrowserState.browser, closed_tabs,
            BrowserState.active_tab_index, closed_profile, BrowserState.current_privacy,
            "browser_close")
        if saved.startswith("recorded"):
            tabs_note = f" {saved[0].upper()}{saved[1:]} for browser_launch(restore_tabs=True)."
    _kill = await _ensure_browser_dead(BrowserState.browser, closed_profile)
    if _close_incomplete(_kill):
        return err(f"close failed: {_close_failure(_kill)}; handle kept so a retry can reach it")
    if forget_tabs:
        tabs_note = _forget_tabs_note(BrowserState.current_instance_id, closed_profile,
                                      BrowserState.current_privacy)
    # Capture the id BEFORE reset() (which may change current_instance_id) so
    # we can drop any attached-browser bookkeeping for it.
    _closed_iid = BrowserState.current_instance_id
    BrowserState.reset()
    BrowserState.current_headless = False
    BrowserState.current_privacy = False
    BrowserState.current_fingerprint = {}
    BrowserState.current_proxy = None
    _ATTACHED_BROWSERS.discard(_closed_iid)
    _clear_tab_scoped_state(closed_tabs)
    # Mark profile as cleanly exited so next launch skips restore dialog
    if privacy_profile is not None:
        removed = await _remove_privacy_profile(privacy_profile)
        if not removed:
            # Keep the cleanup obligation discoverable/retryable. The browser
            # is closed, but a privacy close is not complete until the residue
            # is gone.
            BrowserState.current_profile_dir = privacy_profile
            BrowserState.current_privacy = True
            return err(
                f"Browser closed, but privacy profile cleanup failed: {privacy_profile}"
                f"{_removal_failure(privacy_profile)}"
            )
    elif closed_profile is not None:
        clean_profile_state(closed_profile)
    head = "Browser closed" if was_up else "Browser had no live tabs or had already exited; ended it"
    return ok(f"{head}; privacy profile removed.{_kill_note(_kill)}" if privacy_profile
              else f"{head}.{_kill_note(_kill)}{tabs_note}")


@mcp.tool()
async def browser_recover() -> str:
    """Force-recover from a stuck browser state.

    Escape hatch when browser_close() can't run (graceful shutdown depends
    on internal state that may be corrupt). Steps:
    1. Best-effort browser.stop() — ignore any errors
    2. Kill orphan Chrome PIDs whose argv references the active profile
       (SIGTERM, then SIGKILL after 2s if still alive) — only PIDs spawned
       against THIS MCP profile, never the user's daily Chrome
    3. Reset the current BrowserState (background instances keep running)
    4. Clear devtools / dialog caches
    5. Wipe stale Singleton* lock files in all known profile dirs

    Always succeeds — never raises. Use this when normal close hangs or
    returns an error you can't diagnose. After this, browser_launch()
    again to start fresh.
    """
    steps: list[str] = []

    # 1. Best-effort stop. Capture profile dir BEFORE reset so step 2 can
    #    target only Chrome PIDs that belong to this profile.
    target_profile = BrowserState.current_profile_dir
    target_tabs = list(BrowserState.tabs)
    target_iid = BrowserState.current_instance_id
    try:
        b = BrowserState.browser
        if b and not getattr(b, "stopped", False):
            try:
                b.stop()
                steps.append("browser.stop() ok")
            except Exception as e:
                steps.append(f"browser.stop() failed: {type(e).__name__}")
    except Exception:
        pass

    # 2. SIGTERM/SIGKILL orphan Chrome PIDs that match this profile. Skips
    #    silently if no profile dir is recorded (attached browser, etc).
    if target_profile is not None:
        try:
            from .state import find_chrome_pids_by_profile
            pids = find_chrome_pids_by_profile(target_profile)
        except Exception:
            pids = []
        if pids:
            import signal as _signal
            terminated = 0
            killed = 0
            for pid in pids:
                try:
                    os.kill(pid, _signal.SIGTERM)
                    terminated += 1
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if terminated:
                # Give SIGTERM up to 2s to take effect, then SIGKILL stragglers.
                await asyncio.sleep(2.0)
                for pid in pids:
                    try:
                        os.kill(pid, 0)  # liveness probe
                    except ProcessLookupError:
                        continue
                    except Exception:
                        continue
                    try:
                        os.kill(pid, _signal.SIGKILL)
                        killed += 1
                    except Exception:
                        pass
            steps.append(f"chrome PIDs: {terminated} SIGTERM, {killed} SIGKILL")
        else:
            steps.append("no orphan chrome PIDs")

    # 3. Reset state regardless
    try:
        BrowserState.reset()
        steps.append("state reset")
    except Exception as e:
        steps.append(f"state reset failed: {type(e).__name__}: {e}")

    # 4. Clear only this instance's tab state. Clearing the global sets here
    # silently de-armed background instances and made their next start call
    # stack duplicate handlers on top of the still-live ones.
    _clear_tab_scoped_state(target_tabs)
    _ATTACHED_BROWSERS.discard(target_iid)

    # 5. Sweep stale locks + wipe corrupt window-state across all known profile
    #    dirs. Selective wipe (NOT a full nuke) — preserves cookies/login but
    #    removes the window_placement record + Sessions/ files that survive
    #    sleep/wake cycles and cause "window 0×0 hidden" launches.
    from .state import (
        PROFILE_DIR as _PROFILE_DIR, PROFILES_ROOT, per_process_profile,
        wipe_window_state,
    )
    sweep_dirs: list = [_PROFILE_DIR, per_process_profile()]
    try:
        if PROFILES_ROOT.exists():
            for sub in PROFILES_ROOT.iterdir():
                if sub.is_dir():
                    sweep_dirs.append(sub)
    except Exception:
        pass
    cleared = 0
    in_use = 0
    healed = {"prefs": 0, "sessions": 0, "files": 0}
    for pdir in sweep_dirs:
        # A locked profile belongs to a browser that is still running, perhaps another
        # instance or another session's. Its Sessions/ and Preferences are not ours.
        if is_chrome_profile_locked(pdir):
            in_use += 1
            continue
        try:
            clean_profile_state(pdir)  # only removes locks with dead PIDs
            cleared += 1
        except Exception:
            pass
        try:
            r = wipe_window_state(pdir)
            if r.get("prefs"): healed["prefs"] += 1
            healed["sessions"] += r.get("sessions", 0)
            healed["files"] += r.get("files", 0)
        except Exception:
            pass
    steps.append(f"swept {cleared}/{len(sweep_dirs)} profile dirs, left {in_use} in use alone")
    steps.append(
        f"healed window state: {healed['prefs']} prefs, "
        f"{healed['sessions']} sessions, {healed['files']} blobs"
    )

    return ok("recovered: " + " | ".join(steps))


# go_back, go_forward, reload, the tab tools and the human-help tools live in tools/navigation.py.
# navigate and these helpers stay here: launch paths and other tools share them.


def _cdp_resource_type(event: Any) -> str:
    """Resource type of a CDP network event as a plain string ('Document', …).

    nodriver hands back a ResourceType enum whose str() is 'ResourceType.DOCUMENT',
    so comparing str(event.type_) to 'Document' silently never matches.
    """
    value = getattr(event, "type_", None)
    return str(getattr(value, "value", value) or "")


async def _arm_document_watch(tab: Tab) -> tuple[dict, Any]:
    """Watch the main-frame document response for ONE navigation on `tab`.

    Returns (state, disarm). CDP dispatches Network.* only while the domain is
    enabled on that tab, so when the enable fails the state says so and the
    status is reported as unmeasured — never as "no redirect, status unknown but
    probably fine". The handlers close over `state` and never read module state
    at event time, and disarm() removes exactly these three closures: nodriver's
    remove_handler(evt, fn) drops the whole handler list for the event, which
    would silently kill a concurrent network_start / auth_capture capture.

    Network stays enabled afterwards on purpose — it is idempotent, and
    disabling it here would stop any capture the caller armed earlier.
    """
    from nodriver.cdp import network as cdp_network

    state: dict[str, Any] = {
        "armed": False,
        "arm_error": None,
        "request_id": None,
        "redirects": [],
        "status": None,
        "status_text": "",
        "document_url": None,
        "failure": None,
    }
    # In Chrome the main frame's id IS the page target id, so this filters out
    # iframe documents without an extra Page.getFrameTree round-trip.
    main_frame = str(getattr(getattr(tab, "target", None), "target_id", "") or "")

    def _mine(event: Any) -> bool:
        if state["request_id"] is not None:
            return str(getattr(event, "request_id", "")) == state["request_id"]
        if _cdp_resource_type(event) != "Document":
            return False
        frame = str(getattr(event, "frame_id", "") or "")
        # Without a known frame id, fall back to the first document request —
        # that is the navigation the caller just triggered.
        return not main_frame or not frame or frame == main_frame

    async def on_request(event: Any) -> None:
        try:
            if not _mine(event):
                return
            request_id = str(getattr(event, "request_id", ""))
            redirect = getattr(event, "redirect_response", None)
            if redirect is not None and state["request_id"] == request_id:
                # CDP reuses the request id across a redirect and describes the
                # PREVIOUS hop in redirect_response — that is the only place the
                # intermediate status codes ever appear.
                state["redirects"].append({
                    "url": str(getattr(redirect, "url", "") or ""),
                    "status": getattr(redirect, "status", None),
                })
            elif state["request_id"] is None:
                state["request_id"] = request_id
            request = getattr(event, "request", None)
            state["document_url"] = str(getattr(request, "url", "") or "") or state["document_url"]
        except Exception:
            pass

    async def on_response(event: Any) -> None:
        try:
            if state["request_id"] is None:
                return
            if str(getattr(event, "request_id", "")) != state["request_id"]:
                return
            response = getattr(event, "response", None)
            state["status"] = getattr(response, "status", None)
            state["status_text"] = str(getattr(response, "status_text", "") or "")
            state["document_url"] = (
                str(getattr(response, "url", "") or "") or state["document_url"]
            )
        except Exception:
            pass

    async def on_failed(event: Any) -> None:
        try:
            if state["request_id"] is None:
                return
            if str(getattr(event, "request_id", "")) != state["request_id"]:
                return
            state["failure"] = str(getattr(event, "error_text", "") or "loading failed")
        except Exception:
            pass

    handlers = (
        (cdp_network.RequestWillBeSent, on_request),
        (cdp_network.ResponseReceived, on_response),
        (cdp_network.LoadingFailed, on_failed),
    )
    try:
        await _wait(tab.send(cdp_network.enable()), timeout=5, what="Network.enable")
        state["armed"] = True
    except Exception as exc:
        state["arm_error"] = f"{type(exc).__name__}: {exc}"
    if state["armed"]:
        for event_type, handler in handlers:
            tab.add_handler(event_type, handler)

    def disarm() -> dict:
        for event_type, handler in handlers:
            try:
                tab.handlers[event_type].remove(handler)
            except (KeyError, ValueError):
                pass
        return state

    return state, disarm


def _build_nav_observation(
    requested_url: Optional[str],
    doc_state: dict,
    final_url: Optional[str],
    ready_state: Optional[str],
    probe_error: Optional[str] = None,
) -> dict:
    """Assemble the observed postconditions, naming what could not be measured."""
    redirects = [hop for hop in doc_state.get("redirects", []) if hop.get("url")]
    status = doc_state.get("status")
    observed: dict[str, Any] = {
        "requested_url": requested_url,
        "final_url": final_url or None,
        "redirect_chain": redirects,
        "redirected": bool(redirects),
        "status": status,
        "ready_state": ready_state or None,
    }
    if doc_state.get("status_text"):
        observed["status_text"] = doc_state["status_text"]
    if doc_state.get("failure"):
        observed["load_failure"] = doc_state["failure"]

    unmeasured: list[str] = []
    if not final_url:
        unmeasured.append("final_url")
    if not ready_state:
        unmeasured.append("ready_state")
    if status is None:
        unmeasured.append("status")
        if not doc_state.get("armed"):
            reason = (
                f"Network domain could not be enabled ({doc_state.get('arm_error')})"
                if doc_state.get("arm_error")
                else "the document response was not watched"
            )
        elif doc_state.get("failure"):
            reason = f"the document request failed: {doc_state['failure']}"
        elif doc_state.get("request_id") is None:
            reason = (
                "no main-frame document request was seen — about:blank, a "
                "same-document (history/hash) navigation, or a page served "
                "entirely from the back-forward cache emits none"
            )
        else:
            reason = "the document request never reported a response"
        observed["status_source"] = "unmeasured"
        observed["status_note"] = reason
    else:
        observed["status_source"] = "cdp:Network.responseReceived"
    if probe_error:
        observed["probe_error"] = probe_error
    if unmeasured:
        observed["unmeasured"] = unmeasured
    return observed


async def _observe_navigation(
    tab: Tab,
    requested_url: Optional[str],
    doc_state: dict,
) -> dict:
    """Read back where the tab ACTUALLY ended up, after the navigation."""
    final_url = ""
    ready_state = ""
    probe_error = None
    try:
        probe = await _wait(
            evaluate_isolated(tab, "JSON.stringify([window.location.href, document.readyState])"),
            timeout=5,
            what="navigation postcondition probe",
        )
        final_url, ready_state = json.loads(probe)
    except Exception as exc:
        probe_error = f"{type(exc).__name__}: {exc}"
    return _build_nav_observation(
        requested_url, doc_state, final_url, ready_state, probe_error
    )


def _nav_url_matches(pattern: str, url: str) -> bool:
    """expect_url rule: glob when the pattern holds * ? or [, else prefix match.

    Prefix (rather than equality) is what makes the common case usable — an
    expected "https://site/admin" still matches "https://site/admin?tab=1".
    """
    if not pattern:
        return True
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(url, pattern)
    return url == pattern or url.startswith(pattern)


def _nav_expectation_failures(
    observed: dict,
    expect_url: Optional[str],
    expect_status: Optional[int],
) -> list[str]:
    """Check the caller's expectations against MEASURED values only.

    An unmeasured value fails the expectation instead of passing it: reporting
    "expect_status 200 ok" from a status nobody ever read is exactly the quiet
    lie the postconditions exist to remove.
    """
    failures: list[str] = []
    final_url = observed.get("final_url")
    status = observed.get("status")
    if expect_url:
        if not final_url:
            failures.append(
                f"expect_url {expect_url!r} could not be checked — final_url is "
                f"unmeasured ({observed.get('probe_error') or 'no URL read back'})"
            )
        elif not _nav_url_matches(expect_url, final_url):
            failures.append(f"expect_url {expect_url!r} != final_url {final_url!r}")
    if expect_status is not None:
        if status is None:
            failures.append(
                f"expect_status {expect_status} could not be checked — status is "
                f"unmeasured ({observed.get('status_note')})"
            )
        elif int(status) != int(expect_status):
            failures.append(f"expect_status {expect_status} != status {status}")
    return failures


def _nav_result(
    action: str,
    observed: dict,
    *,
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
    suffix: str = "",
) -> str:
    """Render a navigation result: one summary line, then the postconditions."""
    final_url = observed.get("final_url") or "(final_url unmeasured)"
    status = observed.get("status")
    status_part = f"status {status}" if status is not None else "status unmeasured"
    hops = len(observed.get("redirect_chain") or [])
    redirect_part = f", {hops} redirect(s)" if hops else ""
    ready = observed.get("ready_state") or "unmeasured"
    body = json.dumps(observed, indent=2, default=str)
    failures = _nav_expectation_failures(observed, expect_url, expect_status)
    if failures:
        return err(
            f"{action} postcondition failed: " + "; ".join(failures)
            + f"\n{body}"
        )
    # batch_actions and workflow_run count failures by this prefix, so a page that did not
    # load must carry it even though the tool itself ran.
    if observed.get("load_failure"):
        return err(f"{action}: the page did not load ({observed['load_failure']})\n{body}")
    return ok(
        f"{action}: {final_url} [{status_part}{redirect_part}, ready_state {ready}]"
        f"{suffix}\n{body}"
    )


# How long navigate waits for the load event. It reports ready_state either way, because a
# page with one stuck subresource is still a page the caller can use.
NAV_LOAD_WAIT = 15.0


async def _wait_load_complete(tab, timeout: float) -> bool:
    """Poll readyState until complete. nodriver's Tab.wait(t) is only a sleep of t seconds."""
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(Exception):
            state = await asyncio.wait_for(
                evaluate_isolated(tab, "document.readyState"),
                timeout=max(0.2, min(2.0, deadline - time.monotonic())),
            )
            if state == "complete":
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)


@mcp.tool()
async def navigate(
    url: str,
    wait_until: str = "load",
    auto_verify: bool = True,
    focus: bool = False,
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Navigate the active tab to url. wait_until: load|none.

    Reports the OBSERVED postconditions — requested_url, final_url,
    redirect_chain, status, ready_state — because a redirect otherwise leaves
    the caller reading a page it never asked for while the tool claims success.

    auto_verify: if True (default), automatically detect Cloudflare /
    Turnstile challenges after load and click the checkbox naturally
    (CDP-level click, max 2 attempts). Set False to opt out.

    focus: if True, bring the tab's window to the OS foreground after navigating. Off by
    default, because raising the window takes focus from whatever the person is doing.
    tab_focus does the same on request.

    expect_url: fail when final_url does not match. Glob when the pattern
        contains * ? or [ ; otherwise a prefix match, so "https://x/admin"
        accepts "https://x/admin?tab=1". A final_url that could not be read
        FAILS the check rather than passing it.
    expect_status: fail when the document HTTP status differs. An unmeasured
        status (about:blank, same-document navigation) also fails — the tool
        will not claim an expectation it never verified.
    """
    if not BrowserState.is_up():
        return err("Browser not running. Call browser_launch first.")
    if wait_until not in ("load", "none"):
        return err(f"navigate: wait_until must be 'load' or 'none', not {wait_until!r}")
    tab = BrowserState.active_tab()
    with contextlib.suppress(Exception):
        await _watch_dialogs(tab)
    doc_state, disarm = await _arm_document_watch(tab)
    try:
        await tab.get(url)
        if wait_until != "none":
            await _wait_load_complete(tab, NAV_LOAD_WAIT)
        if focus:
            try:
                await tab.activate()
            except Exception:
                pass
            try:
                await tab.bring_to_front()
            except Exception:
                pass
        verify_suffix = ""
        if auto_verify:
            try:
                verify_suffix = await asyncio.wait_for(_auto_verify_cf(tab), timeout=30.0)
            except Exception:
                verify_suffix = ""
        observed = await _observe_navigation(tab, url, disarm())
        return _nav_result(
            "Navigated", observed,
            expect_url=expect_url, expect_status=expect_status, suffix=verify_suffix,
        )
    except Exception as e:
        return err(str(e))
    finally:
        disarm()


_HUMAN_HELP: dict[str, dict[str, Any]] = {}


def _write_private(path: Path, text: str) -> None:
    """Write a file holding credentials, readable by this user only from the first byte."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    # An existing file keeps its old mode through O_CREAT.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


async def _eval_checked(tab, expression: str) -> Any:
    """Evaluate and return the value. A script that threw raises, so its error can't read as one."""
    value = await _wait(tab.evaluate(expression, return_by_value=True), what="page script")
    if isinstance(value, cdp_runtime.ExceptionDetails):
        raise RuntimeError(f"JavaScript threw: {_js_exception_text(value)}")
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, list, dict)):
        return value.value
    return value


# ══════════════════════════════════════════════════════════════════════════
# Snapshot, reading, screenshot, PDF, inspection, frame and scraping tools live in tools/page.py.


# hover, mouse_move, mouse_click_xy, drag_and_drop, press_key, select_option, uncheck, upload_file,
# click_role, assert_clickable and the wait_for tools live in tools/interaction.py. The core click
# and typing tools stay here with the helpers they share.


@mcp.tool()
async def click(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    humanize: bool = False,
    verify: bool = True,
    js_fallback: bool = False,
) -> str:
    """Click an element by ref (from snapshot) or CSS selector with real mouse input.

    Dispatches a trusted mouse event at the element's centre, scrolling it into
    view first if needed. Refuses and names the blocker when the point belongs to
    something else, so an overlay never produces a false "clicked". The result names
    what was used: method=cdp_mouse, or method=js_click after js_fallback.

    Args:
        ref: element ref from browser_snapshot
        selector: CSS selector
        humanize: approach along a curved path and click a random spot inside
        verify: compare the page before and after, and say so when nothing changed
        js_fallback: when the real click cannot land, call element.click() from JS
            instead of refusing. The event has isTrusted=false, which sites can
            detect, and bypasses whatever was covering the element. The result
            states when this happened. Off by default.
    """
    try:
        tab = BrowserState.active_tab()
        el = None
        if ref:
            el = await resolve_ref(ref)
        elif selector:
            el = await tab.query_selector(selector)
        if el is None:
            return err("element not found")
        label = f"click {ref or selector!r}"
        return await _click_and_report(
            tab, el, label, verify, js_fallback=js_fallback, humanize=humanize,
        )
    except Exception as e:
        return err(str(e))


# Probe run ON the target element before clicking. Answers the question the old
# code never asked: "if I click this element's centre, does the event actually
# reach it?" — hit-tests via elementFromPoint and names the blocker when it fails.
_CLICK_PROBE_JS = """
function() {
  const d = (el) => {
    if (!el) return null;
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      const c = el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 3);
      if (c.length) s += '.' + c.join('.');
    }
    return s;
  };
  const r = this.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const inVp = cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight;
  let top = null, hit = false;
  if (inVp) {
    // A shadow root answers for itself. document.elementFromPoint stops at the
    // host, which does not "contain" anything inside its shadow tree.
    const scope = this.getRootNode();
    top = (scope.elementFromPoint ? scope : document).elementFromPoint(cx, cy);
    hit = !!top && (top === this || this.contains(top) || top.contains(this));
  }
  const cs = getComputedStyle(this);
  return JSON.stringify({
    target: d(this),
    rect: {x: Math.round(r.left), y: Math.round(r.top),
           w: Math.round(r.width), h: Math.round(r.height)},
    point: {x: Math.round(cx), y: Math.round(cy)},
    sized: r.width > 0 && r.height > 0,
    in_viewport: inVp,
    hit: hit,
    blocker: hit ? null : d(top),
    disabled: !!(this.disabled || this.getAttribute('aria-disabled') === 'true'),
    hidden: cs.visibility === 'hidden' || cs.display === 'none' ||
            parseFloat(cs.opacity || '1') === 0,
    pointer_events_none: cs.pointerEvents === 'none'
  });
}
"""


# Cheap page fingerprint — used to tell "the click did something" from
# "the call returned ok but nothing moved".
# hash, because a counter going from 1 to 2 changes neither the length nor the node count.
_PAGE_SIG_JS = """
(() => {
  const html = document.body ? document.body.innerHTML : '';
  let hash = 0;
  for (let i = 0; i < html.length; i++) hash = (hash * 31 + html.charCodeAt(i)) | 0;
  return JSON.stringify({
    url: location.href,
    len: html.length,
    hash: hash,
    nodes: document.getElementsByTagName('*').length,
    active: document.activeElement ? document.activeElement.tagName : null
  });
})()
"""


# Same question asked from the clicked element's own tree. The page fingerprint
# reads the main document only, so a click that changed an iframe, a shadow tree,
# or just a checkbox's state used to be reported as "NO page change".
_ELEMENT_SIG_JS = """
function() {
  const root = this.getRootNode();
  const scope = root.documentElement || root;
  return JSON.stringify({
    len: (scope.innerHTML || '').length,
    nodes: scope.querySelectorAll('*').length,
    connected: this.isConnected,
    focused: root.activeElement === this,
    editable: this.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(this.tagName),
    checked: !!this.checked,
    value: typeof this.value === 'string' ? this.value.length : null,
    open: !!this.open,
    aria: ['expanded', 'pressed', 'selected', 'checked']
      .map(a => this.getAttribute('aria-' + a)).join(','),
    cls: typeof this.className === 'string' ? this.className : ''
  });
}
"""


async def _click_probe(el) -> dict:
    """Hit-test the element before clicking. Returns {} if the probe can't run."""
    try:
        v = await apply_isolated(el, _CLICK_PROBE_JS)
        return json.loads(v) if v else {}
    except Exception:
        return {}


async def _page_sig(tab, el=None, pages: bool = False) -> dict:
    """Snapshot a cheap page fingerprint; {} when unavailable.

    With el, also fingerprints the element's own tree. That half reads as {} once
    the element is gone, which correctly compares as a change. With pages, also lists the
    browser's open tabs, since a click that opens one changes nothing on this page.
    """
    try:
        # Bounded: a native dialog opened by the action under test blocks the renderer.
        v = await _wait(evaluate_isolated(tab, _PAGE_SIG_JS), timeout=5.0, what="page read")
        sig = json.loads(v) if v else {}
    except Exception:
        return {}
    if el is not None and sig:
        try:
            own = await apply_isolated(el, _ELEMENT_SIG_JS)
            sig["element"] = json.loads(own) if own else {}
        except Exception:
            sig["element"] = {}
    if pages and sig:
        from nodriver.cdp import target as cdp_target
        with contextlib.suppress(Exception):
            sig["pages"] = {
                str(info.target_id): str(info.url)
                for info in await tab.send(cdp_target.get_targets())
                if info.type_ == "page"
            }
    return sig


def _blocked_reason(p: dict) -> Optional[str]:
    """Human-readable reason the probe says a click can't land — or None."""
    if not p:
        # An empty probe is an unanswered question, not a pass. Clicking anyway would
        # dispatch real input at a point nothing checked.
        return "the hit test could not run (element gone or its page context was lost)"
    if not p.get("sized"):
        return "element has zero size"
    if p.get("hidden"):
        return "element is hidden (display/visibility/opacity)"
    if p.get("disabled"):
        return "element is disabled"
    if not p.get("in_viewport"):
        return f"element centre {p.get('point')} is outside the viewport — scroll it into view first"
    if p.get("pointer_events_none"):
        return "element has pointer-events:none"
    if not p.get("hit"):
        return f"element is obscured by <{p.get('blocker')}> at {p.get('point')}"
    return None


# Second chance for click_text. nodriver's fuzzy find often returns the FIRST
# node containing the text, which on real sites is routinely an off-screen
# marketing/nav duplicate rather than the control the caller means. Refusing
# there is honest but useless — so re-scan for a candidate that is genuinely
# visible and hit-testable, scrolling it into view when that is all that's wrong.
_CLICK_TEXT_RESCUE_JS = r"""
(() => {
  const needle = %s, exact = %s;
  const cands = [];
  const collect = (nodes) => {
    for (const el of nodes) {
      const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
      if (!t) continue;
      const tl = t.toLowerCase();
      if (exact ? tl !== needle : tl.indexOf(needle) === -1) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' ||
          parseFloat(cs.opacity || '1') === 0 || cs.pointerEvents === 'none') continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      cands.push({el: el, t: t, len: t.length, area: r.width * r.height});
    }
  };
  const CLICKABLE = 'a,button,[role="button"],[role="link"],[role="tab"],' +
                    '[role="menuitem"],[onclick],input[type="submit"],' +
                    'input[type="button"],summary,label';
  collect(document.querySelectorAll(CLICKABLE));
  const interactive = cands.length;
  if (!cands.length) collect(document.querySelectorAll('*'));
  if (!cands.length) return JSON.stringify({found: 0});
  // Shortest text wins: the tightest node around the phrase is the real control,
  // not the section wrapper that happens to contain it.
  cands.sort((a, b) => a.len - b.len || a.area - b.area);
  let scrolled = false;
  for (const c of cands.slice(0, 8)) {
    let r = c.el.getBoundingClientRect();
    let cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) {
      c.el.scrollIntoView({block: 'center', inline: 'center'});
      scrolled = true;
      r = c.el.getBoundingClientRect();
      cx = r.left + r.width / 2; cy = r.top + r.height / 2;
      if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) continue;
    }
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(top === c.el || c.el.contains(top) || top.contains(c.el))) continue;
    let d = c.el.tagName.toLowerCase();
    if (c.el.id) d += '#' + c.el.id;
    return JSON.stringify({
      found: cands.length, interactive: interactive, scrolled: scrolled,
      x: Math.round(cx), y: Math.round(cy), target: d, text: c.t.slice(0, 80)
    });
  }
  return JSON.stringify({found: cands.length, interactive: interactive, blocked: true});
})()
"""


async def _click_text_rescue(tab, text: str, exact: bool) -> dict:
    """Find a genuinely clickable element carrying `text`. {} when none."""
    try:
        expr = _CLICK_TEXT_RESCUE_JS % (json.dumps(text.lower()),
                                        "true" if exact else "false")
        v = await evaluate_isolated(tab, expr)
        return json.loads(v) if v else {}
    except Exception:
        return {}


# nodriver 0.50's Tab.find also searches child frames and returns a plain Element with
# frame-local coordinates. Mouse input goes through the root tab, so clicking it would
# land on whatever the page has at those same numbers. Refs carry the frame offset.
_FRAME_TEXT_HINT = (
    "{text!r} is inside an iframe, and text lookups only click in the main frame. "
    "Take a browser_snapshot and click its ref."
)


def _element_is_in_frame(tab: Any, el: Any) -> bool:
    return getattr(el, "tab", tab) is not tab


async def _js_click(el) -> None:
    """element.click() from JS. isTrusted=false — only ever on explicit request."""
    await el.apply("function() { this.click(); }")


def _new_tab_note(before: dict, after: dict) -> str:
    """What a click opened elsewhere. Left out, a working popup button reads as dead."""
    known, now = before.get("pages"), after.get("pages")
    if not isinstance(known, dict) or not isinstance(now, dict):
        return ""
    fresh = [mask_url(url) for target, url in now.items() if target not in known]
    if not fresh:
        return ""
    return (f"; opened {len(fresh)} new tab(s): {', '.join(fresh[:3])} "
            f"(the active tab did not change; tab_list and tab_select reach it)")


def _click_change(before: dict, after: dict) -> Optional[str]:
    """What changed between two fingerprints, as the tail of a result line; None for nothing.

    A real mouse click moves focus onto its target by itself, so that alone is not
    the page reacting; a dead button must still read as "NO page change". Focus
    moving somewhere ELSE is a reaction, and focus arriving on a field is exactly
    what clicking a field is for, so it gets its own wording.
    """
    opened = _new_tab_note(before, after)
    if before.get("url") != after.get("url"):
        return f"; navigated to {after.get('url')}{opened}"
    own_before = {k: v for k, v in (before.get("element") or {}).items() if k != "focused"}
    own_after = {k: v for k, v in (after.get("element") or {}).items() if k != "focused"}
    # With no element fingerprint the caller clicked a raw point, and whether focus
    # moved says nothing about which element it moved to.
    focus_is_ours = bool((after.get("element") or {}).get("focused")) or "element" not in after
    # Focus falling back to <body> is what clicking anything unfocusable does: the button
    # that held focus loses it. Only focus arriving on another element is the page reacting.
    focus_moved = (
        before.get("active") != after.get("active")
        and after.get("active") not in (None, "BODY", "HTML")
        and not focus_is_ours
    )
    changed = (
        before.get("len") != after.get("len")
        or before.get("hash") != after.get("hash")
        or before.get("nodes") != after.get("nodes")
        or own_before != own_after
        or focus_moved
    )
    if changed:
        return f"; page changed (nodes {before.get('nodes')}→{after.get('nodes')}){opened}"
    if opened:
        return opened
    if own_after.get("editable") and (after.get("element") or {}).get("focused"):
        return "; the field took focus"
    return None


_UNREAD_AFTER_CLICK = ("; the clicked element could no longer be read, while the page itself "
                       "shows no change (it may be navigating)")


def _click_outcome(head: str, detail: str, before: dict, after: dict) -> str:
    """Compare fingerprints and say what the click did, never a bare 'clicked'.

    Only called when the caller asked to verify. An empty fingerprint is a page that could
    not be read (a stale context, a navigation under way), so the outcome is unknown; a
    bare "clicked" there read as success, and a caller retried a click that may have worked.
    """
    if not (before and after):
        when = ("before or after" if not (before or after)
                else "before" if not before else "after")
        return ok(
            f"{head} but the outcome could NOT be verified — {detail}. The event was "
            f"dispatched, but the page could not be read {when} the click, so whether it "
            f"reacted is unknown. Read the page (browser_snapshot, get_url) before clicking "
            f"again."
        )
    change = (_UNREAD_AFTER_CLICK if element_unread(before, after)
              else _click_change(before, after))
    if change is not None:
        return ok(f"{head} — {detail}{change}")
    return ok(
        f"{head} but NO page change detected — {detail}. The event was dispatched; "
        f"the page did not react (handler not bound, click swallowed, or the change "
        f"is purely visual)."
    )


# After an action the page is read again at these pauses (0, 50, 120, 220, 330 and 450 ms
# in all) and the reading stops at the first one that differs. A fixed 350 ms was paid by
# every click, although a handler that changes the page has done so before the click is even
# acknowledged. Only "NO page change" needs the whole wait, and it gets a longer one than
# before, so a handler that reacts after 300 ms is not reported as dead.
_VERIFY_PAUSES = (0.0, 0.05, 0.07, 0.10, 0.11, 0.12)
# One more reading after the first change, so a handler that updates the page and then
# navigates or opens a tab is reported with where it ended up.
_VERIFY_SETTLE = 0.06


async def _sig_after_action(tab, before: dict, el=None, pages: bool = False) -> dict:
    """The page fingerprint once the action shows, or after the full wait when it never does.

    Stops reading when a native dialog opens, since reading would block on it: the caller
    checks _dialog_note again afterwards.
    """
    after: dict = {}
    for pause in _VERIFY_PAUSES:
        if pause:
            await asyncio.sleep(pause)
        if _dialog_note(tab):
            return after
        after = await _page_sig(tab, el, pages=pages)
        # Old page, element unreadable: the document went between the two halves of the read.
        # Not an answer; the next pause shows where the page went (key_outcome).
        if element_unread(before, after):
            continue
        if before and after and _click_change(before, after) is not None:
            await asyncio.sleep(_VERIFY_SETTLE)
            if not _dialog_note(tab):
                again = await _page_sig(tab, el, pages=pages)
                if again and not element_unread(before, again):
                    after = again
            return after
    return after


# The probe above tests the centre. A humanized click lands elsewhere in the box, where an
# overlay can cover part of the element, so the spot actually chosen is tested too. Fractions
# of the element's own box, because a frame's elementFromPoint is local to that frame.
_POINT_HIT_JS = """
function() {
  const r = this.getBoundingClientRect();
  const x = r.left + r.width * %f, y = r.top + r.height * %f;
  const scope = this.getRootNode();
  const top = (scope.elementFromPoint ? scope : document).elementFromPoint(x, y);
  return !!top && (top === this || this.contains(top) || top.contains(this));
}
"""


async def _humanized_point(el, pos, centre_x: float, centre_y: float) -> tuple[float, float]:
    """A random spot inside the element that passed a hit test, else the tested centre."""
    for _ in range(5):
        fx, fy = random.uniform(0.3, 0.7), random.uniform(0.3, 0.7)
        x, y = pos.left + pos.width * fx, pos.top + pos.height * fy
        with contextlib.suppress(Exception):
            if (await apply_isolated(el, _POINT_HIT_JS % (fx, fy)) is True
                    and await outer_blocker(el, x, y) is None):
                return x, y
    return centre_x, centre_y


async def _settled_position(el, quiet: int = 2):
    """The element's position once it stops moving (smooth scroll, entry animation).

    Bounded: something animating forever is clicked where it is now. `quiet` is how many
    still 40 ms readings count as stopped.
    """
    pos = await el.get_position()
    still = 0
    for _ in range(30):
        await asyncio.sleep(0.04)
        again = await el.get_position()
        if pos is None or again is None:
            return again
        moved = abs(again.left - pos.left) >= 1 or abs(again.top - pos.top) >= 1
        pos = again
        still = 0 if moved else still + 1
        if still >= quiet:  # one quiet interval can be a smooth scroll's first frame
            break
    return pos


# Without knowing about a dialog, a click on a button that calls alert() hangs forever.
# Page.enable also needs the renderer, so it has to happen before the dialog, which is why
# input tools arm this first. _app.StealthMCP uses the same record to stop any other tool.
_OPEN_DIALOGS = OPEN_DIALOGS


_dialog_note = dialog_note


_DIALOG_WATCHED: set[str] = set()


async def _watch_dialogs(tab) -> None:
    key = _target_key(tab)
    if key in _DIALOG_WATCHED:
        return
    from nodriver.cdp import page as cdp_page
    await asyncio.wait_for(tab.send(cdp_page.enable()), timeout=3.0)

    def opened(event):
        kind = getattr(event, "type_", None)
        _OPEN_DIALOGS[key] = {"type": str(getattr(kind, "value", kind)),
                              "message": str(getattr(event, "message", ""))[:200],
                              "since": time.monotonic()}

    def closed(_event):
        _OPEN_DIALOGS.pop(key, None)

    tab.add_handler(cdp_page.JavascriptDialogOpening, opened)
    tab.add_handler(cdp_page.JavascriptDialogClosed, closed)
    _DIALOG_WATCHED.add(key)


async def _answer_open_dialog(tab, action: str, text: Optional[str]) -> Optional[str]:
    """Answer a dialog that is already open. None when there is none."""
    found = _OPEN_DIALOGS.get(_target_key(tab))
    if not found:
        return None
    from nodriver.cdp import page as cdp_page
    try:
        await _wait(tab.send(cdp_page.handle_java_script_dialog(
            accept=action == "accept", prompt_text=text or "",
        )), timeout=5.0, what="dialog_handle")
    except Exception as exc:
        return err(f"could not answer the open {found['type']} dialog: {_exc_text(exc)}")
    _OPEN_DIALOGS.pop(_target_key(tab), None)
    return ok(f"answered the open {found['type']} dialog ({action})")


def _browser_process_pid(browser: Any) -> Optional[int]:
    """The PID nodriver started, or None for an attached browser, which is not ours to manage."""
    process = getattr(browser, "_process", None)
    pid = getattr(process, "pid", None) or getattr(browser, "_process_pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


_BACKGROUND_TASKS: set[asyncio.Future] = set()


def _spawn_background(coro) -> None:
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)  # the loop holds only a weak reference
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _ignore_task_result(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


async def _dispatch_input(tab, press) -> None:
    """Run input that a native dialog could block, and stop waiting once one does."""
    with contextlib.suppress(Exception):
        await _watch_dialogs(tab)
    key = _target_key(tab)
    if not BrowserState.current_headless:
        pid = _browser_process_pid(BrowserState.browser)
        app = await asyncio.to_thread(_frontmost_app) if pid else None
        if app and await asyncio.to_thread(_frontmost_pid) != pid:
            _spawn_background(_win_focus_back_from_popup(pid, app))
    # The task copies the context here, so the gesture it runs sees this flag and no other.
    gesture = Gesture()
    bound = CURRENT_GESTURE.set(gesture)
    try:
        task = asyncio.ensure_future(press)
    finally:
        CURRENT_GESTURE.reset(bound)
    deadline = time.monotonic() + TOOL_ACTION_TIMEOUT
    while not task.done():
        await asyncio.wait({task}, timeout=0.05)
        if task.done():
            break
        if key in _OPEN_DIALOGS:
            await asyncio.sleep(0.4)  # an armed dialog_handle answers within this
            if key in _OPEN_DIALOGS and not task.done():
                # Left running, not cancelled: it finishes once the dialog is answered, and
                # cancelling a press blocked on mousedown would never send its mouseReleased.
                # Stopped, though: the rest of a path would reach the page after this tool
                # returned, and open the next page's dialog under another tool.
                gesture.stopped = True
                task.add_done_callback(_ignore_task_result)
                return
        elif time.monotonic() > deadline:
            gesture.stopped = True
            task.add_done_callback(_ignore_task_result)
            raise TimeoutError(f"input was not acknowledged within {TOOL_ACTION_TIMEOUT}s")
    task.result()


async def _prepare_target(el) -> tuple[Any, dict, Optional[str], bool]:
    """Scroll `el` into view and hit-test it: (position, probe, why it is unreachable, scrolled)."""
    # First wait out anything still moving it, such as a smooth scroll in progress:
    # a hit test only holds for as long as the element stays put.
    settled = None
    with contextlib.suppress(Exception):
        settled = await _settled_position(el)
    probe = await _click_probe(el)
    scrolled = False
    if probe.get("sized") and not probe.get("in_viewport"):
        with contextlib.suppress(Exception):
            await el.tab.send(cdp_dom.scroll_into_view_if_needed(
                backend_node_id=el.backend_node_id))
            # A smooth scroll is still moving at this point; probing now would
            # refuse an element that is about to be in view.
            settled = await _settled_position(el)
            probe = await _click_probe(el)
            scrolled = True
    with contextlib.suppress(Exception):
        if await ensure_in_root_view(el):
            settled = await _settled_position(el)
            probe = await _click_probe(el)
            scrolled = True
    reason = _blocked_reason(probe)
    pos = None
    if reason is None:
        # It was just watched until it stopped. One more reading says whether it stayed
        # put through the hit test; only an element that moved again is waited out again.
        pos = await el.get_position()
        if pos is None or settled is None or (
                abs(pos.left - settled.left) >= 1 or abs(pos.top - settled.top) >= 1):
            pos = await _settled_position(el)
        if pos is None:
            reason = "element has no on-screen position"
    if reason is None:
        x, y = pos.left + pos.width / 2, pos.top + pos.height / 2
        # Re-probe: the hit test above ran before the page finished moving, so its pass
        # does not carry over when this one cannot run.
        recheck = await _click_probe(el)
        reason = _blocked_reason(recheck) or await outer_blocker(el, x, y)
        probe = recheck or probe
    return pos, probe, reason, scrolled


async def _click_point_of(tab, el, label: str, fx: float, fy: float, approach: bool = False) -> str:
    """Click a fraction of the element's box, under the same honesty rules as _click_and_report."""
    if not (0 <= fx <= 1 and 0 <= fy <= 1):
        return err(f"{label}: NOT clicked — that point is outside the element "
                   f"({fx * 100:g}% x {fy * 100:g}% of its box)")
    pos, probe, reason, scrolled = await _prepare_target(el)
    target = f"target=<{probe.get('target') or 'unknown'}>"
    # The centre passing or failing says little about an edge: only the chosen point counts.
    if reason is not None and not (probe.get("sized") and "obscured" in reason):
        return err(f"{label}: NOT clicked — {reason}. {target} rect={probe.get('rect')}.")
    pos = pos or await _settled_position(el)
    if pos is None:
        return err(f"{label}: NOT clicked — element has no on-screen position")
    x, y = pos.left + pos.width * fx, pos.top + pos.height * fy
    landed = await apply_isolated(el, _POINT_HIT_JS % (fx, fy))
    blocker = await outer_blocker(el, x, y) if landed is True else None
    if landed is not True or blocker:
        return err(f"{label}: NOT clicked — something else covers that point of the element "
                   f"({int(x)},{int(y)}). {target} rect={probe.get('rect')}.")
    before = await _page_sig(tab, el, pages=True)
    if approach:
        await _dispatch_input(tab, humanized_move(tab, x + 120, y - 60, x, y))
        await asyncio.sleep(0.12)
    else:
        await _dispatch_input(tab, move_pointer(tab, x, y))
    if _dialog_note(tab):
        return err(f"{label}: NOT clicked — moving the pointer there opened a dialog{_dialog_note(tab)}")
    await _dispatch_input(tab, tab.mouse_click(x, y))
    head = f"{label}: clicked"
    detail = f"method=cdp_mouse {target} at ({int(x)},{int(y)})" + (" (scrolled into view)" if scrolled else "")
    note = _dialog_note(tab)
    if note:
        return ok(f"{head} — {detail}{note}")
    after = await _sig_after_action(tab, before, el, pages=True)
    note = _dialog_note(tab)
    if note:
        return ok(f"{head} — {detail}{note}")
    return _click_outcome(head, detail, before, after)


async def _click_and_report(
    tab, el, label: str, verify: bool,
    js_fallback: bool = False, humanize: bool = False,
) -> str:
    """Click `el`, then report what actually happened — never a bare 'clicked'.

    nodriver's Element.click() is itself a JS el.click() preceded by flash(), which
    inserts a marker into the page: an untrusted event plus a visible DOM mutation.
    This dispatches real mouse input at the element's position instead. It refuses
    up-front when the hit-test says the click cannot land, and (with verify) says
    plainly when the page did not change.
    """
    pos, probe, reason, scrolled = await _prepare_target(el)
    target = f"target=<{probe.get('target') or 'unknown'}>"
    if reason is not None:
        if not js_fallback:
            return err(
                f"{label}: NOT clicked — {reason}. "
                f"{target} rect={probe.get('rect')}. "
                f"Try: scroll_to / mouse_click_xy at a different point / dismiss the "
                f"overlay / js_fallback=True for an untrusted JS click."
            )
        before = await _page_sig(tab, el, pages=True) if verify else {}
        await _js_click(el)
        head = (f"{label}: clicked via JS FALLBACK (isTrusted=false, real click refused: "
                f"{reason})")
        detail = f"method=js_click {target}"
        if not verify:
            return ok(f"{head} — {detail}")
        return _click_outcome(head, detail, before, await _sig_after_action(tab, before, el, pages=True))

    before = await _page_sig(tab, el, pages=True) if verify else {}
    x, y = pos.left + pos.width / 2, pos.top + pos.height / 2
    if humanize:
        hx, hy = await _humanized_point(el, pos, x, y)
        # The path and the press are two pieces of input, as in the plain click below: a path
        # that opens a dialog must not be followed by a press, and the result has to say so.
        await _dispatch_input(tab, humanized_approach(
            tab, hx, hy, last_pos_ref=BrowserState.last_mouse_xy))
        if _dialog_note(tab):
            return err(f"{label}: NOT clicked — moving the pointer there opened a dialog"
                       f"{_dialog_note(tab)}")
        await asyncio.sleep(random.uniform(0.05, 0.18))  # dwell before pressing, as a hand does
        await _dispatch_input(tab, tab.mouse_click(int(hx), int(hy)))
        where = f"humanized point ({int(hx)},{int(hy)}) inside the element"
    else:
        await _dispatch_input(tab, move_pointer(tab, x, y))
        if _dialog_note(tab):
            return err(f"{label}: NOT clicked — moving the pointer there opened a dialog"
                       f"{_dialog_note(tab)}")
        await _dispatch_input(tab, tab.mouse_click(x, y))
        where = f"({int(x)},{int(y)})"
    head = f"{label}: clicked"
    detail = f"method=cdp_mouse {target} at {where}" + (" (scrolled into view)" if scrolled else "")
    note = _dialog_note(tab)
    if note or not verify:
        return ok(f"{head} — {detail}{note}")
    after = await _sig_after_action(tab, before, el, pages=True)
    note = _dialog_note(tab)
    if note:
        return ok(f"{head} — {detail}{note}")
    return _click_outcome(head, detail, before, after)


@mcp.tool()
async def click_text(text: str, exact: bool = False, verify: bool = True) -> str:
    """Find and click the element whose text matches, and report what really happened.

    Args:
        text: text to match
        exact: True for exact match, False (default) for fuzzy best-match
        verify: compare a page fingerprint before/after and say so when nothing
            changed (default True). This is what distinguishes a real click from
            one that was dispatched into a void.

    Never claims success on finding alone: if the element is obscured, hidden,
    disabled or off-viewport, this does not pretend otherwise — it first looks
    for another element carrying the same text that IS reachable (scrolling it
    into view if needed), and only errors, naming the blocker, when there is
    genuinely nothing clickable.
    """
    label = f"click_text {text!r}"
    try:
        tab = BrowserState.active_tab()
        el = await tab.find(text, best_match=not exact)
        if el is not None and exact:
            # tab.find only ever matches by containment.
            whole = await el.apply("function() { return (this.innerText || this.value || '').trim(); }")
            if str(whole or "").lower() != text.strip().lower():
                el = None
        in_frame = el is not None and _element_is_in_frame(tab, el)
        if in_frame:
            el = None
        if el is not None:
            probe = await _click_probe(el)
            if _blocked_reason(probe) is None:
                return await _click_and_report(tab, el, label, verify)

        # The fuzzy match is unusable (or absent). Look for a reachable twin —
        # on real pages the first text match is often a hidden nav/marketing copy.
        cand = await _click_text_rescue(tab, text, exact)
        if cand.get("x") is not None:
            before = await _page_sig(tab, pages=True) if verify else {}
            try:
                await _dispatch_input(tab, move_pointer(tab, cand["x"], cand["y"]))
            except Exception:
                pass
            if _dialog_note(tab):
                return err(f"{label}: NOT clicked — moving the pointer there opened a dialog"
                           f"{_dialog_note(tab)}")
            await _dispatch_input(tab, tab.mouse_click(cand["x"], cand["y"]))
            note = (f"{label}: clicked — method=cdp_mouse target=<{cand.get('target')}> "
                    f"at {{'x': {cand['x']}, 'y': {cand['y']}}}"
                    + (" (scrolled into view)" if cand.get("scrolled") else "")
                    + (f"; first text match was unreachable, used a visible one "
                       f"of {cand.get('found')} candidates"
                       if el is not None else ""))
            if _dialog_note(tab):
                return ok(note + _dialog_note(tab))
            if not verify:
                return ok(note)
            after = await _sig_after_action(tab, before, pages=True)
            if _dialog_note(tab):
                return ok(note + _dialog_note(tab))
            head, _, detail = note.partition(" — ")
            return _click_outcome(head, detail, before, after)

        if el is None and not cand.get("found"):
            if in_frame:
                return err(_FRAME_TEXT_HINT.format(text=text))
            return err(f"no element with text {text!r}")
        if el is not None:
            # Report why the original match is unusable, plus what the scan saw.
            probe = await _click_probe(el)
            reason = _blocked_reason(probe) or "unclickable"
            extra = (f" Scanned {cand.get('found')} other element(s) with this text; "
                     f"none were reachable." if cand.get("found") else "")
            return err(
                f"{label}: NOT clicked — {reason}. "
                f"target=<{probe.get('target')}> rect={probe.get('rect')}.{extra} "
                f"Try: scroll_to / mouse_click_xy at a different point / dismiss the overlay."
            )
        return err(f"{label}: NOT clicked — found {cand.get('found')} element(s) with "
                   f"this text but every one is hidden or covered. "
                   f"Try assert_clickable to inspect, or mouse_click_xy.")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def fill(ref: Optional[str] = None, selector: Optional[str] = None,
               value: str = "") -> str:
    """Clear a field and type into it with real key presses, then read it back."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        try:
            await el.clear_input()
        except Exception:
            pass
        lines = re.split(r"\r\n|\r|\n", str(value))
        multiline = len(lines) > 1 and (await el.apply(
            "function() { return this.tagName === 'TEXTAREA' || this.isContentEditable; }"
        )) is True
        try:
            if multiline:
                # send_keys types each character, and a newline typed that way adds nothing.
                # Enter is what a person presses, and only here: in a single-line field it
                # would submit the form.
                await apply_isolated(el, "function() { this.focus(); }")
                for index, line in enumerate(lines):
                    if index:
                        await _dispatch_key(el.tab, key_event_params("Enter"))
                    if line:
                        await _type_keys(el.tab, line, press_controls=False)
            else:
                await apply_isolated(el, "function() { this.focus(); }")
                await _type_keys(el.tab, str(value), press_controls=False)
        except Exception:
            await el.set_value(value)
        actual = await el.apply(
            "function() { return this.isContentEditable ? this.innerText : this.value; }"
        )
        wanted = str(value)
        if multiline:
            # A contenteditable may report a paragraph break as two newlines.
            wanted = "\n".join(lines)
            if isinstance(actual, str):
                actual = re.sub(r"\n+", "\n", actual)
                wanted = re.sub(r"\n+", "\n", wanted)
        if isinstance(actual, str) and actual.strip() != wanted.strip():
            is_secret = (await el.apply("function() { return this.type === 'password'; }")) is True
            shown = f"{len(actual)} chars" if is_secret else repr(actual[:80])
            return err(
                f"typed, but the field reads {shown} rather than the "
                f"{len(str(value))}-char value sent. The page rewrote or rejected it "
                f"(input mask, maxlength, framework state)."
            )
        return ok("filled")
    except Exception as e:
        return err(str(e))


# What has focus, seen from outside any open shadow root: "none", "frame", "multi", "single"
# or "other:<tag>".
_FOCUSED_FIELD_JS = """
(() => {
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  if (!a || a === document.body || a === document.documentElement) return 'none';
  if (a.tagName === 'IFRAME' || a.tagName === 'FRAME') return 'frame';
  if (a.isContentEditable || a.tagName === 'TEXTAREA') return 'multi';
  const noText = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'color', 'range'];
  if (a.tagName === 'INPUT' && !noText.includes(a.type)) return 'single';
  return 'other:' + a.tagName.toLowerCase();
})()
"""


async def _type_keys(tab, text: str, mean_delay: Optional[float] = None,
                     press_controls: bool = True) -> None:
    """Press a key per character. Element.send_keys sends only a char event: a page sees a
    keypress with an empty key and no keydown at all, which nothing a person types produces.

    press_controls=False is for filling one field: Enter would submit its form and Tab would
    leave it, so a newline adds nothing and fill's read-back reports the difference.
    """
    from nodriver.cdp import input_ as cdp_input
    for ch in re.sub(r"\r\n?", "\n", text):
        if ch == "\n":
            if press_controls:
                await _dispatch_key(tab, key_event_params("Enter"))
        elif ch == "\t" and press_controls:
            await _dispatch_key(tab, key_event_params("Tab"))
        elif ch.isascii() and ch.isprintable():
            await _dispatch_key(tab, key_event_params(ch))
        else:
            await tab.send(cdp_input.dispatch_key_event(type_="char", text=ch))
        if mean_delay is not None:
            await asyncio.sleep(max(0.02, random.gauss(mean_delay, 0.08)))


@mcp.tool()
async def type_text(text: str, humanize: bool = False,
                     mean_delay: float = 0.12) -> str:
    """Type into the focused element, one real key press per character.

    Each character sends keydown and keyup, as a keyboard does. A newline presses Enter in
    a textarea or contenteditable and is refused in a single-line field, where it would
    submit the form. Use press_key for shortcuts when no field has focus.
    Use humanize for Gaussian delays between keys.
    """
    try:
        tab = BrowserState.active_tab()
        raw = await tab.evaluate(_FOCUSED_FIELD_JS, return_by_value=True)
        kind = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        if kind == "none":
            return err("no field has focus, so nothing was typed — click or focus one first, "
                       "or use press_key for a shortcut")
        if isinstance(kind, str) and kind.startswith("other:"):
            return err(f"the focused <{kind[6:]}> does not take text, so nothing was typed")
        if kind == "single" and re.search(r"[\r\n]", text):
            return err("a newline in a single-line field would submit the form; nothing was "
                       "typed. Type the text without it, then use press_key('Enter').")
        await _type_keys(tab, text, mean_delay if humanize else None)
        return ok("typed")
    except Exception as e:
        return err(str(e))


async def _dispatch_key(tab: Any, params: dict) -> None:
    """A full key press: down (with text, so it acts on the page), then up."""
    from nodriver.cdp import input_ as cdp_input

    fields = {k: v for k, v in params.items() if k != "text"}
    text = params.get("text")
    # On macOS select-all is an editor command, not something Ctrl/Cmd+A does by itself.
    commands = None
    if (sys.platform == "darwin" and str(params.get("key", "")).lower() == "a"
            and params.get("modifiers", 0) & 6):
        commands = ["selectAll"]
    await tab.send(cdp_input.dispatch_key_event(
        type_="keyDown" if text else "rawKeyDown",
        text=text, unmodified_text=text, commands=commands, **fields,
    ))
    await tab.send(cdp_input.dispatch_key_event(type_="keyUp", **fields))


# Reads a native input, a <label> by its control, or role=checkbox by aria-checked.
# `.checked` alone is undefined on the last two, which reads as "unchecked" forever.
_READ_CHECKED_JS = """
function() {
  if (typeof this.checked === 'boolean') return this.checked;
  if (this.control && typeof this.control.checked === 'boolean') return this.control.checked;
  const aria = this.getAttribute('aria-checked');
  return aria === null ? 'unknown' : aria === 'true';
}
"""


async def _label_of(el) -> Any:
    """The element's <label> as an element in its own right, or None."""
    remote = await el.tab.send(cdp_dom.resolve_node(backend_node_id=el.backend_node_id))
    found, exc = await el.tab.send(cdp_runtime.call_function_on(
        "function() { return (this.labels && this.labels[0]) || null; }",
        object_id=remote.object_id, return_by_value=False,
    ))
    if exc is not None or found.object_id is None:
        return None
    node = await el.tab.send(cdp_dom.describe_node(object_id=found.object_id, depth=-1))
    label = type(el)(node, el.tab, tree=node)
    entry = getattr(el, "ref_entry", None)
    if entry is not None:
        label._ref_entry = entry
    return label


async def _set_checked(ref, selector, state: bool, js_fallback: bool = False) -> str:
    verb = "check" if state else "uncheck"
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        current = await el.apply(_READ_CHECKED_JS)
        if current == "unknown":
            return err(f"{verb}: this element has no checked state "
                       f"(not a checkbox, a label for one, or role=checkbox with aria-checked)")
        if bool(current) == state:
            return ok(f"already {'checked' if state else 'unchecked'}")
        label = f"{verb} {ref or selector!r}"
        result = await _click_and_report(tab, el, label, False)
        how = "method=cdp_mouse"
        if result.startswith("Error:"):
            # Styled checkboxes hide the real input and show a label instead, so the
            # label is what a person actually clicks.
            target = await _label_of(el)
            if target is not None:
                result = await _click_and_report(tab, target, f"{label} via its label", False)
                how = "method=cdp_mouse on its label"
            if result.startswith("Error:"):
                if not js_fallback:
                    return err(f"{result[len('Error: '):]} Neither the input nor a label could "
                               f"be clicked; js_fallback=True toggles it from JS.")
                await _js_click(el)
                how = "method=js_click (isTrusted=false, the input is hidden or covered)"
        if bool(await el.apply(_READ_CHECKED_JS)) != state:
            return err(f"{verb}: clicked with {how} but the state did not change. {result}")
        return ok(f"{'checked' if state else 'unchecked'} — {how}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def check(ref: Optional[str] = None, selector: Optional[str] = None,
                js_fallback: bool = False) -> str:
    """Tick a checkbox/radio (idempotent), confirming the state changed.

    Handles native inputs, labels and role=checkbox. A hidden styled input is clicked
    through its label. js_fallback=True toggles it from JS if nothing can be clicked;
    the result names the method used.
    """
    return await _set_checked(ref, selector, True, js_fallback)


async def _outside_viewport(tab, x: float, y: float) -> str:
    """Why this point cannot receive input, or "". Chrome drops events outside the viewport."""
    raw = await _wait(tab.evaluate("innerWidth + 'x' + innerHeight", return_by_value=True),
                      timeout=5.0, what="viewport read")
    raw = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
    width, _, height = str(raw).partition("x")
    if width.isdigit() and height.isdigit() and not (0 <= x < int(width) and 0 <= y < int(height)):
        return f"({x},{y}) is outside the {width}x{height} viewport, where nothing receives it"
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 10. JAVASCRIPT
# ══════════════════════════════════════════════════════════════════════════


def _js_exception_text(details: Any) -> str:
    thrown = getattr(details, "exception", None)
    return str(getattr(thrown, "description", None) or getattr(thrown, "value", None)
               or getattr(details, "text", "exception"))


def _remote_value_text(remote: Any) -> str:
    """The result of Runtime.evaluate as text. JSON for anything JSON can say."""
    value = getattr(remote, "value", None)
    if value is not None:
        return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    if getattr(remote, "unserializable_value", None) is not None:  # NaN, Infinity, 5n
        return str(remote.unserializable_value)
    kind = str(getattr(getattr(remote, "type_", None), "value", getattr(remote, "type_", "")))
    if kind == "undefined":
        return "undefined"
    if getattr(remote, "subtype", None) == "null" or kind in ("", "object") and not remote.description:
        return "null"
    return str(remote.description)  # a function or symbol has a description, not a value


@mcp.tool()
async def evaluate(
    expression: str,
    await_promise: bool = True,
    timeout_ms: int = 30000,
    survive_navigation: bool = False,
    tab: Optional[int] = None,
) -> str:
    """Execute JavaScript with an explicit bounded timeout.

    ``survive_navigation`` explicitly retries once after a navigation interruption;
    callers must only use it for idempotent expressions because arbitrary JavaScript
    may have side effects before navigation starts.
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 600_000:
        return err("timeout_ms must be an integer between 1 and 600000")
    page = None
    try:
        page = select_tab(tab)
        with contextlib.suppress(Exception):
            await _watch_dialogs(page)
        attempts = 2 if survive_navigation else 1
        for attempt in range(attempts):
            try:
                # Not Tab.evaluate: it also asks for deep serialization, and then Chrome
                # leaves `value` empty for any object or array, which read as null.
                remote, thrown = await _wait(
                    page.send(cdp_runtime.evaluate(
                        expression=expression, user_gesture=True, await_promise=await_promise,
                        return_by_value=True, allow_unsafe_eval_blocked_by_csp=True,
                    )),
                    timeout=timeout_ms / 1000,
                    what="evaluate",
                )
                if thrown:
                    return err(f"JavaScript threw: {_js_exception_text(thrown)}")
                return ok(_remote_value_text(remote))
            except RuntimeError as exc:
                message = str(exc).lower()
                interrupted = "interrupted by target navigation" in message
                if not interrupted or attempt + 1 >= attempts:
                    raise
                await _wait(
                    page.evaluate("document.readyState === 'complete' || document.readyState === 'interactive'", return_by_value=True),
                    timeout=min(timeout_ms / 1000, 30),
                    what="navigation recovery",
                )
        return err("evaluate did not produce a result")
    except Exception as e:
        return err(str(e) + (_dialog_note(page) if page is not None else ""))


@mcp.tool()
async def wait_for_function(
    expression: str,
    timeout_ms: int = 120000,
    poll_ms: int = 500,
    tab: Optional[int] = None,
) -> str:
    """Poll a JavaScript predicate and return its truthy value.

    The expression is evaluated in the selected tab without changing active focus.
    It must be a predicate or a side-effect-free value expression.
    """
    if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 600_000):
        return err("timeout_ms must be an integer between 1 and 600000")
    if (isinstance(poll_ms, bool) or not isinstance(poll_ms, int)
            or not 10 <= poll_ms <= 60_000):
        return err("poll_ms must be an integer between 10 and 60000")
    try:
        page = select_tab(tab)
        deadline = time.monotonic() + timeout_ms / 1000
        wrapped = ("(async () => { const value = await (" + expression + "); "
                   "return JSON.stringify({truthy: Boolean(value), value: value ?? null}); })()")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return err(f"wait_for_function timed out after {timeout_ms}ms")
            try:
                raw = await _wait(
                    page.evaluate(wrapped, await_promise=True, return_by_value=True),
                    timeout=min(remaining, max(0.1, poll_ms / 1000)),
                    what="wait_for_function",
                )
            except TimeoutError:
                continue
            if isinstance(raw, cdp_runtime.ExceptionDetails):
                return err(f"wait_for_function: the predicate threw: {_js_exception_text(raw)}")
            raw = raw.value if hasattr(raw, "value") and not isinstance(raw, (str, dict)) else raw
            state = parse_json(raw)
            if not isinstance(state, dict):
                return err("wait_for_function predicate returned an unserializable value")
            if state.get("truthy"):
                value = state.get("value")
                return ok(json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else str(value))
            await asyncio.sleep(min(poll_ms / 1000, max(0, deadline - time.monotonic())))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def inject_init_script(script: str) -> str:
    """Register a script that runs before page scripts on every navigation of
    the CURRENT tab. Scope is the active tab/target only — it is NOT auto-applied
    to other open tabs or to tabs opened later; re-run per tab if needed."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import page as cdp_page
        # Without the Page domain the script registers and then never runs.
        await tab.send(cdp_page.enable())
        await tab.send(cdp_page.add_script_to_evaluate_on_new_document(source=script))
        return ok("init script registered")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 13. BATCH
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def batch_actions(actions: list[dict]) -> str:
    """Execute a flat list of actions in order and report each one.

    Each action: {type: click|fill|type|wait|press|navigate, ...params}, or the name
    of any workflow_run tool with its arguments alongside, such as
    {"type": "select_option", "selector": "#c", "value": "x"}.
    Add "stop_on_error": true to an action to stop there if it fails.
    Example: [{"type":"click","ref":"e3"},{"type":"fill","ref":"e4","value":"x"}]

    For branching, loops or using one step's result in another, use workflow_run.
    """
    if not isinstance(actions, list):
        actions = parse_json(actions, None)
    # A default of [] turned unparseable input into "0 actions OK": nothing ran, reported
    # as a batch that worked.
    if not isinstance(actions, list):
        return err("batch_actions: actions must be a JSON list of action objects; nothing ran")
    if not actions:
        return err("batch_actions: no actions given; nothing ran")
    _register_workflow_tools()
    results = []
    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            results.append(f"[{i}] ?: ERR not an action object but {type(act).__name__}: "
                           f"{str(act)[:80]}")
            continue
        atype = act.get("type")
        try:
            tool, args = _batch_action_call(act)
            if tool is None:
                r = err(f"unknown action type: {atype}")
            else:
                r = await _WORKFLOW_TOOLS[tool](**args)
            results.append(f"[{i}] {atype}: {str(r)[:160]}")
            # A tool can return an "Error: ..." string WITHOUT raising — honor
            # stop_on_error for those too, not just for exceptions.
            if isinstance(r, str) and r.startswith("Error:") and act.get("stop_on_error"):
                break
        except Exception as e:
            results.append(f"[{i}] {atype}: ERR {e}")
            if act.get("stop_on_error"):
                break
    n_failed = sum(1 for ln in results if ": Error:" in ln or ": ERR " in ln)
    header = f"{len(results)} actions, {n_failed} failed" if n_failed else f"{len(results)} actions OK"
    return ok(header + "\n" + "\n".join(results))


def _batch_action_call(act: dict) -> tuple[Optional[str], dict]:
    """Map a batch action to (tool, args). The six legacy names keep their defaults."""
    atype = act.get("type")
    if atype == "click":
        return "click", {"ref": act.get("ref"), "selector": act.get("selector"),
                         "humanize": act.get("humanize", False)}
    if atype == "fill":
        return "fill", {"ref": act.get("ref"), "selector": act.get("selector"),
                        "value": act.get("value", "")}
    if atype == "type":
        return "type_text", {"text": act.get("text", ""),
                             "humanize": act.get("humanize", False)}
    if atype == "press":
        return "press_key", {"key": act.get("key", "Enter")}
    if atype == "wait":
        return "wait_for", {"selector": act.get("selector"), "text": act.get("text"),
                            "timeout": act.get("timeout", 5.0)}
    if atype == "navigate":
        return "navigate", {"url": act.get("url")}
    if atype in _WORKFLOW_TOOLS:
        return atype, {k: v for k, v in act.items() if k not in ("type", "stop_on_error")}
    return None, {}


@mcp.tool()
async def fill_form(fields: list[dict], submit_ref: Optional[str] = None) -> str:
    """Fill multiple fields then optionally submit.

    fields: [{ref: "e1", value: "..."}, {selector: "#email", value: "..."}]
    """
    if not isinstance(fields, list):
        fields = parse_json(fields, [])
    if not fields:
        return err("fill_form: no fields given")
    results = []
    failed = 0
    for f in fields:
        if "value" not in f:
            # Filling with a default would silently empty a field the caller never meant to.
            failed += 1
            results.append(f"{f.get('ref') or f.get('selector')}: Error: no value given")
            continue
        r = str(await fill(ref=f.get("ref"), selector=f.get("selector"), value=f["value"]))
        failed += r.startswith("Error:")
        results.append(f"{f.get('ref') or f.get('selector')}: {r}")
    if failed:
        if submit_ref:
            results.append("submit: not clicked, because a field failed")
        return err(f"{failed} of {len(fields)} field(s) failed\n" + "\n".join(results))
    if submit_ref:
        r = str(await click(ref=submit_ref))
        results.append(f"submit: {r}")
        if r.startswith("Error:"):
            return err("the fields were filled but the submit failed\n" + "\n".join(results))
    return ok("\n".join(results))


@mcp.tool()
async def navigate_and_snapshot(
    url: str,
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Navigate then immediately snapshot — common pattern.

    The navigation's observed postconditions are kept above the snapshot: a
    snapshot of a page you were redirected to reads exactly like a snapshot of
    the page you asked for. A failed expect_url / expect_status aborts before
    the snapshot rather than describing the wrong page. See navigate().
    """
    nav = await navigate(url, expect_url=expect_url, expect_status=expect_status)
    if str(nav).startswith("Error:"):
        return nav
    snapshot = await _page.browser_snapshot()
    if str(snapshot).startswith("Error:"):
        # A failed snapshot does not make the navigation unobserved — keep its
        # postconditions rather than dropping them with the failure.
        return err(f"navigated, then the snapshot failed.\n{nav}\n{snapshot}")
    return ok(f"{nav}\n\n{snapshot}")


# ══════════════════════════════════════════════════════════════════════════
# 14. VIEWPORT + SCROLL
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def get_viewport_size() -> str:
    """Return the viewport size (innerWidth, innerHeight) and scroll position."""
    try:
        tab = BrowserState.active_tab()
        result = await tab.evaluate(
            "JSON.stringify({width: innerWidth, height: innerHeight, "
            "scrollX: scrollX, scrollY: scrollY})",
            return_by_value=True,
        )
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def set_viewport_size(width: int, height: int) -> str:
    """Resize the browser window, and report the viewport that results.

    The size given is the whole window, so with a visible browser the viewport comes out
    smaller by the height of its toolbars. emulate_device sets an exact viewport.
    """
    if width <= 0 or height <= 0:
        return err(f"set_viewport_size: width and height must be positive, got {width}x{height}")
    try:
        tab = BrowserState.active_tab()
        # set_window_size defaults to the top left corner, which moves the window.
        left, top = 0, 0
        with contextlib.suppress(Exception):
            _window, bounds = await tab.get_window()
            left, top = bounds.left or 0, bounds.top or 0
        await tab.set_window_size(left=left, top=top, width=width, height=height)
        await asyncio.sleep(0.15)
        inner = await tab.evaluate("innerWidth + 'x' + innerHeight", return_by_value=True)
        inner = inner.value if hasattr(inner, "value") and not isinstance(inner, str) else inner
        return ok(f"window set to {width}x{height}; viewport reads {inner}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def scroll(
    direction: str = "down",
    amount: int = 500,
    humanize: bool = True,
) -> str:
    """Scroll page via REAL mouseWheel CDP events (not JS scrollBy).

    humanize=True (default): variable chunks 50-150px + micro-pauses + 20%
    reading-pause chance — bypasses DataDome/PerimeterX behavioral detection.
    humanize=False: instant scroll (faster, less stealthy).

    Directions: up | down | top | bottom

    Reports how far the window really moved. "did not move" means it is already at that
    end, cannot scroll, or the wheel went to a scrollable element under the pointer.
    """
    if direction not in ("up", "down", "top", "bottom"):
        return err(f"scroll: direction must be up, down, top or bottom, not {direction!r}")
    try:
        tab = BrowserState.active_tab()
        before = await _eval_checked(tab, "window.scrollY")
        if direction == "top":
            await _eval_checked(tab, "window.scrollTo(0,0)")
            how = "to top"
        elif direction == "bottom":
            await _eval_checked(tab, "window.scrollTo(0, document.documentElement.scrollHeight)")
            how = "to bottom"
        else:
            dy = amount if direction == "down" else -amount
            if humanize:
                sent = await humanized_scroll(tab, dy)
                how = f"{direction} {sent}px (humanized wheel events)"
            else:
                # Instant mode — single wheel dispatch (still real event)
                from nodriver.cdp import input_ as cdp_input
                await tab.send(cdp_input.dispatch_mouse_event(
                    type_="mouseWheel", x=500, y=400, delta_x=0, delta_y=dy,
                ))
                how = f"{direction} {amount}px (instant wheel)"
        # The wheel is applied after the dispatch returns.
        after = before
        for _ in range(8):
            after = await _eval_checked(tab, "window.scrollY")
            if after != before:
                await asyncio.sleep(0.05)
                after = await _eval_checked(tab, "window.scrollY")
                break
            await asyncio.sleep(0.05)
        if after == before:
            return ok(f"scroll {how}: the window did not move (scrollY stays {before})")
        return ok(f"scrolled {how}; scrollY {before}→{after}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def scroll_to(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    block: str = "center",
    smooth: bool = True,
) -> str:
    """Smooth-scroll a specific element into viewport.

    Args:
        ref: snapshot ref (e.g. "e7") from browser_snapshot
        selector: CSS selector alternative
        block: "start" | "center" | "end" | "nearest" — vertical alignment
        smooth: CSS smooth scroll (default) vs instant jump

    Works even if element is far off-screen (pages of scroll away).
    """
    if not ref and not selector:
        return err("ref or selector required")
    if block not in ("start", "center", "end", "nearest"):
        return err(f"scroll_to: block must be start, center, end or nearest, not {block!r}")
    try:
        tab = BrowserState.active_tab()
        behavior = "smooth" if smooth else "auto"
        el = await resolve_ref(ref) if ref else await tab.query_selector(selector)
        if el is None:
            return err(f"element not found: {selector}")
        if await el.apply("function() { return this.getClientRects().length === 0; }") is True:
            return err(f"{ref or selector} is not rendered, so it cannot be scrolled into view")
        await el.apply(
            f"function() {{ this.scrollIntoView("
            f"{{block:{json.dumps(block)}, behavior:{json.dumps(behavior)}}}); }}"
        )
        # A smooth scroll is still moving when this returns; the next tool
        # would otherwise measure a position that is already out of date. On a busy machine a
        # smooth scroll stalls between frames for longer than two readings, and macOS CI saw
        # it reported done 58 px short; four still readings (160 ms) ride that out.
        quiet = 4 if smooth else 2
        await _settled_position(el, quiet)
        in_view = ("function() { const r = this.getBoundingClientRect(); return r.bottom > 0 "
                   "&& r.right > 0 && r.top < innerHeight && r.left < innerWidth; }")
        visible = await el.apply(in_view)
        if visible is not True and smooth:
            # On a busy machine a smooth scroll can sit still for longer than the two quiet
            # frames _settled_position waits for before its first step, which reads as settled
            # where the element was. macOS CI refused that way in 2 of 2 runs. Wait for it to
            # arrive, bounded, and let it settle there.
            deadline = time.monotonic() + 2.0
            while visible is not True and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                visible = await el.apply(in_view)
            if visible is True:
                await _settled_position(el, quiet)
        if visible is not True:
            return err(f"{ref or selector} is still outside the viewport after scrolling")
        return ok(f"scrolled {ref or selector} into view (block={block})")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 15. DIALOG + ACCESSIBILITY
# ══════════════════════════════════════════════════════════════════════════

# Read-at-fire-time config so re-arming with a new action/text updates the
# response without stacking another CDP handler. Keyed by CDP target id.
_dialog_pre_action: dict[str, Any] = {
    "action": None, "text": None, "_registered_tab_ids": set(),
}


def _instance_dialog_cfg(kind: str) -> dict:
    """Dialog settings of the current instance.

    A handler captures this when armed and is armed once per tab. Keyed by instance, not
    by session: otherwise an instance handed to another session keeps obeying its previous
    owner, while the new owner is told its setting was applied. Instance names are unique
    across sessions, so no session can reach another's settings.
    """
    return _DIALOG_INSTANCE_CFG.setdefault((BrowserState.current_instance_id, kind), {
        "action": "accept", "text": "", "enabled": False, "types": None,
    })


_DIALOG_INSTANCE_CFG: dict[tuple[str, str], dict] = {}


@mcp.tool()
async def dialog_handle(action: str = "accept", text: Optional[str] = None) -> str:
    """Answer the next alert/confirm/prompt. Call BEFORE the action that triggers it.

    A click that opened a dialog with nothing armed reports it; call this then too, and
    the dialog that is already open gets the answer.
    """
    if action not in ("accept", "dismiss"):
        return err(f"dialog_handle: action must be 'accept' or 'dismiss', not {action!r}")
    pre_cfg = _instance_dialog_cfg("pre")
    try:
        tab = BrowserState.active_tab()
        answered = await _answer_open_dialog(tab, action, text)
        if answered:
            return answered
        pre_cfg["action"] = action
        pre_cfg["text"] = text
        pre_cfg["pending"] = True
        from nodriver.cdp import page as cdp_page
        tid = _target_key(tab)
        if tid in _dialog_pre_action["_registered_tab_ids"]:
            # Handler already armed on this tab — config updated above; don't
            # stack a second handler (which would accumulate every call).
            return ok(f"dialog handler re-armed ({action})")

        async def handle(_event):
            # Armed for the NEXT dialog only. Answering every later one would accept a
            # destructive confirm nobody decided on.
            if not pre_cfg.get("pending"):
                return
            pre_cfg["pending"] = False
            try:
                await tab.send(cdp_page.handle_java_script_dialog(
                    accept=(pre_cfg["action"] == "accept"),
                    prompt_text=pre_cfg["text"] or "",
                ))
            except Exception:
                pass

        # nodriver up to 0.48 enabled the domain for a registered handler; 0.50 does not,
        # and without this the dialog event never arrives. Bounded, because the renderer
        # cannot answer while a dialog this server never saw is open.
        await _wait(tab.send(cdp_page.enable()), timeout=5.0, what="dialog_handle")
        tab.add_handler(cdp_page.JavascriptDialogOpening, handle)
        _dialog_pre_action["_registered_tab_ids"].add(tid)
        return ok(f"dialog handler armed ({action})")
    except Exception as e:
        return err(str(e))


# ── dialog_auto_handle (persistent, type-filterable) ──────────────────────
# Read-at-fire-time config so the LLM can update action/types without
# re-registering the handler. Keyed by CDP target id to avoid duplicate handlers
# on the same tab.
_DIALOG_AUTO_CFG: dict = {
    "enabled": False, "action": "accept", "text": "",
    "types": None,  # None=all; or set like {"beforeunload"}
    "_registered_tab_ids": set(),
}

# Track which tabs already have a console / network capture handler armed, so
# repeated console_start/network_start calls reset the buffers but do NOT stack
# duplicate CDP handlers (which would double-record every event). Cleared on
# browser_close / browser_recover alongside the other per-tab caches.
_CONSOLE_ARMED_TAB_IDS: set = set()
# When capture was armed — surfaced by console_get so an empty result is
# interpretable ("listening since X, 0 messages" vs "never listening").
_CONSOLE_STARTED_AT: Optional[str] = None


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
_NETWORK_ARMED_TAB_IDS: set = set()
_NETWORK_CAPTURE_BODIES: dict[str, bool] = {}


def _clear_tab_scoped_state(tabs: list[Any]) -> None:
    """Drop lifecycle state for closed targets without touching live instances."""
    keys = {_target_key(tab) for tab in tabs}
    if not keys:
        return
    forget_refs(keys)
    for key in keys:
        _page._SNAPSHOT_CACHE.pop(key, None)
        _HUMAN_HELP.pop(key, None)
        _dialog_pre_action["_registered_tab_ids"].discard(key)
        _DIALOG_AUTO_CFG["_registered_tab_ids"].discard(key)
        _DIALOG_WATCHED.discard(key)
        _OPEN_DIALOGS.pop(key, None)
        _CONSOLE_ARMED_TAB_IDS.discard(key)
        _NETWORK_ARMED_TAB_IDS.discard(key)
        _NETWORK_CAPTURE_BODIES.pop(key, None)
    # Interception pauses every matching request until the handler answers it, so
    # it must not survive its target. Left armed, the next launch would inherit a
    # handler bound to a dead tab and the first request would stall with no error.
    # Fetch.disable is pointless here — the target is already gone — so only the
    # local state is released; intercept_stop() handles the live case.
    intercepted = _capture._INTERCEPT.get("tab")
    if _capture._INTERCEPT.get("active") and (
            intercepted is None or _target_key(intercepted) in keys):
        watchdog = _capture._INTERCEPT.get("watchdog")
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
        _capture._INTERCEPT.update({
            "active": False, "tab": None, "handler": None, "handler_tab": None,
            "watchdog": None, "stop_reason": "target closed", "disable_error": None,
        })
    try:
        from .tools import devtools as _dt
        if _dt._TRACE_ACTIVE.get("tab_id") in keys:
            _dt._TRACE_ACTIVE.update({
                "tab_id": None, "started_at": 0.0,
                "categories": "", "handler": None,
            })
            _dt._TRACE_BUFFER.clear()
        if _dt._COVERAGE_ACTIVE.get("tab_id") in keys:
            _dt._COVERAGE_ACTIVE.update({
                "tab_id": None, "js": False, "css": False,
            })
    except Exception:
        # The module can be unavailable during partial import/startup failure.
        pass


@mcp.tool()
async def dialog_auto_handle(
    action: str = "accept",
    enabled: bool = True,
    types: Optional[list[str]] = None,
    text: str = "",
) -> str:
    """Install a PERSISTENT auto-handler for native browser dialogs.
    Unlike dialog_handle (one-shot, action baked in at arm time), this
    one stays armed across many dialogs and reads its config at fire
    time — call again with new action/types to update without re-arming.

    Args:
        action: "accept" (Leave / OK) or "dismiss" (Cancel / Stay)
        enabled: True to arm, False to disable (config preserved)
        types: optional list to scope handling — any of:
               ["alert", "confirm", "prompt", "beforeunload"]
               None (default) = handle all types.
        text: prompt response when action="accept" on prompt() dialogs;
              also basic-auth "user:pass" for HTTP 401.

    Common patterns:
        # Form pages with "unsaved changes" guard — auto-leave forever
        dialog_auto_handle(action="accept", types=["beforeunload"])

        # Pages that spam alert() — auto-OK
        dialog_auto_handle(action="accept", types=["alert"])

        # Disable when done
        dialog_auto_handle(enabled=False)

    Native dialogs only (Chrome's own card UI). HTML/CSS modal overlays
    are regular DOM — use click_text("Cancel") / click_role for those.
    """
    if action not in ("accept", "dismiss"):
        return err(f"dialog_auto_handle: action must be 'accept' or 'dismiss', not {action!r}")
    unknown = set(types or ()) - {"alert", "confirm", "prompt", "beforeunload"}
    if unknown:
        return err(f"dialog_auto_handle: unknown dialog type(s) {sorted(unknown)}; "
                   f"use alert, confirm, prompt or beforeunload")
    auto_cfg = _instance_dialog_cfg("auto")
    auto_cfg["enabled"] = bool(enabled)
    auto_cfg["action"] = action
    auto_cfg["text"] = text or ""
    auto_cfg["types"] = set(types) if types else None

    if not enabled:
        return ok("dialog auto-handle disabled (config preserved — re-enable to resume)")

    try:
        tab = BrowserState.active_tab()
        tab_id = _target_key(tab)
        await _answer_open_dialog(tab, action, text)
        if tab_id in _DIALOG_AUTO_CFG["_registered_tab_ids"]:
            return ok(
                f"auto-handle config updated (action={action}, "
                f"types={list(types) if types else 'all'}); already armed on this tab"
            )

        from nodriver.cdp import page as cdp_page

        async def auto_handler(event):
            cfg = auto_cfg
            if not cfg["enabled"]:
                return
            ev_type = getattr(event, "type_", None) or getattr(event, "type", None)
            type_str = getattr(ev_type, "value", str(ev_type)) if ev_type else ""
            if cfg["types"] and type_str not in cfg["types"]:
                return
            try:
                await tab.send(cdp_page.handle_java_script_dialog(
                    accept=(cfg["action"] == "accept"),
                    prompt_text=cfg["text"] or "",
                ))
            except Exception:
                pass

        await _wait(tab.send(cdp_page.enable()), timeout=5.0, what="dialog_auto_handle")
        tab.add_handler(cdp_page.JavascriptDialogOpening, auto_handler)
        _DIALOG_AUTO_CFG["_registered_tab_ids"].add(tab_id)
        return ok(
            f"dialog auto-handle armed (action={action}, "
            f"types={list(types) if types else 'all'})"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def accessibility_snapshot(interesting_only: bool = True) -> str:
    """Return ARIA accessibility tree of current page."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import accessibility as cdp_a11y
        result = await _wait(tab.send(cdp_a11y.get_full_ax_tree()), what="accessibility_snapshot")
        # Filter to meaningful nodes
        nodes = result if isinstance(result, list) else []
        filtered = []
        for n in nodes[:500]:
            node_dict = {
                "role": getattr(getattr(n, "role", None), "value", None),
                "name": getattr(getattr(n, "name", None), "value", None),
                "value": getattr(getattr(n, "value", None), "value", None),
            }
            if interesting_only:
                if not node_dict["name"] and not node_dict["value"]:
                    continue
            filtered.append(node_dict)
        return ok(json.dumps(filtered, indent=2, default=str))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 16. CONSOLE + NETWORK
# ══════════════════════════════════════════════════════════════════════════


def _console_target_key(tab) -> str:
    """Deprecated alias for _target_key — kept so no call site silently diverges."""
    return _target_key(tab)


@mcp.tool()
async def console_start() -> str:
    """Begin capturing console messages + uncaught exceptions of the active tab.

    Arms the listener on THIS tab only. Call it again after switching or opening
    a tab. The Runtime domain is enabled explicitly and re-enabled on every
    navigation, so capture survives page loads instead of silently lapsing.
    """
    global _CONSOLE_STARTED_AT
    try:
        tab = BrowserState.active_tab()
        BrowserState.console_logs.clear()
        BrowserState.page_errors.clear()
        BrowserState.capture_console = True
        _CONSOLE_STARTED_AT = _now_iso()
        tid = _target_key(tab)
        from nodriver.cdp import page as cdp_page

        if tid in _CONSOLE_ARMED_TAB_IDS:
            # Handler is still bound, but the domain can lapse — re-enabling is
            # idempotent and cheap, and it is what makes a re-call actually help.
            try:
                await tab.send(cdp_runtime.enable())
            except Exception as exc:
                BrowserState.capture_console = False
                return err(f"console capture NOT re-armed, Runtime.enable failed: {_exc_text(exc)}")
            return ok("console capture re-armed (handler already bound to this tab)")

        # Bind the sink NOW, to the instance being armed. Reading
        # BrowserState.console_logs at event time would append to whichever
        # instance happens to be current, so a switch mid-capture would file this
        # browser's messages under another one — and stop filling this one.
        sink = BrowserState.console_logs
        error_sink = BrowserState.page_errors

        async def handle(event):
            try:
                # console.clear() is an instruction, not a message. Kept, console_clear
                # would leave its own "clear" behind in the buffer it just emptied.
                if str(getattr(event, "type_", "")) == "clear":
                    return
                args = []
                for a in (event.args or []):
                    val = getattr(a, "value", None)
                    # `or` would drop console.log(0)/false/'' — keep falsy-but-real
                    # values, fall back to description only when value is absent.
                    if val is not None:
                        args.append(val)
                    else:
                        args.append(getattr(a, "description", "") or "")
                sink.append({
                    "type": event.type_,
                    "text": " ".join(str(a) for a in args)[:500],
                })
                _trim_capture(sink)
            except Exception:
                pass

        async def handle_exception(event):
            # A page can fail hard with zero console.log calls — without this,
            # console_get() would report "0 messages" on a page that threw.
            try:
                d = getattr(event, "exception_details", None)
                text = getattr(d, "text", "") or ""
                exc = getattr(d, "exception", None)
                desc = getattr(exc, "description", None) if exc else None
                message = (desc or text or "uncaught exception")[:500]
                sink.append({"type": "exception", "text": message})
                _trim_capture(sink)
                error_sink.append({"text": message, "url": getattr(d, "url", None),
                                   "line": getattr(d, "line_number", None)})
                _trim_capture(error_sink)
            except Exception:
                pass

        async def on_navigated(event):
            # Execution contexts are torn down on navigation; re-enable so the
            # listener keeps receiving events on the new document.
            try:
                await tab.send(cdp_runtime.enable())
            except Exception:
                pass

        tab.add_handler(cdp_runtime.ConsoleAPICalled, handle)
        tab.add_handler(cdp_runtime.ExceptionThrown, handle_exception)
        with contextlib.suppress(Exception):
            await tab.send(cdp_page.enable())
        tab.add_handler(cdp_page.FrameNavigated, on_navigated)
        # After the handlers, so the messages Chrome replays on enable are not missed.
        try:
            await tab.send(cdp_runtime.enable())
        except Exception as exc:
            # No events will come. Claiming attached here makes an empty list read as a
            # quiet page.
            _drop_handlers(tab, [(cdp_runtime.ConsoleAPICalled, handle),
                                 (cdp_runtime.ExceptionThrown, handle_exception),
                                 (cdp_page.FrameNavigated, on_navigated)])
            BrowserState.capture_console = False
            return err(f"console capture NOT started, Runtime.enable failed: {_exc_text(exc)}")
        _CONSOLE_ARMED_TAB_IDS.add(tid)
        return ok("console capture started (console + uncaught exceptions)")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def console_get(limit: int = 100) -> str:
    """Retrieve captured console messages WITH the capture status.

    Returns {attached, capturing, since, total, returned, messages} rather than a
    bare array, because an empty array alone is ambiguous: it cannot tell you
    whether the page logged nothing or nothing was ever listening. `attached`
    false + empty means NOT MONITORED — not "no errors".

    Messages are chronological (oldest first within the last `limit`).
    """
    if limit < 0:
        return err("limit must be >= 0")
    tab = None
    try:
        tab = BrowserState.active_tab()
    except Exception:
        pass
    armed = tab is not None and _target_key(tab) in _CONSOLE_ARMED_TAB_IDS
    logs = BrowserState.console_logs[-limit:] if limit else []
    payload: dict[str, Any] = {
        "attached": armed,
        "capturing": bool(BrowserState.capture_console),
        "since": _CONSOLE_STARTED_AT,
        "total": len(BrowserState.console_logs),
        "returned": len(logs),
        "messages": logs,
    }
    if not (armed and BrowserState.capture_console):
        payload["note"] = (
            "NOT listening on the active tab — an empty 'messages' here means "
            "'nothing was monitored', not 'no errors'. Call console_start() "
            "(re-call it after switching or opening a tab)."
        )
    return ok(json.dumps(payload, indent=2, default=str))


@mcp.tool()
async def network_start(capture_bodies: bool = True) -> str:
    """Begin capturing network requests + responses with full headers.

    Args:
        capture_bodies: if True (default), also indexes by request_id so
            network_get(include_body=True) can fetch response bodies on
            demand via CDP Network.getResponseBody.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        tid = _target_key(tab)
        # Network.enable so events arrive at all, and so getResponseBody works later. If it
        # fails nothing is captured, and an empty log must not read as a quiet page.
        try:
            await tab.send(cdp_network.enable())
        except Exception as exc:
            BrowserState.capture_network = False
            return err(f"network capture NOT started, Network.enable failed: {_exc_text(exc)}")
        BrowserState.network_logs.clear()
        BrowserState.network_index.clear()
        BrowserState.capture_network = True
        # The handler is installed once per target, but this option is mutable:
        # a later network_start(capture_bodies=True) must change what that
        # existing handler indexes instead of merely claiming that it did.
        _NETWORK_CAPTURE_BODIES[tid] = bool(capture_bodies)

        # Bound now, to the instance being armed — see console_start for why
        # reading the class attribute at event time cross-files the capture.
        log_sink = BrowserState.network_logs
        index_sink = BrowserState.network_index

        async def on_req(event):
            try:
                rid = str(event.request_id)
                req = event.request
                req_headers = dict(getattr(req, "headers", None) or {})
                entry = {
                    "request_id": rid,
                    "url": getattr(req, "url", ""),
                    "method": getattr(req, "method", ""),
                    "type": str(getattr(event, "type_", "") or getattr(event, "type", "")),
                    "request_headers": req_headers,
                    "request_body": getattr(req, "post_data", None),
                    "status": None,
                    "response_headers": None,
                    "mime": None,
                    "size": None,
                    "timestamp": getattr(event, "timestamp", None),
                    "wall_time": getattr(event, "wall_time", None),
                }
                if _NETWORK_CAPTURE_BODIES.get(tid, False):
                    index_sink[rid] = entry
                    _trim_index(index_sink)
                log_sink.append({
                    "type": "request", "request_id": rid,
                    "url": entry["url"], "method": entry["method"],
                })
                _trim_capture(log_sink)
            except Exception:
                pass

        async def on_res(event):
            try:
                rid = str(event.request_id)
                resp = event.response
                resp_headers = dict(getattr(resp, "headers", None) or {})
                if rid in index_sink:
                    e = index_sink[rid]
                    e["status"] = getattr(resp, "status", None)
                    e["response_headers"] = resp_headers
                    e["mime"] = getattr(resp, "mime_type", None)
                    e["size"] = getattr(resp, "encoded_data_length", None)
                log_sink.append({
                    "type": "response", "request_id": rid,
                    "url": getattr(resp, "url", ""),
                    "status": getattr(resp, "status", None),
                    "mime": getattr(resp, "mime_type", None),
                })
                _trim_capture(log_sink)
            except Exception:
                pass

        if tid in _NETWORK_ARMED_TAB_IDS:
            return ok(
                f"network capture started (capture_bodies={capture_bodies}); "
                "handler already armed on this tab"
            )
        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        tab.add_handler(cdp_network.ResponseReceived, on_res)
        _NETWORK_ARMED_TAB_IDS.add(tid)
        return ok(f"network capture started (capture_bodies={capture_bodies})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def network_get(
    limit: int = 100,
    filter_url: Optional[str] = None,
    include_body: bool = False,
    max_body_bytes: int = 10000,
    full: bool = False,
    reveal_values: bool = False,
) -> str:
    """Retrieve captured network events.

    Args:
        limit: max entries returned (chronological: oldest first within the last `limit`, newest last)
        filter_url: substring filter on URL
        include_body: fetch response bodies via CDP Network.getResponseBody
            for each matching entry. Bodies are truncated to max_body_bytes.
            Requires network_start(capture_bodies=True) (default).
        max_body_bytes: cap per-body length (default 10000)
        full: if True, return entries with full headers + body fields
            from network_index (use this once you've called network_start).
            Default False = legacy flat event stream (backward compat).
        reveal_values: True returns credential headers, fields and query parameters as
            they are. By default their values read "*** (N chars)", because tool output
            lands in the transcript. Bodies other than JSON or forms cannot be read field
            by field and pass through.
    """
    if limit < 0:
        return err("limit must be >= 0")
    if max_body_bytes < 0:
        return err("max_body_bytes must be >= 0")
    if not (BrowserState.capture_network or BrowserState.network_logs or BrowserState.network_index):
        return err("NOT listening: call network_start() before the traffic. An empty list "
                   "would read as a quiet page when nothing was monitored.")
    if full or include_body:
        # Work on copies: response bodies are fetched on demand, and caching a
        # truncated/error sentinel in network_index made a later call with a
        # larger limit permanently unable to retrieve the real body.
        entries = [dict(entry) for entry in BrowserState.network_index.values()]
        if filter_url:
            entries = [e for e in entries if filter_url in e.get("url", "")]
        entries = entries[-limit:] if limit else []
        if include_body and entries:
            try:
                tab = BrowserState.active_tab()
            except Exception as e:
                return err(f"response body capture unavailable: {e}")
            from nodriver.cdp import network as cdp_network
            for e in entries:
                rid = e.get("request_id")
                if not rid:
                    e["response_body"] = "<missing request_id>"
                    continue
                try:
                    result = await asyncio.wait_for(
                        tab.send(cdp_network.get_response_body(
                            request_id=cdp_network.RequestId(rid)
                        )),
                        timeout=5.0,
                    )
                    body = getattr(result, "body", None)
                    if body is None and isinstance(result, tuple):
                        body = result[0]
                    body_str = str(body) if body is not None else ""
                    if not reveal_values:
                        body_str = mask_body(body_str)
                    orig_len = len(body_str)
                    if orig_len > max_body_bytes:
                        body_str = body_str[:max_body_bytes] + (
                            f"... [truncated, original {orig_len} chars]"
                        )
                    e["response_body"] = body_str
                except asyncio.TimeoutError:
                    e["response_body"] = "<getResponseBody timeout>"
                except Exception as ex:
                    e["response_body"] = f"<getResponseBody error: {ex}>"
        if not reveal_values:
            entries = [mask_request_entry(e) for e in entries]
        return ok(json.dumps(entries, indent=2, default=str)[:50000])
    # Legacy event-stream view
    logs = BrowserState.network_logs
    if filter_url:
        logs = [l for l in logs if filter_url in l.get("url", "")]
    selected = logs[-limit:] if limit else []
    if not reveal_values:
        selected = [mask_request_entry(l) for l in selected]
    return ok(json.dumps(selected, indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════
# 17. DEBUG / META
# ══════════════════════════════════════════════════════════════════════════


def _status_profile() -> str:
    """The profile the current browser really uses, not the default it would get."""
    if BrowserState.current_profile_dir:
        return str(BrowserState.current_profile_dir)
    browser = BrowserState.browser
    if browser is None:
        return str(PROFILE_DIR)
    if getattr(browser, "_process", None) is None and not getattr(browser, "_process_pid", None):
        return "(attached Chrome: profile not known)"
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    return f"(ephemeral) {data_dir}" if data_dir else "(ephemeral)"


@mcp.tool()
async def server_status() -> str:
    """Diagnostic info about the server, browser, and CDP compatibility patches.

    `compat_patches` is the one worth reading when captures come back empty: a
    nodriver compatibility patch that failed to apply does not crash anything —
    it makes the CDP event listener die on the first mismatched payload, so
    console/network capture returns nothing and looks like a quiet page.

    `dock_icon` says whether visible browsers get their own Dock icon (macOS): off,
    beta-ready (they run from Chrome Beta with the project icon), beta-missing,
    icon-missing or unavailable, with the reason. Reading it changes nothing.

    `profile_gc` is the startup pass over leftover privacy-profiles/* and profile-pid<N>
    folders: what it removed, what it kept because it holds data, what was in use, and
    errors. A fallback with cookies, logins, storage, extensions or bookmarks is never
    removed by it; `fallback_profiles_left` counts those for profile_report and
    profile_cleanup.
    """
    if BrowserState.browser is not None:
        # A tab the page opened is unknown until asked, which reported one tab too few.
        with contextlib.suppress(Exception):
            await _refresh_tabs()
    status = {
        "version": __version__,
        "session": BrowserState.session_key(),
        "browser_running": BrowserState.is_up(),
        "instance": BrowserState.current_instance_id,
        "tabs": len(BrowserState.tabs),
        "active_tab": BrowserState.active_tab_index,
        "console_capture": BrowserState.capture_console,
        "network_capture": BrowserState.capture_network,
        "console_logs": len(BrowserState.console_logs),
        "network_logs": len(BrowserState.network_logs),
        "network_indexed": len(BrowserState.network_index),
        "page_errors": len(BrowserState.page_errors),
        "intercepting": bool(_capture._INTERCEPT.get("active")),
        "profile_dir": _status_profile(),
        "compat_patches": _patches.status(),
        "os_cursor": await _os_cursor_status(),
        "dock_icon": await _dock_icon_status(),
        "profile_gc": profile_gc.status(),
    }
    return ok(json.dumps(status, indent=2, default=str))


# Importing Quartz and AppKit for the first time takes seconds while the server is starting
# (profile_gc and the Dock check run then too), and a 3 s wait on it made the first
# server_status say "not measured (TimeoutError)". So it is measured on a thread from the start,
# and a status call that comes before the answer says it is still being measured.
_OS_CURSOR_PROBE: dict = {"thread": None, "started": None, "result": None, "measured_at": None}
_OS_CURSOR_PROBE_LOCK = threading.Lock()
_OS_CURSOR_REMEASURE = 60.0


def _start_os_cursor_probe() -> None:
    """Measure the OS cursor backend on its own thread, unless a measurement is running."""
    with _OS_CURSOR_PROBE_LOCK:
        thread = _OS_CURSOR_PROBE["thread"]
        if thread is not None and thread.is_alive():
            return

        def work() -> None:
            from . import os_input
            try:
                result = os_input.is_available()
            except Exception as exc:
                result = (False, f"could not be measured ({type(exc).__name__}: {exc})")
            _OS_CURSOR_PROBE["result"] = result
            _OS_CURSOR_PROBE["measured_at"] = time.monotonic()

        _OS_CURSOR_PROBE["started"] = time.monotonic()
        thread = threading.Thread(target=work, name="os-cursor-probe", daemon=True)
        _OS_CURSOR_PROBE["thread"] = thread
        thread.start()


async def _os_cursor_status() -> dict:
    """Whether solve_turnstile_oscursor could run here, checked without any input.

    Missing the [os-cursor] extra otherwise shows only when the tool is called. Never waits
    long: an answer not ready yet is reported as still being measured, not as a failure.
    """
    if _OS_CURSOR_PROBE["thread"] is None:
        _start_os_cursor_probe()
    thread = _OS_CURSOR_PROBE["thread"]
    if _OS_CURSOR_PROBE["result"] is None and thread is not None:
        await asyncio.to_thread(thread.join, 0.5)  # a warm measurement takes milliseconds
    result, measured_at = _OS_CURSOR_PROBE["result"], _OS_CURSOR_PROBE["measured_at"]
    if result is None:
        waited = time.monotonic() - (_OS_CURSOR_PROBE["started"] or time.monotonic())
        return {"available": None,
                "detail": f"still being measured (started {waited:.1f} s ago); ask again shortly"}
    if measured_at is not None and time.monotonic() - measured_at > _OS_CURSOR_REMEASURE:
        _start_os_cursor_probe()  # permissions can change; this answer stands until the next one
    available, reason = result
    return {"available": bool(available), "detail": reason}


@mcp.tool()
async def get_page_errors() -> str:
    """Uncaught JS exceptions captured since console_start(), WITH the capture status.

    Returns {attached, total, errors}. `attached` false + empty means NOT MONITORED, not
    "no errors": exceptions are only seen on a tab console_start() has armed.
    """
    try:
        tab = BrowserState.active_tab()
    except Exception:
        tab = None
    attached = tab is not None and _target_key(tab) in _CONSOLE_ARMED_TAB_IDS
    payload: dict[str, Any] = {"attached": attached, "total": len(BrowserState.page_errors),
                               "errors": BrowserState.page_errors}
    if not attached:
        payload["note"] = ("NOT listening on the active tab. An empty list here means nothing "
                           "was monitored. Call console_start().")
    return ok(json.dumps(payload, indent=2, default=str))


# Headers Chrome recomputes per connection. Replaying them verbatim is worse
# than omitting them: a stale :authority or a Content-Length that no longer
# matches the body is itself a bot signal.
_CURL_SKIP_HEADERS = {
    "content-length", "host", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "upgrade", "te", "trailer", "expect",
}


def _shq(value: str) -> str:
    """POSIX single-quote a value so it survives the shell verbatim."""
    return "'" + str(value).replace("'", "'\\''") + "'"


# Runs in-page. Gathers the signals bot-detectors actually read, plus the
# cross-field pairs that expose a spoof: detectors flag INCONSISTENCY (a Windows
# UA on a MacIntel platform) far more reliably than any single "bot-like" value.
_FINGERPRINT_AUDIT_JS = r"""
(() => {
  const out = {};
  const nav = navigator;
  out.webdriver = nav.webdriver === undefined ? "undefined" : String(nav.webdriver);
  out.user_agent = nav.userAgent;
  out.platform = nav.platform;
  out.languages = (nav.languages || []).join(",");
  out.language = nav.language;
  out.hardware_concurrency = nav.hardwareConcurrency;
  out.device_memory = nav.deviceMemory === undefined ? null : nav.deviceMemory;
  out.plugins = nav.plugins ? nav.plugins.length : 0;
  out.mime_types = nav.mimeTypes ? nav.mimeTypes.length : 0;
  out.has_chrome_object = typeof window.chrome === "object" && window.chrome !== null;
  out.chrome_runtime = !!(window.chrome && window.chrome.runtime);
  out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.timezone_offset_min = new Date().getTimezoneOffset();
  out.screen = screen.width + "x" + screen.height;
  out.avail = screen.availWidth + "x" + screen.availHeight;
  out.outer = window.outerWidth + "x" + window.outerHeight;
  out.inner = window.innerWidth + "x" + window.innerHeight;
  out.color_depth = screen.colorDepth;
  out.pixel_ratio = window.devicePixelRatio;
  out.touch_points = nav.maxTouchPoints;
  out.pdf_viewer = nav.pdfViewerEnabled === undefined ? null : nav.pdfViewerEnabled;
  out.secure_context = window.isSecureContext;
  out.ua_data = nav.userAgentData
    ? {brands: nav.userAgentData.brands.map(b => b.brand + "/" + b.version),
       platform: nav.userAgentData.platform, mobile: nav.userAgentData.mobile}
    : null;

  try {
    const c = document.createElement("canvas");
    const gl = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (gl) {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      out.webgl_vendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null;
      out.webgl_renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;
    } else { out.webgl_vendor = null; out.webgl_renderer = null; }
  } catch (e) { out.webgl_vendor = null; out.webgl_renderer = null; }

  try {
    // Same input twice: a canvas that changes between draws is a randomiser,
    // which is itself detectable as non-human.
    const draw = () => {
      const c = document.createElement("canvas");
      c.width = 200; c.height = 40;
      const ctx = c.getContext("2d");
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60"; ctx.fillRect(0, 0, 100, 20);
      ctx.fillStyle = "#069"; ctx.fillText("mcp-stealth-audit", 2, 15);
      return c.toDataURL();
    };
    const a = draw(), b = draw();
    out.canvas_stable = a === b;
    out.canvas_hash = a.length + ":" + a.slice(-24);
  } catch (e) { out.canvas_stable = null; }

  try {
    out.permissions_consistent = null;
    if (nav.permissions && nav.permissions.query) {
      out.permissions_query_present = true;
    }
  } catch (e) {}

  // Classic automation leaks
  const leaks = [];
  for (const k of Object.keys(window)) {
    if (/^(cdc_|\$cdc|__webdriver|__selenium|__nightmare|__fxdriver|_phantom|callPhantom|domAutomation)/.test(k)) {
      leaks.push(k);
    }
  }
  out.automation_globals = leaks;
  out.error_stack_has_puppeteer = (() => {
    try { null.f(); } catch (e) {
      return /puppeteer|playwright|nodriver/i.test(String(e.stack || ""));
    }
    return false;
  })();
  return JSON.stringify(out);
})()
"""


_SPEECH_VOICES_JS = """
new Promise((resolve) => {
  const report = () => {
    const v = speechSynthesis.getVoices();
    return JSON.stringify({total: v.length, google: v.filter((x) => x.name.startsWith("Google")).length});
  };
  if (speechSynthesis.getVoices().length) return resolve(report());
  speechSynthesis.addEventListener("voiceschanged", () => resolve(report()), {once: true});
  setTimeout(() => resolve(report()), 2500);
})
"""


@mcp.tool()
async def fingerprint_audit(url: Optional[str] = None) -> str:
    """Audit THIS browser's own fingerprint for the leaks detectors look for.

    Self-diagnostic for the stealth claim: after fingerprint_rotate, behind a
    proxy, or in headless, is this browser still coherent? Runs entirely
    in-page — no third-party detector required, nothing about you leaves the
    machine.

    Reports findings by severity, and is explicit about what it CANNOT see from
    inside the page (TLS/JA3 and IP-vs-timezone need a network peer), so a clean
    report is never mistaken for a total guarantee.

    Args:
        url: navigate here first (e.g. a real target site) so the audit reflects
            that page's context. Omit to audit the current page.

    Weighs cross-field consistency heavily: detectors flag a Windows UA on a
    MacIntel platform far more reliably than any single suspicious value.
    """
    try:
        tab = BrowserState.active_tab()
        if url:
            await _wait(tab.get(url), what="fingerprint_audit navigate")
            await asyncio.sleep(1.0)
        raw = await _wait(tab.evaluate(_FINGERPRINT_AUDIT_JS, return_by_value=True),
                          what="fingerprint_audit")
        raw = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        fp = json.loads(raw) if raw else {}
        if not fp:
            return err("audit script returned nothing — is a page loaded?")

        critical: list[str] = []
        warnings: list[str] = []
        passed: list[str] = []

        def check(is_ok: bool, label: str, detail: str, severity: str = "critical"):
            if is_ok:
                passed.append(label)
            else:
                (critical if severity == "critical" else warnings).append(
                    f"{label}: {detail}")

        check(fp.get("webdriver") in ("undefined", "false"), "navigator.webdriver",
              f"exposed as {fp.get('webdriver')!r} — the single most-checked flag")
        check(not fp.get("automation_globals"), "automation globals",
              f"leaked {fp.get('automation_globals')}")
        check(not fp.get("error_stack_has_puppeteer"), "error stack",
              "stack traces name an automation library")
        check(bool(fp.get("has_chrome_object")), "window.chrome",
              "missing — real Chrome always defines it")
        check((fp.get("plugins") or 0) > 0, "navigator.plugins",
              "empty plugin array is a headless tell", "warning")
        check(bool(fp.get("languages")), "navigator.languages",
              "empty — real browsers always report at least one", "warning")

        ua = (fp.get("user_agent") or "")
        platform = (fp.get("platform") or "")
        ua_os = ("windows" if "Windows" in ua else
                 "mac" if "Macintosh" in ua or "Mac OS" in ua else
                 "linux" if "Linux" in ua or "X11" in ua else "unknown")
        plat_os = ("windows" if platform.startswith("Win") else
                   "mac" if platform.startswith("Mac") else
                   "linux" if "Linux" in platform else "unknown")
        check(ua_os == plat_os or "unknown" in (ua_os, plat_os),
              "UA vs navigator.platform",
              f"UA says {ua_os}, platform says {plat_os} ({platform!r}) — "
              f"detectors weight this mismatch heavily")

        check("HeadlessChrome" not in ua, "headless marker in UA",
              "the User-Agent announces HeadlessChrome")
        not_checked_hints: list[str] = []
        ua_data = fp.get("ua_data")
        ua_version = chrome_version(ua)
        if ua_version and ua_data is None:
            # userAgentData only exists on secure contexts, so this is unmeasured
            # here rather than clean.
            not_checked_hints.append(
                "Client hints (navigator.userAgentData) — unavailable on this page "
                "because it is not a secure context; audit an https:// page")
        elif ua_version:
            brands = ua_data.get("brands") or []
            check(bool(brands), "userAgentData.brands",
                  "empty on a Chrome UA — real Chrome always lists its brands")
            major = ua_version.split(".")[0]
            chrome_brand = next((b for b in brands if b.startswith("Google Chrome/")), None)
            if brands:
                check(chrome_brand == f"Google Chrome/{major}",
                      "UA version vs client hints",
                      f"UA says Chrome {major}, brands say {brands}")
            hint_os = {"Windows": "windows", "macOS": "mac", "Linux": "linux"}.get(
                ua_data.get("platform") or "")
            if hint_os:
                check(hint_os == ua_os or ua_os == "unknown", "UA vs client hints platform",
                      f"UA says {ua_os}, userAgentData.platform says {ua_data.get('platform')!r}")

        renderer = (fp.get("webgl_renderer") or "")
        if renderer:
            gpu_os = ("mac" if "Apple" in renderer else
                      "windows" if ("Direct3D" in renderer or "ANGLE" in renderer
                                    and "Apple" not in renderer) else "unknown")
            check(gpu_os == "unknown" or gpu_os == plat_os or plat_os == "unknown",
                  "WebGL renderer vs platform",
                  f"renderer {renderer!r} does not match platform {platform!r}")
        else:
            warnings.append("WebGL renderer: unavailable — cannot cross-check GPU")

        check(fp.get("canvas_stable") is not False, "canvas stability",
              "canvas output changes between identical draws — a randomiser is "
              "itself a detectable signal", "warning")

        # Voices load asynchronously, so they are read apart from the synchronous audit script.
        # Every Google Chrome lists "Google ..." voices from its Network Speech component; light
        # mode turns that component off, and a page reading the list sees them missing.
        voices = None
        with contextlib.suppress(Exception):
            raw_voices = await _wait(tab.evaluate(_SPEECH_VOICES_JS, await_promise=True, return_by_value=True),
                                     timeout=5, what="fingerprint_audit voices")
            raw_voices = raw_voices.value if hasattr(raw_voices, "value") and not isinstance(raw_voices, str) else raw_voices
            voices = json.loads(raw_voices) if raw_voices else None
        if not voices or not voices.get("total"):
            not_checked_hints.append("speechSynthesis voices: the list did not load, so it was not compared")
        else:
            fp["speech_voices"] = voices
            if "Google Chrome" in json.dumps((ua_data or {}).get("brands") or []) or (
                    ua_data is None and "Chrome/" in ua and "Edg/" not in ua and "OPR/" not in ua):
                check(voices.get("google", 0) > 0, "speechSynthesis voices",
                      f"{voices['total']} voices and none from Google; Google Chrome lists its Google "
                      f"voices, so the list reads as a different browser (light mode turns them off)",
                      "warning")

        try:
            ow, oh = (fp.get("outer") or "0x0").split("x")
            check(int(ow) > 0 and int(oh) > 0, "window.outer dimensions",
                  f"{fp.get('outer')} — zeros are a classic headless giveaway")
        except Exception:
            pass
        with contextlib.suppress(Exception):
            ow, oh = (int(v) for v in (fp.get("outer") or "").split("x"))
            sw, sh = (int(v) for v in (fp.get("screen") or "").split("x"))
            aw, ah = (int(v) for v in (fp.get("avail") or "").split("x"))
            if ow > 0 and oh > 0 and sw > 0 and sh > 0:
                check(ow <= sw and oh <= sh, "window vs screen size",
                      f"window {fp.get('outer')} is larger than its screen {fp.get('screen')}; "
                      f"headless Chrome reports an 800x600 screen unless --screen-info sets one",
                      "warning")
            if sw > 0 and sh > 0:
                check(aw <= sw and ah <= sh, "screen.avail vs screen size",
                      f"available area {fp.get('avail')} is larger than the screen {fp.get('screen')}")

        verdict = ("clean" if not critical and not warnings else
                   "warnings only" if not critical else "leaks detected")
        return ok(json.dumps({
            "verdict": verdict,
            "critical": critical,
            "warnings": warnings,
            "passed": passed,
            "fingerprint": fp,
            "not_checked": [
                *not_checked_hints,
                "TLS/JA3 fingerprint — needs a network peer; use "
                "http_request(impersonate=...) against a TLS-echo endpoint",
                "IP vs timezone/locale coherence — needs the exit IP's geo",
                "Behavioural signals (mouse/typing cadence) — see mouse_drift / humanize_*",
            ],
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def export_curl(
    request_id: Optional[str] = None,
    url_contains: Optional[str] = None,
    method: Optional[str] = None,
    include_cookies: bool = True,
    reveal_values: bool = False,
) -> str:
    """Turn a captured browser request into a runnable curl AND an http_request call.

    The browser→script bridge. After network_start() + the action you care about,
    hand this a request and get back the command that reproduces it outside the
    browser. Credentials are hidden unless reveal_values=True, because tool output lands
    in the transcript; the http_request form still replays the session, since it takes the
    cookies from the browser itself.

    The `http_request` variant is the one that matters here: replaying a captured
    request with plain curl sends a curl TLS fingerprint, which is exactly what
    anti-bot systems key on — the request that worked in the browser then fails
    on the command line for reasons the headers do not explain. Routing it
    through http_request(impersonate="chrome") keeps a genuine Chrome JA3, so the
    replay looks like the browser it came from.

    Args:
        request_id: id from network_get(). Omit to search by url_contains.
        url_contains: pick the most recent captured request whose URL contains
            this substring — usually easier than copying an id.
        method: additionally require this HTTP method (e.g. "POST") when searching.
        include_cookies: attach the browser's current cookies for that URL.
            Turn off to produce a shareable command with no session in it.
        reveal_values: put real cookie and credential values in the output, which makes
            the curl string runnable as shown.

    Returns the matched request, a `curl` string, and an `http_request` call.
    """
    try:
        index = BrowserState.network_index or {}
        if not index:
            return err(
                "no captured requests — call network_start() first, then trigger "
                "the action you want to reproduce, then retry."
            )
        entry = None
        if request_id:
            entry = index.get(str(request_id))
            if entry is None:
                return err(f"request_id {request_id!r} not found among "
                           f"{len(index)} captured requests")
        else:
            if not url_contains:
                return err("pass request_id= or url_contains= to pick a request")
            matches = [
                e for e in index.values()
                if url_contains in (e.get("url") or "")
                and (method is None
                     or (e.get("method") or "").upper() == method.upper())
            ]
            if not matches:
                shown = method.upper() + " " if method else ""
                return err(f"no captured request matching {shown}{url_contains!r} "
                           f"(searched {len(index)})")
            entry = matches[-1]  # most recent wins

        target_url = entry.get("url") or ""
        http_method = (entry.get("method") or "GET").upper()
        headers = {
            k: v for k, v in (entry.get("request_headers") or {}).items()
            if k.lower() not in _CURL_SKIP_HEADERS and not k.startswith(":")
        }
        body = entry.get("request_body")

        cookie_header = None
        for header_name in list(headers):
            if header_name.lower() == "cookie":
                cookie_header = headers.pop(header_name)
                break
        cookie_status = "disabled" if not include_cookies else "captured_header"
        cookie_error = None
        if include_cookies and not cookie_header:
            # The captured headers often omit Cookie (Chrome attaches it later),
            # so ask Chrome for exactly the cookies applicable to this URL.
            # Filtering Storage.getCookies ourselves is unsafe: suffix matching
            # can leak an example.com cookie to notexample.com, and path/secure/
            # partition rules are easy to reproduce incorrectly.
            try:
                tab = BrowserState.active_tab()
                from nodriver.cdp import network as cdp_network
                jar = await tab.send(cdp_network.get_cookies(urls=[target_url]))
                pairs = [f"{c.name}={c.value}" for c in (jar or [])]
                if pairs:
                    cookie_header = "; ".join(pairs)
                    cookie_status = "live_url_jar"
                else:
                    cookie_status = "live_url_jar_empty"
            except Exception as e:
                cookie_status = "error"
                cookie_error = f"{type(e).__name__}: {e}"
        if not include_cookies:
            cookie_header = None

        shown_url = target_url if reveal_values else mask_url(target_url)
        shown_headers = headers if reveal_values else mask_headers(headers)
        shown_body = body if reveal_values else mask_body(body)
        shown_cookie = cookie_header
        if cookie_header and not reveal_values:
            shown_cookie = masked_value(cookie_header)

        parts = ["curl", "-X", http_method, _shq(shown_url)]
        for key, value in shown_headers.items():
            parts += ["-H", _shq(f"{key}: {value}")]
        if shown_cookie:
            parts += ["-H", _shq(f"Cookie: {shown_cookie}")]
        if shown_body:
            parts += ["--data-raw", _shq(shown_body)]
        parts += ["--compressed"]

        replay_headers = dict(shown_headers)
        if cookie_header and reveal_values:
            replay_headers["Cookie"] = cookie_header
        # With cookies hidden the browser supplies them. Otherwise they are in the headers
        # or were declined, and http_request's default would add the session regardless.
        browser_cookies = include_cookies and not reveal_values
        http_call = (
            "http_request(\n"
            f"  url={shown_url!r},\n"
            f"  method={http_method!r},\n"
            f"  headers={json.dumps(replay_headers)},\n"
            + (f"  data={shown_body!r},\n" if shown_body else "")
            + f"  use_browser_cookies={browser_cookies},\n"
            + "  impersonate='chrome',\n)"
        )
        hidden = [k for k, v in shown_headers.items() if v != headers.get(k)]
        if cookie_header and not reveal_values:
            hidden.append("Cookie")
        if shown_body != body:
            hidden.append("(body fields)")
        if shown_url != target_url:
            hidden.append("(query parameters)")

        return ok(json.dumps({
            "request_id": entry.get("request_id"),
            "url": shown_url,
            "method": http_method,
            "status": entry.get("status"),
            "header_count": len(headers),
            "has_body": bool(body),
            "cookies_attached": bool(cookie_header),
            "cookie_status": cookie_status,
            **({"cookie_error": cookie_error} if cookie_error else {}),
            "curl": " ".join(parts),
            "http_request": http_call,
            **({"values_hidden": hidden,
                "hidden_note": ("These read *** here, so the curl string does not run as shown. "
                                "http_request takes the session cookies from the browser; "
                                "reveal_values=True returns every real value.")}
               if hidden else {}),
            "note": (
                "Prefer the http_request form: plain curl carries a curl TLS "
                "fingerprint, which anti-bot systems flag even when every header "
                "matches. impersonate='chrome' replays it with a real Chrome JA3."
            ),
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))


# Request interception, export_har and dry_run live in tools/capture.py. _drop_handlers stays
# here because console, network and wait tools use it too.


def _drop_handlers(tab: Any, pairs: Any) -> None:
    """Remove exactly these closures. tab.remove_handler(evt, fn) drops the whole list for
    the event on nodriver 0.48.1, which would kill a concurrent capture."""
    for event_type, handler in pairs:
        with contextlib.suppress(KeyError, ValueError, AttributeError):
            tab.handlers[event_type].remove(handler)


# fingerprint_rotate, the identity tools, humanize_click and humanize_type, storage_state_save and
# storage_state_load, solve_captcha and verify_cf live in tools/stealth.py. _apply_storage_state stays
# here because launch paths and the os-cursor solver share it.


async def _apply_storage_state(browser: Browser, path: str) -> dict:
    """Load cookies+localStorage from a JSON file into browser. Returns what happened.

    Raises when the file had cookies and none could be set: a session that did not
    load must not read as one that did.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} holds a JSON {type(data).__name__}, not a storage state object")
    from nodriver.cdp import network as cdp_network
    tab = browser.tabs[0] if browser.tabs else await browser.get("about:blank")
    report: dict[str, Any] = {"cookies_set": 0, "cookies_failed": 0,
                              "origins_set": 0, "origins_failed": 0, "first_error": None}
    for c in data.get("cookies", []):
        try:
            # CookieParam calls expires.to_json(), which a plain number from the file does
            # not have. Session cookies are saved as -1 and must stay session cookies.
            raw_expires = c.get("expires")
            expires = None
            if raw_expires is not None and float(raw_expires) > 0:
                expires = cdp_network.TimeSinceEpoch(float(raw_expires))
            accepted = await tab.send(cdp_network.set_cookie(
                name=c.get("name"), value=c.get("value"),
                domain=c.get("domain"), path=c.get("path", "/"),
                secure=c.get("secure", False),
                http_only=c.get("http_only", c.get("httpOnly", False)),
                expires=expires,
            ))
            if accepted is False:
                raise RuntimeError(f"Chrome rejected cookie {c.get('name')!r}")
            report["cookies_set"] += 1
        except Exception as exc:
            report["cookies_failed"] += 1
            report["first_error"] = report["first_error"] or _exc_text(exc)
    # LocalStorage — one evaluate per origin (was one round-trip PER KEY).
    for origin, pairs in (data.get("origins") or {}).items():
        try:
            await tab.get(origin)
            if pairs:
                await tab.evaluate(
                    f"(() => {{ const d = {json.dumps(pairs)}; "
                    f"for (const k in d) localStorage.setItem(k, d[k]); }})()",
                    return_by_value=True,
                )
            report["origins_set"] += 1
        except Exception as exc:
            report["origins_failed"] += 1
            report["first_error"] = report["first_error"] or _exc_text(exc)
    if report["cookies_failed"] and not report["cookies_set"]:
        raise RuntimeError(
            f"none of the {report['cookies_failed']} cookies could be set: "
            f"{report['first_error']}"
        )
    return report


# ══════════════════════════════════════════════════════════════════════════
# The precision mouse kit (click_turnstile, offset and image clicks, drift, record and replay)
# lives in tools/mouse_kit.py.


# ══════════════════════════════════════════════════════════════════════════
# 21. AI VISION CAPTCHA SOLVER — moved to tools/vision.py (v0.4.1)
# ══════════════════════════════════════════════════════════════════════════
from .tools import vision as _vision  # noqa: F401  -- registers all vision tools
# Re-export private helpers AND tool callables. Tools must remain in server globals
# because workflow_run.dispatch_table looks them up by name there.
from .tools.vision import (  # noqa: F401
    _PROMPT_TEMPLATE, _VISION_LOCATE_PROMPT,
    _parse_tile_indices, _parse_vision_response,
    _claude_vision_pick_tiles, _openai_compat_vision_pick_tiles,
    _resolve_vision_provider,
    solve_recaptcha_ai, vision_locate, vision_describe, vision_ocr, vision_check,
)


# ══════════════════════════════════════════════════════════════════════════
# 22. DUAL-MODE HTTP — moved to tools/network_http.py (v0.4.1)
# ══════════════════════════════════════════════════════════════════════════
from .tools import network_http as _network_http  # noqa: F401
# Re-export so workflow_run + http_request_with_session find them in globals().
from .tools.network_http import (  # noqa: F401
    _get_browser_cookies_for_url,
    http_request, http_request_cancel, http_request_retry, http_request_status,
    http_session_cookies, session_warmup, detect_anti_bot,
)


# ══════════════════════════════════════════════════════════════════════════
# 23. MULTI-INSTANCE BROWSER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════
#
# Run multiple isolated browsers in parallel — one per account, per site, or
# per worker. Each instance has its own profile, tabs, cookies, and logs.
# Idle instances auto-close after configurable timeout (prevents memory leaks).


# How long a Chrome that is running but missed nodriver's window gets to open its port.
SLOW_CHROME_WAIT = 10.0


async def _start_chrome(config: Config) -> Browser:
    """nodriver.start, surviving a Chrome that was merely slow to answer.

    nodriver probes for about 2.3 seconds and raises, which a busy machine, several
    sessions launching at once or a browser update can exceed. The process keeps starting
    anyway, and with no Browser returned nothing knows it exists: it is left running,
    holding its profile. So it is given time and adopted when it answers. Ending it and
    spawning another would only add load and meet the same short window. One that never
    answers is ended, and the launch tried once more.

    nodriver writes the host and port it picked into the config and reads a set pair as
    "connect to a running Chrome", so a retry that keeps them spawns nothing and only
    knocks on the port of the Chrome just ended.

    A failed exception carries `reap_report`: the launch's Chrome is already dealt with.
    """
    host, port = config.host, config.port
    for attempt in (1, 2):
        try:
            return await asyncio.wait_for(
                nodriver.start(config=config), timeout=BROWSER_LAUNCH_TIMEOUT,
            )
        except asyncio.CancelledError:
            await _reap_failed_start(config)
            raise
        except Exception as exc:
            slow = "Failed to connect" in str(exc)
            adopted = await _adopt_slow_chrome(config) if slow else None
            if adopted is not None:
                return adopted
            reap = await _reap_failed_start(config)
            if attempt == 2 or not slow:
                _remove_temp_profile(config)
                exc.reap_report = reap
                raise
            config.host, config.port = host, port
            await asyncio.sleep(1.0)
    raise AssertionError("unreachable")


async def _adopt_slow_chrome(config: Config) -> Optional[Browser]:
    """Connect to the Chrome a failed nodriver.start left starting, once its port answers."""
    registered = nodriver.core.util.get_registered_instances()
    spawned = next((b for b in registered if b.config is config), None)
    http = getattr(spawned, "_http", None)
    process = getattr(spawned, "_process", None)
    if http is None or process is None:
        return None
    deadline = time.monotonic() + SLOW_CHROME_WAIT
    while True:
        if getattr(process, "returncode", None) is not None:
            return None  # exited: it handed over to another Chrome, or crashed
        with contextlib.suppress(Exception):
            await asyncio.wait_for(http.get("version"), timeout=1.0)
            break
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)
    try:
        # With the port in the config this connects rather than spawns. The new Browser
        # takes over the process, or closing it would only drop the socket.
        browser = await asyncio.wait_for(nodriver.start(config=config), timeout=BROWSER_LAUNCH_TIMEOUT)
    except Exception:
        return None
    registered.discard(spawned)
    browser._process, browser._process_pid = process, spawned._process_pid
    return browser


async def _reap_failed_start(config: Config) -> dict:
    """End the Chrome a failed nodriver.start spawned, by the PID nodriver recorded.

    Never by scanning the profile: when another Chrome already holds it, the one spawned
    here hands over and exits, so a scan would find and signal that other browser.
    """
    report: dict = {"killed": [], "still_alive": []}
    registered = nodriver.core.util.get_registered_instances()
    for instance in [b for b in registered if b.config is config]:
        registered.discard(instance)
        report = await _ensure_browser_dead(instance, None, grace=0.3)
    return report


def _remove_temp_profile(config: Config) -> None:
    """Remove the temporary profile nodriver made for an ephemeral launch that failed."""
    profile = Path(str(getattr(config, "user_data_dir", "") or ""))
    if profile.name.startswith("uc_") and Path(tempfile.gettempdir()).resolve() in profile.resolve().parents:
        shutil.rmtree(profile, ignore_errors=True)


def _profile_directory_args(user_data_dir: Optional[Path], extra_args: Optional[list[str]]) -> list[str]:
    """--profile-directory for a user-data dir holding several profiles.

    Without it Chrome opens its profile picker there, a window with no tab. A cloned
    multi-profile browser then fails with "no browser is open", though nothing is wrong
    with it. Uses the profile that browser last used, as a person opening it would.
    """
    if user_data_dir is None or any(
        (arg or "").startswith("--profile-directory=") for arg in (extra_args or [])
    ):
        return []
    chosen = "Default"
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
        local_state = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
        chosen = local_state["profile"]["last_used"] or chosen
    if isinstance(chosen, str) and chosen and "/" not in chosen and "\\" not in chosen \
            and chosen not in (".", "..") and (user_data_dir / chosen).is_dir():
        return [f"--profile-directory={chosen}"]
    return ["--profile-directory=Default"] if (user_data_dir / "Default").is_dir() else []


def _exc_text(e: BaseException) -> str:
    """An exception's message, or its type when str() is blank.

    nodriver raises message-less exceptions on some launch failures, which used to
    surface as an error with nothing after the colon.
    """
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else f"{type(e).__name__} (no message)"


def _instance_profile_path(
    instance_id: str,
    *,
    persistent: bool,
    privacy: bool,
    profile_dir_override: Optional[str],
) -> Optional[Path]:
    """Resolve the user-data dir actually passed to a named Chrome launch."""
    if privacy:
        return _new_privacy_profile(instance_id)
    if profile_dir_override:
        return Path(profile_dir_override).expanduser()
    if not persistent:
        return None
    return PROFILE_DIR if instance_id == "main" else PROFILES_ROOT / instance_id


async def _launch_browser_instance(
    instance_id: str,
    url: str,
    headless: bool,
    proxy: Optional[str],
    user_agent: Optional[str],
    window_width: int,
    window_height: int,
    persistent: bool,
    lang: str,
    extra_args: Optional[list[str]],
    storage_state_path: Optional[str],
    idle_timeout: int,
    profile_dir_override: Optional[str] = None,
    fingerprint: Optional[dict] = None,
    privacy: bool = False,
    desc: str = "",
    restore_tabs: bool = False,
) -> tuple[bool, str]:
    """Shared launcher for browser_launch + spawn_browser.

    Returns (success, message).
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return False, str(e)

    # Pre-flight: ensure Chrome is installed before calling nodriver
    chrome_path = find_chrome_binary()
    if chrome_path is None:
        return False, (
            "Chrome/Chromium not found on this system.\n"
            + chrome_install_hint()
            + "\n\nAfter installing, re-launch the MCP server."
        )

    ensure_dirs()

    if privacy and (profile_dir_override or any(
        (arg or "").startswith("--user-data-dir=") for arg in (extra_args or [])
    )):
        return False, "privacy mode cannot reuse profile_dir or --user-data-dir"
    if privacy:
        persistent = False
    try:
        effective_proxy, effective_fingerprint, identity_source = _launch_identity(
            instance_id,
            proxy=proxy,
            user_agent=user_agent,
            fingerprint=fingerprint,
            privacy=privacy,
        )
    except Exception as e:
        return False, f"identity setup failed: {e}"

    # Determine profile dir per instance
    profile_path = _instance_profile_path(
        instance_id,
        persistent=persistent,
        privacy=privacy,
        profile_dir_override=profile_dir_override,
    )
    privacy_profile = profile_path if privacy else None
    launch_choice: Optional[_LaunchChoice] = None
    if profile_path is not None:
        if profile_path.exists():
            holder = chrome_lock_holder_pid(profile_path)
            if holder is not None:
                return False, (
                    f"profile {profile_path} is locked by Chrome PID {holder}; "
                    "close that browser or clone the profile first"
                )
        # Before mkdir and clean_profile_state write to it: whether the profile is new decides
        # whether Chrome Beta may open it, and a refused profile stays as it was.
        launch_choice = _launch_choice(profile_path, headless, extra_args)
        refused = _profile_version_refusal(profile_path, launch_choice.executable)
        if refused:
            await _remove_privacy_profile(privacy_profile)
            return False, refused
        profile_path.mkdir(parents=True, exist_ok=True)
        if persistent or profile_dir_override:
            clean_profile_state(profile_path)

    if lang and lang != "en-US" and not effective_fingerprint.get("accept_language"):
        effective_fingerprint = {**effective_fingerprint, "accept_language": lang}
    config = Config(
        user_data_dir=str(profile_path) if profile_path is not None else None,
        headless=headless,
        lang=(effective_fingerprint.get("accept_language", "").split(",")[0] or lang),
        browser_args=[*(extra_args or []), *_profile_directory_args(profile_path, extra_args)],
    )
    config.add_argument("--hide-crash-restore-bubble")
    config.add_argument("--disable-session-crashed-bubble")
    config.add_argument("--disable-restore-session-state")
    config.add_argument("--no-default-browser-check")
    for _flag in _LEAN_FLAGS:
        config.add_argument(_flag)
    if effective_fingerprint.get("user_agent"):
        config.add_argument(f"--user-agent={effective_fingerprint['user_agent']}")
    if effective_proxy:
        config.add_argument(f"--proxy-server={effective_proxy}")
    config.add_argument(f"--window-size={window_width},{window_height}")
    if headless:
        _screen_arg = _headless_screen_arg(window_width, window_height, extra_args)
        if _screen_arg:
            config.add_argument(_screen_arg)

    browser: Optional[Browser] = None
    if launch_choice is None:
        launch_choice = _launch_choice(profile_path, headless, extra_args)
    dock_note = await _use_dock_icon_browser(config, headless, launch_choice)
    refused = _profile_version_refusal(profile_path, config.browser_executable_path)
    if refused:
        await _remove_privacy_profile(privacy_profile)
        return False, refused
    with contextlib.suppress(Exception):
        dock_note += profile_version.upgrade_note(profile_path, config.browser_executable_path)
    cookie_move = await _prepare_cookie_move(profile_path, config)
    if cookie_move.flag:
        config.add_argument(_cookie_flag_arg(config))
    if not cookie_move.backup:
        dock_note += cookie_move.note
    previous_app = None if headless else await asyncio.to_thread(_frontmost_app)
    if not headless:
        for _flag in _BACKGROUND_WINDOW_FLAGS:
            config.add_argument(_flag)
    try:
        browser = await _start_chrome(config)
        if not headless:
            await _open_background_window(browser, window_width, window_height)
    except asyncio.TimeoutError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(privacy_profile)
        return False, (
            f"launch timed out after {BROWSER_LAUNCH_TIMEOUT}s — profile "
            f"{profile_path or '(ephemeral)'} may be locked or Chrome is hung."
        )
    except asyncio.CancelledError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(privacy_profile)
        raise
    except Exception as e:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(privacy_profile)
        return False, f"launch failed: {_exc_text(e)}"

    cookie_error = ""
    if cookie_move.backup is not None:
        moved, moved_text = await _finish_cookie_move(browser, Path(str(profile_path)), cookie_move)
        if moved:
            dock_note += moved_text
        else:
            cookie_error = moved_text

    if storage_state_path:
        try:
            await _apply_storage_state(browser, storage_state_path)
        except asyncio.CancelledError:
            await _safe_stop_browser(browser)
            await _remove_privacy_profile(privacy_profile)
            raise
        except Exception as e:
            await _safe_stop_browser(browser)
            await _remove_privacy_profile(privacy_profile)
            return False, f"storage_state load failed: {e}"

    try:
        await asyncio.sleep(0.5)
        main_tab = await _first_live_tab(browser)
        await _apply_fingerprint_to_tab(main_tab, effective_fingerprint)
        await asyncio.wait_for(main_tab.get(url), timeout=BROWSER_NAV_TIMEOUT)
        await _wait_load_complete(main_tab, 3)
    except asyncio.TimeoutError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(privacy_profile)
        return False, (
            f"initial nav timed out after {BROWSER_NAV_TIMEOUT}s on instance "
            f"{instance_id!r}."
        )
    except asyncio.CancelledError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(privacy_profile)
        raise
    except Exception as e:
        # Verified, not assumed: a Chrome still dying keeps the profile lock, and the next
        # launch would then be refused as "in use" by a browser nobody asked for.
        await _ensure_browser_dead(browser, profile_path)
        await _remove_privacy_profile(privacy_profile)
        return False, f"initial nav failed: {_exc_text(e)}"

    slot_tabs, slot_active, restore_text = [main_tab], 0, ""
    if restore_tabs:
        try:
            slot_tabs, slot_active, _report, _summary = await _reopen_saved_tabs(
                instance_id, browser, main_tab, profile_path, privacy, headless,
                effective_fingerprint, url, window_width, window_height)
            restore_text = f"{_summary}\n{json.dumps({'restore_tabs': _report}, indent=2, default=str)}"
        except Exception as e:
            restore_text = f" [restore_tabs: failed: {_exc_text(e)}]"

    # Write into the target instance slot
    if instance_id == BrowserState.current_instance_id:
        BrowserState.browser = browser
        BrowserState.tabs = slot_tabs
        BrowserState.active_tab_index = slot_active
        BrowserState.current_profile_dir = profile_path
        BrowserState.current_desc = desc
        BrowserState.current_headless = headless
        BrowserState.current_privacy = privacy
        BrowserState.current_fingerprint = dict(effective_fingerprint)
        BrowserState.current_proxy = effective_proxy
        BrowserState.current_idle_timeout = idle_timeout
        BrowserState.current_last_active = time.time()
        BrowserState.current_created_at = time.time()
    else:
        # Store as snapshot without becoming current
        snap = InstanceSnapshot(
            instance_id=instance_id,
            browser=browser,
            tabs=slot_tabs,
            active_tab_index=slot_active,
            profile_dir=profile_path,
            desc=desc,
            headless=headless,
            privacy=privacy,
            fingerprint=dict(effective_fingerprint),
            proxy=effective_proxy,
            idle_timeout=idle_timeout,
            last_active=time.time(),
            created_at=time.time(),
        )
        BrowserState.instances[instance_id] = snap

    # Kick off the idle reaper (once)
    _ensure_idle_reaper_running()
    BrowserState.window_closed.pop(instance_id, None)
    if not headless:
        _watch_windows(browser)
    await _give_focus_back(previous_app)
    if cookie_error:
        # The instance is registered and running; the text must still read as a failure.
        return True, (f"Error: instance {instance_id!r} is running, but its cookies were not all "
                      f"moved: {cookie_error}")
    return True, (
        f"instance {instance_id!r} launched (headless={headless}, "
        f"privacy={privacy}, identity={identity_source}, "
        f"profile={profile_path.name if profile_path else '(ephemeral Chrome temp)'})"
        f"{dock_note}{restore_text}"
    )


@contextlib.contextmanager
def _closing_in_background(instance_id: str):
    """While a sweep closes an instance, a call on it waits (_app.INSTANCE_CLOSING) instead of
    acting on a browser half closed, or switching it in while the close goes on."""
    key = (BrowserState.session_key(), instance_id)
    event = asyncio.Event()
    _app_module.INSTANCE_CLOSING[key] = event
    try:
        yield
    finally:
        if _app_module.INSTANCE_CLOSING.get(key) is event:
            del _app_module.INSTANCE_CLOSING[key]
        event.set()


def _still_held(iid: str, snap: Optional[InstanceSnapshot], browser: Any) -> bool:
    """A sweep's candidate is still where the sweep found it. Between its awaits a switch_instance
    can make a stored instance current, or store the current one: closing by the old position
    then ends the wrong browser, or resets the instance that is current by then."""
    if snap is None:
        return BrowserState.current_instance_id == iid and BrowserState.browser is browser
    return BrowserState.instances.get(iid) is snap


async def _end_stored_instance(iid: str, snap: InstanceSnapshot,
                               record_reason: Optional[str] = None) -> bool:
    """Close a stored instance like close_instance does. True once its Chrome is verified gone.

    record_reason: save its tabs first, under that reason. None when Chrome is already gone."""
    with _closing_in_background(iid):
        return await _end_stored_instance_now(iid, snap, record_reason)


async def _end_stored_instance_now(iid: str, snap: InstanceSnapshot,
                                   record_reason: Optional[str]) -> bool:
    _tabs = list(snap.tabs)
    # stop() can return without raising when the socket is gone. Keep the instance
    # reachable and retry later unless the process is really gone; dropping it
    # would leak a live Chrome with no remaining handle.
    if snap.browser:
        if record_reason:
            await _record_tabs_before_close(iid, snap.browser, _tabs, snap.active_tab_index,
                                            snap.profile_dir, snap.privacy, record_reason)
        _dead = await _ensure_browser_dead(snap.browser, snap.profile_dir)
        if _close_incomplete(_dead):
            return False
    # Mark profile cleanly exited (matches close_instance) so the
    # next launch doesn't hit a "Restore pages?" / stale-lock state.
    if snap.privacy:
        if not await _remove_privacy_profile(snap.profile_dir):
            snap.browser = None
            snap.tabs = []
            _clear_tab_scoped_state(_tabs)
            return True
    elif snap.profile_dir:
        clean_profile_state(snap.profile_dir)
    _clear_tab_scoped_state(_tabs)
    BrowserState.instances.pop(iid, None)
    return True


async def _end_current_instance(record_reason: Optional[str] = None) -> bool:
    """Close the calling session's current instance. True once its Chrome is verified gone.

    record_reason: save its tabs first, under that reason. None when Chrome is already gone."""
    with _closing_in_background(BrowserState.current_instance_id):
        return await _end_current_instance_now(record_reason)


async def _end_current_instance_now(record_reason: Optional[str]) -> bool:
    _cur_profile = BrowserState.current_profile_dir
    _cur_privacy = BrowserState.current_privacy
    _cur_tabs = list(BrowserState.tabs)
    if BrowserState.browser:
        if record_reason:
            await _record_tabs_before_close(
                BrowserState.current_instance_id, BrowserState.browser, _cur_tabs,
                BrowserState.active_tab_index, _cur_profile, _cur_privacy, record_reason)
        _dead = await _ensure_browser_dead(BrowserState.browser, _cur_profile)
        if _close_incomplete(_dead):
            # State must remain reachable so the next sweep can retry.
            return False
    BrowserState.reset()
    _clear_tab_scoped_state(_cur_tabs)
    BrowserState.current_headless = False
    BrowserState.current_privacy = False
    if _cur_privacy:
        if not await _remove_privacy_profile(_cur_profile):
            BrowserState.current_profile_dir = _cur_profile
            BrowserState.current_privacy = True
    elif _cur_profile:
        clean_profile_state(_cur_profile)
    return True


def _idle_closed_note(iid: str, timeout: int) -> str:
    return (f"instance {iid!r} was closed at {time.strftime('%H:%M:%S')} after {timeout} s with no "
            f"tool call (idle_timeout_seconds={timeout}).")


async def _sweep_idle_instances() -> None:
    """Close the calling session's instances that are past their idle timeout."""
    for iid, snap in list(BrowserState.instances.items()):
        if not _still_held(iid, snap, snap.browser):
            continue  # switched in while an earlier one was being closed
        if snap.is_running() and snap.is_idle_expired():
            BrowserState.window_closed[iid] = _idle_closed_note(iid, snap.idle_timeout)
            if not await _end_stored_instance(iid, snap, "idle timeout"):
                BrowserState.window_closed.pop(iid, None)
    # A tool call running in this session is use of its current instance, however long it takes.
    if (BrowserState.is_up()
        and BrowserState.current_idle_timeout > 0
        and not _app_module.calls_in_flight(BrowserState.session_key())
        and (time.time() - BrowserState.current_last_active) > BrowserState.current_idle_timeout):
        iid = BrowserState.current_instance_id
        BrowserState.window_closed[iid] = _idle_closed_note(iid, BrowserState.current_idle_timeout)
        if not await _end_current_instance("idle timeout"):
            BrowserState.window_closed.pop(iid, None)


async def _idle_reaper_loop() -> None:
    """Close instances idle past their timeout, in every session and the pool."""
    while True:
        try:
            await asyncio.sleep(IDLE_REAPER_INTERVAL)
        except asyncio.CancelledError:
            return
        with contextlib.suppress(Exception):
            BrowserState.collect_departed_sessions()
        for key in BrowserState.session_keys():
            token = BrowserState.in_session(key)
            try:
                await _sweep_idle_instances()
            except asyncio.CancelledError:
                return
            except Exception:
                continue  # one session's failure must not stop the others
            finally:
                BrowserState.unbind_session(token)


def _ensure_idle_reaper_running() -> None:
    """Start the reaper task once, lazily."""
    if BrowserState._reaper_task is not None and not BrowserState._reaper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
        BrowserState._reaper_task = loop.create_task(_idle_reaper_loop())
    except RuntimeError:
        pass  # no event loop yet — will try again on next launch


# A headed Chrome starts with --no-startup-window so it never takes the keyboard, and Chrome
# started that way does not exit on Quit: the quit Apple Event closes every window and the
# process stays, with its Dock icon and the profile lock. Measured: still running 10 s after the
# quit, while the same Chrome started normally exits in 0.3 s. Nothing in CDP tells a Quit from
# closing every window by hand. Ending it from inside this server only worked while the server
# was alive and healthy: a stdio server exits on a client reconnect, and one started before a
# reinstall keeps running old code. So each such browser gets its own detached watcher process
# (quit_watch, _quit_watcher.py) that ends it after the grace, server or not. The sweep below
# records why for the next call and cleans up after it, and ends the browser itself only when
# no watcher runs for it, so the two never both close it.
_WINDOWLESS_GRACE = float(os.environ.get("MCP_WINDOWLESS_CLOSE_SECONDS", "3"))
_WINDOW_WATCH_INTERVAL = 1.0
_WINDOW_WATCH: dict[int, dict] = {}  # id(browser) -> {"browser", "zero_since", "tool_closed"}
_window_watch_task: Optional[asyncio.Task] = None


def _watch_windows(browser: Any) -> None:
    """Watch a headed browser started with _BACKGROUND_WINDOW_FLAGS for losing every window."""
    _WINDOW_WATCH[id(browser)] = {"browser": browser, "zero_since": None, "tool_closed": False}
    quit_watch.start(browser, _WINDOWLESS_GRACE, _CLOSE_WAIT)
    _ensure_window_watcher_running()


def _window_watch_tool_closed(browser: Any) -> None:
    """A tool closed the last tab on purpose: that is not the person quitting."""
    quit_watch.mark_tool_closed(browser)
    entry = _WINDOW_WATCH.get(id(browser))
    if entry is not None:
        entry["tool_closed"] = True
        entry["zero_since"] = None


async def _page_target_count(browser: Any) -> Optional[int]:
    """Pages the browser has, or None when it could not be asked."""
    from nodriver.cdp import target as cdp_target
    try:
        infos = await asyncio.wait_for(
            (getattr(browser, "connection", None) or browser).send(cdp_target.get_targets()), timeout=3)
    except Exception:
        return None
    return sum(1 for info in infos if getattr(info, "type_", "") == "page")


async def _sweep_windowless() -> set[int]:
    """End the calling session's watched browsers that lost every window. Returns the ids of
    the browsers this session holds, so entries for closed browsers can be dropped."""
    held: set[int] = set()
    candidates = [(BrowserState.current_instance_id, None, BrowserState.browser)]
    candidates += [(iid, snap, snap.browser) for iid, snap in list(BrowserState.instances.items())]
    for iid, snap, browser in candidates:
        if browser is None:
            continue
        held.add(id(browser))
        entry = _WINDOW_WATCH.get(id(browser))
        if entry is None or entry["browser"] is not browser:
            continue
        quit_at = quit_watch.quit_time(browser)
        if quit_at is not None:
            # The watcher is ending it because its last window went: say so at once, and
            # clean up once the watcher is done.
            BrowserState.window_closed[iid] = quit_watch.closed_note(iid, quit_at)
            if quit_watch.running(browser) or not _still_held(iid, snap, browser):
                continue
            if await (_end_current_instance() if snap is None else _end_stored_instance(iid, snap)):
                quit_watch.forget(browser)
                _WINDOW_WATCH.pop(id(browser), None)
            continue
        if quit_watch.running(browser):
            # The watcher decides; two closers would race. A verdict it withdrew (Chrome
            # survived its close and it tries again later) must not outlive it here.
            BrowserState.window_closed.pop(iid, None)
            continue
        quit_watch.reap(browser)
        pages = await _page_target_count(browser)
        now = time.monotonic()
        if pages is None:
            entry["zero_since"] = None  # unanswered is not "no windows"
            continue
        if pages:
            entry["zero_since"] = None
            entry["tool_closed"] = False
            continue
        if entry["tool_closed"]:
            continue
        if entry["zero_since"] is None:
            entry["zero_since"] = now
            continue
        if now - entry["zero_since"] < _WINDOWLESS_GRACE:
            continue
        if not _still_held(iid, snap, browser):
            continue  # switched while this sweep awaited; the next one sees it where it is
        # Said before closing starts: a call arriving while Chrome is being ended must not
        # read as a crash that needs browser_recover.
        BrowserState.window_closed[iid] = (
            f"instance {iid!r} was closed at {time.strftime('%H:%M:%S')} because its last window "
            f"was closed (Quit, or closing every window).")
        ended = await (_end_current_instance("last window closed") if snap is None
                       else _end_stored_instance(iid, snap, "last window closed"))
        if not ended:
            BrowserState.window_closed.pop(iid, None)
            entry["zero_since"] = time.monotonic()  # Chrome did not die: retry after another grace
            continue
        _WINDOW_WATCH.pop(id(browser), None)
    return held


async def _window_watch_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_WINDOW_WATCH_INTERVAL)
        except asyncio.CancelledError:
            return
        if not _WINDOW_WATCH:
            continue
        held: set[int] = set()
        for key in BrowserState.session_keys():
            token = BrowserState.in_session(key)
            try:
                held |= await _sweep_windowless()
            except asyncio.CancelledError:
                return
            except Exception:
                continue  # one session's failure must not stop the others
            finally:
                BrowserState.unbind_session(token)
        for stale in [k for k in _WINDOW_WATCH if k not in held]:
            _WINDOW_WATCH.pop(stale, None)


def _ensure_window_watcher_running() -> None:
    global _window_watch_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _window_watch_task
    if task is not None and not task.done() and task.get_loop() is loop:
        return
    _window_watch_task = loop.create_task(_window_watch_loop())


@mcp.tool()
async def spawn_browser(
    instance_id: str,
    url: str = "about:blank",
    headless: bool = False,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    window_width: int = 1280,
    window_height: int = 800,
    persistent: bool = True,
    lang: str = "en-US",
    extra_args: Optional[list[str]] = None,
    storage_state_path: Optional[str] = None,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT,
    profile_dir: Optional[str] = None,
    desc: str = "",
    fingerprint: Optional[dict] = None,
    privacy: bool = False,
    restore_tabs: bool = False,
) -> str:
    """Create a new named browser instance running in parallel with main.
    Each instance has its own profile, cookies, tabs, logs. Use for multi-account
    scraping or isolated sessions. A headed instance the person quits, or whose windows
    are all closed, is ended by the server, and later calls on it say so.

    Args:
        instance_id: unique name (e.g., "scraper_1", "acct_alice")
        idle_timeout_seconds: auto-close after idle (0 = never, default 600s)
        profile_dir: override profile path (default: ~/.mcp-stealth/profiles/<id>/)
        desc: natural-language purpose used to find the instance by meaning later
        fingerprint: explicit user_agent/accept_language/platform/timezone overrides
        privacy: empty profile deleted when closed. The identity stays the real browser
            and machine unless fingerprint overrides it. To use a logged-in account,
            leave this off
        restore_tabs: reopen the tabs this instance had when it was last closed, as
            browser_launch(restore_tabs=True) does
        other args: same as browser_launch

    Use switch_instance(id) to route subsequent tool calls to this instance.
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    if instance_id in BrowserState.released_ids():
        return err(f"instance {instance_id!r} was released by another session. "
                   f"Take it with switch_instance({instance_id!r}).")
    if _LAUNCH_LOCK.locked():
        return err("another launch is already in progress — wait for it to finish")
    # The name is only visible to other sessions once the launch finishes, so two
    # sessions spawning one name would both pass the check above and share a profile.
    holder = _SPAWNING.get(instance_id)
    if holder is not None:
        return err(f"instance {instance_id!r} is being launched by another session ({holder}).")
    _SPAWNING[instance_id] = BrowserState.session_key()
    try:
        async with _LAUNCH_LOCK:
            if instance_id == BrowserState.current_instance_id:
                if BrowserState.is_up():
                    return err(
                        f"instance {instance_id!r} already running (current). "
                        "Use switch_instance instead."
                    )
                if BrowserState.current_privacy and BrowserState.current_profile_dir:
                    return err(
                        f"instance {instance_id!r} has pending privacy cleanup. "
                        "Retry close_instance() before relaunching."
                    )
            if instance_id in BrowserState.instances:
                state = (
                    "running" if BrowserState.instances[instance_id].is_running()
                    else "stopped or awaiting cleanup"
                )
                return err(
                    f"instance {instance_id!r} already exists ({state}). "
                    "Call close_instance() before reusing its name."
                )
            ok_flag, msg = await _launch_browser_instance(
                instance_id=instance_id,
                url=url,
                headless=headless,
                proxy=proxy,
                user_agent=user_agent,
                window_width=window_width,
                window_height=window_height,
                persistent=persistent,
                lang=lang,
                extra_args=extra_args,
                storage_state_path=storage_state_path,
                idle_timeout=idle_timeout_seconds,
                profile_dir_override=profile_dir,
                fingerprint=fingerprint,
                privacy=privacy,
                desc=desc,
                restore_tabs=restore_tabs,
            )
    finally:
        _SPAWNING.pop(instance_id, None)
    if not ok_flag:
        return err(msg)
    return ok(msg)


@mcp.tool()
async def list_instances() -> str:
    """List all browser instances with status + last-active time.
    Also reports external (non-MCP) Chrome processes that may conflict with
    custom --user-data-dir launches."""
    if BrowserState.browser is not None:
        with contextlib.suppress(Exception):
            await _refresh_tabs()
    snapshots = BrowserState.list_snapshots()
    out = []
    now = time.time()
    for s in snapshots:
        is_current = s.instance_id == BrowserState.current_instance_id
        idle_s = int(now - s.last_active) if s.is_running() else 0
        out.append({
            "instance_id": s.instance_id,
            "current": is_current,
            "running": s.is_running(),
            "tabs": len(s.tabs),
            "idle_seconds": idle_s,
            "idle_timeout": s.idle_timeout,
            "auto_close_in": max(0, s.idle_timeout - idle_s) if s.idle_timeout > 0 and s.is_running() else None,
            "profile": str(s.profile_dir) if s.profile_dir else None,
            "desc": s.desc,
            "headless": s.headless,
            "privacy": s.privacy,
            "uptime_seconds": int(now - s.created_at) if s.is_running() else 0,
        })
    external = find_external_chrome_pids()
    payload = {
        "instances": out,
        "session": BrowserState.session_key(),
        "released_claimable": BrowserState.released_ids(),
        "other_sessions": BrowserState.other_session_instances(),
        "external_chrome_pids": external,
        "warning": (
            f"{len(external)} external Chrome process(es) detected — "
            "custom --user-data-dir pointing to your daily Chrome profile will conflict."
        ) if external else None,
    }
    return ok(json.dumps(payload, indent=2, default=str))


@mcp.tool()
async def instance_describe(instance_id: str, desc: str, append: bool = False) -> str:
    """Set or append the natural-language purpose of a browser instance."""
    try:
        _validate_instance_id(instance_id)
        foreign = BrowserState.foreign_instance(instance_id)
        if foreign:
            return err(foreign)
        if instance_id == BrowserState.current_instance_id:
            old = BrowserState.current_desc
            BrowserState.current_desc = (
                f"{old}; {desc}" if append and old and desc else (desc or old if append else desc)
            )
            updated = BrowserState.current_desc
        else:
            snap = BrowserState.instances.get(instance_id)
            if snap is None:
                return err(f"instance {instance_id!r} not found")
            old = snap.desc
            snap.desc = f"{old}; {desc}" if append and old and desc else (desc or old if append else desc)
            updated = snap.desc
        return ok(f"instance {instance_id!r} description: {updated}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def switch_instance(instance_id: str) -> str:
    """Make instance_id the active one for subsequent tool calls.

    The previous current instance continues running in the background,
    cookies/tabs preserved. Swap back anytime.
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    if instance_id == BrowserState.current_instance_id:
        return ok(f"already on {instance_id!r}")
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    # Being closed by a sweep (idle timeout, last window gone): switching it in now would hand
    # the caller a browser that is going, and the close would then reset the current instance.
    await _app_module.wait_while_closing(BrowserState.session_key(), instance_id)
    BrowserState.claim_released(instance_id)
    existed = instance_id in BrowserState.instances
    BrowserState.switch_to(instance_id)
    if not existed and not BrowserState.is_up():
        closed = BrowserState.window_closed.get(instance_id)
        if closed:
            return ok(f"switched to {instance_id!r}, which is not running: {closed} Start it again "
                      f"with spawn_browser or browser_launch")
        return ok(f"switched to {instance_id!r} (not yet running — call spawn_browser or browser_launch)")
    return ok(f"switched to {instance_id!r}")


@mcp.tool()
async def instance_release(instance_id: str) -> str:
    """Hand a running instance over so another MCP session can take it.

    Only meaningful in HTTP mode, where several sessions share one server. The
    instance keeps running, tabs and logins intact, until another session calls
    switch_instance on it or the idle timeout closes it. Switch away first if it is
    the current one.
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    if instance_id == BrowserState.current_instance_id:
        return err(
            f"{instance_id!r} is this session's current instance. "
            "switch_instance to another name first, then release it."
        )
    if instance_id not in BrowserState.instances:
        return err(f"instance {instance_id!r} not found in this session")
    BrowserState.release_instance(instance_id)
    return ok(f"released {instance_id!r}; another session can take it with "
              f"switch_instance({instance_id!r})")


@mcp.tool()
async def close_instance(instance_id: str, forget_tabs: bool = False) -> str:
    """Close a specific browser instance (frees profile + memory).

    Its open tabs are saved first (persistent profiles only) for restore_tabs=True on the next
    launch of that instance. forget_tabs=True closes without saving and removes the saved list.
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    await _app_module.wait_while_closing(BrowserState.session_key(), instance_id)
    BrowserState.claim_released(instance_id)
    # Close current
    if instance_id == BrowserState.current_instance_id:
        # The handle, not is_up(): see browser_close.
        if BrowserState.browser is not None:
            _profile = BrowserState.current_profile_dir
            _privacy = BrowserState.current_privacy
            _tabs = list(BrowserState.tabs)
            # Remember the placement before the window goes away, so the next
            # launch can land on the same monitor instead of the primary one.
            try:
                await _save_window_bounds(BrowserState.current_instance_id,
                                          BrowserState.tabs[0] if BrowserState.tabs else None)
            except Exception:
                pass
            if forget_tabs:
                _mark_closing(BrowserState.browser)
            else:
                await _record_tabs_before_close(
                    instance_id, BrowserState.browser, _tabs, BrowserState.active_tab_index,
                    _profile, _privacy, "close_instance")
            _kill = await _ensure_browser_dead(BrowserState.browser, _profile)
            if _close_incomplete(_kill):
                return err(f"close failed: {_close_failure(_kill)} — instance retained")
            _forgot = _forget_tabs_note(instance_id, _profile, _privacy) if forget_tabs else ""
            _iid = BrowserState.current_instance_id
            BrowserState.reset()
            _clear_tab_scoped_state(_tabs)
            BrowserState.current_headless = False
            BrowserState.current_privacy = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
            if _privacy:
                if not await _remove_privacy_profile(_profile):
                    BrowserState.current_profile_dir = _profile
                    BrowserState.current_privacy = True
                    return err(f"closed browser but failed to remove privacy profile {_profile}"
                               f"{_removal_failure(_profile)}")
            elif _profile:
                clean_profile_state(_profile)
            _ATTACHED_BROWSERS.discard(_iid)
            return ok(f"closed current instance {instance_id!r}" + _kill_note(_kill) + _forgot)
        if BrowserState.current_privacy:
            _profile = BrowserState.current_profile_dir
            if not await _remove_privacy_profile(_profile):
                return err(f"failed to remove stale privacy profile {_profile}{_removal_failure(_profile)}")
            BrowserState.current_privacy = False
            BrowserState.current_headless = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
            return ok(f"current instance {instance_id!r} was not running; privacy profile removed")
        if forget_tabs:
            gone = tab_record.forget_instance(instance_id)
            return ok(f"current instance {instance_id!r} was not running."
                      + (" Removed its saved tab list." if gone else " It had no saved tab list."))
        return ok(f"current instance {instance_id!r} was not running")
    # Close stored
    snap = BrowserState.instances.get(instance_id)
    if snap is None:
        return err(f"instance {instance_id!r} not found")
    _tabs = list(snap.tabs)
    try:
        await _save_window_bounds(instance_id, snap.tabs[0] if snap.tabs else None)
    except Exception:
        pass
    if forget_tabs:
        _mark_closing(snap.browser)
    else:
        await _record_tabs_before_close(instance_id, snap.browser, _tabs, snap.active_tab_index,
                                        snap.profile_dir, snap.privacy, "close_instance")
    _kill = await _ensure_browser_dead(snap.browser, snap.profile_dir)
    if _close_incomplete(_kill):
        return err(
            f"close failed for instance {instance_id!r}: {_close_failure(_kill)}"
            f" — instance retained for retry"
        )
    _forgot = _forget_tabs_note(instance_id, snap.profile_dir, snap.privacy) if forget_tabs else ""
    # Chrome is stopped even if residue cleanup below fails. Retain a minimal
    # snapshot in that case so a second close_instance call can retry cleanup.
    snap.browser = None
    snap.tabs = []
    _clear_tab_scoped_state(_tabs)
    cleanup_ok = True
    if snap.privacy:
        cleanup_ok = await _remove_privacy_profile(snap.profile_dir)
    elif snap.profile_dir:
        clean_profile_state(snap.profile_dir)
    if not cleanup_ok:
        return err(f"closed browser but failed to remove privacy profile {snap.profile_dir}"
                   f"{_removal_failure(snap.profile_dir)}")
    BrowserState.instances.pop(instance_id, None)
    _ATTACHED_BROWSERS.discard(instance_id)
    return ok(f"closed instance {instance_id!r}" + _kill_note(_kill) + _forgot)


@mcp.tool()
async def close_all_instances() -> str:
    """Close every running browser instance. Useful for cleanup."""
    closed = []
    failures = []
    session = BrowserState.session_key()
    for key in [k for k in list(_app_module.INSTANCE_CLOSING) if k[0] == session]:
        await _app_module.wait_while_closing(*key)  # one closer per browser
    # Close stored
    for iid, snap in list(BrowserState.instances.items()):
        _tabs = list(snap.tabs)
        if snap.browser:
            await _record_tabs_before_close(iid, snap.browser, _tabs, snap.active_tab_index,
                                            snap.profile_dir, snap.privacy, "close_all_instances")
            _kill = await _ensure_browser_dead(snap.browser, snap.profile_dir)
            if _close_incomplete(_kill):
                failures.append(f"{iid}: {_close_failure(_kill)}; instance retained")
                continue
        snap.browser = None
        snap.tabs = []
        _clear_tab_scoped_state(_tabs)
        if snap.privacy:
            if not await _remove_privacy_profile(snap.profile_dir):
                failures.append(f"{iid}: privacy cleanup failed ({snap.profile_dir}"
                                f"{_removal_failure(snap.profile_dir)})")
                continue
        elif snap.profile_dir:
            clean_profile_state(snap.profile_dir)
        BrowserState.instances.pop(iid, None)
        closed.append(iid)
    # Close current. The handle, not is_up(): see browser_close.
    if BrowserState.browser is not None:
        _current_profile = BrowserState.current_profile_dir
        _current_privacy = BrowserState.current_privacy
        _current_tabs = list(BrowserState.tabs)
        await _record_tabs_before_close(
            BrowserState.current_instance_id, BrowserState.browser, _current_tabs,
            BrowserState.active_tab_index, _current_profile, _current_privacy,
            "close_all_instances")
        _kill = await _ensure_browser_dead(BrowserState.browser, _current_profile)
        if _close_incomplete(_kill):
            failures.append(
                f"{BrowserState.current_instance_id}: "
                f"{_close_failure(_kill)}; instance retained"
            )
        else:
            _clear_tab_scoped_state(_current_tabs)
            cleanup_ok = True
            if _current_privacy:
                cleanup_ok = await _remove_privacy_profile(_current_profile)
            elif _current_profile:
                clean_profile_state(_current_profile)
            if cleanup_ok:
                closed.append(BrowserState.current_instance_id)
                BrowserState.reset()
                BrowserState.current_headless = False
                BrowserState.current_privacy = False
                BrowserState.current_fingerprint = {}
                BrowserState.current_proxy = None
            else:
                BrowserState.reset()
                BrowserState.current_profile_dir = _current_profile
                BrowserState.current_privacy = True
                failures.append(
                    f"{BrowserState.current_instance_id}: privacy cleanup failed "
                    f"({_current_profile}{_removal_failure(_current_profile)})"
                )
    elif BrowserState.current_privacy:
        if await _remove_privacy_profile(BrowserState.current_profile_dir):
            BrowserState.current_privacy = False
            BrowserState.current_headless = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
        else:
            failures.append(
                f"{BrowserState.current_instance_id}: stale privacy cleanup failed "
                f"({BrowserState.current_profile_dir}"
                f"{_removal_failure(BrowserState.current_profile_dir)})"
            )
    for _iid in closed:
        _ATTACHED_BROWSERS.discard(_iid)
    if failures:
        return err(
            f"closed {len(closed)} instance(s): {closed}; "
            f"{len(failures)} failure(s): {' | '.join(failures)}"
        )
    return ok(f"closed {len(closed)} instance(s): {closed}")


# ══════════════════════════════════════════════════════════════════════════
# 24. CHROME PROFILE INTEGRATION (list / clone existing profiles)
# ══════════════════════════════════════════════════════════════════════════
#
# Let user start from their existing Chrome profile (with all logins, cookies,
# extensions) instead of a fresh one. Three patterns:
#
#   1. list_chrome_profiles()                           — detect what's on system
#   2. clone_chrome_profile(source, instance_id)        — safe: copy to isolated dir
#   3. spawn_browser(profile_dir=<chrome path>, ...)    — direct: uses profile as-is
#                                                         (requires Chrome desktop closed)


@mcp.tool()
async def list_chrome_profiles(browser: Optional[str] = None) -> str:
    """List profiles across EVERY Chrome-family browser on this system.

    Chrome, Chromium, Edge, Brave, Vivaldi, Opera and Arc are all surveyed —
    people commonly keep their real logged-in sessions in Brave or Edge while a
    near-empty Chrome sits alongside, and reporting only the first browser found
    would hide exactly the profiles worth cloning.

    Each profile reports a `cookies_kb` figure: a profile with kilobytes of
    cookies has real sessions, one with ~0 has never been used for anything.

    Args:
        browser: restrict to one browser id (e.g. "brave", "chrome", "edge").
            Omit to survey them all.

    Reads each browser's 'Local State' JSON read-only — nothing is modified.
    """
    roots = chrome_user_data_roots()
    if browser:
        wanted = browser.strip().lower()
        roots = [(n, p) for n, p in roots if n == wanted]
        if not roots:
            available = ", ".join(n for n, _ in chrome_user_data_roots()) or "none"
            return err(f"no user-data root for browser {browser!r}. Found: {available}")
    if not roots:
        return err(
            "No Chrome-family browser profile directory found. "
            "Install Chrome/Chromium/Edge/Brave and launch it once to create profiles."
        )

    out: list[dict] = []
    browsers: list[dict] = []
    parse_errors: list[str] = []
    for bname, root in roots:
        try:
            data = json.loads((root / "Local State").read_text())
        except Exception as e:
            parse_errors.append(f"{bname}: {e}")
            continue
        info_cache = data.get("profile", {}).get("info_cache", {})
        profiles_order = list(data.get("profile", {}).get("profiles_order", []))
        root_running = is_chrome_profile_locked(root)
        seen = set(profiles_order)
        # Ensure we also include profiles not in profiles_order
        for k in info_cache.keys():
            if k not in seen:
                profiles_order.append(k)
        count_before = len(out)
        for name in profiles_order:
            info = info_cache.get(name, {})
            pdir = root / name
            cookie_candidates = (pdir / "Cookies", pdir / "Network" / "Cookies")
            try:
                cookies_kb = max(
                    (path.stat().st_size // 1024 for path in cookie_candidates
                     if path.exists()),
                    default=0,
                )
            except OSError:
                cookies_kb = 0
            out.append({
                "browser": bname,
                "profile_dir_name": name,
                "display_name": info.get("name", name),
                "email": info.get("user_name", ""),
                "path": str(pdir),
                "exists": pdir.exists(),
                "cookies_kb": cookies_kb,
                # Chromium locks the user-data root, not each Default/Profile N
                # child. Checking pdir always reported false while Chrome ran.
                "in_use": root_running if pdir.exists() else False,
                "last_active_time": info.get("last_active_time", 0),
            })
        browsers.append({
            "browser": bname,
            "root": str(root),
            "running": root_running,
            "profile_count": len(out) - count_before,
        })
    # Richest sessions first — that is what someone scanning this list wants.
    out.sort(key=lambda p: p.get("cookies_kb", 0), reverse=True)
    payload = {
        "browsers": browsers,
        "profile_count": len(out),
        "profiles": out,
        "usage_hint": (
            "clone_chrome_profile(browser='brave', source_profile='Default', "
            "target_instance_id='my_clone') → then spawn_browser(instance_id='my_clone'). "
            "Pick the profile with the most cookies_kb; close its browser first, "
            "or explicitly use allow_running=true for a verified live snapshot."
        ),
    }
    if parse_errors:
        payload["parse_errors"] = parse_errors
    return ok(json.dumps(payload, indent=2, default=str))


def _is_sqlite(path: Path) -> bool:
    """True when the file carries the SQLite header magic."""
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except Exception:
        return False


def _sqlite_snapshot(src: Path, dst: Path) -> bool:
    """Copy a SQLite file the browser may be writing, then verify the copy.

    SQLite's online-backup API looks like the right tool and is not: on a
    database a live browser keeps writing, `Connection.backup()` blocks inside C
    code that never yields to Python, so the call cannot even be interrupted by a
    timeout — measured here as an indefinite hang against a running Brave. A
    plain byte copy returns instantly, and copying the `-wal`/`-shm` companions
    alongside is what lets SQLite reconstruct a coherent view from it.

    The copy is integrity-checked afterwards; False means treat it as suspect
    rather than silently trusting it. The source is never opened for writing.
    """
    import shutil as _shutil
    import sqlite3
    try:
        _shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            sidecar = src.with_name(src.name + suffix)
            if sidecar.exists():
                _shutil.copy2(sidecar, dst.with_name(dst.name + suffix))
    except Exception:
        return False
    try:
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=3)
        try:
            row = conn.execute("pragma quick_check").fetchone()
            return bool(row and row[0] == "ok")
        finally:
            conn.close()
    except Exception:
        return False


@mcp.tool()
async def clone_chrome_profile(
    source_profile: str = "Default",
    target_instance_id: str = "chrome_clone",
    skip_cache: bool = True,
    overwrite: bool = False,
    browser: Optional[str] = None,
    allow_running: bool = False,
) -> str:
    """Clone a profile from ANY Chrome-family browser into an isolated MCP location.

    SAFE: reads the source profile without modification, copies to
    ~/.mcp-stealth/profiles/<target_instance_id>/Default/

    The source browser MUST be closed (we check SingletonLock).
    Preserves: cookies, history, bookmarks, saved passwords, extensions state.
    Skips (if skip_cache=True): Cache, Code Cache, GPUCache, Media Cache,
    Service Worker, IndexedDB (regenerable, saves 500MB+).

    Args:
        source_profile: profile dir name ("Default", "Profile 1", ...).
                        Use list_chrome_profiles() to see options.
        target_instance_id: Name for the cloned instance (becomes folder name).
        skip_cache: Exclude cache dirs for fast + smaller copy (default True).
        overwrite: Delete target if exists before copying (default False).
        allow_running: clone even while the browser is open. Each SQLite
            database is copied with its -wal/-shm companions and then
            integrity-checked; any that fail come back under `sqlite_suspect`
            instead of being trusted silently. Without this flag the call refuses
            while the profile is locked.
        browser: which browser to clone FROM — "brave", "chrome", "edge",
            "chromium", "vivaldi", "opera", "arc". Omit to use the first root
            found, which is NOT necessarily the one holding your sessions:
            an empty Chrome often shadows the Brave/Edge profile you actually
            log in with. Pass this explicitly when in doubt.

    After clone, launch with:
        spawn_browser(instance_id='<target_instance_id>')
    """
    import shutil

    try:
        _validate_instance_id(target_instance_id)
    except ValueError as e:
        return err(str(e))

    root = chrome_user_data_root(browser)
    if root is None:
        found = ", ".join(n for n, _ in chrome_user_data_roots()) or "none"
        if browser:
            return err(f"no user-data root for browser {browser!r}. Found: {found}")
        return err("No Chrome-family profile root found. Install Chrome/Brave/Edge first.")
    try:
        source_path = _direct_child(root, source_profile, "source_profile")
    except ValueError as e:
        return err(str(e))
    if not source_path.exists():
        return err(
            f"Source profile not found: {source_path}\n"
            f"Run list_chrome_profiles() to see available profiles "
            f"(pass browser= to pick a different browser)."
        )
    running = is_chrome_profile_locked(source_path) or is_chrome_profile_locked(root)
    if running and not allow_running:
        label = browser or root.name
        return err(
            f"{label} is currently using this profile (lock file present).\n"
            f"Either close it FULLY (Cmd+Q on macOS, not just the window), or pass "
            f"allow_running=true — every SQLite database (cookies, logins, history) "
            f"is copied with its WAL companions and integrity-checked. Suspect "
            f"copies are reported explicitly. The source profile is only ever "
            f"opened read-only.\n"
            f"Lock: {source_path / 'SingletonLock'}"
        )

    ensure_dirs()
    target_root = PROFILES_ROOT / target_instance_id
    target_default = target_root / "Default"

    if target_root.exists():
        if not overwrite:
            return err(
                f"Target instance already exists: {target_root}\n"
                f"Pass overwrite=true to replace, or use different target_instance_id."
            )
        # overwrite means "replace a stale copy", never "delete a browser out from under
        # whoever is using it", in this session or another.
        foreign = BrowserState.foreign_instance(target_instance_id)
        if foreign:
            return err(f"not overwriting: {foreign}")
        from .state import find_chrome_pids_by_profile
        holder = chrome_lock_holder_pid(target_root)
        running = find_chrome_pids_by_profile(target_root)
        # No process listing on Windows, so nothing there can show the profile is free.
        if holder is not None or running or is_chrome_profile_locked(target_root) or sys.platform == "win32":
            return err(
                f"not overwriting {target_root}: it is in use "
                f"(lock holder {holder}, Chrome PIDs {running}). Close that browser first."
            )
        try:
            shutil.rmtree(target_root)
        except Exception as e:
            return err(f"failed to remove existing target: {e}")

    target_default.mkdir(parents=True, exist_ok=True)

    # Cache-like directories to skip (regenerable, big, Chrome rebuilds them)
    cache_dirs = {
        "cache", "code cache", "gpucache", "dawnwebgpucache", "dawngraphitecache",
        "graphitedawncache", "grshadercache", "media cache", "service worker",
        "indexeddb", "file system", "downloadedupdates", "downloads",
        "safe browsing", "componentupdater", "extensions_crx_cache",
        "component_crx_cache", "gpupersistentcache", "shared dictionary",
    }
    # Files that might cause issues if copied (locks, logs)
    skip_files = {
        "singletonlock", "singletoncookie", "singletonsocket",
        "lock", "lockfile",
    }

    copied_count = 0
    skipped_cache_bytes = 0
    errors: list[str] = []
    snapshotted: list[str] = []
    suspect_dbs: list[str] = []

    for item in source_path.iterdir():
        name_lower = item.name.lower()
        try:
            if item.is_file():
                if skip_cache and name_lower in skip_files:
                    continue
                # Also skip -journal WAL sidecars
                if name_lower.endswith("-journal"):
                    continue
                # Cookies/Login Data/History are live SQLite. A plain byte copy of
                # a database the browser is still writing can land mid-checkpoint
                # and produce a torn file, so snapshot those through SQLite itself.
                if _is_sqlite(item):
                    if _sqlite_snapshot(item, target_default / item.name):
                        snapshotted.append(item.name)
                    else:
                        suspect_dbs.append(item.name)
                    copied_count += 1
                    continue
                shutil.copy2(item, target_default / item.name)
                copied_count += 1
            elif item.is_dir():
                if skip_cache and name_lower in cache_dirs:
                    try:
                        skipped_cache_bytes += sum(
                            f.stat().st_size for f in item.rglob("*") if f.is_file()
                        )
                    except Exception:
                        pass
                    continue
                shutil.copytree(
                    item, target_default / item.name,
                    dirs_exist_ok=True,
                    ignore_dangling_symlinks=True,
                )
                copied_count += 1
        except Exception as e:
            errors.append(f"{item.name}: {type(e).__name__}")
            continue

    # Copy Local State (shared across profiles, needed for Chrome to recognize profile)
    local_state_src = root / "Local State"
    if local_state_src.exists():
        try:
            shutil.copy2(local_state_src, target_root / "Local State")
        except Exception as e:
            errors.append(f"Local State: {e}")

    # Compute target size
    try:
        total_size = sum(f.stat().st_size for f in target_root.rglob("*") if f.is_file())
    except Exception:
        total_size = 0

    result = {
        "source_browser": browser or root.name,
        "source_running": running,
        "sqlite_verified": snapshotted,
        "sqlite_suspect": suspect_dbs,
        "source": str(source_path),
        "target": str(target_root),
        "copied_items": copied_count,
        "target_size_mb": round(total_size / 1024 / 1024, 1),
        "cache_skipped_mb": round(skipped_cache_bytes / 1024 / 1024, 1),
        "errors": errors[:10],  # cap error list
        "verification_scope": (
            "Top-level SQLite files are integrity-checked. Databases nested in "
            "subdirectories are copied without a check, and a live source is a "
            "structural copy, not an atomic point-in-time snapshot — a "
            "transaction committed mid-copy can be missing."
        ),
    }
    # A clone with a corrupt cookie jar looks identical to a good one until a
    # login silently fails hours later, so a suspect database is an error here
    # rather than a field somebody has to notice. The copy is left in place to
    # be inspected; naming it as the next step would endorse it.
    if suspect_dbs or errors:
        result["usable"] = False
        result["recovery"] = (
            "Re-run with the source browser CLOSED for a clean copy, or delete "
            f"{target_root} and retry. The partial copy was kept for inspection."
        )
        return err(json.dumps(result, indent=2, default=str))
    result["usable"] = True
    result["next_step"] = (
        f"spawn_browser(instance_id='{target_instance_id}', "
        f"url='https://example.com', headless=False)"
    )
    return ok(json.dumps(result, indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════
# 24. DEVTOOLS & TESTING — moved to tools/devtools.py (v0.4.1 refactor)
# ══════════════════════════════════════════════════════════════════════════
from .tools import devtools as _devtools  # noqa: F401  -- registers tools with mcp


# ══════════════════════════════════════════════════════════════════════════
# 27. SELF-SUFFICIENT WEB-RESEARCH KIT — tools/research.py
# ══════════════════════════════════════════════════════════════════════════
# web_search / scrape_markdown / smart_fetch / deep_research / search_page.
# HTTP-first (curl_cffi, browserless) with stealth-browser fallback — aims to
# replace firecrawl/tavily and most of exa for everyday research.
from .tools import research as _research  # noqa: F401  -- registers tools with mcp
from .tools import profiles as _profiles  # noqa: F401  -- registers tools with mcp


# ══════════════════════════════════════════════════════════════════════════
# 25. LLM-OPTIMIZED ACTION KIT
# ══════════════════════════════════════════════════════════════════════════
#
# Tools designed for AI-agent workflows: token-efficient page summaries,
# label-fuzzy form filling, NL vision targeting, verification primitives,
# state-diff debugging, resumable workflows, and one-shot detect+bypass.

# ── describe_page ──────────────────────────────────────────────────────────

_DESCRIBE_PAGE_JS = """
(() => {
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    return true;
  };
  // Best-effort label resolution: <label for=ID>, wrapping <label>, aria-label,
  // aria-labelledby, placeholder, name attribute. Returns first non-empty.
  const labelOf = (el) => {
    if (!el) return '';
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    if (el.getAttribute) {
      const al = el.getAttribute('aria-label');
      if (al) return al.trim();
      const alb = el.getAttribute('aria-labelledby');
      if (alb) {
        const ref = document.getElementById(alb);
        if (ref && ref.innerText) return ref.innerText.trim();
      }
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      const nm = el.getAttribute('name');
      if (nm) return nm.trim();
    }
    return '';
  };
  const fieldType = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select') return 'select';
    if (tag !== 'input') return tag;
    const t = (el.type || 'text').toLowerCase();
    return t;
  };
  // Form fields
  const fields = [];
  const inputs = [...document.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden');
  for (const el of inputs.slice(0, 60)) {
    const type = fieldType(el);
    let value;
    if (type === 'checkbox' || type === 'radio') value = !!el.checked;
    else if (type === 'select') value = el.value;
    else if (el.type === 'password') value = el.value ? '(password set, not shown)' : '';
    else value = el.value || '';
    fields.push({
      label: labelOf(el),
      type,
      required: !!el.required,
      value: typeof value === 'string' ? value.slice(0, 200) : value,
      name: el.name || null,
      id: el.id || null,
    });
  }
  // Buttons + submit-like actions
  const actions = [];
  const btns = [...document.querySelectorAll(
    'button, [role=button], input[type=submit], input[type=button], a[href][role=button]'
  )].filter(visible);
  for (const el of btns.slice(0, 30)) {
    const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (txt) actions.push({
      text: txt.slice(0, 80),
      kind: el.tagName.toLowerCase(),
      disabled: !!el.disabled,
    });
  }
  // Headings (page intent signal)
  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .filter(visible).slice(0, 10)
    .map(h => ({level: h.tagName.toLowerCase(), text: h.innerText.trim().slice(0, 120)}));
  // Visible error messages — common patterns
  const errors = [];
  const errorEls = [...document.querySelectorAll(
    '[role=alert], .error, .alert-error, .field-error, [class*="error" i]:not(button), [aria-invalid=true]'
  )].filter(visible).slice(0, 10);
  for (const el of errorEls) {
    const t = (el.innerText || '').trim();
    if (t && t.length < 300) errors.push(t);
  }
  // Top-level navigation links (max 12, same-origin only)
  const origin = location.origin;
  const navLinks = [];
  const navContainer = document.querySelector('nav, header, [role=navigation]');
  if (navContainer) {
    for (const a of navContainer.querySelectorAll('a[href]')) {
      if (!visible(a)) continue;
      const t = (a.innerText || '').trim();
      if (!t) continue;
      try {
        const u = new URL(a.href, origin);
        if (u.origin === origin) navLinks.push(t.slice(0, 60));
      } catch {}
      if (navLinks.length >= 12) break;
    }
  }
  return JSON.stringify({
    title: document.title || '',
    url: location.href,
    headings,
    fields,
    actions,
    errors,
    navigation: [...new Set(navLinks)],
  });
})()
"""


_WAIT_DOM_STABLE_JS = """
((max_ms, stable_ms) => new Promise((resolve) => {
  const start = Date.now();
  let last = start;
  let obs;
  try {
    obs = new MutationObserver(() => { last = Date.now(); });
    obs.observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, characterData: true,
    });
  } catch (e) { resolve('observer_failed'); return; }
  const tick = () => {
    const now = Date.now();
    if (now - last >= stable_ms) { obs.disconnect(); resolve('stable'); }
    else if (now - start >= max_ms) { obs.disconnect(); resolve('timeout'); }
    else setTimeout(tick, 80);
  };
  setTimeout(tick, 80);
}))
"""


@mcp.tool()
async def describe_page(
    wait_stable: bool = False,
    max_wait: float = 2.5,
    stable_ms: int = 400,
) -> str:
    """Compact AI-friendly page summary — replaces accessibility_snapshot
    for LLM workflows. Returns JSON with the page's intent + interactable
    surface in ~10× fewer tokens than a full a11y dump.

    Args:
        wait_stable: if True, install a MutationObserver and wait until the
            DOM has been quiet for `stable_ms` before snapshotting (max
            `max_wait` seconds). Use on SPA / lazy-rendered pages so the
            LLM sees the final state, not a half-hydrated render. Cheap
            (~50-200ms typical, capped at max_wait).
        max_wait: outer cap for stability wait (default 2.5s)
        stable_ms: required quiet window in ms (default 400)

    Output shape:
      {
        "title": "...",
        "url": "...",
        "headings": [{"level":"h1","text":"..."}],
        "fields": [{"label":"...","type":"text|email|...","required":bool,"value":"...","name":"...","id":"..."}],
        "actions": [{"text":"Submit","kind":"button","disabled":false}],
        "errors": ["..."],
        "navigation": ["Dashboard","Settings",...],
        "stability": "stable|timeout|skipped"  (only present if wait_stable=True)
      }

    Use this BEFORE smart_fill so the LLM knows which labels exist.
    """
    try:
        tab = BrowserState.active_tab()
        stability = "skipped"
        if wait_stable:
            try:
                stab_raw = await _wait(
                    tab.evaluate(
                        f"{_WAIT_DOM_STABLE_JS}({int(max_wait * 1000)}, {int(stable_ms)})",
                        await_promise=True, return_by_value=True,
                    ),
                    timeout=max_wait + 2.0,
                    what="describe_page wait_stable",
                )
                stability = str(stab_raw.value if hasattr(stab_raw, "value")
                                 and not isinstance(stab_raw, str) else stab_raw) or "stable"
            except Exception:
                stability = "wait_failed"
        raw = await _wait(
            tab.evaluate(_DESCRIBE_PAGE_JS, return_by_value=True),
            what="describe_page",
        )
        text = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        data = parse_json(str(text), {})
        if not isinstance(data, dict):
            return err("describe_page: failed to parse page summary")
        if wait_stable:
            data["stability"] = stability
        return ok(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── smart_fill ─────────────────────────────────────────────────────────────

_SMART_FILL_FIND_JS = """
(label_query) => {
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const labelOf = (el) => {
    if (!el) return '';
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    const alb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (alb) {
      const ref = document.getElementById(alb);
      if (ref && ref.innerText) return ref.innerText.trim();
    }
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph) return ph.trim();
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return nm.trim();
    return '';
  };
  const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const want = norm(label_query);
  // Score candidates: exact > prefix > substring > token-overlap
  const inputs = [...document.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden' && !el.disabled && !el.readOnly);
  const scored = [];
  const candidates = [];
  for (const el of inputs) {
    const lbl = labelOf(el);
    const lblN = norm(lbl);
    candidates.push(lbl);
    if (!lblN) continue;
    let score = 0;
    if (lblN === want) score = 1000;
    else if (lblN.startsWith(want)) score = 700;
    else if (lblN.includes(want)) score = 500;
    else {
      const tw = want.split(' ').filter(Boolean);
      const lw = lblN.split(' ').filter(Boolean);
      const overlap = tw.filter(t => lw.includes(t)).length;
      if (overlap > 0) score = 100 + overlap * 50;
    }
    if (score > 0) {
      scored.push({score, label: lbl, el, type: el.tagName.toLowerCase(),
        input_type: (el.type || '').toLowerCase()});
    }
  }
  scored.sort((a, b) => b.score - a.score);
  const top = scored[0] || null;
  let best = null;
  if (top) {
    // Measure only the winner, after bringing it on screen: a point outside the
    // viewport would send the mouse click nowhere.
    top.el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    const rect = top.el.getBoundingClientRect();
    best = {score: top.score, label: top.label, type: top.type, input_type: top.input_type,
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2)};
  }
  return {json: JSON.stringify({best, candidates: [...new Set(candidates)].filter(Boolean).slice(0, 30)}),
          el: top ? top.el : null};
}
"""


@mcp.tool()
async def smart_fill(fields: dict, submit_label: Optional[str] = None) -> str:
    """Fill form fields by label text (fuzzy match). LLM-friendly alt to
    fill_form which requires DOM refs.

    Args:
        fields: {"Label": "value", ...} — keys match form field labels
            (case-insensitive, fuzzy: exact > prefix > substring > token).
            Labels resolved from <label>, aria-label, placeholder, name.
        submit_label: optional button text to click after filling
            (e.g., "Create", "Sign in"). Fuzzy-matched on action button text.

    Behavior:
        - Each field: locates input → focus → clear → type value
        - Returns per-field result + list of available labels if missing
        - On miss: error includes candidates so the LLM can retry with
          the correct label name.
    """
    try:
        tab = BrowserState.active_tab()
        if not isinstance(fields, dict):
            return err("smart_fill: fields must be a dict {label: value}")
        if not fields:
            return err("smart_fill: no fields given")
        results: list[dict] = []
        for label, value in fields.items():
            if not isinstance(label, str):
                continue
            group = new_object_group()
            parts = await _wait(
                evaluate_parts(tab, f"({_SMART_FILL_FIND_JS})({json.dumps(label)})", group),
                what=f"smart_fill find '{label}'",
            )
            data = parse_json(parts["json"].value if "json" in parts else "", {})
            best = data.get("best") if isinstance(data, dict) else None
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            if not best:
                results.append({
                    "label": label, "ok": False,
                    "error": "no field matched",
                    "did_you_mean": candidates[:10],
                })
                await release_group(tab, group)
                continue
            # Click into the field then type. Using mouse_click + JS fill is
            # more reliable than .focus() across React/Vue components that
            # listen for native events.
            try:
                # Not for a <select>: clicking one opens a native popup that stays open and
                # takes the next click, and its value is set directly below.
                if best.get("type") != "select":
                    await _dispatch_input(tab, tab.mouse_click(int(best["x"]), int(best["y"])))
                    await asyncio.sleep(0.1)
                # Native value setter so React/Vue see the change. Each element type has its
                # own: HTMLInputElement's throws on a <select> or <textarea>.
                setter_js = f"""
                function() {{
                  const wanted = {json.dumps(str(value))};
                  if (this.tagName === 'SELECT') {{
                    const options = [...this.options];
                    const lower = wanted.trim().toLowerCase();
                    const option = options.find(o => o.value === wanted)
                      || options.find(o => o.text.trim().toLowerCase() === lower);
                    if (!option) return this.value;
                    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')
                      .set.call(this, option.value);
                    this.dispatchEvent(new Event('input', {{bubbles: true}}));
                    this.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return this.value === option.value ? wanted : this.value;
                  }}
                  const proto = this.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                  // A setter ignores maxlength, which typing cannot exceed.
                  const text = this.maxLength >= 0 ? wanted.slice(0, this.maxLength) : wanted;
                  Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, text);
                  this.dispatchEvent(new Event('input', {{bubbles: true}}));
                  this.dispatchEvent(new Event('change', {{bubbles: true}}));
                  return this.value;
                }}
                """
                remote, exc = await _wait(tab.send(cdp_runtime.call_function_on(
                    setter_js, object_id=parts["el"].object_id, return_by_value=True,
                )), what=f"smart_fill set '{label}'")
                if exc is not None:
                    raise RuntimeError(exc.text or "value setter threw")
                actual = remote.value
                if str(actual) != str(value):
                    kind, _ = await _wait(tab.send(cdp_runtime.call_function_on(
                        "function() { return this.type; }", object_id=parts["el"].object_id,
                        return_by_value=True,
                    )), what=f"smart_fill read '{label}'")
                    secret = kind.value == "password" or is_secret_name(label)
                    reads = f"{len(str(actual))} chars" if secret else repr(str(actual)[:80])
                    results.append({
                        "label": label, "matched": best["label"], "ok": False,
                        "error": f"field reads {reads} after setting it",
                    })
                    continue
                results.append({"label": label, "matched": best["label"], "ok": True})
            except Exception as e:
                results.append({"label": label, "ok": False, "error": str(e)})
            finally:
                await release_group(tab, group)
        any_failed = any(not r.get("ok") for r in results)
        # Optional submit
        submit_result = None
        if submit_label and any_failed:
            # Submitting a half-filled form is an action the caller did not ask for.
            submit_result = "NOT clicked: a field above failed"
        elif submit_label:
            try:
                # Reuse click_text fuzzy logic
                r = str(await click_text(submit_label, exact=False))
                submit_result = r[:200]
                any_failed = r.startswith("Error:")
            except Exception as e:
                submit_result = f"submit failed: {e}"
                any_failed = True
        out = {"results": results}
        if submit_result is not None:
            out["submit"] = submit_result
        return (err if any_failed else ok)(json.dumps(out, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── assert_* primitives ────────────────────────────────────────────────────


@mcp.tool()
async def assert_text_present(text: str, timeout: float = 5.0) -> str:
    """Verify text appears anywhere on page within timeout.
    Returns ok(found) or err(not found + sample of body text)."""
    try:
        tab = BrowserState.active_tab()
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_body = ""
        while asyncio.get_event_loop().time() < deadline:
            raw = await _wait(tab.evaluate(
                f"(() => {{ const t = (document.body && document.body.innerText) || ''; "
                f"return JSON.stringify({{found: t.includes({json.dumps(text)}), sample: t.slice(0, 500)}}); }})()",
                return_by_value=True,
            ), what="assert_text_present")
            txt = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
            data = parse_json(str(txt), {})
            if isinstance(data, dict):
                if data.get("found"):
                    return ok(f"text present: {text!r}")
                last_body = data.get("sample", "")
            await asyncio.sleep(0.4)
        return err(
            f"text not found within {timeout}s: {text!r}\nbody sample: {last_body[:300]}"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def assert_url_matches(pattern: str, timeout: float = 5.0) -> str:
    """Verify current URL matches regex within timeout.
    Returns ok(current_url) or err(timeout + last_url)."""
    import re as _re
    try:
        tab = BrowserState.active_tab()
        try:
            rx = _re.compile(pattern)
        except _re.error as e:
            return err(f"invalid regex: {e}")
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_url = ""
        while asyncio.get_event_loop().time() < deadline:
            last_url = await get_url(tab)
            if rx.search(last_url):
                return ok(f"url matches {pattern!r}: {last_url}")
            await asyncio.sleep(0.3)
        return err(f"url did not match {pattern!r} within {timeout}s. last: {last_url}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def assert_element_visible(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    timeout: float = 5.0,
) -> str:
    """Verify an element is visible (rendered, non-zero size, not hidden).
    Pass selector OR text — text uses fuzzy contains match.
    Returns ok(rect) or err(timeout)."""
    if not selector and not text:
        return err("pass selector= or text=")
    try:
        tab = BrowserState.active_tab()
        if selector:
            # An invalid selector can never become visible; waiting would misreport it.
            await _eval_checked(tab, f"document.querySelector({json.dumps(selector)}) && 1")
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_state = "not found"
        while asyncio.get_event_loop().time() < deadline:
            if selector:
                expr = f"""
                (() => {{
                  const el = document.querySelector({json.dumps(selector)});
                  if (!el) return JSON.stringify({{state: 'not_found'}});
                  const r = el.getBoundingClientRect();
                  const cs = getComputedStyle(el);
                  const visible = r.width > 0 && r.height > 0 &&
                    cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
                  return JSON.stringify({{state: visible ? 'visible' : 'hidden',
                    rect: {{x: Math.round(r.left), y: Math.round(r.top),
                            width: Math.round(r.width), height: Math.round(r.height)}}}});
                }})()
                """
            else:
                expr = f"""
                (() => {{
                  const want = {json.dumps(text)}.toLowerCase();
                  const candidates = [...document.querySelectorAll('button, a, [role=button], h1,h2,h3, span, div, label, p')];
                  for (const el of candidates) {{
                    const t = (el.innerText || '').toLowerCase().trim();
                    if (!t.includes(want)) continue;
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    const visible = r.width > 0 && r.height > 0 &&
                      cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
                    if (visible) return JSON.stringify({{state: 'visible', tag: el.tagName.toLowerCase(),
                      rect: {{x: Math.round(r.left), y: Math.round(r.top),
                              width: Math.round(r.width), height: Math.round(r.height)}}}});
                  }}
                  return JSON.stringify({{state: 'not_found'}});
                }})()
                """
            raw = await _wait(tab.evaluate(expr, return_by_value=True),
                              what="assert_element_visible")
            txt = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
            data = parse_json(str(txt), {})
            if isinstance(data, dict):
                last_state = data.get("state", "not found")
                if last_state == "visible":
                    return ok(json.dumps(data, ensure_ascii=False))
            await asyncio.sleep(0.3)
        return err(f"element not visible within {timeout}s (last_state={last_state})")
    except Exception as e:
        return err(str(e))


# ── storage_diff ───────────────────────────────────────────────────────────

# In-memory snapshots keyed by user-supplied name. Lives only for current
# MCP session (not persisted across launches — use storage_state_save for that).
_STORAGE_SNAPSHOTS: dict[Any, dict] = {}


def _snapshot_key(name: str) -> Any:
    """Names are per session: diffing against another session's snapshot would blame the
    action under test for the difference between two browsers. A tuple, because a joined
    string lets the primary session name a snapshot "session-2::x" and reach session-2's.
    """
    key = BrowserState.session_key()
    return name if key == PRIMARY_SESSION else (key, name)


def _forget_session_snapshots(session: str) -> None:
    for name in list(_STORAGE_SNAPSHOTS):
        owned = isinstance(name, str) if session == PRIMARY_SESSION else (
            isinstance(name, tuple) and name[0] == session)
        if owned:
            _STORAGE_SNAPSHOTS.pop(name, None)


SESSION_GONE_HOOKS.append(_forget_session_snapshots)


def _own_snapshot_names() -> list[str]:
    key = BrowserState.session_key()
    if key == PRIMARY_SESSION:
        return [n for n in _STORAGE_SNAPSHOTS if isinstance(n, str)]
    return [n[1] for n in _STORAGE_SNAPSHOTS if isinstance(n, tuple) and n[0] == key]


async def _take_storage_snapshot(tab) -> dict:
    """Capture cookies + localStorage + sessionStorage + url + title.

    Every area records whether it was actually MEASURED. An unreadable area used
    to fall back to its empty default, so a snapshot that failed to read cookies
    was indistinguishable from a page that genuinely had none — and storage_diff
    would then report every real cookie as "removed". Failures are collected in
    `errors` and the area is marked unmeasured instead of being invented.
    """
    snap: dict = {"url": "", "title": "", "cookies": [],
                   "localStorage": {}, "sessionStorage": {},
                   "errors": {}, "measured": {
                       "url": False, "cookies": False,
                       "localStorage": False, "sessionStorage": False,
                   }}
    try:
        snap["url"] = await get_url(tab, strict=True)
        snap["title"] = await get_title(tab, strict=True)
        snap["measured"]["url"] = True
    except Exception as e:
        snap["errors"]["url"] = f"{type(e).__name__}: {e}"
    try:
        from nodriver.cdp import network as cdp_network
        cookies = await _wait(tab.send(cdp_network.get_cookies()),
                              what="storage_snapshot cookies")
        snap["cookies"] = [{
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", ""),
            "domain": getattr(c, "domain", ""),
            "path": getattr(c, "path", "/"),
        } for c in (cookies or [])]
        snap["measured"]["cookies"] = True
    except Exception as e:
        snap["errors"]["cookies"] = f"{type(e).__name__}: {e}"
    for area in ("localStorage", "sessionStorage"):
        try:
            raw = await _wait(tab.evaluate(
                f"JSON.stringify(Object.fromEntries(Object.entries({area})))",
                return_by_value=True,
            ), what=f"storage_snapshot {area}")
            value = str(raw.value if hasattr(raw, "value") else raw)
            parsed = parse_json(value, None)
            if not isinstance(parsed, dict):
                # Storage is often blocked outright (file://, sandboxed frames,
                # third-party cookie policy). That is a real answer, not "empty".
                raise ValueError(f"unparseable {area} payload")
            snap[area] = parsed
            snap["measured"][area] = True
        except Exception as e:
            snap["errors"][area] = f"{type(e).__name__}: {e}"
    return snap


@mcp.tool()
async def storage_snapshot(name: str = "default") -> str:
    """Capture cookies + localStorage + sessionStorage + URL into a named slot
    for later diffing. Use BEFORE an action you want to inspect.

    Areas that could not be read are reported as unmeasured rather than empty:
    a snapshot that silently failed to read cookies would make storage_diff
    announce every existing cookie as "removed" by the action under test.
    """
    try:
        tab = BrowserState.active_tab()
        snap = await _take_storage_snapshot(tab)
        _STORAGE_SNAPSHOTS[_snapshot_key(name)] = snap
        if len(_STORAGE_SNAPSHOTS) > _STORAGE_SNAPSHOT_LIMIT:
            for stale in list(_STORAGE_SNAPSHOTS)[:-_STORAGE_SNAPSHOT_LIMIT]:
                _STORAGE_SNAPSHOTS.pop(stale, None)

        def _area(label: str, count: int) -> str:
            return f"{count} {label}" if snap["measured"].get(
                "cookies" if label == "cookies" else label) else f"{label} UNMEASURED"

        parts = [
            _area("cookies", len(snap["cookies"])),
            _area("localStorage", len(snap["localStorage"])),
            _area("sessionStorage", len(snap["sessionStorage"])),
        ]
        line = f"snapshot '{name}': {', '.join(parts)} at {snap['url'] or '(url unmeasured)'}"
        if snap["errors"]:
            return ok(line + "\nUNMEASURED areas (values below are NOT zero, they "
                             "are unknown): " + json.dumps(snap["errors"]))
        return ok(line)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def storage_diff(name: str = "default", reveal_values: bool = False) -> str:
    """Compare current state vs an earlier storage_snapshot. Returns JSON
    showing what changed (added/removed/modified per area + url change).

    Diffing across a login is exactly when new values are session tokens, and this
    output lands in the transcript. So values read "*** (N chars)" unless
    reveal_values=True; whether something changed is reported either way.

    Pattern:
      storage_snapshot("before")
      <do an action e.g. click login>
      storage_diff("before")  → returns what the action actually changed
    """
    try:
        if _snapshot_key(name) not in _STORAGE_SNAPSHOTS:
            return err(
                f"no snapshot named {name!r}. Available: "
                f"{_own_snapshot_names() or '(none)'}"
            )
        tab = BrowserState.active_tab()
        before = _STORAGE_SNAPSHOTS[_snapshot_key(name)]
        after = await _take_storage_snapshot(tab)

        def _shown(value: Any, cap: int = 200) -> Any:
            if reveal_values:
                return value[:cap] if isinstance(value, str) else value
            return f"*** ({len(str(value or ''))} chars)"

        def _kvdiff(b: dict, a: dict) -> dict:
            keys_b, keys_a = set(b), set(a)
            return {
                "added": {k: _shown(a[k]) for k in keys_a - keys_b},
                "removed": list(keys_b - keys_a),
                "modified": {k: {"before": _shown(b[k]), "after": _shown(a[k])}
                              for k in keys_b & keys_a if b[k] != a[k]},
            }

        # Cookies — by (name, domain) tuple
        # Path is part of a cookie's identity: SID at /admin and SID at / are two cookies,
        # and keying without it let one overwrite the other, hiding a change.
        b_cookies = {(c["name"], c["domain"], c.get("path", "/")): c["value"]
                     for c in before["cookies"]}
        a_cookies = {(c["name"], c["domain"], c.get("path", "/")): c["value"]
                     for c in after["cookies"]}
        cookie_diff = {
            "added": [{"name": n, "domain": d, "path": p, "value": _shown(a_cookies[(n, d, p)], 60)}
                       for (n, d, p) in set(a_cookies) - set(b_cookies)],
            "removed": [{"name": n, "domain": d, "path": p}
                        for (n, d, p) in set(b_cookies) - set(a_cookies)],
            "modified": [{"name": n, "domain": d, "path": p}
                          for (n, d, p) in set(b_cookies) & set(a_cookies)
                          if b_cookies[(n, d, p)] != a_cookies[(n, d, p)]],
        }

        diff = {
            "url_changed": (before["url"] != after["url"]),
            "url_before": before["url"],
            "url_after": after["url"],
            "title_changed": (before["title"] != after["title"]),
            "cookies": cookie_diff,
            "localStorage": _kvdiff(before["localStorage"], after["localStorage"]),
            "sessionStorage": _kvdiff(before["sessionStorage"], after["sessionStorage"]),
        }
        # An area that could not be read on either side produces a diff that
        # LOOKS decisive and is not: unreadable cookies before + readable after
        # renders every existing cookie as "added" by the action under test.
        # Name those areas so the numbers are not taken at face value.
        unreliable = sorted({
            area
            for side in (before, after)
            for area, was_measured in (side.get("measured") or {}).items()
            if not was_measured
        })
        if unreliable:
            diff["unreliable_areas"] = unreliable
            diff["warning"] = (
                "These areas were NOT measured on at least one side, so their "
                "diff is meaningless — differences shown for them reflect a "
                "failed read, not a change made by the action."
            )
            diff["measurement_errors"] = {
                "before": before.get("errors") or {},
                "after": after.get("errors") or {},
            }
        return ok(json.dumps(diff, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── workflow_run (resumable) ───────────────────────────────────────────────

# Map of tool-name → function ref for workflow_run dispatch. Only a curated
# subset is exposed. batch_actions and workflow_run share it, so a step type means
# the same thing in both.
_WORKFLOW_TOOLS: dict[str, Any] = {}


def _register_workflow_tools() -> None:
    """Late-bound registration so all tool defs above this point exist.
    Called lazily on first workflow_run call."""
    global _WORKFLOW_TOOLS
    if _WORKFLOW_TOOLS:
        return
    g = globals()
    registered = mcp._tool_manager._tools
    for name in (
        "navigate", "reload", "go_back", "go_forward",
        "click", "click_text", "click_role", "fill", "type_text", "press_key",
        "select_option", "check", "uncheck",
        "wait_for", "wait_for_navigation", "wait_for_url", "wait_for_response",
        "screenshot", "scroll", "scroll_to",
        "smart_fill", "vision_locate",
        "assert_text_present", "assert_url_matches", "assert_element_visible",
        "storage_snapshot", "storage_diff",
        "cookie_import", "storage_state_load",
        "evaluate", "mouse_click_xy",
        # Readers: a later step can branch on or iterate over what these return.
        "get_text", "get_attribute", "get_url", "get_links", "get_html",
        "query_selector_all", "extract_table", "extract_structured",
        "browser_snapshot", "describe_page", "search_page",
        "hover", "paste_text", "fill_form", "wait_for_function", "wait_for_change",
        "wait_for_network_idle",
    ):
        # The registered tool first: a tool's Python name can differ from its public one,
        # and globals() then yields something else. get_url is the helper needing a tab.
        if name in registered:
            _WORKFLOW_TOOLS[name] = registered[name].fn
        elif name in g and callable(g[name]):
            fn = g[name]
            _WORKFLOW_TOOLS[name] = getattr(fn, "fn", fn) if hasattr(fn, "fn") else fn


@mcp.tool()
async def workflow_run(
    steps: list[dict],
    start_at: int | str = 0,
    stop_on_error: bool = True,
    vars: Optional[dict] = None,
) -> str:
    """Run many tool steps in one call, with branching, loops and earlier results.

    A tool step: {"tool": "fill", "args": {...}, "save_as": "x", "label": "...",
    "continue_on_error": false}

    Using earlier results: save_as stores a step's result, parsed as JSON when it
    is JSON. Any string in args may then reference it as ${x}, ${x.field} or
    ${x[0].name}; a string that is exactly one reference keeps its type. ${last.ok},
    ${last.value} and ${last.text} describe the previous step. A reference to
    something undefined fails the step and lists what is defined.

    Loop: {"foreach": "${rows}", "as": "row", "steps": [...]} sets ${row} and
    ${row_index} for each item.

    Branch: {"if": <condition>, "then": [...], "else": [...]}. Conditions are
    {"value": "${x}", "<op>": y} with op one of equals, not_equals, contains,
    not_contains, matches (regex), gt, gte, lt, lte, truthy, falsy; or
    {"exists": "${x.y}"}, {"all": [...]}, {"any": [...]}, {"not": {...}}. There is no
    expression language and nothing is evaluated as code on the server.

    Example, submitting a form once per row of data taken from the page:
      [{"tool": "evaluate", "args": {"expression": "JSON.stringify(window.rows)"},
        "save_as": "rows"},
       {"foreach": "${rows}", "as": "row", "steps": [
         {"tool": "fill", "args": {"selector": "#name", "value": "${row.name}"}},
         {"if": {"value": "${row.active}", "truthy": true}, "then": [
           {"tool": "check", "args": {"selector": "#active"}}]},
         {"tool": "click", "args": {"selector": "#save"}}]}]

    Args:
        steps: the steps above
        start_at: where to begin. A top-level index, or a dotted path from a failure
            report: "3.2.1" is step 3, item 2 of its loop, inner step 1.
        stop_on_error: stop at the first failure (default). If False, keep going and
            report every result.
        vars: starting variables. Pass the `vars` of a failure report when resuming,
            since nothing is kept between calls.

    Returns JSON with completed (capped at 200 entries, the rest counted), total and
    vars. On failure also failed_at, failed_path, failure_context and resume_with.
    With stop_on_error=False, failed_count and failed_paths list every failure even
    past that cap. Steps the failed step depended on are not re-run on resume, so check
    that any state they left on the page still holds. A start_at that does not exist
    is refused.

    Tools: navigate, reload, go_back/forward, click, click_text, click_role, fill,
    fill_form, type_text, paste_text, press_key, select_option, check, uncheck,
    hover, scroll, scroll_to, mouse_click_xy, smart_fill, wait_for*, screenshot,
    evaluate, get_text, get_attribute, get_url, get_links, get_html, search_page,
    query_selector_all, extract_table, extract_structured, browser_snapshot,
    describe_page, vision_locate, assert_*, storage_*, cookie_import,
    storage_state_load.
    """
    _register_workflow_tools()
    steps = parse_json(steps, None) if isinstance(steps, str) else steps
    if not isinstance(steps, list):
        return err("steps must be a list of dicts")

    async def dispatch(tool: str, args: dict) -> Any:
        return await _WORKFLOW_TOOLS[tool](**args)

    try:
        result = await run_workflow(
            steps, dispatch, lambda name: name in _WORKFLOW_TOOLS,
            start_at=start_at, stop_on_error=stop_on_error, variables=vars,
        )
    except WorkflowError as e:
        return err(str(e))
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    return err(text) if "failed_at" in result else ok(text)


# ── detect_and_bypass ──────────────────────────────────────────────────────


@mcp.tool()
async def detect_and_bypass() -> str:
    """One-shot: detect anti-bot wall on current page and apply the best
    bypass we have. Returns JSON with detection + bypass result.

    Bypass routing:
      - Cloudflare Turnstile / interstitial → _auto_verify_cf (DOM + OpenCV)
      - Other walls (DataDome, PerimeterX, Akamai, Imperva, Kasada) →
        return detection + recommended-action list (no auto-bypass since
        those need session reuse / proxies / paid solvers).
      - No wall detected → returns ok with empty bypass.
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        tab = BrowserState.active_tab()
        # Reuse detect_anti_bot's classification
        det_str = await detect_anti_bot()
        det_text = str(det_str)
        # Strip the "ok: " prefix if present
        if det_text.startswith("Error:"):
            return det_text  # propagate
        # Detect specific systems by inspecting the response text — cheaper
        # than re-running the JS probes.
        det_lower = det_text.lower()
        result: dict = {"detection": det_text[:1000], "bypassed": False, "method": None,
                         "next_steps": []}
        if "cloudflare" in det_lower:
            # Try our automated CF Turnstile flow.
            verify = await _auto_verify_cf(tab, max_attempts=2)
            if verify:
                result["method"] = "auto_verify_cf"
                result["details"] = verify.strip()
                # The page decides. Every outcome of _auto_verify_cf, including "no clickable
                # widget found", says auto-verify, and a click attempt is not a solved wall.
                still = await _cf_challenge_state(tab, active=True)
                result["challenge_still_present"] = still  # None: could not be checked
                result["bypassed"] = still is False and "no clickable widget" not in verify
            if not result["bypassed"]:
                result["next_steps"].append(
                    "If still blocked: solve_captcha(kind='turnstile', ...) with CAPSOLVER_KEY, "
                    "or storage_state_load a previously-saved session."
                )
        if "datadome" in det_lower:
            result["next_steps"].append(
                "DataDome: no automated bypass. Use mouse_record→mouse_replay of a real session, "
                "session_warmup, residential proxy, and storage_state reuse."
            )
        # Match only "perimeterx" — detect_anti_bot always labels this vendor
        # "PerimeterX/HUMAN", so the bare "human" token only added DataDome
        # false positives (its message mentions "a real session" → "human"-free,
        # but other detections containing the substring would mis-fire).
        if "perimeterx" in det_lower:
            result["next_steps"].append(
                "PerimeterX/HUMAN: storage_state_load is most reliable. New sessions need "
                "mobile proxy + humanize_click."
            )
        if "akamai" in det_lower:
            result["next_steps"].append(
                "Akamai: requires _abck cookie sensor data — solve_captcha(kind='akamai', ...) "
                "if your provider supports it, or session reuse."
            )
        if "imperva" in det_lower or "kasada" in det_lower:
            result["next_steps"].append(
                "Imperva/Kasada: paid solver via solve_captcha or session reuse."
            )
        if "(no anti-bot system detected)" in det_lower or "none detected" in det_lower:
            return ok(json.dumps(
                # Nothing was bypassed, because nothing was in the way.
                {"detection": "no anti-bot detected", "bypassed": False, "needed": False,
                 "method": "n/a", "next_steps": []},
                indent=2, ensure_ascii=False,
            ))
        return ok(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 26. NETWORK + AUTH + WAIT — paste, click_and_wait, auth_capture,
#     http_request_with_session, wait_for_request, form_introspect
# ══════════════════════════════════════════════════════════════════════════
#
# Closes gaps surfaced by real-world automation: response body retrieval
# on captured requests, modern-framework paste-event handlers, browser
# session bridge for authenticated API calls, and click outcome
# disambiguation.


# ── paste_text ─────────────────────────────────────────────────────────────


@mcp.tool()
async def paste_text(
    text: str,
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    submit: bool = False,
) -> str:
    """Set field value by simulating a real paste event sequence.

    Use when fill / type_text don't register on modern frameworks (SolidJS
    runes, Svelte 5 runes, some Qwik forms) that ONLY listen for paste
    events or beforeinput with inputType:'insertFromPaste'.

    Sequence dispatched (mimics a real Cmd-V):
      1. focus
      2. ClipboardEvent('paste', {clipboardData: 'text/plain': text})
      3. InputEvent('beforeinput', {inputType: 'insertFromPaste', data: text})
      4. native value setter (HTMLInputElement / HTMLTextAreaElement)
      5. InputEvent('input', {inputType: 'insertFromPaste', data: text})
      6. Event('change')

    Args:
        text: value to paste
        ref: element ref from browser_snapshot
        selector: CSS selector
        submit: if True, simulate Enter keypress after paste
    """
    try:
        tab = BrowserState.active_tab()
        if ref:
            el = await resolve_ref(ref)
        elif selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err("paste failed: element not found")
        else:
            return err("paste_text: pass ref= or selector=")

        # Single-shot JS that does the full event dance — keeps timing tight.
        result_raw = await _wait(el.apply(
            f"""
            function() {{
              const el = this;
              const text = {json.dumps(text)};
              const noText = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'color', 'range'];
              const editable = el.isContentEditable;
              const isField = el.tagName === 'TEXTAREA'
                || (el.tagName === 'INPUT' && !noText.includes(el.type));
              if (!isField && !editable) return JSON.stringify({{ok: false,
                error: '<' + el.tagName.toLowerCase() + '> does not take text'}});
              if (el.disabled) return JSON.stringify({{ok: false, error: 'the field is disabled'}});
              if (el.readOnly) return JSON.stringify({{ok: false, error: 'the field is read-only'}});
              try {{ el.focus(); }} catch (e) {{}}
              // 1. Paste event with DataTransfer
              try {{
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const pasteEv = new ClipboardEvent('paste', {{
                  clipboardData: dt, bubbles: true, cancelable: true,
                }});
                el.dispatchEvent(pasteEv);
              }} catch (e) {{}}
              // 2. beforeinput
              try {{
                const beforeEv = new InputEvent('beforeinput', {{
                  inputType: 'insertFromPaste', data: text,
                  bubbles: true, cancelable: true,
                }});
                el.dispatchEvent(beforeEv);
              }} catch (e) {{}}
              // 3. Native value setter so React/Vue/Solid see the change
              if (editable) {{
                // A contenteditable has no value to set: the text goes in at the selection.
                const range = document.createRange();
                range.selectNodeContents(el);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                if (!document.execCommand('insertText', false, text)) el.textContent = text;
              }} else try {{
                const proto = el.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, text);
              }} catch (e) {{ el.value = text; }}
              // 4. input event
              try {{
                el.dispatchEvent(new InputEvent('input', {{
                  inputType: 'insertFromPaste', data: text, bubbles: true,
                }}));
              }} catch (e) {{
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
              }}
              // 5. change event for blur-validators
              el.dispatchEvent(new Event('change', {{bubbles: true}}));
              const landed = editable ? el.textContent : el.value;
              const unify = (v) => v.split('\\r\\n').join('\\n').split('\\r').join('\\n');
              if (unify(landed) !== unify(text)) {{
                return JSON.stringify({{ok: false, error: 'the field reads ' + landed.length
                  + ' chars after the paste, not the ' + text.length + ' sent (mask, maxlength or framework state)'}});
              }}
              return JSON.stringify({{ok: true, value:
                el.type === 'password' ? `(password, ${{landed.length}} chars)` : landed}});
            }}
            """,
        ), what="paste_text")
        if isinstance(result_raw, cdp_runtime.ExceptionDetails):
            return err(f"paste failed: {_js_exception_text(result_raw)}")
        result_text = result_raw.value if hasattr(result_raw, "value") and not isinstance(result_raw, str) else result_raw
        data = parse_json(str(result_text), {})
        if not isinstance(data, dict) or not data.get("ok"):
            return err(f"paste failed: {data.get('error') if isinstance(data, dict) else result_text}")
        if submit:
            try:
                await _dispatch_input(tab, _dispatch_key(tab, key_event_params("Enter")))
            except Exception:
                with contextlib.suppress(Exception):
                    await el.apply("function() { this.form?.requestSubmit?.(); }")
        return ok(f"pasted {len(text)} chars; field value={str(data.get('value', ''))[:80]}")
    except Exception as e:
        return err(str(e))


# ── auth_capture ───────────────────────────────────────────────────────────


@mcp.tool()
async def auth_capture(
    filter_url_pattern: str,
    count: int = 1,
    timeout: float = 10.0,
    include_response_headers: bool = False,
    reveal_values: bool = False,
) -> str:
    """Intercept the next N requests matching a URL pattern and return
    their headers (Authorization, X-CSRF-*, etc.) — useful for
    SPAs that hold bearer tokens in JS memory and never write them to
    localStorage. Their values are hidden unless reveal_values=True, because
    tool output lands in the transcript. After network_start,
    http_request_with_session reuses a token seen on the same origin without
    showing it.

    Pattern: substring match on URL (case-sensitive). For regex use
    network_get instead.

    Args:
        filter_url_pattern: e.g. "/api/" or "graphql"
        count: stop capturing after this many matches (default 1)
        timeout: max seconds to wait (default 10)
        include_response_headers: also wait for + return response headers
        reveal_values: return credential values as they are

    Returns JSON array of {url, method, request_headers, request_body,
    [response_headers, status]}.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        try:
            await tab.send(cdp_network.enable())
        except Exception:
            pass
        captured: list[dict] = []
        pending_response: dict[str, dict] = {}
        done = asyncio.Event()
        active = {"flag": True}

        async def on_req(event):
            if not active["flag"]:
                return
            try:
                req = event.request
                url = getattr(req, "url", "")
                if filter_url_pattern not in url:
                    return
                rid = str(event.request_id)
                entry = {
                    "url": url,
                    "method": getattr(req, "method", ""),
                    "request_headers": dict(getattr(req, "headers", None) or {}),
                    "request_body": getattr(req, "post_data", None),
                }
                if include_response_headers:
                    pending_response[rid] = entry
                else:
                    captured.append(entry)
                    if len(captured) >= count:
                        active["flag"] = False
                        done.set()
            except Exception:
                pass

        async def on_res(event):
            if not active["flag"]:
                return
            try:
                rid = str(event.request_id)
                if rid not in pending_response:
                    return
                entry = pending_response.pop(rid)
                resp = event.response
                entry["status"] = getattr(resp, "status", None)
                entry["response_headers"] = dict(getattr(resp, "headers", None) or {})
                captured.append(entry)
                if len(captured) >= count:
                    active["flag"] = False
                    done.set()
            except Exception:
                pass

        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        if include_response_headers:
            tab.add_handler(cdp_network.ResponseReceived, on_res)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            active["flag"] = False
            # Unregister our own closures (filter the list; nodriver's
            # remove_handler would drop the whole event's handlers).
            for _evt, _fn in ((cdp_network.RequestWillBeSent, on_req),
                              (cdp_network.ResponseReceived, on_res)):
                try:
                    tab.handlers[_evt].remove(_fn)
                except (KeyError, ValueError):
                    pass
        if not captured:
            return err(
                f"auth_capture: no requests matched {filter_url_pattern!r} within {timeout}s"
            )
        if not reveal_values:
            captured = [mask_request_entry(c) for c in captured]
        return ok(json.dumps(captured, indent=2, default=str)[:30000])
    except Exception as e:
        return err(str(e))


# ── http_request_with_session ──────────────────────────────────────────────


def _latest_auth_header_for(url: str) -> Optional[str]:
    """Pick the most recent Authorization header from network_index for requests to the
    target's exact origin. Used by http_request_with_session as a fallback when the
    caller doesn't pass headers explicitly.

    Origin, not host: a token seen on https://x must not travel to http://x:8080 in
    plaintext, or to another service on that host."""
    try:
        from urllib.parse import urlparse

        def _origin(address: str) -> tuple:
            parts = urlparse(address)
            port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
            return parts.scheme, parts.hostname or "", port

        target = _origin(url)
        if not target[1]:
            return None
        # Iterate in insertion order — index is dict keyed by request_id and
        # Python dicts preserve insertion order, so the last matching entry
        # is the freshest.
        latest = None
        for entry in BrowserState.network_index.values():
            if _origin(entry.get("url", "")) != target:
                continue
            headers = entry.get("request_headers") or {}
            for k, v in headers.items():
                if k.lower() == "authorization" and v:
                    latest = str(v)
                    break
        return latest
    except Exception:
        return None


@mcp.tool()
async def http_request_with_session(
    url: str,
    method: str = "GET",
    json_body: Optional[dict] = None,
    data: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    auth_header: Optional[str] = None,
    impersonate: str = "chrome",
    return_mode: str = "auto",
    timeout: float = 30.0,
    wait_timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    reveal_values: bool = False,
) -> str:
    """Authenticated HTTP request that piggybacks on the BROWSER's session.

    Combines:
      - cookies from active tab (use_browser_cookies=True in http_request)
      - Authorization header — explicit auth_header, OR auto-extracted
        from network_index (the most recent same-host request captured
        via network_start). Pages that hold bearer tokens in JS memory
        only become reachable after navigate / interaction emits a
        request — call network_start once at session begin.

    Args:
        url, method, json_body, data, impersonate, return_mode, timeout,
        wait_timeout, connect_timeout: same as http_request — including the
            tracked lifecycle, so a slow authenticated call hands back a
            request_id for http_request_status instead of a dead end
        extra_headers: merged on top of auto-detected ones
        auth_header: explicit bearer / basic value, e.g. "Bearer eyJ..."

    Returns same shape as http_request.
    """
    headers = dict(extra_headers or {})
    if auth_header:
        headers.setdefault("Authorization", auth_header)
    elif not any(k.lower() == "authorization" for k in headers):
        # Case-fold BOTH sides. Comparing "Authorization" against a lowercased
        # key set never matched, so a caller who passed their own Authorization
        # in extra_headers had it silently overwritten by the sniffed token.
        candidate = _latest_auth_header_for(url)
        if candidate:
            headers["Authorization"] = candidate
    if not headers and not BrowserState.network_index:
        # Soft warning — still useful for cookie-only auth
        pass
    return await http_request(
        url=url, method=method, impersonate=impersonate,
        use_browser_cookies=True, headers=headers,
        data=data, json_body=json_body, timeout=timeout,
        return_mode=return_mode, wait_timeout=wait_timeout,
        connect_timeout=connect_timeout, reveal_values=reveal_values,
    )


# ── wait_for_request ───────────────────────────────────────────────────────


async def _arm_request_watch(
    tab: Any, url_pattern: str, method: Optional[str], require_response: bool,
) -> dict:
    """Subscribe for a matching request NOW. Returns {match, pending, done, disarm}.

    Separate from the waiting so click_and_wait can subscribe before it clicks: a fast API
    call completes before anything subscribed afterwards can see it. Every matching request
    is tracked, because the first one to get a response is the answer, and it need not be
    the last one to have started. Raises when Network cannot be enabled, since no event
    would ever arrive and the wait would read as "nothing matched".
    """
    from nodriver.cdp import network as cdp_network

    await tab.send(cdp_network.enable())
    match: dict = {}
    pending: dict[str, dict] = {}
    done = asyncio.Event()

    async def on_req(event):
        if done.is_set():
            return
        try:
            req = event.request
            url = getattr(req, "url", "")
            if url_pattern not in url:
                return
            if method and getattr(req, "method", "").upper() != method.upper():
                return
            rid = str(event.request_id)
            pending[rid] = {
                "request_id": rid,
                "url": url,
                "method": getattr(req, "method", ""),
                "request_headers": dict(getattr(req, "headers", None) or {}),
                "request_body": getattr(req, "post_data", None),
            }
            if not require_response:
                match.update(pending[rid])
                done.set()
        except Exception:
            pass

    async def on_res(event):
        if done.is_set():
            return
        try:
            rid = str(event.request_id)
            if rid not in pending:
                return
            resp = event.response
            match.update(pending[rid])
            match["status"] = getattr(resp, "status", None)
            match["response_headers"] = dict(getattr(resp, "headers", None) or {})
            match["mime"] = getattr(resp, "mime_type", None)
            done.set()
        except Exception:
            pass

    handlers = [(cdp_network.RequestWillBeSent, on_req)]
    if require_response:
        handlers.append((cdp_network.ResponseReceived, on_res))
    _add_all_handlers(tab, handlers)
    return {"match": match, "pending": pending, "done": done,
            "disarm": lambda: _drop_handlers(tab, handlers)}


def _add_all_handlers(tab: Any, handlers: list) -> None:
    """All or none: a caller that gets an exception has no disarm to clean up with."""
    try:
        for event_type, handler in handlers:
            tab.add_handler(event_type, handler)
    except Exception:
        _drop_handlers(tab, handlers)
        raise


async def _await_request_watch(
    watch: dict, url_pattern: str, method: Optional[str], timeout: float,
    reveal_values: bool = False,
) -> str:
    try:
        await asyncio.wait_for(watch["done"].wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pending = watch["pending"]
        what = (f"{len(pending)} request(s) matched {url_pattern!r} but none got a response"
                if pending else f"no request matched {url_pattern!r}")
        return err(what + (f" method={method}" if method else "") + f" within {timeout}s")
    match = watch["match"] if reveal_values else mask_request_entry(watch["match"])
    return ok(json.dumps(match, indent=2, default=str))


@mcp.tool()
async def wait_for_request(
    url_pattern: str,
    method: Optional[str] = None,
    timeout: float = 15.0,
    require_response: bool = True,
    reveal_values: bool = False,
) -> str:
    """Block until a network request matching url_pattern is observed.
    Replaces the setTimeout(2000)+poll anti-pattern.

    Args:
        url_pattern: substring match
        method: optional HTTP verb filter (GET/POST/...)
        timeout: max seconds to wait
        require_response: also wait for the response phase (default True)
        reveal_values: return credential headers and fields as they are, not "*** (N chars)"

    Returns JSON of the matching entry (url/method/status/request_headers/
    response_headers).
    """
    try:
        tab = BrowserState.active_tab()
        watch = await _arm_request_watch(tab, url_pattern, method, require_response)
        try:
            return await _await_request_watch(watch, url_pattern, method, timeout, reveal_values)
        finally:
            watch["disarm"]()
    except Exception as e:
        return err(str(e))


# ── in-flight tracking ─────────────────────────────────────────────────────

# Types that stay open by design: waiting on them would mean no page using one ever idles.
_NEVER_FINISHED_TYPES = ("EventSource", "WebSocket")


async def _arm_inflight_tracker(tab: Any) -> tuple[dict, Any]:
    """Count this tab's requests in flight from CDP events. Returns (tracker, disarm).

    Sees every kind of request, loading images and scripts included, and writes nothing to
    the page. CDP does not list what is already running, so a request started before arming
    is noticed only through its later response, data or finish events, which count as
    activity; one that stays silent throughout cannot be seen. Raises when Network cannot be
    enabled, because silence would then look like idle.
    """
    from nodriver.cdp import network as cdp_network

    await tab.send(cdp_network.enable())
    tracker: dict[str, Any] = {"inflight": {}, "seen": 0, "last_change": time.monotonic()}

    async def on_request(event: Any) -> None:
        if _cdp_resource_type(event) in _NEVER_FINISHED_TYPES:
            return
        tracker["inflight"][str(event.request_id)] = str(getattr(event.request, "url", ""))
        tracker["seen"] += 1
        tracker["last_change"] = time.monotonic()

    async def on_activity(_event: Any) -> None:
        tracker["last_change"] = time.monotonic()

    async def on_finished(event: Any) -> None:
        tracker["inflight"].pop(str(event.request_id), None)
        tracker["last_change"] = time.monotonic()

    handlers = [
        (cdp_network.RequestWillBeSent, on_request),
        (cdp_network.ResponseReceived, on_activity),
        (cdp_network.DataReceived, on_activity),
        (cdp_network.LoadingFinished, on_finished),
        (cdp_network.LoadingFailed, on_finished),
    ]
    _add_all_handlers(tab, handlers)
    return tracker, lambda: _drop_handlers(tab, handlers)


def _inflight_quiet(tracker: dict, idle_s: float) -> bool:
    return (not tracker["inflight"]
            and time.monotonic() - tracker["last_change"] >= idle_s)


# ── click_and_wait ─────────────────────────────────────────────────────────


@mcp.tool()
async def click_and_wait(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    expect: str = "auto",
    expect_url_pattern: Optional[str] = None,
    expect_text: Optional[str] = None,
    expect_selector: Optional[str] = None,
    expect_request_pattern: Optional[str] = None,
    timeout: float = 8.0,
) -> str:
    """Click + wait for the side-effect to land. Distinguishes a
    successful action from a silent failure (e.g. form invalid where
    click() returns success but submit never happened).

    Args:
        ref / selector / text: element to click (passed through to existing
            click tools — text uses click_text fuzzy match)
        expect: what to wait for after the click. One of:
            "navigation"     — URL changes
            "url"            — URL matches expect_url_pattern (regex)
            "text"           — page contains expect_text
            "selector"       — expect_selector becomes visible
            "request"        — outgoing request matches expect_request_pattern
            "network_idle"   — no in-flight requests for 500ms
            "auto"           — try navigation→network_idle→nothing
                           Passing one expect_* selects its mode instead.
        expect_*: target for the matching expect mode
        timeout: per-mode max wait

    Returns JSON {clicked, observed: {what, evidence}, elapsed_ms}.
    """
    import time as _time
    if not (ref or selector or text):
        return err("click_and_wait: pass ref=, selector=, or text=")
    # Before the click. An error reply after it invites the caller to fix the argument and
    # retry, and that retry submits twice.
    if expect not in ("auto", "navigation", "url", "text", "selector", "request", "network_idle"):
        return err(f"click_and_wait: unknown expect={expect!r}; nothing was clicked")
    if expect == "auto":
        # A target with no matching mode used to be ignored, so the call succeeded without
        # ever looking for it.
        given = [mode for mode, target in (("url", expect_url_pattern), ("text", expect_text),
                                           ("selector", expect_selector),
                                           ("request", expect_request_pattern)) if target]
        if len(given) > 1:
            return err(f"click_and_wait: pass one expect_* target, got {', '.join(given)}; "
                       f"nothing was clicked")
        if given:
            expect = given[0]
    required = {"url": ("expect_url_pattern", expect_url_pattern),
                "text": ("expect_text", expect_text),
                "selector": ("expect_selector", expect_selector),
                "request": ("expect_request_pattern", expect_request_pattern)}
    if expect in required and not required[expect][1]:
        return err(f"expect={expect!r} requires {required[expect][0]}=; nothing was clicked")
    request_watch = None
    disarm_inflight = None
    try:
        tab = BrowserState.active_tab()
        url_before = await get_url(tab, strict=True)
        # Also before the click: whatever it triggers can finish before the call returns.
        if expect == "request":
            request_watch = await _arm_request_watch(tab, expect_request_pattern, None, True)
        inflight = None
        if expect in ("auto", "network_idle"):
            try:
                inflight, disarm_inflight = await _arm_inflight_tracker(tab)
            except Exception as exc:
                if expect == "network_idle":
                    return err(f"network_idle cannot be measured ({_exc_text(exc)}); "
                               f"nothing was clicked")
        t0 = _time.monotonic()
        # Dispatch the click — reuse existing tools
        if text:
            click_res = await click_text(text, exact=False)
        else:
            click_res = await click(ref=ref, selector=selector)
        click_str = str(click_res)
        if click_str.startswith("Error:"):
            return err(f"click failed: {click_str}")

        observed: dict = {"what": None, "evidence": None}
        modes_to_try = [expect] if expect != "auto" else ["navigation", "network_idle"]

        for mode in modes_to_try:
            try:
                if mode == "navigation":
                    deadline = _time.monotonic() + timeout
                    while _time.monotonic() < deadline:
                        url_now = await get_url(tab)
                        # Blank means the read failed mid-navigation, not a new URL.
                        if url_now and url_now != url_before:
                            observed = {"what": "navigation",
                                          "evidence": {"from": url_before, "to": url_now}}
                            break
                        await asyncio.sleep(0.2)
                elif mode == "url":
                    res = await assert_url_matches(expect_url_pattern, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "url", "evidence": str(res)[:200]}
                elif mode == "text":
                    res = await assert_text_present(expect_text, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "text", "evidence": str(res)[:200]}
                elif mode == "selector":
                    res = await assert_element_visible(selector=expect_selector, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "selector", "evidence": str(res)[:200]}
                elif mode == "request":
                    res = await _await_request_watch(
                        request_watch, expect_request_pattern, None, timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "request", "evidence": str(res)[:300]}
                elif mode == "network_idle":
                    # From real request and finish events, armed before the click. The log
                    # length used here stays flat with capture off, requests in flight or not.
                    if inflight is None:
                        observed["network_idle"] = "not measured: Network could not be enabled"
                        continue
                    deadline = _time.monotonic() + timeout
                    while _time.monotonic() < deadline:
                        if _inflight_quiet(inflight, 0.5):
                            observed = {"what": "network_idle",
                                        "evidence": f"{inflight['seen']} request(s) since the "
                                                    f"click, none in flight for 500ms"}
                            break
                        await asyncio.sleep(0.05)
                    if not observed.get("what"):
                        observed["network_idle"] = (
                            f"never idle: {len(inflight['inflight'])} request(s) still in flight")
            except Exception as e:
                observed["error"] = str(e)
            if observed.get("what"):
                break

        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if not observed.get("what") and expect != "auto":
            return err(json.dumps({
                "clicked": click_str[:200],
                "observed": "no matching expect signal",
                **({"detail": observed["network_idle"]} if "network_idle" in observed else {}),
                "elapsed_ms": elapsed_ms,
                "hint": "the click registered but no side-effect was detected — "
                        "form may be invalid; check describe_page errors[]",
            }, indent=2))
        return ok(json.dumps({
            "clicked": click_str[:200],
            "observed": observed,
            "elapsed_ms": elapsed_ms,
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))
    finally:
        if request_watch is not None:
            request_watch["disarm"]()
        if disarm_inflight is not None:
            disarm_inflight()


# ── form_introspect ────────────────────────────────────────────────────────


_FORM_INTROSPECT_JS = """
((selector) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const labelOf = (el) => {
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    if (el.getAttribute) {
      const al = el.getAttribute('aria-label');
      if (al) return al.trim();
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      const nm = el.getAttribute('name');
      if (nm) return nm.trim();
    }
    return '';
  };
  const detectFramework = (el) => {
    const out = [];
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber') || k.startsWith('__reactProps')) out.push('react');
      if (k.startsWith('__vnode') || k === '__vue__') out.push('vue');
      if (k.startsWith('$$') || k.startsWith('$0')) out.push('solid_or_svelte');
      if (k.startsWith('lit-')) out.push('lit');
    }
    return [...new Set(out)];
  };
  const root = selector ? document.querySelector(selector) : document;
  if (!root) return JSON.stringify({error: 'form not found'});
  const inputs = [...root.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden');
  const fields = inputs.map(el => {
    const tag = el.tagName.toLowerCase();
    return {
      label: labelOf(el),
      tag,
      type: tag === 'input' ? (el.type || 'text') : tag,
      name: el.name || null,
      id: el.id || null,
      value: el.type === 'password'
        ? (el.value ? '(password set, not shown)' : '')
        : (typeof el.value === 'string' ? el.value.slice(0, 200) : el.value),
      required: !!el.required,
      disabled: !!el.disabled,
      readonly: !!el.readOnly,
      pattern: el.pattern || null,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      minlength: el.minLength > 0 ? el.minLength : null,
      validation_message: el.validity ? (el.validationMessage || null) : null,
      valid: el.validity ? el.validity.valid : null,
      aria_invalid: el.getAttribute('aria-invalid') === 'true',
      aria_describedby: el.getAttribute('aria-describedby') || null,
      framework: detectFramework(el),
    };
  });
  // Submit / reset buttons inside the form
  const buttons = [...root.querySelectorAll(
    'button, input[type=submit], input[type=reset], [role=button]'
  )].filter(visible).map(el => ({
    text: (el.innerText || el.value || '').trim().slice(0, 80),
    type: el.type || 'button',
    disabled: !!el.disabled,
  }));
  // Form-level metadata
  const formEl = root.tagName === 'FORM' ? root : root.querySelector('form');
  const meta = formEl ? {
    action: formEl.getAttribute('action') || '',
    method: formEl.getAttribute('method') || 'get',
    enctype: formEl.enctype || '',
    novalidate: !!formEl.noValidate,
  } : null;
  return JSON.stringify({fields, buttons, meta});
})
"""


@mcp.tool()
async def form_introspect(form_selector: Optional[str] = None) -> str:
    """Detailed form analysis in a single call. Returns label,
    framework binding (react/vue/solid_or_svelte/lit), validation state,
    and constraints (pattern, min/max length, required) per field.

    Args:
        form_selector: CSS selector for a specific form (default: scan
            whole document for visible inputs)
    """
    try:
        tab = BrowserState.active_tab()
        sel = json.dumps(form_selector) if form_selector else "null"
        raw = await _wait(tab.evaluate(
            f"({_FORM_INTROSPECT_JS})({sel})", return_by_value=True,
        ), what="form_introspect")
        text = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        data = parse_json(str(text), {})
        if isinstance(data, dict) and data.get("error"):
            return err(data["error"])
        return ok(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# ATTACH MODE — connect to existing Chrome via CDP (no new browser launch)
# ══════════════════════════════════════════════════════════════════════════
#
# Requires the target Chrome to be started with `--remote-debugging-port=<N>`.
# macOS shorthand:
#   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
#     --remote-debugging-port=9222 --user-data-dir=/path/to/your/profile
#
# Then `attach_to_chrome(port=9222)` connects without spawning a new browser
# and respects the existing tabs/cookies/profile (including Profile 21, etc).
# `detach()` releases the connection but leaves Chrome running.

# Tracks whether current BrowserState was created via attach (True) or
# spawned by us (False). detach() is a no-op for spawned browsers.
_ATTACHED_BROWSERS: set[str] = set()


def _chrome_flags_from_args(cmd: str) -> tuple[Optional[int], Optional[str]]:
    """Debugging port and profile from a command line. `ps` drops the quoting, so a value runs
    to the next flag: the default profile on macOS sits under "Application Support"."""
    port = None
    found = re.search(r"--remote-debugging-port=(\d+)", cmd)
    if found:
        port = int(found.group(1))
    found = re.search(r"--user-data-dir=(.+?)(?=\s--|$)", cmd)
    udd = found.group(1).strip() if found else None
    if port == 0 and udd:  # Chrome chose the port itself and wrote it here.
        with contextlib.suppress(OSError, ValueError, IndexError):
            port = int((Path(udd) / "DevToolsActivePort").read_text().split()[0])
    return port, udd


@mcp.tool()
async def list_external_chrome() -> str:
    """List Chrome processes running on this machine, with their CDP debugging
    port if any. Use before attach_to_chrome to find a target. Returns array
    of {pid, debugging_port, user_data_dir, cmd}. Chromes without a debugging
    port show port=null and cannot be attached unless restarted with
    --remote-debugging-port=<N>."""
    import subprocess
    import sys
    out: list[dict] = []
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["wmic", "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=3,
            )
            lines = (r.stdout or "").splitlines()
        except Exception:
            return ok(json.dumps([]))
        for line in lines:
            if "chrome.exe" not in line:
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            cmd = parts[1] if len(parts) > 1 else ""
            pid_s = parts[-1].strip()
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            port = None
            udd = None
            for tok in cmd.split():
                if tok.startswith("--remote-debugging-port="):
                    try: port = int(tok.split("=", 1)[1])
                    except ValueError: pass
                elif tok.startswith("--user-data-dir="):
                    udd = tok.split("=", 1)[1]
            out.append({"pid": pid, "debugging_port": port,
                        "user_data_dir": udd, "cmd": cmd[:200]})
    else:
        # POSIX: ps + parse args
        try:
            r = subprocess.run(
                ["ps", "axo", "pid,args"], capture_output=True, text=True, timeout=3,
            )
            lines = (r.stdout or "").splitlines()
        except Exception:
            return ok(json.dumps([]))
        seen_pids: set[int] = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Pick parent Chrome processes — heuristic: has --user-data-dir
            # or is named "Google Chrome" / "chromium" (not helper renderers).
            lower = line.lower()
            is_chrome = (
                "google chrome" in lower or "chromium" in lower or
                "/chrome " in lower or lower.endswith("/chrome")
            )
            if not is_chrome:
                continue
            # Skip helper subprocs (renderers, GPU process, etc) — they run
            # under "Helper" in macOS or have --type=renderer.
            if "--type=" in lower or "Helper" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            cmd = parts[1]
            port, udd = _chrome_flags_from_args(cmd)
            out.append({"pid": pid, "debugging_port": port,
                        "user_data_dir": udd, "cmd": cmd[:200]})
            if len(out) >= 30:
                break
    # Sort: ones with debugging port first
    out.sort(key=lambda x: (x.get("debugging_port") is None, x.get("pid", 0)))
    return ok(json.dumps(out, indent=2))


async def _detect_chrome_debugging_port() -> Optional[int]:
    """Find the lowest debugging port from running Chromes (returns None if none)."""
    import subprocess
    try:
        r = subprocess.run(["ps", "axo", "args"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    ports: list[int] = []
    for line in (r.stdout or "").splitlines():
        for tok in line.split():
            if tok.startswith("--remote-debugging-port="):
                try:
                    ports.append(int(tok.split("=", 1)[1]))
                except ValueError:
                    pass
    return min(ports) if ports else None


@mcp.tool()
async def attach_to_chrome(
    port: Optional[int] = None,
    host: str = "127.0.0.1",
    instance_id: str = "attached",
) -> str:
    """Attach to an existing Chrome instance via CDP — no new browser launch,
    no profile lock conflict. Target Chrome must have been started with
    `--remote-debugging-port=<N>`. Auto-detects port if omitted (picks lowest).

    Use cases:
    - Control your existing Chrome (e.g. Profile 21) without closing it.
    - Drive a Chrome session you launched manually with custom flags.
    - Connect to a remote/Docker Chrome via host=<remote> port=<N>.

    To detach without closing Chrome, call detach(). Calling browser_close()
    after attach DOES close the target Chrome — use detach() instead if you
    want to keep it running.
    """
    try:
        _validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    if BrowserState.is_up():
        return err(
            f"Browser already attached/running on instance {BrowserState.current_instance_id!r}. "
            f"Call browser_close() or detach() first."
        )
    if port is None:
        port = await _detect_chrome_debugging_port()
        if port is None:
            return err(
                "No Chrome with --remote-debugging-port found. Start Chrome with:\n"
                "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=9222 --user-data-dir=/path/to/profile\n"
                "Then call attach_to_chrome(port=9222) or just attach_to_chrome()."
            )
    # Probe the CDP endpoint with httpx — fail fast with a clear message
    # if the port isn't actually open or doesn't speak CDP.
    try:
        async with httpx.AsyncClient(timeout=3.0) as cli:
            r = await cli.get(f"http://{host}:{port}/json/version")
            r.raise_for_status()
            info = r.json()
    except Exception as e:
        return err(
            f"CDP probe failed at http://{host}:{port}/json/version — {e}. "
            "Make sure Chrome is running with --remote-debugging-port and the "
            "port is reachable (no firewall, correct host)."
        )

    config = Config(host=host, port=port)
    try:
        browser = await asyncio.wait_for(
            nodriver.start(config=config),
            timeout=BROWSER_LAUNCH_TIMEOUT,
        )
    except Exception as e:
        return err(f"attach failed after CDP probe ok: {e}")

    BrowserState.browser = browser
    BrowserState.current_instance_id = instance_id
    BrowserState.current_profile_dir = None  # unknown — managed by external Chrome
    BrowserState.current_headless = False
    BrowserState.current_privacy = False
    BrowserState.current_fingerprint = {}
    BrowserState.current_proxy = None
    BrowserState.current_idle_timeout = 0  # never auto-close attached browsers
    BrowserState.current_last_active = time.time()
    BrowserState.current_created_at = time.time()
    _ATTACHED_BROWSERS.add(instance_id)
    await _refresh_tabs()
    return ok(
        f"Attached to existing Chrome at {host}:{port} as {instance_id!r}. "
        f"Browser: {info.get('Browser', '?')}, {len(BrowserState.tabs)} tab(s). "
        "Use detach() to release without closing Chrome."
    )


@mcp.tool()
async def detach() -> str:
    """Release CDP connection to an attached Chrome WITHOUT closing it.
    Only valid for browsers connected via attach_to_chrome — for browsers
    spawned by browser_launch/spawn_browser this is equivalent to a no-op
    (use browser_close instead).

    After detach, Chrome keeps running with all tabs intact. You can
    re-attach later with attach_to_chrome(port=...)."""
    iid = BrowserState.current_instance_id
    if not BrowserState.is_up():
        return err("no browser currently attached/running")
    if iid not in _ATTACHED_BROWSERS:
        return err(
            f"current browser ({iid!r}) was launched by us, not attached. "
            "Use browser_close() to close it. detach() only applies to "
            "attach_to_chrome() connections."
        )
    # Close just the websocket connection, leave the Chrome process alive.
    detached_tabs = list(BrowserState.tabs)
    try:
        if BrowserState.browser:
            # Each tab may hold its own connection, and closing only the browser's would
            # leave those open with their listener tasks running.
            for connection in (*detached_tabs,
                               getattr(BrowserState.browser, "connection", None) or BrowserState.browser):
                await close_connection(connection)
        # Don't call .stop() — that terminates Chrome.
    finally:
        _ATTACHED_BROWSERS.discard(iid)
        BrowserState.reset()
        _clear_tab_scoped_state(detached_tabs)
    return ok(f"detached from {iid!r}. Chrome still running externally.")


from .tools import storage as _storage  # noqa: E402,F401  -- registers cookie and storage tools


from .tools import capture as _capture  # noqa: E402  -- registers interception, HAR and dry_run tools


from .tools import page as _page  # noqa: E402  -- registers snapshot, reading, frame and scraping tools


from .tools import navigation as _navigation  # noqa: E402,F401  -- registers history, tab and human-help tools


from .tools import interaction as _interaction  # noqa: E402,F401  -- registers pointer, keyboard, form and wait tools


from .tools import mouse_kit as _mouse_kit  # noqa: E402,F401  -- registers the precision mouse tools


from .tools import stealth as _stealth  # noqa: E402,F401  -- registers identity, session, captcha and verify tools


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


def _stop_all_browsers() -> None:
    """Stop every browser this process started, in every session and the pool.

    Stopping only the current one left stored instances running after the server
    exited, with nothing remaining that knew about them.
    """
    for key in BrowserState.session_keys():
        token = BrowserState.in_session(key)
        try:
            # What an agent session going to sleep looks like: stdin closes and these browsers
            # end. Their tabs are saved first, from /json/list, as the event loop is gone.
            views = [(BrowserState.current_instance_id, BrowserState.browser, list(BrowserState.tabs),
                      BrowserState.active_tab_index, BrowserState.current_profile_dir,
                      BrowserState.current_privacy)]
            views += [(iid, s.browser, list(s.tabs), s.active_tab_index, s.profile_dir, s.privacy)
                      for iid, s in BrowserState.instances.items()]
            for view in views:
                with contextlib.suppress(Exception):
                    _record_tabs_at_exit(*view)
            handles = [BrowserState.browser]
            handles.extend(s.browser for s in BrowserState.instances.values())
            for browser in handles:
                with contextlib.suppress(Exception):
                    if browser and not getattr(browser, "stopped", False):
                        browser.stop()
        finally:
            BrowserState.unbind_session(token)


def _parse_cli(argv: list[str]) -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp-stealth-chrome",
        description="Stealth Chrome MCP server. Speaks stdio unless --http is given.",
    )
    parser.add_argument("--http", action="store_true",
                        help="serve MCP over HTTP: one server, one session per client")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit a non-loopback --host; anyone with the token controls the browser")
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="hostname clients use, for a proxy or a wildcard --host (repeatable)")
    return parser.parse_args(argv)


def main() -> None:
    """MCP entry point: stdio by default, HTTP with --http.

    Survival rules — MCP stdio servers must NOT die when the parent (Claude
    Code) cancels a tool call or hits Esc:

    1. Detach into a new session via `os.setsid()`. Terminal-group signals
       (the SIGINT a shell sends on Ctrl+C / Esc to its whole process group)
       no longer reach us.
    2. SIG_IGN on SIGINT and SIGTERM at process level. asyncio overrides
       SIGINT once its loop starts, so we also re-install via the loop in
       a startup hook (best-effort — if it fails, setsid still protects us).
    3. Restart loop: if mcp.run() ever raises despite the above (broken
       transport, transient asyncio crash), re-enter up to 3 times before
       actually exiting. EOF on stdin (BrokenPipeError) is a normal
       shutdown signal from the parent and exits immediately.
    """
    import signal

    cli = _parse_cli(sys.argv[1:])
    # A thread, so neither the start nor any tool waits on it; a failure stays in there.
    with contextlib.suppress(Exception):
        dock_icon.start_in_background()
    # Leftover privacy and fallback profiles: one pass on its own thread, see profile_gc.
    with contextlib.suppress(Exception):
        profile_gc.start_in_background(state_data_root())
    # Measured off the critical path, so the first server_status has an answer, not a timeout.
    with contextlib.suppress(Exception):
        _start_os_cursor_probe()
    if cli.http:
        # None of the stdio survival rules below apply: there is no parent to
        # outlive, and SIGTERM must be able to stop a long-running service.
        from . import http_transport

        code = 1
        try:
            code = http_transport.serve(mcp, cli.host, cli.port, cli.allow_remote,
                                        cli.allowed_host)
        except (SystemExit, KeyboardInterrupt):
            code = 0
        finally:
            _stop_all_browsers()
        if code:
            sys.exit(code)
        return

    # 1. Detach from the parent's process group so terminal SIGINT/SIGTERM
    #    aimed at the group never lands on us.
    try:
        os.setsid()
    except (OSError, AttributeError):
        pass  # Windows or already a session leader

    # 2. Ignore at process level. asyncio may override SIGINT later; that's
    #    okay — setsid above already shields from group-targeted signals.
    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGPIPE"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass

    # 3. Restart loop. mcp.run() blocks until stdin EOF or a crash. On a
    #    crash we re-enter; on EOF or repeated crashes we exit cleanly.
    crashes = 0
    while True:
        try:
            mcp.run()
            break  # clean stdin EOF — parent closed connection
        except (BrokenPipeError, EOFError):
            break  # parent gone, stop trying
        except KeyboardInterrupt:
            # Should not arrive (we SIG_IGN'd it), but if asyncio re-raised
            # one anyway, swallow and continue.
            continue
        except Exception:
            crashes += 1
            if crashes >= 3:
                break
            continue
        finally:
            pass

    _stop_all_browsers()


if __name__ == "__main__":
    main()
