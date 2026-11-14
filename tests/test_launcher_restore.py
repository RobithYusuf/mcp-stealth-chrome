"""A browser opened from the Dock comes back with its tabs and, when asked, without Brave's extras.

No agent runs while a person uses that browser, so only the quit watcher sees its tabs. These
tests cover the watcher's list, the launcher turning it into the saved one, and the lean choice.
"""
import json
import os

import pytest

from mcp_stealth_chrome import _quit_watcher as qw
from mcp_stealth_chrome import keep_alive, launcher, tab_record

A, B, C = "https://a.test/one", "https://b.test/two", "file:///tmp/three.html"


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _keeper(tmp_path, clock, **over):
    cfg = {"pid": 4242, "pid_start": "s", "user_data_dir": str(tmp_path / "profile"),
           "tabs_file": str(tmp_path / "x.tabs"), **over}
    return qw.TabKeeper(cfg, clock), tmp_path / "x.tabs"


def _read(path):
    return json.loads(path.read_text())


def test_a_list_is_written_once_it_has_stood_still(tmp_path):
    clock = _Clock()
    keeper, path = _keeper(tmp_path, clock)
    keeper.look([A, B])
    assert not path.exists()  # just seen: it may be a browser in the middle of something
    clock.now += qw.TABS_SETTLE - 0.5
    keeper.look([A, B])
    assert not path.exists()
    clock.now += 1.0
    keeper.look([A, B])
    assert _read(path)["urls"] == [A, B]
    assert _read(path)["user_data_dir"] == str(tmp_path / "profile")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob("*.tmp"))


def test_the_windows_of_a_quit_never_replace_the_full_list(tmp_path):
    clock = _Clock()
    keeper, path = _keeper(tmp_path, clock)
    keeper.look([A, B, C])
    clock.now += 4
    keeper.look([A, B, C])
    assert _read(path)["urls"] == [A, B, C]
    # Quit: the windows go one after another, faster than the settle time, then none is left.
    for shrinking in ([A, B], [A], []):
        clock.now += 0.2
        keeper.look(shrinking)
    clock.now += 60
    keeper.look([])
    keeper.look(None)  # the endpoint stopped answering
    assert _read(path)["urls"] == [A, B, C]


def test_blank_and_internal_pages_are_not_a_list_and_keep_the_saved_one(tmp_path):
    clock = _Clock()
    keeper, path = _keeper(tmp_path, clock)
    for _ in range(3):
        keeper.look(["about:blank", "chrome://newtab/", "devtools://devtools/x"])
        clock.now += 5
    assert not path.exists()
    keeper.look([A, "about:blank"])
    clock.now += 5
    keeper.look([A, "about:blank"])
    assert _read(path)["urls"] == [A]
    keeper.look(["about:blank"])
    clock.now += 5
    keeper.look(["about:blank"])
    assert _read(path)["urls"] == [A]


def test_an_unchanged_list_is_not_written_again_and_no_file_means_no_writing(tmp_path):
    clock = _Clock()
    keeper, path = _keeper(tmp_path, clock)
    keeper.look([A])
    clock.now += 5
    keeper.look([A])
    first = path.stat().st_mtime_ns
    os.utime(path, ns=(1, 1))
    clock.now += 50
    keeper.look([A])
    assert path.stat().st_mtime_ns == 1 and first != 1
    off, nowhere = _keeper(tmp_path, clock, tabs_file=None)
    off.look([A])
    clock.now += 5
    off.look([A])  # nothing raised, nothing written


def test_quit_watch_gives_the_file_only_to_a_visible_kept_browser(monkeypatch, tmp_path):
    from mcp_stealth_chrome import quit_watch
    seen = {}

    class _Proc:
        pid = 1
        stdin = type("S", (), {"write": lambda self, b: None, "close": lambda self: None})()

    def popen(argv, **_kw):
        seen["cfg"] = json.loads(argv[-1])
        return _Proc()

    monkeypatch.setattr(quit_watch, "_identity", lambda b: (4242, "127.0.0.1", 9, str(tmp_path / "p")))
    monkeypatch.setattr(quit_watch, "watch_dir", lambda: tmp_path / "qw")
    monkeypatch.setattr(quit_watch, "pid_start", lambda b, pid: "s")
    monkeypatch.setattr(quit_watch.subprocess, "Popen", popen)
    browser = type("B", (), {})()
    if os.name != "posix":
        pytest.skip("the quit watcher is POSIX only")
    quit_watch.start(browser, 3, 5, keep_alive_record="r.json", tabs_file=str(tmp_path / "t.tabs"))
    assert seen["cfg"]["tabs_file"] == str(tmp_path / "t.tabs")
    quit_watch.start(browser, 3, 5, pages_matter=False, keep_alive_record="r.json",
                     tabs_file=str(tmp_path / "t.tabs"))
    assert "tabs_file" not in seen["cfg"]  # headless: nobody uses it by hand
    quit_watch.start(browser, 3, 5)
    assert "tabs_file" not in seen["cfg"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(tmp_path / "keep"))
    monkeypatch.setenv("MCP_STEALTH_TABS_DIR", str(tmp_path / "tabs"))
    (tmp_path / "keep").mkdir(mode=0o700)
    profile = launcher.profile_for("brave")
    profile.mkdir(parents=True)
    return profile


def _watched(profile, urls, at, user_data_dir=None):
    keep_alive.tabs_path(profile).write_text(json.dumps(
        {"pid": 1, "user_data_dir": str(user_data_dir or profile), "at": at, "urls": urls}))


def test_the_watchers_list_is_read_only_for_its_own_profile(world, tmp_path):
    assert keep_alive.last_tabs(world) is None
    _watched(world, [A, B], 50.0)
    assert keep_alive.last_tabs(world) == {"at": 50.0, "urls": [A, B]}
    _watched(world, [A], 50.0, user_data_dir=tmp_path / "another")
    assert keep_alive.last_tabs(world) is None
    for bad in ("{", json.dumps({"urls": [], "at": 1, "user_data_dir": str(world)}),
                json.dumps({"urls": [1], "at": 1, "user_data_dir": str(world)}),
                json.dumps({"urls": [A], "at": "x", "user_data_dir": str(world)})):
        keep_alive.tabs_path(world).write_text(bad)
        assert keep_alive.last_tabs(world) is None, bad
    keep_alive.tabs_path(world).unlink()
    real = tmp_path / "elsewhere.tabs"
    real.write_text(json.dumps({"urls": [A], "at": 1, "user_data_dir": str(world)}))
    keep_alive.tabs_path(world).symlink_to(real)
    assert keep_alive.last_tabs(world) is None  # never through a link
    assert keep_alive.drop_last_tabs(world) and not keep_alive.tabs_path(world).is_symlink()
    assert real.exists()
    # A record scan of the folder must not take the tab file for a keep-alive record.
    _watched(world, [A], 1.0)
    assert keep_alive.live_profiles() == set()
    assert keep_alive.tabs_path(world).exists()


def test_the_launcher_makes_the_watchers_list_the_saved_one_when_it_is_newer(world):
    import time
    _watched(world, [A, B, C], time.time())
    note = launcher.adopt_watched_tabs("brave")
    assert "recorded 3 tab(s)" in note, note
    entry, _why = tab_record.load("main", world)
    assert [t["url"] for t in entry["tabs"]] == [A, B, C] and entry["active"] == 0
    # A close an agent's server recorded later wins over an older look of the watcher.
    tab_record.save("main", world, [("k", B)], ["k"], "k", "browser_close", force=True)
    _watched(world, [A], time.time() - 3600)
    assert launcher.adopt_watched_tabs("brave") == ""
    entry, _why = tab_record.load("main", world)
    assert [t["url"] for t in entry["tabs"]] == [B]
    keep_alive.drop_last_tabs(world)
    assert launcher.adopt_watched_tabs("brave") == ""


def _cold(monkeypatch, browser="brave"):
    from mcp_stealth_chrome import dock_icon, server
    seen = {}

    async def fake_launch(**kw):
        seen.update(kw)
        seen["as_tabs"] = server.RESTORE_AS_TABS
        return "Browser launched"

    monkeypatch.setattr(server, "browser_launch", fake_launch)
    monkeypatch.setattr(server, "_stop_all_browsers", lambda: None)
    monkeypatch.setattr(dock_icon, "wait", lambda _s: None)
    monkeypatch.setattr(server, "RESTORE_AS_TABS", False)
    # cold_launch marks the process as the launcher's; a test must not leave that behind.
    monkeypatch.setattr(launcher._keep_alive, "OPENED_BY", launcher._keep_alive.OPENED_BY)
    monkeypatch.setattr(launcher._keep_alive, "read", lambda _p: {"state": "kept", "pid": 7})
    code, text, pid = launcher.cold_launch(browser, None, False)
    assert (code, pid) == (0, 7), text
    return seen


def test_a_start_from_the_launcher_restores_tabs_in_one_window(world, monkeypatch):
    seen = _cold(monkeypatch)
    assert seen["restore_tabs"] is True and seen["as_tabs"] is True
    assert seen["keep_alive"] is True and seen["headless"] is False


def test_lean_is_what_the_person_chose_and_otherwise_the_servers_own_rule(world, monkeypatch):
    monkeypatch.setattr(launcher, "LEAN_OVERRIDE", None)
    assert launcher.lean_choice("brave") is None
    assert _cold(monkeypatch)["brave_lean"] is None  # the server decides: lean on a profile it made
    launcher.set_lean("brave", True)
    assert oct(launcher._lean_path("brave").stat().st_mode & 0o777) == "0o600"
    assert _cold(monkeypatch)["brave_lean"] is True
    launcher.set_lean("brave", False)
    assert _cold(monkeypatch)["brave_lean"] is False
    monkeypatch.setattr(launcher, "LEAN_OVERRIDE", True)  # open --lean, for one start
    assert _cold(monkeypatch)["brave_lean"] is True
    # Chrome has none of these features: nothing is passed, so its result carries no lean note.
    assert _cold(monkeypatch, "chrome")["brave_lean"] is None
    launcher._lean_path("brave").write_text("maybe\n")
    monkeypatch.setattr(launcher, "LEAN_OVERRIDE", None)
    assert launcher.lean_choice("brave") is None


def test_the_cli_takes_lean_and_no_lean(world, monkeypatch, capsys):
    app = world / "Stealth Brave.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Resources" / "cli-path").write_text("/usr/local/bin/mcp-stealth-chrome\n")
    monkeypatch.setattr(launcher, "build_app", lambda browser, folder=None: app)
    assert launcher.cli(["install-launcher", "--browser", "brave", "--lean"]) == 0
    assert launcher.lean_choice("brave") is True and "lean" in capsys.readouterr().out
    assert launcher.cli(["install-launcher", "--browser", "brave", "--no-lean"]) == 0
    assert launcher.lean_choice("brave") is False
    assert launcher.cli(["install-launcher", "--browser", "brave"]) == 0
    assert launcher.lean_choice("brave") is False  # left as it was
    assert launcher.cli(["install-launcher", "--browser", "chrome", "--lean"]) == 0
    assert launcher.lean_choice("chrome") is None  # Chrome only: nothing stored


def test_forget_tabs_also_forgets_what_the_watcher_kept(world):
    from mcp_stealth_chrome import server
    _watched(world, [A], 1.0)
    note = server._forget_tabs_note("main", world, False)
    assert "removed" in note and keep_alive.last_tabs(world) is None
