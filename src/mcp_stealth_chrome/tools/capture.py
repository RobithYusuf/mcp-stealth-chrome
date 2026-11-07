"""Request interception (CDP Fetch), HAR export and dry_run.

Moved out of server.py unchanged. Fetch.enable pauses every matching request until
something answers it, so every path through the handler ends in exactly one
continue, fail or fulfill, and interception switches itself off on a deadline.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
import time
from typing import Any, Literal, Optional

from .. import __version__
from .._app import mcp
from ..helpers import confined_output_path, err, mask_request_entry, ok, ts_filename
from ..server import _drop_handlers
from ..state import EXPORT_DIR, BrowserState, ensure_dirs


# ══════════════════════════════════════════════════════════════════════════
# REQUEST INTERCEPTION (CDP Fetch)
# ══════════════════════════════════════════════════════════════════════════
#
# Fetch.enable pauses EVERY matching request until something answers it, so a
# handler that fails to respond does not raise — it hangs the page with no error
# at all. Two rules follow, and the whole design is built around them:
#   1. every path through the handler ends in exactly one continue/fail/fulfill,
#      including the exception path;
#   2. interception disables itself on a deadline, so a forgotten intercept
#      cannot leave the browser wedged.
_INTERCEPT: dict[str, Any] = {
    "active": False,
    "action": "observe",
    "url_pattern": "*",
    "methods": None,          # None = every method is a target
    "log": [],
    "max_events": 500,
    "handler": None,
    "handler_tab": None,      # where `handler` is installed; outlives `tab` on a failed disable
    "tab": None,
    "watchdog": None,
    "expires_at": 0.0,
    "mock": None,
    "counts": {"seen": 0, "passed": 0, "blocked": 0, "mocked": 0},
}

_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _intercept_drop_handler() -> None:
    """Take the RequestPaused handler off its tab, or a restart stacks a second one and one
    paused request gets two answers."""
    handler_tab, handler = _INTERCEPT.get("handler_tab"), _INTERCEPT.get("handler")
    if handler_tab is not None and handler is not None:
        from nodriver.cdp import fetch as cdp_fetch
        _drop_handlers(handler_tab, [(cdp_fetch.RequestPaused, handler)])
    _INTERCEPT["handler_tab"] = None
    _INTERCEPT["handler"] = None


async def _intercept_disable(reason: str = "stopped") -> None:
    """Turn interception off and release the page. Safe to call twice."""
    state = _INTERCEPT
    watchdog = state.get("watchdog")
    # On the deadline path this runs INSIDE the watchdog. Cancelling the current task makes
    # the next await raise CancelledError, so the state below was never updated: `active`
    # stayed true with no stop_reason, and other sessions were told it was in use forever.
    if watchdog is not None and not watchdog.done() and watchdog is not asyncio.current_task():
        watchdog.cancel()
    state["watchdog"] = None
    tab = state.get("tab")
    try:
        if tab is not None:
            from nodriver.cdp import fetch as cdp_fetch
            try:
                await tab.send(cdp_fetch.disable())
            except Exception:
                # Fetch may still be pausing requests, and only this handler answers them.
                # intercept_start removes it before installing the next one.
                pass
            else:
                _intercept_drop_handler()
    finally:
        state["active"] = False
        state["tab"] = None
        state["stop_reason"] = reason


def _intercept_matches(url: str, pattern: str) -> bool:
    """Glob-ish match used only for reporting; CDP does the real filtering."""
    if not pattern or pattern == "*":
        return True
    return fnmatch.fnmatch(url, pattern) or pattern.strip("*") in url


@mcp.tool()
async def intercept_start(
    url_pattern: str = "*",
    action: Literal["observe", "cancel", "mock"] = "observe",
    methods: Optional[str] = None,
    mock_status: int = 200,
    mock_body: str = "",
    mock_content_type: str = "application/json",
    auto_disable_after: float = 180.0,
    max_events: int = 500,
) -> str:
    """See a request — and optionally stop it — BEFORE the server ever gets it.

    Answers "what would this button actually send?" without sending it. The usual
    alternative is patching window.fetch by hand, which misses XHR, misses
    anything the page captured before your script ran, and cannot stop a request
    that is already in flight. This works at the CDP layer, so it sees every
    request regardless of timing.

    Args:
        url_pattern: CDP glob, e.g. "*/api/*" or "*delete*". "*" matches all.
        action: what to do with matching requests —
            "observe" lets them through and records them;
            "cancel"  aborts them (the page sees a network failure);
            "mock"    answers them locally with mock_status/mock_body.
        methods: comma list to target, e.g. "POST,DELETE". Non-matching methods
            are always let through untouched. Omit to target every method.
        mock_status / mock_body / mock_content_type: the canned response.
        auto_disable_after: seconds before interception turns itself off. This is
            a safety net, not a nicety: an interception left on with a dead
            handler wedges the page silently. 0 disables the timer.
        max_events: ring-buffer size for the log.

    Pair with intercept_log() to read what was seen and intercept_stop() to end
    it early. See dry_run() for the common "explore without mutating" preset.
    """
    try:
        tab = BrowserState.active_tab()
        if _INTERCEPT["active"]:
            holder = _INTERCEPT.get("session")
            if holder not in (None, BrowserState.session_key()):
                return err(
                    f"request interception is in use by another MCP session ({holder}). "
                    "This server runs one interception at a time; wait for it to end."
                )
            await _intercept_disable("restarted")

        from nodriver.cdp import fetch as cdp_fetch, network as cdp_network

        wanted: Optional[tuple] = None
        if methods:
            wanted = tuple(m.strip().upper() for m in methods.split(",") if m.strip())
            if not wanted:
                wanted = None

        state = _INTERCEPT
        state.update({
            "active": True,
            "session": BrowserState.session_key(),
            "action": action,
            "url_pattern": url_pattern or "*",
            "methods": wanted,
            "log": [],
            "max_events": max(1, int(max_events)),
            "tab": tab,
            "counts": {"seen": 0, "passed": 0, "blocked": 0, "mocked": 0},
            "mock": {
                "status": int(mock_status),
                "body": mock_body or "",
                "content_type": mock_content_type,
            },
            "stop_reason": None,
        })

        async def on_paused(event):
            request_id = getattr(event, "request_id", None)
            responded = False
            try:
                request = event.request
                method = (getattr(request, "method", "") or "").upper()
                url = getattr(request, "url", "") or ""
                state["counts"]["seen"] += 1
                targeted = state["methods"] is None or method in state["methods"]
                chosen = state["action"] if targeted else "observe"

                if chosen == "cancel":
                    await tab.send(cdp_fetch.fail_request(
                        request_id=request_id,
                        error_reason=cdp_network.ErrorReason.ABORTED))
                    responded = True
                    state["counts"]["blocked"] += 1
                    outcome = "blocked"
                elif chosen == "mock":
                    mock = state["mock"] or {}
                    body_b64 = base64.b64encode(
                        (mock.get("body") or "").encode("utf-8")).decode()
                    await tab.send(cdp_fetch.fulfill_request(
                        request_id=request_id,
                        response_code=int(mock.get("status", 200)),
                        response_headers=[cdp_fetch.HeaderEntry(
                            name="Content-Type",
                            value=mock.get("content_type", "application/json"))],
                        body=body_b64))
                    responded = True
                    state["counts"]["mocked"] += 1
                    outcome = "mocked"
                else:
                    await tab.send(cdp_fetch.continue_request(request_id=request_id))
                    responded = True
                    state["counts"]["passed"] += 1
                    outcome = "passed"

                log = state["log"]
                log.append({
                    "method": method,
                    "url": url,
                    "outcome": outcome,
                    "targeted": targeted,
                    "headers": dict(getattr(request, "headers", None) or {}),
                    "body": getattr(request, "post_data", None),
                })
                if len(log) > state["max_events"]:
                    del log[:-state["max_events"]]
            except Exception:
                pass
            finally:
                # The page is blocked until this request is resolved exactly once.
                # Anything that went wrong above must still end in a decision.
                if not responded:
                    try:
                        await tab.send(cdp_fetch.continue_request(request_id=request_id))
                    except Exception:
                        pass

        _intercept_drop_handler()
        tab.add_handler(cdp_fetch.RequestPaused, on_paused)
        state["handler"] = on_paused
        state["handler_tab"] = tab
        await tab.send(cdp_fetch.enable(patterns=[
            cdp_fetch.RequestPattern(url_pattern=url_pattern or "*")
        ]))

        if auto_disable_after and auto_disable_after > 0:
            state["expires_at"] = time.time() + float(auto_disable_after)

            async def watchdog():
                try:
                    await asyncio.sleep(float(auto_disable_after))
                    if _INTERCEPT["active"]:
                        await _intercept_disable("auto-disabled on deadline")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

            state["watchdog"] = asyncio.create_task(watchdog())
        else:
            state["expires_at"] = 0.0

        return ok(json.dumps({
            "active": True,
            "action": action,
            "url_pattern": url_pattern or "*",
            "methods": list(wanted) if wanted else "all",
            "auto_disable_after_s": auto_disable_after or None,
            "note": (
                "Every matching request is paused until this handler answers it. "
                "Call intercept_stop() when done; it also disables itself after "
                "the deadline so a forgotten intercept cannot wedge the page."
            ),
        }, indent=2))
    except Exception as e:
        try:
            # Only what this call set up. active_tab() raising before the owner check,
            # with another session's interception live, used to switch that one off.
            if _INTERCEPT.get("session") == BrowserState.session_key():
                await _intercept_disable("failed to start")
        except Exception:
            pass
        return err(str(e))


@mcp.tool()
async def intercept_log(limit: int = 50, clear: bool = False, reveal_values: bool = False) -> str:
    """Read what interception saw, with its status attached.

    Reports whether interception is still active and why it stopped, because an
    empty log means nothing on its own: it cannot otherwise be told apart from
    "never started" or "the deadline already fired".

    Credential headers, fields and query parameters read "*** (N chars)" because tool
    output lands in the transcript; reveal_values=True returns them. Bodies other than
    JSON or forms cannot be read field by field and pass through.
    """
    state = _INTERCEPT
    holder = state.get("session")
    if holder not in (None, BrowserState.session_key()):
        # The log holds another session's request headers and bodies.
        return err(f"request interception belongs to another MCP session ({holder}).")
    entries = state["log"][-max(1, int(limit)):]
    payload = {
        "active": state["active"],
        "action": state["action"],
        "url_pattern": state["url_pattern"],
        "methods": list(state["methods"]) if state["methods"] else "all",
        "counts": dict(state["counts"]),
        "expires_in_s": (round(state["expires_at"] - time.time(), 1)
                         if state["active"] and state["expires_at"] else None),
        "stop_reason": state.get("stop_reason"),
        "returned": len(entries),
        "requests": entries if reveal_values else [mask_request_entry(e) for e in entries],
    }
    if not state["active"]:
        payload["note"] = (
            "NOT intercepting — an empty list here means nothing was watched, "
            "not that the page sent nothing. Call intercept_start() or dry_run()."
        )
    if clear:
        state["log"] = []
    return ok(json.dumps(payload, indent=2, default=str))


@mcp.tool()
async def intercept_stop() -> str:
    """Stop intercepting and let the page make requests normally again."""
    holder = _INTERCEPT.get("session")
    if _INTERCEPT["active"] and holder not in (None, BrowserState.session_key()):
        return err(f"request interception belongs to another MCP session ({holder}).")
    was_active = _INTERCEPT["active"]
    counts = dict(_INTERCEPT["counts"])
    await _intercept_disable("stopped by caller")
    if not was_active:
        return ok("interception was not active")
    return ok(json.dumps({"stopped": True, "counts": counts}, indent=2))


@mcp.tool()
async def dry_run(
    enable: bool = True,
    url_pattern: str = "*",
    methods: str = "POST,PUT,PATCH,DELETE",
    auto_disable_after: float = 300.0,
) -> str:
    """Explore a logged-in account without changing anything.

    Blocks every mutating request (POST/PUT/PATCH/DELETE by default) and records
    what would have been sent — method, URL, headers, body. Reads still work, so
    the app is fully navigable while nothing can be created, edited or deleted.

    This is the safe way to answer "which endpoint does the delete button call?"
    on a real account: click it, read intercept_log(), and see the exact request
    that was stopped. GET and HEAD are untouched, so the page keeps working.

    Args:
        enable: False turns dry-run off (same as intercept_stop).
        url_pattern: narrow to one API surface, e.g. "*/api/*".
        methods: which methods to block. Widen or narrow as needed.
        auto_disable_after: seconds before it lifts itself, so a forgotten
            dry-run does not silently break later work.

    The page will surface blocked calls as network errors — that is the intended,
    visible signal that a mutation was prevented, not a bug.
    """
    if not enable:
        return await intercept_stop()
    result = await intercept_start(
        url_pattern=url_pattern,
        action="cancel",
        methods=methods,
        auto_disable_after=auto_disable_after,
    )
    if result.startswith("Error"):
        return result
    return ok(json.dumps({
        "dry_run": True,
        "blocking": [m.strip().upper() for m in methods.split(",") if m.strip()],
        "url_pattern": url_pattern,
        "auto_disable_after_s": auto_disable_after,
        "how_to_read": (
            "Drive the UI as usual, then call intercept_log() to see the exact "
            "requests that were stopped, with headers and body. Blocked calls "
            "appear in the page as network errors — that is the mutation being "
            "prevented, working as intended."
        ),
    }, indent=2))


def _har_entry(entry: dict) -> dict:
    """One captured request in HAR 1.2 shape; sizes and times unknown here say so."""
    from datetime import datetime, timezone
    from urllib.parse import parse_qsl, urlsplit

    def pairs(headers: Optional[dict]) -> list[dict]:
        return [{"name": k, "value": str(v)} for k, v in (headers or {}).items()]

    url = entry.get("url") or ""
    body = entry.get("request_body")
    wall = entry.get("wall_time")
    request: dict[str, Any] = {
        "method": entry.get("method") or "",
        "url": url,
        "httpVersion": "",
        "headers": pairs(entry.get("request_headers")),
        "queryString": [{"name": k, "value": v} for k, v in
                        parse_qsl(urlsplit(url).query, keep_blank_values=True)],
        "cookies": [],
        "headersSize": -1,
        "bodySize": len(body) if body else 0,
    }
    if body:
        kind = next((v for k, v in (entry.get("request_headers") or {}).items()
                     if k.lower() == "content-type"), "")
        request["postData"] = {"mimeType": kind, "text": body}
    return {
        "startedDateTime": (datetime.fromtimestamp(float(wall), timezone.utc).isoformat()
                            if wall else ""),
        "time": -1,
        "request": request,
        "response": {
            "status": entry.get("status") or 0,
            "statusText": "",
            "httpVersion": "",
            "headers": pairs(entry.get("response_headers")),
            "cookies": [],
            "content": {"size": -1, "mimeType": entry.get("mime") or ""},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": entry.get("size") if entry.get("size") is not None else -1,
        },
        "cache": {},
        "timings": {"send": -1, "wait": -1, "receive": -1},
        "_resourceType": entry.get("type"),
        "_requestId": entry.get("request_id"),
    }


@mcp.tool()
async def export_har(filename: Optional[str] = None) -> str:
    """Export captured requests to a HAR file: URLs, headers, bodies sent, status, type.

    Needs network_start(capture_bodies=True), the default. Timings and response bodies are
    not captured. The file holds real header values, so it is written mode 600 and only its
    path is returned.
    """
    try:
        entries = list((BrowserState.network_index or {}).values())
        if not entries:
            return err(f"no indexed requests to export ({len(BrowserState.network_logs)} raw "
                       f"events). Call network_start(capture_bodies=True) before the traffic.")
        ensure_dirs()
        path = confined_output_path(EXPORT_DIR, filename or ts_filename("traffic", "har"))
        document = json.dumps({
            "log": {
                "version": "1.2",
                "creator": {"name": "mcp-stealth-chrome", "version": __version__},
                "entries": [_har_entry(e) for e in entries],
            }
        }, indent=2, default=str)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(document)
        return ok(f"{path}\n{len(entries)} request(s), mode 600: holds real header values")
    except Exception as e:
        return err(str(e))
