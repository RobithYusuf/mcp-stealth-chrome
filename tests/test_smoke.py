"""Smoke test — no-browser checks of pure parsers and helpers.

Run after every refactor commit:
    uv run python tests/test_smoke.py

Browser-required smoke (manual):
- launch nopecha.com/demo/turnstile
- describe_page → fields/actions present
- screenshot → file saved
- browser_close
"""
import json
import sys


def test_cookie_parser():
    from mcp_stealth_chrome.server import _parse_cookie_text
    # JSON array
    r = _parse_cookie_text('[{"name":"a","value":"1"},{"name":"b","value":"2"}]', None)
    assert len(r) == 2 and r[0]["name"] == "a", f"JSON array parse failed: {r}"
    # storage_state
    r = _parse_cookie_text('{"cookies":[{"name":"x","value":"y"}]}', None)
    assert len(r) == 1 and r[0]["value"] == "y", f"storage_state parse failed: {r}"
    # Header
    r = _parse_cookie_text('auth=tok; user=jane', '.example.com')
    assert len(r) == 2 and r[0]["domain"] == ".example.com", f"header parse failed: {r}"
    # With Cookie: prefix
    r = _parse_cookie_text('Cookie: a=1; b=2', '.foo.com')
    assert len(r) == 2 and r[0]["domain"] == ".foo.com", f"prefix strip failed: {r}"
    # Netscape (tab-separated)
    ns = ".example.com\tTRUE\t/\tFALSE\t1735689600\tsessionid\tabc"
    r = _parse_cookie_text(ns, None)
    assert len(r) == 1 and r[0]["expires"] == 1735689600, f"netscape parse failed: {r}"
    print("  cookie parser: 5 formats OK")


def test_helpers_module_state():
    from mcp_stealth_chrome.server import (
        BrowserState, _DIALOG_AUTO_CFG, _STORAGE_SNAPSHOTS,
        _LAUNCH_LOCK, BROWSER_LAUNCH_TIMEOUT, BROWSER_NAV_TIMEOUT,
        TOOL_ACTION_TIMEOUT,
    )
    assert isinstance(_DIALOG_AUTO_CFG, dict)
    assert isinstance(_STORAGE_SNAPSHOTS, dict)
    assert BROWSER_LAUNCH_TIMEOUT > 0
    assert BROWSER_NAV_TIMEOUT > 0
    assert TOOL_ACTION_TIMEOUT > 0
    print(f"  module state OK (launch_to={BROWSER_LAUNCH_TIMEOUT}s, nav_to={BROWSER_NAV_TIMEOUT}s, action_to={TOOL_ACTION_TIMEOUT}s)")


def test_workflow_dispatch_table():
    """workflow_run uses globals() lookup. After refactor it must still
    find the moved tools."""
    from mcp_stealth_chrome.server import _register_workflow_tools, _WORKFLOW_TOOLS
    _register_workflow_tools()
    expected_names = {
        "navigate", "click_text", "fill", "smart_fill", "vision_locate",
        "assert_text_present", "storage_snapshot", "evaluate",
    }
    missing = expected_names - set(_WORKFLOW_TOOLS.keys())
    assert not missing, f"workflow_run lost dispatch entries: {missing}"
    print(f"  workflow dispatch: {len(_WORKFLOW_TOOLS)} tools registered")


def test_vision_provider_resolve():
    """Vision provider helper used by solve_recaptcha_ai + vision_locate."""
    from mcp_stealth_chrome.server import _resolve_vision_provider
    # Pass explicit args so we don't depend on env
    p, b, k, m = _resolve_vision_provider(
        provider="openai", base_url="https://x.example.com/v1",
        api_key="test", model="gpt-x",
    )
    assert p == "openai" and b == "https://x.example.com/v1" and k == "test" and m == "gpt-x"
    print(f"  vision provider resolve OK")


def test_snapshot_js_intact():
    """The big JS strings used by browser_snapshot / describe_page / etc.
    Must be non-empty after move."""
    from mcp_stealth_chrome.server import (
        _DESCRIBE_PAGE_JS, _SMART_FILL_FIND_JS, _TURNSTILE_FIND_JS,
        _CF_CHALLENGE_PROBE_INITIAL_JS, _CF_CHALLENGE_PROBE_ACTIVE_JS,
        _WAIT_DOM_STABLE_JS, _FORM_INTROSPECT_JS, _VISION_LOCATE_PROMPT,
        _PROMPT_TEMPLATE,
    )
    for name, js in [
        ("_DESCRIBE_PAGE_JS", _DESCRIBE_PAGE_JS),
        ("_SMART_FILL_FIND_JS", _SMART_FILL_FIND_JS),
        ("_TURNSTILE_FIND_JS", _TURNSTILE_FIND_JS),
        ("_CF_CHALLENGE_PROBE_INITIAL_JS", _CF_CHALLENGE_PROBE_INITIAL_JS),
        ("_CF_CHALLENGE_PROBE_ACTIVE_JS", _CF_CHALLENGE_PROBE_ACTIVE_JS),
        ("_WAIT_DOM_STABLE_JS", _WAIT_DOM_STABLE_JS),
        ("_FORM_INTROSPECT_JS", _FORM_INTROSPECT_JS),
        ("_VISION_LOCATE_PROMPT", _VISION_LOCATE_PROMPT),
        ("_PROMPT_TEMPLATE", _PROMPT_TEMPLATE),
    ]:
        assert isinstance(js, str) and len(js) > 50, f"{name} too short or wrong type"
    print(f"  9 JS templates intact")


if __name__ == "__main__":
    for fn in (test_cookie_parser, test_helpers_module_state,
                test_workflow_dispatch_table, test_vision_provider_resolve,
                test_snapshot_js_intact):
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            sys.exit(1)
    print("\nALL PASS")
