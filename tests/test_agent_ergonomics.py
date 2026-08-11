"""Fast unit coverage for the agent-facing snapshot and identity helpers."""

import json

import pytest

from mcp_stealth_chrome.snapshot import element_fingerprint, format_snapshot


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

    class _Tab:
        async def evaluate(self, *_args, **_kwargs):
            return "not-json"

    monkeypatch.setattr(
        server.BrowserState,
        "active_tab",
        classmethod(lambda cls: _Tab()),
    )
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

    monkeypatch.setattr(server, "_SNAPSHOT_CACHE", {
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

    assert set(server._SNAPSHOT_CACHE) == {"TARGET-B"}
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
    assert state.private_network_request_policy is not None


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
        out = json.loads(asyncio.run(export(request_id="1")))
    finally:
        server.BrowserState.network_index = previous

    assert tab.command["method"] == "Network.getCookies"
    assert tab.command["params"]["urls"] == ["https://notexample.com/account"]
    assert out["cookie_status"] == "live_url_jar"
    assert "Cookie: sid=secret" in out["curl"]


def test_cookie_domain_filter_never_sends_child_cookie_to_parent():
    from mcp_stealth_chrome.server import _cookie_domain_match

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
