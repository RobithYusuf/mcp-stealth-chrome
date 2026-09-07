"""Typed MCP result envelopes that preserve the server's string tool contract."""
from __future__ import annotations

import base64
import json
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent


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


class ImageResult(str):
    """Compact image metadata, optionally converted to MCP ImageContent."""

    def __new__(cls, text: str, *, mode: str, path: str | None = None,
                data: bytes | None = None, mime_type: str = "image/png"):
        value = super().__new__(cls, text)
        value.mode = mode
        value.path = path
        value.data = data
        value.mime_type = mime_type
        return value

    mode: str
    path: str | None
    data: bytes | None
    mime_type: str

    def to_call_tool_result(self) -> CallToolResult:
        blocks = [TextContent(type="text", text=str(self))]
        if self.mode == "inline_image" and self.data is not None:
            blocks.append(ImageContent(
                type="image",
                data=base64.b64encode(self.data).decode("ascii"),
                mimeType=self.mime_type,
            ))
        return CallToolResult(content=blocks)


def structured_json(data: dict[str, Any]) -> StructuredResult:
    return StructuredResult(json.dumps(data, indent=2, default=str), data)
