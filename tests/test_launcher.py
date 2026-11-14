"""The Dock launcher and `mcp-stealth-chrome open`, without a browser: who holds a profile, what a
click does in each case, the first-run note, the time cap of a browser the person opened, Brave
Beta's choice of profile, and the app bundle (macOS). The real clicks are in test_launcher_e2e.py."""
import argparse
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_stealth_chrome import brave as brave_mode
from mcp_stealth_chrome import brave_beta, dock_icon, keep_alive, launcher, profile_version, state

PROFILE = "/data/brave-profile"
_REAL_CHROME_PREPARE = dock_icon.prepare  # conftest stands it in for every test of this file
OURS = (f"/Applications/Brave Browser Beta.app/Contents/MacOS/Brave Browser Beta --remote-allow-origins=* "
        f"--user-data-dir={PROFILE} --hide-crash-restore-bubble --disable-session-crashed-bubble "
        f"--disable-restore-session-state --remote-debugging-host=127.0.0.1 --remote-debugging-port=50104")


@pytest.fixture
def world(monkeypatch):
    if os.name != "posix":
        pytest.skip("open stops before any of this on Windows (keep_alive needs the quit watcher)")
    calls = argparse.Namespace(lock=("free", None), commands={}, naming=[], cold=[], forward=[])
    monkeypatch.setattr(state, "chrome_lock_state", lambda _p: calls.lock)
    monkeypatch.setattr(state, "processes_naming", lambda _p: calls.naming)
    monkeypatch.setattr(launcher._watcher, "process_command", lambda pid: calls.commands.get(pid))
    monkeypatch.setattr(launcher, "profile_for", lambda _b: Path(PROFILE))
    monkeypatch.setattr(keep_alive, "supported", lambda: None)
    monkeypatch.setattr(brave_mode, "find_brave", lambda: "/Applications/Brave Browser.app/x")

    def cold(browser, url, move):
        calls.cold.append((browser, url, move))
        return 0, "started", 4242

    def forward(found, url):
        calls.forward.append((found.pid, found.port, url))
        return 0, "forward"

    monkeypatch.setattr(launcher, "cold_launch", cold)
    monkeypatch.setattr(launcher, "bring_forward", forward)
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "skipped")
    monkeypatch.setattr(launcher, "activating", lambda: False)
    return calls


@pytest.fixture(autouse=True)
def _lock_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_TEST_LAUNCHER_LOCK", str(tmp_path / "launcher.lock"))


def test_a_free_profile_is_started_once_through_browser_launch(world):
    assert launcher.open_browser("brave", "https://example.com") == (0, "started (PID 4242): started")
    assert world.cold == [("brave", "https://example.com", False)] and world.forward == []


def test_a_running_browser_of_ours_is_only_brought_forward(world):
    world.lock, world.commands = ("held", 777), {777: OURS}
    code, text = launcher.open_browser("brave", "https://example.org")
    assert (code, text) == (0, "forward")
    assert world.forward == [(777, 50104, "https://example.org")] and world.cold == []


def test_a_browser_without_our_flags_is_never_launched_over(world):
    world.lock, world.commands = ("held", 777), {777: OURS.replace(" --disable-restore-session-state", "")}
    code, text = launcher.open_browser("brave")
    assert code == 1 and "did not start" in text and "never opens a second browser" in text
    assert world.cold == [] and world.forward == []


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_a_lock_naming_a_reused_pid_is_stale_only_when_nobody_uses_the_profile(world, tmp_path, monkeypatch):
    profile = tmp_path / "brave-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("host-777")
    monkeypatch.setattr(launcher, "profile_for", lambda _b: profile)
    world.lock, world.commands = ("held", 777), {777: "/bin/sleep 100"}
    world.naming = [(888, f"/x/Brave Browser Beta --user-data-dir={profile}")]
    code, text = launcher.open_browser("brave")
    assert code == 1 and "without holding its lock" in text and world.cold == []
    assert (profile / "SingletonLock").is_symlink(), "a lock in use is never removed"
    world.naming = []
    assert launcher.open_browser("brave")[0] == 0
    assert world.cold and not (profile / "SingletonLock").is_symlink()


def test_an_unreadable_process_list_is_unknown_not_free(world):
    world.naming = None
    code, text = launcher.open_browser("brave")
    assert code == 1 and "could not be read" in text and world.cold == []


def test_a_browser_running_on_the_profile_without_its_lock_still_counts(world):
    world.naming = [(777, OURS)]
    assert launcher.open_browser("brave", None) == (0, "forward") and world.cold == []


def test_a_launch_an_agent_won_is_brought_forward_instead(world, monkeypatch):
    def lost(*_a):
        world.lock, world.commands = ("held", 777), {777: OURS}
        return 1, "Error: profile is held", None
    monkeypatch.setattr(launcher, "cold_launch", lost)
    assert launcher.open_browser("brave") == (0, "forward")


def test_open_is_refused_where_keep_alive_cannot_work(monkeypatch):
    monkeypatch.setattr(keep_alive, "supported", lambda: "keep_alive is not available on Windows")
    started = []
    monkeypatch.setattr(launcher, "cold_launch", lambda *a: started.append(a))
    code, text = launcher.open_browser("brave")
    assert code == 2 and "Windows" in text and started == []


@pytest.mark.parametrize("given,expected", [
    ("https://example.com/a?b=1", "https://example.com/a?b=1"),
    ("example.com", None),
    ("file:///tmp/a.html", None),
    ("javascript:alert(1)", None),
    ("data:text/html,x", None),
])
def test_urls(given, expected):
    assert launcher._url_from(argparse.Namespace(url=given)) == expected


def test_the_app_asks_once_before_its_first_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_STEALTH_DATA_DIR", str(tmp_path))
    started = []
    monkeypatch.setattr(launcher, "_detach", lambda: started.append(1) or 0)
    assert launcher.cli(["open", "--browser", "brave", "--from-app"]) == 3
    assert "debugging port" in capsys.readouterr().err and started == []
    assert launcher.cli(["open", "--browser", "brave", "--from-app", "--acknowledge"]) == 0
    marker = tmp_path / "launcher-acknowledged-brave"
    assert marker.is_file() and started == [1]
    if os.name == "posix":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert launcher.cli(["open", "--browser", "brave", "--from-app"]) == 0


def test_the_app_never_writes_into_another_app(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dock_icon, "ICON_WRITES", True)
    launcher.acknowledge("brave")
    monkeypatch.setattr(launcher, "_detach", lambda: 0)
    assert launcher.cli(["open", "--browser", "brave", "--from-app"]) == 0
    assert dock_icon.ICON_WRITES is False


def test_a_browser_the_person_opened_gets_the_long_cap(monkeypatch):
    monkeypatch.delenv(keep_alive.ENV_MAX_HOURS, raising=False)
    assert keep_alive.cap_hours("launcher") == keep_alive.MAX_HOURS_LIMIT
    assert keep_alive.cap_hours("agent") == keep_alive.DEFAULT_MAX_HOURS
    assert keep_alive.cap_hours(None) == keep_alive.DEFAULT_MAX_HOURS
    monkeypatch.setenv(keep_alive.ENV_MAX_HOURS, "5")
    assert keep_alive.cap_hours("launcher") == 5


def test_the_crash_handler_inside_brave_beta_is_not_a_browser(world):
    """A killed Brave Beta leaves its crash handler running, naming the profile and without
    --type=; taken for a browser it made the next click refuse to start one."""
    world.naming = [(9, "/Applications/Brave Browser Beta.app/Contents/Frameworks/Brave Browser Beta "
                        "Framework.framework/Helpers/chrome_crashpad_handler --monitor-self "
                        f"--database={PROFILE}/Crashpad --annotation=prod=Brave Beta_Mac")]
    assert launcher.open_browser("brave")[0] == 0 and world.cold


def test_brave_beta_is_a_brave_executable():
    assert brave_mode.is_brave_executable("/Applications/Brave Browser Beta.app/Contents/MacOS/Brave Browser Beta")
    assert state._is_browser_main_process(OURS)


# ── Brave Beta's choice ──────────────────────────────────────────────────────────

def _app(apps: Path, name: str, binary: str, version: str) -> Path:
    app = apps / name
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / binary
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    return app


@pytest.fixture
def betas(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    beta = _app(apps, "Brave Browser Beta.app", "Brave Browser Beta", "158.1.101.47")
    brave = _app(apps, "Brave Browser.app", "Brave Browser", "157.1.99.55")
    monkeypatch.setattr(brave_beta, "BETA_APP", beta)
    monkeypatch.setattr(brave_beta.sys, "platform", "darwin")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "1")
    monkeypatch.setattr(brave_beta, "ALLOW_PROFILE_MOVE", False)
    profile_version._binary_version.cache_clear()
    return argparse.Namespace(beta=str(beta / brave_beta.BETA_BINARY),
                              brave=str(brave / "Contents" / "MacOS" / "Brave Browser"), root=tmp_path)


def test_brave_beta_takes_a_new_profile(betas):
    assert brave_beta.choose(betas.root / "new", headless=False, brave=betas.brave) == (betas.beta, True, "")


def test_brave_beta_is_refused_a_profile_brave_made(betas, monkeypatch):
    profile = betas.root / "used"
    profile.mkdir()
    (profile / "Last Version").write_text("157.1.99.55")
    (profile / "Local State").write_text('{"brave": {}}')
    exe, on_beta, note = brave_beta.choose(profile, headless=False, brave=betas.brave)
    assert (exe, on_beta) == (betas.brave, False)
    assert "Brave 157.1.99.55" in note and "Brave Beta would delete its cookies" in note
    monkeypatch.setattr(brave_beta, "ALLOW_PROFILE_MOVE", True)
    exe, on_beta, note = brave_beta.choose(profile, headless=False, brave=betas.brave)
    assert (exe, on_beta) == (betas.beta, True) and "--move-to-beta" in note


def test_brave_beta_off_headless_or_missing(betas, monkeypatch):
    assert brave_beta.choose(None, headless=True, brave=betas.brave) == (betas.brave, False, "")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    assert brave_beta.choose(None, headless=False, brave=betas.brave) == (betas.brave, False, "")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "1")
    monkeypatch.setattr(brave_beta, "BETA_APP", betas.root / "gone" / "Brave Browser Beta.app")
    exe, on_beta, note = brave_beta.choose(None, headless=False, brave=betas.brave)
    assert (exe, on_beta) == (betas.brave, False) and "not installed" in note


def test_a_profile_brave_beta_opened_is_refused_to_brave(betas):
    profile = betas.root / "beta-made"
    profile.mkdir()
    (profile / "Last Version").write_text("158.1.101.47")
    refusal = profile_version.refusal(profile, betas.brave, name="Brave")
    assert "158.1.101.47" in refusal and "Brave 157.1.99.55" in refusal and "Brave Beta" in refusal


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_the_icon_goes_on_brave_beta_and_never_on_brave(tmp_path, monkeypatch):
    brave = tmp_path / "Brave Browser.app"
    brave.mkdir()
    monkeypatch.setattr(dock_icon, "BRAVE_APP", brave)
    link = tmp_path / "links" / "Brave Browser Beta.app"
    link.parent.mkdir()
    link.symlink_to(brave, target_is_directory=True)
    put = []
    monkeypatch.setattr(dock_icon, "_set_icon", lambda app, _icon: put.append(app))
    with pytest.raises(dock_icon._Note, match="refused"):
        dock_icon.put_icon_on(link)
    with pytest.raises(dock_icon._Note, match="refused"):
        dock_icon.put_icon_on(brave)
    assert put == [] and list(brave.iterdir()) == []


def test_no_icon_job_from_a_process_the_app_started(betas, monkeypatch):
    monkeypatch.setattr(dock_icon, "ICON_WRITES", False)
    monkeypatch.setattr(dock_icon, "_icon_failed", {})
    started = []
    monkeypatch.setattr(dock_icon, "_start", lambda job: started.append(job) or True)
    choice = brave_beta.prepare(headless=False)
    assert choice.executable == betas.beta and "does not modify other apps" in choice.note
    assert started == []


def test_the_cookie_reader_keeps_the_launch_keychain(monkeypatch, tmp_path):
    """A reader on the real Keychain read nothing a mock-keychain launch wrote, and asked the
    person's login Keychain for Brave Beta: a dialog on their screen."""
    import asyncio

    from mcp_stealth_chrome import server
    seen = []

    async def fake_start(config):
        seen.append(list(config.browser_args))
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_start_chrome", fake_start)
    with pytest.raises(RuntimeError):
        asyncio.run(server._read_v10_cookies(tmp_path, "Default", "/x/Brave Browser Beta",
                                             ["--use-mock-keychain", "--foo"]))
    assert "--use-mock-keychain" in seen[0] and "--foo" not in seen[0]
    seen.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(server._read_v10_cookies(tmp_path, "Default", "/x/Brave Browser Beta", []))
    assert "--use-mock-keychain" not in seen[0]


# ── the app bundle (macOS) ───────────────────────────────────────────────────────

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="the Dock launcher is macOS only")


@mac_only
def test_the_app_is_built_signed_and_declares_http(tmp_path):
    app = launcher.build_app("brave", tmp_path, cli="/nowhere/mcp-stealth-chrome")
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
        assert info["CFBundleIdentifier"] == "org.mcp-stealth.launcher.brave" and info["LSUIElement"] is True
        assert info["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["http", "https"]
        assert (app / "Contents" / "Resources" / "applet.icns").stat().st_size > 10_000
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        # Rebuilt over itself, and never over somebody else's app.
        assert launcher.build_app("brave", tmp_path, cli="/nowhere/mcp-stealth-chrome") == app
        other = tmp_path / "other"
        foreign = other / launcher.APP_NAMES["brave"] / "Contents"
        foreign.mkdir(parents=True)
        with open(foreign / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "com.someone.else"}, handle)
        with pytest.raises(RuntimeError, match="left alone"):
            launcher.build_app("brave", other, cli="")
        assert "left alone" in launcher.remove_app("brave", other)
        assert list((other / launcher.APP_NAMES["brave"]).iterdir()) == [foreign]
    finally:
        assert launcher.remove_app("brave", tmp_path).startswith("removed")


@mac_only
def test_a_missing_command_is_named_in_a_dialog(tmp_path):
    app = launcher.build_app("brave", tmp_path, cli="/nowhere/mcp-stealth-chrome")
    try:
        home = tmp_path / "home"
        home.mkdir()
        done = subprocess.run(["/bin/sh", str(app / "Contents" / "Resources" / "launch.sh")],
                              capture_output=True, text=True, timeout=30,
                              env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        assert done.returncode == 127, done
        assert "uv tool install mcp-stealth-chrome" in done.stderr
    finally:
        launcher.remove_app("brave", tmp_path)


# ── review F1 to F6 ──────────────────────────────────────────────────────────────

def test_f1_the_mock_keychain_is_refused_outside_tests(monkeypatch):
    """--use-mock-keychain reads and writes cookies under a key nobody else has: outside the test
    switch a launch carrying it is refused before anything starts."""
    import asyncio

    from mcp_stealth_chrome import server
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert "test" in server._mock_keychain_refusal(["--use-mock-keychain"])
    assert server._mock_keychain_refusal(["--mute-audio"]) == ""
    out = asyncio.run(server.browser_launch(persistent=False, headless=True,
                                            extra_args=["--use-mock-keychain"]))
    assert out.startswith("Error") and "--use-mock-keychain" in out
    out = asyncio.run(server.spawn_browser(instance_id="f1", persistent=False, headless=True,
                                           extra_args=["--use-mock-keychain"]))
    assert out.startswith("Error") and "--use-mock-keychain" in out
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert server._mock_keychain_refusal(["--use-mock-keychain"]) == ""


def test_f2_move_to_beta_refuses_what_it_cannot_prove(betas, monkeypatch):
    monkeypatch.setattr(brave_beta, "ALLOW_PROFILE_MOVE", True)
    profile = betas.root / "brave-made"
    profile.mkdir()
    (profile / "Local State").write_text('{"brave": {}}')
    (profile / "Last Version").write_text("157.1.99.55")
    assert brave_beta.choose(profile, headless=False, brave=betas.brave)[1] is True
    # Beta's version unreadable.
    monkeypatch.setattr(profile_version, "binary_version",
                        lambda exe: None if "Beta" in str(exe) else (157, 1, 99, 55))
    assert brave_beta.choose(profile, headless=False, brave=betas.brave)[1] is False
    monkeypatch.undo()
    monkeypatch.setattr(brave_beta, "ALLOW_PROFILE_MOVE", True)
    monkeypatch.setattr(brave_beta, "BETA_APP", pathlib_beta(betas))
    monkeypatch.setattr(brave_beta.sys, "platform", "darwin")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "1")
    # The profile's own version unknown, or a profile Chrome made, or a link.
    (profile / "Last Version").unlink()
    assert brave_beta.choose(profile, headless=False, brave=betas.brave)[1] is False
    (profile / "Last Version").write_text("157.1.99.55")
    (profile / "Local State").write_text('{"profile": {}}')
    assert brave_beta.choose(profile, headless=False, brave=betas.brave)[1] is False
    (profile / "Local State").write_text('{"brave": {}}')
    link = betas.root / "link"
    link.symlink_to(profile, target_is_directory=True)
    assert brave_beta.choose(link, headless=False, brave=betas.brave)[1] is False


def pathlib_beta(betas):
    return Path(betas.beta).parents[2]


def test_f3_one_reservation_per_profile_across_processes_and_reentrant_within_one(tmp_path, monkeypatch):
    from mcp_stealth_chrome import launch_lock
    monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(tmp_path / "ka"))
    profile = tmp_path / "brave-profile"
    other = subprocess.Popen([sys.executable, "-c", (
        "import os,sys,time;os.environ['MCP_STEALTH_KEEP_ALIVE_DIR']=sys.argv[2];"
        "from mcp_stealth_chrome import launch_lock as l\n"
        "with l.hold([sys.argv[1]], wait=5):\n print('held', flush=True); time.sleep(3)"),
        str(profile), str(tmp_path / "ka")], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(launcher.__file__).parents[1])})
    assert other.stdout.readline().strip() == "held"
    with pytest.raises(TimeoutError):
        with launch_lock.hold([profile], wait=0.5):
            pass
    other.wait(10)
    with launch_lock.hold([profile], wait=2):
        with launch_lock.hold([profile], wait=0):  # the launcher calls browser_launch in-process
            pass


def test_f3_open_and_browser_launch_take_the_same_reservation(world, monkeypatch):
    from mcp_stealth_chrome import launch_lock
    taken = []

    class Hold:
        def __init__(self, paths, wait):
            taken.append([str(p) for p in paths])

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(launch_lock, "hold", Hold)
    launcher.open_browser("brave")
    assert taken == [[PROFILE]] and world.cold


@pytest.mark.parametrize("given", ["file:///tmp/x.html", "chrome://settings", "brave://settings",
                                   "example.com", "ftp://example.com/", "javascript:alert(1)", ""])
def test_f4_only_http_and_https_reach_the_browser(given, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_STEALTH_DATA_DIR", str(tmp_path))
    launched = []
    monkeypatch.setattr(launcher, "_detach", lambda: launched.append(1) or 0)
    code = launcher.cli(["open", "--browser", "brave", "--url", given, "--from-app", "--acknowledge"])
    assert code == 1 and launched == [] and "http" in capsys.readouterr().err
    assert not launcher.acknowledged("brave"), "refused before the acknowledgement is written"


def test_f4_https_is_passed_on_unchanged():
    assert launcher._url_from(argparse.Namespace(url="https://example.com/a?b=1#c")) == "https://example.com/a?b=1#c"
    assert launcher._url_from(argparse.Namespace(url="HTTP://example.com")) == "HTTP://example.com"


@pytest.mark.parametrize("which", ["chrome", "brave"])
def test_f5_a_process_the_app_started_writes_nothing_into_another_app(which, betas, monkeypatch, tmp_path):
    """The custom-icon flag left without its Icon file used to be cleared (setxattr on the Beta
    bundle) before ICON_WRITES was looked at."""
    monkeypatch.setattr(dock_icon, "ICON_WRITES", False)
    monkeypatch.setattr(dock_icon, "_icon_failed", {})
    writes = []
    monkeypatch.setattr(dock_icon, "_clear_dangling_flag", lambda app: writes.append(("clear", app)))
    monkeypatch.setattr(dock_icon, "_start", lambda job: writes.append(("job", job)) or True)
    monkeypatch.setattr(dock_icon, "_write_mark", lambda *a: writes.append(("mark", a)))
    monkeypatch.setattr(dock_icon, "_set_icon", lambda *a: writes.append(("icon", a)))
    if which == "brave":
        choice = brave_beta.prepare(headless=False)
    else:
        chrome_beta = _app(tmp_path / "C", "Google Chrome Beta.app", "Google Chrome Beta", "158.0.1.2")
        monkeypatch.setattr(dock_icon, "BETA_APP", chrome_beta)
        monkeypatch.setattr(dock_icon.sys, "platform", "darwin")
        choice = _REAL_CHROME_PREPARE(headless=False)
    assert choice.executable and "does not modify other apps" in choice.note
    assert writes == []


def test_f6_a_warm_click_that_shows_nothing_is_not_a_success(monkeypatch):
    found = launcher.Holder("ours", pid=1234, port=12345)
    monkeypatch.setattr(launcher._keep_alive, "_browser_pid", lambda _h, _p: 1234)
    monkeypatch.setattr(launcher, "_pages", lambda _p: [])
    monkeypatch.setattr(launcher._watcher, "browser_call", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "activating", lambda: True)
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "ok")
    code, text = launcher.bring_forward(found, None)
    assert code != 0 and "no window" in text and "brought it forward" not in text, text
    # A window there, but activation refused: running, not shown.
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "refused")
    monkeypatch.setattr(launcher, "_pages", lambda _p: [{"id": "T1"}])
    monkeypatch.setattr(launcher._watcher, "browser_call", lambda *a, **kw: {})
    code, text = launcher.bring_forward(found, None)
    assert code != 0 and "refused" in text and "brought it forward" not in text, text
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "ok")
    assert launcher.bring_forward(found, None)[0] == 0


@pytest.mark.skipif(os.name != "posix", reason="the reservation uses flock")
def test_f3_browser_launch_waits_for_a_launch_another_process_has_reserved(tmp_path, monkeypatch):
    """A click on the launcher holds the profile's reservation while its browser starts; a server
    launching on that profile meanwhile waits for it instead of starting a second browser."""
    import asyncio

    from mcp_stealth_chrome import server
    monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(tmp_path / "ka"))
    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "BROWSER_LAUNCH_TIMEOUT", 0.5)
    monkeypatch.setattr("mcp_stealth_chrome.state.get_profile_affinity", lambda: None)
    started = []

    async def no_start(config):
        started.append(config)
        raise RuntimeError("must not start")

    monkeypatch.setattr(server, "_start_chrome", no_start)
    other = subprocess.Popen([sys.executable, "-c", (
        "import os,sys,time;os.environ['MCP_STEALTH_KEEP_ALIVE_DIR']=sys.argv[2];"
        "from mcp_stealth_chrome import launch_lock as l\n"
        "with l.hold([sys.argv[1]], wait=5):\n print('held', flush=True); time.sleep(4)"),
        str(tmp_path / "profile"), str(tmp_path / "ka")], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(launcher.__file__).parents[1])})
    try:
        assert other.stdout.readline().strip() == "held"
        out = asyncio.run(server.browser_launch(headless=True, persistent=True))
        assert out.startswith("Error") and "another launch on" in out, out
        assert started == []
    finally:
        other.wait(10)


# ── review 2: F3 and F6 ─────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a folder this user cannot write")
def test_f3_an_unwritable_lock_folder_lets_the_launch_go_on_as_before_and_says_so(tmp_path, monkeypatch):
    """A keep-alive folder that exists but cannot be written raised PermissionError out of every
    persistent launch. The launch goes on as it did before reservations existed, and says so."""
    import asyncio

    from mcp_stealth_chrome import launch_lock, server
    folder = tmp_path / "ka"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(folder))
        profile = tmp_path / "profile"

        async def reserved():
            async with launch_lock.reserve([profile], 1) as busy:
                return busy, launch_lock.note(), server._nav_result("Browser launched", {"final_url": "about:blank"})

        busy, note, result = asyncio.run(reserved())
        assert busy == "" and "no launch reservation" in note and str(folder) in note
        assert "no launch reservation" in result.splitlines()[0], "the launch result says so"
        assert launch_lock.note() == "", "the note belongs to that launch only"
        with launch_lock.hold([profile], 1):
            pass
        monkeypatch.setattr(server, "PROFILE_DIR", profile)
        monkeypatch.setattr("mcp_stealth_chrome.state.get_profile_affinity", lambda: None)
        # This launch settles the process on its profile; later tests must not inherit that.
        monkeypatch.setattr("mcp_stealth_chrome.state._PROFILE_AFFINITY", None)

        async def no_start(config):
            raise RuntimeError("stopped here (simulated)")

        monkeypatch.setattr(server, "_start_chrome", no_start)
        out = asyncio.run(server.browser_launch(headless=True, persistent=True))
        assert isinstance(out, str) and "stopped here" in out, out
    finally:
        folder.chmod(0o700)


def test_f3_windows_reserves_with_its_own_file_lock(tmp_path, monkeypatch):
    """No flock on Windows: the reservation takes a byte lock on the same file (msvcrt.locking),
    which Windows releases when the handle closes or the process ends."""
    from mcp_stealth_chrome import launch_lock
    monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(tmp_path / "ka"))
    locked: dict = {}

    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0

        @staticmethod
        def locking(fd, mode, nbytes):
            name = os.readlink(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else str(fd)
            if mode == FakeMsvcrt.LK_NBLCK:
                if locked.get("held"):
                    raise OSError(36, "locked")
                locked["held"] = name

    monkeypatch.setattr(launch_lock, "fcntl", None)
    monkeypatch.setattr(launch_lock, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(launch_lock, "_held", {})
    profile = tmp_path / "p"
    assert launch_lock._keys([profile]), "Windows must reserve too"
    assert launch_lock._try(launch_lock._keys([profile])[0]) and locked.get("held")
    launch_lock._release(launch_lock._keys([profile])[0])


def test_f6_a_cold_start_not_brought_forward_is_a_failure(world, monkeypatch):
    monkeypatch.setattr(launcher, "activating", lambda: True)
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "refused")
    code, text = launcher.open_browser("brave")
    assert code == 1 and "could not be brought to the front" in text and world.cold


def test_f6_a_target_chrome_would_not_activate_is_a_failure(monkeypatch):
    found = launcher.Holder("ours", pid=1234, port=12345)
    monkeypatch.setattr(launcher._keep_alive, "_browser_pid", lambda _h, _p: 1234)
    monkeypatch.setattr(launcher, "_pages", lambda _p: [{"id": "T1"}])
    monkeypatch.setattr(launcher._watcher, "browser_call", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "activating", lambda: True)
    monkeypatch.setattr(launcher, "activate_pid", lambda _pid: "ok")
    code, text = launcher.bring_forward(found, None)
    assert code == 1 and "brought it forward" not in text and "activateTarget" in text, text
