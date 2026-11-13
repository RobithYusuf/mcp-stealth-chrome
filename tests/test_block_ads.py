"""block_ads without a browser: list download and refresh, the stored engine, decisions, and
the rule that every paused request and every waiting target is answered exactly once."""
import asyncio
import importlib.util
import json
import os
import pathlib
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from mcp_stealth_chrome import adblock

# The adblock package is an optional extra. Tests that build or run the engine need it; the rest,
# the install hints among them, run without it (as the fast CI job, which installs no extra, does).
needs_engine = pytest.mark.skipif(
    importlib.util.find_spec("adblock") is None,
    reason="needs the adblock extra: uv sync --extra adblock")

LIST = """[Adblock Plus 2.0]
! Title: test
||pagead2.googlesyndication.com^
||g.doubleclick.net^
/collect?tid=
@@/allowed.js$script,domain=news.test
"""


class ListServer:
    def __init__(self, body: str = LIST, status: int = 200):
        self.body, self.status, self.requests = body, status, []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.requests.append(dict(self.headers))
                if server.status == 200 and self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                data = server.body.encode()
                self.send_response(server.status)
                self.send_header("ETag", '"v1"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self):
        return f"easylist=http://127.0.0.1:{self.port}/easylist.txt"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv(adblock.ENV_DIR, str(tmp_path / "adblock"))
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    yield tmp_path / "adblock"
    adblock._ENGINE_CACHE.update(key=None, engine=None)


@pytest.fixture
def served(folder, monkeypatch):
    srv = ListServer()
    monkeypatch.setenv(adblock.ENV_LIST_URLS, srv.url())
    yield srv
    srv.close()


@needs_engine
def test_first_use_downloads_into_private_files(served, folder):
    engine, info = adblock.load_engine()
    assert engine is not None, info
    assert info["rules"] == 4
    assert (folder / "easylist.txt").read_text() == LIST
    if sys.platform != "win32":  # Windows keeps no POSIX mode bits
        for name in ("easylist.txt", "engine.dat", "lists.json"):
            assert (folder / name).stat().st_mode & 0o777 == 0o600, name
        assert folder.stat().st_mode & 0o777 == 0o700


@needs_engine
def test_lists_are_checked_at_most_once_a_day(served, folder):
    adblock.load_engine()
    assert len(served.requests) == 1
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    _engine, info = adblock.load_engine()
    time.sleep(0.5)  # a refresh would run on a background thread
    assert len(served.requests) == 1 and "refresh" not in info, "a fresh copy was fetched again"
    meta = json.loads((folder / "lists.json").read_text())
    meta["easylist"]["checked_at"] = time.time() - 25 * 3600
    (folder / "lists.json").write_text(json.dumps(meta))
    engine, info = adblock.load_engine()
    assert engine is not None and "background" in info["refresh"]
    deadline = time.monotonic() + 5
    while len(served.requests) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert served.requests[1].get("If-None-Match") == '"v1"', "the refresh was not conditional"
    time.sleep(0.2)
    assert (folder / "easylist.txt").read_text() == LIST, "a 304 must keep the copy"


@needs_engine
def test_offline_uses_the_last_copy(served, folder, monkeypatch):
    adblock.load_engine()
    served.close()
    meta = json.loads((folder / "lists.json").read_text())
    meta["easylist"]["checked_at"] = 0
    (folder / "lists.json").write_text(json.dumps(meta))
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    engine, info = adblock.load_engine()
    assert engine is not None, info
    assert engine.check_network_urls("https://pagead2.googlesyndication.com/x.js",
                                     "https://news.test/", "script").matched


@needs_engine
def test_an_error_page_never_replaces_a_list(folder, monkeypatch):
    bad = ListServer(body="<html>Sign in to the Wi-Fi</html>")
    monkeypatch.setenv(adblock.ENV_LIST_URLS, bad.url())
    try:
        engine, info = adblock.load_engine()
    finally:
        bad.close()
    assert engine is None
    assert "not an Adblock Plus filter list" in info["reason"], info
    assert not (folder / "easylist.txt").exists()


@needs_engine
def test_no_list_says_why(folder, monkeypatch):
    monkeypatch.setenv(adblock.ENV_LIST_URLS, "easylist=http://127.0.0.1:9/easylist.txt")
    engine, info = adblock.load_engine()
    assert engine is None
    assert info["reason"].startswith("no filter list is available"), info


def test_lists_are_fetched_over_https_only(folder, monkeypatch):
    monkeypatch.delenv(adblock.ENV_LIST_URLS, raising=False)
    body, _entry, note = adblock._download("easylist", "http://easylist.to/easylist/easylist.txt", {}, 1)
    assert body is None and "https only" in note


@needs_engine
def test_the_engine_is_stored_and_rebuilt_when_unusable(served, folder):
    adblock.load_engine()
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    _engine, info = adblock.load_engine()
    assert info["engine"].startswith("loaded from disk"), info
    (folder / "engine.dat").write_bytes(b"not an engine")
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    engine, info = adblock.load_engine()
    assert info["engine"].startswith("built"), info
    assert engine.check_network_urls("https://securepubads.g.doubleclick.net/tag.js",
                                     "https://news.test/", "script").matched


def test_brave_is_left_to_shields():
    pending = adblock.begin(True, "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
    assert pending is not None and pending.task is None
    assert "Shields" in pending.note
    assert adblock.begin(False, "/x/chrome") is None


def test_a_missing_package_says_so(folder, monkeypatch):
    monkeypatch.setitem(sys.modules, "adblock", None)
    engine, info = adblock.load_engine(download=False)
    assert engine is None and "not installed" in info["reason"]


# ── decisions and answers ────────────────────────────────────────────────

def _engine():
    import adblock as rust
    filters = rust.FilterSet()
    filters.add_filter_list(LIST, rule_types="networkonly")
    return rust.Engine(filters, optimize=True)


class FakeWs:
    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    async def send(self, raw):
        if self.fail:
            raise ConnectionError("gone")
        self.sent.append(json.loads(raw))


def _blocker(ws=None):
    blocker = adblock.Blocker(_engine(), "ws://x", "main", "s", {})
    blocker.ws = ws or FakeWs()
    return blocker


@needs_engine
def test_the_page_itself_is_never_blocked_but_its_ad_frames_are():
    b = _blocker()
    page = adblock._Session("S", "T1", "page", "about:blank")
    ad = "https://securepubads.g.doubleclick.net/frame.html"
    assert b.decide(ad, "Document", "T1", page) is None
    assert b.decide(ad, "Document", "F2", page) == "sub_frame"
    assert b.decide("https://news.test/collect?tid=1", "Image", "T1", page) == "image"
    assert b.decide("https://news.test/app.js", "Script", "T1", page) is None
    assert b.decide("data:text/plain,x", "Image", "T1", page) is None


@needs_engine
def test_a_paused_request_is_answered_once_even_when_deciding_fails():
    async def scenario():
        b = _blocker()
        b.engine = None  # check_network_urls raises
        await b._on_paused({"requestId": "r1", "request": {"url": "https://x.test/a.js"},
                            "resourceType": "Script"}, "S")
        b.record = None  # record raises
        await b._on_paused({"requestId": "r2", "request": {"url": "https://x.test/b.js"},
                            "resourceType": "Script"}, "S")
        good = _blocker()
        await good._on_paused({"requestId": "r3", "request": {
            "url": "https://pagead2.googlesyndication.com/a.js"}, "resourceType": "Script",
            "frameId": "T"}, "S")
        return b.ws.sent, good.ws.sent

    sent, blocked = asyncio.run(scenario())
    assert [(m["method"], m["params"]["requestId"]) for m in sent] == [
        ("Fetch.continueRequest", "r1"), ("Fetch.continueRequest", "r2")]
    assert [(m["method"], m["params"]) for m in blocked] == [
        ("Fetch.failRequest", {"requestId": "r3", "errorReason": "BlockedByClient"})]


@needs_engine
def test_a_lost_connection_while_answering_does_not_raise():
    async def scenario():
        b = _blocker(FakeWs(fail=True))
        await b._on_paused({"requestId": "r1", "request": {"url": "https://x.test/a.js"},
                            "resourceType": "Script"}, "S")
    asyncio.run(scenario())


@needs_engine
def test_a_waiting_target_is_released_even_when_its_setup_fails():
    async def scenario():
        b = _blocker()
        calls = []

        async def send(method, params=None, sid=None, wait=True, **_kw):
            calls.append(method)
            if method == "Fetch.enable":
                raise TimeoutError("no answer")
            return {}

        b._send = send
        await b._on_attached({"sessionId": "S1", "waitingForDebugger": True,
                              "targetInfo": {"type": "page", "targetId": "T", "url": "chrome://newtab/"}})
        await b._on_attached({"sessionId": "S2", "waitingForDebugger": True,
                              "targetInfo": {"type": "service_worker", "targetId": "W",
                                             "url": "chrome-extension://abc/sw.js"}})
        return calls, b.uncovered

    calls, uncovered = asyncio.run(scenario())
    # The page's frame tree is read only after its release: a held target answers no Page call.
    assert [c for c in calls if c != "Page.getFrameTree"] == [
        "Fetch.enable", "Runtime.runIfWaitingForDebugger",
        "Runtime.runIfWaitingForDebugger", "Target.detachFromTarget"], calls
    assert calls.index("Page.getFrameTree") > calls.index("Runtime.runIfWaitingForDebugger"), calls
    assert uncovered and uncovered[0]["type"] == "page"


@needs_engine
def test_intercept_rule_rides_on_the_blocker_and_answers_once():
    """intercept_start's rule is asked by the blocker for the intercepted tab: one decision,
    one answer, and an ad it blocks is logged as such rather than answered twice."""
    from mcp_stealth_chrome.tools import capture

    state = {"active": True, "url_pattern": "*", "methods": None, "action": "mock",
             "mock": {"status": 200, "body": "{}", "content_type": "application/json"},
             "counts": {"seen": 0, "passed": 0, "blocked": 0, "mocked": 0}, "log": [],
             "max_events": 50}
    token = object()
    state["broker_token"] = token
    rule = capture._broker_rule(state, token)

    async def scenario():
        b = _blocker()
        b.sessions["S"] = adblock._Session("S", "T1", "page", "https://news.test/")
        await b._set_interceptor("T1", rule)
        await b._on_paused({"requestId": "r1", "frameId": "T1", "resourceType": "XHR",
                            "request": {"url": "https://news.test/api", "method": "GET"}}, "S")
        await b._on_paused({"requestId": "r2", "frameId": "T1", "resourceType": "Script",
                            "request": {"url": "https://pagead2.googlesyndication.com/a.js",
                                        "method": "GET"}}, "S")
        return b.ws.sent, b.report()

    sent, report = asyncio.run(scenario())
    assert [(m["method"], m["params"]["requestId"]) for m in sent] == [
        ("Fetch.fulfillRequest", "r1"), ("Fetch.failRequest", "r2")], sent
    assert [e["outcome"] for e in state["log"]] == ["mocked", "blocked by block_ads"]
    assert report["requests_seen"] == 2 and report["blocked"] == 1, report
    assert state["counts"] == {"seen": 2, "passed": 0, "blocked": 0, "mocked": 1,
                               "blocked_by_block_ads": 1}, state["counts"]


def test_the_cdp_glob_matches_like_fetch_patterns():
    from mcp_stealth_chrome.tools import capture
    assert capture._cdp_glob("*/api/*").match("https://x.test/api/data")
    assert not capture._cdp_glob("*/api/*").match("https://x.test/apix")
    assert capture._cdp_glob("https://?.test/*").match("https://a.test/z")
    assert capture._cdp_glob(r"*\?q=1").match("https://a.test/?q=1")
    assert not capture._cdp_glob(r"*\?q=1").match("https://a.test/xq=1")


def test_status_without_a_browser(folder, monkeypatch):
    monkeypatch.setenv(adblock.ENV_LIST_URLS, "easylist=http://127.0.0.1:9/x.txt")
    out = adblock.status([None])
    assert out["browsers"] == []
    assert "none downloaded yet" in out["lists"]
    assert os.path.basename(out["folder"]) == "adblock"


# ── review fixes ─────────────────────────────────────────────────────────

class SlowServer:
    """Answers with a huge Content-Length, a body with no length, or one byte at a time."""

    def __init__(self, mode: str):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                if mode == "huge-length":
                    self.send_header("Content-Length", str(500 * 1024 * 1024))
                    self.end_headers()
                    self.wfile.write(b"[Adblock Plus 2.0]\n")
                    time.sleep(2)
                    return
                self.end_headers()
                try:
                    if mode == "endless":
                        self.wfile.write(b"[Adblock Plus 2.0]\n")
                        for _ in range(100000):
                            self.wfile.write(b"||ads.example^\n" * 100)
                    elif mode == "late":
                        self.wfile.write(b"[Adblock Plus 2.0]\n")
                        self.wfile.flush()
                        time.sleep(2.5)
                        self.wfile.write(b"|")
                        self.wfile.flush()
                        time.sleep(10)
                    else:  # drip
                        self.wfile.write(b"[Adblock Plus 2.0]\n")
                        for _ in range(60):
                            self.wfile.write(b"|")
                            self.wfile.flush()
                            time.sleep(0.25)
                except OSError:
                    pass

        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("mode", ["huge-length", "endless"])
def test_a_list_larger_than_the_cap_is_refused(folder, monkeypatch, mode):
    srv = SlowServer(mode)
    monkeypatch.setenv(adblock.ENV_LIST_URLS, f"easylist=http://127.0.0.1:{srv.port}/x.txt")
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_MAX_BYTES", "100000")
    try:
        t0 = time.monotonic()
        notes = adblock.refresh_lists()
        took = time.monotonic() - t0
    finally:
        srv.close()
    assert took < 5, took
    assert any("larger than" in n for n in notes), notes
    assert not (folder / "easylist.txt").exists()


def test_a_slow_download_stops_at_its_total_deadline(folder, monkeypatch):
    srv = SlowServer("drip")
    monkeypatch.setenv(adblock.ENV_LIST_URLS, f"easylist=http://127.0.0.1:{srv.port}/x.txt")
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT", "2")
    try:
        t0 = time.monotonic()
        notes = adblock.refresh_lists()
        took = time.monotonic() - t0
    finally:
        srv.close()
    assert took < 4, f"a byte every 0.25 s kept the download going for {took:.1f} s"
    assert any("deadline" in n for n in notes), notes


@needs_engine
def test_the_engine_is_not_held_up_by_a_download(served, folder, monkeypatch):
    """A slow refresh must not hold the lock every launch needs to load the engine."""
    adblock.load_engine()
    srv = SlowServer("drip")
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT", "10")
    try:
        monkeypatch.setenv(adblock.ENV_LIST_URLS,
                           f"easylist=http://127.0.0.1:{served.port}/easylist.txt,"
                           f"other=http://127.0.0.1:{srv.port}/x.txt")
        refresher = threading.Thread(target=adblock.refresh_lists, daemon=True)
        refresher.start()
        time.sleep(0.5)
        adblock._ENGINE_CACHE.update(key=None, engine=None)
        t0 = time.monotonic()
        engine, info = adblock.load_engine(download=False)
        took = time.monotonic() - t0
    finally:
        srv.close()
    assert engine is not None, info
    assert took < 2, f"loading the engine waited {took:.1f} s for a download"


@needs_engine
@pytest.mark.parametrize("name", ["../escape", "../../escape", "ABSOLUTE", "a/b", "..", "x.txt", ""])
def test_list_names_cannot_leave_the_folder(folder, monkeypatch, tmp_path, name):
    if name == "ABSOLUTE":
        name = str(tmp_path / "absolute-escape")  # Path / an absolute name replaces the root
    srv = ListServer()
    monkeypatch.setenv(adblock.ENV_LIST_URLS, f"{name}=http://127.0.0.1:{srv.port}/x.txt")
    target = (folder / f"{name}.txt").resolve() if name else None
    try:
        notes = adblock.refresh_lists()
        engine, info = adblock.load_engine()
    finally:
        srv.close()
    if target is not None and folder.resolve() not in target.parents:
        assert not target.exists(), f"a list was written outside the folder: {target}"
    assert engine is None and "not a valid list name" in info["reason"], (notes, info)


def test_a_redirect_away_from_https_is_refused():
    import urllib.request
    handler = adblock._HttpsOnlyRedirects()
    req = urllib.request.Request("https://easylist.to/easylist/easylist.txt")
    with pytest.raises(Exception, match="redirect"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/list.txt")
    assert handler.redirect_request(req, None, 302, "Found", {},
                                    "https://mirror.example/list.txt") is not None


@needs_engine
def test_one_missing_list_is_partial_not_on(folder, monkeypatch):
    srv = ListServer()
    monkeypatch.setenv(adblock.ENV_LIST_URLS, f"easylist=http://127.0.0.1:{srv.port}/x.txt,"
                                              f"easyprivacy=http://127.0.0.1:9/y.txt")
    try:
        engine, info = adblock.load_engine()
    finally:
        srv.close()
    assert engine is not None
    assert info["coverage"] == "partial" and info["missing_lists"] == ["easyprivacy"], info
    note = adblock._launch_note(info, uncovered=0)
    assert note.startswith(" [block_ads: PARTIAL") and "easyprivacy" in note, note
    assert "block_ads: on" not in note


@needs_engine
def test_an_engine_cache_over_the_cap_or_altered_is_rebuilt(served, folder, monkeypatch):
    adblock.load_engine()
    data = (folder / "engine.dat").read_bytes()
    (folder / "engine.dat").write_bytes(data[:-10] + b"0123456789")  # same size, other bytes
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    _engine, info = adblock.load_engine()
    assert info["engine"].startswith("built"), info
    assert "does not match" in info.get("engine_reload", ""), info  # never handed to the parser
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_MAX_ENGINE_BYTES", "10")
    adblock._ENGINE_CACHE.update(key=None, engine=None)
    _engine, info = adblock.load_engine()
    assert info["engine"].startswith("built") and "too large" in info.get("engine_reload", ""), info


@needs_engine
def test_a_tool_result_says_once_that_block_ads_died():
    b = _blocker()
    b.instance_id = "main"
    b._end("watchdog: the adblock thread stopped answering for 2.1 s; requests are let through")
    adblock._register_ended_for_notice(b, "s1")
    first = adblock.result_notice("s1", "navigated to x")
    assert "block_ads stopped" in first and "watchdog" in first, first
    assert adblock.result_notice("s1", "next") == "next"
    adblock._register_ended_for_notice(b, "s1")
    as_json = json.loads(adblock.result_notice("s1", '{"a": 1}'))
    assert "watchdog" in as_json["block_ads"], as_json


@needs_engine
def test_a_setup_that_never_answers_still_releases_its_target(monkeypatch):
    monkeypatch.setattr(adblock.Blocker, "SETUP_DEADLINE", 0.2)

    async def scenario():
        b = _blocker()
        calls = []

        async def send(method, params=None, sid=None, wait=True, **_kw):
            calls.append(method)
            if method == "Fetch.enable":
                await asyncio.sleep(3600)  # Chrome never answers
            return {}

        b._send = send
        await asyncio.wait_for(b._on_attached({
            "sessionId": "S1", "waitingForDebugger": True,
            "targetInfo": {"type": "page", "targetId": "T", "url": "about:blank"}}), 2)
        return calls, b

    calls, b = asyncio.run(scenario())
    assert "Runtime.runIfWaitingForDebugger" in calls, calls
    assert b.uncovered and "TimeoutError" in b.uncovered[0]["error"], b.uncovered
    assert not b.waiting_targets


@needs_engine
def test_the_watchdog_names_what_is_stuck(monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_STALL", "1")
    b = _blocker()
    assert b.stall_reason() is None
    b.waiting_pauses["r"] = time.monotonic() - 5
    assert "paused request waited" in b.stall_reason()
    b.waiting_pauses.clear()
    b.waiting_targets["s"] = time.monotonic() - 5
    assert "waited" in b.stall_reason() and "to start" in b.stall_reason()
    b.waiting_targets.clear()

    class Silent:
        thread = threading.current_thread()
        loop = type("L", (), {"is_running": lambda self: True})()

        def problem(self):
            return adblock._Runner.problem(self)

        def silent_for(self):
            return 4.0

    b.runner = Silent()
    assert "stopped answering for 4.0 s" in b.stall_reason()
    b._end("done")
    assert b.stall_reason() is None


def test_every_tool_result_can_carry_the_notice():
    from mcp_stealth_chrome import _app, server
    assert server._adblock_notice in _app.RESULT_FILTERS
    tool = server.mcp._tool_manager._tools["server_status"]
    key = server.BrowserState.session_key()
    with adblock._REGISTRY_LOCK:
        adblock._NOTICES[key] = ["block_ads stopped at 00:00:00 (watchdog: test)"]
    result = asyncio.run(server.mcp._call_bound("server_status", {}, None, tool))
    text = "".join(getattr(c, "text", "") for c in (result[0] if isinstance(result, tuple) else result))
    assert "watchdog: test" in json.loads(text)["block_ads_stopped"], text


# ── second review: no target is ever held, the watchdog guards the start, EOF tells ──────

@needs_engine
def test_every_target_is_told_to_run_even_when_not_reported_waiting():
    """Chrome holds a window.open popup until each attached client says go, even when the
    attach event reads waitingForDebugger false; without the go it never loaded."""
    async def scenario():
        b = _blocker()
        sent = []

        async def send(method, params=None, sid=None, wait=True, **_kw):
            sent.append(method)
            return {}

        b._send = send
        await b._on_attached({"sessionId": "S1", "waitingForDebugger": False,
                              "targetInfo": {"type": "page", "targetId": "T", "url": "about:blank"}})
        return sent

    sent = asyncio.run(scenario())
    assert sent[0] == "Fetch.enable", sent
    assert "Runtime.runIfWaitingForDebugger" in sent, sent
    assert sent.index("Runtime.runIfWaitingForDebugger") > sent.index("Target.setAutoAttach"), sent


@needs_engine
def test_the_watchdog_guards_the_start_itself(folder, monkeypatch):
    """A blocker whose adblock thread hangs while it starts is ended by the watchdog within the
    stall time, not after attach()'s own 20 s."""
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_STALL", "1")

    async def hang(self):
        time.sleep(6)  # the adblock thread itself stops answering

    monkeypatch.setattr(adblock.Blocker, "start", hang)

    class Browser:
        websocket_url = "ws://127.0.0.1:9/devtools/browser/test"

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        future.set_result((_engine(), {"coverage": "full", "rules": 4, "lists": {}}))
        t0 = time.monotonic()
        note = await adblock.attach(adblock.Pending(future), Browser(), "main", "s-start")
        return note, time.monotonic() - t0

    note, took = asyncio.run(scenario())
    assert took < 5, f"attach waited {took:.1f} s for a start the watchdog should have ended"
    assert "block_ads: OFF" in note and "watchdog" in note, note


@needs_engine
def test_a_dropped_connection_tells_the_next_tool_result(monkeypatch):
    """The blocker's connection ends while the browser is still up: Fetch is gone with it, so
    nothing is blocked any more, and the session hears so once."""
    class Closed:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration  # a clean close from the other side

        async def close(self):
            pass

    released = []
    monkeypatch.setattr(adblock, "_release_every_target",
                        lambda ws_url, budget: released.append(ws_url) or "")
    monkeypatch.setattr(adblock, "_browser_answers", lambda ws_url, timeout: True)
    b = _blocker(Closed())
    b.session_key = "s-eof"
    asyncio.run(b._read())
    note = adblock.result_notice("s-eof", "text")
    assert "block_ads stopped" in note and "connection" in note, note
    assert released == ["ws://x"], "a target this connection held must be started over a new one"

    monkeypatch.setattr(adblock, "_browser_answers", lambda ws_url, timeout: False)
    gone = _blocker(Closed())
    gone.session_key = "s-closed"
    asyncio.run(gone._read())
    assert adblock.result_notice("s-closed", "text") == "text", "a closed browser is no news"
    assert released == ["ws://x"], "nothing to release in a browser that is gone"


def test_the_download_deadline_is_total_even_inside_one_read(folder, monkeypatch):
    """One byte just before the deadline, then silence: a socket timeout fixed at the start let
    that last read run past the deadline by up to its own length."""
    srv = SlowServer("late")
    monkeypatch.setenv(adblock.ENV_LIST_URLS, f"easylist=http://127.0.0.1:{srv.port}/x.txt")
    monkeypatch.setenv("MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT", "3")
    try:
        t0 = time.monotonic()
        notes = adblock.refresh_lists()
        took = time.monotonic() - t0
    finally:
        srv.close()
    assert took < 3.6, f"a 3 s deadline took {took:.1f} s"
    assert any("deadline" in n or "timed out" in n for n in notes), notes



@needs_engine
def test_the_server_exit_ends_every_blocker_once(monkeypatch):
    """stdin EOF: before the browsers go, each blocker's connection is shut and what it may
    hold is started over a new connection, once, and the session is told."""
    released = []
    monkeypatch.setattr(adblock, "_release_every_target",
                        lambda ws_url, budget: released.append(ws_url) or "")
    b = _blocker()
    b.ws_url, b.session_key = "ws://exit", "s-exit"
    # Ended a moment ago by the watchdog or its own EOF, which released already; not yet
    # forgotten. The exit must not release a second time.
    raced = _blocker()
    raced.ws_url = "ws://raced"
    raced._end("watchdog: test")
    with adblock._REGISTRY_LOCK:
        adblock._BLOCKERS[b.ws_url] = b
        adblock._BLOCKERS[raced.ws_url] = raced
    adblock.shutdown()
    adblock.shutdown()
    assert released == ["ws://exit"], released
    assert b.ended() and "exiting" in b.end_reason
    assert b.ws_url not in adblock._BLOCKERS
    assert "block_ads stopped" in adblock.result_notice("s-exit", "x")
    with adblock._REGISTRY_LOCK:
        adblock._BLOCKERS.pop(raced.ws_url, None)


class FakeCdp:
    """A browser endpoint for _release_every_target: getTargets shows one more target the
    second time it is asked, as a popup opening during the release would."""

    def __init__(self, answer_attach: bool = True, both_at_once: bool = False):
        import websockets.sync.server as wss
        self.attached, self.scans = [], 0
        fake = self

        def handler(ws):
            for raw in ws:
                msg = json.loads(raw)
                method, mid = msg["method"], msg["id"]
                if method == "Target.getTargets":
                    fake.scans += 1
                    ids = ["A"] if fake.scans == 1 and not both_at_once else ["A", "B"]
                    ws.send(json.dumps({"id": mid, "result": {"targetInfos": [
                        {"targetId": i, "type": "page"} for i in ids]}}))
                elif method == "Target.attachToTarget":
                    if not answer_attach:
                        continue
                    fake.attached.append(msg["params"]["targetId"])
                    ws.send(json.dumps({"id": mid, "result": {"sessionId": "S" + msg["params"]["targetId"]}}))
                else:
                    ws.send(json.dumps({"id": mid, "result": {}}))

        self.server = wss.serve(handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/devtools/browser/x"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_the_release_scans_again_for_targets_that_opened_meanwhile():
    fake = FakeCdp()
    try:
        left = adblock._release_every_target(fake.url, 5.0)
    finally:
        fake.close()
    assert fake.attached == ["A", "B"], fake.attached
    assert fake.scans >= 3 and left == "", (fake.scans, left)


def test_a_release_out_of_time_says_what_was_left():
    fake = FakeCdp(answer_attach=False)
    try:
        t0 = time.monotonic()
        left = adblock._release_every_target(fake.url, 1.0)
        took = time.monotonic() - t0
    finally:
        fake.close()
    assert took < 2.5, took
    assert "could not release every held target" in left, left


def test_stopping_the_servers_browsers_ends_block_ads_first(monkeypatch):
    from mcp_stealth_chrome import server
    order = []
    monkeypatch.setattr(adblock, "shutdown", lambda *a, **k: order.append("block_ads"))
    monkeypatch.setattr(server.BrowserState, "session_keys", classmethod(lambda cls: order.append("browsers") or []))
    server._stop_all_browsers()
    assert order == ["block_ads", "browsers"], order


# ── third review: a bounded release, an honest account of what was left, a lazy import ──

class _HungConnection:
    """A connection whose send, or whose close, never returns."""

    def __init__(self, hang: str):
        self.hang = hang

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.hang == "close":
            time.sleep(60)
        return False

    def send(self, raw):
        if self.hang == "send":
            time.sleep(60)

    def recv(self, timeout=None):
        if self.hang == "close":
            message = json.loads(self._last)
            return json.dumps({"id": message["id"], "result": {"targetInfos": []}})
        time.sleep(timeout or 60)
        raise TimeoutError("no reply")


@pytest.mark.parametrize("hang", ["send", "close", "connect"])
def test_the_release_is_bounded_whatever_hangs(monkeypatch, hang):
    import websockets.sync.client as wsc

    def connect(*args, **kwargs):
        if hang == "connect":
            time.sleep(60)
        conn = _HungConnection(hang)
        real_send = conn.send

        def send(raw):
            conn._last = raw
            real_send(raw)
        conn.send = send
        return conn

    monkeypatch.setattr(wsc, "connect", connect)
    t0 = time.monotonic()
    left = adblock._release_every_target("ws://127.0.0.1:9/devtools/browser/x", 1.0)
    took = time.monotonic() - t0
    assert took < 2.5, f"a hung {hang} held the release for {took:.1f} s"
    if hang == "close":
        assert left == "", left  # every target was released; only the goodbye hung
    else:
        assert "which are left is unknown" in left, left  # no target list was ever read


@needs_engine
def test_the_server_exit_is_bounded_in_total(monkeypatch):
    """Several blockers, each with a release that never returns: the exit still finishes."""
    monkeypatch.setattr(adblock, "_release_targets",
                        lambda ws_url, budget, progress: time.sleep(60), raising=False)
    blockers = []
    for i in range(3):
        b = _blocker()
        b.ws_url, b.session_key = f"ws://hung{i}", f"s-hung{i}"
        blockers.append(b)
    with adblock._REGISTRY_LOCK:
        for b in blockers:
            adblock._BLOCKERS[b.ws_url] = b
    t0 = time.monotonic()
    adblock.shutdown()
    took = time.monotonic() - t0
    for b in blockers:
        adblock.result_notice(b.session_key, "")
    assert took < adblock.SHUTDOWN_BUDGET + 1.5, f"the exit waited {took:.1f} s"
    assert all(b.ended() for b in blockers)


def test_a_release_out_of_time_names_every_target_left():
    """Targets A and B, and an attach that never answers: both are left, and both are named."""
    fake = FakeCdp(answer_attach=False, both_at_once=True)
    try:
        left = adblock._release_every_target(fake.url, 1.0)
    finally:
        fake.close()
    assert "2 target(s) not released" in left and "A" in left and "B" in left, left


def test_importing_the_server_loads_nothing_of_block_ads():
    """A new server process: importing it, and a launch without block_ads, load neither
    adblock.py nor the adblock package. Checked in a process of its own, since this one has
    them loaded already."""
    import subprocess
    code = ("import sys\n"
            "from mcp_stealth_chrome import server\n"
            "print('mcp_stealth_chrome.adblock' in sys.modules, 'adblock' in sys.modules)\n")
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(adblock.__file__).parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "False False", (out.stdout, out.stderr[-2000:])


# ── adblock is an optional extra ─────────────────────────────────────────

INSTALL_HINTS = ("uv tool install 'mcp-stealth-chrome[adblock]'",
                 "pip install 'mcp-stealth-chrome[adblock]'")


def test_adblock_is_an_extra_not_a_dependency():
    """No musllinux or Windows ARM64 wheel exists, so as a dependency it made the whole package
    fail to install there, block_ads used or not."""
    import tomllib
    project = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert not [d for d in project["dependencies"] if d.replace(" ", "").startswith("adblock")]
    assert project["optional-dependencies"]["adblock"] == ["adblock>=0.6,<0.7"]


def test_a_missing_package_names_the_extra(folder, monkeypatch):
    monkeypatch.setitem(sys.modules, "adblock", None)
    engine, info = adblock.load_engine(download=False)
    assert engine is None
    assert "not installed" in info["reason"], info
    assert all(hint in info["reason"] for hint in INSTALL_HINTS), info["reason"]


def test_server_status_names_the_extra_when_the_package_is_missing(monkeypatch):
    from mcp_stealth_chrome import server
    monkeypatch.setitem(sys.modules, "adblock", None)
    package = adblock.status([None])["package"]
    assert "not installed" in package and all(h in package for h in INSTALL_HINTS), package
    status = json.loads(asyncio.run(server.mcp._tool_manager._tools["server_status"].fn()))
    assert all(h in status["block_ads"]["package"] for h in INSTALL_HINTS), status["block_ads"]


def test_server_status_names_the_extra_before_any_block_ads_launch():
    """A new server without the package, before anything asked for block_ads: server_status
    already says how to install the extra, and loads nothing of block_ads to say it."""
    import subprocess
    code = ("import sys, json, asyncio\n"
            "sys.modules['adblock'] = None\n"
            "from mcp_stealth_chrome import server\n"
            "status = json.loads(asyncio.run(server.mcp._tool_manager._tools['server_status'].fn()))\n"
            "print('PACKAGE', json.dumps(status['block_ads']['package']))\n"
            "print('LOADED', 'mcp_stealth_chrome.adblock' in sys.modules)\n")
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(adblock.__file__).parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         timeout=120)
    lines = dict(line.split(" ", 1) for line in out.stdout.splitlines() if " " in line)
    assert all(h in json.loads(lines["PACKAGE"]) for h in INSTALL_HINTS), (out.stdout, out.stderr[-2000:])
    assert lines["LOADED"] == "False"


def test_a_reattach_to_brave_leaves_ads_to_shields(monkeypatch):
    """A reattached Brave (its Config names nodriver's Chrome, so only its mark says Brave) gets
    no blocker: Shields blocks ads there, as on a new Brave launch."""
    from mcp_stealth_chrome import brave as brave_mode, server

    def no_engine(*_a, **_k):
        raise AssertionError("a blocker was being set up for Brave")

    monkeypatch.setattr(adblock, "load_engine", no_engine)
    monkeypatch.setattr(adblock.Blocker, "start", no_engine)

    class Kept:
        config = type("C", (), {"browser_executable_path": "/usr/bin/chrome"})()

    kept = Kept()
    brave_mode.mark(kept, brave_mode.BRAVE)
    note = asyncio.run(server._block_ads_on_reattach(True, kept, "main"))
    assert "Shields" in note and "nothing done" in note, note
