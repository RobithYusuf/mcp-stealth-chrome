"""Single FastMCP instance shared between server.py and tools.* submodules.

Extracted so tools/* can register `@mcp.tool()` decorators without creating
a circular import with server.py."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stealth-chrome")
