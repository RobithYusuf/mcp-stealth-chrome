"""Interactive Turnstile handoff through a CDP-free, disposable desktop Chrome.

The debugging port may be open, but NO client may attach during rendering, input,
or the eight-second scoring window. Do not replace the OS capture with a CDP shot.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .. import os_input
from .._app import mcp
from ..helpers import err, get_url, mask_url, ok
from ..state import BrowserState, find_chrome_binary


class _Failure(RuntimeError):
    """A local, credential-free explanation safe for the caller."""


@contextlib.contextmanager
def _desktop_lock():
    # Shared by stdio server processes as well as HTTP sessions. OS locks release on crash.
    identity = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    path = Path(tempfile.gettempdir()) / f"mcp-stealth-oscursor-{identity}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except (BlockingIOError, PermissionError) as exc:
        if not acquired:
            raise _Failure("another OS-cursor solver owns this desktop") from exc
        raise
    finally:
        os.close(fd)


async def _native(fn, *args):
    # Cancellation of to_thread alone leaves input running after cleanup, or a screenshot
    # writer racing removal. Wait for our bounded native operation before propagating it.
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise
    except RuntimeError as exc:
        raise _Failure(str(exc)) from exc


def _checkbox(path: Path, bounds: tuple[int, int, int, int]) -> tuple[float, float, float]:
    import cv2
    import numpy as np
    from nodriver.core import util
    from ..server import _CF_TEMPLATE_THRESHOLD

    image = cv2.imread(str(path))
    template = cv2.imdecode(np.frombuffer(util.get_cf_template(), np.uint8), cv2.IMREAD_COLOR)
    if image is None or template is None:
        raise _Failure("desktop screenshot or Turnstile template could not be decoded")
    left, top, width, height = bounds
    # nodriver's bundled template has appeared at both CSS and device-pixel sizes.
    # Try the native capture and its logical-pixel form; never assume Retina is 2x.
    variants = [image]
    if image.shape[1] != width or image.shape[0] != height:
        variants.append(cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA))
    best = (-1.0, 0.0, 0.0)
    for page in variants:
        if page.shape[0] < template.shape[0] or page.shape[1] < template.shape[1]:
            continue
        # Dark-mode Turnstile reverses the template's contrast. Blurred edges retain
        # the checkbox, panel border and label geometry without lowering the threshold.
        edge_page = cv2.GaussianBlur(cv2.Canny(page, 40, 100), (0, 0), 1)
        edge_template = cv2.GaussianBlur(cv2.Canny(template, 40, 100), (0, 0), 1)
        matches = [cv2.minMaxLoc(cv2.matchTemplate(page, template, cv2.TM_CCOEFF_NORMED)),
                   cv2.minMaxLoc(cv2.matchTemplate(edge_page, edge_template, cv2.TM_CCOEFF_NORMED))]
        _, score, _, loc = max(matches, key=lambda match: match[1])
        x = left + (loc[0] + template.shape[1] / 2) * width / page.shape[1]
        y = top + (loc[1] + template.shape[0] / 2) * height / page.shape[0]
        if math.isfinite(score) and score > best[0]:
            best = score, x, y
    if best[0] < _CF_TEMPLATE_THRESHOLD:
        raise _Failure(f"checkbox not found; best template score {best[0]:.2f}; nothing clicked")
    return best[1], best[2], best[0]


async def _sidecar_cookies(port: int) -> tuple[str, list[dict]]:
    """First CDP client contact. Caller must have finished the scoring window."""
    import websockets
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        response = await client.get(f"http://127.0.0.1:{port}/json/version")
        response.raise_for_status()
        version = response.json()
    endpoint = version["webSocketDebuggerUrl"]
    parts = urlsplit(endpoint)
    if parts.scheme != "ws" or parts.hostname not in ("127.0.0.1", "localhost") or parts.port != port:
        raise _Failure("sidecar returned a non-local CDP endpoint")
    async with websockets.connect(endpoint, open_timeout=3, close_timeout=1) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1:
                if "error" in message:
                    # A CDP error may echo parameters. Never put it in the tool response.
                    raise _Failure("sidecar Storage.getCookies was refused")
                return str(version.get("User-Agent", "")), message["result"]["cookies"]


def _first_party_partition(cookie: dict, host: str) -> bool:
    """True for an unpartitioned cookie, or one partitioned to this same site.

    Cloudflare now issues cf_clearance as a partitioned (CHIPS) cookie keyed to the
    site's own top-level origin. Dropping every partitioned cookie discarded the
    clearance and the handoff always reported failure. A cross-site partition
    (another top-level site, or a cross-site ancestor) is still excluded: it would
    not apply to the main browser's first-party navigation.
    """
    pk = cookie.get("partitionKey")
    if not pk:
        return True
    if isinstance(pk, dict):
        if pk.get("hasCrossSiteAncestor"):
            return False
        top = (urlsplit(pk.get("topLevelSite", "")).hostname or "").lower().strip(".")
    else:
        top = (urlsplit(pk).hostname or str(pk)).lower().strip(".")
    return bool(top) and (host == top or host.endswith("." + top) or top.endswith("." + host))


def _site_cookies(cookies: list[dict], url: str) -> tuple[list[dict], bool, bool]:
    from ..server import _cookie_domain_match
    host = (urlsplit(url).hostname or "").lower().strip(".")
    selected = [cookie for cookie in cookies
                if _cookie_domain_match(cookie.get("domain"), url)
                and _first_party_partition(cookie, host)
                and (cookie.get("expires", -1) <= 0 or cookie["expires"] > time.time())]
    names = {cookie["name"] for cookie in selected if cookie.get("value")}
    return selected, "cf_clearance" in names, "cf_chl_rc_ni" in names


def _stop_sidecar(process: subprocess.Popen) -> None:
    """End only the process tree we launched, before touching its profile."""
    if sys.platform == "win32":
        if process.poll() is not None:
            # Without a live parent taskkill cannot establish ownership of descendants.
            raise RuntimeError("sidecar parent exited; child cleanup could not be verified")
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=8)
        if result.returncode:
            raise RuntimeError("Windows could not terminate the sidecar process tree")
        process.wait(timeout=3)
        return
    # start_new_session=True gives this sidecar an exclusive process group, including
    # renderers, unlike killing only Chrome's parent PID.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("sidecar descendants are still present; private profile retained")
        time.sleep(0.05)


def _shred_tree(root: Path) -> None:
    """Overwrite owned regular files, then unlink. Never follow Chrome's symlinks.

    This is best effort erasure, not a secure-erasure guarantee on SSD/COW filesystems.
    The private directory and credential file permissions remain essential.
    """
    zeroes = bytes(64 * 1024)
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError("refusing to overwrite a multiply-linked sidecar file")
                fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "wb", buffering=0) as handle:
                    if hasattr(os, "fchmod"):
                        os.fchmod(handle.fileno(), 0o600)
                    else:
                        path.chmod(0o600)
                    left = info.st_size
                    while left:
                        written = handle.write(zeroes[:min(left, len(zeroes))])
                        if not written:
                            raise OSError("sidecar file overwrite made no progress")
                        left -= written
                    os.fsync(handle.fileno())
            path.unlink()
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                path.unlink()
            else:
                path.rmdir()
    root.rmdir()


async def _attempt(url: Optional[str], timeout_s: float, report: dict, resources: dict) -> bool:
    from .. import server

    available, reason = await _native(os_input.is_available)
    report["backend"] = reason
    if not available:
        report["reason"] = reason
        return False
    if not BrowserState.is_up():
        raise _Failure("main browser is not running; call browser_launch first")
    tab = BrowserState.active_tab()
    browser = BrowserState.browser
    instance = BrowserState.current_instance_id
    target = url or await get_url(tab, strict=True)
    parsed = urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise _Failure("url must be an HTTP(S) site without embedded credentials")
    report["url"] = mask_url(target)
    if BrowserState.current_proxy:
        raise _Failure("main browser uses a proxy; this plain sidecar requires the same direct IP route")
    binary = find_chrome_binary()
    if not binary:
        raise _Failure("Chrome/Chromium binary not found")
    main_ua = await server._eval_checked(tab, "navigator.userAgent")
    if not isinstance(main_ua, str) or not main_ua:
        raise _Failure("main browser user agent could not be measured")
    root = Path(tempfile.mkdtemp(prefix="mcp-stealth-oscursor-"))
    resources["root"] = root
    root.chmod(0o700)
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = [binary, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            "--window-position=60,60", "--window-size=1280,900", target]
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=sys.platform != "win32")
    resources["process"] = process
    report["stage"] = "rendering_without_cdp"
    await asyncio.sleep(6)
    if process.poll() is not None:
        raise _Failure("sidecar exited before rendering")
    # Capture AFTER activation, otherwise a different app's checkbox might match.
    await _native(os_input.bring_app_frontmost, process.pid)
    bounds = await _native(os_input.window_bounds, process.pid)
    shot = root / "screen.png"
    await _native(os_input.capture_region, bounds, shot)
    x, y, score = await _native(_checkbox, shot, bounds)
    report["template_score"] = round(score, 3)
    report["stage"] = "native_click_without_cdp"
    await _native(os_input.move_and_click, x, y)
    report["clicked"] = True
    report["stage"] = "scoring_without_cdp"
    await asyncio.sleep(8)
    report["stage"] = "reading_sidecar_cookies"
    sidecar_ua, cookies = await _sidecar_cookies(port)
    selected, clearance, rejected = _site_cookies(cookies, target)
    report["sidecar"] = {"cf_clearance_present": clearance, "cf_chl_rc_ni_present": rejected,
                         "passed_cookie_check": clearance and not rejected,
                         "page_status": None, "page_status_source": "unmeasured"}
    if not clearance or rejected:
        report["reason"] = ("challenge remains or clearance was not issued; no cookies transferred; "
                            "a click alone is not a pass")
        return False
    if main_ua != sidecar_ua:
        report["reason"] = "sidecar and main user agents differ; IP/UA-bound clearance was NOT transferred"
        return False
    if (BrowserState.browser is not browser or BrowserState.current_instance_id != instance
            or BrowserState.active_tab() is not tab):
        raise _Failure("main browser or active tab changed during the sidecar attempt; nothing transferred")
    state = {"cookies": [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                          "path": c.get("path", "/"), "expires": c.get("expires", -1),
                          "secure": c.get("secure", False), "http_only": c.get("httpOnly", False)}
                         for c in selected], "origins": []}
    state_path = root / "state.json"
    server._write_private(state_path, json.dumps(state))
    report["stage"] = "cookie_handoff"
    restored = await server._apply_storage_state(browser, str(state_path))
    report["cookies_set"] = restored["cookies_set"]
    report["cookies_failed"] = restored["cookies_failed"]
    if restored["cookies_failed"]:
        report["reason"] = "some site cookies were refused; main navigation was not attempted"
        return False
    report["stage"] = "verifying_main_browser"
    # Reuse navigation's event watch instead of a second browserless fetch (different TLS/session).
    doc, disarm = await server._arm_document_watch(tab)
    try:
        await tab.get(target)
        await server._wait_load_complete(tab, min(10, timeout_s))
        observed = await server._observe_navigation(tab, target, doc)
        active = await server._eval_checked(tab, server._CF_CHALLENGE_PROBE_ACTIVE_JS)
        if not isinstance(active, bool):
            active = None
    finally:
        disarm()
    report["main"] = {"status": observed.get("status"), "ready_state": observed.get("ready_state"),
                      "final_url": mask_url(observed.get("final_url")), "challenge_present": active,
                      "status_source": observed.get("status_source"),
                      "load_failed": bool(observed.get("load_failure"))}
    passed = (observed.get("status") == 200 and observed.get("ready_state") == "complete"
              and not observed.get("load_failure") and active is False)
    report["reason"] = ("main browser returned HTTP 200 with no detected challenge" if passed else
                        "handoff completed but main browser access is NOT confirmed; inspect measured main fields")
    report["stage"] = "complete"
    return passed


@mcp.tool()
async def solve_turnstile_oscursor(url: Optional[str] = None, timeout_s: float = 30.0) -> str:
    """Try interactive Turnstile with the real OS cursor in a disposable headed Chrome.

    Takes desktop focus and moves the physical cursor. Do not use the desktop during
    the call. Requires a running main browser, matching IP and natural user agent,
    and macOS Accessibility + Screen Recording permissions (install [os-cursor]).
    Windows SendInput and Linux X11 backends are implemented but not yet field-tested.
    No sidecar CDP client connects until six seconds of rendering, native input and
    eight seconds of scoring have elapsed. Site cookies are transferred privately
    through the existing storage-state loader, then main HTTP status/challenge are
    checked. A clearance cookie or a click alone never counts as confirmed access.
    url defaults to the active tab URL. timeout_s bounds the attempt, not cleanup;
    at least 16 seconds is required. Only this call's temporary profile is removed.
    """
    if not math.isfinite(timeout_s) or timeout_s < 16:
        return err("solve_turnstile_oscursor: timeout_s must be finite and >= 16 (rendering + scoring)")
    report = {"platform": sys.platform, "stage": "preflight", "clicked": False,
              "sidecar": None, "main": None, "cookies_set": 0}
    resources = {}
    success = False
    try:
        with _desktop_lock():
            try:
                async with asyncio.timeout(timeout_s):
                    success = await _attempt(url, timeout_s, report, resources)
            except TimeoutError:
                report["reason"] = "attempt timed out; unmeasured results remain null, not passed"
            except Exception as exc:
                # Cookie/CDP exceptions can contain secrets. Return known local errors only.
                report["reason"] = str(exc) if isinstance(exc, _Failure) else type(exc).__name__
            finally:
                try:
                    if resources.get("process") is not None:
                        await _native(_stop_sidecar, resources["process"])
                    if resources.get("root") is not None:
                        await _native(_shred_tree, resources["root"])
                    report["cleanup"] = ("sidecar stopped; owned temporary files overwritten and removed"
                                         if resources else "not needed; no sidecar launched")
                except Exception as exc:
                    success = False
                    report["cleanup"] = f"NOT complete ({type(exc).__name__}); owned files retained at {resources.get('root')}"
    except Exception as exc:
        report["reason"] = str(exc) if isinstance(exc, _Failure) else type(exc).__name__
    report["success"] = success
    text = json.dumps(report, ensure_ascii=False)
    return ok(text) if success else err(text)
