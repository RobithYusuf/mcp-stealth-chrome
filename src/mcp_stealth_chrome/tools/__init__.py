"""Tool packages — split out of server.py for editor / diff sanity.

Each submodule registers its tools on the shared `mcp` instance from
mcp_stealth_chrome._app. server.py imports each submodule once at the
end of its module init so all decorators run.

DO NOT add `from .X import *` here — let server.py import each module
explicitly. That keeps the import graph (and therefore the tool
registration order) predictable."""
