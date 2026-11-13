"""Removing a privacy profile once its browser is gone, and naming what still holds it.

Windows refuses to delete a file while any handle to it is open. On a CI runner a Chrome that
did not quit on Browser.close was ended, its helpers still held the profile's files (ps does
not exist there, so they were never waited for), the removal gave up after about a second
with WinError 32, and the next launch refused: "could not remove stale privacy profile".
The deletion is stubbed in most of these, so they hold on every OS.
"""
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from mcp_stealth_chrome import server, state

_real_naming = state.processes_naming

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason=(
    "Toolhelp, NtQueryInformationProcess and Restart Manager exist only on Windows"))
LOCKED = "[WinError 32] The process cannot access the file because it is being used by another process"


def _rmtree_locked(times):
    """shutil.rmtree failing as on Windows while a handle is open, `times` times, then working."""
    real = shutil.rmtree
    calls = []

    def rmtree(path, *args, **kwargs):
        calls.append(str(path))
        if times is None or len(calls) <= times:
            error = PermissionError(13, LOCKED)
            error.filename = os.path.join(str(path), "Default", "Extension Rules", "000003.log")
            raise error
        return real(path, *args, **kwargs)

    return rmtree, calls


@pytest.fixture
def profile(monkeypatch, tmp_path):
    root = tmp_path / "privacy-profiles"
    folder = root / "main-abc12345"
    (folder / "Default" / "Extension Rules").mkdir(parents=True)
    (folder / "Default" / "Extension Rules" / "000003.log").write_text("x")
    monkeypatch.setattr(server, "PRIVACY_PROFILE_ROOT", root)
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_DELAYS", (0.01, 0.02, 0.05))
    monkeypatch.setattr(state, "processes_naming", lambda path, table=None: [])
    monkeypatch.setattr(state, "_win_file_holders", lambda paths: None)
    return folder


def test_a_profile_locked_for_a_while_is_removed_once_it_is_released(monkeypatch, profile):
    """Measured on the runner: the folder could be removed 0.2 to 2 s after Chrome was ended."""
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 5.0)
    rmtree, calls = _rmtree_locked(8)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    assert asyncio.run(server._remove_privacy_profile(profile)) is True
    assert not profile.exists()
    assert len(calls) == 9, calls
    assert server._removal_failure(profile) == ""


def test_a_profile_that_stays_locked_is_reported_with_what_holds_it(monkeypatch, profile):
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.3)
    rmtree, _calls = _rmtree_locked(None)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    held = []

    def holders(paths):
        held.extend(paths)
        return [(4321, "Google Chrome"), (77, "")]

    monkeypatch.setattr(state, "_win_file_holders", holders)
    monkeypatch.setattr(state, "process_table", lambda: [
        (77, 4, r"C:\Windows\System32\SearchProtocolHost.exe Global\UsGthrFltPipeMssGthrPipe1")])
    started = time.monotonic()
    assert asyncio.run(server._remove_privacy_profile(profile)) is False
    assert time.monotonic() - started < 3, "the wait is bounded"
    assert profile.exists()
    reason = server._removal_failure(profile)
    assert "being used by another process" in reason, reason
    assert "PID 4321 (Google Chrome)" in reason, reason
    assert "PID 77 (C:\\Windows\\System32\\SearchProtocolHost.exe" in reason, reason
    assert any(p.endswith("000003.log") for p in held), "the files left are what is asked about"


def test_nothing_is_deleted_while_a_process_still_runs_with_the_profile(monkeypatch, profile):
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.3)
    rmtree, calls = _rmtree_locked(0)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    helper = (999, f"chrome.exe --type=renderer --user-data-dir={profile} --lang=en-US")
    monkeypatch.setattr(state, "processes_naming", lambda path, table=None: [helper])
    assert asyncio.run(server._remove_privacy_profile(profile)) is False
    assert calls == [], "deleted under a live process, which writes the files back"
    assert profile.exists()
    reason = server._removal_failure(profile)
    assert "still runs with it" in reason and "PID 999" in reason, reason


def test_nothing_is_deleted_when_the_process_list_cannot_be_read(monkeypatch, profile):
    """None from processes_naming means the list could not be read (a failed Toolhelp snapshot
    on Windows, no ps elsewhere), not that nothing runs: `or []` once turned it into "free"."""
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.3)
    rmtree, calls = _rmtree_locked(0)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(state, "processes_naming", lambda path, table=None: None)
    assert asyncio.run(server._remove_privacy_profile(profile)) is False
    assert calls == [], "deleted although nobody knows whether Chrome still runs"
    assert profile.exists()
    reason = server._removal_failure(profile)
    assert "process list could not be read" in reason, reason


def test_a_browser_process_whose_command_line_is_unreadable_counts_as_holding_it(monkeypatch, profile):
    """Windows answers an empty command line when OpenProcess is refused. A chrome.exe whose
    command line could not be read may be one of this profile's helpers."""
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.3)
    rmtree, calls = _rmtree_locked(0)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(state, "processes_naming", _real_naming)
    monkeypatch.setattr(state, "process_table", lambda: [
        (4, 0, "System"), (999, 4, state.UNREADABLE_COMMAND.format(image="chrome.exe"))])
    assert asyncio.run(server._remove_privacy_profile(profile)) is False
    assert calls == []
    assert "PID 999" in server._removal_failure(profile)


def test_removal_waits_for_the_last_helper_to_end(monkeypatch, profile):
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 5.0)
    rmtree, calls = _rmtree_locked(0)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    looks = []

    def naming(path, table=None):
        looks.append(path)
        return [(999, f"chrome.exe --type=crashpad-handler --database={profile}\\Crashpad")] \
            if len(looks) < 4 else []

    monkeypatch.setattr(state, "processes_naming", naming)
    assert asyncio.run(server._remove_privacy_profile(profile)) is True
    assert len(looks) >= 4 and len(calls) == 1
    assert not profile.exists()


def test_the_processes_are_looked_up_by_the_path_chrome_was_given(monkeypatch, profile, tmp_path):
    """Resolving changes a path's spelling (a Windows 8.3 short name becomes the long one, a
    link its target), and Chrome's command line holds the spelling it was given."""
    link = tmp_path / "link"
    try:
        link.symlink_to(profile.parent, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot make a symbolic link")
    monkeypatch.setattr(server, "PRIVACY_PROFILE_ROOT", link)
    asked = []

    def naming(path, table=None):
        asked.append(str(path))
        return []

    monkeypatch.setattr(state, "processes_naming", naming)
    given = link / profile.name
    assert asyncio.run(server._remove_privacy_profile(given)) is True
    assert asked and asked[0] == str(given), asked


def test_a_relaunch_names_what_holds_the_stale_profile(monkeypatch, profile):
    """The error the CI run showed, now with its cause."""
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.2)
    rmtree, _calls = _rmtree_locked(None)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(state, "_win_file_holders", lambda paths: [(4321, "Google Chrome")])
    for name, value in (("browser", None), ("tabs", []), ("current_privacy", True),
                        ("current_profile_dir", profile), ("current_instance_id", "main")):
        monkeypatch.setattr(server.BrowserState, name, value)
    launch = server.mcp._tool_manager._tools["browser_launch"].fn
    out = asyncio.run(launch(headless=True, persistent=False))
    assert out.startswith("Error: could not remove stale privacy profile before relaunch"), out
    assert "being used by another process" in out and "PID 4321 (Google Chrome)" in out, out
    assert server.BrowserState.current_privacy is True, "the cleanup stays owed"


def test_the_posix_removal_keeps_its_three_tries(monkeypatch, profile):
    monkeypatch.setattr(server, "_PRIVACY_REMOVE_WAIT", 0.0)
    rmtree, calls = _rmtree_locked(None)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    assert asyncio.run(server._remove_privacy_profile(profile)) is False
    assert len(calls) == 3


def test_a_path_outside_the_privacy_folder_is_never_removed(monkeypatch, profile, tmp_path):
    rmtree, calls = _rmtree_locked(0)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert asyncio.run(server._remove_privacy_profile(outside)) is False
    assert asyncio.run(server._remove_privacy_profile(profile.parent)) is False
    assert calls == [] and outside.exists()


# ── the process helpers on the real system ──────────────────────────────────

_NAP = "import time; time.sleep(60)"


def _end(*pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass


def _wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    return predicate()


def test_a_process_is_found_by_the_profile_it_names_and_not_by_a_longer_one(tmp_path):
    profile = tmp_path / "profile-x"
    proc = subprocess.Popen([sys.executable, "-c", _NAP, f"--user-data-dir={profile}"])
    try:
        found = _wait_for(lambda: [pid for pid, _c in state.processes_naming(profile) or []])
        assert proc.pid in found, found
        shorter = [pid for pid, _c in state.processes_naming(tmp_path / "profile") or []]
        assert proc.pid not in shorter, "profile claimed profile-x"
    finally:
        proc.kill()
        proc.wait()


def test_the_family_of_a_browser_is_found_on_every_system(tmp_path):
    """Windows has no ps, so this found nothing there and the helpers were never waited for.

    A launcher naming the profile starts one helper that does and one program that does not.
    Counted by command line, not by number: a venv's python.exe on Windows is itself a launcher
    that starts the real interpreter as its child.
    """
    profile = str(tmp_path / "profile")
    flag = f"--user-data-dir={profile}"
    script = tmp_path / "launcher.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_NAP!r}, sys.argv[1], 'helper'])\n"
        f"subprocess.Popen([sys.executable, '-c', {_NAP!r}, 'updater-wake'])\n"
        "time.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, str(script), flag])

    def commands(pids):
        table = {pid: command for pid, _ppid, command in state.process_table() or []}
        return {pid: table.get(pid, "") for pid in pids}

    everyone = []
    try:
        def both_started():
            seen = commands(server._descendant_pids(parent.pid))
            texts = list(seen.values())
            if any("updater-wake" in t for t in texts) and any(t.endswith("helper") for t in texts):
                return seen
            return None

        everyone = _wait_for(both_started)
        assert everyone, "the launcher's children were not found"
        own = commands(server._descendant_pids(parent.pid, profile))
        assert any(t.endswith("helper") for t in own.values()), (own, everyone)
        assert not any("updater-wake" in t for t in own.values()), own
        assert all(profile in t for t in own.values()), own
    finally:
        _end(parent.pid, *(everyone or {}))
        parent.wait()


@WINDOWS_ONLY
def test_windows_reads_this_process_and_its_command_line():
    rows = state.process_table()
    assert rows, "no process table"
    mine = [row for row in rows if row[0] == os.getpid()]
    assert mine and mine[0][1] == os.getppid(), mine
    assert "pytest" in mine[0][2] or "python" in mine[0][2].lower(), mine


@WINDOWS_ONLY
def test_windows_names_the_process_holding_a_file_open(tmp_path):
    path = tmp_path / "held.txt"
    with open(path, "w") as handle:
        handle.write("x")
        handle.flush()
        holders = state._win_file_holders([str(path)])
        assert holders is not None and os.getpid() in [pid for pid, _app in holders], holders
    assert state._win_file_holders([str(path)]) == []


def test_a_toolhelp_walk_that_fails_is_not_an_empty_process_list():
    """Process32FirstW failing, or Process32NextW stopping on an error other than
    ERROR_NO_MORE_FILES, once read as "no process runs", and the profile was deleted."""
    def walk(first_ok, items, last):
        left = list(items)
        current = {}

        def first():
            if not first_ok:
                return False
            current["row"] = left.pop(0)
            return True

        def following():
            if not left:
                return False
            current["row"] = left.pop(0)
            return True

        return state._walk_snapshot(first, following, lambda: current["row"], lambda: last)

    assert walk(True, [(4, 0, "System"), (99, 4, "chrome.exe")], 18) == [(4, 0, "System"), (99, 4, "chrome.exe")]
    assert walk(False, [], 18) is None, "Process32FirstW failed"
    assert walk(True, [(4, 0, "System")], 5) is None, "ERROR_ACCESS_DENIED mid-walk"


def test_only_the_removal_counts_an_unreadable_browser_as_a_holder(tmp_path):
    """processes_naming answers which processes name the profile, which the family check of a
    close compares against. An unreadable chrome.exe of another browser on the machine is not
    one of them: counted there, the Windows runner saw a PID outside the family. Only the
    deletion, which must not guess, treats it as possibly holding the profile."""
    profile = tmp_path / "privacy-profiles" / "main-abc"
    table = [(4, 0, "System"), (321, 4, f"chrome.exe --user-data-dir={profile} --type=renderer"),
             (999, 4, state.UNREADABLE_COMMAND.format(image="chrome.exe"))]
    assert [pid for pid, _c in state.processes_naming(profile, table)] == [321]
    assert sorted(pid for pid, _c in state.processes_holding(profile, table)) == [321, 999]
    assert state.processes_holding(profile, []) == []
