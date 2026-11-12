"""Moving cookies to Chrome 158's encryption: the decisions and the bookkeeping, with fakes.
`test_cookie_migration_e2e.py` does it with a real Chrome."""
import asyncio
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


def _profile(folder: Path, *rows, sub: str = "Default", network: bool = False) -> Path:
    """rows: (prefix, persistent, expires_in_seconds)."""
    target = folder / sub / ("Network" if network else "")
    target.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target / "Cookies")
    db.execute("CREATE TABLE cookies (name TEXT, encrypted_value BLOB, is_persistent INTEGER, expires_utc INTEGER)")
    for i, (prefix, persistent, expires_in) in enumerate(rows):
        expires = 0 if expires_in is None else _NOW_CHROME + expires_in * 1_000_000
        db.execute("INSERT INTO cookies VALUES (?, ?, ?, ?)", (f"c{i}", prefix + b"x", persistent, expires))
    db.commit()
    db.close()
    return folder


@pytest.fixture
def chromes(tmp_path, monkeypatch):
    monkeypatch.setattr(cm.sys, "platform", "darwin")
    monkeypatch.setattr(cm, "default_data_dirs", lambda: [tmp_path / "default-chrome"])
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


def test_an_unreadable_database_means_the_flag(tmp_path, chromes):
    profile = tmp_path / "p"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"not a database")
    plan = cm.plan(profile, ARGS, chromes.c158)
    assert plan.kind == "flag" and "could not be read" in plan.reason


def test_a_pending_move_with_its_backup_is_resumed(tmp_path, chromes):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    backup = tmp_path / "backup.json"
    backup.write_text("{}")
    cm.write_marker(profile, {"state": "pending", "backup": str(backup)})
    assert cm.plan(profile, ARGS, chromes.c158).kind == "resume"
    backup.unlink()
    assert cm.plan(profile, ARGS, chromes.c158).kind == "migrate", "no backup: start over, with the flag"


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


def test_databases_are_copied_and_put_back_exactly(tmp_path):
    profile = _profile(tmp_path / "p", (b"v10", 1, 3600))
    (profile / "Default" / "Cookies-journal").write_bytes(b"journal")
    databases = cm.cookie_databases(profile, "Default")
    copied = cm.backup_databases(databases, profile, tmp_path / "copy")
    assert sorted(Path(c).as_posix() for c in copied) == ["Default/Cookies", "Default/Cookies-journal"]
    original = (profile / "Default" / "Cookies").read_bytes()
    (profile / "Default" / "Cookies").write_bytes(b"emptied by Chrome")
    (profile / "Default" / "Cookies-wal").write_bytes(b"written since")
    cm.restore_databases(tmp_path / "copy", profile, copied)
    assert (profile / "Default" / "Cookies").read_bytes() == original
    assert (profile / "Default" / "Cookies-journal").read_bytes() == b"journal"
    assert not (profile / "Default" / "Cookies-wal").exists()


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
