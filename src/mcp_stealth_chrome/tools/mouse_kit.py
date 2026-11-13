"""Precision mouse kit: offset and corner clicks, image-matched clicks, recorded and replayed
pointer paths, idle drift, and click_turnstile.

Moved out of server.py. Shared helpers are reached as _srv.<name> at call time, so a test
that patches server.<name> still reaches this code.
"""
from __future__ import annotations

import asyncio
import json
from typing import Literal, Optional

from .. import server as _srv
from .._app import mcp
from ..helpers import err, ok, parse_json, private_staging, ts_filename
from ..humanize import humanized_move, move_pointer
from ..refs import resolve_ref
from ..state import SCREENSHOT_DIR, BrowserState, ensure_dirs


# ══════════════════════════════════════════════════════════════════════════
# 20. PRECISION MOUSE KIT (#1 differentiator)
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

    Reports what the page shows afterwards: whether the cf-turnstile-response token filled,
    the title, and whether the challenge is still there. A click landing is not a challenge
    passed. Measured: on nopecha.com/demo/cloudflare the click earned a cf_clearance cookie
    and the page still answered 403, and the 2captcha demo uses Cloudflare's test sitekey,
    whose token is a dummy. Neither shows that a real protected site lets you in.
    Does NOT work on a full-page "Just a moment..." challenge: CDP clicks failed 3 of 3
    there. Use solve_turnstile_oscursor, which clicks with the real OS cursor (needs the
    [os-cursor] extra and macOS Accessibility + Screen Recording; takes over the mouse),
    or storage_state_load with a warm session, or solve_captcha.
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
            await _srv._dispatch_input(tab, humanized_move(tab, start_x, start_y, target_x, target_y))
            if _srv._dialog_note(tab):
                return err(f"click_turnstile: NOT clicked — moving the pointer there opened a "
                           f"dialog{_srv._dialog_note(tab)}")
            await asyncio.sleep(0.15)
            await _srv._dispatch_input(tab, tab.mouse_click(target_x, target_y))
            if _srv._dialog_note(tab):
                return ok(f"clicked Turnstile at ({target_x},{target_y}){_srv._dialog_note(tab)}")
            return ok(
                f"clicked Turnstile at ({target_x},{target_y}) — "
                f"found via {data['found']} [tier={data.get('tier','primary')}]; "
                f"{await _srv._cf_after_click(tab)}"
            )
        # Tier 3: fallback to OpenCV template matching
        if fallback_template:
            try:
                clicked, what = await _srv._cf_template_click(tab)
            except Exception as tpl_err:
                return err(
                    f"Turnstile widget not found via selectors ({coords_raw}); "
                    f"template fallback also failed: {tpl_err}"
                )
            if not clicked:
                return err(f"Turnstile widget not found via selectors or template: {what}")
            return ok(f"Turnstile {what}, after the selectors found nothing; "
                      f"{await _srv._cf_after_click(tab)}")
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
        return await _srv._click_point_of(
            tab, el, f"click_element_offset {x_percent}% x {y_percent}%",
            x_percent / 100.0, y_percent / 100.0, approach=humanize,
        )
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
        fx = offset / pos.width if pos.width else 2.0
        fy = offset / pos.height if pos.height else 2.0
        if "right" in corner:
            fx = 1 - fx
        if "bottom" in corner:
            fy = 1 - fy
        return await _srv._click_point_of(tab, el, f"click_at_corner {corner}", fx, fy)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def find_by_image(
    template_path: str,
    threshold: float = 0.85,
) -> str:
    """Find an image on the current page via OpenCV template matching.

    Takes a fresh screenshot, matches against template_path image, returns
    (x, y) center of best match. Use for finding visual buttons/icons when
    DOM selectors aren't available.

    Returns JSON: {"found": true, "x": ..., "y": ..., "score": ..., "template": "..."}
    """
    try:
        import cv2
        tab = BrowserState.active_tab()
        ensure_dirs()
        tmp_path = SCREENSHOT_DIR / ts_filename("match-tmp", "png")
        with private_staging(tmp_path) as staged:
            await tab.save_screenshot(filename=str(staged))
        try:
            page_img = cv2.imread(str(tmp_path))
            template = cv2.imread(template_path)
            if page_img is None:
                return err(f"could not read screenshot at {tmp_path}")
            if template is None:
                return err(f"could not read template at {template_path}")

            # capture_screenshot returns device pixels (DPR-scaled); mouse_click
            # expects CSS pixels. Read the real ratio instead of assuming Retina
            # 2.0 (wrong on non-Retina macs, Linux, Windows, HiDPI emulation).
            dpr_raw = await tab.evaluate("window.devicePixelRatio", return_by_value=True)
            try:
                scale = float(dpr_raw.value if hasattr(dpr_raw, "value") else dpr_raw) or 1.0
            except (TypeError, ValueError):
                scale = 1.0
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
            return ok(json.dumps({
                "found": True, "x": cx, "y": cy,
                "score": float(max_val), "template": template_path,
            }))
        finally:
            # Always remove the temp screenshot — previously leaked on the
            # not-found return and every error path.
            try:
                tmp_path.unlink()
            except Exception:
                pass
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
    """Find image via template matching, then click its center.

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
            await _srv._dispatch_input(tab, humanized_move(tab, x + 150, y - 70, x, y))
            if _srv._dialog_note(tab):
                return err(f"click_at_image: NOT clicked — moving the pointer there opened a "
                           f"dialog{_srv._dialog_note(tab)}")
            await asyncio.sleep(0.1)
        await _srv._dispatch_input(tab, tab.mouse_click(x, y))
        return ok(f"clicked image match at ({x},{y}) score={data['score']:.3f}{_srv._dialog_note(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_drift(
    duration_seconds: float = 2.0,
    segments: int = 4,
) -> str:
    """Simulate idle mouse wandering to pass behavioral ML.

    Random Bezier segments across the viewport — mimics a user thinking.
    Call BEFORE a critical interaction (form submit, button click) to
    establish 'human' behavior pattern before the deterministic action.
    """
    if duration_seconds <= 0 or segments < 1:
        return err(f"mouse_drift: needs duration_seconds > 0 and segments >= 1, "
                   f"got {duration_seconds} and {segments}")
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
            await _srv._dispatch_input(tab, humanized_move(tab, cur_x, cur_y, next_x, next_y, steps=int(per_segment * 40)))
            if _srv._dialog_note(tab):
                return err(f"mouse_drift interrupted{_srv._dialog_note(tab)}")
            cur_x, cur_y = next_x, next_y
            await asyncio.sleep(random.uniform(0.1, 0.4))
        return ok(f"drifted through {segments} segments over {duration_seconds}s")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_record(duration_seconds: float = 5.0) -> str:
    """Record real mouse movements from the page for later replay.

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
        try:
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
        finally:
            # Guarantee teardown even on CancelledError / mid-sleep failure so
            # the mousemove listener + window globals don't leak on the page.
            try:
                await tab.evaluate(
                    "(() => { if (window.__mcpMouseHandler) {"
                    "document.removeEventListener('mousemove', window.__mcpMouseHandler);"
                    "delete window.__mcpMouseHandler; delete window.__mcpMouseRec; } })()",
                    return_by_value=True,
                )
            except Exception:
                pass
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_replay(path_json: str, speed: float = 1.0) -> str:
    """Replay a recorded mouse path (from mouse_record).

    Args:
        path_json: JSON array of {t, x, y} from mouse_record
        speed: 1.0 = original speed, 2.0 = 2x faster, 0.5 = slower
    """
    try:
        tab = BrowserState.active_tab()
        events = parse_json(path_json, [])
        if not isinstance(events, list) or not events:
            return err("empty/invalid path")
        if speed <= 0:
            return err(f"mouse_replay: speed must be above 0, got {speed}")
        for ev in events:
            if not (isinstance(ev, dict) and isinstance(ev.get("x"), (int, float))
                    and isinstance(ev.get("y"), (int, float))):
                return err(f"mouse_replay: every point needs numeric x and y, got {ev!r}; "
                           f"nothing was replayed")
        prev_t = 0
        for ev in events:
            t = ev.get("t", prev_t)
            dt = max(0, (t - prev_t) / 1000.0 / speed)
            await asyncio.sleep(dt)
            await _srv._dispatch_input(tab, move_pointer(tab, int(ev.get("x", 0)), int(ev.get("y", 0))))
            if _srv._dialog_note(tab):
                return err(f"mouse_replay interrupted{_srv._dialog_note(tab)}")
            prev_t = t
        return ok(f"replayed {len(events)} mouse events")
    except Exception as e:
        return err(str(e))
