"""profile_report / profile_cleanup. Everything runs against a temporary HOME."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from mcp_stealth_chrome import state
from mcp_stealth_chrome.server import mcp


def tool(name):
    return mcp._tool_manager._tools[name].fn


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def make_profile(path, cache_bytes=2000, cookies=b"SESSION"):
    (path / "Default" / "Network").mkdir(parents=True)
    (path / "Default" / "Network" / "Cookies").write_bytes(cookies)
    (path / "Default" / "Login Data").write_bytes(b"LOGIN")
    (path / "Default" / "Local Storage").mkdir()
    (path / "Default" / "Local Storage" / "x").write_bytes(b"LS")
    (path / "Default" / "Cache").mkdir()
    (path / "Default" / "Cache" / "blob").write_bytes(b"c" * cache_bytes)
    (path / "Default" / "Service Worker" / "CacheStorage").mkdir(parents=True)
    (path / "Default" / "Service Worker" / "CacheStorage" / "a").write_bytes(b"c")
    (path / "GrShaderCache").mkdir()
    (path / "GrShaderCache" / "s").write_bytes(b"c")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOME", tmp_path)
    root = tmp_path / ".mcp-stealth"
    (root / "profiles").mkdir(parents=True)
    make_profile(root / "profile")
    make_profile(root / "profiles" / "work")
    return root


def report():
    return {r["name"]: r for r in json.loads(asyncio.run(tool("profile_report")()))["profiles"]}


def cleanup(action, names, **kwargs):
    return asyncio.run(tool("profile_cleanup")(action, names, **kwargs))


def test_report_classifies_and_measures(home):
    pid = dead_pid()
    make_profile(home / f"profile-pid{pid}")
    rows = report()
    assert rows["profile"]["kind"] == "main" and rows["profile"]["removable"] is False
    assert rows["profiles/work"]["kind"] == "named" and rows["profiles/work"]["removable"] is False
    row = rows[f"profile-pid{pid}"]
    assert row["kind"] == "fallback" and row["removable"] is True and row["owner_alive"] is False
    assert row["size_bytes"] > row["cache_bytes"] >= 2000
    assert row["last_used"] and row["in_use"] is False


def test_report_changes_nothing(home):
    before = sorted(str(p) for p in home.rglob("*"))
    report()
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_remove_is_a_preview_without_confirm(home):
    name = f"profile-pid{dead_pid()}"
    make_profile(home / name)
    out = json.loads(cleanup("remove_fallback", [name]))
    assert out["dry_run"] is True and "would_free" in out["results"][0]
    assert (home / name).is_dir()


def test_remove_deletes_only_with_confirm(home):
    name = f"profile-pid{dead_pid()}"
    make_profile(home / name)
    out = json.loads(cleanup("remove_fallback", [name], confirm=True))
    assert out["results"][0]["removed"] is True
    assert not (home / name).exists()


@pytest.mark.parametrize("name", [
    "profile", "profiles/work", "..", "../..", "profile-pid", "/etc", "profile/../profile",
])
def test_main_named_and_escaping_names_are_refused(home, name):
    out = cleanup("remove_fallback", [name], confirm=True)
    assert out.startswith("Error:")
    assert (home / "profile" / "Default" / "Network" / "Cookies").exists()
    assert (home / "profiles" / "work" / "Default" / "Network" / "Cookies").exists()
    assert home.exists()


def test_live_owner_refuses_removal(home):
    name = f"profile-pid{os.getppid()}"
    make_profile(home / name)
    assert "still running" in cleanup("remove_fallback", [name], confirm=True)
    assert (home / name).is_dir()


def test_own_fallback_profile_refuses_removal(home):
    name = f"profile-pid{os.getpid()}"
    make_profile(home / name)
    assert "this server process" in cleanup("remove_fallback", [name], confirm=True)
    assert (home / name).is_dir()


def test_symlinked_fallback_is_not_followed(home, tmp_path):
    target = tmp_path / "precious"
    make_profile(target)
    (home / f"profile-pid{dead_pid()}").symlink_to(target)
    assert not [n for n in report() if n.startswith("profile-pid")]
    assert (target / "Default" / "Login Data").exists()


@pytest.mark.parametrize("blocker", ["lock", "chrome"])
def test_profile_in_use_is_left_alone(home, monkeypatch, blocker):
    name = f"profile-pid{dead_pid()}"
    make_profile(home / name)
    if blocker == "lock":
        monkeypatch.setattr(state, "chrome_lock_holder_pid", lambda p: 4242)
    else:
        monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda p: [4242])
    assert "in use" in cleanup("remove_fallback", [name], confirm=True)
    assert "in use" in cleanup("clear_cache", ["profile"], confirm=True)
    assert (home / name).is_dir()
    assert (home / "profile" / "Default" / "Cache" / "blob").exists()


def test_clear_cache_keeps_every_login_artifact(home):
    preview = json.loads(cleanup("clear_cache", ["profile"]))
    assert preview["dry_run"] is True and (home / "profile" / "Default" / "Cache").exists()

    out = json.loads(cleanup("clear_cache", ["profile"], confirm=True))
    assert out["results"][0]["ok"] is True
    default = home / "profile" / "Default"
    assert not (default / "Cache").exists()
    # Sites store things here on purpose, for offline use.
    assert (default / "Service Worker" / "CacheStorage" / "a").exists()
    assert not (home / "profile" / "GrShaderCache").exists()
    assert (default / "Network" / "Cookies").read_bytes() == b"SESSION"
    assert (default / "Login Data").read_bytes() == b"LOGIN"
    assert (default / "Local Storage" / "x").read_bytes() == b"LS"


def test_clear_cache_can_target_one_profile(home):
    cleanup("clear_cache", ["profiles/work"], confirm=True)
    assert (home / "profile" / "Default" / "Cache" / "blob").exists()
    assert not (home / "profiles" / "work" / "Default" / "Cache").exists()


def test_there_is_no_implicit_all(home):
    assert cleanup("clear_cache", []).startswith("Error:")
    assert cleanup("clear_cache", ["*"], confirm=True).startswith("Error:")


def test_unreadable_lock_counts_as_in_use(home):
    """No evidence of a holder is not evidence that it is free."""
    name = f"profile-pid{dead_pid()}"
    make_profile(home / name)
    (home / name / "SingletonLock").symlink_to("garbage-not-a-pid")
    if state.is_chrome_profile_locked(home / name):
        assert "in use" in cleanup("remove_fallback", [name], confirm=True)
        assert (home / name).is_dir()


def test_planted_symlink_cannot_aim_cache_cleanup_outside(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    default = home / "profile" / "Default"
    (default / "Code Cache").symlink_to(outside)
    cleanup("clear_cache", ["profile"], confirm=True)
    assert (outside / "keep").read_text() == "x"


def test_process_match_does_not_cover_sibling_profiles(tmp_path, monkeypatch):
    """A substring match made closing one browser signal every other one."""
    root = tmp_path / ".mcp-stealth"
    listing = "\n".join([
        f"101 chrome --user-data-dir={root}/profile --flag",
        f"102 chrome --user-data-dir={root}/profiles/work --flag",
        f"103 chrome --user-data-dir={root}/profile-pid55",
        f"104 chrome --user-data-dir={root}/profile",
    ])

    class _Result:
        stdout = listing

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    assert state.find_chrome_pids_by_profile(root / "profile") == [101, 104]
    assert state.find_chrome_pids_by_profile(root / "profiles" / "work") == [102]
