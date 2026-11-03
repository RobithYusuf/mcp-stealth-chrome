"""Tools that shipped broken for a long time because nothing ran them against Chrome.

select_option and drag_and_drop failed on every call while tests only checked their names.
Local fixtures and a throwaway profile only. Needs MCP_E2E=1.
"""
import asyncio
import functools
import http.server
import json
import os
import pathlib
import stat
import threading

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SECRETS = ("COOKIESECRET", "AUTHSECRET", "BODYSECRET", "QUERYSECRET", "SERVERSECRET", "RESPONSESECRET")


class _Api(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"ok": True, "token": "RESPONSESECRET"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "server=SERVERSECRET; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def base_url():
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Api, directory=str(FIXTURES)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    try:
        yield lp
    finally:
        lp.run_until_complete(asyncio.sleep(0.4))
        pending = [t for t in asyncio.all_tasks(lp) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            lp.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        lp.close()


def run(lp, name, *args, **kwargs):
    from mcp_stealth_chrome.server import mcp

    return lp.run_until_complete(mcp._tool_manager._tools[name].fn(*args, **kwargs))


def js(lp, expression):
    return run(lp, "evaluate", expression)


@pytest.fixture(scope="module")
def browser(loop):
    out = run(loop, "browser_launch", headless=True, persistent=False, url="about:blank")
    assert not out.startswith("Error"), out
    yield
    run(loop, "browser_close")


@pytest.fixture()
def page(loop, browser, base_url):
    out = run(loop, "navigate", f"{base_url}/old_tools.html", auto_verify=False, focus=False)
    assert out.startswith("Navigated"), out


def centre(lp, selector):
    box = json.loads(js(lp, f"(() => {{ const r = document.querySelector({selector!r})"
                            f".getBoundingClientRect(); return JSON.stringify("
                            f"[Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]); }})()"))
    return box[0], box[1]


# ── select_option ─────────────────────────────────────────────────────────


def test_select_option_by_value_and_by_label(loop, page):
    out = run(loop, "select_option", selector="#pick", value="b")
    assert out.startswith("selected") and "isTrusted=false" in out, out
    assert js(loop, "pick.value") == "b"
    assert js(loop, "log.textContent") == "chosen b"

    out = run(loop, "select_option", selector="#pick", label="Satu")
    assert out.startswith("selected"), out
    assert js(loop, "pick.value") == "a"
    # Whitespace in the markup is not part of the label a person reads.
    assert run(loop, "select_option", selector="#pick", label="Dua Kata").startswith("selected")
    assert js(loop, "String(window.changes)") == "3"


def test_select_option_says_why_it_could_not(loop, page):
    out = run(loop, "select_option", selector="#pick", label="Tidak Ada")
    assert out.startswith("Error") and "Satu" in out, out
    assert "disabled" in run(loop, "select_option", selector="#pick", value="c")
    assert "disabled" in run(loop, "select_option", selector="#off", value="p")
    assert "not a <select>" in run(loop, "select_option", selector="#log", value="p")
    assert run(loop, "select_option", selector="#pick").startswith("Error")
    assert js(loop, "pick.value") == "a"


def test_select_option_reports_a_page_that_changes_it_back(loop, page):
    out = run(loop, "select_option", selector="#stubborn", value="y")
    assert out.startswith("Error") and "changed it back" in out, out


# ── drag_and_drop ─────────────────────────────────────────────────────────


def test_drag_moves_a_mouse_driven_element(loop, page):
    x, y = centre(loop, "#handle")
    out = run(loop, "drag_and_drop", x, y, x + 150, y)
    assert not out.startswith("Error"), out
    assert "NO page change" not in out, out
    assert abs(float(js(loop, "parseFloat(handle.style.left)")) - (x + 150)) < 3
    assert js(loop, "String(window.released)") == "1"


def test_drag_reaches_an_html5_drop_target(loop, page):
    x, y = centre(loop, "#dragme")
    tx, ty = centre(loop, "#dropzone")
    out = run(loop, "drag_and_drop", x, y, tx, ty)
    assert not out.startswith("Error"), out
    assert js(loop, "dropzone.textContent") == "dropped", out


def test_drag_says_when_nothing_happened(loop, page):
    out = run(loop, "drag_and_drop", 700, 500, 720, 500)
    assert "NO page change" in out, out


# ── evaluate ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("expression,text", [
    ("throw new Error('sengaja')", "sengaja"),
    ("noSuchVariable", "noSuchVariable"),
    ("Promise.reject(new Error('ditolak'))", "ditolak"),
])
def test_evaluate_reports_a_throw_as_an_error(loop, page, expression, text):
    out = js(loop, expression)
    assert out.startswith("Error") and text in out, out
    assert "ExceptionDetails" not in out, out


@pytest.mark.parametrize("expression,expected", [
    ("1 + 1", "2"), ("'teks'", "teks"), ("null", "null"), ("new Error('x').message", "x"),
])
def test_evaluate_still_returns_values(loop, page, expression, expected):
    assert js(loop, expression) == expected


# ── clicks and new tabs ───────────────────────────────────────────────────


def test_click_that_opens_a_tab_says_so(loop, page):
    out = run(loop, "click", selector="#popup")
    assert "new tab" in out and "keys_result.html" in out, out
    assert "NO page change" not in out, out
    status = json.loads(run(loop, "server_status"))
    assert status["tabs"] == 2, status  # known without tab_list having run first
    run(loop, "tab_close", index=1)


def test_dead_button_still_reads_as_dead(loop, page):
    out = run(loop, "click", selector="#dead")
    assert "NO page change" in out and "new tab" not in out, out


# ── fill ──────────────────────────────────────────────────────────────────


def test_fill_keeps_newlines_in_a_textarea(loop, page):
    out = run(loop, "fill", selector="#ta", value="baris1\nbaris2\n\nbaris4")
    assert out == "filled", out
    assert js(loop, "JSON.stringify(ta.value)") == json.dumps("baris1\nbaris2\n\nbaris4")


def test_fill_multiline_contenteditable_and_no_submit(loop, page):
    assert run(loop, "fill", selector="#ce", value="a\nb") == "filled"
    assert js(loop, "String(window.submitted)") == "undefined"


def test_fill_refuses_newlines_a_single_line_field_cannot_hold(loop, page):
    out = run(loop, "fill", selector="#one", value="a\nb")
    assert out.startswith("Error"), out
    assert js(loop, "String(window.submitted)") == "undefined"


def test_fill_form_failure_is_a_failure(loop, page):
    out = run(loop, "fill_form", fields=[
        {"selector": "#one", "value": "ok"}, {"selector": "#missing", "value": "x"},
    ])
    assert out.startswith("Error") and "1 of 2" in out, out
    assert "#missing" in out
    batch = run(loop, "batch_actions", actions=[
        {"type": "fill_form", "fields": [{"selector": "#missing", "value": "x"}]},
    ])
    assert "1 failed" in batch, batch


def test_fill_form_does_not_submit_after_a_failed_field(loop, page):
    snapshot = run(loop, "browser_snapshot")
    ref = next(line.split("[")[1].split("]")[0].split("=")[-1]
               for line in snapshot.splitlines() if "Send" in line and "[" in line)
    out = run(loop, "fill_form", submit_ref=ref, fields=[{"selector": "#missing", "value": "x"}])
    assert out.startswith("Error") and "not clicked" in out, out
    assert js(loop, "String(window.submitted)") == "undefined"


# ── credentials in tool output ────────────────────────────────────────────


def _call_api(lp):
    run(lp, "click", selector="#call")
    run(lp, "wait_for", text="api done", timeout=5)


def _no_secrets(out):
    leaked = [s for s in SECRETS if s in out]
    assert not leaked, f"{leaked} in {out[:900]}"


def test_network_get_hides_credentials_unless_asked(loop, page):
    run(loop, "network_start")
    _call_api(loop)
    out = run(loop, "network_get", filter_url="api/login", full=True, include_body=True)
    _no_secrets(out)
    entry = json.loads(out)[-1]
    assert entry["request_headers"]["Authorization"].startswith("***")
    assert entry["request_headers"]["X-Plain"] == "visible"
    assert json.loads(entry["request_body"])["user"] == "budi"
    assert "page=2" in entry["url"]
    flat = run(loop, "network_get", filter_url="api/login")
    _no_secrets(flat)
    shown = run(loop, "network_get", filter_url="api/login", full=True, reveal_values=True)
    assert "AUTHSECRET" in shown and "BODYSECRET" in shown and "QUERYSECRET" in shown


def test_export_curl_hides_credentials_and_still_replays(loop, page):
    run(loop, "network_start")
    _call_api(loop)
    out = run(loop, "export_curl", url_contains="api/login")
    _no_secrets(out)
    payload = json.loads(out)
    assert payload["cookies_attached"] is True and "Cookie" in payload["values_hidden"]
    assert "use_browser_cookies=True" in payload["http_request"]
    assert "COOKIESECRET" in run(loop, "export_curl", url_contains="api/login", reveal_values=True)
    assert "use_browser_cookies=False" in run(
        loop, "export_curl", url_contains="api/login", include_cookies=False)


def test_intercept_log_hides_credentials_unless_asked(loop, page):
    assert "active" in run(loop, "intercept_start", url_pattern="*api/login*", action="mock",
                           mock_body='{"ok":true}', auto_disable_after=30)
    try:
        _call_api(loop)
        out = run(loop, "intercept_log")
        _no_secrets(out)
        assert '"user": "budi"' in json.loads(out)["requests"][-1]["body"]
        assert "AUTHSECRET" in run(loop, "intercept_log", reveal_values=True)
    finally:
        run(loop, "intercept_stop")


def test_request_watchers_hide_credentials_unless_asked(loop, page):
    async def watch(name, **kwargs):
        from mcp_stealth_chrome.server import mcp

        waiting = asyncio.ensure_future(mcp._tool_manager._tools[name].fn(**kwargs))
        await asyncio.sleep(0.3)
        await mcp._tool_manager._tools["click"].fn(selector="#call")
        return await waiting

    out = loop.run_until_complete(watch("wait_for_request", url_pattern="api/login"))
    _no_secrets(out)
    out = loop.run_until_complete(watch("auth_capture", filter_url_pattern="api/login"))
    _no_secrets(out)
    out = loop.run_until_complete(watch("wait_for_request", url_pattern="api/login", reveal_values=True))
    assert "AUTHSECRET" in out


def test_http_request_hides_a_set_cookie(loop, page, base_url):
    out = run(loop, "http_request", url=f"{base_url}/api/login", method="POST", data="{}",
              return_mode="meta")
    assert "SERVERSECRET" not in out, out


def test_smart_fill_never_echoes_a_password(loop, page):
    out = run(loop, "smart_fill", fields={"Password": "PWSECRET"})
    assert "PWS" not in out, out
    assert "chars" in out, out
    # A field that holds nothing secret still shows what it read.
    assert "'abc'" in run(loop, "smart_fill", fields={"Nickname": "abcdef"})


# ── reports about the browser ─────────────────────────────────────────────


def test_status_names_the_ephemeral_profile(loop, browser):
    from mcp_stealth_chrome.state import PROFILE_DIR

    status = json.loads(run(loop, "server_status"))
    assert status["profile_dir"] != str(PROFILE_DIR)
    assert status["profile_dir"].startswith("(ephemeral)")


def test_viewport_is_read_back(loop, browser):
    out = run(loop, "set_viewport_size", 1000, 700)
    assert out.startswith("window set to 1000x700") and "viewport reads" in out, out


def test_export_har_is_a_har_and_private(loop, page):
    from mcp_stealth_chrome.state import EXPORT_DIR

    run(loop, "network_start")
    _call_api(loop)
    name = f"e2e-old-tools-{os.getpid()}.har"
    out = run(loop, "export_har", filename=name)
    path = EXPORT_DIR / name
    try:
        assert not out.startswith("Error"), out
        _no_secrets(out)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        entries = json.loads(path.read_text())["log"]["entries"]
        entry = next(e for e in entries if "api/login" in e["request"]["url"])
        assert entry["request"]["method"] == "POST" and entry["response"]["status"] == 200
        assert {"name": "X-Plain", "value": "visible"} in entry["request"]["headers"]
        assert entry["request"]["postData"]["text"]
        assert entry["startedDateTime"].startswith("20")
        assert {"name": "page", "value": "2"} in entry["request"]["queryString"]
    finally:
        path.unlink(missing_ok=True)


# ── the idle reaper and a stdio browser ───────────────────────────────────


def test_idle_sweep_leaves_the_stdio_browser_alone(loop, browser):
    """spawn_browser starts the reaper, which closed the browser someone was looking at."""
    from mcp_stealth_chrome.server import BrowserState, _sweep_idle_instances

    assert BrowserState.current_idle_timeout == 0
    BrowserState.current_last_active = 1.0
    loop.run_until_complete(_sweep_idle_instances())
    assert BrowserState.is_up()
    assert json.loads(run(loop, "server_status"))["browser_running"] is True


def test_idle_sweep_leaves_it_alone_while_another_instance_is_current(loop, browser):
    from mcp_stealth_chrome.server import BrowserState, _sweep_idle_instances

    out = run(loop, "spawn_browser", instance_id="idle-other", persistent=False, headless=True)
    assert not out.startswith("Error"), out
    try:
        run(loop, "switch_instance", "idle-other")
        BrowserState.instances["main"].last_active = 1.0
        loop.run_until_complete(_sweep_idle_instances())
        assert BrowserState.instances["main"].is_running()
    finally:
        run(loop, "close_instance", "idle-other")
        run(loop, "switch_instance", "main")
    assert BrowserState.is_up()


# ── from the independent verification ─────────────────────────────────────


def test_smart_fill_does_not_exceed_maxlength(loop, page):
    out = run(loop, "smart_fill", fields={"Shortcode": "abcdef"})
    assert "'abc'" in out and '"ok": false' in out, out
    assert js(loop, "short.value") == "abc"


@pytest.mark.parametrize("expression,word", [
    ("Promise.reject(new Error('frame ditolak'))", "frame ditolak"),
    ("throw new Error('frame lempar')", "frame lempar"),
])
def test_frame_evaluate_reports_a_failure(loop, page, expression, word):
    out = run(loop, "frame_evaluate", "keys_result", expression)
    assert out.startswith("Error") and word in out, out


def test_frame_evaluate_awaits_and_refuses_a_bad_pattern(loop, page):
    assert run(loop, "frame_evaluate", "keys_result", "Promise.resolve(7)") == "7"
    assert run(loop, "frame_evaluate", "keys_result", "1 + 1") == "2"
    out = run(loop, "frame_evaluate", "(?<!a)[\\p{L}]", "1")
    assert "ExceptionDetails" not in out, out


def test_wait_for_function_names_the_throw(loop, page):
    out = run(loop, "wait_for_function", "noSuchVariable.x", timeout_ms=2000)
    assert out.startswith("Error") and "noSuchVariable" in out and "unserializable" not in out, out


def test_list_instances_counts_real_tabs(loop, page):
    run(loop, "click", selector="#popup")
    main = next(i for i in json.loads(run(loop, "list_instances"))["instances"] if i["current"])
    assert main["tabs"] == 2, main
    run(loop, "tab_close", index=1)


def test_resize_leaves_the_window_where_it_was(loop, browser):
    from mcp_stealth_chrome.server import BrowserState

    tab = BrowserState.active_tab()

    async def place():
        await tab.set_window_size(left=120, top=90, width=900, height=650)
        await asyncio.sleep(0.2)
        await tab_size()
        _window, bounds = await tab.get_window()
        return bounds.left, bounds.top

    async def tab_size():
        from mcp_stealth_chrome.server import mcp

        out = await mcp._tool_manager._tools["set_viewport_size"].fn(1000, 700)
        assert out.startswith("window set"), out

    assert loop.run_until_complete(place()) == (120, 90)

