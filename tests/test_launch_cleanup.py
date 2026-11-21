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
# stop() returned, but there was no process to check: asked to stop, not seen to end.
_UNVERIFIED = {"stopped": True, "verified": False, "pid": None, "killed": [], "still_alive": [], "detail": ""}
_NOT_SEEN = "stop was requested, but no process could be checked to confirm it ended"
_KEPT_FIELDS = ("current_profile_dir", "current_privacy", "current_headless", "current_fingerprint",
                "current_proxy", "current_idle_timeout")


async def _noop(*_a, **_kw):
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    order = _fake_launch(monkeypatch)
    calls = SimpleNamespace(order=order, preflights=0, preflight={}, commit="", move=None,
                            finish=(True, ""), close_report=dict(_CLOSED), closes=[], started=[],
                            during_close=None,
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
        if calls.during_close is not None:
            await calls.during_close()
        await asyncio.sleep(0)  # a real close awaits (Browser.close, then a poll)
        return dict(calls.close_report)

    monkeypatch.setattr(server, "_start_chrome", start)
    monkeypatch.setattr(server, "_prepare_cookie_move", prepare)
    monkeypatch.setattr(server, "_cookie_preflight", preflight)
    monkeypatch.setattr(server, "_commit_cookie_move", commit)
    monkeypatch.setattr(server, "_finish_cookie_move", finish)
    monkeypatch.setattr(server, "_ensure_browser_dead", dead)
    saved = {name: getattr(server.BrowserState, name) for name in _KEPT_FIELDS}
    saved_instance, saved_instances = server.BrowserState.current_instance_id, dict(server.BrowserState.instances)
    server.BrowserState.reset()
    try:
        yield calls
    finally:
        server.BrowserState.reset()
        server.BrowserState.current_instance_id = saved_instance
        server.BrowserState.instances = saved_instances
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
    assert "could not be closed" in out and "4321" in out and "call browser_close to try again" in out, out
    folders = _privacy_folders(env)
    assert len(folders) == 1, "a privacy profile was deleted under a Chrome still running"
    assert server.BrowserState.browser is env.started[0], "the only handle on a live Chrome was dropped"
    assert server.BrowserState.current_profile_dir == folders[0] and server.BrowserState.current_privacy, \
        "the kept handle does not name its profile, so nothing would remove it"


class _TabThatCannotLoad:
    target = SimpleNamespace(target_id="t1")
    browser = None

    def __init__(self, exc):
        self.exc = exc

    async def get(self, *_a, **_kw):
        raise self.exc

    async def evaluate(self, *_a, **_kw):
        return "complete"

    async def send(self, *_a, **_kw):
        return None


def _fail_after_the_start(monkeypatch, where):
    """Make the launch fail after Chrome started; returns the text the result must carry."""
    if where == "storage_state":
        async def broken(_browser, _path):
            raise ValueError("no cookies key in the file")
        monkeypatch.setattr(server, "_apply_storage_state", broken)
        return "storage_state load failed: no cookies key in the file"
    exc = asyncio.TimeoutError() if where == "navigation timeout" else RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    async def first_tab(_browser):
        return _TabThatCannotLoad(exc)

    monkeypatch.setattr(server, "_first_live_tab", first_tab)
    return ("initial nav timed out after" if where == "navigation timeout"
            else "initial nav failed: net::ERR_NAME_NOT_RESOLVED")


@pytest.mark.parametrize("where", ["storage_state", "navigation", "navigation timeout"])
def test_a_launch_that_fails_after_the_start_ends_its_browser_as_every_close_does(env, monkeypatch, where):
    """_ensure_browser_dead, not stop() alone: stop() can return while Chrome keeps running."""
    said = _fail_after_the_start(monkeypatch, where)
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
    assert said in out and "could not be ended" not in out, out
    assert [b for b, _p in env.closes] == env.started, "the browser was not ended through _ensure_browser_dead"
    assert env.closes[0][1].parent == env.privacy_root, env.closes
    assert server.BrowserState.browser is None
    assert _privacy_folders(env) == []


@pytest.mark.parametrize("where", ["storage_state", "navigation"])
def test_a_browser_that_could_not_be_ended_after_a_failed_start_is_kept_and_said(env, monkeypatch, where):
    said = _fail_after_the_start(monkeypatch, where)
    env.close_report = dict(_ALIVE)
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
    assert said in out and "could not be ended" in out and "4321" in out, out
    assert "call browser_close to try again" in out, out
    folders = _privacy_folders(env)
    assert len(folders) == 1, "a privacy profile was deleted under a Chrome still running"
    assert server.BrowserState.browser is env.started[0], "the only handle on a live Chrome was dropped"
    assert server.BrowserState.current_profile_dir == folders[0] and server.BrowserState.current_privacy


def _spy_profile_writes(env, monkeypatch):
    """Record when the vertical_tabs check and write, brave_lean and the tidy-up run, in env.order.
    The write calls the check itself (looked up in brave at call time), so it shows twice."""
    from mcp_stealth_chrome import brave as brave_mode
    real_check, real_tabs, real_lean, real_clean = (
        brave_mode.check_vertical_tabs, brave_mode.write_vertical_tabs, server._apply_brave_lean,
        server.clean_profile_state)

    def check(*a, **kw):
        env.order.append("check_vertical_tabs")
        return real_check(*a, **kw)

    def tabs(*a, **kw):
        env.order.append("vertical_tabs")
        return real_tabs(*a, **kw)

    def lean(*a, **kw):
        env.order.append("brave_lean")
        return real_lean(*a, **kw)

    def clean(*a, **kw):
        env.order.append("tidy")
        return real_clean(*a, **kw)

    monkeypatch.setattr(brave_mode, "check_vertical_tabs", check)
    monkeypatch.setattr(brave_mode, "write_vertical_tabs", tabs)
    monkeypatch.setattr(server, "_apply_brave_lean", lean)
    monkeypatch.setattr(server, "clean_profile_state", clean)


@pytest.mark.parametrize("refusal", ["preflight", "commit"])
def test_a_refused_launch_writes_neither_vertical_tabs_nor_lean(env, monkeypatch, refusal):
    """The guide's rule: a refused launch writes nothing to the profile. Both writes ran before the
    cookie preflight and the commit, so a refusal there left the tab setting in Preferences."""
    _spy_profile_writes(env, monkeypatch)
    if refusal == "preflight":
        env.preflight = {1: "Chrome PID 99 opened it after this launch checked it"}
    else:
        env.commit = "the strip stopped: a row changed since the backup"
    out = _launch(vertical_tabs="left")
    assert out.startswith("Error") and "start" not in env.order, out
    assert "vertical_tabs" not in env.order and "brave_lean" not in env.order, env.order
    assert not env.profile.exists(), "the refused launch wrote into the profile"


def test_vertical_tabs_and_lean_are_written_after_the_commit_and_before_the_start(env, monkeypatch):
    """vertical_tabs is checked before the preflight, written after the commit (checking again
    right before it writes), then lean and the tidy-up, then the last look and the start."""
    _spy_profile_writes(env, monkeypatch)
    out = _launch(vertical_tabs="left")
    assert "[vertical tabs: on the left]" in out and not out.startswith("Error"), out
    steps = [s for s in env.order if s in ("prepare", "check_vertical_tabs", "preflight1", "commit",
                                          "vertical_tabs", "brave_lean", "tidy", "preflight2", "start")]
    assert steps == ["prepare", "check_vertical_tabs", "preflight1", "commit", "vertical_tabs",
                     "check_vertical_tabs", "brave_lean", "tidy", "preflight2", "start"], steps
    from mcp_stealth_chrome import brave as brave_mode
    assert brave_mode.vertical_tabs_read_back(env.profile, ["--profile-directory=Default"], "chrome") == "left"


def test_vertical_tabs_that_cannot_be_written_still_start_nothing(env, monkeypatch):
    """An unreadable Preferences is refused earlier (the binding check), so the way vertical_tabs
    still fails is a profile it cannot see free. The check runs before the preflight, so that
    refusal comes before anything was written: nothing but the cookie preparation ran."""
    monkeypatch.setattr(state, "processes_naming", lambda _root: None)
    out = _launch(vertical_tabs="left")
    assert "vertical_tabs could not be written into the profile, so nothing was started" in out, out
    assert "cannot be told" in out, out
    assert env.order == ["prepare"], f"the refusal is expected before the preflight: {env.order}"
    assert not env.profile.exists(), "the refused launch wrote into the profile"


def test_a_profile_taken_between_the_check_and_the_write_refuses_after_the_commit(env, monkeypatch):
    """The narrow case left: the check passed, then a browser took the profile before the write.
    The write checks again right before it writes, so it refuses, after the commit (a pending
    marker from that commit is what the next launch resumes), and nothing starts."""
    taken = []
    monkeypatch.setattr(state, "processes_naming",
                        lambda _root: [(4242, "Google Chrome --user-data-dir=x")] if taken else [])

    async def commit_then_taken(_profile, _move):
        env.order.append("commit")
        taken.append(True)  # a browser takes the profile right after the commit
        return ""

    monkeypatch.setattr(server, "_commit_cookie_move", commit_then_taken)
    out = _launch(vertical_tabs="left")
    assert "vertical_tabs could not be written into the profile, so nothing was started" in out, out
    assert "in use by PID 4242" in out, out
    assert env.order == ["prepare", "preflight1", "commit"], env.order
    assert not (env.profile / "Default" / "Preferences").exists()


def test_the_vertical_tabs_check_writes_nothing_and_raises_as_the_write_does(tmp_path):
    from mcp_stealth_chrome import brave as brave_mode
    args = ["--profile-directory=Default"]
    assert brave_mode.check_vertical_tabs(tmp_path / "new", args, None) is None
    prefs, data = brave_mode.check_vertical_tabs(tmp_path / "new", args, "left")
    assert prefs == tmp_path / "new" / "Default" / "Preferences" and data == {}
    assert not (tmp_path / "new").exists(), "the check wrote something"
    (tmp_path / "list" / "Default").mkdir(parents=True)
    (tmp_path / "list" / "Default" / "Preferences").write_text("[]")
    for call in (lambda: brave_mode.check_vertical_tabs(tmp_path / "list", args, "left"),
                 lambda: brave_mode.write_vertical_tabs(tmp_path / "list", args, "chrome", "left")):
        with pytest.raises(ValueError, match="does not hold a settings object"):
            call()
    assert (tmp_path / "list" / "Default" / "Preferences").read_text() == "[]"


@pytest.mark.parametrize("asked,testing_mode,expected", [
    (None, False, True), (None, True, False), (True, True, True), (False, False, False)])
def test_unset_persistent_is_true_except_under_testing_mode(asked, testing_mode, expected):
    """Only the value: a description of where it came from was computed, returned and never read."""
    assert server._launch_default_persistent(asked, testing_mode) is expected


class _DegenerateWindowTab:
    def __init__(self, probes):
        self.probes, self.calls = list(probes), []

    async def evaluate(self, *_a, **_kw):
        import json
        return json.dumps(self.probes.pop(0))

    async def get_window(self):
        self.calls.append("get_window")
        return 1, None

    async def set_window_state(self, state="normal", **_kw):
        self.calls.append(f"set_window_state:{state}")

    async def bring_to_front(self):
        self.calls.append("bring_to_front")

    async def activate(self):
        self.calls.append("activate")


@pytest.mark.parametrize("second,note", [({"w": 1280, "h": 800, "v": "visible"}, False),
                                         ({"w": 0, "h": 0, "v": "hidden"}, True)])
def test_the_health_probe_restores_a_degenerate_window_without_raising_it(second, note):
    """One nudge: the window state set back to normal, never brought to the front (taking the
    foreground to check on a window is what this launch must not do), then one more probe."""
    import inspect
    tab = _DegenerateWindowTab([{"w": 0, "h": 0, "v": "hidden", "sx": 0, "sy": 0}, second])
    out = asyncio.run(server._launch_window_health(SimpleNamespace(headless=False), tab))
    assert tab.calls == ["get_window", "set_window_state:normal"], tab.calls
    assert ("window degenerate" in out) is note, out
    source = inspect.getsource(server._launch_window_health)
    assert "bring_to_front" not in source and "activate" not in source, \
        "the health probe names a call that raises the window"


def test_no_spelling_of_user_data_dir_in_extra_args_survives_the_first_check():
    """_profile_from_extra_args turns a --user-data-dir in extra_args into the launch's
    user_data_dir, or refuses one with no value, before any other check. brave._SWITCH matches
    every argument starting with "--user-data-dir=" (two dashes, the name, "=", then anything,
    newlines included), so no later check needs to look for one in extra_args."""
    import random
    import string
    rng = random.Random(20261121)
    alphabet = string.printable + "\u00e4\u00e9\u6f22\u2028\u00a0\x00"
    values = ["", " ", "\t", "\n", "/x", "~/p", "=x", "a b", "/a\n/b", "C:\\U\\p", "--user-data-dir=/y"]
    values += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))) for _ in range(3000)]
    for value in values:
        arg = f"--user-data-dir={value}"
        for args in ([arg], ["--foo", arg, "--bar=1"], [arg, arg]):
            udd, rest, problem = server._profile_from_extra_args(None, args, "user_data_dir")
            if problem:
                continue
            assert not any(str(a).startswith("--user-data-dir=") for a in rest), (args, rest)
            assert udd == value.strip(), (args, udd)


def test_privacy_with_a_user_data_dir_in_extra_args_is_still_refused(env, tmp_path):
    out = _launch(privacy=True, extra_args=[f"--user-data-dir={tmp_path / 'theirs'}"])
    assert "privacy mode cannot reuse user_data_dir or profile_directory" in out, out
    assert "start" not in env.order


def test_a_user_data_dir_in_extra_args_that_another_chrome_holds_is_refused_with_its_pid(env, tmp_path, monkeypatch):
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242 if p == theirs else None)
    monkeypatch.setattr(server, "_kept_verdict", lambda *_a, **_kw: None)
    out = _launch(extra_args=[f"--user-data-dir={theirs}"])
    assert f"user_data_dir {theirs} is currently in use by Chrome PID 4242" in out, out
    assert "start" not in env.order


@pytest.mark.parametrize("report", ["closed", "alive"])
@pytest.mark.parametrize("where", ["storage_state", "navigation", "cookie_move"])
def test_a_switch_during_the_close_of_a_failed_launch_leaves_the_other_instance_alone(
        env, monkeypatch, tmp_path, where, report):
    """The close of a failed launch awaits; a switch_instance sent meanwhile used to go through,
    and the close then reset the instance switched in (verified) or gave it this launch's profile
    (not ended). Registered as closing, the instance makes the switch wait for the close."""
    from mcp_stealth_chrome.state import InstanceSnapshot
    from test_focus_restore import _Tab
    other_browser = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)
    other_profile = tmp_path / "other-profile"
    server.BrowserState.instances["other"] = InstanceSnapshot(
        instance_id="other", browser=other_browser, tabs=list(other_browser.tabs), profile_dir=other_profile)
    if where == "cookie_move":
        env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
        env.finish = (False, "1 cookie missing")
    else:
        _fail_after_the_start(monkeypatch, where)
    if report == "alive":
        env.close_report = dict(_ALIVE)
    switch = {}

    async def switch_meanwhile():
        switch["task"] = asyncio.ensure_future(
            server.mcp.call_tool("switch_instance", {"instance_id": "other"}))
        await asyncio.sleep(0.05)  # long enough for the switch to go through, were it let

    env.during_close = switch_meanwhile
    launch = server.mcp._tool_manager._tools["browser_launch"].fn

    async def run():
        out = await launch(auto_verify=False, headless=True, privacy=True,
                           storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
        return out, await asyncio.wait_for(switch["task"], 5)

    out, switched = asyncio.run(run())
    state_now = server.BrowserState
    assert out.startswith("Error"), out
    assert "switched to 'other'" in str(switched), switched
    assert state_now.current_instance_id == "other"
    assert state_now.browser is other_browser, "the instance switched in lost its browser to the close"
    assert state_now.current_profile_dir == other_profile, "the close wrote this launch's profile onto it"
    folders = _privacy_folders(env)
    if report == "closed":
        assert "main" not in state_now.instances, "the ended browser was kept as instance main's handle"
        assert folders == []
    else:
        kept = state_now.instances.get("main")
        assert kept is not None and kept.browser is env.started[0], "the live browser's handle was dropped"
        assert len(folders) == 1 and kept.profile_dir == folders[0] and kept.privacy, \
            "the kept handle does not name its own profile"


def test_a_close_that_finds_another_instance_current_records_nothing_on_it(env, monkeypatch, tmp_path):
    """Should the instance change all the same (a switch that does not go through a tool call),
    the close resets nothing and writes nothing onto the instance that is current by then, and
    the result says its outcome could not be recorded."""
    from mcp_stealth_chrome.state import InstanceSnapshot
    from test_focus_restore import _Tab
    other_browser = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)
    other_profile = tmp_path / "other-profile"
    server.BrowserState.instances["other"] = InstanceSnapshot(
        instance_id="other", browser=other_browser, tabs=list(other_browser.tabs), profile_dir=other_profile)
    _fail_after_the_start(monkeypatch, "storage_state")

    async def switch_directly():
        server.BrowserState.switch_to("other")

    env.during_close = switch_directly
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json")
    assert "storage_state load failed" in out and "could not be recorded on it" in out, out
    assert server.BrowserState.browser is other_browser
    assert server.BrowserState.current_profile_dir == other_profile


@pytest.mark.parametrize("where", ["storage_state", "navigation", "navigation timeout", "cookie_move"])
def test_a_stop_that_was_not_seen_to_end_is_said(env, monkeypatch, tmp_path, where):
    """_ensure_browser_dead can answer stopped with nothing verified (no process to check), which
    _close_incomplete does not count as a failure: the handle goes, and the result must say the
    end was not seen, as browser_close does, rather than nothing or "The browser was closed"."""
    if where == "cookie_move":
        env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
        env.finish = (False, "1 cookie missing")
        said = "its cookies were not all moved: 1 cookie missing. The browser was asked to stop"
    else:
        said = _fail_after_the_start(monkeypatch, where)
    env.close_report = dict(_UNVERIFIED)
    out = _launch(storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
    assert said in out and _NOT_SEEN in out, out
    assert out.index(said) < out.index(_NOT_SEEN), "the note comes after the original error"
    assert "The browser was closed" not in out, out
