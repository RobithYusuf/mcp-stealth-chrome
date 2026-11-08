"""A Chrome that runs but does not answer on nodriver's port, with real Chrome.

Fake objects passed the first version of this fix while its retry never spawned anything.
"""
import asyncio
import os
import pathlib
import stat
import subprocess

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="MCP_E2E not set")


def _late_chrome(tmp_path, real, late_spawns):
    """A launcher whose first spawns listen on a port nodriver never asked for."""
    count = tmp_path / "spawns"
    count.write_text("0")
    launcher = tmp_path / "chrome.sh"
    launcher.write_text(f"""#!/bin/bash
c=$(cat "{count}"); echo $((c+1)) > "{count}"
if [ "$c" -lt {late_spawns} ]; then
  args=(); for a in "$@"; do case "$a" in
    --remote-debugging-port=*) args+=("--remote-debugging-port=0");;
    *) args+=("$a");; esac; done
  exec "{real}" "${{args[@]}}"
fi
exec "{real}" "$@"
""")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher, count


def _on_profile(profile):
    out = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if f"--user-data-dir={profile}" in line]


def _config(launcher, profile):
    from nodriver import Config

    return Config(
        browser_executable_path=str(launcher), user_data_dir=str(profile), headless=True,
        browser_args=["--use-mock-keychain"],
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_a_slow_chrome_is_replaced_by_one_that_answers(tmp_path):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import find_chrome_binary

    launcher, count = _late_chrome(tmp_path, find_chrome_binary(), 1)
    profile = tmp_path / "profile"

    async def scenario():
        browser = await server._start_chrome(_config(launcher, profile))
        try:
            version = await browser._http.get("version")
            return version, len(_on_profile(profile))
        finally:
            report = await server._ensure_browser_dead(browser, None)
            assert report["verified"], report

    version, _alive = _run(scenario())
    assert count.read_text().strip() == "2"
    assert "Browser" in version
    assert not _on_profile(profile)


def test_a_chrome_that_never_answers_leaves_nothing_behind(tmp_path):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import find_chrome_binary

    launcher, count = _late_chrome(tmp_path, find_chrome_binary(), 9)
    profile = tmp_path / "profile"

    async def scenario():
        with pytest.raises(Exception, match="Failed to connect") as caught:
            await server._start_chrome(_config(launcher, profile))
        return caught.value

    failure = _run(scenario())
    assert count.read_text().strip() == "2"
    assert not failure.reap_report["still_alive"]
    assert not _on_profile(profile)


def _sleepy_chrome(tmp_path, real, seconds, use_exec=True):
    """A launcher that takes a while before Chrome starts, on the first spawn only."""
    count = tmp_path / "spawns"
    count.write_text("0")
    launcher = tmp_path / "chrome.sh"
    run_it = "exec " if use_exec else ""
    launcher.write_text(f"""#!/bin/bash
c=$(cat "{count}"); echo $((c+1)) > "{count}"
[ "$c" -eq 0 ] && sleep {seconds}
{run_it}"{real}" "$@"
""")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher, count


def test_a_chrome_that_starts_late_is_waited_for_not_replaced(tmp_path):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import find_chrome_binary

    launcher, count = _sleepy_chrome(tmp_path, find_chrome_binary(), 4)
    profile = tmp_path / "profile"

    async def scenario():
        browser = await server._start_chrome(_config(launcher, profile))
        try:
            assert browser._process_pid
            return await browser._http.get("version")
        finally:
            report = await server._ensure_browser_dead(browser, None)
            assert report["verified"] and not report["killed"], report

    assert "Browser" in _run(scenario())
    assert count.read_text().strip() == "1"
    assert not _on_profile(profile)


def test_chrome_under_a_launcher_that_does_not_exec_is_not_orphaned(tmp_path):
    """The launcher is the recorded PID, and ending only it reported a clean close."""
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import find_chrome_binary

    launcher, _count = _sleepy_chrome(tmp_path, find_chrome_binary(), 0, use_exec=False)
    profile = tmp_path / "profile"

    async def scenario():
        browser = await server._start_chrome(_config(launcher, profile))
        await browser._http.get("version")
        assert len(_on_profile(profile)) > 1
        report = await server._ensure_browser_dead(browser, None)
        assert report["verified"], report

    _run(scenario())
    assert not _on_profile(profile)


def test_a_recorded_pid_that_became_something_else_is_not_signalled(tmp_path):
    from mcp_stealth_chrome import server

    innocent = subprocess.Popen(["sleep", "30"])

    class _Stale:
        _process = None
        _process_pid = innocent.pid

        class config:
            user_data_dir = str(tmp_path / "gone")
            port = 1

        def stop(self):
            pass

    try:
        report = _run(server._ensure_browser_dead(_Stale(), None, grace=0.1))
        assert report["verified"] and not report["killed"], report
        assert innocent.poll() is None
    finally:
        innocent.kill()
        innocent.wait()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return server_command(pid) is not None


def server_command(pid):
    from mcp_stealth_chrome import server

    return server._process_command(pid)


@pytest.mark.parametrize("how", ["tab_close", "stale_list"])
def test_relaunch_ends_a_half_open_privacy_browser_first(how):
    """Closing the last tab leaves Chrome running, and its profile was deleted under it."""
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import navigation
    from mcp_stealth_chrome.state import BrowserState

    async def scenario():
        out = await server.browser_launch(headless=True, privacy=True)
        assert not out.startswith("Error"), out
        pid, profile = BrowserState.browser._process_pid, BrowserState.current_profile_dir
        assert profile.exists()
        try:
            if how == "tab_close":
                await navigation.tab_close(index=0)
            else:
                await BrowserState.active_tab().close()
                await asyncio.sleep(0.3)
            out = await server.browser_launch(headless=True, persistent=False)
            assert not out.startswith("Error"), out
            assert BrowserState.browser._process_pid != pid
            assert not _alive(pid), "the old Chrome outlived the relaunch"
            assert not profile.exists(), "the privacy profile outlived its browser"
        finally:
            out = await server.browser_close()
            assert not out.startswith("Error"), out

    _run(scenario())


def test_relaunch_after_the_last_tab_keeps_no_orphan():
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import navigation
    from mcp_stealth_chrome.state import BrowserState

    async def scenario():
        out = await server.browser_launch(headless=True, persistent=False)
        assert not out.startswith("Error"), out
        pid = BrowserState.browser._process_pid
        await navigation.tab_close(index=0)
        out = await server.browser_launch(headless=True, persistent=False)
        assert not out.startswith("Error"), out
        assert not _alive(pid), "the old Chrome outlived the relaunch"
        out = await server.browser_close()
        assert not out.startswith("Error"), out

    _run(scenario())


def test_closing_an_ephemeral_browser_removes_its_temp_profile():
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import BrowserState

    async def scenario():
        out = await server.browser_launch(headless=True, persistent=False)
        assert not out.startswith("Error"), out
        path = pathlib.Path(BrowserState.browser.config.user_data_dir)
        assert path.exists() and path.name.startswith("uc_")
        out = await server.browser_close()
        assert not out.startswith("Error"), out
        assert not path.exists()

    _run(scenario())
