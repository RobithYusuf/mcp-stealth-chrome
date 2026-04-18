"""Shared helper utilities."""
from __future__ import annotations

import json
import time
from typing import Any

from nodriver import Element, Tab

from .state import BrowserState


async def resolve_ref(ref: str) -> Element | None:
    """Find element by data-mcp-ref attribute set via SNAPSHOT_JS."""
    tab = BrowserState.active_tab()
    try:
        return await tab.query_selector(f'[data-mcp-ref="{ref}"]')
    except Exception:
        return None


async def get_url(tab: Tab) -> str:
    """Get current URL via evaluate — nodriver doesn't expose direct property."""
    try:
        result = await tab.evaluate("window.location.href", return_by_value=True)
        return str(result) if result else ""
    except Exception:
        return ""


async def get_title(tab: Tab) -> str:
    try:
        result = await tab.evaluate("document.title", return_by_value=True)
        return str(result) if result else ""
    except Exception:
        return ""


def ts_filename(prefix: str, ext: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}.{ext}"


def ok(text: str) -> str:
    """Success response — FastMCP auto-wraps as TextContent."""
    return text


def err(text: str) -> str:
    """Error response — prefixed so callers can detect."""
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
