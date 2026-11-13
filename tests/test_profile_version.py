"""A profile last opened by a newer Chrome is refused to an older one, never opened by it.

Fakes only: profiles and Chrome bundles are built in temp folders, and no Chrome starts."""
import asyncio
import hashlib
import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import dock_icon, profile_version, server, state
from test_focus_restore import _fake_launch

pv = profile_version


def _chrome(folder: Path, version: str, name: str = "Google Chrome") -> str:
    app = folder / f"{name}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    binary = app / "Contents" / "MacOS" / name
    binary.write_text(name)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    return str(binary)


def _profile(folder: Path, last_version=None, stats_version=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if last_version is not None:
        (folder / "Last Version").write_text(last_version)
    if stats_version is not None:
        (folder / "Local State").write_text(json.dumps(
            {"user_experience_metrics": {"stability": {"stats_version": stats_version}}}))
    (folder / "Default").mkdir(exist_ok=True)
    (folder / "Default" / "Preferences").write_text('{"browser": {"window_placement": {"left": 5}}}')
    return folder


def _digest(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_last_version_is_read_from_both_places_and_the_newer_wins(tmp_path):
    assert pv.last_opened_by(_profile(tmp_path / "a", "158.0.8127.18")) == (158, 0, 8127, 18)
    assert pv.last_opened_by(_profile(tmp_path / "b", None, "158.0.8127.18-64")) == (158, 0, 8127, 18)
    both = _profile(tmp_path / "c", "157.0.8103.39", "158.0.8127.18-64")
    assert pv.last_opened_by(both) == (158, 0, 8127, 18)
    assert pv.last_opened_by(_profile(tmp_path / "d")) is None
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "Last Version").write_text("garbage")
    (tmp_path / "e" / "Local State").write_text("{not json")
    assert pv.last_opened_by(tmp_path / "e") is None


def test_versions_compare_as_numbers_not_text(tmp_path):
    profile = _profile(tmp_path / "p", "158.0.8127.9")
    assert pv.refusal(profile, _chrome(tmp_path / "x", "158.0.8127.10")) == ""
    assert pv.refusal(profile, _chrome(tmp_path / "y", "99.0.9999.99"))


def test_the_bundle_version_is_read_without_starting_chrome(tmp_path):
    binary = _chrome(tmp_path, "157.0.8103.39")
    assert pv.binary_version(binary) == (157, 0, 8103, 39)
    assert pv.binary_version(str(tmp_path / "missing")) is None
    assert pv.binary_version(None) is None


def test_a_chrome_update_is_seen_by_the_cached_version(tmp_path):
    binary = _chrome(tmp_path, "157.0.8103.39")
    assert pv.binary_version(binary) == (157, 0, 8103, 39)
    _chrome(tmp_path, "158.0.1.2")
    Path(binary).touch()
    import os
    os.utime(binary, ns=(10**18, 10**18))
    assert pv.binary_version(binary) == (158, 0, 1, 2)


def test_an_older_chrome_is_refused_with_what_to_do(tmp_path):
    profile = _profile(tmp_path / "p", "158.0.8127.18")
    message = pv.refusal(profile, _chrome(tmp_path / "c", "157.0.8103.39"))
    assert message.startswith(f"refused to open {profile}")
    assert "158.0.8127.18" in message and "157.0.8103.39" in message
    assert "Nothing was started and the profile was not changed" in message
    assert "MCP_STEALTH_DOCK_ICON=1" in message and "persistent=False" in message


@pytest.mark.parametrize("profile_version_text, chrome", [
    ("157.0.8103.39", "157.0.8103.39"),  # the same
    ("157.0.8103.39", "158.0.8127.18"),  # newer opens older: Chrome's normal update
    (None, "100.0.0.0"),                 # nothing recorded
])
def test_the_same_or_a_newer_chrome_is_let_through(tmp_path, profile_version_text, chrome):
    profile = _profile(tmp_path / "p", profile_version_text)
    assert pv.refusal(profile, _chrome(tmp_path / "c", chrome)) == ""


def test_nothing_to_compare_is_not_a_refusal(tmp_path):
    profile = _profile(tmp_path / "p", "158.0.8127.18")
    assert pv.refusal(None, _chrome(tmp_path / "c", "1.0.0.0")) == ""
    assert pv.refusal(tmp_path / "absent", _chrome(tmp_path / "c", "1.0.0.0")) == ""
    assert pv.refusal(profile, str(tmp_path / "no-chrome")) == ""


def test_the_upgrade_is_announced_once_per_major_version(tmp_path):
    profile = _profile(tmp_path / "p", "157.0.8103.39")
    beta = _chrome(tmp_path / "b", "158.0.8127.18", "Google Chrome Beta")
    note = pv.upgrade_note(profile, beta)
    assert "last opened by Chrome 157.0.8103.39" in note and "upgrades it to 158.0.8127.18" in note
    assert pv.upgrade_note(_profile(tmp_path / "q", "158.0.8127.1"), beta) == ""
    assert pv.upgrade_note(_profile(tmp_path / "r"), beta) == ""
    assert pv.upgrade_note(None, beta) == ""


# ── through the launchers ────────────────────────────────────────────────────

@pytest.fixture
def refused_launch(monkeypatch, tmp_path):
    """A profile last opened by Chrome 158, and Google Chrome 157 as the browser to launch.
    Starting Chrome is recorded, so a test can tell that nothing started."""
    order = _fake_launch(monkeypatch)
    profile = _profile(tmp_path / "profile", "158.0.8127.18", "158.0.8127.18-64")
    chrome = _chrome(tmp_path / "apps", "157.0.8103.39")
    monkeypatch.setattr(server, "_default_chrome", lambda: chrome)
    # These are about Google Chrome; with MCP_TEST_DOCK_ICON=1 a headed launch on a profile
    # Beta wrote would rightly go to the real Beta instead.
    monkeypatch.setattr(dock_icon, "chosen", lambda *, headless: None)
    monkeypatch.setattr(server.Config, "browser_executable_path", chrome, raising=False)
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", state._PROFILE_AFFINITY)
    original_init = server.Config.__init__

    def init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.browser_executable_path = chrome

    monkeypatch.setattr(server.Config, "__init__", init)
    return order, profile


def _browser_launch(**kw):
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    try:
        return asyncio.run(fn(auto_verify=False, **kw))
    finally:
        server.BrowserState.reset()


@pytest.mark.parametrize("headless", [False, True])
def test_browser_launch_refuses_a_newer_profile_given_as_user_data_dir(refused_launch, headless):
    order, profile = refused_launch
    before = _digest(profile)
    out = _browser_launch(user_data_dir=str(profile), headless=headless)
    assert out.startswith("Error: refused to open"), out
    assert "start" not in order
    assert _digest(profile) == before


def test_browser_launch_refuses_the_persistent_profile_before_writing_to_it(refused_launch, monkeypatch):
    """The persistent path wipes window placement in Preferences before launching. A refused
    profile must not get even that."""
    order, profile = refused_launch
    monkeypatch.setattr(server, "_resolve_launch_profile", lambda *_a, **_kw: (profile, {}))
    before = _digest(profile)
    out = _browser_launch(persistent=True, headless=True)
    assert out.startswith("Error: refused to open"), out
    assert "start" not in order
    assert _digest(profile) == before


def test_spawn_browser_refuses_a_newer_profile(refused_launch):
    order, profile = refused_launch
    before = _digest(profile)
    try:
        ok, message = asyncio.run(server._launch_browser_instance(
            instance_id="older", url="about:blank", headless=True, proxy=None, user_agent=None,
            window_width=800, window_height=600, persistent=False, lang="en-US", extra_args=None,
            storage_state_path=None, idle_timeout=0, profile_dir_override=str(profile)))
    finally:
        server.BrowserState.reset()
    assert ok is False and message.startswith("refused to open"), message
    assert "start" not in order
    assert _digest(profile) == before


def test_a_profile_the_launch_may_open_still_launches(refused_launch, tmp_path):
    order, _profile_dir = refused_launch
    fine = _profile(tmp_path / "fine", "157.0.8103.39")
    out = _browser_launch(user_data_dir=str(fine), headless=True)
    assert "launched" in out and not out.startswith("Error"), out
    assert "start" in order


# ── which profiles Chrome Beta may open ──────────────────────────────────────

def _cookies(profile: Path, *prefixes: bytes, sub: str = "Default") -> Path:
    """A cookie database like Chrome's, one row per prefix (b"v10" from Google Chrome 157,
    b"v11" from Chrome Beta 158)."""
    import sqlite3
    folder = profile / sub
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "Cookies")
    db.execute("CREATE TABLE cookies (name TEXT, encrypted_value BLOB)")
    db.executemany("INSERT INTO cookies VALUES (?, ?)",
                   [(f"c{i}", prefix + b"\x00secret") for i, prefix in enumerate(prefixes)])
    db.commit()
    db.close()
    return folder / "Cookies"


@pytest.fixture
def two_chromes(tmp_path):
    return (_chrome(tmp_path / "stable", "157.0.8103.39"),
            _chrome(tmp_path / "beta", "158.0.8127.18", "Google Chrome Beta"))


def _verdict(profile, two_chromes, extra_args=()):
    chrome, beta = two_chromes
    return pv.beta_verdict(profile, chrome=chrome, beta=beta, extra_args=extra_args)


def test_beta_gets_a_profile_nothing_has_written_to(tmp_path, two_chromes):
    assert _verdict(None, two_chromes) == (True, "")
    assert _verdict(tmp_path / "not-yet", two_chromes) == (True, "")
    (tmp_path / "empty").mkdir()
    assert _verdict(tmp_path / "empty", two_chromes) == (True, "")


def test_a_profile_with_chrome_data_stays_on_google_chrome(tmp_path, two_chromes):
    profile = _profile(tmp_path / "p", "157.0.8103.39", "157.0.8103.39-64")
    _cookies(profile, b"v10", b"v10")
    allowed, note = _verdict(profile, two_chromes)
    assert allowed is False
    assert "holds data from Chrome 157.0.8103.39" in note and "delete its cookies" in note


@pytest.mark.parametrize("what", ["ds_store", "only_preferences", "lock_only"])
def test_anything_in_the_folder_is_data(tmp_path, two_chromes, what):
    profile = tmp_path / "p"
    profile.mkdir()
    name = {"ds_store": ".DS_Store", "only_preferences": "Preferences", "lock_only": "SingletonLock"}[what]
    (profile / name).write_text("x")
    allowed, note = _verdict(profile, two_chromes)
    assert allowed is False and "another Chrome" in note


def test_a_profile_only_beta_wrote_stays_on_beta(tmp_path, two_chromes):
    profile = _profile(tmp_path / "p", "158.0.8127.18", "158.0.8127.18-64")
    _cookies(profile, b"v11", b"v11")
    allowed, note = _verdict(profile, two_chromes)
    assert allowed is True and "stays on Chrome Beta" in note and "157.0.8103.39" in note


@pytest.mark.parametrize("case", ["one_v10", "v10_in_network", "v10_in_other_profile",
                                  "unreadable_db", "newer_than_beta", "same_major_as_chrome"])
def test_a_beta_looking_profile_in_any_doubt_stays_on_google_chrome(tmp_path, two_chromes, case):
    last = {"newer_than_beta": "159.0.1.1", "same_major_as_chrome": "157.0.9999.1"}.get(case, "158.0.8127.18")
    profile = _profile(tmp_path / "p", last)
    if case == "one_v10":
        _cookies(profile, b"v11", b"v10")
    elif case == "v10_in_network":
        _cookies(profile, b"v11")
        _cookies(profile, b"v10", sub="Default/Network")
    elif case == "v10_in_other_profile":
        _cookies(profile, b"v11")
        _cookies(profile, b"v10", sub="Profile 1")
    elif case == "unreadable_db":
        (profile / "Default" / "Cookies").write_bytes(b"not a database")
    else:
        _cookies(profile, b"v11")
    assert _verdict(profile, two_chromes)[0] is False, case


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL does not exist on Windows; Beta is chosen on macOS only")
@pytest.mark.parametrize("journal", ["rollback", "wal"])
def test_a_v10_cookie_only_a_journal_still_holds_keeps_google_chrome(tmp_path, two_chromes, journal):
    """Read as the file lies (immutable=1), a database whose committed v10 rows sit behind a
    hot journal or only in its write-ahead log looks empty, and Beta would delete them."""
    import sqlite3
    import subprocess
    profile = _profile(tmp_path / "p", "158.0.8127.18")
    database = profile / "Default" / "Cookies"
    db = sqlite3.connect(database)
    if journal == "wal":
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE cookies (name TEXT, encrypted_value BLOB)")
    if journal == "rollback":
        db.executemany("INSERT INTO cookies VALUES (?, ?)", [(f"c{i}", b"v10" + b"x" * 500) for i in range(40)])
    db.commit()
    db.close()
    work = ("c.execute('BEGIN'); c.execute('DELETE FROM cookies'); c.execute('CREATE TABLE f (x BLOB)'); "
            "[c.execute('INSERT INTO f VALUES (zeroblob(4000))') for _ in range(200)]"
            if journal == "rollback" else "c.execute(\"INSERT INTO cookies VALUES ('a', CAST('v10x' AS BLOB))\")")
    subprocess.run([sys.executable, "-c",
                    f"import os, signal, sqlite3\nc = sqlite3.connect({str(database)!r}, isolation_level=None)\n"
                    f"c.execute('PRAGMA cache_size=1')\n{work}\nos.kill(os.getpid(), signal.SIGKILL)\n"])
    assert any(Path(str(database) + s).is_file() and Path(str(database) + s).stat().st_size
               for s in ("-journal", "-wal"))
    assert _verdict(profile, two_chromes)[0] is False


def test_reading_the_cookie_databases_writes_nothing(tmp_path, two_chromes):
    profile = _profile(tmp_path / "p", "158.0.8127.18")
    _cookies(profile, b"v11")
    before = _digest(profile)
    _verdict(profile, two_chromes)
    assert _digest(profile) == before
    assert sorted(p.name for p in (profile / "Default").iterdir()) == ["Cookies", "Preferences"]


def test_unknown_versions_and_odd_folders_are_doubt(tmp_path, two_chromes):
    chrome, beta = two_chromes
    profile = _profile(tmp_path / "p", "158.0.8127.18")
    _cookies(profile, b"v11")
    assert pv.beta_verdict(profile, chrome=None, beta=beta)[0] is False
    assert pv.beta_verdict(profile, chrome=chrome, beta=None)[0] is False
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    assert _verdict(link, two_chromes)[0] is False, "even a link to nothing yet"
    (tmp_path / "file").write_text("x")
    assert _verdict(tmp_path / "file", two_chromes)[0] is False
    allowed, note = _verdict(tmp_path / "fresh", two_chromes, ["--user-data-dir=/somewhere"])
    assert allowed is False and "--user-data-dir" in note


def test_an_unreadable_folder_is_doubt(tmp_path, two_chromes):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("x")
    locked.chmod(0)
    try:
        assert _verdict(locked, two_chromes)[0] is False
    finally:
        locked.chmod(0o700)


# ── through the launchers, with the Dock icon switch on ──────────────────────

@pytest.fixture
def beta_on(monkeypatch, tmp_path, two_chromes):
    """In-process launches with the switch on: Beta and Google Chrome are stand-ins in a temp
    folder, Chrome never starts, and the privacy-profile folder is a temp one."""
    chrome, beta = two_chromes
    order = _fake_launch(monkeypatch)
    started = {}
    original = server._start_chrome

    async def start(config):
        started["path"] = str(config.browser_executable_path)
        return await original(config)

    def prepare(binary, *, headless, profile_refusal=""):
        if headless:
            return dock_icon.DockChrome()
        if profile_refusal:
            return dock_icon.DockChrome(note=f"Google Chrome, because {profile_refusal}")
        return dock_icon.DockChrome(executable=beta, version="158.0.8127.18")

    monkeypatch.setattr(server, "_start_chrome", start)
    monkeypatch.setattr(server, "_default_chrome", lambda: chrome)
    monkeypatch.setattr(dock_icon, "chosen", lambda *, headless: None if headless else beta)
    monkeypatch.setattr(dock_icon, "prepare", prepare)
    monkeypatch.setattr(server, "PRIVACY_PROFILE_ROOT", tmp_path / "privacy-profiles")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", state._PROFILE_AFFINITY)
    original_init = server.Config.__init__

    def init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.browser_executable_path = chrome

    monkeypatch.setattr(server.Config, "__init__", init)
    return SimpleNamespace(chrome=chrome, beta=beta, started=started, order=order)


def _data_profile(folder: Path) -> Path:
    profile = _profile(folder, "157.0.8103.39", "157.0.8103.39-64")
    _cookies(profile, b"v10", b"v10", b"v10")
    return profile


def test_a_profile_with_chrome_cookies_is_opened_by_google_chrome(beta_on, tmp_path):
    profile = _data_profile(tmp_path / "used")
    out = _browser_launch(user_data_dir=str(profile), headless=False)
    assert beta_on.started["path"] == beta_on.chrome, out
    assert ("[Dock icon: Google Chrome, because this profile already holds data from Chrome "
            "157.0.8103.39, and Chrome Beta would delete its cookies") in out


def test_a_new_profile_folder_is_opened_by_beta(beta_on, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    out = _browser_launch(user_data_dir=str(fresh), headless=False)
    assert beta_on.started["path"] == beta_on.beta, out
    assert "[Dock icon: Chrome Beta 158.0.8127.18]" in out


def test_a_temporary_profile_is_opened_by_beta(beta_on):
    out = _browser_launch(persistent=False, headless=False)
    assert beta_on.started["path"] == beta_on.beta, out


def test_a_profile_beta_wrote_is_opened_by_beta_again_and_says_why(beta_on, tmp_path):
    profile = _profile(tmp_path / "beta-made", "158.0.8127.18", "158.0.8127.18-64")
    _cookies(profile, b"v11")
    out = _browser_launch(user_data_dir=str(profile), headless=False)
    assert beta_on.started["path"] == beta_on.beta, out
    assert "stays on Chrome Beta" in out


def test_headless_on_a_beta_profile_is_refused_not_damaged(beta_on, tmp_path):
    profile = _profile(tmp_path / "beta-made", "158.0.8127.18", "158.0.8127.18-64")
    _cookies(profile, b"v11")
    before = _digest(profile)
    out = _browser_launch(user_data_dir=str(profile), headless=True)
    assert out.startswith("Error: refused to open"), out
    assert "path" not in beta_on.started and _digest(profile) == before


@pytest.mark.parametrize("exists", [True, False])
def test_the_persistent_profile_is_judged_before_anything_writes_to_it(beta_on, tmp_path, monkeypatch, exists):
    """The persistent path cleans locks and wipes window placement before launching, and
    either would make a new profile look used, or a used one look new."""
    target = _data_profile(tmp_path / "main") if exists else tmp_path / "main"
    monkeypatch.setattr(server, "_resolve_launch_profile", lambda *_a, **_kw: (target, {}))
    out = _browser_launch(persistent=True, headless=False)
    assert beta_on.started["path"] == (beta_on.chrome if exists else beta_on.beta), out


@pytest.mark.parametrize("exists", [True, False])
def test_spawn_browser_judges_the_profile_before_making_it(beta_on, tmp_path, exists):
    target = _data_profile(tmp_path / "inst") if exists else tmp_path / "inst"
    try:
        ok, message = asyncio.run(server._launch_browser_instance(
            instance_id="inst", url="about:blank", headless=False, proxy=None, user_agent=None,
            window_width=800, window_height=600, persistent=True, lang="en-US", extra_args=None,
            storage_state_path=None, idle_timeout=0, profile_dir_override=str(target)))
    finally:
        server.BrowserState.reset()
    assert ok, message
    assert beta_on.started["path"] == (beta_on.chrome if exists else beta_on.beta), message
    if exists:
        assert "holds data from Chrome 157.0.8103.39" in message


def test_a_privacy_profile_is_opened_by_beta(beta_on):
    try:
        out = _browser_launch(privacy=True, headless=False)
    finally:
        # A privacy profile outlives reset() on purpose, so it is cleaned up after a crash.
        server.BrowserState.current_privacy = False
        server.BrowserState.current_profile_dir = None
    assert beta_on.started["path"] == beta_on.beta, out


def test_headless_never_gets_beta(beta_on, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    out = _browser_launch(user_data_dir=str(fresh), headless=True)
    assert beta_on.started.get("path") == beta_on.chrome and "Dock icon" not in out, out
