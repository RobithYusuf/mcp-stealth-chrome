"""browser_launch's failure paths and the order of its writes to the profile. Fakes only: no Chrome
starts, and every folder is under tmp_path (the profile, the privacy profiles). The fixture also
stubs the kept-browser search, which would otherwise look at the real main profile."""
import asyncio
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import server, state
from test_focus_restore import _fake_launch

_CLOSED = {"stopped": True, "verified": True, "pid": 1, "killed": [], "still_alive": [], "detail": ""}
_ALIVE = {"stopped": True, "verified": False, "pid": 1, "killed": [], "still_alive": [4321], "detail": ""}
_KEPT_FIELDS = ("current_profile_dir", "current_privacy", "current_headless", "current_fingerprint",
                "current_proxy", "current_idle_timeout")


async def _noop(*_a, **_kw):
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    order = _fake_launch(monkeypatch)
    calls = SimpleNamespace(order=order, preflights=0, preflight={}, commit="", move=None,
                            finish=(True, ""), close_report=dict(_CLOSED), closes=[], started=[],
                            profile=tmp_path / "profile", privacy_root=tmp_path / "privacy-profiles")
    monkeypatch.setattr(server, "PRIVACY_PROFILE_ROOT", calls.privacy_root)
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server, "_kept_candidates", lambda *_a, **_kw: [])
    monkeypatch.setattr(server, "_resolve_launch_profile",
                        lambda _id, persistent=True, **_kw: (calls.profile if persistent else None, {}))
    monkeypatch.setattr(server, "_launch_identity", lambda *_a, **_kw: (None, {}, "native"))
    monkeypatch.setattr(server, "_record_tabs_at_launch", _noop)
    # A persistent launch of "main" settles the process's profile affinity; put it back after.
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", state._PROFILE_AFFINITY)
    fake_start = server._start_chrome

    async def start(config):
        browser = await fake_start(config)
        calls.started.append(browser)
        return browser

    async def prepare(_profile, _config):
        order.append("prepare")
        return calls.move or server._CookieMove(plan=None)

    async def preflight(_profile, _move):
        calls.preflights += 1
        order.append(f"preflight{calls.preflights}")
        return calls.preflight.get(calls.preflights, "")

    async def commit(_profile, _move):
        order.append("commit")
        return calls.commit

    async def finish(_browser, _profile, _move):
        order.append("finish")
        return calls.finish

    async def dead(browser, profile_dir=None, grace=1.5):
        calls.closes.append((browser, profile_dir))
        order.append("ensure_dead")
        return dict(calls.close_report)

    monkeypatch.setattr(server, "_start_chrome", start)
    monkeypatch.setattr(server, "_prepare_cookie_move", prepare)
    monkeypatch.setattr(server, "_cookie_preflight", preflight)
    monkeypatch.setattr(server, "_commit_cookie_move", commit)
    monkeypatch.setattr(server, "_finish_cookie_move", finish)
    monkeypatch.setattr(server, "_ensure_browser_dead", dead)
    saved = {name: getattr(server.BrowserState, name) for name in _KEPT_FIELDS}
    server.BrowserState.reset()
    try:
        yield calls
    finally:
        server.BrowserState.reset()
        for name, value in saved.items():
            setattr(server.BrowserState, name, value)


def _launch(**kw):
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    return asyncio.run(fn(auto_verify=False, headless=kw.pop("headless", True), **kw))


def _privacy_folders(env):
    return sorted(env.privacy_root.iterdir()) if env.privacy_root.exists() else []


def test_a_refusal_at_the_last_look_removes_the_privacy_profile(env):
    """Every refusal after the privacy profile is made removes it; the last look before the
    start did not, and left the folder behind."""
    env.preflight = {2: "its cookie database changed after this launch read it"}
    out = _launch(privacy=True)
    assert out.startswith("Error") and "changed after this launch read it" in out, out
    assert "start" not in env.order, env.order
    assert _privacy_folders(env) == [], "the refused privacy launch left its profile behind"


def test_a_cookie_move_left_unfinished_removes_the_privacy_profile_once_the_browser_is_gone(env, tmp_path):
    env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
    env.finish = (False, "2 cookies read back differently")
    out = _launch(privacy=True)
    assert "its cookies were not all moved: 2 cookies read back differently" in out, out
    assert "The browser was closed" in out, out
    assert [b for b, _p in env.closes] == env.started, "the browser was not ended"
    assert _privacy_folders(env) == [], "the failed privacy launch left its profile behind"


def test_a_browser_left_running_by_an_unfinished_move_keeps_its_privacy_profile_and_names_it(env, tmp_path):
    """A profile deleted under a live Chrome is written back, so it stays; the handle that is kept
    names it, so browser_close or the next launch ends that browser and then removes it."""
    env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
    env.finish = (False, "1 cookie missing")
    env.close_report = dict(_ALIVE)
    out = _launch(privacy=True)
    assert "could not be closed" in out and "4321" in out, out
    folders = _privacy_folders(env)
    assert len(folders) == 1, "a privacy profile was deleted under a Chrome still running"
    assert server.BrowserState.browser is env.started[0], "the only handle on a live Chrome was dropped"
    assert server.BrowserState.current_profile_dir == folders[0] and server.BrowserState.current_privacy, \
        "the kept handle does not name its profile, so nothing would remove it"
