"""The Chrome copy that gives a headed stealth browser its own Dock icon. Fakes only: the clone,
the icon and the signature check are stood in for, so this runs anywhere and opens nothing."""
import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import dock_icon, server
from test_focus_restore import _fake_launch, _launch

_REAL_WARM_UP = dock_icon._warm_up


def _make_chrome(folder: Path, version: str = "157.0.1.2") -> Path:
    app = folder / "Applications" / "Google Chrome.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    binary = app / "Contents" / "MacOS" / "Google Chrome"
    binary.write_text("chrome " + version)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version,
                       "CFBundleVersion": version.split(".", 2)[2]}, handle)
    return binary


def _tree(app: Path) -> dict:
    return {str(p.relative_to(app)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(app.rglob("*"))}


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A fake macOS: copying, icon and signature are stand-ins that record their calls."""
    calls = SimpleNamespace(clone=0, verify=0, icon=0, running=False, warmed=[])

    def clone(source, destination):
        calls.clone += 1
        shutil.copytree(source, destination)

    def set_icon(app, _icon):
        calls.icon += 1
        (app / dock_icon.ICON_FILE).write_bytes(b"icns")

    def verify(_app):
        calls.verify += 1

    monkeypatch.setattr(dock_icon.sys, "platform", "darwin")
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "1")  # it is opt-in
    monkeypatch.setattr(dock_icon, "WITHDRAWN", False)  # the build code itself still works
    monkeypatch.setattr(dock_icon, "_failed", {})
    monkeypatch.setattr(dock_icon, "_builder", None)
    monkeypatch.setattr(dock_icon, "_last_note", "")
    monkeypatch.setattr(dock_icon, "_require_clone_support", lambda *_a: None)
    monkeypatch.setattr(dock_icon, "_clone", clone)
    monkeypatch.setattr(dock_icon, "_set_icon", set_icon)
    monkeypatch.setattr(dock_icon, "_verify_signature", verify)
    monkeypatch.setattr(dock_icon, "_warm_up", lambda binary: calls.warmed.append(
        (binary, calls.verify, (tmp_path / "home" / "app" / dock_icon.APP_NAME).exists())))
    calls.steps = []
    monkeypatch.setattr(dock_icon, "_exclude_from_backups", lambda app: calls.steps.append(
        ("exclude", app.parent.name.startswith(".building-"), calls.verify)))
    monkeypatch.setattr(dock_icon, "_unregister", lambda app: calls.steps.append(
        ("unregister", app.parent.name.startswith(".building-"), len(calls.warmed))))
    monkeypatch.setattr(dock_icon, "_resource_fork_size",
                        lambda path: path.stat().st_size if path.exists() else 0)
    monkeypatch.setattr(dock_icon, "_running_from", lambda _app: calls.running)
    calls.binary = _make_chrome(tmp_path)
    calls.root = tmp_path / "home" / "app"
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(calls.root))  # the configured app folder
    return calls


def _settled(binary, **kw):
    """What a launch gets once any build it started has finished: the tests below are about
    what the copy ends up as. The ones about not waiting call `dock_icon.prepare` themselves."""
    choice = dock_icon.prepare(str(binary), **kw)
    if choice.pending:
        assert dock_icon.wait(30), "the build thread did not finish"
        choice = dock_icon.prepare(str(binary), **kw)
    return choice


def _prepare(mac, **kw):
    return _settled(mac.binary, headless=kw.pop("headless", False), root=mac.root, **kw)


def test_a_headed_launch_runs_from_a_copy_with_the_icon(mac):
    source = mac.binary.parents[2]
    before = _tree(source)
    choice = _prepare(mac)
    copy = mac.root / dock_icon.APP_NAME
    assert choice == dock_icon.DockChrome(executable=str(copy / "Contents" / "MacOS" / "Google Chrome"))
    assert Path(choice.executable).read_text() == "chrome 157.0.1.2"
    assert (copy / dock_icon.ICON_FILE).exists() and mac.verify == 1
    assert _tree(source) == before, "the installed Chrome must only be read"
    assert not list(mac.root.glob(".building-*"))


def test_the_copy_is_reused_while_chrome_stays_the_same(mac):
    first = _prepare(mac)
    second = _prepare(mac)
    assert first == second and first.executable
    assert (mac.clone, mac.icon, mac.verify) == (1, 1, 1)


def test_headless_gets_chrome_itself_and_builds_nothing(mac):
    assert _prepare(mac, headless=True) == dock_icon.DockChrome()
    assert mac.clone == 0 and not mac.root.exists()


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", " No ", "2", "enabled"])
def test_it_is_off_unless_asked_for(mac, monkeypatch, value):
    """Opt-in: a launch from Chrome itself with nothing said, no build, no thread, no folder."""
    if value is None:
        monkeypatch.delenv(dock_icon.ENV_SWITCH)
    else:
        monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert dock_icon.prepare(str(mac.binary), headless=False, root=mac.root) == dock_icon.DockChrome()
    assert dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root) is False
    assert dock_icon.status(str(mac.binary), root=mac.root)["state"] == "off"
    assert mac.clone == 0 and not dock_icon.building() and not mac.root.exists()


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_the_env_switch_turns_it_on(mac, monkeypatch, value):
    monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert _prepare(mac).executable


def test_other_systems_have_no_dock_and_report_nothing(mac, monkeypatch):
    monkeypatch.setattr(dock_icon.sys, "platform", "linux")
    assert _prepare(mac) == dock_icon.DockChrome()
    assert mac.clone == 0


def test_the_app_folder_can_be_moved_with_the_env_var(mac, monkeypatch, tmp_path):
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(tmp_path / "elsewhere"))
    choice = _settled(mac.binary, headless=False)
    assert choice.executable.startswith(str(tmp_path / "elsewhere" / dock_icon.APP_NAME))


def test_a_chrome_update_rebuilds_the_copy(mac, tmp_path):
    _prepare(mac)
    _make_chrome(tmp_path, "158.0.9.9")
    choice = _prepare(mac)
    assert mac.clone == 2 and mac.verify == 2
    assert Path(choice.executable).read_text() == "chrome 158.0.9.9"
    assert not list(mac.root.glob(".building-*")), "the old copy is removed, not kept"


def test_a_copy_that_lost_its_icon_is_rebuilt(mac):
    _prepare(mac)
    (mac.root / dock_icon.APP_NAME / dock_icon.ICON_FILE).unlink()
    assert _prepare(mac).executable and mac.clone == 2


def test_a_copy_whose_build_never_finished_is_rebuilt(mac):
    """The marker is written last, after the signature check. Without it nothing says the
    copy at that path was ever verified."""
    _prepare(mac)
    (mac.root / dock_icon.MARKER_NAME).unlink()
    assert _prepare(mac).executable and mac.clone == 2


def test_a_new_icon_rebuilds_the_copy(mac, monkeypatch):
    _prepare(mac)
    monkeypatch.setattr(dock_icon, "_icon_digest", lambda: "another")
    assert _prepare(mac).executable and mac.clone == 2
    assert json.loads((mac.root / dock_icon.MARKER_NAME).read_text())["icon"] == "another"


def test_a_copy_that_fails_the_signature_check_is_never_used(mac, monkeypatch):
    def refuse(_app):
        mac.verify += 1
        raise dock_icon._Unusable("the copy's signature did not verify: invalid signature")

    monkeypatch.setattr(dock_icon, "_verify_signature", refuse)
    choice = _prepare(mac)
    assert choice.executable is None and "signature did not verify" in choice.note
    assert not (mac.root / dock_icon.APP_NAME).exists()
    assert not (mac.root / dock_icon.MARKER_NAME).exists()
    assert not list(mac.root.glob(".building-*"))


def test_a_failed_build_is_not_repeated_on_every_launch(mac, monkeypatch):
    def refuse(_app):
        raise dock_icon._Unusable("the copy's signature did not verify: unsigned")

    monkeypatch.setattr(dock_icon, "_verify_signature", refuse)
    first = _prepare(mac)
    second = _prepare(mac)
    assert first == second and first.note and mac.clone == 1


def test_a_failed_refresh_leaves_the_old_copy_unused(mac, monkeypatch, tmp_path):
    """After an update whose copy fails verification, the stale copy must not be handed out."""
    _prepare(mac)
    _make_chrome(tmp_path, "158.0.9.9")

    def refuse(_app):
        raise dock_icon._Unusable("the copy's signature did not verify: broken")

    monkeypatch.setattr(dock_icon, "_verify_signature", refuse)
    assert _prepare(mac).executable is None


@pytest.mark.parametrize("step, expected", [
    ("_require_clone_support", "same APFS volume"),
    ("_clone", "cp -c failed"),
    ("_set_icon", "icon could not be set"),
])
def test_each_failing_step_falls_back_with_its_reason(mac, monkeypatch, step, expected):
    def fail(*_a):
        raise dock_icon._Unusable(f"{expected} (test)")

    monkeypatch.setattr(dock_icon, step, fail)
    choice = _prepare(mac)
    assert choice.executable is None and expected in choice.note
    assert not (mac.root / dock_icon.APP_NAME).exists()


def test_an_icon_that_did_not_take_is_a_failure(mac, monkeypatch):
    monkeypatch.setattr(dock_icon, "_set_icon", lambda *_a: None)  # says done, wrote nothing
    choice = _prepare(mac)
    assert choice.executable is None and "did not take" in choice.note
    assert mac.verify == 0 and not (mac.root / dock_icon.APP_NAME).exists()


def test_an_unexpected_error_is_a_note_and_not_an_exception(mac, monkeypatch):
    tries = []

    def boom(*_a):
        tries.append(1)
        raise PermissionError("denied")

    monkeypatch.setattr(dock_icon, "_clone", boom)
    choice = _prepare(mac)
    assert choice.executable is None and "PermissionError" in choice.note
    assert _prepare(mac) == choice and len(tries) == 1  # remembered, so not tried on every launch


def test_an_error_outside_the_build_is_a_note_too(mac, monkeypatch):
    def boom(*_a):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(dock_icon, "_stale_reason", boom)
    choice = _prepare(mac)
    assert choice.executable is None and "disk went away" in choice.note


def test_a_stale_copy_is_not_replaced_under_a_running_browser(mac, tmp_path):
    _prepare(mac)
    _make_chrome(tmp_path, "158.0.9.9")
    mac.running = True
    choice = _prepare(mac)
    assert choice.executable is None and "still running from it" in choice.note
    old = mac.root / dock_icon.APP_NAME / "Contents" / "MacOS" / "Google Chrome"
    assert old.read_text() == "chrome 157.0.1.2" and mac.clone == 1
    mac.running = False  # and it is not remembered as a failure
    assert Path(_prepare(mac).executable).read_text() == "chrome 158.0.9.9"


def test_a_fresh_copy_is_used_even_while_a_browser_runs_from_it(mac):
    _prepare(mac)
    mac.running = True
    assert _prepare(mac).executable and mac.clone == 1


@pytest.mark.parametrize("place", ["inside", "is_the_copy"])
def test_the_copy_never_lands_in_chrome_and_is_never_its_own_source(mac, place):
    source = mac.binary.parents[2]
    before = _tree(source)
    if place == "inside":
        choice = _settled(mac.binary, headless=False, root=source / "Contents" / "x")
    else:
        choice = _settled(mac.binary, headless=False, root=source.parent)
    assert choice.executable is None and "overlap" in choice.note
    assert _tree(source) == before and mac.clone == 0


def test_a_binary_outside_an_app_bundle_falls_back(mac, tmp_path):
    loose = tmp_path / "chromium"
    loose.write_text("x")
    choice = _settled(loose, headless=False, root=mac.root)
    assert choice.executable is None and "app bundle" in choice.note


def test_the_real_clone_probe_refuses_a_missing_source(tmp_path):
    """The probe is the one call that cannot fall back to a full copy, so its failure must
    surface. Runs the real function; off macOS there is no clonefile at all."""
    with pytest.raises(dock_icon._Unusable):
        dock_icon._require_clone_support(tmp_path / "missing", tmp_path / "probe")


def test_the_icon_ships_with_the_package():
    data = dock_icon.ICON_PATH.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 10_000


# ── the server side ──────────────────────────────────────────────────────────

def _config(path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
    return SimpleNamespace(browser_executable_path=path)


def test_the_server_points_the_launch_at_the_copy(monkeypatch):
    seen = {}

    def prepare(binary, *, headless):
        seen.update(binary=binary, headless=headless)
        return dock_icon.DockChrome(executable="/copy/Contents/MacOS/Google Chrome")

    monkeypatch.setattr(dock_icon, "prepare", prepare)
    config = _config()
    assert asyncio.run(server._use_dock_icon_copy(config, False)) == ""
    assert config.browser_executable_path == "/copy/Contents/MacOS/Google Chrome"
    assert seen == {"binary": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "headless": False}


def test_the_server_keeps_chrome_and_says_why_on_a_fallback(monkeypatch):
    monkeypatch.setattr(dock_icon, "prepare",
                        lambda *_a, **_kw: dock_icon.DockChrome(note="cp -c failed: no space"))
    config = _config()
    note = asyncio.run(server._use_dock_icon_copy(config, False))
    assert note == " [Dock icon: Chrome's own, because cp -c failed: no space]"
    assert config.browser_executable_path.startswith("/Applications/Google Chrome.app")


def test_the_server_survives_prepare_raising(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("thread died")

    monkeypatch.setattr(dock_icon, "prepare", boom)
    config = _config()
    assert "thread died" in asyncio.run(server._use_dock_icon_copy(config, False))
    assert config.browser_executable_path.startswith("/Applications/")


def _launch_with(monkeypatch, choice):
    order = _fake_launch(monkeypatch)
    started = {}
    original = server._start_chrome

    async def start(config):
        started["path"] = str(config.browser_executable_path)
        return await original(config)

    monkeypatch.setattr(server, "_start_chrome", start)

    def prepare(binary, *, headless):
        order.append("prepare")
        started.update(headless=headless, source=binary)
        return dock_icon.DockChrome() if headless else choice

    monkeypatch.setattr(dock_icon, "prepare", prepare)
    return order, started


def test_browser_launch_starts_the_copy_before_reading_the_frontmost_app(monkeypatch):
    order, started = _launch_with(monkeypatch, dock_icon.DockChrome(executable="/copy/Google Chrome"))
    out = _launch(headless=False)
    assert started["path"] == "/copy/Google Chrome" and started["headless"] is False
    assert "Dock icon" not in out, out
    # Looking at the copy can take a moment (it lists processes). Focus goes back to whatever
    # was frontmost when Chrome started, so that is read last.
    assert order.index("prepare") < order.index("read") < order.index("start")


def test_browser_launch_reports_a_fallback_and_still_launches(monkeypatch):
    _order, started = _launch_with(monkeypatch, dock_icon.DockChrome(note="AppKit is not installed"))
    out = _launch(headless=False)
    assert "launched" in out and not out.startswith("Error"), out
    assert "[Dock icon: Chrome's own, because AppKit is not installed]" in out
    assert started["path"] == started["source"]


def test_headless_browser_launch_is_not_given_the_copy(monkeypatch):
    _order, started = _launch_with(monkeypatch, dock_icon.DockChrome(executable="/copy/Google Chrome"))
    out = _launch(headless=True)
    assert started["headless"] is True and started["path"] != "/copy/Google Chrome"
    assert "Dock icon" not in out


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


def test_a_new_copy_is_run_once_before_it_is_moved_into_place(mac):
    """The first start of a fresh clone takes macOS about 11 seconds. Paid inside the build,
    the launch that follows starts in under a second instead of at the edge of its time limit."""
    _prepare(mac)
    _prepare(mac)  # reused: nothing to warm
    assert len(mac.warmed) == 1
    binary, verified_first, already_in_place = mac.warmed[0]
    assert binary.name == "Google Chrome" and binary.parent.name == "MacOS"
    assert binary.parents[3].name.startswith(".building-"), binary
    assert verified_first == 1, "an unverified copy must not be run"
    assert not already_in_place


def test_a_copy_that_fails_verification_is_never_run(mac, monkeypatch):
    def refuse(_app):
        raise dock_icon._Unusable("the copy's signature did not verify: broken")

    monkeypatch.setattr(dock_icon, "_verify_signature", refuse)
    assert _prepare(mac).executable is None and mac.warmed == []


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("chrome", 60), OSError("exec format error"), RuntimeError("anything")])
def test_the_warm_up_swallows_its_own_failure_and_has_a_time_limit(monkeypatch, failure):
    seen = {}

    def run(command, **kw):
        seen.update(command=command, **kw)
        raise failure

    monkeypatch.setattr(dock_icon.subprocess, "run", run)
    dock_icon._warm_up(Path("/copy/Contents/MacOS/Google Chrome"))
    assert seen["command"] == ["/copy/Contents/MacOS/Google Chrome", "--version"]
    assert 0 < seen["timeout"] <= 120, "a hung first start must not hang the launch"


def test_a_build_survives_its_warm_up_raising(mac, monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("cannot execute")

    real = dock_icon.subprocess.run
    monkeypatch.setattr(dock_icon, "_warm_up", _REAL_WARM_UP)
    monkeypatch.setattr(dock_icon.subprocess, "run", boom)
    choice = _prepare(mac)
    monkeypatch.setattr(dock_icon.subprocess, "run", real)
    assert choice.executable and choice.note == ""


# ── the copy is built in the background and a launch never waits for it ──────

import fcntl
import threading
import time


def _slow_clone(mac, monkeypatch, seconds=1.5):
    """A build that takes a while, and can be let go early."""
    release = threading.Event()

    def clone(source, destination):
        mac.clone += 1
        release.wait(seconds)
        shutil.copytree(source, destination)

    monkeypatch.setattr(dock_icon, "_clone", clone)
    return release


def _now(mac, **kw):
    started = time.monotonic()
    choice = dock_icon.prepare(str(mac.binary), headless=False, root=mac.root, **kw)
    return choice, time.monotonic() - started


def test_a_launch_does_not_wait_for_a_build(mac, monkeypatch):
    release = _slow_clone(mac, monkeypatch, seconds=5)
    first, took_first = _now(mac)       # starts the build
    second, took_second = _now(mac)     # arrives while it runs
    try:
        for choice, took in ((first, took_first), (second, took_second)):
            assert took < 0.5, f"a launch waited {took:.2f}s for the copy"
            assert choice == dock_icon.DockChrome(note=dock_icon.PENDING_NOTE, pending=True)
        assert dock_icon.building()
    finally:
        release.set()
    assert dock_icon.wait(30)
    ready, took = _now(mac)
    assert ready.executable and not ready.pending and took < 0.5


def test_there_is_one_build_at_a_time_in_a_process(mac, monkeypatch):
    release = _slow_clone(mac, monkeypatch, seconds=5)
    try:
        for _ in range(5):
            assert _now(mac)[0].pending
        assert dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root) is False
        threads = [t for t in threading.enumerate() if t.name == "dock-icon-copy"]
        assert len(threads) == 1
    finally:
        release.set()
    assert dock_icon.wait(30)
    assert mac.clone == 1


def test_a_stale_copy_means_chrome_itself_this_time_and_a_rebuild(mac, monkeypatch, tmp_path):
    """Chrome updated while the server ran: the launch that notices does not wait."""
    assert _prepare(mac).executable
    _make_chrome(tmp_path, "158.0.9.9")
    release = _slow_clone(mac, monkeypatch, seconds=5)
    try:
        choice, took = _now(mac)
        assert choice.pending and choice.executable is None and took < 0.5
        assert dock_icon.building()
    finally:
        release.set()
    assert dock_icon.wait(30)
    assert Path(_now(mac)[0].executable).read_text() == "chrome 158.0.9.9"


def test_a_stale_copy_in_use_starts_no_build(mac, tmp_path):
    assert _prepare(mac).executable
    _make_chrome(tmp_path, "158.0.9.9")
    mac.running = True
    choice, _took = _now(mac)
    assert not choice.pending and "still running from it" in choice.note
    assert not dock_icon.building() and mac.clone == 1


def test_the_build_thread_also_leaves_a_copy_in_use_alone(mac, tmp_path):
    """prepare checks before starting a build; the builder checks again under the lock, because
    a browser can start from the copy in between."""
    assert _prepare(mac).executable
    _make_chrome(tmp_path, "158.0.9.9")
    mac.running = True
    plan = dock_icon._plan(mac.binary, mac.root)
    with pytest.raises(dock_icon._Unusable, match="still running from it"):
        dock_icon._ensure_copy(plan)
    assert mac.clone == 1 and plan.key not in dock_icon._failed


def test_another_server_building_means_chrome_itself_and_no_second_build(mac):
    mac.root.mkdir(parents=True)
    with open(mac.root / ".lock", "a+") as other:  # a builder in another process
        fcntl.flock(other, fcntl.LOCK_EX)
        choice, took = _now(mac)
        assert choice == dock_icon.DockChrome(note=dock_icon.PENDING_NOTE, pending=True)
        assert took < 0.5 and not dock_icon.building() and mac.clone == 0
        assert dock_icon.status(str(mac.binary), root=mac.root) == {
            "state": "building", "detail": "another server is preparing the copy"}
    assert _prepare(mac).executable  # the lock is free again


def test_starting_the_server_builds_a_missing_copy(mac):
    assert dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root) is True
    assert dock_icon.wait(30)
    choice, took = _now(mac)
    assert choice.executable and took < 0.5 and mac.clone == 1


def test_starting_the_server_leaves_a_good_copy_alone(mac):
    assert _prepare(mac).executable
    assert dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root) is True
    assert dock_icon.wait(30)
    assert (mac.clone, mac.verify, len(mac.warmed)) == (1, 1, 1)


@pytest.mark.parametrize("why", ["off", "linux"])
def test_starting_the_server_does_nothing_when_it_does_not_apply(mac, monkeypatch, why):
    if why == "off":
        monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    else:
        monkeypatch.setattr(dock_icon.sys, "platform", "linux")
    asked = []
    assert dock_icon.start_in_background(lambda: asked.append(1) or str(mac.binary), root=mac.root) is False
    assert not dock_icon.building() and asked == [] and not mac.root.exists()


def test_starting_the_server_returns_before_chrome_is_even_looked_for(mac):
    release = threading.Event()

    def find():
        release.wait(5)
        return str(mac.binary)

    started = time.monotonic()
    try:
        assert dock_icon.start_in_background(find, root=mac.root) is True
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
    assert dock_icon.wait(30)


@pytest.mark.parametrize("find, expected", [
    (lambda: None, "Chrome was not found"),
    (lambda: 1 / 0, "ZeroDivisionError"),
    (lambda: (_ for _ in ()).throw(SystemExit(3)), "SystemExit"),
])
def test_a_background_failure_stays_in_its_thread_and_is_kept(mac, find, expected):
    problems = []
    previous = threading.excepthook
    threading.excepthook = lambda args: problems.append(args)
    try:
        assert dock_icon.start_in_background(find, root=mac.root) is True
        assert dock_icon.wait(30)
    finally:
        threading.excepthook = previous
    assert problems == [] and expected in dock_icon._last_note
    choice = _prepare(mac)  # and the next launch still gets its copy
    assert choice.executable and dock_icon._last_note == ""


def test_a_failed_background_build_is_what_the_next_launch_reports(mac, monkeypatch):
    def refuse(_app):
        raise dock_icon._Unusable("the copy's signature did not verify: broken")

    monkeypatch.setattr(dock_icon, "_verify_signature", refuse)
    dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root)
    assert dock_icon.wait(30)
    choice, took = _now(mac)
    assert choice == dock_icon.DockChrome(note="the copy's signature did not verify: broken")
    assert took < 0.5 and not dock_icon.building()
    assert dock_icon.status(str(mac.binary), root=mac.root) == {
        "state": "unavailable", "detail": "the copy's signature did not verify: broken"}


# ── server_status ────────────────────────────────────────────────────────────

def test_status_reads_without_building_or_creating_anything(mac):
    report = dock_icon.status(str(mac.binary), root=mac.root)
    assert report == {"state": "stale", "detail": "there is no copy yet"}
    assert not mac.root.exists() and mac.clone == 0 and not dock_icon.building()


def test_status_follows_the_copy_through_a_build(mac, monkeypatch, tmp_path):
    release = _slow_clone(mac, monkeypatch, seconds=5)
    try:
        _now(mac)
        started = time.monotonic()
        during = dock_icon.status(str(mac.binary), root=mac.root)
        assert time.monotonic() - started < 0.5, "status waited for the build"
        assert during == {"state": "building", "detail": "this server is preparing the copy"}
    finally:
        release.set()
    assert dock_icon.wait(30)
    ready = dock_icon.status(str(mac.binary), root=mac.root)
    assert ready["state"] == "ready" and "157.0.1.2" in ready["detail"]
    assert str(mac.root / dock_icon.APP_NAME) in ready["detail"]
    _make_chrome(tmp_path, "158.0.9.9")
    stale = dock_icon.status(str(mac.binary), root=mac.root)
    assert stale["state"] == "stale" and "158.0.9.9" in stale["detail"]
    assert mac.clone == 1 and not dock_icon.building(), "status must not start the rebuild"


def test_status_says_off_unavailable_and_why(mac, monkeypatch):
    assert dock_icon.status(None, root=mac.root)["state"] == "unavailable"
    monkeypatch.setattr(dock_icon.sys, "platform", "linux")
    assert dock_icon.status(str(mac.binary), root=mac.root) == {
        "state": "unavailable", "detail": "only on macOS"}
    monkeypatch.delenv(dock_icon.ENV_SWITCH)  # off wins: nobody asked for it, on any system
    off = dock_icon.status(str(mac.binary), root=mac.root)
    assert off["state"] == "off" and "MCP_STEALTH_DOCK_ICON=1" in off["detail"]


def test_status_keeps_the_reason_a_background_build_did_not_finish(mac):
    dock_icon.start_in_background(lambda: None, root=mac.root)
    assert dock_icon.wait(30)
    report = dock_icon.status(str(mac.binary), root=mac.root)
    assert report["state"] == "stale" and "Chrome was not found" in report["detail"]


def test_status_never_raises(mac, monkeypatch):
    def boom(*_a):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(dock_icon, "_stale_reason", boom)
    report = dock_icon.status(str(mac.binary), root=mac.root)
    assert report["state"] == "unavailable" and "disk went away" in report["detail"]


def test_server_status_carries_the_copy_state_without_starting_a_build(monkeypatch):
    started = []
    monkeypatch.setattr(dock_icon, "status", lambda binary, root=None: {"state": "ready", "detail": binary})
    monkeypatch.setattr(dock_icon, "_start", lambda *_a: started.append(1))
    monkeypatch.setattr(server, "_chrome_for_dock_icon", lambda: "/the/chrome")
    out = asyncio.run(server.mcp._tool_manager._tools["server_status"].fn())
    assert json.loads(out)["dock_icon"] == {"state": "ready", "detail": "/the/chrome"}
    assert started == []


def test_server_status_survives_the_copy_check_failing(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("no")

    monkeypatch.setattr(dock_icon, "status", boom)
    out = asyncio.run(server.mcp._tool_manager._tools["server_status"].fn())
    assert json.loads(out)["dock_icon"]["state"] == "unavailable"


def test_the_server_says_this_time_when_the_copy_is_on_its_way(monkeypatch):
    monkeypatch.setattr(dock_icon, "prepare", lambda *_a, **_kw: dock_icon.DockChrome(
        note=dock_icon.PENDING_NOTE, pending=True))
    config = _config()
    note = asyncio.run(server._use_dock_icon_copy(config, False))
    assert note == " [Dock icon: Chrome's own this time, the copy is still being prepared]"
    assert config.browser_executable_path.startswith("/Applications/Google Chrome.app")


# ── disk, backups and LaunchServices ─────────────────────────────────────────

def test_a_build_marks_the_copy_and_takes_its_build_folder_out_of_launch_services(mac):
    _prepare(mac)
    # (step, still in the build folder, what had happened before it)
    assert mac.steps == [("exclude", True, 0), ("unregister", True, 1)], mac.steps


def test_what_a_dead_server_left_behind_is_removed_even_when_the_copy_is_fine(mac):
    """A server killed mid-build leaves its build folder, and that can hold a whole old copy."""
    assert _prepare(mac).executable
    abandoned = mac.root / ".building-999-dead" / "previous" / "Contents"
    abandoned.mkdir(parents=True)
    (abandoned / "big").write_text("x")
    dock_icon.start_in_background(lambda: str(mac.binary), root=mac.root)
    assert dock_icon.wait(30)
    assert not list(mac.root.glob(".building-*"))
    assert mac.clone == 1 and _prepare(mac).executable, "the good copy is kept, not rebuilt"


def test_a_good_copy_with_nothing_to_clean_does_not_touch_the_lock(mac, monkeypatch):
    """Taking the lock at every server start would make a launch in another server read
    "being prepared" for that moment."""
    assert _prepare(mac).executable
    monkeypatch.setattr(dock_icon, "_LOCK_WAIT", 0.3)
    with open(mac.root / ".lock", "a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        plan = dock_icon._plan(mac.binary, mac.root)
        assert dock_icon._ensure_copy(plan) == plan.copy_binary


@pytest.mark.skipif(sys.platform != "darwin", reason="extended attributes as macOS has them")
def test_the_backup_mark_is_the_one_tmutil_writes(tmp_path):
    app = tmp_path / "X.app"
    app.mkdir()
    dock_icon._exclude_from_backups(app)
    raw = subprocess.run(["/usr/bin/xattr", "-px", dock_icon._BACKUP_EXCLUDE, str(app)],
                         capture_output=True, text=True).stdout
    # Read back from a folder `tmutil addexclusion` had marked.
    assert "".join(raw.split()).upper() == (
        "62706C69737430305F1011636F6D2E6170706C652E6261636B757064"
        "08000000000000010100000000000000010000000000000000000000000000001C")


@pytest.mark.parametrize("helper", ["_exclude_from_backups", "_unregister"])
def test_the_extras_never_fail_a_build(monkeypatch, helper):
    def boom(*_a, **_kw):
        raise OSError("no such tool")

    monkeypatch.setattr(dock_icon.subprocess, "run", boom)
    getattr(dock_icon, helper)(Path("/copy/Stealth Chrome.app"))


def _own_copy(tmp_path):
    app = tmp_path / dock_icon.APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (tmp_path / dock_icon.MARKER_NAME).write_text("{}")
    return app, str(app / "Contents" / "MacOS" / "Google Chrome")


def _forget(monkeypatch, executable, running=False):
    gone = []
    monkeypatch.setattr(dock_icon.sys, "platform", "darwin")
    monkeypatch.setattr(dock_icon, "_running_from", lambda _app: running)
    monkeypatch.setattr(dock_icon, "_unregister", gone.append)
    dock_icon.forget_when_idle(executable)
    return gone


def test_the_copy_leaves_launch_services_when_nothing_runs_from_it(monkeypatch, tmp_path):
    app, executable = _own_copy(tmp_path)
    assert _forget(monkeypatch, executable) == [app]


def test_the_copy_stays_registered_while_another_browser_runs_from_it(monkeypatch, tmp_path):
    _app, executable = _own_copy(tmp_path)
    assert _forget(monkeypatch, executable, running=True) == []


def test_only_a_copy_made_here_is_ever_unregistered(monkeypatch, tmp_path):
    """The person's own Chrome, or any app that merely has the name, must never be touched."""
    assert _forget(monkeypatch, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") == []
    app, executable = _own_copy(tmp_path)
    other = tmp_path / "Google Chrome.app" / "Contents" / "MacOS"  # beside the marker, another name
    other.mkdir(parents=True)
    assert _forget(monkeypatch, str(other / "Google Chrome")) == []
    (tmp_path / dock_icon.MARKER_NAME).unlink()  # the name alone is not enough
    assert _forget(monkeypatch, executable) == []
    assert _forget(monkeypatch, None) == [] and _forget(monkeypatch, "chromium") == []


def test_forgetting_never_raises(monkeypatch, tmp_path):
    _app, executable = _own_copy(tmp_path)

    def boom(_app):
        raise RuntimeError("ps died")

    monkeypatch.setattr(dock_icon.sys, "platform", "darwin")
    monkeypatch.setattr(dock_icon, "_running_from", boom)
    dock_icon.forget_when_idle(executable)


def test_a_verified_close_lets_go_of_the_copy_off_the_event_loop(monkeypatch):
    seen = []
    done = threading.Event()

    def forget(executable):
        seen.append((executable, threading.current_thread().name))
        done.set()

    monkeypatch.setattr(dock_icon, "forget_when_idle", forget)
    # A process that is really ended and really reaped: an orphan, so not left as a zombie.
    pid = int(subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                             capture_output=True, text=True).stdout)
    browser = SimpleNamespace(_process_pid=pid, _process=object(), stop=lambda: None,
                              config=SimpleNamespace(user_data_dir=None, browser_executable_path="/copy/chrome"))
    report = asyncio.run(server._ensure_browser_dead(browser, None, grace=0.1))
    assert report["verified"] and report["killed"] == [pid], report
    assert done.wait(5) and seen == [("/copy/chrome", "dock-icon-forget")]


def test_a_close_that_could_not_be_verified_keeps_the_copy_registered(monkeypatch):
    seen = []
    monkeypatch.setattr(dock_icon, "forget_when_idle", seen.append)
    browser = SimpleNamespace(_process_pid=None, _process=object(), stop=lambda: None,
                              config=SimpleNamespace(user_data_dir=None, browser_executable_path="/copy/chrome"))
    report = asyncio.run(server._ensure_browser_dead(browser, None, grace=0.1))
    time.sleep(0.2)
    assert not report["verified"] and seen == []


# ── removing the copy: when the feature is off, and after an update ──────────

def _built(mac):
    assert _prepare(mac).executable
    return mac.root / dock_icon.APP_NAME


def _everything_under(folder: Path) -> list:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def test_a_server_started_with_the_feature_off_removes_the_copy(mac, monkeypatch):
    copy = _built(mac)
    (mac.root / ".building-1-dead").mkdir()
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    assert dock_icon.start_in_background(lambda: str(mac.binary)) is True
    assert dock_icon.wait(30)
    assert not copy.exists() and not mac.root.exists()
    assert ("unregister", False, 1) in mac.steps, "and it is taken out of LaunchServices first"
    source = mac.binary.parents[2]
    assert (source / "Contents" / "MacOS" / "Google Chrome").read_text() == "chrome 157.0.1.2"


def test_the_copy_is_not_removed_while_a_browser_runs_from_it(mac, monkeypatch):
    copy = _built(mac)
    before = _everything_under(mac.root)
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    mac.running = True
    assert dock_icon.discard_copy() == "left alone: a browser is running from the copy"
    assert copy.is_dir() and _everything_under(mac.root) == before


def test_the_copy_is_not_removed_while_another_server_builds(mac):
    copy = _built(mac)
    with open(mac.root / ".lock", "a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        assert dock_icon.discard_copy() == "left alone: a server is building there"
    assert copy.is_dir() and (mac.root / dock_icon.MARKER_NAME).is_file()


def test_removal_takes_only_what_this_module_made(mac):
    """Four names, and nothing else that happens to be in the folder."""
    _built(mac)
    (mac.root / ".building-9-old" / "previous").mkdir(parents=True)
    (mac.root / "notes.txt").write_text("the person's own file")
    (mac.root / "Other.app" / "Contents").mkdir(parents=True)
    (mac.root / "Stealth Chrome.app.bak").mkdir()
    assert dock_icon.discard_copy() == "removed"
    assert _everything_under(mac.root) == ["Other.app", "Other.app/Contents", "Stealth Chrome.app.bak", "notes.txt"]


def test_removal_only_works_in_the_configured_folder(mac, tmp_path):
    _built(mac)
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
    # the bundle itself is a link
    mac.root.mkdir(parents=True)
    (mac.root / ".lock").write_text("")
    (mac.root / dock_icon.APP_NAME).symlink_to(precious, target_is_directory=True)
    (mac.root / ".building-1-x").symlink_to(precious, target_is_directory=True)
    assert dock_icon.discard_copy().startswith("refused")
    assert (precious / "Contents" / "data").read_text() == "keep"
    assert (mac.root / dock_icon.APP_NAME).is_symlink()
    # a build folder is a link, and the copy holds a link pointing out
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
    copy = _built(mac)
    monkeypatch.undo()
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(mac.root))
    monkeypatch.setattr(dock_icon, "_unregister", lambda _app: None)

    def boom(*_a, **_kw):
        raise OSError("no ps")

    monkeypatch.setattr(dock_icon.subprocess, "run", boom)  # the real _running_from, failing
    assert dock_icon.discard_copy() == "left alone: a browser is running from the copy"
    assert copy.is_dir()


def test_removal_errors_stay_in_the_background_thread(mac, monkeypatch):
    _built(mac)
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(dock_icon.shutil, "rmtree", boom)
    problems = []
    previous = threading.excepthook
    threading.excepthook = lambda args: problems.append(args)
    try:
        assert dock_icon.start_in_background(lambda: str(mac.binary)) is True
        assert dock_icon.wait(30)
    finally:
        threading.excepthook = previous
    assert problems == [] and "PermissionError" in dock_icon._last_note


def _close_last_browser(mac):
    executable = str(mac.root / dock_icon.APP_NAME / "Contents" / "MacOS" / "Google Chrome")
    dock_icon.forget_when_idle(executable)
    assert dock_icon.wait(30)


def test_the_old_copy_goes_as_soon_as_its_last_browser_closes_after_an_update(mac, tmp_path):
    """Chrome updated while a browser ran from the copy, so it could not be replaced then. It
    now shares no blocks with anything: rebuilt at that close, not at some later launch."""
    _built(mac)
    _make_chrome(tmp_path, "158.0.9.9")
    mac.running = True
    assert "still running from it" in dock_icon.prepare(str(mac.binary), headless=False, root=mac.root).note
    assert mac.clone == 1
    mac.running = False
    _close_last_browser(mac)
    assert mac.clone == 2 and not list(mac.root.glob(".building-*"))
    copy = mac.root / dock_icon.APP_NAME / "Contents" / "MacOS" / "Google Chrome"
    assert copy.read_text() == "chrome 158.0.9.9"


def test_closing_a_browser_leaves_a_current_copy_alone(mac, monkeypatch):
    """Not even a thread: while one runs, a launch reads the copy as being prepared."""
    _built(mac)
    started = []
    monkeypatch.setattr(dock_icon, "_start", started.append)
    _close_last_browser(mac)
    assert started == [] and (mac.clone, mac.verify) == (1, 1)


def test_closing_a_browser_does_nothing_while_another_still_runs_from_the_copy(mac, tmp_path):
    _built(mac)
    _make_chrome(tmp_path, "158.0.9.9")
    mac.running = True
    _close_last_browser(mac)
    assert mac.clone == 1 and not dock_icon.building()


def test_closing_the_last_browser_with_the_feature_off_removes_the_copy(mac, monkeypatch):
    copy = _built(mac)
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")
    _close_last_browser(mac)
    assert not copy.exists()


# ── what the copy really takes on disk ───────────────────────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="APFS clones")
def test_disk_use_tells_shared_blocks_from_its_own(tmp_path):
    """`du` counts a clone in full. What matters is what removing it would give back."""
    app = tmp_path / "X.app"
    app.mkdir()
    original = tmp_path / "original.bin"
    original.write_bytes(os.urandom(4_000_000))
    done = subprocess.run(["/bin/cp", "-c", str(original), str(app / "cloned.bin")], capture_output=True)
    if done.returncode != 0:
        pytest.skip("this volume cannot clone")
    os.sync()
    shared = dock_icon._disk_use(app)
    assert shared["own_mb"] < 0.5 and 3.5 < shared["shown_mb"] < 4.5, shared
    (app / "own.bin").write_bytes(os.urandom(2_000_000))
    os.sync()
    mixed = dock_icon._disk_use(app)
    assert 1.5 < mixed["own_mb"] < 2.5 and 5.5 < mixed["shown_mb"] < 6.5, mixed


def test_disk_use_is_nothing_without_a_copy_and_never_follows_a_link(tmp_path):
    assert dock_icon._disk_use(tmp_path / "missing.app") is None
    (tmp_path / "real").mkdir()
    (tmp_path / "link.app").symlink_to(tmp_path / "real", target_is_directory=True)
    assert dock_icon._disk_use(tmp_path / "link.app") is None


def test_status_adds_the_disk_use_and_survives_it_failing(mac, monkeypatch):
    _built(mac)
    monkeypatch.setattr(dock_icon, "_disk_use", lambda app: {"own_mb": 0.0, "shown_mb": 1535.3, "of": app.name})
    report = dock_icon.status(str(mac.binary), root=mac.root)
    assert report["state"] == "ready"
    assert report["disk"] == {"own_mb": 0.0, "shown_mb": 1535.3, "of": dock_icon.APP_NAME}
    monkeypatch.setenv(dock_icon.ENV_SWITCH, "0")  # a copy still on disk shows up when off too
    assert dock_icon.status(str(mac.binary), root=mac.root)["disk"]["shown_mb"] == 1535.3

    def boom(_app):
        raise OSError("no getattrlist")

    monkeypatch.setattr(dock_icon, "_disk_use", boom)
    assert "disk" not in dock_icon.status(str(mac.binary), root=mac.root)
    monkeypatch.setattr(dock_icon, "_disk_use", lambda _app: None)
    assert "disk" not in dock_icon.status(str(mac.binary), root=mac.root)


def test_withdrawn_ignores_the_switch_and_removes_an_old_copy(mac, monkeypatch):
    """0.13.1: the copy got Google's updater to update it instead of the installed Chrome. With
    the switch on, a launch runs Chrome itself, nothing is built, and a copy from before goes."""
    assert _prepare(mac).executable  # a copy built while it was allowed
    assert dock_icon.wait(30)
    monkeypatch.setattr(dock_icon, "WITHDRAWN", True)
    clones = mac.clone
    assert dock_icon.prepare(str(mac.binary), headless=False, root=mac.root) == dock_icon.DockChrome()
    assert dock_icon.status(str(mac.binary), root=mac.root)["state"] == "withdrawn"
    assert dock_icon.discard_copy(mac.root) == "removed"
    assert mac.clone == clones and not mac.root.exists()
