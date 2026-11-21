"""Viewport and scroll tools: get_viewport_size, set_viewport_size, scroll and scroll_to.

Moved out of server.py. The element and settle helpers they use stay there and are reached as
_srv.<name> at call time, so a test that patches server.<name> still reaches the code.
"""

import asyncio
import contextlib
import json
import time
from typing import Optional

from .. import server as _srv
from .._app import mcp
from .. import presence
from ..helpers import err, ok
from ..humanize import humanized_scroll
from ..refs import resolve_ref
from ..state import BrowserState


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
        before = await _srv._eval_checked(tab, "window.scrollY")
        await presence.before_input(tab)  # refuses once the person took over
        if direction == "top":
            await _srv._eval_checked(tab, "window.scrollTo(0,0)")
            how = "to top"
        elif direction == "bottom":
            await _srv._eval_checked(tab, "window.scrollTo(0, document.documentElement.scrollHeight)")
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
            after = await _srv._eval_checked(tab, "window.scrollY")
            if after != before:
                await asyncio.sleep(0.05)
                after = await _srv._eval_checked(tab, "window.scrollY")
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
        await presence.before_input(el.tab)  # refuses once the person took over
        await el.apply(
            f"function() {{ this.scrollIntoView("
            f"{{block:{json.dumps(block)}, behavior:{json.dumps(behavior)}}}); }}"
        )
        # A smooth scroll is still moving when this returns; the next tool
        # would otherwise measure a position that is already out of date. On a busy machine a
        # smooth scroll stalls between frames for longer than two readings, and macOS CI saw
        # it reported done 58 px short; four still readings (160 ms) ride that out.
        quiet = 4 if smooth else 2
        await _srv._settled_position(el, quiet)
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
                await _srv._settled_position(el, quiet)
        if visible is not True:
            return err(f"{ref or selector} is still outside the viewport after scrolling")
        return ok(f"scrolled {ref or selector} into view (block={block})")
    except Exception as e:
        return err(str(e))
