"""Refactor safety net — verify all tools stay registered after splits.

Run after every refactor commit:
    uv run python tests/test_registry.py

Baseline captured at v0.4.0 (pre-refactor): 133 tools.
v0.4.3 bumped to 134 (added tab_focus; also restored cookie_set's
@mcp.tool decorator that was lost during the v0.2.11 cookie_import work
and removed an erroneous decorator from the _active_tab_host helper —
both surfaced by the registry gate).
"""
import sys

EXPECTED_COUNT = 135
EXPECTED_TOOLS = {
    # Lifecycle
    "browser_launch", "browser_close", "browser_recover",
    # Navigation
    "navigate", "go_back", "go_forward", "reload",
    # DOM / content
    "browser_snapshot", "screenshot", "get_text", "get_html", "get_url",
    "save_pdf",
    # Interaction
    "click", "click_text", "click_role", "hover", "fill", "type_text",
    "press_key", "select_option", "check", "uncheck", "upload_file",
    "mouse_click_xy", "mouse_move", "drag_and_drop",
    # Wait
    "wait_for", "wait_for_navigation", "wait_for_url", "wait_for_response",
    "wait_for_request",
    # Tabs
    "tab_list", "tab_new", "tab_select", "tab_close",
    # Storage
    "cookie_list", "cookie_set", "cookie_delete", "cookie_import", "cookie_export",
    "localstorage_get", "localstorage_set", "localstorage_clear",
    "sessionstorage_get", "sessionstorage_set", "sessionstorage_clear",
    "cache_clear", "indexeddb_list", "indexeddb_delete",
    "storage_state_save", "storage_state_load",
    "storage_snapshot", "storage_diff",
    # Scripting / inspect
    "evaluate", "inject_init_script", "inspect_element", "get_attribute",
    "query_selector_all", "get_links", "list_frames", "frame_evaluate",
    "batch_actions", "fill_form", "navigate_and_snapshot",
    # Viewport
    "get_viewport_size", "set_viewport_size", "scroll", "scroll_to",
    # Dialog
    "dialog_handle", "dialog_auto_handle",
    # A11y / debug
    "accessibility_snapshot",
    "console_start", "console_get", "console_clear",
    "network_start", "network_get",
    "server_status", "get_page_errors", "export_har",
    # Extraction
    "detect_content_pattern", "extract_structured", "extract_table", "scrape_page",
    # Stealth / captcha
    "solve_captcha", "verify_cf", "fingerprint_rotate",
    "humanize_click", "humanize_type",
    "click_turnstile", "click_element_offset", "click_at_corner",
    "find_by_image", "click_at_image",
    "mouse_drift", "mouse_record", "mouse_replay",
    "solve_recaptcha_ai",
    # HTTP / behavioral
    "http_request", "http_session_cookies", "session_warmup",
    "detect_anti_bot", "http_request_with_session",
    # Multi-instance
    "spawn_browser", "list_instances", "switch_instance",
    "close_instance", "close_all_instances",
    # Chrome profile integration
    "list_chrome_profiles", "clone_chrome_profile",
    # DevTools / testing
    "performance_trace_start", "performance_trace_stop",
    "performance_metrics", "performance_timeline", "web_vitals",
    "emulate_network", "emulate_cpu", "emulate_device",
    "coverage_start", "coverage_stop",
    "memory_heap_snapshot", "wait_for_network_idle",
    # LLM-optimized kit
    "describe_page", "smart_fill",
    "assert_text_present", "assert_url_matches", "assert_element_visible",
    "vision_locate",
    "workflow_run", "detect_and_bypass",
    # 0.4.0 additions
    "paste_text", "auth_capture", "click_and_wait", "form_introspect",
    # 0.4.3 — focus + cookie_set decorator restoration
    "tab_focus",
}


def test_tool_count():
    from mcp_stealth_chrome.server import mcp
    tools = mcp._tool_manager._tools
    actual = len(tools)
    print(f"  registered tool count: {actual}  (expected {EXPECTED_COUNT})")
    if actual != EXPECTED_COUNT:
        names = sorted(tools.keys())
        missing = [t for t in EXPECTED_TOOLS if t not in tools]
        extra = [t for t in names if t not in EXPECTED_TOOLS]
        print(f"  MISSING ({len(missing)}): {missing}")
        print(f"  EXTRA  ({len(extra)}): {extra}")
        raise SystemExit(1)
    print("  OK — all 133 tools registered")


def test_critical_helpers():
    from mcp_stealth_chrome.server import (
        mcp, _wait, _safe_stop_browser,
        BrowserState, parse_json, ok, err,
    )
    print(f"  helpers importable: _wait, _safe_stop_browser, BrowserState, parse_json, ok, err")


def test_critical_tools_callable():
    """Each tool must be a FastMCP-wrapped callable with .fn attribute."""
    from mcp_stealth_chrome.server import mcp
    tools = mcp._tool_manager._tools
    sample = ["browser_launch", "navigate", "describe_page", "smart_fill",
              "solve_recaptcha_ai", "http_request_with_session", "paste_text"]
    for name in sample:
        if name not in tools:
            raise SystemExit(f"  MISSING: {name}")
        t = tools[name]
        # FastMCP Tool object has .fn attribute pointing to the underlying coroutine
        if not (hasattr(t, "fn") or callable(t)):
            raise SystemExit(f"  NOT CALLABLE: {name}")
    print(f"  {len(sample)} sample tools have callable .fn")


if __name__ == "__main__":
    print("test_tool_count:")
    test_tool_count()
    print("test_critical_helpers:")
    test_critical_helpers()
    print("test_critical_tools_callable:")
    test_critical_tools_callable()
    print("\nALL PASS")
