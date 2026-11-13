"""keep_alive without a real Chrome: the record, every point of the "is it ours" check, the watcher's
time cap, the exit path and profile_gc. Each guard has a test that fails when the guard is removed.

The real-Chrome proof is test_keep_alive_e2e.py. No process this file did not start is touched.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import _quit_watcher as qw
from mcp_stealth_chrome import keep_alive as ka
from mcp_stealth_chrome import profile_gc

posix_only = pytest.mark.skipif(os.name != "posix", reason="keep_alive is refused on Windows")


@pytest.fixture
def records(tmp_path, monkeypatch):
    folder = tmp_path / "keep-alive"
    monkeypatch.setenv(ka.ENV_DIR, str(folder))
    return folder


def _record(profile, **extra):
    base = dict(pid=4242, host="127.0.0.1", port=50123, user_data_dir=str(profile), instance_id="main",
                chrome_version="Chrome/157.0.1.2", headless=True, server_pid=777, watcher_pid=778,
                state="kept")
    base.update(extra)
    return base


CMD = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-host=127.0.0.1 "
       "--remote-debugging-port=50123 --user-data-dir={udd} --hide-crash-restore-bubble "
       "--disable-session-crashed-bubble --disable-restore-session-state about:blank")


@pytest.fixture
def ours(tmp_path, records, monkeypatch):
    """A profile with a record whose every check passes; tests then break one point."""
    profile = tmp_path / "profile"
    profile.mkdir()
    assert ka.write(profile, **_record(profile)) is None
    facts = {"alive": {4242}, "holder": 4242, "command": CMD.format(udd=profile),
             "version": {"Browser": "Chrome/157.0.1.2"}, "browser_pid": 4242, "server": None}
    monkeypatch.setattr(ka._state, "_pid_alive", lambda pid: pid in facts["alive"])
    monkeypatch.setattr(ka._state, "chrome_lock_holder_pid", lambda _p: facts["holder"])
    monkeypatch.setattr(ka._watcher, "process_command",
                        lambda pid: facts["command"] if pid == 4242 else None)
    monkeypatch.setattr(ka, "_json_version", lambda _h, _p: facts["version"])
    monkeypatch.setattr(ka, "_browser_pid", lambda _h, _p: facts["browser_pid"])
    monkeypatch.setattr(ka, "_server_holding", lambda _r: facts["server"])
    monkeypatch.setattr(ka._watcher, "process_start",
                        lambda pid, like=None: facts["start"] if pid == 4242 else None)
    facts["start"] = "Fri Nov 13 10:00:00 2026"
    ka.write(profile, pid_start=facts["start"])
    return SimpleNamespace(profile=profile, facts=facts)


# ── the record ──────────────────────────────────────────────────────────────────────────────

def test_record_is_private_and_keyed_by_the_real_path(tmp_path, records):
    profile = tmp_path / "p"
    profile.mkdir()
    assert ka.write(profile, **_record(profile)) is None
    path = ka.record_path(profile)
    assert path.parent == records
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ka.read(str(profile) + "/") is not None, "the same folder spelled differently"
    assert ka.read(profile)["profile"] == os.path.realpath(profile)


def test_a_record_naming_another_profile_or_a_link_is_not_read(tmp_path, records):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    assert ka.write(one, **_record(one)) is None
    os.replace(ka.record_path(one), ka.record_path(two))  # one's record under two's name
    assert ka.read(two) is None
    if os.name == "posix":
        assert ka.write(one, **_record(one)) is None
        ka.record_path(two).unlink()
        os.symlink(ka.record_path(one), ka.record_path(two))
        data = json.loads(ka.record_path(one).read_text())
        data["profile"] = os.path.realpath(two)
        ka.record_path(one).write_text(json.dumps(data))
        assert ka.read(two) is None, "a record reached through a link was trusted"


def test_remove_only_for_the_pid_it_names(tmp_path, records):
    profile = tmp_path / "p"
    profile.mkdir()
    ka.write(profile, **_record(profile))
    assert not ka.remove(profile, pid=1)
    assert ka.read(profile) is not None
    assert ka.remove(profile, pid=4242)
    assert ka.read(profile) is None


# ── is it ours ──────────────────────────────────────────────────────────────────────────────

@posix_only
def test_check_passes_only_with_every_point(ours):
    assert ka.check(ours.profile)["ok"] is True


@posix_only
@pytest.mark.parametrize("breaks, reason", [
    (lambda f: f["alive"].clear(), "no longer running"),
    (lambda f: f.update(holder=9999), "lock is held by PID 9999"),
    (lambda f: f.update(command="/usr/bin/python3 sleep.py"), "not a browser's main process"),
    (lambda f: f.update(command=f["command"].replace(" --hide-crash-restore-bubble", "")),
     "was not started by this server"),
    (lambda f: f.update(command=f["command"].replace("--remote-debugging-port=50123", "--remote-debugging-port=1")),
     "was not started by this server"),
    (lambda f: f.update(version=None), "DevTools did not answer"),
    (lambda f: f.update(version={"Browser": "Chrome/158.0.0.0"}), "not the recorded"),
    (lambda f: f.update(browser_pid=5555), "is not PID 4242"),
    (lambda f: f.update(server=31337), "server PID 31337 that started it is still running"),
])
def test_check_refuses_when_one_point_fails(ours, breaks, reason):
    breaks(ours.facts)
    verdict = ka.check(ours.profile)
    assert verdict["ok"] is False and reason in verdict["reason"], verdict


@posix_only
def test_check_refuses_a_browser_this_server_holds(ours):
    ours.facts["server"] = os.getpid()
    assert "this server already holds" in ka.check(ours.profile)["reason"]


@posix_only
def test_check_refuses_a_command_line_for_another_folder(ours, tmp_path):
    ours.facts["command"] = CMD.format(udd=tmp_path / "elsewhere")
    assert "was not started by this server" in ka.check(ours.profile)["reason"]


@posix_only
def test_a_dead_chrome_drops_its_record(ours):
    ours.facts["alive"].clear()
    ka.check(ours.profile)
    assert ka.read(ours.profile) is None


@posix_only
def test_server_holding_tells_a_server_from_a_recycled_pid(monkeypatch):
    monkeypatch.setattr(ka._state, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(ka._watcher, "process_command", lambda _pid: "/usr/bin/vim notes.txt")
    assert ka._server_holding({"server_pid": 123}) is None
    monkeypatch.setattr(ka._watcher, "process_command",
                        lambda _pid: "/Users/x/.local/bin/mcp-stealth-chrome")
    assert ka._server_holding({"server_pid": 123}) == 123
    monkeypatch.setattr(ka._watcher, "process_command", lambda _pid: "")  # could not look
    assert ka._server_holding({"server_pid": 123}) == 123


# ── the previous watcher ────────────────────────────────────────────────────────────────────

@posix_only
def test_end_old_watcher_leaves_any_other_process_alone():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        verdict = ka.end_old_watcher({"watcher_pid": other.pid, "pid": 4242, "port": 50123})
        assert "is not its watcher" in verdict
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


@posix_only
def test_end_old_watcher_ends_one_that_ignores_sigterm():
    cfg = json.dumps({"pid": 4242, "host": "127.0.0.1", "port": 50123, "user_data_dir": "/p"})
    body = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)"
    watcher = subprocess.Popen([sys.executable, "-c", body, qw.NAME, cfg])
    try:
        time.sleep(0.3)
        record = {"watcher_pid": watcher.pid, "pid": 4242, "port": 50123,
                  "watcher_start": qw.process_start(watcher.pid)}
        assert ka.end_old_watcher(record) == "ended"
        assert watcher.wait(5) is not None
    finally:
        if watcher.poll() is None:
            watcher.kill()
            watcher.wait()


def test_the_watcher_no_longer_inherits_an_ignored_sigterm():
    source = (qw.__file__ and open(qw.__file__, encoding="utf-8").read())
    assert "signal.signal(signal.SIGTERM, signal.SIG_DFL)" in source


# ── the watcher's time cap ──────────────────────────────────────────────────────────────────

def _cfg(tmp_path, **extra):
    return {"pid": 4242, "host": "127.0.0.1", "port": 1, "user_data_dir": str(tmp_path / "p"),
            "grace": 3.0, "interval": 0.5, "tool_closed_file": str(tmp_path / "t"),
            "quit_file": str(tmp_path / "q"), "server_pid": -1, **extra}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 10_000:
            raise AssertionError("watch never stopped")


def test_the_cap_ends_a_browser_once_it_has_no_server(tmp_path, monkeypatch):
    clock = _Clock()
    ended = []
    monkeypatch.setattr(qw, "process_command", lambda _pid: f"chrome --user-data-dir={tmp_path / 'p'}")
    monkeypatch.setattr(qw, "page_count", lambda *_a: 1)
    monkeypatch.setattr(qw, "server_alive", lambda _cfg: False)
    monkeypatch.setattr(qw, "end_browser", lambda _cfg: ended.append(clock.now) or "closed")
    out = qw.watch(_cfg(tmp_path, max_orphan_seconds=60), sleep=clock.sleep, clock=clock)
    assert out == "time cap: closed" and ended and 60 <= ended[0] <= 61


def test_the_cap_does_not_count_while_a_server_holds_it(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qw, "process_command", lambda _pid: f"chrome --user-data-dir={tmp_path / 'p'}")
    monkeypatch.setattr(qw, "page_count", lambda *_a: 1)
    monkeypatch.setattr(qw, "server_alive", lambda _cfg: clock.now < 500)
    monkeypatch.setattr(qw, "end_browser", lambda _cfg: "closed")
    assert qw.watch(_cfg(tmp_path, max_orphan_seconds=60), sleep=clock.sleep, clock=clock) == "time cap: closed"
    assert 560 <= clock.now <= 561, "counted from launch, not from when the server went"


def test_without_a_cap_a_lone_browser_is_left_alone(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qw, "process_command",
                        lambda _pid: f"chrome --user-data-dir={tmp_path / 'p'}" if clock.now < 900 else None)
    monkeypatch.setattr(qw, "page_count", lambda *_a: 1)
    monkeypatch.setattr(qw, "server_alive", lambda _cfg: False)
    monkeypatch.setattr(qw, "end_browser", lambda _cfg: pytest.fail("ended without a cap"))
    assert qw.watch(_cfg(tmp_path), sleep=clock.sleep, clock=clock) == "gone"


def test_headless_skips_the_window_logic(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qw, "process_command",
                        lambda _pid: f"chrome --user-data-dir={tmp_path / 'p'}" if clock.now < 50 else None)
    monkeypatch.setattr(qw, "page_count", lambda *_a: pytest.fail("pages read for a headless browser"))
    monkeypatch.setattr(qw, "end_browser", lambda _cfg: pytest.fail("a headless browser with no page was ended"))
    assert qw.watch(_cfg(tmp_path, pages_matter=False), sleep=clock.sleep, clock=clock) == "gone"


def test_the_watcher_drops_the_record_of_its_chrome_only(tmp_path, monkeypatch):
    path = tmp_path / "rec.json"
    cfg = _cfg(tmp_path, keep_alive_record=str(path))
    monkeypatch.setattr(qw, "gone", lambda *_a: True)
    path.write_text(json.dumps({"pid": 1}))
    qw.forget_kept(cfg)
    assert path.exists(), "a record of another Chrome was removed"
    path.write_text(json.dumps({"pid": 4242}))
    monkeypatch.setattr(qw, "gone", lambda *_a: False)
    qw.forget_kept(cfg)
    assert path.exists(), "removed while that Chrome still runs"
    monkeypatch.setattr(qw, "gone", lambda *_a: True)
    qw.forget_kept(cfg)
    assert not path.exists()


# ── profile_gc ──────────────────────────────────────────────────────────────────────────────

@posix_only
def test_profile_gc_leaves_a_kept_profile_alone(tmp_path, records, monkeypatch):
    root = tmp_path / "data"
    fallback = root / "profile-pid999999"
    fallback.mkdir(parents=True)
    old = time.time() - 3600
    os.utime(fallback, (old, old))
    monkeypatch.setattr(profile_gc, "_owner_gone", lambda *_a: True)
    why, _ = profile_gc.fallback_refusal(fallback, root, processes=[], held=set(), now=time.time())
    assert why is None, why  # removable without a kept browser
    ka.write(fallback, **_record(fallback, pid=4242))
    monkeypatch.setattr(ka._state, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(ka._state, "chrome_lock_holder_pid", lambda _p: 4242)
    monkeypatch.setattr(ka._watcher, "process_command",
                        lambda pid: CMD.format(udd=fallback) if pid == 4242 else None)
    monkeypatch.setattr(ka._watcher, "process_start", lambda _pid, like=None: None)
    why, _ = profile_gc.fallback_refusal(fallback, root, processes=[], held=set(), now=time.time())
    assert why == "held by a browser kept alive (keep_alive)"


# ── the server side ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def srv():
    from mcp_stealth_chrome import server
    return server


def test_decision_needs_a_persistent_profile(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(srv._keep_alive, "supported", lambda: None)
    assert srv._keep_alive_decision(None, tmp_path, False, False)[0] is False
    assert srv._keep_alive_decision(False, tmp_path, False, False)[0] is False
    keep, note = srv._keep_alive_decision(True, None, False, False)
    assert not keep and "persistent=False" in note
    keep, note = srv._keep_alive_decision(True, tmp_path, True, False)
    assert not keep and "privacy profile" in note
    keep, note = srv._keep_alive_decision(True, tmp_path, False, True)
    assert keep and "invisible Chrome holding memory" in note
    keep, note = srv._keep_alive_decision(True, tmp_path, False, False)
    assert keep and "Quit still ends it" in note
    monkeypatch.setattr(srv._keep_alive, "supported", lambda: "not here")
    keep, note = srv._keep_alive_decision(True, tmp_path, False, False)
    assert not keep and "not here" in note


@posix_only
def test_supported_refuses_without_a_watcher(monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCHER", "0")
    assert "MCP_STEALTH_QUIT_WATCHER" in (ka.supported() or "")


class _FakeBrowser:
    def __init__(self, pid, spawned=True, keep=False, udd="/tmp/p"):
        self._process = SimpleNamespace(pid=pid, returncode=None, _transport=SimpleNamespace(_proc=object())) \
            if spawned else None
        self._process_pid = pid
        self.config = SimpleNamespace(host="127.0.0.1", port=50123, user_data_dir=udd)
        self.stops = 0
        if keep:
            setattr(self, ka.ATTR, True)

    @property
    def stopped(self):
        return self._process is None

    def stop(self):
        self.stops += 1


def test_exit_leaves_a_kept_browser_and_ends_the_rest(srv, tmp_path, records, monkeypatch):
    monkeypatch.setattr(srv, "_process_exists", lambda _pid: True)
    kept = _FakeBrowser(os.getpid(), keep=True, udd=str(tmp_path / "p"))
    (tmp_path / "p").mkdir()
    ka.write(tmp_path / "p", **_record(tmp_path / "p", pid=os.getpid()))
    transport = kept._process._transport
    assert srv._leave_or_end_at_exit(kept, [], 0, tmp_path / "p", False) == "kept"
    assert kept.stops == 0 and transport._proc is None and kept._process is None
    assert ka.read(tmp_path / "p")["state"] == "kept"

    plain = _FakeBrowser(4243)
    assert srv._leave_or_end_at_exit(plain, [], 0, tmp_path / "q", False) == "stopped"
    assert plain.stops == 1

    # Marked, but its record is gone (or never written): it closes like any other.
    orphan = _FakeBrowser(4244, keep=True)
    assert srv._leave_or_end_at_exit(orphan, [], 0, tmp_path / "r", False) == "stopped"


def test_exit_closes_a_reattached_browser_that_is_not_kept(srv, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(srv._keep_alive, "close_at_exit",
                        lambda host, port, pid, udd, start: calls.append((host, port, pid, udd, start))
                        or "closed")
    reattached = _FakeBrowser(4245, spawned=False, udd="/given/p")
    reattached._mcp_pid_start = "Fri Nov 13 10:00:00 2026"
    assert srv._leave_or_end_at_exit(reattached, [], 0, tmp_path / "p", False) == "closed"
    assert calls == [("127.0.0.1", 50123, 4245, "/given/p", "Fri Nov 13 10:00:00 2026")]
    assert reattached.stops == 0, "nodriver's stop() has no process to end there"


def test_a_reattached_browser_is_not_read_as_stopped(srv, monkeypatch):
    monkeypatch.setattr(srv, "_process_exists", lambda pid: pid == 4246)
    assert srv._browser_exited(_FakeBrowser(4246, spawned=False)) is False
    assert srv._browser_exited(_FakeBrowser(4247, spawned=False)) is True


def test_reattach_names_what_it_cannot_apply(srv):
    given = {"headless": True, "proxy": "http://u:p@h:1", "profile_mode": "light", "lang": "en-US",
             "restore_tabs": False, "extra_args": [], "user_agent": None}
    out = srv._reattach_ignored(given)
    assert "headless=True" in out and "proxy" in out and "profile_mode='light'" in out
    assert not any("u:p" in item for item in out), "a proxy credential reached the result"
    assert all("lang" not in item and "restore_tabs" not in item for item in out)


# ── review findings ─────────────────────────────────────────────────────────────────────────

class _ArmBrowser:
    """Enough of a launched browser for _arm_keep_alive."""
    def __init__(self, udd, version="Chrome/157.0.1.2"):
        self._process = SimpleNamespace(pid=os.getpid(), returncode=None)
        self._process_pid = os.getpid()
        self.config = SimpleNamespace(host="127.0.0.1", port=50123, user_data_dir=str(udd))
        self.info = {"Browser": version} if version else None


@pytest.fixture
def armable(srv, tmp_path, records, monkeypatch):
    started = []
    monkeypatch.setattr(srv.quit_watch, "start",
                        lambda *a, **kw: started.append(kw) or SimpleNamespace(pid=os.getpid()))
    monkeypatch.setattr(srv, "_ensure_window_watcher_running", lambda: None)
    monkeypatch.setattr(srv._keep_alive, "_json_version", lambda _h, _p: None)
    profile = tmp_path / "profile"
    profile.mkdir()
    return profile


@posix_only  # arming needs a process start time, which Windows does not give here
def test_f2_the_record_never_holds_a_proxy_credential(srv, armable, monkeypatch):
    monkeypatch.setattr(srv.BrowserState, "current_proxy", "http://alice:s3cretPW@proxy.example:8080")
    assert srv._arm_keep_alive(_ArmBrowser(armable), armable, "main", True, {}) == ""
    text = ka.record_path(armable).read_text()
    assert "s3cretPW" not in text and "alice" not in text, text
    assert "proxy" not in json.loads(text)


@posix_only
def test_f2_an_old_record_with_a_proxy_loses_it_on_the_next_write(ours):
    data = json.loads(ka.record_path(ours.profile).read_text())
    data["proxy"] = "http://alice:s3cretPW@proxy.example:8080"
    ka.record_path(ours.profile).write_text(json.dumps(data))
    ka.write(ours.profile, state="kept")
    assert "s3cretPW" not in ka.record_path(ours.profile).read_text()


@posix_only
def test_f2_the_verdict_carries_the_proxy_from_the_command_line(ours):
    ours.facts["command"] += " --proxy-server=http://alice:pw@proxy.example:8080"
    assert ka.check(ours.profile)["proxy"] == "http://alice:pw@proxy.example:8080"


def test_f2_reattach_reads_the_proxy_from_chrome_itself(srv):
    cmd = CMD.format(udd="/p") + " --proxy-server=http://alice:pw@proxy.example:8080"
    assert srv._keep_alive.proxy_from_command(cmd) == "http://alice:pw@proxy.example:8080"
    assert srv._keep_alive.proxy_from_command(CMD.format(udd="/p")) is None


@posix_only  # arming needs a process start time, which Windows does not give here
def test_f4_no_version_no_keep(srv, armable):
    note = srv._arm_keep_alive(_ArmBrowser(armable, version=None), armable, "main", True, {})
    assert "keep_alive not armed" in note and "version" in note, note
    assert ka.read(armable) is None


@posix_only
def test_f4_a_record_without_a_version_is_refused(ours):
    data = json.loads(ka.record_path(ours.profile).read_text())
    data.pop("chrome_version")
    ka.record_path(ours.profile).write_text(json.dumps(data))
    verdict = ka.check(ours.profile)
    assert verdict["ok"] is False and "Chrome version" in verdict["reason"], verdict


def test_f6_a_new_browser_says_it_closes_with_its_server(srv, tmp_path):
    for value in (None, False):
        keep, note = srv._keep_alive_decision(value, tmp_path, False, False)
        assert not keep and "keep_alive=False" in note and "closes when this server exits" in note, note


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "-3", "0", "abc", "1e400"])
def test_f7_a_cap_that_is_not_a_positive_finite_number_is_refused_and_named(monkeypatch, raw):
    monkeypatch.setenv(ka.ENV_MAX_HOURS, raw)
    assert ka.max_hours() == ka.DEFAULT_MAX_HOURS
    assert ka.ENV_MAX_HOURS in (ka.max_hours_problem() or ""), raw


def test_f7_a_cap_beyond_a_year_is_cut_and_named(monkeypatch):
    monkeypatch.setenv(ka.ENV_MAX_HOURS, "100000")
    assert ka.max_hours() == ka.MAX_HOURS_LIMIT
    assert "cut to" in (ka.max_hours_problem() or "")
    monkeypatch.setenv(ka.ENV_MAX_HOURS, "2.5")
    assert ka.max_hours() == 2.5 and ka.max_hours_problem() is None


@posix_only
def test_f3_a_record_whose_pid_now_runs_something_else_expires(tmp_path, records, monkeypatch):
    """The Chrome is gone and its PID was handed to another program: the record holds nothing."""
    root = tmp_path / "data"
    fallback = root / "profile-pid999999"
    fallback.mkdir(parents=True)
    old = time.time() - 3600
    os.utime(fallback, (old, old))
    monkeypatch.setattr(profile_gc, "_owner_gone", lambda *_a: True)
    ka.write(fallback, **_record(fallback, pid=os.getpid()))  # alive, but this is pytest, not Chrome
    assert os.path.realpath(fallback) not in ka.live_profiles()
    assert ka.read(fallback) is None, "the stale record was kept"
    why, _ = profile_gc.fallback_refusal(fallback, root, processes=[], held=set(), now=time.time())
    assert why is None, why


@posix_only
def test_f3_a_record_whose_chrome_died_expires(tmp_path, records):
    profile = tmp_path / "p"
    profile.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ka.write(profile, **_record(profile, pid=dead.pid))
    assert ka.live_profiles() == set()
    assert ka.read(profile) is None


@posix_only
def test_f3_a_record_whose_command_line_cannot_be_read_still_holds(ours, monkeypatch):
    """Unreadable is not proof of staleness: the profile stays held, the record stays."""
    monkeypatch.setattr(ka._watcher, "process_command", lambda _pid: "")
    assert os.path.realpath(ours.profile) in ka.live_profiles()
    assert ka.read(ours.profile) is not None


@posix_only
def test_f3_a_live_kept_chrome_still_holds_its_profile(ours):
    assert os.path.realpath(ours.profile) in ka.live_profiles()


# ── F1: one reattach at a time, and the browser connected to is the one checked ─────────────

@posix_only
def test_f1_a_second_server_cannot_reattach_while_one_is_taking_it(ours):
    first = ka.check(ours.profile, hold=True)
    try:
        assert first["ok"] is True and first.get("lock_fd") is not None
        second = ka.check(ours.profile, hold=True)
        assert second["ok"] is False and "another server is reattaching" in second["reason"], second
    finally:
        ka.release_lock(first)
    third = ka.check(ours.profile, hold=True)
    ka.release_lock(third)
    assert third["ok"] is True, "the lock outlived the reattach that held it"


@posix_only
def test_f1_the_lock_holds_across_processes_and_dies_with_its_holder(ours, tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time\n"
        "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(30)"),
        str(ka.lock_path(ours.profile))], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        verdict = ka.check(ours.profile, hold=True)
        assert verdict["ok"] is False and "another server is reattaching" in verdict["reason"]
    finally:
        holder.kill()
        holder.wait()
    verdict = ka.check(ours.profile, hold=True)
    ka.release_lock(verdict)
    assert verdict["ok"] is True, "a lock whose holder died still blocked"


@posix_only
def test_f1_a_pid_started_later_than_recorded_is_not_ours(ours):
    ours.facts["start"] = "Fri Nov 13 11:11:11 2026"  # same PID, another process
    verdict = ka.check(ours.profile)
    assert verdict["ok"] is False and "started" in verdict["reason"], verdict


@posix_only
def test_f1_a_record_without_a_start_time_is_refused(ours):
    data = json.loads(ka.record_path(ours.profile).read_text())
    data.pop("pid_start")
    ka.record_path(ours.profile).write_text(json.dumps(data))
    assert "start time" in ka.check(ours.profile)["reason"]


class _Connected:
    """A connected browser answering SystemInfo.getProcessInfo and Browser.getVersion."""
    def __init__(self, pid, product="Chrome/157.0.1.2"):
        self.pid, self.product = pid, product
        self.connection = self

    async def send(self, command):
        method = next(command)["method"]
        if method == "SystemInfo.getProcessInfo":
            reply = {"processInfo": [{"type": "browser", "id": self.pid, "cpuTime": 0},
                                     {"type": "renderer", "id": self.pid + 1, "cpuTime": 0}]}
        else:
            reply = {"protocolVersion": "1.3", "product": self.product, "revision": "r",
                     "userAgent": "ua", "jsVersion": "js"}
        try:
            command.send(reply)
        except StopIteration as done:
            return done.value


@posix_only
@pytest.mark.parametrize("browser, change, reason", [
    (_Connected(5555), None, "is PID 5555"),
    (_Connected(4242, "Chrome/158.0.0.0"), None, "Chrome/158.0.0.0"),
    (_Connected(4242), lambda f: f.update(start="Fri Nov 13 11:11:11 2026"), "started"),
    (_Connected(4242), lambda f: f.update(holder=9999), "lock"),
])
def test_f1_after_connecting_the_browser_must_still_be_the_one_checked(srv, ours, browser, change, reason):
    import asyncio
    record = ka.read(ours.profile)
    if change:
        change(ours.facts)
    with pytest.raises(srv._NotTheKeptBrowser, match=reason):
        asyncio.run(srv._verify_connected(browser, record))


@posix_only
def test_f1_the_right_browser_passes_after_connecting(srv, ours):
    import asyncio
    asyncio.run(srv._verify_connected(_Connected(4242), ka.read(ours.profile)))


# ── F5: a signal only reaches the process that was identified, checked just before ──────────

def _sleeper(*args):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *args])
    deadline = time.time() + 5
    while time.time() < deadline and qw.process_start(proc.pid) is None:
        time.sleep(0.05)
    return proc


@posix_only
def test_f5_signal_needs_the_same_start_time(tmp_path):
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        start = qw.process_start(proc.pid)
        assert start
        names = lambda command: qw.names_profile(command, str(tmp_path))  # noqa: E731
        assert not qw.signal_if_same(proc.pid, 15, "Mon Jan  1 00:00:00 2001", names)
        assert not qw.signal_if_same(proc.pid, 15, None, names), "signalled with no identity"
        assert not qw.signal_if_same(proc.pid, 15, start, lambda _c: False)
        time.sleep(0.2)
        assert proc.poll() is None, "a process that was not proved to be the one was signalled"
        assert qw.signal_if_same(proc.pid, 15, start, names)
        assert proc.wait(5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@posix_only
def test_f5_the_check_runs_right_before_each_signal(tmp_path, monkeypatch):
    """The command line still matches but the start time changed between two looks: no signal."""
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        start = qw.process_start(proc.pid)
        cfg = {"pid": proc.pid, "user_data_dir": str(tmp_path), "host": "127.0.0.1", "port": 1,
               "close_wait": 0.2, "pid_start": start}
        monkeypatch.setattr(qw, "request_browser_close", lambda *_a: False)
        real = qw.process_start
        monkeypatch.setattr(qw, "process_start", lambda pid, like=None: "Mon Jan  1 00:00:00 2001")
        assert qw.end_browser(cfg) == "closed", "a PID started later reads as the browser gone"
        assert proc.poll() is None, "the watcher signalled a process started later"
        monkeypatch.setattr(qw, "process_start", real)
        assert qw.end_browser(cfg) == "signalled"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@posix_only
def test_f5_the_watcher_stops_watching_a_pid_started_later(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qw, "process_command", lambda _pid: f"chrome --user-data-dir={tmp_path / 'p'}")
    monkeypatch.setattr(qw, "process_start", lambda _pid, like=None: "Mon Jan  1 00:00:00 2001")
    monkeypatch.setattr(qw, "end_browser", lambda _cfg: pytest.fail("acted on a reused PID"))
    out = qw.watch(_cfg(tmp_path, pid_start="Fri Nov 13 10:00:00 2026"), sleep=clock.sleep, clock=clock)
    assert out == "pid reused"


@posix_only
def test_f5_close_at_exit_never_signals_an_unproved_process(tmp_path, monkeypatch):
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        monkeypatch.setattr(ka._watcher, "request_browser_close", lambda *_a: False)
        out = ka.close_at_exit("127.0.0.1", 1, proc.pid, str(tmp_path), "Mon Jan  1 00:00:00 2001",
                               wait=0.2)
        assert out == "closed" and proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_f5_end_old_watcher_checks_its_start_time(monkeypatch):
    cfg = json.dumps({"pid": 4242, "host": "127.0.0.1", "port": 50123, "user_data_dir": "/p"})
    watcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", qw.NAME, cfg])
    try:
        time.sleep(0.3)
        record = {"watcher_pid": watcher.pid, "watcher_start": "Mon Jan  1 00:00:00 2001",
                  "pid": 4242, "port": 50123}
        assert ka.end_old_watcher(record) != "ended" and watcher.poll() is None
    finally:
        watcher.kill()
        watcher.wait()


# ── release review findings 5 and 6: the server's own close path ───────────────────────────

def test_a6_descendants_must_name_the_profile_exactly(srv):
    rows = [(100, 1, "chrome --user-data-dir=/tmp/profile"),
            (101, 100, "chrome --type=renderer --user-data-dir=/tmp/profile"),
            (102, 100, "chrome_crashpad_handler --database=/tmp/profile/Crashpad"),
            (103, 100, "chrome --user-data-dir=/tmp/profile-evil"),
            (104, 100, "other --user-data-dir=/tmp/profilex --x")]
    assert sorted(srv._descendant_pids(100, "/tmp/profile", table=rows)) == [101, 102]


class _CloseBrowser:
    def __init__(self, pid, udd, start=None):
        self._process = SimpleNamespace(pid=pid, returncode=None)
        self._process_pid = pid
        self.connection = None
        self.config = SimpleNamespace(host="127.0.0.1", port=1, user_data_dir=udd)
        if start:
            self._mcp_pid_start = start

    @property
    def stopped(self):
        return False

    def stop(self):
        pass


@posix_only
def test_a5_an_unreadable_command_line_is_never_signalled(srv, tmp_path, monkeypatch):
    import asyncio
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        monkeypatch.setattr(srv, "_process_command", lambda _pid: None)  # exists, cannot be read
        report = asyncio.run(srv._ensure_browser_dead(_CloseBrowser(proc.pid, str(tmp_path)), None, grace=0.2))
        time.sleep(0.2)
        assert proc.poll() is None, "a process that could not be identified was signalled"
        assert report["verified"] is False and proc.pid in report["still_alive"], report
        assert srv._close_incomplete(report)
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_f5_the_server_does_not_signal_a_pid_started_later(srv, tmp_path, monkeypatch):
    import asyncio
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        browser = _CloseBrowser(proc.pid, str(tmp_path), start="Mon Jan  1 00:00:00 2001")
        report = asyncio.run(srv._ensure_browser_dead(browser, None, grace=0.2))
        time.sleep(0.2)
        assert proc.poll() is None, "the server signalled a process started after its browser"
        assert report["killed"] == []
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_f5_the_server_still_ends_its_own_browser(srv, tmp_path):
    import asyncio
    import threading
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    threading.Thread(target=proc.wait, daemon=True).start()  # reaped, as asyncio reaps Chrome
    try:
        report = asyncio.run(srv._ensure_browser_dead(_CloseBrowser(proc.pid, str(tmp_path)), None, grace=0.2))
        assert report["verified"] is True and proc.pid in report["killed"], report
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@posix_only
def test_a3_the_server_reads_a_sibling_profile_as_another_program(srv, tmp_path):
    import asyncio
    proc = _sleeper(f"--user-data-dir={tmp_path}-evil")
    try:
        report = asyncio.run(srv._ensure_browser_dead(_CloseBrowser(proc.pid, str(tmp_path)), None, grace=0.2))
        time.sleep(0.2)
        assert proc.poll() is None and report["killed"] == [], report
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_f1_reattach_drops_a_connection_to_another_browser_and_lets_go_of_the_lock(srv, ours, monkeypatch):
    import asyncio
    closed = []

    async def connect(config=None):
        return _Connected(5555)  # the port now answers as another Chrome

    async def close(connection):
        closed.append(connection)

    monkeypatch.setattr(srv.nodriver, "start", connect)
    monkeypatch.setattr(srv, "close_connection", close)
    verdict = ka.check(ours.profile, hold=True)
    assert verdict["ok"]
    with pytest.raises(srv._NotTheKeptBrowser):
        asyncio.run(srv._reattach_kept("main", ours.profile, verdict, url="about:blank",
                                       keep_alive=None, ignored=[]))
    assert closed, "the connection to the wrong browser was kept"
    again = ka.check(ours.profile, hold=True)
    ka.release_lock(again)
    assert again["ok"], "the reattach lock was not released"


@posix_only
def test_f5_close_at_exit_signals_only_the_browser_main_process(tmp_path, monkeypatch):
    """Names the profile and is not gone, but is a helper, not the browser: never signalled."""
    proc = _sleeper(f"--user-data-dir={tmp_path}", "--type=renderer")
    try:
        monkeypatch.setattr(ka._watcher, "request_browser_close", lambda *_a: False)
        out = ka.close_at_exit("127.0.0.1", 1, proc.pid, str(tmp_path), qw.process_start(proc.pid),
                               wait=0.2)
        time.sleep(0.2)
        assert out == "still running" and proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_f5_the_server_reads_a_later_start_as_another_process(srv, tmp_path):
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        same = _CloseBrowser(proc.pid, str(tmp_path), start=qw.process_start(proc.pid))
        later = _CloseBrowser(proc.pid, str(tmp_path), start="Mon Jan  1 00:00:00 2001")
        assert srv._identify(proc.pid, same) == "same"
        assert srv._identify(proc.pid, later) == "different"
    finally:
        proc.kill()
        proc.wait()


@posix_only  # arming needs a process start time, which Windows does not give here
def test_f2_arming_writes_no_proxy_even_before_the_record_drops_one(srv, armable, monkeypatch):
    seen = {}
    real = srv._keep_alive.write
    monkeypatch.setattr(srv._keep_alive, "write", lambda profile, **fields: seen.update(fields) or real(profile, **fields))
    monkeypatch.setattr(srv.BrowserState, "current_proxy", "http://alice:s3cretPW@proxy.example:8080")
    srv._arm_keep_alive(_ArmBrowser(armable), armable, "main", True, {})
    assert "proxy" not in seen


@posix_only  # symlinks need privileges on Windows
def test_processes_naming_holds_the_boundary_for_every_spelling(tmp_path):
    """The boundary bound only to the last spelling (a|b(?=...)), so the first one matched as a
    bare prefix: on the Windows runner /tmp/profile claimed /tmp/profile-evil."""
    from mcp_stealth_chrome import state
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    rows = [(1, 0, f"chrome --user-data-dir={link}-evil"), (2, 0, f"chrome --user-data-dir={real}-evil"),
            (3, 0, f"chrome --user-data-dir={link} --x"), (4, 0, f"chrome --user-data-dir={real}")]
    assert sorted(p for p, _c in state.processes_naming(link, table=rows)) == [3, 4]


# ── second review: K1 to K5 ────────────────────────────────────────────────────────────────

@pytest.fixture
def reattaching(srv, ours, monkeypatch):
    """A reattach that gets as far as the connection, with the watchers and writes observed."""
    seen = {"watchers": [], "ended": []}

    async def connect(config=None):
        return _Connected(4242)

    async def close(_connection):
        seen["closed"] = True

    monkeypatch.setattr(srv.nodriver, "start", connect)
    monkeypatch.setattr(srv, "close_connection", close)
    monkeypatch.setattr(srv.quit_watch, "start", lambda *a, **kw: seen["watchers"].append(kw))
    monkeypatch.setattr(srv._keep_alive, "end_old_watcher",
                        lambda record: seen["ended"].append(record) or "ended")
    return seen


def _reattach(srv, profile):
    import asyncio
    verdict = ka.check(profile, hold=True)
    assert verdict["ok"], verdict
    return asyncio.run(srv._reattach_kept("main", profile, verdict, url="about:blank",
                                          keep_alive=None, ignored=[]))


@posix_only
def test_k1_a_record_that_cannot_name_this_server_aborts_the_reattach(srv, ours, reattaching, monkeypatch):
    monkeypatch.setattr(srv._keep_alive, "write", lambda *_a, **_kw: "OSError: read-only")
    with pytest.raises(srv._ReattachAborted, match="read-only"):
        _reattach(srv, ours.profile)
    assert ka.read(ours.profile)["server_pid"] == 777
    assert reattaching["watchers"] == [] and reattaching["ended"] == [], "watchers touched before the claim"
    assert reattaching.get("closed")
    again = ka.check(ours.profile, hold=True)
    ka.release_lock(again)
    assert again["ok"], "the lock outlived the aborted reattach"


@posix_only
@pytest.mark.parametrize("outcome", ["still running", "PID 778 could not be proved to be its watcher, left alone"])
def test_k1_an_old_watcher_that_will_not_end_aborts_the_reattach(srv, ours, reattaching, monkeypatch, outcome):
    monkeypatch.setattr(srv._keep_alive, "end_old_watcher", lambda _record: outcome)
    with pytest.raises(srv._ReattachAborted, match="watcher"):
        _reattach(srv, ours.profile)
    assert reattaching["watchers"] == [], "a second watcher started beside one still running"
    assert ka.read(ours.profile)["server_pid"] == 777, "the claim was not given back"


@posix_only
def test_k2_a_profile_scan_hit_needs_its_start_time(srv, tmp_path, monkeypatch):
    import asyncio
    import threading
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        browser = _CloseBrowser(0, str(tmp_path))
        browser._process_pid = None
        from mcp_stealth_chrome import state as process_state
        monkeypatch.setattr(process_state, "find_chrome_pids_by_profile", lambda _p: [proc.pid])
        real = qw.process_starts
        monkeypatch.setattr(qw, "process_starts", lambda pids, legacy=False: {})  # start time unreadable
        report = asyncio.run(srv._ensure_browser_dead(browser, tmp_path, grace=0.2))
        time.sleep(0.2)
        assert proc.poll() is None, "a scan hit with no start time was signalled"
        assert proc.pid in report["still_alive"] and report["verified"] is False, report
        monkeypatch.setattr(qw, "process_starts", real)
        report = asyncio.run(srv._ensure_browser_dead(browser, tmp_path, grace=0.2))
        assert proc.pid in report["killed"] and report["verified"] is True, report
    finally:
        if proc.poll() is None:
            proc.kill()


@posix_only  # the quit watcher runs on POSIX only (SIGKILL, SIGHUP)
def test_k3_an_unidentifiable_browser_is_left_after_a_bound_and_the_reason_is_kept(tmp_path, monkeypatch):
    clock = _Clock()
    asked = []
    monkeypatch.setattr(qw, "process_command", lambda _pid: "")  # exists, cannot be read
    monkeypatch.setattr(qw, "page_count", lambda *_a: 0)
    monkeypatch.setattr(qw, "request_browser_close", lambda *_a: asked.append(1) or False)
    monkeypatch.setattr(qw, "wait_gone", lambda *_a, **_kw: False)
    monkeypatch.setattr(qw, "server_alive", lambda _cfg: True)
    cfg = _cfg(tmp_path, pid_start="Fri Nov 13 10:00:00 2026")
    out = qw.watch(cfg, sleep=clock.sleep, clock=clock)
    assert out.startswith("gave up"), out
    assert len(asked) == qw.UNIDENTIFIED_TRIES
    verdict = json.loads(open(cfg["quit_file"]).read())
    assert verdict["not_ended"] and "could not be identified" in verdict["not_ended"]


def test_k3_the_next_tool_call_reports_a_browser_the_watcher_could_not_end(tmp_path, monkeypatch):
    from mcp_stealth_chrome import quit_watch, state
    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCH_DIR", str(tmp_path))
    start = "Fri Nov 13 10:00:00 2026"
    browser = SimpleNamespace(_process=None, _process_pid=4242, _mcp_pid_start=start,
                              config=SimpleNamespace(host="127.0.0.1", port=9, user_data_dir="/p"),
                              connection=SimpleNamespace(closed=False))
    quit_watch._browser_paths(browser)[1].write_text(json.dumps(
        {"pid": 4242, "port": 9, "user_data_dir": "/p", "pid_start": start, "at": 1,
         "not_ended": "its process could not be identified (command line or start time unreadable)"}))
    assert quit_watch.quit_time(browser) is None, "a browser not ended read as quit"
    monkeypatch.setattr(state.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(state.BrowserState, "tabs", [], raising=False)
    monkeypatch.setattr(state.BrowserState, "window_closed", {}, raising=False)
    with pytest.raises(RuntimeError, match="could not be identified") as raised:
        state.BrowserState.active_tab()
    assert "PID 4242" in str(raised.value)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux start ticks from /proc")
def test_k5_linux_start_times_are_clock_ticks_not_seconds():
    start = qw.process_start(os.getpid())
    assert start and start.startswith("ticks:"), start


@pytest.mark.parametrize("first_ok, last_error, expect_none",
                         [(False, 5, True), (False, 18, True), (True, 5, True), (True, 18, False)])
def test_k4_a_process_table_that_failed_to_enumerate_is_unknown(monkeypatch, first_ok, last_error, expect_none):
    """A stand-in kernel32, so this runs everywhere: a failed walk is unknown (None), not empty."""
    import ctypes
    from mcp_stealth_chrome import state

    error = {"last": 0}

    def returning(result):
        def call(*_a):
            error["last"] = last_error
            return result
        return call

    class Lib:
        def __init__(self, _name, use_last_error=False):
            self.CreateToolhelp32Snapshot = lambda *_a: 77
            self.Process32FirstW = returning(1 if first_ok else 0)
            self.Process32NextW = returning(0)
            self.OpenProcess = lambda *_a: 0
            self.CloseHandle = lambda *_a: 1
            self.NtQueryInformationProcess = lambda *_a: 0

    monkeypatch.setattr(ctypes, "WinDLL", Lib, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error["last"], raising=False)
    table = state._win_process_table()
    assert (table is None) is expect_none, table


def test_k4_an_unreadable_process_list_never_deletes_a_privacy_profile(srv, tmp_path, monkeypatch):
    import asyncio
    from mcp_stealth_chrome import state as process_state
    root = tmp_path / "privacy-profiles"
    profile = root / "main-x"
    (profile / "Default").mkdir(parents=True)
    monkeypatch.setattr(srv, "PRIVACY_PROFILE_ROOT", root)
    monkeypatch.setattr(srv, "_PRIVACY_REMOVE_WAIT", 0.3)
    monkeypatch.setattr(process_state, "processes_naming", lambda *_a, **_kw: None)
    assert asyncio.run(srv._remove_privacy_profile(profile)) is False
    assert profile.exists(), "deleted while whether a helper still used it was unknown"
    assert "process list could not be read" in srv._removal_failure(profile)


@posix_only  # the quit watcher runs on POSIX only (SIGKILL, SIGHUP)
def test_k3_a_left_running_verdict_outlives_the_watcher(tmp_path, monkeypatch):
    """The watcher removes its verdict when no server is left to read it, except this one."""
    cfg = _cfg(tmp_path, server_pid=-1)

    def stopped(c, **_kw):
        qw.write_private(c["quit_file"], {"pid": c["pid"], "not_ended": qw.NOT_ENDED})
        return "gave up: identity unreadable"

    monkeypatch.setattr(qw, "watch", stopped)
    monkeypatch.setattr(qw.signal, "signal", lambda *_a: None)
    qw.main(["-", qw.NAME, json.dumps(cfg)])
    assert os.path.exists(cfg["quit_file"])


# ── third review ───────────────────────────────────────────────────────────────────────────

@posix_only
def test_r3_a_failure_after_the_old_watcher_ended_leaves_a_watcher_behind(srv, ours, reattaching, monkeypatch):
    started = []
    monkeypatch.setattr(srv.quit_watch, "start",
                        lambda *a, **kw: started.append(kw) or SimpleNamespace(pid=os.getpid()))

    async def broken(*_a, **_kw):
        raise RuntimeError("the url did not load")

    monkeypatch.setattr(srv, "_reattach_claimed", broken)
    with pytest.raises(RuntimeError, match="did not load"):
        _reattach(srv, ours.profile)
    assert reattaching["ended"], "the old watcher was not ended in this scenario"
    assert len(started) == 1, "the browser was left with no watcher"
    assert started[0].get("keep_alive_record") and started[0].get("max_orphan_seconds")
    record = ka.read(ours.profile)
    assert record["server_pid"] == 777 and record["watcher_pid"] == os.getpid()


@posix_only
def test_r3_a_rollback_that_cannot_be_written_is_said(srv, ours, reattaching, monkeypatch):
    real = ka.write
    calls = {"n": 0}

    def write(profile, **fields):
        calls["n"] += 1
        return real(profile, **fields) if calls["n"] == 1 else "OSError: disk full"

    async def broken(*_a, **_kw):
        raise RuntimeError("the url did not load")

    monkeypatch.setattr(srv._keep_alive, "write", write)
    monkeypatch.setattr(srv, "_reattach_claimed", broken)
    with pytest.raises(RuntimeError) as raised:
        _reattach(srv, ours.profile)
    assert "disk full" in str(raised.value) and "did not load" in str(raised.value)


def test_r3_an_unreadable_process_table_is_an_unknown_family(srv, monkeypatch):
    from mcp_stealth_chrome import state as process_state
    monkeypatch.setattr(process_state, "process_table", lambda: None)
    assert srv._descendant_pids(100, "/tmp/profile") is None


@posix_only
def test_r3_a_close_with_unknown_helpers_is_not_verified(srv, tmp_path, monkeypatch):
    import asyncio
    import threading
    from mcp_stealth_chrome import state as process_state
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        monkeypatch.setattr(process_state, "process_table", lambda: None)
        browser = _CloseBrowser(proc.pid, str(tmp_path), start=qw.process_start(proc.pid))
        report = asyncio.run(srv._ensure_browser_dead(browser, None, grace=0.2))
        assert report["verified"] is False and srv._close_incomplete(report), report
        assert "helper" in report["detail"], report
    finally:
        if proc.poll() is None:
            proc.kill()


@posix_only
def test_r3_a_start_time_in_the_old_format_still_identifies_the_process(tmp_path, monkeypatch):
    """Records written before Linux used clock ticks hold ps lstart text: both are read."""
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    try:
        legacy = qw._ps_starts([proc.pid])[proc.pid]
        monkeypatch.setattr(qw.sys, "platform", "linux")
        monkeypatch.setattr(qw, "_linux_starts", lambda pids: {p: "ticks:123" for p in pids})
        names = qw.browser_of(str(tmp_path))
        assert qw.identity(proc.pid, legacy, names) == "same"
        assert qw.identity(proc.pid, "ticks:123", names) == "same"
        assert qw.identity(proc.pid, "ticks:999", names) == "different"
        assert qw.identity(proc.pid, "Mon Jan  1 00:00:00 2001", names) == "different"
        assert not qw.gone(proc.pid, str(tmp_path), legacy)
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_r3_a_reattach_records_the_start_time_in_the_current_format(srv, ours, reattaching, monkeypatch):
    seen = {}

    async def claimed(browser, *_a, **_kw):
        seen["start"] = getattr(browser, srv.quit_watch.START_ATTR, None)
        return browser, [], 0, {}, "", ""

    legacy = ours.facts["start"]  # the record holds ps lstart text, as before Linux read ticks
    monkeypatch.setattr(ka._watcher, "process_start",
                        lambda pid, like=None: None if pid != 4242 else
                        (legacy if like and not str(like).startswith("ticks:") else "ticks:555"))
    monkeypatch.setattr(srv, "_reattach_claimed", claimed)
    _reattach(srv, ours.profile)
    assert seen["start"] == "ticks:555", "the old format was carried on instead of the current one"


@pytest.fixture
def linux_with_an_old_record(tmp_path, monkeypatch):
    """A process whose recorded start time is ps lstart text while this "Linux" reads ticks."""
    proc = _sleeper(f"--user-data-dir={tmp_path}")
    legacy = qw._ps_starts([proc.pid])[proc.pid]
    monkeypatch.setattr(qw.sys, "platform", "linux")
    monkeypatch.setattr(qw, "_linux_starts", lambda pids: {p: "ticks:123" for p in pids})
    yield SimpleNamespace(proc=proc, legacy=legacy, profile=str(tmp_path))
    proc.kill()
    proc.wait()


@posix_only
def test_r3_the_watcher_keeps_watching_a_browser_recorded_in_the_old_format(linux_with_an_old_record, tmp_path, monkeypatch):
    old = linux_with_an_old_record
    monkeypatch.setattr(qw, "page_count", lambda *_a: 1)
    calls = {"n": 0}

    class Enough(Exception):
        pass

    def sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > 3:
            raise Enough

    with pytest.raises(Enough):
        qw.watch(_cfg(tmp_path, pid=old.proc.pid, user_data_dir=old.profile, pid_start=old.legacy),
                 sleep=sleep)


@posix_only
def test_r3_profile_gc_still_sees_a_kept_browser_recorded_in_the_old_format(linux_with_an_old_record, monkeypatch):
    old = linux_with_an_old_record
    monkeypatch.setattr(ka._state, "chrome_lock_holder_pid", lambda _p: old.proc.pid)
    monkeypatch.setattr(ka._state, "_is_browser_main_process", lambda _c: True)
    record = {"pid": old.proc.pid, "user_data_dir": old.profile, "pid_start": old.legacy}
    assert ka.still_running(record) is True


@posix_only
def test_r3_the_server_identifies_a_browser_recorded_in_the_old_format(srv, linux_with_an_old_record):
    old = linux_with_an_old_record
    browser = _CloseBrowser(old.proc.pid, old.profile, start=old.legacy)
    assert srv._identify(old.proc.pid, browser) == "same"
