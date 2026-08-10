"""Fast unit coverage for the agent-facing snapshot and identity helpers."""

from mcp_stealth_chrome.snapshot import element_fingerprint, format_snapshot


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


def test_privacy_proxy_supports_pool_and_session_token():
    from mcp_stealth_chrome.server import _privacy_proxy

    rotated = _privacy_proxy("http://user-{session}@a:1|http://user-{session}@b:2")
    assert rotated is not None
    assert "{session}" not in rotated
    assert rotated.startswith(("http://user-"))
    assert rotated.endswith(("@a:1", "@b:2"))
