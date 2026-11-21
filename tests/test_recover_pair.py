"""browser_recover is the way out of a stuck state, so it depends on nothing that can be stuck.

It does not wait for a close of the current instance (_app.RUNS_DURING_A_CLOSE, with
server_status, which only reads); every other tool still waits. And it acts only on the instance
and browser it began on: taken before its first await, reset after it only while that instance is
still the current one and holds the same browser (or none). Fakes only: no Chrome starts, and
browser_recover's signals and profile sweep are pointed under tmp_path. Where a test needs the
recover's own await (the 2 s after SIGTERM), the "orphan Chrome" it signals is a sleeper process
this test started, never reaped before the recover is done, so its PID cannot go to anyone else."""
import asyncio
import contextlib
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import _app, server, state
from test_focus_restore import _Tab
from test_launch_cleanup import env, _ALIVE, _privacy_folders  # noqa: F401  (env is a fixture)
from test_launch_owner import owner, _call, _RECOVERED  # noqa: F401  (owner is a fixture)

_WAIT = 2.0  # the closing wait, shortened from 60 s for these tests
_STILL = "is still being closed after"


class _Browser(SimpleNamespace):
    def stop(self):
        self.stopped_by_recover = True


@pytest.fixture
def current(owner, monkeypatch):
    """Instance 'main' current, with a browser of its own; the closing wait shortened."""
    monkeypatch.setattr(_app, "_CLOSING_WAIT_SECONDS", _WAIT)
    browser = _Browser(tabs=[_Tab()], stopped=False, config=None)
    server.BrowserState.browser = browser
    server.BrowserState.tabs = list(browser.tabs)
    return browser


async def _while_a_close_never_ends(act):
    """`act` runs while a close of the current instance is registered and never finishes."""
    with server._closing_in_background(server.BrowserState.current_instance_id):
        started = time.monotonic()
        out = await act()
        return out, time.monotonic() - started


def test_browser_recover_does_not_wait_for_a_close_that_never_ends(current):
    out, took = asyncio.run(_while_a_close_never_ends(lambda: _call("browser_recover", {})))
    assert _STILL not in out and "recovered:" in out, out
    assert took < _WAIT, f"it waited {took:.2f} s for the close"
    assert getattr(current, "stopped_by_recover", False), "it did not stop the browser"
    assert server.BrowserState.browser is None and server.BrowserState.tabs == [], "it did not reset"


def test_server_status_does_not_wait_for_a_close_that_never_ends(current):
    out, took = asyncio.run(_while_a_close_never_ends(lambda: _call("server_status", {})))
    assert _STILL not in out and '"version"' in out, out[:300]
    assert took < _WAIT, f"it waited {took:.2f} s for the close"
    assert server.BrowserState.browser is current, "a status read changed the instance"


def test_every_other_tool_still_waits_for_the_close(current):
    out, took = asyncio.run(_while_a_close_never_ends(lambda: _call("switch_instance", {"instance_id": "other"})))
    assert f"instance 'main' {_STILL} {_WAIT:g} s; nothing was done" in out, out
    assert took >= _WAIT
    assert server.BrowserState.current_instance_id == "main" and server.BrowserState.browser is current
    assert _app.RUNS_DURING_A_CLOSE == {"browser_recover", "server_status"}


# ── browser_recover acts only on the instance and browser it began on ────────────────────────


@pytest.fixture
def stale_pid(owner, monkeypatch, tmp_path):
    """An "orphan Chrome" on the instance's recorded profile: a sleeper this test started. The
    recover SIGTERMs it and then waits 2 s, its own await. `reached` is set as it looks."""
    profile = tmp_path / "stale-profile"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reached = asyncio.Event()

    def pids(path):
        if path == profile:
            reached.set()
            return [sleeper.pid]
        return []

    monkeypatch.setattr(state, "find_chrome_pids_by_profile", pids)
    server.BrowserState.current_profile_dir = profile
    yield SimpleNamespace(profile=profile, reached=reached)
    # Reaped only now that every recover is done: until then its PID cannot be handed out again.
    with contextlib.suppress(Exception):
        sleeper.kill()
    sleeper.wait(timeout=10)


async def _recover_in_the_background(stale):
    task = asyncio.ensure_future(_call("browser_recover", {}))
    await asyncio.wait_for(stale.reached.wait(), 5)
    await asyncio.sleep(0.1)  # it has sent SIGTERM and sleeps now
    assert not task.done()
    return task


def test_a_switch_while_browser_recover_waits_leaves_the_instance_switched_in_alone(owner, stale_pid):
    """A switch_instance during the recover's own await: resetting by what is current then reset
    the instance switched in. Now nothing of it is reset, and the result says main was not."""
    main_browser = _Browser(tabs=[_Tab()], stopped=False, config=None)
    server.BrowserState.browser = main_browser
    server.BrowserState.tabs = list(main_browser.tabs)

    async def run():
        task = await _recover_in_the_background(stale_pid)
        switched = await _call("switch_instance", {"instance_id": "other"})
        return switched, await task

    switched, out = asyncio.run(run())
    assert "switched to 'other'" in switched, switched
    assert ("Error: browser_recover did not reset: instance 'main' stopped being the current one while this "
            "recover ran, so nothing of the current instance ('other') was reset") in out, out
    current = server.BrowserState
    assert current.current_instance_id == "other" and current.browser is owner.other_browser, \
        "the recover reset the instance switched in"
    assert current.tabs == owner.other_browser.tabs
    assert current.instances["main"].browser is main_browser
    assert getattr(main_browser, "stopped_by_recover", False), "it did not stop the browser it began on"
    server.BrowserState.switch_to("main")


def test_a_handle_a_cut_short_launch_kept_while_browser_recover_waits_is_not_dropped(owner, monkeypatch):
    """The edge named in A6: a recover begins while a launch's browser is started but not yet
    the instance's (it finds the instance empty) and waits for the launch it asked to stop. The
    launch, cut short, cannot end that browser and keeps a handle on the empty instance. The
    recover's reset then dropped the only handle on a live Chrome. Now it resets nothing and
    says so."""
    from test_launch_owner import _Recover
    owner.close_report = dict(_ALIVE)
    real_start = server._start_chrome
    recover = _Recover()

    async def start(config):
        browser = await real_start(config)
        await recover.start()
        return browser

    monkeypatch.setattr(server, "_start_chrome", start)

    async def run():
        fn = server.mcp._tool_manager._tools["browser_launch"].fn
        out = await fn(auto_verify=False, headless=True, privacy=True)
        return out, await recover.said()

    out, recovered = asyncio.run(run())
    assert _RECOVERED in out and "its handle and profile were kept: call browser_close to try again" in out, out
    assert ("Error: browser_recover did not reset: instance 'main' holds another browser than when this "
            "recover began") in recovered, recovered
    current = server.BrowserState
    assert current.browser is owner.started[0], "the recover dropped the handle on a browser still running"
    folders = _privacy_folders(owner)
    assert len(folders) == 1 and current.current_profile_dir == folders[0] and current.current_privacy
