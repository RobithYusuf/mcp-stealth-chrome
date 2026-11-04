"""Shared helper utilities."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit
from typing import Any

from nodriver import Tab

from .state import BrowserState


# Names whose values are credentials. Matched as whole words, so "author" and the
# ":authority" pseudo-header stay readable. Tool output lands in the agent's transcript.
_SECRET_NAME = re.compile(
    r"(?:^|[-_.\s\[\]])(?:auth|authentication|authorization|bearer|tokens?|jwt|secret|password"
    r"|passwd|passphrase|passcode|pwd|pass|otp|pin|cvv|cvc|api[-_]?key|apikey|csrf|xsrf|session"
    r"|sessionid|sid|cookies?|credentials?|signature)(?:$|[-_.\s\[\]])",
    re.IGNORECASE,
)


def is_secret_name(name: Any) -> bool:
    """Whether a header, form or JSON field of this name carries a credential."""
    return bool(_SECRET_NAME.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", str(name))))


def masked_value(value: Any) -> str:
    """Enough to see that a value exists and is non-empty, as cookie_list does."""
    return f"*** ({len(str(value))} chars)"


def mask_headers(headers: dict) -> dict:
    return {k: masked_value(v) if is_secret_name(k) else v for k, v in (headers or {}).items()}


def _mask_all(node: Any) -> Any:
    """Everything below a secret key: {"token": {"value": "x"}} is as secret as {"token": "x"}."""
    if isinstance(node, dict):
        return {k: _mask_all(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_all(item) for item in node]
    return node if node is None or isinstance(node, bool) else masked_value(node)


# A session object usually mixes the user and expiry with its tokens. Those stay readable, and
# any secret names inside are still caught field by field.
_CONTAINER_ONLY_NAMES = re.compile(r"^(?:session|sessions)$", re.IGNORECASE)


def _mask_json(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if not is_secret_name(k):
                out[k] = _mask_json(v)
            elif isinstance(v, (dict, list)):
                out[k] = _mask_json(v) if _CONTAINER_ONLY_NAMES.match(str(k)) else _mask_all(v)
            elif v is None or isinstance(v, bool):
                out[k] = v
            else:
                out[k] = masked_value(v)
        return out
    if isinstance(node, list):
        return [_mask_json(item) for item in node]
    return node


def mask_json_value(value: Any) -> Any:
    """Credential fields hidden in an already parsed JSON document."""
    return _mask_json(value)


def _mask_pairs(text: str) -> str | None:
    """A query or form body with secret fields hidden, or None when it has none.

    Works on the raw pairs. Decoding and re-encoding the rest would rewrite ordinary
    parameters, and a captured URL has to stay byte for byte what was requested.
    """
    pieces = text.split("&")
    found = False
    for index, piece in enumerate(pieces):
        name = unquote_plus(piece.partition("=")[0])
        if is_secret_name(name):
            found = True
            pieces[index] = f"{piece.partition('=')[0]}=***"
    return "&".join(pieces) if found else None


def mask_body(body: Any) -> Any:
    """Hide credential fields in a JSON or form body; returned unchanged when it has none.

    Other formats cannot be read field by field, so they pass through as they are.
    """
    if not isinstance(body, str) or not body:
        return body
    text = body.strip()
    if text[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return body
        masked = _mask_json(parsed)
        return body if masked == parsed else json.dumps(masked, ensure_ascii=False)
    if "=" in text and "\n" not in text and not text.startswith("<"):
        return _mask_pairs(text) or body
    return body


def mask_url(url: Any) -> Any:
    """Hide credential query parameters such as ?access_token=."""
    if not isinstance(url, str) or "?" not in url:
        return url
    try:
        parts = urlsplit(url)
        query = _mask_pairs(parts.query)
        return parts._replace(query=query).geturl() if query else url
    except ValueError:
        return url


def mask_request_entry(entry: dict) -> dict:
    """A copy of a captured request safe to return from a tool."""
    out = dict(entry)
    for key in ("headers", "request_headers", "response_headers"):
        if isinstance(out.get(key), dict):
            out[key] = mask_headers(out[key])
    for key in ("body", "request_body"):
        if key in out:
            out[key] = mask_body(out[key])
    if "url" in out:
        out["url"] = mask_url(out["url"])
    return out


def target_key(tab: Any) -> str:
    """Stable per-tab identity backed by the CDP target id when available."""
    target = getattr(tab, "target", None)
    return str(getattr(target, "target_id", None) or id(tab))


# Native dialogs open right now, per tab. A dialog blocks the renderer: input never gets its
# acknowledgement and anything that needs the page never answers.
OPEN_DIALOGS: dict[str, dict] = {}


def dialog_note(tab: Any) -> str:
    """Words for a dialog that is open on this tab, or "" when none is."""
    found = OPEN_DIALOGS.get(target_key(tab))
    if not found:
        return ""
    return (f"; a native {found['type']} dialog is open ({found['message']!r}) and blocks the "
            f"page until it is answered: call dialog_handle(action='accept' or 'dismiss')")


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


class PageReadError(RuntimeError):
    """The page's URL or title could not be read. Blank would claim that it has none."""


async def _read_page_string(tab: Tab, expression: str, what: str, strict: bool) -> str:
    try:
        result = await tab.evaluate(expression, return_by_value=True)
        # nodriver hands back a raw RemoteObject when the value is falsy —
        # unwrap so a blank result never becomes the wrapper's repr string.
        result = result.value if hasattr(result, "value") else result
    except Exception as exc:
        # A lost connection is never "no URL yet", even for a caller who is polling:
        # it would otherwise wait out its whole timeout against a dead browser.
        if transport_lost(str(exc)):
            raise
        if strict:
            raise PageReadError(f"could not read the page {what}: {exc}") from exc
        return ""
    return str(result) if result else ""


async def get_url(tab: Tab, strict: bool = False) -> str:
    """The page URL via evaluate — nodriver doesn't expose a direct property.

    strict=False returns "" when the read fails, for loops polling through a navigation
    where that is expected. Anything that REPORTS the URL passes strict=True.
    """
    return await _read_page_string(tab, "window.location.href", "URL", strict)


async def get_title(tab: Tab, strict: bool = False) -> str:
    """The page title. Blank is a real answer here: many pages have no title."""
    return await _read_page_string(tab, "document.title", "title", strict)


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
