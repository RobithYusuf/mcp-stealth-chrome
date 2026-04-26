"""mcp-stealth-chrome — Powerful stealth browser MCP server.

Built on nodriver (CDP direct, no WebDriver) + FastMCP.
Tool-parity with mcp-camoufox (Node/Firefox sister package).

Differentiators vs existing Python stealth MCPs:
- storage_state save/load (session reuse bypasses Cloudflare/Turnstile entirely)
- CapSolver integration (Turnstile solver)
- Fingerprint rotation tool
- Humanize click/type (Bezier, Gaussian)
- Auto Cloudflare verify via nodriver's verify_cf
"""

__version__ = "0.2.4"
