"""Human-like behavior simulation — Bezier mouse movement, Gaussian typing delays.

Layered on top of nodriver's native mouse/keyboard methods to add realistic
timing and trajectory patterns that fool behavioral ML fingerprinting.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Tuple

from nodriver import Element, Tab


def _bezier_point(t: float, p0: Tuple[float, float], p1: Tuple[float, float],
                  p2: Tuple[float, float], p3: Tuple[float, float]) -> Tuple[float, float]:
    """Cubic Bezier interpolation."""
    u = 1 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


async def humanized_move(tab: Tab, start_x: float, start_y: float,
                          end_x: float, end_y: float, steps: int = 25) -> None:
    """Move cursor along a randomized cubic Bezier path with variable speed."""
    dx, dy = end_x - start_x, end_y - start_y
    dist = math.hypot(dx, dy)
    curve = max(20, dist * 0.3)

    # Control points — offset perpendicular for natural curve
    angle = math.atan2(dy, dx) + math.pi / 2
    c1 = (start_x + dx * 0.3 + math.cos(angle) * random.uniform(-curve, curve),
          start_y + dy * 0.3 + math.sin(angle) * random.uniform(-curve, curve))
    c2 = (start_x + dx * 0.7 + math.cos(angle) * random.uniform(-curve, curve),
          start_y + dy * 0.7 + math.sin(angle) * random.uniform(-curve, curve))
    p0, p3 = (start_x, start_y), (end_x, end_y)

    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out via smoothstep
        t = t * t * (3 - 2 * t)
        x, y = _bezier_point(t, p0, c1, c2, p3)
        await tab.mouse_move(int(x), int(y))
        await asyncio.sleep(random.uniform(0.005, 0.02))


async def humanized_click(tab: Tab, element: Element) -> None:
    """Click with Bezier approach + randomized dwell."""
    pos = await element.get_position()
    if pos is None:
        await element.click()
        return
    # Random point inside element
    target_x = pos.left + pos.width * random.uniform(0.3, 0.7)
    target_y = pos.top + pos.height * random.uniform(0.3, 0.7)

    # Get current mouse — nodriver doesn't expose, start from a reasonable origin
    start_x = target_x + random.uniform(-200, 200)
    start_y = target_y + random.uniform(-150, 150)
    await humanized_move(tab, start_x, start_y, target_x, target_y)
    await asyncio.sleep(random.uniform(0.05, 0.18))
    await tab.mouse_click(int(target_x), int(target_y))


async def humanized_type(element: Element, text: str,
                          mean_delay: float = 0.12, jitter: float = 0.08) -> None:
    """Type character-by-character with Gaussian-distributed delay."""
    await element.focus()
    for ch in text:
        # Gauss-distributed delay — mean ± jitter, clipped to positive
        delay = max(0.02, random.gauss(mean_delay, jitter))
        await element.send_keys(ch)
        await asyncio.sleep(delay)
