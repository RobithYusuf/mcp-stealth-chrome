"""browser_launch's failure paths and the order of its writes to the profile. Fakes only: no Chrome
starts, and every folder is under tmp_path (the profile, the privacy profiles). The fixture also
stubs the kept-browser search, which would otherwise look at the real main profile."""
import asyncio
import sys
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
                            during_close=None, written_at_look=[],
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

    async def preflight(_profile, _move, written=None):
        calls.preflights += 1
        order.append(f"preflight{calls.preflights}")
        calls.written_at_look.append(written)
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


def _existing_profile(env):
    """A profile with something in it, as a launch finds a used one: the fixture's resolver and
    ensure_dirs make no folder, so "the folder is still missing" would prove nothing about what
    the launch writes. exit_type Crashed is what the tidy-up would rewrite."""
    import json
    (env.profile / "Default").mkdir(parents=True)
    (env.profile / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed"}}))
    return _digest(env.profile)


def _digest(root):
    return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else b"")
                  for p in root.rglob("*"))


async def _close_through_a_tool_call():
    """browser_close as a client sends it: through the call wrapper, which waits while the
    instance is registered as closing."""
    result = await asyncio.wait_for(server.mcp.call_tool("browser_close", {}), 10)
    return str(result)


def test_a_refusal_at_the_last_look_removes_the_privacy_profile(env):
    """Every refusal after the privacy profile is made removes it; the last look before the
    start did not, and left the folder behind."""
    env.preflight = {2: "its cookie database changed after this launch read it"}
    out = _launch(privacy=True)
    assert out.startswith("Error") and "changed after this launch read it" in out, out
    assert "start" not in env.order, env.order
    assert _privacy_folders(env) == [], "the refused privacy launch left its profile behind"


def _then_browser_close_ends_it(env, folder):
    """The browser_close a kept handle asks for acts on this launch's browser and profile: still
    running, it fails and keeps both; once it ends, the privacy profile goes."""
    browser = env.started[0]
    again = asyncio.run(_close_through_a_tool_call())
    assert "close failed" in again and "handle kept" in again, again
    assert env.closes[-1] == (browser, folder), env.closes[-1]
    assert server.BrowserState.browser is browser and _privacy_folders(env) == [folder]
    env.close_report = dict(_CLOSED)
    closed = asyncio.run(_close_through_a_tool_call())
    assert "privacy profile removed" in closed, closed
    assert env.closes[-1] == (browser, folder), env.closes[-1]
    assert server.BrowserState.browser is None and _privacy_folders(env) == []


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
    _then_browser_close_ends_it(env, folders[0])


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
    return ("the page did not answer within" if where == "navigation timeout"
            else "initial nav failed: net::ERR_NAME_NOT_RESOLVED")


@pytest.mark.parametrize("where", ["storage_state", "navigation"])
def test_a_launch_that_fails_after_the_start_ends_its_browser_as_every_close_does(env, monkeypatch, where):
    """_ensure_browser_dead, not stop() alone: stop() can return while Chrome keeps running."""
    said = _fail_after_the_start(monkeypatch, where)
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
    assert said in out and "could not be ended" not in out, out
    assert [b for b, _p in env.closes] == env.started, "the browser was not ended through _ensure_browser_dead"
    assert env.closes[0][1].parent == env.privacy_root, env.closes
    assert server.BrowserState.browser is None
    assert _privacy_folders(env) == []


def test_commit_timeout_keeps_the_privacy_profile_until_close(env, monkeypatch):
    said = _fail_after_the_start(monkeypatch, "navigation timeout")
    out = _launch(privacy=True)
    assert out.startswith("Error:") and said in out, out
    assert not env.closes and server.BrowserState.browser is env.started[0]
    assert _privacy_folders(env) == [server.BrowserState.current_profile_dir]
    assert "privacy profile removed" in asyncio.run(_close_through_a_tool_call())
    assert server.BrowserState.browser is None and not _privacy_folders(env)


def test_a_failed_privacy_launch_leaves_no_privacy_profile_to_clean_up(env, monkeypatch):
    """The profile is published with the handle (current_privacy among it); once the failed launch
    removed that folder, the instance does not name a privacy profile any more, so browser_close
    does not claim to remove one."""
    _fail_after_the_start(monkeypatch, "storage_state")
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json")
    assert "storage_state load failed" in out and _privacy_folders(env) == [], out
    assert server.BrowserState.current_privacy is False
    closed = asyncio.run(_close_through_a_tool_call())
    assert "Browser was not running." in closed and "privacy profile removed" not in closed, closed


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
    _then_browser_close_ends_it(env, folders[0])


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
    before = _existing_profile(env)
    reached = ["prepare", "check_vertical_tabs", "preflight1"]
    if refusal == "preflight":
        said = "Chrome PID 99 opened it after this launch checked it"
        env.preflight = {1: said}
    else:
        said = "the strip stopped: a row changed since the backup"
        env.commit = said
        reached.append("commit")
    out = _launch(vertical_tabs="left")
    assert out.startswith(f"Error: {said}"), f"not the injected refusal: {out}"
    steps = [s for s in env.order if s in ("prepare", "check_vertical_tabs", "preflight1", "commit",
                                          "vertical_tabs", "brave_lean", "tidy", "preflight2", "start")]
    assert steps == reached, steps
    assert _digest(env.profile) == before, "the refused launch wrote into the profile"


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
    before = _existing_profile(env)
    monkeypatch.setattr(state, "processes_naming", lambda _root: None)
    out = _launch(vertical_tabs="left")
    assert "vertical_tabs could not be written into the profile, so nothing was started" in out, out
    assert "cannot be told" in out, out
    assert env.order == ["prepare"], f"the refusal is expected before the preflight: {env.order}"
    assert _digest(env.profile) == before, "the refused launch wrote into the profile"


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
    assert "The cookie step had already been committed (it had nothing to write)." in out, out
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
            # Only an empty value is refused; every other one is taken out and becomes the folder.
            assert bool(problem) == (not value.strip()), (args, problem)
            if problem:
                continue
            assert not any(str(a).startswith("--user-data-dir=") for a in rest), (args, rest)
            assert udd == value.strip(), (args, udd)
            assert rest == [a for a in args if a != arg], (args, rest)


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
    (not ended). Registered as closing, the instance makes the switch wait for the close, and the
    launch returns before the switch runs on: it is not refused (_launch_gate) but comes after
    the launch, and finds both instances as they should be. A switch sent at any other await of
    the launch is refused: tests/test_launch_owner.py."""
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
    """Should the instance change all the same (a switch that does not go through a tool call,
    so no gate sees it), the launch resets nothing and writes nothing onto the instance that is
    current by then, keeps its own error, and says what happened to its instance. Its browser
    is closed once, and its privacy profile goes with it."""
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
    assert out == ("Error: storage_state load failed: no cookies key in the file. Instance 'main' stopped "
                   "being the current one, or its browser was replaced, while this launch ran; the current "
                   "instance was left as it was."), out
    assert server.BrowserState.browser is other_browser
    assert server.BrowserState.current_profile_dir == other_profile
    assert [b for b, _p in env.closes] == env.started, env.closes
    assert _privacy_folders(env) == []


@pytest.mark.parametrize("where", ["storage_state", "navigation", "cookie_move"])
def test_a_stop_that_was_not_seen_to_end_is_said(env, monkeypatch, tmp_path, where):
    """Failures that stop a started browser say when the end was not verified."""
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


@pytest.mark.parametrize("where", ["last look", "storage_state", "navigation", "cookie_move"])
def test_a_privacy_profile_that_could_not_be_removed_is_said_and_kept_for_later(env, monkeypatch, tmp_path, where):
    """_remove_privacy_profile returns False when the process list cannot be read or a file stays
    held, and after a storage_state load the folder may hold session data. The failure is said,
    after the original error, and the path stays on the instance, so the next close removes it."""
    if where == "last look":
        env.preflight = {2: "its cookie database changed after this launch read it"}
        said = "changed after this launch read it"
    elif where == "cookie_move":
        env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
        env.finish = (False, "1 cookie missing")
        said = "its cookies were not all moved: 1 cookie missing"
    else:
        said = _fail_after_the_start(monkeypatch, where)
    real_remove = server._remove_privacy_profile

    async def cannot_remove(profile_dir):
        if profile_dir is None:
            return True
        monkeypatch.setitem(server._PRIVACY_REMOVE_FAILURES, str(profile_dir),
                            "a process still runs with it: PID 77 (crashpad)")
        return False

    monkeypatch.setattr(server, "_remove_privacy_profile", cannot_remove)
    out = _launch(privacy=True, storage_state_path="/nowhere/state.json" if where == "storage_state" else None)
    folders = _privacy_folders(env)
    assert len(folders) == 1, folders
    left = f"The privacy profile could not be removed: {folders[0]}; a process still runs with it: PID 77"
    assert said in out and left in out and out.index(said) < out.index(left), out
    assert server.BrowserState.browser is None
    assert server.BrowserState.current_profile_dir == folders[0] and server.BrowserState.current_privacy, \
        "the folder left behind is not kept on the instance, so nothing would remove it"
    monkeypatch.setattr(server, "_remove_privacy_profile", real_remove)
    closed = asyncio.run(server.mcp._tool_manager._tools["browser_close"].fn())
    assert "stale privacy profile removed" in closed, closed
    assert _privacy_folders(env) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_plain_write_error_after_the_commit_says_what_the_cookie_step_had_committed(env, tmp_path):
    """The check only reads, so a write that fails on its own (a sub-profile folder that cannot be
    written, a full disk) refuses after the commit, with nothing changed since the check. The
    refusal says nothing was started and what the cookie step had already committed."""
    import os
    sub = env.profile / "Default"
    sub.mkdir(parents=True)
    (sub / "Preferences").write_text('{"profile": {}}')
    os.chmod(sub, 0o500)  # readable, not writable: the temporary file cannot be made
    env.move = server._CookieMove(plan=None, marker={"state": "pending"}, strip=True)
    try:
        out = _launch(vertical_tabs="left")
    finally:
        os.chmod(sub, 0o700)
    assert "vertical_tabs could not be written into the profile, so nothing was started" in out, out
    assert ("The cookie step had already been committed (the cookie move's pending marker and the removal "
            "of the v10 cookies its backup holds; the next launch resumes the move).") in out, out
    assert env.order[:3] == ["prepare", "preflight1", "commit"] and "start" not in env.order, env.order
    assert (sub / "Preferences").read_text() == '{"profile": {}}'


def test_the_last_look_is_told_what_this_launch_had_written(env):
    """The last look comes after the commit, the settings and the tidy-up, so its refusal must not
    say the profile was not changed; the first look is told nothing and keeps that text."""
    env.preflight = {2: "its cookie database changed after this launch read it"}
    out = _launch(vertical_tabs="left")
    assert out.startswith("Error") and "changed after this launch read it" in out, out
    assert env.written_at_look == [
        None, "this launch had already written the vertical tabs setting in it and run its tidy-up of "
              "stale locks and window state"], env.written_at_look


def test_on_a_clean_profile_the_last_look_says_the_tidy_up_ran_not_that_it_wrote(env):
    """clean_profile_state and the window-state wipe change nothing on a clean profile (no stale
    lock, exit_type Normal), so the last look's refusal must not say this launch wrote there."""
    import json
    (env.profile / "Default").mkdir(parents=True)
    (env.profile / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Normal"}}))
    before = _digest(env.profile)
    env.preflight = {2: "its cookie database changed after this launch read it"}
    out = _launch()
    assert out.startswith("Error") and "changed after this launch read it" in out, out
    assert _digest(env.profile) == before, "the tidy-up changed a clean profile after all"
    assert env.written_at_look == [None, "this launch had already run its tidy-up of stale locks and window state"], \
        env.written_at_look


def test_the_last_look_refusal_names_the_writes_and_the_first_keeps_its_text(tmp_path):
    from mcp_stealth_chrome import cookie_migration
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    plan = SimpleNamespace(fingerprint=("not what is there",), subprofile="Default", marker=None)
    move = server._CookieMove(plan=plan)
    assert cookie_migration.fingerprint_of(profile, "Default") != plan.fingerprint
    first = asyncio.run(server._cookie_preflight(profile, move))
    assert "Nothing was started and the profile was not changed." in first, first
    written = "this launch had already written the vertical tabs setting in it"
    last = asyncio.run(server._cookie_preflight(profile, move, written=written))
    assert f"Nothing was started; {written}." in last and "the profile was not changed" not in last, last
    assert last.replace(f"Nothing was started; {written}.",
                        "Nothing was started and the profile was not changed.") == first


def test_on_brave_lean_and_the_tabs_are_written_after_the_commit_and_read_back(env, monkeypatch, tmp_path):
    """The Chrome case above only records that the lean helper was called; Chrome gets nothing from
    it. On Brave lean writes, so read back what it wrote, and the flag it adds to the start."""
    import json
    from mcp_stealth_chrome import brave as brave_mode, brave_lean
    wrapper = tmp_path / "brave-wrapper"
    wrapper.write_text("")
    monkeypatch.setenv(brave_mode.ENV_PATH, str(wrapper))
    _spy_profile_writes(env, monkeypatch)
    out = _launch(browser="brave", brave_lean=True, vertical_tabs="right")
    assert not out.startswith("Error") and "[vertical tabs: on the right]" in out, out
    steps = [s for s in env.order if s in ("prepare", "check_vertical_tabs", "preflight1", "commit",
                                          "vertical_tabs", "brave_lean", "tidy", "preflight2", "start")]
    assert steps == ["prepare", "check_vertical_tabs", "preflight1", "commit", "vertical_tabs",
                     "check_vertical_tabs", "brave_lean", "tidy", "preflight2", "start"], steps
    args = ["--profile-directory=Default"]
    assert brave_lean.read_back(env.profile, args) == {**brave_lean.LOCAL_STATE, **brave_lean.PREFERENCES}
    assert brave_mode.vertical_tabs_read_back(env.profile, args, "brave") == "right"
    flags = next(entry[1] for entry in env.order if isinstance(entry, tuple) and entry[0] == "flags")
    features = [f for f in flags if f.startswith("--disable-features=")]
    assert len(features) == 1 and all(name in features[0] for name in brave_lean.FEATURES), features
    assert json.loads((env.profile / "Default" / "Preferences").read_text())["brave"]["tabs"] == {
        "vertical_tabs_enabled": True, "vertical_tabs_on_right": True}


def test_an_instance_forgets_a_privacy_folder_once_it_is_removed(tmp_path, monkeypatch):
    """After a failed privacy launch (and after a browser_close) list_instances went on giving the
    removed folder as the instance's profile. The instance that names the folder, and holds no
    browser, names no profile once the folder is gone; one that runs keeps what it runs on."""
    root = tmp_path / "privacy-profiles"
    folder = root / "main-abc123"
    folder.mkdir(parents=True)
    monkeypatch.setattr(server, "PRIVACY_PROFILE_ROOT", root)
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_DELAYS", (0.0,))
    monkeypatch.setattr("mcp_stealth_chrome.state.processes_holding", lambda _p: [])
    monkeypatch.setattr(server.BrowserState, "browser", None)
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", folder)
    monkeypatch.setattr(server.BrowserState, "current_privacy", True)
    assert asyncio.run(server._remove_privacy_profile(folder)) is True
    assert not folder.exists()
    assert server.BrowserState.current_profile_dir is None and server.BrowserState.current_privacy is False

    # Another folder's removal, or an instance with a browser, changes nothing.
    other, mine = root / "main-other", root / "main-mine"
    other.mkdir(); mine.mkdir()
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", mine)
    monkeypatch.setattr(server.BrowserState, "current_privacy", True)
    assert asyncio.run(server._remove_privacy_profile(other)) is True
    assert server.BrowserState.current_profile_dir == mine and server.BrowserState.current_privacy is True
    monkeypatch.setattr(server.BrowserState, "browser", object())
    assert asyncio.run(server._remove_privacy_profile(mine)) is True
    assert server.BrowserState.current_profile_dir == mine


def test_a_new_browser_starts_its_own_uptime(monkeypatch):
    """list_instances counts uptime from current_created_at, which browser_launch never set for a
    new browser: it counted from the server's start or the instance's earlier browser."""
    import time
    import types

    old = time.time() - 500
    monkeypatch.setattr(server.BrowserState, "current_created_at", old)
    monkeypatch.setattr(server.BrowserState, "tabs", [])
    monkeypatch.setattr(server, "_watch_windows", lambda _b: None)
    req = types.SimpleNamespace(headless=True, privacy=False)
    plan = types.SimpleNamespace(profile_path=None, effective_fingerprint={}, effective_proxy=None)
    before = time.time()
    server._launch_record_browser(req, plan, object(), object())
    assert server.BrowserState.current_created_at >= before
