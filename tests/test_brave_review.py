"""The review of browser="brave" (F1 to F9): each finding as a test. Fast, no browser starts.

Every profile here is a stand-in built under tmp_path. A launch that gets as far as starting a
browser fails the test (`_start_chrome` is replaced), so "refused" also means "nothing started".
"""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from mcp_stealth_chrome import brave, cookie_migration, state

BRAVE_EXE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
CHROME_EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _profile(folder: Path, local_state=None, prefs=None, sub="Default") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if local_state is not None:
        (folder / "Local State").write_text(local_state if isinstance(local_state, str) else json.dumps(local_state))
    if prefs is not None:
        (folder / sub).mkdir(exist_ok=True)
        (folder / sub / "Preferences").write_text(prefs if isinstance(prefs, str) else json.dumps(prefs))
    return folder


def _snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """browser_launch and _launch_browser_instance stopped before any browser starts."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "data" / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "data" / "profiles")
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", None)
    exe = tmp_path / "brave-bin" / "brave"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setenv(brave.ENV_PATH, str(exe))
    seen = {}

    async def no_start(config):
        seen["config"] = config
        raise RuntimeError("STOP: a browser would start here")

    monkeypatch.setattr(server, "_start_chrome", no_start)

    async def no_cookie_move(profile_path, config):
        seen["cookie_profile"] = profile_path
        return server._CookieMove(cookie_migration.Plan("none"))

    monkeypatch.setattr(server, "_prepare_cookie_move", no_cookie_move)
    return server, seen


def _spawn(server, **kw):
    args = dict(instance_id="w", url="about:blank", headless=True, proxy=None, user_agent=None,
                window_width=800, window_height=600, persistent=False, lang="en-US", extra_args=None,
                storage_state_path=None, idle_timeout=0)
    args.update(kw)
    return asyncio.run(server._launch_browser_instance(**args))


# ── F1: --user-data-dir in extra_args ───────────────────────────────────────

@pytest.mark.parametrize("args,expect", [
    (["--user-data-dir=/p", "--x"], ("/p", ["--x"], "")),
    (["-user-data-dir=/p"], ("/p", [], "")),
    (["--User-Data-Dir=/p"], ("/p", [], "")),
    (["--user-data-dir=/p", "--user-data-dir=/p"], ("/p", [], "")),
    (["--x"], (None, ["--x"], "")),
])
def test_the_profile_flag_is_taken_out_of_extra_args(args, expect):
    assert brave.split_profile_args(args) == expect


@pytest.mark.parametrize("args,needle", [
    (["--user-data-dir=/a", "--user-data-dir=/b"], "two different"),
    (["--user-data-dir"], "needs a value"),
    (["--user-data-dir="], "needs a value"),
    (["--profile-directory=../outside"], "one folder name"),
    (["--profile-directory=/abs/elsewhere"], "one folder name"),
    (["--profile-directory=A", "--profile-directory=B"], "two different"),
])
def test_unsafe_profile_flags_are_refused(args, needle):
    _udd, _rest, problem = brave.split_profile_args(args)
    assert needle in problem


@pytest.mark.parametrize("browser,maker", [("brave", "chrome"), ("chrome", "brave")])
def test_extra_args_user_data_dir_gets_the_binding(launcher, tmp_path, browser, maker):
    server, seen = launcher
    other = _profile(tmp_path / "other", {"brave": {}} if maker == "brave" else {"profile": {}})
    before = _snapshot(other)
    text = asyncio.run(server.browser_launch(browser=browser, persistent=False, headless=True,
                                             extra_args=[f"--user-data-dir={other}"]))
    assert text.startswith("Error:") and f"made by {brave.name(maker)}" in text, text
    assert "config" not in seen and _snapshot(other) == before
    ok, text = _spawn(server, browser=browser, extra_args=[f"--user-data-dir={other}"])
    assert not ok and f"made by {brave.name(maker)}" in text, text
    assert "config" not in seen and _snapshot(other) == before


def test_extra_args_user_data_dir_is_the_profile_migration_and_launch_see(launcher, tmp_path):
    server, seen = launcher
    chosen = _profile(tmp_path / "chosen", {"brave": {}})
    text = asyncio.run(server.browser_launch(browser="brave", headless=True,
                                             extra_args=[f"--user-data-dir={chosen}"]))
    assert "STOP" in text, text
    assert Path(str(seen["cookie_profile"])) == chosen
    flags = [a for a in seen["config"]() if "user-data-dir" in a.lower()]
    assert flags == [f"--user-data-dir={chosen}"], flags


def test_two_roots_are_refused(launcher, tmp_path):
    server, seen = launcher
    a, b = _profile(tmp_path / "a"), _profile(tmp_path / "b")
    text = asyncio.run(server.browser_launch(browser="brave", headless=True, user_data_dir=str(a),
                                             extra_args=[f"--user-data-dir={b}"]))
    assert text.startswith("Error:") and "two different" in text and "config" not in seen
    ok, text = _spawn(server, browser="brave", profile_dir_override=str(a), extra_args=[f"--user-data-dir={b}"])
    assert not ok and "two different" in text


# ── F2: unknown means refuse, both ways ─────────────────────────────────────

def test_a_broken_local_state_is_unknown_even_beside_a_brave_preferences(tmp_path):
    """Second review: one unreadable piece of evidence makes the maker unknown, refused both ways."""
    p = _profile(tmp_path / "p", "{broken", {"brave": {}, "profile": {}})
    assert brave.profile_browser(p) == "unknown"
    assert "cannot be told" in brave.binding_refusal(p, "chrome")
    assert "cannot be told" in brave.binding_refusal(p, "brave")


@pytest.mark.parametrize("build", [
    lambda d: _profile(d, "{broken"),                                  # Local State unreadable, nothing else
    lambda d: _profile(d, {}, {"brave": {}}),                           # Local State says Chrome, Preferences Brave
    lambda d: (_profile(d), (d / "Default").mkdir(), (d / "Default" / "Cookies").write_bytes(b"x")),  # data, no maker
    lambda d: _profile(d, None, "{broken"),                             # only an unreadable Preferences
])
@pytest.mark.parametrize("product", ["brave", "chrome"])
def test_a_profile_of_unknown_maker_is_refused_to_both(tmp_path, build, product):
    folder = tmp_path / "p"
    build(folder)
    assert brave.profile_browser(folder) == "unknown"
    why = brave.binding_refusal(folder, product)
    assert "cannot be told" in why and "Nothing was started" in why


def test_new_and_empty_profiles_still_open(tmp_path):
    assert brave.binding_refusal(tmp_path / "missing", "chrome") == ""
    (tmp_path / "empty").mkdir()
    assert brave.binding_refusal(tmp_path / "empty", "brave") == ""


def test_a_failing_check_refuses(monkeypatch, tmp_path):
    from mcp_stealth_chrome import server

    def boom(*_a, **_k):
        raise OSError("disk")
    monkeypatch.setattr(brave, "binding_refusal", boom)
    assert "could not be checked" in server._profile_version_refusal(tmp_path, CHROME_EXE)


# ── F3 / F4: where and when Preferences are written ─────────────────────────

def test_the_writer_stays_inside_the_profile(tmp_path):
    root = _profile(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    for args in (["--profile-directory=../outside"], [f"--profile-directory={outside}"]):
        with pytest.raises(ValueError):
            brave.write_vertical_tabs(root, args, "brave", "right")
    (root / "Default").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        brave.write_vertical_tabs(root, [], "brave", "right")
    real = _profile(tmp_path / "real", None, {"a": 1})
    (real / "Profile 2").mkdir()
    (real / "Profile 2" / "Preferences").symlink_to(outside / "Preferences")
    with pytest.raises(ValueError):
        brave.write_vertical_tabs(real, ["--profile-directory=Profile 2"], "brave", "right")
    assert list(outside.iterdir()) == []


def test_the_writer_refuses_paths_and_links_that_stay_inside_too(tmp_path):
    """Each check on its own: a path-like name that still lands inside, and a link to another
    folder inside, are refused as well; where a link leads is not where the browser writes."""
    root = _profile(tmp_path / "root", None, {"a": 1})
    with pytest.raises(ValueError, match="one folder name"):
        brave.write_vertical_tabs(root, ["--profile-directory=A/../Default"], "brave", "right")
    (root / "Other").mkdir()
    (root / "Linked").symlink_to(root / "Other", target_is_directory=True)
    with pytest.raises(ValueError, match="is a link"):
        brave.write_vertical_tabs(root, ["--profile-directory=Linked"], "brave", "right")
    assert json.loads((root / "Default" / "Preferences").read_text()) == {"a": 1}
    assert list((root / "Other").iterdir()) == []


def test_the_last_profile_directory_flag_is_the_one_written(tmp_path):
    root = _profile(tmp_path / "root")
    brave.write_vertical_tabs(root, ["--profile-directory=A", "--profile-directory=A"], "brave", "left")
    assert brave.vertical_tabs_read_back(root, ["--profile-directory=A"], "brave") == "left"


def test_the_writer_refuses_a_lock_it_cannot_read(tmp_path):
    root = _profile(tmp_path / "root", None, {"a": 1})
    (root / "SingletonLock").write_text("not a symlink")
    assert state.is_chrome_profile_locked(root) and state.chrome_lock_holder_pid(root) is None
    before = _snapshot(root)
    with pytest.raises(ValueError, match="in use"):
        brave.write_vertical_tabs(root, [], "brave", "right")
    assert _snapshot(root) == before


def test_the_writer_refuses_when_running_browsers_cannot_be_listed(tmp_path, monkeypatch):
    root = _profile(tmp_path / "root", None, {"a": 1})
    monkeypatch.setattr(state, "process_table", lambda: None)
    with pytest.raises(ValueError, match="cannot be told"):
        brave.write_vertical_tabs(root, [], "brave", "right")
    monkeypatch.setattr(state, "process_table", lambda: [(4242, 1, f"brave --user-data-dir={root}")])
    with pytest.raises(ValueError, match="in use"):
        brave.write_vertical_tabs(root, [], "brave", "right")
    assert json.loads((root / "Default" / "Preferences").read_text()) == {"a": 1}


# ── F5: a Brave executable with another name ────────────────────────────────

def test_a_brave_wrapper_runs_as_brave(monkeypatch, tmp_path):
    from mcp_stealth_chrome import dock_icon, server

    wrapper = tmp_path / "my-brave-wrapper"
    wrapper.write_text("")
    monkeypatch.setenv(brave.ENV_PATH, str(wrapper))
    product, exe, problem = brave.check_request("brave", None)
    assert (product, exe, problem) == ("brave", str(wrapper), "")
    monkeypatch.setattr(dock_icon, "prepare", lambda *a, **k: pytest.fail("Dock icon asked for Brave"))
    choice = server._launch_choice(None, False, [], exe)
    assert choice.product == "brave"

    class Config:
        browser_executable_path = CHROME_EXE
    config = Config()
    asyncio.run(server._use_dock_icon_browser(config, False, choice))
    assert config.browser_executable_path == str(wrapper)
    chrome_made = _profile(tmp_path / "c", {"profile": {}})
    assert "made by Google Chrome" in server._profile_version_refusal(chrome_made, str(wrapper), product="brave")

    class Browser:
        config = Config()
    b = Browser()
    brave.mark(b, "brave")
    assert brave.product_of(b) == "brave"


# ── F6: attach ──────────────────────────────────────────────────────────────

def test_an_attached_browser_of_unknown_kind_gets_no_google_chrome_hints(monkeypatch):
    from nodriver.cdp import network

    from mcp_stealth_chrome import server

    captured = {}
    real = network.set_user_agent_override

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(network, "set_user_agent_override", spy)

    class Tab:
        def __init__(self, product):
            self.browser = type("B", (), {})()
            self.browser.config = type("C", (), {"browser_executable_path": CHROME_EXE})()
            brave.mark(self.browser, product)
            self.sent = []

        async def send(self, command):
            self.sent.append(command)
            if len(self.sent) == 1:
                return ("1.3", "Chrome/157.0.8103.39", "r",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36", "js")

    with pytest.raises(RuntimeError, match="could not be told"):
        asyncio.run(server._apply_fingerprint_to_tab(Tab("unknown"), {"accept_language": "id-ID"}))
    tab = Tab("unknown")
    asyncio.run(server._apply_fingerprint_to_tab(tab, {}))  # nothing to override: nothing sent but the version
    assert len(tab.sent) == 1
    tab = Tab("brave")
    asyncio.run(server._apply_fingerprint_to_tab(tab, {"accept_language": "id-ID"}))
    brands = [b.brand for b in captured["user_agent_metadata"].brands]
    assert "Brave" in brands and "Google Chrome" not in brands


def test_attach_tells_brave_from_the_process_on_that_port(monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(state, "process_table", lambda: [
        (11, 1, f"{BRAVE_EXE} --remote-debugging-port=9333 --user-data-dir=/p"),
        (12, 11, f"{BRAVE_EXE} --type=renderer --remote-debugging-port=9333"),
        (13, 1, f"{CHROME_EXE} --remote-debugging-port=9444")])
    assert server._attached_product_from_processes("127.0.0.1", 9333) == "brave"
    assert server._attached_product_from_processes("localhost", 9444) == "chrome"
    assert server._attached_product_from_processes("127.0.0.1", 9555) is None
    assert server._attached_product_from_processes("10.0.0.5", 9333) is None  # not this machine


# ── F7: emulate_device on Android ───────────────────────────────────────────

@pytest.mark.parametrize("product,brand", [("brave", "Brave"), ("chrome", "Google Chrome")])
def test_emulate_device_on_android_names_the_browser(monkeypatch, product, brand):
    from nodriver.cdp import network

    from mcp_stealth_chrome.state import BrowserState
    from mcp_stealth_chrome.tools import devtools

    class Tab:
        def __init__(self):
            self.browser = type("B", (), {})()
            brave.mark(self.browser, product)

        async def send(self, command):
            if getattr(command, "__name__", "") == "get_version" or not hasattr(self, "_v"):
                self._v = True
                return ("1.3", "Chrome/157.0.8103.39", "r", "UA", "js")
            return None

    captured = {}
    real = network.set_user_agent_override

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(network, "set_user_agent_override", spy)
    monkeypatch.setattr(BrowserState, "active_tab", staticmethod(lambda *a, **k: Tab()))
    text = asyncio.run(devtools.emulate_device("pixel-8"))
    assert not text.startswith("Error"), text
    brands = [b.brand for b in captured["user_agent_metadata"].brands]
    assert brand in brands and ("Google Chrome" in brands) == (product == "chrome"), brands


# ── F8: unknown version means the safe path ─────────────────────────────────

def _cookie_db(profile: Path) -> None:
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(profile / "Default" / "Cookies") as db:
        db.execute("CREATE TABLE cookies (is_persistent INTEGER, expires_utc INTEGER, encrypted_value BLOB)")
        db.execute("INSERT INTO cookies VALUES (1, 0, ?)", (b"v10abc",))


def _bundle(tmp_path: Path, name: str, version: str) -> str:
    import plistlib
    contents = tmp_path / f"{name}.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as out:
        plistlib.dump({"CFBundleShortVersionString": version}, out)
    exe = contents / "MacOS" / name
    exe.write_text("")
    return str(exe)


@pytest.mark.skipif(sys.platform != "darwin", reason="the Keychain key change exists on macOS only")
def test_unknown_version_with_v10_cookies_is_refused(tmp_path):
    profile = tmp_path / "p"
    _cookie_db(profile)
    known = _bundle(tmp_path, "Brave Browser", "158.1.99.1")
    unknown = _bundle(tmp_path / "u", "Brave Browser", "nightly")
    assert cookie_migration.plan(profile, [], known, "brave").kind == "migrate"
    refused = cookie_migration.plan(profile, [], unknown, "brave")
    assert refused.kind == "refuse" and "version" in refused.reason
    empty = tmp_path / "e"
    empty.mkdir()
    assert cookie_migration.plan(empty, [], unknown, "brave").kind == "none"  # nothing to lose
    # Chrome is decided exactly as release-fixes decides it: an unknown version is "none".
    assert cookie_migration.plan(profile, [], unknown).kind == "none"
    assert cookie_migration.plan(profile, [], unknown, "chrome").kind == "none"


def test_a_refused_cookie_plan_stops_the_launch(monkeypatch, tmp_path):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(cookie_migration, "plan",
                        lambda *a, **k: cookie_migration.Plan("refuse", "its version cannot be read"))
    move = asyncio.run(server._prepare_cookie_move(tmp_path, type("C", (), {
        "browser_args": [], "browser_executable_path": BRAVE_EXE})()))
    assert move.refuse and "its version cannot be read" in move.refuse
    assert "with Brave 158 or newer" in move.refuse


def test_a_launch_stops_on_a_refused_cookie_plan(monkeypatch, tmp_path):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cookie_migration, "plan",
                        lambda *a, **k: cookie_migration.Plan("refuse", "its version cannot be read"))

    async def no_start(config):
        raise AssertionError("a browser was started")
    monkeypatch.setattr(server, "_start_chrome", no_start)
    folder = _profile(tmp_path / "p", {"profile": {}})
    text = asyncio.run(server.browser_launch(headless=True, user_data_dir=str(folder)))
    assert text.startswith("Error:") and "its version cannot be read" in text, text
    ok, text = _spawn(server, profile_dir_override=str(folder))
    assert not ok and "its version cannot be read" in text, text


def test_default_folders_follow_the_browser():
    chrome_dirs = [str(p) for p in cookie_migration.default_data_dirs("chrome")]
    brave_dirs = [str(p) for p in cookie_migration.default_data_dirs("brave")]
    assert all("Google" in d for d in chrome_dirs) and all("BraveSoftware" in d for d in brave_dirs)


# ── F9: the Brave folders in report, cleanup and the start-up pass ──────────

def test_profile_report_lists_every_brave_folder(monkeypatch, tmp_path):
    from mcp_stealth_chrome.tools import profiles

    monkeypatch.setattr(profiles, "_root", lambda: tmp_path)
    for rel in ("profile", "profiles/work", "brave-profile", "brave-profiles/acct", "brave-profile-pid999999"):
        (tmp_path / rel).mkdir(parents=True)
    names = {(n, k) for n, _p, k in profiles._named_profiles()}
    assert {("brave-profile", "main"), ("brave-profiles/acct", "named"),
            ("brave-profile-pid999999", "fallback")} <= names


def test_a_brave_fallback_is_removable_like_a_chrome_one(monkeypatch, tmp_path):
    from mcp_stealth_chrome import profile_gc
    from mcp_stealth_chrome.tools import profiles

    monkeypatch.setattr(profiles, "_root", lambda: tmp_path)
    (tmp_path / "brave-profile-pid999999").mkdir()
    monkeypatch.setattr(profiles, "_in_use", lambda p: {"in_use": False})
    monkeypatch.setattr(state, "_pid_alive", lambda pid: False)
    assert profiles._removal_refusal("brave-profile-pid999999", tmp_path / "brave-profile-pid999999") is None
    assert profiles._removal_refusal("brave-profile", tmp_path / "brave-profile") is not None
    assert profile_gc.FALLBACK_NAME.match("brave-profile-pid12").group(1) == "12"
    assert not profile_gc.FALLBACK_NAME.match("brave-profiles")
    assert not profile_gc.FALLBACK_NAME.match("xbrave-profile-pid12")


def test_the_writer_refuses_a_link_is_symlink_cannot_see(tmp_path, monkeypatch):
    """A Windows junction is a link Path.is_symlink() reports as False (Python 3.11). Stood in
    for here by a real symlink with is_symlink() blinded: the resolved path still has to be inside."""
    root = _profile(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Default").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(ValueError, match="not inside"):
        brave.write_vertical_tabs(root, [], "brave", "right")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("seen,product", [("object", "brave"), ("undefined", "unknown"), (None, "unknown")])
def test_attach_falls_back_to_navigator_brave(monkeypatch, seen, product):
    """With no process to read (another machine, or no list), navigator.brave in an isolated
    world decides. A target nodriver has not updated yet has url "", and still counts."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "_attached_product_from_processes", lambda host, port: None)

    async def isolated(tab, expression):
        assert expression == "typeof navigator.brave"
        if seen is None:
            raise RuntimeError("no world")
        return seen
    monkeypatch.setattr(server, "evaluate_isolated", isolated)
    tab = type("T", (), {"target": type("I", (), {"url": ""})()})()
    browser = type("B", (), {"tabs": [tab]})()
    assert asyncio.run(server._attached_product(browser, "10.0.0.5", 9222)) == product


# ── Second review: F2 and F6 ────────────────────────────────────────────────

@pytest.mark.parametrize("build", [
    # Local State says Brave, a valid Preferences says Chrome.
    lambda d: _profile(d, {"brave": {}}, {"profile": {}}),
    # Local State and Default agree on Chrome, another sub-profile's Preferences is unreadable.
    lambda d: (_profile(d, {"profile": {}}, {"profile": {}}), _profile(d, None, "{cut", sub="Profile 1")),
    # Local State and Default agree on Brave, another sub-profile's Preferences is unreadable.
    lambda d: (_profile(d, {"brave": {}}, {"brave": {}}), _profile(d, None, "{cut", sub="Profile 1")),
    # Local State says Chrome, one Preferences says Brave and one Chrome.
    lambda d: (_profile(d, {"profile": {}}, {"brave": {}}), _profile(d, None, {"profile": {}}, sub="Profile 1")),
])
@pytest.mark.parametrize("product", ["brave", "chrome"])
def test_every_piece_of_evidence_must_be_readable_and_agree(tmp_path, build, product):
    folder = tmp_path / "p"
    build(folder)
    assert brave.profile_browser(folder) == "unknown"
    assert "cannot be told" in brave.binding_refusal(folder, product)


def test_agreeing_evidence_still_names_the_maker(tmp_path):
    b = _profile(tmp_path / "b", {"brave": {}}, {"brave": {}})
    _profile(b, None, {"brave_shields": {}}, sub="Profile 1")
    assert brave.profile_browser(b) == "brave"
    c = _profile(tmp_path / "c", {"profile": {}}, {"profile": {}})
    assert brave.profile_browser(c) == "chrome"
    only_local_state = _profile(tmp_path / "ls", {"brave": {}})
    assert brave.profile_browser(only_local_state) == "brave"


@pytest.mark.parametrize("command,product", [
    (f"{CHROME_EXE} --remote-debugging-port=9222", "chrome"),
    ("/usr/bin/google-chrome-stable --remote-debugging-port=9222", "chrome"),
    ("/opt/google/chrome/chrome --remote-debugging-port=9222", "chrome"),
    (r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222', "chrome"),
    ("/usr/bin/chromium --remote-debugging-port=9222", "chrome"),
    (f"{BRAVE_EXE} --remote-debugging-port=9222", "brave"),
    ("/opt/my-brave-wrapper --remote-debugging-port=9222", None),
    ("/opt/vivaldi/vivaldi-bin --remote-debugging-port=9222", None),
    ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge --remote-debugging-port=9222", None),
])
def test_only_a_known_executable_names_the_attached_browser(monkeypatch, command, product):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(state, "process_table", lambda: [(11, 1, command)])
    assert server._attached_product_from_processes("127.0.0.1", 9222) == product


@pytest.mark.parametrize("seen,product", [("object", "brave"), ("undefined", "unknown")])
def test_an_unknown_executable_falls_through_to_navigator_brave(monkeypatch, seen, product):
    """A wrapper or another Chromium on the port: navigator.brave says Brave, and nothing says
    Chrome, which is not a guess either."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(state, "process_table", lambda: [(11, 1, "/opt/my-wrapper --remote-debugging-port=9222")])

    async def isolated(tab, expression):
        return seen
    monkeypatch.setattr(server, "evaluate_isolated", isolated)
    tab = type("T", (), {"target": type("I", (), {"url": "about:blank"})()})()
    browser = type("B", (), {"tabs": [tab]})()
    assert asyncio.run(server._attached_product(browser, "127.0.0.1", 9222)) == product


def test_attach_says_when_the_browser_could_not_be_told():
    import inspect

    from mcp_stealth_chrome import server

    text = server._attached_note("unknown")
    assert "could not be told" in text and "client hints" in text
    assert server._attached_note("brave") == "" and server._attached_note("chrome") == ""
    assert "_attached_note(" in inspect.getsource(server.attach_to_chrome)


@pytest.mark.skipif(sys.platform != "darwin", reason="the Keychain key change exists on macOS only")
def test_default_folders_used_follow_the_browser(tmp_path, monkeypatch):
    """Brave keeps the v10 key in its own default folder, Chrome in Chrome's: each browser's
    plan skips only its own (stand-ins under a temporary home)."""
    from test_cookie_migration import _chrome, _profile as cookie_profile

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    support = tmp_path / "home" / "Library" / "Application Support"
    brave_default = cookie_profile(support / "BraveSoftware" / "Brave-Browser", (b"v10", 1, 3600))
    chrome_default = cookie_profile(support / "Google" / "Chrome", (b"v10", 1, 3600))
    brave158 = _bundle(tmp_path / "b", "Brave Browser", "158.1.101.48")
    chrome158 = _chrome(tmp_path / "c", "158.0.8127.18")
    assert cookie_migration.plan(brave_default, [], brave158, "brave").kind == "none"
    assert cookie_migration.plan(brave_default, [], chrome158, "chrome").kind == "migrate"
    assert cookie_migration.plan(chrome_default, [], chrome158, "chrome").kind == "none"
    assert cookie_migration.plan(chrome_default, [], brave158, "brave").kind == "migrate"


def test_the_writer_refuses_a_lock_whose_state_is_unknown(tmp_path, monkeypatch):
    """chrome_lock_state (main 0.16.4) says "unknown" when the lock cannot even be looked at;
    is_chrome_profile_locked reads that as free. Unknown is not free."""
    root = _profile(tmp_path / "root", None, {"a": 1})
    real = os.path.lexists

    def cannot_look(path):
        if str(path).endswith("SingletonLock"):
            raise PermissionError("no")
        return real(path)
    monkeypatch.setattr(os.path, "lexists", cannot_look)
    monkeypatch.setattr(state, "process_table", lambda: [])
    assert state.chrome_lock_state(root)[0] == "unknown"
    with pytest.raises(ValueError, match="in use"):
        brave.write_vertical_tabs(root, [], "brave", "right")
    assert json.loads((root / "Default" / "Preferences").read_text()) == {"a": 1}


# ── Third review: F2-3, linked evidence ─────────────────────────────────────

def _linked(tmp_path: Path, which: str, target_content=None) -> Path:
    """A profile whose Local State or Default/Preferences is a link: dangling when
    target_content is None, else to a file outside the profile holding it."""
    folder = _profile(tmp_path / "p", {"profile": {}} if which == "prefs" else None,
                      {"profile": {}} if which == "local_state" else None)
    target = tmp_path / "outside" / "evidence.json"
    if target_content is not None:
        target.parent.mkdir()
        target.write_text(json.dumps(target_content))
    link = folder / ("Local State" if which == "local_state" else "Default/Preferences")
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(target)
    return folder


@pytest.mark.parametrize("which", ["prefs", "local_state"])
@pytest.mark.parametrize("target_content", [None, {"profile": {}}, {"brave": {}}])
@pytest.mark.parametrize("product", ["brave", "chrome"])
def test_linked_evidence_is_unknown_and_never_followed(tmp_path, which, target_content, product):
    folder = _linked(tmp_path, which, target_content)
    before = _snapshot(folder)
    assert brave.profile_browser(folder) == "unknown"
    assert "cannot be told" in brave.binding_refusal(folder, product)
    assert _snapshot(folder) == before


def test_a_healthy_chrome_profile_is_still_chrome(tmp_path):
    folder = _profile(tmp_path / "c", {"profile": {}}, {"profile": {}})
    _profile(folder, None, {"profile": {}}, sub="Profile 1")
    assert brave.profile_browser(folder) == "chrome"
    assert brave.binding_refusal(folder, "chrome") == ""


# ── keep_alive (main 0.17.0) ────────────────────────────────────────────────

def test_kept_candidates_follow_the_browser(tmp_path, monkeypatch):
    """A Brave launch looks for a kept Brave in Brave's folders, never at Chrome's profile or
    Chrome main's affinity; Chrome's candidates are unchanged."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", tmp_path / "profile-pid7")
    assert server._kept_candidates("main", "brave") == [tmp_path / "brave-profile"]
    assert server._kept_candidates("acct", "brave") == [tmp_path / "brave-profiles" / "acct"]
    assert server._kept_candidates("main") == [tmp_path / "profile-pid7", tmp_path / "profile"]


@pytest.mark.parametrize("maker,asked", [("chrome", "brave"), ("brave", "chrome")])
def test_a_kept_browser_of_the_other_kind_is_not_reattached(tmp_path, monkeypatch, maker, asked):
    from mcp_stealth_chrome import keep_alive, server

    profile = _profile(tmp_path / "p", {"brave": {}} if maker == "brave" else {"profile": {}})
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242)
    monkeypatch.setattr(keep_alive, "read", lambda p: {"pid": 4242, "port": 9})
    monkeypatch.setattr(keep_alive, "check", lambda *a, **k: pytest.fail("checked a browser of the other kind"))
    verdict = server._kept_verdict(profile, asked)
    assert verdict and not verdict["ok"], verdict
    assert f"made by {brave.name(maker)}" in verdict["reason"]
    assert "made by" in server._not_reattached_note(profile, verdict)


def test_a_kept_browser_of_the_same_kind_is_checked(tmp_path, monkeypatch):
    from mcp_stealth_chrome import keep_alive, server

    profile = _profile(tmp_path / "p", {"brave": {}})
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242)
    monkeypatch.setattr(keep_alive, "read", lambda p: {"pid": 4242})
    monkeypatch.setattr(keep_alive, "check", lambda profile, hold=False: {"ok": True, "record": {}})
    assert server._kept_verdict(profile, "brave") == {"ok": True, "record": {}}


def test_a_reattached_browser_is_marked_with_its_profile_s_maker(tmp_path):
    """The marking itself is proved on real Brave: test_brave_e2e, keep_alive."""
    from mcp_stealth_chrome import server

    assert server._reattached_product(_profile(tmp_path / "b", {"brave": {}}, {"brave": {}})) == "brave"
    assert server._reattached_product(_profile(tmp_path / "c", {"profile": {}})) == "chrome"
    assert server._reattached_product(_profile(tmp_path / "u", "{broken")) == "unknown"


def test_vertical_tabs_is_named_as_not_applied_on_a_reattach():
    from mcp_stealth_chrome import server

    assert "vertical_tabs='right'" in server._reattach_ignored({"vertical_tabs": "right"})
    assert server._reattach_ignored({"vertical_tabs": None}) == []


def test_a_brave_launch_on_a_kept_chrome_profile_is_told_and_not_reattached(launcher, tmp_path, monkeypatch):
    """The explicit folder of browser_launch and spawn_browser: the kept Chrome is never
    checked for a Brave launch, and the result says why it was not reattached."""
    from mcp_stealth_chrome import keep_alive

    server, seen = launcher
    profile = _profile(tmp_path / "kept-chrome", {"profile": {}}, {"profile": {}})
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242)
    monkeypatch.setattr(keep_alive, "read", lambda p: {"pid": 4242, "port": 9})
    monkeypatch.setattr(keep_alive, "check", lambda *a, **k: pytest.fail("checked a kept Chrome for Brave"))
    text = asyncio.run(server.browser_launch(browser="brave", headless=True, user_data_dir=str(profile)))
    assert text.startswith("Error:") and "was not reattached" in text and "made by Google Chrome" in text, text
    ok, text = _spawn(server, browser="brave", profile_dir_override=str(profile))
    assert not ok and "config" not in seen, text


def test_linux_prefers_brave_s_own_binary_over_its_wrapper_script(monkeypatch):
    """/usr/bin/brave-browser is a shell script: the PID nodriver records is the script's, not the
    Brave holding the profile lock, so keep_alive could never reattach (Ubuntu CI: lock held by
    6007, kept PID 6001). The binary goes first, as /opt/google/chrome/chrome does for Chrome."""
    monkeypatch.setattr(brave.sys, "platform", "linux")
    found = brave.candidates()
    assert found[0] == "/opt/brave.com/brave/brave"
    assert found.index("/opt/brave.com/brave/brave") < found.index("/usr/bin/brave-browser")
