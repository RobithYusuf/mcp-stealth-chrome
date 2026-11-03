"""Shared helper utilities."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nodriver import Tab

from .state import BrowserState


def target_key(tab: Any) -> str:
    """Stable per-tab identity backed by the CDP target id when available."""
    target = getattr(tab, "target", None)
    return str(getattr(target, "target_id", None) or id(tab))


def select_tab(index: int | None = None) -> Tab:
    """Resolve a tab index without changing active selection or OS focus."""
    active = BrowserState.active_tab()
    if index is None:
        return active
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(BrowserState.tabs):
        raise ValueError(f"tab index {index!r} out of range (0..{len(BrowserState.tabs) - 1})")
    return BrowserState.tabs[index]


def transport_lost(text: str) -> bool:
    """Recognize CDP transport failures, not ordinary page JavaScript errors."""
    message = text.lower()
    return any(marker in message for marker in (
        "no close frame received", "connectionclosed", "websocket is closed",
        "websocket connection is closed", "session with given id not found",
        "cdp session lost", "cdp websocket lost", "-32001",
    ))


async def get_url(tab: Tab) -> str:
    """Get current URL via evaluate — nodriver doesn't expose direct property."""
    try:
        result = await tab.evaluate("window.location.href", return_by_value=True)
        # nodriver hands back a raw RemoteObject when the value is falsy —
        # unwrap so a blank result never becomes the wrapper's repr string.
        result = result.value if hasattr(result, "value") else result
        return str(result) if result else ""
    except Exception:
        return ""


async def get_title(tab: Tab) -> str:
    try:
        result = await tab.evaluate("document.title", return_by_value=True)
        result = result.value if hasattr(result, "value") else result
        return str(result) if result else ""
    except Exception:
        return ""


def ts_filename(prefix: str, ext: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}.{ext}"


def confined_output_path(root: Path, filename: str) -> Path:
    """Resolve an output filename without letting it escape its managed root."""
    base = root.resolve()
    target = (root / filename).resolve()
    if target == base or base not in target.parents:
        raise ValueError(f"output path must stay inside {base}: {filename!r}")
    return target


def ok(text: str) -> str:
    """Success response — FastMCP auto-wraps as TextContent."""
    return text


def err(text: str) -> str:
    """Error response with recovery guidance on the first transport failure."""
    if transport_lost(text) and "browser_recover()" not in text:
        text += (" — CDP transport lost. Call browser_recover() then browser_launch(); "
                 "persistent profiles and logins are preserved. The action outcome is unknown; "
                 "do not blindly retry side effects.")
    return f"Error: {text}"


def parse_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default
