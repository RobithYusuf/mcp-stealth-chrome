"""A headed window must not take focus from the person. Fakes only: no window opens here."""
import asyncio

import pytest
import inspect
import subprocess
from types import SimpleNamespace

from mcp_stealth_chrome import identity, server


def test_frontmost_app_is_read_from_lsappinfo(monkeypatch):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        out = "ASN:0x0-0x1:\n" if cmd[1] == "front" else '"LSBundlePath"="/Applications/Orca.app"\n'
        return SimpleNamespace(stdout=out)

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._frontmost_app() == "/Applications/Orca.app"
    assert calls[1][-1] == "ASN:0x0-0x1:"


def test_frontmost_app_is_nothing_off_macos_or_on_failure(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    assert server._frontmost_app() is None
    monkeypatch.setattr(server.sys, "platform", "darwin")

    def boom(*_a, **_kw):
        raise subprocess.TimeoutExpired("lsappinfo", 2)

    monkeypatch.setattr(server.subprocess, "run", boom)
    assert server._frontmost_app() is None


def _record_open(monkeypatch, front):
    opened = []

    def fake_run(cmd, **_kw):
        opened.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "_frontmost_app", lambda: front)
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return opened


def test_focus_goes_back_when_chrome_took_it(monkeypatch):
    opened = _record_open(monkeypatch, "/Applications/Google Chrome.app")
    asyncio.run(server._give_focus_back("/Applications/Orca.app"))
    assert opened == [["open", "-a", "/Applications/Orca.app"]]


def test_focus_is_left_alone_when_nothing_took_it(monkeypatch):
    opened = _record_open(monkeypatch, "/Applications/Orca.app")
    asyncio.run(server._give_focus_back("/Applications/Orca.app"))
    assert opened == []
    opened = _record_open(monkeypatch, None)
    asyncio.run(server._give_focus_back("/Applications/Orca.app"))
    asyncio.run(server._give_focus_back(None))
    assert opened == []


def test_a_failure_giving_focus_back_never_fails_the_launch(monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("no open")

    monkeypatch.setattr(server, "_frontmost_app", lambda: "/Applications/Chrome.app")
    monkeypatch.setattr(server.subprocess, "run", boom)
    asyncio.run(server._give_focus_back("/Applications/Orca.app"))


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

    async def give_back(app):
        order.append(("give_back", app))

    async def noop(*_a, **_kw):
        return None

    async def first_tab(_browser):
        return _Tab()

    async def arm(_tab):
        return {}, lambda: {}

    async def observe(*_a, **_kw):
        return {"final_url": "about:blank", "status": None}

    monkeypatch.setattr(server, "_frontmost_app", lambda: order.append("read") or "/Applications/Orca.app")
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
    assert order[-1] == ("give_back", "/Applications/Orca.app")


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
        return None

    monkeypatch.setattr(server, "_refresh_tabs", refresh)
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


def test_frontmost_pid_is_read_from_lsappinfo(monkeypatch):
    def fake_run(cmd, **_kw):
        return SimpleNamespace(stdout="ASN:0x0-0x1:\n" if cmd[1] == "front" else '"pid"=4242\n')

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._frontmost_pid() == 4242
    monkeypatch.setattr(server.sys, "platform", "linux")
    assert server._frontmost_pid() is None


def _popup_watch(monkeypatch, fronts):
    opened = []
    seq = iter(fronts)

    def fake_run(cmd, **_kw):
        opened.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "_frontmost_pid", lambda: next(seq, fronts[-1]))
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return opened


def test_a_popup_taking_focus_gets_it_sent_back(monkeypatch):
    opened = _popup_watch(monkeypatch, [111, 111, 777])
    asyncio.run(server._win_focus_back_from_popup(777, "/Applications/Orca.app", seconds=1))
    assert opened == [["open", "-a", "/Applications/Orca.app"]]


def test_a_person_switching_apps_is_left_alone(monkeypatch):
    """Their own Chrome has another PID, so only this browser's process counts."""
    opened = _popup_watch(monkeypatch, [111, 222, 333])
    asyncio.run(server._win_focus_back_from_popup(777, "/Applications/Orca.app", seconds=0.1))
    assert opened == []


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
    assert server._chrome_flags_from_args(cmd) == (54321, str(root))
    assert server._chrome_flags_from_args("/x/Chrome --user-data-dir=/a/b") == (None, "/a/b")
    assert server._chrome_flags_from_args("/x/Chrome --remote-debugging-port=9222") == (9222, None)


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
