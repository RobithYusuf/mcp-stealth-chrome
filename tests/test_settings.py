"""The person's settings (settings.py): the environment, then ~/.mcp-stealth/settings.json, then the
default; the command that changes them; and the overlay's own copy of the rule, which reads the
same file while it runs."""
import ast
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from mcp_stealth_chrome import dock_icon, presence, settings

ENVS = [s.env for s in settings.SETTINGS]


@pytest.fixture
def sfile(tmp_path, monkeypatch):
    """A settings file of the test's own, and none of the settings' variables set."""
    path = tmp_path / "data" / "settings.json"
    monkeypatch.setenv(settings.ENV_FILE, str(path))
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    return path


def test_a_missing_file_gives_every_default(sfile):
    assert not sfile.exists()
    for s in settings.SETTINGS:
        assert settings.resolve(s.key) == {"value": s.default, "source": "default"}


def test_the_file_wins_over_the_default_and_the_environment_over_the_file(sfile, monkeypatch):
    settings.write({"overlay_bar": "compact", "overlay_light": False})
    assert settings.resolve("overlay_bar") == {"value": "compact", "source": "file"}
    assert settings.resolve("overlay_light") == {"value": False, "source": "file"}
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_BAR", "hover")
    assert settings.resolve("overlay_bar") == {"value": "hover", "source": "env MCP_STEALTH_OVERLAY_BAR"}


def test_a_value_that_does_not_parse_is_skipped_with_a_note(sfile, monkeypatch):
    sfile.parent.mkdir(parents=True)
    sfile.write_text(json.dumps({"overlay_bar": "tiny", "overlay_hide_after": -5, "overlay_lock": "maybe"}))
    bar = settings.resolve("overlay_bar")
    assert bar["value"] == "full" and bar["source"] == "default" and "'tiny'" in bar["note"]
    assert settings.resolve("overlay_hide_after")["value"] == 90.0
    assert settings.resolve("overlay_lock")["value"] is True
    # A bad environment value falls to the file, and says so.
    settings.write({"overlay_bar": "compact"})
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_BAR", "huge")
    bar = settings.resolve("overlay_bar")
    assert bar["value"] == "compact" and bar["source"] == "file" and "MCP_STEALTH_OVERLAY_BAR" in bar["note"]


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "\"text\"", ""])
def test_a_file_that_is_not_an_object_reads_as_no_settings(sfile, content):
    sfile.parent.mkdir(parents=True)
    sfile.write_text(content)
    assert settings.read_file() == {}
    assert settings.resolve("overlay_bar")["source"] == "default"


@pytest.mark.skipif(sys.platform == "win32", reason="links need privileges on Windows")
def test_a_settings_file_that_is_a_link_is_neither_read_nor_written_through(sfile, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"overlay_bar": "off"}))
    sfile.parent.mkdir(parents=True)
    sfile.symlink_to(elsewhere)
    assert settings.read_file() == {}
    with pytest.raises(OSError):
        settings.write({"overlay_bar": "compact"})
    assert json.loads(elsewhere.read_text()) == {"overlay_bar": "off"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps no POSIX mode bits")
def test_write_merges_removes_and_keeps_the_file_private(sfile):
    settings.write({"overlay_bar": "compact", "dock_icon": True})
    settings.write({"overlay_light": False, "dock_icon": None})
    assert json.loads(sfile.read_text()) == {"overlay_bar": "compact", "overlay_light": False}
    assert stat.S_IMODE(sfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(sfile.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in sfile.parent.iterdir()) == [".settings.json.lock", "settings.json"]  # no temporary file


def test_set_value_refuses_what_it_does_not_know(sfile):
    with pytest.raises(ValueError, match="unknown setting 'overlay_size'"):
        settings.set_value("overlay_size", "big")
    with pytest.raises(ValueError, match="full, compact, hover, off"):
        settings.set_value("overlay_bar", "tiny")
    with pytest.raises(ValueError, match="true or false"):
        settings.set_value("overlay_light", "dim")
    with pytest.raises(ValueError, match="from 1 to 86400"):
        settings.set_value("overlay_hide_after", "0")
    assert not sfile.exists()


def test_set_value_says_when_the_environment_still_wins(sfile, monkeypatch):
    assert "still wins" not in settings.set_value("overlay_bar", "compact")
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_BAR", "full")
    assert "MCP_STEALTH_OVERLAY_BAR is set in this environment and still wins" in settings.set_value(
        "overlay_bar", "hover")


def test_the_command_lists_sets_and_resets(sfile, capsys):
    assert settings.cli(["set", "overlay_bar", "Compact"]) == 0
    assert settings.cli(["set", "agent_overlay", "on"]) == 0
    capsys.readouterr()
    assert settings.cli([]) == 0
    out = capsys.readouterr().out
    assert str(sfile) in out
    assert "overlay_bar         compact  (file)" in out
    assert "agent_overlay       on       (file)" in out
    assert "overlay_light       on       (default)" in out
    assert settings.cli(["reset", "overlay_bar"]) == 0
    assert json.loads(sfile.read_text()) == {"agent_overlay": True}
    assert settings.cli(["reset"]) == 0
    assert json.loads(sfile.read_text()) == {}
    assert settings.cli(["set", "overlay_bar"]) == 2
    assert settings.cli(["reset", "nope"]) == 2
    assert settings.cli(["set", "overlay_bar", "tiny"]) == 2
    assert "refused" in capsys.readouterr().out


def test_the_command_is_reached_from_the_entry_point(sfile, capsys):
    from mcp_stealth_chrome import launcher
    assert "settings" in launcher.COMMANDS
    assert launcher.cli(["settings", "set", "overlay_light", "off"]) == 0
    assert settings.get("overlay_light") is False


def test_the_readers_follow_the_file(sfile, monkeypatch):
    assert presence.wanted(None) is False and dock_icon.asked_for() is False
    settings.write({"agent_overlay": True, "dock_icon": True, "overlay_bar": "off", "overlay_light": False})
    assert presence.wanted(None) is True and dock_icon.asked_for() is True
    assert presence.wanted(False) is False  # an argument is the caller's choice
    # With no bar there is nothing to take over with, so no lock either.
    assert presence.style() == {"bar": "off", "light": False, "lock": False}
    monkeypatch.setenv("MCP_STEALTH_AGENT_OVERLAY", "0")
    assert presence.wanted(None) is False


def test_the_overlay_starts_with_the_file_and_what_the_environment_fixed(sfile, monkeypatch, tmp_path):
    settings.write({"overlay_bar": "compact", "overlay_hide_after": 30})
    monkeypatch.setenv("MCP_STEALTH_OVERLAY_LIGHT", "off")
    cfg = presence.overlay_config(tmp_path, 4242, False, "start")
    assert cfg["settings_file"] == str(sfile)
    assert cfg["fixed"] == {"overlay_light": False}
    assert cfg["style"] == {"bar": "compact", "light": False, "lock": True}
    assert cfg["hide_after"] == 30.0
    json.dumps(cfg)  # it goes to the overlay on its command line


def _overlay_rule(settings_file, fixed):
    """The overlay's own settings functions, taken from its source: the script imports AppKit at
    the top, so it cannot be imported here."""
    source = (Path(presence.__file__).with_name("_agent_overlay.py")).read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"read_settings", "write_settings", "_merge_settings", "_flag", "chosen_style"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in body} == wanted
    space = {"os": os, "json": json, "stat": stat, "time": time, "SETTINGS_FILE": str(settings_file),
             "FIXED": fixed, "BAR_MODES": ("full", "compact", "hover", "off")}
    exec(compile(ast.Module(body=body, type_ignores=[]), "_agent_overlay.py", "exec"), space)
    return space


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay runs on macOS only (it reads os.getuid)")
@pytest.mark.parametrize("content,env", [
    ({}, {}),
    ({"overlay_bar": "compact", "overlay_light": False}, {}),
    ({"overlay_bar": "HOVER", "overlay_lock": "0"}, {}),
    ({"overlay_bar": "tiny", "overlay_light": "dim", "overlay_lock": 0}, {}),
    ({"overlay_bar": "off", "overlay_light": 1}, {}),
    ({"overlay_bar": "compact", "overlay_lock": False}, {"MCP_STEALTH_OVERLAY_BAR": "full", "MCP_STEALTH_OVERLAY_LOCK": "on"}),
    ({}, {"MCP_STEALTH_OVERLAY_LIGHT": "no"}),
])
def test_the_overlay_reads_the_file_as_the_server_does(sfile, monkeypatch, content, env, tmp_path):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    sfile.parent.mkdir(parents=True)
    sfile.write_text(json.dumps(content))
    fixed = presence.overlay_config(tmp_path, 1, False)["fixed"]
    rule = _overlay_rule(sfile, fixed)
    bar, light, lock = rule["chosen_style"](rule["read_settings"]())
    server = presence.style()
    assert (bar, light, lock and bar != "off") == (server["bar"], server["light"], server["lock"])


@pytest.mark.skipif(sys.platform == "win32", reason="the overlay runs on macOS only")
def test_a_choice_from_the_overlay_menu_lands_in_the_file_the_server_reads(sfile):
    settings.write({"agent_overlay": True})
    rule = _overlay_rule(sfile, {})
    rule["write_settings"]({"overlay_bar": "hover"})
    assert settings.resolve("overlay_bar") == {"value": "hover", "source": "file"}
    assert settings.get("agent_overlay") is True  # merged, not replaced
    assert stat.S_IMODE(sfile.stat().st_mode) == 0o600
    assert sorted(p.name for p in sfile.parent.iterdir()) == [".settings.json.lock", "settings.json"]


def test_server_status_reports_each_setting_and_where_it_came_from(sfile, monkeypatch):
    from mcp_stealth_chrome import server
    settings.write({"overlay_bar": "compact"})
    monkeypatch.setenv("MCP_STEALTH_DOCK_ICON", "1")
    report = server._settings_report()
    assert report["file"] == str(sfile)
    assert report["overlay_bar"] == {"value": "compact", "source": "file"}
    assert report["dock_icon"] == {"value": True, "source": "env MCP_STEALTH_DOCK_ICON"}
    assert report["overlay_light"] == {"value": True, "source": "default"}
    monkeypatch.setattr(settings, "report", lambda: 1 / 0)
    assert "ZeroDivisionError" in server._settings_report()["error"]


def test_the_developers_own_file_never_reaches_the_suite():
    """conftest points every test at a file of the run's own."""
    given = os.environ.get(settings.ENV_FILE, "")
    assert given and Path(given) != Path.home() / ".mcp-stealth" / "settings.json"


@pytest.mark.skipif(sys.platform == "win32", reason="the lock is flock, POSIX only")
def test_two_writers_at_once_lose_no_choice(sfile):
    """Read, merge and rename were not one step: two writers each read the old file and the
    second rename dropped the first one's choice. The command and the overlay share one lock."""
    import threading
    settings.write({})
    rule = _overlay_rule(sfile, {})

    def server_side(n):
        for i in range(40):
            settings.write({f"server_{n}_{i}": i})

    def overlay_side():
        for i in range(40):
            rule["write_settings"]({f"overlay_{i}": i})

    threads = [threading.Thread(target=server_side, args=(n,)) for n in range(2)]
    threads.append(threading.Thread(target=overlay_side))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(sfile.read_text())
    assert len(data) == 120, f"{120 - len(data)} choices were lost"


def _overlay_live(fixed, cursor_at_start=False, show_cursor=False):
    """Overlay.apply_timing_and_cursor with the globals it uses, taken from the overlay's source."""
    source = (Path(presence.__file__).with_name("_agent_overlay.py")).read_text(encoding="utf-8")
    tree = ast.parse(source)
    flag = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_flag")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Overlay")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "apply_timing_and_cursor")
    holder = ast.ClassDef(name="Holder", bases=[], keywords=[], body=[method], decorator_list=[])
    space = {"FIXED": fixed, "CONFIG": {"hide_after": 90.0, "cursor": cursor_at_start}, "HIDE_AFTER": 90.0}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[flag, holder], type_ignores=[])), "_agent_overlay.py", "exec"), space)
    obj = space["Holder"]()
    obj.control, obj.cursor_seen, obj.saved = {"show_cursor": show_cursor}, cursor_at_start, 0

    def save():
        obj.saved += 1
    obj.save = save
    return obj, space


def test_the_overlay_applies_its_hide_delay_and_the_cursor_setting_as_they_change():
    obj, space = _overlay_live({})
    obj.apply_timing_and_cursor({"overlay_hide_after": 10})
    assert space["HIDE_AFTER"] == 10.0
    obj.apply_timing_and_cursor({"overlay_hide_after": -1})  # out of range: kept
    assert space["HIDE_AFTER"] == 10.0
    obj.apply_timing_and_cursor({"agent_cursor": True})
    assert obj.control["show_cursor"] is True and obj.saved == 1
    # The bar's arrow turned it off for this browser; the file did not change: left off.
    obj.control["show_cursor"] = False
    obj.apply_timing_and_cursor({"agent_cursor": True})
    assert obj.control["show_cursor"] is False
    # The environment fixed both: the file does not move them.
    obj, space = _overlay_live({"overlay_hide_after": 45.0, "agent_cursor": False})
    obj.apply_timing_and_cursor({"overlay_hide_after": 5, "agent_cursor": True})
    assert space["HIDE_AFTER"] == 45.0 and obj.control["show_cursor"] is False
