"""Moving cookies to Chrome 158's encryption: the decisions and the bookkeeping, with fakes.
`test_cookie_migration_e2e.py` does it with a real Chrome."""
import asyncio
import contextlib
import shutil
import json
import os
import plistlib
import sqlite3
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import cookie_migration as cm, server

_NOW_CHROME = int((time.time() + 11_644_473_600) * 1_000_000)


def _chrome(folder: Path, version: str) -> str:
    app = folder / "Google Chrome.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    binary = app / "Contents" / "MacOS" / "Google Chrome"
    binary.write_text("chrome")
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    return str(binary)


def _mark(profile: Path, backup: Path, sub: str = "Default", **extra) -> None:
    """A pending marker as this version writes it, for a backup already on disk."""
    cookies = json.loads(backup.read_text())["cdp_cookies"]
    cm.write_marker(profile, {"state": "pending", "version": cm.MARKER_VERSION, "subprofile": sub,
                              "profile": os.path.realpath(profile), "backup": str(backup),
                              "digest": cm.file_digest(backup), "count": len(cookies), **extra})


def _names(profile: Path, sub: str = "Default") -> list:
    """(name, prefix) of every row in the sub-profile's cookie databases."""
    out = []
    for database in (profile / sub / "Network" / "Cookies", profile / sub / "Cookies"):
        if database.is_file():
            with contextlib.closing(sqlite3.connect(database)) as db:
                out += db.execute("SELECT name, substr(encrypted_value, 1, 3) FROM cookies").fetchall()
    return sorted(out)


def _profile(folder: Path, *rows, sub: str = "Default", network: bool = False) -> Path:
    """rows: (prefix, persistent, expires_in_seconds)."""
    target = folder / sub / ("Network" if network else "")
    target.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target / "Cookies")
    db.execute("CREATE TABLE IF NOT EXISTS cookies (name TEXT, encrypted_value BLOB, is_persistent INTEGER, "
               "expires_utc INTEGER, host_key TEXT, path TEXT)")
    start = db.execute("SELECT count(*) FROM cookies").fetchone()[0]
    for i, (prefix, persistent, expires_in) in enumerate(rows, start):
        expires = 0 if expires_in is None else _NOW_CHROME + expires_in * 1_000_000
        db.execute("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
                   (f"c{i}", prefix + b"x", persistent, expires, "x.test", "/"))
    db.commit()
    db.close()
    # A Chrome profile has a Local State; without one the launch's binding cannot tell which
    # browser made the folder and refuses it (brave.binding_refusal).
    if not (folder / "Local State").exists():
        (folder / "Local State").write_text('{"profile": {}}')
    return folder


@pytest.fixture
def chromes(tmp_path, monkeypatch):
    monkeypatch.setattr(cm.sys, "platform", "darwin")
    monkeypatch.setattr(cm, "default_data_dirs", lambda product="chrome": [tmp_path / "default-chrome"])
    return SimpleNamespace(c157=_chrome(tmp_path / "157", "157.0.8103.39"),
                           c158=_chrome(tmp_path / "158", "158.0.8127.18"))


ARGS = ["--profile-directory=Default"]


def test_it_applies_from_chrome_158_on_macos_only(chromes, monkeypatch):
    assert cm.applies(chromes.c158) and not cm.applies(chromes.c157)
    assert not cm.applies(None)
    monkeypatch.setattr(cm.sys, "platform", "linux")
    assert not cm.applies(chromes.c158)


def test_a_profile_with_v10_cookies_is_moved_by_chrome_158(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v10", 1, 3600), (b"v10", 0, None))
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert (plan.kind, plan.rows, plan.v10, plan.subprofile) == ("migrate", 2, 3, "Default")
    assert cm.plan(profile, ARGS, chromes.c157).kind == "none", "Chrome 157 reads v10"


@pytest.mark.parametrize("case", ["no_profile", "missing", "no_cookies", "only_v11", "default_dir"])
def test_nothing_to_move(tmp_path, chromes, case):
    profile = {
        "no_profile": None,
        "missing": tmp_path / "missing",
        "no_cookies": tmp_path / "empty",
        "only_v11": _profile(tmp_path / "v11", (b"v11", 1, 3600)),
        "default_dir": _profile(tmp_path / "default-chrome", (b"v10", 1, 3600)),
    }[case]
    if case == "no_cookies":
        profile.mkdir()
    assert cm.plan(profile, ARGS, chromes.c158).kind == "none"


def test_the_subprofile_launched_is_the_one_judged(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    _profile(profile, (b"v10", 1, 3600), sub="Profile 1", network=True)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "none"
    assert cm.plan(profile, ["--profile-directory=Profile 1"], chromes.c158).kind == "migrate"


def test_an_unreadable_database_refuses_the_launch(tmp_path, chromes):
    """Neither way of opening it is safe when what it holds is unknown: Chrome 158 deletes v10
    cookies without the flag and v11 cookies with it (Beta 159: 3 v11 cookies, opened with the
    flag, read 0 and left 0 rows)."""
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"not a database")
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse" and "could not be read" in plan.reason, plan


def _die_inside_a_transaction(database: Path, statements: str) -> None:
    """A writer that dies (SIGKILL) with its transaction open, as a killed Chrome does. A cache
    of one page makes SQLite spill the uncommitted pages into the file, behind a hot journal."""
    code = (f"import os, signal, sqlite3\n"
            f"c = sqlite3.connect({str(database)!r}, isolation_level=None)\n"
            f"c.execute('PRAGMA cache_size=1')\n"
            f"{statements}\n"
            f"os.kill(os.getpid(), signal.SIGKILL)\n")
    import subprocess
    subprocess.run([sys.executable, "-c", code], check=False)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL does not exist on Windows; the move runs on macOS only")
def test_a_hot_journal_is_rolled_back_before_the_cookies_are_counted(tmp_path, chromes):
    """Chrome's cookie database uses a rollback journal. A Chrome killed in the middle of a
    write leaves pages that were never committed; read as they lie (immutable=1), a deletion
    that never happened looked like a profile with nothing to move."""
    profile = _profile(tmp_path / "p", *[(b"v10", 1, 3600)] * 40)
    database = profile / "Default" / "Cookies"
    _die_inside_a_transaction(database, (
        "c.execute('BEGIN')\nc.execute('DELETE FROM cookies')\n"
        "c.execute('CREATE TABLE filler (x BLOB)')\n"
        "[c.execute('INSERT INTO filler VALUES (zeroblob(4000))') for _ in range(200)]"))
    assert Path(str(database) + "-journal").stat().st_size > 0, "the test needs a hot journal"
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert (plan.kind, plan.v10, plan.rows) == ("migrate", 40, 40), plan
    assert Path(str(database) + "-journal").stat().st_size > 0, "the profile itself was not rolled back"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL does not exist on Windows; the move runs on macOS only")
def test_cookies_only_in_the_write_ahead_log_are_counted(tmp_path, chromes):
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    database = profile / "Default" / "Cookies"
    db = sqlite3.connect(database)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE cookies (name TEXT, encrypted_value BLOB, is_persistent INTEGER, "
               "expires_utc INTEGER, host_key TEXT, path TEXT)")
    db.commit()
    db.close()
    _die_inside_a_transaction(database, (
        f"c.execute(\"INSERT INTO cookies VALUES ('a', CAST('v10x' AS BLOB), 1, {_NOW_CHROME + 10**9}, "
        f"'x.test', '/')\")"))
    assert Path(str(database) + "-wal").stat().st_size > 0
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert (plan.kind, plan.v10) == ("migrate", 1), plan


def test_a_database_written_while_it_is_copied_is_not_trusted(tmp_path, chromes, monkeypatch):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    database = profile / "Default" / "Cookies"
    real = cm.cookie_db.shutil.copyfile

    def copy_while_written(source, target, *a, **kw):
        out = real(source, target, *a, **kw)
        if str(source) == str(database):
            _profile(profile, (b"v10", 1, 3600))  # another Chrome writes in the meantime
        return out

    monkeypatch.setattr(cm.cookie_db.shutil, "copyfile", copy_while_written)
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse" and "changing" in plan.reason, plan


def test_a_profile_with_v10_and_v11_cookies_is_moved_and_never_flagged(tmp_path, chromes):
    """The flag deletes the v11 cookies, so a profile holding both is moved again even when the
    same Chrome moved it before."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v11", 1, 3600))
    cm.write_marker(profile, {"state": "done", "major": 158})
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert (plan.kind, plan.v10, plan.v11) == ("migrate", 1, 1), plan


def test_a_pending_move_whose_backup_lacks_a_v10_cookie_starts_over(tmp_path, chromes):
    """Resuming opens without the flag, which deletes every v10 cookie: each one must be in the
    backup, or the move is made again from the start."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v10", 1, 3600))
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"cdp_cookies": [{"name": "c0", "domain": "x.test", "path": "/"}]}))
    _mark(profile, backup)
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "migrate" and "c1@x.test" in plan.reason, plan
    backup.write_text(json.dumps({"cdp_cookies": [{"name": f"c{i}", "domain": "x.test", "path": "/"}
                                                  for i in range(2)]}))
    _mark(profile, backup)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"


def test_a_pending_move_with_its_backup_is_resumed(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    backup = _pending(profile, tmp_path)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"
    backup.unlink()
    assert cm.plan(profile, ARGS, chromes.c158).kind == "refuse", "no backup: never a new move over a pending one"


def test_v10_written_again_by_the_same_chrome_does_not_loop(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    cm.write_marker(profile, {"state": "done", "major": 158})
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "flag" and "again" in plan.reason
    cm.write_marker(profile, {"state": "done", "major": 157})
    assert cm.plan(profile, ARGS, chromes.c158).kind == "migrate"


@pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows keeps no POSIX mode bits (stat reports 0o666), so 0600 cannot be checked there; "
    "the migration itself only runs on macOS."))
def test_files_are_private(tmp_path):
    cm.write_marker(tmp_path, {"state": "pending"})
    assert stat.S_IMODE((tmp_path / cm.MARKER).stat().st_mode) == 0o600


def test_databases_are_copied_verified_and_private(tmp_path):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    _profile(profile, (b"v11", 1, 3600), network=True)
    target = tmp_path / "copy"
    copied = cm.copy_databases(profile, "Default", target)
    assert sorted(Path(c).as_posix() for c in copied) == ["Default/Cookies", "Default/Network/Cookies"]
    for name in copied:
        assert (target / name).read_bytes() == (profile / name).read_bytes()
        if sys.platform != "win32":
            assert stat.S_IMODE((target / name).stat().st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_a_copy_taken_while_the_database_is_written_is_refused(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    real = cm.shutil.copyfileobj

    def copy_while_written(src, out, *a, **kw):
        real(src, out, *a, **kw)
        _profile(profile, (b"v10", 1, 3600))  # a writer in the meantime

    monkeypatch.setattr(cm.shutil, "copyfileobj", copy_while_written)
    with pytest.raises(cm.cookie_db.Changed):
        cm.copy_databases(profile, "Default", tmp_path / "copy")


def test_a_damaged_copy_is_refused(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "p", *[(b"v10", 1, 3600)] * 50)
    real = cm.shutil.copyfileobj

    def damaging(src, out, *a, **kw):
        real(src, out, *a, **kw)
        out.seek(4096)
        out.write(b"\xff" * 4096)  # a torn page

    monkeypatch.setattr(cm.shutil, "copyfileobj", damaging)
    with pytest.raises(cm.cookie_db.Unreadable):
        cm.copy_databases(profile, "Default", tmp_path / "copy")

def test_cookie_params_keep_every_attribute_chrome_reports():
    cookie = {"name": "n", "value": "v", "domain": ".x.test", "path": "/p", "expires": 2e9, "size": 2,
              "httpOnly": True, "secure": True, "session": False, "sameSite": "Lax", "priority": "High",
              "sourceScheme": "Secure", "sourcePort": 443,
              "partitionKey": {"topLevelSite": "https://top.test", "hasCrossSiteAncestor": False}}
    params = cm.cookie_params(cookie)
    assert params == {k: cookie[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly",
                                            "priority", "sourceScheme", "sourcePort", "sameSite",
                                            "expires", "partitionKey")}
    assert "expires" not in cm.cookie_params({**cookie, "session": True, "expires": -1})
    assert cm.key(cookie) == ("n", ".x.test", "/p", "https://top.test")


def test_the_backup_is_a_storage_state_file():
    document = cm.backup_document([{"name": "n", "value": "v", "domain": "d", "path": "/",
                                    "expires": 1, "secure": True, "httpOnly": True}], Path("/p"), "/c")
    assert document["cookies"] == [{"name": "n", "value": "v", "domain": "d", "path": "/",
                                    "expires": 1, "secure": True, "http_only": True}]
    assert document["origins"] == {} and len(document["cdp_cookies"]) == 1


# ── the server side ──────────────────────────────────────────────────────────

class _Connection:
    """Answers Storage.getCookies with what was set, minus whatever `drop` names."""

    def __init__(self, drop=()):
        self.cookies, self.drop, self.calls = [], set(drop), []

    async def send(self, generator):
        request = next(generator)
        self.calls.append(request["method"])
        if request["method"] == "Storage.setCookies":
            self.cookies += [c for c in request["params"]["cookies"] if c["name"] not in self.drop]
            answer = {}
        else:
            answer = {"cookies": list(self.cookies)}
        try:
            generator.send(answer)
        except StopIteration as done:
            return done.value


def _move(tmp_path, names):
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    backup = tmp_path / "backup.json"
    cookies = [{"name": n, "value": "v", "domain": "x.test", "path": "/"} for n in names]
    backup.write_text(json.dumps(cm.backup_document(cookies, profile, "/c")))
    cm.write_marker(profile, {"state": "pending", "backup": str(backup)})
    return profile, server._CookieMove(plan=None, backup=backup)


def test_finishing_sets_reads_back_and_marks_done(tmp_path):
    profile, move = _move(tmp_path, ["a", "b"])
    connection = _Connection()
    moved, text = asyncio.run(server._finish_cookie_move(SimpleNamespace(connection=connection), profile, move))
    assert moved and f"migrated 2 cookies to Chrome 158 encryption; backup at {move.backup}" in text
    assert connection.calls == ["Storage.setCookies", "Storage.getCookies"]
    assert cm.read_marker(profile)["state"] == "done"


def test_a_cookie_that_did_not_come_back_is_an_error_and_the_move_stays_pending(tmp_path):
    profile, move = _move(tmp_path, ["a", "b", "c"])
    moved, text = asyncio.run(server._finish_cookie_move(
        SimpleNamespace(connection=_Connection(drop={"b"})), profile, move))
    assert not moved
    assert "2 of 3 cookies came back" in text and "b@x.test" in text
    assert f"storage_state_load({str(move.backup)!r})" in text
    assert cm.read_marker(profile)["state"] == "pending"


def test_the_flag_reaches_chrome_when_the_move_says_so(monkeypatch):
    from test_focus_restore import _fake_launch, _launch
    order = _fake_launch(monkeypatch)

    async def prepare(_profile, _config):
        return server._CookieMove(plan=None, flag=True, note=" [cookies: test]")

    monkeypatch.setattr(server, "_prepare_cookie_move", prepare)
    out = _launch(headless=True)
    flags = next(item[1] for item in order if isinstance(item, tuple) and item[0] == "flags")
    disabled = [f for f in flags if f.startswith("--disable-features=")]
    assert disabled and cm.FEATURE in disabled[-1].split("=", 1)[1].split(","), flags
    assert "[cookies: test]" in out


def test_no_flag_without_a_reason(monkeypatch):
    from test_focus_restore import _fake_launch, _launch
    order = _fake_launch(monkeypatch)
    _launch(headless=True)
    flags = next(item[1] for item in order if isinstance(item, tuple) and item[0] == "flags")
    assert not any(cm.FEATURE in f for f in flags), flags


# ── the last look before Chrome starts ───────────────────────────────────────

def _launch_on(profile: Path, **kw) -> str:
    fn = server.mcp._tool_manager._tools["browser_launch"].fn
    try:
        return asyncio.run(fn(user_data_dir=str(profile), headless=True, auto_verify=False, **kw))
    finally:
        server.BrowserState.reset()


def _spawn_on(profile: Path) -> tuple:
    try:
        return asyncio.run(server._launch_browser_instance(
            instance_id="inst", url="about:blank", headless=True, proxy=None, user_agent=None,
            window_width=800, window_height=600, persistent=True, lang="en-US", extra_args=None,
            storage_state_path=None, idle_timeout=0, profile_dir_override=str(profile)))
    finally:
        server.BrowserState.reset()


@pytest.fixture
def decided(monkeypatch, chromes):
    """Launches whose cookie decision is made as for Chrome 158, with `after` run between the
    decision and the start, where another program could act."""
    from test_focus_restore import _fake_launch
    order = _fake_launch(monkeypatch)
    real = server._prepare_cookie_move
    hooks = SimpleNamespace(after=lambda profile: None, order=order)

    async def prepare(profile_path, config):
        config.browser_executable_path = chromes.c158
        move = await real(profile_path, config)
        hooks.after(Path(profile_path))
        return move

    monkeypatch.setattr(server, "_prepare_cookie_move", prepare)
    # spawn_browser looks for a Chrome before anything else; a CI runner may have none.
    monkeypatch.setattr(server, "find_chrome_binary", lambda: chromes.c158)
    return hooks


@pytest.mark.parametrize("launch", [_launch_on, _spawn_on])
def test_a_v10_cookie_arriving_after_the_check_stops_the_launch(tmp_path, decided, launch):
    """Decided "nothing to move" on a profile of v11 cookies, then a v10 cookie is written:
    started without the flag, Chrome 158 would delete it."""
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    decided.after = lambda p: _profile(p, (b"v10", 1, 3600))
    out = launch(profile)
    text = out if isinstance(out, str) else out[1]
    assert "refused to open" in text and "changed after this launch read it" in text, text
    assert "start" not in decided.order


def test_a_chrome_that_takes_the_profile_after_the_check_stops_the_launch(tmp_path, decided, monkeypatch):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    from mcp_stealth_chrome import state
    decided.after = lambda p: monkeypatch.setattr(state, "chrome_lock_state", lambda _p: ("held", 4242))
    out = _launch_on(profile)
    assert out.startswith("Error: refused to open") and "Chrome PID 4242 opened it" in out, out
    assert "start" not in decided.order


def test_a_profile_left_as_it_was_decided_starts(tmp_path, decided):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    out = _launch_on(profile)
    assert "launched" in out and not out.startswith("Error"), out
    assert "start" in decided.order


def test_an_unreadable_cookie_database_starts_nothing(tmp_path, decided):
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"not a database")
    (profile / "Local State").write_text('{"profile": {}}')  # a Chrome profile, as the binding sees it
    out = _launch_on(profile)
    assert out.startswith("Error: refused to open") and "could not be read" in out, out
    assert "start" not in decided.order


def test_a_failed_move_of_a_profile_holding_v11_cookies_never_uses_the_flag(tmp_path):
    """The flag keeps v10 cookies and deletes v11 ones, so a move that fails beside v11 cookies
    refuses the launch rather than flag it."""
    mixed = cm.Plan("migrate", rows=2, v10=1, v11=1)
    move = server._cookie_move_failed(mixed, tmp_path, "simulated")
    assert not move.flag and "refused to open" in move.refuse and "1 v11 cookies" in move.refuse, move
    only_v10 = cm.Plan("migrate", rows=1, v10=1)
    move = server._cookie_move_failed(only_v10, tmp_path, "simulated")
    assert move.flag and not move.refuse and "simulated" in move.note


def test_a_resumed_move_takes_the_v10_rows_out_from_beside_v11_ones(tmp_path, chromes):
    """Chrome 158 reads nothing from a store holding both kinds and deletes every row (Beta 159:
    5 v10 and 2 v11 of one host, 0 left), so resuming leaves the v11 rows alone in it."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v11", 1, 3600))
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"cdp_cookies": [{"name": "c0", "domain": "x.test", "path": "/"}]}))
    cm.copy_databases(profile, "Default", tmp_path / "copy")
    _mark(profile, backup, database_copy=str(tmp_path / "copy"))
    config = server.Config(user_data_dir=str(profile), headless=True, browser_args=ARGS)
    config.browser_executable_path = chromes.c158
    move = asyncio.run(server._prepare_cookie_move(profile, config))
    assert move.backup == backup and move.strip and not move.flag and not move.refuse, move
    assert _names(profile) == [("c0", b"v10"), ("c1", b"v11")], "preparing writes nothing"
    assert asyncio.run(server._cookie_preflight(profile, move)) == ""
    assert asyncio.run(server._commit_cookie_move(profile, move)) == ""
    assert _names(profile) == [("c1", b"v11")]


# ── how long the plaintext backups stay ──────────────────────────────────────

def _backup(folder: Path, profile: Path, name: str, saved_at: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cm.BACKUP_PREFIX}-{name}.json"
    path.write_text(json.dumps({"cookies": [{"name": "sid", "value": "secret"}], "saved_at": saved_at,
                                "pre_v11_migration": {"profile": str(profile)}}))
    path.chmod(0o644)
    db_copy = folder / f"{cm.BACKUP_PREFIX}-{name}-db" / "Default"
    db_copy.mkdir(parents=True)
    (db_copy / "Cookies").write_bytes(b"copy")
    return path


def test_a_backup_goes_once_its_move_is_done_and_the_retention_has_passed(tmp_path):
    day = 86400
    now = time.time()
    store = tmp_path / "storage-states"
    old_done, recent_done, pending = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for profile in (old_done, recent_done, pending):
        profile.mkdir()
    cm.write_marker(old_done, {"state": "done", "finished_at": now - 15 * day})
    cm.write_marker(recent_done, {"state": "done", "finished_at": now - 2 * day})
    cm.write_marker(pending, {"state": "pending"})
    gone = _backup(store, old_done, "a-1", now - 15 * day - 60)
    kept = [_backup(store, recent_done, "b-1", now - 2 * day - 60),
            _backup(store, pending, "c-1", now - 40 * day),
            _backup(store, tmp_path / "missing", "d-1", now - 40 * day)]
    report = cm.expire_backups(store, now=now, keep_days=14)
    assert not gone.exists() and not (store / f"{gone.stem}-db").exists(), report
    assert [p.name for p in report["removed"]] == [gone.name], report
    for path in kept:
        assert path.exists() and (store / f"{path.stem}-db").exists(), path
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, "kept, but private"


def test_an_older_backup_of_a_profile_moved_again_goes_too(tmp_path):
    now = time.time()
    store = tmp_path / "storage-states"
    profile = tmp_path / "p"
    profile.mkdir()
    cm.write_marker(profile, {"state": "done", "finished_at": now - 20 * 86400})
    first = _backup(store, profile, "p-1", now - 60 * 86400)
    cm.expire_backups(store, now=now, keep_days=14)
    assert not first.exists()


def test_expiry_never_follows_a_link_or_touches_other_files(tmp_path):
    now = time.time()
    store = tmp_path / "storage-states"
    profile = tmp_path / "p"
    profile.mkdir()
    cm.write_marker(profile, {"state": "done", "finished_at": now - 30 * 86400})
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"saved_at": 0, "pre_v11_migration": {"profile": str(profile)}}))
    store.mkdir()
    try:
        (store / f"{cm.BACKUP_PREFIX}-link.json").symlink_to(elsewhere)
    except OSError:
        pytest.skip("this account cannot make a symbolic link")
    (store / "my-session.json").write_text(json.dumps({"saved_at": 0, "pre_v11_migration": {"profile": str(profile)}}))
    cm.expire_backups(store, now=now, keep_days=14)
    assert elsewhere.exists() and (store / f"{cm.BACKUP_PREFIX}-link.json").is_symlink()
    assert (store / "my-session.json").exists(), "only this module's backups"


def test_expiry_refuses_the_persons_own_folder_under_a_test(monkeypatch, tmp_path):
    """Proved on a stand-in built like the real folder, never by running against the real one."""
    stand_in = tmp_path / "home" / ".mcp-stealth"
    monkeypatch.setattr(cm, "_PROTECTED_UNDER_TEST", stand_in)
    profile = tmp_path / "p"
    profile.mkdir()
    cm.write_marker(profile, {"state": "done", "finished_at": time.time() - 30 * 86400})
    expired = _backup(stand_in / "storage-states", profile, "p-1", time.time() - 31 * 86400)
    report = cm.expire_backups(stand_in / "storage-states", now=time.time(), keep_days=0)
    assert report["state"] == "refused" and report["removed"] == [] and expired.exists()


def test_a_server_removes_expired_backups_when_it_starts(tmp_path):
    from _protocol import Client
    data = tmp_path / "data"
    profile = tmp_path / "p"
    profile.mkdir()
    cm.write_marker(profile, {"state": "done", "finished_at": time.time() - 30 * 86400})
    expired = _backup(data / "storage-states", profile, "p-1", time.time() - 31 * 86400)
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data)})
    try:
        assert not client.call("server_status").failed
        deadline = time.monotonic() + 20
        while expired.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not expired.exists() and not (expired.parent / f"{expired.stem}-db").exists()
    finally:
        client.close()


# ── a crash at every step of a move ──────────────────────────────────────────

class _Crash(BaseException):
    """The process dying: not an Exception, so no handler on the way turns it into a result."""


async def _fake_reader(folder, sub, _executable, _launch_args=None):
    """What Chrome with the flag reads: every v10 row of the temporary folder, decrypted."""
    cookies = []
    for database in (Path(folder) / sub / "Network" / "Cookies", Path(folder) / sub / "Cookies"):
        if database.is_file():
            with contextlib.closing(sqlite3.connect(database)) as db:
                for name, host, path, prefix in db.execute(
                        "SELECT name, host_key, path, substr(encrypted_value, 1, 3) FROM cookies"):
                    assert prefix == b"v10", "the reader saw a v11 row and would delete everything"
                    cookies.append({"name": name, "domain": host, "path": path, "value": f"value-{name}",
                                    "expires": time.time() + 3600, "session": False,
                                    "secure": False, "httpOnly": False})
    return cookies


def _mixed(tmp_path) -> Path:
    """v10 and v11 cookies in both databases a sub-profile can have."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v11", 1, 3600))
    _profile(profile, (b"v10", 1, 3600), (b"v11", 1, 3600), network=True)
    with sqlite3.connect(profile / "Default" / "Network" / "Cookies") as db:
        db.execute("UPDATE cookies SET name = 'n' || name")  # distinct names across the two
    return profile


def _launch_once(profile: Path, executable: str) -> str:
    """prepare, preflight and commit, as a launch does before Chrome starts. Returns the
    refusal, or "" when Chrome would now start."""
    config = server.Config(user_data_dir=str(profile), headless=True, browser_args=ARGS)
    config.browser_executable_path = executable
    move = asyncio.run(server._prepare_cookie_move(profile, config))
    if move.refuse:
        return move.refuse
    return asyncio.run(server._cookie_preflight(profile, move)) or \
        asyncio.run(server._commit_cookie_move(profile, move))


def _what_chrome_158_ends_with(profile: Path) -> set:
    """What the profile holds after Chrome 158 opens it without the flag and the pending move is
    finished: Chrome keeps v11 rows only when the store holds no v10 (else it drops it all),
    then sets every cookie of the backup."""
    rows = _names(profile)
    kept = set() if any(p == b"v10" for _n, p in rows) else {n for n, _p in rows}
    marker = cm.read_marker(profile)
    if marker.get("state") == "pending":
        kept |= {c["name"] for c in cm.verified_backup(Path(marker["backup"]), marker)}
    return kept


CRASH_POINTS = [
    ("copy_databases", "after"), ("reader_folder", "after"), ("_read_v10_cookies", "after"),
    ("write_durable", "before"), ("write_durable", "after"), ("verified_backup", "after"),
    ("fingerprint_of", "after"), ("write_marker", "before"), ("write_marker", "after"),
    ("_committed", "after"),  # after the first database's transaction, before the second
]


@pytest.mark.skipif(sys.platform == "win32", reason="the move runs on macOS only; fsync of a folder is POSIX")
@pytest.mark.parametrize("point", CRASH_POINTS, ids=[f"{n}-{w}" for n, w in CRASH_POINTS])
def test_a_crash_at_any_step_of_a_mixed_move_loses_no_cookie(tmp_path, chromes, monkeypatch, point):
    """A mixed profile (v10 and v11, in both databases) has its move cut off at each step, as a
    killed server would. Afterwards either the profile is exactly as it was, or a durable pending
    marker and backup cover every v10 cookie; and the next launch ends with every cookie."""
    profile = _mixed(tmp_path)
    original = _names(profile)
    everything = {n for n, _p in original}
    monkeypatch.setattr(server, "_read_v10_cookies", _fake_reader)
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    name, when = point
    owner = server if name == "_read_v10_cookies" else cm
    real = getattr(owner, name)
    calls = []

    if asyncio.iscoroutinefunction(real):
        async def crashing(*a, **kw):
            calls.append(1)
            if when == "before" and len(calls) == 1:
                raise _Crash(name)
            out = await real(*a, **kw)
            if when == "after" and len(calls) == 1:
                raise _Crash(name)
            return out
    else:
        def crashing(*a, **kw):
            calls.append(1)
            if when == "before" and len(calls) == 1:
                raise _Crash(name)
            out = real(*a, **kw)
            if when == "after" and len(calls) == 1:
                raise _Crash(name)
            return out

    monkeypatch.setattr(owner, name, crashing)
    with pytest.raises(_Crash):
        _launch_once(profile, chromes.c158)
    assert calls, f"{name} was never reached"
    monkeypatch.setattr(owner, name, real)

    marker = cm.read_marker(profile)
    if _names(profile) != original:
        assert marker.get("state") == "pending", "the profile changed with no pending marker"
        saved = cm.verified_backup(Path(marker["backup"]), marker)
        assert saved is not None, "the pending marker names a backup that cannot be trusted"
        assert {n for n, p in original if p == b"v10"} <= {c["name"] for c in saved}
        assert {n for n, p in original if p == b"v11"} <= {n for n, _p in _names(profile)}
    # The next launch, with nothing cut off, ends with every cookie.
    assert _launch_once(profile, chromes.c158) == ""
    assert _what_chrome_158_ends_with(profile) == everything


def test_the_backup_and_the_marker_are_fsynced_with_their_folder(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append(os.path.realpath(f"/dev/fd/{fd}") if sys.platform != "win32" else fd)
        return real(fd)

    monkeypatch.setattr(cm.os, "fsync", fsync)
    cm.write_durable(tmp_path / "backup.json", "{}")
    if sys.platform != "win32":
        assert len(synced) >= 2, "the file and its folder"
    assert json.loads((tmp_path / "backup.json").read_text()) == {}


def test_a_durable_write_cut_off_leaves_the_old_file_whole(tmp_path, monkeypatch):
    target = tmp_path / "marker.json"
    target.write_text('{"state": "pending"}')

    def no_rename(*_a, **_kw):
        raise _Crash("rename")

    monkeypatch.setattr(cm.os, "replace", no_rename)
    with pytest.raises(_Crash):
        cm.write_durable(target, '{"state": "done"}')
    assert json.loads(target.read_text()) == {"state": "pending"}
    assert [p.name for p in tmp_path.iterdir()] == ["marker.json"], "no temporary file left"


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need a privilege on Windows")
def test_a_link_at_the_marker_or_backup_is_replaced_not_written_through(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    link = tmp_path / "folder" / cm.MARKER
    link.parent.mkdir()
    link.symlink_to(outside)
    assert cm.read_marker(link.parent) == {}, "a linked marker is not trusted"
    cm.write_marker(link.parent, {"state": "pending"})
    assert outside.read_text() == "not yours"
    assert not link.is_symlink() and cm.read_marker(link.parent) == {"state": "pending"}


def test_a_backup_that_is_not_the_one_the_marker_recorded_is_not_resumed(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    backup = tmp_path / "backup.json"
    cookies = [{"name": "c0", "domain": "x.test", "path": "/", "value": "a"}]
    cm.write_durable(backup, json.dumps({"cdp_cookies": cookies}))
    _mark(profile, backup)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"
    backup.write_text(json.dumps({"cdp_cookies": [{**cookies[0], "value": "b"}]}))  # same count, other value
    assert cm.plan(profile, ARGS, chromes.c158).kind == "refuse", "not the digest the marker recorded"
    backup.write_text(json.dumps({"cdp_cookies": cookies + cookies}))
    _mark(profile, backup, count=1)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "refuse", "not the count the marker recorded"
    with sqlite3.connect(profile / "Default" / "Cookies") as db:
        db.execute("DELETE FROM cookies")
    gone = cm.plan(profile, ARGS, chromes.c158)
    assert gone.kind == "refuse" and "backup" in gone.reason, "the only copy is not trusted: start nothing"


# ── the review of the redesign: races, partitions, refusals, values ─────────

@pytest.fixture
def fake_reader(monkeypatch):
    monkeypatch.setattr(server, "_read_v10_cookies", _fake_reader)
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)


def test_a_v10_cookie_written_during_the_backup_refuses_and_writes_nothing(tmp_path, chromes, fake_reader, monkeypatch):
    profile = _mixed(tmp_path)
    real = server._read_v10_cookies

    async def reader_then_writer(*a, **kw):
        out = await real(*a, **kw)
        _profile(profile, (b"v10", 1, 3600))  # another Chrome writes while the copy is read
        return out

    monkeypatch.setattr(server, "_read_v10_cookies", reader_then_writer)
    before = _names(profile)
    out = _launch_once(profile, chromes.c158)
    assert "refused to open" in out and "changed" in out, out
    assert _names(profile) == sorted(before + [("c2", b"v10")]) and cm.read_marker(profile) == {}, \
        "a refusal writes nothing (the row is the writer's own)"


def test_a_v10_cookie_written_between_the_check_and_the_strip_is_not_deleted(tmp_path, chromes, fake_reader, monkeypatch):
    """The strip checks, inside its transaction, that every row it deletes is in the backup."""
    profile = _mixed(tmp_path)
    everything = {n for n, _p in _names(profile)}
    real = cm.write_marker

    def marker_then_writer(*a, **kw):
        real(*a, **kw)
        _profile(profile, (b"v10", 1, 3600))  # c2: after the preflight, before the strip

    monkeypatch.setattr(cm, "write_marker", marker_then_writer)
    out = _launch_once(profile, chromes.c158)
    assert "refused to open" in out and "arrived after the backup" in out, out
    assert ("c2", b"v10") in _names(profile)
    assert ("nc0", b"v10") in _names(profile), "every database is checked before any is changed"
    monkeypatch.setattr(cm, "write_marker", real)
    assert _launch_once(profile, chromes.c158) == ""
    assert _what_chrome_158_ends_with(profile) == everything | {"c2"}


def test_partitioned_cookies_of_one_name_are_counted_apart(tmp_path, chromes):
    """Two partitioned cookies share name, host and path; a backup holding one is not enough."""
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    with sqlite3.connect(profile / "Default" / "Cookies") as db:
        db.execute("CREATE TABLE cookies (name TEXT, encrypted_value BLOB, is_persistent INTEGER, "
                   "expires_utc INTEGER, host_key TEXT, path TEXT, top_frame_site_key TEXT)")
        for site in ("https://a.test", "https://b.test"):
            db.execute("INSERT INTO cookies VALUES ('id', CAST('v10x' AS BLOB), 1, 0, 'w.test', '/', ?)", (site,))
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert sum(plan.v10_keys.values()) == 2 and len(plan.v10_keys) == 2, plan.v10_keys
    backup = tmp_path / "backup.json"
    one = {"name": "id", "domain": "w.test", "path": "/", "value": "v",
           "partitionKey": {"topLevelSite": "https://a.test", "hasCrossSiteAncestor": False}}
    cm.write_durable(backup, json.dumps({"cdp_cookies": [one]}))
    _mark(profile, backup)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "migrate"
    two = {**one, "partitionKey": {"topLevelSite": "https://b.test", "hasCrossSiteAncestor": False}}
    cm.write_durable(backup, json.dumps({"cdp_cookies": [one, two]}))
    _mark(profile, backup)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"


@pytest.mark.parametrize("launch", ["browser_launch", "spawn_browser"])
def test_a_refused_launch_writes_nothing_in_the_profile(tmp_path, chromes, monkeypatch, launch):
    """A crashed profile (exit_type Crash, a stale lock) whose cookie database cannot be read:
    clean_profile_state and the window-state wipe once ran before the refusal."""
    from test_focus_restore import _fake_launch
    from mcp_stealth_chrome import state
    order = _fake_launch(monkeypatch)
    # A persistent launch of "main" settles the process's profile affinity; put it back after.
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", state._PROFILE_AFFINITY)
    profile = tmp_path / "main"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"not a database")
    (profile / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed"}}))
    if sys.platform != "win32":
        (profile / "SingletonLock").symlink_to("otherhost-999999")
    monkeypatch.setattr(server, "find_chrome_binary", lambda: chromes.c158)
    real_init = server.Config.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.browser_executable_path = chromes.c158

    monkeypatch.setattr(server.Config, "__init__", init)

    def digest():
        return sorted((str(p.relative_to(profile)), p.is_symlink() and os.readlink(p) or
                       (p.read_bytes() if p.is_file() else b"")) for p in profile.rglob("*"))

    before = digest()
    try:
        if launch == "browser_launch":
            monkeypatch.setattr(server, "_resolve_launch_profile", lambda *_a, **_kw: (profile, {}))
            fn = server.mcp._tool_manager._tools["browser_launch"].fn
            out = asyncio.run(fn(persistent=True, headless=True, auto_verify=False))
        else:
            _ok, out = asyncio.run(server._launch_browser_instance(
                instance_id="inst", url="about:blank", headless=True, proxy=None, user_agent=None,
                window_width=800, window_height=600, persistent=True, lang="en-US", extra_args=None,
                storage_state_path=None, idle_timeout=0, profile_dir_override=str(profile)))
    finally:
        server.BrowserState.reset()
    assert "refused to open" in out and "start" not in order, out
    assert digest() == before, "the refusal wrote to the profile"


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need a privilege on Windows")
def test_a_linked_sub_profile_is_neither_read_nor_changed(tmp_path, chromes):
    elsewhere = _profile(tmp_path / "elsewhere", (b"v10", 1, 3600))
    profile = tmp_path / "p"
    profile.mkdir()
    (profile / "Default").symlink_to(elsewhere / "Default", target_is_directory=True)
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse" and "is a link" in plan.reason, plan
    with pytest.raises(cm.cookie_db.Unreadable):
        cm.strip(profile, "Default", b"v10")
    assert _names(elsewhere) == [("c0", b"v10")]
    from mcp_stealth_chrome import profile_version as pv
    assert pv._no_v10_cookies(profile) is False, "a linked sub-profile is doubt for Chrome Beta"


def test_a_reader_that_fails_on_a_v10_profile_falls_back_to_the_flag_and_starts(tmp_path, chromes, fake_reader, monkeypatch):
    """Nothing in the profile is touched by the move, so the flag the fallback chooses is not
    then refused by the launch's own check (a put-back copy used to change the fingerprint)."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))

    async def broken(*_a, **_kw):
        raise RuntimeError("Chrome did not answer (simulated)")

    monkeypatch.setattr(server, "_read_v10_cookies", broken)
    config = server.Config(user_data_dir=str(profile), headless=True, browser_args=ARGS)
    config.browser_executable_path = chromes.c158
    move = asyncio.run(server._prepare_cookie_move(profile, config))
    assert move.flag and not move.refuse, move
    assert asyncio.run(server._cookie_preflight(profile, move)) == ""
    assert asyncio.run(server._commit_cookie_move(profile, move)) == ""
    assert _names(profile) == [("c0", b"v10")] and cm.read_marker(profile) == {}


def test_a_cookie_that_came_back_with_another_value_keeps_the_move_pending(tmp_path):
    """Only keys were compared, so a cookie set again with another value read as moved."""
    profile, move = _move(tmp_path, ["a", "b"])
    swap = {"a"}

    class Swapping(_Connection):
        async def send(self, gen):
            answer = await super().send(gen)
            for cookie in (answer or {}).get("cookies") or []:
                if cookie["name"] in swap:
                    cookie["value"] = "SOMETHING-ELSE"
            return answer

    moved, text = asyncio.run(server._finish_cookie_move(SimpleNamespace(connection=Swapping()), profile, move))
    assert not moved and "1 with a different value" in text and "a@x.test" in text, text
    assert "SOMETHING-ELSE" not in text and "value-a" not in text, "no value is printed"
    assert cm.read_marker(profile)["state"] == "pending"


def test_a_change_in_another_sub_profile_does_not_refuse_this_one(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    move = server._CookieMove(cm.plan(profile, ARGS, chromes.c158))
    _profile(profile, (b"v10", 1, 3600), sub="Profile 1")  # another persona, another browser
    assert asyncio.run(server._cookie_preflight(profile, move)) == ""
    _profile(profile, (b"v10", 1, 3600))
    assert "changed" in asyncio.run(server._cookie_preflight(profile, move))


def test_a_refusal_says_what_can_be_done(tmp_path, chromes):
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"not a database")
    config = server.Config(user_data_dir=str(profile), headless=True, browser_args=ARGS)
    config.browser_executable_path = chromes.c158
    text = asyncio.run(server._prepare_cookie_move(profile, config)).refuse
    for needed in ("Close every Chrome", "clone_chrome_profile", "restore it from a backup",
                   "persistent=False starts on an empty profile, without these logins"):
        assert needed in text, (needed, text)


def test_a_move_made_again_over_a_half_done_strip_carries_the_earlier_backup(tmp_path, chromes, fake_reader, monkeypatch):
    """The first database's v10 rows went; a writer then added a v10 row to the second, so the
    strip stopped there. The next launch moves again because the backup lacks that row, and the
    rows only the earlier backup still holds must come along."""
    profile = _mixed(tmp_path)
    everything = {n for n, _p in _names(profile)}
    real = cm._committed

    def then_a_writer(database):
        real(database)
        if "Network" in str(database):
            _profile(profile, (b"v10", 1, 3600))  # c2, into Default/Cookies

    monkeypatch.setattr(cm, "_committed", then_a_writer)
    out = _launch_once(profile, chromes.c158)
    assert "arrived after the backup" in out, out
    assert ("nc0", b"v10") not in _names(profile), "the first database was stripped"
    monkeypatch.setattr(cm, "_committed", real)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "migrate"
    assert _launch_once(profile, chromes.c158) == ""
    assert _what_chrome_158_ends_with(profile) == everything | {"c2"}


# ── review round 2: F14 to F22 ───────────────────────────────────────────────

def _pending(profile: Path, tmp_path: Path, *, sub="Default", bound_to=None, names=("c0",), legacy=False,
             copy=True) -> Path:
    """A pending marker as this version writes it (or as v0.16.1 did, legacy=True), with a backup
    holding `names` and, unless copy=False, a database copy taken from the profile."""
    backup = tmp_path / f"backup-{sub.replace(' ', '_')}.json"
    cookies = [{"name": n, "domain": "x.test", "path": "/", "value": f"value-{n}"} for n in names]
    cm.write_durable(backup, json.dumps({"cdp_cookies": cookies}))
    database_copy = tmp_path / f"copy-{sub.replace(' ', '_')}"
    if copy and not database_copy.exists():
        cm.copy_databases(profile, sub, database_copy)
    marker = {"state": "pending", "backup": str(backup), "database_copy": str(database_copy)}
    if not legacy:
        marker.update(version=cm.MARKER_VERSION, subprofile=sub,
                      profile=os.path.realpath(bound_to or profile),
                      digest=cm.file_digest(backup), count=len(cookies))
    cm.write_marker(profile, marker)
    return backup


def test_f14_a_pending_move_of_another_sub_profile_is_never_used(tmp_path, chromes):
    """A move of Default was cut off; launching Profile 1, whose cookie has the same key, used
    Default's backup to strip and set Profile 1's cookies."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    _profile(profile, (b"v10", 1, 3600), sub="Profile 1")
    _pending(profile, tmp_path)
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"
    other = cm.plan(profile, ["--profile-directory=Profile 1"], chromes.c158)
    assert other.kind == "refuse" and "'Default'" in other.reason, other


def test_f14_a_pending_marker_of_another_profile_folder_is_never_used(tmp_path, chromes):
    """A profile folder copied with its marker: the marker names the folder it was written in."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    _pending(profile, tmp_path, bound_to=tmp_path / "original")
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse" and "another profile folder" in plan.reason, plan


def test_f15_a_row_rewritten_with_the_same_key_stops_the_strip(tmp_path, chromes, fake_reader, monkeypatch):
    """Between the preflight and the strip a writer gives a v10 cookie a new value under the
    same key. Comparing keys let the strip delete the new value and set the old one back."""
    profile = _mixed(tmp_path)
    real = cm.write_marker

    def marker_then_writer(*a, **kw):
        real(*a, **kw)
        with sqlite3.connect(profile / "Default" / "Cookies") as db:
            db.execute("UPDATE cookies SET encrypted_value = CAST('v10-newer' AS BLOB) WHERE name = 'c0'")

    monkeypatch.setattr(cm, "write_marker", marker_then_writer)
    out = _launch_once(profile, chromes.c158)
    assert "refused to open" in out and "changed since the backup" in out, out
    assert ("c0", b"v10") in _names(profile), "the newer row was not deleted"


def test_f16_a_pending_move_with_a_damaged_backup_refuses_with_the_way_back(tmp_path, chromes):
    """Part of the profile may already be stripped; a new move would read only what is left."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    backup = _pending(profile, tmp_path)
    backup.write_text(json.dumps({"cdp_cookies": []}))  # damaged since
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse", plan
    assert "database copy" in plan.reason and "passes its integrity check" in plan.reason, plan.reason
    shutil.rmtree(tmp_path / "copy-Default")
    assert "database copy" in cm.plan(profile, ARGS, chromes.c158).reason


def test_f17_a_pending_marker_written_by_an_older_version_is_not_trusted(tmp_path, chromes):
    """v0.16.1 wrote no digest, count or binding, so its backup cannot be checked."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    _pending(profile, tmp_path, legacy=True)
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "refuse" and "older version" in plan.reason, plan


def test_f18_a_lock_that_cannot_be_read_refuses(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    move = server._CookieMove(cm.plan(profile, ARGS, chromes.c158))
    (profile / "SingletonLock").write_text("not a link")  # exists, names no PID
    out = asyncio.run(server._cookie_preflight(profile, move))
    assert "refused to open" in out and "SingletonLock" in out, out
    (profile / "SingletonLock").unlink()
    assert asyncio.run(server._cookie_preflight(profile, move)) == ""


def test_f19_a_write_after_the_commit_stops_the_start(tmp_path, decided, monkeypatch):
    profile = _profile(tmp_path / "p", (b"v11", 1, 3600))
    real = server._commit_cookie_move

    async def commit_then_writer(p, move):
        out = await real(p, move)
        _profile(Path(p), (b"v10", 1, 3600))  # in the gap before Chrome starts
        return out

    monkeypatch.setattr(server, "_commit_cookie_move", commit_then_writer)
    out = _launch_on(profile)
    assert out.startswith("Error: refused to open") and "changed" in out, out
    assert "start" not in decided.order


def test_f20_an_orphan_backup_goes_after_the_retention(tmp_path):
    """A backup written before a crash that came before its marker is referenced by nothing."""
    now = time.time()
    store = tmp_path / "storage-states"
    untouched = tmp_path / "untouched"
    untouched.mkdir()
    old = _backup(store, untouched, "u-1", now - 20 * 86400)
    new = _backup(store, untouched, "u-2", now - 1 * 86400)
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / cm.MARKER).write_text("{not json")
    kept = _backup(store, unreadable, "x-1", now - 20 * 86400)
    cm.expire_backups(store, now=now, keep_days=14)
    assert not old.exists() and new.exists() and kept.exists()


def test_f20_reader_folders_left_by_a_killed_server_are_removed(tmp_path, monkeypatch):
    stale = tmp_path / "mcp-stealth-reader-old"
    fresh = tmp_path / "mcp-stealth-reader-new"
    other = tmp_path / "someone-else"
    for folder in (stale, fresh, other):
        (folder / "Default").mkdir(parents=True)
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    os.utime(other, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(cm, "_named_by_a_process", lambda _path: False)
    cm.remove_reader_leftovers(tmp_path)
    assert not stale.exists() and fresh.exists() and other.exists()
    os.utime(fresh, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(cm, "_named_by_a_process", lambda _path: None)  # unknown: keep
    cm.remove_reader_leftovers(tmp_path)
    assert fresh.exists()


@pytest.mark.parametrize("launch", [_launch_on, _spawn_on])
def test_f21_a_move_that_did_not_finish_closes_its_browser(tmp_path, decided, fake_reader, monkeypatch, launch):
    """Left running, the next browser_launch reused it and never tried the move again."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    closed = []

    async def finish(_browser, _profile, move):
        return False, "1 of 2 cookies came back as they were (simulated)"

    async def dead(browser, _profile):
        closed.append(browser)
        return {"verified": True}

    monkeypatch.setattr(server, "_finish_cookie_move", finish)
    monkeypatch.setattr(server, "_ensure_browser_dead", dead)
    out = launch(profile)
    text = out if isinstance(out, str) else out[1]
    assert "simulated" in text and closed, text
    assert "launch again" in text, text


def test_f22_every_attribute_set_back_is_compared(tmp_path):
    saved = {"name": "a", "domain": "x.test", "path": "/", "value": "v", "priority": "High",
             "sourceScheme": "Secure", "sourcePort": 443,
             "partitionKey": {"topLevelSite": "https://t.test", "hasCrossSiteAncestor": False}}
    for field, other in (("priority", "Low"), ("sourceScheme", "NonSecure"), ("sourcePort", 80),
                         ("partitionKey", {"topLevelSite": "https://t.test", "hasCrossSiteAncestor": True})):
        missing, differ = cm.compare([saved], [{**saved, field: other}])
        assert differ == [cm.key(saved)], field
    assert cm.compare([saved], [dict(saved)]) == ([], [])


# ── review round 3: F20 and F21 ──────────────────────────────────────────────

def test_f20_no_backup_goes_while_its_profiles_move_is_pending(tmp_path):
    """Move A stripped part of the profile, a retry wrote backup B carrying A's cookies, and B
    was then damaged. A, past the retention, is the only copy of those rows: it stays, with its
    database copy, until the marker says done."""
    now = time.time()
    store = tmp_path / "storage-states"
    profile = tmp_path / "p"
    profile.mkdir()
    a = _backup(store, profile, "p-a", now - 40 * 86400)
    b = store / f"{cm.BACKUP_PREFIX}-p-b.json"
    b.write_text("{damaged")
    cm.write_marker(profile, {"state": "pending", "version": cm.MARKER_VERSION, "backup": str(b),
                              "digest": "0" * 64, "count": 3})
    cm.expire_backups(store, now=now, keep_days=14)
    assert a.exists() and (store / f"{a.stem}-db").exists()
    cm.write_marker(profile, {"state": "done", "backup": str(b), "finished_at": now - 20 * 86400})
    cm.expire_backups(store, now=now, keep_days=14)
    assert not a.exists(), "superseded once the move is done"


@pytest.mark.parametrize("breaks", ["compare", "write_marker"])
def test_f21_a_finish_that_raises_closes_the_browser(tmp_path, decided, fake_reader, monkeypatch, breaks):
    """Comparing and writing the done marker sat outside the error handling: an exception there
    left the browser running, and the next launch reused it without resuming the move."""
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    closed = []

    async def dead(browser, _profile):
        closed.append(browser)
        return {"verified": True}

    class _Conn(_Connection):
        pass

    real_finish = server._finish_cookie_move

    async def finish(_browser, p, move):
        return await real_finish(SimpleNamespace(connection=_Conn()), p, move)

    def broken(*_a, **_kw):
        raise OSError("disk full (simulated)")

    real_write = cm.write_marker

    def write_marker(profile_path, data):
        if data.get("state") == "done" and breaks == "write_marker":
            broken()
        return real_write(profile_path, data)

    monkeypatch.setattr(server, "_finish_cookie_move", finish)
    monkeypatch.setattr(server, "_ensure_browser_dead", dead)
    if breaks == "compare":
        monkeypatch.setattr(cm, "compare", broken)
    monkeypatch.setattr(cm, "write_marker", write_marker)
    out = _launch_on(profile)
    assert out.startswith("Error:") and "simulated" in out and closed, out
    assert "launch again" in out, out
    assert cm.read_marker(profile)["state"] == "pending"


@pytest.mark.parametrize("failure", ["raises", "reads_none"])
def test_a_brave_reader_that_cannot_read_the_cookies_refuses_and_changes_nothing(tmp_path, chromes, failure, monkeypatch):
    """Brave Beta without access to "Brave Safe Storage" (the person denied or did not answer the
    Keychain dialog) read 0 of 3 cookies; the launch that followed with the protection turned off
    could not decrypt them either and dropped all 3. For Brave a reader that fails is a refusal."""
    from mcp_stealth_chrome import brave as brave_mode
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600), (b"v10", 1, 3600))
    before = _names(profile)

    async def reader(*_a, **_kw):
        if failure == "raises":
            raise RuntimeError("Chrome did not answer (simulated)")
        return []

    monkeypatch.setattr(server, "_read_v10_cookies", reader)
    config = server.Config(user_data_dir=str(profile), headless=True, browser_args=ARGS)
    config.browser_executable_path = chromes.c158
    brave_mode.mark(config, brave_mode.BRAVE)
    move = asyncio.run(server._prepare_cookie_move(profile, config))
    assert move.refuse and not move.flag, move
    assert "Keychain" in move.refuse and "Brave Safe Storage" in move.refuse
    assert "Nothing was started and the profile was not changed" in move.refuse
    assert _names(profile) == before and cm.read_marker(profile) == {}
