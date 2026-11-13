"""Fast unit coverage for the agent-facing snapshot and identity helpers."""

import json
import sys
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome.snapshot import element_fingerprint, format_snapshot

# Issue-style reasons for what does not hold on Windows yet. Each names the code to change.
POSIX_PROCESS_CHECKS = pytest.mark.skipif(sys.platform == "win32", reason=(
    "These fake the POSIX probe, os.kill(pid, 0), and SIGKILL. On Windows _process_exists asks "
    "OpenProcess/GetExitCodeProcess instead (state._win_pid_alive) and there is no SIGKILL; "
    "the e2e close tests cover it there."))
POSIX_MODE_BITS = pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows keeps no POSIX mode bits: chmod only sets read-only and stat reports 0o666, so "
    "0600 cannot be written or checked; the user profile's ACLs are what protect the file."))



def test_managed_output_paths_and_instance_names_cannot_escape_roots(tmp_path):
    """Managed output/profile parameters are names, not arbitrary write paths."""
    from mcp_stealth_chrome.helpers import confined_output_path
    from mcp_stealth_chrome.server import _direct_child, _identity_path

    root = tmp_path / "exports"
    root.mkdir()
    assert confined_output_path(root, "report.json") == root / "report.json"
    assert _direct_child(root, "Profile 1", "profile") == root / "Profile 1"

    with pytest.raises(ValueError):
        confined_output_path(root, "../escaped.json")
    with pytest.raises(ValueError):
        confined_output_path(root, str(tmp_path / "absolute.json"))
    with pytest.raises(ValueError):
        _direct_child(root, "../Default", "profile")
    with pytest.raises(ValueError):
        _direct_child(root, r"..\Default", "profile")
    with pytest.raises(ValueError):
        _identity_path("..")


def test_format_snapshot_delta_prefix_and_count():
    elements = [
        {"ref": "e0", "tag": "button", "text": "Old"},
        {"ref": "e1", "tag": "button", "text": "New"},
    ]
    changed = {element_fingerprint(elements[1])}
    rendered = format_snapshot(
        elements,
        "https://example.test/",
        "Example",
        changed_fingerprints=changed,
    )
    assert "Interactive elements (2, 1 changed)" in rendered
    assert "  ref=e0" in rendered
    assert '* ref=e1  [button] "New"' in rendered


def test_format_snapshot_first_delta_marks_everything():
    elements = [
        {"ref": "e0", "tag": "a", "text": "One"},
        {"ref": "e1", "tag": "input", "placeholder": "Two"},
    ]
    rendered = format_snapshot(
        elements,
        "about:blank",
        "",
        changed_fingerprints={element_fingerprint(el) for el in elements},
    )
    assert "Interactive elements (2, 2 changed)" in rendered
    assert len([line for line in rendered.splitlines() if line.startswith("* ref=")]) == 2


def test_format_snapshot_unchanged_preserves_real_element_count():
    rendered = format_snapshot(
        [],
        "https://example.test/",
        "Example",
        unchanged_from="abc123",
        changed_fingerprints=set(),
        total_count=7,
    )
    assert "Interactive elements (7, unchanged; details omitted)" in rendered
    assert "Interactive elements (0" not in rendered


def test_wait_for_human_inherits_timeout_without_a_magic_value():
    """An explicit timeout must survive.

    The first cut keyed the inherit path off `timeout == 300`, so a caller who
    genuinely wanted 300s silently got the stored handoff timeout instead. The
    contract is now `None means inherit`, which cannot collide with a real value.
    """
    import inspect

    from mcp_stealth_chrome.server import mcp

    sig = inspect.signature(mcp._tool_manager._tools["wait_for_human"].fn)
    assert sig.parameters["timeout"].default is None


def test_wait_for_human_preserves_zero_second_handoff_timeout(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Target:
        target_id = "HUMAN-ZERO"

    class _Tab:
        target = _Target()

    class _Loop:
        def time(self):
            return 0.0

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
    monkeypatch.setattr(server.asyncio, "get_event_loop", lambda: _Loop())
    server._HUMAN_HELP["HUMAN-ZERO"] = {
        "baseline": {"url": "before", "nodes": 1, "len": 1},
        "timeout": 0.0,
    }
    try:
        wait = server.mcp._tool_manager._tools["wait_for_human"].fn
        result = asyncio.run(wait(done_url_contains="would-match-if-polled"))
        assert result.startswith("Error: timeout after 0.0s")
    finally:
        server._HUMAN_HELP.pop("HUMAN-ZERO", None)


def test_focus_and_handoff_do_not_claim_unconfirmed_foreground(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Target:
        target_id = "NO-FOCUS"

    class _Tab:
        target = _Target()

        async def activate(self):
            raise RuntimeError("activate failed")

        async def bring_to_front(self):
            raise RuntimeError("front failed")

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
    monkeypatch.setattr(server.BrowserState, "current_headless", False)
    monkeypatch.setattr(server.sys, "platform", "linux")
    focus = server.mcp._tool_manager._tools["tab_focus"].fn
    handoff = server.mcp._tool_manager._tools["request_human_help"].fn

    focus_result = asyncio.run(focus())
    handoff_result = asyncio.run(handoff(objective="finish login"))
    assert focus_result.startswith("Error: could not confirm")
    assert handoff_result.startswith("Error: human help unavailable")
    assert "NO-FOCUS" not in server._HUMAN_HELP


def test_browser_snapshot_rejects_unmeasured_invalid_json(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    from mcp_stealth_chrome.refs import parse_frame_payload

    async def _collect(_tab, _options):
        return parse_frame_payload("not-json")

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: object()),
    )
    from mcp_stealth_chrome.tools import page

    monkeypatch.setattr(page, "collect_snapshot", _collect)
    snapshot = server.mcp._tool_manager._tools["browser_snapshot"].fn
    result = asyncio.run(snapshot())
    assert result.startswith("Error: snapshot script returned invalid JSON")


def test_target_key_is_shared_and_console_alias_delegates():
    """Per-tab caches must agree on one key, or they desync after navigation."""
    from mcp_stealth_chrome.server import _console_target_key, _target_key

    class _Target:
        target_id = "TARGET-123"

    class _Tab:
        target = _Target()

    tab = _Tab()
    assert _target_key(tab) == "TARGET-123"
    assert _console_target_key(tab) == _target_key(tab)

    # No CDP target yet: fall back to object identity rather than crashing.
    class _Bare:
        pass

    bare = _Bare()
    assert _target_key(bare) == str(id(bare))


def test_per_target_handlers_do_not_duplicate_and_network_mode_reconfigures(monkeypatch):
    """A replacement Tab wrapper for one CDP target must not stack handlers."""
    import asyncio
    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    class _Target:
        target_id = "TARGET-STABLE"

    class _Tab:
        target = _Target()

        def __init__(self):
            self.handlers = []

        async def send(self, _command):
            return None

        def add_handler(self, event_type, handler):
            self.handlers.append((event_type, handler))

        async def send(self, _command):
            return None

    first = _Tab()
    replacement = _Tab()
    active = iter((first, replacement))
    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: next(active)),
    )

    server._NETWORK_ARMED_TAB_IDS.clear()
    server._NETWORK_CAPTURE_BODIES.clear()
    old_logs = server.BrowserState.network_logs
    old_index = server.BrowserState.network_index
    try:
        network_start = server.mcp._tool_manager._tools["network_start"].fn
        asyncio.run(network_start(capture_bodies=False))
        assert len(first.handlers) == 2

        asyncio.run(network_start(capture_bodies=True))
        assert replacement.handlers == []

        request_handler = first.handlers[0][1]
        event = SimpleNamespace(
            request_id="request-1",
            request=SimpleNamespace(
                headers={}, url="https://example.test/", method="GET",
                post_data=None,
            ),
            type_="Document",
            timestamp=1,
        )
        asyncio.run(request_handler(event))
        assert "request-1" in server.BrowserState.network_index
    finally:
        server._NETWORK_ARMED_TAB_IDS.clear()
        server._NETWORK_CAPTURE_BODIES.clear()
        server.BrowserState.network_logs = old_logs
        server.BrowserState.network_index = old_index


def test_dialog_handlers_use_stable_target_identity(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Target:
        target_id = "DIALOG-TARGET"

    class _Tab:
        target = _Target()

        def __init__(self):
            self.handlers = []

        def add_handler(self, event_type, handler):
            self.handlers.append((event_type, handler))

        async def send(self, _command):
            return None

    first = _Tab()
    replacement = _Tab()
    active = iter((first, replacement))
    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: next(active)),
    )
    server._dialog_pre_action["_registered_tab_ids"].clear()
    try:
        dialog_handle = server.mcp._tool_manager._tools["dialog_handle"].fn
        asyncio.run(dialog_handle(action="accept"))
        asyncio.run(dialog_handle(action="dismiss"))
        assert len(first.handlers) == 1
        assert replacement.handlers == []
    finally:
        server._dialog_pre_action["_registered_tab_ids"].clear()


def test_devtools_active_state_survives_tab_wrapper_replacement(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import devtools

    class _Target:
        target_id = "DEVTOOLS-TARGET"

    class _Tab:
        target = _Target()

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
    devtools._COVERAGE_ACTIVE.update({
        "tab_id": "DEVTOOLS-TARGET", "js": False, "css": False,
    })
    coverage_stop = server.mcp._tool_manager._tools["coverage_stop"].fn
    result = asyncio.run(coverage_stop())
    assert not result.startswith("Error:")
    assert devtools._COVERAGE_ACTIVE["tab_id"] is None


def test_tab_cache_cleanup_preserves_other_live_targets(monkeypatch):
    from mcp_stealth_chrome import server

    class _TargetA:
        target_id = "TARGET-A"

    class _TargetB:
        target_id = "TARGET-B"

    class _TabA:
        target = _TargetA()

    from mcp_stealth_chrome.tools import page

    monkeypatch.setattr(page, "_SNAPSHOT_CACHE", {
        "TARGET-A": {"hash": "a"}, "TARGET-B": {"hash": "b"},
    })
    monkeypatch.setattr(server, "_HUMAN_HELP", {
        "TARGET-A": {"objective": "a"}, "TARGET-B": {"objective": "b"},
    })
    monkeypatch.setattr(server, "_NETWORK_ARMED_TAB_IDS", {"TARGET-A", "TARGET-B"})
    monkeypatch.setattr(server, "_NETWORK_CAPTURE_BODIES", {
        "TARGET-A": True, "TARGET-B": False,
    })

    server._clear_tab_scoped_state([_TabA()])

    assert set(page._SNAPSHOT_CACHE) == {"TARGET-B"}
    assert set(server._HUMAN_HELP) == {"TARGET-B"}
    assert server._NETWORK_ARMED_TAB_IDS == {"TARGET-B"}
    assert server._NETWORK_CAPTURE_BODIES == {"TARGET-B": False}


def test_trace_stop_removes_handlers_when_cdp_end_fails(monkeypatch):
    import asyncio
    from collections import defaultdict

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import devtools

    class _Target:
        target_id = "TRACE-TARGET"

    class _Tab:
        target = _Target()

        def __init__(self):
            self.handlers = defaultdict(list)

        def add_handler(self, event_type, handler):
            self.handlers[event_type].append(handler)

        def remove_handler(self, event_type, handler):
            self.handlers[event_type].remove(handler)

        async def send(self, _command):
            raise RuntimeError("Tracing.end failed")

    tab = _Tab()
    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: tab),
    )
    devtools._TRACE_ACTIVE.update({
        "tab_id": "TRACE-TARGET", "started_at": 1.0,
        "categories": "x", "handler": lambda _event: None,
    })
    stop = server.mcp._tool_manager._tools["performance_trace_stop"].fn
    result = asyncio.run(stop())
    assert result.startswith("Error: Tracing.end failed")
    assert all(not handlers for handlers in tab.handlers.values())


def test_browser_roots_are_all_reported_and_selectable(monkeypatch):
    """Only surveying the first browser hides the profiles people actually use.

    A near-empty Chrome routinely sits alongside the Brave/Edge install holding
    the real logged-in sessions, so returning one root made the tool look like it
    had scanned the machine while omitting everything worth cloning.
    """
    import pathlib

    from mcp_stealth_chrome import state

    fake = [("chrome", pathlib.Path("/tmp/chrome")),
            ("brave", pathlib.Path("/tmp/brave"))]
    monkeypatch.setattr(state, "chrome_user_data_roots", lambda: fake)

    assert state.chrome_user_data_root() == fake[0][1]          # first by default
    assert state.chrome_user_data_root("brave") == fake[1][1]   # named root wins
    assert state.chrome_user_data_root("edge") is None          # honest miss, not a guess


def test_profile_survey_uses_root_lock_and_network_cookie_db(monkeypatch, tmp_path):
    import asyncio

    from mcp_stealth_chrome import server

    root = tmp_path / "Chrome"
    profile = root / "Default"
    (profile / "Network").mkdir(parents=True)
    (profile / "Network" / "Cookies").write_bytes(b"x" * 3072)
    (root / "Local State").write_text(json.dumps({
        "profile": {
            "profiles_order": ["Default"],
            "info_cache": {"Default": {"name": "Person 1"}},
        }
    }))
    monkeypatch.setattr(
        server, "chrome_user_data_roots", lambda: [("chrome", root)]
    )
    monkeypatch.setattr(
        server, "is_chrome_profile_locked", lambda path: path == root
    )

    survey = server.mcp._tool_manager._tools["list_chrome_profiles"].fn
    payload = json.loads(asyncio.run(survey()))
    assert payload["browsers"][0]["running"] is True
    assert payload["profiles"][0]["in_use"] is True
    assert payload["profiles"][0]["cookies_kb"] == 3


def test_windows_brave_binary_and_opera_profile_root(monkeypatch, tmp_path):
    import sys

    from mcp_stealth_chrome import state

    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    brave = local / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    opera = roaming / "Opera Software" / "Opera Stable"
    brave.parent.mkdir(parents=True)
    brave.write_bytes(b"")
    opera.mkdir(parents=True)
    (opera / "Local State").write_text("{}")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))

    assert state.find_chrome_binary() == str(brave)
    assert ("opera", opera) in state.chrome_user_data_roots()


def test_close_instance_keeps_failed_browser_for_retry(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import InstanceSnapshot

    class _BrokenBrowser:
        def stop(self):
            raise RuntimeError("still alive")

    snap = InstanceSnapshot(
        instance_id="worker", browser=_BrokenBrowser(), tabs=[object()]
    )
    monkeypatch.setattr(server.BrowserState, "instances", {"worker": snap})
    monkeypatch.setattr(server.BrowserState, "current_instance_id", "main")
    close = server.mcp._tool_manager._tools["close_instance"].fn
    result = asyncio.run(close("worker"))
    assert result.startswith("Error: close failed")
    assert server.BrowserState.instances["worker"] is snap
    assert snap.browser is not None


def test_close_instance_retains_failed_privacy_cleanup(monkeypatch, tmp_path):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import InstanceSnapshot

    class _Browser:
        def stop(self):
            return None

    async def _cleanup_fails(_path):
        return False

    profile = tmp_path / "privacy"
    snap = InstanceSnapshot(
        instance_id="private", browser=_Browser(), tabs=[object()],
        profile_dir=profile, privacy=True,
    )
    monkeypatch.setattr(server.BrowserState, "instances", {"private": snap})
    monkeypatch.setattr(server.BrowserState, "current_instance_id", "main")
    monkeypatch.setattr(server, "_remove_privacy_profile", _cleanup_fails)
    close = server.mcp._tool_manager._tools["close_instance"].fn
    result = asyncio.run(close("private"))
    assert result.startswith("Error: closed browser but failed")
    assert server.BrowserState.instances["private"] is snap
    assert snap.browser is None
    assert snap.tabs == []


def test_close_current_retains_failed_privacy_cleanup(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    class _Browser:
        _process = SimpleNamespace(returncode=None)
        stopped = False

        def stop(self):
            return None

    async def _cleanup_fails(_path):
        return False

    profile = tmp_path / "privacy-current"
    monkeypatch.setattr(server.BrowserState, "current_instance_id", "private")
    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    monkeypatch.setattr(server.BrowserState, "tabs", [object()])
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", profile)
    monkeypatch.setattr(server.BrowserState, "current_privacy", True)
    monkeypatch.setattr(server, "_remove_privacy_profile", _cleanup_fails)
    close = server.mcp._tool_manager._tools["close_instance"].fn
    result = asyncio.run(close("private"))
    assert result.startswith("Error: closed browser but failed")
    assert server.BrowserState.current_privacy is True
    assert server.BrowserState.current_profile_dir == profile


def test_close_all_reports_and_retains_stop_failures(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import InstanceSnapshot

    class _BrokenBrowser:
        def stop(self):
            raise RuntimeError("refused")

    snap = InstanceSnapshot(
        instance_id="stuck", browser=_BrokenBrowser(), tabs=[object()]
    )
    monkeypatch.setattr(server.BrowserState, "instances", {"stuck": snap})
    monkeypatch.setattr(server.BrowserState, "browser", None)
    monkeypatch.setattr(server.BrowserState, "tabs", [])
    monkeypatch.setattr(server.BrowserState, "current_privacy", False)
    close_all = server.mcp._tool_manager._tools["close_all_instances"].fn
    result = asyncio.run(close_all())
    assert result.startswith("Error: closed 0 instance(s)")
    assert server.BrowserState.instances["stuck"] is snap


def test_instance_snapshot_does_not_report_dead_browser_running():
    from types import SimpleNamespace

    from mcp_stealth_chrome.state import InstanceSnapshot

    launched = SimpleNamespace(
        _process=object(), stopped=True,
    )
    attached = SimpleNamespace(
        _process=None, connection=SimpleNamespace(closed=True),
    )
    assert InstanceSnapshot(browser=launched, tabs=[object()], instance_id="a").is_running() is False
    assert InstanceSnapshot(browser=attached, tabs=[object()], instance_id="b").is_running() is False


def test_spawn_refuses_stale_instance_slot_before_launch(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import InstanceSnapshot

    calls = []

    async def _unexpected_launch(**kwargs):
        calls.append(kwargs)
        return True, "launched"

    monkeypatch.setattr(server.BrowserState, "current_instance_id", "main")
    monkeypatch.setattr(
        server.BrowserState,
        "instances",
        {"worker": InstanceSnapshot(instance_id="worker")},
    )
    monkeypatch.setattr(server, "_launch_browser_instance", _unexpected_launch)
    spawn = server.mcp._tool_manager._tools["spawn_browser"].fn
    result = asyncio.run(spawn(instance_id="worker"))
    assert result.startswith("Error: instance 'worker' already exists")
    assert calls == []


def test_named_ephemeral_launch_does_not_claim_or_create_persistent_profile(monkeypatch, tmp_path):
    from mcp_stealth_chrome import server

    assert server._instance_profile_path(
        "worker", persistent=False, privacy=False, profile_dir_override=None,
    ) is None

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # what expanduser reads on Windows
    override = server._instance_profile_path(
        "worker", persistent=False, privacy=False,
        profile_dir_override="~/chosen-profile",
    )
    assert override == tmp_path / "chosen-profile"


def test_sqlite_snapshot_copies_wal_and_verifies(tmp_path):
    """Cloning a live profile must not depend on Connection.backup().

    That API blocks inside C on a database a running browser keeps writing and
    ignores signals, so it can hang indefinitely. A byte copy plus the -wal
    companion returns immediately and still yields a readable database — but it
    is only worth trusting because the copy is integrity-checked afterwards.
    """
    import sqlite3

    from mcp_stealth_chrome.server import _is_sqlite, _sqlite_snapshot

    src = tmp_path / "Cookies"
    conn = sqlite3.connect(src)
    conn.execute("pragma journal_mode=WAL")
    conn.execute("create table cookies (host_key text)")
    conn.executemany("insert into cookies values (?)", [(".x.com",), (".threads.com",)])
    conn.commit()

    dst = tmp_path / "out" / "Cookies"
    dst.parent.mkdir()
    assert _is_sqlite(src)
    assert _sqlite_snapshot(src, dst) is True
    if (src.with_name("Cookies-wal")).exists():
        assert dst.with_name("Cookies-wal").exists(), "WAL companion must travel with the db"

    copied = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    assert copied.execute("select count(*) from cookies").fetchone()[0] == 2
    copied.close()
    conn.close()

    plain = tmp_path / "notes.txt"
    plain.write_text("not a database")
    assert _is_sqlite(plain) is False


def test_client_security_state_patch_tolerates_missing_field():
    """A KeyError here is raised inside the CDP listener and kills it silently.

    Chrome 147+ can omit privateNetworkRequestPolicy; nodriver indexes it
    directly. Once the listener dies every later network event is lost, which
    looks like "the site sent nothing" rather than a crash — the same failure
    shape as the sameParty cookie bug.
    """
    from mcp_stealth_chrome import patches
    from nodriver.cdp import network as cdp_network

    patches.apply_all()
    state = cdp_network.ClientSecurityState.from_json({
        "initiatorIsSecureContext": True,
        "initiatorIPAddressSpace": "Public",
    })
    # The field was renamed between nodriver versions; either way it must be filled.
    assert (getattr(state, "private_network_request_policy", None)
            or getattr(state, "local_network_access_request_policy", None)) is not None


def test_export_curl_builds_a_replayable_pair():
    """The http_request form is the point: plain curl replays with a curl JA3."""
    import asyncio

    from mcp_stealth_chrome.server import BrowserState, mcp

    previous = BrowserState.network_index
    BrowserState.network_index = {
        "42": {
            "request_id": "42",
            "url": "https://api.example.test/v1/items?page=2",
            "method": "POST",
            "status": 200,
            "request_headers": {
                "User-Agent": "Mozilla/5.0 (Macintosh)",
                "Content-Type": "application/json",
                "Content-Length": "17",      # recomputed per connection
                "Host": "api.example.test",  # ditto
                ":authority": "api.example.test",
            },
            "request_body": '{"q":"it\'s ok"}',
        }
    }
    try:
        raw = asyncio.run(mcp._tool_manager._tools["export_curl"].fn(
            url_contains="/v1/items", include_cookies=False))
        out = json.loads(raw)
    finally:
        BrowserState.network_index = previous

    assert out["method"] == "POST"
    assert "curl -X POST" in out["curl"]
    assert "Content-Type: application/json" in out["curl"]
    # Volatile headers must not be replayed: a stale Content-Length is itself a tell.
    assert "Content-Length" not in out["curl"]
    assert ":authority" not in out["curl"]
    # The body carries a quote — it has to survive shell quoting intact.
    assert """'{"q":"it'\\''s ok"}'""" in out["curl"]
    assert "impersonate='chrome'" in out["http_request"]


def test_export_curl_uses_chrome_url_cookie_rules(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    class _Tab:
        def __init__(self):
            self.command = None

        async def send(self, command):
            self.command = next(command)
            return [SimpleNamespace(name="sid", value="secret")]

    tab = _Tab()
    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: tab),
    )
    previous = server.BrowserState.network_index
    server.BrowserState.network_index = {
        "1": {
            "request_id": "1",
            "url": "https://notexample.com/account",
            "method": "GET",
            "request_headers": {},
        }
    }
    try:
        export = server.mcp._tool_manager._tools["export_curl"].fn
        out = json.loads(asyncio.run(export(request_id="1", reveal_values=True)))
        hidden = asyncio.run(export(request_id="1"))
    finally:
        server.BrowserState.network_index = previous

    assert tab.command["method"] == "Network.getCookies"
    assert tab.command["params"]["urls"] == ["https://notexample.com/account"]
    assert out["cookie_status"] == "live_url_jar"
    assert "Cookie: sid=secret" in out["curl"]
    assert "secret" not in hidden and "values_hidden" in json.loads(hidden)


def test_cookie_domain_filter_never_sends_child_cookie_to_parent():
    from mcp_stealth_chrome.tools.storage import _cookie_domain_match

    assert _cookie_domain_match(".example.com", "https://sub.example.com/x") is True
    assert _cookie_domain_match("sub.example.com", "https://example.com/x") is False
    assert _cookie_domain_match("example.com", "https://notexample.com/x") is False


def test_export_curl_surfaces_cookie_measurement_failure(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def send(self, _command):
            raise RuntimeError("cookie domain unavailable")

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
    previous = server.BrowserState.network_index
    server.BrowserState.network_index = {
        "1": {
            "request_id": "1", "url": "https://example.test/",
            "method": "GET", "request_headers": {},
        }
    }
    try:
        export = server.mcp._tool_manager._tools["export_curl"].fn
        out = json.loads(asyncio.run(export(request_id="1")))
    finally:
        server.BrowserState.network_index = previous

    assert out["cookies_attached"] is False
    assert out["cookie_status"] == "error"
    assert "cookie domain unavailable" in out["cookie_error"]


def test_network_get_does_not_cache_truncated_bodies(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def send(self, _command):
            return ("abcdefgh", False)

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
    previous = server.BrowserState.network_index
    server.BrowserState.network_index = {
        "1": {"request_id": "1", "url": "https://example.test/"}
    }
    try:
        network_get = server.mcp._tool_manager._tools["network_get"].fn
        short = json.loads(asyncio.run(network_get(
            include_body=True, max_body_bytes=3,
        )))
        full = json.loads(asyncio.run(network_get(
            include_body=True, max_body_bytes=20,
        )))
        assert "response_body" not in server.BrowserState.network_index["1"]
    finally:
        server.BrowserState.network_index = previous

    assert short[0]["response_body"].startswith("abc... [truncated")
    assert full[0]["response_body"] == "abcdefgh"


def test_network_get_reports_when_body_capture_was_not_measured(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    def _no_tab(cls):
        raise RuntimeError("browser gone")

    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(_no_tab))
    previous = server.BrowserState.network_index
    server.BrowserState.network_index = {
        "1": {"request_id": "1", "url": "https://example.test/"}
    }
    try:
        network_get = server.mcp._tool_manager._tools["network_get"].fn
        result = asyncio.run(network_get(include_body=True))
    finally:
        server.BrowserState.network_index = previous
    assert result.startswith("Error: response body capture unavailable: browser gone")


def test_console_and_network_zero_limits_return_zero_entries():
    import asyncio

    from mcp_stealth_chrome import server

    old_console = server.BrowserState.console_logs
    old_network = server.BrowserState.network_logs
    server.BrowserState.console_logs = [{"text": "one"}]
    server.BrowserState.network_logs = [{"url": "https://example.test/"}]
    try:
        console_get = server.mcp._tool_manager._tools["console_get"].fn
        network_get = server.mcp._tool_manager._tools["network_get"].fn
        console = json.loads(asyncio.run(console_get(limit=0)))
        network = json.loads(asyncio.run(network_get(limit=0)))
    finally:
        server.BrowserState.console_logs = old_console
        server.BrowserState.network_logs = old_network
    assert console["returned"] == 0
    assert console["messages"] == []
    assert network == []


def test_privacy_proxy_supports_pool_and_session_token():
    from mcp_stealth_chrome.server import _privacy_proxy

    rotated = _privacy_proxy("http://user-{session}@a:1|http://user-{session}@b:2")
    assert rotated is not None
    assert "{session}" not in rotated
    assert rotated.startswith(("http://user-"))
    assert rotated.endswith(("@a:1", "@b:2"))


def test_capture_sinks_stay_with_their_instance_across_a_switch():
    """A handler armed on one browser must never file events under another.

    The handler binds its sink when capture is armed. If snapshot/restore copied
    those buffers, the switch would hand the instance a NEW list and the old
    handler would keep appending to an orphan — so the user reads a log that
    stopped growing while the events pile up somewhere unreachable. Worse, code
    that appended to the class attribute at event time filed instance A's
    messages under whichever instance happened to be current.
    """
    from mcp_stealth_chrome.state import BrowserState

    prev_id, prev_logs = BrowserState.current_instance_id, BrowserState.console_logs
    prev_instances = dict(BrowserState.instances)
    try:
        BrowserState.current_instance_id = "cap_a"
        BrowserState.console_logs = []
        sink_a = BrowserState.console_logs        # what the handler closes over

        BrowserState.instances["cap_a"] = BrowserState.snapshot_current()
        BrowserState.current_instance_id = "cap_b"
        BrowserState.console_logs = []
        sink_b = BrowserState.console_logs

        # A's handler fires while B is the current instance.
        sink_a.append({"type": "log", "text": "from A"})
        assert sink_b == [], "instance B must not receive instance A's events"

        # Switching back must surface what A captured while it was in background.
        BrowserState.restore_from(BrowserState.instances["cap_a"])
        assert BrowserState.console_logs == [{"type": "log", "text": "from A"}]
        assert BrowserState.console_logs is sink_a, "identity is the contract"
    finally:
        BrowserState.instances.clear()
        BrowserState.instances.update(prev_instances)
        BrowserState.current_instance_id = prev_id
        BrowserState.console_logs = prev_logs


def test_capture_buffers_are_bounded():
    """Unbounded growth is invisible until the process is in trouble."""
    import mcp_stealth_chrome.server as srv

    events = []
    for i in range(srv.CAPTURE_MAX_EVENTS + 250):
        events.append({"i": i})
        srv._trim_capture(events)
    assert len(events) == srv.CAPTURE_MAX_EVENTS
    # The newest events are the ones kept.
    assert events[-1]["i"] == srv.CAPTURE_MAX_EVENTS + 249

    index = {}
    for i in range(srv.CAPTURE_MAX_INDEXED + 40):
        index[str(i)] = {"i": i}
        srv._trim_index(index)
    assert len(index) == srv.CAPTURE_MAX_INDEXED
    assert str(srv.CAPTURE_MAX_INDEXED + 39) in index
    assert "0" not in index


def test_storage_snapshot_marks_unmeasured_areas_instead_of_empty():
    """An unreadable area must not masquerade as an empty one.

    If cookies fail to read and fall back to [], storage_diff then reports every
    real cookie as "removed" — the action under test gets blamed for a failed
    measurement.
    """
    import asyncio

    from mcp_stealth_chrome.server import _take_storage_snapshot

    class _Tab:
        async def send(self, _cmd):
            raise RuntimeError("cookies unavailable")

        async def evaluate(self, _expr, **_kw):
            raise RuntimeError("storage blocked")

    snap = asyncio.run(_take_storage_snapshot(_Tab()))
    assert snap["measured"]["cookies"] is False
    assert snap["measured"]["localStorage"] is False
    assert "cookies" in snap["errors"] and "localStorage" in snap["errors"]
    # The empty containers are still there for shape, but nothing claims they
    # were measured.
    assert snap["cookies"] == [] and snap["localStorage"] == {}


def test_storage_diff_flags_areas_it_could_not_measure():
    import asyncio
    import json as _json

    import mcp_stealth_chrome.server as srv

    before = {
        "url": "https://a.test/", "title": "a", "cookies": [],
        "localStorage": {}, "sessionStorage": {},
        "errors": {"cookies": "RuntimeError: boom"},
        "measured": {"url": True, "cookies": False,
                     "localStorage": True, "sessionStorage": True},
    }
    after = dict(before, errors={}, measured={"url": True, "cookies": True,
                                              "localStorage": True,
                                              "sessionStorage": True})
    srv._STORAGE_SNAPSHOTS["unit_diff"] = before

    async def fake_snapshot(_tab):
        return after

    original = srv._take_storage_snapshot
    original_tab = srv.BrowserState.active_tab
    srv._take_storage_snapshot = fake_snapshot
    srv.BrowserState.active_tab = classmethod(lambda cls: object())
    try:
        out = _json.loads(asyncio.run(
            srv.mcp._tool_manager._tools["storage_diff"].fn("unit_diff")))
    finally:
        srv._take_storage_snapshot = original
        srv.BrowserState.active_tab = original_tab
        srv._STORAGE_SNAPSHOTS.pop("unit_diff", None)

    assert out["unreliable_areas"] == ["cookies"]
    assert "meaningless" in out["warning"]


def test_compat_patch_status_is_visible():
    """A silently failed patch kills the CDP listener; it must be inspectable."""
    from mcp_stealth_chrome import patches

    patches.apply_all()
    st = patches.status()
    assert st["healthy"] is True
    assert "cookie_same_party" in st["applied"]
    assert "client_security_state" in st["applied"]


@POSIX_PROCESS_CHECKS
def test_close_kills_the_process_when_cdp_cannot(monkeypatch):
    """A dead CDP socket must not turn into an orphaned Chrome.

    browser.stop() speaks CDP. When the websocket is already gone it can return
    without raising while Chrome keeps running — trusting it reports a clean
    close, drops the handle, and leaves a process nobody owns. Death has to be
    confirmed out-of-band and signalled directly when it is not.
    """
    import asyncio
    import os

    import mcp_stealth_chrome.server as srv

    class _Browser:
        _process_pid = 424242
        def stop(self):
            return None            # the silent-failure case: no exception

    alive = {424242}
    signalled = []

    def fake_kill(pid, sig):
        if sig == 0:
            if pid in alive:
                return None
            raise ProcessLookupError(pid)
        signalled.append((pid, sig))
        alive.discard(pid)         # the signal is what actually ends it

    monkeypatch.setattr(os, "kill", fake_kill)
    report = asyncio.run(srv._ensure_browser_dead(_Browser(), None, grace=0))

    assert report["pid"] == 424242
    assert 424242 in report["killed"], "a surviving process must be signalled"
    assert report["still_alive"] == []
    assert report["verified"] is True
    assert "signalled PID(s)" in srv._kill_note(report)


def test_unverifiable_death_is_never_reported_as_clean():
    """No PID and no profile to scan means no proof — and no proof is not success."""
    import asyncio

    import mcp_stealth_chrome.server as srv

    class _Broken:
        _process_pid = None
        def stop(self):
            raise RuntimeError("websocket already closed")

    report = asyncio.run(srv._ensure_browser_dead(_Broken(), None, grace=0))
    assert report["stopped"] is False
    assert report["verified"] is False, "absence of evidence is not evidence of death"
    assert srv._kill_note(report) == ""


def test_window_placement_refuses_states_that_would_hide_the_window(tmp_path, monkeypatch):
    """Placement memory must never recreate the invisible-window bug.

    wipe_window_state() exists because a corrupt saved placement produces a
    window that is open and unusable — 0x0, or parked off every display. Keeping
    our own copy is only safe if it refuses the same shapes.
    """
    import asyncio

    import mcp_stealth_chrome.server as srv

    monkeypatch.setattr(srv, "_window_state_path", lambda iid: tmp_path / f"{iid}.json")

    def saved(**over):
        base = {"left": 100, "top": 100, "width": 1000, "height": 700,
                "window_state": "normal"}
        base.update(over)
        (tmp_path / "w.json").write_text(json.dumps(base))

    saved(width=0, height=0)
    out = asyncio.run(srv._restore_window_bounds("w", None))
    assert out["restored"] is False and "implausible size" in out["reason"]

    saved(width=40, height=30)
    out = asyncio.run(srv._restore_window_bounds("w", None))
    assert out["restored"] is False, "a tiny window is as unusable as a zero one"

    # Minimised/fullscreen are states, not placements — restoring them would
    # hide the window or seize the screen without being asked.
    saved(window_state="minimized")
    out = asyncio.run(srv._restore_window_bounds("w", None))
    assert out["restored"] is False and "minimized" in out["reason"]

    # Nothing saved yet is a normal condition, not a failure.
    (tmp_path / "w.json").unlink()
    out = asyncio.run(srv._restore_window_bounds("w", None))
    assert out["restored"] is False and out["reason"] == "no saved placement"


def test_verify_capture_tolerates_browser_zoom(monkeypatch):
    """A host zoomed to 90% reports dpr 0.9; the clip still rasterises at CSS size."""
    from mcp_stealth_chrome import image_output

    monkeypatch.setattr(image_output, "image_dimensions", lambda _d: (42, 20, "image/png"))
    clip = {"width": 42, "height": 20}
    assert image_output.verify_capture(b"x", clip, 0.9, 1.0, {})[2] is None
    # A genuinely wrong capture is still refused.
    assert image_output.verify_capture(b"x", {"width": 400, "height": 300}, 0.9, 1.0, {})[2]


class _BrokenTab:
    def __init__(self, message):
        self.message = message

    async def evaluate(self, *_a, **_k):
        raise RuntimeError(self.message)


def test_a_failed_url_read_is_not_a_page_without_a_url():
    import asyncio

    from mcp_stealth_chrome.helpers import PageReadError, get_title, get_url

    tab = _BrokenTab("Execution context was destroyed")
    assert asyncio.run(get_url(tab)) == ""  # polling through a navigation
    assert asyncio.run(get_title(tab)) == ""
    with pytest.raises(PageReadError):
        asyncio.run(get_url(tab, strict=True))
    with pytest.raises(PageReadError):
        asyncio.run(get_title(tab, strict=True))


def test_a_lost_connection_raises_even_for_pollers():
    """It used to make a poll wait out its whole timeout against a dead browser."""
    import asyncio

    from mcp_stealth_chrome.helpers import PageReadError, get_url

    tab = _BrokenTab("websocket connection is closed")
    for strict in (False, True):
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(get_url(tab, strict=strict))
        assert not isinstance(caught.value, PageReadError)


def test_get_url_tool_reports_failure_instead_of_blank(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "active_tab",
                        classmethod(lambda cls: _BrokenTab("Execution context was destroyed")))
    out = asyncio.run(server.mcp._tool_manager._tools["get_url"].fn())
    assert out.startswith("Error:") and "could not read the page URL" in out


def test_report_lines_say_when_facts_are_unreadable():
    import asyncio

    from mcp_stealth_chrome import server

    url, title = asyncio.run(server._page_facts_for_report(_BrokenTab("context destroyed")))
    assert url.startswith("(unreadable") and title == "(unreadable)"


def test_storage_snapshot_marks_an_unread_url_as_unmeasured():
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab(_BrokenTab):
        async def send(self, *_a, **_k):
            raise RuntimeError("no")

    snap = asyncio.run(server._take_storage_snapshot(_Tab("context destroyed")))
    assert snap["measured"]["url"] is False and "url" in snap["errors"]


def _fake_browser(tabs, live_ids):
    class _Info:
        type_ = "page"

        def __init__(self, target_id):
            self.target_id = target_id

    class _Conn:
        async def send(self, _command):
            return [_Info(i) for i in live_ids]

    class _Browser:
        connection = _Conn()
        created = 0

        def __init__(self):
            self.tabs = tabs

        async def update_targets(self):
            pass

        async def get(self, url, new_tab=False):
            self.created += 1
            return "created-tab"

    return _Browser()


def _fake_tab(target_id, alive=True):
    class _Target:
        pass

    class _Tab:
        target = _Target()

        async def send(self, _command):
            if not alive:
                raise RuntimeError("server rejected WebSocket connection: HTTP 404")

    _Tab.target.target_id = target_id
    return _Tab()


def test_startup_tab_that_chrome_replaced_is_skipped():
    """The cause of spawn_browser failing on a profile copied from another browser."""
    import asyncio

    from mcp_stealth_chrome import server

    gone, alive = _fake_tab("gone"), _fake_tab("alive")
    browser = _fake_browser([gone, alive], ["alive"])
    assert asyncio.run(server._first_live_tab(browser)) is alive


def test_tab_listed_as_live_but_refusing_is_skipped():
    import asyncio

    from mcp_stealth_chrome import server

    refusing, alive = _fake_tab("a", alive=False), _fake_tab("b")
    browser = _fake_browser([refusing, alive], ["a", "b"])
    assert asyncio.run(server._first_live_tab(browser)) is alive


def test_a_new_tab_is_opened_when_none_answer():
    import asyncio

    from mcp_stealth_chrome import server

    browser = _fake_browser([_fake_tab("a", alive=False)], ["a"])
    assert asyncio.run(server._first_live_tab(browser, attempts=1)) == "created-tab"
    assert browser.created == 1


def test_multi_profile_dir_opens_the_last_used_profile(tmp_path):
    """Without this Chrome shows its profile picker, a window with no tab to drive."""
    import json as _json

    from mcp_stealth_chrome import server

    (tmp_path / "Default").mkdir()
    (tmp_path / "Profile 2").mkdir()
    state_file = tmp_path / "Local State"
    state_file.write_text(_json.dumps({"profile": {"last_used": "Profile 2"}}))
    args = server._profile_directory_args
    assert args(tmp_path, []) == ["--profile-directory=Profile 2"]
    assert args(tmp_path, ["--profile-directory=Other"]) == []  # the caller's choice wins
    assert args(None, []) == []

    state_file.write_text(_json.dumps({"profile": {"last_used": "../../etc"}}))
    assert args(tmp_path, []) == ["--profile-directory=Default"]
    state_file.write_text("not json")
    assert args(tmp_path, []) == ["--profile-directory=Default"]
    for shape in ({}, {"profile": {}}, {"profile": None}, [], {"profile": {"last_used": None}}, {"profile": {"last_used": 5}}):
        state_file.write_text(_json.dumps(shape))
        assert args(tmp_path, []) == ["--profile-directory=Default"], shape
    state_file.unlink()
    assert args(tmp_path, []) == ["--profile-directory=Default"]
    assert args(tmp_path / "Default", []) == []  # no sub-profile: leave Chrome to it


@POSIX_PROCESS_CHECKS
def test_closing_a_browser_with_a_known_pid_never_scans_for_others(monkeypatch):
    """persistent=False recorded the shared profile, so its close signalled the real one."""
    import asyncio
    import signal

    from mcp_stealth_chrome import server, state

    signalled = []

    def fake_kill(pid, sig):
        if sig != 0:
            signalled.append(pid)
        elif pid == 111:  # our own process is gone; anything else would look alive
            raise ProcessLookupError

    class _Browser:
        _process_pid = 111

        def stop(self):
            pass

    monkeypatch.setattr(server.os, "kill", fake_kill)
    monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda _p: [222])
    report = asyncio.run(server._ensure_browser_dead(_Browser(), state.PROFILE_DIR, grace=0))
    assert 222 not in signalled and report["pid"] == 111
    assert signal.SIGKILL not in signalled


def test_ephemeral_launch_claims_no_profile():
    """The resolver answers None, and browser_launch must keep it that way."""
    import inspect

    from mcp_stealth_chrome import server

    path, info = server._resolve_launch_profile("main", persistent=False, profile_conflict="fallback")
    assert path is None and info["actual_profile"] is None
    assert "resolved_profile or PROFILE_DIR" not in inspect.getsource(server.browser_launch)


@POSIX_MODE_BITS
def test_identity_pin_temp_file_is_private_from_the_first_byte(tmp_path, monkeypatch):
    import asyncio
    import os as _os
    import stat

    from mcp_stealth_chrome import server

    from mcp_stealth_chrome.tools import stealth

    monkeypatch.setattr(server, "IDENTITIES_DIR", tmp_path)
    monkeypatch.setattr(stealth, "ensure_dirs", lambda: None)
    seen = []
    real_replace = _os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(_os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(server.os, "replace", spy)
    out = asyncio.run(server.mcp._tool_manager._tools["identity_pin"].fn(
        instance_id="pin_mode", fingerprint={"timezone": "UTC"}))
    assert not out.startswith("Error:"), out
    assert seen == [0o600]


# ── High-severity findings from the seeded benchmark ──────────────────────


class _CookieAt:
    def __init__(self, name, domain, path="/", secure=False, value="v"):
        self.name, self.domain, self.path, self.secure, self.value = name, domain, path, secure, value


@pytest.mark.parametrize("cookie,url,sent", [
    (_CookieAt("a", ".x.test"), "https://sub.x.test/", True),
    (_CookieAt("a", "x.test"), "https://x.test/", True),
    (_CookieAt("a", "x.test"), "https://sub.x.test/", False),
    (_CookieAt("a", "x.test", path="/admin"), "https://x.test/public", False),
    (_CookieAt("a", "x.test", path="/admin"), "https://x.test/admin/users", True),
    (_CookieAt("a", "x.test", path="/admin"), "https://x.test/administrator", False),
    (_CookieAt("a", "x.test", secure=True), "http://x.test/", False),
    (_CookieAt("a", "x.test", secure=True), "https://x.test/", True),
    (_CookieAt("a", ".x.test"), "https://notx.test/", False),
])
def test_http_requests_only_send_cookies_a_browser_would(cookie, url, sent):
    from urllib.parse import urlparse

    from mcp_stealth_chrome.tools.network_http import _cookie_applies

    assert _cookie_applies(cookie, urlparse(url)) is sent


def test_same_name_cookie_keeps_the_most_specific_path(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http as nh

    class _Jar:
        async def get_all(self):
            return [_CookieAt("sid", "x.test", "/", value="site"),
                    _CookieAt("sid", "x.test", "/admin", value="admin")]

    class _Browser:
        cookies = _Jar()

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    jar, _meta = asyncio.run(nh._snapshot_browser_cookies("https://x.test/admin/a"))
    assert jar == {"sid": "admin"}


def test_sniffed_token_only_goes_back_to_its_exact_origin(monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "network_index", {
        "1": {"url": "https://x.test/api", "request_headers": {"Authorization": "Bearer secret"}},
    })
    assert server._latest_auth_header_for("https://x.test/other") == "Bearer secret"
    assert server._latest_auth_header_for("https://x.test:443/other") == "Bearer secret"
    assert server._latest_auth_header_for("http://x.test:8080/") is None
    assert server._latest_auth_header_for("http://x.test/") is None
    assert server._latest_auth_header_for("https://x.test:8443/") is None


def test_redirected_post_failure_is_not_called_safe_to_replay():
    from mcp_stealth_chrome.tools.network_http import _classify_http_error

    class _Dead(Exception):
        code = 7

    assert _classify_http_error(_Dead("connect failed"))["outcome"] == "not_executed"
    after_redirect = _classify_http_error(_Dead("connect failed"), may_have_redirected=True)
    assert after_redirect["outcome"] == "unknown"
    assert after_redirect["retry_safe_any_method"] is False


@pytest.mark.parametrize("proxy,shown", [
    ("http://user:s3cret@host:8080", "http://user:***@host:8080"),
    ("socks5://u:p@h:1|http://a:b@c:2", "socks5://u:***@h:1|http://a:***@c:2"),
    ("http://host:8080", "http://host:8080"),
    (None, None),
])
def test_proxy_password_is_masked(proxy, shown):
    from mcp_stealth_chrome import server

    assert server._mask_proxy_password(proxy) == shown


def test_identity_show_masks_unless_asked(tmp_path, monkeypatch):
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "IDENTITIES_DIR", tmp_path)
    (tmp_path / "with_proxy.json").write_text(_json.dumps(
        {"instance_id": "with_proxy", "fingerprint": {}, "proxy": "http://u:s3cret@h:1"}))
    show = server.mcp._tool_manager._tools["identity_show"].fn
    assert "s3cret" not in asyncio.run(show("with_proxy"))
    assert "s3cret" in asyncio.run(show("with_proxy", reveal_values=True))


def test_storage_diff_masks_new_credentials_unless_asked(monkeypatch):
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    empty = {"url": "u", "title": "t", "cookies": [], "localStorage": {"k": "old"},
             "sessionStorage": {}, "errors": {},
             "measured": {"url": True, "cookies": True, "localStorage": True, "sessionStorage": True}}
    after = {**empty, "cookies": [{"name": "SID", "domain": "x", "value": "TOKENVALUE"}],
             "localStorage": {"k": "NEWSECRET", "jwt": "JWTSECRET"}}

    async def fake_snapshot(_tab):
        return after

    monkeypatch.setattr(server, "_take_storage_snapshot", fake_snapshot)
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: object()))
    monkeypatch.setitem(server._STORAGE_SNAPSHOTS, "login", empty)
    diff = server.mcp._tool_manager._tools["storage_diff"].fn
    masked = asyncio.run(diff("login"))
    for secret in ("TOKENVALUE", "NEWSECRET", "JWTSECRET", "old"):
        assert secret not in masked
    assert _json.loads(masked)["cookies"]["added"][0]["name"] == "SID"
    assert "TOKENVALUE" in asyncio.run(diff("login", reveal_values=True))


def test_storage_state_load_really_sets_cookies_and_reports_counts(tmp_path, monkeypatch):
    """A numeric expires raised inside set_cookie, was swallowed, and it said "loaded"."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    state_file = tmp_path / "s.json"
    state_file.write_text(_json.dumps({"cookies": [
        {"name": "a", "value": "1", "domain": "x.test", "expires": 1893456000.0},
        {"name": "b", "value": "2", "domain": "x.test", "expires": -1.0},
    ]}))
    sent = []

    class _Tab:
        async def send(self, command):
            sent.append(next(command))  # the real generator: this is what raised

    class _Browser:
        tabs = [_Tab()]

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    out = asyncio.run(server.mcp._tool_manager._tools["storage_state_load"].fn(str(state_file)))
    assert "2 cookies set" in out and "NOT applied" not in out
    assert sent[0]["params"]["expires"] == 1893456000.0
    assert "expires" not in sent[1]["params"]


def test_storage_state_load_says_so_when_nothing_could_be_set(tmp_path, monkeypatch):
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    state_file = tmp_path / "s.json"
    state_file.write_text(_json.dumps({"cookies": [{"name": "a", "value": "1", "domain": "x"}]}))

    class _Tab:
        async def send(self, _command):
            raise RuntimeError("rejected")

    class _Browser:
        tabs = [_Tab()]

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    out = asyncio.run(server.mcp._tool_manager._tools["storage_state_load"].fn(str(state_file)))
    assert out.startswith("Error:") and "none of the 1 cookies" in out


def test_dialog_handle_answers_only_the_next_dialog(monkeypatch):
    """It went on accepting every later dialog, a destructive confirm included."""
    import asyncio

    from mcp_stealth_chrome import server

    class _Target:
        target_id = "ONE-SHOT"

    class _Tab:
        target = _Target()

        def __init__(self):
            self.handlers, self.answers = [], []

        def add_handler(self, _event, handler):
            self.handlers.append(handler)

        async def send(self, command):
            request = next(command)
            if request["method"] == "Page.handleJavaScriptDialog":
                self.answers.append(request["params"]["accept"])

    tab = _Tab()
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    server._dialog_pre_action["_registered_tab_ids"].discard("ONE-SHOT")
    dialog = server.mcp._tool_manager._tools["dialog_handle"].fn
    try:
        asyncio.run(dialog(action="accept"))
        asyncio.run(tab.handlers[0](None))
        asyncio.run(tab.handlers[0](None))
        assert tab.answers == [True]
        asyncio.run(dialog(action="dismiss"))  # arming again works, on the same handler
        asyncio.run(tab.handlers[0](None))
        assert tab.answers == [True, False]
    finally:
        server._dialog_pre_action["_registered_tab_ids"].discard("ONE-SHOT")


# ── Medium-severity findings from the seeded benchmark ────────────────────


def _tool(name):
    from mcp_stealth_chrome import server

    return server.mcp._tool_manager._tools[name].fn


class _CdpTab:
    """Enough of a nodriver Tab to run the real CDP generators and fire events back."""

    def __init__(self, target_id="T1", fail=()):
        from types import SimpleNamespace

        self.target = SimpleNamespace(target_id=target_id)
        self.handlers, self.sent, self.fail = {}, [], fail

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    async def send(self, command):
        import asyncio

        request = next(command)
        await asyncio.sleep(0)  # a real send suspends, which is where a self-cancel lands
        if request["method"] in self.fail or "*" in self.fail:
            raise RuntimeError(f"{request['method']} refused")
        self.sent.append(request)
        return None

    async def fire(self, event_type, **fields):
        from types import SimpleNamespace

        for handler in list(self.handlers.get(event_type, [])):
            await handler(SimpleNamespace(**fields))

    def methods(self):
        return [r["method"] for r in self.sent]


def _active(monkeypatch, tab):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    return tab


# ── I and K: close paths ──


class _LiveBrowser:
    """A handle whose process exists, so is_up() depends only on the tabs."""

    stopped = False
    _process = object()

    def stop(self):
        return None


def _running(monkeypatch, tabs, profile=None, privacy=False):
    from mcp_stealth_chrome import server

    browser = _LiveBrowser()
    for name, value in (("browser", browser), ("tabs", tabs), ("current_headless", True),
                        ("current_profile_dir", profile), ("current_privacy", privacy),
                        ("current_instance_id", "main"), ("instances", {})):
        monkeypatch.setattr(server.BrowserState, name, value)
    return browser


def _death_report(monkeypatch, **fields):
    from mcp_stealth_chrome import server

    report = {"stopped": True, "verified": True, "pid": 1, "killed": [], "still_alive": [], "detail": ""}
    report.update(fields)
    calls = []

    async def fake(browser, profile_dir=None, grace=1.5):
        calls.append(browser)
        return report

    monkeypatch.setattr(server, "_ensure_browser_dead", fake)
    return calls


def _privacy_removal(monkeypatch):
    from mcp_stealth_chrome import server

    removed = []

    async def fake(profile):
        removed.append(profile)
        return True

    monkeypatch.setattr(server, "_remove_privacy_profile", fake)
    return removed


@pytest.mark.parametrize("name,args", [
    ("browser_close", ()), ("close_instance", ("main",)), ("close_all_instances", ()),
])
def test_a_surviving_process_keeps_its_handle_and_its_privacy_profile(monkeypatch, tmp_path, name, args):
    """stop() returning with a PID still alive read as closed: the handle was dropped,
    leaving a Chrome nothing could reach, and its privacy profile was deleted under it."""
    import asyncio

    from mcp_stealth_chrome import server

    browser = _running(monkeypatch, [_CdpTab()], tmp_path / "p", privacy=True)
    _death_report(monkeypatch, verified=False, still_alive=[4242], killed=[4242])
    removed = _privacy_removal(monkeypatch)
    out = asyncio.run(_tool(name)(*args))
    assert out.startswith("Error:") and "4242" in out, out
    assert server.BrowserState.browser is browser
    assert removed == []


def test_a_stored_instance_with_a_surviving_process_is_kept(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import InstanceSnapshot

    snap = InstanceSnapshot(instance_id="worker", browser=_LiveBrowser(), tabs=[_CdpTab()])
    monkeypatch.setattr(server.BrowserState, "instances", {"worker": snap})
    monkeypatch.setattr(server.BrowserState, "current_instance_id", "main")
    _death_report(monkeypatch, verified=False, still_alive=[4242])
    out = asyncio.run(_tool("close_instance")("worker"))
    assert out.startswith("Error:") and "4242" in out
    assert server.BrowserState.instances["worker"] is snap and snap.browser is not None


def test_browser_close_says_when_it_had_to_signal_the_process(monkeypatch):
    import asyncio

    _running(monkeypatch, [_CdpTab()])
    _death_report(monkeypatch, killed=[4242])
    out = asyncio.run(_tool("browser_close")())
    assert out.startswith("Browser closed") and "signalled PID(s) [4242]" in out


def test_a_clean_close_reads_as_it_always_did(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    _running(monkeypatch, [_CdpTab()])
    _death_report(monkeypatch)
    assert asyncio.run(_tool("browser_close")()) == "Browser closed."
    assert server.BrowserState.browser is None


@pytest.mark.parametrize("name,args", [
    ("browser_close", ()), ("close_instance", ("main",)), ("close_all_instances", ()),
])
def test_last_tab_closed_still_ends_chrome_before_touching_its_profile(monkeypatch, tmp_path, name, args):
    """No tabs made is_up() false, so close said "not running" and deleted the privacy
    profile from under a live Chrome without stopping it."""
    import asyncio

    from mcp_stealth_chrome import server

    browser = _running(monkeypatch, [], tmp_path / "p", privacy=True)
    assert not server.BrowserState.is_up()
    ended = _death_report(monkeypatch)
    removed = _privacy_removal(monkeypatch)
    out = asyncio.run(_tool(name)(*args))
    assert ended == [browser]
    assert removed == [tmp_path / "p"]
    assert "not running" not in out, out
    assert server.BrowserState.browser is None


def test_no_handle_at_all_is_still_not_running(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "browser", None)
    monkeypatch.setattr(server.BrowserState, "current_privacy", False)
    called = _death_report(monkeypatch)
    assert asyncio.run(_tool("browser_close")()) == "Browser was not running."
    assert called == []


# ── AB and AA: interception ──


@pytest.fixture()
def intercept(monkeypatch):
    from mcp_stealth_chrome.tools import capture

    fresh = dict(capture._INTERCEPT, log=[], counts=dict(capture._INTERCEPT["counts"]),
                 active=False, tab=None, handler=None, handler_tab=None, watchdog=None, session=None)
    monkeypatch.setattr(capture, "_INTERCEPT", fresh)
    return fresh


def test_interception_deadline_really_ends_it(monkeypatch, intercept):
    """The watchdog cancelled itself, so state never updated: active stayed true forever
    and every other session was told interception was in use."""
    import asyncio
    import json as _json

    tab = _active(monkeypatch, _CdpTab())

    async def scenario():
        await _tool("intercept_start")(auto_disable_after=0.01)
        watchdog = intercept["watchdog"]
        await asyncio.wait_for(asyncio.wait({watchdog}), 2)
        return _json.loads(await _tool("intercept_log")())

    log = asyncio.run(scenario())
    assert log["active"] is False
    assert log["stop_reason"] == "auto-disabled on deadline"
    assert "Fetch.disable" in tab.methods()


def _paused():
    from types import SimpleNamespace

    from nodriver.cdp import fetch as cdp_fetch

    return dict(request_id=cdp_fetch.RequestId("r1"),
                request=SimpleNamespace(method="GET", url="https://x.test/", headers={}, post_data=None))


def test_restarting_interception_answers_each_pause_once(monkeypatch, intercept):
    """start, stop, start left two handlers, so one pause got two continueRequest calls."""
    import asyncio

    from nodriver.cdp import fetch as cdp_fetch

    tab = _active(monkeypatch, _CdpTab())

    async def scenario():
        await _tool("intercept_start")(auto_disable_after=0)
        await _tool("intercept_stop")()
        assert tab.handlers[cdp_fetch.RequestPaused] == []
        await _tool("intercept_start")(auto_disable_after=0)
        await tab.fire(cdp_fetch.RequestPaused, **_paused())
        await _tool("intercept_stop")()

    asyncio.run(scenario())
    assert tab.methods().count("Fetch.continueRequest") == 1
    assert len(intercept["log"]) == 1


def test_a_failed_disable_keeps_answering_then_a_restart_replaces_the_handler(monkeypatch, intercept):
    """Fetch may still be pausing requests, and nothing else would answer them."""
    import asyncio

    from nodriver.cdp import fetch as cdp_fetch

    tab = _active(monkeypatch, _CdpTab(fail=("Fetch.disable",)))

    async def scenario():
        await _tool("intercept_start")(auto_disable_after=0)
        await _tool("intercept_stop")()
        assert len(tab.handlers[cdp_fetch.RequestPaused]) == 1
        await tab.fire(cdp_fetch.RequestPaused, **_paused())
        assert tab.methods().count("Fetch.continueRequest") == 1
        await _tool("intercept_start")(auto_disable_after=0)
        assert len(tab.handlers[cdp_fetch.RequestPaused]) == 1

    asyncio.run(scenario())
    assert intercept["active"] is True


# ── T and S: what a click tests ──


class _Pos:
    left, top, width, height = 100.0, 100.0, 100.0, 40.0


class _ClickEl:
    """An element whose right half is covered by something else."""

    def __init__(self, probe_results=None):
        self.probe_results = probe_results
        self.point_checks = []

    async def get_position(self):
        return _Pos()

    async def apply(self, js):
        import json as _json
        import re

        from mcp_stealth_chrome import server

        if js == server._CLICK_PROBE_JS:
            if self.probe_results is not None:
                result = self.probe_results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            return _json.dumps({"sized": True, "in_viewport": True, "hit": True, "target": "button"})
        fx = float(re.search(r"r\.width \* ([\d.]+)", js).group(1))
        self.point_checks.append(fx)
        return fx < 0.5


class _MouseTab:
    def __init__(self):
        self.clicks = []

    async def mouse_click(self, x, y):
        self.clicks.append((x, y))

    async def send(self, _command):
        return None


def _quick_clicks(monkeypatch):
    import mcp_stealth_chrome.humanize as hz
    from mcp_stealth_chrome import server

    async def settled(el):
        return await el.get_position()

    async def no_move(*_a, **_k):
        return None

    monkeypatch.setattr(server, "_settled_position", settled)
    monkeypatch.setattr(hz, "humanized_move", no_move)


def test_humanized_click_only_presses_a_point_that_passed_a_hit_test(monkeypatch):
    """The centre was tested, then a random point elsewhere in the box was clicked, which
    could be on an overlay covering part of the element."""
    import asyncio
    import itertools
    import random

    from mcp_stealth_chrome import server

    _quick_clicks(monkeypatch)
    # Scripted, not left to chance: the first spot is on the covered half, the second is not.
    samples = itertools.cycle([0.6, 0.4, 0.4, 0.5])
    monkeypatch.setattr(random, "uniform", lambda _low, _high: next(samples))
    el, tab = _ClickEl(), _MouseTab()
    out = asyncio.run(server._click_and_report(tab, el, "c", False, humanize=True))
    assert "clicked" in out
    assert el.point_checks == [0.6, 0.4], "each sampled spot must be tested until one passes"
    assert tab.clicks == [(140, 120)], f"pressed somewhere other than the spot that passed: {tab.clicks}"


def test_humanized_click_falls_back_to_the_tested_centre(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    _quick_clicks(monkeypatch)

    class _Covered(_ClickEl):
        async def apply(self, js):
            result = await super().apply(js)
            return False if result is True else result

    el, tab = _Covered(), _MouseTab()
    asyncio.run(server._click_and_report(tab, el, "c", False, humanize=True))
    assert tab.clicks == [(150, 120)]


class _DialogTab(_MouseTab):
    """A page whose n-th pointer move opens a native dialog.

    That move is acknowledged only once the dialog is answered, as Chrome's renderer is held
    inside the handler; every input sent afterwards is recorded with the time it was sent.
    """

    def __init__(self, target_id, opens_on=3):
        from types import SimpleNamespace

        super().__init__()
        self.target = SimpleNamespace(target_id=target_id)
        self.moves = []
        self.opens_on = opens_on
        self.answered = None

    def add_handler(self, *_args):
        return None

    def answer(self):
        from mcp_stealth_chrome.helpers import OPEN_DIALOGS

        OPEN_DIALOGS.pop(self.target.target_id, None)
        self.answered.set()

    async def send(self, command):
        import asyncio
        import time

        from mcp_stealth_chrome.helpers import OPEN_DIALOGS

        request = next(command)
        if self.answered is None:
            self.answered = asyncio.Event()
        await asyncio.sleep(0)
        params = request.get("params", {})
        if request["method"] == "Input.dispatchMouseEvent" and params.get("type") == "mouseMoved":
            self.moves.append((params["x"], params["y"]))
            if len(self.moves) == self.opens_on:
                OPEN_DIALOGS[self.target.target_id] = {
                    "type": "alert", "message": "move", "since": time.monotonic()}
                await self.answered.wait()
        return None


def test_a_path_cut_short_by_a_dialog_sends_nothing_after_the_tool_returns():
    """The step that opened the dialog finished once it was answered, and every later step went
    out then, into whatever page was open: a CI run's next tool found an alert it never opened."""
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.humanize import humanized_move

    async def scenario():
        tab = _DialogTab("PATH-CUT")
        await server._dispatch_input(tab, humanized_move(tab, 0, 0, 300, 300, steps=20))
        at_return = len(tab.moves)
        tab.answer()  # dialog_handle
        await asyncio.sleep(0.6)  # 17 more steps of 10-30 ms each would all have gone by now
        return at_return, len(tab.moves)

    at_return, later = asyncio.run(scenario())
    assert at_return == 3, at_return
    assert later == 3, f"{later - at_return} pointer moves were sent after the tool had returned"


def test_a_humanized_click_whose_path_opens_a_dialog_is_not_clicked(monkeypatch):
    """It answered 'clicked' with the press still waiting behind the dialog, and the press then
    landed after dialog_handle, wherever the page was by then."""
    import asyncio

    from mcp_stealth_chrome import server

    async def settled(el):
        return await el.get_position()

    monkeypatch.setattr(server, "_settled_position", settled)

    async def scenario():
        tab = _DialogTab("CLICK-CUT")
        out = await server._click_and_report(tab, _ClickEl(), "c", False, humanize=True)
        at_return = len(tab.moves)
        tab.answer()
        await asyncio.sleep(0.6)
        return out, tab, at_return

    out, tab, at_return = asyncio.run(scenario())
    assert out.startswith("Error:") and "NOT clicked" in out, out
    assert "moving the pointer there opened a dialog" in out and "dialog_handle" in out, out
    assert tab.clicks == [], f"pressed after the dialog was answered: {tab.clicks}"
    assert len(tab.moves) == at_return, "the path went on after the tool returned"


@pytest.mark.parametrize("probes", [
    [RuntimeError("context lost")] * 3,
    # The first passes, then the page moves and the check that counts cannot run.
    ['{"sized": true, "in_viewport": true, "hit": true}', RuntimeError("context lost")],
])
def test_a_probe_that_cannot_run_refuses_rather_than_clicks(monkeypatch, probes):
    """An empty probe counted as a pass, and real input went out at an untested point."""
    import asyncio

    from mcp_stealth_chrome import server

    _quick_clicks(monkeypatch)
    tab = _MouseTab()
    out = asyncio.run(server._click_and_report(tab, _ClickEl(list(probes)), "c", False))
    assert tab.clicks == []
    assert out.startswith("Error:") and "NOT clicked" in out and "hit test could not run" in out


# ── L and P: profiles ──


def _profile_with_window_state(root, locked=False):
    import json as _json
    import os

    default = root / "Default"
    (default / "Sessions").mkdir(parents=True)
    (default / "Sessions" / "Session_1").write_text("tabs")
    prefs = {"browser": {"window_placement": {"left": 1}}, "profile": {"exit_type": "Crashed"}}
    (default / "Preferences").write_text(_json.dumps(prefs))
    if locked:
        # This test process is alive, so the lock reads as held.
        (root / "SingletonLock").symlink_to(f"testhost-{os.getpid()}")
    return root


def test_recover_leaves_a_running_profile_alone(monkeypatch, tmp_path):
    """Recovering instance A wiped running B's Sessions/ and rewrote its Preferences."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server, state

    profiles = tmp_path / "profiles"
    busy = _profile_with_window_state(profiles / "busy", locked=True)
    idle = _profile_with_window_state(profiles / "idle")
    monkeypatch.setattr(state, "PROFILES_ROOT", profiles)
    monkeypatch.setattr(state, "PROFILE_DIR", tmp_path / "main")
    monkeypatch.setattr(state, "per_process_profile", lambda: tmp_path / "pid")
    monkeypatch.setattr(server.BrowserState, "browser", None)
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", None)
    before = (busy / "Default" / "Preferences").read_text()

    out = asyncio.run(_tool("browser_recover")())

    assert "left 1 in use" in out, out
    assert (busy / "Default" / "Sessions" / "Session_1").exists()
    assert (busy / "Default" / "Preferences").read_text() == before
    assert not (idle / "Default" / "Sessions" / "Session_1").exists()
    assert "window_placement" not in _json.loads(
        (idle / "Default" / "Preferences").read_text()).get("browser", {})


def test_clean_profile_state_never_rewrites_a_locked_profile(tmp_path):
    from mcp_stealth_chrome.state import clean_profile_state

    busy = _profile_with_window_state(tmp_path / "busy", locked=True)
    before = (busy / "Default" / "Preferences").read_text()
    clean_profile_state(busy)
    assert (busy / "Default" / "Preferences").read_text() == before
    assert (busy / "SingletonLock").is_symlink()


def test_conflict_clone_refuses_a_copy_with_errors(monkeypatch, tmp_path):
    """A failed copy of a non-SQLite file came back as a usable point-in-time copy."""
    import shutil

    from mcp_stealth_chrome import server

    source = tmp_path / "src"
    (source / "Default").mkdir(parents=True)
    (source / "Default" / "Preferences").write_text("{}")
    (source / "Local State").write_text("{}")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "out")
    real = shutil.copy2

    def flaky(src, dst):
        if str(src).endswith("Local State"):
            raise PermissionError("nope")
        return real(src, dst)

    monkeypatch.setattr(shutil, "copy2", flaky)
    clone, report = server._safe_profile_clone(source, "x")
    assert clone is None
    assert any("Local State" in e for e in report["errors"])


def test_conflict_clone_tolerates_a_file_the_live_browser_just_removed(monkeypatch, tmp_path):
    """Chrome rotates files while it runs. One gone since the listing is not in this copy,
    and treating it as an error would make the policy fail on any busy profile."""
    import shutil

    from mcp_stealth_chrome import server

    source = tmp_path / "src"
    (source / "Default").mkdir(parents=True)
    (source / "Default" / "Preferences").write_text("{}")
    (source / "Default" / "tmp123").write_text("x")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "out")
    real = shutil.copy2

    def vanishing(src, dst):
        if str(src).endswith("tmp123"):
            raise FileNotFoundError(src)
        return real(src, dst)

    monkeypatch.setattr(shutil, "copy2", vanishing)
    clone, report = server._safe_profile_clone(source, "x")
    assert clone is not None and report["errors"] == []


# ── H: browser cookies ──


def test_a_failed_cookie_read_is_not_a_measured_zero(monkeypatch):
    """The exception was swallowed into [], so the caller's failure branch was unreachable
    and an authenticated request went out cookie-less, reported as measured."""
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http as nh

    class _Jar:
        async def get_all(self):
            raise RuntimeError("cdp gone")

    class _Browser:
        cookies = _Jar()

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    jar, meta = asyncio.run(nh._snapshot_browser_cookies("https://x.test/"))
    assert jar == {} and meta["measured"] is False
    assert "cdp gone" in meta["reason"]


# ── AC, AD, BG: click_and_wait; BA: wait_for_request ──


def _click_and_wait_page(monkeypatch, tab, during_click=None):
    from mcp_stealth_chrome import server

    _active(monkeypatch, tab)
    clicks = []

    async def fake_url(_tab, strict=False):
        return "https://x.test/"

    async def fake_click(**kwargs):
        clicks.append(kwargs)
        if during_click:
            await during_click()
        return "click: clicked"

    monkeypatch.setattr(server, "get_url", fake_url)
    monkeypatch.setattr(server, "click", fake_click)
    return clicks


def _request_events(tab, rid="r1", url="https://x.test/api/save"):
    from types import SimpleNamespace

    from nodriver.cdp import network as cdp_network

    async def started():
        await tab.fire(cdp_network.RequestWillBeSent, request_id=rid, type_="XHR",
                       request=SimpleNamespace(url=url, method="POST", headers={}, post_data=None))

    async def responded():
        await tab.fire(cdp_network.ResponseReceived, request_id=rid,
                       response=SimpleNamespace(status=201, headers={}, mime_type="application/json"))

    async def finished():
        await tab.fire(cdp_network.LoadingFinished, request_id=rid)

    return started, responded, finished


def test_click_and_wait_sees_a_request_fired_during_the_click(monkeypatch):
    """Subscribing after the click missed a fast API call and reported a timeout, and the
    caller's retry then repeated the action."""
    import asyncio
    import json as _json

    from nodriver.cdp import network as cdp_network

    tab = _CdpTab()
    started, responded, _finished = _request_events(tab)

    async def during():
        await started()
        await responded()

    clicks = _click_and_wait_page(monkeypatch, tab, during)
    out = asyncio.run(_tool("click_and_wait")(
        selector="#s", expect="request", expect_request_pattern="/api/save", timeout=0.3))
    assert not out.startswith("Error:"), out
    assert _json.loads(out)["observed"]["what"] == "request"
    assert '\\"status\\": 201' in out and "_pending" not in out
    assert len(clicks) == 1
    assert tab.handlers[cdp_network.RequestWillBeSent] == []


@pytest.mark.parametrize("kwargs,named", [
    ({"expect": "url"}, "expect_url_pattern"),
    ({"expect": "text"}, "expect_text"),
    ({"expect": "selector"}, "expect_selector"),
    ({"expect": "request"}, "expect_request_pattern"),
    ({"expect": "typo"}, "unknown expect"),
])
def test_click_and_wait_checks_its_arguments_before_clicking(monkeypatch, kwargs, named):
    """The error came after the click, so correcting it and retrying submitted twice."""
    import asyncio

    clicks = _click_and_wait_page(monkeypatch, _CdpTab())
    out = asyncio.run(_tool("click_and_wait")(selector="#submit", **kwargs))
    assert out.startswith("Error:") and named in out
    assert clicks == []


def test_click_and_wait_does_not_call_a_page_with_requests_in_flight_idle(monkeypatch):
    """With capture off the log stays empty, so this reported network_idle after 500ms
    regardless of what the page was doing."""
    import asyncio
    import json as _json
    import time

    from nodriver.cdp import network as cdp_network

    tab = _CdpTab()
    started, _responded, finished = _request_events(tab)
    _click_and_wait_page(monkeypatch, tab, started)
    t0 = time.monotonic()

    async def scenario():
        waiter = asyncio.ensure_future(_tool("click_and_wait")(
            selector="#s", expect="network_idle", timeout=4))
        await asyncio.sleep(0.8)
        assert not waiter.done(), "reported idle with a request in flight"
        await finished()
        return await waiter

    out = asyncio.run(scenario())
    assert _json.loads(out)["observed"]["what"] == "network_idle"
    assert time.monotonic() - t0 >= 1.2  # the finish, plus 500ms quiet
    assert tab.handlers[cdp_network.LoadingFinished] == []


def test_click_and_wait_times_out_honestly_on_a_stuck_request(monkeypatch):
    import asyncio

    tab = _CdpTab()
    started, _r, _f = _request_events(tab)
    _click_and_wait_page(monkeypatch, tab, started)
    out = asyncio.run(_tool("click_and_wait")(selector="#s", expect="network_idle", timeout=0.3))
    assert out.startswith("Error:") and "still in flight" in out


def test_click_and_wait_auto_does_not_claim_idle_it_could_not_measure(monkeypatch):
    import asyncio
    import json as _json

    tab = _CdpTab(fail=("Network.enable",))
    clicks = _click_and_wait_page(monkeypatch, tab)
    out = asyncio.run(_tool("click_and_wait")(selector="#s", timeout=0.1))
    observed = _json.loads(out)["observed"]
    assert observed["what"] is None and "not measured" in observed["network_idle"]
    assert len(clicks) == 1
    refused = asyncio.run(_tool("click_and_wait")(selector="#s", expect="network_idle"))
    assert refused.startswith("Error:") and len(clicks) == 1


def test_wait_for_request_settles_on_whichever_match_responds_first(monkeypatch):
    """Only the latest match was kept: A starts, B starts, A responds, and it timed out
    saying no request matched."""
    import asyncio
    import json as _json

    tab = _active(monkeypatch, _CdpTab())
    start_a, respond_a, _ = _request_events(tab, "a", "https://x.test/api/a")
    start_b, _, _ = _request_events(tab, "b", "https://x.test/api/b")

    async def scenario():
        waiter = asyncio.ensure_future(_tool("wait_for_request")("/api/", timeout=1))
        await asyncio.sleep(0.01)
        await start_a()
        await start_b()
        await respond_a()
        return await waiter

    out = _json.loads(asyncio.run(scenario()))
    assert out["url"].endswith("/api/a") and out["status"] == 201


def test_wait_for_request_separates_seen_without_response_from_never_seen(monkeypatch):
    import asyncio

    tab = _active(monkeypatch, _CdpTab())
    start, _, _ = _request_events(tab)

    async def scenario():
        waiter = asyncio.ensure_future(_tool("wait_for_request")("/api/", timeout=0.1))
        await asyncio.sleep(0.01)
        await start()
        return await waiter

    assert "none got a response" in asyncio.run(scenario())
    assert "no request matched" in asyncio.run(_tool("wait_for_request")("/api/", timeout=0.01))


def test_wait_for_request_reports_a_network_domain_it_could_not_enable(monkeypatch):
    import asyncio

    _active(monkeypatch, _CdpTab(fail=("Network.enable",)))
    out = asyncio.run(_tool("wait_for_request")("/api/", timeout=0.01))
    assert out.startswith("Error:") and "Network.enable" in out


# ── AE: wait_for_network_idle ──


class _IdleTab(_CdpTab):
    ready_state = "complete"

    async def evaluate(self, _expr, return_by_value=True):
        return self.ready_state


def test_wait_for_network_idle_counts_every_request_from_cdp(monkeypatch):
    """It patched fetch and XHR in the page: an image was invisible, a body still downloading
    counted as finished, and window.__mcp_netidle was left for any script to read."""
    import asyncio

    tab = _active(monkeypatch, _IdleTab())
    started, _r, finished = _request_events(tab)

    async def scenario():
        waiter = asyncio.ensure_future(_tool("wait_for_network_idle")(idle_ms=100, timeout=3))
        await asyncio.sleep(0.01)
        await started()
        await asyncio.sleep(0.3)
        assert not waiter.done(), "reported idle with a request in flight"
        await finished()
        return await waiter

    out = asyncio.run(scenario())
    assert not out.startswith("Error:") and "active=0" in out, out
    assert "1 request(s)" in out


def test_wait_for_network_idle_waits_for_the_document_and_names_what_stuck(monkeypatch):
    import asyncio

    tab = _active(monkeypatch, _IdleTab())
    tab.ready_state = "loading"
    out = asyncio.run(_tool("wait_for_network_idle")(idle_ms=10, timeout=0.2))
    assert out.startswith("Error:") and "readyState" in out

    tab2 = _active(monkeypatch, _IdleTab())
    started, _r, _f = _request_events(tab2)

    async def scenario():
        waiter = asyncio.ensure_future(_tool("wait_for_network_idle")(idle_ms=10, timeout=0.2))
        await asyncio.sleep(0.01)
        await started()
        return await waiter

    stuck = asyncio.run(scenario())
    assert "still in flight" in stuck and "/api/save" in stuck


def test_wait_for_network_idle_ignores_streams_that_never_finish(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from nodriver.cdp import network as cdp_network

    tab = _active(monkeypatch, _IdleTab())
    event_source = SimpleNamespace(value="EventSource")

    async def scenario():
        waiter = asyncio.ensure_future(_tool("wait_for_network_idle")(idle_ms=50, timeout=1))
        await asyncio.sleep(0.01)
        await tab.fire(cdp_network.RequestWillBeSent, request_id="s", type_=event_source,
                       request=SimpleNamespace(url="https://x.test/stream"))
        return await waiter

    assert not asyncio.run(scenario()).startswith("Error:")


# ── AR and AS: research ──


class _NavFailTab:
    async def get(self, _url):
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")


def test_a_failed_browser_navigation_is_an_error_not_status_200(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    _active(monkeypatch, _NavFailTab())
    out = asyncio.run(_tool("scrape_markdown")("https://x.test/", use_browser=True))
    assert out.startswith("Error:") and "ERR_NAME_NOT_RESOLVED" in out
    out = asyncio.run(_tool("smart_fetch")("https://x.test/", force_browser=True))
    assert out.startswith("Error:") and "ERR_NAME_NOT_RESOLVED" in out


def test_best_effort_readers_still_get_an_empty_page_from_a_failed_navigation():
    import asyncio

    from mcp_stealth_chrome.tools.research import _browser_render

    assert asyncio.run(_browser_render(_NavFailTab(), "https://x.test/")) == ""


def test_browser_fetch_never_claims_a_status_it_did_not_see(monkeypatch):
    """A real 403 page read as status 200."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import research

    async def rendered(_tab, _url, **_kwargs):
        return "<html><body><p>Forbidden</p></body></html>"

    monkeypatch.setattr(research, "_browser_render", rendered)
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    _active(monkeypatch, object())
    for out in (asyncio.run(_tool("scrape_markdown")("https://x.test/", use_browser=True)),
                asyncio.run(_tool("smart_fetch")("https://x.test/", force_browser=True))):
        data = _json.loads(out)
        assert data["status"] is None and data["status_measured"] is False


@pytest.mark.parametrize("resource", ["issues", "releases"])
def test_github_errors_are_not_an_empty_list(monkeypatch, resource):
    """A 403 rate limit read the same as a repo with no issues."""
    import asyncio

    from mcp_stealth_chrome.tools import research

    async def forbidden(*_a, **_k):
        return 403, "rate limited", {}

    monkeypatch.setattr(research, "_fetch_http", forbidden)
    out = asyncio.run(_tool("github_read")("o/r", resource=resource))
    assert out.startswith("Error:") and "403" in out


def test_github_success_still_lists(monkeypatch):
    import asyncio
    import json as _json

    from mcp_stealth_chrome.tools import research

    async def fine(*_a, **_k):
        return 200, _json.dumps([{"number": 1, "title": "t", "tag_name": "v1"}]), {}

    monkeypatch.setattr(research, "_fetch_http", fine)
    assert _json.loads(asyncio.run(_tool("github_read")("o/r", resource="issues")))["count"] == 1
    assert _json.loads(asyncio.run(_tool("github_read")("o/r", resource="releases")))["count"] == 1


# ── AT: detect_and_bypass ──


@pytest.mark.parametrize("verify,challenge,bypassed,still", [
    (" [auto-verify: CF detected but no clickable widget found]", True, False, True),
    (" [auto-verify: CF detected but no clickable widget found]", False, False, False),
    (" [auto-verify: DOM@(1, 2)]", True, False, True),
    (" [auto-verify: DOM@(1, 2)]", False, True, False),
    (" [auto-verify: DOM@(1, 2)]", RuntimeError("navigating"), False, None),
    ("", False, False, None),
])
def test_bypassed_comes_from_the_page_not_the_message(monkeypatch, verify, challenge, bypassed, still):
    """Every outcome string contains "auto-verify", so an explicit failure read as bypassed."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            if isinstance(challenge, Exception):
                raise challenge
            return challenge

    async def detected():
        return "cloudflare challenge"

    async def verified(_tab, max_attempts=2):
        return verify

    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    _active(monkeypatch, _Tab())
    monkeypatch.setattr(server, "detect_anti_bot", detected)
    monkeypatch.setattr(server, "_auto_verify_cf", verified)
    out = _json.loads(asyncio.run(_tool("detect_and_bypass")()))
    assert out["bypassed"] is bypassed
    assert out.get("challenge_still_present") is still
    assert bool(out["next_steps"]) is not bypassed


# ── AX and CG: cookie files ──


def test_samesite_survives_export_and_import(monkeypatch, tmp_path):
    """default=str wrote "CookieSameSite.NONE", which the importer could not map, so a
    SameSite=None login cookie silently fell back to Chrome's default."""
    import asyncio
    import json as _json
    from types import SimpleNamespace

    from nodriver.cdp import network as cdp_network

    from mcp_stealth_chrome import server

    cookie = SimpleNamespace(
        name="sid", value="v", domain="x.test", path="/", expires=-1.0, http_only=True,
        secure=True, same_site=cdp_network.CookieSameSite.NONE)

    class _Jar:
        async def get_all(self):
            return [cookie]

    class _Browser:
        cookies = _Jar()

    from mcp_stealth_chrome.tools import storage

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    monkeypatch.setattr(storage, "STORAGE_STATE_DIR", tmp_path)
    monkeypatch.setattr(storage, "ensure_dirs", lambda: None)
    assert not asyncio.run(_tool("cookie_export")("c.json")).startswith("Error:")
    saved = _json.loads((tmp_path / "c.json").read_text())
    assert saved[0]["sameSite"] == "None"

    tab = _active(monkeypatch, _CdpTab())
    asyncio.run(_tool("cookie_import")(file_path=str(tmp_path / "c.json")))
    sent = [r for r in tab.sent if r["method"] == "Network.setCookies"][0]
    assert sent["params"]["cookies"][0]["sameSite"] == "None"


def test_clear_first_does_not_wipe_the_jar_for_malformed_input(monkeypatch):
    """clear_first wiped every cookie in the browser, then answered "no valid cookies"."""
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "browser", object())
    tab = _active(monkeypatch, _CdpTab())
    out = asyncio.run(_tool("cookie_import")(cookies=[{"nope": 1}, "garbage"], clear_first=True))
    assert out.startswith("Error:") and "nothing was cleared" in out
    assert tab.sent == []

    out = asyncio.run(_tool("cookie_import")(
        cookies=[{"name": "a", "value": "1", "domain": "x.test"}], clear_first=True))
    assert not out.startswith("Error:")
    assert tab.methods() == ["Network.clearBrowserCookies", "Network.setCookies"]


# ── AJ: screenshot region ──


@pytest.mark.parametrize("kwargs", [
    {"region_selector": "#band"},
    {"region": {"x": 10, "y": 20, "width": 30, "height": 40}},
])
def test_screenshot_region_is_converted_to_page_space(monkeypatch, tmp_path, kwargs):
    """capture_beyond_viewport makes a clip page-space, so with the page scrolled 500px the
    viewport numbers captured some other strip, which still looked valid."""
    import asyncio
    import base64
    import json as _json

    from mcp_stealth_chrome import server

    class _Tab(_CdpTab):
        async def evaluate(self, expr, return_by_value=True):
            if "getBoundingClientRect" in expr:
                return _json.dumps({"x": 10, "y": 20, "width": 30, "height": 40})
            return _json.dumps({"pageLeft": 7, "pageTop": 500, "dpr": 1, "innerW": 800, "innerH": 600})

        async def send(self, command):
            request = next(command)
            self.sent.append(request)
            return base64.b64encode(b"not really a png").decode()

    tab = _active(monkeypatch, _Tab())
    from mcp_stealth_chrome.tools import page

    monkeypatch.setattr(page, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(page, "ensure_dirs", lambda: None)
    asyncio.run(_tool("screenshot")(filename="s.png", max_dimension=0, **kwargs))
    clip = tab.sent[0]["params"]["clip"]
    assert (clip["x"], clip["y"], clip["width"], clip["height"]) == (17, 520, 30, 40)


# ── AY: scrape_page ──


def test_scrape_page_truncates_a_body_with_no_newline(monkeypatch):
    """rfind returned -1, which is truthy, so text[:-1] handed back nearly everything."""
    import asyncio
    import json as _json

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            return _json.dumps({"contentType": "text/html", "title": "t", "url": "u", "body": "x" * 25})

    _active(monkeypatch, _Tab())
    out = _json.loads(asyncio.run(_tool("scrape_page")(return_mode="text", max_chars=5)))
    assert out["text"].startswith("xxxxx\n") and "truncated at 5/25" in out["text"]


def test_scrape_page_still_cuts_at_a_newline(monkeypatch):
    import asyncio
    import json as _json

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            return _json.dumps({"contentType": "text/html", "title": "t", "url": "u", "body": "ab\ncdefgh"})

    _active(monkeypatch, _Tab())
    out = _json.loads(asyncio.run(_tool("scrape_page")(return_mode="text", max_chars=5)))
    assert "truncated at 2/9" in out["text"]


# ── AP: http_request results ──


def _bounded(body, key="body"):
    import json as _json

    from mcp_stealth_chrome.tools.network_http import _RESULT_LIMIT, _bounded_result

    text = _bounded_result({"status": 200}, key, body)
    assert len(text) <= _RESULT_LIMIT
    return _json.loads(text)  # the point: it always parses


def test_a_large_result_with_many_headers_still_fits():
    """Indenting nested meta adds characters per line, so it has to be measured in place."""
    import json as _json

    from mcp_stealth_chrome.tools.network_http import _RESULT_LIMIT, _bounded_result

    meta = {"status": 200, "headers": {f"x-header-{i}": "v" * 20 for i in range(60)}}
    text = _bounded_result(meta, "body", {"rows": ["x" * 100] * 400})
    assert len(text) <= _RESULT_LIMIT
    assert _json.loads(text)["meta"]["truncated"] is True


@POSIX_PROCESS_CHECKS
def test_a_process_that_takes_a_moment_to_die_after_sigkill_counts_as_dead(monkeypatch):
    """Checked at once, it read as still alive, and that now keeps the handle and fails the close."""
    import asyncio
    import os
    import signal

    from mcp_stealth_chrome import server

    class _Browser:
        _process_pid = 424243

        def stop(self):
            return None

    state = {"killed_at": None, "polls_after": 0}

    def fake_kill(pid, sig):
        if sig == signal.SIGKILL:
            state["killed_at"] = True
        elif sig == 0 and state["killed_at"]:
            state["polls_after"] += 1
            if state["polls_after"] > 3:
                raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    report = asyncio.run(server._ensure_browser_dead(_Browser(), None, grace=0))
    assert report["still_alive"] == [] and report["verified"] is True


def test_a_small_result_is_untouched():
    out = _bounded({"a": 1})
    assert out == {"meta": {"status": 200}, "body": {"a": 1}}


@pytest.mark.parametrize("body", [
    {"rows": ["x" * 100] * 400},
    "y" * 40000,
    '"\\\n' * 20000,          # escapes to several times its length
    "é" * 40000,              # \uXXXX under ensure_ascii
], ids=["rows", "plain", "escapes", "non-ascii"])  # a 40,000-char id overflows Windows' env limit
def test_a_large_result_stays_valid_json_and_says_it_was_cut(body):
    """A 30KB response came back as 25,000 chars of invalid JSON with no marker."""
    out = _bounded(body)
    meta = out["meta"]
    assert meta["truncated"] is True and meta["status"] == 200
    assert meta["returned_length"] == len(out["body_text"]) > 1000
    assert meta["original_length"] > meta["returned_length"]


def test_http_request_returns_parseable_json_for_a_large_response():
    import asyncio
    import http.server
    import json as _json
    import threading

    payload = _json.dumps({"rows": ["x" * 1000] * 30}).encode()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a):
            pass

    server_ = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server_.serve_forever, daemon=True).start()
    try:
        out = asyncio.run(_tool("http_request")(f"http://127.0.0.1:{server_.server_port}/"))
    finally:
        server_.shutdown()
    parsed = _json.loads(out)
    assert parsed["meta"]["truncated"] is True
    assert parsed["meta"]["original_length"] == len(payload)


# ── AH, AG, AF: capture ──


@pytest.fixture()
def console_state(monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "console_logs", [])
    monkeypatch.setattr(server.BrowserState, "page_errors", [])
    monkeypatch.setattr(server.BrowserState, "network_logs", [])
    monkeypatch.setattr(server.BrowserState, "network_index", {})
    monkeypatch.setattr(server.BrowserState, "capture_console", False)
    monkeypatch.setattr(server.BrowserState, "capture_network", False)
    monkeypatch.setattr(server, "_CONSOLE_ARMED_TAB_IDS", set())
    monkeypatch.setattr(server, "_NETWORK_ARMED_TAB_IDS", set())
    return server


def _exception_event():
    from types import SimpleNamespace

    return dict(exception_details=SimpleNamespace(
        text="Uncaught", url="https://x.test/a.js", line_number=3,
        exception=SimpleNamespace(description="Error: boom")))


def test_get_page_errors_returns_what_was_captured(monkeypatch, console_state):
    """Nothing appended to page_errors anywhere, so it answered [] on a page that threw."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome.server import cdp_runtime

    tab = _active(monkeypatch, _CdpTab())

    async def scenario():
        await _tool("console_start")()
        await tab.fire(cdp_runtime.ExceptionThrown, **_exception_event())
        return _json.loads(await _tool("get_page_errors")())

    out = asyncio.run(scenario())
    assert out["attached"] is True and out["total"] == 1
    assert out["errors"][0]["text"] == "Error: boom" and out["errors"][0]["line"] == 3


def test_get_page_errors_says_when_nothing_was_listening(monkeypatch, console_state):
    import asyncio
    import json as _json

    _active(monkeypatch, _CdpTab())
    out = _json.loads(asyncio.run(_tool("get_page_errors")()))
    assert out["attached"] is False and out["errors"] == [] and "NOT listening" in out["note"]


def test_console_clear_empties_the_buffer_in_place(monkeypatch, console_state):
    """console_get still returned the old messages after console_clear said it cleared."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome.server import cdp_runtime

    class _Tab(_CdpTab):
        async def evaluate(self, _expr, return_by_value=True):
            return None

    tab = _active(monkeypatch, _Tab())

    def message(kind, value):
        from types import SimpleNamespace

        return dict(type_=kind, args=[SimpleNamespace(value=value)])

    async def scenario():
        await _tool("console_start")()
        buffer = console_state.BrowserState.console_logs
        await tab.fire(cdp_runtime.ConsoleAPICalled, **message("log", "old"))
        assert not (await _tool("console_clear")()).startswith("Error:")
        # The page's own console.clear() event arrives too, and is not a message.
        await tab.fire(cdp_runtime.ConsoleAPICalled, **message("clear", "console.clear"))
        assert console_state.BrowserState.console_logs is buffer
        assert _json.loads(await _tool("console_get")())["total"] == 0
        # The installed handler still writes to the list people read.
        await tab.fire(cdp_runtime.ConsoleAPICalled, **message("log", "new"))
        return _json.loads(await _tool("console_get")())

    out = asyncio.run(scenario())
    assert [m["text"] for m in out["messages"]] == ["new"]


def test_console_start_says_when_runtime_could_not_be_enabled(monkeypatch, console_state):
    """It answered "started", and console_get then said attached and capturing over an
    empty list, which reads as a quiet page."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome.server import cdp_runtime

    tab = _active(monkeypatch, _CdpTab(fail=("Runtime.enable",)))
    out = asyncio.run(_tool("console_start")())
    assert out.startswith("Error:") and "NOT started" in out
    status = _json.loads(asyncio.run(_tool("console_get")()))
    assert status["attached"] is False and status["capturing"] is False
    assert tab.handlers[cdp_runtime.ConsoleAPICalled] == []


def test_console_restart_says_when_runtime_could_not_be_enabled(monkeypatch, console_state):
    import asyncio

    tab = _active(monkeypatch, _CdpTab())
    asyncio.run(_tool("console_start")())
    tab.fail = ("Runtime.enable",)
    out = asyncio.run(_tool("console_start")())
    assert out.startswith("Error:") and "NOT re-armed" in out
    assert console_state.BrowserState.capture_console is False


def test_network_start_says_when_network_could_not_be_enabled(monkeypatch, console_state):
    import asyncio

    from nodriver.cdp import network as cdp_network

    tab = _active(monkeypatch, _CdpTab(fail=("Network.enable",)))
    out = asyncio.run(_tool("network_start")())
    assert out.startswith("Error:") and "NOT started" in out
    assert console_state.BrowserState.capture_network is False
    assert not tab.handlers.get(cdp_network.RequestWillBeSent)
    assert "T1" not in console_state._NETWORK_ARMED_TAB_IDS


# ── AU: emulate_device ──


class _VersionTab(_CdpTab):
    NATIVE = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0.0.0 Safari/537.36"

    async def send(self, command):
        request = next(command)
        self.sent.append(request)
        if request["method"] == "Browser.getVersion":
            try:
                command.send({"protocolVersion": "1.3", "product": "Chrome/156.0.1.2",
                              "revision": "r", "userAgent": self.NATIVE, "jsVersion": "1"})
            except StopIteration as stop:
                return stop.value
        return None

    def overrides(self):
        return [r["params"] for r in self.sent if r["method"] == "Network.setUserAgentOverride"]


def test_device_reset_restores_the_user_agent(monkeypatch):
    """desktop cleared the metrics only and said "overrides cleared" with Safari's UA active."""
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "current_fingerprint", {})
    tab = _active(monkeypatch, _VersionTab())
    out = asyncio.run(_tool("emulate_device")("desktop"))
    assert "Emulation.clearDeviceMetricsOverride" in tab.methods()
    assert tab.overrides()[-1]["userAgent"] == _VersionTab.NATIVE
    assert tab.overrides()[-1]["userAgentMetadata"]["platform"] == "macOS"
    assert _VersionTab.NATIVE in out


def test_device_reset_returns_to_a_pinned_identity(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "current_fingerprint",
                        {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0 Safari/537.36"})
    tab = _active(monkeypatch, _VersionTab())
    asyncio.run(_tool("emulate_device")("desktop"))
    assert "Windows" in tab.overrides()[-1]["userAgent"]


def test_android_device_sends_hints_that_match_the_device(monkeypatch):
    import asyncio

    tab = _active(monkeypatch, _VersionTab())
    asyncio.run(_tool("emulate_device")("pixel-8"))
    override = tab.overrides()[-1]
    hints = override["userAgentMetadata"]
    assert hints["platform"] == "Android" and hints["mobile"] is True
    assert hints["model"] == "Pixel 8" and hints["platformVersion"] == "14"
    assert hints["brands"], "brands empty is the defect itself"
    assert override["platform"] == "Linux armv81"


def test_safari_device_says_userAgentData_cannot_match(monkeypatch):
    import asyncio

    tab = _active(monkeypatch, _VersionTab())
    out = asyncio.run(_tool("emulate_device")("iphone-15"))
    assert "userAgentMetadata" not in tab.overrides()[-1]
    assert tab.overrides()[-1]["platform"] == "iPhone"
    assert "userAgentData" in out


# ── Y: snapshot diff ──


def test_snapshot_hash_sees_every_displayed_field():
    """An icon button going from aria-label Save to Delete read "unchanged; details omitted"."""
    from mcp_stealth_chrome.snapshot import snapshot_hash

    base = {"tag": "button", "role": "button", "text": "", "ref": "e0", "aria": "Save",
            "type": "", "name": "", "placeholder": "", "hidden": False}
    assert snapshot_hash([base]) != snapshot_hash([{**base, "aria": "Delete"}])
    for key, value in (("type", "submit"), ("name", "n"), ("placeholder", "p"), ("hidden", True)):
        assert snapshot_hash([base]) != snapshot_hash([{**base, key: value}]), key
    assert snapshot_hash([base]) == snapshot_hash([{**base, "ref": "e9"}])


# ── AN: storage_diff ──


def test_storage_diff_tells_cookies_on_different_paths_apart(monkeypatch):
    """SID at /admin changing, with SID at / unchanged, produced an empty diff."""
    import asyncio
    import json as _json

    from mcp_stealth_chrome import server

    def snapshot(admin_value):
        return {"url": "u", "title": "t", "localStorage": {}, "sessionStorage": {}, "errors": {},
                "measured": {"url": True, "cookies": True, "localStorage": True, "sessionStorage": True},
                "cookies": [{"name": "SID", "domain": "x", "path": "/admin", "value": admin_value},
                            {"name": "SID", "domain": "x", "path": "/", "value": "site"}]}

    async def fake_snapshot(_tab):
        return snapshot("new")

    monkeypatch.setattr(server, "_take_storage_snapshot", fake_snapshot)
    _active(monkeypatch, object())
    monkeypatch.setitem(server._STORAGE_SNAPSHOTS, "paths", snapshot("old"))
    cookies = _json.loads(asyncio.run(_tool("storage_diff")("paths")))["cookies"]
    assert cookies["modified"] == [{"name": "SID", "domain": "x", "path": "/admin"}]
    assert cookies["added"] == [] and cookies["removed"] == []


# ── BF and W: smart_fill ──


def _smart_fill_page(monkeypatch, *, field_type="input", setter_result=lambda v: v, submit="clicked"):
    import json as _json
    import re
    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    submits, presses = [], []

    class _Tab:
        async def mouse_click(self, x, y):
            presses.append((x, y))

        async def send(self, command):
            request = next(command)
            wanted = re.search(r"const wanted = (\".*?\");", request["params"]["functionDeclaration"])
            value = _json.loads(wanted.group(1)) if wanted else None
            return SimpleNamespace(value=setter_result(value)), None

    async def parts(_tab, _expr, _group):
        best = {"label": "L", "x": 1, "y": 2, "type": field_type, "score": 1}
        return {"json": SimpleNamespace(value=_json.dumps({"best": best, "candidates": []})),
                "el": SimpleNamespace(object_id=server.cdp_runtime.RemoteObjectId("o"))}

    async def release(*_a):
        return None

    async def click_text(label, exact=False):
        submits.append(label)
        return submit

    _active(monkeypatch, _Tab())
    monkeypatch.setattr(server, "evaluate_parts", parts)
    monkeypatch.setattr(server, "release_group", release)
    monkeypatch.setattr(server, "click_text", click_text)
    return submits, presses


def test_smart_fill_reports_a_failed_submit(monkeypatch):
    """A missing submit button got an ok reply, so a workflow carried on as if submitted."""
    import asyncio

    submits, _ = _smart_fill_page(monkeypatch, submit="Error: no element with text 'Go'")
    out = asyncio.run(_tool("smart_fill")({"Name": "x"}, submit_label="Go"))
    assert out.startswith("Error:") and "no element with text" in out
    assert submits == ["Go"]


def test_smart_fill_does_not_submit_after_a_field_failed(monkeypatch):
    import asyncio

    submits, _ = _smart_fill_page(monkeypatch, setter_result=lambda _v: "masked")
    out = asyncio.run(_tool("smart_fill")({"Name": "x"}, submit_label="Go"))
    assert out.startswith("Error:") and "NOT clicked" in out
    assert submits == []


def test_smart_fill_success_still_submits(monkeypatch):
    import asyncio

    submits, _ = _smart_fill_page(monkeypatch)
    out = asyncio.run(_tool("smart_fill")({"Name": "x"}, submit_label="Go"))
    assert not out.startswith("Error:")
    assert submits == ["Go"]


def test_smart_fill_does_not_click_a_select_open(monkeypatch):
    import asyncio

    _, presses = _smart_fill_page(monkeypatch, field_type="select")
    assert not asyncio.run(_tool("smart_fill")({"Country": "x"})).startswith("Error:")
    assert presses == []


# ── CD: solve_captcha ──


def test_capsolver_ready_with_no_token_is_an_error(monkeypatch):
    """It returned "", and the tool then answered ok("token: ")."""
    import asyncio

    from mcp_stealth_chrome import captcha

    class _Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class _Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, json=None):
            return _Response({"taskId": "t"} if "createTask" in url
                             else {"status": "ready", "solution": {}})

    real_sleep = asyncio.sleep

    async def no_wait(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(captcha.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(captcha.asyncio, "sleep", no_wait)
    with pytest.raises(captcha.CapSolverError, match="no token"):
        asyncio.run(captcha.solve("T", "https://x.test", "k", api_key="not-a-key"))


@pytest.mark.parametrize("page,up,prefix,shown", [
    (True, True, "token: TOK", "injected: true"),
    (False, True, "Error: token: TOK", "injected: false"),
    (RuntimeError("context lost"), True, "Error: token: TOK", "context lost"),
    (True, False, "Error: token: TOK", "no browser running"),
])
def test_solve_captcha_reports_whether_the_token_reached_the_page(monkeypatch, page, up, prefix, shown):
    """A missing field or a raising evaluate read as success, so the caller submitted an
    unsolved form. The token is kept either way: it was paid for."""
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            if isinstance(page, Exception):
                raise page
            return page

    async def solved(**_kwargs):
        return "TOK"

    from mcp_stealth_chrome.tools import stealth

    monkeypatch.setattr(stealth, "capsolver_solve", solved)
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: up))
    _active(monkeypatch, _Tab())
    out = asyncio.run(_tool("solve_captcha")(
        "turnstile", "https://x.test", "k", inject_selector="#t"))
    assert out.startswith(prefix) and shown in out, out


def test_solve_captcha_without_injection_is_unchanged(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    async def solved(**_kwargs):
        return "TOK"

    from mcp_stealth_chrome.tools import stealth

    monkeypatch.setattr(stealth, "capsolver_solve", solved)
    assert asyncio.run(_tool("solve_captcha")("turnstile", "https://x.test", "k")) == "token: TOK"


class _SlowConfig:
    def __init__(self, path):
        self.user_data_dir = str(path)
        self.host = None
        self.port = None


class _FailedBrowser:
    """What nodriver leaves registered after Chrome spawned and the port never answered."""

    _process = object()
    _process_pid = 4242

    def __init__(self, config):
        self.config = config

    def stop(self):
        pass


def _nodriver_start(monkeypatch, tmp_path, outcomes):
    """A nodriver.start that treats a set host and port as nodriver does."""
    from mcp_stealth_chrome import server, state

    spawned, signalled = [], []
    registered = set()
    # nodriver walks its real registry at exit, which must not meet a stand-in.
    monkeypatch.setattr(server.nodriver.core.util, "get_registered_instances", lambda: registered)

    async def fake_start(config=None):
        if config.host is None or config.port is None:
            config.host, config.port = "127.0.0.1", 50000 + len(spawned)
            spawned.append(config.port)
            registered.add(_FailedBrowser(config))
        outcome = outcomes[min(len(spawned) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fake_kill(pid, sig):
        if sig:
            signalled.append(pid)

    monkeypatch.setattr(server.nodriver, "start", fake_start)
    monkeypatch.setattr(server.os, "kill", fake_kill)
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda _p: [7777])
    monkeypatch.setattr(server, "_pid_recycled", lambda _pid, _browser: False)
    monkeypatch.setattr(server, "_descendant_pids", lambda _pid, _data_dir=None: [])
    return spawned, signalled


async def _no_sleep(_seconds=0):
    return None


@POSIX_PROCESS_CHECKS
def test_slow_chrome_is_ended_and_really_started_again(tmp_path, monkeypatch):
    """A retry keeping nodriver's port spawns nothing, so it counts spawns, not calls."""
    import asyncio

    from mcp_stealth_chrome import server

    spawned, signalled = _nodriver_start(
        monkeypatch, tmp_path, [Exception("Failed to connect to browser"), "browser"],
    )
    assert asyncio.run(server._start_chrome(_SlowConfig(tmp_path))) == "browser"
    assert len(spawned) == 2
    assert 4242 in signalled


def test_failed_start_never_signals_whoever_holds_the_profile(tmp_path, monkeypatch):
    """A Chrome that hands over to the holder exits; a profile scan would hit the holder."""
    import asyncio

    from mcp_stealth_chrome import server

    _spawned, signalled = _nodriver_start(
        monkeypatch, tmp_path, [Exception("Failed to connect to browser")],
    )
    with pytest.raises(Exception, match="Failed to connect") as caught:
        asyncio.run(server._start_chrome(_SlowConfig(tmp_path)))
    assert 7777 not in signalled
    assert "killed" in caught.value.reap_report


def test_failed_start_only_retries_a_slow_chrome(tmp_path, monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    spawned, _signalled = _nodriver_start(monkeypatch, tmp_path, [RuntimeError("no such binary")])
    with pytest.raises(RuntimeError):
        asyncio.run(server._start_chrome(_SlowConfig(tmp_path)))
    assert len(spawned) == 1


def test_only_nodrivers_own_temp_profile_is_removed(tmp_path, monkeypatch):
    import tempfile as _tempfile

    from mcp_stealth_chrome import server

    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))
    temp = tmp_path / "uc_abc"
    temp.mkdir()
    real = tmp_path / "profile"
    real.mkdir()
    server._remove_temp_profile(_SlowConfig(temp))
    server._remove_temp_profile(_SlowConfig(real))
    assert not temp.exists() and real.exists()


@pytest.mark.parametrize("name,secret", [
    ("Cookie", True), ("Set-Cookie", True), ("Authorization", True), ("Proxy-Authorization", True),
    ("X-CSRF-Token", True), ("x-api-key", True), ("X-Auth-Token", True), ("accessToken", True),
    ("access_token", True), ("password", True), ("passwordConfirm", True), ("user[password]", True),
    ("sessionId", True), ("apiKey", True),
    ("Content-Type", False), ("User-Agent", False), (":authority", False), ("author", False),
    ("sec-ch-ua", False), ("X-Forwarded-For", False), ("passenger", False), ("tokenize", False),
])
def test_secret_names(name, secret):
    from mcp_stealth_chrome.helpers import is_secret_name

    assert is_secret_name(name) is secret


def test_masking_keeps_what_is_not_secret_byte_for_byte():
    from mcp_stealth_chrome.helpers import mask_body, mask_url

    for body in ('{"q":"it\'s ok"}', "page=2&q=a+b", "plain text", "<xml a=\"1\"/>", "", None):
        assert mask_body(body) == body
    assert mask_url("https://x.test/a?page=2&q=a%20b") == "https://x.test/a?page=2&q=a%20b"


def test_masking_hides_secrets_at_any_depth():
    from mcp_stealth_chrome.helpers import mask_body, mask_url

    out = json.loads(mask_body(json.dumps({
        "user": "budi", "password": "P1", "nested": [{"token": "T1", "keep": 1}],
        "session": {"id": 5, "note": "n"}, "pin": 1234, "auth": True,
    })))
    assert out["user"] == "budi" and out["nested"][0]["keep"] == 1
    assert out["password"].startswith("***") and out["nested"][0]["token"].startswith("***")
    assert out["pin"].startswith("***") and out["auth"] is True
    assert out["session"] == {"id": 5, "note": "n"}  # a container is searched, not erased
    assert mask_body("user=budi&password=P1") == "user=budi&password=***"
    assert "T1" not in mask_url("https://x.test/a?access_token=T1&page=2")
    assert "page=2" in mask_url("https://x.test/a?access_token=T1&page=2")


def test_handlers_are_added_all_or_none():
    from mcp_stealth_chrome import server

    class _Tab:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, event, handler):
            if event == "second":
                raise RuntimeError("no such domain")
            self.handlers.setdefault(event, []).append(handler)

    tab = _Tab()
    with pytest.raises(RuntimeError):
        server._add_all_handlers(tab, [("first", print), ("second", print)])
    assert tab.handlers == {"first": []}


def test_storage_state_does_not_count_a_rejected_cookie(tmp_path):
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def send(self, _command):
            return False

    class _Browser:
        tabs = [_Tab()]

    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "cookies": [{"name": "sid", "value": "v", "domain": "x.test"}], "origins": {},
    }))
    with pytest.raises(RuntimeError, match="rejected cookie 'sid'"):
        asyncio.run(server._apply_storage_state(_Browser(), str(state)))


def test_cookie_delete_says_so_when_the_cookie_survives(monkeypatch):
    """Network.deleteCookies answers success whether or not anything matched, so only reading
    back shows a delete that did nothing. Real Chrome deletes these, so a fake has to refuse."""
    import asyncio
    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    stubborn = SimpleNamespace(name="cf_clearance", domain=".site.test", path="/", partition_key=None)

    async def get_all():
        return [stubborn]

    class Tab:
        async def send(self, *_args):
            return None

    browser = SimpleNamespace(cookies=SimpleNamespace(get_all=get_all))
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: Tab()))
    tool = server.mcp._tool_manager._tools["cookie_delete"].fn
    out = asyncio.run(tool(name="cf_clearance", domain=".site.test"))
    assert out.startswith("Error") and "still present" in out, out


# ── 0.12.7: a failed Turnstile click names the path that passed ──


def test_failed_turnstile_click_points_to_the_os_cursor_tool(monkeypatch):
    """CDP clicks failed 3 of 3 on a full-page challenge and the OS cursor passed 3 of 3,
    yet the failure only said NOT passed and left the agent to click again."""
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            return '{"title": "Just a moment...", "token": ""}'

    async def still(_tab, active=False):
        return True

    monkeypatch.setattr(server, "_cf_challenge_state", still)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda *_a: _real_sleep(0))
    out = asyncio.run(server._cf_after_click(_Tab(), wait=0))
    assert "NOT passed" in out and "solve_turnstile_oscursor" in out and "[os-cursor]" in out, out


def test_passed_turnstile_click_does_not_suggest_another_tool(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server

    class _Tab:
        async def evaluate(self, _js, return_by_value=True):
            return '{"title": "Shop", "token": "abcdefghij"}'

    async def gone(_tab, active=False):
        return False

    monkeypatch.setattr(server, "_cf_challenge_state", gone)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda *_a: _real_sleep(0))
    out = asyncio.run(server._cf_after_click(_Tab(), wait=0))
    assert "passed as far as the page shows" in out and "solve_turnstile_oscursor" not in out, out


@pytest.mark.parametrize("clicks", [True, False])
def test_auto_verify_failure_points_to_the_os_cursor_tool(monkeypatch, clicks):
    import asyncio

    from mcp_stealth_chrome import server

    async def present(_tab, active=False):
        return True

    async def dom_click(_tab, offset_x=30):
        return (10, 10) if clicks else None

    async def template_click(_tab, _template=None):
        return False, "no match"

    monkeypatch.setattr(server, "_has_cf_challenge", present)
    monkeypatch.setattr(server, "_attempt_turnstile_click", dom_click)
    monkeypatch.setattr(server, "_cf_template_click", template_click)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda *_a: _real_sleep(0))
    out = asyncio.run(server._auto_verify_cf(object(), max_attempts=1, budget=3.0))
    assert "solve_turnstile_oscursor" in out and "[os-cursor]" in out, out


def test_click_turnstile_description_points_to_the_os_cursor_tool():
    from mcp_stealth_chrome import server

    for name in ("click_turnstile", "verify_cf"):
        doc = server.mcp._tool_manager._tools[name].description
        assert "solve_turnstile_oscursor" in doc, name


# ── Quit on a headed browser: Chrome started with --no-startup-window keeps running ──


def _window_watch_setup(monkeypatch, pages):
    import asyncio

    from mcp_stealth_chrome import server

    browser = object()
    ended = []

    async def count(_browser):
        return pages[0]

    async def end_current(record_reason=None):
        ended.append("current")
        server.BrowserState.browser = None
        return True

    monkeypatch.setattr(server, "_page_target_count", count)
    monkeypatch.setattr(server, "_end_current_instance", end_current)
    monkeypatch.setattr(server, "_WINDOWLESS_GRACE", 0.0)
    monkeypatch.setattr(server, "_WINDOW_WATCH", {id(browser): {"browser": browser, "zero_since": None,
                                                                "tool_closed": False}})
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "instances", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    sweep = lambda: asyncio.run(server._sweep_windowless())  # noqa: E731
    return server, browser, ended, sweep


def test_browser_without_windows_is_ended_and_the_next_call_says_why(monkeypatch):
    """Quit closed every window and left Chrome running with its Dock icon: the server must
    end it and say so, not keep a windowless process alive."""
    server, _browser, ended, sweep = _window_watch_setup(monkeypatch, [0])
    sweep()
    assert ended == []  # the first sighting only starts the grace period
    sweep()
    assert ended == ["current"]
    note = server.BrowserState.window_closed[server.BrowserState.current_instance_id]
    assert "last window" in note
    monkeypatch.setattr(server.BrowserState, "tabs", [], raising=False)
    with pytest.raises(RuntimeError, match="last window"):
        server.BrowserState.active_tab()


def test_browser_with_a_window_is_left_alone(monkeypatch):
    _server, _browser, ended, sweep = _window_watch_setup(monkeypatch, [1])
    sweep(); sweep()
    assert ended == []


def test_last_tab_closed_by_a_tool_is_not_a_quit(monkeypatch):
    server, browser, ended, sweep = _window_watch_setup(monkeypatch, [0])
    server._window_watch_tool_closed(browser)
    sweep(); sweep()
    assert ended == []


def test_unanswered_target_query_never_closes(monkeypatch):
    _server, _browser, ended, sweep = _window_watch_setup(monkeypatch, [None])
    sweep(); sweep(); sweep()
    assert ended == []


class _FakeWatcher:
    def __init__(self, running):
        self.running = running

    def poll(self):
        return None if self.running else 0


def _watched_browser(tmp_path, monkeypatch, watcher_running, verdict):
    import time
    from types import SimpleNamespace

    from mcp_stealth_chrome import quit_watch

    monkeypatch.setenv("MCP_STEALTH_QUIT_WATCH_DIR", str(tmp_path))
    browser = SimpleNamespace(_process=None, _process_pid=4242, stopped=True,
                              config=SimpleNamespace(host="127.0.0.1", port=9, user_data_dir="/p"),
                              _quit_watcher=_FakeWatcher(watcher_running))
    if verdict:
        (tmp_path / "chrome-4242-9.quit").write_text(
            json.dumps({"pid": 4242, "port": 9, "user_data_dir": "/p", "at": time.time()}))
    return browser, quit_watch


def _sweep_with(monkeypatch, browser):
    import asyncio

    from mcp_stealth_chrome import server

    asked, ended = [], []

    async def count(_browser):
        asked.append(1)
        return 0

    async def end_current(record_reason=None):
        ended.append("current")
        server.BrowserState.browser = None
        return True

    monkeypatch.setattr(server, "_page_target_count", count)
    monkeypatch.setattr(server, "_end_current_instance", end_current)
    monkeypatch.setattr(server, "_WINDOWLESS_GRACE", 0.0)
    monkeypatch.setattr(server, "_WINDOW_WATCH", {id(browser): {"browser": browser, "zero_since": None,
                                                                "tool_closed": False}})
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "instances", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    sweep = lambda: asyncio.run(server._sweep_windowless())  # noqa: E731
    return server, asked, ended, sweep


def test_a_running_quit_watcher_is_the_only_one_that_closes(tmp_path, monkeypatch):
    """Two closers would race into a double close. While the watcher runs, the sweep neither
    asks for pages nor ends the browser."""
    browser, _qw = _watched_browser(tmp_path, monkeypatch, watcher_running=True, verdict=False)
    _server, asked, ended, sweep = _sweep_with(monkeypatch, browser)
    sweep(); sweep(); sweep()
    assert asked == [] and ended == []


def test_the_watchers_verdict_is_recorded_at_once_and_cleaned_up_after(tmp_path, monkeypatch):
    browser, qw = _watched_browser(tmp_path, monkeypatch, watcher_running=True, verdict=True)
    server, _asked, ended, sweep = _sweep_with(monkeypatch, browser)
    sweep()
    note = server.BrowserState.window_closed[server.BrowserState.current_instance_id]
    assert "last window" in note and ended == []  # still closing: the watcher's job
    browser._quit_watcher.running = False
    sweep()
    assert ended == ["current"] and qw.quit_time(browser) is None


def test_a_quit_read_before_the_sweep_is_not_a_crash(tmp_path, monkeypatch):
    """The watcher ended Chrome a moment ago and the sweep has not run yet: the next call must
    say the last window was closed, not that the browser died and needs browser_recover."""
    from mcp_stealth_chrome import server

    browser, _qw = _watched_browser(tmp_path, monkeypatch, watcher_running=False, verdict=True)
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [object()], raising=False)
    with pytest.raises(RuntimeError, match="last window") as raised:
        server.BrowserState.active_tab()
    assert "browser_recover" not in str(raised.value)


def test_a_crash_without_a_verdict_still_reads_as_a_crash(tmp_path, monkeypatch):
    from mcp_stealth_chrome import server

    browser, _qw = _watched_browser(tmp_path, monkeypatch, watcher_running=False, verdict=False)
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [object()], raising=False)
    with pytest.raises(RuntimeError, match="browser_recover"):
        server.BrowserState.active_tab()


def test_a_dead_watcher_hands_back_to_the_sweep(tmp_path, monkeypatch):
    """A watcher that failed to start or died: the server's own sweep still ends the browser."""
    browser, _qw = _watched_browser(tmp_path, monkeypatch, watcher_running=False, verdict=False)
    _server, asked, ended, sweep = _sweep_with(monkeypatch, browser)
    sweep(); sweep()
    assert asked and ended == ["current"]


def test_tab_close_marks_the_browser_before_closing_its_last_tab(monkeypatch):
    """The watcher polls on its own; marked after the close, it could read the empty browser
    first."""
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import navigation

    order = []

    class Tab:
        target = None

        async def close(self):
            order.append("close")

    async def refresh():
        return True  # the browser answered

    tab = Tab()
    monkeypatch.setattr(server, "_refresh_tabs", refresh)
    monkeypatch.setattr(server, "_clear_tab_scoped_state", lambda tabs: None)
    monkeypatch.setattr(server, "_window_watch_tool_closed", lambda b: order.append("mark"))
    monkeypatch.setattr(server.BrowserState, "tabs", [tab], raising=False)
    monkeypatch.setattr(server.BrowserState, "active_tab_index", 0, raising=False)
    monkeypatch.setattr(server.BrowserState, "browser", object(), raising=False)
    asyncio.run(navigation.tab_close())
    assert order[:2] == ["mark", "close"], order


@pytest.mark.parametrize("case", ["close_failed", "close_failed_but_tab_gone", "after_close_failed"])
def test_tab_close_that_fails_leaves_no_false_tool_closed_mark(monkeypatch, case):
    """The mark tells the quit watcher that a tool emptied the browser, so it waits instead of
    ending it. A close that did not happen must take it back; one that happened keeps it and
    says what failed after it."""
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import navigation

    marked = {"on": False}

    class Tab:
        target = SimpleNamespace(target_id="T1")

        async def close(self):
            if case.startswith("close_failed"):
                raise RuntimeError("websocket closed (simulated)")

    refreshes = []

    async def refresh():
        refreshes.append(1)
        if case == "after_close_failed" and len(refreshes) > 1:  # the first is the list check
            raise RuntimeError("update_targets failed (simulated)")
        return True

    async def pages(_browser):
        return [] if case == "close_failed_but_tab_gone" else [("T1", "https://a.example/")]

    tab = Tab()
    monkeypatch.setattr(server, "_refresh_tabs", refresh)
    monkeypatch.setattr(server, "_page_targets", pages)
    monkeypatch.setattr(server, "_clear_tab_scoped_state", lambda tabs: None)
    monkeypatch.setattr(server, "_window_watch_tool_closed", lambda b: marked.update(on=True))
    monkeypatch.setattr(server, "_window_watch_tool_closed_undo", lambda b: marked.update(on=False),
                        raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [tab], raising=False)
    monkeypatch.setattr(server.BrowserState, "active_tab_index", 0, raising=False)
    monkeypatch.setattr(server.BrowserState, "browser", object(), raising=False)
    out = asyncio.run(navigation.tab_close())
    if case == "close_failed":
        assert out.startswith("Error: tab 0 not closed") and marked["on"] is False, (out, marked)
    elif case == "close_failed_but_tab_gone":  # Chrome no longer lists it: it did close
        assert out == "closed tab 0" and marked["on"] is True, (out, marked)
    else:
        assert out.startswith("Error: closed tab 0, but") and marked["on"] is True, (out, marked)


def test_testing_mode_features_join_nodrivers_list(tmp_path):
    """Chrome honours only the last --disable-features, and nodriver's own comes first, so
    testing_mode's flag dropped IsolateOrigins,site-per-process."""
    import nodriver

    from mcp_stealth_chrome import server

    config = nodriver.Config(user_data_dir=str(tmp_path), headless=True)
    ours = ("Translate", "BackForwardCache", "AcceptCHFrame")
    config.add_argument(server._disable_features_arg(config, ours))
    last = [a for a in config() if a.startswith("--disable-features=")][-1]
    features = last.split("=", 1)[1].split(",")
    assert {"IsolateOrigins", "site-per-process", *ours} <= set(features), last
    assert len(features) == len(set(features)), last


# --- a headless window must fit in its own screen ---------------------------------------------

def _screen_arg(monkeypatch, real, width=1280, height=800, args=None):
    from mcp_stealth_chrome import server
    monkeypatch.setattr(server, "_real_screen", lambda: real)
    return server._headless_screen_arg(width, height, args)


def test_headless_screen_is_the_real_display_when_the_window_fits(monkeypatch):
    real = {"width": 1800, "height": 1169, "top": 39, "bottom": 62, "left": 0, "right": 0}
    assert _screen_arg(monkeypatch, real) == "--screen-info={1800x1169 workAreaTop=39 workAreaBottom=62}"


@pytest.mark.parametrize("real", [None, {"width": 1024, "height": 768, "top": 25, "bottom": 0, "left": 0, "right": 0}])
def test_headless_screen_falls_back_to_one_that_holds_the_window(monkeypatch, real):
    """Headless Chrome's own 800x600 screen is smaller than the default window."""
    import re
    arg = _screen_arg(monkeypatch, real, width=2000, height=1200)
    width, height = map(int, re.match(r"--screen-info=\{(\d+)x(\d+)", arg).groups())
    assert width >= 2000 and height >= 1200, arg


def test_headless_screen_grows_past_every_listed_size(monkeypatch):
    arg = _screen_arg(monkeypatch, None, width=5000, height=3000)
    assert arg.startswith("--screen-info={5000x3000"), arg


def test_a_callers_own_screen_info_is_left_alone(monkeypatch):
    assert _screen_arg(monkeypatch, None, args=["--screen-info={1024x768}"]) is None


# --- closing ends this browser's processes, and only those -------------------------------------

def _launcher_with_a_foreign_child(tmp_path):
    """A process whose command line names the profile, with one child that does and one that
    does not, as Chrome has its helpers and also the machine-wide updater it wakes."""
    import subprocess
    import sys
    import time

    profile = str(tmp_path / "profile")
    nap = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {nap!r}, '--user-data-dir={profile}'])\n"
        f"subprocess.Popen([sys.executable, '-c', {nap!r}, 'updater-wake'])\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, f"--user-data-dir={profile}"])
    from mcp_stealth_chrome import server
    for _ in range(100):
        if len(server._descendant_pids(parent.pid)) == 2:
            break
        time.sleep(0.05)
    return parent, profile


def _end(pids):
    import contextlib
    import os
    import signal
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


@POSIX_PROCESS_CHECKS
def test_the_browsers_family_leaves_out_what_is_not_the_browser(tmp_path):
    from mcp_stealth_chrome import server

    parent, profile = _launcher_with_a_foreign_child(tmp_path)
    everyone = server._descendant_pids(parent.pid)
    try:
        own = server._descendant_pids(parent.pid, profile)
        assert len(everyone) == 2 and len(own) == 1, (everyone, own)
        assert profile in server._process_command(own[0])
    finally:
        _end([parent.pid, *everyone])
        parent.wait()


@POSIX_PROCESS_CHECKS
def test_closing_does_not_signal_a_program_the_browser_merely_started(tmp_path):
    """Chrome wakes GoogleUpdater, which is still running when Chrome has gone. It was counted
    as the browser: 4 closes in 5 said "CDP stop did not end the process" and sent it SIGTERM."""
    import asyncio
    import time
    import types

    from mcp_stealth_chrome import server

    parent, profile = _launcher_with_a_foreign_child(tmp_path)
    everyone = server._descendant_pids(parent.pid)
    own = server._descendant_pids(parent.pid, profile)
    foreign = [pid for pid in everyone if pid not in own]

    class _Browser:
        _process_pid = parent.pid
        _process = parent
        config = types.SimpleNamespace(user_data_dir=profile, browser_executable_path=None,
                                       host=None, port=None)

        def stop(self):
            _end([parent.pid, *own])
            parent.wait()

    try:
        started = time.monotonic()
        report = asyncio.run(server._ensure_browser_dead(_Browser(), None))
        took = time.monotonic() - started
        assert report["verified"] is True and report["killed"] == [], report
        assert server._process_exists(foreign[0]), "the foreign child was ended with the browser"
        assert took < 1.0, f"a browser that was already gone still took {took:.2f}s to close"
    finally:
        _end([parent.pid, *everyone])


@POSIX_PROCESS_CHECKS
def test_a_browser_that_ignores_stop_is_still_signalled_and_verified(tmp_path):
    import asyncio
    import types

    from mcp_stealth_chrome import server

    parent, profile = _launcher_with_a_foreign_child(tmp_path)
    everyone = server._descendant_pids(parent.pid)
    own = server._descendant_pids(parent.pid, profile)

    class _Browser:
        _process_pid = parent.pid
        _process = parent
        config = types.SimpleNamespace(user_data_dir=profile, browser_executable_path=None,
                                       host=None, port=None)

        def stop(self):
            return None

    try:
        import threading
        threading.Thread(target=parent.wait, daemon=True).start()  # reap, as asyncio does
        report = asyncio.run(server._ensure_browser_dead(_Browser(), None, grace=0.2))
        assert sorted(report["killed"]) == sorted([parent.pid, *own]), report
        assert report["verified"] is True and report["still_alive"] == [], report
    finally:
        _end([parent.pid, *everyone])


def test_a_close_by_a_tool_stops_the_quit_watcher_first(monkeypatch):
    """browser_close and close_instance end the browser themselves; a watcher left running
    could see the closing browser empty and close it a second time."""
    import asyncio
    from types import SimpleNamespace

    from mcp_stealth_chrome import quit_watch, server

    stopped = []
    monkeypatch.setattr(quit_watch, "stop", lambda browser: stopped.append(browser))
    browser = SimpleNamespace(_process=None, _process_pid=None, connection=None,
                              config=SimpleNamespace(port=None, user_data_dir=None), stop=lambda: None)
    asyncio.run(server._ensure_browser_dead(browser))
    assert stopped == [browser]


def test_os_cursor_status_does_not_wait_for_a_slow_first_measurement(monkeypatch):
    """Importing Quartz and AppKit the first time took seconds while the server started, and
    the first server_status said "not measured (TimeoutError)"."""
    import asyncio
    import time as _time

    from mcp_stealth_chrome import os_input, server

    def slow():
        _time.sleep(2.5)
        return True, "measured"

    monkeypatch.setattr(os_input, "is_available", slow)
    monkeypatch.setattr(server, "_OS_CURSOR_PROBE",
                        {"thread": None, "started": None, "result": None, "measured_at": None})
    started = _time.monotonic()
    first = asyncio.run(server._os_cursor_status())
    assert _time.monotonic() - started < 1.5
    assert first["available"] is None and "still being measured" in first["detail"], first
    server._OS_CURSOR_PROBE["thread"].join(5)
    assert asyncio.run(server._os_cursor_status()) == {"available": True, "detail": "measured"}


def test_the_idle_sweep_leaves_the_current_instance_alone_while_a_call_runs(monkeypatch):
    """A call running longer than idle_timeout_seconds is use: on Windows CI the reaper closed
    the current instance during tab_new, and the next tab_select read an empty list."""
    import asyncio
    import time

    from mcp_stealth_chrome import _app, server

    ended = []

    async def end(reason=None):
        ended.append(reason)
        return True

    monkeypatch.setattr(server, "_end_current_instance", end)
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    monkeypatch.setattr(server.BrowserState, "instances", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_idle_timeout", 2, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_last_active", time.time() - 10, raising=False)
    key = server.BrowserState.session_key()
    monkeypatch.setitem(_app._CALLS_IN_FLIGHT, key, 1)
    asyncio.run(server._sweep_idle_instances())
    assert ended == []
    _app._CALLS_IN_FLIGHT.pop(key)
    asyncio.run(server._sweep_idle_instances())
    assert ended == ["idle timeout"]
    assert "idle_timeout_seconds=2" in server.BrowserState.window_closed[
        server.BrowserState.current_instance_id]


def test_the_idle_sweep_skips_an_instance_switched_in_while_it_closed_another(monkeypatch):
    """Between the sweep's awaits a switch_instance can make a stored instance current; closing
    it by its old place ended the browser the caller had just switched to."""
    import asyncio

    from types import SimpleNamespace

    from mcp_stealth_chrome import server

    def snap():
        return SimpleNamespace(browser=object(), idle_timeout=1, is_running=lambda: True,
                               is_idle_expired=lambda: True)

    stored = {"a": snap(), "b": snap()}
    closed = []

    async def end(iid, s, reason=None):
        closed.append(iid)
        stored.pop("b", None)  # switch_instance("b") while "a" was being closed
        return True

    monkeypatch.setattr(server, "_end_stored_instance", end)
    monkeypatch.setattr(server.BrowserState, "instances", stored, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: False))
    asyncio.run(server._sweep_idle_instances())
    assert closed == ["a"]


def test_switch_instance_waits_while_a_sweep_closes_that_instance(monkeypatch):
    """Switched in mid-close, the caller got a browser that was going, and the close then reset
    whichever instance was current by the time it finished."""
    import asyncio

    from mcp_stealth_chrome import _app, server

    switched = []
    monkeypatch.setattr(server.BrowserState, "switch_to",
                        classmethod(lambda cls, iid: switched.append(iid)))
    monkeypatch.setattr(server.BrowserState, "window_closed",
                        {"gone": "instance 'gone' was closed (idle_timeout_seconds=2)."}, raising=False)

    async def run():
        event = asyncio.Event()
        monkeypatch.setitem(_app.INSTANCE_CLOSING, (server.BrowserState.session_key(), "gone"), event)
        task = asyncio.ensure_future(server.switch_instance("gone"))
        await asyncio.sleep(0.2)
        assert switched == [] and not task.done()
        event.set()
        return await asyncio.wait_for(task, 2)

    reply = asyncio.run(run())
    assert switched == ["gone"]
    assert "not running" in reply and "idle_timeout_seconds=2" in reply, reply


def test_the_idle_sweep_leaves_alone_an_instance_close_instance_is_closing(monkeypatch):
    """close_instance awaited (window bounds, tab record, Browser.close) without saying it was
    closing, so the idle sweep could pick the same stored instance and close it a second time."""
    import asyncio

    from mcp_stealth_chrome import server

    snap = SimpleNamespace(browser=object(), tabs=[], active_tab_index=0, profile_dir=None,
                           privacy=False, idle_timeout=1, is_running=lambda: True,
                           is_idle_expired=lambda: True)
    swept = []

    async def end(iid, s, reason=None):
        swept.append(iid)
        return True

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(server, "_end_stored_instance", end)
    monkeypatch.setattr(server, "_save_window_bounds", noop)
    monkeypatch.setattr(server, "_record_tabs_before_close", noop)
    monkeypatch.setattr(server, "_clear_tab_scoped_state", lambda tabs: None)
    monkeypatch.setattr(server.BrowserState, "instances", {"a": snap}, raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed", {}, raising=False)
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: False))

    async def run():
        release = asyncio.Event()

        async def slow_dead(_browser, _profile):
            await release.wait()
            return {"verified": True}

        monkeypatch.setattr(server, "_ensure_browser_dead", slow_dead)
        closing = asyncio.ensure_future(server.close_instance("a"))
        await asyncio.sleep(0.05)
        await server._sweep_idle_instances()
        release.set()
        return await asyncio.wait_for(closing, 2)

    reply = asyncio.run(run())
    assert swept == [], "closed twice"
    assert "closed instance 'a'" in reply or "closed" in reply, reply


def test_a_close_still_running_after_the_wait_is_an_error_not_a_go_ahead(monkeypatch):
    """wait_while_closing gave up after 60 s and carried on, so the caller switched in, or
    used, an instance whose close then changed the state under it."""
    import asyncio

    from mcp_stealth_chrome import _app, server

    switched = []
    monkeypatch.setattr(_app, "_CLOSING_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(server.BrowserState, "switch_to",
                        classmethod(lambda cls, iid: switched.append(iid)))

    async def run():
        event = asyncio.Event()  # never set: the close is stuck
        key = server.BrowserState.session_key()
        monkeypatch.setitem(_app.INSTANCE_CLOSING, (key, "stuck"), event)
        monkeypatch.setitem(_app.INSTANCE_CLOSING, (key, server.BrowserState.current_instance_id), event)
        return (await server.switch_instance("stuck"), await server.close_instance("stuck"),
                await server.mcp.call_tool("tab_list", {}))

    switch_reply, close_reply, listed = asyncio.run(run())
    assert switched == []
    for reply in (switch_reply, close_reply):
        assert reply.startswith("Error") and "still being closed" in reply, reply
    assert "still being closed" in str(listed), listed


def test_tab_select_names_why_the_browser_went_rather_than_the_index(monkeypatch):
    import asyncio

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import navigation

    iid = server.BrowserState.current_instance_id
    monkeypatch.setattr(server.BrowserState, "browser", None, raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [], raising=False)
    monkeypatch.setattr(server.BrowserState, "window_closed",
                        {iid: f"instance {iid!r} was closed (idle_timeout_seconds=2)."}, raising=False)
    for reply in (asyncio.run(navigation.tab_select(0)), asyncio.run(navigation.tab_close(0)),
                  asyncio.run(navigation.tab_list())):
        assert reply.startswith("Error") and "idle_timeout_seconds=2" in reply, reply
        assert "out of range" not in reply, reply


def test_a_background_target_refresh_of_a_gone_browser_ends_quietly():
    """nodriver starts update_targets() as tasks nobody awaits. Each one failing on a closed
    browser was logged as "Task exception was never retrieved" with a Rich traceback, which
    blocked the event loop 1.1 to 1.3 s per target on Windows; the idle reaper read that as
    idleness and closed an instance spawned a moment before."""
    import asyncio

    from nodriver.core import browser as nd_browser
    from nodriver.core import tab as nd_tab

    from mcp_stealth_chrome import patches

    patches.apply_all()
    assert patches.status()["healthy"], patches.status()

    class Browser:
        async def update_targets(self):
            raise ConnectionRefusedError(61, "Connect call failed")

    async def run():
        unretrieved = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unretrieved.append(ctx))
        tasks = [module.asyncio.create_task(Browser().update_targets())
                 for module in (nd_browser, nd_tab)]
        await asyncio.wait(tasks)
        assert [t.result() for t in tasks] == [None, None]
        with pytest.raises(ConnectionRefusedError):  # awaited, as _refresh_tabs does, it still says so
            await Browser().update_targets()
        del tasks
        import gc
        gc.collect()
        return unretrieved

    assert asyncio.run(run()) == []


def test_process_command_on_windows_reads_the_process_table_not_ps(monkeypatch):
    """Windows has no ps. The runner's (Git Bash) took 1.1 s up to the 3 s timeout, on the event
    loop, at every close, and on a person's machine nothing was checked."""
    from mcp_stealth_chrome import server, state

    def no_subprocess(*args, **kwargs):
        raise AssertionError(f"ran {args[0]!r}")

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server.subprocess, "run", no_subprocess)
    monkeypatch.setattr(state, "process_table", lambda: [
        (4, 0, ""), (77, 4, r'"C:\chrome.exe" --user-data-dir=C:\p'), (78, 77, "")])
    assert server._process_command(77) == r'"C:\chrome.exe" --user-data-dir=C:\p'
    assert server._process_command(78) is None  # unreadable is not evidence
    assert server._process_command(99) is None
