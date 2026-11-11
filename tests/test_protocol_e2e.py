"""Every tool below failed or lied over real MCP against real Chrome, with fakes staying green.

Needs MCP_E2E=1. Local fixture only, an ephemeral profile, headless. One test per defect, so
reverting a fix fails exactly the test that names it.
"""
import functools
import http.server
import json
import os
import pathlib
import struct
import threading
import time
import zlib

import pytest

from _protocol import SAFE_ARGS, Client

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PAGES = {
    "/page2.html": "<!doctype html><title>Page Two</title><h1>Second</h1><button id=b2>Btn2</button>",
    "/tall.html": "<!doctype html><title>Short</title><p>not scrollable</p>",
    "/slowimg.html": '<!doctype html><title>Slow</title><h1>x</h1><img src="/slow.png">',
    "/turnstile.html": ('<!doctype html><title>Just a moment...</title>'
                        '<div class="cf-turnstile" data-sitekey="x" id="ts" style="width:300px;height:65px"></div>'
                        '<input name="cf-turnstile-response" type="hidden" id="tok">'),
    "/loadalert.html": "<!doctype html><title>Boot</title><script>alert('boot')</script><p id=p>after</p>",
    "/movealert.html": ('<!doctype html><title>Move</title><button id="b" style="width:200px;height:60px" '
                        'onmousemove="if(!window.done){window.done=1;alert(\'move\')}">Hot</button>'),
    "/moment.html": ('<!doctype html><title>Just a moment...</title>'
                     '<div class="cf-turnstile" data-sitekey="x" style="width:300px;height:65px"></div>'),
    "/widget.html": ('<!doctype html><title>Login</title>'
                     '<div class="cf-turnstile" data-sitekey="x" style="width:300px;height:65px"></div>'
                     '<input name="cf-turnstile-response" type="hidden">'),
    "/solved.html": ('<!doctype html><title>Welcome</title>'
                     '<div class="cf-turnstile" data-sitekey="x" style="width:300px;height:65px" '
                     'onclick="document.getElementById(\'tok\').value=\'SYNTHTOKEN\';this.remove()"></div>'
                     '<input name="cf-turnstile-response" type="hidden" id="tok">'),
    "/latewidget.html": ('<!doctype html><title>Login</title><p>form</p><script>setTimeout(() => {'
                         'document.body.insertAdjacentHTML("beforeend", \'<div class="cf-turnstile" data-sitekey="x" '
                         'style="width:300px;height:65px"></div><input name="cf-turnstile-response" type="hidden">\');'
                         '}, 300); /* turnstile.render comes later */</script>'),
    "/late.html": ('<!doctype html><title>Late</title><p id="out">0</p>'
                   '<button id="now" onclick="out.textContent=\'now\'">Now</button>'
                   '<button id="late" onclick="setTimeout(() => out.textContent=\'late\', 300)">Late</button>'
                   '<button id="dead">Dead</button>'),
    "/watched.html": (
        '<!doctype html><title>Watched</title><script>window.__calls = [];'
        'const own = location.origin;'
        'const wrap = (proto, name) => { const orig = proto[name]; proto[name] = function() {'
        ' const stack = String(new Error().stack).split("\\n").slice(2);'
        ' if (!stack.some(line => line.includes(own))) window.__calls.push(name);'
        ' return orig.apply(this, arguments); }; };'
        'for (const name of ["querySelector", "querySelectorAll", "getElementsByTagName", "elementFromPoint"])'
        ' wrap(Document.prototype, name);'
        'for (const name of ["querySelector", "querySelectorAll", "getBoundingClientRect", "getAttribute"])'
        ' wrap(Element.prototype, name);'
        'wrap(Node.prototype, "contains"); wrap(Node.prototype, "getRootNode"); wrap(window, "getComputedStyle");'
        '</script><p id="out">0</p><button id="b" onclick="out.textContent = +out.textContent + 1">Press</button>'
        '<div style="position:relative;margin-top:30px"><button id="under">Under</button>'
        '<div id="cover" style="position:absolute;inset:-5px 0 -5px 0;width:200px"></div></div>'),
    "/labels.html": (
        '<!doctype html><title>Labels</title>'
        '<p><label for="email">Email</label> <input id="email" type="email"></p>'
        '<p><label>Phone <input id="phone" type="tel"></label></p>'
        '<p><span id="cap">Postal code</span> <input id="zip" aria-labelledby="cap"></p>'
        '<p><label>Colour <select id="colour"><option>Red</option><option>Blue</option></select></label></p>'
        '<p><input id="dark" type="checkbox" style="display:none"><label for="dark">Dark mode</label></p>'
        '<p><input id="bare"></p><p><label>Orphan text</label></p>'),
    "/idb.html": ("<!doctype html><title>IDB</title><script>const r=indexedDB.open('protodb',1);"
                  "r.onupgradeneeded=()=>r.result.createObjectStore('o');r.onsuccess=()=>r.result.close();</script>"),
}


def _png(size: int = 64) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
    rows = b"".join(
        b"\0" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 90)) for x in range(size))
        for y in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class _Fixture(http.server.SimpleHTTPRequestHandler):
    requests: list = []
    loads = 0

    def log_message(self, *_args):
        pass

    def _reply(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Fixture.requests.append({"path": self.path, "cookie": self.headers.get("Cookie")})
        path = self.path.split("?")[0]
        if path in PAGES:
            return self._reply(200, PAGES[path].encode())
        if path == "/counter.html":
            time.sleep(0.3)
            _Fixture.loads += 1
            return self._reply(200, f"<!doctype html><title>Count</title><p id=n>{_Fixture.loads}</p>".encode())
        if path == "/img.png":
            return self._reply(200, _png(), "image/png")
        if path == "/slow.png":
            time.sleep(2.0)
            return self._reply(200, b"", "image/png")
        if path == "/data.json":
            return self._reply(200, b'{"a": 1}', "application/json")
        if path == "/gone":
            return self._reply(404, b"short")
        return super().do_GET()


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Fixture, directory=str(FIXTURES)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}"
    srv.shutdown()


def launch(mcp, url, **kw):
    return mcp.call("browser_launch", _timeout=90, url=url, headless=True, persistent=False,
                    extra_args=SAFE_ARGS, **kw)


@pytest.fixture(scope="module")
def mcp(base):
    client = Client()
    reply = launch(client, f"{base}/protocol.html")
    assert not reply.failed, reply.text
    yield client
    client.call("close_all_instances", _timeout=40)
    client.close()


@pytest.fixture
def page(mcp, base):
    """A fresh copy of the main fixture, and a clean mouse state."""
    if not mcp.call("server_status").json()["browser_running"]:
        launch(mcp, f"{base}/protocol.html")
    reply = mcp.call("navigate", url=f"{base}/protocol.html")
    assert not reply.failed, reply.text
    return mcp


def events(mcp, *kinds):
    seen = json.loads(mcp.evaluate("JSON.stringify(window.__ev)"))
    mcp.evaluate("window.__ev=[];1")
    return [e for e in seen if not kinds or e["t"] in kinds]


# ── results the client refuses ─────────────────────────────────────────────

def test_image_tools_return_what_the_output_schema_promises(page):
    for name, kw in (("screenshot", {}), ("screenshot_element", {"selector": "#img"}),
                     ("inspect_image", {"selector": "#img"})):
        reply = page.call(name, **kw)  # the SDK raises on a reply the schema rejects
        assert "resource_link" in reply.kinds, f"{name}: {reply.kinds}"
        assert isinstance((reply.structured or {}).get("result"), str)


def test_inline_image_block_passes_the_client_too(page):
    reply = page.call("screenshot", output_mode="inline_image")
    assert "image" in reply.kinds


def test_scrape_page_of_a_json_document(page, base):
    page.call("navigate", url=f"{base}/data.json")
    reply = page.call("scrape_page")
    assert json.loads(reply.text)["data"] == {"a": 1}
    assert reply.structured["data"] == {"a": 1}


# ── evaluate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expression,expected", [
    ("({a:1})", {"a": 1}), ("[1,2]", [1, 2]), ("true", True), ("false", False),
    ("null", None), ("1+1", 2),
])
def test_evaluate_returns_the_value_as_json(page, expression, expected):
    assert json.loads(page.evaluate(expression)) == expected


def test_evaluate_says_undefined_and_describes_what_json_cannot(page):
    assert page.evaluate("undefined") == "undefined"
    assert page.evaluate("NaN") == "NaN"
    assert page.evaluate("'plain text'") == "plain text"
    assert "Error" not in page.evaluate("(()=>{})")


def test_batch_and_workflow_get_the_same_values(page):
    assert "[\n  1,\n  2\n]" in page.call("batch_actions", actions=[{"type": "evaluate", "expression": "[1,2]"}]).text


# ── dialogs ────────────────────────────────────────────────────────────────

def test_a_click_that_opens_an_alert_returns_and_can_be_answered(page):
    reply = page.call("click", selector="#alertbtn", _timeout=30)
    assert reply.seconds < 15, reply.seconds
    assert "alert" in reply.text and "dialog_handle" in reply.text, reply.text
    assert not reply.failed, "the click landed, so this is not the boundary stopping it"
    answered = page.call("dialog_handle", action="accept", _timeout=15)
    assert "answered" in answered.text, answered.text
    assert page.evaluate("document.getElementById('dlg').textContent") == "alert-done"


@pytest.mark.parametrize("tool,kw", [
    ("click", {"selector": "#b"}),
    ("mouse_click_xy", {"x": 50, "y": 30}),
    ("humanize_click", {"x": 50, "y": 30}),
    ("humanize_click", {"selector": "#b"}),
    ("click_element_offset", {"selector": "#b"}),
    ("click_at_corner", {"selector": "#b"}),
    ("hover", {"selector": "#b"}),
    ("mouse_move", {"x": 50, "y": 30}),
    ("drag_and_drop", {"start_x": 50, "start_y": 30, "end_x": 60, "end_y": 40}),
    ("mouse_replay", {"path_json": '[{"t":0,"x":50,"y":30}]'}),
])
def test_a_dialog_opened_by_the_pointer_moving_is_reported(mcp, base, tool, kw):
    """The move comes before the press, so the dialog can open before the click is even sent."""
    mcp.call("navigate", url=f"{base}/movealert.html")
    reply = mcp.call(tool, _timeout=25, **kw)
    # Answered before any assertion: a dialog left open fails every test after this one.
    answered = mcp.call("dialog_handle", action="accept", _timeout=10)
    assert reply.seconds < 15, reply.seconds
    assert "dialog_handle" in reply.text, reply.text
    assert f"{tool} stopped" not in reply.text, "the tool should see this itself, not be cut off"
    assert "answered" in answered.text, answered.text
    assert mcp.evaluate("1+1", timeout_ms=5000) == "2"


def test_a_tool_called_while_a_dialog_is_open_says_so_at_once(page):
    page.call("click", selector="#alertbtn", _timeout=25)
    for name, kw in (("get_text", {}), ("browser_snapshot", {}), ("get_html", {"selector": "#title"}),
                     ("query_selector_all", {"selector": "h1"}), ("wait_for", {"selector": "#title"})):
        reply = page.call(name, _timeout=10, **kw)
        assert reply.seconds < 2 and reply.failed and "dialog_handle" in reply.text, (name, reply.text)
    assert page.call("server_status").json()["browser_running"] is True
    assert "answered" in page.call("dialog_handle", action="accept", _timeout=10).text
    assert page.call("get_text", selector="#title", _timeout=10).text == "Sweep Heading"


def test_a_dialog_raised_while_loading_stops_navigate(page, base):
    reply = page.call("navigate", url=f"{base}/loadalert.html", _timeout=25)
    assert reply.seconds < 10 and reply.failed and "alert" in reply.text and "dialog_handle" in reply.text, reply.text
    assert "answered" in page.call("dialog_handle", action="accept", _timeout=10).text
    assert page.evaluate("document.readyState", timeout_ms=5000) in ("interactive", "complete")


def test_a_dialog_armed_in_advance_is_not_interrupted(page):
    page.call("dialog_handle", action="accept", text="typed")
    reply = page.call("click", selector="#promptbtn", _timeout=25)
    assert not reply.failed, reply.text
    assert page.evaluate("document.getElementById('dlg').textContent") == "prompt:typed"


def test_browser_close_works_with_a_dialog_open(base):
    client = Client()
    try:
        launch(client, f"{base}/protocol.html")
        client.call("click", selector="#alertbtn", _timeout=25)
        assert client.call("browser_close", _timeout=30).text.startswith("Browser closed")
    finally:
        client.close()


def test_auto_handle_answers_a_dialog_already_open(page):
    page.call("click", selector="#alertbtn", _timeout=25)
    assert not page.call("dialog_auto_handle", action="accept", _timeout=10).failed
    assert page.evaluate("document.getElementById('dlg').textContent", timeout_ms=5000) == "alert-done"
    page.call("dialog_auto_handle", enabled=False)


def test_a_key_that_opens_a_dialog_returns_too(page):
    page.evaluate("document.addEventListener('keydown',e=>{if(e.key==='q')alert('k')});1")
    reply = page.call("press_key", key="q", _timeout=30)
    assert reply.seconds < 15 and "dialog_handle" in reply.text, reply.text
    page.call("dialog_handle", action="dismiss", _timeout=15)


def test_evaluate_that_opens_a_dialog_names_it(page):
    reply = page.call("evaluate", expression="alert('e')", timeout_ms=1500, _timeout=20)
    assert reply.failed and "dialog_handle" in reply.text, reply.text
    page.call("dialog_handle", action="accept", _timeout=15)


def test_a_reply_after_a_timeout_leaves_the_connection_working(page):
    """nodriver 0.48 killed its CDP listener on the late reply, and every later call hung."""
    assert page.call("evaluate", expression="new Promise(r=>setTimeout(r,800,'late'))",
                     timeout_ms=100, _timeout=10).failed
    time.sleep(1.2)
    assert page.call("evaluate", expression="1+1", timeout_ms=5000, _timeout=12).text == "2"


def test_invalid_dialog_arguments_are_refused_before_a_dialog_can_stick(page):
    assert page.call("dialog_handle", action="bogus").failed
    assert page.call("dialog_auto_handle", action="bogus").failed
    assert page.call("dialog_auto_handle", types=["notatype"]).failed
    page.call("dialog_handle", action="accept")
    page.call("click", selector="#confirmbtn", _timeout=20)
    assert page.evaluate("document.getElementById('dlg').textContent") == "confirm:true"


@pytest.mark.parametrize("text", ["null", '{"a":1}', "[1]", "123"])
def test_prompt_text_arrives_as_sent(page, text):
    page.call("dialog_handle", action="accept", text=text)
    page.call("click", selector="#promptbtn", _timeout=20)
    assert page.evaluate("document.getElementById('dlg').textContent") == f"prompt:{text}"


def test_inject_init_script_runs_on_a_fresh_tab(base):
    """A fresh client: evaluate enables the Page domain, and that hid this for anyone who
    had already called it."""
    client = Client()
    try:
        launch(client, f"{base}/page2.html")
        assert not client.call("inject_init_script", script="window.__i=7").failed
        client.call("navigate", url=f"{base}/tall.html")
        assert client.evaluate("String(window.__i)") == "7"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


# ── click reports ──────────────────────────────────────────────────────────

def test_same_length_text_change_is_a_page_change(page):
    for reply in (page.call("click", selector="#inc"), page.call("click", selector="#swap"),
                  page.call("click_text", text="Increment"), page.call("humanize_click", selector="#inc")):
        assert "NO page change" not in reply.text, reply.text
    assert page.evaluate("document.getElementById('count').textContent") == "3"


def test_a_dead_button_still_reads_as_dead(page):
    assert "NO page change" in page.call("click", selector="#dead").text


def test_focus_falling_to_body_is_not_a_page_reaction(page):
    page.call("click", selector="#dead")  # a button now holds focus
    assert page.evaluate("document.activeElement.id") == "dead"
    reply = page.call("click", selector="#plain")
    assert page.evaluate("document.activeElement.tagName") == "BODY"
    assert "NO page change" in reply.text, reply.text


def test_focus_arriving_on_another_element_is_a_page_reaction(page):
    reply = page.call("click", selector="#focuser")
    assert page.evaluate("document.activeElement.id") == "email"
    assert "NO page change" not in reply.text, reply.text


def test_keys_without_text_do_not_stall_the_mouse(page):
    for key in ("Escape", "ArrowDown", "Home", "F1"):
        page.call("press_key", key=key, verify=False)
        reply = page.call("mouse_click_xy", x=5, y=5, _timeout=20)
        assert reply.seconds < 2, (key, reply.seconds)


def test_click_role_finds_implicit_roles_without_a_name(page):
    reply = page.call("click_role", role="button")
    assert not reply.failed, reply.text
    reply = page.call("click_role", role="textbox")
    assert not reply.failed, reply.text


def test_click_role_name_reaches_the_control_not_its_label(page):
    reply = page.call("click_role", role="textbox", name="Full Name")
    assert "target=<input#name>" in reply.text, reply.text


def test_click_text_exact_refuses_a_partial_match(page):
    assert page.call("click_text", text="Inc", exact=True, _timeout=30).failed
    assert page.evaluate("document.getElementById('count').textContent") == "0"
    assert not page.call("click_text", text="Increment", exact=True).failed


def test_click_and_wait_checks_the_target_it_was_given(page):
    assert page.call("click_and_wait", selector="#dead", expect_text="never", timeout=0.5).failed
    assert page.call("click_and_wait", selector="#dead", expect_selector="#never", timeout=0.5).failed
    assert not page.call("click_and_wait", selector="#delay", expect_selector="#late", timeout=4).failed


def test_hover_refuses_a_covered_element_and_reaches_a_far_one(page):
    assert page.call("hover", selector="#blocked").failed
    reply = page.call("hover", selector="#farbtn")
    assert not reply.failed and float(page.evaluate("scrollY")) > 0, reply.text
    assert page.call("hover", selector="#disabledbtn").text.startswith("hovered")


def test_point_clicks_refuse_what_they_would_miss(page):
    for reply in (page.call("click_element_offset", selector="#blocked", humanize=False),
                  page.call("click_at_corner", selector="#blocked", offset=4),
                  page.call("click_element_offset", selector="#cornerbox", x_percent=150),
                  page.call("click_at_corner", selector="#cornerbox", offset=5000)):
        assert reply.failed, reply.text
    assert page.evaluate("String(window.blockedClicked)") == "undefined"


def test_point_clicks_land_and_report_the_outcome(page):
    reply = page.call("click_at_corner", selector="#cornerbox", corner="bottom-right", offset=8)
    seen = json.loads(page.evaluate("document.getElementById('cornerout').textContent"))
    assert abs(seen["x"] - 192) <= 2 and abs(seen["y"] - 92) <= 2 and seen["t"] is True
    assert "page changed" in reply.text
    assert "NO page change" in page.call("click_element_offset", selector="#dead", humanize=False).text


def test_raw_points_outside_the_viewport_are_refused(page):
    for reply in (page.call("mouse_click_xy", x=-5, y=99999), page.call("humanize_click", x=-50, y=-50),
                  page.call("mouse_move", x=-50, y=-60)):
        assert reply.failed and "viewport" in reply.text, reply.text


def test_mouse_buttons_still_work_after_the_input_change(page):
    box = json.loads(page.evaluate("JSON.stringify(document.getElementById('inc').getBoundingClientRect())"))
    x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    assert not page.call("mouse_click_xy", x=x, y=y).failed
    assert page.evaluate("document.getElementById('count').textContent") == "1"
    page.call("mouse_click_xy", x=x, y=y, button="right")
    assert page.evaluate("document.getElementById('count').textContent") == "1"
    assert page.call("mouse_click_xy", x=x, y=y, button="bogus").failed


# ── typing ─────────────────────────────────────────────────────────────────

def test_typing_sends_real_keys(page):
    page.call("click", selector="#name")
    events(page)
    page.call("type_text", text="Ab1!")
    assert page.evaluate("document.getElementById('name').value") == "Ab1!"
    downs = [e["key"] for e in events(page, "keydown") if e["key"] != "Shift"]
    assert downs == ["A", "b", "1", "!"]
    assert not [e for e in events(page) if not e["tr"]]


def test_humanize_type_and_fill_send_real_keys_too(page):
    page.call("click", selector="#name")
    events(page)
    page.call("humanize_type", text="xy", mean_delay=0.01)
    assert len(events(page, "keydown")) == 2
    page.call("fill", selector="#email", value="zz")
    assert len(events(page, "keydown")) >= 2


def test_typing_keeps_non_ascii_and_newlines(page):
    page.call("click", selector="#ta")
    page.call("type_text", text="é中\nl2")
    assert json.loads(page.evaluate("JSON.stringify(document.getElementById('ta').value)")) == "é中\nl2"


def test_typing_refuses_what_would_be_lost(page):
    page.evaluate("document.activeElement && document.activeElement.blur();1")
    assert page.call("type_text", text="nofocus").failed
    assert page.call("humanize_type", text="q", mean_delay=0.001).failed
    page.call("click", selector="#name")
    assert page.call("type_text", text="a\nb").failed
    assert page.evaluate("document.getElementById('name').value") == ""


def test_press_key_selects_all(page):
    page.call("fill", selector="#name", value="abcdef")
    page.call("click", selector="#name")
    for key in ("Meta+a", "Control+a"):
        page.evaluate("document.getElementById('name').setSelectionRange(6,6);1")
        page.call("press_key", key=key if os.uname().sysname == "Darwin" else "Control+a", verify=False)
        assert page.evaluate("(()=>{const e=document.getElementById('name');return e.selectionStart+'-'+e.selectionEnd})()") == "0-6"


def test_paste_text_reports_what_landed(page):
    assert not page.call("paste_text", text="ce text", selector="#ce").failed
    assert page.evaluate("document.getElementById('ce').textContent") == "ce text"
    assert page.call("paste_text", text="x", selector="#dead").failed
    page.evaluate("document.getElementById('name').disabled=true;document.getElementById('email').readOnly=true;1")
    assert page.call("paste_text", text="x", selector="#name").failed
    assert page.call("paste_text", text="x", selector="#email").failed
    assert page.evaluate("document.getElementById('name').value") == ""


def test_upload_file_refuses_what_is_not_a_file(page, tmp_path):
    good = tmp_path / "up.txt"
    good.write_text("hello")
    assert page.call("upload_file", file_path=str(tmp_path / "none.txt"), selector="#file").failed
    assert page.call("upload_file", file_path=str(tmp_path), selector="#file").failed
    assert page.call("upload_file", file_path="~/definitely-not-there-mcp.txt", selector="#file").failed
    assert page.evaluate("document.getElementById('file').files.length") == "0"
    assert not page.call("upload_file", file_path=str(good), selector="#file").failed
    assert page.evaluate("document.getElementById('fileinfo').textContent") == "up.txt:5"


# ── text arguments that look like JSON ─────────────────────────────────────

def test_wait_for_looks_for_the_text_null(page):
    assert page.call("wait_for", text="null", timeout=0.3).failed
    assert page.call("wait_for", selector="null", timeout=0.3).failed


def test_select_option_value_null(page):
    assert not page.call("select_option", selector="#sel", value="null").failed
    assert page.evaluate("document.getElementById('sel').value") == "null"


def test_storage_key_that_looks_like_json(page):
    page.evaluate("localStorage.clear();localStorage.setItem('[1]','v1');localStorage.setItem('null','v2');1")
    assert page.call("localstorage_get", key="[1]").text == "v1"
    assert page.call("localstorage_get", key="null").text == "v2"


def test_cookie_import_raw_json(page):
    page.call("cookie_import", raw_text='[{"name":"rawc","value":"V7","domain":"localhost"}]')
    assert "rawc=V7" in page.evaluate("document.cookie")


# ── waiting ────────────────────────────────────────────────────────────────

def test_wait_for_navigation_measures_instead_of_sleeping(page, base):
    start = time.monotonic()
    assert not page.call("wait_for_navigation", timeout=5).failed
    assert time.monotonic() - start < 2
    page.call("navigate", url=f"{base}/slowimg.html", wait_until="none")
    assert page.call("wait_for_navigation", timeout=0.2).failed
    time.sleep(2.2)


def test_navigate_waits_for_load_and_validates_wait_until(page, base):
    reply = page.call("navigate", url=f"{base}/slowimg.html")
    assert "ready_state complete" in reply.text, reply.text
    assert page.call("navigate", url=f"{base}/page2.html", wait_until="bogus").failed


def test_wait_for_text_means_visible_text(page):
    assert page.call("wait_for", text="uniqueword123", timeout=1).text.endswith("found")
    assert page.call("wait_for", text="hiddenonlytext", timeout=0.3).failed
    assert page.call("wait_for", text="SCRIPTMARKER", timeout=0.3).failed


def test_a_page_that_did_not_load_is_counted_as_failed(page):
    bad = "http://nonexistent.invalid/"
    assert page.call("navigate", url=bad).failed
    assert "1 failed" in page.call("batch_actions", actions=[{"type": "navigate", "url": bad}]).text
    assert page.call("workflow_run", steps=[{"tool": "navigate", "args": {"url": bad}}]).failed


def test_network_get_says_when_nothing_is_listening(base):
    client = Client()
    try:
        launch(client, f"{base}/page2.html")
        assert client.call("network_get").failed
        client.call("network_start")
        reply = client.call("network_get")
        assert not reply.failed
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


# ── launch options ─────────────────────────────────────────────────────────

def test_lang_reaches_the_page_and_the_server(base):
    client = Client()
    try:
        _Fixture.requests.clear()
        launch(client, f"{base}/page2.html", lang="id-ID")
        assert client.evaluate("navigator.language") == "id-ID"
        assert client.evaluate("navigator.userAgentData.brands.length") != "0"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_spawn_browser_lang_too(base):
    client = Client()
    try:
        client.call("spawn_browser", _timeout=90, instance_id="langy", url=f"{base}/page2.html", headless=True,
                    persistent=False, extra_args=SAFE_ARGS, lang="id-ID")
        client.call("switch_instance", instance_id="langy")
        assert client.evaluate("navigator.language") == "id-ID"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


@pytest.mark.parametrize("option", [{"profile_mode": "light"}, {"testing_mode": True}])
def test_image_blocking_covers_the_first_page(base, option):
    client = Client()
    try:
        launch(client, f"{base}/protocol.html", **option)
        assert client.evaluate("document.getElementById('img').naturalWidth") == "0"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


# ── storage and cookies ────────────────────────────────────────────────────

def test_storage_tools_report_a_refused_write(page):
    page.call("navigate", url="about:blank")
    for reply in (page.call("localstorage_set", key="a", value="1"), page.call("localstorage_clear"),
                  page.call("sessionstorage_set", key="a", value="1"), page.call("sessionstorage_clear"),
                  page.call("sessionstorage_get")):
        assert reply.failed, reply.text


def test_invalid_css_is_an_error(page):
    for reply in (page.call("query_selector_all", selector="[[["),
                  page.call("extract_structured", container_selector="[[[", fields=[{"name": "t", "selector": ".ct"}]),
                  page.call("wait_for_change", selector="[[[", timeout=0.5),
                  page.call("assert_element_visible", selector="[[[", timeout=0.5)):
        assert reply.failed and "valid selector" in reply.text, reply.text


def test_cookie_delete_really_deletes_and_says_otherwise(page):
    gone = lambda name: f'"{name}"' not in page.call("cookie_list").text  # noqa: E731
    page.call("cookie_set", name="dotted", value="x", domain=".localhost")
    assert not gone("dotted")
    assert not page.call("cookie_delete", name="dotted", domain=".localhost").failed
    assert gone("dotted")
    page.call("cookie_set", name="nodomain", value="x", domain="localhost")
    assert not page.call("cookie_delete", name="nodomain").failed
    assert gone("nodomain")
    page.call("cookie_set", name="deep", value="x", domain="localhost", path="/sub")
    assert not page.call("cookie_delete", name="deep", domain="localhost").failed
    assert gone("deep")
    assert page.call("cookie_delete", name="neverthere").failed
    assert page.call("cookie_set", name="", value="x", domain="localhost").failed


def test_http_request_sends_browser_cookies_on_localhost(page, base):
    page.evaluate("document.cookie='hrc=HRV';1")
    _Fixture.requests.clear()
    reply = page.call("http_request", url=f"{base}/page2.html?hr=1")
    seen = [r for r in _Fixture.requests if "hr=1" in r["path"]][-1]
    assert "hrc=HRV" in (seen["cookie"] or ""), reply.text[:300]


def test_indexeddb_delete_of_nothing_says_so(page, base):
    page.call("navigate", url=f"{base}/idb.html")
    time.sleep(0.3)
    assert page.call("indexeddb_delete", database_name="nosuchdb").failed
    assert not page.call("indexeddb_delete", database_name="protodb").failed


# ── Cloudflare ─────────────────────────────────────────────────────────────

def test_click_turnstile_reports_a_challenge_not_passed(page, base):
    page.call("navigate", url=f"{base}/turnstile.html", auto_verify=False)
    reply = page.call("click_turnstile")
    assert "clicked Turnstile" in reply.text and "NOT passed" in reply.text and "token empty" in reply.text, reply.text
    assert "solve_turnstile_oscursor" in reply.text, reply.text


def test_click_turnstile_reports_a_filled_token(page, base):
    page.call("navigate", url=f"{base}/solved.html", auto_verify=False)
    reply = page.call("click_turnstile")
    assert "token filled (10 chars)" in reply.text and "SYNTHTOKEN" not in reply.text, reply.text


def test_nothing_is_clicked_on_a_page_without_a_widget(page, base):
    page.call("navigate", url=f"{base}/page2.html", auto_verify=False)
    assert page.call("click_turnstile", fallback_template=True).failed
    assert page.call("verify_cf").failed
    assert page.call("verify_cf", template_image="/nonexistent/template.png").failed


def test_auto_verify_says_the_challenge_is_still_there(page, base):
    reply = page.call("navigate", url=f"{base}/turnstile.html", _timeout=50)
    assert "challenge still present" in reply.text, reply.text[:300]


def test_a_plain_page_is_not_kept_waiting_for_a_challenge(page, base):
    """navigate slept 0.6 s on every page in case a challenge appeared, and Tab.get another 0.5 s
    when the URL did not change: 1.13 s to reopen a local page that loads in 60 ms."""
    page.call("navigate", url=f"{base}/page2.html")
    times = [page.call("navigate", url=f"{base}/page2.html").seconds for _ in range(3)]
    assert max(times) < 0.45, times


def test_a_challenge_arriving_after_load_is_still_seen(page, base):
    """The widget is inserted 300 ms after load. The page's own script names turnstile, which is
    what keeps the short wait for it."""
    reply = page.call("navigate", url=f"{base}/latewidget.html", _timeout=50)
    assert "challenge still present" in reply.text, reply.text[:300]


def test_reopening_the_same_url_reads_the_new_document(page, base):
    """Without the fixed wait, the load must still be the new one: the server answers after 0.3 s
    with a number that goes up on every request."""
    first = page.call("navigate", url=f"{base}/counter.html")
    before = int(page.evaluate("document.getElementById('n').textContent").strip('"'))
    second = page.call("navigate", url=f"{base}/counter.html")
    after = int(page.evaluate("document.getElementById('n').textContent").strip('"'))
    assert after == before + 1, (before, after)
    for reply in (first, second):
        assert '"status": 200' in reply.text and '"ready_state": "complete"' in reply.text, reply.text
    assert second.seconds >= 0.3, second.seconds


def test_a_click_that_changed_the_page_is_reported_at_once(page, base):
    """Every click waited a fixed 350 ms before looking: 530 ms for a button that had already
    changed the page by the time the click was acknowledged."""
    page.call("navigate", url=f"{base}/late.html")
    times = []
    for _ in range(3):
        reply = page.call("click", selector="#now")
        assert "page changed" in reply.text, reply.text
        times.append(reply.seconds)
        page.evaluate("out.textContent = '0'; 1")
    assert max(times) < 0.35, times


def test_a_handler_that_reacts_late_is_not_reported_dead(page, base):
    """The page changes 300 ms after the click. Stopping early on a change must not shorten the
    wait for a page that has not changed yet."""
    page.call("navigate", url=f"{base}/late.html")
    for _ in range(3):
        reply = page.call("click", selector="#late")
        assert "page changed" in reply.text and "NO page change" not in reply.text, reply.text
        page.evaluate("out.textContent = '0'; 1")
        time.sleep(0.1)


def test_a_dead_button_still_gets_the_whole_wait(page, base):
    page.call("navigate", url=f"{base}/late.html")
    reply = page.call("click", selector="#dead")
    assert "NO page change detected" in reply.text, reply.text
    assert reply.seconds >= 0.45, reply.seconds


def test_a_click_calls_nothing_the_page_can_replace(page, base):
    """The hit test and the before and after fingerprints ran in the page's own world: 18 calls
    to getBoundingClientRect, elementFromPoint, getComputedStyle and the like for one click,
    each of which a page can replace and so watch. The challenge look on every navigation
    handed a replaced querySelector the Turnstile selectors."""
    def calls():
        return json.loads(page.evaluate("JSON.stringify(window.__calls)"))

    assert not page.call("navigate", url=f"{base}/watched.html").failed
    assert calls() == [], "navigate"
    reply = page.call("click", selector="#b")
    assert "page changed" in reply.text and calls() == [], (reply.text, calls())
    reply = page.call("click_text", text="Press")
    assert "page changed" in reply.text and calls() == [], (reply.text, calls())
    assert not page.call("hover", selector="#b").failed and calls() == [], calls()
    reply = page.call("click", selector="#under")
    assert reply.failed and "obscured by <div#cover>" in reply.text and calls() == [], (reply.text, calls())
    assert page.evaluate("out.textContent").strip('"') == "2"


def _row(snapshot, needle):
    rows = [line for line in snapshot.splitlines() if needle in line]
    assert len(rows) == 1, (needle, rows)
    return rows[0]


def test_a_field_is_named_by_its_label_on_its_own_row(page, base):
    """An input with a <label for> read as a bare [input], its label on a row of its own: 75 of
    the 158 rows of a form, and nothing on the field's row to find it by."""
    page.call("navigate", url=f"{base}/labels.html")
    text = page.call("browser_snapshot", diff_from_last=False).text
    assert 'type=email label="Email"' in _row(text, "type=email"), text
    assert 'label="Phone"' in _row(text, "type=tel"), text          # a label around the field
    assert 'label="Postal code"' in text, text                      # aria-labelledby
    colour = _row(text, "[select]")
    assert 'label="Colour"' in colour, text                         # not "Colour Red Blue"
    for folded in ('[label] "Email"', '[label] "Phone', '[label] "Colour'):
        assert folded not in text, (folded, text)
    assert "3 label row(s)" in text, text


def test_a_label_without_a_listed_field_keeps_its_row(page, base):
    """The checkbox is display:none, as styled ones are: its label is the thing to click."""
    page.call("navigate", url=f"{base}/labels.html")
    text = page.call("browser_snapshot", diff_from_last=False).text
    assert '[label] "Dark mode"' in text and '[label] "Orphan text"' in text, text


def test_a_named_field_is_found_and_filled_by_that_name(page, base):
    page.call("navigate", url=f"{base}/labels.html")
    text = page.call("browser_snapshot", diff_from_last=False, name_contains="postal").text
    row = _row(text, 'label="Postal code"')
    ref = row.split("ref=")[1].split()[0]
    assert not page.call("fill", ref=ref, value="12345").failed
    assert page.evaluate("document.getElementById('zip').value").strip('"') == "12345"


def test_full_mode_keeps_the_label_rows(page, base):
    page.call("navigate", url=f"{base}/labels.html")
    text = page.call("browser_snapshot", diff_from_last=False, mode="full").text
    assert '[label] "Email"' in text and 'type=email label="Email"' in text, text


def test_an_empty_token_means_the_challenge_is_not_over(page, base):
    """The title reads normally here, so only the empty token says the widget is unsolved."""
    reply = page.call("navigate", url=f"{base}/widget.html", _timeout=50)
    assert "challenge still present" in reply.text, reply.text[:300]


def test_the_interstitial_title_means_the_challenge_is_not_over(page, base):
    """No token input and no text in the body: only the title gives this page away."""
    reply = page.call("navigate", url=f"{base}/moment.html", _timeout=50)
    assert "challenge still present" in reply.text, reply.text[:300]


def test_detect_and_bypass_does_not_claim_a_bypass_of_nothing(page):
    reply = page.call("detect_and_bypass").json()
    assert reply["bypassed"] is False and reply["needed"] is False


# ── the rest ───────────────────────────────────────────────────────────────

def test_session_warmup_of_an_unreachable_host(page):
    assert page.call("session_warmup", target_url="http://nonexistent.invalid/x", dwell_seconds=0.1).failed
    assert page.call("session_warmup", target_url="notaurl", dwell_seconds=0.1).failed


def test_browser_paths_do_not_sit_out_the_challenge_wait(page, base):
    reply = page.call("scrape_markdown", url=f"{base}/page2.html", use_browser=True)
    data = reply.json()
    assert data["elapsed_ms"] < 10000 and data["active_tab_navigated"] is True
    assert page.call("smart_fetch", url=f"{base}/gone").json()["path"] == "http"


def test_scroll_reports_what_moved(page, base):
    assert page.call("scroll", direction="sideways").failed
    moved = page.call("scroll", direction="down", amount=300, humanize=False)
    assert "scrollY 0→" in moved.text, moved.text
    page.call("navigate", url=f"{base}/tall.html")
    assert "did not move" in page.call("scroll", direction="down", amount=300, humanize=False).text


def test_scroll_to_refuses_and_settles(page):
    assert "block must be" in page.call("scroll_to", selector="#farbtn", block="bogus").text
    assert "not rendered" in page.call("scroll_to", selector="#hiddenbtn").text
    assert not page.call("scroll_to", selector="#farbtn", block="start").failed
    first = page.evaluate("document.getElementById('farbtn').getBoundingClientRect().top")
    time.sleep(0.5)
    assert page.evaluate("document.getElementById('farbtn').getBoundingClientRect().top") == first


def test_get_html_honours_outer(page):
    assert page.call("get_html", selector="#title").text == "Sweep Heading"
    assert page.call("get_html", selector="#title", outer=True).text.startswith("<h1")


def test_nonsense_arguments_are_refused(page):
    assert page.call("set_viewport_size", width=-5, height=0).failed
    assert page.call("mouse_drift", duration_seconds=-1, segments=-3).failed
    assert page.call("mouse_replay", path_json='[{"x":1}]').failed
    assert page.call("mouse_replay", path_json='[{"x":1,"y":1}]', speed=0).failed
    assert page.call("emulate_cpu", rate=0.1).failed
    assert page.call("smart_fill", fields={}).failed
    assert page.call("fill_form", fields=[{"selector": "#name"}]).failed
    assert page.call("frame_evaluate", frame_url_pattern="nomatch", expression="1").failed
    assert page.call("extract_table", selector="#none").failed
    assert page.call("extract_table", selector="#para").failed
    assert page.call("fingerprint_rotate").failed
    assert page.call("fingerprint_rotate", timezone="Not/AZone").failed


def test_a_refused_timezone_is_not_called_applied(page):
    """Chrome rejects a bad id only the first time. Once one override is set it accepts the
    call and keeps the old zone, so only reading it back shows nothing changed."""
    assert not page.call("fingerprint_rotate", timezone="Asia/Tokyo").failed
    reply = page.call("fingerprint_rotate", timezone="Not/AZone")
    assert reply.failed and "was not applied" in reply.text, reply.text
    assert not page.call("fingerprint_rotate", timezone="Asia/Jakarta").failed


def test_storage_state_load_of_a_list(page, tmp_path):
    state = tmp_path / "s.json"
    state.write_text("[1]")
    reply = page.call("storage_state_load", file_path=str(state))
    assert reply.failed and "not a storage state" in reply.text, reply.text


def test_emulated_phone_has_touch_points_and_desktop_resets_them(page, base):
    page.call("emulate_device", preset="iphone-15")
    page.call("reload")
    assert page.evaluate("navigator.maxTouchPoints") != "0"
    page.call("emulate_device", preset="desktop")
    page.call("reload")
    assert page.evaluate("navigator.maxTouchPoints") == "0"
