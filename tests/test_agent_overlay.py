"""agent_overlay's server side, without a window: when it starts and why not, the refusal while
the person holds the browser, the pause for their typing, and how wait_for_human hears the bar.
The overlay itself is proved on a real window in test_agent_overlay_e2e.py (MCP_HEADED=1)."""
import asyncio
import json
import subprocess
import sys
import time

import pytest

from mcp_stealth_chrome import presence


class _Browser:
    pass


class _Tab:
    def __init__(self, browser):
        self.browser = browser


def _overlay(tmp_path, control=None):
    """A server-side handle on a folder, with a process standing in for the overlay."""
    folder = tmp_path / "overlay"
    folder.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    browser = _Browser()
    overlay = presence.Overlay(browser, 4242, folder, proc)
    setattr(browser, presence.ATTR, overlay)
    if control is not None:
        (folder / "control.json").write_text(json.dumps(control))
    return browser, overlay


@pytest.fixture
def made(tmp_path):
    made = []

    def make(control=None):
        browser, overlay = _overlay(tmp_path, control)
        made.append(overlay)
        return browser, overlay
    yield make
    for overlay in made:
        overlay.proc.kill()
        overlay.proc.wait()


def test_off_unless_asked(monkeypatch):
    monkeypatch.delenv("MCP_STEALTH_AGENT_OVERLAY", raising=False)
    assert presence.wanted(None) is False
    assert presence.attach(_Browser(), 1, ask=None, headless=False) == ""
    monkeypatch.setenv("MCP_STEALTH_AGENT_OVERLAY", "1")
    assert presence.wanted(None) is True
    assert presence.wanted(False) is False


@pytest.mark.parametrize("kw,why", [
    ({"headless": True}, "headless"),
    ({"headless": False, "persons_browser": True}, "launcher"),
])
def test_says_why_it_is_off(kw, why):
    note = presence.attach(_Browser(), 1234, ask=True, **kw)
    assert note.startswith(" [agent_overlay: OFF") and why in note, note


def test_off_for_a_browser_this_server_did_not_start():
    note = presence.attach(_Browser(), None, ask=True, headless=False)
    assert "OFF" in note and "did not start" in note


def test_macos_only_and_needs_pyobjc(monkeypatch):
    monkeypatch.setattr(presence.sys, "platform", "linux")
    assert presence.unavailable() == "macOS only"
    monkeypatch.setattr(presence.sys, "platform", "darwin")
    monkeypatch.setattr(presence.importlib.util, "find_spec", lambda name: None)
    assert "os-cursor" in presence.unavailable()


def test_the_overlay_script_is_standalone():
    source = presence._script()
    compile(source, "_agent_overlay.py", "exec")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "mcp_stealth_chrome" in line or line.startswith("from .")]


def test_refused_while_the_person_holds_the_browser(made):
    browser, overlay = made({"holder": "person", "since": time.time()})
    text = presence.refusal("click", browser)
    assert text.startswith("Error: the person took over this browser") and "click did nothing" in text
    assert "Resume agent" in text and "wait_for_human" in text
    assert presence.refusal("server_status", browser) is None
    assert presence.refusal("wait_for_human", browser) is None
    overlay.control_file.write_text(json.dumps({"holder": "agent"}))
    assert presence.refusal("click", browser) is None


def test_no_overlay_no_refusal():
    assert presence.refusal("click", _Browser()) is None
    assert presence.refusal("click", None) is None


def test_input_stops_once_the_person_holds_it(made):
    browser, _overlay_ = made({"holder": "person"})
    with pytest.raises(presence.PersonHasBrowser):
        asyncio.run(presence.before_input(_Tab(browser)))


def test_input_waits_for_the_person_to_stop_typing_and_says_so(made, monkeypatch):
    monkeypatch.setattr(presence, "POLITE", 0.4)
    browser, overlay = made({"holder": "agent", "person_last_input": time.time()})
    monkeypatch.setattr("mcp_stealth_chrome.state.BrowserState.session_key", classmethod(lambda cls: "s"))
    started = time.time()
    asyncio.run(presence.before_input(_Tab(browser)))
    assert time.time() - started >= 0.3
    assert json.loads(overlay.agent_file.read_text())["waiting"] is None
    note = presence.result_note("s", "Typed 3 chars")
    assert note.startswith("Typed 3 chars [agent_overlay: the person typed in the browser")
    assert presence.result_note("s", "next") == "next"  # said once


def test_input_goes_straight_on_without_a_recent_key(made):
    browser, _overlay_ = made({"holder": "agent", "person_last_input": time.time() - 30})
    started = time.time()
    asyncio.run(presence.before_input(_Tab(browser)))
    assert time.time() - started < 0.1


def test_wait_for_human_hears_done_and_resume(made):
    browser, overlay = made({"holder": "agent"})
    asked = presence.ask(browser, "Tick Agree")
    assert json.loads(overlay.agent_file.read_text())["help"] == "Tick Agree"
    assert presence.answered(browser, asked) is None
    overlay.control_file.write_text(json.dumps({"holder": "agent", "help_done": asked + 1}))
    assert presence.answered(browser, asked) == "done"
    overlay.control_file.write_text(json.dumps({"holder": "agent", "reason": "button", "since": asked + 2}))
    assert presence.answered(browser, asked) == "resumed"
    overlay.control_file.write_text(json.dumps({"holder": "agent", "reason": "button", "since": asked - 5}))
    assert presence.answered(browser, asked) is None  # a resume from before the wait is no answer


def test_the_pointer_and_clicks_reach_the_agent_file(made):
    browser, overlay = made({})
    presence.note_pointer(_Tab(browser), 10, 20)
    presence.note_pointer(_Tab(browser), 30, 40, pressed=True)
    agent = json.loads(overlay.agent_file.read_text())
    assert agent["cursor"] == [30, 40] and agent["click_at"] > 0


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps no POSIX mode bits")
def test_files_are_private(made):
    browser, overlay = made({})
    overlay.touch(status="clicking")
    assert overlay.agent_file.stat().st_mode & 0o777 == 0o600


def test_stop_ends_the_process_and_removes_the_folder(made):
    browser, overlay = made({})
    presence.stop(browser)
    assert overlay.proc.poll() is not None
    assert not overlay.folder.exists()
    assert presence.of(browser) is None


def test_a_gate_refuses_at_the_tool_boundary(monkeypatch):
    from mcp_stealth_chrome import _app
    from mcp_stealth_chrome import server
    monkeypatch.setattr(_app, "CALL_GATES", [lambda name: "Error: held" if name == "tab_list" else None])
    result = asyncio.run(server.mcp.call_tool("tab_list", {}))
    content = result[0] if isinstance(result, tuple) else result
    assert "Error: held" in content[0].text
    result = asyncio.run(server.mcp.call_tool("server_status", {}))
    content = result[0] if isinstance(result, tuple) else result
    assert "Error: held" not in content[0].text


@pytest.mark.parametrize("name,args,shown", [
    ("navigate", {"url": "https://shop.example.com/cart?token=abc"}, "Opening shop.example.com"),
    ("click_text", {"text": "Add to cart"}, "Clicking “Add to cart”"),
    ("click_role", {"role": "button", "name": "Pay"}, "Clicking “Pay”"),
    ("click", {"selector": "#plus"}, "Clicking #plus"),
    ("click", {"ref": "e12"}, "Clicking"),
    ("press_key", {"key": "Enter"}, "Pressing Enter"),
    ("fill", {"selector": "#pw", "value": "hunter2"}, "Filling in #pw"),
    ("check", {"selector": "#agree"}, "Ticking #agree"),
    ("type_text", {"text": "hunter2"}, "Typing"),
    ("some_new_tool", {}, "Some new tool"),
])
def test_the_bar_says_what_is_done_and_never_what_is_typed(name, args, shown):
    text = presence.describe(name, args)
    assert text == shown
    assert "hunter2" not in text and "token" not in text
