"""What only shows over the protocol, needing no browser: FastMCP parses arguments and the
client validates results, and calling a tool function directly skips both."""
import http.server
import json
import threading

import pytest

from _protocol import Client
from mcp_stealth_chrome import helpers


class _Echo(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *_args):
        pass

    def _send(self, body: str, ctype="application/json", extra=()):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        _Echo.seen.append(body)
        self._send("{}")

    def do_GET(self):
        if self.path.startswith("/hang"):
            import time
            time.sleep(2)
        if self.path.startswith("/go"):
            self.send_response(302)
            self.send_header("Location", "/tokens?access_token=SYNTHURLSECRET")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(json.dumps({
            "ok": True, "token": "SYNTHBODYSECRET",
            "auth": {"value": "SYNTHNESTEDSECRET"},
            "session": {"user": "budi", "token": "SYNTHSESSIONSECRET"},
        }), extra=[("Set-Cookie", "s=SYNTHCOOKIESECRET; Path=/")])


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def mcp():
    client = Client()
    yield client
    client.close()


@pytest.mark.parametrize("value", ['{"a":1}', "[1]", "null", "true", "123"])
def test_text_that_looks_like_json_reaches_the_tool_unchanged(mcp, base, value):
    """http_request(data=...) failed validation or lost the body: see patches.py."""
    reply = mcp.call("http_request", url=f"{base}/x", method="POST", data=value)
    assert not reply.failed, reply.text
    assert _Echo.seen[-1] == value


def test_export_curl_form_of_json_body_works_with_session_tool(mcp, base):
    reply = mcp.call("http_request_with_session", url=f"{base}/x", method="POST", data='{"a":1}')
    assert not reply.failed, reply.text
    assert _Echo.seen[-1] == '{"a":1}'


def _listed_tools(mcp):
    import asyncio
    return asyncio.run_coroutine_threadsafe(mcp.session.list_tools(), mcp._loop).result(30).tools


def _invalid_other_argument(tool, keep: str) -> dict | None:
    """A wrong value for another argument, so validation stops the call before the tool runs."""
    for name, prop in tool.inputSchema.get("properties", {}).items():
        kinds = {prop.get("type")} | {a.get("type") for a in prop.get("anyOf", [])}
        if name != keep and "string" not in kinds and kinds & {"integer", "number", "boolean", "array", "object"}:
            return {name: "not-valid"}
    return None


def test_every_optional_text_parameter_takes_json_looking_text(mcp):
    """Validation only: each call is stopped by another bad argument, so nothing runs, and the
    one question is whether the text parameter was reported as invalid too."""
    checked = 0
    for tool in _listed_tools(mcp):
        for name, prop in tool.inputSchema.get("properties", {}).items():
            if not any(a.get("type") == "string" for a in prop.get("anyOf", [])):
                continue
            stop = _invalid_other_argument(tool, name)
            if stop is None:
                continue
            reply = mcp.call(tool.name, _timeout=20, **{name: "[1]"}, **stop)
            assert reply.is_error, f"{tool.name} ran: {reply.text[:120]}"
            assert f"\n{name}\n" not in reply.text, f"{tool.name}.{name}: {reply.text[:200]}"
            checked += 1
    assert checked >= 100


def test_descriptions_are_clean_on_every_listed_tool(mcp):
    # The tool count is pinned in test_registry; here we only guard description hygiene.
    tools = _listed_tools(mcp)
    assert all("⭐" not in (t.description or "") for t in tools)
    assert not [t.name for t in tools if "removed v0.2.0" in (t.description or "")]


def test_compat_patch_is_reported(mcp):
    patches = mcp.call("server_status").json()["compat_patches"]
    assert "fastmcp_keeps_text" in patches["applied"] and patches["healthy"]


def test_file_url_is_refused(mcp, tmp_path):
    secret = tmp_path / "s.txt"
    secret.write_text("SYNTHLOCALFILE")
    for tool in ("http_request", "http_request_with_session"):
        reply = mcp.call(tool, url=f"file://{secret}")
        assert reply.failed and "SYNTHLOCALFILE" not in reply.text, reply.text


def test_http_request_hides_credentials_in_body_and_url(mcp, base):
    reply = mcp.call("http_request", url=f"{base}/go")
    assert not reply.failed, reply.text
    for secret in ("SYNTHURLSECRET", "SYNTHBODYSECRET", "SYNTHNESTEDSECRET", "SYNTHSESSIONSECRET", "SYNTHCOOKIESECRET"):
        assert secret not in reply.text, secret
    assert "budi" in reply.text
    shown = mcp.call("http_request", url=f"{base}/go", reveal_values=True)
    assert "SYNTHURLSECRET" in shown.text and "SYNTHBODYSECRET" in shown.text


def test_pending_and_status_hide_the_url_token(mcp, base):
    reply = mcp.call("http_request", url=f"{base}/hang?access_token=SYNTHPENDING", timeout=10, wait_timeout=0.1)
    assert "SYNTHPENDING" not in reply.text and "still running" in reply.text
    listing = mcp.call("http_request_status")
    assert "SYNTHPENDING" not in listing.text and "access_token=" in listing.text


def test_mask_url_leaves_ordinary_parameters_alone():
    assert helpers.mask_url("https://x.test/?q=%FF&access_token=S&b=c%20d") == \
        "https://x.test/?q=%FF&access_token=***&b=c%20d"
    assert helpers.mask_url("https://x.test/?q=%FF&b=1") == "https://x.test/?q=%FF&b=1"


def test_secret_key_hides_what_is_under_it_except_session():
    masked = json.loads(helpers.mask_body(json.dumps({
        "token": {"value": "S1"}, "credentials": ["S2", {"x": "S3"}],
        "session": {"user": "budi", "password": "S4"},
    })))
    text = json.dumps(masked)
    assert not any(s in text for s in ("S1", "S2", "S3", "S4"))
    assert masked["session"]["user"] == "budi"


def test_server_status_says_whether_the_os_cursor_backend_can_run(mcp):
    """A missing [os-cursor] extra showed only when solve_turnstile_oscursor was called."""
    from mcp_stealth_chrome import os_input

    seen = mcp.call("server_status").json()["os_cursor"]
    available, _reason = os_input.is_available()
    assert seen["available"] is available and seen["detail"], seen
