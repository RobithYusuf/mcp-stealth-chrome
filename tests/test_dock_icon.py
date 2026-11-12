"""A headed stealth browser runs from Chrome Beta carrying the project icon, when asked for.
Fakes only, apart from tests marked for macOS that set a real icon on a bundle in a temp folder:
nothing here touches /Applications."""
import asyncio
import json
import os
import plistlib
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import dock_icon, server

from test_focus_restore import _fake_launch, _launch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason=(
    "The Dock icon is macOS only, and the fake bundles here use names Windows cannot create "
    "(the custom-icon file is `Icon\\r`). The module importing on Windows is test_smoke's job."))


def _make_app(folder: Path, name: str, binary: str, version: str) -> Path:
    app = folder / name
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    exe = app / "Contents" / "MacOS" / binary
    exe.write_text(f"{binary} {version}")
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version,
                       "CFBundleVersion": version.split(".", 2)[2]}, handle)
    return app


def _tree(app: Path) -> dict:
    return {str(p.relative_to(app)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(app.rglob("*"))}


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A fake macOS with Chrome Beta and Google Chrome in a temp Applications folder. Setting
    the icon is a stand-in that writes `Icon\\r` and records the call."""
    apps = tmp_path / "Applications"
    calls = SimpleNamespace(icon=[], marks={}, block=None)
    calls.beta = _make_app(apps, "Google Chrome Beta.app", "Google Chrome Beta", "158.0.8127.18")
    calls.chrome = _make_app(apps, "Google Chrome.app", "Google Chrome", "157.0.8103.39")

    def set_icon(app, _icon):
        if calls.block is not None:
            calls.block.wait(10)
        calls.icon.append(Path(app))
        (Path(app) / dock_icon.ICON_FILE).write_bytes(b"icns")

    monkeypatch.setattr(dock_icon.sys, "platform", "darwin")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "1")
    monkeypatch.setattr(dock_icon, "BETA_APP", calls.beta)
    monkeypatch.setattr(dock_icon, "CHROME_APP", calls.chrome)
    monkeypatch.setattr(dock_icon, "_icon_failed", {})
    monkeypatch.setattr(dock_icon, "_worker", None)
    monkeypatch.setattr(dock_icon, "_last_note", "")
    monkeypatch.setattr(dock_icon, "_set_icon", set_icon)
    monkeypatch.setattr(dock_icon, "_resource_fork_size",
                        lambda path: path.stat().st_size if path.exists() else 0)
    monkeypatch.setattr(dock_icon, "_read_mark", lambda path: calls.marks.get(str(path), "")
                        if path.exists() else "")
    monkeypatch.setattr(dock_icon, "_write_mark", lambda path, digest: calls.marks.__setitem__(
        str(path), digest))
    monkeypatch.setattr(dock_icon, "_running_from", lambda _app: False)
    monkeypatch.setattr(dock_icon, "_unregister", lambda _app: None)
    calls.cleared = []
    monkeypatch.setattr(dock_icon, "_clear_dangling_flag", calls.cleared.append)
    calls.root = tmp_path / "home" / "app"
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(calls.root))
    calls.binary = str(calls.beta / "Contents" / "MacOS" / "Google Chrome Beta")
    return calls


def _settled(mac):
    choice = dock_icon.prepare(headless=False)
    assert dock_icon.wait(10)
    return choice


def test_a_headed_launch_runs_from_beta_with_the_icon(mac):
    first = _settled(mac)
    assert first.executable == mac.binary and first.version == "158.0.8127.18"
    assert first.note == dock_icon.PENDING_NOTE, "the icon was missing, and this launch says so"
    assert mac.icon == [mac.beta]
    again = dock_icon.prepare(headless=False)
    assert again == dock_icon.DockChrome(executable=mac.binary, version="158.0.8127.18")
    assert mac.icon == [mac.beta], "an icon in place is not set again"


def test_the_icon_never_touches_contents_or_google_chrome(mac):
    beta_contents, chrome = _tree(mac.beta / "Contents"), _tree(mac.chrome)
    _settled(mac)
    assert _tree(mac.beta / "Contents") == beta_contents
    assert _tree(mac.chrome) == chrome


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "2", "beta"])
def test_it_is_off_unless_asked_for(mac, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(dock_icon.ENV_SWITCH)
    else:
        monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert dock_icon.prepare(headless=False) == dock_icon.DockChrome()
    assert dock_icon.chosen(headless=False) is None
    assert dock_icon.start_in_background() is False
    assert dock_icon.status()["state"] == "off"
    assert mac.icon == [] and not (mac.beta / dock_icon.ICON_FILE).exists()


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_the_env_switch_turns_it_on(mac, monkeypatch, value):
    monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert _settled(mac).executable == mac.binary


def test_headless_keeps_google_chrome_and_sets_nothing(mac):
    assert dock_icon.prepare(headless=True) == dock_icon.DockChrome()
    assert dock_icon.chosen(headless=True) is None
    assert not dock_icon.building() and mac.icon == []


def test_other_systems_have_no_dock_and_report_nothing(mac, monkeypatch):
    monkeypatch.setattr(dock_icon.sys, "platform", "linux")
    assert dock_icon.prepare(headless=False) == dock_icon.DockChrome()
    assert dock_icon.start_in_background() is False
    assert dock_icon.status()["state"] == "unavailable"


def test_without_beta_it_is_google_chrome_and_the_result_says_why(mac):
    shutil.rmtree(mac.beta)
    choice = dock_icon.prepare(headless=False)
    assert choice.executable is None and "Google Chrome Beta is not installed" in choice.note
    assert dock_icon.chosen(headless=False) is None
    assert dock_icon.status() == {"state": "beta-missing", "detail": dock_icon.missing_note()}
    assert mac.icon == []


def test_an_update_that_took_the_icon_off_gets_it_back(mac):
    _settled(mac)
    (mac.beta / dock_icon.ICON_FILE).unlink()  # what an update of Beta does
    assert dock_icon.status()["state"] == "icon-missing"
    choice = _settled(mac)
    assert choice.executable == mac.binary and choice.note == dock_icon.PENDING_NOTE
    assert mac.icon == [mac.beta, mac.beta]
    assert dock_icon.status()["state"] == "beta-ready"


def test_a_launch_without_the_icon_first_clears_a_dangling_flag(mac):
    """Without it the Dock shows a folder for that launch, measured on Chrome Beta 158."""
    _settled(mac)
    assert mac.cleared == [mac.beta]
    dock_icon.prepare(headless=False)
    assert mac.cleared == [mac.beta], "not while the icon is in place"


def test_a_new_project_icon_replaces_the_old_one(mac, monkeypatch):
    _settled(mac)
    monkeypatch.setattr(dock_icon, "_icon_digest", lambda: "another")
    assert dock_icon.prepare(headless=False).note == dock_icon.PENDING_NOTE
    assert dock_icon.wait(10) and len(mac.icon) == 2
    assert dock_icon.prepare(headless=False).note == ""


def test_an_icon_without_our_mark_is_replaced(mac):
    (mac.beta / dock_icon.ICON_FILE).write_bytes(b"someone else's")
    assert _settled(mac).note == dock_icon.PENDING_NOTE and mac.icon == [mac.beta]


def test_a_launch_never_waits_for_the_icon(mac):
    mac.block = threading.Event()
    started = time.monotonic()
    choice = dock_icon.prepare(headless=False)
    took = time.monotonic() - started
    try:
        assert choice.executable == mac.binary and choice.note == dock_icon.PENDING_NOTE
        assert took < 0.5, took
        assert dock_icon.building()
        # A second launch meanwhile starts no second job.
        assert dock_icon.prepare(headless=False).note == dock_icon.PENDING_NOTE
    finally:
        mac.block.set()
    assert dock_icon.wait(10) and mac.icon == [mac.beta]


def test_an_icon_that_cannot_be_set_is_a_note_and_is_not_retried(mac, monkeypatch):
    tries = []

    def refuse(app, _icon):
        tries.append(app)
        raise dock_icon._Note("the icon could not be set: osascript answered 'refused'")

    monkeypatch.setattr(dock_icon, "_set_icon", refuse)
    first = _settled(mac)
    assert first.executable == mac.binary, "Beta is still used, with its own icon"
    second = dock_icon.prepare(headless=False)
    assert second.executable == mac.binary and "osascript answered 'refused'" in second.note
    assert dock_icon.wait(10) and len(tries) == 1, "remembered, not tried on every launch"
    assert dock_icon.status()["state"] == "icon-missing"
    assert "refused" in dock_icon.status()["detail"]
    # A Beta update is a new chance.
    os.utime(mac.beta / "Contents" / "Info.plist", ns=(1, 1))
    _settled(mac)
    assert len(tries) == 2


def test_an_icon_that_did_not_take_is_a_failure(mac, monkeypatch):
    monkeypatch.setattr(dock_icon, "_set_icon", lambda *_a: None)
    _settled(mac)
    assert "did not take" in dock_icon.prepare(headless=False).note


@pytest.mark.parametrize("which", ["chrome", "renamed"])
def test_the_icon_only_ever_goes_on_beta(mac, which, tmp_path):
    if which == "chrome":
        target = mac.chrome
    else:
        target = tmp_path / "Something Else.app"
        target.mkdir()
    before = _tree(target)
    with pytest.raises(dock_icon._Note, match="refused"):
        dock_icon.put_icon_on(target)
    assert mac.icon == [] and _tree(target) == before


def test_a_link_named_like_beta_that_points_at_chrome_is_refused(mac, tmp_path):
    link = tmp_path / "links" / "Google Chrome Beta.app"
    link.parent.mkdir()
    link.symlink_to(mac.chrome, target_is_directory=True)
    with pytest.raises(dock_icon._Note, match="refused"):
        dock_icon.put_icon_on(link)
    assert mac.icon == [] and not (mac.chrome / dock_icon.ICON_FILE).exists()


def test_prepare_and_status_never_raise(mac, monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("disk went away")

    monkeypatch.setattr(dock_icon, "beta_binary", boom)
    assert "disk went away" in dock_icon.prepare(headless=False).note
    report = dock_icon.status()
    assert report["state"] == "unavailable" and "disk went away" in report["detail"]


def test_status_reads_only(mac):
    before = _tree(mac.beta.parent)
    report = dock_icon.status()
    assert report["state"] == "icon-missing" and "158.0.8127.18" in report["detail"]
    assert _tree(mac.beta.parent) == before and not dock_icon.building()
    assert not mac.root.exists()


def test_status_off_only_suggests_the_switch_when_it_would_work(mac, monkeypatch):
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    assert "158.0.8127.18" in dock_icon.status()["detail"]
    shutil.rmtree(mac.beta)
    detail = dock_icon.status()["detail"]
    assert "no effect" in detail and "not installed" in detail


def test_status_mentions_an_old_copy_still_on_disk(mac):
    _old_copy(mac)
    assert "still there" in dock_icon.status()["old_copy"]


def test_starting_the_server_puts_a_missing_icon_on(mac):
    assert dock_icon.start_in_background() is True
    assert dock_icon.wait(10) and mac.icon == [mac.beta]
    assert dock_icon.start_in_background() is False, "nothing left to do"


def test_starting_the_server_does_nothing_without_beta(mac):
    shutil.rmtree(mac.beta)
    assert dock_icon.start_in_background() is False


def test_starting_the_server_returns_at_once(mac):
    mac.block = threading.Event()
    started = time.monotonic()
    try:
        assert dock_icon.start_in_background() is True
        assert time.monotonic() - started < 0.5
    finally:
        mac.block.set()
    assert dock_icon.wait(10)


def test_a_background_failure_stays_in_its_thread(mac, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("AppKit crashed")

    monkeypatch.setattr(dock_icon, "_set_icon", boom)
    problems = []
    previous = threading.excepthook
    threading.excepthook = lambda args: problems.append(args)
    try:
        assert dock_icon.start_in_background() is True
        assert dock_icon.wait(10)
    finally:
        threading.excepthook = previous
    assert problems == [] and "AppKit crashed" in dock_icon._last_note
    assert "AppKit crashed" in dock_icon.status()["last_note"]


def test_the_icon_ships_with_the_package():
    data = dock_icon.ICON_PATH.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 10_000


@pytest.mark.skipif(sys.platform != "darwin", reason="a real custom icon needs macOS")
def test_the_real_icon_goes_on_a_bundle_root_and_nowhere_else(tmp_path, monkeypatch):
    """The real NSWorkspace call and the real mark, on a stand-in bundle in a temp folder."""
    app = _make_app(tmp_path, "Google Chrome Beta.app", "Google Chrome Beta", "158.0.8127.18")
    monkeypatch.setattr(dock_icon, "BETA_APP", app)
    monkeypatch.setattr(dock_icon, "_icon_failed", {})
    contents = _tree(app / "Contents")
    assert not dock_icon._icon_current(app)
    dock_icon.put_icon_on(app)
    assert dock_icon._resource_fork_size(app / dock_icon.ICON_FILE) > 1000
    assert dock_icon._read_mark(app / dock_icon.ICON_FILE) == dock_icon._icon_digest()
    assert dock_icon._icon_current(app)
    assert _tree(app / "Contents") == contents
    assert sorted(p.name for p in app.iterdir()) == ["Contents", dock_icon.ICON_FILE]
    # What an update does: the icon file goes, and the check sees it.
    (app / dock_icon.ICON_FILE).unlink()
    assert not dock_icon._icon_current(app)


@pytest.mark.skipif(sys.platform != "darwin", reason="FinderInfo is a macOS attribute")
def test_a_dangling_custom_icon_flag_is_cleared_and_a_real_one_kept(tmp_path, monkeypatch):
    import subprocess
    app = _make_app(tmp_path, "Google Chrome Beta.app", "Google Chrome Beta", "158.0.8127.18")
    monkeypatch.setattr(dock_icon, "BETA_APP", app)
    monkeypatch.setattr(dock_icon, "_icon_failed", {})

    def flags():
        out = subprocess.run(["xattr", "-px", "com.apple.FinderInfo", str(app)],
                             capture_output=True, text=True).stdout.split()
        return int(out[8], 16) if len(out) > 8 else 0  # all zeros: macOS drops the attribute

    assert dock_icon._clear_dangling_flag(app) is False  # no FinderInfo at all
    dock_icon.put_icon_on(app)
    assert flags() & 0x04
    assert dock_icon._clear_dangling_flag(app) is False and flags() & 0x04, "a real icon keeps it"
    (app / dock_icon.ICON_FILE).unlink()
    started = time.monotonic()
    assert dock_icon._clear_dangling_flag(app) is True
    assert time.monotonic() - started < 0.05
    assert not flags() & 0x04
    assert dock_icon._clear_dangling_flag(app) is False
    assert sorted(p.name for p in app.iterdir()) == ["Contents"]


# ── the server side ──────────────────────────────────────────────────────────

_CHOICE = server._LaunchChoice("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _config(path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
    return SimpleNamespace(browser_executable_path=path)


def test_the_server_points_a_headed_launch_at_beta(monkeypatch):
    seen = {}

    def prepare(binary, *, headless, profile_refusal=""):
        seen.update(binary=binary, headless=headless)
        return dock_icon.DockChrome(executable="/Beta/Contents/MacOS/Google Chrome Beta",
                                    version="158.0.8127.18")

    monkeypatch.setattr(dock_icon, "prepare", prepare)
    config = _config()
    note = asyncio.run(server._use_dock_icon_browser(config, False, _CHOICE))
    assert note == " [Dock icon: Chrome Beta 158.0.8127.18]"
    assert config.browser_executable_path == "/Beta/Contents/MacOS/Google Chrome Beta"
    assert seen["headless"] is False


def test_the_server_names_the_pending_icon(monkeypatch):
    monkeypatch.setattr(dock_icon, "prepare", lambda *_a, **_kw: dock_icon.DockChrome(
        executable="/Beta/x", version="158.0.8127.18", note=dock_icon.PENDING_NOTE))
    note = asyncio.run(server._use_dock_icon_browser(_config(), False, _CHOICE))
    assert note == f" [Dock icon: Chrome Beta 158.0.8127.18; {dock_icon.PENDING_NOTE}]"


def test_the_server_keeps_chrome_and_says_why_without_beta(monkeypatch):
    monkeypatch.setattr(dock_icon, "prepare",
                        lambda *_a, **_kw: dock_icon.DockChrome(note=dock_icon.missing_note()))
    config = _config()
    note = asyncio.run(server._use_dock_icon_browser(config, False, _CHOICE))
    assert note == f" [Dock icon: {dock_icon.missing_note()}]"
    assert config.browser_executable_path.startswith("/Applications/Google Chrome.app")


def test_the_server_says_nothing_when_it_is_off(monkeypatch):
    monkeypatch.setattr(dock_icon, "prepare", lambda *_a, **_kw: dock_icon.DockChrome())
    config = _config()
    assert asyncio.run(server._use_dock_icon_browser(config, False, _CHOICE)) == ""
    assert config.browser_executable_path.startswith("/Applications/Google Chrome.app")


def test_the_server_survives_prepare_raising(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("thread died")

    monkeypatch.setattr(dock_icon, "prepare", boom)
    config = _config()
    assert "thread died" in asyncio.run(server._use_dock_icon_browser(config, False, _CHOICE))
    assert config.browser_executable_path.startswith("/Applications/")


def _launch_with(monkeypatch, choice):
    order = _fake_launch(monkeypatch)
    started = {}
    original = server._start_chrome

    async def start(config):
        started["path"] = str(config.browser_executable_path)
        return await original(config)

    monkeypatch.setattr(server, "_start_chrome", start)

    def prepare(binary, *, headless, profile_refusal=""):
        order.append("prepare")
        started.update(headless=headless, source=binary)
        return dock_icon.DockChrome() if headless else choice

    monkeypatch.setattr(dock_icon, "prepare", prepare)
    return order, started


def test_browser_launch_picks_beta_before_reading_the_frontmost_app(monkeypatch):
    order, started = _launch_with(monkeypatch, dock_icon.DockChrome(
        executable="/Beta/Contents/MacOS/Google Chrome Beta", version="158.0.8127.18"))
    out = _launch(headless=False)
    assert started["path"] == "/Beta/Contents/MacOS/Google Chrome Beta"
    assert "[Dock icon: Chrome Beta 158.0.8127.18]" in out, out
    assert order.index("prepare") < order.index("read") < order.index("start")


def test_browser_launch_reports_a_fallback_and_still_launches(monkeypatch):
    _order, started = _launch_with(monkeypatch, dock_icon.DockChrome(note=dock_icon.missing_note()))
    out = _launch(headless=False)
    assert "launched" in out and not out.startswith("Error"), out
    assert f"[Dock icon: {dock_icon.missing_note()}]" in out
    assert started["path"] == started["source"]


def test_headless_browser_launch_is_not_given_beta(monkeypatch):
    _order, started = _launch_with(monkeypatch, dock_icon.DockChrome(executable="/Beta/x"))
    out = _launch(headless=True)
    assert started["headless"] is True and started["path"] != "/Beta/x"
    assert "Dock icon" not in out


def test_server_status_carries_the_state_without_starting_anything(monkeypatch):
    started = []
    monkeypatch.setattr(dock_icon, "status", lambda root=None: {"state": "beta-ready", "detail": "x"})
    monkeypatch.setattr(dock_icon, "_start", lambda *_a: started.append(1))
    out = asyncio.run(server.mcp._tool_manager._tools["server_status"].fn())
    assert json.loads(out)["dock_icon"] == {"state": "beta-ready", "detail": "x"}
    assert started == []


def test_server_status_survives_the_check_failing(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("no")

    monkeypatch.setattr(dock_icon, "status", boom)
    out = asyncio.run(server.mcp._tool_manager._tools["server_status"].fn())
    assert json.loads(out)["dock_icon"]["state"] == "unavailable"


def test_running_from_sees_a_process_under_either_spelling_of_the_path(monkeypatch, tmp_path):
    real = tmp_path / "real"
    (real / "X.app").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    listing = f"{real.resolve()}/X.app/Contents/MacOS/Google Chrome --flag\n/usr/bin/other\n"
    monkeypatch.setattr(dock_icon.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(stdout=listing))
    assert dock_icon._running_from(tmp_path / "link" / "X.app")
    assert not dock_icon._running_from(tmp_path / "link" / "Y.app")


def test_running_from_treats_an_unreadable_process_list_as_in_use(monkeypatch, tmp_path):
    def boom(*_a, **_kw):
        raise OSError("no ps")

    monkeypatch.setattr(dock_icon.subprocess, "run", boom)
    assert dock_icon._running_from(tmp_path / "X.app")


# ── the old Chrome copy: removal only ────────────────────────────────────────

def _old_copy(mac) -> Path:
    """What 0.13.0 left in the app folder: the copy, its marker and its lock."""
    copy = mac.root / dock_icon.APP_NAME
    (copy / "Contents" / "MacOS").mkdir(parents=True)
    (copy / "Contents" / "MacOS" / "Google Chrome").write_text("chrome")
    (mac.root / dock_icon.MARKER_NAME).write_text(json.dumps({"verified": True}))
    (mac.root / ".lock").write_text("")
    return copy


def _everything_under(folder: Path) -> list:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


@pytest.mark.parametrize("switch", ["0", "1"])
def test_a_server_start_removes_an_old_copy_either_way(mac, monkeypatch, switch):
    copy = _old_copy(mac)
    (mac.root / ".building-1-dead").mkdir()
    gone = []
    monkeypatch.setattr(dock_icon, "_unregister", gone.append)
    monkeypatch.setenv(dock_icon.ENV_SWITCH, switch)
    assert dock_icon.start_in_background() is True
    assert dock_icon.wait(10)
    assert not copy.exists() and not mac.root.exists()
    assert gone == [copy], "it is taken out of LaunchServices first"


def test_an_old_copy_in_use_is_left_and_said_so(mac, monkeypatch):
    copy = _old_copy(mac)
    before = _everything_under(mac.root)
    monkeypatch.setattr(dock_icon, "_running_from", lambda _app: True)
    assert dock_icon.discard_copy() == "left alone: a browser is running from the copy"
    assert copy.is_dir() and _everything_under(mac.root) == before
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    assert dock_icon.start_in_background() is True and dock_icon.wait(10)
    assert "running from the copy" in dock_icon.status()["last_note"]
    assert copy.is_dir()


def test_the_copy_is_not_removed_while_another_server_builds(mac):
    fcntl = pytest.importorskip("fcntl", reason="flock is POSIX")
    copy = _old_copy(mac)
    with open(mac.root / ".lock", "a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        assert dock_icon.discard_copy() == "left alone: a server is building there"
    assert copy.is_dir() and (mac.root / dock_icon.MARKER_NAME).is_file()


def test_without_a_file_lock_the_copy_is_left_alone(mac, monkeypatch):
    copy = _old_copy(mac)
    monkeypatch.setattr(dock_icon, "fcntl", None)
    assert dock_icon.discard_copy() == "left alone: no file lock on this platform"
    assert copy.is_dir() and (mac.root / dock_icon.MARKER_NAME).is_file()


def test_removal_takes_only_what_this_module_made(mac):
    """Four names, and nothing else that happens to be in the folder."""
    _old_copy(mac)
    (mac.root / ".building-9-old" / "previous").mkdir(parents=True)
    (mac.root / "notes.txt").write_text("the person's own file")
    (mac.root / "Other.app" / "Contents").mkdir(parents=True)
    (mac.root / "Stealth Chrome.app.bak").mkdir()
    assert dock_icon.discard_copy() == "removed"
    assert _everything_under(mac.root) == ["Other.app", "Other.app/Contents", "Stealth Chrome.app.bak", "notes.txt"]


def test_removal_only_works_in_the_configured_folder(mac, tmp_path):
    _old_copy(mac)
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(mac.root, elsewhere, symlinks=True)
    before = _everything_under(elsewhere)
    assert dock_icon.discard_copy(elsewhere).startswith("refused")
    assert dock_icon.discard_copy(mac.root.parent).startswith("refused")
    assert _everything_under(elsewhere) == before and (mac.root / dock_icon.APP_NAME).is_dir()


@pytest.mark.parametrize("place", ["home", "above_home", "root"])
def test_removal_refuses_a_folder_that_is_not_its_own(mac, monkeypatch, tmp_path, place):
    """MCP_STEALTH_APP_DIR pointed at the home folder must not turn this loose on it."""
    home = tmp_path / "users" / "me"
    (home / dock_icon.APP_NAME / "Contents").mkdir(parents=True)
    (home / ".lock").write_text("")
    (home.parent / ".lock").write_text("")
    (home.parent / dock_icon.APP_NAME).mkdir()
    monkeypatch.setattr(dock_icon.Path, "home", classmethod(lambda cls: home))
    target = {"home": home, "above_home": home.parent, "root": Path("/")}[place]
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(target))
    before = _everything_under(home.parent)
    assert dock_icon.discard_copy().startswith("refused"), dock_icon.discard_copy()
    assert _everything_under(home.parent) == before


def test_removal_refuses_a_folder_this_server_never_built_in(mac):
    """No lock file means the folder is someone else's, even with a bundle of that name."""
    (mac.root / dock_icon.APP_NAME / "Contents").mkdir(parents=True)
    (mac.root / dock_icon.MARKER_NAME).write_text("{}")
    assert dock_icon.discard_copy().startswith("refused")
    assert (mac.root / dock_icon.APP_NAME / "Contents").is_dir()


def test_removal_never_follows_a_link(mac, tmp_path):
    precious = tmp_path / "precious"
    (precious / "Contents").mkdir(parents=True)
    (precious / "Contents" / "data").write_text("keep")
    mac.root.mkdir(parents=True)
    (mac.root / ".lock").write_text("")
    (mac.root / dock_icon.APP_NAME).symlink_to(precious, target_is_directory=True)
    (mac.root / ".building-1-x").symlink_to(precious, target_is_directory=True)
    assert dock_icon.discard_copy().startswith("refused")
    assert (precious / "Contents" / "data").read_text() == "keep"
    assert (mac.root / dock_icon.APP_NAME).is_symlink()
    (mac.root / dock_icon.APP_NAME).unlink()
    (mac.root / dock_icon.APP_NAME / "Contents").mkdir(parents=True)
    (mac.root / dock_icon.APP_NAME / "Contents" / "out").symlink_to(precious, target_is_directory=True)
    assert dock_icon.discard_copy() == "removed"
    assert (precious / "Contents" / "data").read_text() == "keep"
    assert (mac.root / ".building-1-x").is_symlink(), "a linked build folder is left as it is"


def test_removal_refuses_an_app_folder_that_is_a_link(mac, tmp_path):
    real = tmp_path / "real"
    (real / dock_icon.APP_NAME / "Contents").mkdir(parents=True)
    (real / ".lock").write_text("")
    mac.root.parent.mkdir(parents=True)
    mac.root.symlink_to(real, target_is_directory=True)
    assert dock_icon.discard_copy() == "nothing to remove"
    assert (real / dock_icon.APP_NAME / "Contents").is_dir()


def test_an_unreadable_process_list_means_the_copy_stays(mac, monkeypatch):
    copy = _old_copy(mac)
    monkeypatch.undo()
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(mac.root))
    monkeypatch.setattr(dock_icon, "_unregister", lambda _app: None)

    def boom(*_a, **_kw):
        raise OSError("no ps")

    monkeypatch.setattr(dock_icon.subprocess, "run", boom)  # the real _running_from, failing
    assert dock_icon.discard_copy() == "left alone: a browser is running from the copy"
    assert copy.is_dir()


def test_removal_errors_stay_in_the_background_thread(mac, monkeypatch):
    _old_copy(mac)

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(dock_icon.shutil, "rmtree", boom)
    problems = []
    previous = threading.excepthook
    threading.excepthook = lambda args: problems.append(args)
    try:
        assert dock_icon.start_in_background() is True
        assert dock_icon.wait(10)
    finally:
        threading.excepthook = previous
    assert problems == [] and "PermissionError" in dock_icon._last_note


def test_a_profile_beta_must_not_open_means_google_chrome_and_no_icon_work(mac):
    choice = dock_icon.prepare(headless=False, profile_refusal="this profile already holds data")
    assert choice == dock_icon.DockChrome(note="Google Chrome, because this profile already holds data")
    assert not dock_icon.building() and mac.icon == [] and mac.cleared == []
