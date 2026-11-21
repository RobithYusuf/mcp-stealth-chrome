"""JavaScript tools: evaluate, wait_for_function and inject_init_script, and the helper that turns a
page value into text.

Moved out of server.py. _js_exception_text stays there because _eval_checked and paste_text use it;
this module reaches it as _srv._js_exception_text at call time, so a test that patches
server.<name> still reaches the code.
"""

import asyncio
import contextlib
import json
import time
from typing import Any, Optional

from nodriver.cdp import runtime as cdp_runtime

from .. import server as _srv
from .._app import mcp
from ..helpers import err, ok, parse_json, select_tab
from ..state import BrowserState


def _remote_value_text(remote: Any) -> str:
    """The result of Runtime.evaluate as text. JSON for anything JSON can say."""
    value = getattr(remote, "value", None)
    if value is not None:
        return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    if getattr(remote, "unserializable_value", None) is not None:  # NaN, Infinity, 5n
        return str(remote.unserializable_value)
    kind = str(getattr(getattr(remote, "type_", None), "value", getattr(remote, "type_", "")))
    if kind == "undefined":
        return "undefined"
    if getattr(remote, "subtype", None) == "null" or kind in ("", "object") and not remote.description:
        return "null"
    return str(remote.description)  # a function or symbol has a description, not a value


@mcp.tool()
async def evaluate(
    expression: str,
    await_promise: bool = True,
    timeout_ms: int = 30000,
    survive_navigation: bool = False,
    tab: Optional[int] = None,
) -> str:
    """Execute JavaScript with an explicit bounded timeout.

    ``survive_navigation`` explicitly retries once after a navigation interruption;
    callers must only use it for idempotent expressions because arbitrary JavaScript
    may have side effects before navigation starts.
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 600_000:
        return err("timeout_ms must be an integer between 1 and 600000")
    page = None
    try:
        page = select_tab(tab)
        with contextlib.suppress(Exception):
            await _srv._watch_dialogs(page)
        attempts = 2 if survive_navigation else 1
        for attempt in range(attempts):
            try:
                # Not Tab.evaluate: it also asks for deep serialization, and then Chrome
                # leaves `value` empty for any object or array, which read as null.
                remote, thrown = await _srv._wait(
                    page.send(cdp_runtime.evaluate(
                        expression=expression, user_gesture=True, await_promise=await_promise,
                        return_by_value=True, allow_unsafe_eval_blocked_by_csp=True,
                    )),
                    timeout=timeout_ms / 1000,
                    what="evaluate",
                )
                if thrown:
                    return err(f"JavaScript threw: {_srv._js_exception_text(thrown)}")
                return ok(_remote_value_text(remote))
            except RuntimeError as exc:
                message = str(exc).lower()
                interrupted = "interrupted by target navigation" in message
                if not interrupted or attempt + 1 >= attempts:
                    raise
                await _srv._wait(
                    page.evaluate("document.readyState === 'complete' || document.readyState === 'interactive'", return_by_value=True),
                    timeout=min(timeout_ms / 1000, 30),
                    what="navigation recovery",
                )
        return err("evaluate did not produce a result")
    except Exception as e:
        return err(str(e) + (_srv._dialog_note(page) if page is not None else ""))


@mcp.tool()
async def wait_for_function(
    expression: str,
    timeout_ms: int = 120000,
    poll_ms: int = 500,
    tab: Optional[int] = None,
) -> str:
    """Poll a JavaScript predicate and return its truthy value.

    The expression is evaluated in the selected tab without changing active focus.
    It must be a predicate or a side-effect-free value expression.
    """
    if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 600_000):
        return err("timeout_ms must be an integer between 1 and 600000")
    if (isinstance(poll_ms, bool) or not isinstance(poll_ms, int)
            or not 10 <= poll_ms <= 60_000):
        return err("poll_ms must be an integer between 10 and 60000")
    try:
        page = select_tab(tab)
        deadline = time.monotonic() + timeout_ms / 1000
        wrapped = ("(async () => { const value = await (" + expression + "); "
                   "return JSON.stringify({truthy: Boolean(value), value: value ?? null}); })()")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return err(f"wait_for_function timed out after {timeout_ms}ms")
            try:
                raw = await _srv._wait(
                    page.evaluate(wrapped, await_promise=True, return_by_value=True),
                    timeout=min(remaining, max(0.1, poll_ms / 1000)),
                    what="wait_for_function",
                )
            except TimeoutError:
                continue
            if isinstance(raw, cdp_runtime.ExceptionDetails):
                return err(f"wait_for_function: the predicate threw: {_srv._js_exception_text(raw)}")
            raw = raw.value if hasattr(raw, "value") and not isinstance(raw, (str, dict)) else raw
            state = parse_json(raw)
            if not isinstance(state, dict):
                return err("wait_for_function predicate returned an unserializable value")
            if state.get("truthy"):
                value = state.get("value")
                return ok(json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else str(value))
            await asyncio.sleep(min(poll_ms / 1000, max(0, deadline - time.monotonic())))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def inject_init_script(script: str) -> str:
    """Register a script that runs before page scripts on every navigation of
    the CURRENT tab. Scope is the active tab/target only — it is NOT auto-applied
    to other open tabs or to tabs opened later; re-run per tab if needed."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import page as cdp_page
        # Without the Page domain the script registers and then never runs.
        await tab.send(cdp_page.enable())
        await tab.send(cdp_page.add_script_to_evaluate_on_new_document(source=script))
        return ok("init script registered")
    except Exception as e:
        return err(str(e))
