"""press_key and browser identity, against local fixtures only.

Identity checks run on http://localhost because navigator.userAgentData exists only
on secure contexts, and localhost counts as one.

Unit tests always run. Real-Chrome tests need MCP_E2E=1.
"""
import asyncio
import functools
import http.server
import json
import os
import pathlib
import re
import sys
import threading
import time

import pytest

from mcp_stealth_chrome.identity import (
    brand_list,
    chrome_version,
    key_event_params,
    default_accept_language,
    navigator_languages,
    navigator_platform,
    os_family,
    strip_headless,
    ua_metadata_fields,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
E2E = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")
WIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.7339.80 Safari/537.36")


# ── Pure functions ────────────────────────────────────────────────────────


def test_enter_has_everything_a_form_needs():
    params = key_event_params("Enter")
    assert params["key"] == params["code"] == "Enter"
    assert params["windows_virtual_key_code"] == 13 and params["text"] == "\r"


@pytest.mark.parametrize("name,key,code,vk", [
    ("tab", "Tab", "Tab", 9),
    ("Esc", "Escape", "Escape", 27),
    ("a", "a", "KeyA", 65),
    ("7", "7", "Digit7", 55),
    (" ", " ", "Space", 32),
    ("ArrowDown", "ArrowDown", "ArrowDown", 40),
    ("F5", "F5", "F5", 116),
    ("/", "/", "Slash", 191),
])
def test_key_table(name, key, code, vk):
    params = key_event_params(name)
    assert (params["key"], params["code"], params["windows_virtual_key_code"]) == (key, code, vk)


def test_uppercase_letter_implies_shift_and_shortcuts_do_not_type():
    assert key_event_params("A")["modifiers"] == 8 and key_event_params("A")["text"] == "A"
    shortcut = key_event_params("Control+a")
    assert shortcut["modifiers"] == 2 and shortcut["text"] is None and shortcut["code"] == "KeyA"
    assert key_event_params("Shift+Tab")["modifiers"] == 8
    assert key_event_params("+")["key"] == "+"


@pytest.mark.parametrize("bad", ["", "NotAKey", "Hyper+a"])
def test_unknown_key_is_an_error_not_a_silent_no_op(bad):
    with pytest.raises(ValueError):
        key_event_params(bad)


def test_ua_helpers():
    assert chrome_version(WIN_UA) == "140.0.7339.80"
    assert chrome_version("Mozilla/5.0 (X11; rv:128.0) Gecko/20100101 Firefox/128.0") is None
    assert "HeadlessChrome" not in strip_headless("x HeadlessChrome/156.0.1.2 y")
    assert os_family(WIN_UA) == "windows" and navigator_platform("windows") == "Win32"
    assert os_family(WIN_UA, "MacIntel") == "mac"  # an explicit platform wins


def test_metadata_agrees_with_the_ua_it_came_from():
    hints = ua_metadata_fields(WIN_UA)
    assert hints["platform"] == "Windows" and hints["full_version"] == "140.0.7339.80"
    brands = {b["brand"]: b["version"] for b in hints["brands"]}
    assert brands["Google Chrome"] == brands["Chromium"] == "140"
    assert len(brands) == 3
    assert ua_metadata_fields("Mozilla/5.0 (X11; rv:128.0) Gecko/20100101 Firefox/128.0") is None


@pytest.mark.parametrize("value,tags", [
    ("id-ID,id;q=0.9,en;q=0.8", ["id-ID", "id", "en"]),
    ("en-US", ["en-US"]),
    (" fr-FR , fr ;q=0.5,fr", ["fr-FR", "fr"]),
    ("", []),
])
def test_languages_lose_their_weights(value, tags):
    assert navigator_languages(value) == tags


def test_chrome_style_header_generation():
    assert default_accept_language(["id-ID", "id", "en"]) == "id-ID,id;q=0.9,en;q=0.8"
    assert default_accept_language(["en"]) == "en"


def test_default_identity_is_not_invented():
    from mcp_stealth_chrome import server

    for privacy in (True, False):
        _, fp, source = server._launch_identity(
            "no_pin_here", proxy=None, user_agent=None, fingerprint=None, privacy=privacy)
        assert fp == {} and source == "native"


def test_caller_can_override_any_field_per_launch():
    from mcp_stealth_chrome import server

    _, fp, source = server._launch_identity(
        "no_pin_here", proxy=None, user_agent=None, privacy=True,
        fingerprint={"timezone": "Asia/Jakarta"})
    assert fp == {"timezone": "Asia/Jakarta"} and source == "caller"


def test_chrome_never_receives_an_override_without_metadata(monkeypatch):
    """The reported defect: brands empty because metadata was never sent."""
    from mcp_stealth_chrome import server

    sent = []

    class _Tab:
        async def send(self, command):
            request = next(command)
            sent.append(request)
            if request["method"] == "Browser.getVersion":
                try:
                    command.send({"protocolVersion": "1.3", "product": "Chrome/156.0.1.2",
                                  "revision": "r", "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0.1.2 Safari/537.36",
                                  "jsVersion": "1"})
                except StopIteration as stop:
                    return stop.value
            return None

    async def scenario(fp):
        sent.clear()
        await server._apply_fingerprint_to_tab(_Tab(), fp)
        return [r for r in sent if r["method"] == "Network.setUserAgentOverride"]

    full = asyncio.run(scenario({"user_agent": WIN_UA}))[0]["params"]
    assert full["userAgentMetadata"]["platform"] == "Windows"
    assert full["platform"] == "Win32"  # inferred, or it would contradict the UA

    language_only = asyncio.run(scenario({"accept_language": "id-ID"}))[0]["params"]
    assert "156.0.1.2" in language_only["userAgent"]
    assert language_only["userAgentMetadata"]["brands"]

    assert asyncio.run(scenario({})) == []  # nothing asked, nothing overridden


# ── Real Chrome on local fixtures ─────────────────────────────────────────


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/headers":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/slow"):
            time.sleep(1.5)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        super().do_GET()

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(_Quiet, directory=str(FIXTURES))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
        for t in pending:
            t.cancel()
        if pending:
            lp.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        lp.close()


def run(lp, name, *args, **kwargs):
    return lp.run_until_complete(tool(name)(*args, **kwargs))


IDENTITY_JS = """(async () => {
  const d = navigator.userAgentData;
  const high = d ? await d.getHighEntropyValues(['fullVersionList','platformVersion','architecture','bitness']) : null;
  return JSON.stringify({ua: navigator.userAgent, platform: navigator.platform,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone, language: navigator.language,
    brands: d ? d.brands : null, hintPlatform: d ? d.platform : null, high});
})()"""


def identity(lp):
    return json.loads(run(lp, "evaluate", IDENTITY_JS))


@pytest.fixture()
def browser(loop):
    def launch(**kwargs):
        kwargs.setdefault("persistent", False)
        out = run(loop, "browser_launch", headless=True, **kwargs)
        assert not out.startswith("Error:"), out
        return out
    yield launch
    run(loop, "browser_close")


@pytest.fixture()
def keys_page(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    return loop


@E2E
def test_root_cause_key_only_event_cannot_submit_a_form(keys_page):
    """Confirms the suspected cause of press_key having no effect."""
    from nodriver.cdp import input_ as cdp_input
    from mcp_stealth_chrome.server import BrowserState

    lp = keys_page
    run(lp, "click", selector="#q")
    tab = BrowserState.active_tab()

    async def old_way():
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", key="Enter"))
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", key="Enter"))
        await asyncio.sleep(0.8)

    lp.run_until_complete(old_way())
    assert "keys.html" in run(lp, "evaluate", "location.href")
    events = json.loads(run(lp, "evaluate", "JSON.stringify(window.events.filter(e => e.key === 'Enter'))"))
    assert [e["type"] for e in events] == ["keydown", "keyup"]  # no keypress
    assert events[0]["code"] == "" and events[0]["keyCode"] == 0


@E2E
def test_enter_submits_the_form_and_reports_navigation(keys_page):
    lp = keys_page
    run(lp, "click", selector="#q")
    run(lp, "type_text", "monitor")
    out = run(lp, "press_key", "Enter")
    assert out.startswith("pressed Enter") and "navigated to" in out and "q=monitor" in out, out
    assert "input#q" in out


@E2E
def test_key_event_fields_are_complete_and_trusted(keys_page):
    lp = keys_page
    run(lp, "click", selector="#q")
    run(lp, "press_key", "Enter", verify=False, settle_ms=0)
    # The page navigated; the listener data is gone, so re-check with a non-submitting key.
    lp.run_until_complete(asyncio.sleep(0.4))
    run(lp, "go_back")
    lp.run_until_complete(asyncio.sleep(0.3))
    run(lp, "click", selector="#q")
    run(lp, "press_key", "a")
    events = json.loads(run(lp, "evaluate", "JSON.stringify(window.events.filter(e => e.key === 'a'))"))
    assert [e["type"] for e in events] == ["keydown", "keypress", "keyup"]
    assert events[0]["code"] == "KeyA" and events[0]["keyCode"] == 65 and events[0]["trusted"] is True
    assert "a" in run(lp, "evaluate", "document.getElementById('q').value")


@E2E
def test_key_with_no_effect_says_so(keys_page):
    out = run(keys_page, "press_key", "Escape", settle_ms=200)
    assert "NO page change detected" in out and out.startswith("pressed Escape")


@E2E
def test_typing_and_focus_changes_are_reported(keys_page):
    lp = keys_page
    run(lp, "click", selector="#q")
    assert "focused element changed" in run(lp, "press_key", "x")
    assert "focus moved" in run(lp, "press_key", "Tab")


@E2E
def test_verify_can_be_turned_off(keys_page):
    assert "effect not checked" in run(keys_page, "press_key", "Escape", verify=False)


@E2E
def test_unknown_key_refused(keys_page):
    assert run(keys_page, "press_key", "NotAKey").startswith("Error: unknown key")


@E2E
def test_paste_submit_really_presses_enter(keys_page):
    lp = keys_page
    out = run(lp, "paste_text", "monitor", selector="#q", submit=True)
    assert "pasted" in out
    lp.run_until_complete(asyncio.sleep(0.8))
    assert "q=monitor" in run(lp, "evaluate", "location.href")


@E2E
def test_click_names_the_method_used(keys_page):
    lp = keys_page
    assert "method=cdp_mouse" in run(lp, "click", selector="#next")
    assert "method=cdp_mouse" in run(lp, "click_text", "Next")
    covered = "(() => { const d=document.createElement('div'); d.id='veil';" \
              "d.style='position:fixed;inset:0;z-index:9'; document.body.appendChild(d); })()"
    run(lp, "evaluate", covered)
    refused = run(lp, "click", selector="#next")
    assert refused.startswith("Error:") and "NOT clicked" in refused and "veil" in refused
    forced = run(lp, "click", selector="#next", js_fallback=True)
    assert "method=js_click" in forced and "JS FALLBACK" in forced


@E2E
@pytest.mark.parametrize("privacy", [False, True])
def test_default_identity_matches_the_real_browser(loop, browser, base_url, privacy):
    from nodriver.cdp import browser as cdp_browser
    from mcp_stealth_chrome.server import BrowserState

    launched = browser(**({"privacy": True} if privacy else {}))
    assert "identity=native" in launched
    run(loop, "navigate", f"{base_url}/keys.html")
    seen = identity(loop)
    real = loop.run_until_complete(BrowserState.active_tab().send(cdp_browser.get_version()))
    full = real[1].split("/")[1]
    major = full.split(".")[0]

    assert f"Chrome/{major}." in seen["ua"] and "HeadlessChrome" not in seen["ua"]
    # The UA string is reduced by Chrome; the build number belongs in the hints.
    assert full in [b["version"] for b in seen["high"]["fullVersionList"]]
    brands = {b["brand"]: b["version"] for b in seen["brands"]}
    assert brands, "brands must not be empty"
    assert brands["Google Chrome"] == major and "Chromium" in brands
    family = {"darwin": ("MacIntel", "macOS"), "win32": ("Win32", "Windows")}.get(
        sys.platform, ("Linux x86_64", "Linux"))
    assert seen["platform"] == family[0] and seen["hintPlatform"] == family[1]
    assert seen["tz"] == loop.run_until_complete(_local_timezone())
    audit = json.loads(run(loop, "fingerprint_audit"))
    for label in ("userAgentData.brands", "UA version vs client hints", "headless marker in UA"):
        assert label in audit["passed"], audit


async def _local_timezone():
    from mcp_stealth_chrome.server import BrowserState

    # What the browser reports with no override is the machine's own zone.
    tab = BrowserState.active_tab()
    from nodriver.cdp import emulation
    await tab.send(emulation.set_timezone_override(timezone_id=""))
    value = await tab.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone", return_by_value=True)
    return value.value if hasattr(value, "value") else value


@E2E
def test_generated_brands_match_a_chrome_with_no_override(loop, base_url):
    """brand_list must reproduce the real list, or an override shows up as a difference.

    Driven through nodriver directly, because this server's own launch overrides the
    UA in headless mode and the comparison would be against itself. Headless Chrome
    names itself HeadlessChrome in the brand list, so that one entry is mapped.
    """
    import nodriver

    async def scenario():
        browser = await nodriver.start(headless=True)
        try:
            tab = await browser.get(f"{base_url}/keys.html")
            await asyncio.sleep(0.3)
            raw = await tab.evaluate(
                "JSON.stringify({ua: navigator.userAgent, brands: navigator.userAgentData.brands})",
                return_by_value=True)
            return json.loads(raw.value if hasattr(raw, "value") else raw)
        finally:
            browser.stop()

    seen = loop.run_until_complete(scenario())
    major = int(chrome_version(seen["ua"]).split(".")[0])
    real = [{"brand": b["brand"].replace("HeadlessChrome", "Google Chrome"), "version": b["version"]}
            for b in seen["brands"]]
    assert real == brand_list(major)


@E2E
def test_full_override_stays_consistent(loop, browser, base_url):
    launched = browser(fingerprint={
        "user_agent": WIN_UA, "timezone": "Asia/Jakarta", "accept_language": "id-ID,id;q=0.9",
    })
    assert "identity=caller" in launched
    run(loop, "navigate", f"{base_url}/keys.html")
    seen = identity(loop)
    assert seen["ua"] == WIN_UA and seen["platform"] == "Win32"
    assert seen["hintPlatform"] == "Windows" and seen["tz"] == "Asia/Jakarta"
    assert seen["language"].startswith("id")
    assert {b["brand"]: b["version"] for b in seen["brands"]}["Google Chrome"] == "140"
    assert seen["high"]["fullVersionList"]


@E2E
@pytest.mark.parametrize("field,value,check", [
    ("timezone", "Asia/Tokyo", lambda s: s["tz"] == "Asia/Tokyo"),
    ("accept_language", "fr-FR,fr;q=0.9", lambda s: s["language"].startswith("fr")),
])
def test_partial_override_leaves_the_rest_real(loop, browser, base_url, field, value, check):
    browser(fingerprint={field: value})
    run(loop, "navigate", f"{base_url}/keys.html")
    seen = identity(loop)
    assert check(seen), seen
    assert seen["brands"] and "HeadlessChrome" not in seen["ua"]
    assert re.search(r"Chrome/\d+", seen["ua"])


@E2E
def test_fingerprint_rotate_keeps_hints_in_step(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    assert "applied" in run(loop, "fingerprint_rotate", user_agent=WIN_UA)
    run(loop, "reload")
    seen = identity(loop)
    assert seen["ua"] == WIN_UA and seen["hintPlatform"] == "Windows"
    assert {b["brand"]: b["version"] for b in seen["brands"]}["Google Chrome"] == "140"


# ── Cookie values stay out of tool output ─────────────────────────────────


class _Cookie:
    name = "SID"
    value = "super-secret-session-token"
    domain = "example.test"
    path = "/"
    expires = 0
    http_only = True
    secure = True
    same_site = "Lax"


@pytest.fixture()
def fake_jar(monkeypatch, tmp_path):
    from mcp_stealth_chrome import server

    class _Jar:
        async def get_all(self):
            return [_Cookie()]

    class _Browser:
        cookies = _Jar()

    from mcp_stealth_chrome.tools import storage

    monkeypatch.setattr(server.BrowserState, "browser", _Browser())
    monkeypatch.setattr(storage, "STORAGE_STATE_DIR", tmp_path)
    return tmp_path


def test_cookie_list_masks_values_by_default(fake_jar):
    out = asyncio.run(tool("cookie_list")())
    assert _Cookie.value not in out
    row = json.loads(out)[0]
    assert row["value"] == "***" and row["value_masked"] is True
    assert row["value_length"] == len(_Cookie.value) and row["name"] == "SID"
    assert row["http_only"] is True


def test_cookie_list_reveals_only_on_request(fake_jar):
    row = json.loads(asyncio.run(tool("cookie_list")(reveal_values=True)))[0]
    assert row["value"] == _Cookie.value and "value_masked" not in row


def test_cookie_export_response_never_holds_a_value_and_file_is_private(fake_jar):
    import stat

    out = asyncio.run(tool("cookie_export")("c.json"))
    assert _Cookie.value not in out
    path = fake_jar / "c.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())[0]["value"] == _Cookie.value  # still importable


def test_cookie_export_can_mask_the_file_too(fake_jar):
    out = asyncio.run(tool("cookie_export")("m.json", reveal_values=False))
    assert "cannot be imported" in out
    assert _Cookie.value not in (fake_jar / "m.json").read_text()


def test_existing_export_file_is_tightened(fake_jar):
    import stat

    path = fake_jar / "old.json"
    path.write_text("[]")
    path.chmod(0o644)
    asyncio.run(tool("cookie_export")("old.json"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── navigator.languages and the Accept-Language header, seen from both sides ──


def _seen_languages(loop, base_url):
    run(loop, "navigate", f"{base_url}/keys.html")
    languages = json.loads(run(loop, "evaluate", "JSON.stringify(navigator.languages)"))
    header = json.loads(run(loop, "evaluate",
        "fetch('/headers').then(r => r.text())"))["accept-language"]
    return languages, header


@E2E
@pytest.mark.parametrize("privacy", [False, True])
def test_weighted_language_is_split_between_navigator_and_header(loop, browser, base_url, privacy):
    """The measured bug: navigator.languages held "id;q=0.9" and "en;q=0.8"."""
    browser(privacy=privacy, fingerprint={"accept_language": "id-ID,id;q=0.9,en;q=0.8"})
    languages, header = _seen_languages(loop, base_url)
    assert languages == ["id-ID", "id", "en"]
    assert header == "id-ID,id;q=0.9,en;q=0.8"


@E2E
def test_unusual_weights_still_reach_the_header_exactly(loop, browser, base_url):
    browser(fingerprint={"accept_language": "fr-FR,en;q=0.3"})
    languages, header = _seen_languages(loop, base_url)
    assert languages == ["fr-FR", "en"]
    assert header == "fr-FR,en;q=0.3"


@E2E
def test_privacy_changes_the_profile_and_not_the_identity(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    normal = identity(loop)
    run(loop, "browser_close")
    browser(privacy=True)
    run(loop, "navigate", f"{base_url}/keys.html")
    private = identity(loop)
    for field in ("ua", "platform", "tz", "brands", "hintPlatform"):
        assert normal[field] == private[field], field


@E2E
def test_spawn_works_on_a_copied_multi_profile_browser(loop, tmp_path):
    """The known failure: spawn_browser on a clone stopped at Chrome's profile picker."""
    data = tmp_path / "clone"
    profiles = ["Default", "Profile 1", "Profile 2"]
    for name in profiles:
        (data / name).mkdir(parents=True)
    (data / "Local State").write_text(json.dumps({"profile": {
        "last_used": "Default", "last_active_profiles": ["Default"], "picker_shown": True,
        "info_cache": {name: {"name": name, "active_time": 1.0} for name in profiles},
    }}))
    iid = f"clone_e2e_{os.getpid()}"
    try:
        out = run(loop, "spawn_browser", iid, headless=True, profile_dir=str(data))
        assert not out.startswith("Error:"), out
        run(loop, "switch_instance", iid)
        assert "about:blank" in run(loop, "get_url")
    finally:
        run(loop, "close_instance", iid)
        run(loop, "switch_instance", "main")


@E2E
def test_identity_survives_cross_site_navigation(loop, browser, base_url):
    """nodriver 0.50 opened a new CDP session per navigation, which dropped overrides."""
    browser(fingerprint={"user_agent": WIN_UA, "timezone": "Asia/Tokyo"})
    other_site = base_url.replace("localhost", "127.0.0.1")
    for url in (f"{base_url}/keys.html", f"{other_site}/keys.html", f"{base_url}/keys.html"):
        run(loop, "navigate", url)
        state = json.loads(run(loop, "evaluate",
            "JSON.stringify([navigator.platform, navigator.userAgent, "
            "Intl.DateTimeFormat().resolvedOptions().timeZone])"))
        assert state == ["Win32", WIN_UA, "Asia/Tokyo"], url


@E2E
@pytest.mark.parametrize("tool_name,kwargs", [
    ("dialog_auto_handle", {"action": "accept"}),
    ("dialog_handle", {"action": "accept"}),
])
def test_dialog_handlers_receive_events_on_a_fresh_tab(loop, browser, base_url, tool_name, kwargs):
    """nodriver 0.50 stopped enabling the Page domain for registered handlers."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    assert not run(loop, tool_name, **kwargs).startswith("Error:")
    answer = run(loop, "evaluate", "confirm('ok?')", timeout_ms=8000)
    assert answer == "true", answer


@E2E
def test_ephemeral_browser_records_no_shared_profile(loop, browser):
    from mcp_stealth_chrome.server import BrowserState

    browser()
    assert BrowserState.current_profile_dir is None


# ── Found by the independent review round ─────────────────────────────────


@E2E
def test_popup_is_listed_and_a_closed_tab_is_not(loop, browser, base_url):
    """nodriver 0.50 stopped tracking targets by itself."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", "document.getElementById('next').onclick = () => window.open('about:blank'); 1")
    run(loop, "click", selector="#next")
    loop.run_until_complete(asyncio.sleep(0.6))
    listing = run(loop, "tab_list")
    assert "[1]" in listing and "about:blank" in listing, listing
    assert listing.startswith("Active tab: 0"), listing  # the active tab did not move
    assert "closed tab 1" == run(loop, "tab_close", 1)
    loop.run_until_complete(asyncio.sleep(0.3))
    assert "[1]" not in run(loop, "tab_list")


@E2E
def test_tab_url_stays_current_after_navigation(loop, browser, base_url):
    from mcp_stealth_chrome.server import BrowserState

    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    loop.run_until_complete(asyncio.sleep(0.3))
    tab = BrowserState.active_tab()
    assert "keys.html" in str(tab.target.url)


@E2E
def test_text_lookup_never_clicks_at_frame_local_coordinates(loop, browser, base_url):
    """The root page has a button exactly where the frame-local numbers would land."""
    browser()
    run(loop, "navigate", f"{base_url}/frame_text.html")
    loop.run_until_complete(asyncio.sleep(0.7))
    out = run(loop, "click_text", "Inner only")
    assert run(loop, "evaluate", "document.title") == "frame text", out
    # nodriver 0.50 finds it in the frame and gets a hint; up to 0.48 it finds nothing.
    assert out.startswith("Error:") and ("browser_snapshot" in out or "no element" in out), out


@E2E
def test_workflow_can_read_the_url(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    out = json.loads(run(loop, "workflow_run", [{"tool": "get_url", "save_as": "u"}]))
    assert "keys.html" in out["vars"]["u"]


def _start_external_chrome(tmp_path):
    import socket
    import subprocess
    import time

    from mcp_stealth_chrome.state import find_chrome_binary

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([
        str(find_chrome_binary()), f"--remote-debugging-port={port}", "--headless=new",
        f"--user-data-dir={tmp_path}", "--use-mock-keychain", "--no-first-run", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return proc, port
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise AssertionError("Chrome did not open its debugging port")


@E2E
def test_attached_browser_stays_usable_and_close_really_closes_it(loop, tmp_path):
    """Liveness read .connection, gone in 0.50, and close only dropped the socket."""
    proc, port = _start_external_chrome(tmp_path)
    try:
        out = run(loop, "attach_to_chrome", port=port)
        assert not out.startswith("Error:"), out
        assert "2" in run(loop, "evaluate", "1+1")
        closed = run(loop, "browser_close")
        assert not closed.startswith("Error:"), closed
        assert proc.wait(timeout=10) is not None
    finally:
        # attach_to_chrome renames the current instance; later tests expect "main".
        run(loop, "switch_instance", "main")
        if proc.poll() is None:
            proc.kill()


@E2E
def test_detach_leaves_the_external_browser_running(loop, tmp_path):
    proc, port = _start_external_chrome(tmp_path)
    try:
        assert not run(loop, "attach_to_chrome", port=port).startswith("Error:")
        assert "detached" in run(loop, "detach")
        loop.run_until_complete(asyncio.sleep(0.5))
        assert proc.poll() is None
    finally:
        run(loop, "switch_instance", "main")
        proc.kill()
        proc.wait(timeout=10)


@E2E
def test_close_all_really_ends_stored_browsers(loop, tmp_path):
    import subprocess

    iid = f"closeall_{os.getpid()}"
    run(loop, "spawn_browser", iid, headless=True, profile_dir=str(tmp_path / "p"))
    assert "closed" in run(loop, "close_all_instances")
    loop.run_until_complete(asyncio.sleep(0.5))
    left = subprocess.run(["pgrep", "-f", f"user-data-dir={tmp_path}/p"], capture_output=True, text=True)
    assert not left.stdout.strip()


# ── High-severity findings, on real Chrome ────────────────────────────────


@E2E
def test_a_password_value_stays_out_of_every_report(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        const p = document.createElement('input'); p.type = 'password'; p.id = 'pw';
        p.setAttribute('aria-label', 'Password'); p.value = 'HUNTER2SECRET';
        document.getElementById('search').appendChild(p); })()""")
    for name, kwargs in (("browser_snapshot", {"diff_from_last": False}),
                         ("browser_snapshot", {"mode": "fast", "diff_from_last": False}),
                         ("describe_page", {}), ("form_introspect", {})):
        out = str(run(loop, name, **kwargs))
        assert "HUNTER2SECRET" not in out, name
    assert "HUNTER2SECRET" not in run(loop, "paste_text", "HUNTER2SECRET", selector="#pw")
    assert "Password" in str(run(loop, "browser_snapshot", diff_from_last=False))


@E2E
def test_click_role_with_a_name_never_clicks_the_wrong_one(loop, browser, base_url):
    """It used to fall back to the first element of that role."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        for (const label of ['Delete', 'Save']) {
          const b = document.createElement('div'); b.setAttribute('role', 'button');
          b.textContent = label; b.onclick = () => { document.title = label; };
          document.body.appendChild(b);
        } })()""")
    assert "clicked" in run(loop, "click_role", "button", "Save")
    assert run(loop, "evaluate", "document.title") == "Save"
    run(loop, "evaluate", "document.title = 'untouched'")
    out = run(loop, "click_role", "button", "NoSuchThing")
    assert out.startswith("Error:")
    assert run(loop, "evaluate", "document.title") == "untouched"


@E2E
def test_bad_url_on_a_running_browser_keeps_it_open(loop, browser, base_url):
    """browser_launch(url) on a live browser with a bad URL used to end it and every tab."""
    from mcp_stealth_chrome.server import BrowserState

    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    same_browser = BrowserState.browser
    run(loop, "browser_launch", headless=True, persistent=False, url="not-a-valid-url://x")
    assert BrowserState.browser is same_browser, "the browser was ended over a bad URL"
    assert run(loop, "evaluate", "1+1") == "2"


@E2E
def test_fresh_launch_reports_the_page_it_actually_reached(loop, base_url):
    """A redirect read as "Loaded <what you asked for>", and expect_* was never checked."""
    redirect = "data:text/html,<script>location.replace('" + base_url + "/keys.html')</script>"
    out = run(loop, "browser_launch", headless=True, persistent=False, url=redirect)
    try:
        assert "Browser launched" in out and "keys.html" in out, out
    finally:
        run(loop, "browser_close")
    refused = run(loop, "browser_launch", headless=True, persistent=False,
                  url=f"{base_url}/keys.html", expect_url="https://elsewhere.test/*")
    try:
        assert refused.startswith("Error:") and "postcondition failed" in refused, refused
    finally:
        run(loop, "browser_close")


# ── Medium-severity findings, on real Chrome ──────────────────────────────


@E2E
def test_smart_fill_sets_a_select_by_value_or_visible_text(loop, browser, base_url):
    """The HTMLInputElement value setter throws on a <select>, so one could never be filled."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        const s = document.createElement('select');
        s.setAttribute('aria-label', 'Country');
        s.innerHTML = '<option value="">-</option><option value="fr">France</option><option value="de">Germany</option>';
        window.changes = 0; s.addEventListener('change', () => window.changes++);
        document.getElementById('search').appendChild(s); })()""")
    out = run(loop, "smart_fill", {"Country": "fr"})
    assert not out.startswith("Error:"), out
    assert run(loop, "evaluate", "document.querySelector('select').value") == "fr"
    out = run(loop, "smart_fill", {"Country": "Germany"})
    assert not out.startswith("Error:"), out
    assert run(loop, "evaluate", "document.querySelector('select').value") == "de"
    assert run(loop, "evaluate", "window.changes") == "2"
    out = run(loop, "smart_fill", {"Country": "Atlantis"})
    assert out.startswith("Error:")
    assert run(loop, "evaluate", "document.querySelector('select').value") == "de"


@E2E
def test_region_screenshot_of_a_scrolled_page_is_the_right_strip(loop, browser, base_url, tmp_path, monkeypatch):
    """Viewport numbers used as page coordinates captured some other strip of the page."""
    import cv2
    import numpy as np

    from mcp_stealth_chrome import server

    from mcp_stealth_chrome.tools import page

    monkeypatch.setattr(page, "SCREENSHOT_DIR", tmp_path)
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        document.body.innerHTML = '<div style="height:900px;background:rgb(255,0,0)"></div>'
          + '<div id="band" style="height:120px;background:rgb(0,255,0)"></div>'
          + '<div style="height:2000px;background:rgb(0,0,255)"></div>';
        document.body.style.margin = 0;
        window.scrollTo(0, 800); })()""")
    for name, kwargs in (("sel.png", {"region_selector": "#band"}),
                         ("reg.png", {"region": {"x": 0, "y": 100, "width": 200, "height": 120}})):
        out = run(loop, "screenshot", filename=name, **kwargs)
        assert not str(out).startswith("Error:"), out
        image = cv2.imread(str(tmp_path / name))
        blue, green, red = image[image.shape[0] // 2, image.shape[1] // 2]
        assert green > 200 and red < 50 and blue < 50, (name, blue, green, red)
        assert np.mean(image[:, :, 1]) > 200, name


@E2E
def test_console_clear_and_page_errors_on_a_real_page(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    assert json.loads(run(loop, "get_page_errors"))["attached"] is False
    run(loop, "console_start")
    run(loop, "evaluate", "console.log('old'); setTimeout(() => { throw new Error('boom-e2e'); }, 0); 1")
    loop.run_until_complete(asyncio.sleep(0.4))
    errors = json.loads(run(loop, "get_page_errors"))
    assert errors["attached"] is True and "boom-e2e" in errors["errors"][0]["text"]
    assert json.loads(run(loop, "console_get"))["total"] >= 2
    assert run(loop, "console_clear") == "console cleared"
    loop.run_until_complete(asyncio.sleep(0.3))
    assert json.loads(run(loop, "console_get"))["total"] == 0, "console.clear() left a message behind"
    run(loop, "evaluate", "console.log('new'); 1")
    loop.run_until_complete(asyncio.sleep(0.3))
    assert [m["text"] for m in json.loads(run(loop, "console_get"))["messages"]] == ["new"]


@E2E
def test_device_emulation_and_its_reset_on_a_real_page(loop, browser, base_url):
    """desktop left Safari's UA active, and overrides left userAgentData.brands empty."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    native = identity(loop)
    assert native["brands"]

    run(loop, "emulate_device", "pixel-8")
    run(loop, "reload")
    phone = identity(loop)
    assert "Pixel 8" in phone["ua"] and phone["hintPlatform"] == "Android"
    assert phone["brands"], "brands empty is what no real Chrome reports"
    assert phone["platform"] == "Linux armv81"

    assert "reset to desktop" in run(loop, "emulate_device", "desktop")
    run(loop, "reload")
    back = identity(loop)
    assert "Pixel" not in back["ua"] and "Mobile" not in back["ua"]
    assert back["hintPlatform"] == native["hintPlatform"] and back["platform"] == native["platform"]
    assert back["brands"], "reset left brands empty"
    assert back["ua"] == strip_headless(native["ua"])
    assert run(loop, "evaluate", "matchMedia('(pointer: coarse)').matches || innerWidth < 500") == "false"


@E2E
def test_wait_for_network_idle_sees_an_image_still_loading(loop, browser, base_url):
    """The old page-side tracker only saw fetch and XHR, and left a global behind."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", "setTimeout(() => { new Image().src = '/slow?img=' + Date.now(); }, 100); 1")
    started = time.monotonic()
    out = run(loop, "wait_for_network_idle", idle_ms=400, timeout=8)
    assert not out.startswith("Error:"), out
    assert time.monotonic() - started >= 1.5, f"idle with the image in flight: {out}"
    assert run(loop, "evaluate", "typeof window.__mcp_netidle") == "undefined"
    assert "native code" in run(loop, "evaluate", "window.fetch.toString()")


@E2E
def test_click_and_wait_network_idle_and_request_against_real_events(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        document.getElementById('next').onclick = () => { fetch('/slow?click=' + Date.now()); };
        })()""")
    started = time.monotonic()
    out = run(loop, "click_and_wait", selector="#next", expect="network_idle", timeout=8)
    assert not out.startswith("Error:"), out
    assert time.monotonic() - started >= 1.5, f"idle with the fetch in flight: {out}"
    assert json.loads(out)["observed"]["what"] == "network_idle"

    out = run(loop, "click_and_wait", selector="#next", expect="request",
              expect_request_pattern="/slow", timeout=8)
    assert not out.startswith("Error:"), out
    assert "/slow" in json.loads(out)["observed"]["evidence"]


@E2E
def test_closing_the_last_tab_then_browser_close_really_ends_chrome(loop, tmp_path):
    """close said "not running" and left Chrome running with the profile held."""
    from mcp_stealth_chrome.server import BrowserState

    run(loop, "browser_launch", headless=True, persistent=False)
    pid = getattr(BrowserState.browser, "_process_pid", None)
    assert pid
    run(loop, "tab_close")
    out = run(loop, "browser_close")
    assert "not running" not in out and not out.startswith("Error:"), out
    assert BrowserState.browser is None
    loop.run_until_complete(asyncio.sleep(0.5))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@E2E
def test_interception_restart_on_a_real_page_answers_once(loop, browser, base_url):
    """A doubled continueRequest fails on the second call, and logs and counts doubled."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "intercept_start", url_pattern="*headers*", auto_disable_after=0)
    run(loop, "intercept_stop")
    run(loop, "intercept_start", url_pattern="*headers*", auto_disable_after=0)
    body = run(loop, "evaluate", "fetch('/headers').then(r => r.status)", await_promise=True)
    assert body == "200"
    log = json.loads(run(loop, "intercept_log"))
    run(loop, "intercept_stop")
    assert log["counts"]["seen"] == 1 and log["counts"]["passed"] == 1
    assert len(log["requests"]) == 1


@E2E
def test_interception_deadline_fires_on_a_real_browser(loop, browser, base_url):
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "intercept_start", url_pattern="*headers*", auto_disable_after=0.2)
    loop.run_until_complete(asyncio.sleep(0.6))
    log = json.loads(run(loop, "intercept_log"))
    assert log["active"] is False and log["stop_reason"] == "auto-disabled on deadline"
    assert run(loop, "evaluate", "fetch('/headers').then(r => r.status)", await_promise=True) == "200"


@E2E
def test_humanized_click_never_lands_on_a_partial_overlay(loop, browser, base_url):
    """The centre passed the hit test, then a random point in the box was pressed."""
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    run(loop, "evaluate", """(() => {
        document.body.innerHTML =
          '<button id="b" style="position:absolute;left:100px;top:100px;width:200px;height:60px">go</button>'
          + '<div id="o" style="position:absolute;left:210px;top:90px;width:120px;height:80px;background:#000"></div>';
        window.hits = {b: 0, o: 0};
        document.getElementById('b').onclick = () => window.hits.b++;
        document.getElementById('o').onclick = () => window.hits.o++; })()""")
    for _ in range(6):
        out = run(loop, "click", selector="#b", humanize=True, verify=False)
        assert "clicked" in out, out
    hits = json.loads(run(loop, "evaluate", "JSON.stringify(window.hits)"))
    assert hits == {"b": 6, "o": 0}


@E2E
def test_samesite_round_trip_through_a_real_cookie_jar(loop, browser, base_url, tmp_path, monkeypatch):
    from nodriver.cdp import network as cdp_network

    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import storage

    monkeypatch.setattr(storage, "STORAGE_STATE_DIR", tmp_path)
    browser()
    run(loop, "navigate", f"{base_url}/keys.html")
    tab = server.BrowserState.active_tab()
    loop.run_until_complete(tab.send(cdp_network.set_cookie(
        name="rt", value="v", domain="localhost", path="/", secure=True,
        same_site=cdp_network.CookieSameSite.NONE)))
    assert not run(loop, "cookie_export", "rt.json").startswith("Error:")
    saved = {c["name"]: c for c in json.loads((tmp_path / "rt.json").read_text())}
    assert saved["rt"]["sameSite"] == "None"
    assert not run(loop, "cookie_import", file_path=str(tmp_path / "rt.json"), clear_first=True).startswith("Error:")
    jar = loop.run_until_complete(tab.send(cdp_network.get_cookies()))
    assert {c.name: c.same_site for c in jar}["rt"] == cdp_network.CookieSameSite.NONE
