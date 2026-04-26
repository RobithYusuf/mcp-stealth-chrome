"""mcp-stealth-chrome — FastMCP server entry + all tool implementations.

Architecture parallels mcp-camoufox (Node/Firefox sister package):
- single-file tool registry for easy maintenance
- same tool names, same parameters, same ref system
- nodriver CDP direct + async throughout
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

import nodriver
from mcp.server.fastmcp import FastMCP
from nodriver import Browser, Config, Tab

from . import __version__
from . import patches as _patches
_patches.apply_all()
from .captcha import CapSolverError, solve as capsolver_solve
from .helpers import (
    err,
    get_title,
    get_url,
    ok,
    parse_json,
    resolve_ref,
    ts_filename,
)
from .humanize import humanized_click, humanized_move, humanized_scroll, humanized_type
from .snapshot import (
    SNAPSHOT_JS,
    SNAPSHOT_JS_FAST,
    SNAPSHOT_JS_VIEWPORT,
    format_snapshot,
    snapshot_hash,
)
from .state import (
    DEFAULT_IDLE_TIMEOUT,
    EXPORT_DIR,
    IDLE_REAPER_INTERVAL,
    PROFILE_DIR,
    PROFILES_ROOT,
    SCREENSHOT_DIR,
    STORAGE_STATE_DIR,
    BrowserState,
    InstanceSnapshot,
    chrome_install_hint,
    chrome_user_data_root,
    clean_profile_state,
    ensure_dirs,
    find_chrome_binary,
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


async def _wait(coro, timeout: Optional[float] = None, what: str = "operation"):
    """Wrap a CDP coroutine with a hard timeout — surfaces a clean error
    instead of letting a stuck page freeze the whole tool call. Re-raises
    asyncio.TimeoutError as a regular exception with a useful message."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout or TOOL_ACTION_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{what} timed out after {timeout or TOOL_ACTION_TIMEOUT}s — "
            f"page JS may be stuck or blocked. Try reload, or close+launch."
        )

# Serialize concurrent launches inside ONE MCP process. Cross-process collision
# is handled separately by `resolve_default_profile()` (per-PID fallback).
_LAUNCH_LOCK = asyncio.Lock()


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
    if (!anyEmpty) return false;  // every input has a token → solved
  }
  const txt = (document.body && document.body.innerText || '').toLowerCase();
  const phrases = ['performing security verification', 'just a moment',
    'checking your browser', 'verify you are human', 'verifying you are human'];
  if (phrases.some(p => txt.includes(p))) return true;
  return !!document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
  );
})()
"""


async def _has_cf_challenge(tab: Tab, *, active: bool = False) -> bool:
    """Detect a Cloudflare/Turnstile challenge.

    active=False (default): broad detection used BEFORE attempting any click.
        Matches loader script and host containers so we don't miss a
        widget that hasn't fully rendered yet.
    active=True: strict detection used BETWEEN click attempts. Returns True
        only when visible challenge UI is still on the page (not just the
        post-solve dormant markers)."""
    js = _CF_CHALLENGE_PROBE_ACTIVE_JS if active else _CF_CHALLENGE_PROBE_INITIAL_JS
    try:
        v = await asyncio.wait_for(
            tab.evaluate(js, return_by_value=True), timeout=3.0
        )
        return bool(v.value if hasattr(v, "value") else v)
    except Exception:
        return False


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
    start_x = target_x + 180
    start_y = target_y - 80
    try:
        await humanized_move(tab, start_x, start_y, target_x, target_y)
        await asyncio.sleep(0.15)
        await tab.mouse_click(target_x, target_y)
        return (target_x, target_y)
    except Exception:
        return None


async def _auto_verify_cf(tab: Tab, max_attempts: int = 2) -> str:
    """Run on the tab right after load. Detects CF challenge + attempts click.
    Caps at max_attempts; never loops or blocks beyond ~12s total. Returns
    a short suffix to append to the caller's status line, or '' if no
    challenge was seen.

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
    if not await _has_cf_challenge(tab):
        await asyncio.sleep(0.6)
        if not await _has_cf_challenge(tab):
            return ""
    # Challenge present — give the widget another beat to paint its checkbox
    # so OpenCV template match has something to find.
    await asyncio.sleep(2.0)

    actions: list[str] = []
    for _ in range(max(1, max_attempts)):
        # 2. DOM tier
        clicked = await _attempt_turnstile_click(tab)
        if clicked is not None:
            actions.append(f"DOM@{clicked}")
            await asyncio.sleep(2.5)
            if not await _has_cf_challenge(tab, active=True):
                break

        # 3. OpenCV template tier — handles shadow-DOM / cross-origin iframes
        try:
            await asyncio.wait_for(tab.verify_cf(flash=False), timeout=4.0)
            actions.append("template")
            await asyncio.sleep(2.5)
            if not await _has_cf_challenge(tab, active=True):
                break
        except Exception:
            pass

    if not actions:
        return " [auto-verify: CF detected but no clickable widget found]"
    return f" [auto-verify: {' → '.join(actions)}]"

from ._app import mcp  # single FastMCP instance shared with tools/* submodules


# ── Utility: ensure active tab after operations that may shift tabs ────────

async def _refresh_tabs() -> None:
    """Sync BrowserState.tabs with browser.tabs (after new windows etc.)."""
    if BrowserState.browser:
        BrowserState.tabs = list(BrowserState.browser.tabs)


# ══════════════════════════════════════════════════════════════════════════
# 1. LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def browser_launch(
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
    testing_mode: bool = False,
    auto_verify: bool = True,
) -> str:
    """Launch stealth Chrome via nodriver. Creates persistent profile by default.

    Args:
        url: initial URL to load
        headless: run without UI (many sites detect headless — prefer False)
        proxy: "http://user:pass@host:port" or "socks5://host:port"
        user_agent: override UA string
        window_width, window_height: viewport size
        persistent: reuse profile at ~/.mcp-stealth/profile
        lang: browser language
        extra_args: additional Chromium flags
        storage_state_path: load cookies/localStorage from JSON before first nav
        testing_mode: 2-5× faster startup+nav for perf/regression testing —
            disables image loading, background throttling dampers, translate,
            notifications, media autoplay. WARNING: reduces stealth — not for
            anti-bot work (sites can detect missing images as automation signal).
        auto_verify: if True (default), automatically detect Cloudflare /
            Turnstile challenges after the initial load and dispatch a
            CDP-level click on the checkbox. Caps at 2 attempts ~6s total —
            never loops. Set False to opt out.
    """
    if BrowserState.is_up():
        return ok(f"Browser already running with {len(BrowserState.tabs)} tab(s).")

    if _LAUNCH_LOCK.locked():
        return err("another launch is already in progress — wait for it to finish")

    async with _LAUNCH_LOCK:
        # Re-check inside the lock (race with another concurrent call)
        if BrowserState.is_up():
            return ok(f"Browser already running with {len(BrowserState.tabs)} tab(s).")

        ensure_dirs()
        # Per-process profile fallback: if another live MCP server already
        # holds the shared default profile lock, use ~/.mcp-stealth/profile-pid<N>/
        # so parallel Claude sessions never collide on Chrome's SingletonLock.
        profile_path = resolve_default_profile(persistent)
        used_fallback = persistent and profile_path != PROFILE_DIR
        if persistent:
            clean_profile_state(profile_path)  # only clears stale locks
        config = Config(
            user_data_dir=str(profile_path) if persistent else None,
            headless=headless,
            lang=lang,
            browser_args=list(extra_args or []),
        )
        # Extra flags to suppress any first-run / restore / notification interrupts
        config.add_argument("--hide-crash-restore-bubble")
        config.add_argument("--disable-session-crashed-bubble")
        config.add_argument("--disable-restore-session-state")
        config.add_argument("--no-default-browser-check")
        if user_agent:
            config.add_argument(f"--user-agent={user_agent}")
        if proxy:
            config.add_argument(f"--proxy-server={proxy}")
        config.add_argument(f"--window-size={window_width},{window_height}")
        if testing_mode:
            for _flag in (
                "--blink-settings=imagesEnabled=false",
                "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
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
        try:
            browser = await asyncio.wait_for(
                nodriver.start(config=config),
                timeout=BROWSER_LAUNCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await _safe_stop_browser(browser)
            return err(
                f"launch timed out after {BROWSER_LAUNCH_TIMEOUT}s — "
                f"profile {profile_path} may be locked by another Chrome, or "
                f"Chrome is hung. Kill any stale Chrome processes and retry."
            )
        except asyncio.CancelledError:
            await _safe_stop_browser(browser)
            raise
        except Exception as e:
            await _safe_stop_browser(browser)
            return err(f"launch failed: {e}")

        BrowserState.browser = browser

        if storage_state_path:
            try:
                await _apply_storage_state(BrowserState.browser, storage_state_path)
            except asyncio.CancelledError:
                await _safe_stop_browser(BrowserState.browser)
                BrowserState.reset()
                raise
            except Exception as e:
                await _safe_stop_browser(BrowserState.browser)
                BrowserState.reset()
                return err(f"storage_state load failed: {e}")

        try:
            await asyncio.sleep(0.5)
            main = BrowserState.browser.main_tab
            if main is None:
                await BrowserState.browser.update_targets()
                main = BrowserState.browser.tabs[0] if BrowserState.browser.tabs else None
            if main is None:
                main = await asyncio.wait_for(
                    BrowserState.browser.get(url), timeout=BROWSER_NAV_TIMEOUT
                )
            else:
                await asyncio.wait_for(main.get(url), timeout=BROWSER_NAV_TIMEOUT)
            try:
                await asyncio.wait_for(main.wait(t=3), timeout=BROWSER_NAV_TIMEOUT)
            except asyncio.TimeoutError:
                pass  # initial load wait is best-effort
        except asyncio.TimeoutError:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            return err(
                f"initial nav timed out after {BROWSER_NAV_TIMEOUT}s — Chrome "
                f"started but couldn't load {url}. Check network/proxy."
            )
        except asyncio.CancelledError:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            raise
        except Exception as e:
            await _safe_stop_browser(BrowserState.browser)
            BrowserState.reset()
            return err(f"initial nav failed: {e}")
        BrowserState.tabs = [main]
        BrowserState.active_tab_index = 0
        BrowserState.current_profile_dir = profile_path
        suffix = (
            f" [profile fallback: {profile_path.name} — default profile is in use "
            f"by another Chrome]"
            if used_fallback else ""
        )
        verify_suffix = ""
        if auto_verify:
            try:
                verify_suffix = await asyncio.wait_for(_auto_verify_cf(main), timeout=25.0)
            except (asyncio.TimeoutError, Exception):
                verify_suffix = ""
        return ok(
            f"Browser launched (headless={headless}, persistent={persistent}). "
            f"Loaded {url}{suffix}{verify_suffix}"
        )


@mcp.tool()
async def browser_close() -> str:
    """Close the browser and free the profile lock."""
    if not BrowserState.is_up():
        return ok("Browser was not running.")
    try:
        if BrowserState.browser:
            BrowserState.browser.stop()
    except Exception as e:
        return err(f"close failed: {e}")
    BrowserState.reset()
    # Clear transient caches that key off tab identity (id() can be reused)
    _SNAPSHOT_CACHE.clear()
    _TRACE_ACTIVE.update({"tab_id": None, "started_at": 0.0, "categories": "",
                           "handler": None})
    _TRACE_BUFFER.clear()
    _COVERAGE_ACTIVE.update({"tab_id": None, "js": False, "css": False})
    # Drop registered dialog handler tab IDs so a new tab with a recycled
    # id() doesn't get skipped on re-arm.
    try:
        _DIALOG_AUTO_CFG["_registered_tab_ids"].clear()
    except Exception:
        pass
    # Mark profile as cleanly exited so next launch skips restore dialog
    clean_profile_state(PROFILE_DIR)
    return ok("Browser closed.")


# ══════════════════════════════════════════════════════════════════════════
# 2. NAVIGATION
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def navigate(url: str, wait_until: str = "load", auto_verify: bool = True) -> str:
    """Navigate the active tab to url. wait_until: load|domcontentloaded|none.

    auto_verify: if True (default), automatically detect Cloudflare /
    Turnstile challenges after load and click the checkbox naturally
    (CDP-level click, max 2 attempts). Set False to opt out.
    """
    if not BrowserState.is_up():
        return err("Browser not running. Call browser_launch first.")
    tab = BrowserState.active_tab()
    try:
        await tab.get(url)
        if wait_until != "none":
            await tab.wait()
        verify_suffix = ""
        if auto_verify:
            try:
                verify_suffix = await asyncio.wait_for(_auto_verify_cf(tab), timeout=25.0)
            except (asyncio.TimeoutError, Exception):
                verify_suffix = ""
        return ok(f"Navigated to {await get_url(tab)}{verify_suffix}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def go_back() -> str:
    """Go back in history."""
    try:
        tab = BrowserState.active_tab()
        await tab.back()
        return ok(f"At {await get_url(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def go_forward() -> str:
    """Go forward in history."""
    try:
        tab = BrowserState.active_tab()
        await tab.forward()
        return ok(f"At {await get_url(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def reload() -> str:
    """Reload the active tab."""
    try:
        tab = BrowserState.active_tab()
        await tab.reload()
        return ok(f"Reloaded {await get_url(tab)}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 3. DOM / CONTENT
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def browser_snapshot(
    mode: Literal["full", "fast", "viewport"] = "full",
    diff_from_last: bool = False,
) -> str:
    """Inject SNAPSHOT_JS and return a ref-indexed list of interactive elements.

    Refs (e0, e1, ...) are attached via data-mcp-ref and valid until next nav.

    Modes (performance vs completeness tradeoff):
      full      — default; same shape as mcp-camoufox (computed-style visibility + full attrs)
      fast      — skip getComputedStyle + minimal attrs (2-3× faster, less info per element)
      viewport  — full fidelity but only elements inside current scroll viewport
                  (5-10× faster on long feeds/SERPs, pair with scroll for segment-by-segment)

    diff_from_last=True caches a DOM hash per tab; if the hash matches the previous
    call on the same URL, returns "unchanged" without re-serializing the element list
    (near-instant for re-check loops).
    """
    try:
        tab = BrowserState.active_tab()
        js = {
            "full": SNAPSHOT_JS,
            "fast": SNAPSHOT_JS_FAST,
            "viewport": SNAPSHOT_JS_VIEWPORT,
        }.get(mode, SNAPSHOT_JS)
        raw = await _wait(tab.evaluate(js, return_by_value=True), what="browser_snapshot")
        elements = parse_json(raw, [])
        if not isinstance(elements, list):
            elements = []
        url = await get_url(tab)
        title = await get_title(tab)
        h = snapshot_hash(elements)
        cache_key = id(tab)
        last = _SNAPSHOT_CACHE.get(cache_key)
        if diff_from_last and last and last["url"] == url and last["hash"] == h:
            return ok(format_snapshot([], url, title, mode=mode, unchanged_from=h))
        _SNAPSHOT_CACHE[cache_key] = {"url": url, "hash": h}
        return ok(format_snapshot(elements, url, title, mode=mode))
    except Exception as e:
        return err(str(e))


# Per-tab snapshot hash cache for diff_from_last (cleared on browser_close / reset)
_SNAPSHOT_CACHE: dict[int, dict[str, str]] = {}


@mcp.tool()
async def screenshot(
    filename: Optional[str] = None,
    full_page: bool = False,
    return_base64: bool = False,
    format: Literal["auto", "png", "jpeg"] = "auto",
    quality: Optional[int] = None,
    region: Optional[dict] = None,
    max_dimension: int = 1920,
) -> str:
    """Screenshot active tab. Saves to ~/.mcp-stealth/screenshots/.

    Args:
        filename: output name (default timestamped)
        full_page: stitch entire page height (slower, larger file)
        return_base64: append base64 body to response (useful for vision models)
        format: "auto" (from extension, default), "png" (lossless), or "jpeg" (smaller)
        quality: JPEG quality 1-100 (default 80) — ignored for PNG
        region: clip to {x, y, width, height} — uses CDP Page.captureScreenshot clip
                (skips full-viewport paint, 2-5× faster for small crops)
        max_dimension: if either width or height exceeds this (px), the image is
            resized proportionally via OpenCV INTER_AREA. Default 1920 keeps output
            under the 2000 px per-side limit that LLM image tools (Claude/GPT) enforce
            — prevents "image exceeds dimension limit" failures on long full_page
            captures or hi-DPR device emulation. Pass 0 to disable resizing.
    """
    try:
        tab = BrowserState.active_tab()
        ensure_dirs()
        fname = filename or ts_filename("shot", "png")
        path = SCREENSHOT_DIR / fname
        ext = path.suffix.lower().lstrip(".")
        if format == "auto":
            fmt = "png" if ext == "png" else "jpeg"
        else:
            fmt = format
        # Region clip → use raw CDP; nodriver's save_screenshot doesn't expose clip
        if region:
            from nodriver.cdp import page as cdp_page
            clip = cdp_page.Viewport(
                x=float(region.get("x", 0)),
                y=float(region.get("y", 0)),
                width=float(region["width"]),
                height=float(region["height"]),
                scale=1.0,
            )
            kwargs: dict[str, Any] = {"format_": fmt, "clip": clip, "capture_beyond_viewport": True}
            if fmt == "jpeg" and quality is not None:
                kwargs["quality"] = int(quality)
            b64 = await _wait(tab.send(cdp_page.capture_screenshot(**kwargs)),
                              what="screenshot (region)")
            data = base64.b64decode(b64)
            path.write_bytes(data)
        else:
            # nodriver path — quality param only valid for jpeg
            save_kwargs: dict[str, Any] = {
                "filename": str(path),
                "format": fmt,
                "full_page": full_page,
            }
            if fmt == "jpeg" and quality is not None:
                save_kwargs["quality"] = int(quality)
            await _wait(tab.save_screenshot(**save_kwargs), what="screenshot")

        # Auto-downscale if either dimension exceeds max_dimension.
        # Uses cv2 (already a dep via opencv-python). INTER_AREA = best for shrinking.
        resized_info = ""
        if max_dimension and max_dimension > 0:
            try:
                import cv2
                img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                if img is not None:
                    h, w = img.shape[:2]
                    longest = max(h, w)
                    if longest > max_dimension:
                        scale = max_dimension / longest
                        new_w = int(round(w * scale))
                        new_h = int(round(h * scale))
                        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                        if fmt == "jpeg":
                            cv2.imwrite(str(path), resized,
                                        [cv2.IMWRITE_JPEG_QUALITY, int(quality or 85)])
                        else:
                            cv2.imwrite(str(path), resized)
                        resized_info = f" [resized {w}×{h} → {new_w}×{new_h}]"
            except Exception:
                # Resizing is best-effort; original file is already saved.
                pass

        if return_base64:
            data = path.read_bytes()
            return ok(f"{path}{resized_info}\n---base64---\n{base64.b64encode(data).decode()}")
        return ok(f"{path}{resized_info}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_text(selector: Optional[str] = None, ref: Optional[str] = None) -> str:
    """Return innerText of element (by selector or ref) or whole document."""
    try:
        tab = BrowserState.active_tab()
        if ref:
            el = await resolve_ref(ref)
            if el is None:
                return err(f"ref {ref} not found")
            return ok(el.text_all or "")
        if selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err(f"selector not found: {selector}")
            return ok(el.text_all or "")
        result = await tab.evaluate("document.body.innerText", return_by_value=True)
        return ok(str(result or ""))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_html(selector: Optional[str] = None, outer: bool = False) -> str:
    """Return innerHTML (or outerHTML) of element or whole document."""
    try:
        tab = BrowserState.active_tab()
        if selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err(f"selector not found: {selector}")
            html = await el.get_html()
            return ok(html or "")
        html = await tab.get_content()
        return ok(html or "")
    except Exception as e:
        return err(str(e))


@mcp.tool(name="get_url")
async def get_current_url() -> str:
    """Return current URL of active tab."""
    try:
        tab = BrowserState.active_tab()
        return ok(await get_url(tab))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def save_pdf(filename: Optional[str] = None, landscape: bool = False) -> str:
    """Save current page as PDF via CDP Page.printToPDF."""
    try:
        tab = BrowserState.active_tab()
        ensure_dirs()
        fname = filename or ts_filename("page", "pdf")
        path = EXPORT_DIR / fname
        from nodriver.cdp import page as cdp_page
        result = await tab.send(cdp_page.print_to_pdf(landscape=landscape))
        # result[0] is base64 data per CDP spec
        data = result[0] if isinstance(result, tuple) else result
        path.write_bytes(base64.b64decode(data))
        return ok(str(path))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 4. INTERACTION
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def click(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    humanize: bool = False,
) -> str:
    """Click an element by ref (from snapshot) or CSS selector. JS fallback on failure."""
    try:
        tab = BrowserState.active_tab()
        el = None
        if ref:
            el = await resolve_ref(ref)
        elif selector:
            el = await tab.query_selector(selector)
        if el is None:
            return err("element not found")
        try:
            if humanize:
                await humanized_click(tab, el)
            else:
                await el.click()
        except Exception:
            # JS fallback for overlay-blocked elements
            await tab.evaluate(
                f'document.querySelector(\'[data-mcp-ref="{ref}"]\').click()'
                if ref else f'document.querySelector({json.dumps(selector)}).click()',
                return_by_value=True,
            )
        return ok("clicked")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def click_text(text: str, exact: bool = False) -> str:
    """Find and click element whose text matches."""
    try:
        tab = BrowserState.active_tab()
        el = await tab.find(text, best_match=not exact)
        if el is None:
            return err(f"no element with text {text!r}")
        await el.click()
        return ok(f"clicked text {text!r}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def click_role(role: str, name: Optional[str] = None) -> str:
    """Click by ARIA role (e.g. button, link, textbox), optional accessible name."""
    try:
        tab = BrowserState.active_tab()
        sel = f'[role="{role}"]'
        if name:
            sel = f'[role="{role}"][aria-label*="{name}"], [role="{role}"]:has-text("{name}")'
        el = await tab.query_selector(sel)
        if el is None and name:
            el = await tab.find(name, best_match=True)
        if el is None:
            return err(f"no {role} found")
        await el.click()
        return ok(f"clicked {role}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def hover(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Hover over element."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos = await el.get_position()
        if pos is None:
            return err("position unavailable")
        await tab.mouse_move(int(pos.left + pos.width / 2), int(pos.top + pos.height / 2))
        return ok("hovered")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def fill(ref: Optional[str] = None, selector: Optional[str] = None,
               value: str = "") -> str:
    """Fill input/textarea via set_value (fast, works for standard inputs)."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        try:
            await el.clear_input()
        except Exception:
            pass
        try:
            await el.send_keys(value)
        except Exception:
            await el.set_value(value)
        return ok("filled")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def type_text(text: str, humanize: bool = False,
                     mean_delay: float = 0.12) -> str:
    """Type into focused element (keystroke-by-keystroke). Use humanize for Gaussian delays."""
    try:
        tab = BrowserState.active_tab()
        active = await tab.evaluate(
            "document.activeElement ? document.activeElement.tagName : null",
            return_by_value=True,
        )
        if not active:
            return err("no focused element — click/focus an input first")
        # Get active element handle via a marker
        await tab.evaluate(
            "document.activeElement.setAttribute('data-mcp-focused','1')",
            return_by_value=True,
        )
        el = await tab.query_selector("[data-mcp-focused='1']")
        if el is None:
            return err("focused element lookup failed")
        if humanize:
            await humanized_type(el, text, mean_delay=mean_delay)
        else:
            await el.send_keys(text)
        await tab.evaluate(
            "document.querySelectorAll('[data-mcp-focused]').forEach(e=>e.removeAttribute('data-mcp-focused'))",
            return_by_value=True,
        )
        return ok("typed")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a single key (Enter, Escape, Tab, ArrowDown, a, etc)."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import input_ as cdp_input
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", key=key))
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", key=key))
        return ok(f"pressed {key}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def select_option(
    ref: Optional[str] = None, selector: Optional[str] = None,
    value: Optional[str] = None, label: Optional[str] = None,
) -> str:
    """Select <option> by value or label."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        target = value or label or ""
        await el.select_option(target)
        return ok(f"selected {target!r}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def check(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Tick a checkbox/radio (idempotent)."""
    return await _set_checked(ref, selector, True)


@mcp.tool()
async def uncheck(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Untick a checkbox."""
    return await _set_checked(ref, selector, False)


async def _set_checked(ref, selector, state: bool) -> str:
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        # Get current state, click if different
        current = await tab.evaluate(
            f'!!document.querySelector(\'[data-mcp-ref="{ref}"]\').checked'
            if ref else f'!!document.querySelector({json.dumps(selector)}).checked',
            return_by_value=True,
        )
        if bool(current) != state:
            await el.click()
        return ok(f"{'checked' if state else 'unchecked'}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def upload_file(
    file_path: str,
    ref: Optional[str] = None, selector: Optional[str] = None,
) -> str:
    """Upload a file via <input type=file>."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        await el.send_file(file_path)
        return ok(f"uploaded {file_path}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 5. MOUSE XY + DRAG
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def mouse_click_xy(x: int, y: int, button: str = "left") -> str:
    """Click at raw viewport coordinates."""
    try:
        tab = BrowserState.active_tab()
        await tab.mouse_click(x, y, button=button)
        return ok(f"clicked ({x},{y})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_move(x: int, y: int, humanize: bool = False) -> str:
    """Move cursor to raw coordinates. humanize=True uses Bezier path."""
    try:
        tab = BrowserState.active_tab()
        if humanize:
            # Start from a random offset; nodriver has no current-pos getter
            await humanized_move(tab, x + 100, y + 100, x, y)
        else:
            await tab.mouse_move(x, y)
        return ok(f"moved to ({x},{y})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def drag_and_drop(start_x: int, start_y: int, end_x: int, end_y: int) -> str:
    """Drag from (start_x, start_y) to (end_x, end_y)."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import input_ as cdp_input
        await tab.mouse_move(start_x, start_y)
        await tab.send(cdp_input.dispatch_mouse_event(
            type_="mousePressed", x=start_x, y=start_y, button="left", click_count=1,
        ))
        # Intermediate steps for natural drag
        steps = 20
        for i in range(1, steps + 1):
            t = i / steps
            await tab.send(cdp_input.dispatch_mouse_event(
                type_="mouseMoved",
                x=int(start_x + (end_x - start_x) * t),
                y=int(start_y + (end_y - start_y) * t),
                button="left",
            ))
            await asyncio.sleep(0.02)
        await tab.send(cdp_input.dispatch_mouse_event(
            type_="mouseReleased", x=end_x, y=end_y, button="left", click_count=1,
        ))
        return ok("dropped")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 6. WAIT
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def wait_for(selector: Optional[str] = None, text: Optional[str] = None,
                    timeout: float = 10.0) -> str:
    """Wait until selector exists or text appears on page."""
    try:
        tab = BrowserState.active_tab()
        if selector:
            await tab.wait_for(selector=selector, timeout=timeout)
            return ok(f"{selector} appeared")
        if text:
            el = await tab.find(text, best_match=True, timeout=timeout)
            if el is None:
                return err(f"{text!r} not found")
            return ok(f"{text!r} found")
        await asyncio.sleep(timeout)
        return ok(f"slept {timeout}s")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_navigation(timeout: float = 15.0) -> str:
    """Wait until the page finishes loading."""
    try:
        tab = BrowserState.active_tab()
        await tab.wait(t=timeout)
        return ok(f"navigated to {await get_url(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_url(pattern: str, timeout: float = 15.0) -> str:
    """Wait until URL matches a regex pattern."""
    try:
        tab = BrowserState.active_tab()
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = await get_url(tab)
            if regex.search(cur):
                return ok(f"URL matches: {cur}")
            await asyncio.sleep(0.3)
        return err(f"timeout waiting for URL {pattern}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_response(url_pattern: str, timeout: float = 15.0) -> str:
    """Wait for a network response whose URL matches regex."""
    try:
        tab = BrowserState.active_tab()
        regex = re.compile(url_pattern)
        matched: dict[str, Any] = {}
        from nodriver.cdp import network as cdp_network

        async def handler(event):
            if hasattr(event, "response") and regex.search(event.response.url):
                matched["url"] = event.response.url
                matched["status"] = event.response.status

        tab.add_handler(cdp_network.ResponseReceived, handler)
        deadline = time.time() + timeout
        while time.time() < deadline and "url" not in matched:
            await asyncio.sleep(0.2)
        tab.remove_handler(cdp_network.ResponseReceived, handler)
        if "url" in matched:
            return ok(f"response {matched['status']} {matched['url']}")
        return err("timeout")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 7. TABS
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def tab_list() -> str:
    """List all open tabs with index, URL, title."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        await _refresh_tabs()
        lines = [f"Active tab: {BrowserState.active_tab_index}"]
        for i, t in enumerate(BrowserState.tabs):
            url = await get_url(t)
            title = await get_title(t)
            marker = "*" if i == BrowserState.active_tab_index else " "
            lines.append(f"{marker}[{i}] {title[:40]} | {url}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def tab_new(url: str = "about:blank") -> str:
    """Open a new tab and make it active."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        new_tab = await BrowserState.browser.get(url, new_tab=True)
        await _refresh_tabs()
        BrowserState.active_tab_index = BrowserState.tabs.index(new_tab)
        return ok(f"opened tab [{BrowserState.active_tab_index}] {url}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def tab_select(index: int) -> str:
    """Switch to tab at given index (from tab_list)."""
    try:
        await _refresh_tabs()
        if index < 0 or index >= len(BrowserState.tabs):
            return err(f"tab {index} out of range")
        BrowserState.active_tab_index = index
        await BrowserState.tabs[index].activate()
        return ok(f"switched to tab {index}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def tab_close(index: Optional[int] = None) -> str:
    """Close tab at index (defaults to active)."""
    try:
        await _refresh_tabs()
        idx = index if index is not None else BrowserState.active_tab_index
        if idx < 0 or idx >= len(BrowserState.tabs):
            return err(f"tab {idx} out of range")
        await BrowserState.tabs[idx].close()
        await _refresh_tabs()
        if BrowserState.active_tab_index >= len(BrowserState.tabs):
            BrowserState.active_tab_index = max(0, len(BrowserState.tabs) - 1)
        return ok(f"closed tab {idx}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 8. COOKIES
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def cookie_list(url: Optional[str] = None) -> str:
    """List all cookies (optionally filtered by URL)."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        cookies = await BrowserState.browser.cookies.get_all()
        if url:
            cookies = [c for c in cookies if url in (c.domain or "")]
        data = [{"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path, "expires": c.expires,
                 "http_only": c.http_only, "secure": c.secure} for c in cookies]
        return ok(json.dumps(data, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
def _active_tab_host() -> Optional[str]:
    """Best-effort hostname of the active tab — used as default cookie domain
    when raw_text doesn't include one."""
    try:
        if not BrowserState.browser or not BrowserState.tabs:
            return None
        url = getattr(BrowserState.tabs[BrowserState.active_tab_index], "url", "")
        if not url:
            return None
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        return f".{host}" if host and "." in host else host
    except Exception:
        return None


def _parse_cookie_text(text: str, default_domain: Optional[str]) -> list[dict]:
    """Auto-detect cookie text format and return list of cookie dicts.

    Handles:
      - JSON array: [{...}]
      - JSON object with cookies key: {"cookies": [...]} (storage_state)
      - Header string: "a=1; b=2" or "Cookie: a=1; b=2"
      - curl --cookie value (same as header)
      - Netscape cookies.txt (tab-separated, 7 columns)
    """
    text = text.strip()
    if not text:
        return []
    # 1. JSON
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
        except Exception:
            pass  # fall through to text parsing
    # 2. Netscape cookies.txt — tab-separated, lines may have leading "# "
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if lines and "\t" in lines[0]:
        out: list[dict] = []
        for ln in lines:
            parts = ln.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            try:
                exp = int(expires)
            except ValueError:
                exp = 0
            out.append({
                "name": name, "value": value,
                "domain": domain, "path": path,
                "secure": secure.lower() == "true",
                "expires": exp,
            })
        if out:
            return out
    # 3. Header / curl format: "name=val; name=val2" (optional "Cookie:" prefix)
    s = text
    if ":" in s.split(";", 1)[0]:
        # "Cookie: a=1; b=2" → strip header name
        head, rest = s.split(":", 1)
        if head.strip().lower() in ("cookie", "set-cookie"):
            s = rest
    out = []
    for pair in s.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name:
            continue
        c: dict = {"name": name, "value": value}
        if default_domain:
            c["domain"] = default_domain
        out.append(c)
    return out


async def cookie_set(name: str, value: str, domain: str, path: str = "/",
                     secure: bool = False, http_only: bool = False) -> str:
    """Set a cookie on the browser."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        from nodriver.cdp import network as cdp_network
        tab = BrowserState.active_tab()
        await tab.send(cdp_network.set_cookie(
            name=name, value=value, domain=domain, path=path,
            secure=secure, http_only=http_only,
        ))
        return ok(f"cookie {name} set on {domain}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cookie_delete(name: str, domain: Optional[str] = None) -> str:
    """Delete cookies matching name (optionally scoped to domain)."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        from nodriver.cdp import network as cdp_network
        tab = BrowserState.active_tab()
        if domain:
            await tab.send(cdp_network.delete_cookies(name=name, domain=domain))
        else:
            await tab.send(cdp_network.delete_cookies(name=name))
        return ok(f"cookie {name} deleted")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cookie_import(
    cookies: Optional[list[dict]] = None,
    file_path: Optional[str] = None,
    raw_text: Optional[str] = None,
    default_domain: Optional[str] = None,
    clear_first: bool = False,
) -> str:
    """Bulk-import cookies. Three input modes — pick whichever is easiest:

    1. cookies=[{...}]            inline array of dicts (DevTools / EditThisCookie shape)
    2. file_path="..."            JSON file (array OR storage_state {"cookies":[...]})
    3. raw_text="..."             paste any of these and we auto-parse:
                                    • JSON array / object (same shapes as 1+2)
                                    • Header string: "name=val; name2=val2" or
                                      "Cookie: name=val; name2=val2"
                                    • Netscape cookies.txt (tab-separated)
                                    • curl --cookie / -b argument string
       NOTE: header / netscape formats lack domain — pass default_domain=".example.com"
       (or call this AFTER navigate so the active tab's URL provides it).

    Per-cookie fields (when JSON):
      {"name":"...", "value":"...", "domain":".example.com", "path":"/",
       "expires":1234567890, "secure":true, "httpOnly":false, "sameSite":"Lax"}

    Args:
        cookies: inline array
        file_path: JSON file
        raw_text: any cookie text — format auto-detected
        default_domain: fallback domain for header / netscape cookies (or auto-uses
            current tab URL's host)
        clear_first: wipe all existing cookies before import (default False)

    For full cookies + localStorage + sessionStorage restore, use storage_state_load.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        if cookies is None and not file_path and not raw_text:
            return err("pass cookies=[...], file_path='...', or raw_text='...'")
        # raw_text → cookies list (auto-detect)
        if raw_text and not cookies and not file_path:
            cookies = _parse_cookie_text(raw_text, default_domain or _active_tab_host())
            if not cookies:
                return err("could not parse raw_text — got 0 cookies")
        if file_path:
            raw = Path(file_path).read_text()
            parsed = json.loads(raw)
            # Accept either [...] or {"cookies": [...]}
            if isinstance(parsed, dict) and "cookies" in parsed:
                cookies = parsed["cookies"]
            elif isinstance(parsed, list):
                cookies = parsed
            else:
                return err("file content must be a list or {cookies: [...]}")
        if not isinstance(cookies, list) or not cookies:
            return err("no cookies to import")
        from nodriver.cdp import network as cdp_network
        tab = BrowserState.active_tab()
        if clear_first:
            await tab.send(cdp_network.clear_browser_cookies())

        # CDP type coercion — required because CookieParam.to_json() iterates
        # fields and calls .to_json() on each, which crashes for plain strings
        # in enum-typed fields (sameSite) and ints where floats are expected.
        def _coerce_same_site(v):
            if v is None or v == "":
                return None
            # Already a CookieSameSite enum?
            try:
                from enum import Enum
                if isinstance(v, Enum):
                    return v
            except Exception:
                pass
            # Map common strings to the enum (case-insensitive). "Unspecified"
            # → None (not a CDP value, will let Chrome decide).
            sv = str(v).strip().lower()
            mapping = {"strict": "Strict", "lax": "Lax", "none": "None"}
            canonical = mapping.get(sv)
            if canonical is None:
                return None
            try:
                return cdp_network.CookieSameSite(canonical)
            except Exception:
                return None

        params: list = []
        skipped = 0
        for c in cookies:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                skipped += 1
                continue
            kwargs: dict = {"name": str(c["name"]), "value": str(c["value"])}
            # Plain string fields
            for src, dst in (
                ("domain", "domain"), ("path", "path"), ("url", "url"),
            ):
                if src in c and c[src] is not None:
                    kwargs[dst] = str(c[src])
            # Bool fields
            for src, dst in (
                ("secure", "secure"),
                ("httpOnly", "http_only"), ("http_only", "http_only"),
            ):
                if src in c and c[src] is not None:
                    kwargs[dst] = bool(c[src])
            # Float fields — CDP TimeSinceEpoch is a float; reject session
            # cookies (expires=-1 / "Session" / 0) since they don't map.
            if "expires" in c and c["expires"] is not None:
                try:
                    exp = float(c["expires"])
                    if exp > 0:
                        kwargs["expires"] = exp
                except (TypeError, ValueError):
                    pass
            # Enum field
            if "sameSite" in c or "same_site" in c:
                ss = _coerce_same_site(c.get("sameSite", c.get("same_site")))
                if ss is not None:
                    kwargs["same_site"] = ss
            try:
                params.append(cdp_network.CookieParam(**kwargs))
            except Exception:
                skipped += 1
        if not params:
            return err(f"no valid cookies (skipped {skipped} entries)")
        await tab.send(cdp_network.set_cookies(cookies=params))
        msg = f"imported {len(params)} cookies (clear_first={clear_first})"
        if skipped:
            msg += f", skipped {skipped} invalid"
        return ok(msg)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cookie_export(filename: Optional[str] = None,
                         url: Optional[str] = None) -> str:
    """Export cookies to a JSON file. Cookies-only (use storage_state_save for full session).

    Output is a plain JSON array compatible with cookie_import / EditThisCookie /
    Playwright cookies format. Saved to ~/.mcp-stealth/storage-states/.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        cookies = await BrowserState.browser.cookies.get_all()
        if url:
            cookies = [c for c in cookies if url in (c.domain or "")]
        data = [{
            "name": c.name, "value": c.value, "domain": c.domain,
            "path": c.path, "expires": c.expires,
            "httpOnly": c.http_only, "secure": c.secure,
            "sameSite": getattr(c, "same_site", None),
        } for c in cookies]
        ensure_dirs()
        fname = filename or ts_filename("cookies", "json")
        path = STORAGE_STATE_DIR / fname
        path.write_text(json.dumps(data, indent=2, default=str))
        return ok(f"{path}\nexported {len(data)} cookies")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 9. STORAGE (local + session)
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def localstorage_get(key: Optional[str] = None) -> str:
    """Get localStorage — all keys or one specific key."""
    try:
        tab = BrowserState.active_tab()
        if key:
            result = await tab.evaluate(
                f"localStorage.getItem({json.dumps(key)})", return_by_value=True,
            )
            return ok(str(result) if result is not None else "null")
        all_ls = await tab.get_local_storage()
        return ok(json.dumps(all_ls, indent=2))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def localstorage_set(key: str, value: str) -> str:
    """Set a localStorage entry."""
    try:
        tab = BrowserState.active_tab()
        await tab.evaluate(
            f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})",
            return_by_value=True,
        )
        return ok("set")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def localstorage_clear() -> str:
    """Clear all localStorage for current origin."""
    try:
        tab = BrowserState.active_tab()
        await tab.evaluate("localStorage.clear()", return_by_value=True)
        return ok("cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_get(key: Optional[str] = None) -> str:
    """Get sessionStorage — all keys or one."""
    try:
        tab = BrowserState.active_tab()
        if key:
            result = await tab.evaluate(
                f"sessionStorage.getItem({json.dumps(key)})", return_by_value=True,
            )
            return ok(str(result) if result is not None else "null")
        # Enumerate all keys
        result = await tab.evaluate(
            "(() => {var o={}; for(var i=0;i<sessionStorage.length;i++){var k=sessionStorage.key(i); o[k]=sessionStorage.getItem(k);} return JSON.stringify(o);})()",
            return_by_value=True,
        )
        return ok(str(result or "{}"))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_set(key: str, value: str) -> str:
    """Set a sessionStorage entry."""
    try:
        tab = BrowserState.active_tab()
        await tab.evaluate(
            f"sessionStorage.setItem({json.dumps(key)}, {json.dumps(value)})",
            return_by_value=True,
        )
        return ok("set")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_clear() -> str:
    """Clear all sessionStorage for current origin (parity with localstorage_clear)."""
    try:
        tab = BrowserState.active_tab()
        await tab.evaluate("sessionStorage.clear()", return_by_value=True)
        return ok("cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cache_clear() -> str:
    """Clear the browser HTTP cache (CDP Network.clearBrowserCache).

    Mirrors DevTools → Application → Clear storage → Clear site data (cache).
    Does NOT touch cookies, localStorage, or IndexedDB — use dedicated tools
    or browser_launch(persistent=False) for a full wipe.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        await tab.send(cdp_network.clear_browser_cache())
        return ok("browser HTTP cache cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def indexeddb_list() -> str:
    """List IndexedDB databases for the current origin.

    Reads via CDP IndexedDB.requestDatabaseNames. Use indexeddb_delete(name)
    to drop one. Useful for clearing SPA state (many PWAs store auth / drafts
    in IndexedDB rather than localStorage).
    """
    try:
        tab = BrowserState.active_tab()
        url = await get_url(tab)
        from urllib.parse import urlparse
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
        if not origin:
            return err(f"cannot derive origin from URL: {url}")
        from nodriver.cdp import indexed_db as cdp_idb
        await tab.send(cdp_idb.enable())
        names = await tab.send(cdp_idb.request_database_names(security_origin=origin))
        if not names:
            return ok(f"no IndexedDB databases for {origin}")
        lines = [f"IndexedDB databases for {origin}:"]
        for n in names:
            lines.append(f"  {n}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def indexeddb_delete(database_name: str) -> str:
    """Delete an IndexedDB database by name (scoped to current origin)."""
    try:
        tab = BrowserState.active_tab()
        url = await get_url(tab)
        from urllib.parse import urlparse
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
        if not origin:
            return err(f"cannot derive origin from URL: {url}")
        from nodriver.cdp import indexed_db as cdp_idb
        await tab.send(cdp_idb.delete_database(
            database_name=database_name, security_origin=origin,
        ))
        return ok(f"deleted IndexedDB '{database_name}' for {origin}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 10. JAVASCRIPT
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def evaluate(expression: str) -> str:
    """Execute arbitrary JS expression in page context. Returns stringified result."""
    try:
        tab = BrowserState.active_tab()
        result = await _wait(tab.evaluate(expression, return_by_value=True), what="evaluate")
        # Unwrap nodriver RemoteObject if returned (happens for some primitives)
        if hasattr(result, "value") and not isinstance(result, (str, int, float, bool, list, dict)):
            result = result.value
        if result is None:
            return ok("null")
        if isinstance(result, (dict, list)):
            return ok(json.dumps(result, indent=2, default=str))
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def inject_init_script(script: str) -> str:
    """Register a script that runs on every new document (before page scripts)."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import page as cdp_page
        await tab.send(cdp_page.add_script_to_evaluate_on_new_document(source=script))
        return ok("init script registered")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 11. INSPECTION
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def inspect_element(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Return tag, attributes, position, text for an element."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos = await el.get_position()
        info = {
            "tag": el.tag_name,
            "attributes": dict(el.attrs) if el.attrs else {},
            "text": (el.text_all or "")[:200],
            "position": {
                "x": pos.left, "y": pos.top,
                "width": pos.width, "height": pos.height,
            } if pos else None,
        }
        return ok(json.dumps(info, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_attribute(
    name: str, ref: Optional[str] = None, selector: Optional[str] = None,
) -> str:
    """Get attribute value of element."""
    try:
        tab = BrowserState.active_tab()
        sel_for_js = f'[data-mcp-ref="{ref}"]' if ref else selector
        if not sel_for_js:
            return err("ref or selector required")
        result = await tab.evaluate(
            f'document.querySelector({json.dumps(sel_for_js)}).getAttribute({json.dumps(name)})',
            return_by_value=True,
        )
        return ok(str(result) if result is not None else "null")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def query_selector_all(selector: str, limit: int = 50) -> str:
    """Return count + attrs of all elements matching CSS selector."""
    try:
        tab = BrowserState.active_tab()
        result = await tab.evaluate(
            f"(() => {{ var els = document.querySelectorAll({json.dumps(selector)}); "
            f"var out = []; for(var i=0;i<Math.min(els.length,{limit});i++){{"
            "var el=els[i]; var r=el.getBoundingClientRect(); "
            "out.push({tag:el.tagName.toLowerCase(), text:(el.innerText||'').slice(0,80), "
            "href:el.href||'', id:el.id||'', class:el.className||'', x:r.x, y:r.y}); }"
            "return JSON.stringify({count:els.length,items:out}); })()",
            return_by_value=True,
        )
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_links(same_origin: bool = False, limit: int = 200) -> str:
    """List all <a> links on page."""
    try:
        tab = BrowserState.active_tab()
        js = (
            "(() => { var origin = location.origin; var links = "
            "[...document.querySelectorAll('a[href]')].map(a=>({"
            "text:(a.innerText||'').trim().slice(0,100), href:a.href}))"
            f".filter(l => l.href && (!{str(same_origin).lower()} || l.href.startsWith(origin)))"
            f".slice(0, {limit}); return JSON.stringify(links); }})()"
        )
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 12. FRAMES
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def list_frames() -> str:
    """List all iframes and their URLs."""
    try:
        tab = BrowserState.active_tab()
        tree = await tab.get_frame_tree()
        frames = []

        def walk(node, depth=0):
            frame = node.frame if hasattr(node, "frame") else node
            frames.append({
                "id": getattr(frame, "id_", None),
                "url": getattr(frame, "url", None),
                "depth": depth,
            })
            for child in getattr(node, "child_frames", None) or []:
                walk(child, depth + 1)
        walk(tree)
        return ok(json.dumps(frames, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def frame_evaluate(frame_url_pattern: str, expression: str) -> str:
    """Run JS inside an iframe matching URL pattern."""
    try:
        tab = BrowserState.active_tab()
        regex = re.compile(frame_url_pattern)
        # Simplified: find iframe element by src match, then evaluate within
        result = await tab.evaluate(
            f"(() => {{ var iframes = document.querySelectorAll('iframe'); "
            f"for (var f of iframes) {{ if (/{regex.pattern}/.test(f.src)) {{ "
            f"try {{ return JSON.stringify(f.contentWindow.eval({json.dumps(expression)})); }} "
            f"catch(e){{ return 'ERR:'+e.message; }} }} }} return 'no frame matched'; }})()",
            return_by_value=True,
        )
        return ok(str(result))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 13. BATCH
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def batch_actions(actions: list[dict]) -> str:
    """Execute a list of actions sequentially.

    Each action: {type: click|fill|type|wait|press|navigate, ...params}
    Example: [{"type":"click","ref":"e3"},{"type":"fill","ref":"e4","value":"x"}]
    """
    if not isinstance(actions, list):
        actions = parse_json(actions, [])
    results = []
    for i, act in enumerate(actions):
        atype = act.get("type")
        try:
            if atype == "click":
                r = await click(ref=act.get("ref"), selector=act.get("selector"),
                                 humanize=act.get("humanize", False))
            elif atype == "fill":
                r = await fill(ref=act.get("ref"), selector=act.get("selector"),
                                value=act.get("value", ""))
            elif atype == "type":
                r = await type_text(text=act.get("text", ""),
                                     humanize=act.get("humanize", False))
            elif atype == "press":
                r = await press_key(key=act.get("key", "Enter"))
            elif atype == "wait":
                r = await wait_for(selector=act.get("selector"), text=act.get("text"),
                                    timeout=act.get("timeout", 5.0))
            elif atype == "navigate":
                r = await navigate(url=act.get("url"))
            else:
                r = err(f"unknown action type: {atype}")
            results.append(f"[{i}] {atype}: {str(r)[:80]}")
        except Exception as e:
            results.append(f"[{i}] {atype}: ERR {e}")
            if act.get("stop_on_error"):
                break
    return ok("\n".join(results))


@mcp.tool()
async def fill_form(fields: list[dict], submit_ref: Optional[str] = None) -> str:
    """Fill multiple fields then optionally submit.

    fields: [{ref: "e1", value: "..."}, {selector: "#email", value: "..."}]
    """
    if not isinstance(fields, list):
        fields = parse_json(fields, [])
    results = []
    for f in fields:
        r = await fill(ref=f.get("ref"), selector=f.get("selector"),
                        value=f.get("value", ""))
        results.append(str(r)[:50])
    if submit_ref:
        r = await click(ref=submit_ref)
        results.append(f"submit: {str(r)[:50]}")
    return ok("\n".join(results))


@mcp.tool()
async def navigate_and_snapshot(url: str) -> str:
    """Navigate then immediately snapshot — common pattern."""
    nav = await navigate(url)
    if str(nav).startswith("Error:"):
        return nav
    return await browser_snapshot()


# ══════════════════════════════════════════════════════════════════════════
# 14. VIEWPORT + SCROLL
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def get_viewport_size() -> str:
    """Return current window dimensions."""
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
    """Resize the browser window."""
    try:
        tab = BrowserState.active_tab()
        await tab.set_window_size(width=width, height=height)
        return ok(f"set to {width}x{height}")
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
    """
    try:
        tab = BrowserState.active_tab()
        if direction == "top":
            await tab.evaluate("window.scrollTo(0,0)", return_by_value=True)
            return ok("scrolled to top")
        if direction == "bottom":
            await tab.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)",
                return_by_value=True,
            )
            return ok("scrolled to bottom")

        dy = amount if direction == "down" else -amount
        if humanize:
            actual = await humanized_scroll(tab, dy)
            return ok(f"scrolled {direction} {actual}px (humanized wheel events)")
        # Instant mode — single wheel dispatch (still real event)
        from nodriver.cdp import input_ as cdp_input
        await tab.send(cdp_input.dispatch_mouse_event(
            type_="mouseWheel", x=500, y=400, delta_x=0, delta_y=dy,
        ))
        return ok(f"scrolled {direction} {amount}px (instant wheel)")
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
    try:
        tab = BrowserState.active_tab()
        if ref:
            target_sel = f'[data-mcp-ref="{ref}"]'
        else:
            target_sel = selector
        behavior = "smooth" if smooth else "auto"
        js = (
            f"(() => {{ var el = document.querySelector({json.dumps(target_sel)}); "
            f"if (!el) return 'not_found'; "
            f"el.scrollIntoView({{block:{json.dumps(block)}, behavior:{json.dumps(behavior)}}}); "
            f"return 'ok'; }})()"
        )
        res = await tab.evaluate(js, return_by_value=True)
        if str(res) == "not_found":
            return err(f"element not found: {target_sel}")
        # Wait for smooth scroll to complete
        if smooth:
            await asyncio.sleep(random.uniform(0.4, 0.8))
        return ok(f"scrolled element into view (block={block})")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 15. DIALOG + ACCESSIBILITY
# ══════════════════════════════════════════════════════════════════════════

_dialog_pre_action: dict[str, Any] = {"action": None, "text": None}


@mcp.tool()
async def dialog_handle(action: str = "accept", text: Optional[str] = None) -> str:
    """Pre-arm handler for next alert/confirm/prompt. Call BEFORE action that triggers it."""
    _dialog_pre_action["action"] = action
    _dialog_pre_action["text"] = text
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import page as cdp_page

        async def handle(_event):
            try:
                await tab.send(cdp_page.handle_java_script_dialog(
                    accept=(action == "accept"),
                    prompt_text=text or "",
                ))
            except Exception:
                pass

        tab.add_handler(cdp_page.JavascriptDialogOpening, handle)
        return ok(f"dialog handler armed ({action})")
    except Exception as e:
        return err(str(e))


# ── dialog_auto_handle (persistent, type-filterable) ──────────────────────
# Read-at-fire-time config so the LLM can update action/types without
# re-registering the handler. Keyed by tab id() to avoid duplicate handlers
# on the same tab.
_DIALOG_AUTO_CFG: dict = {
    "enabled": False, "action": "accept", "text": "",
    "types": None,  # None=all; or set like {"beforeunload"}
    "_registered_tab_ids": set(),
}


@mcp.tool()
async def dialog_auto_handle(
    action: str = "accept",
    enabled: bool = True,
    types: Optional[list[str]] = None,
    text: str = "",
) -> str:
    """⭐ Install a PERSISTENT auto-handler for native browser dialogs.
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
    _DIALOG_AUTO_CFG["enabled"] = bool(enabled)
    _DIALOG_AUTO_CFG["action"] = action
    _DIALOG_AUTO_CFG["text"] = text or ""
    _DIALOG_AUTO_CFG["types"] = set(types) if types else None

    if not enabled:
        return ok("dialog auto-handle disabled (config preserved — re-enable to resume)")

    try:
        tab = BrowserState.active_tab()
        tab_id = id(tab)
        if tab_id in _DIALOG_AUTO_CFG["_registered_tab_ids"]:
            return ok(
                f"auto-handle config updated (action={action}, "
                f"types={list(types) if types else 'all'}); already armed on this tab"
            )

        from nodriver.cdp import page as cdp_page

        async def auto_handler(event):
            cfg = _DIALOG_AUTO_CFG
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


@mcp.tool()
async def console_start() -> str:
    """Begin capturing console messages of active tab."""
    try:
        tab = BrowserState.active_tab()
        BrowserState.console_logs = []
        BrowserState.capture_console = True
        from nodriver.cdp import runtime as cdp_runtime

        async def handle(event):
            try:
                args = [getattr(a, "value", None) or getattr(a, "description", "")
                        for a in (event.args or [])]
                BrowserState.console_logs.append({
                    "type": event.type_,
                    "text": " ".join(str(a) for a in args)[:500],
                })
            except Exception:
                pass

        tab.add_handler(cdp_runtime.ConsoleAPICalled, handle)
        return ok("console capture started")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def console_get(limit: int = 100) -> str:
    """Retrieve captured console messages (most recent first)."""
    logs = BrowserState.console_logs[-limit:]
    return ok(json.dumps(logs, indent=2, default=str))


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
        BrowserState.network_logs = []
        BrowserState.network_index = {}
        BrowserState.capture_network = True
        from nodriver.cdp import network as cdp_network

        # Enable Network domain so getResponseBody works later
        try:
            await tab.send(cdp_network.enable())
        except Exception:
            pass

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
                }
                if capture_bodies:
                    BrowserState.network_index[rid] = entry
                BrowserState.network_logs.append({
                    "type": "request", "request_id": rid,
                    "url": entry["url"], "method": entry["method"],
                })
            except Exception:
                pass

        async def on_res(event):
            try:
                rid = str(event.request_id)
                resp = event.response
                resp_headers = dict(getattr(resp, "headers", None) or {})
                if rid in BrowserState.network_index:
                    e = BrowserState.network_index[rid]
                    e["status"] = getattr(resp, "status", None)
                    e["response_headers"] = resp_headers
                    e["mime"] = getattr(resp, "mime_type", None)
                    e["size"] = getattr(resp, "encoded_data_length", None)
                BrowserState.network_logs.append({
                    "type": "response", "request_id": rid,
                    "url": getattr(resp, "url", ""),
                    "status": getattr(resp, "status", None),
                    "mime": getattr(resp, "mime_type", None),
                })
            except Exception:
                pass

        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        tab.add_handler(cdp_network.ResponseReceived, on_res)
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
) -> str:
    """Retrieve captured network events.

    Args:
        limit: max entries returned (most recent first)
        filter_url: substring filter on URL
        include_body: fetch response bodies via CDP Network.getResponseBody
            for each matching entry. Bodies are truncated to max_body_bytes.
            Requires network_start(capture_bodies=True) (default).
        max_body_bytes: cap per-body length (default 10000)
        full: if True, return entries with full headers + body fields
            from network_index (use this once you've called network_start).
            Default False = legacy flat event stream (backward compat).
    """
    if full or include_body:
        entries = list(BrowserState.network_index.values())
        if filter_url:
            entries = [e for e in entries if filter_url in e.get("url", "")]
        entries = entries[-limit:]
        if include_body and entries:
            try:
                tab = BrowserState.active_tab()
                from nodriver.cdp import network as cdp_network
                for e in entries:
                    if e.get("response_body") is not None:
                        continue
                    rid = e.get("request_id")
                    if not rid:
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
                        if len(body_str) > max_body_bytes:
                            body_str = body_str[:max_body_bytes] + (
                                f"... [truncated, original {len(body)} chars]"
                            )
                        e["response_body"] = body_str
                    except asyncio.TimeoutError:
                        e["response_body"] = "<getResponseBody timeout>"
                    except Exception as ex:
                        e["response_body"] = f"<getResponseBody error: {ex}>"
            except Exception:
                pass
        return ok(json.dumps(entries, indent=2, default=str)[:50000])
    # Legacy event-stream view
    logs = BrowserState.network_logs
    if filter_url:
        logs = [l for l in logs if filter_url in l.get("url", "")]
    return ok(json.dumps(logs[-limit:], indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════
# 17. DEBUG / META
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def server_status() -> str:
    """Diagnostic info about the server and browser."""
    status = {
        "version": __version__,
        "browser_running": BrowserState.is_up(),
        "tabs": len(BrowserState.tabs),
        "active_tab": BrowserState.active_tab_index,
        "console_capture": BrowserState.capture_console,
        "network_capture": BrowserState.capture_network,
        "console_logs": len(BrowserState.console_logs),
        "network_logs": len(BrowserState.network_logs),
        "page_errors": len(BrowserState.page_errors),
        "profile_dir": str(PROFILE_DIR),
    }
    return ok(json.dumps(status, indent=2))


@mcp.tool()
async def get_page_errors() -> str:
    """Retrieve JS errors caught on active tab."""
    return ok(json.dumps(BrowserState.page_errors, indent=2, default=str))


@mcp.tool()
async def export_har(filename: Optional[str] = None) -> str:
    """Export captured network traffic to HAR-like JSON file."""
    try:
        ensure_dirs()
        fname = filename or ts_filename("traffic", "har")
        path = EXPORT_DIR / fname
        path.write_text(json.dumps({
            "log": {
                "version": "1.2",
                "creator": {"name": "mcp-stealth-chrome", "version": __version__},
                "entries": BrowserState.network_logs,
            }
        }, indent=2, default=str))
        return ok(str(path))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 18. SCRAPING
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def detect_content_pattern() -> str:
    """Heuristically detect the most likely repeating container on page.

    Useful for scraping job listings, product cards, search results.
    Returns top-3 candidate CSS selectors ranked by child-similarity.
    """
    try:
        tab = BrowserState.active_tab()
        js = r"""
        (() => {
          var groups = {};
          var els = document.querySelectorAll('div,li,article,section');
          for (var el of els) {
            var parent = el.parentElement;
            if (!parent || parent.children.length < 3) continue;
            var sig = parent.tagName + '>' + el.tagName + '.' + (el.className||'').split(' ').slice(0,2).join('.');
            groups[sig] = groups[sig] || {count: 0, sample: el, parent: parent};
            groups[sig].count++;
          }
          var ranked = Object.entries(groups)
            .filter(([,v]) => v.count >= 3)
            .sort((a,b) => b[1].count - a[1].count).slice(0,3);
          return JSON.stringify(ranked.map(([sig,v]) => ({
            signature: sig, count: v.count,
            sample_selector: v.sample.tagName.toLowerCase() +
              (v.sample.className ? '.'+v.sample.className.split(' ').slice(0,2).join('.') : ''),
            parent_selector: v.parent.tagName.toLowerCase() +
              (v.parent.id?'#'+v.parent.id:'') +
              (v.parent.className ? '.'+v.parent.className.split(' ').slice(0,2).join('.') : '')
          })));
        })()
        """
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def extract_structured(
    container_selector: str,
    fields: list[dict],
    limit: int = 100,
) -> str:
    """Extract structured data from repeating containers.

    fields: [{name: "title", selector: ".job-title", attribute: "text|href|src|..."}]
    Only direct text nodes of element are captured for "text" (prevents child-field mixing).
    """
    if not isinstance(fields, list):
        fields = parse_json(fields, [])
    try:
        tab = BrowserState.active_tab()
        fields_json = json.dumps(fields)
        js = r"""
        (() => {
          var SELECT = """ + json.dumps(container_selector) + r""";
          var FIELDS = """ + fields_json + r""";
          var LIMIT = """ + str(limit) + r""";

          // Filter top-level — skip containers nested inside another same-selector match
          var all = Array.from(document.querySelectorAll(SELECT));
          var tops = all.filter(el => {
            var p = el.parentElement;
            while (p) { if (all.includes(p)) return false; p = p.parentElement; }
            return true;
          }).slice(0, LIMIT);

          function directText(el) {
            var out = '';
            for (var n of el.childNodes) if (n.nodeType === 3) out += n.textContent;
            return out.trim();
          }

          var rows = tops.map(container => {
            var row = {};
            for (var f of FIELDS) {
              var target = container.querySelector(f.selector);
              if (!target) { row[f.name] = null; continue; }
              var attr = f.attribute || 'text';
              if (attr === 'text') row[f.name] = (target.innerText || '').trim();
              else if (attr === 'direct_text_only') row[f.name] = directText(target);
              else if (attr === 'html') row[f.name] = target.innerHTML;
              else row[f.name] = target.getAttribute(attr);
            }
            return row;
          });
          return JSON.stringify(rows);
        })()
        """
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def extract_table(selector: str = "table", include_headers: bool = True) -> str:
    """Extract a <table> as JSON rows with optional header keys."""
    try:
        tab = BrowserState.active_tab()
        js = r"""
        (() => {
          var t = document.querySelector(""" + json.dumps(selector) + r""");
          if (!t) return JSON.stringify({error:'table not found'});
          var rows = [...t.querySelectorAll('tr')];
          var headers = [];
          var out = [];
          rows.forEach((r, i) => {
            var cells = [...r.children].map(c => c.innerText.trim());
            if (i === 0 && """ + ('true' if include_headers else 'false') + r""") headers = cells;
            else if (headers.length) {
              var obj = {}; cells.forEach((c, j) => obj[headers[j]||`col${j}`] = c);
              out.push(obj);
            } else out.push(cells);
          });
          return JSON.stringify({headers: headers, rows: out});
        })()
        """
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def scrape_page(only_main_content: bool = True, max_chars: int = 8000) -> str:
    """Clean readable text extraction — drops nav, footer, scripts, styles.

    Smart-truncates at paragraph boundary (not mid-word).
    """
    try:
        tab = BrowserState.active_tab()
        js = r"""
        (() => {
          var main = """ + str(only_main_content).lower() + r""";
          var root = main ? (document.querySelector('main,article,[role=main]') || document.body) : document.body;
          var clone = root.cloneNode(true);
          clone.querySelectorAll('script,style,nav,footer,aside,noscript').forEach(e=>e.remove());
          var title = document.title;
          var url = location.href;
          var text = clone.innerText.replace(/\n{3,}/g, '\n\n').trim();
          var links = [...document.querySelectorAll('a[href]')]
            .slice(0,30).map(a => ({text: a.innerText.trim().slice(0,80), href: a.href}));
          return JSON.stringify({title, url, text, links});
        })()
        """
        raw = await tab.evaluate(js, return_by_value=True)
        data = parse_json(raw, {})
        text = data.get("text", "") if isinstance(data, dict) else ""
        if len(text) > max_chars:
            cut = text.rfind("\n", 0, max_chars)
            if cut == -1:
                cut = max_chars
            text = text[:cut] + f"\n\n[truncated at {cut}/{len(text)} chars]"
            if isinstance(data, dict):
                data["text"] = text
        return ok(json.dumps(data, indent=2, default=str))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 19. ⭐ DIFFERENTIATORS (vs vibheksoni/patchright-mcp-lite/puppeteer-real)
# ══════════════════════════════════════════════════════════════════════════


async def _apply_storage_state(browser: Browser, path: str) -> None:
    """Load cookies+localStorage from a JSON file into browser."""
    data = json.loads(Path(path).read_text())
    # Cookies
    from nodriver.cdp import network as cdp_network
    tab = browser.tabs[0] if browser.tabs else await browser.get("about:blank")
    for c in data.get("cookies", []):
        try:
            await tab.send(cdp_network.set_cookie(
                name=c.get("name"), value=c.get("value"),
                domain=c.get("domain"), path=c.get("path", "/"),
                secure=c.get("secure", False), http_only=c.get("http_only", False),
                expires=c.get("expires"),
            ))
        except Exception:
            continue
    # LocalStorage — set via script per origin (requires navigation to that origin first)
    for origin, pairs in (data.get("origins") or {}).items():
        try:
            await tab.get(origin)
            for k, v in pairs.items():
                await tab.evaluate(
                    f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})",
                    return_by_value=True,
                )
        except Exception:
            continue


@mcp.tool()
async def storage_state_save(filename: Optional[str] = None) -> str:
    """⭐ Save cookies + localStorage of current origin to JSON.

    DIFFERENTIATOR: Per research, session-reuse is THE most reliable way to
    bypass Cloudflare Turnstile — it never triggers if session valid.
    Login manually once → save state → reuse forever until expiry.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        ensure_dirs()
        fname = filename or ts_filename("state", "json")
        path = STORAGE_STATE_DIR / fname
        tab = BrowserState.active_tab()
        cookies = await BrowserState.browser.cookies.get_all()
        local_storage = await tab.get_local_storage()
        origin = (await get_url(tab)).rsplit("/", 1)[0] if await get_url(tab) else ""
        state = {
            "cookies": [{
                "name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires,
                "secure": c.secure, "http_only": c.http_only,
            } for c in cookies],
            "origins": {origin: local_storage} if origin else {},
            "saved_at": time.time(),
        }
        path.write_text(json.dumps(state, indent=2, default=str))
        return ok(f"saved {len(state['cookies'])} cookies to {path}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def storage_state_load(file_path: str) -> str:
    """⭐ Load cookies + localStorage from a saved JSON file.

    Call BEFORE navigating to protected site so session is ready.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running — launch first")
        await _apply_storage_state(BrowserState.browser, file_path)
        return ok(f"storage state loaded from {file_path}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def solve_captcha(
    kind: Literal["turnstile", "recaptcha_v2", "recaptcha_v3", "hcaptcha"],
    website_url: str,
    website_key: str,
    api_key: Optional[str] = None,
    inject_selector: Optional[str] = None,
    action: Optional[str] = None,
) -> str:
    """Solve a CAPTCHA via CapSolver HTTP API.

    kind: turnstile | recaptcha_v2 | recaptcha_v3 | hcaptcha
    Needs CAPSOLVER_KEY env var (or pass api_key). Returns solved token.
    If inject_selector given, also injects token into that form field
    (e.g. input[name='cf-turnstile-response']).
    """
    type_map = {
        "turnstile": "AntiTurnstileTaskProxyLess",
        "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
        "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
    }
    meta = {"action": action} if action else None
    try:
        token = await capsolver_solve(
            task_type=type_map[kind],
            website_url=website_url,
            website_key=website_key,
            api_key=api_key,
            metadata=meta,
        )
    except CapSolverError as e:
        return err(f"CapSolver: {e}")
    # Inject if requested
    if inject_selector and BrowserState.is_up():
        try:
            tab = BrowserState.active_tab()
            await tab.evaluate(
                f'(() => {{ var el = document.querySelector({json.dumps(inject_selector)}); '
                f'if (el) {{ el.value = {json.dumps(token)}; '
                f'el.dispatchEvent(new Event("input",{{bubbles:true}})); '
                f'el.dispatchEvent(new Event("change",{{bubbles:true}})); return true; }} return false; }})()',
                return_by_value=True,
            )
        except Exception:
            pass
    return ok(f"token: {token}")


@mcp.tool()
async def verify_cf(template_image: Optional[str] = None) -> str:
    """⭐ Use nodriver's built-in Cloudflare challenge verification.

    Uses OpenCV template matching to find the Turnstile checkbox on a screenshot
    and click it. template_image is a path to a cropped image of the checkbox;
    without it, the bundled English default is used.

    Works on simple CF interstitials. For managed-mode Turnstile (ChatGPT-level),
    combine with storage_state or solve_captcha.
    """
    try:
        tab = BrowserState.active_tab()
        await tab.verify_cf(template_image=template_image, flash=False)
        return ok("cloudflare challenge attempted via template-matching click")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def fingerprint_rotate(
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform: Optional[str] = None,
    timezone: Optional[str] = None,
) -> str:
    """Override fingerprint vectors for active tab: user_agent, accept_language,
    platform (Win32/MacIntel/Linux x86_64), timezone (Asia/Jakarta, etc).
    Applied via CDP. Persists until next tab creation.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        from nodriver.cdp import emulation as cdp_emulation
        if user_agent or accept_language or platform:
            kwargs = {}
            if user_agent:
                kwargs["user_agent"] = user_agent
            if accept_language:
                kwargs["accept_language"] = accept_language
            if platform:
                kwargs["platform"] = platform
            await tab.send(cdp_network.set_user_agent_override(**kwargs))
        if timezone:
            await tab.send(cdp_emulation.set_timezone_override(timezone_id=timezone))
        return ok("fingerprint overrides applied")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def humanize_click(ref: Optional[str] = None,
                          selector: Optional[str] = None) -> str:
    """⭐ Click with Bezier-curve mouse approach + randomized dwell."""
    return await click(ref=ref, selector=selector, humanize=True)


@mcp.tool()
async def humanize_type(text: str, mean_delay: float = 0.12) -> str:
    """⭐ Type with Gaussian-distributed keystroke delays."""
    return await type_text(text=text, humanize=True, mean_delay=mean_delay)


# ══════════════════════════════════════════════════════════════════════════
# 20. ⭐⭐ PRECISION MOUSE KIT (#1 differentiator)
# ══════════════════════════════════════════════════════════════════════════
#
# Other MCPs click at the CENTER of bounding boxes.
# We click where humans actually click — offset-calibrated positions
# for checkboxes, toggles, image-matched coordinates, recorded trajectories.
#
# Proven: these tools bypass Cloudflare Turnstile on dash.cloudflare.com (2026-04).


@mcp.tool()
async def click_turnstile(
    offset_x: int = 30,
    offset_y: Optional[int] = None,
    fallback_template: bool = True,
) -> str:
    """Auto-find and click the Cloudflare Turnstile checkbox.

    Three-tier detection strategy:
      1. Primary selectors: iframe[src*=challenges.cloudflare.com], [data-sitekey], .cf-turnstile
      2. Secondary: .turnstile, input[name=cf-turnstile-response] → nearest sized container
      3. Fallback (if fallback_template=True): OpenCV template match via verify_cf
         — covers out-of-process iframe cases (e.g. nopecha.com/captcha/turnstile)

    Args:
        offset_x: pixels from widget left edge (default 30, calibrated for CF checkbox)
        offset_y: vertical offset (default = container center)
        fallback_template: if selectors fail, try OpenCV template click (default True)

    Known to work on: 2captcha.com/demo/cloudflare-turnstile, dash.cloudflare.com login,
    nopecha.com/captcha/turnstile (via template fallback).
    Does NOT work on: Cloudflare managed-mode interstitials ("Just a moment..." full-page
    challenges) — use solve_captcha or storage_state_load for those.
    """
    try:
        tab = BrowserState.active_tab()
        # Wait a moment for widget to fully render if just navigated
        await asyncio.sleep(0.5)
        coords_raw = await tab.evaluate(
            """
            (() => {
              // Tier 1: standard CF attributes
              const primary = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                '[data-testid*="challenge-widget"]',
                '[data-testid*="turnstile"]',
                '[data-sitekey]',
                '.cf-turnstile',
              ];
              // Tier 2: common non-standard wrappers (nopecha, custom demos)
              const secondary = [
                '.turnstile',
                '[id*="turnstile" i]',
                '[id*="cf-chl"]',
                '[class*="turnstile" i]',
              ];
              const tryPick = (selectors, tier) => {
                for (const sel of selectors) {
                  const els = document.querySelectorAll(sel);
                  for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 20) continue;
                    return {
                      tier, found: sel,
                      left: Math.round(r.left),
                      top: Math.round(r.top),
                      width: Math.round(r.width),
                      height: Math.round(r.height),
                    };
                  }
                }
                return null;
              };
              let hit = tryPick(primary, 'primary') || tryPick(secondary, 'secondary');
              if (hit) return JSON.stringify(hit);
              // Tier 2b: find hidden cf-turnstile-response input → walk up to sized ancestor
              const inp = document.querySelector('input[name="cf-turnstile-response"]');
              if (inp) {
                let el = inp.parentElement;
                while (el && el !== document.body) {
                  const r = el.getBoundingClientRect();
                  if (r.width >= 80 && r.height >= 30) {
                    return JSON.stringify({
                      tier: 'response-input-ancestor',
                      found: 'input[name="cf-turnstile-response"]→ancestor',
                      left: Math.round(r.left),
                      top: Math.round(r.top),
                      width: Math.round(r.width),
                      height: Math.round(r.height),
                    });
                  }
                  el = el.parentElement;
                }
              }
              return 'not_found';
            })()
            """,
            return_by_value=True,
        )
        data = parse_json(coords_raw, None)
        if isinstance(data, dict):
            target_x = data["left"] + offset_x
            target_y = data["top"] + (offset_y if offset_y is not None else data["height"] // 2)
            start_x = target_x + 180
            start_y = target_y - 80
            await humanized_move(tab, start_x, start_y, target_x, target_y)
            await asyncio.sleep(0.15)
            await tab.mouse_click(target_x, target_y)
            return ok(
                f"clicked Turnstile at ({target_x},{target_y}) — "
                f"found via {data['found']} [tier={data.get('tier','primary')}]"
            )
        # Tier 3: fallback to OpenCV template matching (nodriver built-in)
        if fallback_template:
            try:
                await tab.verify_cf(flash=False)
                return ok(
                    "clicked Turnstile via template-matching fallback "
                    "(selector tiers exhausted — out-of-process iframe likely)"
                )
            except Exception as tpl_err:
                return err(
                    f"Turnstile widget not found via selectors ({coords_raw}); "
                    f"template fallback also failed: {tpl_err}"
                )
        return err(f"Turnstile widget not found on page ({coords_raw})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def click_element_offset(
    x_percent: float = 50.0,
    y_percent: float = 50.0,
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    humanize: bool = True,
) -> str:
    """Click inside element at percentage position (not center).

    Examples:
      x_percent=8          → checkbox at left edge of label
      x_percent=90         → right-side toggle slider
      y_percent=20         → top portion of a card
    """
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos = await el.get_position()
        if pos is None:
            return err("element has no position")
        target_x = int(pos.left + pos.width * (x_percent / 100.0))
        target_y = int(pos.top + pos.height * (y_percent / 100.0))
        if humanize:
            await humanized_move(tab, target_x + 120, target_y - 60, target_x, target_y)
            await asyncio.sleep(0.12)
        await tab.mouse_click(target_x, target_y)
        return ok(f"clicked at ({target_x},{target_y}) = {x_percent}% x {y_percent}% of element")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def click_at_corner(
    corner: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right",
    offset: int = 8,
    ref: Optional[str] = None,
    selector: Optional[str] = None,
) -> str:
    """Click at a corner of element (close X buttons, delete icons, dismiss).

    corner: top-left | top-right | bottom-left | bottom-right
    offset: inset pixels from corner (default 8px — works for most X buttons)
    """
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos = await el.get_position()
        if pos is None:
            return err("element has no position")
        if corner == "top-left":
            x, y = int(pos.left + offset), int(pos.top + offset)
        elif corner == "top-right":
            x, y = int(pos.left + pos.width - offset), int(pos.top + offset)
        elif corner == "bottom-left":
            x, y = int(pos.left + offset), int(pos.top + pos.height - offset)
        else:
            x, y = int(pos.left + pos.width - offset), int(pos.top + pos.height - offset)
        await tab.mouse_click(x, y)
        return ok(f"clicked {corner} at ({x},{y})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def find_by_image(
    template_path: str,
    threshold: float = 0.85,
) -> str:
    """⭐ Find an image on the current page via OpenCV template matching.

    Takes a fresh screenshot, matches against template_path image, returns
    (x, y) center of best match. Use for finding visual buttons/icons when
    DOM selectors aren't available.

    Returns JSON: {"found": true, "x": ..., "y": ..., "score": ..., "template": "..."}
    """
    try:
        import cv2
        import numpy as np
        tab = BrowserState.active_tab()
        ensure_dirs()
        tmp_path = SCREENSHOT_DIR / ts_filename("match-tmp", "png")
        await tab.save_screenshot(filename=str(tmp_path))

        page_img = cv2.imread(str(tmp_path))
        template = cv2.imread(template_path)
        if page_img is None:
            return err(f"could not read screenshot at {tmp_path}")
        if template is None:
            return err(f"could not read template at {template_path}")

        # Scale for Retina: screenshot is 2x CSS pixels on macOS
        scale = 2.0
        result = cv2.matchTemplate(page_img, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return ok(json.dumps({
                "found": False, "score": float(max_val),
                "threshold": threshold, "template": template_path,
            }))
        th, tw = template.shape[:2]
        # Convert screenshot-pixel coords back to CSS coords
        cx = int((max_loc[0] + tw / 2) / scale)
        cy = int((max_loc[1] + th / 2) / scale)
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return ok(json.dumps({
            "found": True, "x": cx, "y": cy,
            "score": float(max_val), "template": template_path,
        }))
    except ImportError:
        return err("opencv-python not installed")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def click_at_image(
    template_path: str,
    threshold: float = 0.85,
    humanize: bool = True,
) -> str:
    """⭐ Find image via template matching, then click its center.

    Combines find_by_image + humanize_move + mouse_click. Useful for visual
    CAPTCHAs, custom buttons without reliable selectors, or interacting with
    canvas-based UIs.
    """
    raw = await find_by_image(template_path=template_path, threshold=threshold)
    data = parse_json(raw, {})
    if not isinstance(data, dict) or not data.get("found"):
        return err(f"image not found (result: {raw})")
    x, y = int(data["x"]), int(data["y"])
    try:
        tab = BrowserState.active_tab()
        if humanize:
            await humanized_move(tab, x + 150, y - 70, x, y)
            await asyncio.sleep(0.1)
        await tab.mouse_click(x, y)
        return ok(f"clicked image match at ({x},{y}) score={data['score']:.3f}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_drift(
    duration_seconds: float = 2.0,
    segments: int = 4,
) -> str:
    """⭐ Simulate idle mouse wandering to pass behavioral ML.

    Random Bezier segments across the viewport — mimics a user thinking.
    Call BEFORE a critical interaction (form submit, button click) to
    establish 'human' behavior pattern before the deterministic action.
    """
    try:
        import random
        tab = BrowserState.active_tab()
        # Get viewport
        vp = await tab.evaluate(
            "JSON.stringify({w: innerWidth, h: innerHeight})", return_by_value=True,
        )
        vp_data = parse_json(vp, {"w": 1280, "h": 800})
        w, h = vp_data.get("w", 1280), vp_data.get("h", 800)
        per_segment = duration_seconds / max(1, segments)
        cur_x, cur_y = random.randint(w // 4, 3 * w // 4), random.randint(h // 4, 3 * h // 4)
        for _ in range(segments):
            next_x = random.randint(int(w * 0.1), int(w * 0.9))
            next_y = random.randint(int(h * 0.1), int(h * 0.9))
            await humanized_move(tab, cur_x, cur_y, next_x, next_y, steps=int(per_segment * 40))
            cur_x, cur_y = next_x, next_y
            await asyncio.sleep(random.uniform(0.1, 0.4))
        return ok(f"drifted through {segments} segments over {duration_seconds}s")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_record(duration_seconds: float = 5.0) -> str:
    """⭐ Record real mouse movements from the page for later replay.

    Injects a listener that captures mousemove events during duration. Move
    your mouse naturally in the Chrome window while this runs. The recorded
    path can then be played back via mouse_replay() — highest-stealth
    behavioral pattern (indistinguishable from human).

    Returns: JSON array of {t, x, y} events.
    """
    try:
        tab = BrowserState.active_tab()
        await tab.evaluate(
            """
            (() => {
              window.__mcpMouseRec = [];
              const t0 = performance.now();
              window.__mcpMouseHandler = (e) => {
                window.__mcpMouseRec.push({t: Math.round(performance.now() - t0), x: e.clientX, y: e.clientY});
              };
              document.addEventListener('mousemove', window.__mcpMouseHandler, {passive: true});
            })()
            """,
            return_by_value=True,
        )
        await asyncio.sleep(duration_seconds)
        data = await tab.evaluate(
            """
            (() => {
              document.removeEventListener('mousemove', window.__mcpMouseHandler);
              const out = window.__mcpMouseRec || [];
              delete window.__mcpMouseRec;
              delete window.__mcpMouseHandler;
              return JSON.stringify(out);
            })()
            """,
            return_by_value=True,
        )
        return ok(str(data))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_replay(path_json: str, speed: float = 1.0) -> str:
    """⭐ Replay a recorded mouse path (from mouse_record).

    Args:
        path_json: JSON array of {t, x, y} from mouse_record
        speed: 1.0 = original speed, 2.0 = 2x faster, 0.5 = slower
    """
    try:
        tab = BrowserState.active_tab()
        events = parse_json(path_json, [])
        if not isinstance(events, list) or not events:
            return err("empty/invalid path")
        prev_t = 0
        for ev in events:
            t = ev.get("t", prev_t)
            dt = max(0, (t - prev_t) / 1000.0 / speed)
            await asyncio.sleep(dt)
            await tab.mouse_move(int(ev.get("x", 0)), int(ev.get("y", 0)))
            prev_t = t
        return ok(f"replayed {len(events)} mouse events")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 21. ⭐⭐ AI VISION CAPTCHA SOLVER — moved to tools/vision.py (v0.4.1)
# ══════════════════════════════════════════════════════════════════════════
from .tools import vision as _vision  # noqa: F401  -- registers solve_recaptcha_ai + vision_locate
# Re-export private helpers AND tool callables. Tools must remain in server's
# globals() because workflow_run.dispatch_table looks them up by name there.
from .tools.vision import (  # noqa: F401, E402
    _PROMPT_TEMPLATE, _VISION_LOCATE_PROMPT,
    _parse_tile_indices, _parse_vision_response,
    _claude_vision_pick_tiles, _openai_compat_vision_pick_tiles,
    _resolve_vision_provider,
    solve_recaptcha_ai, vision_locate,
)


# ══════════════════════════════════════════════════════════════════════════
# 22. ⭐⭐⭐ DUAL-MODE HTTP (curl_cffi TLS-perfect) + BEHAVIORAL
# ══════════════════════════════════════════════════════════════════════════
#
# UNIQUE in MCP ecosystem — combine browser (for login/rendering) with
# curl_cffi (for high-volume API scraping with real browser JA3/JA4 fingerprint).
#
# Use case: login via browser → save cookies → scrape hundreds of URLs via
# curl_cffi 10× faster, same stealth level as real Chrome.


_http_session_state: dict[str, Any] = {"cookies": [], "last_origin": None}


async def _get_browser_cookies_for_url(url: str) -> list[dict]:
    """Extract cookies from browser that apply to URL."""
    if not BrowserState.browser:
        return []
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        target_host = parsed.hostname or ""
        all_cookies = await BrowserState.browser.cookies.get_all()
        matching = []
        for c in all_cookies:
            domain = (c.domain or "").lstrip(".")
            if target_host == domain or target_host.endswith("." + domain):
                matching.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                })
        return matching
    except Exception:
        return []


@mcp.tool()
async def http_request(
    url: str,
    method: str = "GET",
    impersonate: str = "chrome",
    use_browser_cookies: bool = True,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[str] = None,
    json_body: Optional[dict] = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    return_mode: str = "auto",
) -> str:
    """HTTP request with TLS-perfect browser fingerprint via curl_cffi.
    Use for API scraping after browser login — same stealth as real Chrome's JA3/JA4.

    Args:
        url, method: target URL and HTTP verb
        impersonate: chrome, chrome124, firefox, safari, edge (default chrome)
        use_browser_cookies: auto-inject cookies from active browser tab
        headers, params: extra headers/query params
        data: raw body string (form-urlencoded or custom)
        json_body: JSON body dict (sets Content-Type automatically)
        timeout, follow_redirects: usual HTTP options
        return_mode: auto (json if parseable else text), json, text, or meta (status+headers only)
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return err("curl-cffi not installed — pip install curl-cffi")

    cookies_dict = {}
    if use_browser_cookies:
        browser_cookies = await _get_browser_cookies_for_url(url)
        for c in browser_cookies:
            cookies_dict[c["name"]] = c["value"]

    hdrs = dict(headers or {})

    try:
        async with AsyncSession(impersonate=impersonate) as session:
            kwargs = {
                "timeout": timeout,
                "allow_redirects": follow_redirects,
            }
            if params:
                kwargs["params"] = params
            if hdrs:
                kwargs["headers"] = hdrs
            if cookies_dict:
                kwargs["cookies"] = cookies_dict
            if data is not None:
                kwargs["data"] = data
            if json_body is not None:
                kwargs["json"] = json_body

            resp = await session.request(method.upper(), url, **kwargs)

            body_text = ""
            if return_mode != "meta":
                try:
                    body_text = resp.text
                except Exception:
                    body_text = "<binary>"
                if len(body_text) > 20_000:
                    body_text = body_text[:20_000] + f"\n\n[truncated — full size {len(resp.content)} bytes]"

            elapsed_ms = None
            if hasattr(resp, "elapsed") and resp.elapsed is not None:
                try:
                    # curl_cffi returns float seconds OR timedelta depending on version
                    e = resp.elapsed
                    elapsed_ms = int(e.total_seconds() * 1000) if hasattr(e, "total_seconds") else int(float(e) * 1000)
                except Exception:
                    elapsed_ms = None
            meta = {
                "status": resp.status_code,
                "url": str(resp.url),
                "elapsed_ms": elapsed_ms,
                "headers": dict(resp.headers),
                "cookies_sent": len(cookies_dict),
                "impersonate": impersonate,
            }

            if return_mode == "json":
                try:
                    return ok(json.dumps({"meta": meta, "body": resp.json()}, indent=2, default=str)[:25000])
                except Exception:
                    return ok(json.dumps({"meta": meta, "body_text": body_text}, indent=2, default=str)[:25000])
            if return_mode == "text":
                return ok(f"{json.dumps(meta, indent=2, default=str)}\n\n--- BODY ---\n{body_text}")
            if return_mode == "meta":
                return ok(json.dumps(meta, indent=2, default=str))
            # auto mode
            try:
                parsed = resp.json()
                return ok(json.dumps({"meta": meta, "body": parsed}, indent=2, default=str)[:25000])
            except Exception:
                return ok(f"{json.dumps(meta, indent=2, default=str)}\n\n--- BODY (text) ---\n{body_text}")
    except Exception as e:
        return err(f"http_request: {e}")


@mcp.tool()
async def http_session_cookies(url: str) -> str:
    """⭐ Inspect which browser cookies would be sent with a request to URL.

    Helpful to verify session sharing works before making requests.
    """
    cookies = await _get_browser_cookies_for_url(url)
    return ok(json.dumps({
        "url": url,
        "count": len(cookies),
        "cookies": [{"name": c["name"], "domain": c["domain"], "path": c["path"]} for c in cookies],
    }, indent=2))


@mcp.tool()
async def session_warmup(
    target_url: str,
    pattern: Literal["homepage_first", "referer_chain", "natural_browse"] = "homepage_first",
    dwell_seconds: float = 2.0,
) -> str:
    """Warm up session by navigating naturally before hitting target URL.
    Anti-bot systems score trust by session history — direct deep-URL hits look suspicious.

    Patterns:
      - homepage_first: goto origin → wait → goto target
      - referer_chain: goto origin → find link to target → click
      - natural_browse: homepage → scroll → random click → scroll → target
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        tab = BrowserState.active_tab()
        actions = []

        if pattern == "homepage_first":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds)
            # mouse drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=3)
            actions.append("mouse drifted")
            await tab.get(target_url)
            actions.append(f"navigated to {target_url}")

        elif pattern == "referer_chain":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds)
            # find link that leads closer to target
            link_data = await tab.evaluate(
                f"""
                (() => {{
                  const t = {json.dumps(target_url)};
                  const links = Array.from(document.querySelectorAll('a[href]'));
                  const hit = links.find(a => t.startsWith(a.href) || a.href.includes(new URL(t).pathname.split('/')[1] || ''));
                  if (!hit) return null;
                  const r = hit.getBoundingClientRect();
                  return JSON.stringify({{
                    href: hit.href,
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    visible: r.width > 0 && r.height > 0,
                  }});
                }})()
                """,
                return_by_value=True,
            )
            ldata = parse_json(link_data, None)
            if isinstance(ldata, dict) and ldata.get("visible"):
                await humanized_move(tab, ldata["x"] + 100, ldata["y"] - 50,
                                      ldata["x"], ldata["y"])
                await tab.mouse_click(ldata["x"], ldata["y"])
                actions.append(f"clicked link to {ldata['href']}")
                await asyncio.sleep(dwell_seconds)
                # if still not at target, nav directly
                cur = await get_url(tab)
                if target_url not in cur:
                    await tab.get(target_url)
                    actions.append(f"direct nav to {target_url}")
            else:
                await tab.get(target_url)
                actions.append(f"no link found, direct nav to {target_url}")

        elif pattern == "natural_browse":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds / 2)
            # scroll
            await tab.evaluate("window.scrollBy(0, 400)", return_by_value=True)
            actions.append("scrolled 400px")
            await asyncio.sleep(dwell_seconds / 2)
            # drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=4)
            actions.append("mouse drifted")
            # random visible link click
            rand_link = await tab.evaluate(
                """
                (() => {
                  const links = Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                      const r = a.getBoundingClientRect();
                      return r.width > 30 && r.height > 10 && a.href.startsWith(location.origin);
                    });
                  if (links.length === 0) return null;
                  const pick = links[Math.floor(Math.random() * Math.min(links.length, 5))];
                  const r = pick.getBoundingClientRect();
                  return JSON.stringify({href: pick.href, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)});
                })()
                """,
                return_by_value=True,
            )
            rdata = parse_json(rand_link, None)
            if isinstance(rdata, dict):
                await humanized_move(tab, rdata["x"] + 150, rdata["y"] - 60,
                                      rdata["x"], rdata["y"])
                await tab.mouse_click(rdata["x"], rdata["y"])
                actions.append(f"random click → {rdata['href']}")
                await asyncio.sleep(dwell_seconds)
                await tab.evaluate("window.scrollBy(0, 300)", return_by_value=True)
                actions.append("scrolled on intermediate page")
                await asyncio.sleep(dwell_seconds / 2)
            await tab.get(target_url)
            actions.append(f"final nav to {target_url}")

        return ok("session warmup complete:\n  " + "\n  ".join(actions))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def detect_anti_bot() -> str:
    """⭐ Analyze current page + HTTP headers to identify anti-bot system.

    Detects: Cloudflare, DataDome, PerimeterX/HUMAN, Akamai Bot Manager,
    Kasada, Imperva/Incapsula, F5 Shape, none. Returns system + recommended
    bypass strategy from our toolkit.
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        tab = BrowserState.active_tab()

        # JS probes — look for telltale script names, objects, cookies
        probes = await tab.evaluate(
            """
            (() => {
              const out = {};
              // Cookies (accessible from JS if not HttpOnly)
              out.cookies = document.cookie;
              // Page HTML signature
              out.html_head = document.documentElement.outerHTML.slice(0, 5000);
              // Known globals
              out.has_turnstile = !!window.turnstile;
              out.has_grecaptcha = !!window.grecaptcha;
              out.has_hcaptcha = !!window.hcaptcha;
              out.has_px = !!window._pxAppId || !!window._pxCID;
              out.has_kasada = !!window.KPSDK;
              out.has_imperva = !!window._impervasecure;
              return JSON.stringify(out);
            })()
            """,
            return_by_value=True,
        )
        data = parse_json(probes, {})
        cookies = str(data.get("cookies", ""))
        html = str(data.get("html_head", ""))

        detections = []
        strategies = []

        # Cloudflare signatures
        if ("__cf_bm" in cookies or "cf_clearance" in cookies or
            "cdn-cgi" in html or "challenges.cloudflare.com" in html or
            "cf-beacon" in html or data.get("has_turnstile")):
            detections.append("Cloudflare")
            strategies.append("click_turnstile() or verify_cf() for challenges")
            strategies.append("http_request(impersonate='chrome') for API calls")

        # DataDome
        if "datadome" in cookies.lower() or "dd_s" in cookies or "datadome" in html.lower():
            detections.append("DataDome")
            strategies.append("⚠️ DataDome is HARDEST — use mouse_drift + session_warmup + residential proxy")
            strategies.append("mouse_record + mouse_replay of real human session")

        # PerimeterX / HUMAN
        if (data.get("has_px") or "_px" in cookies or "perimeterx" in html.lower() or
            "_pxhd" in cookies):
            detections.append("PerimeterX/HUMAN")
            strategies.append("storage_state_load (session reuse) is most reliable")
            strategies.append("mouse_behavior_profile + mobile proxy for new sessions")

        # Akamai Bot Manager
        if ("_abck" in cookies or "bm_sz" in cookies or "akamai" in html.lower() or
            "ak-bm-api" in html):
            detections.append("Akamai Bot Manager")
            strategies.append("http_request with impersonate='chrome' for TLS match")
            strategies.append("session_warmup(pattern='natural_browse')")

        # Kasada
        if data.get("has_kasada") or "kpsdk" in html.lower() or "kasada" in html.lower():
            detections.append("Kasada")
            strategies.append("⚠️ Kasada is VERY HARD — requires residential proxy + real browser")
            strategies.append("consider CapSolver or commercial service for this target")

        # Imperva / Incapsula
        if data.get("has_imperva") or "incap_ses" in cookies or "visid_incap" in cookies:
            detections.append("Imperva/Incapsula")
            strategies.append("http_request + cookie persistence after warmup")

        # reCAPTCHA / hCaptcha presence
        if data.get("has_grecaptcha"):
            detections.append("reCAPTCHA (v2 or v3)")
            strategies.append("solve_recaptcha_ai() (Claude vision) or solve_captcha(kind='recaptcha_v2')")
        if data.get("has_hcaptcha"):
            detections.append("hCaptcha")
            strategies.append("solve_captcha(kind='hcaptcha', ...) via CapSolver")

        if not detections:
            detections.append("none detected")
            strategies.append("proceed with normal automation — site has no/low anti-bot")

        return ok(json.dumps({
            "detected": detections,
            "recommended_tools": strategies,
            "cookies_found": [c.split("=")[0].strip() for c in cookies.split(";") if "=" in c][:20],
        }, indent=2))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 23. ⭐ MULTI-INSTANCE BROWSER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════
#
# Run multiple isolated browsers in parallel — one per account, per site, or
# per worker. Each instance has its own profile, tabs, cookies, and logs.
# Idle instances auto-close after configurable timeout (prevents memory leaks).


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
) -> tuple[bool, str]:
    """Shared launcher for browser_launch + spawn_browser.

    Returns (success, message).
    """
    # Pre-flight: ensure Chrome is installed before calling nodriver
    chrome_path = find_chrome_binary()
    if chrome_path is None:
        return False, (
            "Chrome/Chromium not found on this system.\n"
            + chrome_install_hint()
            + "\n\nAfter installing, re-launch the MCP server."
        )

    ensure_dirs()

    # Determine profile dir per instance
    if profile_dir_override:
        profile_path = Path(profile_dir_override)
    elif instance_id == "main":
        profile_path = PROFILE_DIR
    else:
        profile_path = PROFILES_ROOT / instance_id
    profile_path.mkdir(parents=True, exist_ok=True)

    if persistent:
        clean_profile_state(profile_path)

    config = Config(
        user_data_dir=str(profile_path) if persistent else None,
        headless=headless,
        lang=lang,
        browser_args=list(extra_args or []),
    )
    config.add_argument("--hide-crash-restore-bubble")
    config.add_argument("--disable-session-crashed-bubble")
    config.add_argument("--disable-restore-session-state")
    config.add_argument("--no-default-browser-check")
    if user_agent:
        config.add_argument(f"--user-agent={user_agent}")
    if proxy:
        config.add_argument(f"--proxy-server={proxy}")
    config.add_argument(f"--window-size={window_width},{window_height}")

    browser: Optional[Browser] = None
    try:
        browser = await asyncio.wait_for(
            nodriver.start(config=config), timeout=BROWSER_LAUNCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        await _safe_stop_browser(browser)
        return False, (
            f"launch timed out after {BROWSER_LAUNCH_TIMEOUT}s — profile "
            f"{profile_path} may be locked or Chrome is hung."
        )
    except asyncio.CancelledError:
        await _safe_stop_browser(browser)
        raise
    except Exception as e:
        await _safe_stop_browser(browser)
        return False, f"launch failed: {e}"

    if storage_state_path:
        try:
            await _apply_storage_state(browser, storage_state_path)
        except asyncio.CancelledError:
            await _safe_stop_browser(browser)
            raise
        except Exception as e:
            await _safe_stop_browser(browser)
            return False, f"storage_state load failed: {e}"

    try:
        await asyncio.sleep(0.5)
        main_tab = browser.main_tab
        if main_tab is None:
            await browser.update_targets()
            main_tab = browser.tabs[0] if browser.tabs else None
        if main_tab is None:
            main_tab = await asyncio.wait_for(browser.get(url), timeout=BROWSER_NAV_TIMEOUT)
        else:
            await asyncio.wait_for(main_tab.get(url), timeout=BROWSER_NAV_TIMEOUT)
        try:
            await asyncio.wait_for(main_tab.wait(t=3), timeout=BROWSER_NAV_TIMEOUT)
        except asyncio.TimeoutError:
            pass
    except asyncio.TimeoutError:
        await _safe_stop_browser(browser)
        return False, (
            f"initial nav timed out after {BROWSER_NAV_TIMEOUT}s on instance "
            f"{instance_id!r}."
        )
    except asyncio.CancelledError:
        await _safe_stop_browser(browser)
        raise
    except Exception as e:
        await _safe_stop_browser(browser)
        return False, f"initial nav failed: {e}"

    # Write into the target instance slot
    if instance_id == BrowserState.current_instance_id:
        BrowserState.browser = browser
        BrowserState.tabs = [main_tab]
        BrowserState.active_tab_index = 0
        BrowserState.current_profile_dir = profile_path
        BrowserState.current_idle_timeout = idle_timeout
        BrowserState.current_last_active = time.time()
        BrowserState.current_created_at = time.time()
    else:
        # Store as snapshot without becoming current
        snap = InstanceSnapshot(
            instance_id=instance_id,
            browser=browser,
            tabs=[main_tab],
            active_tab_index=0,
            profile_dir=profile_path,
            idle_timeout=idle_timeout,
            last_active=time.time(),
            created_at=time.time(),
        )
        BrowserState.instances[instance_id] = snap

    # Kick off the idle reaper (once)
    _ensure_idle_reaper_running()
    return True, f"instance {instance_id!r} launched (headless={headless}, profile={profile_path.name})"


async def _idle_reaper_loop() -> None:
    """Close instances that have been idle past their timeout."""
    while True:
        try:
            await asyncio.sleep(IDLE_REAPER_INTERVAL)
            # Check stored instances
            to_close = []
            for iid, snap in list(BrowserState.instances.items()):
                if snap.is_running() and snap.is_idle_expired():
                    to_close.append((iid, snap))
            for iid, snap in to_close:
                try:
                    if snap.browser:
                        snap.browser.stop()
                except Exception:
                    pass
                BrowserState.instances.pop(iid, None)
            # Check current instance
            if (BrowserState.is_up()
                and BrowserState.current_idle_timeout > 0
                and (time.time() - BrowserState.current_last_active) > BrowserState.current_idle_timeout):
                try:
                    if BrowserState.browser:
                        BrowserState.browser.stop()
                except Exception:
                    pass
                BrowserState.reset()
        except asyncio.CancelledError:
            return
        except Exception:
            # Don't let reaper crash
            continue


def _ensure_idle_reaper_running() -> None:
    """Start the reaper task once, lazily."""
    if BrowserState._reaper_task is not None and not BrowserState._reaper_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        BrowserState._reaper_task = loop.create_task(_idle_reaper_loop())
    except RuntimeError:
        pass  # no event loop yet — will try again on next launch


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
) -> str:
    """Create a new named browser instance running in parallel with main.
    Each instance has its own profile, cookies, tabs, logs. Use for multi-account
    scraping or isolated sessions.

    Args:
        instance_id: unique name (e.g., "scraper_1", "acct_alice")
        idle_timeout_seconds: auto-close after idle (0 = never, default 600s)
        profile_dir: override profile path (default: ~/.mcp-stealth/profiles/<id>/)
        other args: same as browser_launch

    Use switch_instance(id) to route subsequent tool calls to this instance.
    """
    if instance_id == BrowserState.current_instance_id and BrowserState.is_up():
        return err(f"instance {instance_id!r} already running (current). Use switch_instance instead.")
    if instance_id in BrowserState.instances and BrowserState.instances[instance_id].is_running():
        return err(f"instance {instance_id!r} already running.")
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
    )
    if not ok_flag:
        return err(msg)
    return ok(msg)


@mcp.tool()
async def list_instances() -> str:
    """⭐ List all browser instances with status + last-active time."""
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
            "uptime_seconds": int(now - s.created_at) if s.is_running() else 0,
        })
    return ok(json.dumps(out, indent=2, default=str))


@mcp.tool()
async def switch_instance(instance_id: str) -> str:
    """⭐ Make instance_id the active one for subsequent tool calls.

    The previous current instance continues running in the background,
    cookies/tabs preserved. Swap back anytime.
    """
    if instance_id == BrowserState.current_instance_id:
        return ok(f"already on {instance_id!r}")
    existed = instance_id in BrowserState.instances
    BrowserState.switch_to(instance_id)
    if not existed and not BrowserState.is_up():
        return ok(f"switched to {instance_id!r} (not yet running — call spawn_browser or browser_launch)")
    return ok(f"switched to {instance_id!r}")


@mcp.tool()
async def close_instance(instance_id: str) -> str:
    """⭐ Close a specific browser instance (frees profile + memory)."""
    # Close current
    if instance_id == BrowserState.current_instance_id:
        if BrowserState.is_up():
            try:
                if BrowserState.browser:
                    BrowserState.browser.stop()
            except Exception as e:
                return err(f"close failed: {e}")
            BrowserState.reset()
            if BrowserState.current_profile_dir:
                clean_profile_state(BrowserState.current_profile_dir)
            return ok(f"closed current instance {instance_id!r}")
        return ok(f"current instance {instance_id!r} was not running")
    # Close stored
    snap = BrowserState.instances.get(instance_id)
    if snap is None:
        return err(f"instance {instance_id!r} not found")
    try:
        if snap.browser:
            snap.browser.stop()
    except Exception:
        pass
    if snap.profile_dir:
        clean_profile_state(snap.profile_dir)
    BrowserState.instances.pop(instance_id, None)
    return ok(f"closed instance {instance_id!r}")


@mcp.tool()
async def close_all_instances() -> str:
    """⭐ Close every running browser instance. Useful for cleanup."""
    closed = []
    # Close stored
    for iid, snap in list(BrowserState.instances.items()):
        try:
            if snap.browser:
                snap.browser.stop()
        except Exception:
            pass
        if snap.profile_dir:
            clean_profile_state(snap.profile_dir)
        closed.append(iid)
    BrowserState.instances.clear()
    # Close current
    if BrowserState.is_up():
        try:
            if BrowserState.browser:
                BrowserState.browser.stop()
        except Exception:
            pass
        if BrowserState.current_profile_dir:
            clean_profile_state(BrowserState.current_profile_dir)
        closed.append(BrowserState.current_instance_id)
    BrowserState.reset()
    return ok(f"closed {len(closed)} instance(s): {closed}")


# ══════════════════════════════════════════════════════════════════════════
# 24. ⭐ CHROME PROFILE INTEGRATION (list / clone existing profiles)
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
async def list_chrome_profiles() -> str:
    """List all Chrome/Chromium/Edge/Brave profiles found on this system.

    Reads browser 'Local State' JSON (read-only). Returns profile name, user email,
    path, whether in-use (Chrome currently running on it), and whether it exists.
    """
    root = chrome_user_data_root()
    if root is None:
        return err(
            "No Chrome-family browser profile directory found. "
            "Install Chrome/Chromium/Edge/Brave and launch it once to create profiles."
        )
    local_state = root / "Local State"
    try:
        data = json.loads(local_state.read_text())
    except Exception as e:
        return err(f"failed to parse Local State at {local_state}: {e}")

    info_cache = data.get("profile", {}).get("info_cache", {})
    profiles_order = data.get("profile", {}).get("profiles_order", [])
    seen = set(profiles_order)
    # Ensure we also include profiles not in profiles_order
    for k in info_cache.keys():
        if k not in seen:
            profiles_order.append(k)

    out = []
    for name in profiles_order:
        info = info_cache.get(name, {})
        pdir = root / name
        out.append({
            "profile_dir_name": name,
            "display_name": info.get("name", name),
            "email": info.get("user_name", ""),
            "path": str(pdir),
            "exists": pdir.exists(),
            "in_use": is_chrome_profile_locked(pdir) if pdir.exists() else False,
            "last_active_time": info.get("last_active_time", 0),
        })
    return ok(json.dumps({
        "browser_root": str(root),
        "browser_running": is_chrome_profile_locked(root),
        "profile_count": len(out),
        "profiles": out,
        "usage_hint": (
            "clone_chrome_profile(source_profile='Default', target_instance_id='my_clone') "
            "→ then spawn_browser(instance_id='my_clone') to use it"
        ),
    }, indent=2, default=str))


@mcp.tool()
async def clone_chrome_profile(
    source_profile: str = "Default",
    target_instance_id: str = "chrome_clone",
    skip_cache: bool = True,
    overwrite: bool = False,
) -> str:
    """Clone an existing Chrome profile into isolated mcp-stealth location.

    SAFE: reads source profile without modification, copies to
    ~/.mcp-stealth/profiles/<target_instance_id>/Default/

    Chrome desktop MUST be closed for source profile (we check SingletonLock).
    Preserves: cookies, history, bookmarks, saved passwords, extensions state.
    Skips (if skip_cache=True): Cache, Code Cache, GPUCache, Media Cache,
    Service Worker, IndexedDB (regenerable, saves 500MB+).

    Args:
        source_profile: Chrome profile dir name ("Default", "Profile 1", etc).
                        Use list_chrome_profiles() to see options.
        target_instance_id: Name for the cloned instance (becomes folder name).
        skip_cache: Exclude cache dirs for fast + smaller copy (default True).
        overwrite: Delete target if exists before copying (default False).

    After clone, launch with:
        spawn_browser(instance_id='<target_instance_id>')
    """
    import shutil

    root = chrome_user_data_root()
    if root is None:
        return err("No Chrome profile root found. Install Chrome first.")
    source_path = root / source_profile
    if not source_path.exists():
        return err(
            f"Source profile not found: {source_path}\n"
            f"Run list_chrome_profiles() to see available profiles."
        )
    if is_chrome_profile_locked(source_path) or is_chrome_profile_locked(root):
        return err(
            f"Chrome is currently using this profile (lock file present). "
            f"Close Chrome desktop FULLY (Cmd+Q on macOS, not just window close), "
            f"then retry.\n"
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

    for item in source_path.iterdir():
        name_lower = item.name.lower()
        try:
            if item.is_file():
                if skip_cache and name_lower in skip_files:
                    continue
                # Also skip -journal WAL sidecars
                if name_lower.endswith("-journal"):
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
        "source": str(source_path),
        "target": str(target_root),
        "copied_items": copied_count,
        "target_size_mb": round(total_size / 1024 / 1024, 1),
        "cache_skipped_mb": round(skipped_cache_bytes / 1024 / 1024, 1),
        "errors": errors[:10],  # cap error list
        "next_step": (
            f"spawn_browser(instance_id='{target_instance_id}', "
            f"url='https://example.com', headless=False)"
        ),
    }
    return ok(json.dumps(result, indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════
# 24. ⭐ DEVTOOLS & TESTING — moved to tools/devtools.py (v0.4.1 refactor)
# ══════════════════════════════════════════════════════════════════════════
from .tools import devtools as _devtools  # noqa: F401  -- registers tools with mcp


# ══════════════════════════════════════════════════════════════════════════
# 25. ⭐⭐⭐ LLM-OPTIMIZED ACTION KIT
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
    """⭐ Compact AI-friendly page summary — replaces accessibility_snapshot
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
      // Tag input with marker so we can re-query it server-side without
      // sending the element across CDP boundary.
      const marker = '__mcp_smart_' + Math.random().toString(36).slice(2, 10);
      el.setAttribute('data-mcp-smart', marker);
      const rect = el.getBoundingClientRect();
      scored.push({score, label: lbl, marker, type: el.tagName.toLowerCase(),
        input_type: (el.type || '').toLowerCase(),
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2)});
    }
  }
  scored.sort((a, b) => b.score - a.score);
  return JSON.stringify({best: scored[0] || null, candidates: [...new Set(candidates)].filter(Boolean).slice(0, 30)});
}
"""


@mcp.tool()
async def smart_fill(fields: dict, submit_label: Optional[str] = None) -> str:
    """⭐ Fill form fields by label text (fuzzy match). LLM-friendly alt to
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
        results: list[dict] = []
        for label, value in fields.items():
            if not isinstance(label, str):
                continue
            raw = await _wait(
                tab.evaluate(
                    f"({_SMART_FILL_FIND_JS})({json.dumps(label)})",
                    return_by_value=True,
                ),
                what=f"smart_fill find '{label}'",
            )
            text = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
            data = parse_json(str(text), {})
            best = data.get("best") if isinstance(data, dict) else None
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            if not best:
                results.append({
                    "label": label, "ok": False,
                    "error": "no field matched",
                    "did_you_mean": candidates[:10],
                })
                continue
            # Click into the field then type. Using mouse_click + JS fill is
            # more reliable than .focus() across React/Vue components that
            # listen for native events.
            try:
                await tab.mouse_click(int(best["x"]), int(best["y"]))
                await asyncio.sleep(0.1)
                # Native value setter so React/Vue see the change
                await _wait(tab.evaluate(
                    f"""
                    (() => {{
                      const el = document.querySelector('[data-mcp-smart="{best['marker']}"]');
                      if (!el) return false;
                      const setter = Object.getOwnPropertyDescriptor(
                        el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
                        'value'
                      ).set;
                      setter.call(el, {json.dumps(str(value))});
                      el.dispatchEvent(new Event('input', {{bubbles: true}}));
                      el.dispatchEvent(new Event('change', {{bubbles: true}}));
                      return true;
                    }})()
                    """,
                    return_by_value=True,
                ), what=f"smart_fill set '{label}'")
                results.append({"label": label, "matched": best["label"], "ok": True})
            except Exception as e:
                results.append({"label": label, "ok": False, "error": str(e)})
        # Optional submit
        submit_result = None
        if submit_label:
            try:
                # Reuse click_text fuzzy logic
                r = await click_text(submit_label, exact=False)
                submit_result = str(r)[:200]
            except Exception as e:
                submit_result = f"submit failed: {e}"
        out = {"results": results}
        if submit_result is not None:
            out["submit"] = submit_result
        any_failed = any(not r.get("ok") for r in results)
        return (err if any_failed else ok)(json.dumps(out, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── assert_* primitives ────────────────────────────────────────────────────


@mcp.tool()
async def assert_text_present(text: str, timeout: float = 5.0) -> str:
    """⭐ Verify text appears anywhere on page within timeout.
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
    """⭐ Verify current URL matches regex within timeout.
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
    """⭐ Verify an element is visible (rendered, non-zero size, not hidden).
    Pass selector OR text — text uses fuzzy contains match.
    Returns ok(rect) or err(timeout)."""
    if not selector and not text:
        return err("pass selector= or text=")
    try:
        tab = BrowserState.active_tab()
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
_STORAGE_SNAPSHOTS: dict[str, dict] = {}


async def _take_storage_snapshot(tab) -> dict:
    """Capture cookies + localStorage + sessionStorage + url + title."""
    snap: dict = {"url": "", "title": "", "cookies": [],
                   "localStorage": {}, "sessionStorage": {}}
    try:
        snap["url"] = await get_url(tab)
        snap["title"] = await get_title(tab)
    except Exception:
        pass
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
    except Exception:
        pass
    try:
        ls_raw = await _wait(tab.evaluate(
            "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))",
            return_by_value=True,
        ), what="storage_snapshot localStorage")
        snap["localStorage"] = parse_json(
            str(ls_raw.value if hasattr(ls_raw, "value") else ls_raw), {}) or {}
    except Exception:
        pass
    try:
        ss_raw = await _wait(tab.evaluate(
            "JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))",
            return_by_value=True,
        ), what="storage_snapshot sessionStorage")
        snap["sessionStorage"] = parse_json(
            str(ss_raw.value if hasattr(ss_raw, "value") else ss_raw), {}) or {}
    except Exception:
        pass
    return snap


@mcp.tool()
async def storage_snapshot(name: str = "default") -> str:
    """⭐ Capture cookies + localStorage + sessionStorage + URL into named slot
    for later diffing. Use BEFORE an action you want to inspect."""
    try:
        tab = BrowserState.active_tab()
        snap = await _take_storage_snapshot(tab)
        _STORAGE_SNAPSHOTS[name] = snap
        return ok(
            f"snapshot '{name}': {len(snap['cookies'])} cookies, "
            f"{len(snap['localStorage'])} localStorage, "
            f"{len(snap['sessionStorage'])} sessionStorage at {snap['url']}"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def storage_diff(name: str = "default") -> str:
    """⭐ Compare current state vs an earlier storage_snapshot. Returns JSON
    showing what changed (added/removed/modified per area + url change).

    Pattern:
      storage_snapshot("before")
      <do an action e.g. click login>
      storage_diff("before")  → returns what the action actually changed
    """
    try:
        if name not in _STORAGE_SNAPSHOTS:
            return err(
                f"no snapshot named {name!r}. Available: "
                f"{list(_STORAGE_SNAPSHOTS.keys()) or '(none)'}"
            )
        tab = BrowserState.active_tab()
        before = _STORAGE_SNAPSHOTS[name]
        after = await _take_storage_snapshot(tab)

        def _kvdiff(b: dict, a: dict) -> dict:
            keys_b, keys_a = set(b), set(a)
            return {
                "added": {k: a[k][:200] if isinstance(a[k], str) else a[k]
                           for k in keys_a - keys_b},
                "removed": list(keys_b - keys_a),
                "modified": {k: {"before": (b[k][:200] if isinstance(b[k], str) else b[k]),
                                  "after": (a[k][:200] if isinstance(a[k], str) else a[k])}
                              for k in keys_b & keys_a if b[k] != a[k]},
            }

        # Cookies — by (name, domain) tuple
        b_cookies = {(c["name"], c["domain"]): c["value"] for c in before["cookies"]}
        a_cookies = {(c["name"], c["domain"]): c["value"] for c in after["cookies"]}
        cookie_diff = {
            "added": [{"name": n, "domain": d, "value": (a_cookies[(n, d)][:60] if a_cookies[(n, d)] else "")}
                       for (n, d) in set(a_cookies) - set(b_cookies)],
            "removed": [{"name": n, "domain": d} for (n, d) in set(b_cookies) - set(a_cookies)],
            "modified": [{"name": n, "domain": d}
                          for (n, d) in set(b_cookies) & set(a_cookies)
                          if b_cookies[(n, d)] != a_cookies[(n, d)]],
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
        return ok(json.dumps(diff, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── workflow_run (resumable) ───────────────────────────────────────────────

# Map of tool-name → function ref for workflow_run dispatch. Only a curated
# subset is exposed (the ones useful for sequencing — read-only inspectors
# like get_text are intentionally excluded since the workflow output already
# captures step results).
_WORKFLOW_TOOLS: dict[str, Any] = {}


def _register_workflow_tools() -> None:
    """Late-bound registration so all tool defs above this point exist.
    Called lazily on first workflow_run call."""
    global _WORKFLOW_TOOLS
    if _WORKFLOW_TOOLS:
        return
    g = globals()
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
    ):
        if name in g and callable(g[name]):
            # FastMCP wraps tools — unwrap to the underlying coroutine fn
            fn = g[name]
            _WORKFLOW_TOOLS[name] = getattr(fn, "fn", fn) if hasattr(fn, "fn") else fn


@mcp.tool()
async def workflow_run(
    steps: list[dict],
    start_at: int = 0,
    stop_on_error: bool = True,
) -> str:
    """⭐ Execute a list of tool steps sequentially. Resumable — pass
    start_at=N to skip the first N steps.

    Each step: {"tool": "<name>", "args": {...}, "label": "optional"}

    Args:
        steps: list of step dicts
        start_at: index to begin from (for resume after a fix)
        stop_on_error: abort on first failure (default True). If False,
            continue and collect all results.

    Returns JSON:
      {
        "completed": [
          {"index": 0, "tool": "navigate", "ok": true, "result": "..."},
          ...
        ],
        "failed_at": 3,                # index of failure (omitted on success)
        "failure_context": {...},      # last step's input + error (for LLM debug)
        "resume_with": "workflow_run(steps=..., start_at=4)"  # hint
      }

    Allowed tools (curated for sequencing): navigate, reload, go_back/forward,
    click, click_text, click_role, fill, type_text, press_key, select_option,
    check, uncheck, wait_for*, screenshot, scroll, scroll_to, smart_fill,
    vision_locate, assert_*, storage_*, cookie_import, storage_state_load,
    evaluate, mouse_click_xy.
    """
    _register_workflow_tools()
    if not isinstance(steps, list):
        return err("steps must be a list of dicts")
    completed: list[dict] = []
    for i in range(start_at, len(steps)):
        step = steps[i]
        if not isinstance(step, dict) or "tool" not in step:
            entry = {"index": i, "ok": False, "error": "step must have 'tool' key"}
            completed.append(entry)
            if stop_on_error:
                return err(json.dumps({
                    "completed": completed, "failed_at": i,
                    "failure_context": {"step": step, "error": "malformed"},
                    "resume_with": f"workflow_run(steps=..., start_at={i + 1})",
                }, indent=2, ensure_ascii=False))
            continue
        tool_name = step["tool"]
        args = step.get("args", {}) or {}
        label = step.get("label", "")
        if tool_name not in _WORKFLOW_TOOLS:
            entry = {"index": i, "tool": tool_name, "ok": False,
                     "error": f"unknown or non-allowlisted tool: {tool_name}",
                     "available": sorted(_WORKFLOW_TOOLS.keys())[:20]}
            completed.append(entry)
            if stop_on_error:
                return err(json.dumps({
                    "completed": completed, "failed_at": i,
                    "failure_context": entry,
                    "resume_with": f"workflow_run(steps=..., start_at={i + 1})",
                }, indent=2, ensure_ascii=False))
            continue
        try:
            fn = _WORKFLOW_TOOLS[tool_name]
            if not isinstance(args, dict):
                raise ValueError(f"args must be a dict, got {type(args).__name__}")
            res = await fn(**args)
            res_str = str(res)
            is_err = res_str.startswith("Error:")
            entry = {"index": i, "tool": tool_name, "label": label,
                     "ok": not is_err, "result": res_str[:500]}
            completed.append(entry)
            if is_err and stop_on_error:
                return err(json.dumps({
                    "completed": completed, "failed_at": i,
                    "failure_context": {"step": step, "result": res_str},
                    "resume_with": f"workflow_run(steps=..., start_at={i})",
                    "hint": "fix the underlying issue then re-run with start_at unchanged "
                            "(retry same step) or start_at+1 (skip).",
                }, indent=2, ensure_ascii=False))
        except Exception as e:
            entry = {"index": i, "tool": tool_name, "label": label,
                     "ok": False, "error": str(e)}
            completed.append(entry)
            if stop_on_error:
                return err(json.dumps({
                    "completed": completed, "failed_at": i,
                    "failure_context": {"step": step, "error": str(e)},
                    "resume_with": f"workflow_run(steps=..., start_at={i})",
                }, indent=2, ensure_ascii=False))
    return ok(json.dumps({"completed": completed, "total": len(steps)},
                          indent=2, ensure_ascii=False))


# ── detect_and_bypass ──────────────────────────────────────────────────────


@mcp.tool()
async def detect_and_bypass() -> str:
    """⭐ One-shot: detect anti-bot wall on current page and apply the best
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
            if verify and "auto-verify" in verify:
                result["bypassed"] = True
                result["method"] = "auto_verify_cf"
                result["details"] = verify.strip()
            else:
                result["next_steps"].append(
                    "If still blocked: solve_captcha(kind='turnstile', ...) with CAPSOLVER_KEY, "
                    "or storage_state_load a previously-saved session."
                )
        if "datadome" in det_lower:
            result["next_steps"].append(
                "DataDome: no automated bypass. Use mouse_record→mouse_replay of a real session, "
                "session_warmup, residential proxy, and storage_state reuse."
            )
        if "perimeterx" in det_lower or "human" in det_lower:
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
                {"detection": "no anti-bot detected", "bypassed": True,
                 "method": "n/a", "next_steps": []},
                indent=2, ensure_ascii=False,
            ))
        return ok(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 26. ⭐⭐⭐ NETWORK + AUTH + WAIT — paste, click_and_wait, auth_capture,
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
    """⭐ Set field value by simulating a real paste event sequence.

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
        ref: data-mcp-ref from browser_snapshot
        selector: CSS selector
        submit: if True, simulate Enter keypress after paste
    """
    try:
        tab = BrowserState.active_tab()
        target_selector: Optional[str] = None
        if ref:
            el = await resolve_ref(ref)
            if el is None:
                return err(f"ref {ref} not found")
            # Walk up to find a queryable selector via data-mcp-ref attribute
            target_selector = f'[data-mcp-ref="{ref}"]'
        elif selector:
            target_selector = selector
        else:
            return err("paste_text: pass ref= or selector=")

        # Single-shot JS that does the full event dance — keeps timing tight.
        result_raw = await _wait(tab.evaluate(
            f"""
            (() => {{
              const el = document.querySelector({json.dumps(target_selector)});
              if (!el) return JSON.stringify({{ok: false, error: 'element not found'}});
              const text = {json.dumps(text)};
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
              try {{
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
              return JSON.stringify({{ok: true, value: el.value}});
            }})()
            """,
            return_by_value=True,
        ), what="paste_text")
        result_text = result_raw.value if hasattr(result_raw, "value") and not isinstance(result_raw, str) else result_raw
        data = parse_json(str(result_text), {})
        if not isinstance(data, dict) or not data.get("ok"):
            return err(f"paste failed: {data.get('error') if isinstance(data, dict) else result_text}")
        if submit:
            try:
                await tab.send_keys("\r")  # Enter
            except Exception:
                try:
                    await tab.evaluate(
                        f"""document.querySelector({json.dumps(target_selector)}).form?.requestSubmit?.()"""
                    )
                except Exception:
                    pass
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
) -> str:
    """⭐ Intercept the next N requests matching a URL pattern and return
    their headers (Authorization, Cookie, X-CSRF-*, etc.) — useful for
    SPAs that hold bearer tokens in JS memory and never write them to
    localStorage.

    Pattern: substring match on URL (case-sensitive). For regex use
    network_get instead.

    Args:
        filter_url_pattern: e.g. "/api/" or "graphql"
        count: stop capturing after this many matches (default 1)
        timeout: max seconds to wait (default 10)
        include_response_headers: also wait for + return response headers

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
            active["flag"] = False  # disable handlers (handler stays bound but no-ops)
        if not captured:
            return err(
                f"auth_capture: no requests matched {filter_url_pattern!r} within {timeout}s"
            )
        return ok(json.dumps(captured, indent=2, default=str)[:30000])
    except Exception as e:
        return err(str(e))


# ── http_request_with_session ──────────────────────────────────────────────


def _latest_auth_header_for(url: str) -> Optional[str]:
    """Pick the most recent Authorization header from network_index for
    requests whose URL shares the host of the target. Used by
    http_request_with_session as a fallback when the caller doesn't pass
    headers explicitly."""
    try:
        from urllib.parse import urlparse
        target_host = urlparse(url).hostname or ""
        if not target_host:
            return None
        # Iterate in insertion order — index is dict keyed by request_id and
        # Python dicts preserve insertion order, so the last matching entry
        # is the freshest.
        latest = None
        for entry in BrowserState.network_index.values():
            host = urlparse(entry.get("url", "")).hostname or ""
            if host != target_host:
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
) -> str:
    """⭐ Authenticated HTTP request that piggybacks on the BROWSER's session.

    Combines:
      - cookies from active tab (use_browser_cookies=True in http_request)
      - Authorization header — explicit auth_header, OR auto-extracted
        from network_index (the most recent same-host request captured
        via network_start). Pages that hold bearer tokens in JS memory
        only become reachable after navigate / interaction emits a
        request — call network_start once at session begin.

    Args:
        url, method, json_body, data, impersonate, return_mode, timeout:
            same as http_request
        extra_headers: merged on top of auto-detected ones
        auth_header: explicit bearer / basic value, e.g. "Bearer eyJ..."

    Returns same shape as http_request.
    """
    headers = dict(extra_headers or {})
    if auth_header:
        headers.setdefault("Authorization", auth_header)
    elif "Authorization" not in {k.lower() for k in headers}:
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
        return_mode=return_mode,
    )


# ── wait_for_request ───────────────────────────────────────────────────────


@mcp.tool()
async def wait_for_request(
    url_pattern: str,
    method: Optional[str] = None,
    timeout: float = 15.0,
    require_response: bool = True,
) -> str:
    """⭐ Block until a network request matching url_pattern is observed.
    Replaces the setTimeout(2000)+poll anti-pattern.

    Args:
        url_pattern: substring match
        method: optional HTTP verb filter (GET/POST/...)
        timeout: max seconds to wait
        require_response: also wait for the response phase (default True)

    Returns JSON of the matching entry (url/method/status/request_headers/
    response_headers).
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        try:
            await tab.send(cdp_network.enable())
        except Exception:
            pass
        match: dict = {}
        done = asyncio.Event()
        active = {"flag": True}

        async def on_req(event):
            if not active["flag"]:
                return
            try:
                req = event.request
                url = getattr(req, "url", "")
                if url_pattern not in url:
                    return
                if method and getattr(req, "method", "").upper() != method.upper():
                    return
                rid = str(event.request_id)
                match.update({
                    "request_id": rid,
                    "url": url,
                    "method": getattr(req, "method", ""),
                    "request_headers": dict(getattr(req, "headers", None) or {}),
                    "request_body": getattr(req, "post_data", None),
                })
                if not require_response:
                    active["flag"] = False
                    done.set()
            except Exception:
                pass

        async def on_res(event):
            if not active["flag"]:
                return
            try:
                rid = str(event.request_id)
                if match.get("request_id") != rid:
                    return
                resp = event.response
                match["status"] = getattr(resp, "status", None)
                match["response_headers"] = dict(getattr(resp, "headers", None) or {})
                match["mime"] = getattr(resp, "mime_type", None)
                active["flag"] = False
                done.set()
            except Exception:
                pass

        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        if require_response:
            tab.add_handler(cdp_network.ResponseReceived, on_res)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return err(
                f"no request matched {url_pattern!r}"
                + (f" method={method}" if method else "")
                + f" within {timeout}s"
            )
        finally:
            active["flag"] = False
        return ok(json.dumps(match, indent=2, default=str))
    except Exception as e:
        return err(str(e))


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
    """⭐ Click + wait for the side-effect to land. Distinguishes a
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
        expect_*: target for the matching expect mode
        timeout: per-mode max wait

    Returns JSON {clicked, observed: {what, evidence}, elapsed_ms}.
    """
    import time as _time
    if not (ref or selector or text):
        return err("click_and_wait: pass ref=, selector=, or text=")
    try:
        tab = BrowserState.active_tab()
        url_before = await get_url(tab)
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
                        if url_now != url_before:
                            observed = {"what": "navigation",
                                          "evidence": {"from": url_before, "to": url_now}}
                            break
                        await asyncio.sleep(0.2)
                elif mode == "url":
                    if not expect_url_pattern:
                        return err("expect='url' requires expect_url_pattern=")
                    res = await assert_url_matches(expect_url_pattern, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "url", "evidence": str(res)[:200]}
                elif mode == "text":
                    if not expect_text:
                        return err("expect='text' requires expect_text=")
                    res = await assert_text_present(expect_text, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "text", "evidence": str(res)[:200]}
                elif mode == "selector":
                    if not expect_selector:
                        return err("expect='selector' requires expect_selector=")
                    res = await assert_element_visible(selector=expect_selector, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "selector", "evidence": str(res)[:200]}
                elif mode == "request":
                    if not expect_request_pattern:
                        return err("expect='request' requires expect_request_pattern=")
                    res = await wait_for_request(expect_request_pattern, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "request", "evidence": str(res)[:300]}
                elif mode == "network_idle":
                    # Quick network-idle: no in-flight CDP requests for 500ms
                    deadline = _time.monotonic() + timeout
                    last_count = len(BrowserState.network_logs)
                    last_change = _time.monotonic()
                    while _time.monotonic() < deadline:
                        cur = len(BrowserState.network_logs)
                        if cur != last_count:
                            last_count = cur
                            last_change = _time.monotonic()
                        elif _time.monotonic() - last_change >= 0.5:
                            observed = {"what": "network_idle", "evidence": f"{cur} events"}
                            break
                        await asyncio.sleep(0.1)
            except Exception as e:
                observed["error"] = str(e)
            if observed.get("what"):
                break

        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if not observed.get("what") and expect != "auto":
            return err(json.dumps({
                "clicked": click_str[:200],
                "observed": "no matching expect signal",
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
      value: typeof el.value === 'string' ? el.value.slice(0, 200) : el.value,
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
    """⭐ Detailed form analysis in a single call. Returns label,
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
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Stdio MCP entry point.

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

    try:
        if BrowserState.browser and not getattr(BrowserState.browser, "stopped", False):
            BrowserState.browser.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
