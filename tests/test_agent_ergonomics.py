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


def test_privacy_proxy_supports_pool_and_session_token():
    from mcp_stealth_chrome.server import _privacy_proxy

    rotated = _privacy_proxy("http://user-{session}@a:1|http://user-{session}@b:2")
    assert rotated is not None
    assert "{session}" not in rotated
    assert rotated.startswith(("http://user-"))
    assert rotated.endswith(("@a:1", "@b:2"))
