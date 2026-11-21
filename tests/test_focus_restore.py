"""A headed window must not take focus from the person. Fakes only: no window opens here."""
import asyncio

import pytest
import inspect
import subprocess
from types import SimpleNamespace

from mcp_stealth_chrome import identity, server
from mcp_stealth_chrome.tools import attach


def _record_activate(monkeypatch, front):
    activated = []
    monkeypatch.setattr(server, "_frontmost_pid", lambda: front)
    monkeypatch.setattr(server, "_activate_pid", lambda pid: activated.append(pid) or True)
    return activated


def test_focus_goes_back_to_the_process_that_had_it(monkeypatch):
    """By PID: `open -a <bundle>` named an application, and with several Chromes running macOS
    brought another project's browser to the front instead of the one the person was using."""
    activated = _record_activate(monkeypatch, 777)       # this server's Chrome took the front
    asyncio.run(server._give_focus_back(4242))
    assert activated == [4242]


def test_focus_is_left_alone_when_nothing_took_it(monkeypatch):
    activated = _record_activate(monkeypatch, 4242)
    asyncio.run(server._give_focus_back(4242))
    assert activated == []
    activated = _record_activate(monkeypatch, None)       # unreadable is not "taken"
    asyncio.run(server._give_focus_back(4242))
    asyncio.run(server._give_focus_back(None))
    assert activated == []


def test_a_failure_giving_focus_back_never_fails_the_launch(monkeypatch):
    def boom(_pid):
        raise OSError("no AppKit")

    monkeypatch.setattr(server, "_frontmost_pid", lambda: 777)
    monkeypatch.setattr(server, "_activate_pid", boom)
    asyncio.run(server._give_focus_back(4242))


def test_activation_names_a_process_never_an_application(monkeypatch):
    """Nothing that moves focus may run `open -a`: it cannot tell one Chrome from another."""
    from mcp_stealth_chrome.tools import navigation
    for module in (server, navigation):
        source = inspect.getsource(module)
        assert '"open", "-a"' not in source, module.__name__
    monkeypatch.setattr(server.sys, "platform", "linux")
    assert server._activate_pid(4242) is False
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert server._activate_pid(None) is False


def test_the_process_manager_path_says_no_for_a_process_that_ended():
    """It is asked first: AppKit's activateWithOptions was sat on for about a second after a
    popup brought Chrome to the front (1033 and 1039 ms in front, the limit is 500)."""
    import sys
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert server._front_process(gone.pid) is False


class _Hand:
    """What _foreground_for_human needs of a tab."""

    async def activate(self):
        return None

    async def bring_to_front(self):
        return None


def _hand_over(monkeypatch, fronts, pid=777):
    from mcp_stealth_chrome.tools import navigation
    activated = []
    seq = iter(fronts)
    monkeypatch.setattr(navigation.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_browser_process_pid", lambda _browser: pid)
    monkeypatch.setattr(server, "_frontmost_pid", lambda: next(seq, fronts[-1]))
    monkeypatch.setattr(server, "_activate_pid", lambda p: activated.append(p) or True)
    actions = asyncio.run(navigation._foreground_for_human(_Hand()))
    return actions, activated, navigation


def test_a_hand_over_brings_only_this_browser_to_the_front(monkeypatch):
    """request_human_help ran `open -a "Google Chrome.app"` and another project's browser came
    to the front (the person: "the focus is wrong")."""
    actions, activated, _nav = _hand_over(monkeypatch, [777])            # already in front
    assert activated == [] and "this browser is the app in front" in actions
    actions, activated, _nav = _hand_over(monkeypatch, [4242, 777])      # another app was
    assert activated == [777] and "this browser is the app in front" in actions


def test_a_hand_over_that_did_not_reach_the_front_says_so(monkeypatch):
    actions, activated, nav = _hand_over(monkeypatch, [4242, 4242])
    assert activated == [777] and nav.NOT_IN_FRONT in actions
    assert "this browser is the app in front" not in actions


class _Tab:
    target = SimpleNamespace(target_id="t1")
    browser = None

    async def evaluate(self, *_a, **_kw):
        return "complete"

    async def get(self, *_a, **_kw):
        return self

    async def send(self, *_a, **_kw):
        return None

    async def activate(self):
        return None

    async def bring_to_front(self):
        return None

    async def wait(self, *_a, **_kw):
        return None


def _fake_launch(monkeypatch):
    order = []

    async def start(config):
        order.append("start")
        order.append(("flags", list(config.browser_args)))
        return SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)

    async def give_back(previous):
        order.append(("give_back", previous))

    async def noop(*_a, **_kw):
        return None

    async def first_tab(_browser):
        return _Tab()

    async def arm(_tab):
        return {}, lambda: {}

    async def observe(*_a, **_kw):
        return {"final_url": "about:blank", "status": None}

    monkeypatch.setattr(server, "_frontmost_pid", lambda: order.append("read") or 4242)
    monkeypatch.setattr(server, "_give_focus_back", give_back)
    async def background_window(_browser, _width=None, _height=None):
        order.append("background_window")
        return _Tab()

    monkeypatch.setattr(server, "_open_background_window", background_window)
    monkeypatch.setattr(server, "_start_chrome", start)
    monkeypatch.setattr(server, "_first_live_tab", first_tab)
    monkeypatch.setattr(server, "_apply_fingerprint_to_tab", noop)
    monkeypatch.setattr(server, "_arm_document_watch", arm)
    monkeypatch.setattr(server, "_observe_navigation", observe)
    monkeypatch.setattr(server, "_auto_verify_cf", noop)
    return order


def _launch(**kw):
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    try:
        return asyncio.run(fn(persistent=False, auto_verify=False, **kw))
    finally:
        server.BrowserState.reset()


def test_headed_launch_reads_the_app_first_and_gives_focus_back_last(monkeypatch):
    order = _fake_launch(monkeypatch)
    out = _launch(headless=False)
    assert "launched" in out, out
    assert order[0] == "read" and order[1] == "start"
    assert order[-1] == ("give_back", 4242)  # the process the person was using, by PID


def test_headed_launch_never_opens_a_foreground_window(monkeypatch):
    """Chrome becomes the frontmost app when it opens its own startup window, and giving focus
    back afterwards cannot undo the keys that went into it."""
    order = _fake_launch(monkeypatch)
    _launch(headless=False)
    flags = next(item[1] for item in order if isinstance(item, tuple) and item[0] == "flags")
    assert "--no-startup-window" in flags and "--disable-backgrounding-occluded-windows" in flags
    assert "background_window" in order


def test_headless_launch_keeps_its_startup_window(monkeypatch):
    order = _fake_launch(monkeypatch)
    _launch(headless=True)
    flags = next(item[1] for item in order if isinstance(item, tuple) and item[0] == "flags")
    assert "--no-startup-window" not in flags
    assert "background_window" not in order


def test_a_hidden_window_is_not_raised_to_check_on_it(monkeypatch):
    raised = []

    class HiddenTab(_Tab):
        async def evaluate(self, *_a, **_kw):
            return '{"w":800,"h":600,"v":"hidden"}'

        async def bring_to_front(self):
            raised.append("front")

        async def activate(self):
            raised.append("activate")

    _fake_launch(monkeypatch)

    async def first_tab(_browser):
        return HiddenTab()

    monkeypatch.setattr(server, "_first_live_tab", first_tab)
    _launch(headless=False)
    assert raised == []


def _cdp_params(generator):
    return next(generator)


def test_background_window_asks_chrome_for_one_on_both_nodriver_shapes():
    sent = []

    class Conn:
        async def send(self, command):
            sent.append(_cdp_params(command))
            return "new-target"

    tab = SimpleNamespace(target=SimpleNamespace(target_id="new-target"))

    async def update():
        return None

    for browser in (SimpleNamespace(connection=Conn(), update_targets=update, tabs=[tab]),
                    SimpleNamespace(send=Conn().send, update_targets=update, tabs=[tab])):
        assert asyncio.run(server._open_background_window(browser, 800, 600)) is tab
    params = sent[0]["params"]
    assert sent[0]["method"] == "Target.createTarget"
    assert params["newWindow"] is True and params["background"] is True
    assert params["width"] == 800 and params["height"] == 600


def test_background_window_says_so_when_chrome_does_not_list_it():
    async def send(_command):
        return "gone"

    async def update():
        return None

    browser = SimpleNamespace(send=send, update_targets=update, tabs=[])
    try:
        asyncio.run(server._open_background_window(browser))
    except RuntimeError as exc:
        assert "did not list" in str(exc)
    else:
        raise AssertionError("expected an error")


def _tab_select_fixture(monkeypatch, headless):
    activated = []

    class Tab(_Tab):
        async def activate(self):
            activated.append(True)

    async def refresh():
        return True  # the browser answered

    monkeypatch.setattr(server, "_refresh_tabs", refresh)
    monkeypatch.setattr(server.BrowserState, "browser", object(), raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [Tab(), Tab()], raising=False)
    monkeypatch.setattr(server.BrowserState, "active_tab_index", 0, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_headless", headless, raising=False)
    return activated, server.mcp._tool_manager._tools["tab_select"].fn


def test_tab_select_moves_the_internal_tab_without_raising_the_window(monkeypatch):
    activated, select = _tab_select_fixture(monkeypatch, headless=False)
    assert "switched to tab 1" in asyncio.run(select(index=1))
    assert server.BrowserState.active_tab_index == 1
    assert activated == []


def test_tab_select_with_focus_still_raises_it(monkeypatch):
    activated, select = _tab_select_fixture(monkeypatch, headless=False)
    asyncio.run(select(index=1, focus=True))
    assert activated == [True]


def test_tab_select_headless_still_activates(monkeypatch):
    activated, select = _tab_select_fixture(monkeypatch, headless=True)
    asyncio.run(select(index=1))
    assert activated == [True]


def test_tab_new_headed_opens_behind_and_headless_as_before(monkeypatch):
    calls = []

    class Browser:
        tabs = []

        async def get(self, *_a, **_kw):
            calls.append("foreground")
            raise RuntimeError("stop here")

    async def background(_browser, *_a):
        calls.append("background")
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_open_background_window", background)
    monkeypatch.setattr(server.BrowserState, "browser", Browser(), raising=False)
    fn = server.mcp._tool_manager._tools["tab_new"].fn
    monkeypatch.setattr(server.BrowserState, "current_headless", False, raising=False)
    asyncio.run(fn(url="about:blank"))
    monkeypatch.setattr(server.BrowserState, "current_headless", True, raising=False)
    asyncio.run(fn(url="about:blank"))
    assert calls == ["background", "foreground"]


def test_frontmost_pid_asks_in_process_first_and_lsappinfo_when_that_fails(monkeypatch):
    """In process it costs 0.04 ms; two lsappinfo processes cost 15.5 ms, before every input."""
    spawned = []

    def fake_run(cmd, **_kw):
        spawned.append(cmd)
        return SimpleNamespace(stdout="ASN:0x0-0x1:\n" if cmd[1] == "front" else '"pid"=4242\n')

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "_front_pid_in_process", lambda: 777)
    assert server._frontmost_pid() == 777 and spawned == []
    monkeypatch.setattr(server, "_front_pid_in_process", lambda: None)
    assert server._frontmost_pid() == 4242 and len(spawned) == 2
    monkeypatch.setattr(server.sys, "platform", "linux")
    assert server._frontmost_pid() is None


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="the Process Manager is macOS")
def test_the_in_process_read_names_a_running_process():
    import os
    pid = server._front_pid_in_process()
    assert pid is None or (isinstance(pid, int) and pid > 0 and pid != os.getpid())


def _popup_watch(monkeypatch, fronts, sticks=True):
    """`fronts`: the app in front at each look. With `sticks`, a hand-back holds: the process
    activated stays in front from then on, as it does once Chrome has finished its popup."""
    activated = []
    seq = iter(fronts)
    held = []

    def front():
        return held[-1] if held else next(seq, fronts[-1])

    def activate(pid):
        activated.append(pid)
        if sticks:
            held.append(pid)
        return True

    monkeypatch.setattr(server, "_frontmost_pid", front)
    monkeypatch.setattr(server, "_activate_pid", activate)
    return activated


def test_a_popup_taking_focus_gets_it_sent_back(monkeypatch):
    """To the process that had it (111), which may itself be a Chrome: never "the application"."""
    activated = _popup_watch(monkeypatch, [111, 111, 777])
    asyncio.run(server._win_focus_back_from_popup(777, 111, seconds=1))
    assert activated == [111]


def test_the_popup_watch_hands_focus_back_until_it_stays(monkeypatch):
    """Chrome takes the front again while its popup opens: one hand-back was not enough."""
    activated = _popup_watch(monkeypatch, [111, 777, 111, 777, 111], sticks=False)
    monkeypatch.setattr(server, "_FOCUS_STAYED", 0.05)
    asyncio.run(server._win_focus_back_from_popup(777, 111, seconds=1))
    assert activated == [111, 111]


def test_a_person_switching_apps_is_left_alone(monkeypatch):
    """Their own Chrome has another PID, so only this browser's process counts."""
    activated = _popup_watch(monkeypatch, [111, 222, 333])
    asyncio.run(server._win_focus_back_from_popup(777, 111, seconds=0.1))
    assert activated == []


def test_browser_process_pid_of_an_attached_browser_is_none():
    assert server._browser_process_pid(SimpleNamespace(_process=None)) is None
    assert server._browser_process_pid(SimpleNamespace(_process=SimpleNamespace(pid=5))) == 5
    assert server._browser_process_pid(SimpleNamespace(_process_pid=6)) == 6


def test_headless_launch_touches_no_focus(monkeypatch):
    order = _fake_launch(monkeypatch)
    out = _launch(headless=True)
    assert "launched" in out, out
    assert "read" not in order and ("give_back", None) in order


def test_navigate_no_longer_raises_the_window_by_default():
    fn = server.mcp._tool_manager._tools["navigate"].fn
    assert inspect.signature(fn).parameters["focus"].default is False


def test_navigate_with_focus_still_raises_it(monkeypatch):
    raised = []

    class Tab(_Tab):
        async def bring_to_front(self):
            raised.append(True)

    tab = Tab()

    async def arm(_tab):
        return {}, lambda: {}

    async def observe(*_a, **_kw):
        return {"final_url": "about:blank"}

    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    monkeypatch.setattr(server, "_arm_document_watch", arm)
    monkeypatch.setattr(server, "_observe_navigation", observe)
    fn = server.mcp._tool_manager._tools["navigate"].fn
    asyncio.run(fn(url="about:blank", auto_verify=False))
    assert raised == []
    asyncio.run(fn(url="about:blank", auto_verify=False, focus=True))
    assert raised == [True]


def test_key_events_carry_no_native_code():
    """It holds later mouse events for 5 seconds on macOS. See AGENTS.md."""
    assert "native_virtual_key_code" not in identity.key_event_params("Escape")
    assert identity.key_event_params("Escape")["windows_virtual_key_code"] == 27


def test_chrome_flags_keep_a_profile_path_with_spaces(tmp_path):
    root = tmp_path / "Application Support"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("54321\n/devtools/browser/x\n")
    cmd = f"/x/Chrome --remote-debugging-port=0 --user-data-dir={root} --no-first-run"
    assert attach._chrome_flags_from_args(cmd) == (54321, str(root))
    assert attach._chrome_flags_from_args("/x/Chrome --user-data-dir=/a/b") == (None, "/a/b")
    assert attach._chrome_flags_from_args("/x/Chrome --remote-debugging-port=9222") == (9222, None)


# --- tab_new: its own window, or a tab behind in an existing one ------------------------------

@pytest.mark.parametrize("headless, new_window, mode", [
    (False, None, "window"), (False, True, "window"), (False, False, "behind"),
    (True, None, "tab"), (True, False, "tab"), (True, True, "window"),
])
def test_tab_new_mode(headless, new_window, mode):
    from mcp_stealth_chrome.tools import navigation
    assert navigation._tab_new_mode(headless, new_window) == mode


def _behind(monkeypatch, windows, state, emulation_error=None):
    from mcp_stealth_chrome.tools import navigation
    old, new = SimpleNamespace(name="old"), SimpleNamespace(name="new")

    async def window_of(_browser, tab):
        return windows[tab.name]

    async def visibility(_tab):
        return state

    monkeypatch.setattr(server, "_window_of", window_of)
    monkeypatch.setattr(server, "_page_visibility", visibility)
    return asyncio.run(navigation._behind_note(object(), new, old, emulation_error))


def test_a_tab_behind_that_reads_visible_says_so(monkeypatch):
    note = _behind(monkeypatch, {"old": 1, "new": 1}, "visible")
    assert "in the same window" in note and "reads as visible" in note and "hidden" not in note


def test_a_tab_behind_that_reads_hidden_says_so_and_why(monkeypatch):
    """Focus emulation failing leaves the page hidden. Saying "opened" alone would read as shown."""
    note = _behind(monkeypatch, {"old": 1, "new": 1}, "hidden", "RuntimeError: nope")
    assert "reads as hidden" in note and "focus emulation failed" in note and "focus=True" in note
    assert "reads as visible" not in note


def test_a_tab_that_landed_in_another_window_says_so(monkeypatch):
    note = _behind(monkeypatch, {"old": 1, "new": 2}, "visible")
    assert "another existing window" in note and "same window" not in note


def test_an_unreadable_visibility_is_not_reported_as_visible(monkeypatch):
    note = _behind(monkeypatch, {"old": None, "new": None}, None)
    assert "could not be read" in note and "reads as visible" not in note


def test_tab_select_says_when_the_selected_page_reads_hidden(monkeypatch):
    activated, select = _tab_select_fixture(monkeypatch, headless=False)

    async def hidden(_tab):
        return "hidden"

    monkeypatch.setattr(server, "_page_visibility", hidden)
    out = asyncio.run(select(index=1))
    assert "switched to tab 1" in out and "reads as hidden" in out and activated == []


def test_tab_select_with_focus_does_not_warn(monkeypatch):
    activated, select = _tab_select_fixture(monkeypatch, headless=False)

    async def hidden(_tab):
        return "hidden"

    monkeypatch.setattr(server, "_page_visibility", hidden)
    assert "hidden" not in asyncio.run(select(index=1, focus=True)) and activated == [True]


def test_tab_new_same_window_headed_uses_a_background_tab_with_emulation(monkeypatch):
    calls = []

    async def behind(_browser):
        calls.append("behind")
        raise RuntimeError("stop here")

    async def window(_browser, *_a):
        calls.append("window")
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_open_background_tab", behind)
    monkeypatch.setattr(server, "_open_background_window", window)
    monkeypatch.setattr(server.BrowserState, "browser", SimpleNamespace(tabs=[]), raising=False)
    fn = server.mcp._tool_manager._tools["tab_new"].fn
    monkeypatch.setattr(server.BrowserState, "current_headless", False, raising=False)
    asyncio.run(fn(url="about:blank", new_window=False))
    monkeypatch.setattr(server.BrowserState, "current_headless", True, raising=False)
    asyncio.run(fn(url="about:blank", new_window=True))
    assert calls == ["behind", "window"]


def test_background_tab_is_asked_for_without_a_new_window():
    sent = []

    class Conn:
        async def send(self, command):
            sent.append(_cdp_params(command))
            return "new-target"

    async def update():
        return None

    tab = SimpleNamespace(target=SimpleNamespace(target_id="new-target"))
    browser = SimpleNamespace(connection=Conn(), update_targets=update, tabs=[tab])
    assert asyncio.run(server._open_background_tab(browser)) is tab
    assert sent[0]["method"] == "Target.createTarget"
    assert sent[0]["params"]["background"] is True and not sent[0]["params"].get("newWindow")
