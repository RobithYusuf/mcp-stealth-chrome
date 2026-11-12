"""A new profile must not install Chrome's default apps. Needs MCP_E2E=1: a real Chrome, headless,
an ephemeral profile and the real HOME.

Each default app runs in its own renderer. On a temporary profile that was 12 processes and about
1.1 GB, against 10 and about 0.9 GB without them, and persistent=False pays it on every launch.
"""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

DEFAULT_APPS = {
    "ghbmnnjooekpmoecnnnilnnbdlolhkhi": "Google Docs Offline",
    "nmmhkkegccagdldgiimedpiccmgmieda": "Chrome Web Store Payments",
}


def test_ephemeral_launch_runs_no_default_apps():
    from nodriver.cdp import target as cdp_target

    from mcp_stealth_chrome import server

    tools = server.mcp._tool_manager._tools

    async def scenario():
        out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False)
        assert not out.startswith("Error"), out
        try:
            await asyncio.sleep(6)  # default apps install shortly after startup
            browser = server.BrowserState.browser
            infos = await (getattr(browser, "connection", None) or browser).send(cdp_target.get_targets())
            return {i.url.split("/")[2] for i in infos if i.url.startswith("chrome-extension://")}
        finally:
            await tools["browser_close"].fn()

    running = asyncio.run(scenario())
    assert not running & set(DEFAULT_APPS), [DEFAULT_APPS[x] for x in running & set(DEFAULT_APPS)]


@pytest.mark.skipif(sys.platform == "win32", reason="reads Chrome's command line with ps, which Windows lacks")
def test_testing_mode_keeps_nodrivers_disabled_features():
    """Chrome honours only the last --disable-features. testing_mode's own flag used to replace
    nodriver's, which turned site isolation back on in that mode only."""
    import subprocess

    from mcp_stealth_chrome import server

    tools = server.mcp._tool_manager._tools

    async def scenario():
        out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False,
                                               testing_mode=True)
        assert not out.startswith("Error"), out
        try:
            pid = server.BrowserState.browser._process_pid
            return subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
        finally:
            await tools["browser_close"].fn()

    args = asyncio.run(scenario()).split()
    last = [a for a in args if a.startswith("--disable-features=")][-1]
    features = set(last.split("=", 1)[1].split(","))
    assert {"IsolateOrigins", "site-per-process", "Translate", "BackForwardCache"} <= features, last


@pytest.mark.skipif(sys.platform.startswith("linux"), reason=(
    "Linux CI has no speech-dispatcher, so Chrome lists no voices at all and the audit rightly "
    "says it could not compare; the Google voices this checks are a macOS and Windows list."))
@pytest.mark.parametrize("mode, voices_pass", [("default", True), ("light", False)])
def test_fingerprint_audit_sees_missing_google_voices(mode, voices_pass):
    """Light mode turns off Chrome's component extensions, which takes the "Google" speech voices
    with them. Pages read that list, and the audit said "clean" while it was missing."""
    import json

    from mcp_stealth_chrome import server

    tools = server.mcp._tool_manager._tools

    async def scenario():
        out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False,
                                               profile_mode=mode)
        assert not out.startswith("Error"), out
        try:
            await asyncio.sleep(3)
            return json.loads(await tools["fingerprint_audit"].fn())
        finally:
            await tools["browser_close"].fn()

    report = asyncio.run(scenario())
    assert "speech_voices" in report["fingerprint"], report["not_checked"]
    flagged = any(w.startswith("speechSynthesis voices") for w in report["warnings"])
    assert ("speechSynthesis voices" in report["passed"]) is voices_pass, report
    assert flagged is not voices_pass, report["warnings"]


@pytest.mark.parametrize("extra_args, fits", [(None, True), (["--screen-info={800x600}"], False)])
def test_headless_window_fits_its_screen(extra_args, fits):
    """Headless Chrome reports an 800x600 screen whatever the window, so the default 1280x800
    window was larger than its screen and the audit said "clean"."""
    import json

    from mcp_stealth_chrome import server

    tools = server.mcp._tool_manager._tools

    async def scenario():
        out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False,
                                               extra_args=extra_args)
        assert not out.startswith("Error"), out
        try:
            sizes = await tools["evaluate"].fn(expression=(
                "[outerWidth, outerHeight, screen.width, screen.height, "
                "screen.availWidth, screen.availHeight, devicePixelRatio]"))
            return json.loads(await tools["fingerprint_audit"].fn()), json.loads(sizes)
        finally:
            await tools["browser_close"].fn()

    report, (ow, oh, sw, sh, aw, ah, dpr) = asyncio.run(scenario())
    assert (ow <= sw and oh <= sh) is fits, (ow, oh, sw, sh)
    assert aw <= sw and ah <= sh and dpr == 1, (aw, ah, sw, sh, dpr)
    assert ("window vs screen size" in report["passed"]) is fits, report
    assert any(w.startswith("window vs screen size") for w in report["warnings"]) is not fits, report["warnings"]
