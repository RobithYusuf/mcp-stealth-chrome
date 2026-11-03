"""Single FastMCP instance shared between server.py and tools.* submodules."""
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .result_types import StructuredResult
from .state import BrowserState
from .helpers import target_key


_TAB_SERIALIZED_TOOLS = frozenset({
    "evaluate", "wait_for_function", "get_text", "screenshot",
    "screenshot_element", "inspect_image", "vision_describe", "vision_ocr", "vision_check",
})
_TAB_LOCKS: dict[str, asyncio.Lock] = {}


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
            result = await tool.run(arguments, context=context, convert_result=False)
            if isinstance(result, StructuredResult):
                return result.to_call_tool_result()
            from .image_output import ImageResult
            if isinstance(result, ImageResult):
                from mcp.types import CallToolResult
                return CallToolResult(content=result.to_mcp_content())
            return tool.fn_metadata.convert_result(result)
        finally:
            if lock is not None and lock.locked():
                lock.release()


mcp = StealthMCP("stealth-chrome")
