"""After close_instance of the current instance, that instance stays current, and the results say so.

v0.19.0: close_instance('X') of the current instance answered "closed current instance 'X'", and a
following browser_launch(browser="brave") started instance X (brave-profiles/X) while answering
"Browser launched ...", naming neither. The current instance is not switched to main on its own:
a tool call after the close would then act on main's browser, which may be the person's. Instead
both results name the instance.
"""
import asyncio
import os
import pathlib
import shutil
import signal
import tempfile

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import instance_notes, state
from mcp_stealth_chrome.state import BrowserState


def test_a_launch_of_main_is_unchanged():
    text = "Browser launched (headless=True). Loaded: about:blank [x]\n{\"a\": 1}"
    assert instance_notes.launched_as(text, "main") == text


def test_a_launch_of_another_instance_names_it_on_the_first_line():
    text = "Browser launched (headless=True). Loaded: about:blank [x]\n{\"a\": 1}"
    out = instance_notes.launched_as(text, "X")
    head, body = out.split("\n", 1)
    assert "launched as instance 'X'" in head and "switch_instance('main')" in head
    assert body == "{\"a\": 1}"  # the JSON body still parses


def test_an_error_is_left_as_it_is():
    assert instance_notes.launched_as("Error: no", "X") == "Error: no"


def test_closing_the_current_instance_says_it_stays_current():
    BrowserState.switch_to("X")
    try:
        out = instance_notes.closed_current("closed current instance 'X'", "X")
    finally:
        BrowserState.switch_to("main")
        BrowserState.instances.pop("X", None)
    assert "'X' stays this session's current instance" in out
    assert "switch_instance('main')" in out
    assert instance_notes.closed_current("Error: close failed", "X") == "Error: close failed"
    # main stays main; nothing to add
    assert instance_notes.closed_current("closed current instance 'main'", "main") == \
        "closed current instance 'main'"


def test_the_tools_carry_the_notes(monkeypatch):
    """The decorators are on the registered tools, and keep their signature for FastMCP."""
    import inspect

    from mcp_stealth_chrome import server

    assert "block_ads" in inspect.signature(server.browser_launch).parameters
    assert "forget_tabs" in inspect.signature(server.close_instance).parameters
    registered = {t.name: t.fn for t in server.mcp._tool_manager.list_tools()}
    assert registered["browser_launch"].__code__.co_qualname.startswith("names_the_instance")
    assert registered["close_instance"].__code__.co_qualname.startswith("says_what_stays_current")
    assert "block_ads" in registered["browser_launch"].__doc__

    async def body(*_a, **_k):
        return "Browser launched (headless=True). Loaded: about:blank [x]\n{}"

    wrapped = instance_notes.names_the_instance(body)
    BrowserState.switch_to("X")
    try:
        assert "launched as instance 'X'" in asyncio.run(wrapped())
    finally:
        BrowserState.switch_to("main")
        BrowserState.instances.pop("X", None)


@pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")
def test_launch_after_closing_the_current_instance_names_it():
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-instance-notes-"))
    env = {"MCP_STEALTH_DATA_DIR": str(root / "data"), "MCP_STEALTH_TABS_DIR": ""}
    client = Client(env=env)
    try:
        out = client.call("spawn_browser", _timeout=90, instance_id="X", headless=True,
                          extra_args=SAFE_ARGS)
        assert not out.failed, out.text
        assert not client.call("switch_instance", instance_id="X").failed
        closed = client.call("close_instance", instance_id="X", _timeout=40)
        assert not closed.failed, closed.text
        assert "'X' stays this session's current instance" in closed.text, closed.text
        launched = client.call("browser_launch", _timeout=90, headless=True, extra_args=SAFE_ARGS)
        assert not launched.failed, launched.text
        assert "launched as instance 'X'" in launched.text.splitlines()[0], launched.text
        assert str(root / "data" / "profiles" / "X") in launched.text, launched.text
        assert not client.call("close_instance", instance_id="X", _timeout=40).failed
        assert not client.call("switch_instance", instance_id="main").failed
        launched = client.call("browser_launch", _timeout=90, headless=True, extra_args=SAFE_ARGS)
        assert not launched.failed and "launched as instance" not in launched.text, launched.text
    finally:
        client.call("close_all_instances", _timeout=60)
        client.close()
        for pid, _command in state.processes_naming(root) or []:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)
