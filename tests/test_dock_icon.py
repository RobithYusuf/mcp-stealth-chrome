"""The Chrome copy that gives a headed stealth browser its own Dock icon. Fakes only: the clone,
the icon and the signature check are stood in for, so this runs anywhere and opens nothing."""
import asyncio
import json
import os
import plistlib
import shutil
import subprocess
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
    monkeypatch.delenv(dock_icon.ENV_SWITCH, raising=False)
    monkeypatch.setattr(dock_icon, "_failed", {})
    monkeypatch.setattr(dock_icon, "_require_clone_support", lambda *_a: None)
    monkeypatch.setattr(dock_icon, "_clone", clone)
    monkeypatch.setattr(dock_icon, "_set_icon", set_icon)
    monkeypatch.setattr(dock_icon, "_verify_signature", verify)
    monkeypatch.setattr(dock_icon, "_warm_up", lambda binary: calls.warmed.append(
        (binary, calls.verify, (tmp_path / "home" / "app" / dock_icon.APP_NAME).exists())))
    monkeypatch.setattr(dock_icon, "_resource_fork_size",
                        lambda path: path.stat().st_size if path.exists() else 0)
    monkeypatch.setattr(dock_icon, "_running_from", lambda _app: calls.running)
    calls.binary = _make_chrome(tmp_path)
    calls.root = tmp_path / "home" / "app"
    return calls


def _prepare(mac, **kw):
    return dock_icon.prepare(str(mac.binary), headless=kw.pop("headless", False), root=mac.root, **kw)


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


@pytest.mark.parametrize("value", ["0", "false", "off", " No "])
def test_the_env_switch_turns_it_off(mac, monkeypatch, value):
    monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert _prepare(mac) == dock_icon.DockChrome()
    assert mac.clone == 0 and not mac.root.exists()


@pytest.mark.parametrize("value", ["1", "true", ""])
def test_any_other_value_leaves_it_on(mac, monkeypatch, value):
    monkeypatch.setenv(dock_icon.ENV_SWITCH, value)
    assert _prepare(mac).executable


def test_other_systems_have_no_dock_and_report_nothing(mac, monkeypatch):
    monkeypatch.setattr(dock_icon.sys, "platform", "linux")
    assert _prepare(mac) == dock_icon.DockChrome()
    assert mac.clone == 0


def test_the_app_folder_can_be_moved_with_the_env_var(mac, monkeypatch, tmp_path):
    monkeypatch.setenv(dock_icon.ENV_APP_DIR, str(tmp_path / "elsewhere"))
    choice = dock_icon.prepare(str(mac.binary), headless=False)
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
        choice = dock_icon.prepare(str(mac.binary), headless=False, root=source / "Contents" / "x")
    else:
        choice = dock_icon.prepare(str(mac.binary), headless=False, root=source.parent)
    assert choice.executable is None and "overlap" in choice.note
    assert _tree(source) == before and mac.clone == 0


def test_a_binary_outside_an_app_bundle_falls_back(mac, tmp_path):
    loose = tmp_path / "chromium"
    loose.write_text("x")
    choice = dock_icon.prepare(str(loose), headless=False, root=mac.root)
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
    # Building the copy takes seconds, in which the person may switch apps. Focus goes back to
    # whatever was frontmost when Chrome started, so that is read after the copy is ready.
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
