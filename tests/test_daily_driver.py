"""Sharing one browser with a person, and the daily-driver guards.

- The debugging port refuses web pages (no --remote-allow-origins=*).
- tab_list never hangs on a tab showing a dialog, and says which tab and why.
- An active tab closed outside the tools is said, never replaced by another tab in silence.
- Tools act only on tabs the server opened or took over, unless tab_select picks one.
- upload_file(drop=True) drops a file on a drop zone.
- clone_chrome_profile names the site data it left out, and keep_site_data copies it.

The person is a second CDP client: Chrome sends the same target events for Cmd+T, a link the
person follows, or a tab the person closes. E2E parts need MCP_E2E=1.
"""
import asyncio
import http.server
import json
import os
import pathlib
import threading
import time

import pytest

from mcp_stealth_chrome import patches


# ── fast ────────────────────────────────────────────────────────────────────

def test_nodriver_no_longer_passes_remote_allow_origins():
    from nodriver.core.config import Config
    patches.apply_all()
    args = Config(headless=True)()
    assert not [a for a in args if a.startswith("--remote-allow-origins")], args
    assert "no_remote_allow_origins" in patches.status()["applied"]


def test_a_callers_own_remote_allow_origins_is_kept():
    from nodriver.core.config import Config
    patches.apply_all()
    args = Config(headless=True, browser_args=["--remote-allow-origins=http://localhost:9"])()
    assert "--remote-allow-origins=http://localhost:9" in args, args
    assert "--remote-allow-origins=*" not in args, args


def _source_profile(root: pathlib.Path) -> pathlib.Path:
    default = root / "Default"
    for name, size in (("IndexedDB", 3000), ("Service Worker", 2000), ("File System", 1000),
                       ("Cache", 5000), ("Local Storage", 100)):
        (default / name).mkdir(parents=True)
        (default / name / "blob").write_bytes(b"x" * size)
    (default / "Preferences").write_text("{}")
    (root / "Local State").write_text("{}")
    return root


def _clone(tmp_path, monkeypatch, **kw) -> tuple[dict, pathlib.Path]:
    from mcp_stealth_chrome import server
    root = _source_profile(tmp_path / "Google" / "Chrome")
    data = tmp_path / "data"
    monkeypatch.setattr(server, "PROFILE_DIR", data / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", data / "profiles")
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server, "chrome_user_data_root", lambda b=None: root)
    monkeypatch.setattr(server, "chrome_user_data_roots", lambda: [("chrome", root)])
    text = asyncio.run(server.clone_chrome_profile(target_instance_id="daily", browser="chrome", **kw))
    assert not text.startswith("Error"), text
    return json.loads(text[text.index("{"):]), data / "profiles" / "daily" / "Default"


def test_clone_names_the_site_data_it_left_out(tmp_path, monkeypatch):
    result, clone = _clone(tmp_path, monkeypatch)
    assert set(result["not_copied"]) == {"IndexedDB", "Service Worker", "File System"}, result
    assert "sign in again" in result["not_copied_means"]
    assert "keep_site_data=True" in result["not_copied_means"]
    assert not (clone / "IndexedDB").exists() and not (clone / "Cache").exists()
    assert (clone / "Local Storage").exists()


def test_keep_site_data_copies_it_and_still_skips_the_caches(tmp_path, monkeypatch):
    result, clone = _clone(tmp_path, monkeypatch, keep_site_data=True)
    assert "not_copied" not in result, result
    assert "keep_site_data=True" in result["site_data"]
    for name in ("IndexedDB", "Service Worker", "File System"):
        assert (clone / name / "blob").exists(), name
    assert not (clone / "Cache").exists()


# ── real Chrome ─────────────────────────────────────────────────────────────

e2e = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome; set MCP_E2E=1 to run")

PAGES = {
    "/plain": "<title>plain</title><p id=p>plain</p>",
    "/person": "<title>person</title><p>the person's page</p>",
    "/alert": "<title>alerting</title><script>setTimeout(() => alert('a person'), 300)</script>",
    "/opener": ("<title>opener</title>"
                "<button id=pop onclick=\"window.open('/plain?popup', '_blank')\">popup</button>"
                "<a id=noopener href='/plain?noopener' target=_blank rel=noopener>link</a>"
                "<button id=key onclick=\"window.open('/plain?bykey', '_blank')\">by key</button>"),
    "/drop": ("<title>drop</title><div id=drop style='width:300px;height:120px;border:1px dashed'>drop"
              "</div><p id=none style='width:300px;height:60px'>not a drop zone</p><p id=got></p><script>"
              "const d = document.getElementById('drop');"
              "d.addEventListener('dragover', e => e.preventDefault());"
              "d.addEventListener('drop', e => { e.preventDefault(); const f = e.dataTransfer.files[0];"
              " document.getElementById('got').textContent = f ? f.name + ':' + f.size : 'none'; });"
              "</script>"),
}


class _Pages(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "").encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


def call(loop, name, **kw):
    from mcp_stealth_chrome.server import mcp
    r = loop.run_until_complete(asyncio.wait_for(mcp.call_tool(name, kw), 60))
    blocks = r[0] if isinstance(r, tuple) else r
    return "\n".join(getattr(b, "text", str(b)) for b in blocks)


@pytest.fixture
def browser(loop, site):
    out = call(loop, "browser_launch", url=site + "/plain", headless=True, persistent=False)
    assert "Browser launched" in out, out
    try:
        yield
    finally:
        call(loop, "browser_close")


def _browser_ws():
    from mcp_stealth_chrome.state import BrowserState
    b = BrowserState.browser
    return getattr(b, "websocket_url", None) or b.info.webSocketDebuggerUrl


class Person:
    """A second CDP client on the same browser: what a person's hands make Chrome do."""

    def __init__(self, loop):
        import websockets
        self.loop = loop
        self.ids = iter(range(1, 10_000))
        self.ws = loop.run_until_complete(websockets.connect(_browser_ws()))

    def send(self, method, params=None, session=None):
        async def go():
            msg = {"id": next(self.ids), "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await self.ws.send(json.dumps(msg))
            while True:
                reply = json.loads(await self.ws.recv())
                if reply.get("id") == msg["id"]:
                    return reply.get("result", reply.get("error"))
        return self.loop.run_until_complete(asyncio.wait_for(go(), 15))

    def open_tab(self, url, watch_dialogs=False):
        target = self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        if watch_dialogs:
            self.send("Page.enable", session=session)
        self.send("Page.navigate", {"url": url}, session=session)
        self.loop.run_until_complete(asyncio.sleep(1.0))
        return target, session

    def close(self):
        self.loop.run_until_complete(self.ws.close())


@pytest.fixture
def person(loop, browser):
    p = Person(loop)
    try:
        yield p
    finally:
        p.close()


def _index_of(loop, needle):
    rows = call(loop, "tab_list").splitlines()[1:]
    return next(int(r.split("]")[0].split("[")[1]) for r in rows if needle in r)


@e2e
def test_a_page_that_knows_the_websocket_url_cannot_drive_the_browser(loop, browser):
    ws = _browser_ws()
    reply = call(loop, "evaluate", expression=f"""new Promise(r => {{ const s = new WebSocket({json.dumps(ws)});
        const t = setTimeout(() => r('timeout'), 5000);
        s.onopen = () => {{ s.send(JSON.stringify({{id: 1, method: 'Browser.getVersion'}})); }};
        s.onmessage = m => {{ clearTimeout(t); r('OPEN ' + m.data.slice(0, 40)); }};
        s.onerror = () => {{ clearTimeout(t); r('refused'); }}; }})""")
    assert reply == "refused", reply
    assert call(loop, "evaluate", expression="1 + 1") == "2"


@e2e
def test_tab_list_answers_while_a_persons_tab_shows_a_dialog(loop, person, site):
    _target, session = person.open_tab(site + "/alert", watch_dialogs=True)
    try:
        started = time.monotonic()
        listed = call(loop, "tab_list")
        assert time.monotonic() - started < 15
        row = next(line for line in listed.splitlines() if "/alert" in line)
        assert "a native dialog is probably open" in row, listed
        assert "not opened by this server" in row, listed
        assert "plain" in listed, listed
    finally:
        person.send("Page.handleJavaScriptDialog", {"accept": True}, session=session)


@e2e
def test_an_active_tab_the_person_closes_is_said_and_not_replaced(loop, person, site):
    from mcp_stealth_chrome.state import BrowserState
    person.open_tab(site + "/person")
    active = BrowserState.tabs[BrowserState.active_tab_index]
    person.send("Target.closeTarget", {"targetId": str(active.target.target_id)})
    loop.run_until_complete(asyncio.sleep(0.5))
    url = call(loop, "get_url")
    assert url.startswith("Error") and "closed outside this server's tools" in url, url
    assert "/person" not in url
    listed = call(loop, "tab_list")
    assert listed.splitlines()[0].startswith("Active tab: none"), listed
    clicked = call(loop, "click", selector="p")
    assert clicked.startswith("Error") and "closed outside" in clicked, clicked


@e2e
def test_tools_never_slide_onto_a_persons_tab(loop, person, site):
    person.open_tab(site + "/person")
    listed = call(loop, "tab_list")
    assert "not opened by this server" in next(r for r in listed.splitlines() if "/person" in r), listed
    closed = call(loop, "tab_close")  # the agent closes its own, last tab of its own
    assert closed.startswith("closed"), closed
    url = call(loop, "get_url")
    assert url.startswith("Error") and "/person" not in url, url
    assert "opened outside this server" in url, url
    # Picking it by index is the explicit request, and the result says whose tab it is.
    picked = call(loop, "tab_select", index=_index_of(loop, "/person"))
    assert "not opened by this server" in picked, picked
    assert call(loop, "get_url").endswith("/person")


@e2e
def test_a_browser_whose_tabs_are_all_its_own_behaves_as_before(loop, browser, site):
    assert call(loop, "navigate", url=site + "/opener").startswith("Navigated")
    assert "new tab" in call(loop, "click", selector="#pop")
    assert "new tab" in call(loop, "click", selector="#noopener")
    # A popup a key press opens: no click result lists it, its opener makes it ours.
    call(loop, "evaluate", expression="document.getElementById('key').focus(); 1")
    call(loop, "press_key", key="Enter")
    assert call(loop, "tab_new", url=site + "/plain?tabnew").startswith("opened tab")
    listed = call(loop, "tab_list")
    assert "?bykey" in listed, listed
    assert "not opened by this server" not in listed, listed
    for needle in ("?popup", "?noopener", "?bykey", "?tabnew"):
        picked = call(loop, "tab_select", index=_index_of(loop, needle))
        assert picked.startswith("switched to tab") and "not opened" not in picked, picked
        assert call(loop, "get_url").endswith(needle)
    call(loop, "tab_select", index=_index_of(loop, "?popup"))
    assert call(loop, "tab_close").startswith("closed")
    assert not call(loop, "get_url").startswith("Error")


@e2e
def test_upload_file_drops_on_a_drop_zone(loop, browser, site, tmp_path):
    payload = tmp_path / "clip.bin"
    payload.write_bytes(b"x" * 4096)
    call(loop, "navigate", url=site + "/drop")
    dropped = call(loop, "upload_file", file_path=str(payload), selector="#drop", drop=True)
    assert "dropped" in dropped and "NO page change" not in dropped, dropped
    assert call(loop, "evaluate", expression="document.getElementById('got').textContent") == "clip.bin:4096"
    ignored = call(loop, "upload_file", file_path=str(payload), selector="#none", drop=True)
    assert "NO page change detected" in ignored and "may not take drops there" in ignored, ignored


def _person_title(person, target):
    session = person.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    return person.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True},
                       session=session)["result"]["value"]


@e2e
def test_a_tab_argument_never_reaches_a_persons_tab(loop, person, site):
    target, _session = person.open_tab(site + "/person")
    idx = _index_of(loop, "/person")
    calls = {
        "evaluate": dict(expression="document.title = 'changed by the agent'; 1", tab=idx),
        "wait_for_function": dict(expression="true", tab=idx, timeout_ms=1000),
        "get_text": dict(tab=idx),
        "screenshot": dict(tab=idx),
        "screenshot_element": dict(selector="p", tab=idx),
        "inspect_image": dict(selector="p", tab=idx),
    }
    for name, kw in calls.items():
        out = call(loop, name, **kw)
        assert "not opened by this server" in out and "Nothing was done" in out, (name, out)
    batch = call(loop, "batch_actions", actions=[{"type": "evaluate", "tab": idx,
                                                   "expression": "document.title = 'batch'; 1"}])
    assert "not opened by this server" in batch, batch
    flow = call(loop, "workflow_run", steps=[{"tool": "evaluate", "args": {
        "tab": idx, "expression": "document.title = 'workflow'; 1"}}])
    assert "not opened by this server" in flow, flow
    assert _person_title(person, target) == "person"
    # tab_select is the one explicit way in; afterwards the index works.
    assert "not opened by this server" in call(loop, "tab_select", index=idx)
    call(loop, "tab_select", index=_index_of(loop, "/plain"))
    assert call(loop, "evaluate", expression="document.title", tab=idx) == "person"


@e2e
def test_tab_close_by_index_never_closes_a_persons_tab(loop, person, site):
    target, _session = person.open_tab(site + "/person")
    idx = _index_of(loop, "/person")
    refused = call(loop, "tab_close", index=idx)
    assert refused.startswith("Error") and "not opened by this server" in refused, refused
    targets = person.send("Target.getTargets")["targetInfos"]
    assert any(t["targetId"] == target for t in targets), "the person's tab was closed"
    assert "/person" in call(loop, "tab_list")
    call(loop, "tab_select", index=idx)
    call(loop, "tab_select", index=_index_of(loop, "/plain"))
    assert call(loop, "tab_close", index=_index_of(loop, "/person")).startswith("closed")


@e2e
def test_a_caller_requested_allow_origins_is_named_and_honoured(loop, site):
    launched = call(loop, "browser_launch", url=site + "/plain", headless=True, persistent=False,
                    extra_args=["--remote-allow-origins=*"])
    try:
        assert "remote_allow_origins=*" in launched and "any web page" in launched, launched
        status = json.loads(call(loop, "server_status"))
        assert status["remote_allow_origins"] == {"main": "*"}, status["remote_allow_origins"]
        ws = _browser_ws()
        opened = call(loop, "evaluate", expression=f"""new Promise(r => {{ const s = new WebSocket({json.dumps(ws)});
            const t = setTimeout(() => r('timeout'), 5000);
            s.onopen = () => {{ clearTimeout(t); r('OPEN'); s.close(); }};
            s.onerror = () => {{ clearTimeout(t); r('refused'); }}; }})""")
        assert opened == "OPEN", opened
        spawned = call(loop, "spawn_browser", instance_id="dd-origins", headless=True,
                       extra_args=["--remote-allow-origins=http://localhost:9"])
        assert "remote_allow_origins=http://localhost:9" in spawned, spawned
        status = json.loads(call(loop, "server_status"))
        assert status["remote_allow_origins"] == {"main": "*", "dd-origins": "http://localhost:9"}, status
    finally:
        call(loop, "close_instance", instance_id="dd-origins")
        call(loop, "browser_close")
    launched = call(loop, "browser_launch", url=site + "/plain", headless=True, persistent=False)
    try:
        assert "remote_allow_origins" not in launched, launched
        assert json.loads(call(loop, "server_status"))["remote_allow_origins"] == {"main": "off"}
    finally:
        call(loop, "browser_close")


@e2e
def test_launching_again_never_ends_a_browser_whose_active_tab_is_not_the_agents(loop, person, site):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.state import BrowserState
    browser = BrowserState.browser
    person.open_tab(site + "/person")
    assert call(loop, "tab_close").startswith("closed")  # the agent's own and only tab
    again = call(loop, "browser_launch", headless=True, persistent=False)
    assert not again.startswith("Error") and "tab_new" in again, again
    assert BrowserState.browser is browser, "the browser was replaced"
    assert server._browser_process_pid(browser) and server._process_exists(server._browser_process_pid(browser))
    opened = call(loop, "browser_launch", url=site + "/plain?again", headless=True, persistent=False)
    assert "new tab" in opened and BrowserState.browser is browser, opened
    assert call(loop, "get_url").endswith("/plain?again")
    assert "/person" in call(loop, "tab_list")
