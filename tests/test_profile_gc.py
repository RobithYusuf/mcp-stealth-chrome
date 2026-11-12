"""The leftover-profile pass, on folders made for each test. Never the person's own: every
test points MCP_STEALTH_DATA_DIR at tmp_path, and run_once refuses under pytest without it."""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from mcp_stealth_chrome import profile_gc as gc
from mcp_stealth_chrome import state

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows: profile_gc._processes returns None (no ps), and an unreadable process list removes "
    "nothing, so the pass never removes a folder there. These tests describe the POSIX pass."))

DEAD = 99_999_998  # above any PID macOS or Linux hands out


@pytest.fixture(autouse=True)
def _never_the_real_folder(tmp_path, monkeypatch):
    """With MCP_STEALTH_DATA_DIR unset, data_root() falls back to HOME/.mcp-stealth. HOME is
    moved here too, so a test that unsets the variable, or a mutation that removes a guard,
    still lands in tmp_path. A mutation check of the pytest refusal once ran a real pass on
    the person's own ~/.mcp-stealth because only the variable was set."""
    monkeypatch.setattr(state, "HOME", tmp_path / "home")
    assert not str(state.data_root()).startswith(os.path.expanduser("~/.mcp-stealth"))


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "privacy-profiles").mkdir(parents=True)
    monkeypatch.setenv(state.ENV_DATA_DIR, str(data))
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", None)
    for session in state._SESSIONS.values():
        monkeypatch.setattr(session, "current_profile_dir", None)
    return data


def _old(path, days=2):
    when = time.time() - days * 86400
    for base, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(base, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def _profile(parent, name, old=True):
    path = parent / name
    (path / "Default").mkdir(parents=True)
    (path / "Local State").write_text("{}")
    if old:
        _old(path)
    return path


def _db(path, table, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} (host_key TEXT, name TEXT, value TEXT)")
    con.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


def _sleeper(*argv):
    """A live process whose command line carries argv."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])
    time.sleep(0.2)
    return proc


def _names(report, key):
    return sorted(row["name"] for row in report[key])


def test_an_empty_profile_of_a_dead_server_is_removed(root):
    path = _profile(root, f"profile-pid{DEAD}")
    report = gc.run_once(root)
    assert not path.exists() and _names(report, "removed") == [f"profile-pid{DEAD}"], report


@pytest.mark.parametrize("what", ["cookie", "login", "account_login", "history", "autofill", "card",
                                  "top_sites", "sessions", "old_session", "bookmark", "extension",
                                  "indexeddb", "local_storage"])
def test_a_profile_holding_something_a_person_would_miss_stays(root, what):
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    default = path / "Default"
    if what == "cookie":
        _db(default / "Network" / "Cookies", "cookies", [(".example.com", "sid", "SECRET-VALUE")])
    elif what == "login":
        _db(default / "Login Data", "logins", [("https://example.com", "me", "pw")])
    elif what == "account_login":
        _db(default / "Login Data For Account", "logins", [("https://example.com", "me", "pw")])
    elif what == "history":
        _db(default / "History", "urls", [("https://example.com/", "Example", "")])
    elif what == "autofill":
        _db(default / "Web Data", "autofill", [("email", "me@example.com", "")])
    elif what == "card":
        _db(default / "Web Data", "credit_cards", [("4111", "Me", "")])
    elif what == "top_sites":
        _db(default / "Top Sites", "top_sites", [("https://example.com/", "Example", "")])
    elif what == "sessions":
        (default / "Sessions").mkdir()
        (default / "Sessions" / "Session_13400000000000000").write_bytes(b"SNSS" + b"\0" * 60)
    elif what == "old_session":
        (default / "Current Session").write_bytes(b"SNSS" + b"\0" * 60)
    elif what == "bookmark":
        (default / "Bookmarks").write_text(json.dumps(
            {"roots": {"bookmark_bar": {"children": [{"type": "url", "url": "https://example.com"}]}}}))
    elif what == "extension":
        (default / "Extensions" / "abcdefghijklmnop").mkdir(parents=True)
    elif what == "indexeddb":
        (default / "IndexedDB" / "https_example.com_0.indexeddb.leveldb").mkdir(parents=True)
    else:
        (default / "Local Storage" / "leveldb").mkdir(parents=True)
        (default / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"\x01" * 40)
    _old(path)
    report = gc.run_once(root)
    assert path.exists(), report
    assert _names(report, "kept_with_data") == [path.name], report
    assert report["fallback_profiles_left"]["with_data"] == 1


def test_a_cookie_database_that_cannot_be_read_counts_as_data(root):
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    (path / "Default" / "Network").mkdir()
    (path / "Default" / "Network" / "Cookies").write_bytes(b"this is not sqlite" * 100)
    _old(path)
    report = gc.run_once(root)
    assert path.exists() and _names(report, "kept_with_data") == [path.name], report
    assert report["kept_with_data"][0]["readable"] is False


def test_a_locked_profile_stays(root):
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    os.symlink(f"host-{os.getpid()}", path / "SingletonLock")
    _old(path)
    assert path.exists() and gc.run_once(root) and path.exists()
    assert gc.status()["skipped_in_use"][0]["why"].startswith("locked")


def test_a_profile_a_live_process_names_stays(root):
    path = _profile(root, f"profile-pid{DEAD}")
    proc = _sleeper(f"--user-data-dir={path}")
    try:
        report = gc.run_once(root)
    finally:
        proc.kill()
        proc.wait()
    assert path.exists() and "named by running process" in report["skipped_in_use"][0]["why"], report


def test_a_recycled_pid_counts_as_gone_only_when_it_is_not_this_server(root):
    other = _sleeper("unrelated-program")
    server = _sleeper("-m", "mcp_stealth_chrome")
    try:
        recycled = _profile(root, f"profile-pid{other.pid}")
        alive = _profile(root, f"profile-pid{server.pid}")
        report = gc.run_once(root)
    finally:
        for proc in (other, server):
            proc.kill()
            proc.wait()
    assert not recycled.exists() and alive.exists(), report
    assert "still running" in next(r["why"] for r in report["skipped_in_use"] if r["name"] == alive.name)


def test_a_link_is_never_followed(root, tmp_path):
    elsewhere = _profile(tmp_path, "elsewhere")
    old = time.time() - 2 * 86400
    for link in (root / f"profile-pid{DEAD}", root / "privacy-profiles" / "main-abcdefgh"):
        os.symlink(elsewhere, link)
        os.utime(link, (old, old), follow_symlinks=False)  # so only the link check can stop it
    report = gc.run_once(root)
    assert sorted((r["name"], r["why"]) for r in report["skipped"]) == [
        ("privacy-profiles/main-abcdefgh", "not a real folder directly in its root"),
        (f"profile-pid{DEAD}", "not a real folder directly in its root")], report
    assert not report["errors"], report
    assert (root / f"profile-pid{DEAD}").is_symlink() and (root / "privacy-profiles" / "main-abcdefgh").is_symlink()
    assert (elsewhere / "Local State").exists()


def test_a_linked_privacy_root_is_left_alone(root, tmp_path):
    real = tmp_path / "real-privacy"
    _profile(real, "main-abcdefgh")
    (root / "privacy-profiles").rmdir()
    os.symlink(real, root / "privacy-profiles")
    gc.run_once(root)
    assert (real / "main-abcdefgh").exists()


@pytest.mark.parametrize("name", ["profile-pidX", "profile-pid12a", "profile-pid", "Profile-pid12",
                                  "profile-pid12 ", "xprofile-pid12"])
def test_names_that_only_look_alike_are_untouched(root, name):
    path = _profile(root, name)
    gc.run_once(root)
    assert path.exists()


def test_named_and_main_profiles_are_untouched(root):
    for path in (_profile(root, "profile"), _profile(root / "profiles", f"profile-pid{DEAD}"),
                 _profile(root, "app")):
        gc.run_once(root)
        assert path.exists(), path


def test_this_servers_own_fallback_and_its_affinity_stay(root, monkeypatch):
    own = _profile(root, f"profile-pid{os.getpid()}")
    pinned = _profile(root, f"profile-pid{DEAD}")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", pinned)
    report = gc.run_once(root)
    assert own.exists() and pinned.exists(), report


def test_a_profile_changed_moments_ago_stays(root):
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    report = gc.run_once(root)
    assert path.exists() and "changed in the last" in report["skipped_in_use"][0]["why"]


def test_a_leftover_privacy_profile_is_removed(root):
    path = _profile(root / "privacy-profiles", "main-abcdefgh")
    report = gc.run_once(root)
    assert not path.exists() and _names(report, "removed") == ["privacy-profiles/main-abcdefgh"]


def test_a_privacy_profile_this_server_holds_stays(root):
    path = _profile(root / "privacy-profiles", "main-abcdefgh")
    session = state._SESSIONS[state.PRIMARY_SESSION]
    session.current_profile_dir = path
    report = gc.run_once(root)
    assert path.exists() and report["skipped_in_use"][0]["why"] == "held by this server"


def test_a_privacy_profile_in_a_pooled_instance_stays(root):
    path = _profile(root / "privacy-profiles", "work-abcdefgh")
    pool = state._SESSIONS[state.POOL_SESSION].instances
    pool["work"] = state.InstanceSnapshot(instance_id="work", profile_dir=path)
    try:
        gc.run_once(root)
    finally:
        pool.pop("work", None)
    assert path.exists()


def test_a_privacy_profile_a_browser_still_uses_stays(root):
    path = _profile(root / "privacy-profiles", "main-abcdefgh")
    proc = _sleeper(f"--user-data-dir={path}")
    try:
        gc.run_once(root)
    finally:
        proc.kill()
        proc.wait()
    assert path.exists()


def test_an_unreadable_process_list_removes_nothing(root, monkeypatch):
    path = _profile(root, f"profile-pid{DEAD}")
    monkeypatch.setattr(gc, "_processes", lambda: None)
    report = gc.run_once(root)
    assert path.exists() and report["state"] == "done", report
    assert report["skipped"][0]["why"] == "the process list could not be read", report


def test_under_a_test_it_refuses_the_persons_own_folder(monkeypatch, tmp_path):
    """Proved on a stand-in: a folder built like ~/.mcp-stealth takes the protected path's place.
    The person's real folder is never handed to run_once, here or in any mutation of this guard."""
    stand_in = tmp_path / "home" / ".mcp-stealth"
    decoy = _profile(stand_in, f"profile-pid{DEAD}")
    _profile(stand_in / "privacy-profiles", "main-abcdefgh")
    monkeypatch.setattr(gc, "_PROTECTED_UNDER_TEST", stand_in)
    report = gc.run_once(stand_in)
    assert report["state"] == "refused" and not report["removed"] and decoy.exists()


def test_the_report_names_what_it_did_and_holds_no_secret(root):
    keep = _profile(root, f"profile-pid{DEAD}", old=False)
    _db(keep / "Default" / "Network" / "Cookies", "cookies", [(".example.com", "sid", "SECRET-VALUE")])
    _old(keep)
    gone = _profile(root / "privacy-profiles", "main-abcdefgh")
    gc.run_once(root)
    status = gc.status()
    text = json.dumps(status)
    assert status["state"] == "done" and "SECRET-VALUE" not in text and not gone.exists()
    assert "removed 1" in status["summary"] and "kept 1 with data" in status["summary"]
    found = gc.contents(keep)
    assert found["cookies"] == 1 and found["cookie_domains"] == ["example.com"] and "SECRET" not in json.dumps(found)


def test_turning_it_off_runs_nothing(root, monkeypatch):
    path = _profile(root, f"profile-pid{DEAD}")
    monkeypatch.setenv(gc.ENV_SWITCH, "0")
    gc.start_in_background(root)
    time.sleep(0.2)
    assert gc.status()["state"] == "off" and path.exists()


@pytest.mark.parametrize("command, server", [
    ("/Users/x/.local/share/uv/tools/mcp-stealth-chrome/bin/python /Users/x/.local/bin/mcp-stealth-chrome", True),
    ("uvx mcp-stealth-chrome --http", True),
    ("/repo/.venv/bin/python -m mcp_stealth_chrome", True),
    ("/repo/.venv/bin/python -c from mcp_stealth_chrome.server import main; main()", True),
    ("/Users/x/Projects/mcp-stealth-chrome/.venv/bin/python -m pytest tests/", False),
    ("/Users/x/Projects/mcp-stealth-chrome/.venv/bin/python other.py", False),
    ("/opt/mcp_stealth_chrome/venv/bin/python other.py", False),
    ("/usr/bin/sleep 60", False),
])
def test_a_server_is_known_by_how_it_starts(command, server):
    assert gc.is_server_command(command) is server


def test_removal_checks_once_more_that_the_folder_is_still_in_its_root(root):
    """Between the decision and the removal a folder can be replaced or moved."""
    nested = _profile(root / "profiles", "kept")
    assert gc._remove(nested, root) is not None and nested.exists()


def test_a_web_data_file_without_those_tables_is_not_data(root):
    """Chrome versions differ in which autofill tables exist; a missing one has no rows."""
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    _db(path / "Default" / "Web Data", "meta", [("version", "1", "")])
    _old(path)
    report = gc.run_once(root)
    assert not path.exists(), report


def test_an_empty_sessions_folder_is_not_data(root):
    path = _profile(root, f"profile-pid{DEAD}", old=False)
    (path / "Default" / "Sessions").mkdir()
    (path / "Default" / "Sessions" / "Session_1").write_bytes(b"")
    _old(path)
    gc.run_once(root)
    assert not path.exists()
