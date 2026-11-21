"""network_start(save_bodies=...) on real headless Chrome against a local fixture. Needs MCP_E2E=1.

One HTTP server plays every host (--host-resolver-rules), so shop.test and cdn.test are two
sites and a navigation between them swaps the renderer process. Each product page fetches a
60 KB JSON API for itself, as a shop's product page does; the fixture's log says what was asked.

    MCP_E2E=1 uv run pytest tests/test_save_bodies_e2e.py -v
"""
import asyncio
import contextlib
import importlib.util
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

PRODUCT = """<!doctype html><html><head><title>product {n}</title></head><body><h1>product {n}</h1>
<script>
fetch('/api/v4/pdp/get_pc?item={n}&access_token=SECRET{n}', {{headers: {{'x-sign': 'page-only'}}}})
  .then(r => r.json()).then(d => {{ document.title = 'loaded ' + d.data.item.title; }});
</script></body></html>"""
# Navigates away as soon as its fetch resolves: the body's document is gone by the time anyone
# reads it, which lost bodies before durable messages (state-instances-capture.md).
NAVIGATES = """<!doctype html><html><body>navigates
<script>
fetch('/api/v4/pdp/get_pc?item={n}').then(r => r.text()).then(() => {{ location.href = '/landing'; }});
</script></body></html>"""
LANDING = "<!doctype html><html><body>landing</body></html>"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
KINDS = """<!doctype html><html><body>kinds
<script>
fetch('/api/raw?x=1').then(r => r.text());
fetch('/api/octet').then(r => r.text());
fetch('/api/big').then(r => r.text());
fetch('/api/text').then(r => r.text());
fetch('/api/img.png').then(r => r.blob());
</script></body></html>"""
SLOW = """<!doctype html><html><body>slow<script>fetch('/api/slow').catch(() => {{}});</script></body></html>"""
# A cross-site frame that fetches the API: with --site-per-process its own process and target.
FRAMED = """<!doctype html><html><body>framed
<iframe src="http://cdn.test:{p}/p/77"></iframe></body></html>"""
EXTRA = {
    "/framed": ("text/html", FRAMED),
    "/kinds": ("text/html", KINDS),
    "/slow": ("text/html", SLOW),
    "/api/raw": ("application/json", json.dumps({"token": "RAWSECRET", "session_info": None,
                                                  "items": [1, 2, 3]}).encode()),
    "/api/octet": ("application/octet-stream", json.dumps({"via": "octet", "n": 7}).encode()),
    "/api/big": ("application/json", json.dumps({"pad": "z" * 200_000}).encode()),
    "/api/text": ("text/plain", b"plain words, not json"),
    "/api/img.png": ("image/png", PNG),
    "/lists/easylist.txt": ("text/plain", b"[Adblock Plus 2.0]\n! Title: test\n||ads.test^\n"),
    "/lists/easyprivacy.txt": ("text/plain", b"[Adblock Plus 1.1]\n! Title: test\n||track.test^\n"),
}


def _api_body(item: str) -> bytes:
    models = [{"name": f"model-{item}-{i}", "price": 1000 + i, "stock": i, "sku": f"S{i:04d}",
               "description": "d" * 400} for i in range(120)]
    body = {
        "error": None,
        "session_info": None,
        "data": {
            "item": {"title": f"Item {item}", "models": models},
            "shop_detailed": {"name": f"Shop {item}", "rating_star": 4.5, "follower_count": 9},
            "auth": {"token": f"tok-{item}-abcdef", "user": "u1"},
        },
    }
    return json.dumps(body).encode()


class Fixture:
    def __init__(self):
        self.log: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                with fixture.lock:
                    fixture.log.append((host, self.path))
                path, _, query = self.path.partition("?")
                kind, body = "text/html", LANDING.encode()
                if path.startswith("/p/"):
                    body = PRODUCT.format(n=path[3:]).encode()
                elif path.startswith("/navigates/"):
                    body = NAVIGATES.format(n=path[len("/navigates/"):]).encode()
                elif path == "/api/v4/pdp/get_pc":
                    params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
                    kind, body = "application/json", _api_body(params.get("item", "?"))
                elif path in EXTRA:
                    kind, body = EXTRA[path]
                    body = body.format(p=fixture.port).encode() if isinstance(body, str) else body
                elif path == "/api/slow":
                    # Headers, half a body, then nothing: still loading when its tab closes.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b'{"partial": "')
                    self.wfile.flush()
                    time.sleep(20)
                    return
                elif path == "/favicon.ico":
                    kind, body = "image/x-icon", b""
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            request_queue_size = 128

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def api_calls(self) -> int:
        with self.lock:
            return sum(1 for _h, p in self.log if p.startswith("/api/v4/pdp/get_pc"))

    def api_calls_any(self) -> int:
        with self.lock:
            return sum(1 for _h, p in self.log if p.startswith("/api/"))


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from mcp_stealth_chrome.tools import monitoring
    monkeypatch.setattr(monitoring, "EXPORT_DIR", tmp_path / "exports")
    fx = Fixture()
    yield fx
    fx.server.shutdown()


ARGS = ["--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost"]
SAVE = [{"url_pattern": "*/api/v4/pdp/get_pc*", "name": "pdp",
         "extract": ["data.item.title", "data.item.models[].{name,price,stock}",
                     "data.shop_detailed.{name,rating_star}", "data.auth.token", "session_info",
                     "data.nothing.here"]}]


async def _until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


def test_twelve_pages_twelve_records(fixture):
    """12 page loads, each fetching its own 60 KB JSON: by navigate across two sites, a reload,
    a tab opened later, a page that navigates as its fetch resolves and a popup that opens the
    API itself. 12 of 12 are saved with the fields asked for, masked, and the tool answers stay
    small."""
    p = fixture.port
    answers: list[str] = []

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           auto_verify=False, extra_args=ARGS)
        assert not out.startswith("Error"), out
        try:
            started = await tool("network_start")(save_bodies=SAVE)
            answers.append(started)
            assert not started.startswith("Error"), started
            for n in range(1, 9):  # 8 navigations, alternating sites
                host = "shop.test" if n % 2 else "cdn.test"
                await tool("navigate")(f"http://{host}:{p}/p/{n}")
                assert await _until(lambda: fixture.api_calls() >= n), n
            await tool("reload")()  # 9: the same page again
            assert await _until(lambda: fixture.api_calls() >= 9)
            # Bounded: a tab held for ever by the saver must fail this test, not hang it.
            await asyncio.wait_for(tool("tab_new")(url=f"http://cdn.test:{p}/p/10"), 30)  # 10
            assert await _until(lambda: fixture.api_calls() >= 10)
            await tool("navigate")(f"http://shop.test:{p}/navigates/11")  # 11
            assert await _until(lambda: fixture.api_calls() >= 11)
            # 12: the page opens the API URL itself in a new tab: the body is that tab's first
            # document response.
            await asyncio.wait_for(tool("evaluate")(
                f"window.open('http://shop.test:{p}/api/v4/pdp/get_pc?item=12') && 1"), 30)
            assert await _until(lambda: fixture.api_calls() >= 12)
            plain = await tool("network_start")()  # out of habit: saving must go on
            answers.append(plain)
            assert "body saving continues" in plain, plain

            async def saved() -> dict:
                return json.loads(await tool("network_save_status")())
            deadline = time.monotonic() + 15
            status = await saved()
            while time.monotonic() < deadline and status["names"]["pdp"]["saved"] < 12:
                await asyncio.sleep(0.2)
                status = await saved()
            answers.append(json.dumps(status))
            url = await tool("get_url")()
            return status, url
        finally:
            closed = await asyncio.wait_for(tool("browser_close")(), 60)
            final = json.loads(await tool("network_save_status")())
            assert "Browser closed" in closed, closed
            fixture.final = final

    status, url = asyncio.run(scenario())
    pdp = status["names"]["pdp"]
    assert pdp["matched"] == 12 and pdp["saved"] == 12, pdp
    assert pdp["pending"] == 0 and pdp["not_saved"] == {}, pdp
    assert status["state"] == "saving" and status["mode"] == "masked", status
    assert fixture.final["state"] == "stopped", fixture.final
    assert fixture.final["names"]["pdp"]["saved"] == 12, fixture.final
    assert "closed" in fixture.final["stop_reason"], fixture.final

    path = pdp["files"][0]["path"]
    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == 12, len(lines)
    if os.name != "nt":  # Windows keeps no POSIX mode bits (helpers.append_private_line)
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    records = [json.loads(line) for line in lines]
    items = sorted(int(r["body"]["data.item.title"].split()[1]) for r in records)
    assert items == [1, 2, 3, 4, 5, 6, 7, 8, 8, 10, 11, 12], items  # 8 twice: the reload
    for record in records:
        meta, body = record["meta"], record["body"]
        assert set(body) == {p for p in SAVE[0]["extract"]}, body.keys()
        assert len(body["data.item.models[].{name,price,stock}"]) == 120
        assert body["data.item.models[].{name,price,stock}"][0].keys() == {"name", "price", "stock"}
        assert body["data.shop_detailed.{name,rating_star}"]["rating_star"] == 4.5
        assert body["data.auth.token"].startswith("*** ("), body["data.auth.token"]
        assert body["session_info"] is None
        assert body["data.nothing.here"] is None and meta["missing"] == ["data.nothing.here"]
        assert meta["status"] == 200 and meta["mime"] == "application/json"
        assert "SECRET" not in line_of(record) and "tok-" not in line_of(record)
        assert meta["ts"][:4].isdigit()
    navigated = [r for r in records if r["body"]["data.item.title"] == "Item 11"]
    assert len(navigated) == 1 and navigated[0]["meta"]["page_url"].endswith("/navigates/11")
    assert url.endswith("/landing"), url
    popup = [r for r in records if r["body"]["data.item.title"] == "Item 12"]
    assert len(popup) == 1 and "/api/v4/pdp/get_pc?item=12" in popup[0]["meta"]["page_url"]
    total = sum(len(a.encode()) for a in answers)
    assert total < 2048, (total, answers)


def line_of(record: dict) -> str:
    return json.dumps(record)


async def _call_bound(name: str, args: dict) -> str:
    """A tool through the server's own call path, so result filters (notices) run."""
    from mcp_stealth_chrome import server
    tool_obj = server.mcp._tool_manager._tools[name]
    result = await server.mcp._call_bound(name, args, None, tool_obj)
    return "".join(getattr(c, "text", "") for c in (result[0] if isinstance(result, tuple) else result))


def test_every_kind_of_body_is_saved_or_counted_and_stopping_says_so(fixture, monkeypatch):
    """JSON, JSON Chrome hands over in base64, text, an image, a body over the cap and one
    still loading when its tab closes: each is saved or counted with its reason. Then
    save_bodies=[] stops saving with the final counts, and a later start keeps them under
    previous."""
    from mcp_stealth_chrome import server
    monkeypatch.setattr(server, "CAPTURE_BODY_MAX_BYTES", 100_000)
    p = fixture.port
    save = [{"url_pattern": "*/api/raw*", "name": "raw"},
            {"url_pattern": "*/api/*", "name": "rest"}]

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           auto_verify=False, extra_args=ARGS)
        assert not out.startswith("Error"), out
        try:
            started = await tool("network_start")(save_bodies=save)
            assert "raw: */api/raw*" in started and "rest: */api/*" in started, started
            await tool("navigate")(f"http://shop.test:{p}/kinds")
            await tool("tab_new")(url=f"http://shop.test:{p}/slow")
            await asyncio.sleep(1.5)
            await tool("tab_close")()

            async def status():
                return json.loads(await tool("network_save_status")())
            deadline = time.monotonic() + 15
            now = await status()
            while time.monotonic() < deadline and any(
                    item["pending"] for item in now["names"].values()):
                await asyncio.sleep(0.2)
                now = await status()
            stopped = await tool("network_start")(save_bodies=[])
            after_stop = await status()
            calls = fixture.api_calls_any()
            await tool("navigate")(f"http://shop.test:{p}/kinds")
            assert await _until(lambda: fixture.api_calls_any() >= calls + 5)
            later = await status()
            again = await tool("network_start")(save_bodies=save[:1])
            replaced = await tool("network_start")(save_bodies=save[:1])
            last = await status()
            return now, stopped, after_stop, later, again, replaced, last
        finally:
            await tool("browser_close")()

    now, stopped, after_stop, later, again, replaced, last = asyncio.run(scenario())
    raw, rest = now["names"]["raw"], now["names"]["rest"]
    assert raw["matched"] == 1 and raw["saved"] == 1, raw
    assert rest["matched"] == 5, rest
    assert rest["saved"] == 2 and rest["not_saved"] == {"too_large": 1, "binary": 1, "tab_closed": 1}, rest
    for item in (raw, rest):
        assert item["matched"] == item["saved"] + item["pending"] + sum(item["not_saved"].values())
    records = [json.loads(line) for line in open(rest["files"][0]["path"], encoding="utf-8")]
    by_kind = {r["meta"]["mime"]: r for r in records}
    octet = by_kind["application/octet-stream"]
    assert octet["body"] == {"via": "octet", "n": 7}, octet
    text = by_kind["text/plain"]
    assert text["meta"]["json"] is False and text["body"] == "plain words, not json", text
    raw_record = json.loads(open(raw["files"][0]["path"], encoding="utf-8").readline())
    assert raw_record["body"]["token"].startswith("*** (") and raw_record["body"]["session_info"] is None
    assert "body saving stopped" in stopped and "rest 2 of 5 saved, 3 not saved" in stopped, stopped
    assert after_stop["state"] == "stopped" and after_stop["stop_reason"] == "network_start(save_bodies=[])"
    assert later["names"]["rest"]["matched"] == 5, "a response after the stop was counted"
    assert "The earlier saving run ended" not in again, again  # the earlier one had stopped already
    assert "The earlier saving run ended (raw 0 of 0 saved)" in replaced, replaced
    assert last["state"] == "saving" and last["previous"]["stop_reason"].startswith("replaced"), last


@pytest.mark.parametrize("how", ["stopped", "hung"])
def test_a_dead_saver_thread_never_holds_a_tab(fixture, how):
    """Fail open: with the saver's thread dead or hung, a popup opened then (a target held for
    the debugger) still loads, its opener goes on, and the status and the next tool result
    say saving stopped and why."""
    from mcp_stealth_chrome import body_saver, server
    p = fixture.port

    async def scenario():
        out = await tool("browser_launch")(url=f"http://shop.test:{p}/p/1", headless=True,
                                           persistent=False, auto_verify=False, extra_args=ARGS)
        assert not out.startswith("Error"), out
        try:
            started = await tool("network_start")(save_bodies=SAVE)
            assert not started.startswith("Error"), started
            tab = server.BrowserState.active_tab()
            loop = body_saver._RUNNER.loop
            if how == "stopped":
                loop.call_soon_threadsafe(loop.stop)
            else:
                loop.call_soon_threadsafe(time.sleep, 20)
            calls = fixture.api_calls()
            opened = asyncio.ensure_future(tab.evaluate(
                f"window.open('http://cdn.test:{p}/p/2') && 1"))
            popped = await _until(lambda: fixture.api_calls() > calls, 20)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(opened, 10)

            async def alive():
                try:
                    return await asyncio.wait_for(tab.evaluate("1+1"), 2) == 2
                except Exception:
                    return False
            opener = await _until_async(alive, 10)
            text = await _call_bound("get_url", {})
            status = json.loads(await tool("network_save_status")())
            return popped, opener, text, status
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tool("browser_close")(), 30)

    popped, opener, text, status = asyncio.run(scenario())
    assert popped, "a popup opened after the saver died was never released"
    assert opener, "the opener stayed frozen behind its popup"
    assert status["state"] == "stopped" and "watchdog" in status["stop_reason"], status
    assert "body saving stopped" in text, text
    for item in status["names"].values():
        assert item["pending"] == 0
        assert item["matched"] == item["saved"] + sum(item["not_saved"].values()), item


def test_a_connection_lost_while_a_popup_is_held_still_lets_it_load(fixture, monkeypatch):
    """The saver's connection ends while a popup waits for its setup, and the browser lives on:
    the end path starts every target over a new connection."""
    from mcp_stealth_chrome import body_saver, server
    p = fixture.port
    monkeypatch.setenv("MCP_STEALTH_BODIES_STALL", "60")  # the end path, not the watchdog
    hold = {"on": False}
    real = body_saver.Saver._on_attached

    async def slow(self, params):
        if hold["on"] and (params.get("targetInfo") or {}).get("type") == "page":
            await asyncio.sleep(60)  # the popup stays held for this client
        return await real(self, params)

    monkeypatch.setattr(body_saver.Saver, "_on_attached", slow)

    async def scenario():
        out = await tool("browser_launch")(url=f"http://shop.test:{p}/p/1", headless=True,
                                           persistent=False, auto_verify=False, extra_args=ARGS)
        assert not out.startswith("Error"), out
        try:
            await tool("network_start")(save_bodies=SAVE)
            tab = server.BrowserState.active_tab()
            saver = body_saver.saver_for(server.BrowserState.browser)
            hold["on"] = True
            calls = fixture.api_calls()
            opened = asyncio.ensure_future(tab.evaluate(
                f"window.open('http://cdn.test:{p}/p/2') && 1"))
            await asyncio.sleep(1.5)
            held = fixture.api_calls() == calls
            saver._shut_socket()  # the connection ends; the browser does not
            popped = await _until(lambda: fixture.api_calls() > calls, 15)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(opened, 10)
            text = await _call_bound("get_url", {})
            return held, popped, text
        finally:
            hold["on"] = False
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tool("browser_close")(), 30)

    held, popped, text = asyncio.run(scenario())
    assert held, "the popup was not held, so this proves nothing"
    assert popped, "the popup held for the lost connection never loaded"
    assert "body saving stopped" in text, text


needs_engine = pytest.mark.skipif(importlib.util.find_spec("adblock") is None,
                                  reason="needs the adblock extra: uv sync --extra adblock")


@needs_engine
def test_saving_and_block_ads_hold_targets_side_by_side(fixture, tmp_path, monkeypatch):
    """Both clients hold every new tab and popup and each lets it go: a tab and a popup load,
    their bodies are saved, and blocking goes on."""
    p = fixture.port
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DIR", str(tmp_path / "adblock"))
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_LIST_URLS",
                       f"easylist=http://127.0.0.1:{p}/lists/easylist.txt,"
                       f"easyprivacy=http://127.0.0.1:{p}/lists/easyprivacy.txt")
    from mcp_stealth_chrome import adblock
    adblock._ENGINE_CACHE.update(key=None, engine=None)

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False, extra_args=ARGS)
        assert "block_ads: on" in out, out
        try:
            await tool("network_start")(save_bodies=SAVE)
            await tool("tab_new")(url=f"http://shop.test:{p}/p/1")
            assert await _until(lambda: fixture.api_calls() >= 1)
            await tool("evaluate")(f"window.open('http://cdn.test:{p}/p/2') && 1")
            assert await _until(lambda: fixture.api_calls() >= 2)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = json.loads(await tool("network_save_status")())
                if status["names"]["pdp"]["saved"] >= 2:
                    break
                await asyncio.sleep(0.2)
            server_status = json.loads(await tool("server_status")())
            return status, server_status
        finally:
            await tool("browser_close")()

    status, server_status = asyncio.run(scenario())
    assert status["names"]["pdp"]["saved"] == 2 and status["names"]["pdp"]["matched"] == 2, status
    assert server_status["block_ads"]["browsers"][0]["active"], server_status["block_ads"]


async def _until_async(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.2)
    return await predicate()


def test_a_cross_site_frame_is_covered(fixture):
    """An out-of-process iframe is a target of its own: held and set up like a tab, so the API
    it fetches is saved, with the frame's document as page_url."""
    p = fixture.port

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           auto_verify=False, extra_args=ARGS + ["--site-per-process"])
        assert not out.startswith("Error"), out
        try:
            await tool("network_start")(save_bodies=SAVE)
            await tool("navigate")(f"http://shop.test:{p}/framed")
            assert await _until(lambda: fixture.api_calls() >= 1, 15)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = json.loads(await tool("network_save_status")())
                if status["names"]["pdp"]["saved"] >= 1:
                    break
                await asyncio.sleep(0.2)
            return status
        finally:
            await tool("browser_close")()

    status = asyncio.run(scenario())
    pdp = status["names"]["pdp"]
    assert pdp["matched"] == 1 and pdp["saved"] == 1, pdp
    assert status["targets_covered"].get("iframe", 0) >= 1, status
    assert pdp["last"]["meta"]["page_url"].endswith("/p/77"), pdp["last"]
