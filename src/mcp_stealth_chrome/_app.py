"""Single FastMCP instance shared between server.py and tools.* submodules."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .result_types import StructuredResult
from .state import BrowserState
from .helpers import OPEN_DIALOGS, dialog_note, target_key


_TAB_SERIALIZED_TOOLS = frozenset({
    "evaluate", "wait_for_function", "get_text", "screenshot",
    "screenshot_element", "inspect_image", "vision_describe", "vision_ocr", "vision_check",
})
_TAB_LOCKS: dict[str, asyncio.Lock] = {}

# These do not need the page's renderer, so an open native dialog cannot block them.
_RUNS_DESPITE_DIALOG = frozenset({
    "dialog_handle", "dialog_auto_handle", "browser_close", "browser_recover", "browser_launch",
    "spawn_browser", "attach_to_chrome", "detach", "close_instance", "close_all_instances",
    "switch_instance", "list_instances", "instance_describe", "instance_release", "server_status",
    "tab_list", "tab_new", "tab_select", "tab_close", "http_request", "http_request_status",
    "http_request_cancel", "http_request_retry", "http_request_with_session",
    "http_session_cookies", "list_chrome_profiles", "clone_chrome_profile", "list_external_chrome",
    "profile_report", "profile_cleanup", "identity_pin", "identity_show", "console_get",
    "get_page_errors", "network_get", "intercept_log", "intercept_stop", "export_har", "export_curl",
    "request_human_help", "wait_for_human", "web_search", "deep_research", "github_read", "youtube_read",
})
# Called with the tool's name after every tool call, inside the caller's session. server.py adds
# the saved-tabs writer here. Each must be cheap and must not await; one that raises is ignored.
AFTER_TOOL_HOOKS: list = []
# Long enough for a dialog_handle armed in advance to answer, and for an input tool to return
# its own better message. A dialog still open after this is blocking the tool.
_DIALOG_GRACE_SECONDS = 1.5


def _active_dialog_key() -> str | None:
    if not BrowserState.is_up():
        return None
    try:
        return target_key(BrowserState.active_tab())
    except Exception:
        return None


async def _run_unless_a_dialog_blocks(name: str, run) -> Any:
    """Stop a tool waiting on a page that a native dialog has frozen.

    Anything that needs the renderer waits for ever while a dialog is open, including calls
    with no timeout of their own, and the dialog can open before the call or during it.
    """
    if name in _RUNS_DESPITE_DIALOG:
        return await run()
    key = _active_dialog_key()
    if key in OPEN_DIALOGS:
        return f"Error: {name} did not run" + dialog_note(BrowserState.active_tab())
    task = asyncio.ensure_future(run())
    while not task.done():
        await asyncio.wait({task}, timeout=0.2)
        found = OPEN_DIALOGS.get(key) if key else None
        if (not task.done() and found
                and time.monotonic() - found.get("since", 0) >= _DIALOG_GRACE_SECONDS):
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
            return f"Error: {name} stopped" + dialog_note(BrowserState.active_tab())
    return task.result()


class StealthMCP(FastMCP):
    """FastMCP boundary preserving native content and rejecting tab races."""

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        context = self.get_context()
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return await super().call_tool(name, arguments)

        try:
            session = context.session
        except ValueError:  # called outside a request, as direct callers do
            session = None
        token = None
        if session is not None:
            request = getattr(context.request_context, "request", None)
            headers = getattr(request, "headers", None)
            http_id = headers.get("mcp-session-id") if headers is not None else None
            token = BrowserState.bind_session(session, http_id)
        try:
            return await self._call_bound(name, arguments, context, tool)
        finally:
            for hook in AFTER_TOOL_HOOKS:
                try:
                    hook(name)
                except Exception:
                    pass
            if token is not None:
                BrowserState.unbind_session(token)

    async def _call_bound(self, name: str, arguments: dict[str, Any], context: Any, tool: Any):
        lock = None
        if name in _TAB_SERIALIZED_TOOLS and BrowserState.is_up():
            index = arguments.get("tab")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(BrowserState.tabs):
                target = BrowserState.tabs[index]
            else:
                target = BrowserState.active_tab()
            key = target_key(target)
            lock = _TAB_LOCKS.setdefault(key, asyncio.Lock())
            if lock.locked():
                return tool.fn_metadata.convert_result(
                    f"Error: tab {key} is busy with another {name}-class operation; "
                    "wait for it to settle instead of racing the same CDP target."
                )
            await lock.acquire()

        try:
            result = await _run_unless_a_dialog_blocks(
                name, lambda: tool.run(arguments, context=context, convert_result=False))
            if isinstance(result, StructuredResult):
                return result.to_call_tool_result()
            from .image_output import ImageResult
            if isinstance(result, ImageResult):
                from mcp.types import CallToolResult
                # Every tool is annotated -> str, so FastMCP advertises an output schema
                # requiring {"result": str}. Official clients reject a reply without it.
                return CallToolResult(
                    content=result.to_mcp_content(), structuredContent={"result": str(result)},
                )
            return tool.fn_metadata.convert_result(result)
        finally:
            if lock is not None and lock.locked():
                lock.release()


mcp = StealthMCP("stealth-chrome")
