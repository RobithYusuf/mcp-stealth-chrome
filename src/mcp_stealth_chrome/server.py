"""mcp-stealth-chrome — FastMCP server entry + all tool implementations.

Architecture parallels mcp-camoufox (Node/Firefox sister package):
- single-file tool registry for easy maintenance
- same tool names, same parameters, same ref system
- nodriver CDP direct + async throughout
"""
from __future__ import annotations

import asyncio
from collections import Counter
import base64
import contextlib
import contextvars
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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Optional

import nodriver
from nodriver import Browser, Config, Tab
from nodriver.cdp import dom as cdp_dom, runtime as cdp_runtime

from . import patches as _patches
_patches.apply_all()
from .key_outcome import element_unread
from .helpers import (
    OPEN_DIALOGS,
    BODY_REVEAL_NOTE,
    dialog_note,
    err,
    get_title,
    get_url,
    mask_url,
    ok,
    PageReadError,
    parse_json,
    target_key,
    transport_lost,
)
from .identity import (
    default_accept_language,
    key_event_params,
    navigator_languages,
    navigator_platform,
    os_family,
    strip_headless,
    reduced_user_agent,
    native_family,
    ua_metadata_fields,
)
from .refs import (
    apply_isolated,
    close_connection,
    ensure_in_root_view,
    evaluate_isolated,
    evaluate_parts,  # noqa: F401  -- tools/action_kit.py reads it as _srv.evaluate_parts
    forget_refs,
    outer_blocker,
    release_group,  # noqa: F401  -- tools/action_kit.py reads it as _srv.release_group
    resolve_ref,
)
from . import brave as brave_mode
from . import brave_lean
from . import dock_icon
from . import brave_beta
from . import launch_lock
from . import profile_gc
from . import profile_version
from . import presence
from . import body_saver
from . import cookie_migration
from . import quit_watch
from . import keep_alive as _keep_alive
from . import tab_record
from . import instance_notes
from . import run_marker
from .state import data_root as state_data_root
from .humanize import (
    CURRENT_GESTURE, Gesture, humanized_approach, humanized_move, humanized_mouse_press,
    move_pointer,
)
from .state import (
    DEFAULT_IDLE_TIMEOUT,
    IDENTITIES_DIR,
    IDLE_REAPER_INTERVAL,
    PROFILE_DIR,
    PROFILES_ROOT,
    PRIVACY_PROFILE_ROOT,
    STORAGE_STATE_DIR,
    BrowserState,
    InstanceSnapshot,
    TabNotUsable,
    chrome_lock_holder_pid,
    chrome_user_data_root,  # noqa: F401  -- tools/chrome_profiles.py reads it as _srv.<name>
    chrome_user_data_roots,  # noqa: F401  -- tools/chrome_profiles.py reads it as _srv.<name>
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
NAV_COMMIT_TIMEOUT = float(os.environ.get("BROWSER_NAV_TIMEOUT", "30"))
BROWSER_NAV_TIMEOUT = NAV_COMMIT_TIMEOUT
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


# Response bodies kept by Chrome outside the renderer (Network.enable enableDurableMessages), so
# they outlive the document that loaded them. Without it a 324-byte JSON reply read after the
# page moved on answered "No resource with given identifier found". Chrome bounds the store by
# these sizes and drops the oldest past the total; nothing runs per request on our side.
CAPTURE_BODY_BUFFER_BYTES = int(os.environ.get("CAPTURE_BODY_BUFFER_BYTES", str(64 * 2**20)))
CAPTURE_BODY_MAX_BYTES = int(os.environ.get("CAPTURE_BODY_MAX_BYTES", str(10 * 2**20)))


def _resource_type(event: Any) -> str:
    """"XHR", "Fetch", "Document"...: the CDP resource type's own name."""
    value = getattr(event, "type_", None) or getattr(event, "type", None)
    return str(getattr(value, "value", value) or "")


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


# browser_launch's own defaults: a size other than this was asked for by the caller.
_DEFAULT_WINDOW = (1280, 800)


async def _restore_window_bounds(instance_id: str, tab, size: Optional[tuple] = None) -> dict:
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
    if size:
        # The caller asked for a size on this launch: the saved one must not undo it.
        width, height = int(size[0]), int(size[1])
        result["size_from"] = "this launch"
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


def _identify(pid: int, browser: Any) -> str:
    """What a recorded PID runs now: "same" (this browser), "different" (another program, or a
    later process on the reused number), "gone", or "unknown" when that cannot be told.

    A handle can outlive its Chrome after a crash, and the OS hands the number out again.
    Signalling it then kills something innocent, so only "same" may be signalled, and
    "unknown" is never read as "same". The profile must be named exactly (/tmp/profile is not
    /tmp/profile-evil), and on POSIX the process must have the start time recorded for this
    browser (quit_watch.pid_start).
    """
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    command = _process_command(pid)
    if command is None:
        return "gone" if not _process_exists(pid) else "unknown"
    if not data_dir:
        return "unknown"
    if not _keep_alive._watcher.names_profile(command, str(data_dir)):
        return "different"
    if sys.platform == "win32":
        return "same"  # no start time there; the exact command line is the identity
    recorded = getattr(browser, quit_watch.START_ATTR, None)
    now = _keep_alive._watcher.process_start(pid, recorded)
    if not recorded or now is None:
        return "unknown"
    return "same" if now == recorded else "different"


def _pid_recycled(pid: int, browser: Any) -> bool:
    """Whether a recorded PID now belongs to another program, on positive evidence."""
    return _identify(pid, browser) == "different"


def _signal_verified(pid: int, sig: int, start: Optional[str], matches: Any) -> bool:
    """Signal a PID only while it is provably the process meant, checked right before."""
    if sys.platform == "win32":
        command = _process_command(pid)
        if not command or not matches(command):
            return False
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False
    return _keep_alive._watcher.signal_if_same(pid, sig, start, matches)


def _descendant_pids(pid: int, data_dir: Optional[str] = None,
                     table: Optional[list[tuple[int, int, str]]] = None) -> Optional[list[int]]:
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
    from .state import process_table, processes_naming
    rows = process_table() if table is None else table
    if rows is None:
        return None  # the process list could not be read: unknown, never "no helpers"
    # A path boundary, as processes_naming has it: /tmp/profile names /tmp/profile/Crashpad
    # (the crash handler's --database) but never /tmp/profile-evil.
    # The folder as given, not through Path: on Windows Path("/tmp/p") reads back as \\tmp\\p.
    named = ({p for p, _c in processes_naming(str(data_dir), table=rows) or []}  # type: ignore[arg-type]
             if data_dir else None)
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
            if named is None or child in named:
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
    if browser is None or _socket_dropped(browser):
        return False
    # Up to 0.48 the socket is browser.connection; from 0.50 the Browser is the Connection and
    # .connection stays None. Asking only .connection never sent Browser.close on 0.50, so every
    # close there ended Chrome with SIGTERM before it wrote its cookies.
    connection = getattr(browser, "connection", None)
    if connection is None:
        connection = browser
    try:
        await asyncio.wait_for(connection.send(cdp_browser.close()), timeout=2)
        return True
    except Exception:
        # The reply can be lost as Chrome closes the socket on its way out; whether it is
        # really going is decided by watching the process, not by this answer.
        return _socket_dropped(browser)


def _socket_dropped(browser: Any) -> bool:
    """The browser's CDP socket was open and is now closed, on either nodriver design. A socket
    never opened is not dropped: send() opens it."""
    with contextlib.suppress(Exception):
        connection = getattr(browser, "connection", None)
        if connection is not None:
            return bool(getattr(connection, "closed", False))
        socket = getattr(browser, "socket", None)
        return socket is not None and getattr(socket, "close_code", None) is not None
    return False


async def _ensure_browser_dead(browser, profile_dir=None, grace: float = 1.5) -> dict:
    presence.stop(browser)  # its overlay goes first: nothing is left over a closing window
    # Body saving ends before its browser: bodies whose loading finished are written first.
    await body_saver.stop_for(browser, "its browser was closed")
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
    if isinstance(pid, int) and pid > 0 and browser is not None:
        # Recorded at launch; read now for a browser that has none, so a PID handed out again
        # during this close still reads as another process.
        quit_watch.pid_start(browser, pid)
    identity = _identify(pid, browser) if isinstance(pid, int) and pid > 0 else "gone"
    recycled = identity == "different"
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    family = (_descendant_pids(pid, str(data_dir) if data_dir else None)
              if isinstance(pid, int) and pid > 0 and identity == "same" else [])
    helpers_unknown = family is None
    family = family or []
    family_starts = _keep_alive._watcher.process_starts(family) or {} if family else {}
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
    # Collect every PID that could still be ours. One whose identity cannot be told is never
    # signalled; if it still runs, the close is not verified and the handle is kept.
    if identity == "unknown" and isinstance(pid, int) and pid > 0:
        await _all_gone([pid], grace)
        if _process_exists(pid):
            report["still_alive"].append(pid)
            report["detail"] = (f"PID {pid} could not be identified (its command line or start time "
                                f"could not be read), so it was not signalled")
        report["verified"] = not report["still_alive"]
        return report
    candidates: list[int] = [pid] if isinstance(pid, int) and pid > 0 else []
    candidates += [child for child in family if child not in candidates]
    # The profile scan is only a fallback for when no PID was captured. With a PID the
    # target is known, and a scan can only add processes that are not ours to signal.
    scanned: dict[int, Optional[str]] = {}
    if profile_dir is not None and not candidates:
        try:
            from .state import find_chrome_pids_by_profile
            for extra in find_chrome_pids_by_profile(profile_dir):
                if extra not in candidates:
                    candidates.append(extra)
            # Their identity: the start time each had at the scan (none on Windows, where the
            # exact command line stands in for it).
            starts = _keep_alive._watcher.process_starts(candidates) or {}
            scanned = {extra: starts.get(extra) for extra in candidates}
        except Exception:
            pass
    if not candidates:
        # Nothing to check against — cannot prove the process ended.
        return report

    # Let a graceful stop finish before escalating. Chrome is gone about 0.1 s after stop(),
    # so this returns then instead of sitting out the whole grace on every close.
    await _all_gone(candidates, grace)
    from .state import processes_naming as _naming

    def _verified(target: int, sig: int) -> bool:
        # Each signal only to the process meant, checked right before it: the browser by its
        # exact profile and start time, a helper by naming the profile and the start time it
        # had when the close began. A PID found by the profile scan (no PID was recorded) has
        # no recorded start and goes by its command line alone, as before.
        if target == pid:
            return _signal_verified(target, sig, getattr(browser, quit_watch.START_ATTR, None),
                                    lambda c: _keep_alive._watcher.names_profile(c, str(data_dir)))
        if target in scanned:
            # Found by the profile scan, with no PID recorded: its start time read at the scan
            # is its identity, and without one it is not signalled.
            return _signal_verified(target, sig, scanned[target],
                                    lambda c: bool(_naming(str(profile_dir), table=[(target, 0, c)])))
        if target in family and data_dir:
            names = lambda c: bool(_naming(str(data_dir), table=[(target, 0, c)]))  # type: ignore[arg-type]  # noqa: E731
            if sys.platform == "win32" or target not in family_starts:
                return _signal_verified(target, sig, None, names) if sys.platform == "win32" else False
            return _signal_verified(target, sig, family_starts[target], names)
        return False  # nothing identifies it: not signalled, and reported

    unidentified: list[int] = []
    for target in candidates:
        if not _process_exists(target):
            continue                    # already gone, or not ours to signal
        if _verified(target, _signal.SIGTERM):
            report["killed"].append(target)
        else:
            unidentified.append(target)
    if report["killed"]:
        await _all_gone(report["killed"], 1.0)
        for target in list(report["killed"]):
            if not _process_exists(target):
                continue
            # Windows has no SIGKILL; os.kill with any other signal is TerminateProcess there.
            if not _verified(target, getattr(_signal, "SIGKILL", _signal.SIGTERM)):
                report["still_alive"].append(target)
        # SIGKILL takes a moment, and the verdict below decides whether the handle is kept.
        for _ in range(20):
            if not any(_process_exists(target) for target in report["killed"]):
                break
            await asyncio.sleep(0.1)
    for target in candidates:
        if _process_exists(target) and target not in report["still_alive"]:
            if target in unidentified and _identify(target, browser) == "different" and target == pid:
                continue  # it ended and its number went to another program meanwhile
            report["still_alive"].append(target)
    if unidentified:
        left = [t for t in unidentified if t in report["still_alive"]]
        if left:
            report["detail"] = (report.get("detail") or "") + (
                f"PID(s) {left} could not be proved to be this browser's (command line or start "
                f"time unreadable, or changed), so they were not signalled")
    report["verified"] = not report["still_alive"]
    if helpers_unknown and report["verified"]:
        # The browser's helpers could not be listed, so none could be waited for: they may
        # still hold the profile. Not verified, and the handle stays.
        report["verified"] = False
        report["helpers_unknown"] = True
        report["detail"] = ((report.get("detail") or "") + "the process list could not be read, so "
                            "whether the browser's helper processes ended is unknown")
    if report["verified"]:
        await _discard_temp_profile(browser)
        if isinstance(pid, int) and pid > 0 and data_dir:
            # Gone, so a later launch has nothing to reattach to.
            with contextlib.suppress(Exception):
                _keep_alive.remove(data_dir, pid)
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
    return (bool(report.get("still_alive")) or bool(report.get("helpers_unknown"))
            or not (report.get("stopped") or report.get("verified")))


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


@dataclass
class _Launching:
    """A browser_launch under way in a session: the instance it launches and the task running it.
    browser_recover sets `ended_by`, and the launch then ends as an error that records nothing
    more; should it not end by itself, recover cancels the task (`cancelled`)."""
    instance_id: str
    task: Any
    ended_by: str = ""
    cancelled: bool = False
    plan: Any = None  # the launch's _LaunchPlan, once it has one
    # What the instance's slot held once the launch's own first steps were done (a stale handle
    # ended, a running browser not reused): the launch publishes its browser only over that.
    slot: Any = None
    slot_taken: bool = False  # `slot` has been recorded


# session -> the browser_launch under way there; read through _launching().
_LAUNCHING: dict[str, _Launching] = {}
# The launch the running task carries out (set in _launch_run), so a step deep in it can ask
# whether its own launch was cut short; another task that copied this context is not it.
_THIS_LAUNCH: contextvars.ContextVar = contextvars.ContextVar("mcp_this_launch", default=None)
# How long browser_recover waits for a launch it asked to stop to end by itself, and again
# after cancelling one that did not.
_RECOVER_LAUNCH_WAIT = float(os.environ.get("MCP_STEALTH_RECOVER_LAUNCH_WAIT", "2"))

# A launch awaits between its steps, and a call that changed the current instance or its
# browser meanwhile had the launch's later steps, and its cleanup, act on another instance.
# These tools are refused while a launch of the same session runs (_launch_gate), and
# close_instance only for the launching instance. Every tool that can change the current
# instance or its browser is either here or in _ALLOWED_DURING_LAUNCH, with its reason
# (tests/test_launch_owner.py fails on one that is in neither). batch_actions and workflow_run
# call tools without the gate, so none of these may become a workflow tool.
_REFUSED_DURING_LAUNCH = frozenset({"switch_instance", "attach_to_chrome", "detach", "browser_close",
                                    "close_all_instances", "spawn_browser"})
_ALLOWED_DURING_LAUNCH = {
    "browser_recover": "the way out of a stuck state must not depend on what is stuck: it ends the "
                       "browser as before, and the launch it cut short ends as an error that "
                       "records nothing more",
    "browser_launch": "a second launch in the same session is refused by browser_launch itself "
                      "(another launch is already in progress)",
    "close_instance": "refused for the launching instance only; closing another instance acts on its "
                      "stored snapshot and leaves the launch alone",
    "instance_release": "refuses the current instance by itself, and the launching instance stays "
                        "the current one for the whole launch (switch_instance is refused)",
}


def _launching() -> Optional[_Launching]:
    """The browser_launch under way in the calling session, or None. One whose task is done
    counts as absent, so a marker that was somehow left behind refuses nothing; so does one
    browser_recover cancelled that has not ended yet, or a launch hung on an await that ignores
    the cancel would keep the session refused after the recover."""
    entry = _LAUNCHING.get(BrowserState.session_key())
    if entry is None or entry.task is None or entry.task.done() or entry.cancelled:
        return None
    return entry


def _launch_cut_reason() -> str:
    """Why the browser_launch running in this very task was cut short (browser_recover asked it
    to stop), or "". A reattach asks before it takes a kept browser over, so a launch that was
    cut short gives that browser back as it was instead of recording it."""
    entry = _THIS_LAUNCH.get()
    if entry is None or entry.task is not asyncio.current_task():
        return ""
    if entry.ended_by:
        # Who cut it short is said once, by the launch's own result (_launch_settle).
        return "this browser_launch was cut short before it took the browser over"
    taken = _launch_slot_taken(entry)
    return f"{taken}, before this browser_launch took the browser over" if taken else ""


def _launch_slot_taken(launching: _Launching) -> str:
    """Why the launching instance's slot is no longer the launch's to fill, or "". A call that was
    already running when the launch began passed the gate before there was a mark, and can switch
    instances or publish a browser (attach_to_chrome) while the launch awaits: publishing over
    it lost that call's handle."""
    iid = launching.instance_id
    if BrowserState.current_instance_id != iid:
        return f"instance {iid!r} stopped being the current one while this launch ran (another call switched)"
    if launching.slot_taken and BrowserState.browser is not launching.slot:
        return f"another call put a browser on instance {iid!r} while this launch ran"
    return ""


def _being_launched(instance_id: str) -> bool:
    """A browser_launch of this session is launching that instance; a sweep leaves it alone."""
    entry = _launching()
    return entry is not None and entry.instance_id == instance_id


def _launch_gate(name: str) -> Optional[str]:
    """CALL_GATES: refuse a tool that would change the current instance or its browser while a
    browser_launch of this session runs. Cheap and never awaits."""
    entry = _launching()
    if entry is None:
        return None
    if name == "close_instance":
        refused = (_app_module.CALL_ARGUMENTS.get() or {}).get("instance_id") == entry.instance_id
    else:
        refused = name in _REFUSED_DURING_LAUNCH
    if not refused:
        return None
    return err(f"browser_launch of instance {entry.instance_id!r} is in progress in this session; "
               f"{name} was not done. Call it again once the launch has returned.")


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


def _brand_for(tab: Any) -> str:
    """The brand a client-hints override must name for this tab's browser. Raises for a browser
    whose kind could not be told (attach measured nothing): naming one could be the wrong one."""
    product = brave_mode.product_of(tab)
    if product == brave_mode.UNKNOWN:
        raise RuntimeError(
            "which browser this is could not be told (it was attached, and neither its process nor "
            "navigator.brave said), so its client hints were not changed: they would name a "
            "browser it may not be")
    return brave_mode.brand(product)


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
    needs_cleanup = (not requested_ua and native != real) or bool(
        getattr(getattr(getattr(tab, "browser", None), "config", None), "headless", False))
    if requested_ua or language or platform or needs_cleanup:
        # Brave lists itself as Brave, with reduced versions: an override naming Google Chrome
        # there contradicts navigator.brave, which no override can remove.
        product = _brand_for(tab)
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
        hints = ua_metadata_fields(user_agent, platform, real_version, product=product)
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
# How long _remove_privacy_profile waits for one read of the process list (processes_holding,
# _profile_holders). They only read, so abandoning one is harmless; unbounded, a read that never
# returned held a cancelled launch, and its launch lock, past browser_recover's wait.
_HOLDERS_READ_WAIT = float(os.environ.get("MCP_STEALTH_HOLDERS_READ_WAIT", "3"))
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


async def _write_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a write to the profile in a worker thread, never left half way. Cancelling a task that
    awaits asyncio.to_thread does not stop the thread: browser_recover cancelled a launch while
    its cookie strip ran, and the strip went on writing the database beside the cancel path and
    the next launch. So the thread is awaited under asyncio.shield, and a cancel that comes
    meanwhile waits for the thread to return (the write is bounded by itself: SQLite's busy
    timeout, a file write and rename) before it goes on up. Read-only thread calls need none."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError as cancelled:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue  # cancelled again (an anyio scope repeats it): the write still comes first
            except Exception:
                break
        if not work.cancelled():
            work.exception()  # retrieved: the cancel is what this call answers now
        raise cancelled


def _forget_removed_profile(profile_dir: Path) -> None:
    """The instance that still names a privacy folder just removed names no profile any more.
    list_instances and server_status went on giving the removed folder as the instance's profile
    after a browser_close and after a failed launch. Only an instance holding no browser: one
    that runs names what it runs on."""
    if BrowserState.browser is None and BrowserState.current_profile_dir == profile_dir:
        BrowserState.current_profile_dir = None
        BrowserState.current_privacy = False


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
    from .state import processes_holding
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
        # short name becomes the long one), and processes_naming tries both. An unreadable
        # browser process counts too (processes_holding).
        try:
            found = await asyncio.wait_for(asyncio.to_thread(processes_holding, profile_dir),
                                           timeout=_HOLDERS_READ_WAIT)
            late = False
        except asyncio.TimeoutError:
            found, late = None, True
        users = found or []
        if found is None:
            # Unknown is not "none": a helper may still run and write the files back.
            reason = (f"the process list could not be read in {_HOLDERS_READ_WAIT:g} s, so it is not known "
                      f"whether Chrome still runs" if late else
                      "the process list could not be read, so it is not known whether Chrome still runs")
            await asyncio.sleep(delay)
        elif users:
            reason = "a process still runs with it: " + ", ".join(
                f"PID {pid} ({_short_command(command)})" for pid, command in users)
            await asyncio.sleep(delay)
        else:
            try:
                await _write_in_thread(shutil.rmtree, target)
            except FileNotFoundError:
                pass
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(delay)
                if not target.exists():
                    _forget_removed_profile(profile_dir)
                    return True
                reason = "it was written again after being removed"
        if not target.exists():
            _forget_removed_profile(profile_dir)
            return True
        if attempt >= len(_PRIVACY_REMOVE_DELAYS) and time.monotonic() >= deadline:
            break
    try:
        holders = await asyncio.wait_for(asyncio.to_thread(
            _profile_holders, profile_dir, _left_in(target), {pid for pid, _command in users}),
            timeout=_HOLDERS_READ_WAIT)
    except asyncio.TimeoutError:
        holders = f"who holds it is unknown: reading the process list took longer than {_HOLDERS_READ_WAIT:g} s"
    _PRIVACY_REMOVE_FAILURES[str(profile_dir)] = "; ".join(p for p in (reason, holders) if p)
    return False




async def _safe_stop_browser(browser: Optional[Browser]) -> None:
    """Best-effort shutdown — used in cleanup paths so a half-launched Chrome
    doesn't leak its profile lock."""
    if browser is None:
        return
    presence.stop(browser)  # a launch that failed after its overlay started leaves none behind
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
  // The phrases alone are also what a page ABOUT Cloudflare says: a product landing page quoting
  // "Just a moment..." read as a challenge, and auto_verify waited and said "CF detected but no
  // clickable widget found". They count only with the challenge page's own marks, which it carries
  // before its widget renders: its orchestrate script, its form, or its inline _cf_chl_opt.
  const cfText = phrases.some(p => txt.includes(p)) && (
    !!document.querySelector('script[src*="/cdn-cgi/challenge-platform/"][src*="/orchestrate/"], ' +
                             'form[action*="__cf_chl"]')
    || Array.from(document.scripts).some(
      s => !s.src && s.text.length < 20000 && s.text.indexOf('_cf_chl_opt') >= 0));
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
  const loginForm = Array.from(responseInputs).some(i => i.form?.querySelector(
      'input[name="user"], input[type="email"], input[type="password"], input[autocomplete="username"]'));
  const loginInterstitial = !!document.querySelector(
    'script[src*="/cdn-cgi/challenge-platform/"][src*="/orchestrate/"], form[action*="__cf_chl"]')
    || Array.from(document.scripts).some(
      s => !s.src && s.text.length < 20000 && s.text.indexOf('_cf_chl_opt') >= 0);
  if (loginForm && !loginInterstitial) return false;
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
from . import _app
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
    BrowserState.own_untracked()  # tabs counted before the list changes: a first look owns them
    BrowserState.tabs = list(browser.tabs)
    _own_pages_opened_by_ours()
    if current is not None:
        wanted = _target_key(current)
        for position, tab in enumerate(BrowserState.tabs):
            if _target_key(tab) == wanted:
                BrowserState.active_tab_index = position
                break
        else:
            if answered:
                # The active tab is gone and no tool closed it. Keeping the index would make the
                # next tool act on whichever tab now sits there, possibly a person's.
                BrowserState.active_tab_index = -1
                BrowserState.active_lost = _active_lost_note(current)
    return answered


def _active_lost_note(tab: Any) -> str:
    return (f"The active tab ({BrowserState.tab_label(tab)}) was closed outside this server's tools, "
            f"by a person or by its own page. No other tab was picked in its place: tab_list shows "
            f"the tabs, tab_select picks the one to act on, and tab_new opens a new one.")


def _own_pages_opened_by_ours() -> None:
    """A page that one of ours opened (window.open, a target=_blank link) is ours too."""
    if not BrowserState.owned_targets:
        return
    for tab in BrowserState.tabs:
        key = _target_key(tab)
        if key in BrowserState.owned_targets:
            continue
        opener = getattr(getattr(tab, "target", None), "opener_id", None)
        if opener and str(opener) in BrowserState.owned_targets:
            BrowserState.owned_targets.add(key)


async def _check_active_tab_alive() -> None:
    """Before a tool runs: notice an active tab that was closed outside the tools.

    nodriver 0.48 drops a closed target by itself, 0.50 only on update_targets, and a Tab whose
    target is gone answered "server rejected WebSocket connection: HTTP 500". One browser-level
    call (no renderer needed, so an open dialog does not block it) says whether it still exists.
    """
    browser = BrowserState.browser
    index = BrowserState.active_tab_index
    if browser is None or not 0 <= index < len(BrowserState.tabs):
        return
    tab = BrowserState.tabs[index]
    from nodriver.cdp import target as cdp_target
    connection = getattr(browser, "connection", None) or browser
    try:
        await asyncio.wait_for(connection.send(cdp_target.get_target_info(
            target_id=cdp_target.TargetID(_target_key(tab)))), timeout=2)
        return
    except asyncio.TimeoutError:
        return
    except Exception as exc:
        if "no target" not in str(exc).lower():
            return
    await _refresh_tabs()


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


def _safe_profile_clone(source: Path, instance_id: str,
                        root: Optional[Path] = None) -> tuple[Optional[Path], dict]:
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
    target = (root or PROFILES_ROOT) / f"{instance_id}-clone-{int(time.time())}"
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


def _holder_story(holder: int, profile: Path) -> str:
    """Who the Chrome holding a profile is, for the fallback warning: the caller's next move
    differs (wait for another session, close a leftover, or leave the person's browser alone)."""
    from .profile_gc import is_server_command
    from .state import process_table
    record = _keep_alive.read(profile) or {}
    if record.get("pid") == holder:
        # A kept or taken-over browser: its parent is PID 1 whoever drives it; the record says who.
        server = _keep_alive._server_holding(record)
        if server == os.getpid():
            return f"Chrome PID {holder}, which this server already drives (another session or instance)"
        if server is not None:
            return f"Chrome PID {holder}, which another stealth server (PID {server}) drives"
        return f"Chrome PID {holder}, a browser of ours whose server is gone ({_keep_alive.describe(record)})"
    table = process_table() or []
    row = next((r for r in table if r[0] == holder), None)
    if row is None:
        return f"Chrome PID {holder}"
    ours = all(flag in row[2] for flag in _keep_alive.OUR_FLAGS)
    if ours and row[1] == os.getpid():
        return f"Chrome PID {holder}, which this server already drives (another session or instance)"
    parent = next((r for r in table if r[0] == row[1]), None)
    if ours and parent is not None and is_server_command(parent[2]):
        return f"Chrome PID {holder}, which another stealth server (PID {row[1]}) drives"
    if ours:
        return f"Chrome PID {holder}, started by this server's code (parent PID {row[1]})"
    return f"Chrome PID {holder}, which this server did not start (your own Chrome, or another tool's)"


def _resolve_launch_profile(
    instance_id: str,
    *,
    persistent: bool,
    profile_conflict: str,
    browser: str = "chrome",
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

    browser="brave" resolves the same way among Brave's own folders (brave_mode), without the
    affinity, which belongs to Chrome's main profile.
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

    on_brave = browser == brave_mode.BRAVE
    if on_brave:
        desired = brave_mode.instance_profile(_brave_data_root(), instance_id)
    else:
        desired = PROFILE_DIR if instance_id == "main" else PROFILES_ROOT / instance_id
    info["desired_profile"] = str(desired)

    pinned = None if on_brave else get_profile_affinity()
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
        clone, clone_report = _safe_profile_clone(
            desired, instance_id, brave_mode.profiles_root(_brave_data_root()) if on_brave else None)
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

    if on_brave:
        fallback = brave_mode.fallback_profile(_brave_data_root(), instance_id)
    else:
        fallback = (
            per_process_profile() if instance_id == "main"
            else PROFILES_ROOT / f"{instance_id}-pid{os.getpid()}"
        )
    fallback.mkdir(parents=True, exist_ok=True)
    info["actual_profile"] = str(fallback)
    try:
        story = _holder_story(holder, desired)
    except Exception:
        story = f"Chrome PID {holder}"
    info["holder"] = story
    info["session_state_source"] = (
        f"FALLBACK profile {fallback.name} — {desired} is held by {story}. "
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


def _front_pid_in_process() -> Optional[int]:
    """The app in front, asked of the Process Manager in this process: 0.04 ms, against 15.5 ms
    for two lsappinfo processes, and it runs before every input on a visible browser and every
    20 ms in the popup watch. Fresh in a process with no run loop (6 of 6 switches agreed with
    lsappinfo, and a switch showed in under 1 ms), which NSWorkspace is not."""
    try:
        import ctypes

        class _PSN(ctypes.Structure):
            _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

        services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        psn, pid = _PSN(), ctypes.c_int(0)
        if services.GetFrontProcess(ctypes.byref(psn)) != 0:
            return None
        if services.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid)) != 0:
            return None
        return pid.value if pid.value > 0 else None
    except Exception:
        return None


def _frontmost_pid() -> Optional[int]:
    if sys.platform != "darwin":
        return None
    found = _front_pid_in_process()
    if found is not None:
        return found
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                               timeout=2, check=False).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "pid", front], capture_output=True,
                              text=True, timeout=2, check=False).stdout
    except Exception:
        return None
    found = re.search(r'"pid"=(\d+)', info)
    return int(found.group(1)) if found else None


def _front_process(pid: int) -> bool:
    """The Process Manager's way to the front, by PID. AppKit's activateWithOptions is a request
    macOS sits on for about a second when another app has just come to the front by itself (a
    popup): Chrome stayed in front 1033 and 1039 ms where `open -a` took it back at once."""
    try:
        import ctypes

        class _PSN(ctypes.Structure):
            _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

        services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        psn = _PSN()
        if services.GetProcessForPID(ctypes.c_int(pid), ctypes.byref(psn)) != 0:
            return False
        return services.SetFrontProcessWithOptions(ctypes.byref(psn), ctypes.c_uint32(0)) == 0
    except Exception:
        return False


def _activate_pid(pid: Optional[int]) -> bool:
    """macOS: bring ONE process to the front, by PID, through the Objective-C runtime (no PyObjC).

    Focus used to move with `open -a <bundle>`, which names an application, not a process. With
    several Chromes running from one bundle (the person's own, another project's browser, this
    server's) macOS chose among them: request_human_help left another project's browser in
    front (seen on 21 Nov 2026: the app in front afterwards was that browser's PID), and focus
    "given back" to a person who was using a Chrome could land on a different one. Measured from
    a background process: a test browser came to the front by PID and the person's app got the
    front back by PID. True when macOS took the request; callers check `_frontmost_pid`."""
    if sys.platform != "darwin" or not pid:
        return False
    if _front_process(int(pid)):
        return True
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        by_pid = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_int)(("objc_msgSend", objc))
        activate = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_ulong)(("objc_msgSend", objc))
        sel = objc.sel_registerName
        pool = send(send(objc.objc_getClass(b"NSAutoreleasePool"), sel(b"alloc")), sel(b"init"))
        try:
            app = by_pid(objc.objc_getClass(b"NSRunningApplication"),
                         sel(b"runningApplicationWithProcessIdentifier:"), int(pid))
            # 2: NSApplicationActivateIgnoringOtherApps
            return bool(app) and bool(activate(app, sel(b"activateWithOptions:"), 2))
        finally:
            send(pool, sel(b"drain"))
    except Exception:
        return False


_FOCUS_STAYED = 0.4  # seconds the front must stay away from this browser before the watch ends


async def _win_focus_back_from_popup(browser_pid: int, back_to: int, seconds: float = 1.5) -> None:
    """window.open activates Chrome, and no flag or CDP call prevents that. So after input on
    a headed browser, watch briefly and give focus straight back if this Chrome took it.

    Only this browser's own process counts, so a person switching to any other app, their own
    Chrome included, is left alone, and focus goes back to the very process that had it
    (`back_to`, a PID), never to "the application". Keys typed in the moment before it returns
    still reach Chrome: this narrows that window and does not close it.
    """
    deadline = time.monotonic() + seconds
    returned_at = None
    try:
        while time.monotonic() < deadline:
            front = await asyncio.to_thread(_frontmost_pid)
            if front == browser_pid:
                # Chrome is still opening its popup when the front first changes, and takes the
                # front again after one hand-back (656 and 1057 ms in front, measured, where the
                # limit is 500): keep handing it back until it has stayed away.
                await asyncio.to_thread(_activate_pid, back_to)
                returned_at = time.monotonic()
            elif returned_at is not None and time.monotonic() - returned_at >= _FOCUS_STAYED:
                return
            await asyncio.sleep(0.02)
    except Exception:
        pass


async def _give_focus_back(previous: Optional[int]) -> None:
    """A headed Chrome takes the foreground when its window opens, away from whatever the
    person was using: give it back to that process (a PID). Best effort: a launch must never
    fail over this."""
    if not previous:
        return
    try:
        await asyncio.sleep(0.3)  # the window activates a moment after CDP answers
        if await asyncio.to_thread(_frontmost_pid) not in (None, previous):
            await asyncio.to_thread(_activate_pid, previous)
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
    product: str = "chrome"


def _launch_choice(profile_path: Optional[Path], headless: bool,
                   extra_args: Optional[list] = None, executable: str = "") -> _LaunchChoice:
    """Chrome Beta only for a profile it cannot damage: see profile_version.beta_verdict.
    Must run before clean_profile_state or the window-state wipe, which write to the profile
    and would make a new one look used. `executable` is a browser the caller chose (Brave),
    which the Dock icon never replaces."""
    if executable:
        # A visible Brave runs from Brave Beta, with Beta's own icon, on the same terms.
        picked, on_beta, note = brave_beta.choose(profile_path, headless=headless,
                                                  brave=executable, extra_args=extra_args)
        return _LaunchChoice(picked, beta=on_beta, note=note, product=brave_mode.BRAVE)
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
    brave_mode.mark(config, choice.product)  # read by the cookie plan, whatever the file is called
    if choice.product == brave_mode.BRAVE:
        config.browser_executable_path = choice.executable
        if not choice.beta:
            return f" [Dock icon: Brave, because {choice.note}]" if choice.note else ""
        picked = await asyncio.to_thread(brave_beta.prepare, headless=headless)
        if not picked.executable:
            # Beta went between the choice and now: Brave itself, and say so.
            config.browser_executable_path = brave_mode.find_brave() or choice.executable
            return f" [Dock icon: Brave, because {picked.note or 'Brave Beta could not be used'}]"
        config.browser_executable_path = picked.executable
        version = f" {picked.version}" if picked.version else ""
        keychain = "" if "--use-mock-keychain" in (config.browser_args or []) else brave_beta.KEYCHAIN_NOTE
        tail = "".join(f"; {t}" for t in (picked.note, choice.note, keychain) if t)
        return f" [Dock icon: Brave Beta{version}{tail}]"
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


def _profile_version_refusal(profile_path: Optional[Path], executable: Optional[str],
                             product: Optional[str] = None) -> str:
    """Why `executable` must not open this profile, or "": see profile_version. A profile last
    opened by Chrome Beta is refused to an older Google Chrome rather than damaged by it.

    `product` is the browser the launch chose; without it the executable's name decides. A check
    that fails is a refusal: a guard that cannot answer has not said yes."""
    try:
        executable = str(executable or "")
        if product is None:
            product = (brave_mode.BRAVE if brave_mode.is_brave_executable(executable)
                       else brave_mode.CHROME)
        # A profile belongs to the browser that made it: Brave and Chrome cannot read each
        # other's cookies, and each drops what it cannot read.
        bound = brave_mode.binding_refusal(profile_path, product)
        if bound:
            return bound
        if product == brave_mode.BRAVE:
            return profile_version.refusal(
                profile_path, executable,
                name="Brave Beta" if brave_beta.is_beta_executable(executable) else "Brave")
        return profile_version.refusal(profile_path, executable,
                                       beta=executable.startswith(str(dock_icon.BETA_APP) + "/"))
    except Exception as exc:
        return (f"refused to open {profile_path}: the profile could not be checked "
                f"({_exc_text(exc)}). Nothing was started and the profile was not changed.")


def _profile_from_extra_args(chosen: Optional[str], extra_args: Optional[list],
                             label: str) -> tuple[Optional[str], list, str]:
    """(profile folder, extra_args without --user-data-dir, problem).

    Chromium opens the last --user-data-dir it is given, and nodriver puts its own first, so a
    --user-data-dir in extra_args is the profile that really opens. It becomes the launch's
    profile folder, so the lock, binding, version and cookie checks look at that folder and the
    browser gets it once. Naming a different one in `label` as well is refused."""
    found, rest, problem = brave_mode.split_profile_args(extra_args)
    if problem:
        return chosen, list(extra_args or []), f"{problem}. Nothing was started."
    if found is None:
        return chosen, rest, ""
    if chosen:
        same = False
        with contextlib.suppress(OSError):
            same = Path(chosen).expanduser().resolve() == Path(found).expanduser().resolve()
        if not same:
            return chosen, list(extra_args or []), (
                f"{label}={chosen!r} and --user-data-dir={found!r} in extra_args name two different "
                f"profile folders. Pass one. Nothing was started.")
    return found, rest, ""


# Executables that are Google Chrome or Chromium by name. Anything else on the port (a wrapper,
# Edge, Vivaldi) is not assumed to be Chrome.
_CHROME_EXECUTABLES = {
    "google chrome", "google chrome beta", "google chrome dev", "google chrome canary",
    "google chrome for testing", "chrome", "chrome.exe", "google-chrome", "google-chrome-stable",
    "google-chrome-beta", "google-chrome-unstable", "chromium", "chromium.exe", "chromium-browser",
}


def _attached_product_from_processes(host: str, port: int) -> Optional[str]:
    """"brave" or "chrome" from the main process listening on this debugging port, when the host
    is this machine, that process can be found and its executable is one of those browsers by
    name; otherwise None, and navigator.brave decides."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    from .state import process_table
    table = process_table()
    if not table:
        return None
    flag = re.compile(rf"--remote-debugging-port={int(port)}(?!\d)")
    for _pid, _ppid, command in table:
        if command and "--type=" not in command and flag.search(command):
            executable = command.split(" --", 1)[0].strip().strip('"')
            name = re.split(r"[\\/]", executable)[-1].lower()
            if brave_mode.is_brave_executable(name):
                return brave_mode.BRAVE
            return brave_mode.CHROME if name in _CHROME_EXECUTABLES else None
    return None


async def _attached_product(browser: Any, host: str, port: int) -> str:
    """What an attached browser is: its process first, then navigator.brave read in a page's
    isolated world (Brave defines it there too). navigator.brave missing only says "not Brave",
    so that, like no answer at all, is "unknown": nothing names it Chrome on a guess."""
    found = await asyncio.to_thread(_attached_product_from_processes, host, port)
    if found:
        return found
    with contextlib.suppress(Exception):
        # Not an internal page, where no world can be made; a target not yet updated reads "".
        tabs = [t for t in getattr(browser, "tabs", []) or []
                if not str(getattr(getattr(t, "target", None), "url", "") or "").startswith(
                    ("chrome", "devtools:", "brave:", "edge:"))]
        for tab in tabs[:3]:
            seen = await _wait(evaluate_isolated(tab, "typeof navigator.brave"), timeout=3,
                               what="attach product")
            if seen == "object":
                return brave_mode.BRAVE
            if seen == "undefined":
                break
    return brave_mode.UNKNOWN


def _attached_note(product: str) -> str:
    """What attach_to_chrome adds when it could not tell which browser it reached."""
    if product != brave_mode.UNKNOWN:
        return ""
    return (" Which browser this is could not be told (its executable is not Google Chrome, "
            "Chromium or Brave by name, or could not be read, and navigator.brave did not say "
            "Brave), so fingerprint_rotate and emulate_device will not change its client hints.")


def _brave_status() -> dict:
    """Whether browser="brave" can launch here, for server_status."""
    try:
        path = brave_mode.find_brave()
        found = profile_version.binary_version(path) if path else None
        return {"installed": bool(path), "path": path,
                "version": profile_version.text(found) if found else None,
                "lean": brave_lean.status()}
    except Exception as exc:
        return {"installed": None, "detail": _exc_text(exc)}


def _lean_on_reattach(product: str, given: Optional[bool]) -> str:
    """A reattach starts no browser, so no lean flag and no setting is applied: the running
    Brave keeps what it was started with. Said whatever brave_lean was, the default included."""
    if product != brave_mode.BRAVE:
        return ""
    asked = f"brave_lean={given!r}" if given is not None else "brave_lean (default)"
    return (f"{asked} not applied: the running Brave keeps the flags and settings it was started "
            f"with; browser_close() and launch again to apply it")


def _brave_lean_running() -> Optional[dict]:
    """The current browser's brave_lean, as its launch decided it; None when it is not Brave."""
    browser = BrowserState.browser
    if browser is None or brave_mode.product_of(browser) != brave_mode.BRAVE:
        return None
    outcome = getattr(getattr(browser, "config", None), brave_lean.ATTR, None)
    if outcome is None:
        return {"enabled": None, "detail": "not started by this server (reattached or attached), "
                                           "so the launch that started it decided"}
    return outcome.summary()


def _brave_data_root() -> Path:
    """Where Brave's profiles go: beside Chrome's, so a test pointing PROFILES_ROOT at a
    temporary folder moves both."""
    return PROFILES_ROOT.parent


def _browser_note(config: Config, product: str) -> str:
    """The launch result's word on which browser runs, for Brave; Chrome's results read as before."""
    executable = config.browser_executable_path
    if product != brave_mode.BRAVE:
        return ""
    found = profile_version.binary_version(str(executable))
    return f" [browser: Brave{' ' + profile_version.text(found) if found else ''}]"


def _brave_lean_registry() -> Path:
    """Where brave_lean records the profiles this server made empty: the server's data folder,
    never a profile, so a copy of a profile carries no proof with it."""
    chosen = os.environ.get(brave_lean.REGISTRY_ENV, "").strip()
    return Path(chosen).expanduser() if chosen else _brave_data_root() / "brave-lean"


def _apply_brave_lean(config: Config, product: str, brave_lean_arg: Optional[bool],
                      server_chosen: bool = False, registry: Optional[Path] = None) -> str:
    """brave_lean before the browser starts (see brave_lean). Left unset it is on only for a
    profile this server made empty (`server_chosen`: the launch picked the folder, not the
    caller); True asks for it on any profile but the person's own Brave folder; False puts back
    what an earlier lean launch wrote. Chrome has none of these features: nothing is done there,
    and the result says so only when brave_lean was passed. Never raises."""
    if product != brave_mode.BRAVE:
        return " [brave_lean ignored: it applies to Brave only]" if brave_lean_arg is not None else ""
    root = Path(config.user_data_dir) if config.user_data_dir else None
    outcome = brave_lean.apply(root, list(config.browser_args or []), brave_lean_arg,
                               server_chosen=server_chosen,
                               registry=registry or _brave_lean_registry())
    if outcome.enabled:
        config.add_argument(_disable_features_arg(config, brave_lean.FEATURES))
    with contextlib.suppress(Exception):
        setattr(config, brave_lean.ATTR, outcome)
    return outcome.note()


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

    Preparing it writes nothing in the profile. `flag`: launch with the protection turned off.
    `refuse`: start nothing, and why. `marker`: the pending marker _commit_cookie_move writes
    once the launch has checked the profile, and `strip` whether the v10 rows then go from
    beside v11 ones. `backup`: after the launch, set the cookies in this file again and check
    them. `note` goes into the launch result."""
    plan: Any
    flag: bool = False
    backup: Optional[Path] = None
    note: str = ""
    refuse: str = ""
    marker: Optional[dict] = None
    strip: bool = False


def _cookie_flag_arg(config: Config) -> str:
    return _disable_features_arg(config, (cookie_migration.FEATURE,))


async def _prepare_cookie_move(profile_path: Optional[Path], config: Config) -> _CookieMove:
    """Before Chrome 158 opens a profile holding v10 cookies: read them with the protection off
    from a copy, keep them in a durable backup, and say what the launch must commit. Never
    raises, and writes nothing in the profile: a refusal leaves it exactly as it was."""
    executable = str(config.browser_executable_path or "")
    product = getattr(config, "_mcp_stealth_product", None) or (
        brave_mode.BRAVE if brave_mode.is_brave_executable(executable) else brave_mode.CHROME)
    plan = await asyncio.to_thread(cookie_migration.plan, profile_path, list(config.browser_args or []),
                                   executable, product)
    if plan.kind == "none":
        return _CookieMove(plan)
    if plan.kind == "refuse":
        return _CookieMove(plan, refuse=_cookie_refusal(profile_path, plan.reason, plan.marker, product))
    if plan.kind == "flag":
        return _CookieMove(plan, flag=True, note=f" [cookies: {plan.reason}]")
    if plan.kind == "resume":
        # Every loaded v10 row is in the verified backup (plan checked). Beside v11 rows Chrome
        # would delete them all, so they go at commit, and the backup sets them again.
        return _CookieMove(plan, backup=Path(str(plan.marker["backup"])), strip=bool(plan.v10 and plan.v11))
    try:
        return await _move_cookies_out(Path(profile_path), config, plan, executable, product)
    except cookie_migration.Changed as exc:
        return _CookieMove(plan, refuse=_cookie_refusal(profile_path, (
            f"its cookies changed while they were being backed up ({exc}), so what Chrome would "
            "delete is no longer known"), plan.marker, product))
    except Exception as exc:
        return _cookie_move_failed(plan, profile_path, _exc_text(exc), product)


def _cookie_refusal(profile_path: Any, why: str, marker: Optional[dict] = None,
                    product: str = "chrome", written: Optional[str] = None) -> str:
    """The refusal, with what can be done about it. Nothing was started. Left unset, `written`
    keeps the text that says the profile was not changed (Chrome's text is unchanged; for Brave
    it names Brave); a refusal that comes after this launch wrote in the profile passes what it
    wrote instead."""
    saved = ""
    if marker and marker.get("backup"):
        saved = (f" A backup of its cookies is at {marker['backup']} (storage_state_load reads it) "
                 f"and a copy of its cookie databases at {marker.get('database_copy') or 'an unrecorded place'}.")
    browser = "Brave" if product == brave_mode.BRAVE else "Chrome"
    done = ("Nothing was started and the profile was not changed." if written is None
            else f"Nothing was started; {written}.")
    return (f"refused to open {profile_path} with {browser} 158 or newer: {why}. {done}{saved} "
            "Close every Chrome that has this profile open and "
            "launch again, which decides afresh. To work meanwhile without risking these cookies, "
            "clone_chrome_profile makes a copy to launch instead. If the cookie database itself is "
            "damaged, Chrome cannot use it either: restore it from a backup of the profile first. "
            "persistent=False starts on an empty profile, without these logins")


def _cookie_move_failed(plan: Any, profile_path: Any, why: str, product: str = "chrome") -> _CookieMove:
    """A move that did not finish preparing; the profile was not touched. The flag keeps the
    v10 cookies but deletes v11 ones (Beta 159 with the flag read 0 of 3 and left 0 rows), so a
    profile holding v11 is not started at all.

    For Brave it is always a refusal. Brave Beta is not on the ACL of the "Brave Safe Storage"
    Keychain item, and a reader that cannot read the cookies is most likely one macOS did not let
    use that key: the launch with the flag cannot decrypt them either, and dropped all 3 (measured
    with the dialog unanswered). Unknown is not safe."""
    if product == brave_mode.BRAVE:
        return _CookieMove(plan, refuse=_cookie_refusal(profile_path, (
            f"Brave could not read the cookies it would have to move ({why}). Usually this means "
            f"macOS did not let this Brave use 'Brave Safe Storage' in your Keychain: the first "
            f"start asks, and only Always Allow lets it read them. Opened now, Brave would drop "
            f"every cookie it cannot read"), plan.marker, product))
    if getattr(plan, "v11", 0):
        return _CookieMove(plan, refuse=_cookie_refusal(profile_path, (
            f"its cookies could not be moved to Chrome 158 encryption ({why}), and it holds "
            f"{plan.v11} v11 cookies the protection turned off would delete beside "
            f"{plan.v10} v10 ones it would keep; the next launch tries again"), plan.marker))
    return _CookieMove(plan, flag=True, note=(
        f" [cookies NOT moved to Chrome 158 encryption: {why}. This launch opens the profile with "
        f"the protection turned off, so nothing is lost, and the next launch tries again]"))


async def _cookie_preflight(profile_path: Optional[Path], move: _CookieMove,
                            written: Optional[str] = None) -> str:
    """The last look before anything is written: the profile must be as the cookie decision
    found it. A Chrome that took its lock, or a cookie database of the launched sub-profile
    written since (another program, a v10 cookie arriving after the check), means that decision
    no longer holds. Only reads. Returns why it is refused, or ""."""
    plan = move.plan
    if profile_path is None or getattr(plan, "fingerprint", None) is None:
        return ""

    def look() -> str:
        from .state import chrome_lock_state
        lock, holder = chrome_lock_state(Path(profile_path))
        if lock == "held":
            return f"Chrome PID {holder} opened it after this launch checked it"
        if lock == "unknown":
            return (f"its SingletonLock ({Path(profile_path) / 'SingletonLock'}) is there but names no "
                    "process that can be read, so a Chrome may hold it; if none runs, that file is "
                    "stale and can be removed")
        try:
            now = cookie_migration.fingerprint_of(Path(profile_path), plan.subprofile)
        except Exception as exc:
            return f"its cookie databases could not be checked again ({_exc_text(exc)})"
        if now != plan.fingerprint:
            return "its cookie database changed after this launch read it"
        return ""

    why = await asyncio.to_thread(look)
    return _cookie_refusal(profile_path, (
        f"{why}, so what Chrome would delete is no longer known"), getattr(plan, "marker", None),
        written=written) if why else ""


async def _commit_cookie_move(profile_path: Optional[Path], move: _CookieMove) -> str:
    """The writes a move makes in the profile, after _cookie_preflight and right before Chrome
    starts: the pending marker (durable), then, beside v11 cookies, the v10 rows, each database
    in one transaction that checks they are all in the backup. Returns why the launch must not
    go on, or "". From the marker on, every crash resumes on the next launch."""
    if profile_path is None:
        return ""
    profile = Path(profile_path)
    try:
        if move.marker is not None:
            await _write_in_thread(cookie_migration.write_marker, profile, move.marker)
        if move.strip:
            cookies = await asyncio.to_thread(
                cookie_migration.verified_backup, move.backup, cookie_migration.read_marker(profile))
            if cookies is None:
                raise cookie_migration.Changed(f"the backup {move.backup} no longer matches its marker")
            allowed = dict(Counter(cookie_migration.key(c) for c in cookies))
            marker = cookie_migration.read_marker(profile)
            # Every row deleted must be the very row the backup was read from (database copy).
            snapshot = await asyncio.to_thread(
                cookie_migration.snapshot_digests, Path(str(marker.get("database_copy") or "")),
                move.plan.subprofile, b"v10")
            await _write_in_thread(cookie_migration.strip, profile, move.plan.subprofile, b"v10", allowed,
                                   snapshot)
            # Its own change, checked row by row: the last look before Chrome starts compares to this.
            move.plan.fingerprint = await asyncio.to_thread(
                cookie_migration.fingerprint_of, profile, move.plan.subprofile)
    except Exception as exc:
        marker = cookie_migration.read_marker(profile)
        return _cookie_refusal(profile_path, (
            f"its cookies could not be prepared for Chrome 158 ({_exc_text(exc)}); the move stays "
            "pending, and the next launch resumes it from the backup"), marker)
    return ""


async def _close_after_cookie_failure(browser: Any, profile_path: Any) -> str:
    """Close the browser a move could not finish in, so the next launch resumes the move. What
    to do next, in words."""
    closed = await _ensure_browser_dead(browser, profile_path)
    if _close_incomplete(closed):
        return (f"The browser could not be closed ({_close_failure(closed)}): close it with browser_close, "
                "then launch again, which resumes the move")
    if BrowserState.browser is browser:
        BrowserState.reset()
    return "The browser was closed; launch again, which resumes the move"


async def _read_v10_cookies(folder: Path, subprofile: str, executable: str,
                            launch_args: Optional[list] = None) -> list:
    """Every cookie a headless Chrome with the protection turned off reads from `folder`, a
    temporary user-data folder. Raises when it cannot read them or does not end.

    The reader keeps the launch's Keychain: a launch with --use-mock-keychain encrypted with the
    mock key, and a reader on the real Keychain read none of it and asked the person's login
    Keychain for Brave Beta, which its item does not list (a dialog on their screen)."""
    keychain = [a for a in (launch_args or []) if a == "--use-mock-keychain"]
    reader_config = Config(
        user_data_dir=str(folder), headless=True,
        browser_args=[f"--profile-directory={subprofile}", "--no-default-browser-check",
                      "--disable-restore-session-state", "--hide-crash-restore-bubble", *_LEAN_FLAGS,
                      *keychain],
    )
    reader_config.browser_executable_path = executable
    reader_config.add_argument(_cookie_flag_arg(reader_config))
    reader = None
    cookies: list = []
    problem: Optional[BaseException] = None
    try:
        reader = await _start_chrome(reader_config)
        answer = await asyncio.wait_for(
            (getattr(reader, "connection", None) or reader).send(_cdp_call("Storage.getCookies")),
            timeout=30)
        cookies = list((answer or {}).get("cookies") or [])
    except Exception as exc:
        problem = exc
    closed = await _ensure_browser_dead(reader, folder) if reader is not None else {"verified": True}
    if problem is not None:
        raise RuntimeError(f"reading them with the protection turned off failed ({_exc_text(problem)})")
    if not closed.get("verified"):
        raise RuntimeError(f"the Chrome that read them did not end ({_kill_note(closed) or 'not verified'})")
    return cookies


async def _move_cookies_out(profile: Path, config: Config, plan: Any, executable: str,
                            product: str = "chrome") -> _CookieMove:
    """Steps 1 to 4 of cookie_migration. Nothing in the profile is written or opened: the
    databases are copied (verified, fsynced), a Chrome with the flag reads the v10 rows from a
    temporary folder, the backup is written durably and read back, and the profile must still
    be as the decision read it. The pending marker is left to _commit_cookie_move."""
    ensure_dirs()
    # Unique: a launch retried within the same second after a cut-off move must not collide.
    name = (f"{cookie_migration.BACKUP_PREFIX}-{cookie_migration.label(profile)}-"
            f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}")
    db_copy = STORAGE_STATE_DIR / f"{name}-db"
    await asyncio.to_thread(cookie_migration.copy_databases, profile, plan.subprofile, db_copy)
    folder = await asyncio.to_thread(cookie_migration.reader_folder, db_copy, plan.subprofile, b"v10")
    try:
        cookies = await _read_v10_cookies(folder, plan.subprofile, executable,
                                          list(config.browser_args or []))
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    missing = cookie_migration.missing_from(plan.v10_keys, Counter(cookie_migration.key(c) for c in cookies))
    expected = sum(plan.v10_keys.values())
    if missing or len(cookies) < expected:
        sample = ", ".join(f"{n}@{h}" for n, h, *_ in missing[:5])
        return _cookie_move_failed(plan, profile, (
            f"Chrome read {len(cookies)} of the {expected} cookies in the profile with the protection "
            f"turned off{', not ' + sample if sample else ''}; the profile was not touched"), product)
    if plan.marker.get("state") == "pending" and plan.marker.get("backup"):
        # A move made again over a pending one: cookies only that backup still holds (their rows
        # already taken out from beside v11 ones) are carried over, or they would be lost.
        earlier = await asyncio.to_thread(
            cookie_migration.verified_backup, Path(str(plan.marker["backup"])), plan.marker)
        read = {cookie_migration.key(c) for c in cookies}
        cookies += [c for c in earlier or [] if cookie_migration.key(c) not in read]
    backup = STORAGE_STATE_DIR / f"{name}.json"
    document = cookie_migration.backup_document(cookies, profile, executable)
    await _write_in_thread(cookie_migration.write_durable, backup, json.dumps(document))
    saved = await asyncio.to_thread(cookie_migration.verified_backup, backup)
    if saved is None or [cookie_migration.value_digest(c) for c in saved] != \
            [cookie_migration.value_digest(c) for c in cookies]:
        raise RuntimeError(f"the backup {backup} did not read back as written")
    now = await asyncio.to_thread(cookie_migration.fingerprint_of, profile, plan.subprofile)
    if now != plan.fingerprint:
        raise cookie_migration.Changed("a cookie database of the profile was written meanwhile")
    marker = {"state": "pending", "version": cookie_migration.MARKER_VERSION,
              "profile": os.path.realpath(profile), "subprofile": plan.subprofile,
              "backup": str(backup), "database_copy": str(db_copy),
              "digest": await asyncio.to_thread(cookie_migration.file_digest, backup),
              "count": len(cookies), "major": profile_version.binary_version(executable)[0],
              "started_at": time.time()}
    return _CookieMove(plan, backup=backup, marker=marker, strip=bool(plan.v11))


async def _finish_cookie_move(browser: Any, profile: Path, move: _CookieMove) -> tuple[bool, str]:
    """Step 7: set the backed-up cookies in the Chrome just started without the flag, read them
    back and compare keys, value digests, flags and expiry. (True, note) once all match and the
    move is done; (False, text) otherwise, with the marker left pending so the next launch sets
    them again. No value is printed."""
    if move.backup is None:
        return True, move.note
    connection = getattr(browser, "connection", None) or browser
    try:
        cookies = cookie_migration.verified_backup(move.backup, cookie_migration.read_marker(profile))
        if cookies is None:
            raise RuntimeError("the backup does not match its marker")
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
        back = list((answer or {}).get("cookies") or [])
    except Exception as exc:
        return False, (f"the cookies could not be set again ({_exc_text(exc)}); every one of them is in "
                       f"{move.backup}, storage_state_load({str(move.backup)!r}) sets them, and the "
                       "next launch of this profile tries again")
    try:
        missing, differ = cookie_migration.compare(cookies, back)
    except Exception as exc:
        return False, (f"the cookies set again could not be compared ({_exc_text(exc)}); every one of them "
                       f"is in {move.backup}, and the move stays pending")
    if missing or differ:
        wrong = missing + differ
        sample = ", ".join(f"{n}@{d}" for n, d, *_ in wrong[:5])
        return False, (f"{len(cookies) - len(wrong)} of {len(cookies)} cookies came back as they were after "
                       f"the move to Chrome 158 encryption; {len(missing)} missing and {len(differ)} with a "
                       f"different value, flag or expiry, among them {sample}{' and more' if len(wrong) > 5 else ''}"
                       f"{'; refused: ' + refused[0] if refused else ''}. All {len(cookies)} are in "
                       f"{move.backup}: storage_state_load({str(move.backup)!r}) sets them, and the next "
                       "launch of this profile tries again")
    try:
        marker = cookie_migration.read_marker(profile)
        cookie_migration.write_marker(profile, {**marker, "state": "done", "finished_at": time.time()})
    except Exception as exc:
        return False, (f"every cookie came back, but the move could not be marked done ({_exc_text(exc)}); "
                       f"they are all in {move.backup}, and the move stays pending")
    # Backups of earlier moves whose retention has passed; this one stays for its own.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(cookie_migration.expire_backups, move.backup.parent)
    days = cookie_migration.retention_days()
    return True, (f" [cookies: migrated {len(cookies)} cookies to Chrome 158 encryption; "
                  f"backup at {move.backup}, removed after {days:g} days]")


async def _dock_icon_status() -> dict:
    try:
        return await asyncio.wait_for(asyncio.to_thread(dock_icon.status), timeout=3)
    except Exception as exc:
        return {"state": "unavailable", "detail": _exc_text(exc)}


def _settings_report() -> dict:
    from . import settings
    try:
        return settings.report()
    except Exception as exc:  # never fails server_status
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _brave_beta_status() -> dict:
    try:
        return await asyncio.wait_for(asyncio.to_thread(brave_beta.status), timeout=3)
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
# A browser being closed: from then on the background writer leaves it alone, and so does a
# retried close, so the windows closing one by one are never written over the list the first
# close recorded. Not for a while but for good: a close that fails keeps the handle, and the
# browser can linger half closed for minutes. A new launch is a new Browser object.
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


# The test switch for --use-mock-keychain. It reads and writes cookies under a key no other
# browser has, so a production launch carrying it would leave a profile whose logins only a mock
# can read. pytest sets PYTEST_CURRENT_TEST for every test and the servers a test starts inherit
# it; a server started by a person never has it.
_MOCK_KEYCHAIN = "--use-mock-keychain"


def _mock_keychain_refusal(extra_args: Optional[list]) -> str:
    if _MOCK_KEYCHAIN not in [str(a).strip() for a in (extra_args or [])]:
        return ""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    return (f"{_MOCK_KEYCHAIN} is refused: it is for tests only (it applies only under pytest). "
            f"Cookies written with it can only be read with it again. Nothing was started.")


def _launch_reservation(instance_id: str, product: str, *, persistent: bool, privacy: bool,
                        explicit: Optional[str], extra_args: Optional[list]) -> list:
    """The profiles a launch may open, reserved across processes (launch_lock) from the first look
    at who holds them until the browser holds them itself. None for a temporary profile."""
    if privacy:
        return []
    root, _rest, _problem = brave_mode.split_profile_args(extra_args)
    chosen = explicit or root
    if chosen:
        return [Path(str(chosen)).expanduser()]
    if not persistent:
        return []
    return _kept_candidates(instance_id, product if product in brave_mode.BROWSERS else "chrome")


# ── keep_alive: a browser left running when this server exits, and reattaching to it ──────

def _keep_alive_decision(keep_alive: Optional[bool], profile: Any, privacy: bool,
                         headless: bool) -> tuple[bool, str]:
    """(keep this browser at exit, note for the result). Only a persistent profile can be
    kept: there is nothing for a later server to find otherwise."""
    if not keep_alive:
        # Said even when left unset: on a reattach unset means "as it was", so a new browser's
        # result must not leave the caller to guess.
        return False, " [keep_alive=False: this browser closes when this server exits]"
    if privacy:
        return False, (" [keep_alive ignored: a privacy profile is deleted when the browser "
                       "closes, so it closes with this server]")
    if profile is None:
        return False, (" [keep_alive ignored: persistent=False has no profile a later server "
                       "could find, so it closes with this server]")
    why = _keep_alive.supported()
    if why:
        return False, f" [keep_alive ignored: {why}]"
    cap_note = f"; {_keep_alive.max_hours_problem()}" if _keep_alive.max_hours_problem() else ""
    hours = _keep_alive.cap_hours()
    if headless:
        return True, (f" [keep_alive{cap_note}: this headless browser keeps running after this server "
                      f"exits, an invisible Chrome holding memory, until a later launch on "
                      f"this profile reattaches and closes it, or it has had no server for "
                      f"{hours:g} h (MCP_STEALTH_KEEP_ALIVE_MAX_HOURS)]")
    return True, (f" [keep_alive{cap_note}: stays open when this server exits; Quit still ends it, and "
                  f"it ends once it has had no server for {hours:g} h "
                  f"(MCP_STEALTH_KEEP_ALIVE_MAX_HOURS). A later browser_launch on this "
                  f"profile reattaches to it]")


def _keep_alive_on_reuse(keep_alive: Optional[bool]) -> str:
    """browser_launch on the browser already running here: apply an explicit keep_alive, and say
    what holds either way."""
    browser = BrowserState.browser
    headless = BrowserState.current_headless
    privacy = BrowserState.current_privacy
    attached = BrowserState.current_instance_id in _ATTACHED_BROWSERS
    profile = None if attached else BrowserState.current_profile_dir
    kept = bool(getattr(browser, _keep_alive.ATTR, False))
    if keep_alive and not kept:
        keep, note = _keep_alive_decision(True, profile, privacy, headless)
        if keep:
            note = _arm_keep_alive(browser, profile, BrowserState.current_instance_id, headless,
                                   BrowserState.current_fingerprint) or note
        return note
    if keep_alive is False and kept:
        _disarm_keep_alive(browser, profile, headless)
        return " [keep_alive=False: this browser now closes when this server exits]"
    if kept:
        return " [keep_alive: on, it stays open when this server exits]"
    return " [keep_alive=False: this browser closes when this server exits]"


def _chrome_version_of(browser: Any) -> Optional[str]:
    info = getattr(browser, "info", None)
    with contextlib.suppress(Exception):
        value = info.get("Browser") if hasattr(info, "get") else getattr(info, "Browser", None)
        if value:
            return str(value)
    return None


def _arm_keep_alive(browser: Any, profile: Any, instance_id: str, headless: bool,
                    fingerprint: dict, *, previous: Optional[dict] = None) -> str:
    """Mark a running browser to stay at exit: a watcher that outlives this server (Quit, time
    cap) and the record a later server finds it by. '' when armed, else why it was not, and
    the browser then closes with this server like any other."""
    pid = _browser_process_pid(browser)
    config = getattr(browser, "config", None)
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    if not (isinstance(pid, int) and pid > 0 and host and port):
        return " [keep_alive not armed: the browser's PID or port is unknown, so it closes with this server]"
    version = (_chrome_version_of(browser) or (previous or {}).get("chrome_version")
               or (_keep_alive._json_version(str(host), int(port)) or {}).get("Browser"))
    if not version:
        # A later server matches the browser on the port to the record by this; without it
        # anything answering there would do.
        return (" [keep_alive not armed: the browser's Chrome version could not be read, so it "
                "closes with this server]")
    started = quit_watch.pid_start(browser, pid)
    if not started:
        # Without it a PID handed to another program later could not be told from this Chrome.
        return (" [keep_alive not armed: the start time of the browser's process could not be "
                "read, so it closes with this server]")
    path = str(_keep_alive.record_path(profile))
    opened_by = (previous or {}).get("opened_by") or _keep_alive.OPENED_BY
    cap = _keep_alive.cap_hours(opened_by) * 3600
    if headless:
        proc = quit_watch.start(browser, _WINDOWLESS_GRACE, _CLOSE_WAIT, max_orphan_seconds=cap,
                                pages_matter=False, keep_alive_record=path)
    else:
        _WINDOW_WATCH[id(browser)] = {"browser": browser, "zero_since": None, "tool_closed": False}
        proc = quit_watch.start(browser, _WINDOWLESS_GRACE, _CLOSE_WAIT, max_orphan_seconds=cap,
                                keep_alive_record=path,
                                tabs_file=str(_keep_alive.tabs_path(profile)))
        _ensure_window_watcher_running()
    if proc is None:
        return (" [keep_alive not armed: its watcher could not start, and nothing could end the "
                "browser on Quit or at the time cap, so it closes with this server]")
    now = time.time()
    failed = _keep_alive.write(
        profile, pid=pid, host=str(host), port=int(port), instance_id=instance_id,
        chrome_version=version,
        headless=bool(headless), fingerprint=dict(fingerprint or {}), server_pid=os.getpid(),
        user_data_dir=str(getattr(config, "user_data_dir", "") or profile),
        pid_start=started, watcher_pid=proc.pid,
        watcher_start=_keep_alive._watcher.process_start(proc.pid), state="held", launched_at=(previous or {}).get("launched_at", now),
        attached_at=now if previous else None, kept_at=None, max_hours=cap / 3600,
        opened_by=opened_by, keep=True)
    if failed:
        # A plain watcher, as any headed browser has; none for headless.
        if headless:
            quit_watch.stop(browser)
        else:
            quit_watch.start(browser, _WINDOWLESS_GRACE, _CLOSE_WAIT)
        return f" [keep_alive not armed: its record could not be written ({failed}), so it closes with this server]"
    with contextlib.suppress(Exception):
        setattr(browser, _keep_alive.ATTR, True)
    return ""


def _disarm_keep_alive(browser: Any, profile: Any, headless: bool) -> None:
    """keep_alive off for a browser that had it: plain watcher; for a browser this server took
    over, a record that names this server as the one driving it, with keep False.

    Not removed: a Chrome taken over (an orphan, or a kept browser reattached) is not this
    server's child, so its parent stays PID 1, and with no record another launch read it as an
    orphan and took it from under this server (review of 0.25.2). The record makes keep_alive.check
    refuse it while this server lives; a close removes it once Chrome is gone, and if this server
    is killed the next launch finds a record whose server is gone and takes it over as before."""
    with contextlib.suppress(Exception):
        setattr(browser, _keep_alive.ATTR, False)
    if headless:
        quit_watch.stop(browser)
    else:
        quit_watch.start(browser, _WINDOWLESS_GRACE, _CLOSE_WAIT)
    if profile is None:
        return
    pid = _browser_process_pid(browser)
    if getattr(browser, "_process", None) is not None:
        # This server's own child: its parent is this server, so no launch reads it as an orphan
        # while this server lives, and the record goes as before.
        _keep_alive.remove(profile, pid)
        return
    record = _keep_alive.read(profile)
    if record is None or record.get("pid") != pid:
        return
    proc = quit_watch.watcher(browser)
    live = proc is not None and proc.poll() is None
    _keep_alive.write(profile, server_pid=os.getpid(), state="held", keep=False,
                      watcher_pid=proc.pid if live else None,
                      watcher_start=_keep_alive._watcher.process_start(proc.pid) if live else None)


def _kept_candidates(instance_id: str, browser: str = "chrome") -> list[Path]:
    """The profiles a persistent launch of this instance would ask for, in that order. A Brave
    launch asks for Brave's own folder only (brave_mode), without Chrome main's affinity."""
    from .state import get_profile_affinity
    if browser == brave_mode.BRAVE:
        return [brave_mode.instance_profile(_brave_data_root(), instance_id)]
    found: list[Path] = []
    pinned = get_profile_affinity() if instance_id == "main" else None
    if pinned is not None:
        found.append(Path(pinned))
    desired = PROFILE_DIR if instance_id == "main" else PROFILES_ROOT / instance_id
    if all(_keep_alive.profile_key(p) != _keep_alive.profile_key(desired) for p in found):
        found.append(desired)
    return found


def _server_profile_for(instance_id: str, browser: str = "chrome") -> str:
    """The profile folder this server chooses for an instance (no affinity), as a record key."""
    if browser == brave_mode.BRAVE:
        return _keep_alive.profile_key(brave_mode.instance_profile(_brave_data_root(), instance_id))
    return _keep_alive.profile_key(PROFILE_DIR if instance_id == "main" else PROFILES_ROOT / instance_id)


def _orphan_given_back(profile: Path, verdict: dict) -> bool:
    """After a takeover of an orphan answered an error: True when it never completed (its record
    still names no server), and then the record is dropped so the next launch looks afresh and
    this one may fall back. False when the takeover completed and only the page failed (a url
    that did not load, expect_url): the browser is this session's now, and falling back would
    replace the handle and leave it driven by nobody (review of 0.25.2)."""
    record = verdict.get("record") or {}
    if record.get("state") != _keep_alive.ORPHAN:
        return False
    now = _keep_alive.read(profile) or {}
    if now.get("pid") == record.get("pid") and now.get("server_pid"):
        return False
    if now.get("pid") == record.get("pid"):
        _keep_alive.remove(profile, record.get("pid"))
    return True


def _kept_verdict(profile: Path, browser: str = "chrome",
                  adopt_orphan_for: Optional[str] = None) -> Optional[dict]:
    """keep_alive.check for a profile some Chrome holds, or None when nothing was kept there.

    An ok verdict holds the profile's reattach lock: the caller reattaches at once, and
    _reattach_kept lets it go when done. A profile the other browser made (brave_mode binding)
    is not reattached for this launch: it would hand a Brave launch a Chrome, or the reverse.

    adopt_orphan_for (an instance id, for a profile the server chose itself): with no record, a
    browser of ours whose server is gone is described into an "orphan" record and checked like
    a kept one, rather than leaving the launch to an empty fallback profile. A record made here
    that does not pass is removed again."""
    if not profile.exists() or chrome_lock_holder_pid(profile) is None:
        return None
    record = _keep_alive.read(profile)
    made = False
    if record is None and adopt_orphan_for is not None:
        record = _keep_alive.orphan_record(profile, adopt_orphan_for)
        made = record is not None
    if record is None:
        return None
    bound = brave_mode.binding_refusal(profile, browser)
    if bound:
        if made:
            _keep_alive.remove(profile, record["pid"])
        return {"ok": False, "record": record, "holder": None,
                "reason": f"browser={browser!r} was asked for and {bound}"}
    verdict = _keep_alive.check(profile, hold=True)
    if made and not verdict.get("ok"):
        _keep_alive.remove(profile, record["pid"])
    return verdict


def _reattached_product(profile: Path) -> str:
    """What a reattached browser is: the maker of its profile, which the launch's binding has
    matched to the browser asked for. Marked on the new Browser, whose Config names nodriver's
    Chrome, so identity overrides name the right brand."""
    maker = brave_mode.profile_browser(profile)
    return maker if maker in (brave_mode.BRAVE, brave_mode.CHROME) else brave_mode.UNKNOWN


class _ReattachAborted(RuntimeError):
    """A reattach stopped before taking the browser over; nothing of the browser was changed."""


class _NotTheKeptBrowser(_ReattachAborted):
    """The browser connected to is not the one the checks found: it changed in between."""


async def _verify_connected(browser: Any, record: dict) -> None:
    """After connecting, the browser at the other end must still be the one checked before.

    Between keep_alive.check and the connection the Chrome can end and its PID or port go to
    another one. Asked over the very connection: its browser process PID and version; then the
    lock, the command line and the start time once more."""
    from nodriver.cdp import browser as cdp_browser, system_info as cdp_system_info
    connection = getattr(browser, "connection", None) or browser
    pid = int(record["pid"])
    infos = await _wait(connection.send(cdp_system_info.get_process_info()), timeout=5,
                        what="SystemInfo.getProcessInfo")
    found = [int(getattr(i, "id_", 0)) for i in infos or [] if getattr(i, "type_", "") == "browser"]
    if found != [pid]:
        raise _NotTheKeptBrowser(f"the browser on port {record['port']} is PID "
                                 f"{found[0] if found else 'unknown'}, not the kept PID {pid}")
    version = await _wait(connection.send(cdp_browser.get_version()), timeout=5,
                          what="Browser.getVersion")
    product = version[1] if isinstance(version, (tuple, list)) and len(version) > 1 else None
    if product != record.get("chrome_version"):
        raise _NotTheKeptBrowser(f"the browser on port {record['port']} is {product}, not the "
                                 f"recorded {record.get('chrome_version')}")
    problem = await asyncio.to_thread(_keep_alive.identity_problem, record)
    if problem:
        raise _NotTheKeptBrowser(problem)


def _not_reattached_note(profile: Path, verdict: Optional[dict]) -> str:
    if not verdict or verdict.get("ok"):
        return ""
    return (f" [a browser kept alive on {profile} was not reattached: {verdict.get('reason')}]")


async def _reattach_kept(instance_id: str, profile: Path, verdict: dict, *, url: str,
                         keep_alive: Optional[bool], ignored: list[str]) -> tuple[Any, list, int, dict, str, str]:
    """Connect to a browser a previous server of ours left running on `profile`.

    Opens nothing on the profile: no Chrome starts, so the profile version guard, the Chrome
    Beta choice and the cookie move have nothing to act on. Returns (browser, tabs, selected
    index, observed, summary, keep_alive note). Raises when Chrome does not take the connection.
    """
    try:
        return await _reattach_locked(instance_id, profile, verdict, url=url,
                                      keep_alive=keep_alive, ignored=ignored)
    finally:
        # Held from the check until the record names this server (or is gone).
        _keep_alive.release_lock(verdict)


async def _reattach_locked(instance_id: str, profile: Path, verdict: dict, *, url: str,
                           keep_alive: Optional[bool], ignored: list[str]
                           ) -> tuple[Any, list, int, dict, str, str]:
    record = verdict["record"]
    pid, port = int(record["pid"]), int(record["port"])
    host = str(record.get("host") or "127.0.0.1")
    headless = bool(record.get("headless"))
    fingerprint = dict(record.get("fingerprint") or {})
    # The folder exactly as Chrome was given it, which is what the watcher and a close compare.
    config = Config(user_data_dir=str(record["user_data_dir"]), headless=headless, host=host, port=port)
    browser = await asyncio.wait_for(nodriver.start(config=config), timeout=BROWSER_LAUNCH_TIMEOUT)
    brave_mode.mark(browser, _reattached_product(Path(_keep_alive.profile_key(profile))))
    # Not spawned here, but ours: the PID and its start time are what closing it and the
    # watcher go by.
    browser._process_pid = pid
    setattr(browser, quit_watch.START_ATTR, record.get("pid_start"))
    try:
        await _verify_connected(browser, record)
        # Proved with the record's own format; from here on the current one, which the record
        # is rewritten in when this reattach arms it.
        fresh = await asyncio.to_thread(_keep_alive._watcher.process_start, pid)
        if fresh:
            setattr(browser, quit_watch.START_ATTR, fresh)
        return await _reattach_connected(browser, instance_id, profile, record, url=url,
                                         keep_alive=keep_alive, ignored=ignored)
    except BaseException:
        # Drop only the connection: Chrome and its tabs stay as they were.
        with contextlib.suppress(Exception):
            await close_connection(getattr(browser, "connection", None) or browser)
        raise


async def _reattach_connected(browser: Any, instance_id: str, profile: Path, record: dict, *,
                              url: str, keep_alive: Optional[bool], ignored: list[str]
                              ) -> tuple[Any, list, int, dict, str, str]:
    pid, port = int(record["pid"]), int(record["port"])
    headless = bool(record.get("headless"))
    fingerprint = dict(record.get("fingerprint") or {})
    # Claimed before anything else changes: once the lock goes, the record must name this
    # server, or another one reads the old server as gone and takes the browser too.
    failed = _keep_alive.write(profile, server_pid=os.getpid(), state="attaching")
    if failed or (_keep_alive.read(profile) or {}).get("server_pid") != os.getpid():
        raise _ReattachAborted(f"its record could not be made to name this server ({failed or 'not written'})")
    watcher_note = ""
    try:
        watcher_note = await asyncio.to_thread(_keep_alive.end_old_watcher, record)
        if watcher_note not in ("ended", "none running") and "is not its watcher" not in watcher_note:
            # A watcher still running, or one that could not be proved to be it: a second one
            # beside it would make two closers for one browser.
            raise _ReattachAborted(f"the previous quit watcher could not be ended ({watcher_note})")
        return await _reattach_claimed(browser, instance_id, profile, record, watcher_note,
                                       url=url, keep_alive=keep_alive, ignored=ignored)
    except BaseException as exc:
        # Given back, so the browser can be taken by a later launch, as it could before. If the
        # old watcher was already ended, a new one takes its place first: the browser is never
        # left with nothing to end it on Quit or at the time cap.
        fields: dict[str, Any] = {"server_pid": record.get("server_pid"), "state": record.get("state")}
        if watcher_note == "ended" and not quit_watch.running(browser):
            proc = quit_watch.start(
                browser, _WINDOWLESS_GRACE, _CLOSE_WAIT,
                max_orphan_seconds=_keep_alive.cap_hours(record.get("opened_by")) * 3600,
                pages_matter=not headless, keep_alive_record=str(_keep_alive.record_path(profile)),
                tabs_file=str(_keep_alive.tabs_path(profile)))
            if proc is not None:
                fields.update(watcher_pid=proc.pid,
                              watcher_start=_keep_alive._watcher.process_start(proc.pid))
        failed = _keep_alive.write(profile, **fields)
        if failed and isinstance(exc, Exception):
            # The record still names this server, so others refuse the browser until it exits.
            raise _ReattachAborted(f"{exc}; and the record could not be given back ({failed})") from exc
        raise


# On a Browser reattached from a record the launcher wrote: the person's browser. Nothing of the
# agent's ends it (idle timeout) or acts on all of its tabs (block_ads).
_PERSONS_ATTR = "_mcp_persons_browser"


def _persons_browser(browser: Any) -> bool:
    return bool(getattr(browser, _PERSONS_ATTR, False))


async def _reuse_without_an_agent_tab(refusal: str, url: str, expect_url: Optional[str],
                                      expect_status: Optional[int], keep_alive: Optional[bool]) -> str:
    """browser_launch on a running browser whose active tab the tools may not use.

    With a url it opens in a new tab of the agent's, as a reattach does; without one the browser
    is left exactly as it is and the result says what the agent can do. Nothing is ended.
    """
    head = f"reusing the running browser ({len(BrowserState.tabs)} tab(s)), left as it was"
    keep_note = _keep_alive_on_reuse(keep_alive)
    if url and url != "about:blank":
        from .tools import navigation as _navigation
        opened = await _navigation.tab_new(url=url, expect_url=expect_url, expect_status=expect_status)
        if opened.startswith("Error: antibot_paused:"):
            return opened.replace(
                "Error: antibot_paused:",
                "Error: antibot_paused: browser is running and stays open; ", 1) + keep_note
        if opened.startswith("Error:"):
            detail = opened[len("Error: "):]
            if detail.startswith("Opened tab"):
                return err(f"{head}; {detail}")
            return err(f"{head}; {url} could not be opened in a new tab of the agent's: {detail}")
        return ok(f"{head}; none of its tabs is the agent's to use, so {url} {opened} "
                  f"(a new tab of the agent's){keep_note}")
    return ok(f"{head}: {refusal} Or pass url= to open a page in a new tab of the agent's.{keep_note}")


def _owned_before(record: dict, tabs: list) -> tuple[set, list]:
    """The tabs an earlier agent session owned, as far as they can count, and notes on any dropped.

    The record is a file any process running as this user can write, so it is no proof (such a
    process can drive the debugging port anyway). It fails safe instead: an entry counts only for
    the browser generation it was written for and only while that page target exists.
    """
    claimed = {str(k) for k in (record.get("owned") or [])}
    if not claimed:
        return set(), []
    if record.get("owned_for") != _keep_alive.generation(record):
        return set(), [f"{len(claimed)} tab(s) the record lists as an agent's were claimed for another "
                       f"browser (or by an older record), so they were not taken over"]
    present = {_target_key(t) for t in tabs}
    gone = claimed - present
    notes = [f"{len(gone)} tab(s) the record lists as an agent's are no longer open, so they were "
             f"dropped"] if gone else []
    return claimed & present, notes


def _keep_on_reattach(keep_alive: Optional[bool], record: dict) -> bool:
    """Whether a reattached browser stays at this server's exit. Asked: as asked. Left unset: as
    it was, where a record without "keep" was kept and an orphan was not; the person's browser
    (opened from the launcher) always stays, whatever keep False an earlier agent's one-off
    keep_alive=False left in its record (review of 0.25.2)."""
    if keep_alive is not None:
        return bool(keep_alive)
    return bool(record.get("keep", True)) or record.get("opened_by") == _keep_alive.LAUNCHER


async def _reattach_claimed(browser: Any, instance_id: str, profile: Path, record: dict,
                            watcher_note: str, *, url: str, keep_alive: Optional[bool],
                            ignored: list[str]) -> tuple[Any, list, int, dict, str, str]:
    pid, port = int(record["pid"]), int(record["port"])
    headless = bool(record.get("headless"))
    fingerprint = dict(record.get("fingerprint") or {})
    with contextlib.suppress(Exception):
        await asyncio.wait_for(browser.update_targets(), timeout=5)
    order = [str(k) for k in (record.get("tab_order") or [])]
    rank = {key: i for i, key in enumerate(order)}
    with contextlib.suppress(Exception):
        # In place, so the order survives every later update_targets and _refresh_tabs.
        browser.targets.sort(key=lambda t: rank.get(str(t.target.target_id), len(rank)))
    tabs = list(browser.tabs)
    opened_note = ""
    # A browser the person opened from the launcher holds the person's tabs: a reattach takes
    # over only what an agent opened there before (saved at its exit) and what it opens now.
    # Any other kept browser was an agent's, and every tab it holds is taken over as before.
    from_launcher = record.get("opened_by") == _keep_alive.LAUNCHER
    with contextlib.suppress(Exception):
        setattr(browser, _PERSONS_ATTR, from_launcher)
    agents_before, owned_notes = _owned_before(record, tabs)
    opened_now: set[str] = set()
    if not tabs:
        tabs = [await (_open_background_window(browser) if not headless
                       else browser.get("about:blank", new_tab=True))]
        opened_note = "; it had no page, so one was opened"
        opened_now.add(_target_key(tabs[0]))
    for tab in tabs:
        if from_launcher and _target_key(tab) not in agents_before | opened_now:
            continue  # the person's tab keeps the person's identity
        with contextlib.suppress(Exception):
            await _wait(_apply_fingerprint_to_tab(tab, fingerprint), timeout=5, what="identity")
    active_key = record.get("active")
    select = next((i for i, t in enumerate(tabs) if _target_key(t) == active_key), 0)
    observed: dict[str, Any] = {}
    if from_launcher and _target_key(tabs[select]) not in (agents_before | opened_now):
        # Not the person's tab they were last on: an agent's own one, if there is any.
        select = next((i for i, t in enumerate(tabs)
                       if _target_key(t) in (agents_before | opened_now)), select)
    if url and url != "about:blank":
        match = next((i for i, t in enumerate(tabs)
                      if str(getattr(t.target, "url", "") or "") == url
                      and (not from_launcher or _target_key(t) in agents_before)), None)
        if match is not None:
            select = match
            observed = await _observe_navigation(tabs[match], url, {"armed": False})
            observed["note"] = "already open in a tab of the reattached browser; selected, not reloaded"
        else:
            # A new tab, so no page the kept browser holds (a half-filled form) is navigated away.
            # In a browser the person opened, as a tab of their window: a window of the agent's
            # own opened exactly over theirs (same saved placement) and read as "my tabs are gone".
            in_their_window = from_launcher and not headless
            if headless:
                tab = await browser.get("about:blank", new_tab=True)
            elif in_their_window:
                tab = await _open_background_tab(browser)
            else:
                tab = await _open_background_window(browser)
            with contextlib.suppress(Exception):
                await _wait(_apply_fingerprint_to_tab(tab, fingerprint), timeout=5, what="identity")
            if in_their_window:
                with contextlib.suppress(Exception):
                    await _wait(_keep_painting_behind(tab), timeout=5, what="focus emulation")
            doc_state, disarm = await _arm_document_watch(tab)
            try:
                timeout_observation = await _commit_navigation(tab, url, doc_state)
                if (timeout_observation is None or
                        timeout_observation.get("_commit_timeout_recovered")):
                    await _wait_load_complete(tab, 3)
                    observed = _carry_nav_timeout_recovery(
                        await _observe_navigation(tab, url, disarm()), timeout_observation)
                else:
                    observed = timeout_observation
            finally:
                disarm()
            observed["_doc_state"] = doc_state
            tabs.append(tab)
            opened_now.add(_target_key(tab))
            select = len(tabs) - 1
            observed["note"] = ("opened as a new tab behind the one the person's window shows; their "
                                "tabs were left as they were, and tab_focus shows this one"
                                if in_their_window else
                                "opened in a new tab; the kept tabs were left as they were")
    elif from_launcher and _target_key(tabs[select]) not in (agents_before | opened_now):
        # Target-list metadata is enough to report the reattach; do not evaluate the person's page.
        observed = {"probe_error": "the selected tab belongs to the person and was not read",
                    "status_note": "no document navigation"}
    else:
        # Nothing navigated: read back the page the selected agent tab shows.
        observed = await _observe_navigation(tabs[select], None, {"armed": False})
    # The last await is behind: a browser_launch that browser_recover cut short meanwhile takes
    # nothing over, and the browser is given back as it was (_reattach_connected), never ended:
    # it may be the person's, opened from the launcher.
    cut = _launch_cut_reason()
    if cut:
        raise _ReattachAborted(cut)
    if not headless:
        _watch_windows(browser)  # replaced below when keep_alive stays on
    # Left unset, a reattach keeps what the browser had: whoever kept it wanted it kept. An
    # orphan was never kept, so it closes with this server unless keep_alive=True asks otherwise.
    orphan = record.get("state") == _keep_alive.ORPHAN
    if _keep_on_reattach(keep_alive, record):
        failed = _arm_keep_alive(browser, profile, instance_id, headless, fingerprint,
                                 previous=record)
        keep_text = failed or " [keep_alive: still on, so it stays open when this server exits too]"
    else:
        _disarm_keep_alive(browser, profile, headless)
        keep_text = (" [keep_alive=False: it was never kept, so it now closes when this server exits]"
                     if orphan and keep_alive is None else
                     " [keep_alive=False: it now closes when this server exits]")
    pages = [{"index": i, "url": tab_record.shown_url(str(getattr(t.target, "url", "") or "")),
              "title": str(getattr(t.target, "title", "") or "")[:120]} for i, t in enumerate(tabs)]
    details = {
        "reattached": {"pid": pid, "port": port, "chrome": record.get("chrome_version"),
                       "headless": headless, "left_running": _keep_alive.describe(record),
                       "previous_watcher": watcher_note, "tabs": pages, "selected": select},
    }
    notes = [f"no Chrome was started, so the profile version guard, the Chrome Beta choice and "
             f"the cookie move did not run"]
    marker = cookie_migration.read_marker(Path(_keep_alive.profile_key(profile)))
    if marker.get("state") == "pending":
        notes.append("a cookie move to v11 is still pending for this profile; it runs on the next "
                     "launch that starts Chrome, not on a reattach")
    given = {"brave_lean=True": True, "brave_lean=False": False}
    asked = next((given[item] for item in ignored if item in given), None)
    ignored = [item for item in ignored if item not in given]
    lean_note = _lean_on_reattach(brave_mode.product_of(browser), asked)
    if lean_note:
        notes.append(lean_note)
    if ignored:
        notes.append("not applied, the running browser keeps its own: " + ", ".join(ignored))
    keys = [_target_key(t) for t in tabs]
    owned = set(keys) if not from_launcher else {k for k in keys if k in agents_before | opened_now}
    notes.extend(owned_notes)
    if from_launcher:
        theirs = len(keys) - len(owned)
        notes.append("the agent shares this browser's cookie jar with the person: cookie_list, "
                     "cookie_export, storage_state_save and http_request_with_session read the person's "
                     "logins, and a cookie the agent sets is theirs too")
        notes.append(f"the person opened this browser from the launcher, so its tabs are theirs: tools "
                     f"act on the {len(owned)} tab(s) an agent opened (now or in an earlier session) and "
                     f"not on the person's {theirs} until tab_select picks one; tab_new or url= opens one "
                     f"of the agent's own")
        if keys[select] not in owned:
            notes.append("the selected tab is the person's, so the next tool refuses it until "
                         "tab_select or tab_new")
    details["reattached"]["notes"] = notes
    details["_owned"] = owned
    summary = f"reattached to the running browser (pid {pid}, {len(tabs)} tab(s){opened_note})"
    if orphan:
        summary = (f"took over a browser of ours left running when its server went (pid {pid}, "
                   f"{len(tabs)} tab(s){opened_note}): its logins and tabs are used, instead of an "
                   f"empty fallback profile")
    return browser, tabs, select, {**observed, **details}, summary, keep_text


async def _block_ads_on_reattach(block_ads: bool, browser: Any, instance_id: str) -> str:
    """block_ads on a reattached browser: a blocker of its own, as on a new one. The blocker of
    the server that kept it ended at that server's exit, so nothing blocks until a launch asks."""
    if not block_ads:
        return ""
    if _persons_browser(browser):
        return (" [block_ads: not attached: the person opened this browser from the launcher, and "
                "blocking acts on every one of their tabs too]")
    module = _adblock_module()
    # A reattached Brave's Config names nodriver's Chrome; its mark says what it is. Shields
    # blocks there, so block_ads stays a no-op that says so, as on a new Brave launch.
    executable = ("Brave Browser" if brave_mode.product_of(browser) == brave_mode.BRAVE
                  else getattr(getattr(browser, "config", None), "browser_executable_path", None))
    return await module.attach(module.begin(True, executable), browser, instance_id,
                               BrowserState.session_key())


async def _launch_by_reattach(profile: Path, verdict: dict, *, url: str, keep_alive: Optional[bool],
                             ignored: list[str], expect_url: Optional[str],
                             expect_status: Optional[int], block_ads: bool = False,
                             agent_overlay: Optional[bool] = None,
                             plan: Optional["_LaunchPlan"] = None) -> str:
    """browser_launch's reattach: the kept browser becomes the calling session's current one.

    Once it is recorded, it is the launch's browser (plan.browser): should browser_recover cut
    the launch short during the awaits that follow, the launch ends it as it ends one it
    started. A cut before it is recorded gives it back instead (_reattach_claimed)."""
    iid = plan.launching.instance_id if plan is not None else BrowserState.current_instance_id
    record = verdict["record"]
    ignored = [item for item in ignored if item != f"headless={bool(record.get('headless'))!r}"]
    try:
        browser, tabs, select, observed, summary, keep_text = await _reattach_kept(
            iid, profile, verdict, url=url, keep_alive=keep_alive, ignored=ignored)
    except _NotTheKeptBrowser as e:
        return err(f"the browser kept alive on {profile} ({_keep_alive.describe(record)}) changed "
                   f"between the check and the connection: {e}. Nothing was changed and the "
                   f"connection was dropped; browser_launch again to look afresh")
    except _ReattachAborted as e:
        return err(f"the browser kept alive on {profile} ({_keep_alive.describe(record)}) could not "
                   f"be taken over safely: {e}. Nothing was changed and the connection was dropped")
    except Exception as e:
        return err(f"a browser kept alive on {profile} ({_keep_alive.describe(record)}) is ours and "
                   f"free, but connecting to it failed: {_exc_text(e)}. It was left running as it "
                   f"was; browser_launch again to retry, or end it with Quit")
    owned = observed.pop("_owned", None)
    doc_state = observed.pop("_doc_state", None)
    BrowserState.browser = browser
    if plan is not None:
        plan.browser, plan.adopted = browser, True
        plan.profile_path = Path(_keep_alive.profile_key(profile))
    BrowserState.tabs = tabs
    BrowserState.owned_targets = ({BrowserState.COUNTED, *owned} if owned is not None
                                  else {_target_key(t) for t in tabs})
    BrowserState.adopted_targets = set()
    BrowserState.active_lost = ""
    BrowserState.active_tab_index = select
    BrowserState.current_profile_dir = Path(_keep_alive.profile_key(profile))
    BrowserState.current_headless = bool(record.get("headless"))
    BrowserState.current_privacy = False
    BrowserState.current_fingerprint = dict(record.get("fingerprint") or {})
    BrowserState.current_proxy = verdict.get("proxy")
    # Never ended for the agent's idleness: the person's browser is theirs to Quit.
    BrowserState.current_idle_timeout = (DEFAULT_IDLE_TIMEOUT if BrowserState.http_mode()
                                         and not _persons_browser(browser) else 0)
    BrowserState.current_last_active = time.time()
    BrowserState.current_created_at = time.time()
    if BrowserState.http_mode():
        _ensure_idle_reaper_running()
    _ATTACHED_BROWSERS.discard(iid)
    BrowserState.window_closed.pop(iid, None)
    if iid == "main":
        from .state import set_profile_affinity
        set_profile_affinity(BrowserState.current_profile_dir)
    observed["profile"] = {"actual_profile": str(BrowserState.current_profile_dir),
                           "session_state_source": (
                               "taken over: a browser of ours left running when its server went"
                               if (verdict.get("record") or {}).get("state") == _keep_alive.ORPHAN
                               else "reattached to the browser kept alive on it")}
    ads_note = await _block_ads_on_reattach(block_ads, browser, iid)
    ads_note += await presence.attach(browser, _browser_process_pid(browser), ask=agent_overlay,
                                headless=bool(record.get("headless")),
                                persons_browser=_persons_browser(browser))
    if "commit_timeout_seconds" in observed and not observed.get("_commit_timeout_recovered"):
        return err(_nav_commit_timeout_message(summary, observed))
    action = "Loaded" if url and url != "about:blank" else "Selected tab shows"
    trip = (await _tripwire_after_navigation(tabs[select], observed, doc_state=doc_state,
                                             headless=bool(record.get("headless")))
            if doc_state is not None and _target_key(tabs[select]) in BrowserState.owned_targets else None)
    return _tripwire_render(trip, "browser_launch", _nav_result(
        f"{summary[0].upper()}{summary[1:]}. {action}", observed,
        expect_url=expect_url, expect_status=expect_status, suffix=keep_text + ads_note),
        asked_for=url if url and url != "about:blank" else None, launched=True)


# browser_launch / spawn_browser parameters that only shape a new Chrome, with the value that
# means "not asked for". A reattach starts none, so any other value is named as not applied.
_NEW_CHROME_ONLY = {
    "headless": False, "proxy": None, "user_agent": None, "fingerprint": None,
    "storage_state_path": None, "testing_mode": False, "profile_mode": "default",
    "restore_tabs": False, "extra_args": None, "lang": "en-US", "profile_directory": None,
    "vertical_tabs": None, "brave_lean": None,
}


def _reattach_ignored(given: dict) -> list[str]:
    """Launch parameters a reattach cannot apply, named for the result."""
    out = []
    for name, default in _NEW_CHROME_ONLY.items():
        if name in given and given[name] not in (default, [], {}):
            out.append(f"{name}={given[name]!r}" if name not in ("proxy", "fingerprint", "user_agent")
                       else name)
    return out


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
    return getattr(browser, _CLOSING_ATTR, None) is not None


def _tab_limit(browser: Any) -> Optional[int]:
    """How many tabs a saved list keeps: a person's browser (opened from the launcher, in this
    process or reattached to) holds far more than the 30 an agent's list is cut to, and a
    server writing 30 over the 48 the person had open would lose the rest at the next start."""
    if _persons_browser(browser) or _keep_alive.OPENED_BY == _keep_alive.LAUNCHER:
        return tab_record.MAX_TABS_PERSON
    return None


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
    if _closing(browser):
        return "not recorded: an earlier close of this browser already recorded its tabs"
    _mark_closing(browser)
    keys = [_target_key(t) for t in tabs]
    active = keys[active_index] if 0 <= active_index < len(keys) else None
    try:
        pages = await asyncio.wait_for(_page_targets(browser), timeout=_TAB_RECORD_READ_TIMEOUT)
    except Exception:
        pages = None
    return tab_record.save(instance_id, scope, pages, keys, active, reason, force=True,
                           limit=_tab_limit(browser))


def _record_tabs_at_exit(instance_id: str, browser: Any, tabs: list, active_index: int,
                         profile: Any, privacy: bool) -> str:
    """The stdin-EOF form of _record_tabs_before_close: no event loop, so /json/list over HTTP."""
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None or browser is None or _browser_exited(browser):
        return "not recorded"
    config = getattr(browser, "config", None)
    pages = tab_record.pages_over_http(getattr(config, "host", None), getattr(config, "port", None),
                                       timeout=_TAB_RECORD_READ_TIMEOUT)
    keys = [_target_key(t) for t in tabs]
    active = keys[active_index] if 0 <= active_index < len(keys) else None
    return tab_record.save(instance_id, scope, pages, keys, active, "server exit", force=True,
                           limit=_tab_limit(browser))


async def _record_tabs_at_launch(instance_id: str, launching: Optional["_Launching"] = None) -> str:
    """The first record of a browser, written as its launch returns. The quit watcher ends a
    browser once its last window is gone, when nothing is left to read, so a Quit before the
    background writer's first pass (about 2.3 s after the call) would leave no list at all.
    One read, capped like a close's; never raises, and an empty list replaces nothing. For
    browser_launch (`launching`), nothing is written once the launch was cut short while the
    pages were read."""
    try:
        view = _instance_view(instance_id)
        if view is None:
            return "not recorded"
        browser, keys, active, profile, privacy = view
        scope = _tab_record_scope(instance_id, profile, privacy)
        if scope is None or _closing(browser):
            return "not recorded"
        pages = await asyncio.wait_for(_page_targets(browser), timeout=_TAB_RECORD_READ_TIMEOUT)
        if launching is not None and not _launch_owned(None, launching):
            return "not recorded: the launch was cut short while its pages were read"
        return tab_record.save(instance_id, scope, pages, keys, active, "at launch", force=False,
                               limit=_tab_limit(browser))
    except Exception as exc:
        return f"not recorded: {_exc_text(exc)}"


def _browser_exited(browser: Any) -> bool:
    """Whether a browser this server drives is known to have ended. A reattached one was not
    spawned here, so nodriver's `stopped` (no subprocess means stopped) says nothing about it."""
    if getattr(browser, "_process", None) is None:
        pid = getattr(browser, "_process_pid", None)
        if isinstance(pid, int) and pid > 0:
            return not _process_exists(pid)
    return getattr(browser, "stopped", False) is True


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
    return tab_record.save(instance_id, scope, second, keys, active, "while running", force=False,
                           limit=_tab_limit(browser))


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


async def _active_tab_closed_check(_name: str) -> None:
    await _check_active_tab_alive()


_app_module.BEFORE_TOOL_HOOKS[:] = [
    hook for hook in _app_module.BEFORE_TOOL_HOOKS
    if getattr(hook, "__name__", "") != "_active_tab_closed_check"] + [_active_tab_closed_check]


def _adblock_module() -> Any:
    """block_ads's module, imported on first use: a launch without it never loads it, nor the
    adblock package it uses."""
    from . import adblock
    return adblock


def _adblock_if_loaded() -> Any:
    """block_ads's module when some launch has loaded it, else None. Nothing is imported."""
    return sys.modules.get(f"{__package__}.adblock")


def _adblock_notice(session_key: str, result: str) -> str:
    """The notice of a block_ads that stopped, for the next tool result. Only once block_ads
    was used: before that there is nothing to tell, and nothing is imported."""
    module = _adblock_if_loaded()
    return result if module is None else module.result_notice(session_key, result)


if _adblock_notice not in _app_module.RESULT_FILTERS:
    _app_module.RESULT_FILTERS.append(_adblock_notice)


# agent_overlay: the refusal while the person holds the browser, what the agent is doing, and
# the note of a pause for the person's typing. Each is a no-op for a browser without an overlay.
def _overlay_gate(name: str) -> Optional[str]:
    return presence.refusal(name, BrowserState.browser)


async def _overlay_before_tool(name: str) -> None:
    browser = BrowserState.browser
    if presence.of(browser) is None:
        return
    tab = None
    with contextlib.suppress(Exception):
        tab = BrowserState.active_tab()
    await presence.before_tool(name, browser, tab)


def _overlay_after_tool(_name: str) -> None:
    browser = BrowserState.browser
    tab = None
    if presence.of(browser) is not None:
        with contextlib.suppress(Exception):
            tab = BrowserState.active_tab()
    presence.after_tool(browser, tab)


def _overlay_note(session_key: str, result: str) -> str:
    return presence.result_note(session_key, result, BrowserState.browser)


for _hooks, _hook in ((_app_module.CALL_GATES, _overlay_gate),
                      (_app_module.CALL_GATES, _launch_gate),
                      (_app_module.BEFORE_TOOL_HOOKS, _overlay_before_tool),
                      (_app_module.AFTER_TOOL_HOOKS, _overlay_after_tool),
                      (_app_module.RESULT_FILTERS, _overlay_note)):
    _hooks[:] = [h for h in _hooks if getattr(h, "__name__", "") != _hook.__name__] + [_hook]


async def _load_saved_tab(tab: Any, url: str) -> dict:
    doc_state, disarm = await _arm_document_watch(tab)
    try:
        await asyncio.wait_for(tab.get(url), timeout=BROWSER_NAV_TIMEOUT)
        await _wait_load_complete(tab, 10)
        return await _observe_navigation(tab, url, disarm())
    finally:
        disarm()


# The Dock launcher sets this: a person's browser comes back as one window with its tabs, not as
# one window per page. An agent's launch keeps a window per page, so each one reads as visible.
RESTORE_AS_TABS = False
_RESTORE_PARALLEL = 8


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
            if headless:
                opened = await _wait(browser.get("about:blank", new_tab=True), timeout=10,
                                     what="open a tab")
            elif RESTORE_AS_TABS:
                opened = await _wait(_open_background_tab(browser), timeout=10, what="open a tab")
            else:
                opened = await _wait(_open_background_window(browser, window_width, window_height),
                                     timeout=10, what="open a window")
            await _apply_fingerprint_to_tab(opened, fingerprint)
            slot["tab"] = opened
        except Exception as exc:
            slot["error"] = f"could not open a tab: {_exc_text(exc)}"
    loads = [s for s in slots if s["tab"] is not None]
    # A person's list can hold dozens of pages: a few at a time, not all at once.
    gate = asyncio.Semaphore(_RESTORE_PARALLEL)

    async def load(slot: dict) -> Any:
        async with gate:
            return await _load_saved_tab(slot["tab"], slot["url"])

    results = await asyncio.gather(*(load(s) for s in loads), return_exceptions=True)
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


@dataclass(frozen=True)
class _LaunchRequest:
    """What the caller of browser_launch asked for, as `_launch_check_arguments` left it: a
    --user-data-dir in extra_args is user_data_dir here. Every phase reads it, none changes it.

    A reattach names what it cannot apply from these fields (`_reattach_ignored(vars(req))`),
    which hold the same values browser_launch's own locals held at that point."""
    url: str
    headless: bool
    proxy: Optional[str]
    user_agent: Optional[str]
    window_width: int
    window_height: int
    lang: str
    extra_args: Optional[list[str]]
    storage_state_path: Optional[str]
    testing_mode: bool
    auto_verify: bool
    user_data_dir: Optional[str]
    profile_directory: Optional[str]
    fingerprint: Optional[dict]
    privacy: bool
    profile_conflict: str
    expect_url: Optional[str]
    expect_status: Optional[int]
    profile_mode: str
    restore_tabs: bool
    keep_alive: Optional[bool]
    vertical_tabs: Optional[str]
    block_ads: bool
    brave_lean: Optional[bool]
    agent_overlay: Optional[bool]
    product: str
    brave_executable: Any


@dataclass
class _LaunchPlan:
    """What a new launch decides before Chrome starts, handed from phase to phase of
    browser_launch. Each field is set by the phase named beside it and read by later ones.

    `persistent` ends as the result reports it: privacy=True and an explicit user_data_dir
    turn it off."""
    persistent: Any
    # Effective extra_args: the profile phases may inject --profile-directory into them.
    merged_extra_args: list
    effective_proxy: Optional[str] = None  # _launch_privacy_and_identity
    effective_fingerprint: Optional[dict] = None
    identity_source: str = ""
    # Why a browser kept alive on the profile asked for was not taken over, for the result.
    kept_note: str = ""  # _launch_kept_browser
    explicit_udd: Optional[Path] = None  # _launch_explicit_profile
    profile_path: Optional[Path] = None  # the profile phases; _launch_config for privacy
    used_fallback: bool = False
    profile_info: Optional[dict] = None  # _launch_server_profile: the resolver's answer
    tidy_profile: Optional[Path] = None
    # Decided before anything writes to the profile; see _launch_choice.
    launch_choice: Optional[_LaunchChoice] = None
    privacy_profile: Optional[Path] = None  # _launch_config
    final_udd: Optional[str] = None
    config: Optional[Config] = None
    dock_note: str = ""  # _launch_prepare_profile on: notes for the result
    cookie_move: Any = None  # _launch_prepare_profile
    previous_app: Optional[int] = None  # _launch_keep_keyboard
    # What this launch has written in the profile, in order, for a refusal after the commit.
    written: tuple = ()  # _launch_commit_profile
    # The tidy-up ran (clean_profile_state, the window-state wipe). Kept apart from `written`:
    # either can change nothing, and the wipe's own failure is suppressed.
    tidied: bool = False  # _launch_commit_profile
    # Who owns the launch: its marker (instance id, task) and the browser it started or took
    # over. Every step after the start, and every cleanup, acts on that pair, never on what is
    # current by then.
    launching: Any = None  # browser_launch
    browser: Any = None  # browser_launch, from _launch_start_chrome; _launch_by_reattach
    adopted: bool = False  # _launch_ready_browser made it BrowserState.browser
    ended: bool = False  # _launch_end_started_browser ran: no later step ends the browser again
    cancel_note: str = ""  # _launch_cancelled: the privacy profile it could not remove


def _launch_check_arguments(browser: str, vertical_tabs: Optional[str], user_data_dir: Optional[str],
                            extra_args: Optional[list[str]]) -> tuple:
    """browser_launch, first: refuse a request that cannot work, before anything else runs.

    Returns (product, brave_executable, user_data_dir, extra_args, problem). A --user-data-dir
    in extra_args comes back as user_data_dir, so every later check looks at the folder the
    browser gets."""
    product, brave_executable, problem = brave_mode.check_request(browser, vertical_tabs)
    if problem:
        return product, brave_executable, user_data_dir, extra_args, problem
    user_data_dir, extra_args, problem = _profile_from_extra_args(user_data_dir, extra_args, "user_data_dir")
    if problem:
        return product, brave_executable, user_data_dir, extra_args, problem
    return product, brave_executable, user_data_dir, extra_args, _mock_keychain_refusal(extra_args)


def _launch_default_persistent(persistent: Optional[bool], testing_mode: bool) -> Optional[bool]:
    """persistent left unset is True, except under testing_mode; a value passed stays as passed."""
    # Local testing wants a clean room by default: a testing_mode run that
    # silently accumulated state in the real login profile was both a surprise
    # and a slow way to make tests depend on each other.
    if persistent is None:
        return not testing_mode
    return persistent


async def _launch_end_stale_browser() -> Optional[str]:
    """Before the launch lock: end a browser whose handle outlived its tabs, and remove the
    privacy profile the last browser left. Returns an error result, or None to go on."""
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
    return None


async def _launch_reuse_running(req: _LaunchRequest) -> Optional[str]:
    """Inside the launch lock: answer from the browser already running, once CDP answers.

    Returns the result, or None to start a new browser: none runs, or its handle was stale
    and that browser was ended."""
    # Re-check inside the lock (race with another concurrent call).
    # Reusing a live browser is not just an optimisation: starting a second
    # Chrome against the same profile is what produces "CDP unreachable",
    # and the half-started process is what gets orphaned.
    if not BrowserState.is_up():
        return None
    _running = brave_mode.product_of(BrowserState.browser)
    if _running != req.product and _running != brave_mode.UNKNOWN:
        return err(
            f"{brave_mode.name(_running)} is already running as instance "
            f"{BrowserState.current_instance_id!r}; browser={req.product!r} was not applied and "
            f"nothing changed. browser_close() first, or spawn_browser(instance_id=..., "
            f"browser={req.product!r}) for one alongside it.")
    _answered = False
    try:
        # is_up() only proves a handle exists. The socket can be gone
        # while Chrome still runs, so make CDP actually answer.
        _tab = BrowserState.active_tab()
        await _wait(_tab.evaluate("1", return_by_value=True),
                    timeout=5, what="reuse probe")
        _answered = True
        reuse_head = f"reusing the running browser ({len(BrowserState.tabs)} tab(s))"
        if req.restore_tabs:
            reuse_head += ("; restore_tabs NOT applied, the running browser keeps its own "
                           "tabs and nothing was reopened")
        if req.profile_mode != "default":
            reuse_head += (f"; profile_mode={req.profile_mode!r} NOT applied, it only "
                           f"takes effect on a new launch")
        _reuse_keep_note = _keep_alive_on_reuse(req.keep_alive)
        if req.vertical_tabs is not None:
            reuse_head += (f"; vertical_tabs={req.vertical_tabs!r} NOT applied, it only "
                           f"takes effect on a new launch")
        if req.brave_lean is not None:
            reuse_head += (f"; brave_lean={req.brave_lean!r} NOT applied, it only "
                           f"takes effect on a new launch")
        if req.block_ads:
            reuse_head += _adblock_module().reuse_note(req.block_ads, BrowserState.browser)
        reuse_head += await presence.attach(
            BrowserState.browser, _browser_process_pid(BrowserState.browser),
            ask=req.agent_overlay, headless=BrowserState.current_headless,
            persons_browser=_persons_browser(BrowserState.browser))
        _doc_state, _disarm = await _arm_document_watch(_tab)
        try:
            timeout_observation = None
            if req.url and req.url != "about:blank":
                timeout_observation = await _commit_navigation(_tab, req.url, _doc_state)
                if (timeout_observation is not None and
                        not timeout_observation.get("_commit_timeout_recovered")):
                    return err(_nav_commit_timeout_message(reuse_head, timeout_observation))
                try:
                    await _wait(_tab.wait(), timeout=10, what="reuse settle")
                except Exception:
                    pass
            _observed = _carry_nav_timeout_recovery(
                await _observe_navigation(_tab, req.url, _disarm()), timeout_observation)
        finally:
            _disarm()
        _observed["profile"] = {
            "actual_profile": str(BrowserState.current_profile_dir)
            if BrowserState.current_profile_dir else None,
            "session_state_source": "reused running browser — profile unchanged",
        }
        # A second launch on a browser still on the verification page says so in a line.
        _trip = await _tripwire_after_navigation(_tab, _observed, doc_state=_doc_state,
                                                 headless=BrowserState.current_headless)
        return _tripwire_render(_trip, "browser_launch", _nav_result(
            reuse_head, _observed,
            expect_url=req.expect_url, expect_status=req.expect_status, suffix=_reuse_keep_note,
        ), asked_for=req.url if req.url and req.url != "about:blank" else None, launched=True)
    except TabNotUsable as refusal:
        # The browser is fine; only its active tab is not one the tools may use (the
        # person's, or none is selected). Never the stale-handle path below: that ends
        # the browser, and on one the person opened it is theirs.
        return await _reuse_without_an_agent_tab(str(refusal), req.url, req.expect_url,
                                                 req.expect_status, req.keep_alive)
    except Exception as e:
        # The browser answered, so what failed was the navigation: a bad or slow
        # URL. Ending it here would lose every tab over a typo.
        if _answered and not transport_lost(str(e)):
            return err(
                f"the running browser could not load {req.url}: {_exc_text(e)}. "
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
    return None


def _launch_privacy_and_identity(req: _LaunchRequest, plan: _LaunchPlan) -> Optional[str]:
    """Refuse privacy mode over a caller's profile, and settle the proxy and fingerprint this
    browser gets (plan.effective_*). Returns an error result, or None to go on."""
    # A --user-data-dir in extra_args is req.user_data_dir by now (_profile_from_extra_args).
    if req.privacy and (req.user_data_dir or req.profile_directory):
        return err("privacy mode cannot reuse user_data_dir or profile_directory")
    if req.privacy:
        plan.persistent = False
    try:
        plan.effective_proxy, plan.effective_fingerprint, plan.identity_source = _launch_identity(
            BrowserState.current_instance_id,
            proxy=req.proxy,
            user_agent=req.user_agent,
            fingerprint=req.fingerprint,
            privacy=req.privacy,
        )
    except Exception as e:
        return err(f"identity setup failed: {e}")
    return None


async def _launch_explicit_profile(req: _LaunchRequest, plan: _LaunchPlan) -> Optional[str]:
    """The caller's user_data_dir: it must exist and be free (or held by a browser of ours that
    was kept, which is taken over), its sub-profile must exist, and the browser chosen for it
    must pass `_profile_version_refusal`. Nothing is written to the folder here.

    Returns the reattach's result or a refusal, or None to go on with the plan filled in."""
    explicit_udd = plan.explicit_udd = Path(req.user_data_dir).expanduser()
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
        kept = _kept_verdict(explicit_udd, req.product)
        if kept and kept.get("ok"):
            return await _launch_by_reattach(
                explicit_udd, kept, url=req.url, keep_alive=req.keep_alive,
                ignored=_reattach_ignored(vars(req)), expect_url=req.expect_url,
                expect_status=req.expect_status,
                block_ads=req.block_ads, agent_overlay=req.agent_overlay, plan=plan)
        return err(
            f"user_data_dir {explicit_udd} is currently in use by Chrome PID {holder_pid}. "
            "Close that Chrome window first (a single Chrome process locks the whole "
            "user-data-dir, regardless of profile_directory). "
            "Or use clone_chrome_profile to take a snapshot copy of the profile, "
            "or attach_to_chrome(port=...) if Chrome was started with --remote-debugging-port."
            + _not_reattached_note(explicit_udd, kept)
        )
    # Validate sub-profile if specified
    if req.profile_directory:
        try:
            pd_path = _direct_child(
                explicit_udd, req.profile_directory, "profile_directory"
            )
        except ValueError as e:
            return err(str(e))
        if not pd_path.exists():
            avail = [p.name for p in explicit_udd.iterdir()
                     if p.is_dir() and (p / "Preferences").exists()][:15]
            return err(
                f"profile_directory {req.profile_directory!r} not found inside {explicit_udd}. "
                f"Available: {avail}"
            )
        plan.merged_extra_args.append(f"--profile-directory={req.profile_directory}")
    else:
        plan.merged_extra_args.extend(_profile_directory_args(explicit_udd, plan.merged_extra_args))
    plan.launch_choice = _launch_choice(explicit_udd, req.headless, plan.merged_extra_args,
                                        req.brave_executable)
    refused = _profile_version_refusal(explicit_udd, plan.launch_choice.executable, req.product)
    if refused:
        return err(refused)
    plan.persistent = False  # we're managing user_data_dir ourselves; skip default profile flow
    plan.profile_path = explicit_udd
    plan.used_fallback = False
    return None


async def _launch_kept_browser(req: _LaunchRequest, plan: _LaunchPlan) -> Optional[str]:
    """No user_data_dir: take over a browser a previous server of ours kept on the profile this
    launch asks for. A --user-data-dir in extra_args never reaches here: _profile_from_extra_args
    made it user_data_dir, whose lock _launch_explicit_profile checks.

    Returns the reattach's result, or None to go on to the profile resolver; plan.kept_note
    says why a kept browser was not taken over."""
    # A browser a previous server of ours kept alive on the profile this launch asks
    # for is taken over, not raced: falling back would hide its tabs and logins.
    if plan.persistent:
        # The launch's own instance, never read again after an await (_Launching).
        iid = plan.launching.instance_id
        _own_folder = _server_profile_for(iid, req.product)
        for _candidate in _kept_candidates(iid, req.product):
            # Only the folder the server chose itself: a caller's --user-data-dir that
            # became main's affinity after a custom reattach is never taken as an orphan.
            _adopt = iid if _keep_alive.profile_key(_candidate) == _own_folder else None
            _kept = _kept_verdict(_candidate, req.product, adopt_orphan_for=_adopt)
            if _kept and _kept.get("ok"):
                _taken = await _launch_by_reattach(
                    _candidate, _kept, url=req.url, keep_alive=req.keep_alive,
                    ignored=_reattach_ignored(vars(req)), expect_url=req.expect_url,
                    expect_status=req.expect_status,
                    block_ads=req.block_ads, agent_overlay=req.agent_overlay, plan=plan)
                if not (_taken.startswith("Error") and _orphan_given_back(_candidate, _kept)):
                    return _taken
                # An orphan that could not be taken over: the launch goes on as it did
                # before the takeover existed (a fallback, saying so), not an error on
                # every launch until that Chrome ends.
                plan.kept_note += (f" [a browser of ours left running on {_candidate} could not "
                                   f"be taken over: {_taken[len('Error: '):][:300]}]")
                continue
            plan.kept_note += _not_reattached_note(_candidate, _kept)
    return None


def _launch_server_profile(req: _LaunchRequest, plan: _LaunchPlan, resolved_profile: Optional[Path],
                           profile_info: dict) -> Optional[str]:
    """The profile the resolver chose for this instance: main's affinity, then for a persistent
    one the lock, the browser chosen for it and `_profile_version_refusal`. Nothing is written
    to the folder here.

    Returns a refusal, or None to go on with the plan filled in."""
    plan.profile_info = profile_info
    if profile_info.get("error"):
        return err(profile_info["error"])
    # None for an ephemeral launch. Recording the shared profile here instead made
    # browser_close scan it and signal whichever Chrome really was using it: the
    # browser of another session, with its logins.
    plan.profile_path = resolved_profile
    plan.used_fallback = bool(plan.persistent) and "conflict_with_pid" in profile_info
    if plan.persistent and BrowserState.current_instance_id == "main" and req.product == brave_mode.CHROME:
        from .state import set_profile_affinity
        set_profile_affinity(plan.profile_path)

    # Friendly upfront check on the default profile too (after resolve_default_profile
    # has potentially picked a fallback). If somehow the resolved one is still locked
    # by a live process, refuse with actionable error rather than hanging.
    if plan.persistent:
        holder_pid = chrome_lock_holder_pid(plan.profile_path)
        if holder_pid is not None:
            return err(
                f"Default MCP profile is locked by PID {holder_pid}: a browser is using it, usually "
                f"the one another mcp-stealth-chrome session started. Close that browser "
                f"(browser_close in that session, or quit its window) and retry, or use "
                f"persistent=False for an ephemeral throwaway profile."
            )
        # Before the window-state wipe below writes to it: a refused profile stays as it was.
        plan.launch_choice = _launch_choice(plan.profile_path, req.headless, plan.merged_extra_args,
                                            req.brave_executable)
        refused = _profile_version_refusal(plan.profile_path, plan.launch_choice.executable, req.product)
        if refused:
            return err(refused)
        # clean_profile_state and the window-state wipe write to the profile, so they
        # wait until the cookie checks below have passed: a refusal writes nothing.
        plan.tidy_profile = plan.profile_path
    return None


def _launch_config(req: _LaunchRequest, plan: _LaunchPlan) -> None:
    """The folder the browser gets (a privacy launch makes its new profile here) and the flags
    it starts with, into plan.config."""
    # Determine final user_data_dir for Config (None = nodriver picks a temp dir).
    if req.privacy:
        plan.privacy_profile = _new_privacy_profile(BrowserState.current_instance_id)
        plan.profile_path = plan.privacy_profile
        plan.final_udd = str(plan.privacy_profile)
    elif plan.explicit_udd is not None:
        plan.final_udd = str(plan.explicit_udd)
    elif plan.persistent:
        plan.final_udd = str(plan.profile_path)
    else:
        plan.final_udd = None

    # nodriver 0.50 never passes Config.lang on, and macOS Chrome ignores --lang. The
    # override applied to the tab below is what actually changes it.
    if req.lang and req.lang != "en-US" and not plan.effective_fingerprint.get("accept_language"):
        plan.effective_fingerprint = {**plan.effective_fingerprint, "accept_language": req.lang}
    config = plan.config = Config(
        user_data_dir=plan.final_udd,
        headless=req.headless,
        lang=(plan.effective_fingerprint.get("accept_language", "").split(",")[0] or req.lang),
        browser_args=list(plan.merged_extra_args),
    )
    # Extra flags to suppress any first-run / restore / notification interrupts
    config.add_argument("--hide-crash-restore-bubble")
    config.add_argument("--disable-session-crashed-bubble")
    config.add_argument("--disable-restore-session-state")
    config.add_argument("--no-default-browser-check")
    for _flag in _LEAN_FLAGS:
        config.add_argument(_flag)
    if plan.effective_fingerprint.get("user_agent"):
        config.add_argument(f"--user-agent={plan.effective_fingerprint['user_agent']}")
    if plan.effective_proxy:
        config.add_argument(f"--proxy-server={plan.effective_proxy}")
    config.add_argument(f"--window-size={req.window_width},{req.window_height}")
    if req.headless:
        _screen_arg = _headless_screen_arg(req.window_width, req.window_height, plan.merged_extra_args)
        if _screen_arg:
            config.add_argument(_screen_arg)
    # Force a deterministic on-screen position. Without this, Chrome
    # restores its last saved position from the persistent profile —
    # which after a macOS sleep/wake cycle is often off-screen or 0×0,
    # making the window invisible and CDP Browser.getWindowForTarget
    # return -32000 "window not found". Override unless the user
    # already supplied their own --window-position via extra_args.
    if not any((a or "").startswith("--window-position=")
                for a in plan.merged_extra_args):
        config.add_argument("--window-position=100,100")
    if req.profile_mode == "light":
        for _flag in LIGHT_MODE_FLAGS:
            config.add_argument(_flag)
    if req.testing_mode:
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


def _headless_native_user_agent(config: Config) -> Optional[str]:
    """The reduced UA Chrome uses globally when headless has no explicit identity."""
    version = profile_version.binary_version(str(getattr(config, "browser_executable_path", "") or ""))
    if version is None:
        return None
    return reduced_user_agent(profile_version.text(version), native_family())


def _apply_headless_native_user_agent(config: Config, headless: bool,
                                      fingerprint: Optional[dict]) -> str:
    """Set the process UA for worker scopes, or say why that protection was unavailable."""
    if (not headless or (fingerprint or {}).get("user_agent")
            or any((arg or "").startswith("--user-agent=") for arg in config.browser_args)):
        return ""
    native_ua = _headless_native_user_agent(config)
    if native_ua:
        config.add_argument(f"--user-agent={native_ua}")
        return ""
    return (" [identity: headless default UA was NOT replaced because Chrome's version could not "
            "be read; SharedWorker and ServiceWorker may expose HeadlessChrome]")


async def _launch_prepare_profile(req: _LaunchRequest, plan: _LaunchPlan) -> Optional[str]:
    """Pick the executable (the Dock icon's Beta or not), refuse it when it may not open the
    profile, prepare the cookie move, and check that vertical_tabs can be written (the write
    waits for the commit). Nothing here writes in the profile. The notes go to plan.dock_note.

    Returns a refusal (the privacy profile removed), or None to go on."""
    config = plan.config
    if plan.launch_choice is None:
        plan.launch_choice = _launch_choice(plan.profile_path, req.headless, plan.merged_extra_args,
                                            req.brave_executable)
    plan.dock_note = await _use_dock_icon_browser(config, req.headless, plan.launch_choice)
    plan.dock_note += _apply_headless_native_user_agent(
        config, req.headless, plan.effective_fingerprint)
    refused = _profile_version_refusal(plan.profile_path, config.browser_executable_path, req.product)
    if refused:
        await _remove_privacy_profile(plan.privacy_profile)
        return err(refused)
    with contextlib.suppress(Exception):
        plan.dock_note += profile_version.upgrade_note(plan.profile_path, config.browser_executable_path)
    cookie_move = plan.cookie_move = await _prepare_cookie_move(plan.profile_path, config)
    if cookie_move.refuse:
        await _remove_privacy_profile(plan.privacy_profile)
        return err(cookie_move.refuse)
    if cookie_move.flag:
        config.add_argument(_cookie_flag_arg(config))
    if not cookie_move.backup:
        plan.dock_note += cookie_move.note
    try:
        brave_mode.check_vertical_tabs(Path(config.user_data_dir), config.browser_args, req.vertical_tabs)
    except Exception as e:
        await _remove_privacy_profile(plan.privacy_profile)
        return err(f"vertical_tabs could not be written into the profile, so nothing was "
                   f"started: {_exc_text(e)}")
    return None


async def _launch_keep_keyboard(req: _LaunchRequest, plan: _LaunchPlan) -> None:
    """A headed browser must not take the keyboard: note the app in front, to give it back,
    and start without a window of its own."""
    # Read before Chrome starts, while the person's app is still the frontmost one.
    plan.previous_app = None if req.headless else await asyncio.to_thread(_frontmost_pid)
    if not req.headless:
        for _flag in _BACKGROUND_WINDOW_FLAGS:
            plan.config.add_argument(_flag)


def _launch_written(plan: _LaunchPlan) -> Optional[str]:
    """What this launch had done to the profile, for a refusal that comes after the commit; None
    when nothing was. A write is said as written; the tidy-up only as run, since it may change
    nothing."""
    done = []
    if plan.written:
        done.append(f"written {', '.join(plan.written)} in it")
    if plan.tidied:
        done.append("run its tidy-up of stale locks and window state")
    return f"this launch had already {' and '.join(done)}" if done else None


async def _launch_commit_profile(req: _LaunchRequest, plan: _LaunchPlan) -> Optional[str]:
    """The first writes to the profile, only once the cookie checks have passed: the cookie
    move's commit, the vertical tabs and brave_lean settings, then the tidy-up of locks and
    window state. Returns a refusal (the privacy profile removed), or None to go on.

    vertical_tabs was checked before the preflight (_launch_prepare_profile), but that check only
    reads: any failure of the write itself (a profile that changed since, a folder that cannot
    be written, a full disk) comes after the commit, and its refusal says what the cookie step
    had already committed; a pending marker is what the next launch resumes, as after a crash
    there. plan.written records each write, for the last look's refusal."""
    # The preflight, the commit, the settings and the tidy-up stay in this order: a refusal
    # at the preflight or the commit writes nothing.
    refused = await _cookie_preflight(plan.profile_path, plan.cookie_move)
    if not refused and _launch_lost(plan):
        return None  # cut short while it looked: nothing is committed (_launch_settle says so)
    refused = refused or await _commit_cookie_move(plan.profile_path, plan.cookie_move)
    if refused:
        await _remove_privacy_profile(plan.privacy_profile)
        return err(refused)
    move = plan.cookie_move
    if getattr(move, "marker", None) is not None:
        plan.written += ("the cookie move's pending marker"
                         + (" and the removal of the v10 cookies its backup holds" if move.strip else ""),)
    if _launch_lost(plan):
        return None  # cut short during the commit: no setting is written (_launch_settle says what was)
    config = plan.config
    try:
        plan.dock_note += _browser_note(config, req.product) + brave_mode.write_vertical_tabs(
            Path(config.user_data_dir), config.browser_args, req.product, req.vertical_tabs)
    except Exception as e:
        await _remove_privacy_profile(plan.privacy_profile)
        committed = (f" ({', '.join(plan.written)}; the next launch resumes the move)" if plan.written
                     else " (it had nothing to write)")
        return err(f"vertical_tabs could not be written into the profile, so nothing was "
                   f"started: {_exc_text(e)}. The cookie step had already been committed{committed}.")
    if req.vertical_tabs is not None:
        plan.written += ("the vertical tabs setting",)
    plan.dock_note += _apply_brave_lean(config, req.product, req.brave_lean,
                                        server_chosen=plan.explicit_udd is None)
    lean = getattr(config, brave_lean.ATTR, None)
    if lean is not None and (getattr(lean, "written", None) or getattr(lean, "restored", None)):
        plan.written += ("brave_lean's settings",)
    if plan.tidy_profile is not None:
        plan.tidied = True
        clean_profile_state(plan.tidy_profile)  # only clears stale locks
        # Selectively wipe corrupt window-placement / session-restore state. Idempotent and
        # cheap (a few file writes), preserves cookies and logins. Saves us from "window
        # 0x0, hidden" bugs that survive macOS sleep/wake cycles. See state.wipe_window_state.
        with contextlib.suppress(Exception):
            from .state import wipe_window_state as _wws
            _wws(plan.tidy_profile)
    return None


async def _launch_start_chrome(req: _LaunchRequest, plan: _LaunchPlan) -> tuple:
    """Start the browser on plan.config, after a last look at the profile's cookie databases.

    Returns (error result or None, the browser, the ad blocker waiting for it). A start that
    failed ends what it started and removes the privacy profile."""
    # The last look, as close to the start as this server can make it; Chrome takes the lock
    # about half a second later, and that gap is a known limit (no lock it honours can be
    # held across it). Its refusal names what this launch had written by then.
    refused = await _cookie_preflight(plan.profile_path, plan.cookie_move, written=_launch_written(plan))
    if refused:
        return err(refused + await _launch_remove_privacy(plan)), None, None
    if plan.launching is not None and not _launch_owned(None, plan.launching):
        return None, None, None  # cut short during the last look: nothing starts (_launch_settle)
    ads_pending = (_adblock_module().begin(req.block_ads, plan.config.browser_executable_path)
                   if req.block_ads else None)
    browser: Optional[Browser] = None
    try:
        browser = await _start_chrome(plan.config)
        _mark_allow_origins(browser, plan.config)
        brave_mode.mark(browser, req.product)
        if not req.headless:
            await _open_background_window(browser, req.window_width, req.window_height)
    except asyncio.TimeoutError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(plan.privacy_profile)
        return err(
            f"launch timed out after {BROWSER_LAUNCH_TIMEOUT}s — "
            f"profile {plan.profile_path} may be locked by another Chrome, or "
            f"Chrome is hung. Kill any stale Chrome processes and retry."
        ), None, None
    except asyncio.CancelledError:
        await _safe_stop_browser(browser)
        await _remove_privacy_profile(plan.privacy_profile)
        raise
    except Exception as e:
        # "CDP unreachable" means the process very likely STARTED and only
        # the websocket failed — so _safe_stop_browser, which speaks CDP,
        # cannot end it. Reaping our own PID here is the difference between
        # a failed launch and a failed launch plus an orphan nobody owns.
        reap = getattr(e, "reap_report", {})
        await _remove_privacy_profile(plan.privacy_profile)
        # Diagnose common nodriver "Failed to connect to browser" — usually
        # means Chrome started but its CDP websocket couldn't be reached
        # (profile locked, port collision, AV interference).
        msg = str(e)
        hint = ""
        if "Failed to connect" in msg or "websocket" in msg.lower() or "connection refused" in msg.lower():
            holder = chrome_lock_holder_pid(plan.profile_path) if plan.persistent else None
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
                parts.append(f"→ Profile {plan.profile_path} is locked by PID {holder}.")
            elif ext:
                parts.append(f"→ Other Chrome processes running: PIDs {ext[:5]}{'...' if len(ext)>5 else ''}.")
            parts.append(
                "Retried once. Chrome did not open its debugging port in time, which "
                "usually means the machine is busy or Chrome is updating, so trying "
                "again shortly often works."
                + (" If this profile is in use, persistent=False or "
                   "clone_chrome_profile gives an isolated one." if plan.persistent else "")
            )
            hint = " ".join(parts)
        return err(hint or f"launch failed: {e}"), None, None
    return None, browser, ads_pending


def _launch_owned(plan: Optional[_LaunchPlan], launching: _Launching) -> bool:
    """The launch still holds its instance: nothing cut it short (browser_recover), the
    launching instance is still the current one, and its browser, once adopted, is still the
    current browser."""
    if launching.ended_by or BrowserState.current_instance_id != launching.instance_id:
        return False
    return plan is None or not plan.adopted or BrowserState.browser is plan.browser


def _launch_lost(plan: _LaunchPlan) -> bool:
    """The launch no longer holds its instance (_launch_owned), for a step that is about to write."""
    return plan.launching is not None and not _launch_owned(plan, plan.launching)


def _launch_lost_note(launching: _Launching) -> str:
    """Who took the launch's instance from it, for the result of the launch it cut short. What
    browser_recover did to the instance is said by its own result, not here: it marks the launch
    before it waits, and may then find another pair and reset nothing."""
    iid = launching.instance_id
    if launching.ended_by:
        return f" {launching.ended_by} asked this launch to stop while it ran."
    return (f" Instance {iid!r} stopped being the current one, or its browser was replaced, while "
            f"this launch ran; the current instance was left as it was.")


def _sentence(text: str) -> str:
    return text if text.endswith(".") else text + "."


async def _launch_settle(req: _LaunchRequest, plan: Optional[_LaunchPlan], launching: _Launching,
                         stop: Optional[str]) -> Optional[str]:
    """What a step's answer becomes, checked after every await of browser_launch. While the
    launch still holds its instance (_launch_owned): the step's own answer, or None to go on.

    Once it does not, the launch is cut short. browser_recover, which is never refused, is
    what does that (a change that went round _launch_gate would too). An error the step had
    reached stays, with who cut the launch short after it; a success it had reached does not
    count. The browser the launch started or took over is ended, once
    (_launch_end_started_browser), its privacy profile removed, and nothing more is recorded:
    no handle for a browser that ended, no tabs, no watcher, no keep_alive record."""
    if _launch_owned(plan, launching):
        return stop
    if stop is not None and stop.startswith("Error"):
        text = stop
    else:
        text = err(f"browser_launch of instance {launching.instance_id!r} did not finish.")
        written = _launch_written(plan) if plan is not None else None
        if written:
            text += f" {written[0].upper()}{written[1:]}."
    text = _sentence(text) + _launch_lost_note(launching)
    if plan is None or plan.ended:
        return text
    if plan.browser is None:
        return text + await _launch_remove_privacy(plan)
    reap, kept, removal = await _launch_end_started_browser(req, plan)
    if _close_incomplete(reap):
        text += f" Its browser could not be ended ({_close_failure(reap)})"
        text += "; its handle and profile were kept: call browser_close to try again." if kept else "."
    else:
        ended = "was ended" if reap.get("verified") else "was asked to stop"
        text += f" Its browser {ended}{_kill_note(reap)}."
    return text + removal


async def _launch_remove_privacy(plan: _LaunchPlan) -> str:
    """Remove this launch's privacy profile, if it made one; "" once it is gone. One that cannot
    be removed (a process list that cannot be read, a file still held) stays recorded on the
    instance, as browser_close keeps it, so the next browser_launch (its stale-privacy step) or
    browser_close removes it; the returned note says so, with the reason. Recorded only while
    the instance is still the current one: its removal awaits."""
    iid = plan.launching.instance_id if plan.launching is not None else BrowserState.current_instance_id
    if await _remove_privacy_profile(plan.privacy_profile):
        # Published with the handle (_launch_ready_browser): an instance still naming the folder
        # just removed no longer has a privacy profile to clean up.
        if (plan.privacy_profile is not None and BrowserState.current_instance_id == iid
                and BrowserState.current_profile_dir == plan.privacy_profile):
            BrowserState.current_privacy = False
        return ""
    note = (f" The privacy profile could not be removed: {plan.privacy_profile}"
            f"{_removal_failure(plan.privacy_profile)}")
    if BrowserState.current_instance_id != iid:
        return note + (f"; instance {iid!r} is no longer the current one, so it could not be kept "
                       f"on it for a later removal.")
    if BrowserState.browser is not None and BrowserState.browser is not plan.browser:
        return note + (f"; instance {iid!r} holds another browser now, so it could not be kept on it "
                       f"for a later removal.")
    BrowserState.current_profile_dir, BrowserState.current_privacy = plan.privacy_profile, True
    return note + "; it is kept on this instance, so the next browser_launch or browser_close removes it."


async def _launch_end_started_browser(req: _LaunchRequest, plan: _LaunchPlan) -> tuple[dict, bool, str]:
    """A launch that failed after the start, or was cut short, ends the browser it started or
    took over as every close path does (`_ensure_browser_dead`: stop() can return while Chrome
    runs on). Once that browser is verified gone the instance is reset (if it still holds it)
    and the privacy profile removed. One that could not be ended keeps its handle, naming its
    profile so browser_close or the next launch ends it: on its instance while that still
    holds it, or once browser_recover emptied it meanwhile (its reset dropped the handle).

    The instance and the browser are the launch's own (plan.launching, plan.browser), never what
    is current by then, and the close is registered for that instance (_closing_in_background),
    so a call on it waits for the close. Nothing is recorded on another instance, and nothing
    of the current instance is touched once it holds another browser. Returns (the close
    report, whether the instance was reset by it or holds its handle, a note when the privacy
    profile could not be removed)."""
    iid, browser = plan.launching.instance_id, plan.browser
    plan.ended = True
    with _closing_in_background(iid):
        reap = await _ensure_browser_dead(browser, plan.profile_path)
        current = BrowserState.current_instance_id == iid
        held = current and BrowserState.browser is browser
        if _close_incomplete(reap):
            if not (held or (current and BrowserState.browser is None)):
                return reap, False, ""
            BrowserState.browser = browser
            BrowserState.current_profile_dir, BrowserState.current_privacy = plan.profile_path, req.privacy
            return reap, True, ""
        if held:
            BrowserState.reset()
            plan.adopted = False  # the launch ended its browser itself: no longer the current browser
        return reap, held, await _launch_remove_privacy(plan)


async def _launch_cancelled(plan: _LaunchPlan) -> None:
    """A launch cancelled past the start: its own browser is stopped (a cancel cannot wait for a
    verified end), the instance reset only while it still holds that browser, and the privacy
    profile removed (plan.cancel_note says when it could not be)."""
    plan.ended = True
    await _safe_stop_browser(plan.browser)
    if (BrowserState.current_instance_id == plan.launching.instance_id
            and BrowserState.browser is plan.browser):
        BrowserState.reset()
    plan.cancel_note = await _launch_remove_privacy(plan)


def _launch_recover_cancelled(launching: _Launching) -> str:
    """The result of a launch browser_recover cancelled: it had not ended by itself when asked."""
    plan = launching.plan
    text = (f"browser_launch of instance {launching.instance_id!r} did not finish: browser_recover "
            f"asked it to stop, and it was cancelled at the step it was waiting on.")
    if plan is not None and plan.browser is not None:
        text += " Its browser was asked to stop; that it ended was not checked."
    return err(text + (plan.cancel_note if plan is not None else ""))


def _launch_end_note(reap: dict, recorded: bool) -> str:
    """What the result adds after a failure past the start, from the close report. A stop that
    was asked for but not seen to end says so (_kill_note), as browser_close does. A launch that
    lost its instance meanwhile is then cut short, and _launch_settle says so after this."""
    if _close_incomplete(reap):
        note = f" The browser could not be ended ({_close_failure(reap)})"
        note += "; its handle and profile were kept: call browser_close to try again." if recorded else "."
    else:
        note = _kill_note(reap)
    return note


async def _launch_ready_browser(req: _LaunchRequest, plan: _LaunchPlan, browser: Any,
                                ads_pending: Any) -> Optional[str]:
    """The started browser becomes this instance's, and what must be in place before its first
    navigation is put there: the ad blocker, the overlay, the moved cookies, the storage state.

    Returns an error result (the browser closed or stopped first), or None to go on."""
    # Never over a browser or an instance another call put there meanwhile (_launch_slot_taken):
    # the launch ends its own and leaves that one as it is.
    taken = _launch_slot_taken(plan.launching) if plan.launching is not None else ""
    if taken:
        reap, _recorded, removal = await _launch_end_started_browser(req, plan)
        text = (f"browser_launch of instance {plan.launching.instance_id!r} did not finish: {taken}. It changed "
                f"nothing of what that call left, and ")
        if _close_incomplete(reap):
            text += f"its own browser could not be ended ({_close_failure(reap)})."
        else:
            text += f"its own browser {'was ended' if reap.get('verified') else 'was asked to stop'}{_kill_note(reap)}."
        return err(text + removal)
    # The handle and its profile in one step, no await between: a browser_recover reading the
    # instance in between paired the new browser with the old profile, and signalled the PIDs
    # of whichever Chrome had that folder.
    BrowserState.browser = browser
    BrowserState.current_profile_dir, BrowserState.current_privacy = plan.profile_path, req.privacy
    plan.adopted = True
    # Before the first navigation, so its own requests are already looked at.
    if ads_pending is not None:
        plan.dock_note += await _adblock_module().attach(
            ads_pending, browser, plan.launching.instance_id, BrowserState.session_key())
    plan.dock_note += await presence.attach(browser, _browser_process_pid(browser), ask=req.agent_overlay,
                                            headless=req.headless)

    if plan.cookie_move.backup is not None:
        moved, moved_text = await _finish_cookie_move(browser, Path(str(plan.profile_path)), plan.cookie_move)
        if not moved:
            # Left running, the next browser_launch reuses it and never tries the move again.
            # Ended or kept is read from the close report, never from which browser is current.
            reap, recorded, removal = await _launch_end_started_browser(req, plan)
            if _close_incomplete(reap):
                closed = f"The browser could not be closed ({_close_failure(reap)})"
                if recorded:
                    closed += ": call browser_close to try again, then launch again, which resumes the move"
            else:
                # Asked to stop is not seen to end: without a process to check, it says so.
                ended = "was closed" if reap.get("verified") else "was asked to stop"
                closed = f"The browser {ended}; launch again, which resumes the move{_kill_note(reap)}"
            return err(f"its cookies were not all moved: {moved_text}. {closed}{removal}")
        plan.dock_note += moved_text

    if req.storage_state_path:
        try:
            await _apply_storage_state(browser, req.storage_state_path)
        except asyncio.CancelledError:
            await _launch_cancelled(plan)
            raise
        except Exception as e:
            reap, recorded, removal = await _launch_end_started_browser(req, plan)
            return err(f"storage_state load failed: {e}{_launch_end_note(reap, recorded)}"
                       f"{removal}")
    return None


async def _launch_first_page(req: _LaunchRequest, plan: _LaunchPlan) -> tuple:
    """The identity goes on the first tab, and the caller's url loads there.
    Returns (error result or None, the tab, what the load observed, and its document response state).
    On an error the browser was ended and the privacy profile removed, or the result says why they
    were kept.
    """
    try:
        await asyncio.sleep(0.5)
        main = await _first_live_tab(plan.browser)
        await _apply_fingerprint_to_tab(main, plan.effective_fingerprint)
        if req.profile_mode == "light" or req.testing_mode:
            # The first document after startup still loads images despite the flag;
            # every later one obeys it.
            await asyncio.wait_for(main.get("about:blank"), timeout=NAV_COMMIT_TIMEOUT)
        doc_state, _disarm = await _arm_document_watch(main)
        try:
            timeout_observation = await _commit_navigation(main, req.url, doc_state)
            if timeout_observation is not None and not timeout_observation.get("_commit_timeout_recovered"):
                return None, main, timeout_observation, doc_state
            await _wait_load_complete(main, 3)
            _launch_observed = _carry_nav_timeout_recovery(
                await _observe_navigation(main, req.url, _disarm()), timeout_observation)
        finally:
            _disarm()
    except asyncio.CancelledError:
        await _launch_cancelled(plan)
        raise
    except Exception as e:
        reap, recorded, removal = await _launch_end_started_browser(req, plan)
        return err(f"initial nav failed: {e}{_launch_end_note(reap, recorded)}"
                   f"{removal}"), None, None, None
    return None, main, _launch_observed, doc_state


def _launch_record_browser(req: _LaunchRequest, plan: _LaunchPlan, browser: Any, main: Any) -> None:
    """The instance's state names the new browser: its tab, profile, identity, idle timeout,
    and for a headed one the watch for its windows."""
    BrowserState.tabs = [main]
    BrowserState.own(BrowserState.tabs)  # opened or taken over by this server
    BrowserState.active_tab_index = 0
    BrowserState.current_profile_dir = plan.profile_path
    BrowserState.current_headless = req.headless
    BrowserState.current_privacy = req.privacy
    BrowserState.current_fingerprint = dict(plan.effective_fingerprint)
    BrowserState.current_proxy = plan.effective_proxy
    # list_instances' uptime is this browser's: left alone, it counted from the server's start
    # or from the instance's previous browser (84 s on a browser just launched).
    BrowserState.current_created_at = time.time()
    # A stdio browser_launch is not idle-reaped: it is what the person is looking at,
    # and the server exits with its client anyway. The timeout has to be cleared and not
    # merely the reaper left off, because any spawn_browser starts the reaper, which
    # then closed this browser too. Over HTTP a client can vanish, so the reaper is what
    # eventually ends its browser.
    BrowserState.current_idle_timeout = (DEFAULT_IDLE_TIMEOUT if BrowserState.http_mode()
                                         and not _persons_browser(BrowserState.browser) else 0)
    if BrowserState.http_mode():
        _ensure_idle_reaper_running()
    BrowserState.window_closed.pop(BrowserState.current_instance_id, None)
    if not req.headless:
        _watch_windows(browser)


def _launch_keep_alive(req: _LaunchRequest, plan: _LaunchPlan, browser: Any) -> str:
    """keep_alive for the new browser (its record written when asked), and the notes on the
    profile: the fallback, a kept browser not taken over, the allowed origins."""
    suffix = (
        f" [{plan.profile_info.get('session_state_source')}]"
        if plan.used_fallback else ""
    )
    suffix += plan.kept_note
    _keep, _keep_note = _keep_alive_decision(req.keep_alive, plan.final_udd, req.privacy, req.headless)
    if _keep:
        _failed = _arm_keep_alive(browser, plan.final_udd, BrowserState.current_instance_id,
                                  req.headless, plan.effective_fingerprint)
        _keep_note = _failed or _keep_note
        if not _failed and plan.used_fallback:
            _keep_note += (f" [it runs on the fallback profile {plan.final_udd}, which no later default "
                           f"launch asks for: reattach with user_data_dir={plan.final_udd!r}]")
    suffix += _keep_note
    suffix += _allow_origins_note(browser)
    return suffix


async def _launch_window_health(req: _LaunchRequest, main: Any) -> str:
    """A headed window that came up degenerate, nudged once and reported. Returns the note."""
    # Post-launch health check — degenerate window detection.
    # After a macOS sleep/wake cycle (or some persistent-profile state
    # corruption) Chrome can come up with outerWidth==0, position
    # off-screen, or visibilityState=='hidden'. Tools work but every
    # mouse_click_xy / set_viewport_size fails with cryptic CDP errors.
    # Detect once, set the window state back to normal without raising the window, and
    # probe again; report rather than fix silently so the caller knows when to call
    # browser_recover().
    health_suffix = ""
    if not req.headless:
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
    return health_suffix


async def _launch_auto_verify(req: _LaunchRequest, main: Any) -> str:
    """auto_verify: a Cloudflare challenge on the first page, answered once. Returns the note."""
    verify_suffix = ""
    if req.auto_verify:
        try:
            verify_suffix = await asyncio.wait_for(_auto_verify_cf(main), timeout=30.0)
        except Exception:
            verify_suffix = ""
    return verify_suffix


async def _launch_place_window(req: _LaunchRequest, plan: _LaunchPlan, main: Any) -> str:
    """The window goes back where this instance's was last time. Returns the note."""
    # Put the window back where it was last time. Headless has no window to
    # place, and an explicit --window-position means the caller has already
    # decided — neither should be overridden.
    place_suffix = ""
    if not req.headless and not any(
        (a or "").startswith("--window-position=") for a in plan.merged_extra_args
    ):
        try:
            asked = ((req.window_width, req.window_height)
                     if (req.window_width, req.window_height) != _DEFAULT_WINDOW else None)
            placed = await _restore_window_bounds(
                plan.launching.instance_id, main, size=asked)
            if placed.get("restored"):
                act = placed.get("actual") or {}
                place_suffix = (
                    f" [window {'clamped to a connected display' if placed.get('clamped') else 'restored'}"
                    f" at ({act.get('left')},{act.get('top')}) {act.get('width')}x{act.get('height')}"
                    + (", the size asked for on this launch" if asked else "") + "]"
                )
        except Exception:
            pass  # placement is a convenience; never fail a launch over it
    return place_suffix


async def _launch_restore_tabs(req: _LaunchRequest, plan: _LaunchPlan, browser: Any, main: Any,
                               observed: dict) -> tuple:
    """restore_tabs: the saved tabs reopen beside the first one. Returns (the note, what the
    launch observed, with the restore's report added when it ran). The tabs are recorded only
    while the launch still holds its instance: after a browser_recover meanwhile, the launch is
    cut short (_launch_settle) and records nothing more."""
    restore_suffix = ""
    if req.restore_tabs:
        try:
            _all_tabs, _select, _report, restore_suffix = await _reopen_saved_tabs(
                plan.launching.instance_id, browser, main, plan.profile_path, req.privacy,
                req.headless, plan.effective_fingerprint, req.url, req.window_width, req.window_height)
            if not _launch_owned(plan, plan.launching):
                return restore_suffix, observed
            BrowserState.tabs = _all_tabs
            BrowserState.own(BrowserState.tabs)  # opened or taken over by this server
            BrowserState.active_tab_index = _select
            observed = {**observed, "restore_tabs": _report}
        except Exception as e:
            restore_suffix = f" [restore_tabs: failed: {_exc_text(e)}]"
    return restore_suffix, observed


def _launch_result(req: _LaunchRequest, plan: _LaunchPlan, observed: dict, *, suffix: str,
                   verify_suffix: str, health_suffix: str, place_suffix: str, restore_suffix: str,
                   trip: Optional[_Trip] = None) -> str:
    """The launch's result: the fallback warning first, what was launched and what the first
    load observed (checked against expect_url and expect_status), then every note."""
    profile_info = plan.profile_info
    warning = (f"WARNING: EMPTY FALLBACK PROFILE, your saved logins are NOT here "
         f"({profile_info.get('holder') or 'another Chrome'} holds "
         f"{profile_info.get('desired_profile')}); do not log in again here, that splits the "
         f"logins over two folders. "
         if plan.used_fallback and str(profile_info.get("session_state_source") or "").startswith("FALLBACK")
         else "")
    if trip is not None and trip.mode == "pause":
        paused = _tripwire_pause(trip, "browser_launch reached a verification page", asked_for=req.url,
                                 launched=True)
        return f"{paused}\n{warning.rstrip()}{suffix}" if plan.used_fallback else paused
    result = _nav_result(
        warning + f"Browser launched (headless={req.headless}, persistent={plan.persistent}, "
        f"privacy={req.privacy}, identity={plan.identity_source}). Loaded",
        observed,
        expect_url=req.expect_url, expect_status=req.expect_status,
        suffix=f"{suffix}{verify_suffix}{health_suffix}{place_suffix}{restore_suffix}{plan.dock_note}",
    )
    return _tripwire_render(trip, "browser_launch", result, asked_for=req.url, launched=True)


@mcp.tool()
@instance_notes.names_the_instance
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
    keep_alive: Optional[bool] = None,
    browser: Literal["chrome", "brave"] = "chrome",
    vertical_tabs: Optional[str] = None,
    block_ads: bool = False,
    brave_lean: Optional[bool] = None,
    agent_overlay: Optional[bool] = None,
) -> str:
    """Launch stealth Chrome via nodriver, on a persistent profile by default.

    The initial navigation commit is bounded by 30 seconds by default without stopping the tab;
    a timeout reports its observed URL and ready_state. Load completion remains best-effort.

    The result names the profile actually used (actual_profile, session_state_source) and what the first load
    observed (final_url, redirect_chain, status, ready_state). After a redirect, read final_url.

    Args:
        url: initial URL to load
        headless: run without UI (many sites detect headless, so prefer False). A visible window opens behind
            the app in use and does not take the keyboard; a click that calls window.open brings Chrome forward
            for about 130 ms. With the dock_icon setting (macOS; `mcp-stealth-chrome settings` or
            MCP_STEALTH_DOCK_ICON=1; off by default) a visible browser on a new profile runs from Google Chrome
            Beta with the project icon (Brave Beta, with its own, for browser="brave"); without Beta, or on a
            profile Beta must not open, the result says why. If the person quits it (Cmd+Q, Dock > Quit) or
            closes every window, the server ends it within seconds and the next call says so and why: call
            browser_launch again. tab_close on the last tab does not.
        proxy: "http://user:pass@host:port" or "socks5://host:port"
        user_agent: override UA string
        window_width, window_height: viewport size
        persistent: reuse ~/.mcp-stealth/profile (the "main" instance; others get ~/.mcp-stealth/profiles/<id>).
            Unset means True, except under testing_mode: a throwaway profile unless persistent=True is passed.
        lang: browser language
        extra_args: additional Chromium flags
        storage_state_path: load cookies/localStorage from JSON before first nav
        testing_mode: 2-5× faster start and navigation for perf/regression tests: no image loading, background
            throttling dampers, translate, notifications or media autoplay. WARNING: less stealth, not for
            anti-bot work (sites can read missing images as automation).
        auto_verify: True (default) detects a Cloudflare / Turnstile challenge after the first load and clicks
            its checkbox through CDP: at most 2 attempts, ~6s, never a loop. False opts out.
        user_data_dir: an EXISTING profile root, e.g. "~/Library/Application Support/Google/Chrome" (~ expands),
            instead of persistent and the MCP profile. Its Chrome MUST be closed first: a locked profile is
            refused with the PID holding it.
        profile_directory: with user_data_dir, the sub-profile in it (e.g. "Profile 21"; default "Default"), to
            drive one persona without cloning. list_chrome_profiles lists them.
        fingerprint: explicit CDP overrides (user_agent, accept_language, platform, timezone), over a pinned
            identity for this launch.
        privacy: an empty profile, deleted when the browser closes: no history, logins or cookies are present
            or kept. The identity stays the real browser and machine unless fingerprint overrides it. A proxy
            pool (a|b) picks one member. Not with user_data_dir. For a logged-in account leave it off (the
            default keeps logins); persistent=False with privacy=False tries something without saving a login.
        profile_conflict: when another Chrome holds the profile: "fallback" (default) uses a separate
            per-process profile and says so; "fail" refuses with the holding PID; "clone" launches from an
            integrity-checked read-only copy. Nothing on disk is deleted or overwritten.
        expect_url, expect_status: validate the first load, as in navigate(); an unmeasured value fails. A
            verification page there answers as in navigate (antibot_tripwire), the browser left running on it.
        profile_mode: "light": no images and no background Chrome services, saving memory and network on
            ordinary pages. Pages can notice the failed images (img.naturalWidth stays 0) and layout or vision
            tools see none, so keep "default" on bot-protected sites or when the look matters.
        restore_tabs: reopen the tabs this instance had on this profile when this server last closed it
            (browser_close, close_instance, idle timeout, Quit, or the server's exit), behind the app in use,
            with the same tab selected. Without a url the first saved page loads in the first tab; with one,
            that page stays first and selected and the saved pages follow. The result lists each page as
            reopened (final URL, status) or failed (why), secret URL parameters masked, or says there was no
            saved list; it never fails the launch. Persistent profiles only (persistent=False and privacy=True
            keep no tabs). The list is ~/.mcp-stealth/window-state/<instance>-tabs.json, readable by you only;
            browser_close(forget_tabs=True) removes it.
        keep_alive: True leaves this browser running, tabs and page state included, when this server exits
            (client slept, reconnected or closed); the next browser_launch on the profile reattaches instead of
            starting another Chrome: "reattached to the running browser (pid N, K tabs)". It still ends on
            Quit, on browser_close after a reattach, and after MCP_STEALTH_KEEP_ALIVE_MAX_HOURS (24) with no
            server. Persistent profiles only: persistent=False and privacy=True ignore it and say so. Headless
            works too (an invisible Chrome holding memory). Not on Windows. Unset: False for a new browser,
            unchanged on a reattach. A reattach happens whenever a browser an earlier server of ours kept holds
            the profile, whatever this value; it never attaches to the person's own Chrome or one a live server
            drives.
        browser: "chrome" (default) or "brave" (must be installed). Brave keeps its Shields (ads and trackers
            blocked), farbling (canvas and audio values differ per site and session) and identity
            (navigator.brave, userAgentData naming Brave). Its profiles: ~/.mcp-stealth/brave-profile (main),
            ~/.mcp-stealth/brave-profiles/<id> (others). A profile opens only in the browser that made it,
            since neither reads the other's cookies.
        vertical_tabs: "left", "right" or "off" writes the tab-strip setting into the profile before start;
            None (default) leaves it. "right" is Brave only, refused on Chrome.
        block_ads: block ad and tracker requests in every tab, popup, frame and worker (EasyList and
            EasyPrivacy, Brave's adblock-rust engine). Requests only: nothing in the page is hidden, so an ad
            can leave an empty box, and a page can notice the net::ERR_BLOCKED_BY_CLIENT failures. The lists
            download on first use into ~/.mcp-stealth/adblock, are checked at most daily and work offline from
            the last copy. The result says whether it is on (without a list: off and why, never a failed
            launch); server_status shows what was blocked. Does nothing on Brave, whose Shields block ads.
        brave_lean: Brave only (ignored on Chrome, said so when passed). True turns off what an agent does not
            use: Leo AI, VPN, Speedreader and Playlist for this process (--disable-features); P3A, the daily
            usage ping and Tor windows (Local State); toolbar and sidebar buttons, new tab widgets and
            sponsored images, Brave News, the wallet's page provider, and the Web Discovery, Wayback Machine
            and default-browser prompts (Preferences, written before launch into a profile seen free, never
            the person's own Brave folder). Shields, farbling, navigator.brave and the brands, vertical tabs,
            Safe Browsing, filter list updates, Rewards, Ads and the wallet page stay. None (default) does
            this only on a profile this server made empty; a user_data_dir, a clone or an older profile keeps
            its settings unless True is passed. False keeps Brave as it ships and undoes an earlier lean
            launch. The result says what was turned off and what was not; server_status shows it.
        agent_overlay: macOS, headed. Over the window: a light while the agent acts, a bar saying what it
            does, and a lock so a stray click or scroll never reaches the page. The bar's Take over hands the
            browser to the person; while they hold it, every tool that acts on or reads the page is refused,
            saying so, until they press Resume agent (wait_for_human waits for that). A key they press
            pauses the agent's input for a moment, and the result says so. request_human_help shows its
            request in the bar with Done. The bar can show the agent's cursor (agent_cursor setting, off).
            None (default) follows the agent_overlay setting, off unless set (`mcp-stealth-chrome settings`
            or MCP_STEALTH_AGENT_OVERLAY=1). Needs the os-cursor extra (PyObjC); without it, headless, or on a
            browser the person opened from the launcher, the result says it is off and why.
    """
    product, brave_executable, user_data_dir, extra_args, problem = _launch_check_arguments(
        browser, vertical_tabs, user_data_dir, extra_args)
    if problem:
        return err(problem)
    persistent = _launch_default_persistent(persistent, testing_mode)
    req = _LaunchRequest(
        url=url, headless=headless, proxy=proxy, user_agent=user_agent, window_width=window_width,
        window_height=window_height, lang=lang, extra_args=extra_args,
        storage_state_path=storage_state_path, testing_mode=testing_mode, auto_verify=auto_verify,
        user_data_dir=user_data_dir, profile_directory=profile_directory, fingerprint=fingerprint,
        privacy=privacy, profile_conflict=profile_conflict, expect_url=expect_url,
        expect_status=expect_status, profile_mode=profile_mode, restore_tabs=restore_tabs,
        keep_alive=keep_alive, vertical_tabs=vertical_tabs, block_ads=block_ads,
        brave_lean=brave_lean, agent_overlay=agent_overlay, product=product,
        brave_executable=brave_executable)
    # The launch's owner: set before the first await, cleared however it ends (a raise and a
    # cancel too). While it is set, _launch_gate refuses what would change this instance or its
    # browser, and the sweeps leave the instance alone. Only browser_recover may still end it:
    # after every await _launch_settle looks, and a launch cut short records nothing more.
    launching = _Launching(BrowserState.current_instance_id, None)
    if _launching() is not None:
        return err("another launch is already in progress — wait for it to finish")
    session = BrowserState.session_key()
    # The launch runs in a task of its own, which browser_recover cancels when the launch hangs
    # on an await that never returns. Not this call's own task: FastMCP runs a session's
    # requests in one anyio task group, and a task cancelled from outside the group cancels its
    # siblings, browser_recover's own call among them. A client's cancel of this call still
    # reaches the launch (awaiting a task passes a cancel on to it).
    run = asyncio.ensure_future(_launch_run(req, launching, session, persistent))
    launching.task = run
    _LAUNCHING[session] = launching
    try:
        return await run
    except asyncio.CancelledError:
        if not (launching.cancelled and run.cancelled() and asyncio.current_task().cancelling() == 0):
            raise
        return _launch_recover_cancelled(launching)
    finally:
        if _LAUNCHING.get(session) is launching and run.done():
            del _LAUNCHING[session]


async def _launch_run(req: _LaunchRequest, launching: _Launching, session: str, persistent: Any) -> str:
    """browser_launch's steps, in their own task (see browser_launch). After every await
    _launch_settle looks whether the launch still holds its instance. A cancel ends the browser
    it started (_launch_cancelled) and goes on up; the mark is released however it ends."""
    _THIS_LAUNCH.set(launching)
    plan: Optional[_LaunchPlan] = None
    try:
        # No early "already running" return here. It short-circuited the reuse path
        # below — the one that probes CDP and honours the caller's url — so a launch
        # against a live browser answered "already running" and left the caller on
        # whatever page happened to be open, believing the navigation had happened.
        stop = await _launch_end_stale_browser()
        if (stop := await _launch_settle(req, None, launching, stop)) is not None:
            return stop

        if _LAUNCH_LOCK.locked():
            return err("another launch is already in progress — wait for it to finish")

        _reserved = _launch_reservation(BrowserState.current_instance_id, req.product, persistent=bool(persistent),
                                        privacy=req.privacy, explicit=req.user_data_dir, extra_args=req.extra_args)
        async with _LAUNCH_LOCK, launch_lock.reserve(_reserved, BROWSER_LAUNCH_TIMEOUT) as _busy:
            if (stop := await _launch_settle(req, None, launching, err(_busy) if _busy else None)) is not None:
                return stop
            stop = await _launch_reuse_running(req)
            if (stop := await _launch_settle(req, None, launching, stop)) is not None:
                return stop
            launching.slot, launching.slot_taken = BrowserState.browser, True

            ensure_dirs()

            plan = launching.plan = _LaunchPlan(persistent=persistent, merged_extra_args=list(req.extra_args or []),
                                                launching=launching)
            if (stop := _launch_privacy_and_identity(req, plan)) is not None:
                return stop
            # The launch choice and every refusal come before anything writes to the profile.
            # If the user passed user_data_dir explicitly, that takes precedence over
            # both `persistent` and any --user-data-dir already in extra_args.
            if req.user_data_dir:
                stop = await _launch_explicit_profile(req, plan)
            else:
                stop = await _launch_kept_browser(req, plan)
                if stop is None:
                    # Each instance has one deterministic profile, and a conflict over it is
                    # settled by the caller's profile_conflict. Only "main" takes part in
                    # process affinity: a second HTTP session must not repoint it, or the
                    # primary's next launch lands on a different cookie jar and its logins
                    # look wiped.
                    resolved_profile, profile_info = _resolve_launch_profile(
                        launching.instance_id,
                        persistent=bool(plan.persistent),
                        profile_conflict=req.profile_conflict,
                        browser=req.product,
                    )
                    stop = _launch_server_profile(req, plan, resolved_profile, profile_info)
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop

            _launch_config(req, plan)
            stop = await _launch_prepare_profile(req, plan)
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop
            await _launch_keep_keyboard(req, plan)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            # The cookie move: prepared above, then preflight, commit, the vertical tabs and lean
            # settings, tidy-up, preflight, start.
            stop = await _launch_commit_profile(req, plan)
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop
            stop, started, ads_pending = await _launch_start_chrome(req, plan)
            plan.browser = started
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop
            stop = await _launch_ready_browser(req, plan, started, ads_pending)
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop
            stop, main, observed, doc_state = await _launch_first_page(req, plan)
            if (stop := await _launch_settle(req, plan, launching, stop)) is not None:
                return stop

            # From here on the launch records its browser; after each await it checks again, so a
            # recover meanwhile stops the steps that would record more.
            _launch_record_browser(req, plan, started, main)
            suffix = _launch_keep_alive(req, plan, started)
            if "commit_timeout_seconds" in observed and not observed.get("_commit_timeout_recovered"):
                await _give_focus_back(plan.previous_app)
                return await _launch_settle(req, plan, launching, err(_nav_commit_timeout_message(
                    f"Browser is running{suffix}; first page", observed)))
            health_suffix = await _launch_window_health(req, main)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            verify_suffix = await _launch_auto_verify(req, main)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            place_suffix = await _launch_place_window(req, plan, main)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            restore_suffix, observed = await _launch_restore_tabs(req, plan, started, main, observed)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            await _record_tabs_at_launch(launching.instance_id, launching)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            # The first page as the tripwire sees it: a verification page the launch reached is
            # said as one, with the browser running on it (antibot_tripwire).
            trip = await _tripwire_after_navigation(main, observed, verify_suffix=verify_suffix,
                                                    doc_state=doc_state, headless=req.headless)

            await _give_focus_back(plan.previous_app)
            if (stop := await _launch_settle(req, plan, launching, None)) is not None:
                return stop
            return _launch_result(req, plan, observed, suffix=suffix, verify_suffix=verify_suffix,
                                  health_suffix=health_suffix, place_suffix=place_suffix,
                                  restore_suffix=restore_suffix, trip=trip)
    except asyncio.CancelledError:
        # A phase with no cancel path of its own: the browser it started still goes.
        if plan is not None and plan.browser is not None and not plan.ended:
            await _launch_cancelled(plan)
        raise
    finally:
        if _LAUNCHING.get(session) is launching:
            del _LAUNCHING[session]


def _forget_tabs_note(instance_id: str, profile: Any, privacy: bool) -> str:
    """Remove the saved tab list of an instance and profile, for forget_tabs=True."""
    scope = _tab_record_scope(instance_id, profile, privacy)
    if scope is None:
        return " No tab list is kept for this profile."
    # The list the quit watcher keeps for a kept browser goes too, or a later start from the
    # Dock would bring back tabs the caller asked to forget.
    watched = _keep_alive.drop_last_tabs(scope)
    if tab_record.forget(instance_id, scope) or watched:
        return " Its saved tab list was removed."
    return " It had no saved tab list."


@mcp.tool()
async def browser_close(forget_tabs: bool = False) -> str:
    """Close the browser and free the profile lock.

    The open tabs are saved first (persistent profiles only), so browser_launch(restore_tabs=True)
    can reopen them. forget_tabs=True closes without saving and removes the saved list.
    """
    with _closing_in_background(BrowserState.current_instance_id):
        return await _browser_close_now(forget_tabs)


async def _browser_close_now(forget_tabs: bool) -> str:
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


def _profile_is_browsers(browser: Any, profile: Any) -> bool:
    """Whether `profile` is the folder this browser was started on (its Config.user_data_dir)."""
    folder = getattr(getattr(browser, "config", None), "user_data_dir", None)
    if not folder:
        return False
    try:
        return os.path.realpath(str(folder)) == os.path.realpath(str(profile))
    except Exception:
        return False


async def _recover_stops_launch(launching: _Launching) -> str:
    """browser_recover's step for a launch of its instance still running, once marked to stop.
    It is given _RECOVER_LAUNCH_WAIT to end by itself (its next look cuts it short and ends its
    browser with a verified end); one still running then, hung on an await that never returns,
    is cancelled (its cancel path stops its browser) and given the same time again. One that
    has still not ended no longer holds the instance (_launching), and the step says so."""
    head = f"browser_launch of instance {launching.instance_id!r} was still running"
    task = launching.task
    if task is None or task.done() or task is asyncio.current_task():
        return f"{head}: asked to stop"
    await asyncio.wait({task}, timeout=_RECOVER_LAUNCH_WAIT)
    if task.done():
        return f"{head}: asked to stop, it ended as an error that records nothing more"
    launching.cancelled = True
    task.cancel()
    await asyncio.wait({task}, timeout=_RECOVER_LAUNCH_WAIT)
    if task.done():
        return (f"{head}: asked to stop, it was still waiting after {_RECOVER_LAUNCH_WAIT:g} s and was "
                f"cancelled; its browser was asked to stop")
    return (f"{head}: asked to stop and cancelled, it had still not ended {_RECOVER_LAUNCH_WAIT:g} s "
            f"later. Instance {launching.instance_id!r} is no longer held for it, so other tools are "
            f"not refused; a launch lock it holds stays held until it ends")


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
    # The instance and the browser this recover acts on, taken before its first await. It does
    # not wait for a close (_app.RUNS_DURING_A_CLOSE), and between its awaits a switch_instance
    # can make another instance current, or a launch it cut short can keep a handle on a browser
    # it could not end: resetting by what is current then reset the instance switched in, or
    # dropped the only handle on a live Chrome. So it resets only that pair (step 3).
    target_iid, target_browser = BrowserState.current_instance_id, BrowserState.browser
    target_profile = BrowserState.current_profile_dir
    target_tabs = list(BrowserState.tabs)
    # A browser the person opened from the launcher that still answers is not stuck: whatever
    # the agent could not do (a tab that is not its own, most likely) is no reason to end it.
    if target_browser is not None and _persons_browser(target_browser):
        from nodriver.cdp import browser as cdp_browser
        with contextlib.suppress(Exception):
            await asyncio.wait_for((getattr(target_browser, "connection", None)
                                    or target_browser).send(cdp_browser.get_version()), timeout=3)
            return err("browser_recover did not end this browser: the person opened it from the "
                       "launcher and it answers, so it is not stuck. tab_new opens a tab of the "
                       "agent's; tab_select picks one of the person's on purpose; the person quits it.")

    # Never refused during a launch: the way out of a stuck state must not depend on what is
    # stuck. A browser_launch of this instance still running ends as an error and records
    # nothing more (_launch_settle looks after each of its awaits). Marked before anything is
    # stopped or reset here, so the launch cannot record what this recover is about to reset.
    launching = _launching()
    if launching is not None and launching.instance_id == target_iid:
        launching.ended_by = "browser_recover"
        steps.append(await _recover_stops_launch(launching))

    # 1. Best-effort stop. The profile dir was taken above, so step 2 can
    #    target only Chrome PIDs that belong to this profile.
    try:
        b = target_browser
        if b and not getattr(b, "stopped", False):
            try:
                b.stop()
                steps.append("browser.stop() ok")
            except Exception as e:
                steps.append(f"browser.stop() failed: {type(e).__name__}")
    except Exception:
        pass

    # 2. SIGTERM/SIGKILL orphan Chrome PIDs that match this profile. Skips
    #    silently if no profile dir is recorded (attached browser, etc). With a browser
    #    in hand, only a profile proved to be that browser's (its --user-data-dir) is
    #    scanned: another path is no identity of its process.
    if (target_profile is not None and target_browser is not None
            and not _profile_is_browsers(target_browser, target_profile)):
        steps.append(f"no Chrome PIDs looked for: the recorded profile {target_profile} is not proved to "
                     f"be this browser's")
        target_profile = None
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
                    # _process_exists, not os.kill(pid, 0): on Windows that sends Ctrl+C.
                    if not _process_exists(pid):
                        continue
                    try:
                        os.kill(pid, _signal.SIGKILL)
                        killed += 1
                    except Exception:
                        pass
            steps.append(f"chrome PIDs: {terminated} SIGTERM, {killed} SIGKILL")
        else:
            steps.append("no orphan chrome PIDs")

    # 3. Reset the instance this recover began on, while it is still the current one and holds
    #    the same browser, or none (a close that finished meanwhile left it empty). Otherwise
    #    nothing of the current instance is reset or cleared, and the result says so.
    if BrowserState.current_instance_id != target_iid:
        not_reset = (f"instance {target_iid!r} stopped being the current one while this recover ran, so "
                     f"nothing of the current instance ({BrowserState.current_instance_id!r}) was reset; "
                     f"switch_instance({target_iid!r}) and browser_recover again to reset it")
    elif BrowserState.browser is not target_browser and BrowserState.browser is not None:
        not_reset = (f"instance {target_iid!r} holds another browser than when this recover began (one "
                     f"a launch kept or started meanwhile), so it was not reset; browser_recover again "
                     f"acts on the browser it holds now")
    else:
        not_reset = ""
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

    if not_reset:
        return err(f"browser_recover did not reset: {not_reset}. Done: " + " | ".join(steps))
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

    previous_navigation_stopped = bool(getattr(tab, "_mcp_navigation_stopped_after_timeout", False))
    with contextlib.suppress(Exception):
        delattr(tab, "_mcp_navigation_stopped_after_timeout")
    state: dict[str, Any] = {
        "armed": False,
        "arm_error": None,
        "request_id": None,
        "redirects": [],
        "status": None,
        "status_text": "",
        "document_url": None,
        "failure": None,
        "previous_navigation_stopped": previous_navigation_stopped,
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
            # Cloudflare's own word that this document is its challenge page, in any language:
            # the tripwire then looks at the page (antibot.py).
            for name, value in dict(getattr(response, "headers", None) or {}).items():
                if str(name).lower() == "cf-mitigated":
                    state["cf_mitigated"] = str(value).strip().lower()
                    state["cf_loader"] = str(getattr(event, "loader_id", "") or "")
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
    # Network.enable is session-scoped and idempotent, but a cancelled navigation can leave
    # nodriver waiting on the next command while Chrome finishes the old one.  Do not send a
    # second enable on the same CDP session: the first successful enable already covers every
    # later document, and its event handlers are scoped by `state` below.
    enabled_connection = getattr(tab, "_mcp_network_enabled_connection", None)
    enabled_domains = getattr(tab, "enabled_domains", ())
    if (enabled_connection is tab or cdp_network in enabled_domains) and not getattr(tab, "closed", False):
        state["armed"] = True
    else:
        try:
            await _wait(tab.send(cdp_network.enable()), timeout=5, what="Network.enable")
            state["armed"] = True
            with contextlib.suppress(Exception):
                setattr(tab, "_mcp_network_enabled_connection", tab)
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
    if doc_state.get("previous_navigation_stopped"):
        observed["navigation_note"] = "the earlier navigation was stopped after its commit timeout"
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
    probe_timeout: float = 5,
) -> dict:
    """Read back where the tab ACTUALLY ended up, after the navigation."""
    final_url = ""
    ready_state = ""
    probe_error = None
    try:
        probe = await _wait(
            evaluate_isolated(tab, "JSON.stringify([window.location.href, document.readyState])"),
            timeout=probe_timeout,
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


def _nav_timeout_recovered(requested_url: Optional[str], observed: dict) -> bool:
    """Whether a commit timeout still produced proof of the requested document."""
    status = observed.get("status")
    ready = observed.get("ready_state")
    final = str(observed.get("final_url") or "")
    requested = str(requested_url or "")
    if status is None or ready not in ("interactive", "complete") or not final:
        return False

    def same_document(left: str, right: str) -> bool:
        return left.split("#", 1)[0].rstrip("/") == right.split("#", 1)[0].rstrip("/")

    if not requested:
        return True
    if requested and same_document(final, requested):
        return True
    redirects = observed.get("redirect_chain") or []
    return bool(redirects and requested and same_document(
        str(redirects[0].get("url") or ""), requested))


def _carry_nav_timeout_recovery(observed: dict, timeout_observation: Optional[dict]) -> dict:
    """Keep the successful-after-timeout note when a later load probe rebuilds observation."""
    if timeout_observation and timeout_observation.get("_commit_timeout_recovered"):
        observed["commit_timeout_seconds"] = timeout_observation.get("commit_timeout_seconds")
        observed["_commit_timeout_recovered"] = True
    return observed


async def _stop_navigation_after_timeout(tab: Tab) -> Optional[bool]:
    """Stop a timed-out load, returning True, False, or None when it cannot be measured."""
    from nodriver.cdp import page as cdp_page
    try:
        await _wait(tab.send(cdp_page.stop_loading()), timeout=5, what="Page.stopLoading")
    except Exception:
        return False
    with contextlib.suppress(Exception):
        setattr(tab, "_mcp_navigation_stopped_after_timeout", True)
    return True


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
    if observed.get("_commit_timeout_recovered"):
        suffix += (f" (answer arrived after {observed.get('commit_timeout_seconds', NAV_COMMIT_TIMEOUT):g}s "
                   "bound)")
    if observed.get("navigation_note"):
        suffix += f" ({observed['navigation_note']})"
    body_observed = {key: value for key, value in observed.items() if not key.startswith("_")}
    body = json.dumps(body_observed, indent=2, default=str)
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
    # A launch going on without its profile reservation says so (launch_lock); "" otherwise.
    suffix += launch_lock.note()
    return ok(
        f"{action}: {final_url} [{status_part}{redirect_part}, ready_state {ready}]"
        f"{suffix}\n{body}"
    )


async def _commit_navigation(tab, url: str, doc_state: dict) -> Optional[dict]:
    """Wait for Chrome to commit, recovering a loaded document when the bound was crossed."""
    try:
        await asyncio.wait_for(tab.get(url), timeout=NAV_COMMIT_TIMEOUT)
    except asyncio.TimeoutError:
        observed = await _observe_navigation(tab, url, doc_state, probe_timeout=0.5)
        observed["commit_timeout_seconds"] = NAV_COMMIT_TIMEOUT
        if _nav_timeout_recovered(url, observed):
            observed["_commit_timeout_recovered"] = True
        else:
            stopped = await _stop_navigation_after_timeout(tab)
            observed["loading_stopped"] = stopped is True
            if stopped is False:
                observed["loading_stop_error"] = "Page.stopLoading failed"
        return observed
    return None


def _nav_commit_timeout_message(action: str, observed: dict) -> str:
    """Describe a commit timeout and whether the unfinished load was stopped."""
    observed = dict(observed)
    observed["commit_timeout_seconds"] = NAV_COMMIT_TIMEOUT
    stopped = observed.get("loading_stopped") is True
    observed["loading_stopped"] = stopped
    observed["tab_may_still_be_loading"] = not stopped
    if stopped:
        detail = "The earlier navigation was stopped; use navigate again to load a page."
    else:
        detail = "The tab may still be loading; use wait_for_url or wait_for, or navigate again."
    body = json.dumps(observed, indent=2, default=str)
    shown_url = repr(observed.get("final_url") or "(unmeasured)")
    ready = observed.get("ready_state") or "unmeasured"
    return (
        f"{action}: the page did not answer within {NAV_COMMIT_TIMEOUT:g}s; observed URL "
        f"{shown_url}, ready_state {ready}. {detail}\n{body}"
    )


def _nav_commit_timeout(action: str, observed: dict) -> str:
    return err(_nav_commit_timeout_message(action, observed))


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


# ── The anti-bot tripwire ──────────────────────────────────────────────────────────────────────
# antibot.py holds the rules and the words; this is the page side: one probe, the request for the
# person, and what a tool says. It looks after every navigation the tools make and between the
# steps of workflow_run and batch_actions, and it never solves or bypasses anything.

# The verification page last reported on a tab (its target key): a second tool on it says so in a
# line, and a check that finds the tab elsewhere withdraws the request the tripwire made for it.
_TRIPWIRE_SEEN: dict[str, dict[str, Any]] = {}
# Per session: what the next result says besides (a request the tripwire withdrew).
_TRIPWIRE_NOTES: dict[str, list[str]] = {}
# "workflow_run" or "batch_actions" while one runs its steps: a pause there says to resume the run.
_TRIPWIRE_RUN: contextvars.ContextVar = contextvars.ContextVar("antibot_tripwire_run", default=None)
TRIPWIRE_HELP_TIMEOUT = 300.0  # a tripwire request waits as long as request_human_help's default

# One read of the page, in the isolated world: its address, the words a rule for its host names
# (read only on such a host), and, when asked, whether a Cloudflare challenge page is PRESENT. A
# site merely served through Cloudflare, or a Turnstile widget in a login form, is none: the page
# must carry the challenge page's own marks, which do not depend on its language (Cloudflare writes
# the page, title included, in the browser's language), and its widget must not be solved.
_TRIPWIRE_PROBE_JS = """
(() => {
  const out = {href: location.href};
  const words = %s;
  const host = (location.hostname || '').toLowerCase();
  let text = null;
  for (const h of Object.keys(words)) {
    if (host !== h && !host.endsWith('.' + h)) continue;
    if (text === null) text = ((document.body && document.body.innerText) || '').toLowerCase();
    out.words = (out.words || []).concat(words[h].filter(w => text.includes(w)));
  }
  // Only committed child documents readable from this origin. src can still name a page
  // that redirected elsewhere, so it is not evidence for a pause.
  out.frames = [];
  for (let n = 0; n < Math.min(window.length, 16); n++) {
    try {
      const child = window.frames[n], href = child.location.href;
      const host = child.location.hostname.toLowerCase();
      const patterns = Object.keys(words).filter(h => host === h || host.endsWith('.' + h));
      const text = patterns.length ? (child.document.body?.innerText || '').slice(0, 20000).toLowerCase() : '';
      out.frames.push({href, words: patterns.flatMap(h => words[h].filter(w => text.includes(w)))});
    } catch (_) { /* cross-origin or detached */ }
  }
  if (%s) {
    let mark = '';
    if (document.querySelector('script[src*="/cdn-cgi/challenge-platform/"][src*="/orchestrate/"]')) {
      mark = 'its challenge script /cdn-cgi/challenge-platform/.../orchestrate/';
    } else if (document.querySelector('form[action*="__cf_chl_f_tk="]')) {
      mark = 'its challenge form (__cf_chl_f_tk)';
    } else {
      for (const s of document.scripts) {
        if (!s.src && s.text.length < 20000 && s.text.indexOf('_cf_chl_opt') >= 0) {
          mark = 'its challenge script (_cf_chl_opt)';
          break;
        }
      }
    }
    const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
    let solved = inputs.length > 0;
    for (const i of inputs) { if (!i.value || i.value.length <= 5) { solved = false; break; } }
    if (!mark && !solved && /^\\s*just a moment/i.test(document.title || '')
        && (inputs.length || document.querySelector('iframe[src*="challenges.cloudflare.com"]'))) {
      mark = "its title 'Just a moment...' (English only) with an unsolved widget";
    }
    out.cf = (mark && !solved) ? mark : '';
    out.solved = solved;
  }
  return JSON.stringify(out);
})()
"""


@dataclass
class _Trip:
    """A verification page the tripwire found, and what it did about it."""
    mode: str                   # "pause" or "warn"
    verdict: Any                # antibot.Verdict
    repeat: bool = False        # the page reported before on this tab, its request still pending
    where: str = ""             # pause: "bar", "window", "headless" or "pending" (antibot.next_line)
    again: str = ""             # why the person was asked again
    request: Optional[dict] = None  # exact handoff at detection, including its id and owner
    target: Optional[str] = None    # popup target when this pause came from a new tab
    auto_verify: str = ""       # what auto_verify said on this page, kept in a Cloudflare pause


def _tripwire_settings() -> tuple[str, tuple]:
    """("pause" | "warn" | "off", the rules): the built-in ones and the person's antibot_rules."""
    from . import antibot, settings
    data = settings.read_file()
    mode = settings.resolve("antibot_tripwire", data)["value"]
    if mode not in ("pause", "warn"):
        return "off", ()
    own = settings.resolve("antibot_rules", data)["value"]
    return mode, antibot.BUILT_IN + antibot.person_rules(own)


async def _tripwire_probe(tab: Tab, rules: tuple, want_cf: bool) -> Optional[dict]:
    """The probe's answer, or None when the page could not be read."""
    from . import antibot
    js = _TRIPWIRE_PROBE_JS % (json.dumps(antibot.text_words(rules)), "true" if want_cf else "false")
    try:
        raw = await asyncio.wait_for(evaluate_isolated(tab, js), timeout=3.0)
    except Exception:
        return None
    data = parse_json(raw, None)
    return data if isinstance(data, dict) and data.get("href") is not None else None


def _tripwire_note(text: str) -> None:
    notes = _TRIPWIRE_NOTES.setdefault(BrowserState.session_key(), [])
    if text not in notes:
        notes.append(text)


def _tripwire_result_note(session_key: str, result: str) -> str:
    notes = _TRIPWIRE_NOTES.pop(session_key, None)
    return f"{result} [antibot_tripwire: {' '.join(notes)}]" if notes else result


def _handoff_clock() -> float:
    """The handoff's monotonic clock: navigation._clock once that module is loaded (tests replace
    it), so a tripwire request's grace is measured as wait_for_human measures it."""
    nav = sys.modules.get(f"{__package__}.tools.navigation")
    return (getattr(nav, "_clock", None) or time.monotonic)()

_HELP_REQUEST_SEQ = 0


def _help_owner(browser: Any) -> Optional[str]:
    if browser is BrowserState.browser:
        return BrowserState.current_instance_id
    return next((name for name, slot in BrowserState.instances.items() if slot.browser is browser), None)

def _record_human_help(tab: Tab, objective: str, timeout: float, *, by: str, clock: float,
                       baseline: Optional[dict], browser: Any, target: Optional[str] = None) -> dict:
    """Record one correlated help request for a browser and its owning instance."""
    global _HELP_REQUEST_SEQ
    key = target or _target_key(tab)
    _HELP_REQUEST_SEQ += 1
    request_id = f"help-{_HELP_REQUEST_SEQ}"
    owner = _help_owner(browser)
    if owner is None:
        raise RuntimeError("cannot identify the browser instance for this help request")
    for old_key, old in tuple(_HUMAN_HELP.items()):
        # A tripwire may replace an earlier tripwire request, but never an explicit
        # request_human_help request. A new explicit request may intentionally supersede either.
        if old.get("browser") is browser and (by == "agent" or old.get("by") == "tripwire"):
            old["replaced_by"] = request_id
            del _HUMAN_HELP[old_key]
            _tripwire_note(f"help request {old['request_id']} was replaced by {request_id}.")
    record = {
        "objective": objective,
        "target": key,
        "baseline": baseline,
        "requested_at": time.time(),
        "requested_clock": clock,
        "timeout": max(0.0, float(timeout)),
        "by": by,
        "request_id": request_id,
        "browser": browser,
        "instance_id": owner,
    }
    _HUMAN_HELP[key] = record
    record["asked_at"] = record["shown_at"] = presence.ask(browser, objective, request_id)
    record["was_shown"] = record["asked_at"] is not None
    return record


def _tab_for_target(target: Optional[str]) -> Optional[Tab]:
    if not target:
        return None
    candidates = list(BrowserState.tabs)
    browser = BrowserState.browser
    candidates.extend(getattr(browser, "tabs", ()) or ())
    return next((tab for tab in candidates if _target_key(tab) == str(target)), None)


async def _tripwire_look(tab: Tab, *, url: Optional[str] = None, want_cf: bool = False,
                         header: Optional[str] = None, loader: Optional[str] = None, settle_text: bool = False,
                         headless: Optional[bool] = None) -> Optional[_Trip]:
    """Whether `tab` shows a verification page, with the request for the person made when it does
    and the tripwire pauses. None when the tripwire is off (nothing read at all) or the page is no
    verification page as far as was looked; then a request the tripwire made for this tab is
    withdrawn, and the next result says so. A known unruled host with no Cloudflare signal
    needs no probe. An ordinary step checkpoint has no URL yet: its single evaluation
    also checks up to 16 direct same-origin frames, never cross-origin or nested documents."""
    from . import antibot
    mode, rules = _tripwire_settings()
    if mode == "off":
        return None
    browser = getattr(tab, "browser", None)
    verdict = antibot.url_verdict(url, rules) if url else None
    parent_rules = antibot.rules_for(antibot._host(url), rules) if url else []
    relevant = not url or bool(parent_rules) or want_cf
    if verdict is None and not relevant:
        _tripwire_left(browser, _target_key(tab))
        return None
    if verdict is None:
        # A bounded DOM probe is needed only for hosts with a configured rule or a CF look.
        # This preserves the zero-extra-CDP-call common path.
        seen = await _tripwire_probe(tab, rules, want_cf)
        if seen is None:
            return None  # unread: nothing known, and nothing withdrawn either
        if (settle_text and url and antibot.wants_text(url, rules) and not seen.get("words")
                and not seen.get("cf") and not seen.get("frames")):
            await asyncio.sleep(0.3)
            # Text that appears late is still a page read; re-check ownership immediately
            # before evaluating it because Take over may have happened during the delay.
            if presence.refusal("get_text", browser):
                return None
            seen = await _tripwire_probe(tab, rules, want_cf)
            if seen is None:
                return None
        href = str(seen.get("href") or url or "")
        verdict = antibot.url_verdict(href, rules)
        if verdict is None and seen.get("words"):
            verdict = antibot.text_verdict(href, seen["words"], rules)
        if verdict is None and seen.get("cf"):
            verdict = antibot.cloudflare_verdict(href, str(seen["cf"]), header)
        if verdict is None and header == "challenge" and loader and seen.get("solved") is False:
            # A new document, even at the same URL, cannot inherit the old challenge header.
            from nodriver.cdp import page as cdp_page
            tree = await asyncio.wait_for(tab.send(cdp_page.get_frame_tree()), timeout=1)
            if str(tree.frame.loader_id) == loader:
                verdict = antibot.cloudflare_verdict(href, "current document", header)
        if verdict is None:
            for frame in seen.get("frames") or ():
                if not isinstance(frame, dict):
                    continue
                frame_url = str(frame.get("href") or "")
                frame_verdict = antibot.url_verdict(frame_url, rules)
                if frame_verdict is None and frame.get("words"):
                    frame_verdict = antibot.text_verdict(frame_url, frame["words"], rules)
                if frame_verdict is not None:
                    verdict = replace(frame_verdict,
                                      trigger=frame_verdict.trigger + " (in child frame)")
                    break
    key = _target_key(tab)
    if verdict is None:
        _tripwire_left(browser, key)
        return None
    trip = _tripwire_seen(browser, key, mode, verdict,
                          BrowserState.current_headless if headless is None else headless)
    if trip.where == "ask":
        _tripwire_ask(tab, browser, trip, await _page_sig(tab))
    return trip


def _tripwire_left(browser: Any, key: Optional[str]) -> None:
    """The tab is on no verification page: forget the one reported, and withdraw the request the
    tripwire made for it (never one the agent made with request_human_help)."""
    seen = _TRIPWIRE_SEEN.pop(key, None)
    request = _HUMAN_HELP.get(key)
    if not request or request["browser"] is not browser or request.get("by") != "tripwire":
        return
    _HUMAN_HELP.pop(key, None)
    answered = presence.answered(browser, float(request.get("asked_at") or request.get("requested_at")
                                                or 0), request.get("request_id")) if request.get("was_shown") else None
    if presence.shows_request(browser, request.get("shown_at"), request.get("request_id")):
        presence.end_help(browser, request.get("request_id"))
    where = f" ({seen['place']})" if seen else ""
    if answered == "done":  # the person pressed Done and no wait took it: closed, not withdrawn
        _tripwire_note(f"the tab is no longer on the verification page{where}; the person had pressed "
                       "Done on the tripwire's request, which is now closed.")
        return
    _tripwire_note(f"the tab is no longer on the verification page{where}, so the request for help "
                   "the tripwire made was withdrawn.")


def _tripwire_seen(browser: Any, key: Optional[str], mode: str, verdict: Any, headless: bool) -> _Trip:
    """What a verification page found on a tab, preserving explicit agent handoffs."""
    seen = _TRIPWIRE_SEEN.get(key)
    same = bool(seen) and seen["host"] == verdict.host and seen["path"] == verdict.path
    _TRIPWIRE_SEEN[key] = {"host": verdict.host, "path": verdict.path, "place": verdict.place}
    if mode != "pause":
        return _Trip(mode, verdict, repeat=same)
    agent_key, agent_request = next(
        ((request_key, request) for request_key, request in _HUMAN_HELP.items()
         if request.get("browser") is browser and request.get("by") == "agent"),
        (None, None),
    )
    if agent_request is not None:
        trip = _Trip(mode, verdict, repeat=same, where="agent_pending", request=agent_request)
        if agent_key != key:
            trip.target = key
        return trip
    request = _HUMAN_HELP.get(key)
    if request is not None and request["browser"] is browser:
        where = ("pending" if request.get("by") != "tripwire"
                 else "bar" if request.get("was_shown") else "window")
        return _Trip(mode, verdict, repeat=same, where=where, request=request)
    if headless:
        return _Trip(mode, verdict, repeat=same, where="headless")
    return _Trip(mode, verdict, where="ask", again="after the request for it was answered" if same else "")


def _tripwire_ask(tab: Tab, browser: Any, trip: _Trip, baseline: Optional[dict], *, target: Optional[str] = None) -> None:
    """The tripwire's request for the person, recorded as request_human_help records it and shown
    in the overlay's bar when there is one. The window is not brought forward: the agent calls
    request_human_help itself for that."""
    record = _record_human_help(
        tab, f"This site shows a verification page ({trip.verdict.host}). Please complete it, "
             "then press Done.", TRIPWIRE_HELP_TIMEOUT, by="tripwire", clock=_handoff_clock(),
        baseline=baseline, browser=browser, target=target)
    trip.request = record
    trip.where = "bar" if record["was_shown"] else "window"


def _tripwire_after_action(tab: Tab, result: str, after: Optional[dict]) -> str:
    """Check the URL reached by the action and URLs of tabs it opened. The action itself did
    happen, so a detected verification page remains a non-error action result."""
    from . import antibot
    if result.startswith("Error:"):
        return result
    mode, rules = _tripwire_settings()
    if mode == "off":
        return result
    candidates = []
    url = (after or {}).get("url")
    if url:
        candidates.append((None, url))
    before_pages = (after or {}).get("before_pages")
    pages = (after or {}).get("pages") or {}
    if isinstance(before_pages, dict) and isinstance(pages, dict):
        candidates.extend((target, page_url) for target, page_url in pages.items()
                          if target not in before_pages and target in (after.get("opened_here") or ()))
    verdict = None
    target = None
    for candidate_target, candidate_url in candidates:
        found = antibot.url_verdict(candidate_url, rules)
        if found is not None:
            verdict, target = found, candidate_target
            if candidate_target is not None:
                verdict = replace(verdict, trigger=verdict.trigger + " (in opened tab)")
            break
    if verdict is None:
        return result
    try:
        browser = tab.browser  # the opener owns the popup even before it enters the tab cache
        key = str(target) if target is not None else _target_key(tab)
        trip = _tripwire_seen(browser, key, mode, verdict, BrowserState.current_headless)
        if target is not None:
            trip.target = key
        if trip.where == "ask":
            _tripwire_ask(tab, browser, trip, None if target else after, target=target)
        run = _TRIPWIRE_RUN.get()
        if target and mode == "pause" and run:
            run["popup"] = (browser, trip)
    except Exception:
        return result
    if mode == "warn":
        return antibot.with_first_line(result, antibot.warning_line(verdict, trip.repeat))
    return antibot.with_first_line(result, antibot.action_line(verdict, trip.where))


_TRIPWIRE_PENDING = {
    "bar": "the request is pending in the overlay's bar: wait_for_human",
    "window": ("the request is pending, and no overlay shows it: request_human_help brings the "
               "window forward, then wait_for_human"),
    "pending": "a request made with request_human_help is pending on this tab: wait_for_human",
    "agent_pending": ("an explicit request made with request_human_help is still pending in the "
                      "overlay's bar: call wait_for_human"),
    "headless": "this browser is headless, so nobody can see a request: a visible browser is needed",
}


def _tripwire_pause(trip: _Trip, lead: str, *, asked_for: Optional[str] = None,
                    launched: bool = False) -> str:
    """The pause, as an error a workflow, an agent and a person all read the same way: the status
    word right after "Error: ", then trigger, page, asked for and next. `lead` is the first line's
    sentence ("navigate reached a verification page"). A page reported before whose request is
    still pending is said in one line."""
    from . import antibot
    verdict = trip.verdict
    running = "; the browser is running and stays open on it" if launched else ""
    if trip.repeat:
        popup = f" on popup tab {trip.target}" if trip.target else ""
        return err(f"{antibot.PAUSED}: still on the verification page reported before "
                   f"({verdict.place}){popup}{running}; {_TRIPWIRE_PENDING[trip.where]}.")
    if trip.again:
        lead += f", again {trip.again}; the person was asked again"
    elif launched:
        lead += running
    elif asked_for:
        lead += ", not the page asked for"
    if trip.target:
        lead += f" on popup tab {trip.target}"
    run = _TRIPWIRE_RUN.get()
    again = "run resume_with" if run else "navigate again"
    cloudflare = verdict.kind == "cloudflare"
    return err(f"{antibot.PAUSED}: {lead}.\n" + antibot.pause_lines(
        verdict, asked_for, antibot.next_line(trip.where, again, cloudflare),
        trip.auto_verify if cloudflare else ""))


def _tripwire_render(trip: Optional[_Trip], tool: str, result: str, *,
                     asked_for: Optional[str] = None, launched: bool = False) -> str:
    """A navigation tool's result through the tripwire: its own (nothing found, or off), its own
    with the warning line first (warn), or the pause in its place (pause). With expect_url or
    expect_status the pause is the answer: it is the more exact reason."""
    from . import antibot
    if trip is None:
        return result
    if trip.mode == "warn":
        return antibot.with_first_line(result, antibot.warning_line(trip.verdict, trip.repeat))
    return _tripwire_pause(trip, f"{tool} reached a verification page", asked_for=asked_for,
                           launched=launched)


async def _tripwire_after_navigation(tab: Tab, observed: Optional[dict], *,
                                     verify_suffix: Optional[str] = None,
                                     doc_state: Optional[dict] = None,
                                     headless: Optional[bool] = None) -> Optional[_Trip]:
    """Look at the reached document, retaining the main response's challenge evidence. A launch
    with a supplied URL also checks the current DOM when there was no observed response."""
    header = (doc_state or {}).get("cf_mitigated")
    left = bool(verify_suffix) and "auto-verify" in str(verify_suffix) \
        and "challenge gone" not in str(verify_suffix)
    try:
        trip = await _tripwire_look(tab, url=(observed or {}).get("final_url"),
                                    want_cf=left or header == "challenge", header=header,
                                    loader=(doc_state or {}).get("cf_loader"), settle_text=True,
                                    headless=headless)
    except Exception:
        return None  # a look that failed leaves the navigation's own result, as before it existed
    if trip is not None and left:
        # Its own words on what was tried and the tools that fit stay in the pause.
        trip.auto_verify = str(verify_suffix).strip().removeprefix("[auto-verify:").removesuffix("]").strip()
    return trip


def _popup_handoff(browser: Any, trip: _Trip) -> _Trip:
    """The popup URL was just confirmed. Reuse the click's handoff only while it is pending."""
    old = trip.request
    if old is None:  # headless: the click never asked anybody
        return trip
    request_id, key = old["request_id"], old["target"]
    answered = None
    if _HUMAN_HELP.get(key) is old:
        shown = old["shown_at"]
        if shown is not None:
            answered = presence.answered(browser, shown, request_id)
        if not answered and (not old["was_shown"] or presence.shows_request(browser, shown, request_id)):
            return trip
        if not answered and old["by"] == "agent":
            # An explicit request whose bar stopped showing it still belongs to the agent.
            trip.where = "pending"
            return trip
        del _HUMAN_HELP[key]
        presence.end_help(browser, request_id)
    # A wait may already have consumed it, or another request may now own the bar. Apply
    # the normal browser-local priority rules again; never close that other request's id.
    fresh = _tripwire_seen(browser, trip.target, trip.mode, trip.verdict, headless=False)
    fresh.target = trip.target
    if fresh.where == "ask":
        _tripwire_ask(None, browser, fresh, None, target=trip.target)
        if not answered:
            fresh.again = "because the earlier request is no longer pending"
    return fresh


async def _tripwire_step(previous: Optional[str], next_tool: str) -> Optional[str]:
    """Before a step of workflow_run or batch_actions: the step before may have sent the page away
    (a click), or reached a Cloudflare challenge a navigation did not look for (auto_verify=False,
    no header). None to go on, a pause (antibot.PAUSE_PREFIX) to stop before the step, or a warning
    line. Before a call's first step only a step that is no navigation is looked before: a
    navigation leaves the page, and looks at the one it reaches. A page said before by the step
    that led there is not said again in warn mode."""
    from . import antibot
    if previous is None and next_tool in antibot.NAVIGATION_TOOLS:
        return None
    if not BrowserState.is_up():
        return None
    try:
        run = _TRIPWIRE_RUN.get()
        popup = run.pop("popup", None) if run else None
        trip = None
        if popup is not None:
            browser, trip = popup
            # Only revisit a popup whose URL matched at click time. Target metadata suffices:
            # never inspect a popup's document or keep a verdict after its URL changed.
            pages = await asyncio.wait_for(_page_targets(browser), timeout=2)
            current = dict(pages).get(trip.target)
            if current != trip.verdict.url:
                _tripwire_left(browser, trip.target)
                trip = None
            else:
                trip = _popup_handoff(browser, trip)
        if trip is None:
            trip = await _tripwire_look(BrowserState.active_tab(), want_cf=True)
    except Exception:
        return None  # no tab to look at, or a look that failed: the step runs as before
    if trip is None:
        return None
    if trip.mode == "warn":
        return None if trip.repeat else antibot.warning_line(trip.verdict, False)
    # Whole, also for a page said before (by the click that led there): the run's report names
    # what fired and where once, and a resume reads it.
    trip.repeat = False
    after = f"after the step before ({previous})" if previous else "before the first step"
    return _tripwire_pause(trip, f"the tab is on a verification page {after}; the run stopped "
                                 "before its next step")


# The note a withdrawn request leaves goes on the next result of the session, whatever tool gave it.
_app_module.RESULT_FILTERS[:] = [
    h for h in _app_module.RESULT_FILTERS if getattr(h, "__name__", "") != "_tripwire_result_note"
] + [_tripwire_result_note]


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

    The navigation commit is bounded by 30 seconds (the page is not stopped); a timeout says
    the observed URL and ready_state and that the tab may still be loading. wait_until=load then
    waits for readyState=complete up to 15 seconds after the commit.

    Reports the OBSERVED postconditions: requested_url, final_url, redirect_chain, status, ready_state. After
    a redirect, read final_url.

    auto_verify: if True (default), detect a Cloudflare / Turnstile challenge after load and click its
    checkbox (CDP-level click, max 2 attempts). False opts out.

    A site's verification page (a bot check that stayed) answers with the antibot_tripwire setting: by
    default an error starting "antibot_paused:" that says what fired, asking the person in the overlay's bar.

    focus: if True, bring the tab's window to the OS foreground after navigating. Off by default, since it
    takes focus from whatever the person is doing; tab_focus does the same on request.

    expect_url: fail when final_url does not match. Glob when the pattern contains * ? or [ ; otherwise a
        prefix match, so "https://x/admin" accepts "https://x/admin?tab=1". A final_url that could not be
        read FAILS the check.
    expect_status: fail when the document HTTP status differs. An unmeasured status (about:blank,
        same-document navigation) also fails.
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
        timeout_observation = await _commit_navigation(tab, url, doc_state)
        if timeout_observation is not None and not timeout_observation.get("_commit_timeout_recovered"):
            return _nav_commit_timeout("navigate", timeout_observation)
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
        doc = disarm()
        observed = _carry_nav_timeout_recovery(
            await _observe_navigation(tab, url, doc), timeout_observation)
        result = _nav_result(
            "Navigated", observed,
            expect_url=expect_url, expect_status=expect_status, suffix=verify_suffix,
        )
        trip = await _tripwire_after_navigation(tab, observed, verify_suffix=verify_suffix,
                                                doc_state=doc)
        return _tripwire_render(trip, "navigate", result, asked_for=url)
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

    Dispatches a trusted mouse event at the element's centre, scrolling it into view first if needed. Refuses
    and names the blocker when the point belongs to something else, so an overlay never produces a false
    "clicked". The result names what was used: method=cdp_mouse, or method=js_click after js_fallback.

    Args:
        ref: element ref from browser_snapshot
        selector: CSS selector
        humanize: approach along a curved path and click a random spot inside
        verify: compare the page before and after, and say so when nothing changed
        js_fallback: when the real click cannot land, call element.click() from JS instead of refusing. The
            event has isTrusted=false, which sites can detect, and bypasses whatever was covering the element.
            The result states when this happened. Off by default.
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
            infos = await tab.send(cdp_target.get_targets())
            sig["pages"] = {str(info.target_id): str(info.url) for info in infos if info.type_ == "page"}
            sig["opened_here"] = [str(info.target_id) for info in infos if info.type_ == "page"
                                  and str(getattr(info, "opener_id", "")) == _target_key(tab)]
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


def _click_outcome(tab: Tab, head: str, detail: str, before: dict, after: dict) -> str:
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
        # A click that led to a site's verification page says so first (antibot_tripwire).
        checked_after = dict(after)
        checked_after["before_pages"] = before.get("pages")
        return _tripwire_after_action(tab, ok(f"{head} — {detail}{change}"), checked_after)
    return ok(
        f"{head} but NO page change detected within {_VERIFY_WINDOW_MS}ms — {detail}. "
        f"The event was dispatched, but no page change was observed within this verification "
        f"window; a later reaction or a purely visual effect is still possible."
    )


# After an action the page is read again at these pauses (0, 50, 120, 220, 330 and 450 ms
# in all) and the reading stops at the first one that differs. A fixed 350 ms was paid by
# every click, although a handler that changes the page has done so before the click is even
# acknowledged. Only "NO page change" needs the whole wait, and it gets a longer one than
# before, so a handler that reacts after 300 ms is not reported as dead.
_VERIFY_PAUSES = (0.0, 0.05, 0.07, 0.10, 0.11, 0.12)
_VERIFY_WINDOW_MS = round(sum(_VERIFY_PAUSES) * 1000)
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


# The --remote-allow-origins a browser was started with, recorded at launch. Without one Chrome
# refuses every websocket that carries an Origin, so no web page can reach the debugging port;
# a caller can still ask for it in extra_args, and then results and server_status say so.
_ALLOW_ORIGINS_ATTR = "_mcp_remote_allow_origins"
_ALLOW_ORIGINS_FLAG = re.compile(r"--remote-allow-origins=(\S+)")


def _allow_origins_by_instance() -> dict:
    """Per instance of this session: the --remote-allow-origins its browser runs with."""
    found = {}
    if BrowserState.browser is not None:
        found[BrowserState.current_instance_id] = _allow_origins_of(BrowserState.browser)
    for iid, snap in BrowserState.instances.items():
        if snap.browser is not None:
            found[iid] = _allow_origins_of(snap.browser)
    return found


def _mark_allow_origins(browser: Any, config: Any) -> None:
    asked = [str(a).split("=", 1)[1] for a in (getattr(config, "_browser_args", None) or [])
             if str(a).startswith("--remote-allow-origins=")]
    with contextlib.suppress(Exception):
        setattr(browser, _ALLOW_ORIGINS_ATTR, asked[-1] if asked else "off")


def _allow_origins_of(browser: Any) -> str:
    """"off", the origins a caller allowed, "unknown", or why there is no answer."""
    recorded = getattr(browser, _ALLOW_ORIGINS_ATTR, None)
    if recorded:
        return recorded
    pid = _browser_process_pid(browser)
    if pid is None:
        return "not started by this server"
    command = _process_command(pid)
    if command is None:
        return "unknown"
    found = _ALLOW_ORIGINS_FLAG.findall(command)
    value = found[-1] if found else "off"
    with contextlib.suppress(Exception):
        setattr(browser, _ALLOW_ORIGINS_ATTR, value)
    return value


def _allow_origins_note(browser: Any) -> str:
    value = getattr(browser, _ALLOW_ORIGINS_ATTR, None)
    if not value or value == "off":
        return ""
    who = "any web page" if value == "*" else f"a page from {value}"
    return (f" [remote_allow_origins={value}: kept because extra_args asked for it; {who} that learns "
            f"this browser's websocket URL can drive it]")


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
    await presence.before_input(tab)  # refuses once the person took over; lets their typing end
    with contextlib.suppress(Exception):
        await _watch_dialogs(tab)
    key = _target_key(tab)
    if not BrowserState.current_headless:
        pid = _browser_process_pid(BrowserState.browser)
        front = await asyncio.to_thread(_frontmost_pid) if pid else None
        if front and front != pid:
            _spawn_background(_win_focus_back_from_popup(pid, front))
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
    return _click_outcome(tab, head, detail, before, after)


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
        await presence.before_input(tab)  # a JS click is input too
        await _js_click(el)
        head = (f"{label}: clicked via JS FALLBACK (isTrusted=false, real click refused: "
                f"{reason})")
        detail = f"method=js_click {target}"
        if not verify:
            return ok(f"{head} — {detail}")
        return _click_outcome(tab, head, detail, before, await _sig_after_action(tab, before, el, pages=True))

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
    return _click_outcome(tab, head, detail, before, after)


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
            # The first match only out of view, and one reachable element with the text: that
            # element is the first match itself (an element only out of view is reachable, so it
            # is among those found), and "(scrolled into view)" says all. "First text match was
            # unreachable, used a visible one of 1 candidates" read as if another element had been
            # clicked. Hidden, covered, disabled or zero-size: another element was, and it says so.
            found = cand.get("found") or 0
            why = _blocked_reason(probe) if el is not None else None
            only_out_of_view = (el is not None and bool(probe) and probe.get("sized") and not probe.get("hidden")
                                and not probe.get("disabled") and not probe.get("in_viewport"))
            same = only_out_of_view and found == 1
            note = (f"{label}: clicked — method=cdp_mouse target=<{cand.get('target')}> "
                    f"at ({cand['x']},{cand['y']})"
                    + (" (scrolled into view)" if cand.get("scrolled") else "")
                    + (f"; the first text match was unreachable ({why}), so a reachable element "
                       f"with the same text was clicked" + (f" ({found} found)" if found > 1 else "")
                       if el is not None and not same else ""))
            if _dialog_note(tab):
                return ok(note + _dialog_note(tab))
            if not verify:
                return ok(note)
            after = await _sig_after_action(tab, before, pages=True)
            if _dialog_note(tab):
                return ok(note + _dialog_note(tab))
            head, _, detail = note.partition(" — ")
            return _click_outcome(tab, head, detail, before, after)

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
    """Clear a field and type into it with real key presses, then read it back.

    A value the page only formatted (a card mask adding spaces, a date mask adding "/") is
    filled and says how it reads; a value the page cut or changed is an error. Readonly,
    disabled and contenteditable=false fields are reported as not editable with the value
    actually read after the attempt.
    """
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        restrictions = await el.apply(
            """function() { return JSON.stringify({
                readonly: this.readOnly === true,
                disabled: this.disabled === true,
                contenteditable_false: this.getAttribute('contenteditable') === 'false'
            }); }"""
        )
        try:
            restrictions = json.loads(restrictions or "{}")
        except (TypeError, ValueError):
            restrictions = {}
        restriction = next((name for name, present in (
            ("readonly", restrictions.get("readonly")),
            ("disabled", restrictions.get("disabled")),
            ("contenteditable=false", restrictions.get("contenteditable_false")),
        ) if present), None)
        if restriction:
            actual = await el.apply(
                "function() { return this.isContentEditable ? this.innerText : "
                "(typeof this.value === 'string' ? this.value : this.innerText); }"
            )
            is_secret = (await el.apply("function() { return this.type === 'password'; }")) is True
            shown = f"{len(actual)} chars" if is_secret and isinstance(actual, str) else repr(actual[:80])
            return err(f"field is {restriction} (not editable); after the attempted fill it holds {shown}")
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
        except presence.PersonHasBrowser:
            raise  # never the JS fallback: the person took the browser
        except Exception:
            await el.set_value(value)
        actual = await el.apply(
            "function() { return this.isContentEditable ? this.innerText : "
            "(typeof this.value === 'string' ? this.value : this.innerText); }"
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
            # An input mask that only adds separators kept every character: PXP's card field
            # reads '4111 1111 1111 1111' and its expiry '12/30', and calling that an error made a
            # filled checkout look failed. A character dropped or changed is still one.
            if _FORMAT_SEPARATORS.sub("", actual) == _FORMAT_SEPARATORS.sub("", wanted) != "":
                return ok(f"filled; the page formatted it as {shown}")
            return err(
                f"typed, but the field reads {shown} rather than the "
                f"{len(str(value))}-char value sent. The page rewrote or rejected it "
                f"(input mask, maxlength, framework state)."
            )
        return ok("filled")
    except Exception as e:
        return err(str(e))


# Spaces (also no-break) and the punctuation masks add. Not newlines or tabs: a single-line field
# that drops a newline lost a character, and fill must say so.
_FORMAT_SEPARATORS = re.compile(r"[ \u00a0/.\-()]+")


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
        await presence.before_input(tab)
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
                await presence.before_input(tab)  # a JS click is input too
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
# 10. JAVASCRIPT — moved to tools/scripting.py
# ══════════════════════════════════════════════════════════════════════════


def _js_exception_text(details: Any) -> str:
    thrown = getattr(details, "exception", None)
    return str(getattr(thrown, "description", None) or getattr(thrown, "value", None)
               or getattr(details, "text", "exception"))


# ══════════════════════════════════════════════════════════════════════════
# 13. BATCH — moved to tools/batch.py
# ══════════════════════════════════════════════════════════════════════════


def _inner_arguments_error(tool: str, args: dict) -> Optional[str]:
    """The refusal a direct call would get for arguments `tool` does not take, for a step of
    batch_actions or workflow_run, which call the function itself and so skip that check.
    A step that is no MCP tool (sleep) is checked against its own schema, not let through."""
    registered = mcp._tool_manager.get_tool(tool)
    if registered is None and tool in _STEP_ONLY_TOOLS:
        registered = _StepSchema(parameters=_STEP_ONLY_TOOLS[tool][1])
    if registered is None:
        return None
    return _app.unknown_arguments_error(tool, registered, args)


# ══════════════════════════════════════════════════════════════════════════
# 14. VIEWPORT + SCROLL — moved to tools/viewport.py
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# 15. DIALOG + ACCESSIBILITY — moved to tools/dialogs.py
# ══════════════════════════════════════════════════════════════════════════

# Read-at-fire-time config so re-arming with a new action/text updates the
# response without stacking another CDP handler. Keyed by CDP target id.
_dialog_pre_action: dict[str, Any] = {
    "action": None, "text": None, "_registered_tab_ids": set(),
}


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


# Active subscription per target: owning index, tab wrapper and exact registered callbacks.
_NETWORK_CAPTURES: dict[str, tuple[dict, Any, tuple]] = {}


def _clear_tab_scoped_state(tabs: list[Any]) -> None:
    """Drop lifecycle state for closed targets without touching live instances."""
    keys = {_target_key(tab) for tab in tabs}
    if not keys:
        return
    forget_refs(keys)
    for key in keys:
        _page._SNAPSHOT_CACHE.pop(key, None)
        _HUMAN_HELP.pop(key, None)
        _TRIPWIRE_SEEN.pop(key, None)  # the verification page the tripwire last said on it
        _dialog_pre_action["_registered_tab_ids"].discard(key)
        _DIALOG_AUTO_CFG["_registered_tab_ids"].discard(key)
        _DIALOG_WATCHED.discard(key)
        _OPEN_DIALOGS.pop(key, None)
        subscription = _NETWORK_CAPTURES.pop(key, None)
        if subscription is not None:
            _drop_handlers(subscription[1], subscription[2])
        _NETWORK_KEPT_BODIES.discard(key)
        _NETWORK_DOC_LOADER.pop(key, None)
        presence._OVERRIDES.pop(key, None)  # emulate_device's record, for the overlay's lock
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
        # A rule riding on block_ads stops at once: it answers only while active and current.
        with _capture._INTERCEPT_LOCK:
            _capture._INTERCEPT.update({
                "active": False, "tab": None, "handler": None, "handler_tab": None,
                "watchdog": None, "stop_reason": "target closed", "disable_error": None,
                "broker": None, "broker_token": None,
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


# ══════════════════════════════════════════════════════════════════════════
# 16. CONSOLE + NETWORK — moved to tools/monitoring.py
# ══════════════════════════════════════════════════════════════════════════


# Tabs whose DevTools session had Chrome keep bodies past a navigation at some point.
_NETWORK_KEPT_BODIES: set = set()


# Target key -> loaderId of the tab's current main-frame document, to tell a body lost to a
# navigation from any other getResponseBody error.
_NETWORK_DOC_LOADER: dict[str, str] = {}


_NETWORK_GET_MAX_CHARS = 50000


# What a reveal_values="body" reply of network_get and auth_capture carries beside its entries.
# reveal_note, not note: the note of a reply cut for size replaced the warning.
_BODY_MODE_EXTRA = {"reveal_values": "body", "reveal_note": BODY_REVEAL_NOTE}


def _bounded_entries(entries: list, extra: Optional[dict] = None) -> str:
    """The entries as a JSON list, or, past _NETWORK_GET_MAX_CHARS, an object naming how many
    of the oldest were left out. A plain slice of the text cut a body in half and returned
    JSON that no longer parsed, with nothing saying so. With `extra` (reveal_values="body")
    the reply is always an object holding those keys and `entries`."""
    if extra:
        text = json.dumps({**extra, "entries": entries}, indent=2, default=str)
    else:
        text = json.dumps(entries, indent=2, default=str)
    if len(text) <= _NETWORK_GET_MAX_CHARS:
        return text
    kept = list(entries)
    while kept:
        kept = kept[max(1, len(kept) // 10):] if len(kept) > 10 else kept[1:]
        text = json.dumps({**(extra or {}), "entries": kept}, indent=2, default=str)
        if len(text) <= _NETWORK_GET_MAX_CHARS - 400:
            break
    left_out = len(entries) - len(kept)
    return json.dumps({
        **(extra or {}),
        "entries": kept,
        "omitted": left_out,
        "note": (f"{left_out} of {len(entries)} entries, the oldest, were left out to stay under "
                 f"{_NETWORK_GET_MAX_CHARS} chars; ask with a smaller limit, filter_url, "
                 "resource_type, max_body_bytes or max_url_len"),
    }, indent=2, default=str)


# ══════════════════════════════════════════════════════════════════════════
# 17. DEBUG / META — moved to tools/diagnostics.py
# ══════════════════════════════════════════════════════════════════════════


# Importing Quartz and AppKit for the first time takes seconds while the server is starting
# (profile_gc and the Dock check run then too), and a 3 s wait on it made the first
# server_status say "not measured (TimeoutError)". So it is measured on a thread from the start,
# and a status call that comes before the answer says it is still being measured.
_OS_CURSOR_PROBE: dict = {"thread": None, "started": None, "result": None, "measured_at": None}
_OS_CURSOR_PROBE_LOCK = threading.Lock()


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


# Request interception, export_har and dry_run live in tools/capture.py. _drop_handlers stays
# here because console, network and wait tools use it too.


def _drop_handlers(tab: Any, pairs: Any) -> None:
    """Remove exactly these closures. tab.remove_handler(evt, fn) drops the whole list for
    the event on nodriver 0.48.1, which would kill a concurrent capture."""
    for event_type, handler in pairs:
        with contextlib.suppress(KeyError, ValueError, AttributeError):
            tab.handlers[event_type].remove(handler)


# fingerprint_rotate, the identity tools, humanize_type, storage_state_save and
# storage_state_load, solve_captcha and verify_cf live in tools/stealth.py. _apply_storage_state stays
# here because launch paths and the os-cursor solver share it.


async def _apply_storage_state(browser: Browser, path: str, tab: Any = None) -> dict:
    """Load cookies+localStorage from a JSON file into browser. Returns what happened.

    Raises when the file had cookies and none could be set: a session that did not
    load must not read as one that did. `tab` is the page navigated to each origin; left out
    (a browser this launch just started) it is the first one.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} holds a JSON {type(data).__name__}, not a storage state object")
    from nodriver.cdp import network as cdp_network
    if tab is None:
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
# 23. MULTI-INSTANCE BROWSER MANAGEMENT — moved to tools/instances.py
# ══════════════════════════════════════════════════════════════════════════
#
# Run multiple isolated browsers in parallel — one per account, per site, or
# per worker. Each instance has its own profile, tabs, cookies, and logs.
# Idle instances auto-close after configurable timeout (prevents memory leaks).
#
# The tools and _launch_browser_instance live in tools/instances.py. What they share with
# browser_launch stays here: starting Chrome, the idle reaper and the window watch.


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
    run_marker.mark(config)  # only while a test run sets MCP_STEALTH_TEST_RUN
    for attempt in (1, 2):
        try:
            browser = await asyncio.wait_for(
                nodriver.start(config=config), timeout=BROWSER_LAUNCH_TIMEOUT,
            )
            _record_pid_start(browser)
            return browser
        except asyncio.CancelledError:
            await _reap_failed_start(config)
            raise
        except Exception as exc:
            slow = "Failed to connect" in str(exc)
            adopted = await _adopt_slow_chrome(config) if slow else None
            if adopted is not None:
                _record_pid_start(adopted)
                return adopted
            reap = await _reap_failed_start(config)
            if attempt == 2 or not slow:
                _remove_temp_profile(config)
                exc.reap_report = reap
                raise
            config.host, config.port = host, port
            await asyncio.sleep(1.0)
    raise AssertionError("unreachable")


def _record_pid_start(browser: Any) -> None:
    """When this browser's process started, read once at launch: a close later signals only a
    process with that start time, so a PID handed to another program meanwhile is left alone."""
    pid = _browser_process_pid(browser)
    if isinstance(pid, int) and pid > 0:
        with contextlib.suppress(Exception):
            quit_watch.pid_start(browser, pid)


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


def _being_closed(instance_id: str) -> bool:
    """Another close of this instance is under way; a sweep leaves it to that one."""
    return (BrowserState.session_key(), instance_id) in _app_module.INSTANCE_CLOSING


@contextlib.contextmanager
def _closing_in_background(instance_id: str):
    """While an instance is being closed (by a sweep, close_instance, close_all_instances or
    browser_close), a call on it waits (_app.INSTANCE_CLOSING) instead of acting on a browser
    half closed or switching it in while the close goes on, and a sweep does not close it too.
    Entered with no await before it, after any wait_while_closing, so two closers never overlap."""
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
        if not _still_held(iid, snap, snap.browser) or _being_closed(iid) or _being_launched(iid):
            continue  # switched in while an earlier one was being closed, closing or launching
        if snap.is_running() and snap.is_idle_expired():
            BrowserState.window_closed[iid] = _idle_closed_note(iid, snap.idle_timeout)
            if not await _end_stored_instance(iid, snap, "idle timeout"):
                BrowserState.window_closed.pop(iid, None)
    # A tool call running in this session is use of its current instance, however long it takes.
    if (BrowserState.is_up()
        and BrowserState.current_idle_timeout > 0
        and not _being_closed(BrowserState.current_instance_id)
        and not _being_launched(BrowserState.current_instance_id)
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


def _window_watch_tool_closed_undo(browser: Any) -> None:
    """A tab_close that marked the browser did not close its tab after all."""
    quit_watch.unmark_tool_closed(browser)
    entry = _WINDOW_WATCH.get(id(browser))
    if entry is not None:
        entry["tool_closed"] = False


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
        if _being_closed(iid) or _being_launched(iid):
            continue  # a tool or another sweep is closing it, or a launch is not done with it
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
        if _being_closed(iid) or _being_launched(iid):
            continue  # a close or a launch of it began while the count was asked for
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


# ══════════════════════════════════════════════════════════════════════════
# 24. CHROME PROFILE INTEGRATION (list / clone existing profiles) — moved to tools/chrome_profiles.py
# ══════════════════════════════════════════════════════════════════════════


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
# 25. LLM-OPTIMIZED ACTION KIT — moved to tools/action_kit.py
# ══════════════════════════════════════════════════════════════════════════


# ── workflow_run (resumable) ───────────────────────────────────────────────

# Map of tool-name → function ref for workflow_run dispatch. Only a curated
# subset is exposed. batch_actions and workflow_run share it, so a step type means
# the same thing in both.
_WORKFLOW_TOOLS: dict[str, Any] = {}

from . import workflow as _workflow  # noqa: E402

# Steps that are no MCP tool: name -> (function, the schema _inner_arguments_error checks
# their arguments against). Not advertised as tools, so the tool count does not change.
_STEP_ONLY_TOOLS: dict[str, tuple[Any, dict]] = {
    "sleep": (_workflow.sleep_step, _workflow.SLEEP_PARAMETERS),
}


@dataclass(frozen=True)
class _StepSchema:
    """What _app.unknown_arguments_error reads of a registered tool: its parameter schema."""
    parameters: dict


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
        "mouse_click_xy", "mouse_drift",
        # Readers (evaluate among them): a later step can branch on or iterate over what these
        # return, and a summary of the run keeps their result.
        *_workflow.READERS,
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
    for name, (fn, _schema) in _STEP_ONLY_TOOLS.items():
        _WORKFLOW_TOOLS[name] = fn


# ══════════════════════════════════════════════════════════════════════════
# 26. NETWORK + AUTH + WAIT — moved to tools/action_kit.py
# ══════════════════════════════════════════════════════════════════════════


def _add_all_handlers(tab: Any, handlers: list) -> None:
    """All or none: a caller that gets an exception has no disarm to clean up with."""
    try:
        for event_type, handler in handlers:
            tab.add_handler(event_type, handler)
    except Exception:
        _drop_handlers(tab, handlers)
        raise


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


# ══════════════════════════════════════════════════════════════════════════
# ATTACH MODE — moved to tools/attach.py
# ══════════════════════════════════════════════════════════════════════════

# Tracks whether current BrowserState was created via attach (True) or
# spawned by us (False). detach() is a no-op for spawned browsers.
_ATTACHED_BROWSERS: set[str] = set()


from .tools import storage as _storage  # noqa: E402,F401  -- registers cookie and storage tools


from .tools import capture as _capture  # noqa: E402  -- registers interception, HAR and dry_run tools


from .tools import page as _page  # noqa: E402  -- registers snapshot, reading, frame and scraping tools


from .tools import navigation as _navigation  # noqa: E402,F401  -- registers history, tab and human-help tools


from .tools import interaction as _interaction  # noqa: E402,F401  -- registers pointer, keyboard, form and wait tools


from .tools import mouse_kit as _mouse_kit  # noqa: E402,F401  -- registers the precision mouse tools


from .tools import monitoring as _monitoring  # noqa: E402,F401  -- registers console and network tools


from .tools import diagnostics as _diagnostics  # noqa: E402,F401  -- registers server_status and the debug tools


from .tools import stealth as _stealth  # noqa: E402,F401  -- registers identity, session, captcha and verify tools


from .tools import scripting as _scripting  # noqa: E402,F401  -- registers evaluate, wait_for_function, inject_init_script


from .tools import batch as _batch  # noqa: E402,F401  -- registers batch_actions, fill_form, navigate_and_snapshot


from .tools import viewport as _viewport  # noqa: E402,F401  -- registers the viewport and scroll tools


from .tools import dialogs as _dialogs  # noqa: E402,F401  -- registers dialog_handle, dialog_auto_handle, accessibility_snapshot


from .tools import chrome_profiles as _chrome_profiles  # noqa: E402,F401  -- registers list_chrome_profiles, clone_chrome_profile


from .tools import action_kit as _action_kit  # noqa: E402,F401  -- registers describe_page, smart_fill, the assert, storage, workflow and wait tools


from .tools import instances as _instances  # noqa: E402,F401  -- registers spawn_browser and the instance tools


from .tools import attach as _attach  # noqa: E402,F401  -- registers attach_to_chrome, detach, list_external_chrome


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


def _stop_all_browsers() -> None:
    """Stop every browser this process started, in every session and the pool.

    Stopping only the current one left stored instances running after the server
    exited, with nothing remaining that knew about them.
    """
    # block_ads first: its connections go, and every target they may hold is started, once.
    with contextlib.suppress(Exception):
        if _adblock_if_loaded() is not None:
            _adblock_if_loaded().shutdown()
    # Body saving the same way (network_start(save_bodies=...)): it holds new targets too.
    with contextlib.suppress(Exception):
        body_saver.shutdown()
    with contextlib.suppress(Exception):
        presence.stop_all()
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
            owned = {BrowserState.current_instance_id: set(BrowserState.owned_targets)}
            owned.update({iid: set(s.owned_targets) for iid, s in BrowserState.instances.items()})
            for iid, browser, tabs, active_index, profile, privacy in views:
                with contextlib.suppress(Exception):
                    _leave_or_end_at_exit(browser, tabs, active_index, profile, privacy,
                                          owned=owned.get(iid))
        finally:
            BrowserState.unbind_session(token)


def _leave_or_end_at_exit(browser: Any, tabs: list, active_index: int, profile: Any,
                          privacy: bool, owned: Optional[set] = None) -> str:
    """At exit, with no event loop: leave a browser kept alive running, end every other one."""
    if browser is None:
        return "none"
    pid = _browser_process_pid(browser)
    if (getattr(browser, _keep_alive.ATTR, False) and not privacy and profile is not None
            and isinstance(pid, int) and _process_exists(pid)):
        keys = [_target_key(t) for t in tabs]
        active = keys[active_index] if 0 <= active_index < len(keys) else None
        # The launcher's own process opened this browser for the person: none of its tabs is an
        # agent's, so a later reattach takes over none of them.
        mine = [] if _keep_alive.OPENED_BY == _keep_alive.LAUNCHER else [
            k for k in keys if owned is None or not owned or k in owned]
        if _keep_alive.mark_kept(profile, pid, keys, active, owned=mine) is None:
            _keep_alive.release(browser)
            return "kept"
    config = getattr(browser, "config", None)
    if getattr(browser, "_process", None) is None and isinstance(pid, int) and pid > 0:
        # Reattached: nodriver's stop() has no process to end, so it would stay running.
        outcome = _keep_alive.close_at_exit(str(getattr(config, "host", "127.0.0.1")),
                                            int(getattr(config, "port", 0) or 0), pid,
                                            str(getattr(config, "user_data_dir", "") or ""),
                                            getattr(browser, quit_watch.START_ATTR, None))
        if outcome != "still running" and profile is not None:
            _keep_alive.remove(profile, pid)
        return outcome
    if not getattr(browser, "stopped", False):
        browser.stop()
    return "stopped"


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

    from . import launcher
    if sys.argv[1:2] and sys.argv[1] in launcher.COMMANDS:
        # Not a server: open the agent browser (or bring it forward), or build the Dock launcher.
        sys.exit(launcher.cli(sys.argv[1:]))
    cli = _parse_cli(sys.argv[1:])
    # A thread, so neither the start nor any tool waits on it; a failure stays in there.
    with contextlib.suppress(Exception):
        dock_icon.start_in_background()
    # Leftover privacy and fallback profiles: one pass on its own thread, see profile_gc.
    with contextlib.suppress(Exception):
        profile_gc.start_in_background(state_data_root())
    # Plain-text cookie backups of finished Chrome 158 moves, once their retention has passed.
    with contextlib.suppress(Exception):
        def _cookie_leftovers() -> None:
            with contextlib.suppress(Exception):
                cookie_migration.expire_backups(state_data_root() / "storage-states")
            with contextlib.suppress(Exception):
                cookie_migration.remove_reader_leftovers()

        threading.Thread(target=_cookie_leftovers, daemon=True, name="cookie-leftovers").start()
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
