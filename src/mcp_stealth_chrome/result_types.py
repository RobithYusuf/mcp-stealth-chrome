"""Typed MCP result envelopes that preserve the server's string tool contract."""
from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent


class StructuredResult(str):
    """A JSON string that the MCP boundary exposes as structuredContent."""

    def __new__(cls, text: str, data: dict[str, Any]):
        value = super().__new__(cls, text)
        value.data = data
        return value

    data: dict[str, Any]

    def to_call_tool_result(self) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=str(self))],
            structuredContent=self.data,
        )


def structured_json(data: dict[str, Any]) -> StructuredResult:
    return StructuredResult(json.dumps(data, indent=2, default=str), data)
