"""A profile Chrome 157 wrote keeps its cookies when Chrome 158 opens it.

Chrome 158 on macOS deletes the v10 cookies Chrome 157 stores in a profile outside its default
folder. Google Chrome here is still 157, so Chrome Beta 158 stands in for it: a `google-chrome`
script on the server's PATH runs Beta, and nodriver looks there first. Profiles are temporary
folders made by Google Chrome 157 with the real Keychain, as a person's are; the server keeps its
backups in a temporary MCP_STEALTH_DATA_DIR. Needs MCP_E2E=1, macOS and Chrome Beta 158 or newer.
"""
import asyncio
import functools
import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
from urllib.parse import quote

import pytest

from _protocol import LOOPBACK_ONLY, Client
from mcp_stealth_chrome import cookie_migration, dock_icon, profile_version, server

BETA = dock_icon.BETA_APP / dock_icon.BETA_BINARY
pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome"),
    pytest.mark.skipif(sys.platform != "darwin", reason="the protection exists on macOS only"),
    pytest.mark.skipif(not BETA.is_file() or (profile_version.binary_version(str(BETA)) or (0,))[0] < 158,
                       reason="needs Chrome Beta 158 or newer as the stand-in for Chrome 158"),
]

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
NAMES = ["a", "b", "c", "d", "e"]
ARGS = [LOOPBACK_ONLY]  # the real Keychain, so cookies are v10 as in a person's profile


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _rows(profile: pathlib.Path) -> list:
    """(name, prefix) of each stored cookie, read without writing anything."""
    database = profile / "Default" / "Cookies"
    with sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True) as db:
        return sorted(db.execute("SELECT name, substr(encrypted_value, 1, 3) FROM cookies").fetchall())


@pytest.fixture
def world(tmp_path):
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # nodriver takes the shortest of the Chromes it finds, so the script's path must be shorter
    # than /Applications/Google Chrome.app/Contents/MacOS/Google Chrome.
    wrapper_dir = pathlib.Path(tempfile.mkdtemp(prefix="c", dir="/tmp"))
    wrapper = wrapper_dir / "chrome"
    wrapper.write_text(f'#!/bin/sh\nexec "{BETA}" "$@"\n')
    wrapper.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    world = {
        "page": f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html",
        "wrapper": str(wrapper),
        "data": data,
        "chrome158": {"PATH": f"{wrapper_dir}:{os.environ['PATH']}", "MCP_STEALTH_DATA_DIR": str(data)},
        "profile": pathlib.Path(tempfile.mkdtemp(prefix="v10-profile-")),
    }
    yield world
    httpd.shutdown()
    shutil.rmtree(world["profile"], ignore_errors=True)
    shutil.rmtree(wrapper_dir, ignore_errors=True)


def _profile_from_chrome_157(world) -> list:
    """Google Chrome 157 makes the profile and stores five cookies in it, all v10."""
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(world["data"])})
    try:
        launched = client.call("browser_launch", _timeout=120, url=world["page"], headless=True,
                               user_data_dir=str(world["profile"]), extra_args=ARGS)
        assert not launched.failed, launched.text
        assert "Chrome/157." in client.evaluate("navigator.userAgent")
        client.evaluate(f"{json.dumps(NAMES)}.forEach(n => document.cookie = n + '=v-' + n + "
                        "'; max-age=86400; path=/'), 'ok'")
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
    rows = _rows(world["profile"])
    assert rows == [(n, b"v10") for n in NAMES], rows
    return rows


def _launch_as_158(world, **kw):
    client = Client(env=world["chrome158"])
    launched = client.call("browser_launch", _timeout=180, url=world["page"], headless=True,
                           user_data_dir=str(world["profile"]), extra_args=ARGS, **kw)
    return client, launched


def _cookies_on_page(client) -> list:
    return sorted(client.evaluate("document.cookie").split("; "))


def test_chrome_158_moves_the_cookies_and_loses_none(world):
    _profile_from_chrome_157(world)
    client, launched = _launch_as_158(world)
    try:
        assert not launched.failed, launched.text
        ua = client.evaluate("navigator.userAgent")
        assert int(ua.split("Chrome/")[1].split(".")[0]) >= 158, ua  # Beta moves on: 158, then 159
        assert "migrated 5 cookies to Chrome 158 encryption; backup at " in launched.text, launched.text
        backup = pathlib.Path(launched.text.split("backup at ", 1)[1].split("]", 1)[0])
        assert backup.parent == world["data"] / "storage-states"
        assert "pre-v11-migration" in backup.name and world["profile"].name in backup.name
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        saved = json.loads(backup.read_text())
        assert sorted(c["name"] for c in saved["cookies"]) == NAMES  # what storage_state_load reads
        assert _cookies_on_page(client) == [f"{n}=v-{n}" for n in NAMES]
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert _rows(world["profile"]) == [(n, b"v11") for n in NAMES], "now in Chrome 158's encryption"
        assert cookie_migration.read_marker(world["profile"])["state"] == "done"

        again = client.call("browser_launch", _timeout=120, url=world["page"], headless=True,
                            user_data_dir=str(world["profile"]), extra_args=ARGS)
        assert not again.failed and "cookies" not in again.text.split("\n", 1)[0], again.text
        assert _cookies_on_page(client) == [f"{n}=v-{n}" for n in NAMES]
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def _config(world) -> server.Config:
    config = server.Config(user_data_dir=str(world["profile"]), headless=True,
                           browser_args=["--profile-directory=Default", *ARGS])
    config.browser_executable_path = world["wrapper"]
    return config


@pytest.fixture
def backups_in_temp(world, monkeypatch):
    target = world["data"] / "storage-states"
    target.mkdir(exist_ok=True)
    monkeypatch.setattr(server, "STORAGE_STATE_DIR", target)
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    return target


def test_a_move_that_fails_after_reading_loses_nothing(world, backups_in_temp, monkeypatch):
    """Steps 1 and 2 done, then a failure before the backup is complete: the profile is still
    v10, this launch would run with the flag, and the next launch moves every cookie."""
    _profile_from_chrome_157(world)

    def broken(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(cookie_migration, "backup_document", broken)
    move = asyncio.run(server._prepare_cookie_move(world["profile"], _config(world)))
    assert move.flag and move.backup is None and "disk full (simulated)" in move.note, move
    assert _rows(world["profile"]) == [(n, b"v10") for n in NAMES], "nothing was lost or rewritten"
    assert cookie_migration.read_marker(world["profile"]) == {}
    monkeypatch.undo()

    client, launched = _launch_as_158(world)
    try:
        assert "migrated 5 cookies to Chrome 158 encryption" in launched.text, launched.text
        assert _cookies_on_page(client) == [f"{n}=v-{n}" for n in NAMES]
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_a_move_cut_off_after_its_backup_is_finished_by_the_next_launch(world, backups_in_temp):
    """Everything up to the pending marker, then no launch (a crash, a killed server): the next
    launch opens without the flag, which drops the v10 rows, and sets the cookies from the backup."""
    _profile_from_chrome_157(world)
    move = asyncio.run(server._prepare_cookie_move(world["profile"], _config(world)))
    assert not move.flag and move.backup is not None and move.backup.is_file(), move
    assert cookie_migration.read_marker(world["profile"])["state"] == "pending"
    assert _rows(world["profile"]) == [(n, b"v10") for n in NAMES], "the reading Chrome changed nothing"

    client, launched = _launch_as_158(world)
    try:
        assert not launched.failed, launched.text
        assert f"migrated 5 cookies to Chrome 158 encryption; backup at {move.backup}" in launched.text
        assert _cookies_on_page(client) == [f"{n}=v-{n}" for n in NAMES]
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert _rows(world["profile"]) == [(n, b"v11") for n in NAMES]
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_spawn_browser_moves_the_cookies_too(world):
    _profile_from_chrome_157(world)
    client = Client(env=world["chrome158"])
    try:
        spawned = client.call("spawn_browser", _timeout=180, instance_id="moved", url=world["page"],
                              headless=True, profile_dir=str(world["profile"]), extra_args=ARGS)
        assert not spawned.failed and "migrated 5 cookies" in spawned.text, spawned.text
        assert not client.call("close_instance", _timeout=40, instance_id="moved").failed
        assert _rows(world["profile"]) == [(n, b"v11") for n in NAMES]
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_a_flag_that_stops_working_costs_no_cookie(world, backups_in_temp, monkeypatch):
    """Should a later Chrome drop the kill switch, the reading Chrome 158 deletes the v10
    cookies it cannot read. The databases copied beforehand are put back, and nothing moves."""
    _profile_from_chrome_157(world)
    monkeypatch.setattr(server, "_cookie_flag_arg", lambda _config: "--disable-features=NoSuchFeature")
    move = asyncio.run(server._prepare_cookie_move(world["profile"], _config(world)))
    assert move.flag and move.backup is None, move
    assert "read 0 of the 5 cookies" in move.note and "put back" in move.note, move.note
    assert _rows(world["profile"]) == [(n, b"v10") for n in NAMES], "restored from the copy"
    assert cookie_migration.read_marker(world["profile"]) == {}
