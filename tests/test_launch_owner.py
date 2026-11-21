"""Who may change the current instance while a browser_launch runs (docs/agents/browser-lifecycle.md).

A launch marks its session (server._LAUNCHING: the instance and the task) before its first await.
While the mark stands, _launch_gate, one of _app.CALL_GATES, refuses every tool that would change
the current instance or end its browser; browser_recover is never refused, and a launch it cut
short ends as an error that records nothing more. Fakes only: no Chrome starts, every folder is
under tmp_path, and browser_recover's signals and profile sweep are pointed there too."""
import ast
import asyncio
import contextlib
import inspect
import textwrap
import time
import types
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import _app, keep_alive, presence, server, state
from test_focus_restore import _Tab
from test_launch_cleanup import env, _privacy_folders  # noqa: F401  (env is a fixture)

# Every tool that can change this session's current instance, or end or replace its browser.
# The scan below must find exactly these; each must be refused during a launch or allowed with
# a reason (server._REFUSED_DURING_LAUNCH, server._ALLOWED_DURING_LAUNCH).
CHANGERS = {"switch_instance", "spawn_browser", "close_instance", "close_all_instances", "instance_release",
            "attach_to_chrome", "detach", "browser_close", "browser_recover", "browser_launch"}

# What each refused tool is sent at every await. Each would fail early and harmlessly if it ran
# (an invalid name, a port nothing listens on), so a gate that let one through shows as a
# different text, not as a browser started.
REFUSED_CALLS = {
    "switch_instance": {"instance_id": "other"},
    "close_instance": {"instance_id": "main"},
    "attach_to_chrome": {"port": 1},
    "detach": {},
    "browser_close": {},
    "close_all_instances": {},
    "spawn_browser": {"instance_id": "not a valid name!"},
}

# The awaits of browser_launch: each phase it awaits, before and after it, and the awaits inside
# the phases after which a step acts on the launch's own browser (storage_state, the cookie
# move, the first tab and its load, the saved tabs), and the wait for the profile reservation.
PHASES = ("_launch_end_stale_browser", "_launch_reuse_running", "_launch_kept_browser",
          "_launch_prepare_profile", "_launch_keep_keyboard", "_launch_commit_profile",
          "_launch_start_chrome", "_launch_ready_browser", "_launch_first_page",
          "_launch_window_health", "_launch_auto_verify", "_launch_place_window",
          "_launch_restore_tabs", "_record_tabs_at_launch", "_give_focus_back")
INNER = ("_apply_storage_state", "_finish_cookie_move", "_first_live_tab", "_observe_navigation",
         "_reopen_saved_tabs")
POINTS = ([f"{when}:{name}" for name in PHASES for when in ("before", "after")]
          + [f"in:{name}" for name in INNER] + ["in:reserve"])
_STARTED = POINTS.index("after:_launch_start_chrome")


def _refusal(name):
    return (f"Error: browser_launch of instance 'main' is in progress in this session; {name} was not "
            f"done. Call it again once the launch has returned.")


_RECOVERED = "browser_recover reset instance 'main' while this launch ran."


def _text(result):
    return str(result)


async def _call(name, arguments):
    """A tool call as a client sends one: through the call wrapper, in a task of its own."""
    return _text(await asyncio.wait_for(asyncio.ensure_future(server.mcp.call_tool(name, arguments)), 10))


class _Recover:
    """browser_recover as a client sends it beside a launch: in a task of its own, never awaited
    by the launch (a recover waits for the launch it asked to stop). start() returns once the
    recover has asked the launch to stop, or has ended; said() is its result."""

    def __init__(self):
        self.task = None

    async def start(self):
        self.task = asyncio.ensure_future(_call("browser_recover", {}))
        entry = server._LAUNCHING.get(server.BrowserState.session_key())
        for _ in range(1000):
            if self.task.done() or (entry is not None and entry.ended_by):
                break
            await asyncio.sleep(0)
        return self

    async def said(self):
        return await asyncio.wait_for(self.task, 20)


async def _beside(launch, recover):
    """The launch's result, then the recover's."""
    out = await launch
    return out, await recover.said()


class _At:
    """Runs `act` once, when the launch reaches `point`."""

    def __init__(self, point, act):
        self.point, self.act, self.result, self.seen = point, act, None, []

    async def fire(self, here):
        self.seen.append(here)
        if here == self.point and self.result is None:
            self.result = await self.act()


def _instrument(monkeypatch, at):
    """Every await of browser_launch reports to `at` (see PHASES, INNER)."""
    for name in PHASES:
        real = getattr(server, name)

        def phase(*a, _name=name, _real=real, **kw):
            async def run():
                await at.fire(f"before:{_name}")
                out = await _real(*a, **kw)
                await at.fire(f"after:{_name}")
                return out
            return run()
        monkeypatch.setattr(server, name, phase)
    for name in INNER:
        real = getattr(server, name)

        def inner(*a, _name=name, _real=real, **kw):
            async def run():
                await at.fire(f"in:{_name}")
                return await _real(*a, **kw)
            return run()
        monkeypatch.setattr(server, name, inner)
    real_reserve = server.launch_lock.reserve

    @contextlib.asynccontextmanager
    async def reserve(*a, **kw):
        await at.fire("in:reserve")
        async with real_reserve(*a, **kw) as busy:
            yield busy
    monkeypatch.setattr(server.launch_lock, "reserve", reserve)


@pytest.fixture
def owner(env, monkeypatch, tmp_path):
    """A launch that reaches every await: privacy (a profile to remove), storage_state, a cookie
    move to finish and saved tabs to reopen, all faked. A second instance 'other' is stored, and
    browser_recover's signals and sweep are pointed at tmp_path."""
    from mcp_stealth_chrome.state import InstanceSnapshot
    server.BrowserState.current_instance_id = "main"
    other_browser = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)
    server.BrowserState.instances["other"] = InstanceSnapshot(
        instance_id="other", browser=other_browser, tabs=list(other_browser.tabs),
        profile_dir=tmp_path / "other-profile")
    env.other_browser = other_browser
    env.move = server._CookieMove(plan=None, backup=tmp_path / "backup.json")
    env.finish = (True, "")
    env.state_file = tmp_path / "state.json"
    env.state_file.write_text('{"cookies": []}')

    async def storage(_browser, _path, tab=None):
        return {}

    async def reopen(_iid, _browser, main, *_a, **_kw):
        return [main, _Tab()], 0, {"reopened": 1}, " [restore_tabs: 1 reopened]"

    monkeypatch.setattr(server, "_apply_storage_state", storage)
    monkeypatch.setattr(server, "_reopen_saved_tabs", reopen)
    monkeypatch.setattr(state, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(state, "PROFILE_DIR", tmp_path / "main-profile")
    monkeypatch.setattr(state, "per_process_profile", lambda: tmp_path / "pid-profile")
    monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda _p: [])
    yield env
    server.BrowserState.instances.pop("other", None)


def _launch_with(owner):
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    return fn(auto_verify=False, headless=True, privacy=True, restore_tabs=True,
              storage_state_path=str(owner.state_file))


def _no_mark():
    return server._LAUNCHING.get(server.BrowserState.session_key()) is None


# ── The inventory ──────────────────────────────────────────────────────────────────────────


def _changes_the_instance(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for item in (target.elts if isinstance(target, ast.Tuple) else [target]):
                    if (isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name)
                            and item.value.id == "BrowserState"
                            and item.attr in ("browser", "current_instance_id")):
                        return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "BrowserState"
                and node.func.attr in ("reset", "switch_to", "restore_from", "release_instance")):
            return True
    return False


def _reaches_a_change(fn, seen):
    """Whether fn, or a function of this package it calls (by name, or as module.name), sets
    BrowserState.browser or current_instance_id, resets, switches or releases an instance."""
    fn = inspect.unwrap(fn)
    if fn in seen:
        return False
    seen.add(fn)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return False
    if _changes_the_instance(tree):
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func, target = node.func, None
        if isinstance(func, ast.Name):
            target = fn.__globals__.get(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = fn.__globals__.get(func.value.id)
            if isinstance(module, types.ModuleType):
                target = getattr(module, func.attr, None)
        if (isinstance(target, types.FunctionType) and target.__module__.startswith("mcp_stealth_chrome")
                and _reaches_a_change(target, seen)):
            return True
    return False


def test_every_tool_that_can_change_the_current_instance_is_refused_or_allowed_with_a_reason():
    """A tool added later that can change the current instance or end its browser fails here
    until it is refused during a launch (one name in server._REFUSED_DURING_LAUNCH) or named as
    allowed with its reason."""
    found = {name for name, tool in server.mcp._tool_manager._tools.items()
             if _reaches_a_change(tool.fn, set())}
    assert found == CHANGERS, (f"new: {sorted(found - CHANGERS)}, no longer found: "
                               f"{sorted(CHANGERS - found)}")
    refused, allowed = server._REFUSED_DURING_LAUNCH, server._ALLOWED_DURING_LAUNCH
    for name in sorted(CHANGERS):
        assert (name in refused) != (name in allowed), f"{name}: neither refused nor allowed, or both"
        if name in allowed:
            assert len(allowed[name]) > 30, f"{name}: allowed without a reason"
    assert set(refused) | set(allowed) == CHANGERS
    assert server._launch_gate in _app.CALL_GATES


def test_no_tool_that_changes_the_current_instance_is_called_round_the_gate():
    """batch_actions and workflow_run call tools directly, without the call wrapper and its gates."""
    server._register_workflow_tools()
    assert not CHANGERS & set(server._WORKFLOW_TOOLS)
    from mcp_stealth_chrome.tools import batch
    assert not CHANGERS & {tool for tool, _defaults in batch._BATCH_ALIASES.values()}


def test_during_a_launch_the_gate_refuses_exactly_what_it_should(owner):
    """With a launch of 'main' under way, every refused tool is refused through the call wrapper
    without running; close_instance of another instance, and a page tool, are not."""
    async def run():
        hold = asyncio.Event()
        task = asyncio.ensure_future(hold.wait())
        server._LAUNCHING[server.BrowserState.session_key()] = server._Launching("main", task)
        try:
            refused = {name: await _call(name, args) for name, args in REFUSED_CALLS.items()}
            release = await _call("instance_release", {"instance_id": "main"})
            closed_other = await _call("close_instance", {"instance_id": "other"})
            page = await _call("get_url", {})
            recover_gate = server._launch_gate("browser_recover")
        finally:
            hold.set()
            await task
            server._LAUNCHING.pop(server.BrowserState.session_key(), None)
        return refused, release, closed_other, page, recover_gate

    refused, release, closed_other, page, recover_gate = asyncio.run(run())
    for name, out in refused.items():
        assert _refusal(name) in out, (name, out)
    assert "current instance" in release and _refusal("instance_release") not in release, release
    assert "in progress" not in closed_other and "other" not in server.BrowserState.instances, closed_other
    assert "in progress" not in page, page
    assert recover_gate is None
    assert server.BrowserState.current_instance_id == "main"


# ── At every await ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("point", POINTS)
def test_at_every_await_the_tools_that_would_change_the_instance_are_refused(owner, monkeypatch, point):
    """A switch, close_instance of the launching instance, attach_to_chrome, detach,
    browser_close, close_all_instances and spawn_browser, each sent through the call wrapper at
    this await, is refused and changes nothing; the launch then finishes on its own instance."""
    async def send_all():
        return {name: await _call(name, args) for name, args in REFUSED_CALLS.items()}

    at = _At(point, send_all)
    _instrument(monkeypatch, at)

    out = asyncio.run(_launch_with(owner))
    assert point in at.seen, at.seen
    for name, said in at.result.items():
        assert _refusal(name) in said, (name, said)
    assert "Browser launched" in out, out
    current = server.BrowserState
    assert current.current_instance_id == "main" and current.browser is owner.started[0]
    assert current.instances["other"].browser is owner.other_browser
    assert owner.closes == [], "a refused tool closed something"
    assert _no_mark()


@pytest.mark.parametrize("point", POINTS)
def test_a_browser_recover_at_every_await_cuts_the_launch_short_and_records_nothing(owner, monkeypatch, point):
    """browser_recover is never refused. Sent at this await, it does what it does (resets the
    instance) and says it cut the launch short; the launch ends as an error saying so, ends the
    browser it had started, once, removes its privacy profile, and records nothing more: no
    handle, no tabs."""
    recover = _Recover()
    at = _At(point, recover.start)
    _instrument(monkeypatch, at)

    out, said = asyncio.run(_beside(_launch_with(owner), recover))
    assert point in at.seen, at.seen
    assert "browser_launch of instance 'main' was still running: asked to stop, it ended" in said, said
    assert out.startswith("Error") and out.count(_RECOVERED) == 1, out
    # Looked at after every await: the step it was in finishes, and no later one starts.
    later = at.seen[at.seen.index(point) + 1:]
    assert not [here for here in later if here.startswith("before:")], f"the launch went on: {later}"
    current = server.BrowserState
    assert current.browser is None and current.tabs == [], "the cut-short launch recorded its browser"
    assert current.current_instance_id == "main"
    assert current.instances["other"].browser is owner.other_browser
    if point == "in:reserve" or (not point.startswith("in:") and POINTS.index(point) < _STARTED):
        assert owner.started == [] and owner.closes == [], out
    if owner.started:
        assert [b for b, _p in owner.closes] == owner.started, "its browser was not ended, or ended twice"
        assert "Its browser was ended" in out, out
    assert _privacy_folders(owner) == []
    assert _no_mark()


@pytest.mark.parametrize("point", ["after:_launch_ready_browser", "before:_launch_window_health"])
def test_after_a_recover_the_next_launch_starts_afresh(owner, monkeypatch, point):
    """Before and after the launch recorded its browser: nothing it left behind is reused, the
    next browser_launch starts a browser of its own."""
    recover = _Recover()
    at = _At(point, recover.start)
    _instrument(monkeypatch, at)
    out, _said = asyncio.run(_beside(_launch_with(owner), recover))
    assert _RECOVERED in out, out
    again = asyncio.run(server.mcp._tool_manager._tools["browser_launch"].fn(auto_verify=False, headless=True))
    assert "Browser launched" in again and "reusing" not in again, again
    assert len(owner.started) == 2 and server.BrowserState.browser is owner.started[1]


def test_a_recover_during_the_close_of_a_failed_launch_keeps_its_error_and_says_so(owner, monkeypatch):
    """A step that already failed closes its browser. A browser_recover sent through the call
    wrapper during that close does not wait for the close (_app.RUNS_DURING_A_CLOSE): it asks
    the launch to stop and waits for it. The launch keeps the step's own error first, with what
    happened after it; the browser is closed once, not again by the cut-short launch, and
    nothing is recorded."""
    async def broken(_browser, _path, tab=None):
        raise ValueError("no cookies key in the file")

    monkeypatch.setattr(server, "_apply_storage_state", broken)
    recover = _Recover()
    owner.during_close = recover.start
    out, said = asyncio.run(_beside(_launch_with(owner), recover))
    assert "recovered:" in said and "was still running: asked to stop, it ended" in said, said
    assert out == f"Error: storage_state load failed: no cookies key in the file. {_RECOVERED}", out
    assert [b for b, _p in owner.closes] == owner.started
    assert server.BrowserState.browser is None and _privacy_folders(owner) == []


def test_a_browser_that_a_cut_short_launch_could_not_end_keeps_a_handle(owner, monkeypatch):
    """Absence of a handle is for a browser that ended: one still running after the cut is kept on
    its instance, naming its profile, so browser_close can reach it. The recover, which began on
    that same browser, then resets it as it resets any browser it could not end (a limit the
    guide names): its own result says so."""
    from test_launch_cleanup import _ALIVE
    owner.close_report = dict(_ALIVE)
    recover = _Recover()
    at = _At("after:_launch_first_page", recover.start)
    _instrument(monkeypatch, at)
    seen = {}

    async def run():
        out = await _launch_with(owner)
        current = server.BrowserState
        seen.update(browser=current.browser, tabs=list(current.tabs), profile=current.current_profile_dir,
                    privacy=current.current_privacy)
        return out, await recover.said()

    out, said = asyncio.run(run())
    assert _RECOVERED in out and "could not be ended" in out and "call browser_close to try again" in out, out
    assert seen["browser"] is owner.started[0] and seen["tabs"] == []
    folders = _privacy_folders(owner)
    assert len(folders) == 1 and seen["profile"] == folders[0] and seen["privacy"]
    assert "recovered:" in said and "state reset" in said, said


# ── The reattach ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def kept(owner, monkeypatch, tmp_path):
    """A browser kept alive on the profile, taken over by the launch: the real _reattach_connected
    and _reattach_claimed on a fake connection, with a record in the test's keep-alive folder."""
    profile = tmp_path / "kept-profile"
    profile.mkdir()
    record = {"pid": 99999, "port": 9, "host": "127.0.0.1", "user_data_dir": str(profile),
              "headless": True, "server_pid": 424242, "state": "kept"}
    assert keep_alive.write(profile, **record) is None
    taken = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)

    async def locked(instance_id, profile_, verdict, *, url, keep_alive, ignored):
        return await server._reattach_connected(taken, instance_id, profile_, verdict["record"], url=url,
                                                keep_alive=keep_alive, ignored=ignored)

    monkeypatch.setattr(server, "_kept_candidates", lambda *_a, **_kw: [profile])
    monkeypatch.setattr(server, "_kept_verdict", lambda *_a, **_kw: {"ok": True, "record": dict(record)})
    monkeypatch.setattr(server, "_reattach_locked", locked)
    monkeypatch.setattr(server, "_arm_keep_alive", lambda *_a, **_kw: "")
    yield SimpleNamespace(profile=profile, browser=taken)
    keep_alive.remove(profile)


def _launch_kept():
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    return fn(auto_verify=False, headless=True)


def test_a_recover_while_a_kept_browser_is_being_taken_over_gives_it_back(owner, kept, monkeypatch):
    """Cut short before the reattach recorded it, the kept browser is given back as it was (it
    may be the person's, opened from the launcher): its record names its old server again,
    nothing ended it, and the instance holds nothing of it."""
    real = server._observe_navigation
    recover = _Recover()

    async def observe(*a, **kw):
        if recover.task is None:
            await recover.start()
        return await real(*a, **kw)

    monkeypatch.setattr(server, "_observe_navigation", observe)
    out, said = asyncio.run(_beside(_launch_kept(), recover))
    assert "was still running: asked to stop, it ended" in said, said
    assert "could not be taken over safely: this browser_launch was cut short" in out and _RECOVERED in out, out
    assert (keep_alive.read(kept.profile) or {}).get("server_pid") == 424242, "the record was not given back"
    assert owner.closes == [], "the kept browser was ended"
    assert server.BrowserState.browser is None and server.BrowserState.tabs == []


def test_a_recover_after_a_kept_browser_was_taken_over_ends_it(owner, kept, monkeypatch):
    """Once recorded it is the launch's browser, and browser_recover ended it as the current one
    (it is not the person's, which a recover leaves alone while it answers): the cut-short launch
    verifies that end as for one it started, and records nothing more."""
    real = presence.attach
    recover = _Recover()

    async def attach(*a, **kw):
        if recover.task is None:
            await recover.start()
        return await real(*a, **kw)

    monkeypatch.setattr(presence, "attach", attach)
    out, said = asyncio.run(_beside(_launch_kept(), recover))
    assert "was still running: asked to stop, it ended" in said, said
    assert out.startswith("Error: browser_launch of instance 'main' did not finish.") and _RECOVERED in out, out
    assert [b for b, _p in owner.closes] == [kept.browser]
    assert server.BrowserState.browser is None and server.BrowserState.tabs == []


# ── The mark itself ────────────────────────────────────────────────────────────────────────


def test_a_launch_that_raises_leaves_no_mark(owner, monkeypatch):
    async def broken(*_a, **_kw):
        raise RuntimeError("the profile phase broke")

    monkeypatch.setattr(server, "_launch_prepare_profile", broken)
    with pytest.raises(RuntimeError, match="the profile phase broke"):
        asyncio.run(_launch_with(owner))
    assert _no_mark()
    assert "switched to 'other'" in asyncio.run(_call("switch_instance", {"instance_id": "other"}))


def test_a_cancelled_launch_leaves_no_mark_and_no_handle(owner, monkeypatch):
    reached = {}

    async def hang(_browser):
        reached["at"] = time.monotonic()
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_first_live_tab", hang)

    async def run():
        task = asyncio.ensure_future(_launch_with(owner))
        while "at" not in reached:
            await asyncio.sleep(0.01)
        refused = await _call("switch_instance", {"instance_id": "other"})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return refused, await _call("switch_instance", {"instance_id": "other"})

    refused, after = asyncio.run(run())
    assert _refusal("switch_instance") in refused, refused
    assert "switched to 'other'" in after, after
    assert _no_mark()
    assert server.BrowserState.instances.get("main") is None, "the cancelled launch's browser was kept"


@pytest.mark.parametrize("where", ["_first_live_tab", "_reopen_saved_tabs"])
def test_a_launch_cancelled_after_another_browser_took_its_instance_stops_its_own(owner, monkeypatch, where):
    """What is current is no longer the launch's browser (a call that was already running when the
    launch began published another): the cancel stops the browser the launch started
    (plan.browser), not the current one, and leaves that one where it is. In the first page's
    own cancel path, and in a phase with none (the saved tabs, after the record)."""
    stopped = []
    theirs = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None)

    async def safe_stop(browser):
        stopped.append(browser)

    async def hang(*_a, **_kw):
        server.BrowserState.browser = theirs
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_safe_stop_browser", safe_stop)
    monkeypatch.setattr(server, where, hang)

    async def run():
        task = asyncio.ensure_future(_launch_with(owner))
        while server.BrowserState.browser is not theirs:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert stopped == owner.started, stopped
    assert _no_mark() and server.BrowserState.browser is theirs


_HUNG = {}


async def _never(_browser):
    """An await that never returns: the reviewer's send(get_targets()) with no timeout."""
    _HUNG["in"] = True
    await asyncio.get_running_loop().create_future()


async def _until_hung():
    for _ in range(500):
        if _HUNG.pop("in", False):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the launch never reached the await that hangs")


def test_browser_recover_cancels_a_launch_hung_on_an_await_that_never_returns(owner, monkeypatch):
    """No cancel from the test: the recover asks the launch to stop, waits, then cancels it. The
    launch answers that it was cancelled, its cancel path stops its browser and removes its
    privacy profile, and the mark is gone, so the session is not refused any more."""
    monkeypatch.setattr(server, "_RECOVER_LAUNCH_WAIT", 1.0)  # the cancel path removes a folder
    monkeypatch.setattr(server, "_first_live_tab", _never)
    stopped = []

    async def safe_stop(browser):
        stopped.append(browser)

    monkeypatch.setattr(server, "_safe_stop_browser", safe_stop)

    async def run():
        launch = asyncio.ensure_future(_launch_with(owner))
        await _until_hung()
        said = await _call("browser_recover", {})
        out = await asyncio.wait_for(launch, 5)
        return said, out, _no_mark(), await _call("switch_instance", {"instance_id": "other"})

    said, out, no_mark, switched = asyncio.run(run())
    assert "was still running: asked to stop, it was still waiting after 1 s and was cancelled" in said, said
    assert out == ("Error: browser_launch of instance 'main' did not finish: browser_recover asked it to stop, "
                   "and it was cancelled at the step it was waiting on. Its browser was asked to stop; that it "
                   "ended was not checked."), out
    assert no_mark, "the cancelled launch kept its mark"
    assert "switched to 'other'" in switched, switched
    assert stopped == owner.started and _privacy_folders(owner) == []
    server.BrowserState.switch_to("main")


def test_a_launch_that_ignores_the_cancel_no_longer_holds_the_instance(owner, monkeypatch):
    """A launch that swallows the cancel and keeps waiting: the recover says it had not ended, and
    its mark refuses nothing (a switch goes through). When it wakes it is cut short and touches
    nothing of the instance switched in."""
    monkeypatch.setattr(server, "_RECOVER_LAUNCH_WAIT", 0.2)
    release = {}

    async def deaf(_browser):
        release["event"] = asyncio.Event()
        _HUNG["in"] = True
        while not release["event"].is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release["event"].wait()
        raise RuntimeError("woke up after the recover")

    monkeypatch.setattr(server, "_first_live_tab", deaf)

    async def run():
        launch = asyncio.ensure_future(_launch_with(owner))
        await _until_hung()
        said = await _call("browser_recover", {})
        switched = await _call("switch_instance", {"instance_id": "other"})
        pending = not launch.done()
        release["event"].set()
        return said, switched, pending, await asyncio.wait_for(launch, 5)

    said, switched, pending, out = asyncio.run(run())
    assert "asked to stop and cancelled, it had still not ended 0.2 s later" in said, said
    assert pending and "switched to 'other'" in switched, switched
    assert out.startswith("Error: initial nav failed: woke up after the recover") and _RECOVERED in out, out
    current = server.BrowserState
    assert current.current_instance_id == "other" and current.browser is owner.other_browser
    assert [b for b, _p in owner.closes] == owner.started
    server.BrowserState.switch_to("main")


def test_a_mark_whose_task_is_done_refuses_nothing(owner):
    async def run():
        done = asyncio.ensure_future(asyncio.sleep(0))
        await done
        server._LAUNCHING[server.BrowserState.session_key()] = server._Launching("main", done)
        try:
            return (server._being_launched("main"),
                    await _call("switch_instance", {"instance_id": "other"}))
        finally:
            server._LAUNCHING.pop(server.BrowserState.session_key(), None)

    launching, switched = asyncio.run(run())
    assert launching is False
    assert "switched to 'other'" in switched, switched


# ── The sweeps ─────────────────────────────────────────────────────────────────────────────


def _hold_a_launch_of(instance_id):
    hold = asyncio.Event()
    task = asyncio.ensure_future(hold.wait())
    server._LAUNCHING[server.BrowserState.session_key()] = server._Launching(instance_id, task)
    return hold, task


def test_the_idle_reaper_leaves_the_launching_instance_alone(owner, monkeypatch):
    ended = []

    async def end(reason=None):
        ended.append(reason)
        return True

    monkeypatch.setattr(server, "_end_current_instance", end)
    # A started process that has not stopped: is_up() reads it as a live browser.
    server.BrowserState.browser = SimpleNamespace(tabs=[_Tab()], stopped=False, config=None, _process=object())
    server.BrowserState.tabs = list(server.BrowserState.browser.tabs)
    server.BrowserState.current_idle_timeout = 1
    server.BrowserState.current_last_active = time.time() - 60

    async def run():
        hold, task = _hold_a_launch_of("main")
        try:
            await server._sweep_idle_instances()
            during = list(ended)
        finally:
            hold.set()
            await task
            server._LAUNCHING.pop(server.BrowserState.session_key(), None)
        await server._sweep_idle_instances()
        return during

    during = asyncio.run(run())
    assert during == [], "the reaper closed the instance a launch was not done with"
    assert ended == ["idle timeout"], "the control: once the launch is over, the reaper closes it"


def test_the_window_sweep_leaves_the_launching_instance_alone(owner, monkeypatch):
    ended = []

    async def end(reason=None):
        ended.append(reason)
        return True

    async def no_pages(_browser):
        return 0

    browser = SimpleNamespace(tabs=[], stopped=False, config=None)
    server.BrowserState.browser = browser
    monkeypatch.setattr(server, "_end_current_instance", end)
    monkeypatch.setattr(server, "_page_target_count", no_pages)
    monkeypatch.setattr(server.quit_watch, "quit_time", lambda _b: None)
    monkeypatch.setattr(server.quit_watch, "running", lambda _b: False)
    monkeypatch.setattr(server.quit_watch, "reap", lambda _b: None)
    monkeypatch.setitem(server._WINDOW_WATCH, id(browser),
                        {"browser": browser, "zero_since": time.monotonic() - 60, "tool_closed": False})

    async def run():
        hold, task = _hold_a_launch_of("main")
        try:
            await server._sweep_windowless()
            during = list(ended)
        finally:
            hold.set()
            await task
            server._LAUNCHING.pop(server.BrowserState.session_key(), None)
        await server._sweep_windowless()
        return during

    during = asyncio.run(run())
    assert during == [], "the window sweep closed the instance a launch was not done with"
    assert ended == ["last window closed"], "the control: once the launch is over, the sweep closes it"
