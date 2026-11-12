"""A real MCP client for the tests: stdio, session.call_tool, the server in its own process.

A fake proves only the call shape. Several tools failed on every call while tests that called
the function directly stayed green, because FastMCP's argument parsing and the client's output
validation only run over the protocol.

MCP_TEST_SRC and MCP_TEST_SERVER_PYTHON point the server at another source tree or interpreter,
which is how the suite runs against both supported mcp and nodriver versions.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import sys
import threading
from dataclasses import dataclass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = pathlib.Path(__file__).resolve().parent.parent
LOOPBACK_ONLY = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"
SAFE_ARGS = ["--use-mock-keychain", LOOPBACK_ONLY]


@dataclass
class Reply:
    text: str
    is_error: bool
    kinds: list
    structured: object
    seconds: float

    @property
    def failed(self) -> bool:
        return self.is_error or self.text.startswith("Error")

    def json(self):
        return json.loads(self.text)


class Client:
    def __init__(self, env: dict | None = None, python: str | None = None):
        self._env = env
        self._python = python
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._crash: BaseException | None = None
        self.session: ClientSession | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(60) or self._crash:
            raise RuntimeError(f"server did not start: {self._crash}")

    def _run(self):
        self._loop.run_until_complete(self._main())

    async def _main(self):
        self._stop = asyncio.Event()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.environ.get("MCP_TEST_SRC", str(REPO / "src"))
        env.update(self._env or {})
        params = StdioServerParameters(
            command=self._python or os.environ.get("MCP_TEST_SERVER_PYTHON", sys.executable),
            args=["-c", "from mcp_stealth_chrome.server import main; main()"],
            env=env,
        )
        try:
            with open(os.devnull, "w") as quiet:
                async with stdio_client(params, errlog=quiet) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self.session = session
                        self._ready.set()
                        await self._stop.wait()
        except BaseException as exc:  # surfaced to the constructor
            self._crash = exc
            self._ready.set()

    def call(self, name: str, /, _timeout: float = 60, **arguments) -> Reply:
        async def go():
            started = asyncio.get_running_loop().time()
            res = await self.session.call_tool(
                name, arguments, read_timeout_seconds=datetime.timedelta(seconds=_timeout))
            text = "\n".join(c.text if hasattr(c, "text") else f"<{c.type}>" for c in res.content)
            return Reply(text, bool(res.isError), [c.type for c in res.content],
                         res.structuredContent, asyncio.get_running_loop().time() - started)
        return asyncio.run_coroutine_threadsafe(go(), self._loop).result(_timeout + 5)

    def evaluate(self, expression: str, **kw) -> str:
        return self.call("evaluate", expression=expression, **kw).text

    def close(self):
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(15)


# Headed launches run from Chrome Beta with the project icon (macOS). The suite turns it off for
# the servers it starts; a test about it passes this.
DOCK_ICON_ON = {"MCP_STEALTH_DOCK_ICON": "1"}
