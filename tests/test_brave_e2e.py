"""browser="brave" on real Brave, through the full server: it stays Brave.

Needs MCP_E2E=1 and Brave installed (skipped otherwise, with the reason). Every server gets
MCP_STEALTH_DATA_DIR in a temporary folder, so Brave's default profiles
(<data>/brave-profile, <data>/brave-profiles/<id>) and the saved tab lists live there and never in
~/.mcp-stealth. Pages come from a local fixture server.

Shields is shown on a local page that loads a script from pagead2.googlesyndication.com, a host
EasyList blocks (`||pagead2.googlesyndication.com^`). --host-resolver-rules maps only that host to
the fixture server, so nothing leaves the machine and a request that is not blocked reaches the
fixture and is counted. A new profile has no filter lists until Brave's component updater fetches
them (measured: about 12 s), so that test needs the network and waits up to two minutes.
"""
import functools
import hashlib
import http.server
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from urllib.parse import quote

import pytest

from _protocol import LOOPBACK_ONLY, Client
from mcp_stealth_chrome import brave, state

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real browser"),
    pytest.mark.skipif(brave.find_brave() is None, reason="Brave is not installed"),
]
FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")
AD_HOST = "pagead2.googlesyndication.com"
AD_RULE = f"--host-resolver-rules=MAP {AD_HOST} 127.0.0.1"
# Same as test_cookie_close_e2e: Windows Chrome reads none of a non-default profile's cookies back.
WINDOWS_COOKIES_NOT_READ_BACK = pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows CI: Chromium reads back none of a non-default profile's cookies on relaunch "
    "(see test_cookie_close_e2e)"))

CANVAS = """(() => {
  const c = document.createElement('canvas'); c.width = 240; c.height = 60;
  const x = c.getContext('2d'); x.textBaseline = 'top'; x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(10, 10, 100, 30); x.fillStyle = '#069';
  x.fillText('farbling probe 123', 4, 20);
  return c.toDataURL();
})()"""


class _Site(http.server.SimpleHTTPRequestHandler):
    ad_hits: list = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if "adsbygoogle" in self.path:
            _Site.ad_hits.append(self.headers.get("Host"))
        return super().do_GET()


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Site, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture
def data():
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-brave-e2e-"))
    try:
        yield root
    finally:
        # Only processes naming this test's own temporary folder.
        for pid, _command in state.processes_naming(root) or []:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.5)
        shutil.rmtree(root, ignore_errors=True)


def _client(data: pathlib.Path) -> Client:
    return Client(env={"MCP_STEALTH_DATA_DIR": str(data / "mcp")})


def _launch(client, url, **kw):
    kw.setdefault("headless", True)
    reply = client.call("browser_launch", _timeout=120, url=url, **kw)
    assert not reply.failed, reply.text
    return reply


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_brave_stays_brave_under_the_identity_override(site, data):
    """Headless always overrides the UA (it says HeadlessChrome) and lang= overrides the language,
    so the client hints are rebuilt: on Brave they must name Brave as Brave does."""
    client = _client(data)
    try:
        reply = _launch(client, f"http://127.0.0.1:{site}/protocol.html", browser="brave",
                        persistent=False, lang="id-ID", extra_args=[LOOPBACK_ONLY])
        assert "[browser: Brave " in reply.text
        seen = json.loads(client.evaluate("""(async () => JSON.stringify({
            isBrave: await navigator.brave.isBrave(),
            ua: navigator.userAgent, languages: navigator.languages,
            brands: navigator.userAgentData.brands.map(b => b.brand + ' ' + b.version),
            full: (await navigator.userAgentData.getHighEntropyValues(['fullVersionList']))
                  .fullVersionList.map(b => b.brand + ' ' + b.version)}))()"""))
        major = seen["ua"].split("Chrome/")[1].split(".")[0]
        assert seen["isBrave"] is True
        assert "HeadlessChrome" not in seen["ua"]
        assert seen["languages"] == ["id-ID"]
        assert f"Brave {major}" in seen["brands"] and f"Chromium {major}" in seen["brands"]
        assert f"Brave {major}.0.0.0" in seen["full"], seen["full"]
        assert not any("Google Chrome" in b for b in seen["brands"] + seen["full"])
        # A tab opened later gets the same identity.
        assert not client.call("tab_new", url=f"http://127.0.0.1:{site}/protocol.html").failed
        brands = client.evaluate("navigator.userAgentData.brands.map(b => b.brand).join(',')")
        assert "Brave" in brands and "Google Chrome" not in brands
        audit = client.call("fingerprint_audit", _timeout=60)
        report = json.loads(audit.text[audit.text.index("{"):])
        # The identity checks only: the WebGL check flags a runner's software GPU (SwiftShader) on
        # Chrome as well, which says nothing about Brave.
        identity_findings = [c for c in report["critical"] if "client hints" in c or "userAgentData" in c
                             or "headless" in c.lower() or "webdriver" in c]
        assert identity_findings == [], report
        assert "Brave vs client hints" in report["passed"]
        assert any(p.startswith("Brave farbling: expected") for p in report["passed"])
        listed = json.loads(client.call("list_instances").text)
        assert listed["instances"][0]["browser"] == "brave"
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def test_shields_blocks_an_easylist_host_that_chrome_loads(site, data):
    page = f"http://127.0.0.1:{site}/shields.html"
    client = _client(data)
    try:
        before = len(_Site.ad_hits)
        _launch(client, page, browser="chrome", persistent=False, extra_args=[AD_RULE])
        deadline = time.time() + 15
        while client.evaluate("window.adResult") == "pending" and time.time() < deadline:
            time.sleep(0.2)
        assert client.evaluate("window.adResult") == "loaded"
        assert client.evaluate("String(window.adScriptRan === true)") == "true"
        assert len(_Site.ad_hits) > before, "Chrome's request reached the fixture"
        client.call("browser_close", _timeout=40)

        _launch(client, page, browser="brave", persistent=False, extra_args=[AD_RULE])
        started, result, hits = time.monotonic(), None, len(_Site.ad_hits)
        while time.monotonic() - started < 120:
            hits = len(_Site.ad_hits)
            deadline = time.time() + 10
            while (result := client.evaluate("window.adResult")) == "pending" and time.time() < deadline:
                time.sleep(0.2)
            if result == "blocked":
                break
            time.sleep(2)  # the filter lists of a new profile are still on their way
            assert not client.call("reload", _timeout=30).failed
        waited = round(time.monotonic() - started, 1)
        assert result == "blocked", f"Shields did not block {AD_HOST} within {waited} s"
        assert len(_Site.ad_hits) == hits, "the blocked request still reached the fixture"
        assert client.evaluate("String(window.adScriptRan === true)") == "false"
        print(f"\nShields blocked {AD_HOST} {waited} s after the first load of a new profile")
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def test_farbling_differs_per_site_and_per_session_on_brave_only(site, data):
    sites = [f"http://127.0.0.1:{site}/protocol.html", f"http://localhost:{site}/protocol.html"]
    seen = {}
    client = _client(data)
    try:
        for browser, session in (("brave", 1), ("brave", 2), ("chrome", 1)):
            _launch(client, sites[0], browser=browser, persistent=False, extra_args=[LOOPBACK_ONLY])
            for url in sites:
                assert not client.call("navigate", url=url, _timeout=30).failed
                seen[(browser, session, url)] = _hash(client.evaluate(CANVAS))
            client.call("browser_close", _timeout=40)
    finally:
        client.close()
    print("\ncanvas hashes:", json.dumps({f"{b}#{s} {u.split('/')[2]}": h for (b, s, u), h in seen.items()}))
    assert seen[("brave", 1, sites[0])] != seen[("brave", 1, sites[1])], "per site"
    assert seen[("brave", 1, sites[0])] != seen[("brave", 2, sites[0])], "per session"
    assert seen[("chrome", 1, sites[0])] == seen[("chrome", 1, sites[1])], "Chrome does not farble"


def _cookie_rows(profile: pathlib.Path) -> list:
    for database in (profile / "Default" / "Network" / "Cookies", profile / "Default" / "Cookies"):
        if database.exists():
            with sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True) as db:
                return sorted(name for (name,) in db.execute("SELECT name FROM cookies"))
    return []


@WINDOWS_COOKIES_NOT_READ_BACK
def test_cookies_set_before_close_come_back_in_brave(site, data):
    """Brave keeps them under its own Keychain item (Brave Safe Storage) on macOS; the close
    asks Brave to quit, so they are written before it exits."""
    profile = data / "brave-cookies"
    page = f"http://127.0.0.1:{site}/protocol.html"
    client = _client(data)
    try:
        profile.mkdir()
        _launch(client, page, browser="brave", user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
        client.evaluate("['a','b','c'].forEach(n => document.cookie = n + '=1; max-age=86400; path=/'), 'ok'")
        closed = client.call("browser_close", _timeout=40)
        assert closed.text.startswith("Browser closed"), closed.text
        assert _cookie_rows(profile) == ["a", "b", "c"]
        assert brave.profile_browser(profile) == "brave"
        _launch(client, page, browser="brave", user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
        assert sorted(client.evaluate("document.cookie").split("; ")) == ["a=1", "b=1", "c=1"]
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def test_a_profile_is_refused_to_the_other_browser_before_anything_writes_to_it(site, data):
    page = f"http://127.0.0.1:{site}/protocol.html"
    client = _client(data)
    try:
        made = {}
        for browser in ("brave", "chrome"):
            folder = data / f"made-by-{browser}"
            folder.mkdir()
            _launch(client, page, browser=browser, user_data_dir=str(folder), extra_args=[LOOPBACK_ONLY])
            assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
            made[browser] = folder
        for browser, other in (("brave", "chrome"), ("chrome", "brave")):
            folder = made[other]
            stamp = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
            refused = client.call("browser_launch", _timeout=60, url=page, headless=True,
                                  browser=browser, user_data_dir=str(folder), extra_args=[LOOPBACK_ONLY])
            assert refused.failed, refused.text
            assert f"made by {brave.name(other)}" in refused.text, refused.text
            assert f"browser='{other}'" in refused.text
            assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == stamp, "nothing written"
            assert not state.processes_naming(folder), "nothing started"
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def test_default_brave_profiles_restore_tabs_and_vertical_tabs(site, data):
    """main's Brave profile is <data>/brave-profile, a spawned one <data>/brave-profiles/<id>, and
    neither touches Chrome's folders. restore_tabs brings the tabs back; vertical_tabs is written
    before launch and Brave keeps it."""
    urls = [f"http://127.0.0.1:{site}/protocol.html?tab={n}" for n in (1, 2)]
    mcp = data / "mcp"
    client = _client(data)
    try:
        _launch(client, urls[0], browser="brave", vertical_tabs="right", extra_args=[LOOPBACK_ONLY])
        assert not client.call("tab_new", url=urls[1]).failed
        closed = client.call("browser_close", _timeout=40)
        assert "Recorded 2 tab(s)" in closed.text, closed.text
        assert brave.vertical_tabs_read_back(mcp / "brave-profile", [], "brave") == "right"
        assert brave.profile_browser(mcp / "brave-profile") == "brave"
        assert not (mcp / "profile" / "Local State").exists(), "Chrome's main profile untouched"

        again = _launch(client, "about:blank", browser="brave", restore_tabs=True, extra_args=[LOOPBACK_ONLY])
        lines = client.call("tab_list").text.splitlines()[1:]
        assert [line.rsplit(" | ", 1)[1] for line in lines] == urls, again.text
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")

        spawned = client.call("spawn_browser", _timeout=120, instance_id="b2", url=urls[0], headless=True,
                              browser="brave", vertical_tabs="left", extra_args=[LOOPBACK_ONLY])
        assert not spawned.failed, spawned.text
        assert "[browser: Brave " in spawned.text and "[vertical tabs: on the left]" in spawned.text
        assert client.call("switch_instance", instance_id="b2").text
        assert client.evaluate("typeof navigator.brave") == "object"
        assert not client.call("close_instance", instance_id="b2", _timeout=40).failed
        assert brave.vertical_tabs_read_back(mcp / "brave-profiles" / "b2", [], "brave") == "left"
        assert not (mcp / "profiles" / "b2").exists()

        refused = client.call("browser_launch", _timeout=30, url="about:blank", headless=True,
                              browser="chrome", vertical_tabs="right")
        assert refused.failed and "only puts vertical tabs on the left" in refused.text
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


@pytest.mark.skipif(os.name != "posix", reason="keep_alive is refused on Windows (no quit watcher)")
def test_a_kept_brave_is_reattached_as_brave_and_never_handed_to_chrome(site, data):
    """keep_alive: a Brave left running when its server goes is found by the next browser="brave"
    launch in Brave's own folder and keeps naming Brave under the headless identity override; a
    Chrome launch on that profile is told why it is not reattached."""
    env = {"MCP_STEALTH_DATA_DIR": str(data / "mcp"), "MCP_STEALTH_KEEP_ALIVE_DIR": str(data / "keep-alive")}
    page = f"http://127.0.0.1:{site}/protocol.html"
    profile = data / "mcp" / "brave-profile"
    first = Client(env=env)
    try:
        _launch(first, page, browser="brave", persistent=True, keep_alive=True, extra_args=[LOOPBACK_ONLY])
    finally:
        first.close()  # stdin EOF: the server exits, Brave stays
    time.sleep(2)
    assert state.processes_naming(profile), "the kept Brave ended with its server"

    chrome = Client(env=env)
    try:
        refused = chrome.call("browser_launch", _timeout=60, url=page, headless=True, browser="chrome",
                              user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
        assert refused.failed and "was not reattached" in refused.text and "made by Brave" in refused.text, refused.text
    finally:
        chrome.close()

    second = Client(env=env)
    try:
        out = _launch(second, "about:blank", browser="brave", persistent=True, vertical_tabs="right",
                      keep_alive=False, extra_args=[LOOPBACK_ONLY])
        assert "Reattached to the running browser" in out.text, out.text
        assert "vertical_tabs='right'" in out.text, "not named as not applied"
        seen = json.loads(second.evaluate("JSON.stringify({b: typeof navigator.brave, "
                                          "brands: navigator.userAgentData ? navigator.userAgentData.brands.map(x => x.brand) : [], "
                                          "ua: navigator.userAgent})"))
        assert seen["b"] == "object" and "HeadlessChrome" not in seen["ua"], seen
        assert not client_brands_name_google(second, page)
        assert json.loads(second.call("server_status").text)["browser"] == "brave"
    finally:
        second.close()  # keep_alive=False on the reattach: Brave ends with this server
    deadline = time.time() + 20
    while state.processes_naming(profile) and time.time() < deadline:
        time.sleep(0.5)
    assert not state.processes_naming(profile), "the reattached Brave outlived keep_alive=False"


def client_brands_name_google(client, page) -> bool:
    """Brands on a secure page of the reattached browser, where userAgentData exists."""
    assert not client.call("navigate", url=page, _timeout=30).failed
    brands = client.evaluate("navigator.userAgentData.brands.map(b => b.brand).join(',')")
    assert "Brave" in brands, brands
    return "Google Chrome" in brands
