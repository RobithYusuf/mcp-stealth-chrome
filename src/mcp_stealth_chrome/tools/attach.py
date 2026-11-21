"""Attach mode: attach_to_chrome, detach and list_external_chrome, for a Chrome started outside
this server with --remote-debugging-port.

Moved out of server.py unchanged. _ATTACHED_BROWSERS, which the close paths in server.py read,
stays there and is reached here as _srv.<name> at call time, like the other server helpers and
close_connection, which tests patch on server.
"""

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import nodriver
from nodriver import Config

from .. import server as _srv
from .._app import mcp
from .. import brave as brave_mode
from ..helpers import err, ok
from ..state import BrowserState

# Requires the target Chrome to be started with `--remote-debugging-port=<N>`.
# macOS shorthand:
#   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
#     --remote-debugging-port=9222 --user-data-dir=/path/to/your/profile
#
# Then `attach_to_chrome(port=9222)` connects without spawning a new browser
# and respects the existing tabs/cookies/profile (including Profile 21, etc).
# `detach()` releases the connection but leaves Chrome running.


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
        _srv._validate_instance_id(instance_id)
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
            timeout=_srv.BROWSER_LAUNCH_TIMEOUT,
        )
    except Exception as e:
        return err(f"attach failed after CDP probe ok: {e}")
    # Config names the Chrome nodriver would start, not the browser on that port, and Brave's
    # /json/version says Chrome too. So measure it, and say unknown rather than guess.
    attached_product = await _srv._attached_product(browser, host, port)
    brave_mode.mark(browser, attached_product)

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
    _srv._ATTACHED_BROWSERS.add(instance_id)
    BrowserState.owned_targets, BrowserState.adopted_targets = set(), set()
    BrowserState.active_lost = ""
    await _srv._refresh_tabs()
    BrowserState.own(BrowserState.tabs)  # attaching takes over every tab open now
    return ok(
        f"Attached to existing Chrome at {host}:{port} as {instance_id!r}. "
        f"Browser: {info.get('Browser', '?')} ({brave_mode.name(attached_product)}), "
        f"{len(BrowserState.tabs)} tab(s).{_srv._attached_note(attached_product)} "
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
    if iid not in _srv._ATTACHED_BROWSERS:
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
                await _srv.close_connection(connection)
        # Don't call .stop() — that terminates Chrome.
    finally:
        _srv._ATTACHED_BROWSERS.discard(iid)
        BrowserState.reset()
        _srv._clear_tab_scoped_state(detached_tabs)
    return ok(f"detached from {iid!r}. Chrome still running externally.")
