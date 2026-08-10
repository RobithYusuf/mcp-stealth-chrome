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


def test_privacy_proxy_supports_pool_and_session_token():
    from mcp_stealth_chrome.server import _privacy_proxy

    rotated = _privacy_proxy("http://user-{session}@a:1|http://user-{session}@b:2")
    assert rotated is not None
    assert "{session}" not in rotated
    assert rotated.startswith(("http://user-"))
    assert rotated.endswith(("@a:1", "@b:2"))
