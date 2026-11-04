"""A headed window must not take focus from the person. Fakes only: no window opens here."""
import asyncio
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

    async def start(_config):
        order.append("start")
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
