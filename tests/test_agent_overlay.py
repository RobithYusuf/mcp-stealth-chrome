"""agent_overlay's server side, without a window: when it starts and why not, the refusal while
the person holds the browser, the pause for their typing, and how wait_for_human hears the bar.
The overlay itself is proved on a real window in test_agent_overlay_e2e.py (MCP_HEADED=1)."""
import asyncio
import json
import os
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


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _overlay(tmp_path, control=None, pid=None):
    """A server-side handle on a folder, with a process standing in for the overlay."""
    folder = tmp_path / "overlay"
    folder.mkdir()
    proc = subprocess.Popen(SLEEPER)
    browser = _Browser()
    overlay = presence.Overlay(browser, pid or 4242, folder, proc, False)
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
    assert asyncio.run(presence.attach(_Browser(), 1, ask=None, headless=False)) == ""
    monkeypatch.setenv("MCP_STEALTH_AGENT_OVERLAY", "1")
    assert presence.wanted(None) is True
    assert presence.wanted(False) is False


@pytest.mark.parametrize("kw,why", [
    ({"headless": True}, "headless"),
    ({"headless": False, "persons_browser": True}, "launcher"),
])
def test_says_why_it_is_off(kw, why):
    note = asyncio.run(presence.attach(_Browser(), 1234, ask=True, **kw))
    assert note.startswith(" [agent_overlay: OFF") and why in note, note


def test_off_for_a_browser_this_server_did_not_start():
    note = asyncio.run(presence.attach(_Browser(), None, ask=True, headless=False))
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


# What the review of the first version found, each proved here.

def test_an_overlay_that_died_while_the_person_held_the_browser_still_refuses(made, monkeypatch):
    browser, overlay = made({"holder": "person", "since": time.time()})
    overlay.proc.kill()
    overlay.proc.wait()
    monkeypatch.setattr(presence, "_spawn", lambda *a: (_ for _ in ()).throw(OSError("no display")))
    text = presence.refusal("get_text", browser)
    assert text and "overlay has stopped" in text and "browser_launch(agent_overlay=False)" in text
    with pytest.raises(presence.PersonHasBrowser):
        asyncio.run(presence.before_input(_Tab(browser)))
    from mcp_stealth_chrome import _app
    token = _app.CALL_ARGUMENTS.set({"agent_overlay": False})
    try:
        assert presence.refusal("browser_launch", browser) is None  # the way out
    finally:
        _app.CALL_ARGUMENTS.reset(token)
    token = _app.CALL_ARGUMENTS.set({"agent_overlay": True})
    try:
        assert presence.refusal("browser_launch", browser)  # not with any other launch
    finally:
        _app.CALL_ARGUMENTS.reset(token)


def test_a_dead_overlay_is_started_again_and_keeps_the_hold(made, monkeypatch):
    import os
    browser, overlay = made({"holder": "person"})
    overlay.pid = os.getpid()  # the browser still runs
    overlay.proc.kill()
    overlay.proc.wait()
    spawned = []
    monkeypatch.setattr(presence, "_spawn", lambda *a: spawned.append(a) or subprocess.Popen(SLEEPER))
    text = presence.refusal("click", browser)
    assert spawned and overlay.alive() and overlay.restarts == 1
    assert text and "Take over" in text and "overlay has stopped" not in text
    for _ in range(presence.RESTARTS + 2):
        overlay.proc.kill()
        overlay.proc.wait()
        presence.refusal("click", browser)
    assert overlay.restarts == presence.RESTARTS  # no endless restarts
    overlay.proc.kill()


def test_stop_ends_an_overlay_that_ignores_sigterm(tmp_path):
    folder = tmp_path / "overlay"
    folder.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import signal, time; "
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    time.sleep(0.3)
    browser = _Browser()
    overlay = presence.Overlay(browser, 4242, folder, proc, False)
    setattr(browser, presence.ATTR, overlay)
    started = time.time()
    presence.stop(browser)
    assert proc.poll() is not None, "the overlay outlived stop()"
    assert not folder.exists()
    assert time.time() - started < 5


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay is macOS only")
def test_attach_waits_for_the_overlay_and_says_off_when_it_dies_at_start(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_DIR", str(tmp_path / "root"))
    monkeypatch.setattr(presence, "unavailable", lambda: None)

    def dies(folder, pid, cursor, start=None):
        (folder / "overlay.log").write_text("ImportError: no AppKit here\n")
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(presence, "_spawn", dies)
    browser = _Browser()
    note = asyncio.run(presence.attach(browser, 1234, ask=True, headless=False))
    assert "OFF" in note and "no AppKit here" in note, note
    assert presence.of(browser) is None
    assert not any((tmp_path / "root").iterdir())  # its folder went with it

    def comes_up(folder, pid, cursor, start=None):
        code = ("import json, os, time; "
                f"json.dump({{'holder': 'agent', 'overlay_pid': os.getpid()}}, open({str(folder / 'control.json')!r}, 'w')); "
                "time.sleep(60)")
        return subprocess.Popen([sys.executable, "-c", code])
    monkeypatch.setattr(presence, "_spawn", comes_up)
    browser = _Browser()
    note = asyncio.run(presence.attach(browser, os.getpid(), ask=True, headless=False))
    assert note.startswith(" [agent_overlay: on:"), note
    overlay = presence.of(browser)
    assert overlay is not None and overlay.ready()
    presence.stop(browser)


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay is macOS only")
def test_agent_overlay_false_on_a_browser_with_one_stops_it(made):
    browser, overlay = made({"holder": "agent"})
    note = asyncio.run(presence.attach(browser, 4242, ask=False, headless=False))
    assert "stopped" in note
    assert overlay.proc.poll() is not None and presence._handle(browser) is None


def test_state_files_are_not_read_through_a_link(made, tmp_path):
    browser, overlay = made()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"holder": "person"}))
    overlay.control_file.symlink_to(target)
    assert overlay.read_control() == {}
    assert presence.refusal("click", browser) is None


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay is macOS only")
def test_a_state_folder_that_is_a_link_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_DIR", str(link))
    monkeypatch.setattr(presence, "unavailable", lambda: None)
    note = asyncio.run(presence.attach(_Browser(), 1234, ask=True, headless=False))
    assert "OFF" in note and "is a link" in note


def test_each_batch_step_meets_the_take_over_refusal(monkeypatch):
    from mcp_stealth_chrome import server
    monkeypatch.setattr(server.presence, "refusal", lambda name, browser: "Error: held by the person")
    result = asyncio.run(server.batch_actions(actions=[{"type": "get_url"}]))
    assert "held by the person" in result
    result = asyncio.run(server.workflow_run(steps=[{"tool": "get_url"}]))
    assert "held by the person" in result


# The second review: what was left.

def test_take_over_stops_a_scroll_under_way(made, monkeypatch):
    from mcp_stealth_chrome import humanize
    browser, overlay = made({"holder": "agent"})
    monkeypatch.setattr(humanize.asyncio, "sleep", _no_sleep)

    class Tab:
        def __init__(self):
            self.browser = browser
            self.sent = 0

        async def send(self, _command):
            self.sent += 1
            overlay.control_file.write_text(json.dumps({"holder": "person"}))  # Take over now

    tab = Tab()
    with pytest.raises(presence.PersonHasBrowser):
        asyncio.run(humanize.humanized_scroll(tab, 900, position=(10, 10)))
    assert tab.sent == 1  # not one wheel event after the hand-over, nor a JS scroll


async def _no_sleep(*_args, **_kw):
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay is macOS only")
def test_an_overlay_that_never_says_it_is_up_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_DIR", str(tmp_path / "root"))
    monkeypatch.setattr(presence, "unavailable", lambda: None)
    monkeypatch.setattr(presence, "READY_WAIT", 0.4)
    procs = []
    monkeypatch.setattr(presence, "_spawn", lambda *a: procs.append(subprocess.Popen(SLEEPER)) or procs[-1])
    browser = _Browser()
    note = asyncio.run(presence.attach(browser, os.getpid(), ask=True, headless=False))
    assert "OFF" in note and "did not confirm" in note, note
    assert procs[0].poll() is not None, "the silent process was left running"
    assert presence._handle(browser) is None


def test_a_process_that_never_got_its_code_is_ended(tmp_path, monkeypatch):
    started = []
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        started.append(real_popen(*a, **kw))
        return started[-1]
    monkeypatch.setattr(presence.subprocess, "Popen", popen)
    monkeypatch.setattr(presence, "_script", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    with pytest.raises(OSError):
        presence._spawn(tmp_path, 4242, False)
    assert started and started[0].poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="start times come from ps")
def test_prune_tells_a_reused_pid_from_the_browser(tmp_path):
    from mcp_stealth_chrome import _quit_watcher
    me = os.getpid()
    mine = _quit_watcher.generation(_quit_watcher.process_start(me))
    kept = tmp_path / f"chrome-{me}-{mine}-a"
    reused = tmp_path / f"chrome-{me}-0123456789-b"
    gone = tmp_path / "chrome-999999-0123456789-c"
    for folder in (kept, reused, gone):
        folder.mkdir()
    presence._prune(tmp_path)
    assert kept.exists() and not reused.exists() and not gone.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="start times come from ps")
def test_no_restart_for_a_pid_that_now_runs_something_else(made, monkeypatch):
    browser, overlay = made({"holder": "person"})
    overlay.pid, overlay.start = os.getpid(), "Mon Jan  1 00:00:00 2001"
    overlay.proc.kill()
    overlay.proc.wait()
    monkeypatch.setattr(presence, "_spawn", lambda *a: pytest.fail("restarted for another process"))
    assert presence.refusal("click", browser)  # still refused: the hold stands
    assert overlay.restarts == 0


@pytest.mark.parametrize("env,expected", [
    ({}, {"bar": "full", "light": True, "lock": True}),
    ({"MCP_STEALTH_OVERLAY_BAR": "compact"}, {"bar": "compact", "light": True, "lock": True}),
    ({"MCP_STEALTH_OVERLAY_BAR": "HOVER"}, {"bar": "hover", "light": True, "lock": True}),
    # The bar's button is the only way to take over: with no bar, no lock either.
    ({"MCP_STEALTH_OVERLAY_BAR": "off"}, {"bar": "off", "light": True, "lock": False}),
    ({"MCP_STEALTH_OVERLAY_BAR": "tiny"}, {"bar": "full", "light": True, "lock": True}),
    ({"MCP_STEALTH_OVERLAY_LIGHT": "off"}, {"bar": "full", "light": False, "lock": True}),
    ({"MCP_STEALTH_OVERLAY_LOCK": "0"}, {"bar": "full", "light": True, "lock": False}),
    ({"MCP_STEALTH_OVERLAY_LOCK": "maybe"}, {"bar": "full", "light": True, "lock": True}),
])
def test_the_overlay_style_comes_from_the_environment(monkeypatch, env, expected):
    for name in ("MCP_STEALTH_OVERLAY_BAR", "MCP_STEALTH_OVERLAY_LIGHT", "MCP_STEALTH_OVERLAY_LOCK"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert presence.style() == expected
