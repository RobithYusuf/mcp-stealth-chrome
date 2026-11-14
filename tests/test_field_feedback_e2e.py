"""Field reports from a checkout run, reproduced on local fixtures over real MCP and real Chrome.

Needs MCP_E2E=1. Headless, an ephemeral profile, loopback only. The page is served on
http://localhost:<port>; its card frame and 3-D Secure frame come from http://127.0.0.1:<port>,
another site, so Chrome puts them in their own process as a payment page's frames are.
"""
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse

import pytest

from _protocol import SAFE_ARGS, Client

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

LONG_CONFIG = "eyJ" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 60
VAULT = json.dumps({"gatewayTokens": [{
    "id": "gt_synth_7f3a9c2e41b8", "token": "4111110000001111", "cardScheme": "VISA",
    "cardExpiryMonth": "12", "issuerName": "Test Bank AG",
    "lastSuccessfulPurchaseDate": "2026-11-13T09:41:07Z"}], "schemeTokens": []})


def _pages(cross: str) -> dict:
    return {
        "/checkout.html": (
            "<!doctype html><title>Checkout</title><style>body{margin:0}"
            "#card{position:absolute;left:20px;top:20px;width:300px;height:80px;border:0}"
            "#pay{position:absolute;left:20px;top:150px;width:120px;height:40px}"
            "#threeds{position:absolute;left:20px;top:220px;width:300px;height:100px;border:0}</style>"
            f'<iframe id="card" name="card-expiry" src="{cross}/card.html"></iframe>'
            '<button id="pay" name="pay">Pay now</button>'
            f'<form id="f" target="threeds" method="POST" action="{cross}/3ds?config={LONG_CONFIG}'
            '&signature=SYNTHSIG"><input type="hidden" name="creq" value="x"></form>'
            '<iframe id="threeds" name="threeds"></iframe>'
            "<script>window.post3ds = () => document.getElementById('f').submit();</script>"),
        "/card.html": ('<!doctype html><title>Card</title><body style="margin:0">'
                       '<input name="expiry" style="width:280px;height:60px"></body>'),
        "/leave.html": ("<!doctype html><title>Leave</title><script>"
                        "document.cookie = 'sid=SYNTHSID; path=/';"
                        "fetch('/api/tokens?access_token=SYNTHURL', {headers: {"
                        "'Authorization': 'Bearer SYNTHAUTH', 'X-Api-Key': 'SYNTHKEY'}})"
                        ".then(r => r.text()).then(() => location.href = '/done.html');"
                        "</script>"),
        "/leave-site.html": ("<!doctype html><title>Leave</title><script>"
                             "fetch('/api/tokens').then(r => r.text())"
                             f".then(() => location.href = '{cross}/done.html');</script>"),
        "/done.html": "<!doctype html><title>Done</title><p>done</p>",
        "/many.html": ("<!doctype html><title>Many</title><script>window.done = 0;"
                       "for (let i = 0; i < 300; i++) fetch('/api/item?i=' + i).then(r => r.text())"
                       ".then(() => { window.done++; });</script>"),
        "/plain.html": "<!doctype html><title>Plain</title><p id=p>still</p>",
    }


class _Fixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _reply(self, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/tokens":
            return self._reply(VAULT, "application/json")
        if path == "/api/item":
            return self._reply('{"ok": true, "n": 1}', "application/json")
        page = _pages(f"http://127.0.0.1:{self.server.server_port}").get(path)
        if page is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._reply(page)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/api/"):
            return self._reply('{"ok": true}', "application/json")
        self._reply("<!doctype html><title>3DS</title><body style='margin:0'><p>Challenge</p></body>")


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def mcp(base):
    client = Client()
    reply = client.call("browser_launch", _timeout=90, url=f"{base}/plain.html", headless=True,
                        persistent=False, extra_args=SAFE_ARGS)
    assert not reply.failed, reply.text
    yield client
    client.call("close_all_instances", _timeout=40)
    client.close()


def _go(mcp, url):
    reply = mcp.call("navigate", url=url)
    assert not reply.failed, reply.text


def _checkout(mcp, base):
    _go(mcp, f"{base}/checkout.html")
    reply = mcp.call("wait_for", selector="#pay", timeout=10)
    assert not reply.failed, reply.text


# ── 2 and 8: screenshot ────────────────────────────────────────────────────


# Windows CI: the first screenshot of a server process does not answer within 60 s, and every
# later one takes about 1.2 s (open; AGENTS.md, test_protocol_e2e.py). This takes that first one,
# so the screenshot tests below measure what they are about.
@pytest.mark.xfail(sys.platform == "win32", strict=False, reason=(
    "Windows: the first screenshot of a server process does not answer (open)"))
def test_the_first_screenshot_of_this_server(mcp, base):
    _go(mcp, f"{base}/plain.html")
    reply = mcp.call("screenshot", output_mode="path")
    assert not reply.failed and reply.seconds < 30, (reply.seconds, reply.text[:200])


def _shot(mcp, **kw):
    """A screenshot, waiting out one still held by the Windows first-screenshot stall above."""
    deadline = time.monotonic() + 90
    while True:
        reply = mcp.call("screenshot", **kw)
        if "is busy with another" not in reply.text or time.monotonic() > deadline:
            return reply
        time.sleep(2)


def test_2_quality_without_a_name_gives_a_jpeg(mcp, base):
    _go(mcp, f"{base}/plain.html")
    reply = _shot(mcp, quality=45, output_mode="path")
    assert not reply.failed, reply.text
    first = reply.text.splitlines()[0]
    assert first.endswith(".jpg") and "image/jpeg" in reply.text, reply.text
    reply = _shot(mcp, filename="field-q.png", quality=45, output_mode="path")
    assert "image/png" in reply.text and "quality=45 not applied" in reply.text, reply.text


def test_8_a_resized_screenshot_says_how_to_map_a_point(mcp, base):
    _go(mcp, f"{base}/checkout.html")
    width = int(mcp.evaluate("innerWidth"))
    reply = _shot(mcp, max_dimension=width // 2, output_mode="path")
    assert not reply.failed, reply.text
    found = re.search(r"1 image px = ([0-9.]+) CSS px", reply.text)
    assert found, reply.text
    dpr = float(mcp.evaluate("devicePixelRatio"))
    saved = int(re.search(r"image: (\d+)×", reply.text).group(1))
    assert abs(float(found.group(1)) - width / saved) < 0.01 * width / saved, reply.text
    assert "mouse_click_xy(x=image_x ×" in reply.text
    # The mapping is right: the Pay button's centre read off the image lands on it.
    ratio = float(found.group(1))
    image_x, image_y = round(80 / ratio), round(170 / ratio)
    hit = mcp.evaluate(f"document.elementFromPoint({image_x * ratio}, {image_y * ratio}).id")
    assert hit == "pay" and dpr > 0


# ── 3 and 4: bodies ────────────────────────────────────────────────────────


def test_3_a_small_json_body_survives_the_page_moving_on(mcp, base):
    _go(mcp, f"{base}/plain.html")
    assert not mcp.call("network_start").failed
    _go(mcp, f"{base}/leave.html")
    assert not mcp.call("wait_for_url", pattern="done.html", timeout=10).failed
    time.sleep(0.3)
    reply = mcp.call("network_get", include_body=True, filter_url="/api/tokens")
    assert not reply.failed, reply.text
    entry = reply.json()[-1]
    assert not entry["response_body"].startswith("<getResponseBody"), entry["response_body"]
    body = json.loads(entry["response_body"])
    card = body["gatewayTokens"][0]
    # 4: the default hides everything under the secret-named gatewayTokens.
    assert card["cardScheme"].startswith("***") and card["token"].startswith("***")
    assert "4111110000001111" not in reply.text and "VISA" not in reply.text
    # reveal_values="body": values under the parent show, credentials do not.
    shown = mcp.call("network_get", include_body=True, filter_url="/api/tokens",
                     reveal_values="body")
    doc = shown.json()
    assert doc["reveal_values"] == "body" and "at the caller's request" in doc["note"], doc
    entry = doc["entries"][-1]
    card = json.loads(entry["response_body"])["gatewayTokens"][0]
    assert card["cardScheme"] == "VISA" and card["issuerName"] == "Test Bank AG"
    assert card["lastSuccessfulPurchaseDate"] == "2026-11-13T09:41:07Z"
    assert card["token"].startswith("***")
    for secret in ("4111110000001111", "SYNTHAUTH", "SYNTHKEY", "SYNTHSID", "SYNTHURL"):
        assert secret not in shown.text, secret
    assert entry["request_headers"]["Authorization"].startswith("***")
    revealed = mcp.call("network_get", include_body=True, filter_url="/api/tokens", reveal_values=True)
    assert "4111110000001111" in revealed.text and "SYNTHAUTH" in revealed.text


def test_7_network_get_filters_by_resource_type_and_cuts_urls(mcp, base):
    _go(mcp, f"{base}/plain.html")
    assert not mcp.call("network_start").failed
    _checkout(mcp, base)
    rows = mcp.call("network_get", full=True, resource_type="document").json()
    assert rows and all(r["resource_type"] == "Document" for r in rows), rows
    rows = mcp.call("network_get", resource_type="xhr,fetch").json()
    assert rows == [], rows
    mcp.evaluate("post3ds(); 1")
    assert not mcp.call("wait_for", frame_url="/3ds", timeout=10).failed
    time.sleep(0.5)
    rows = mcp.call("network_get", full=True, filter_url="/3ds").json()
    assert rows and len(rows[-1]["url"]) == 201 and "max_url_len=0" in rows[-1]["url_cut"], rows
    assert "SYNTHSIG" not in json.dumps(rows)
    whole = mcp.call("network_get", full=True, filter_url="/3ds", max_url_len=0).json()
    assert "url_cut" not in whole[-1] and "signature=***" in whole[-1]["url"]


@pytest.mark.parametrize("path", ["/leave.html", "/leave-site.html"])
def test_3_the_body_survives_a_same_and_a_cross_site_navigation(mcp, base, path):
    _go(mcp, f"{base}/plain.html")
    started = mcp.call("network_start")
    assert "past a navigation" in started.text, started.text
    _go(mcp, f"{base}{path}")
    assert not mcp.call("wait_for_url", pattern="done.html", timeout=10).failed
    reply = mcp.call("network_get", include_body=True, filter_url="/api/tokens",
                     reveal_values="body")
    assert '"cardScheme": "VISA"' in reply.json()["entries"][-1]["response_body"], reply.text


def test_3r_a_clear_says_kept_and_drops_the_old_bodies_from_every_tool(mcp, base):
    """Review 2: re-arming emptied the lists but said nothing of the bodies Chrome kept."""
    _go(mcp, f"{base}/plain.html")
    started = mcp.call("network_start")
    assert "Chrome keeps response bodies past a navigation" in started.text, started.text
    _go(mcp, f"{base}/leave.html")
    assert not mcp.call("wait_for_url", pattern="done.html", timeout=10).failed
    before = mcp.call("network_get", include_body=True, filter_url="/api/tokens").json()
    assert before and before[-1]["response_body"].startswith("{"), before
    again = mcp.call("network_start")
    assert "no tool can read them any more" in again.text, again.text
    assert mcp.call("network_get", include_body=True, filter_url="/api/tokens").json() == []
    off = mcp.call("network_start", capture_bodies=False)
    assert "keeps response bodies" not in off.text, off.text


POST_JS = ("setTimeout(() => fetch('/api/vault', {method: 'POST', headers: {"
           "'Authorization': 'Bearer SYNTHAUTH', 'Content-Type': 'application/json'}, "
           "body: JSON.stringify({tokens: {type: 'card', issuerName: 'Test Bank AG', "
           "token: 'SYNTHTOKEN'}})}), 400); 1")


def _check_body_mode(text: str):
    assert "at the caller's request" in text, text
    assert "Test Bank AG" in text and "SYNTHTOKEN" not in text and "SYNTHAUTH" not in text, text


@pytest.mark.parametrize("tool", ["auth_capture", "wait_for_request", "export_curl",
                                  "intercept_log"])
def test_4r_reveal_body_on_every_captured_traffic_tool(mcp, base, tool):
    _go(mcp, f"{base}/plain.html")
    assert not mcp.call("network_start").failed
    if tool == "intercept_log":
        assert not mcp.call("intercept_start", url_pattern="*api/vault*").failed, "start"
    mcp.evaluate(POST_JS)
    if tool == "auth_capture":
        reply = mcp.call("auth_capture", filter_url_pattern="/api/vault", reveal_values="body")
    elif tool == "wait_for_request":
        reply = mcp.call("wait_for_request", url_pattern="/api/vault", reveal_values="body")
    else:
        time.sleep(1.5)
        if tool == "export_curl":
            reply = mcp.call("export_curl", url_contains="/api/vault", reveal_values="body")
        else:
            reply = mcp.call("intercept_log", reveal_values="body")
            mcp.call("intercept_stop")
    assert not reply.failed, reply.text
    _check_body_mode(reply.text)
    plain = mcp.call(tool, **({"filter_url_pattern": "/api/vault", "timeout": 1}
                              if tool == "auth_capture" else
                              {"url_pattern": "/api/vault", "timeout": 1}
                              if tool == "wait_for_request" else
                              {"url_contains": "/api/vault"} if tool == "export_curl" else {}))
    assert "Test Bank AG" not in plain.text


def test_3_cost_on_a_page_of_300_requests(mcp, base):
    """Measured, printed for the report: load time with bodies kept and without."""
    times = {}
    for keep in (False, True, False, True):
        _go(mcp, f"{base}/plain.html")
        assert not mcp.call("network_start", capture_bodies=keep).failed
        started = time.monotonic()
        _go(mcp, f"{base}/many.html")
        assert not mcp.call("wait_for_function", expression="window.done === 300",
                                         timeout_ms=20000).failed
        times.setdefault(keep, []).append(time.monotonic() - started)
    print(f"\n300 fetches: bodies kept {times[True]}, not kept {times[False]}")
    assert min(times[True]) < 15


# ── 5: what a click at a point hit ─────────────────────────────────────────


def test_5_mouse_click_xy_names_what_it_hit(mcp, base):
    _checkout(mcp, base)
    reply = mcp.call("mouse_click_xy", x=80, y=170)
    assert not reply.failed, reply.text
    assert "on button#pay name='pay'" in reply.text and "Pay now" in reply.text, reply.text
    reply = mcp.call("mouse_click_xy", x=100, y=50)
    assert "on iframe#card name='card-expiry'" in reply.text, reply.text
    assert "showing http://127.0.0.1:" in reply.text and "/card.html" in reply.text, reply.text
    assert "at (80,30) inside the frame" in reply.text, reply.text


def test_5_a_form_posted_frame_is_named_by_its_url(mcp, base):
    _checkout(mcp, base)
    mcp.evaluate("post3ds(); 1")
    assert not mcp.call("wait_for", frame_url="/3ds", timeout=10).failed
    reply = mcp.call("mouse_click_xy", x=100, y=260)
    assert "on iframe#threeds" in reply.text and "/3ds?config=" in reply.text, reply.text
    assert "more chars; list_frames(max_url_len=0)" in reply.text, reply.text


# ── 6 and 7: frames ────────────────────────────────────────────────────────


def test_6_wait_for_a_frame_by_its_url(mcp, base):
    _checkout(mcp, base)
    # What the reporter tried: the posted frame has no src, so a selector never matches.
    assert mcp.call("wait_for", selector="iframe[src*='3ds']", timeout=1).failed
    mcp.evaluate("setTimeout(post3ds, 300); 1")
    reply = mcp.call("wait_for", frame_url=r"/3ds\?config=", timeout=10)
    assert not reply.failed and "appeared" in reply.text and "out of process" in reply.text, reply.text
    reply = mcp.call("wait_for", frame_url="never-there", timeout=1)
    assert reply.failed and "child frames seen" in reply.text
    assert mcp.call("wait_for", selector="#pay", frame_url="x").failed


def test_7_list_frames_cuts_long_urls_and_says_so(mcp, base):
    _checkout(mcp, base)
    mcp.evaluate("post3ds(); 1")
    assert not mcp.call("wait_for", frame_url="/3ds", timeout=10).failed
    rows = mcp.call("list_frames").json()
    posted = next(r for r in rows if "/3ds" in (r["url"] or ""))
    assert len(posted["url"]) == 201 and "chars cut" in posted["url_cut"], posted
    rows = mcp.call("list_frames", max_url_len=0).json()
    posted = next(r for r in rows if "/3ds" in (r["url"] or ""))
    assert posted["url"].endswith("signature=SYNTHSIG") and "url_cut" not in posted


# ── 9: wait_for_human with nothing handed over ─────────────────────────────


def test_9_wait_for_human_without_a_handoff_says_so(mcp, base):
    _go(mcp, f"{base}/plain.html")
    mcp.evaluate("setTimeout(() => document.getElementById('p').textContent = 'moved', 300); 1")
    reply = mcp.call("wait_for_human", timeout=5)
    assert not reply.failed and "no handoff was pending" in reply.text, reply.text
    reply = mcp.call("wait_for_human", timeout=0.5)
    assert reply.failed and "no handoff was pending" in reply.text, reply.text
