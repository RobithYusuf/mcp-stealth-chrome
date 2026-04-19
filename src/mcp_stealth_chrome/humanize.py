"""Human-like behavior simulation — Bezier curves, parabolic speed, wheel events.

Layered on top of nodriver to add realistic timing/trajectory patterns that
fool behavioral ML (DataDome, PerimeterX). Key techniques (evidence-based):

1. Bezier curve paths — real cursors move in curves, not straight lines
2. Parabolic speed — slow start, fast middle, slow end (human acceleration)
3. Real wheel events via CDP — NOT window.scrollBy (detected)
4. Variable scroll chunks — uniform distance = bot signature
5. Occasional reading pauses — 20% probability 0.5-2s gaps
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Optional, Tuple

from nodriver import Element, Tab


def _bezier_point(t: float, p0: Tuple[float, float], p1: Tuple[float, float],
                  p2: Tuple[float, float], p3: Tuple[float, float]) -> Tuple[float, float]:
    """Cubic Bezier interpolation."""
    u = 1 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


async def humanized_move(
    tab: Tab, start_x: float, start_y: float,
    end_x: float, end_y: float,
    steps: Optional[int] = None,
    last_pos_ref: Optional[dict] = None,
) -> Tuple[int, int]:
    """Move cursor along randomized Bezier path with PARABOLIC speed curve.

    Parabolic speed = slower at start/end, faster in middle (human physics).
    Auto-calculates steps based on distance (min 15, max 50).

    last_pos_ref: if passed dict, updates ["x"], ["y"] after move (for tracking).
    Returns final (x, y).
    """
    dx, dy = end_x - start_x, end_y - start_y
    dist = math.hypot(dx, dy)

    # Auto-scale steps by distance (short moves = fewer steps)
    if steps is None:
        steps = max(15, min(50, int(dist / 20)))

    curve = max(20, dist * 0.3)

    # Control points — offset perpendicular to travel direction for natural arc
    angle = math.atan2(dy, dx) + math.pi / 2
    c1 = (start_x + dx * 0.3 + math.cos(angle) * random.uniform(-curve, curve),
          start_y + dy * 0.3 + math.sin(angle) * random.uniform(-curve, curve))
    c2 = (start_x + dx * 0.7 + math.cos(angle) * random.uniform(-curve, curve),
          start_y + dy * 0.7 + math.sin(angle) * random.uniform(-curve, curve))
    p0, p3 = (start_x, start_y), (end_x, end_y)

    base_delay = random.uniform(0.008, 0.015)

    for i in range(1, steps + 1):
        t = i / steps
        # Smoothstep for position (ease-in-out curve)
        t_smooth = t * t * (3 - 2 * t)
        x, y = _bezier_point(t_smooth, p0, c1, c2, p3)
        await tab.mouse_move(int(x), int(y))

        # Parabolic speed: speed_factor peaks at t=0.5 (middle)
        # delay is INVERSE of speed — slower at edges, faster at middle
        speed_factor = 1 - 4 * (t - 0.5) ** 2  # 0→1→0 shape
        delay = base_delay * (1.8 - speed_factor)  # edges slower
        # Add micro-jitter
        delay += random.uniform(-0.002, 0.003)
        await asyncio.sleep(max(0.003, delay))

    if last_pos_ref is not None:
        last_pos_ref["x"] = int(end_x)
        last_pos_ref["y"] = int(end_y)
    return int(end_x), int(end_y)


async def humanized_click(
    tab: Tab, element: Element,
    last_pos_ref: Optional[dict] = None,
) -> None:
    """Click element with Bezier approach from last-known cursor position."""
    pos = await element.get_position()
    if pos is None:
        await element.click()
        return
    # Random spot inside element (not dead center — that's a bot tell)
    target_x = pos.left + pos.width * random.uniform(0.3, 0.7)
    target_y = pos.top + pos.height * random.uniform(0.3, 0.7)

    # Start from last-known cursor position if available, else random nearby
    if last_pos_ref and last_pos_ref.get("x") is not None:
        start_x = last_pos_ref["x"]
        start_y = last_pos_ref["y"]
    else:
        start_x = target_x + random.uniform(-200, 200)
        start_y = target_y + random.uniform(-150, 150)

    await humanized_move(tab, start_x, start_y, target_x, target_y,
                          last_pos_ref=last_pos_ref)
    # Dwell before click (human hesitation)
    await asyncio.sleep(random.uniform(0.05, 0.18))
    await tab.mouse_click(int(target_x), int(target_y))


async def humanized_type(
    element: Element, text: str,
    mean_delay: float = 0.12, jitter: float = 0.08,
) -> None:
    """Type character-by-character with Gaussian-distributed delay."""
    await element.focus()
    for ch in text:
        delay = max(0.02, random.gauss(mean_delay, jitter))
        await element.send_keys(ch)
        await asyncio.sleep(delay)


async def humanized_scroll(
    tab: Tab, delta_y: int,
    position: Optional[Tuple[int, int]] = None,
) -> int:
    """Scroll via REAL mouseWheel CDP events (not JS scrollBy).

    Breaks total delta into random 50-150px chunks with micro-pauses, plus
    occasional reading pauses (20% chance 0.5-2s) — mimics human scroll pattern
    that DataDome/PerimeterX behavioral ML expects.

    Args:
        delta_y: total pixels to scroll (positive=down, negative=up)
        position: (x, y) where wheel event originates (default = viewport center)

    Returns actual pixels scrolled.
    """
    from nodriver.cdp import input_ as cdp_input

    # Default wheel origin to viewport center
    if position is None:
        try:
            vp = await tab.evaluate(
                "JSON.stringify([innerWidth/2, innerHeight/2])",
                return_by_value=True,
            )
            import json as _j
            try:
                px, py = _j.loads(str(vp))
                position = (int(px), int(py))
            except Exception:
                position = (500, 400)
        except Exception:
            position = (500, 400)

    target = abs(delta_y)
    direction = 1 if delta_y > 0 else -1
    scrolled = 0

    while scrolled < target:
        remaining = target - scrolled
        # Variable chunk — DataDome detects uniform distances
        chunk = min(random.randint(50, 150), remaining)
        try:
            await tab.send(cdp_input.dispatch_mouse_event(
                type_="mouseWheel",
                x=position[0], y=position[1],
                delta_x=0,
                delta_y=chunk * direction,
            ))
        except Exception:
            # Fallback: JS scroll (less stealthy but works)
            await tab.evaluate(
                f"window.scrollBy(0, {chunk * direction})",
                return_by_value=True,
            )
        scrolled += chunk
        # Micro-pause between chunks (scroll bursts, not continuous)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        # 20% chance of longer reading pause
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(0.5, 2.0))

    return scrolled
