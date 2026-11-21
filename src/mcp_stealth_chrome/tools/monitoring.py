"""Console and network capture: console_start, console_get, network_start and network_get,
with the body keeping that goes with network capture.

Moved out of server.py unchanged. The capture state (armed tabs, the buffers, the set of kept
bodies, the document loaders) stays in server.py and is reached here as _srv.<name> at call time,
so a test that patches server.<name> still reaches the moved code. A body is masked on the way
out (helpers.mask_body) unless the caller asks with reveal_values="body".
"""

import asyncio
import contextlib
import json
from typing import Any, Literal, Optional, Union

from nodriver.cdp import runtime as cdp_runtime

from .._app import mcp
from ..helpers import cut_url, err, mask_body, mask_request_entry, reveal_mode, ok
from ..state import BrowserState
from .. import server as _srv


def _console_target_key(tab) -> str:
    """Deprecated alias for _target_key — kept so no call site silently diverges."""
    return _srv._target_key(tab)


@mcp.tool()
async def console_start() -> str:
    """Begin capturing console messages + uncaught exceptions of the active tab.

    Arms the listener on THIS tab only. Call it again after switching or opening
    a tab. The Runtime domain is enabled explicitly and re-enabled on every
    navigation, so capture survives page loads instead of silently lapsing.
    """
    try:
        tab = BrowserState.active_tab()
        BrowserState.console_logs.clear()
        BrowserState.page_errors.clear()
        BrowserState.capture_console = True
        _srv._CONSOLE_STARTED_AT = _srv._now_iso()
        tid = _srv._target_key(tab)
        from nodriver.cdp import page as cdp_page

        if tid in _srv._CONSOLE_ARMED_TAB_IDS:
            # Handler is still bound, but the domain can lapse — re-enabling is
            # idempotent and cheap, and it is what makes a re-call actually help.
            try:
                await tab.send(cdp_runtime.enable())
            except Exception as exc:
                BrowserState.capture_console = False
                return err(f"console capture NOT re-armed, Runtime.enable failed: {_srv._exc_text(exc)}")
            return ok("console capture re-armed (handler already bound to this tab)")

        # Bind the sink NOW, to the instance being armed. Reading
        # BrowserState.console_logs at event time would append to whichever
        # instance happens to be current, so a switch mid-capture would file this
        # browser's messages under another one — and stop filling this one.
        sink = BrowserState.console_logs
        error_sink = BrowserState.page_errors

        async def handle(event):
            try:
                # console.clear() is an instruction, not a message. Kept, console_clear
                # would leave its own "clear" behind in the buffer it just emptied.
                if str(getattr(event, "type_", "")) == "clear":
                    return
                args = []
                for a in (event.args or []):
                    val = getattr(a, "value", None)
                    # `or` would drop console.log(0)/false/'' — keep falsy-but-real
                    # values, fall back to description only when value is absent.
                    if val is not None:
                        args.append(val)
                    else:
                        args.append(getattr(a, "description", "") or "")
                sink.append({
                    "type": event.type_,
                    "text": " ".join(str(a) for a in args)[:500],
                })
                _srv._trim_capture(sink)
            except Exception:
                pass

        async def handle_exception(event):
            # A page can fail hard with zero console.log calls — without this,
            # console_get() would report "0 messages" on a page that threw.
            try:
                d = getattr(event, "exception_details", None)
                text = getattr(d, "text", "") or ""
                exc = getattr(d, "exception", None)
                desc = getattr(exc, "description", None) if exc else None
                message = (desc or text or "uncaught exception")[:500]
                sink.append({"type": "exception", "text": message})
                _srv._trim_capture(sink)
                error_sink.append({"text": message, "url": getattr(d, "url", None),
                                   "line": getattr(d, "line_number", None)})
                _srv._trim_capture(error_sink)
            except Exception:
                pass

        async def on_navigated(event):
            # Execution contexts are torn down on navigation; re-enable so the
            # listener keeps receiving events on the new document.
            try:
                await tab.send(cdp_runtime.enable())
            except Exception:
                pass

        tab.add_handler(cdp_runtime.ConsoleAPICalled, handle)
        tab.add_handler(cdp_runtime.ExceptionThrown, handle_exception)
        with contextlib.suppress(Exception):
            await tab.send(cdp_page.enable())
        tab.add_handler(cdp_page.FrameNavigated, on_navigated)
        # After the handlers, so the messages Chrome replays on enable are not missed.
        try:
            await tab.send(cdp_runtime.enable())
        except Exception as exc:
            # No events will come. Claiming attached here makes an empty list read as a
            # quiet page.
            _srv._drop_handlers(tab, [(cdp_runtime.ConsoleAPICalled, handle),
                                 (cdp_runtime.ExceptionThrown, handle_exception),
                                 (cdp_page.FrameNavigated, on_navigated)])
            BrowserState.capture_console = False
            return err(f"console capture NOT started, Runtime.enable failed: {_srv._exc_text(exc)}")
        _srv._CONSOLE_ARMED_TAB_IDS.add(tid)
        return ok("console capture started (console + uncaught exceptions)")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def console_get(limit: int = 100) -> str:
    """Retrieve captured console messages WITH the capture status.

    Returns {attached, capturing, since, total, returned, messages} rather than a
    bare array, because an empty array alone is ambiguous: it cannot tell you
    whether the page logged nothing or nothing was ever listening. `attached`
    false + empty means NOT MONITORED — not "no errors".

    Messages are chronological (oldest first within the last `limit`).
    """
    if limit < 0:
        return err("limit must be >= 0")
    tab = None
    try:
        tab = BrowserState.active_tab()
    except Exception:
        pass
    armed = tab is not None and _srv._target_key(tab) in _srv._CONSOLE_ARMED_TAB_IDS
    logs = BrowserState.console_logs[-limit:] if limit else []
    payload: dict[str, Any] = {
        "attached": armed,
        "capturing": bool(BrowserState.capture_console),
        "since": _srv._CONSOLE_STARTED_AT,
        "total": len(BrowserState.console_logs),
        "returned": len(logs),
        "messages": logs,
    }
    if not (armed and BrowserState.capture_console):
        payload["note"] = (
            "NOT listening on the active tab — an empty 'messages' here means "
            "'nothing was monitored', not 'no errors'. Call console_start() "
            "(re-call it after switching or opening a tab)."
        )
    return ok(json.dumps(payload, indent=2, default=str))


def _cdp_raw(method: str, params: Optional[dict] = None):
    """A CDP command nodriver has no wrapper for (Network.configureDurableMessages is newer)."""
    response = yield {"method": method, "params": params or {}}
    return response


# Bumped by every network_start: a network_get still waiting on Chrome when the capture is
# restarted must not hand back a body from before the restart.
_NETWORK_GENERATION = [0]


def _network_restarted() -> None:
    _NETWORK_GENERATION[0] += 1


_KEPT_COST = (f"bodies stay in Chrome's memory for this tab until it closes, up to "
              f"{_srv.CAPTURE_BODY_BUFFER_BYTES // 2**20} MB in all (oldest dropped first, one body up "
              f"to {_srv.CAPTURE_BODY_MAX_BYTES // 2**20} MB); no command frees them earlier (measured, "
              "Chrome 157), and after a later network_start no call can look them up, though a "
              "network_get already running is stopped rather than finished. Loading 300 fetches "
              "measured no slower with them kept")


async def _configure_kept_bodies(tab: Any, keep: bool, tid: str) -> str:
    """Words for whether Chrome keeps response bodies past a navigation, after setting it.

    Opt-in (keep_bodies=True): a plain network_start sends what origin/main sent and keeps
    nothing. Measured on Chrome 157: Network.configureDurableMessages with sizes turns keeping
    on and answers, with no sizes it stops keeping new bodies, and no command drops bodies
    already kept (not configureDurableMessages({}), smaller sizes, nor Network.disable):
    Chrome holds them for this tab's DevTools session. Another session cannot read them.
    """
    from nodriver.cdp import network as cdp_network
    earlier = ""
    if tid in _srv._NETWORK_KEPT_BODIES:
        earlier = ("; bodies Chrome kept before this call stay in its memory until the tab "
                   "closes, and later calls cannot look up the earlier requests (their ids are "
                   "cleared)")
    not_kept = ("; response bodies are not kept past a navigation: read one before the page "
                "moves on, or call network_start(keep_bodies=True) to keep them")
    if not keep:
        if tid in _srv._NETWORK_KEPT_BODIES:
            # Stop keeping new ones; the old ones cannot be dropped (see above).
            with contextlib.suppress(Exception):
                await tab.send(_cdp_raw("Network.configureDurableMessages", {}))
            _srv._NETWORK_KEPT_BODIES.discard(tid)
        return not_kept + earlier
    try:
        await tab.send(_cdp_raw("Network.configureDurableMessages", {
            "maxTotalBufferSize": _srv.CAPTURE_BODY_BUFFER_BYTES,
            "maxResourceBufferSize": _srv.CAPTURE_BODY_MAX_BYTES}))
    except Exception as exc:
        try:
            await tab.send(cdp_network.enable(
                max_total_buffer_size=_srv.CAPTURE_BODY_BUFFER_BYTES,
                max_resource_buffer_size=_srv.CAPTURE_BODY_MAX_BYTES, enable_durable_messages=True))
        except Exception as exc2:
            return (f"; bodies are NOT kept past a navigation ({_srv._exc_text(exc2)}): read them "
                    f"before the page moves on{earlier}")
        _srv._NETWORK_KEPT_BODIES.add(tid)
        return (f"; asked Chrome to keep response bodies past a navigation, but this Chrome did "
                f"not confirm it ({_srv._exc_text(exc)}); if it does keep them, {_KEPT_COST}{earlier}")
    _srv._NETWORK_KEPT_BODIES.add(tid)
    return f"; Chrome keeps response bodies past a navigation: {_KEPT_COST}{earlier}"


@mcp.tool()
async def network_start(capture_bodies: bool = True, keep_bodies: bool = False) -> str:
    """Begin capturing network requests + responses with full headers.

    Args:
        capture_bodies: if True (default), also indexes by request_id so
            network_get(include_body=True) can fetch response bodies on
            demand via CDP Network.getResponseBody.
        keep_bodies: have Chrome keep response bodies past a navigation, so a body can still
            be read after the page moved on (a page that navigates as soon as its fetch
            returns). Off by default: Chrome then drops a body with its document. The cost:
            kept bodies stay in Chrome's memory for this tab until it closes, up to
            CAPTURE_BODY_BUFFER_BYTES (64 MB), and nothing frees them earlier.
    """
    if keep_bodies and not capture_bodies:
        return err("network_start: keep_bodies=True needs capture_bodies=True, which is what "
                   "lets network_get read a body; nothing was started")
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        tid = _srv._target_key(tab)
        keep = bool(keep_bodies) and _srv.CAPTURE_BODY_BUFFER_BYTES > 0
        # Network.enable so events arrive at all, and so getResponseBody works later. If it
        # fails nothing is captured, and an empty log must not read as a quiet page.
        try:
            if keep:
                await tab.send(cdp_network.enable(
                    max_total_buffer_size=_srv.CAPTURE_BODY_BUFFER_BYTES,
                    max_resource_buffer_size=_srv.CAPTURE_BODY_MAX_BYTES))
            else:
                await tab.send(cdp_network.enable())
        except Exception as exc:
            BrowserState.capture_network = False
            return err(f"network capture NOT started, Network.enable failed: {_srv._exc_text(exc)}")
        kept_note = (await _configure_kept_bodies(tab, keep, tid)) if capture_bodies else ""
        if keep_bodies and not keep:
            kept_note = "; keep_bodies=True not applied: CAPTURE_BODY_BUFFER_BYTES is 0" + kept_note
        _network_restarted()
        BrowserState.network_logs.clear()
        BrowserState.network_index.clear()
        BrowserState.capture_network = True
        # The handler is installed once per target, but this option is mutable:
        # a later network_start(capture_bodies=True) must change what that
        # existing handler indexes instead of merely claiming that it did.
        _srv._NETWORK_CAPTURE_BODIES[tid] = bool(capture_bodies)

        # Bound now, to the instance being armed — see console_start for why
        # reading the class attribute at event time cross-files the capture.
        log_sink = BrowserState.network_logs
        index_sink = BrowserState.network_index

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
                    "resource_type": _srv._resource_type(event),
                    "_loader": str(getattr(event, "loader_id", "") or ""),
                    "request_headers": req_headers,
                    "request_body": getattr(req, "post_data", None),
                    "status": None,
                    "response_headers": None,
                    "mime": None,
                    "size": None,
                    "timestamp": getattr(event, "timestamp", None),
                    "wall_time": getattr(event, "wall_time", None),
                }
                if (entry["resource_type"] == "Document" and entry["_loader"]
                        and str(getattr(event, "frame_id", "") or "") == tid):
                    _srv._NETWORK_DOC_LOADER[tid] = entry["_loader"]
                if _srv._NETWORK_CAPTURE_BODIES.get(tid, False):
                    index_sink[rid] = entry
                    _srv._trim_index(index_sink)
                log_sink.append({
                    "type": "request", "request_id": rid,
                    "url": entry["url"], "method": entry["method"],
                    "resource_type": entry["resource_type"],
                })
                _srv._trim_capture(log_sink)
            except Exception:
                pass

        async def on_res(event):
            try:
                rid = str(event.request_id)
                resp = event.response
                resp_headers = dict(getattr(resp, "headers", None) or {})
                if rid in index_sink:
                    e = index_sink[rid]
                    e["status"] = getattr(resp, "status", None)
                    e["response_headers"] = resp_headers
                    e["mime"] = getattr(resp, "mime_type", None)
                    e["size"] = getattr(resp, "encoded_data_length", None)
                log_sink.append({
                    "type": "response", "request_id": rid,
                    "url": getattr(resp, "url", ""),
                    "status": getattr(resp, "status", None),
                    "mime": getattr(resp, "mime_type", None),
                    "resource_type": _srv._resource_type(event),
                })
                _srv._trim_capture(log_sink)
            except Exception:
                pass

        if tid in _srv._NETWORK_ARMED_TAB_IDS:
            return ok(
                f"network capture started (capture_bodies={capture_bodies}); "
                f"handler already armed on this tab{kept_note}"
            )
        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        tab.add_handler(cdp_network.ResponseReceived, on_res)
        _srv._NETWORK_ARMED_TAB_IDS.add(tid)
        return ok(f"network capture started (capture_bodies={capture_bodies}){kept_note}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def network_get(
    limit: int = 100,
    filter_url: Optional[str] = None,
    include_body: bool = False,
    max_body_bytes: int = 10000,
    full: bool = False,
    reveal_values: Union[bool, Literal["body"]] = False,
    resource_type: Optional[str] = None,
    max_url_len: int = 200,
) -> str:
    """Retrieve captured network events.

    Args:
        limit: max entries returned (chronological: oldest first within the last `limit`, newest last)
        filter_url: substring filter on URL
        include_body: return response bodies, truncated to max_body_bytes. network_start
            asks Chrome to keep bodies outside the page (CAPTURE_BODY_BUFFER_BYTES, 64 MB in
            all, the oldest dropped first; one body up to CAPTURE_BODY_MAX_BYTES, 10 MB), so
            they can still be read after the page navigated away. Requires
            network_start(capture_bodies=True).
        max_body_bytes: cap per-body length (default 10000)
        full: if True, return entries with full headers + body fields
            from network_index (use this once you've called network_start).
            Default False = legacy flat event stream (backward compat).
        reveal_values: True returns credential headers, fields and query parameters as
            they are. By default their values read "*** (N chars)", because tool output
            lands in the transcript. "body" shows body values under a secret-named parent
            (a token vault's {"gatewayTokens": [...]}) and keeps credential headers, URL
            secrets and every body value whose own key is secret-named hidden; the reply is
            then {reveal_values, note, entries}. Bodies other than JSON or forms cannot be
            read field by field and pass through.
        resource_type: only these CDP resource types, comma separated, any case:
            "xhr,fetch" leaves out documents, scripts, styles, images and fonts.
        max_url_len: URLs longer than this are cut, and the entry says by how much
            (`url_cut`). 0 returns every URL whole.
    """
    if limit < 0:
        return err("limit must be >= 0")
    if max_body_bytes < 0:
        return err("max_body_bytes must be >= 0")
    if max_url_len < 0:
        return err("max_url_len must be >= 0 (0 = whole URLs)")
    try:
        reveal = reveal_mode(reveal_values)
    except ValueError as exc:
        return err(f"network_get: {exc}; nothing was read")
    body_mode = reveal == "body"
    extra = _srv._BODY_MODE_EXTRA if body_mode else None
    if not (BrowserState.capture_network or BrowserState.network_logs or BrowserState.network_index):
        return err("NOT listening: call network_start() before the traffic. An empty list "
                   "would read as a quiet page when nothing was monitored.")
    wanted = None
    if resource_type:
        wanted = {part.strip().lower() for part in str(resource_type).split(",") if part.strip()}

    def keep(entry: dict) -> bool:
        if filter_url and filter_url not in (entry.get("url") or ""):
            return False
        return wanted is None or str(entry.get("resource_type") or "").lower() in wanted

    if full or include_body:
        # Work on copies: response bodies are fetched on demand, and caching a
        # truncated/error sentinel in network_index made a later call with a
        # larger limit permanently unable to retrieve the real body.
        entries = [dict(entry) for entry in BrowserState.network_index.values() if keep(entry)]
        entries = entries[-limit:] if limit else []
        generation = _NETWORK_GENERATION[0]
        if include_body and entries:
            try:
                tab = BrowserState.active_tab()
            except Exception as e:
                return err(f"response body capture unavailable: {e}")
            from nodriver.cdp import network as cdp_network
            tid = _srv._target_key(tab)
            for e in entries:
                rid = e.get("request_id")
                if not rid:
                    e["response_body"] = "<missing request_id>"
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
                    if reveal != "all":
                        body_str = mask_body(body_str, own_keys_only=body_mode)
                    orig_len = len(body_str)
                    if orig_len > max_body_bytes:
                        body_str = body_str[:max_body_bytes] + (
                            f"... [truncated, original {orig_len} chars]"
                        )
                    e["response_body"] = body_str
                except asyncio.TimeoutError:
                    e["response_body"] = "<getResponseBody timeout>"
                except Exception as ex:
                    e["response_body"] = _lost_body_text(ex, e, tid)
                if _NETWORK_GENERATION[0] != generation:
                    # network_start cleared the capture while this call waited on Chrome: a
                    # body read from before it is not handed back.
                    return err("network_get: network capture was restarted while this call ran; "
                               "nothing is returned, so no body from before the restart leaves. "
                               "Call network_get again")
        for e in entries:
            e.pop("_loader", None)
        if reveal != "all":
            entries = [mask_request_entry(e, body_mode=body_mode) for e in entries]
        entries = [cut_url(e, max_url_len) for e in entries]
        return ok(_srv._bounded_entries(entries, extra))
    # Legacy event-stream view
    logs = [l for l in BrowserState.network_logs if keep(l)]
    selected = logs[-limit:] if limit else []
    if reveal != "all":
        selected = [mask_request_entry(l, body_mode=body_mode) for l in selected]
    selected = [cut_url(l, max_url_len) for l in selected]
    return ok(_srv._bounded_entries(selected, extra))


def _lost_body_text(exc: Exception, entry: dict, tid: str) -> str:
    """What network_get says for a body Chrome could not give. A body whose document is gone
    says so, and how to keep one, instead of the raw "No resource with given identifier"."""
    text = str(exc)
    loader = entry.get("_loader")
    current = _srv._NETWORK_DOC_LOADER.get(tid)
    if "No resource with given identifier" in text and loader and current and loader != current:
        if tid in _srv._NETWORK_KEPT_BODIES:
            return ("<body lost although bodies are kept: the page navigated and Chrome no longer "
                    "has it (over CAPTURE_BODY_MAX_BYTES, or pushed out of CAPTURE_BODY_BUFFER_BYTES)>")
        return ("<body lost: the page navigated after this response and Chrome drops a body with "
                "its document; network_start(keep_bodies=True) keeps bodies past a navigation>")
    return f"<getResponseBody error: {text}>"
