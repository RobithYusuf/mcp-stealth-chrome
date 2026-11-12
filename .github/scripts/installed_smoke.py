"""Checks a lock-free install of the built wheel, the way a PyPI user gets it.

uv.lock hides a dependency that breaks at import (mcp 2.0 removed FastMCP and every fresh
install died while the locked suite stayed green). This runs in a venv made from the wheel
alone: it reads the wheel's metadata for the caps, starts the installed console script over
stdio, and calls server_status through a real MCP client.

    python installed_smoke.py dist/mcp_stealth_chrome-X-py3-none-any.whl
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path


def check_caps(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        meta = zf.read(name).decode()
    requires = [line.split(":", 1)[1].strip() for line in meta.splitlines()
                if line.startswith("Requires-Dist:")]
    version = next(line.split(":", 1)[1].strip() for line in meta.splitlines()
                   if line.startswith("Version:"))
    for package, cap in (("mcp", "<2"), ("nodriver", "<0.51")):
        found = [r for r in requires if re.match(rf"{package}\b", r) and "extra ==" not in r]
        if len(found) != 1 or cap not in found[0].replace(" ", ""):
            sys.exit(f"Requires-Dist for {package} lost its {cap} cap: {found}")
        print(f"Requires-Dist: {found[0]}")
    return version


async def call_server_status(script: str, env: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=script, args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            reply = await session.call_tool("server_status", {})
            if reply.isError:
                sys.exit(f"server_status failed: {reply.content}")
            text = "".join(getattr(c, "text", "") for c in reply.content)
            return {"tools": len(tools.tools), "status": json.loads(text)}


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    version = check_caps(wheel)
    installed = metadata.version("mcp-stealth-chrome")
    if installed != version:
        sys.exit(f"venv holds {installed}, the wheel is {version}")
    for package in ("mcp", "nodriver"):
        print(f"resolved {package}=={metadata.version(package)}")

    scripts = Path(sysconfig.get_path("scripts"))
    script = shutil.which("mcp-stealth-chrome", path=str(scripts))
    if not script:
        sys.exit(f"console script mcp-stealth-chrome is missing from {scripts}")

    data = tempfile.mkdtemp(prefix="mcp-stealth-smoke-")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "MCP_STEALTH_DATA_DIR": data,
        "MCP_STEALTH_PROFILE_GC": "0",
        "MCP_STEALTH_DOCK_ICON": "0",
        "MCP_STEALTH_APP_DIR": os.path.join(data, "app"),
    })
    try:
        result = asyncio.run(asyncio.wait_for(call_server_status(script, env), 120))
    finally:
        shutil.rmtree(data, ignore_errors=True)
    status = result["status"]
    print(f"tools advertised: {result['tools']}")
    print(f"server_status version={status.get('version')} "
          f"compat_patches={json.dumps(status.get('compat_patches'))}")
    if status.get("version") != version:
        sys.exit(f"the server reports {status.get('version')}, the wheel is {version}")
    if not status.get("compat_patches", {}).get("healthy"):
        sys.exit("a compatibility patch failed to apply on the resolved versions")
    if result["tools"] < 100:
        sys.exit(f"only {result['tools']} tools registered")
    print("installed wheel OK")


if __name__ == "__main__":
    main()
