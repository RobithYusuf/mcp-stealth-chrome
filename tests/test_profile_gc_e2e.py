"""The startup pass in a real server, on a data folder made for this test. Needs MCP_E2E=1.

The server gets MCP_STEALTH_DATA_DIR, so its profiles, fallbacks included, live in tmp_path;
Chrome keeps the real HOME, since a temporary one shows macOS's Keychain dialog.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from _protocol import SAFE_ARGS, Client

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

DEAD = 99_999_997


def _old(path):
    when = time.time() - 3 * 86400
    for base, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(base, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def _profile(path, cookies=()):
    (path / "Default" / "Network").mkdir(parents=True)
    (path / "Local State").write_text("{}")
    if cookies:
        con = sqlite3.connect(path / "Default" / "Network" / "Cookies")
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
        con.executemany("INSERT INTO cookies VALUES (?, ?, ?)", cookies)
        con.commit()
        con.close()
    _old(path)
    return path


def _wait_done(client, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.call("server_status").json()["profile_gc"]
        if status.get("state") in ("done", "failed", "refused"):
            return status
        time.sleep(0.3)
    raise AssertionError(f"the pass did not finish: {status}")


def test_a_real_server_removes_exactly_the_safe_leftovers(tmp_path):
    data = tmp_path / "data"
    keeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        empty_dead = _profile(data / f"profile-pid{DEAD}")
        with_cookie = _profile(data / f"profile-pid{DEAD - 1}", [(".example.com", "sid", "SECRET-VALUE")])
        live_owner = _profile(data / f"profile-pid{keeper.pid}")          # alive, not a server
        lookalike = _profile(data / "profile-pid12a")
        named = _profile(data / "profiles" / "work")
        main = _profile(data / "profile")
        privacy_left = _profile(data / "privacy-profiles" / "main-abcdefgh")
        os.symlink(f"host-{keeper.pid}", privacy_locked := _profile(data / "privacy-profiles" / "main-locked1") / "SingletonLock")
        client = Client(env={"MCP_STEALTH_DATA_DIR": str(data), "MCP_STEALTH_PROFILE_GC": "1",
                             "MCP_STEALTH_PROFILE_GC_DELAY": "0.2"})
        try:
            status = _wait_done(client)
        finally:
            client.close()
    finally:
        keeper.kill()
        keeper.wait()
    assert status["state"] == "done", status
    # The PID of a running program that is not this server counts as gone, so its empty
    # fallback goes; the one with a cookie stays whatever its owner.
    gone = {empty_dead, live_owner, privacy_left}
    for path in gone:
        assert not path.exists(), (path, status)
    for path in (with_cookie, lookalike, named, main, privacy_locked.parent):
        assert path.exists(), (path, status)
    assert [r["name"] for r in status["kept_with_data"]] == [with_cookie.name], status
    assert status["fallback_profiles_left"]["with_data"] == 1
    assert "SECRET-VALUE" not in json.dumps(status)


def test_a_persistent_launch_that_falls_back_still_works_and_is_not_removed(tmp_path):
    """The shared profile is locked by another process, so the launch takes profile-pid<N>.
    The pass runs while that browser is up and must leave it alone."""
    data = tmp_path / "data"
    (data / "profile").mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    os.symlink(f"host-{holder.pid}", data / "profile" / "SingletonLock")
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data), "MCP_STEALTH_PROFILE_GC": "1",
                         "MCP_STEALTH_PROFILE_GC_DELAY": "4", "MCP_STEALTH_PROFILE_GC_MIN_AGE": "0"})
    try:
        reply = client.call("browser_launch", _timeout=90, url="about:blank", headless=True,
                            persistent=True, extra_args=SAFE_ARGS)
        assert not reply.failed, reply.text
        status = client.call("server_status").json()
        used = status["profile_dir"]
        assert os.path.basename(used).startswith("profile-pid") and used.startswith(str(data)), used
        assert client.call("server_status").json()["profile_gc"]["state"] == "not run", "ran before the launch"
        gc = _wait_done(client)
        assert os.path.isdir(used), gc
        mine = next(r for r in gc["skipped_in_use"] if r["name"] == os.path.basename(used))
        assert mine["why"] in ("held by this server",) or mine["why"].startswith("locked"), mine
        assert client.evaluate("1 + 1") == "2"
        assert not client.call("browser_close", _timeout=40).failed
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        holder.kill()
        holder.wait()
    assert os.path.isdir(data / "profile") and os.path.islink(data / "profile" / "SingletonLock")
