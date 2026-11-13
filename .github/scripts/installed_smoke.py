"""Checks a lock-free install of the built wheel, the way a PyPI user gets it.

uv.lock hides a dependency that breaks at import (mcp 2.0 removed FastMCP and every fresh
install died while the locked suite stayed green). This runs in a venv made from the wheel
alone: it reads the wheel's metadata for the caps, starts the installed console script over
stdio, and calls server_status through a real MCP client.

    python installed_smoke.py dist/mcp_stealth_chrome-X-py3-none-any.whl [--with-adblock]

Without --with-adblock it is the plain install: the adblock package must NOT be there (it is an
optional extra, since it has no wheel for musllinux or Windows ARM64), a plain launch must work,
and block_ads=True must launch too and say it is OFF and how to install the extra. With
--with-adblock (the wheel installed as [adblock]) it checks the compiled engine instead.
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
    adblock = [r for r in requires if re.match(r"adblock\b", r)]
    if len(adblock) != 1 or "<0.7" not in adblock[0].replace(" ", "") or "extra ==" not in adblock[0]:
        sys.exit(f"adblock must be the optional extra 'adblock', capped <0.7: {adblock}")
    print(f"Requires-Dist: {adblock[0]}")
    return version


HINTS = ("uv tool install 'mcp-stealth-chrome[adblock]'", "pip install 'mcp-stealth-chrome[adblock]'")


async def call_server_status(script: str, env: dict, launch: bool) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=script, args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

            async def call(name: str, **arguments) -> str:
                reply = await session.call_tool(name, arguments)
                text = "".join(getattr(c, "text", "") for c in reply.content)
                if reply.isError or text.startswith("Error"):
                    sys.exit(f"{name} failed: {text[:2000]}")
                return text

            out = {"tools": len(tools.tools), "status": json.loads(await call("server_status"))}
            if launch:
                plain = dict(url="about:blank", headless=True, persistent=False, auto_verify=False)
                out["plain"] = await call("browser_launch", **plain)
                await call("browser_close")
                out["with_block_ads"] = await call("browser_launch", block_ads=True, **plain)
                out["status_after"] = json.loads(await call("server_status"))
                await call("browser_close")
            return out


def check_engine() -> None:
    """block_ads needs the compiled engine on this OS; a missing wheel only shows at launch."""
    print(f"resolved adblock=={metadata.version('adblock')}")
    import adblock
    filters = adblock.FilterSet()
    filters.add_filter_list("[Adblock Plus 2.0]\n||ads.example^\n", rule_types="networkonly")
    engine = adblock.Engine(filters, optimize=True)
    if not engine.check_network_urls("https://ads.example/a.js", "https://news.example/", "script").matched:
        sys.exit("the adblock engine did not block a listed URL")
    print("adblock engine OK")


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    with_adblock = "--with-adblock" in sys.argv[2:]
    version = check_caps(wheel)
    installed = metadata.version("mcp-stealth-chrome")
    if installed != version:
        sys.exit(f"venv holds {installed}, the wheel is {version}")
    for package in ("mcp", "nodriver"):
        print(f"resolved {package}=={metadata.version(package)}")
    import importlib.util
    if with_adblock:
        check_engine()
    elif importlib.util.find_spec("adblock") is not None:
        sys.exit("the adblock package is installed without the [adblock] extra; it must be optional")
    else:
        print("adblock not installed, as a plain install has it")

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
        result = asyncio.run(asyncio.wait_for(
            call_server_status(script, env, launch=not with_adblock), 240))
    finally:
        shutil.rmtree(data, ignore_errors=True)
    status = result["status"]
    print(f"tools advertised: {result['tools']}")
    print(f"server_status version={status.get('version')} "
          f"compat_patches={json.dumps(status.get('compat_patches'))}")
    if status.get("version") != version:
        sys.exit(f"the server reports {status.get('version')}, the wheel is {version}")
    if "browsers" not in (status.get("block_ads") or {}):
        sys.exit(f"server_status has no block_ads report: {status.get('block_ads')}")
    if not status.get("compat_patches", {}).get("healthy"):
        sys.exit("a compatibility patch failed to apply on the resolved versions")
    if result["tools"] < 100:
        sys.exit(f"only {result['tools']} tools registered")
    if not with_adblock:
        package = (status.get("block_ads") or {}).get("package", "")
        if not all(h in package for h in HINTS):
            sys.exit(f"server_status does not say how to install the adblock extra: {package}")
        print(f"plain launch: {result['plain'].splitlines()[0][:160]}")
        blocked = result["with_block_ads"]
        if "block_ads: OFF" not in blocked or not all(h in blocked for h in HINTS):
            sys.exit(f"block_ads=True without the extra did not say OFF and how to install it: {blocked[:1500]}")
        print("block_ads=True without the extra: launched, OFF, install hint given")
        after = (result["status_after"].get("block_ads") or {}).get("package", "")
        if not all(h in after for h in HINTS):
            sys.exit(f"server_status after block_ads=True does not give the install hint: {after}")
    print("installed wheel OK")


if __name__ == "__main__":
    main()
