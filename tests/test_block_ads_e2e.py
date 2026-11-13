"""block_ads on real Chrome against a local fixture. Needs MCP_E2E=1.

One HTTP server plays every host: Chrome resolves every name to 127.0.0.1
(--host-resolver-rules), so a request for pagead2.googlesyndication.com reaches the fixture
and the fixture's log is the ground truth of what left the browser. The filter lists are
served by the same server and downloaded through the real download path into a temporary
folder. Their rules are copied verbatim from EasyList and EasyPrivacy, apart from one
exception rule of the test's own.

    MCP_E2E=1 uv run pytest tests/test_block_ads_e2e.py -v
"""
import asyncio
import contextlib
import importlib.util
import json
import os
import pathlib
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")
# The adblock package is an optional extra; block_ads cannot block without it.
needs_engine = pytest.mark.skipif(
    importlib.util.find_spec("adblock") is None,
    reason="needs the adblock extra: uv sync --extra adblock")

EASYLIST = """[Adblock Plus 2.0]
! Title: EasyList (lines copied from the real list, for a test)
||pagead2.googlesyndication.com^
||g.doubleclick.net^
||adnxs.com^
||amazon-adsystem.com^
! not from EasyList: an exception, to prove @@ rules are honoured
@@/allowed.js$script,domain=news.test
"""
EASYPRIVACY = """[Adblock Plus 1.1]
! Title: EasyPrivacy (lines copied from the real list, for a test)
||googletagmanager.com^
/collect?tid=
||criteo.com^$third-party
||google-analytics.com^$script,third-party,xmlhttprequest
"""

PAGE = """<!doctype html><html><head><title>news</title>
<link rel="stylesheet" href="/site.css">
<script src="/app.js"></script>
<script src="http://pagead2.googlesyndication.com:{p}/pagead/js/adsbygoogle.js"></script>
<script src="http://www.googletagmanager.com:{p}/gtm.js?id=GTM-TEST"></script>
<script src="http://ib.adnxs.com:{p}/allowed.js"></script>
</head><body><h1>Article</h1><p>Text the reader came for.</p>
<img src="/logo.png">
<img src="/collect?tid=UA-1&cid=2">
<img src="http://static.criteo.com:{p}/pixel.gif">
<iframe src="http://securepubads.g.doubleclick.net:{p}/ad-frame.html"></iframe>
<iframe src="http://widget.test:{p}/widget.html"></iframe>
<script>
fetch('/api/data').catch(() => {{}});
fetch('http://www.google-analytics.com:{p}/g/collect?v=2&from=page').catch(() => {{}});
new Worker('/worker.js');
</script></body></html>"""
WIDGET = """<!doctype html><html><body>widget<script src="/widget.js"></script>
<img src="http://ib.adnxs.com:{p}/px?from=widget"></body></html>"""
WORKER = """fetch('/api/worker-ok').catch(() => {{}});
fetch('http://www.google-analytics.com:{p}/g/collect?from=worker').catch(() => {{}});"""
SECOND = """<!doctype html><html><body>second
<script src="http://pagead2.googlesyndication.com:{p}/pagead/js/second.js"></script>
<img src="/second.png"></body></html>"""

# Liveness: 40 images the server answers slowly, plus a chain the page walks one image at a
# time, so requests are still being made when the blocker is killed.
CHAIN = """<!doctype html><html><body>chain
{imgs}
<script>
let i = 0;
function next() {{
  if (i >= 30) {{ fetch('/chain-done').catch(() => {{}}); return; }}
  const im = new Image();
  im.onload = im.onerror = () => {{ i++; next(); }};
  im.src = '/chain/' + i + '.png';
}}
next();
</script></body></html>"""
# The page opens a popup itself (no tool can, once the server is dead) and then says it is alive.
KILLED = """<!doctype html><html><body>killed
<script>
let i = 0;
function next() {{
  if (i >= 40) {{ fetch('/chain-done').catch(() => {{}}); return; }}
  const im = new Image();
  im.onload = im.onerror = () => {{ i++; next(); }};
  im.src = '/chain/' + i + '.png';
}}
next();
setTimeout(() => {{
  window.open('http://widget.test:{p}/second.html?popup');
  fetch('/opener-alive').catch(() => {{}});
}}, {delay});
</script></body></html>"""
LINKS = """<!doctype html><html><body>
<a id="same" href="http://securepubads.g.doubleclick.net:{p}/landing.html?via=link">same tab</a>
<a id="blank" target="_blank" href="http://pagead2.googlesyndication.com:{p}/landing.html?via=blank">new tab</a>
</body></html>"""
WORKERS = """<!doctype html><html><body>workers<script>
new Worker('/w-outer.js');
new SharedWorker('/w-shared.js');
navigator.serviceWorker.register('/w-service.js');
</script></body></html>"""
W_OUTER = "new Worker('/w-inner.js'); fetch('/api/outer-ok').catch(() => {{}});"
W_INNER = """fetch('/api/inner-ok').catch(() => {{}});
fetch('http://www.google-analytics.com:{p}/g/collect?from=nested').catch(() => {{}});"""
W_SHARED = """fetch('/api/shared-ok').catch(() => {{}});
fetch('http://www.google-analytics.com:{p}/g/collect?from=shared').catch(() => {{}});"""
W_SERVICE = """self.addEventListener('install', () => {{
  fetch('/api/service-ok').catch(() => {{}});
  fetch('http://www.google-analytics.com:{p}/g/collect?from=service').catch(() => {{}});
}});"""
EXTRA = {
    "/chain.html": ("text/html", CHAIN.replace("{imgs}", "".join(
        f'<img src="/slow/{i}.png">' for i in range(40)))),
    "/links.html": ("text/html", LINKS),
    "/killed-early.html": ("text/html", KILLED.replace("{delay}", "1500")),
    "/killed-late.html": ("text/html", KILLED.replace("{delay}", "300")),
    "/kept.html": ("text/html", KILLED.replace("{delay}", "4000")),
    "/workers.html": ("text/html", WORKERS),
    "/w-outer.js": ("text/javascript", W_OUTER),
    "/w-inner.js": ("text/javascript", W_INNER),
    "/w-shared.js": ("text/javascript", W_SHARED),
    "/w-service.js": ("text/javascript", W_SERVICE),
    "/landing.html": ("text/html", "<!doctype html><html><body>landing</body></html>"),
}

ALLOWED = {
    ("news.test", "/"), ("news.test", "/site.css"), ("news.test", "/app.js"),
    ("ib.adnxs.com", "/allowed.js"), ("news.test", "/logo.png"), ("widget.test", "/widget.html"),
    ("widget.test", "/widget.js"), ("news.test", "/api/data"), ("news.test", "/worker.js"),
    ("news.test", "/api/worker-ok"),
}
BLOCKED = {
    ("pagead2.googlesyndication.com", "/pagead/js/adsbygoogle.js"),
    ("www.googletagmanager.com", "/gtm.js?id=GTM-TEST"),
    ("news.test", "/collect?tid=UA-1&cid=2"),
    ("static.criteo.com", "/pixel.gif"),
    ("securepubads.g.doubleclick.net", "/ad-frame.html"),
    ("www.google-analytics.com", "/g/collect?v=2&from=page"),
    ("ib.adnxs.com", "/px?from=widget"),
    ("www.google-analytics.com", "/g/collect?from=worker"),
}


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
                p = fixture.port
                path = self.path.split("?")[0]
                kind, body = "text/plain", b"/* ok */"
                if path == "/lists/easylist.txt":
                    body = EASYLIST.encode()
                elif path == "/lists/easyprivacy.txt":
                    body = EASYPRIVACY.encode()
                elif host == "news.test" and path == "/":
                    kind, body = "text/html", PAGE.format(p=p).encode()
                elif path == "/second.html":
                    kind, body = "text/html", SECOND.format(p=p).encode()
                elif path in ("/widget.html", "/ad-frame.html"):
                    kind, body = "text/html", WIDGET.format(p=p).encode()
                elif path == "/worker.js":
                    kind, body = "text/javascript", WORKER.format(p=p).encode()
                elif path in EXTRA:
                    kind, body = EXTRA[path][0], EXTRA[path][1].format(p=p).encode()
                elif path == "/redirect-to-ad":
                    self.send_response(302)
                    self.send_header("Location",
                                     f"http://securepubads.g.doubleclick.net:{p}/landing.html?via=redirect")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif path.startswith(("/slow/", "/chain/")):
                    time.sleep(0.1 if path.startswith("/slow/") else 0.05)
                    kind = "image/png"
                elif path.endswith(".js"):
                    kind = "text/javascript"
                elif path.endswith(".css"):
                    kind = "text/css"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            # The default backlog of 5 refuses some of Chrome's parallel connections on Windows,
            # and a refused image then reads as a request that never left the browser.
            request_queue_size = 128

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def has(self, host: str, path: str) -> bool:
        with self.lock:
            return any(h == host and p.startswith(path) for h, p in self.log)

    def seen(self) -> set:
        with self.lock:
            return {e for e in self.log if e[1] != "/favicon.ico" and not e[1].startswith("/lists/")}

    def clear(self):
        with self.lock:
            self.log.clear()


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    fx = Fixture()
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DIR", str(tmp_path / "adblock"))
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_LIST_URLS",
                       f"easylist=http://127.0.0.1:{fx.port}/lists/easylist.txt,"
                       f"easyprivacy=http://127.0.0.1:{fx.port}/lists/easyprivacy.txt")
    from mcp_stealth_chrome import adblock
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    yield fx
    fx.server.shutdown()


def _args(isolate: bool) -> list[str]:
    args = ["--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost"]
    if isolate:
        # Every cross-site frame in its own process and CDP target: the case auto-attach covers.
        args.append("--site-per-process")
    return args


async def _wait_for(fx: Fixture, wanted: set, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not wanted <= fx.seen():
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)  # anything that would wrongly get through has time to arrive


def _blocker_report() -> dict:
    from mcp_stealth_chrome import adblock, server
    blocker = adblock.blocker_for(server.BrowserState.browser)
    assert blocker is not None, f"no live blocker for the browser: {adblock.status()}"
    return blocker.report()


@needs_engine
@pytest.mark.parametrize("isolate", [False, True], ids=["shared-process", "site-per-process"])
def test_blocks_exactly_the_listed_requests(fixture, isolate):
    url = f"http://news.test:{fixture.port}/"

    async def scenario():
        out = await tool("browser_launch")(url=url, headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(isolate))
        assert not out.startswith("Error"), out
        try:
            assert "block_ads: on" in out, out
            await _wait_for(fixture, ALLOWED)
            seen = fixture.seen()
            report = _blocker_report()
            status = json.loads(await tool("server_status")())
            text = await tool("get_text")()
            return seen, report, status, text
        finally:
            await tool("browser_close")()

    seen, report, status, text = asyncio.run(scenario())
    assert seen == ALLOWED, {"missing": ALLOWED - seen, "unexpected": seen - ALLOWED, "report": report}
    assert report["blocked"] == len(BLOCKED), report
    blocked_urls = {r["url"].split(f":{fixture.port}", 1)[-1] for r in report["recent_blocked"]}
    assert len(report["recent_blocked"]) == min(10, len(BLOCKED))
    assert blocked_urls <= {path for _host, path in BLOCKED}, report["recent_blocked"]
    assert "Text the reader came for." in text
    assert status["block_ads"]["browsers"][0]["blocked"] == len(BLOCKED)
    if isolate:
        # The widget frame was its own target, and its own request was still blocked.
        assert report["targets_covered"].get("iframe", 0) >= 1, report


@needs_engine
def test_new_tabs_popups_and_the_page_itself(fixture):
    """A tab opened later, a popup and a reload are covered too; the page the caller navigates
    to is never blocked, even on an ad domain."""
    p = fixture.port

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            fixture.clear()
            await tool("tab_new")(url=f"http://news.test:{p}/second.html")
            await _wait_for(fixture, {("news.test", "/second.png")})
            tab_seen = fixture.seen()
            fixture.clear()
            await tool("evaluate")(f"window.open('http://widget.test:{p}/second.html') && 1")
            await _wait_for(fixture, {("widget.test", "/second.png")})
            popup_seen = fixture.seen()
            fixture.clear()
            nav = await tool("navigate")(f"http://securepubads.g.doubleclick.net:{p}/ad-frame.html")
            await _wait_for(fixture, {("securepubads.g.doubleclick.net", "/ad-frame.html")}, 5)
            direct_seen = fixture.seen()
            return tab_seen, popup_seen, direct_seen, nav, _blocker_report()
        finally:
            await tool("browser_close")()

    tab_seen, popup_seen, direct_seen, nav, report = asyncio.run(scenario())
    assert tab_seen == {("news.test", "/second.html"), ("news.test", "/second.png")}, tab_seen
    assert popup_seen == {("widget.test", "/second.html"), ("widget.test", "/second.png")}, popup_seen
    assert ("securepubads.g.doubleclick.net", "/ad-frame.html") in direct_seen, (direct_seen, nav)
    assert not nav.startswith("Error"), nav
    assert report["blocked"] >= 3, report  # second.js twice, the widget pixel on the ad page


@needs_engine
def test_intercept_start_shares_the_fetch_domain(fixture):
    """intercept_start on a block_ads browser: no ad request is logged as passed, and stopping
    the interception leaves ad blocking on."""
    url = f"http://news.test:{fixture.port}/"

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            started = await tool("intercept_start")(action="observe")
            assert "block_ads is on" in started, started
            fixture.clear()
            await tool("navigate")(url)
            await _wait_for(fixture, ALLOWED)
            during = fixture.seen()
            log = json.loads((await tool("intercept_log")(limit=500)))
            await tool("intercept_stop")()
            fixture.clear()
            await tool("reload")()
            await _wait_for(fixture, ALLOWED)
            after = fixture.seen()
            return during, log, after
        finally:
            await tool("browser_close")()

    during, log, after = asyncio.run(scenario())
    assert during == ALLOWED, {"missing": ALLOWED - during, "unexpected": during - ALLOWED}
    assert after == ALLOWED, {"missing": ALLOWED - after, "unexpected": after - ALLOWED}
    passed = [r["url"] for r in log["requests"] if r["outcome"] == "passed"]
    for _host, path in BLOCKED:
        assert not any(u.endswith(path) and _host in u for u in passed), (path, passed)
    assert "block_ads" in log


@needs_engine
def test_no_list_means_off_and_the_launch_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DIR", str(tmp_path / "adblock"))
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_LIST_URLS", "easylist=http://127.0.0.1:9/easylist.txt")
    from mcp_stealth_chrome import adblock
    adblock._ENGINE_CACHE.update(key=None, engine=None)

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False)
        try:
            return out, await tool("evaluate")("1+1")
        finally:
            await tool("browser_close")()

    out, value = asyncio.run(scenario())
    assert not out.startswith("Error"), out
    assert "block_ads: OFF, no filter list is available" in out, out
    assert "2" in value


# ── review fixes: liveness, one Fetch owner, main documents, workers, partial lists ──────

async def _until(check, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.1)
    return False


def _kill_adblock_thread(how: str) -> None:
    """Stop the adblock event loop (its thread ends, its socket stays open and unread), or
    hang it (the thread lives but answers nothing for 20 s)."""
    from mcp_stealth_chrome import adblock
    runner = getattr(adblock, "_RUNNER", None)
    loop = runner.loop if runner is not None else adblock._LOOP
    assert loop is not None and loop.is_running()
    if how == "stopped":
        loop.call_soon_threadsafe(loop.stop)
    else:
        loop.call_soon_threadsafe(time.sleep, 20)


@needs_engine
@pytest.mark.parametrize("how", ["stopped", "hung"])
def test_a_dead_adblock_thread_never_holds_the_page(fixture, how):
    """Fail open: with the blocker's thread dead or hung in the middle of a page load, the page
    still finishes, a popup opened meanwhile (a target held for the debugger) still loads, and
    server_status says block_ads ended and why."""
    from nodriver import cdp

    from mcp_stealth_chrome import server
    p = fixture.port

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            tab = server.BrowserState.active_tab()
            await tab.send(cdp.page.navigate(f"http://news.test:{p}/chain.html"))

            async def started():
                return fixture.has("news.test", "/chain/2.png")
            assert await _until(started, 10), "the page never started loading"
            _kill_adblock_thread(how)
            # A popup now is a new target held for the debugger by a blocker that no longer
            # answers; the opener's window.open waits for it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tab.evaluate(
                    f"window.open('http://widget.test:{p}/second.html') && 1"), 15)

            async def complete():
                try:
                    return await asyncio.wait_for(tab.evaluate("document.readyState"), 3) == "complete"
                except Exception:
                    return False
            loaded = await _until(complete, 20)

            async def chain_done():
                return fixture.has("news.test", "/chain-done")
            walked = await _until(chain_done, 10)

            async def popup_loaded():
                return fixture.has("widget.test", "/second.png")
            popped = await _until(popup_loaded, 10)
            status = json.loads(await asyncio.wait_for(tool("server_status")(), 15))
            return loaded, walked, popped, status
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tool("browser_close")(), 30)

    loaded, walked, popped, status = asyncio.run(scenario())
    assert loaded, "the page never finished loading after the adblock thread died"
    assert walked, "requests made after the death were held"
    assert popped, "a popup opened after the death was never released"
    ended = status["block_ads"].get("ended") or []
    assert any("watchdog" in (e.get("ended") or "") for e in ended), status["block_ads"]
    assert not status["block_ads"]["browsers"], "a dead blocker is still listed as active"


@needs_engine
def test_intercept_start_rides_the_blockers_fetch_session(fixture, monkeypatch):
    """One Fetch owner per request: with block_ads on, intercept_start enables no Fetch domain
    of its own, every decision is made on the adblock thread, each request is counted and
    answered once, and a mock still answers."""
    import threading as _threading

    import nodriver.cdp.fetch as cdp_fetch

    from mcp_stealth_chrome import adblock
    enables, threads = [], set()
    real_enable, real_decide = cdp_fetch.enable, adblock.Blocker.decide

    def spy_enable(*a, **kw):
        enables.append(kw)
        return real_enable(*a, **kw)

    def spy_decide(self, *a, **kw):
        threads.add(_threading.current_thread().name)
        return real_decide(self, *a, **kw)

    monkeypatch.setattr(cdp_fetch, "enable", spy_enable)
    monkeypatch.setattr(adblock.Blocker, "decide", spy_decide)
    url = f"http://news.test:{fixture.port}/"

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            started = await tool("intercept_start")(url_pattern="*/api/data", action="mock",
                                                    mock_body='{"mocked": 1}')
            assert not started.startswith("Error"), started
            fixture.clear()
            await tool("navigate")(url)
            await _wait_for(fixture, ALLOWED - {("news.test", "/api/data")})
            mocked = await tool("evaluate")("fetch('/api/data').then(r => r.text())")
            log = json.loads(await tool("intercept_log")(limit=500))
            report = _blocker_report()
            during = fixture.seen()
            await tool("intercept_stop")()
            fixture.clear()
            await tool("reload")()
            await _wait_for(fixture, ALLOWED)
            return mocked, log, report, during, fixture.seen()
        finally:
            await tool("browser_close")()

    mocked, log, report, during, after = asyncio.run(scenario())
    assert not enables, f"intercept_start enabled a second Fetch domain: {enables}"
    assert threads == {"mcp-stealth-adblock"}, threads
    assert '"mocked": 1' in mocked, mocked
    assert ("news.test", "/api/data") not in during, "the mock did not answer"
    assert during == ALLOWED - {("news.test", "/api/data")}, during
    assert after == ALLOWED, {"missing": ALLOWED - after, "unexpected": after - ALLOWED}
    assert report["blocked"] == len(BLOCKED), report
    assert "answer_errors" not in report, report
    outcomes = [r["outcome"] for r in log["requests"]]
    assert outcomes.count("mocked") == 2, log  # the page's fetch and ours
    assert log["counts"]["seen"] == log["counts"]["mocked"], log  # only */api/data is targeted


@needs_engine
def test_top_level_documents_on_ad_domains_always_load(fixture):
    """What the caller or the person opens is never blocked, whichever way it is opened, and
    a page target's main frame id is its target id (what the top-level check relies on)."""
    from nodriver import cdp

    from mcp_stealth_chrome import server
    p = fixture.port
    ad = "securepubads.g.doubleclick.net"

    async def scenario():
        out = await tool("browser_launch")(url=f"http://news.test:{p}/links.html", headless=True,
                                           persistent=False, block_ads=True, auto_verify=False,
                                           extra_args=_args(True))
        assert "block_ads: on" in out, out
        try:
            got = {}
            await tool("click")(selector="#blank")
            got["target=_blank"] = await _until(
                lambda: _async(fixture.has("pagead2.googlesyndication.com", "/landing.html?via=blank")), 10)
            await tool("tab_select")(index=0)
            await tool("click")(selector="#same")
            got["link"] = await _until(lambda: _async(fixture.has(ad, "/landing.html?via=link")), 10)
            await tool("navigate")(f"http://news.test:{p}/redirect-to-ad")
            got["redirect"] = await _until(lambda: _async(fixture.has(ad, "/landing.html?via=redirect")), 10)
            await tool("tab_new")(url=f"http://{ad}:{p}/landing.html?via=tab_new")
            got["tab_new"] = await _until(lambda: _async(fixture.has(ad, "/landing.html?via=tab_new")), 10)
            await tool("evaluate")(f"window.open('http://{ad}:{p}/landing.html?via=popup') && 1")
            got["popup"] = await _until(lambda: _async(fixture.has(ad, "/landing.html?via=popup")), 10)
            await server._refresh_tabs()
            mismatched = []
            for t in server.BrowserState.tabs:
                tree = await t.send(cdp.page.get_frame_tree())
                if str(tree.frame.id_) != str(t.target.target_id):
                    mismatched.append((str(tree.frame.id_), str(t.target.target_id)))
            return got, mismatched, _blocker_report()
        finally:
            await tool("browser_close")()

    got, mismatched, report = asyncio.run(scenario())
    assert all(got.values()), got
    assert not mismatched, mismatched
    assert not report.get("main_frame_mismatch"), report


async def _async(value):
    return value


@needs_engine
def test_workers_of_every_kind_are_covered(fixture):
    """Ad requests from a worker inside a worker, a shared worker and a service worker are
    blocked too; their own first-party requests go through."""
    p = fixture.port
    args = _args(False) + [f"--unsafely-treat-insecure-origin-as-secure=http://news.test:{p}"]

    async def scenario():
        out = await tool("browser_launch")(url=f"http://news.test:{p}/workers.html", headless=True,
                                           persistent=False, block_ads=True, auto_verify=False,
                                           extra_args=args)
        assert "block_ads: on" in out, out
        try:
            want = {("news.test", f"/api/{k}-ok") for k in ("outer", "inner", "shared", "service")}
            await _wait_for(fixture, want, 15)
            return fixture.seen(), _blocker_report()
        finally:
            await tool("browser_close")()

    seen, report = asyncio.run(scenario())
    for kind in ("outer", "inner", "shared", "service"):
        assert ("news.test", f"/api/{kind}-ok") in seen, (kind, seen)
    leaked = [s for s in seen if s[0] == "www.google-analytics.com"]
    assert not leaked, {"leaked": leaked, "report": report}


@needs_engine
def test_one_missing_list_is_reported_as_partial(fixture, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_LIST_URLS",
                       f"easylist=http://127.0.0.1:{fixture.port}/lists/easylist.txt,"
                       f"easyprivacy=http://127.0.0.1:9/easyprivacy.txt")

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False)
        try:
            return out, json.loads(await tool("server_status")())
        finally:
            await tool("browser_close")()

    out, status = asyncio.run(scenario())
    assert "block_ads: PARTIAL" in out and "easyprivacy" in out, out
    assert "block_ads: on" not in out, out
    assert status["block_ads"]["browsers"][0]["coverage"] == "partial", status["block_ads"]


def _server_pids() -> set:
    """Server processes below this test process (two deep on Windows, where a venv's python.exe
    is a launcher)."""
    from mcp_stealth_chrome import state
    rows = state.process_table() or []
    below, grew = {os.getpid()}, True
    while grew:
        grew = False
        for pid, ppid, _command in rows:
            if ppid in below and pid not in below:
                below.add(pid)
                grew = True
    return {pid for pid, _ppid, command in rows
            if pid in below and "mcp_stealth_chrome.server import main" in command}


@needs_engine
@pytest.mark.parametrize("when", ["before-the-popup", "while-the-popup-loads", "hung-then-killed"])
def test_a_killed_server_never_freezes_the_page_or_a_popup(fixture, tmp_path, when):
    """SIGKILL runs no Python at all. Whatever block_ads had set up then must not hold anything:
    the page goes on loading, the popup it opens loads, and the opener goes on after it.
    hung-then-killed freezes the server first (SIGSTOP, a server that stopped answering), lets the
    page open its popup meanwhile, and only then kills it: a target held for the debugger at that
    moment stayed held after its client was gone, with the opener frozen behind it."""
    import signal

    from _protocol import Client
    from mcp_stealth_chrome import state
    p = fixture.port
    profile = tmp_path / "profile"
    profile.mkdir()
    if when == "hung-then-killed" and not hasattr(signal, "SIGSTOP"):
        pytest.skip("Windows has no SIGSTOP to freeze the server with")
    page = "killed-late" if when == "while-the-popup-loads" else "killed-early"
    env = {"MCP_STEALTH_DATA_DIR": str(tmp_path / "data"),
           "MCP_STEALTH_ADBLOCK_DIR": os.environ["MCP_STEALTH_ADBLOCK_DIR"],
           "MCP_STEALTH_ADBLOCK_LIST_URLS": os.environ["MCP_STEALTH_ADBLOCK_LIST_URLS"]}
    before = _server_pids()
    client = Client(env=env)
    ours = _server_pids() - before
    assert ours, "the server this test started was not found"
    try:
        out = client.call("browser_launch", _timeout=90, url="about:blank", headless=True,
                          user_data_dir=str(profile), block_ads=True, auto_verify=False,
                          extra_args=["--use-mock-keychain", "--disable-popup-blocking",
                                      "--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost"])
        assert "block_ads: on" in out.text, out.text
        fixture.clear()
        client.call("evaluate", expression=f"location.href = 'http://news.test:{p}/{page}.html'; 1")
        mark = ("news.test", "/chain/3.png") if when == "before-the-popup" else ("widget.test", "/second.html")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not fixture.has(*mark):
            time.sleep(0.02)
        assert fixture.has(*mark), f"never saw {mark}: {fixture.seen()}"
        if when == "hung-then-killed":
            for pid in ours:
                os.kill(pid, signal.SIGSTOP)
            time.sleep(3)  # the page opens its popup at 1.5 s, while nothing answers Chrome
        for pid in ours:  # SIGTERM is TerminateProcess on Windows
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        killed_at = time.monotonic()
        wanted = {("news.test", "/chain-done"), ("news.test", "/opener-alive"),
                  ("widget.test", "/second.png")}
        while time.monotonic() - killed_at < 20 and not all(fixture.has(*w) for w in wanted):
            time.sleep(0.1)
        missing = [w for w in wanted if not fixture.has(*w)]
    finally:
        client.close()
        for pid, _command in state.processes_naming(profile) or []:  # orphaned by the kill
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
    assert not missing, f"after the server was killed these never arrived: {missing}"


@needs_engine
def test_a_dropped_blocker_connection_lets_everything_through_and_says_so(fixture):
    """The blocker's own connection goes (here: its socket shut under it) in the middle of a page
    load: the page finishes, and the next tool result says block_ads stopped and why."""
    from nodriver import cdp

    from mcp_stealth_chrome import adblock, server
    p = fixture.port

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            tab = server.BrowserState.active_tab()
            blocker = adblock.blocker_for(server.BrowserState.browser)
            await tab.send(cdp.page.navigate(f"http://news.test:{p}/chain.html"))
            assert await _until(lambda: _async(fixture.has("news.test", "/chain/2.png")), 10)
            blocker._shut_socket()

            async def chain_done():
                return fixture.has("news.test", "/chain-done")
            walked = await _until(chain_done, 15)
            await asyncio.sleep(0.5)
            tool_obj = server.mcp._tool_manager._tools["get_url"]
            result = await server.mcp._call_bound("get_url", {}, None, tool_obj)
            text = "".join(getattr(c, "text", "") for c in (
                result[0] if isinstance(result, tuple) else result))
            return walked, text, adblock.blocker_for(server.BrowserState.browser)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tool("browser_close")(), 30)

    walked, text, live = asyncio.run(scenario())
    assert walked, "requests stayed held after the blocker's connection went"
    assert "block_ads stopped" in text, text
    assert live is None, "a blocker without a connection is still listed as live"



@needs_engine
def test_a_connection_lost_while_a_popup_is_held_still_lets_it_load(fixture, monkeypatch):
    """The blocker's connection ends while a popup waits for it (its setup has not run yet)
    and the browser lives on: the end path starts every target over a new connection, so the
    popup and its opener carry on, and the next tool result says block_ads stopped."""
    from nodriver import cdp

    from mcp_stealth_chrome import adblock, server
    p = fixture.port
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_STALL", "60")  # this is the end path, not the watchdog
    hold = {"on": False}
    real = adblock.Blocker._on_attached

    async def slow(self, params):
        if hold["on"] and (params.get("targetInfo") or {}).get("type") == "page":
            await asyncio.sleep(60)  # the popup stays held for this client
        return await real(self, params)

    monkeypatch.setattr(adblock.Blocker, "_on_attached", slow)

    async def scenario():
        out = await tool("browser_launch")(url=f"http://news.test:{p}/links.html", headless=True,
                                           persistent=False, block_ads=True, auto_verify=False,
                                           extra_args=_args(False))
        assert "block_ads: on" in out, out
        try:
            tab = server.BrowserState.active_tab()
            blocker = adblock.blocker_for(server.BrowserState.browser)
            hold["on"] = True
            opened = asyncio.ensure_future(tab.evaluate(
                f"window.open('http://widget.test:{p}/second.html') && 1"))
            await asyncio.sleep(1.5)
            held = not fixture.has("widget.test", "/second.html")
            blocker._shut_socket()  # the connection ends; the browser does not
            popped = await _until(lambda: _async(fixture.has("widget.test", "/second.png")), 15)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(opened, 10)

            async def opener_alive():
                try:
                    return await asyncio.wait_for(tab.evaluate("1+1"), 2) == 2
                except Exception:
                    return False
            alive = await _until(opener_alive, 10)
            tool_obj = server.mcp._tool_manager._tools["get_url"]
            result = await server.mcp._call_bound("get_url", {}, None, tool_obj)
            text = "".join(getattr(c, "text", "") for c in (
                result[0] if isinstance(result, tuple) else result))
            return held, popped, alive, text
        finally:
            hold["on"] = False
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tool("browser_close")(), 30)

    held, popped, alive, text = asyncio.run(scenario())
    assert held, "the popup was not held, so this proves nothing"
    assert popped, "the popup held for the lost connection never loaded"
    assert alive, "the opener stayed frozen behind its popup"
    assert "block_ads stopped" in text, text


def test_a_launch_without_block_ads_never_loads_it():
    """A new server process launches real Chrome without block_ads, intercepts, reads its status,
    closes and runs its exit path: adblock.py and the adblock package are never loaded."""
    import subprocess
    import sys

    from mcp_stealth_chrome import adblock
    code = """
import asyncio, sys
from mcp_stealth_chrome import server
tools = server.mcp._tool_manager._tools

async def main():
    out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False,
                                           auto_verify=False)
    assert not out.startswith("Error"), out
    try:
        assert not (await tools["intercept_start"].fn()).startswith("Error")
        assert not (await tools["intercept_stop"].fn()).startswith("Error")
        assert '"browsers": []' in await tools["server_status"].fn()
    finally:
        await tools["browser_close"].fn()

asyncio.run(main())
server._stop_all_browsers()
print("RESULT", "mcp_stealth_chrome.adblock" in sys.modules, "adblock" in sys.modules)
"""
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(adblock.__file__).parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         timeout=180)
    result = [line for line in out.stdout.splitlines() if line.startswith("RESULT")]
    assert result == ["RESULT False False"], (out.stdout[-1500:], out.stderr[-3000:])


def test_block_ads_without_the_package_launches_and_says_how_to_install_it():
    """A server without the adblock extra: block_ads=True still launches, the result says
    block_ads is OFF and how to install the extra, and server_status says the same."""
    import subprocess
    import sys

    from mcp_stealth_chrome import adblock
    code = """
import asyncio, json, sys
sys.modules["adblock"] = None  # as an install without the extra has it
from mcp_stealth_chrome import server
tools = server.mcp._tool_manager._tools

async def main():
    out = await tools["browser_launch"].fn(url="about:blank", headless=True, persistent=False,
                                           block_ads=True, auto_verify=False)
    try:
        print("LAUNCH", json.dumps(out))
        print("EVAL", json.dumps(await tools["evaluate"].fn("1+1")))
        print("STATUS", json.dumps(json.loads(await tools["server_status"].fn())["block_ads"]))
    finally:
        await tools["browser_close"].fn()

asyncio.run(main())
"""
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(adblock.__file__).parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         timeout=180)
    lines = {line.split(" ", 1)[0]: json.loads(line.split(" ", 1)[1])
             for line in out.stdout.splitlines() if line.split(" ", 1)[0] in ("LAUNCH", "EVAL", "STATUS")}
    assert set(lines) == {"LAUNCH", "EVAL", "STATUS"}, (out.stdout[-1500:], out.stderr[-3000:])
    hints = ("uv tool install 'mcp-stealth-chrome[adblock]'", "pip install 'mcp-stealth-chrome[adblock]'")
    assert not lines["LAUNCH"].startswith("Error"), lines["LAUNCH"]
    assert "block_ads: OFF" in lines["LAUNCH"] and all(h in lines["LAUNCH"] for h in hints), lines["LAUNCH"]
    assert "2" in lines["EVAL"]
    assert all(h in lines["STATUS"]["package"] for h in hints), lines["STATUS"]



@needs_engine
@pytest.mark.skipif(os.name != "posix", reason="keep_alive is refused on Windows (no quit watcher)")
def test_keep_alive_leaves_the_browser_unfrozen_and_a_reattach_blocks_again(fixture, tmp_path):
    """keep_alive with block_ads: the server exits (stdin EOF) while the page loads, and the kept
    browser goes on, a popup it opens afterwards loading too. Nothing blocks once its server is
    gone: an ad request then goes out. A later server reattaching with block_ads=True blocks
    again, and says so."""
    import signal

    from _protocol import Client
    from mcp_stealth_chrome import state
    p = fixture.port
    env = {"MCP_STEALTH_DATA_DIR": str(tmp_path / "data"),
           "MCP_STEALTH_KEEP_ALIVE_DIR": str(tmp_path / "keep-alive"),
           "MCP_STEALTH_TABS_DIR": "",
           "MCP_STEALTH_ADBLOCK_DIR": os.environ["MCP_STEALTH_ADBLOCK_DIR"],
           "MCP_STEALTH_ADBLOCK_LIST_URLS": os.environ["MCP_STEALTH_ADBLOCK_LIST_URLS"]}
    args = ["--use-mock-keychain", "--disable-popup-blocking",
            "--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost"]
    try:
        client = Client(env=env)
        try:
            out = client.call("browser_launch", _timeout=90, url="about:blank", headless=True,
                              persistent=True, keep_alive=True, block_ads=True, auto_verify=False,
                              extra_args=args)
            assert "block_ads: on" in out.text and "keep_alive" in out.text, out.text
            fixture.clear()
            client.call("evaluate", expression=f"location.href = 'http://news.test:{p}/kept.html'; 1")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not fixture.has("news.test", "/chain/3.png"):
                time.sleep(0.05)
            assert fixture.has("news.test", "/chain/3.png"), fixture.seen()
        finally:
            client.close()  # stdin EOF while the page still loads; its popup opens at 4 s
        wanted = {("news.test", "/chain-done"), ("news.test", "/opener-alive"),
                  ("widget.test", "/second.png")}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(fixture.has(*w) for w in wanted):
            time.sleep(0.1)
        missing = [w for w in wanted if not fixture.has(*w)]
        assert not missing, f"the kept browser froze after its server exited: {missing}"
        assert fixture.has("pagead2.googlesyndication.com", "/pagead/js/second.js"), \
            "an ad request after the server exit was expected to go out: nothing blocks then"

        client = Client(env=env)
        try:
            out = client.call("browser_launch", _timeout=90, url="about:blank", headless=True,
                              persistent=True, block_ads=True, auto_verify=False, extra_args=args)
            assert "Reattached to the running browser" in out.text, out.text
            assert "block_ads: on" in out.text, out.text
            fixture.clear()
            nav = client.call("navigate", url=f"http://news.test:{p}/")
            assert not nav.failed, nav.text
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not ALLOWED <= fixture.seen():
                time.sleep(0.1)
            time.sleep(1)
            seen = fixture.seen()
            status = json.loads(client.call("server_status").text)["block_ads"]
        finally:
            client.call("close_all_instances", _timeout=40)
            client.close()
        leaked = BLOCKED & seen
        assert not leaked, f"after the reattach these ad requests went out: {leaked}"
        assert status["browsers"] and status["browsers"][0]["blocked"] >= len(BLOCKED) - 1, status
    finally:
        for pid, _command in state.processes_naming(tmp_path) or []:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)


def test_block_ads_with_brave_is_left_to_shields():
    """browser="brave" with block_ads=True: Brave's own Shields blocks, so no blocker is set up
    and the result says so. Real Brave; skipped where it is not installed."""
    from mcp_stealth_chrome import brave as brave_mode
    if brave_mode.find_brave() is None:
        pytest.skip("Brave is not installed")

    async def scenario():
        out = await tool("browser_launch")(url="about:blank", headless=True, persistent=False,
                                           browser="brave", block_ads=True, auto_verify=False)
        try:
            return out, json.loads(await tool("server_status")())["block_ads"]
        finally:
            await tool("browser_close")()

    out, status = asyncio.run(scenario())
    assert not out.startswith("Error"), out
    assert "Shields" in out and "block_ads: on" not in out, out
    assert status["browsers"] == [], status
