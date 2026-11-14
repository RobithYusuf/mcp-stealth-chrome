"""The six findings of the security and reliability sweep, each reproduced without a browser.

1. intercept_stop reported "stopped" when Fetch.disable failed, and could never be retried.
2. http_request carried caller headers (X-Api-Key) to whatever origin a redirect named.
3. mask_url left userinfo and fragment tokens readable, and some tools printed raw URLs.
4. Screenshots, PDFs, traces, heap snapshots and fetched images were written 0644.
5. click(verify=True) answered a bare "clicked" when the page could not be read.
6. batch_actions answered "0 actions OK" for input that was not a list of actions.
"""
import asyncio
import base64
import http.server
import json
import os
import socket
import stat
import sys
import threading
from types import SimpleNamespace

import pytest

POSIX_MODE_BITS = pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows keeps no POSIX mode bits: chmod only sets read-only and stat reports 0o666, so "
    "0600 cannot be written or checked; the user profile's ACLs are what protect the file."))


def _tool(name):
    from mcp_stealth_chrome import server

    return server.mcp._tool_manager._tools[name].fn


def _active(monkeypatch, tab):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    return tab


class _CdpTab:
    """Enough of a nodriver Tab to run the real CDP generators and fire events back."""

    def __init__(self, target_id="T1", fail=()):
        self.target = SimpleNamespace(target_id=target_id)
        self.handlers, self.sent, self.fail = {}, [], set(fail)
        self.replies = {}

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_handler(self, event_type, handler):
        self.handlers.get(event_type, []).remove(handler)

    async def send(self, command):
        request = next(command)
        await asyncio.sleep(0)
        if request["method"] in self.fail:
            raise RuntimeError(f"{request['method']} refused")
        self.sent.append(request)
        hook = self.replies.get(request["method"])
        return hook(self) if hook else None

    async def fire(self, event_type, **fields):
        for handler in list(self.handlers.get(event_type, [])):
            result = handler(SimpleNamespace(**fields))
            if asyncio.iscoroutine(result):
                await result

    def methods(self):
        return [r["method"] for r in self.sent]


# ── 1. intercept_stop when Fetch.disable fails ──


@pytest.fixture()
def intercept(monkeypatch):
    from mcp_stealth_chrome.tools import capture

    fresh = dict(capture._INTERCEPT, log=[], counts=dict(capture._INTERCEPT["counts"]),
                 active=False, tab=None, handler=None, handler_tab=None, watchdog=None,
                 session=None, stop_reason=None, disable_error=None)
    monkeypatch.setattr(capture, "_INTERCEPT", fresh)
    return fresh


def _paused(method="POST"):
    from nodriver.cdp import fetch as cdp_fetch

    return dict(request_id=cdp_fetch.RequestId("r1"),
                request=SimpleNamespace(method=method, url="https://x.test/api", headers={},
                                        post_data=None))


def test_a_failed_disable_is_not_reported_as_stopped_and_can_be_retried(monkeypatch, intercept):
    """Stop answered {"stopped": true} while the handler and Fetch were still live, and every
    later stop answered "interception was not active", so nothing could take them off."""
    from nodriver.cdp import fetch as cdp_fetch

    tab = _active(monkeypatch, _CdpTab(fail=("Fetch.disable",)))

    async def scenario():
        await _tool("intercept_start")(action="cancel", auto_disable_after=0)
        first = await _tool("intercept_stop")()
        log = json.loads(await _tool("intercept_log")())
        # Fetch may still pause requests: the handler answers them, and lets them through.
        await tab.fire(cdp_fetch.RequestPaused, **_paused())
        tab.fail.clear()
        second = await _tool("intercept_stop")()
        return first, log, second

    first, log, second = asyncio.run(scenario())
    assert first.startswith("Error:") and "NOT stopped" in first and "Fetch.disable" in first, first
    assert log["active"] is True and "Fetch.disable failed" in (log["disable_error"] or ""), log
    assert "Fetch.continueRequest" in tab.methods() and "Fetch.failRequest" not in tab.methods()
    assert json.loads(second)["stopped"] is True, second
    assert tab.handlers[cdp_fetch.RequestPaused] == []
    assert intercept["active"] is False and intercept["tab"] is None


def test_the_deadline_retries_a_failed_disable(monkeypatch, intercept):
    """On the deadline nobody was left to try again: the watchdog gave up after one go."""
    from nodriver.cdp import fetch as cdp_fetch

    from mcp_stealth_chrome.tools import capture

    monkeypatch.setattr(capture, "_DISABLE_RETRY_DELAYS", (0.05,) * 10)
    tab = _active(monkeypatch, _CdpTab(fail=("Fetch.disable",)))

    async def until(predicate):
        # Polled, not slept: Windows timers are coarse (about 15 ms).
        log: dict = {}
        for _ in range(200):
            log = json.loads(await _tool("intercept_log")())
            if predicate(log):
                return log
            await asyncio.sleep(0.01)
        return log

    async def scenario():
        await _tool("intercept_start")(auto_disable_after=0.01)
        during = await until(lambda log: log["stop_reason"] is not None)
        tab.fail.clear()
        return during, await until(lambda log: not log["active"])

    during, after = asyncio.run(scenario())
    assert during["active"] is True and "Fetch.disable failed" in during["stop_reason"], during
    assert after["active"] is False and after["stop_reason"] == "auto-disabled on deadline", after
    assert tab.handlers[cdp_fetch.RequestPaused] == []


# ── 2. http_request redirects keep caller headers on their origin ──


class _Recorder(http.server.BaseHTTPRequestHandler):
    """[/prefix]/r/<code>/<location with ~ for />, /loop, anything else 200 JSON. Records each
    request, with every Cookie header it carried joined by "; "."""

    seen: list = []

    def _go(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.seen.append({"port": self.server.server_port, "method": self.command,
                          "path": self.path, "key": self.headers.get("X-Api-Key"),
                          "auth": self.headers.get("Authorization"),
                          "cookie": "; ".join(self.headers.get_all("Cookie") or []) or None,
                          "body": body})
        marks = [i for i in (self.path.find("/r/"), self.path.find("/b/")) if i >= 0]
        route = self.path[min(marks):] if marks else self.path
        if route.startswith(("/r/", "/b/")):
            _, kind, code, location = route.split("/", 3)
            location = (base64.urlsafe_b64decode(location).decode() if kind == "b"
                        else location.replace("~", "/"))
            self.send_response(int(code))
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/hang"):
            import time

            time.sleep(3)
        if self.path.startswith("/set-sid"):  # a login that rotates the session cookie
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Set-Cookie", "sid=rotated; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = b'{"ok": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_HEAD = do_PUT = _go

    def log_message(self, *_args):
        pass


@pytest.fixture()
def two_origins(monkeypatch):
    """Two servers on 127.0.0.1, so the second differs by port, plus a browser cookie for
    127.0.0.1 as use_browser_cookies would read it."""
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http

    seen: list = []
    handler = type("H", (_Recorder,), {"seen": seen})
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) for _ in range(2)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    state = SimpleNamespace(a=servers[0].server_port, b=servers[1].server_port, seen=seen,
                            browser=[_cookie("sid", "abc")])

    async def cookies(_url):
        return list(state.browser)

    monkeypatch.setattr(server.BrowserState, "browser", object())
    monkeypatch.setattr(network_http, "_get_browser_cookies_for_url", cookies)
    try:
        yield state
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


def _cookie(name, value, domain="127.0.0.1", path="/", secure=False, expires=-1):
    """A browser cookie as _get_browser_cookies_for_url hands it over."""
    return {"name": name, "value": value, "domain": domain, "path": path,
            "secure": secure, "expires": expires}


def _sent(meta):
    """cookies_sent of every request in order, as the result reports them."""
    return [h["cookies_sent"] for h in meta.get("redirects", [])] + [meta["cookies_sent"]]


def _names(seen_one):
    return sorted(seen_one["cookie"].split("; ")) if seen_one["cookie"] else []


def _received(seen):
    return [len(s["cookie"].split("; ")) if s["cookie"] else 0 for s in seen]


def _request(url, **kwargs):
    kwargs.setdefault("headers", {"X-Api-Key": "SECRET", "Authorization": "Bearer T"})
    out = asyncio.run(_tool("http_request")(url, return_mode="meta", **kwargs))
    return out


def _hop(code, location):
    return f"/r/{code}/{location.replace('/', '~')}"


def _secret_hop(code, location):
    """A redirect whose Location only the server knows: nothing secret in the first URL."""
    return f"/b/{code}/{base64.urlsafe_b64encode(location.encode()).decode()}"


@pytest.mark.parametrize("other_host", ["localhost", "127.0.0.1"], ids=["host", "port"])
def test_a_redirect_to_another_origin_does_not_carry_caller_headers(two_origins, other_host):
    """curl carried X-Api-Key to the host a 302 named; only Cookie and Authorization were
    dropped, and those by curl's own rule."""
    o = two_origins
    out = _request(f"http://127.0.0.1:{o.a}" + _hop(302, f"http://{other_host}:{o.b}/final"))
    meta = json.loads(out)
    first, second = o.seen
    assert first["key"] == "SECRET" and first["auth"] == "Bearer T"
    assert second["port"] == o.b and second["path"] == "/final"
    assert second["key"] is None and second["auth"] is None, second
    assert meta["status"] == 200 and len(meta["redirects"]) == 1
    assert meta["redirects"][0]["status"] == 302
    assert set(meta["headers_not_forwarded"]["names"]) >= {"X-Api-Key", "Authorization"}
    # Browser cookies go where they went before: curl scopes them to the first host, not port.
    assert first["cookie"] == "sid=abc"
    assert second["cookie"] == ("sid=abc" if other_host == "127.0.0.1" else None), second


def test_a_same_origin_redirect_keeps_the_headers(two_origins):
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _hop(302, "/final")))
    assert [s["key"] for s in o.seen] == ["SECRET", "SECRET"]
    assert [s["cookie"] for s in o.seen] == ["sid=abc", "sid=abc"]
    assert "headers_not_forwarded" not in meta and meta["redirects"][0]["status"] == 302


def test_a_cookie_a_redirect_sets_replaces_the_browsers_on_the_next_hop(two_origins):
    """The browser's cookies sit in the session's jar, so a server that rotates the session
    on the way (a login redirect) is answered with its new value, not the stale one."""
    o = two_origins
    json.loads(_request(f"http://127.0.0.1:{o.a}/set-sid"))
    assert [s["cookie"] for s in o.seen] == ["sid=abc", "sid=rotated"]


def test_cookies_sent_is_counted_per_request(two_origins):
    """Each hop reports what went out with it: the browser cookie on both requests to
    127.0.0.1, none on the request to another host."""
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _secret_hop(
        302, f"http://127.0.0.1:{o.b}" + _hop(302, f"http://localhost:{o.a}/end"))))
    assert _received(o.seen) == [1, 1, 0]
    assert _sent(meta) == [1, 1, 0], meta


def test_a_path_scoped_browser_cookie_stays_on_its_path(two_origins):
    """A cookie the browser keeps for /admin went to every path once it was in the jar as /."""
    o = two_origins
    o.browser = [_cookie("adm", "1", path="/admin"), _cookie("sid", "abc")]
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}/admin" + _secret_hop(
        302, "/admin/next" + _hop(302, "/public"))))
    assert [s["path"] for s in o.seen][1:] == ["/admin/next/r/302/~public", "/public"]
    assert [_names(s) for s in o.seen] == [["adm=1", "sid=abc"], ["adm=1", "sid=abc"], ["sid=abc"]]
    assert _sent(meta) == [2, 2, 1], meta


def test_two_browser_cookies_of_one_name_on_two_paths_are_both_kept(two_origins):
    o = two_origins
    o.browser = [_cookie("sid", "deep", path="/admin"), _cookie("sid", "site")]
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}/admin" + _hop(302, "/public")))
    assert [_names(s) for s in o.seen] == [["sid=deep", "sid=site"], ["sid=site"]]
    assert _sent(meta) == [2, 1], meta


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_a_loopback_cookie_follows_its_host_to_another_port_and_no_further(two_origins, host):
    """localhost has no dot. Its cookies went out as a Cookie header with the caller's headers,
    so a redirect to another port of the same host dropped them, unlike 127.0.0.1's."""
    o = two_origins
    o.browser = [_cookie("sid", "abc", domain=host)]
    other = "127.0.0.1" if host == "localhost" else "localhost"
    meta = json.loads(_request(f"http://{host}:{o.a}" + _secret_hop(
        302, f"http://{host}:{o.b}" + _hop(302, f"http://{other}:{o.a}/end"))))
    assert [s["cookie"] for s in o.seen] == ["sid=abc", "sid=abc", None]
    assert _sent(meta) == [1, 1, 0], meta
    assert "Cookie" not in meta.get("headers_not_forwarded", {}).get("names", [])


@pytest.fixture()
def v6_origins(monkeypatch):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http

    class V6(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

    seen: list = []
    handler = type("H", (_Recorder,), {"seen": seen})
    try:
        servers = [V6(("::1", 0), handler) for _ in range(2)]
    except OSError as exc:
        pytest.skip(f"no IPv6 loopback here: {exc}")
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def cookies(_url):
        return [_cookie("sid", "abc", domain="::1")]

    monkeypatch.setattr(server.BrowserState, "browser", object())
    monkeypatch.setattr(network_http, "_get_browser_cookies_for_url", cookies)
    try:
        yield SimpleNamespace(a=servers[0].server_port, b=servers[1].server_port, seen=seen)
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


def test_an_ipv6_cookie_follows_its_host_to_another_port(v6_origins):
    o = v6_origins
    meta = json.loads(_request(f"http://[::1]:{o.a}" + _hop(302, f"http://[::1]:{o.b}/final")))
    assert [s["cookie"] for s in o.seen] == ["sid=abc", "sid=abc"]
    assert _sent(meta) == [1, 1], meta


def _self_signed(folder):
    """A certificate for shop.test, or skip when the cryptography package is missing."""
    import datetime

    crypto = pytest.importorskip("cryptography")
    del crypto
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shop.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("shop.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


@pytest.fixture()
def shop(monkeypatch, tmp_path):
    """https://shop.test:<a> and http://shop.test:<b>, both on 127.0.0.1 through curl's own
    resolver. Not a loopback name: curl, like Chrome, sends Secure cookies over plain http to
    localhost, 127.0.0.1 and ::1, so only a real host name shows the Secure rule."""
    import ssl

    import curl_cffi.requests
    from curl_cffi import CurlOpt

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http

    cert_file, key_file = _self_signed(tmp_path)
    seen: list = []
    handler = type("H", (_Recorder,), {"seen": seen})
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) for _ in range(2)]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    servers[0].socket = context.wrap_socket(servers[0].socket, server_side=True)
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    a, b = servers[0].server_port, servers[1].server_port
    real = curl_cffi.requests.AsyncSession

    class Resolving(real):
        def __init__(self, **kwargs):
            kwargs["verify"] = False
            kwargs["curl_options"] = {CurlOpt.RESOLVE: [f"shop.test:{a}:127.0.0.1",
                                                        f"shop.test:{b}:127.0.0.1"]}
            super().__init__(**kwargs)

    state = SimpleNamespace(a=a, b=b, seen=seen, browser=[
        _cookie("sec", "1", domain="shop.test", secure=True),
        _cookie("plain", "2", domain=".shop.test")])

    async def cookies(_url):
        return list(state.browser)

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", Resolving)
    monkeypatch.setattr(server.BrowserState, "browser", object())
    monkeypatch.setattr(network_http, "_get_browser_cookies_for_url", cookies)
    try:
        yield state
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


def test_a_secure_browser_cookie_never_goes_out_over_http(shop):
    """https -> http on the same host: the Secure cookie went along once it was in the jar
    without its flag."""
    meta = json.loads(_request(f"https://shop.test:{shop.a}"
                               + _hop(302, f"http://shop.test:{shop.b}/final")))
    assert [s["port"] for s in shop.seen] == [shop.a, shop.b]
    assert [_names(s) for s in shop.seen] == [["plain=2", "sec=1"], ["plain=2"]]
    assert _sent(meta) == [2, 1], meta


@pytest.fixture()
def idn_origin(monkeypatch):
    """http://bücher.example:<port> on 127.0.0.1 through curl's resolver; curl sends the A-label."""
    import curl_cffi.requests
    from curl_cffi import CurlOpt

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http

    seen: list = []
    handler = type("H", (_Recorder,), {"seen": seen})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    real = curl_cffi.requests.AsyncSession

    class Resolving(real):
        def __init__(self, **kwargs):
            kwargs["curl_options"] = {CurlOpt.RESOLVE: [f"xn--bcher-kva.example:{port}:127.0.0.1"]}
            super().__init__(**kwargs)

    class _Jar:
        async def get_all(self):
            return [SimpleNamespace(name="sid", value="abc", domain="xn--bcher-kva.example",
                                    path="/", secure=False, expires=-1)]

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", Resolving)
    monkeypatch.setattr(server.BrowserState, "browser", SimpleNamespace(cookies=_Jar()))
    try:
        yield SimpleNamespace(port=port, seen=seen)
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_unicode_url_gets_the_cookie_chrome_keeps_under_its_a_label(idn_origin):
    """Chrome stores an IDN cookie under its A-label; a URL written in Unicode found none of
    them, and even a match would have gone into the jar under a host curl never asks for."""
    o = idn_origin
    meta = json.loads(_request(f"http://bücher.example:{o.port}" + _hop(302, "/final")))
    assert [s["cookie"] for s in o.seen] == ["sid=abc", "sid=abc"]
    assert _sent(meta) == [1, 1], meta
    assert meta["cookie_snapshot"]["count"] == 1


@pytest.mark.parametrize("code,method,then,body", [
    (303, "POST", "GET", ""), (303, "PUT", "GET", ""), (302, "POST", "GET", ""),
    (301, "POST", "GET", ""), (307, "POST", "POST", "a=1"), (308, "POST", "POST", "a=1"),
    (302, "PUT", "PUT", "a=1"),
])
def test_redirect_method_rewrites_follow_browsers(two_origins, code, method, then, body):
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _hop(code, "/final"),
                               method=method, data="a=1"))
    assert [(s["method"], s["body"]) for s in o.seen] == [(method, "a=1"), (then, body)]
    assert meta.get("final_method", method) == then


def test_too_many_redirects_fails_as_before(two_origins):
    o = two_origins
    out = _request(f"http://127.0.0.1:{o.a}/loop")
    assert out.startswith("Error:") and '"curl_code": 47' in out and "Maximum (30)" in out, out
    assert len(o.seen) == 31  # the request and the 30 redirects curl followed


def test_follow_redirects_false_returns_the_redirect(two_origins):
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _hop(302, "/final"),
                               follow_redirects=False))
    assert meta["status"] == 302 and len(o.seen) == 1 and "redirects" not in meta


def test_a_redirect_to_another_scheme_is_not_followed(two_origins):
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _hop(302, "file:///etc/passwd")))
    assert meta["status"] == 302 and len(o.seen) == 1
    assert "only http and https" in meta["redirect_note"]


# ── 3. mask_url and the URLs tools print ──


@pytest.mark.parametrize("url,expected", [
    ("https://user:SECRET@x.test/p", "https://***@x.test/p"),
    ("https://ghp_SECRET@github.com/o/r.git", "https://***@github.com/o/r.git"),
    ("//u:SECRET@x.test/", "//***@x.test/"),
    ("https://x.test/cb#access_token=SECRET&expires_in=3600",
     "https://x.test/cb#access_token=***&expires_in=3600"),
    ("https://x.test/#/cb?id_token=SECRET&state=1", "https://x.test/#/cb?id_token=***&state=1"),
    ("https://x.test/a?b=%20c&access_token=SECRET#frag",
     "https://x.test/a?b=%20c&access_token=***#frag"),
])
def test_mask_url_hides_userinfo_and_fragment_tokens(url, expected):
    from mcp_stealth_chrome.helpers import mask_url

    assert mask_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://x.test/a?q=a+b&x=%2F&empty=&flag", "https://medium.com/@me/post?x=1",
    "https://x.test/page#section-2", "https://x.test/?", "https://x.test/#", "/rel?a=1#b=2",
])
def test_mask_url_leaves_ordinary_urls_byte_for_byte(url):
    from mcp_stealth_chrome.helpers import mask_url

    assert mask_url(url) == url


def test_http_request_errors_hide_userinfo():
    """The error named the URL through a mask that only knew the query."""
    out = asyncio.run(_tool("http_request")("http://user:SECRET@127.0.0.1:1/x?token=SECRET2",
                                            timeout=5))
    assert out.startswith("Error:") and "SECRET" not in out, out
    assert "***@127.0.0.1:1" in out


def test_http_session_cookies_masks_the_url(monkeypatch):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http

    async def cookies(_url):
        return []

    monkeypatch.setattr(server.BrowserState, "browser", object())
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    monkeypatch.setattr(network_http, "_get_browser_cookies_for_url", cookies)
    out = asyncio.run(_tool("http_session_cookies")(
        "https://u:SECRET@x.test/?access_token=SECRET#id_token=SECRET"))
    assert "SECRET" not in out, out
    assert json.loads(out)["url"] == "https://***@x.test/?access_token=***#id_token=***"


@pytest.mark.parametrize("final", ["https://x.test/cb", "chrome-error://chromewebdata/", "raise"])
def test_session_warmup_masks_its_actions_and_errors(monkeypatch, final):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import mouse_kit, network_http

    target = "https://u:SECRET@x.test/cb?access_token=SECRET#id_token=SECRET"

    class _Tab:
        async def get(self, url):
            if final == "raise" and url == target:
                raise RuntimeError(f"could not load {url}")

    async def drift(**_kwargs):
        return "drifted"

    async def final_url(_tab, strict=False):
        return final

    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    _active(monkeypatch, _Tab())
    monkeypatch.setattr(mouse_kit, "mouse_drift", drift)
    monkeypatch.setattr(network_http, "get_url", final_url)
    out = asyncio.run(_tool("session_warmup")(target, dwell_seconds=0))
    assert "SECRET" not in out, out
    assert "x.test" in out


# ── 4. Page-derived files are 0600 ──


@pytest.fixture()
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _png(width=4, height=4):
    import cv2
    import numpy as np

    ok_, buf = cv2.imencode(".png", np.zeros((height, width, 3), dtype=np.uint8))
    assert ok_
    return buf.tobytes()


@POSIX_MODE_BITS
@pytest.mark.parametrize("data", [b"\x00bytes", "text é"])
def test_write_private_creates_and_overwrites_at_0600(tmp_path, umask_022, data):
    from mcp_stealth_chrome.helpers import write_private

    fresh, existing = tmp_path / "fresh", tmp_path / "existing"
    existing.write_bytes(b"old contents that are longer")
    existing.chmod(0o644)
    for path in (fresh, existing):
        write_private(path, data)
        assert _mode(path) == 0o600, oct(_mode(path))
        expected = data.encode() if isinstance(data, str) else data
        assert path.read_bytes() == expected


@POSIX_MODE_BITS
def test_staging_publishes_a_private_file_for_a_writer_it_does_not_control(tmp_path, umask_022):
    """nodriver's save_screenshot opens its filename itself, with Path.write_bytes."""
    from mcp_stealth_chrome.helpers import private_staging

    path = _existing(tmp_path / "shot.png")
    with private_staging(path) as staged:
        assert staged.parent == tmp_path and _mode(staged) == 0o600
        assert path.read_bytes() == b"stale", "the old file stays until the new one is whole"
        staged.write_bytes(b"picture")
    assert _mode(path) == 0o600 and path.read_bytes() == b"picture"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shot.png"]


def test_staging_that_got_nothing_publishes_nothing(tmp_path):
    from mcp_stealth_chrome.helpers import private_staging

    path = tmp_path / "shot.png"
    path.write_bytes(b"old")
    with pytest.raises(RuntimeError):
        with private_staging(path):
            pass
    with pytest.raises(ValueError):
        with private_staging(path) as staged:
            staged.write_bytes(b"half")
            raise ValueError("capture failed")
    assert path.read_bytes() == b"old" and sorted(p.name for p in tmp_path.iterdir()) == ["shot.png"]


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs a privilege there")
def test_write_private_replaces_a_symlink_instead_of_writing_through_it(tmp_path):
    """os.open followed a symlink at the final path and overwrote a file outside the folder."""
    from mcp_stealth_chrome.helpers import write_private

    outside = tmp_path / "outside"
    outside.write_bytes(b"not yours")
    managed = tmp_path / "managed"
    managed.mkdir()
    link = managed / "shot.png"
    link.symlink_to(outside)
    write_private(link, b"secret")
    assert outside.read_bytes() == b"not yours"
    assert not link.is_symlink() and link.read_bytes() == b"secret"


def test_write_private_never_shows_a_partial_file(tmp_path, monkeypatch):
    """The file was truncated and then written in place: a reader saw it empty or half done."""
    from mcp_stealth_chrome import helpers

    path = tmp_path / "trace.json"
    path.write_bytes(b"old")
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append((path.read_bytes(), open(src, "rb").read()))
        return real_replace(src, dst)

    monkeypatch.setattr(helpers.os, "replace", watching_replace)
    helpers.write_private(path, b"new contents")
    assert seen == [(b"old", b"new contents")]
    assert path.read_bytes() == b"new contents"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trace.json"]


def _existing(path):
    path.write_bytes(b"stale")
    path.chmod(0o644)
    return path


@POSIX_MODE_BITS
@pytest.mark.parametrize("resize", [False, True])
def test_screenshot_file_is_private(monkeypatch, tmp_path, umask_022, resize):
    from mcp_stealth_chrome.tools import page

    class _Tab:
        async def save_screenshot(self, filename, format="png", full_page=False, **_kw):
            from pathlib import Path

            Path(filename).write_bytes(_png(40, 20))
            return filename

    monkeypatch.setattr(page, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(page, "ensure_dirs", lambda: None)
    monkeypatch.setattr(page, "select_tab", lambda _tab=None: _Tab())
    path = _existing(tmp_path / "s.png")
    out = asyncio.run(_tool("screenshot")(filename="s.png", output_mode="path",
                                         max_dimension=10 if resize else 0))
    assert not out.startswith("Error"), out
    assert _mode(path) == 0o600
    assert ("resized" in out) is resize


@POSIX_MODE_BITS
def test_screenshot_region_file_is_private(monkeypatch, tmp_path, umask_022):
    from mcp_stealth_chrome import image_output
    from mcp_stealth_chrome.tools import page

    tab = _CdpTab()
    tab.replies["Page.captureScreenshot"] = lambda _t: base64.b64encode(_png()).decode()

    async def context(_tab):
        return {}

    monkeypatch.setattr(page, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(page, "ensure_dirs", lambda: None)
    monkeypatch.setattr(page, "select_tab", lambda _tab=None: tab)
    monkeypatch.setattr(image_output, "viewport_context", context)
    monkeypatch.setattr(image_output, "viewport_clip",
                        lambda region, _ctx: {"x": 0, "y": 0, "width": 4, "height": 4})
    out = asyncio.run(_tool("screenshot")(filename="r.png", output_mode="path", max_dimension=0,
                                         region={"x": 0, "y": 0, "width": 4, "height": 4}))
    assert not out.startswith("Error"), out
    assert _mode(tmp_path / "r.png") == 0o600


@POSIX_MODE_BITS
def test_save_pdf_file_is_private(monkeypatch, tmp_path, umask_022):
    from mcp_stealth_chrome.tools import page

    tab = _CdpTab()
    tab.replies["Page.printToPDF"] = lambda _t: (base64.b64encode(b"%PDF-1.4").decode(), None)
    _active(monkeypatch, tab)
    monkeypatch.setattr(page, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(page, "ensure_dirs", lambda: None)
    path = _existing(tmp_path / "p.pdf")
    out = asyncio.run(_tool("save_pdf")(filename="p.pdf"))
    assert not out.startswith("Error"), out
    assert _mode(path) == 0o600 and path.read_bytes() == b"%PDF-1.4"


@POSIX_MODE_BITS
def test_trace_and_heap_files_are_private(monkeypatch, tmp_path, umask_022):
    from nodriver.cdp import heap_profiler as cdp_heap, tracing as cdp_tracing

    from mcp_stealth_chrome.helpers import target_key
    from mcp_stealth_chrome.tools import devtools

    tab = _CdpTab()

    def end_trace(t):
        for handler in list(t.handlers.get(cdp_tracing.TracingComplete, [])):
            handler(SimpleNamespace())

    def heap_chunk(t):
        for handler in list(t.handlers.get(cdp_heap.AddHeapSnapshotChunk, [])):
            handler(SimpleNamespace(chunk='{"snapshot": {}}'))

    tab.replies["Tracing.end"] = end_trace
    tab.replies["HeapProfiler.takeHeapSnapshot"] = heap_chunk
    _active(monkeypatch, tab)
    monkeypatch.setattr(devtools, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(devtools, "ensure_dirs", lambda: None)
    monkeypatch.setitem(devtools._TRACE_ACTIVE, "tab_id", target_key(tab))
    monkeypatch.setitem(devtools._TRACE_ACTIVE, "started_at", 0.0)
    monkeypatch.setitem(devtools._TRACE_ACTIVE, "handler", None)
    trace, heap = _existing(tmp_path / "t.json"), _existing(tmp_path / "h.heapsnapshot")
    out = asyncio.run(_tool("performance_trace_stop")(filename="t.json"))
    assert not out.startswith("Error"), out
    out = asyncio.run(_tool("memory_heap_snapshot")(filename="h.heapsnapshot", stable_ms=0,
                                                    max_wait=2))
    assert not out.startswith("Error"), out
    assert _mode(trace) == 0o600 and _mode(heap) == 0o600
    assert json.loads(heap.read_text()) == {"snapshot": {}}


@POSIX_MODE_BITS
def test_screenshot_element_and_inspect_image_files_are_private(monkeypatch, tmp_path, umask_022):
    from mcp_stealth_chrome import image_output

    png = _png()
    box = {"found": True, "x": 0, "y": 0, "w": 4, "h": 4, "dpr": 1}

    async def measure(*_a, **_k):
        return box

    async def capture(*_a, **_k):
        return png

    async def source(*_a, **_k):
        return "https://x.test/a.png", None, ""

    async def fetch(*_a, **_k):
        return png, {"status": 200, "mime": "image/png"}

    monkeypatch.setattr(image_output, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(image_output, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(image_output, "ensure_image_dirs", lambda: None)
    monkeypatch.setattr(image_output, "select_tab", lambda _tab=None: object())
    monkeypatch.setattr(image_output, "measure_box", measure)
    monkeypatch.setattr(image_output, "capture_clip", capture)
    monkeypatch.setattr(image_output, "verify_capture", lambda *_a: (4, 4, None))
    monkeypatch.setattr(image_output, "_resolve_image_source", source)
    monkeypatch.setattr(image_output, "_fetch_image_bytes", fetch)
    element, asset = _existing(tmp_path / "e.png"), _existing(tmp_path / "a.png")
    out = asyncio.run(_tool("screenshot_element")(selector="#x", filename="e.png", output="path"))
    assert not out.startswith("Error"), out
    out = asyncio.run(_tool("inspect_image")(url="https://x.test/a.png", filename="a.png",
                                            output="path"))
    assert not out.startswith("Error"), out
    assert _mode(element) == 0o600 and _mode(asset) == 0o600


def test_page_derived_files_are_only_written_through_write_private():
    """A new writer in these modules would bring a 0644 file back."""
    import re
    from pathlib import Path

    import mcp_stealth_chrome

    root = Path(mcp_stealth_chrome.__file__).parent
    for name in ("tools/page.py", "image_output.py", "tools/devtools.py", "tools/vision.py",
                 "tools/mouse_kit.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert not re.search(r"\.write_(bytes|text)\(|cv2\.imwrite\(", source), name
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if ".save_screenshot(" in line and "def " not in line and "#" not in line.split(
                    ".save_screenshot(")[0]:
                window = "\n".join(lines[max(0, index - 4):index + 1])
                assert "private_staging(" in window and "filename=str(staged)" in window or (
                    '"filename": str(staged)' in window), f"{name}:{index + 1} writes in place"


# ── 5. A click whose outcome could not be read ──


class _Pos:
    left, top, width, height = 100.0, 100.0, 100.0, 40.0


class _ClickEl:
    async def get_position(self):
        return _Pos()

    async def apply(self, js):
        from mcp_stealth_chrome import server

        if js == server._CLICK_PROBE_JS:
            return json.dumps({"sized": True, "in_viewport": True, "hit": True,
                               "target": "button"})
        return True


class _MouseTab:
    def __init__(self):
        self.clicks = []

    async def mouse_click(self, x, y):
        self.clicks.append((x, y))

    async def send(self, _command):
        return None


@pytest.mark.parametrize("before,after", [({}, {}), ({}, {"url": "u"}), ({"url": "u"}, {})])
def test_click_outcome_says_when_it_could_not_verify(before, after):
    from mcp_stealth_chrome import server

    out = server._click_outcome("click 'x': clicked", "method=cdp_mouse", before, after)
    assert "could NOT be verified" in out, out
    assert out != "click 'x': clicked — method=cdp_mouse"


def test_a_verified_click_on_an_unreadable_page_is_not_a_bare_clicked(monkeypatch):
    """_page_sig answers {} when the page cannot be read, and the click said "clicked"."""
    from mcp_stealth_chrome import server

    async def settled(el):
        return await el.get_position()

    async def unreadable(*_a, **_k):
        return {}

    monkeypatch.setattr(server, "_settled_position", settled)
    monkeypatch.setattr(server, "_page_sig", unreadable)
    tab = _MouseTab()
    out = asyncio.run(server._click_and_report(tab, _ClickEl(), "click 'x'", True))
    assert tab.clicks == [(150, 120)]
    assert "could NOT be verified" in out and "before or after" in out, out


# ── 6. batch_actions with input that is not a list of actions ──


@pytest.mark.parametrize("actions", ["not-json", '{"type": "click"}', "[]", [], 7])
def test_batch_actions_refuses_input_that_is_not_a_list_of_actions(actions):
    out = asyncio.run(_tool("batch_actions")(actions))
    assert out.startswith("Error:") and "nothing ran" in out, out


def test_batch_actions_counts_a_non_object_item_as_a_failure(monkeypatch):
    from mcp_stealth_chrome import server

    server._register_workflow_tools()
    ran = []

    async def wait_for(**kw):
        ran.append(kw)
        return "waited"

    monkeypatch.setitem(server._WORKFLOW_TOOLS, "wait_for", wait_for)
    out = asyncio.run(_tool("batch_actions")(["click", {"type": "wait", "selector": "#a"}, None]))
    assert out.startswith("3 actions, 2 failed"), out
    assert "[0] ?: ERR not an action object but str" in out and len(ran) == 1


# ── Review of the fixes ──
# (a) A restart after a failed Fetch.disable dropped the old tab's handler.


def test_a_restart_on_another_tab_after_a_failed_disable_is_refused(monkeypatch, intercept):
    """Tab A's handler was dropped while Fetch stayed on there: A's pauses went unanswered, and
    the next stop disabled only B and said "stopped"."""
    from nodriver.cdp import fetch as cdp_fetch

    from mcp_stealth_chrome import server

    tab_a, tab_b = _CdpTab("A", fail=("Fetch.disable",)), _CdpTab("B")
    current = {"tab": tab_a}
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: current["tab"]))

    async def scenario():
        await _tool("intercept_start")(action="cancel", auto_disable_after=0)
        await _tool("intercept_stop")()
        current["tab"] = tab_b
        out = await _tool("intercept_start")(auto_disable_after=0)
        await tab_a.fire(cdp_fetch.RequestPaused, **_paused())
        log = json.loads(await _tool("intercept_log")())
        return out, log

    out, log = asyncio.run(scenario())
    assert out.startswith("Error:") and "NOT started" in out, out
    assert len(tab_a.handlers[cdp_fetch.RequestPaused]) == 1, "tab A lost its only answer"
    assert tab_a.methods().count("Fetch.continueRequest") == 1
    assert tab_b.handlers.get(cdp_fetch.RequestPaused, []) == [] and tab_b.sent == []
    assert intercept["tab"] is tab_a and log["active"] is True and log["disable_error"]


def test_a_restart_on_the_same_tab_answers_each_pause_once(monkeypatch, intercept):
    """The swap on one tab is safe: nothing awaits between dropping and adding a handler."""
    from nodriver.cdp import fetch as cdp_fetch

    tab = _active(monkeypatch, _CdpTab(fail=("Fetch.disable",)))

    async def scenario():
        await _tool("intercept_start")(action="cancel", auto_disable_after=0)
        await _tool("intercept_stop")()
        out = await _tool("intercept_start")(action="mock", auto_disable_after=0)
        await tab.fire(cdp_fetch.RequestPaused, **_paused())
        return out

    out = asyncio.run(scenario())
    assert not out.startswith("Error"), out
    assert len(tab.handlers[cdp_fetch.RequestPaused]) == 1
    answers = [m for m in tab.methods() if m in ("Fetch.continueRequest", "Fetch.failRequest",
                                                  "Fetch.fulfillRequest")]
    assert answers == ["Fetch.fulfillRequest"], answers


# (d) The redirect chain on a failing hop, and Location headers.


def test_a_failure_after_a_redirect_reports_the_chain_masked(two_origins):
    """The error named only the first URL: the hop that failed and the chain were gone, and the
    hop's URL, from a Location the server chose, was not masked."""
    o = two_origins
    out = _request(f"http://127.0.0.1:{o.a}"
                   + _secret_hop(302, "http://u:SECRET@127.0.0.1:1/x?access_token=SECRET#id_token=SECRET"),
                   timeout=5)
    assert out.startswith("Error:") and "SECRET" not in out, out
    detail = json.loads(out.split("\n", 1)[1])
    assert detail["redirects"][0]["status"] == 302 and "after 1 redirect(s)" in out
    # curl does not send the fragment, and resp.url reports what was requested.
    assert detail["failed_at"].startswith("http://***@127.0.0.1:1/x?access_token=***")
    # The first server did get the request, so nothing says it was never executed.
    assert detail["outcome"] == "unknown" and detail["retry_safe_any_method"] is False


def test_a_location_header_is_masked(two_origins):
    o = two_origins
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}"
                               + _secret_hop(302, "https://u:SECRET@x.test/cb?a=1&access_token=SECRET"),
                               follow_redirects=False))
    location = next(v for k, v in meta["headers"].items() if k.lower() == "location")
    assert location == "https://***@x.test/cb?a=1&access_token=***"
    revealed = json.loads(_request(f"http://127.0.0.1:{o.a}" + _secret_hop(302, "https://u:S@x.test/"),
                                   follow_redirects=False, reveal_values=True))
    assert "u:S@" in json.dumps(revealed["headers"])


@pytest.mark.parametrize("url,expected", [
    ("user:SECRET@example.test/path", "***@example.test/path"),
    ("user:SECRET@example.test", "***@example.test"),
])
def test_mask_url_hides_userinfo_without_a_scheme(url, expected):
    from mcp_stealth_chrome.helpers import mask_url

    assert mask_url(url) == expected


@pytest.mark.parametrize("url", ["mailto:me@example.test", "@scope/pkg", "example.test/@me?x=1"])
def test_mask_url_leaves_addresses_and_at_paths_alone(url):
    from mcp_stealth_chrome.helpers import mask_url

    assert mask_url(url) == url


def test_url_valued_headers_are_masked_everywhere_headers_are():
    from mcp_stealth_chrome.helpers import mask_headers

    out = mask_headers({"Location": "/next#access_token=SECRET", "Referer": "https://x/?a=1",
                        "Content-Location": "https://u:SECRET@x/", "X-Plain": "keep?token=x"})
    assert "SECRET" not in json.dumps(out)
    assert out["Referer"] == "https://x/?a=1" and out["X-Plain"] == "keep?token=x"


# Policy, not a leak: navigation tools return the page's URL as it is, because agents work with
# it (a workflow variable, a URL to open again). Masking applies to the HTTP client's output
# and to captured traffic. A previous round masked these and broke that use.

_TOKEN_URL = "https://u:SECRET@x.test/cb?a=1&access_token=SECRET#id_token=SECRET&state=s"


class _UrlTab:
    def __init__(self, url=_TOKEN_URL):
        self.url = url
        self.target = SimpleNamespace(target_id="U1")

    async def evaluate(self, expression, return_by_value=True, **_kw):
        if "JSON.stringify([window.location.href, document.title])" in expression:
            return json.dumps([self.url, "Callback"])
        if "title" in expression:
            return "Callback"
        return self.url


def test_navigation_tools_return_the_url_as_it_is(monkeypatch):
    from mcp_stealth_chrome import server

    async def no_problem():
        return None

    _active(monkeypatch, _UrlTab())
    monkeypatch.setattr(server, "_tab_list_problem", no_problem)
    monkeypatch.setattr(server.BrowserState, "tabs", [_UrlTab()])
    monkeypatch.setattr(server.BrowserState, "active_tab_index", 0)
    assert asyncio.run(_tool("get_url")()) == _TOKEN_URL
    assert _TOKEN_URL in asyncio.run(_tool("tab_list")())
    observed = {"requested_url": "https://x.test/login", "final_url": _TOKEN_URL,
                "redirect_chain": [], "status": 200, "ready_state": "complete"}
    assert _TOKEN_URL in server._nav_result("Navigated", observed)
    assert _TOKEN_URL in server._click_change({"url": "https://x.test/"}, {"url": _TOKEN_URL})


def test_a_workflow_variable_keeps_the_real_url(monkeypatch):
    """get_url saved as ${u} and opened again must be the same URL, token and all."""
    from mcp_stealth_chrome import server

    _active(monkeypatch, _UrlTab())
    server._register_workflow_tools()
    opened = []

    async def navigate(**kw):
        opened.append(kw["url"])
        return "Navigated"

    monkeypatch.setitem(server._WORKFLOW_TOOLS, "navigate", navigate)
    out = asyncio.run(_tool("workflow_run")([
        {"tool": "get_url", "save_as": "u"},
        {"tool": "navigate", "args": {"url": "${u}"}},
    ]))
    assert opened == [_TOKEN_URL], (opened, out)


# F5 and F6 of the second review: the chain is kept real and masked only on output, and a
# deadline or a cancel after a redirect names the chain and the hop it stopped on.


def test_the_record_keeps_real_hops_and_only_the_output_is_masked(two_origins):
    from mcp_stealth_chrome.tools import network_http

    o = two_origins
    secret_hop = f"http://127.0.0.1:{o.a}/final?access_token=SECRET"
    meta = json.loads(_request(f"http://127.0.0.1:{o.a}" + _secret_hop(302, secret_hop)))
    assert "SECRET" not in json.dumps(meta["redirects"])
    record = network_http._TRACKED[meta["request_id"]]
    assert record.hops[0]["to"] == secret_hop, record.hops


def _hanging_after_a_redirect(o):
    return f"http://127.0.0.1:{o.a}" + _secret_hop(
        302, f"http://127.0.0.1:{o.a}/hang?access_token=SECRET")


def test_a_deadline_after_a_redirect_names_the_chain(two_origins, monkeypatch):
    from mcp_stealth_chrome.tools import network_http

    monkeypatch.setattr(network_http, "HTTP_DEADLINE_GRACE", -4.0)  # deadline 1 s, curl's 4.5 s
    out = _request(_hanging_after_a_redirect(two_origins), timeout=5, connect_timeout=0.5)
    assert "operation deadline" in out and "after 1 redirect(s)" in out, out
    assert "SECRET" not in out
    detail = json.loads(out.split("\n", 1)[1])
    assert detail["redirects"][0]["status"] == 302
    assert detail["failed_at"].endswith("/hang?access_token=***")


def test_a_cancel_after_a_redirect_names_the_chain(two_origins):
    from mcp_stealth_chrome.tools import network_http

    async def scenario():
        pending = await _tool("http_request")(_hanging_after_a_redirect(two_origins),
                                              timeout=10, wait_timeout=0)
        request_id = json.loads(pending.split("\n", 1)[1])["request_id"]
        record = network_http._TRACKED[request_id]
        for _ in range(300):
            if record.hops:
                break
            await asyncio.sleep(0.01)
        cancel = await _tool("http_request_cancel")(request_id)
        await asyncio.sleep(0.05)
        return cancel, await _tool("http_request_status")(request_id)

    cancel, out = asyncio.run(scenario())
    assert "cancelled" in out and "after 1 redirect(s)" in out, out
    assert "SECRET" not in out
    assert json.loads(out.split("\n", 1)[1])["failed_at"].endswith("/hang?access_token=***")
    # The cancel's own reply: a caller reading only it must still see the redirect it stopped on.
    reply = json.loads(cancel)
    assert "SECRET" not in cancel
    assert reply["cancelled"] is True and reply["redirects"][0]["status"] == 302, reply
    assert reply["failed_at"].endswith("/hang?access_token=***"), reply
