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


# ── The lock follows the page: zoom, a resize between tool calls, full screen ────────────────


class _Viewport:
    def __init__(self, w, h):
        self.client_width, self.client_height = w, h


def test_the_page_is_measured_in_screen_points_whatever_its_zoom():
    """CSS pixels shrink with zoom: at 125% the lock came out 800x442 over a 1000x553 page."""
    at_100 = (_Viewport(2000, 1106), None, None, _Viewport(1000, 553))
    at_125 = (_Viewport(2000, 1106), None, None, _Viewport(800, 442))
    assert presence.page_size(at_100, 1000) == (1000.0, 553.0)
    assert presence.page_size(at_125, 1000) == (1000.0, 553.0)
    assert presence.page_size((_Viewport(1000, 553), None, None, _Viewport(800, 442)), 1000) == (1000.0, 553.0)
    # A classic scrollbar takes a few pixels: the display's scale still rounds to 2.
    assert presence.page_size((_Viewport(1970, 1106), None, None, _Viewport(985, 553)), 1000) == (985.0, 553.0)
    # Without the device-pixel viewport, the CSS one as before.
    assert presence.page_size((None, None, None, _Viewport(800, 442)), 1000) == (800, 442)


class _Bounds:
    left, top, width, height, window_state = 100, 100, 1000, 640, "normal"


class _MetricsTab:
    target = None

    def __init__(self):
        self.sent = 0

    async def send(self, cmd):
        self.sent += 1
        name = getattr(cmd, "__qualname__", "") or repr(cmd)
        if "window_for_target" in repr(cmd) or "get_window_for_target" in name:
            return 1, _Bounds()
        return (_Viewport(2000, 1106), None, None, _Viewport(800, 442))


def test_refresh_window_writes_only_what_changed(tmp_path, monkeypatch):
    browser, overlay = _overlay(tmp_path)
    try:
        writes = []
        real = overlay.write
        monkeypatch.setattr(overlay, "write", lambda **kw: (writes.append(kw), real(**kw)))
        from nodriver.cdp import browser as cdp_browser
        monkeypatch.setattr(cdp_browser, "get_window_for_target", lambda target_id=None: "window_for_target")
        tab = _MetricsTab()
        asyncio.run(presence.refresh_window(overlay, tab))
        assert writes == [{"window": [100, 100, 1000, 640], "viewport": [0.0, 87.0, 1000.0, 553.0],
                           "window_state": "normal", "zoom": 1.25}]
        asyncio.run(presence.refresh_window(overlay, tab))
        assert len(writes) == 1  # nothing changed: the file is left alone
    finally:
        overlay.proc.kill()


def _overlay_page_rect(full=False, saved=None):
    """The overlay's own insets() and Overlay.page_rect, taken from its source (it imports AppKit
    at the top, so it cannot be imported here)."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(presence.__file__).with_name("_agent_overlay.py")).read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "insets")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Overlay")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "page_rect")
    holder = ast.ClassDef(name="Holder", bases=[], keywords=[], body=[method], decorator_list=[])
    space = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[func, holder], type_ignores=[])), "_agent_overlay.py", "exec"), space)
    obj = space["Holder"]()
    obj.full, obj.insets = full, saved
    return obj


def test_the_lock_follows_a_window_resized_between_tool_calls():
    measured = {"window": [100, 100, 1000, 640], "viewport": [0.0, 87.0, 1000.0, 553.0], "window_state": "normal"}
    o = _overlay_page_rect()
    assert o.page_rect((100.0, 100.0, 1000.0, 640.0), measured) == (100.0, 187.0, 1000.0, 553.0)
    # The person widened and moved the window; the server has not measured since.
    assert o.page_rect((220.0, 100.0, 1250.0, 640.0), measured) == (220.0, 187.0, 1250.0, 553.0)


def test_in_full_screen_the_lock_is_the_content_window_and_its_insets_are_not_kept():
    fs = {"window": [0, 0, 1728, 1117], "viewport": [0.0, 121.0, 1728.0, 996.0], "window_state": "fullscreen"}
    o = _overlay_page_rect(full=True, saved=(0.0, 87.0, 0.0, 0.0))
    assert o.page_rect((0.0, 121.0, 1728.0, 996.0), fs) == (0.0, 121.0, 1728.0, 996.0)
    # Back from full screen before the next measurement: the normal insets, not full screen's.
    o.full = False
    assert o.page_rect((220.0, 100.0, 1250.0, 640.0), fs) == (220.0, 187.0, 1250.0, 553.0)


def test_a_call_marks_its_overlay_busy_until_it_ends_whatever_is_current_by_then(tmp_path):
    """A call longer than the quiet times read as idle: HIDE_AFTER took the lock away mid-call."""
    browser, overlay = _overlay(tmp_path)
    other = _Browser()  # switch_instance during the call: another browser is current at its end

    async def one_call(current_at_end, running_before):
        await presence.before_tool("wait_for", browser, None)
        assert overlay.agent["busy"] is True and overlay.in_flight == running_before + 1
        presence.after_tool(current_at_end)

    async def two_at_once():
        await presence.before_tool("wait_for", browser, None)
        await asyncio.gather(one_call(browser, 1))  # a second call starts and ends meanwhile
        assert overlay.agent["busy"] is True and overlay.in_flight == 1
        presence.after_tool(browser)
        assert overlay.agent["busy"] is False and overlay.in_flight == 0

    try:
        asyncio.run(one_call(other, 0))
        assert overlay.agent["busy"] is False and overlay.in_flight == 0
        assert json.loads((overlay.folder / "agent.json").read_text())["busy"] is False
        asyncio.run(two_at_once())
    finally:
        overlay.proc.kill()


def _overlay_functions(*names, **constants):
    """Plain functions of the overlay, taken from its source (it imports AppKit at the top)."""
    import ast
    import math
    from pathlib import Path
    source = (Path(presence.__file__).with_name("_agent_overlay.py")).read_text()
    tree = ast.parse(source)
    found = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}
    values = {}
    for node in tree.body:  # the module's own constants, so the test checks what ships
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in constants:
                value = node.value
                # float(CONFIG.get("name", default)): the default the overlay ships with
                if isinstance(value, ast.Call) and value.args and isinstance(value.args[0], ast.Call) \
                        and getattr(value.args[0].func, "attr", None) == "get":
                    value = value.args[0].args[1]
                values[node.targets[0].id] = ast.literal_eval(value)
    assert set(found) == set(names) and set(values) == set(constants)
    space = {"math": math, "time": time, **values}
    exec(compile(ast.Module(body=list(found.values()), type_ignores=[]), "_agent_overlay.py", "exec"), space)
    return space


def test_the_light_breathes_between_a_thin_ring_and_the_full_one():
    """Asked for on 21 Nov 2026: the light thickens and thins. The mask is the thin ring plus what
    the full light adds over it, faded in and out on the GPU, so at full strength it is exactly
    the 64 pt light that shipped, and faded out exactly the thin one."""
    f = _overlay_functions("light_at", "thin_at", "extra_at", "bands", DEPTH=1, THIN=1)
    assert f["THIN"] < f["DEPTH"]
    depths = [i * 0.25 for i in range(int(f["DEPTH"] * 4) + 1)]
    for d in depths:
        thin, extra, full = f["thin_at"](d), f["extra_at"](d), f["light_at"](d)
        assert thin <= full + 1e-12
        assert abs(thin + extra * (1 - thin) - full) < 1e-9, d
    assert f["thin_at"](f["THIN"]) == 0.0 and f["light_at"](f["THIN"]) > 0.1
    # Nested bands draw a profile with one peak exactly: extra_at rises from the edge, then falls.
    extra = [f["extra_at"](d) for d in depths]
    peak = extra.index(max(extra))
    assert 0 < peak < len(extra) - 1
    assert all(a <= b for a, b in zip(extra[:peak], extra[1:peak + 1]))
    assert all(a >= b for a, b in zip(extra[peak:], extra[peak + 1:]))
    bands = f["bands"](f["extra_at"], 0.5)
    for (lo, hi, _add), (lo2, hi2, _add2) in zip(bands, bands[1:]):
        assert lo <= lo2 and hi2 <= hi  # nested, widest first: no two meet edge to edge
    assert f["bands"](f["light_at"], 0.5)[0][0] == 0.0  # the full light starts at the edge


def test_the_overlay_floats_only_where_no_app_switch_can_leave_it_above_another_app():
    """With the browser in front, the floating level held the light over the app a Cmd+Tab went
    to for 163 and 179 ms (measured), until a window scan saw that app over the browser."""
    f = _overlay_functions("wants_floating", SWITCH_SETTLE=1)
    floats = f["wants_floating"]
    settled = f["SWITCH_SETTLE"] + 1
    assert floats(False, False, settled)            # behind another app, uncovered: so a Cmd+Tab TO
    # the browser cannot cover it (a Command rule in 0.25.14 dropped it then, and it showed late)
    assert not floats(True, False, settled)         # covered: sit just above the browser
    assert not floats(False, True, settled)         # the browser in front: just above it
    assert not floats(False, False, 0.0)            # an app has just come to the front


def test_an_idle_agent_leaves_the_page_to_the_person():
    """The lock stayed 90 s after the agent's last call, so a person whose agent had finished had to
    press Take over to use their page, and nothing heard it. Steps of a working agent came at most
    14.7 s apart (median 4.3 s), so past IDLE_AFTER the lock goes and the bar says the agent is idle."""
    f = _overlay_functions("choose_mode", QUIET_AFTER=1, IDLE_AFTER=1)
    f["HIDE_AFTER"] = 90.0
    choose = f["choose_mode"]
    assert f["QUIET_AFTER"] < 14.7 < f["IDLE_AFTER"] < f["HIDE_AFTER"]
    assert choose(None, "agent", 0.0, None) == "agent"
    assert choose(None, "agent", 5.0, None) == "thinking"
    assert choose(None, "agent", f["IDLE_AFTER"] + 1, None) == "resting"
    assert choose(None, "agent", 91.0, None) == "idle"
    assert choose(None, "agent", f["IDLE_AFTER"] + 1, "polite") == "polite"
    # Help and the person's hold never give way to quiet: their buttons are the way back.
    assert choose("Log in", "agent", 500.0, None) == "help"
    assert choose(None, "person", 500.0, None) == "person"
    # Done pressed and no call took it yet: not asking any more (the bar went on asking for help
    # until a call came), and it fades like anything else.
    assert choose("Log in", "agent", 5.0, None, True) == "noted"
    assert choose("Log in", "agent", 30.0, None, True) == "noted"
    assert choose("Log in", "agent", 500.0, None, True) == "idle"
    assert choose("Log in", "person", 5.0, None, True) == "person"


def test_the_bar_says_whether_the_agent_knows_of_a_take_over(made, monkeypatch):
    monkeypatch.setattr("mcp_stealth_chrome.state.BrowserState.session_key", classmethod(lambda cls: "s"))
    told = _overlay_functions("told")["told"]
    browser, overlay = made({"holder": "person", "since": time.time() - 1})
    said = lambda: told(json.loads(overlay.agent_file.read_text()), overlay.read_control())  # noqa: E731
    assert said() == "The agent learns it at its next step"
    presence.refusal("click", browser)
    assert said().startswith("The agent was told at ")
    asyncio.run(presence.before_tool("http_request", browser, None))
    assert said() == "The agent went on without the browser (http_request)"
    note = presence.result_note("s", "200 OK")
    assert "the person holds this browser" in note and "go on only with what they asked for" in note
    presence.waiting_for_person(browser, True)
    assert said() == "The agent is waiting for you"
    presence.waiting_for_person(browser, False)
    overlay.control_file.write_text(json.dumps({"holder": "person", "since": time.time() + 1}))
    assert said() == "The agent learns it at its next step"  # a new Take over starts over


def test_input_refused_mid_tool_also_tells_the_bar(made):
    browser, overlay = made({"holder": "person", "since": time.time() - 1})
    with pytest.raises(presence.PersonHasBrowser):
        asyncio.run(presence.before_input(_Tab(browser)))
    assert json.loads(overlay.agent_file.read_text())["told_at"] >= overlay.read_control()["since"]


def test_what_the_person_did_while_the_agent_was_idle_reaches_its_next_call(made, monkeypatch):
    monkeypatch.setattr("mcp_stealth_chrome.state.BrowserState.session_key", classmethod(lambda cls: "s"))
    browser, overlay = made({"holder": "agent"})
    overlay.touch(status="reading the page")
    last = overlay.agent["active_at"]
    overlay.control_file.write_text(json.dumps(
        {"holder": "agent", "person_used": ["clicked", "typed"], "person_used_at": last + 30}))
    asyncio.run(presence.before_tool("get_text", browser, None))
    note = presence.result_note("s", "page text")
    assert "while the agent was idle the person clicked and typed in the browser" in note
    assert "read it again" in note
    asyncio.run(presence.before_tool("get_text", browser, None))
    assert presence.result_note("s", "again") == "again"  # said once
    # Use from before the agent's own last call is old news.
    overlay.touch()
    overlay.control_file.write_text(json.dumps(
        {"holder": "agent", "person_used": ["scrolled"], "person_used_at": overlay.agent["active_at"] - 1}))
    asyncio.run(presence.before_tool("get_text", browser, None))
    assert presence.result_note("s", "x") == "x"






def test_the_idle_time_reaches_the_overlay(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_STEALTH_OVERLAY_IDLE_AFTER", raising=False)
    assert presence.overlay_config(tmp_path, 1, False)["idle_after"] == presence.IDLE_AFTER
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_IDLE_AFTER", "4")
    assert presence.overlay_config(tmp_path, 1, False)["idle_after"] == 4.0
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_IDLE_AFTER", "soon")
    assert presence.overlay_config(tmp_path, 1, False)["idle_after"] == presence.IDLE_AFTER



def test_a_done_no_wait_took_reaches_the_next_result_and_closes_the_request(made, monkeypatch):
    """Done pressed while the agent read the page instead of waiting: the bar kept asking for
    help and the agent was never told (PROD, 21 Nov 2026)."""
    monkeypatch.setattr("mcp_stealth_chrome.state.BrowserState.session_key", classmethod(lambda cls: "s"))
    browser, overlay = made({"holder": "agent"})
    asked = presence.ask(browser, "Log in")
    assert presence.result_note("s", "page text", browser) == "page text"  # not answered yet
    overlay.control_file.write_text(json.dumps({"holder": "agent", "reason": "help done", "help_done": asked + 3}))
    text = presence.result_note("s", "page text", browser)
    assert "the person pressed Done on the request for help at" in text
    assert json.loads(overlay.agent_file.read_text())["help"] is None
    assert presence.result_note("s", "again", browser) == "again"  # once
    # A Done from an earlier request says nothing about a new one.
    asked = presence.ask(browser, "Tick Agree")
    overlay.control_file.write_text(json.dumps({"holder": "agent", "reason": "help done", "help_done": asked - 5}))
    assert presence.result_note("s", "x", browser) == "x"
    assert json.loads(overlay.agent_file.read_text())["help"] == "Tick Agree"


def test_the_launch_note_says_what_the_settings_really_show():
    full = presence.on_note({"bar": "full", "light": True, "lock": True})
    assert "a light around the window" in full and "a bar with Take over" in full and "a lock over the page" in full
    assert "the tools that act on or read the page are refused" in full
    off = presence.on_note({"bar": "off", "light": True, "lock": False})
    assert "no bar" in off and "no Take over button" in off and "a lock" not in off
    assert "a bar with Take over" not in off and "holds the browser" not in off
    assert "while the pointer is over the window" in presence.on_note({"bar": "hover", "light": False, "lock": True})
    assert "a light" not in presence.on_note({"bar": "full", "light": False, "lock": True})

