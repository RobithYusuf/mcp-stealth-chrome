"""The saved tab list behind restore_tabs, without a browser.

Each guard here was mutation-checked: putting the bug back makes its test fail. The real-Chrome
round trips (browser_close, stdin EOF, a killed server, Quit) are in test_restore_tabs_e2e.py.
"""
import asyncio
import http.server
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import server, tab_record


@pytest.fixture
def tabs_dir(tmp_path, monkeypatch):
    folder = tmp_path / "window-state"
    monkeypatch.setenv(tab_record.ENV_DIR, str(folder))
    return folder


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "profile"
    p.mkdir()
    return p


PAGES = [("t1", "https://a.example/one"), ("t2", "https://b.example/two"), ("t3", "http://127.0.0.1/three")]
KNOWN = ["t1", "t2", "t3"]


def _doc(folder: Path, instance: str = "main") -> dict:
    return json.loads((folder / f"{instance}-tabs.json").read_text())


def test_record_is_private_and_written_whole(tabs_dir, profile):
    out = tab_record.save("main", profile, PAGES, KNOWN, "t2", "browser_close", force=True)
    assert out == "recorded 3 tab(s)"
    path = tabs_dir / "main-tabs.json"
    if sys.platform != "win32":  # Windows keeps no POSIX mode bits
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tabs_dir.iterdir()] == ["main-tabs.json"], "a temporary file was left"
    entry = _doc(tabs_dir)["profiles"][tab_record.profile_key(profile)]
    assert [t["url"] for t in entry["tabs"]] == [u for _, u in PAGES]
    assert entry["active"] == 1 and entry["reason"] == "browser_close"
    assert entry["profile"] == tab_record.profile_key(profile) and entry["saved_at"] > 0


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps no POSIX mode bits")
def test_record_stays_private_when_the_folder_umask_is_loose(tabs_dir, profile):
    previous = os.umask(0)
    try:
        tab_record.save("main", profile, PAGES, KNOWN, None, "x", force=True)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tabs_dir / "main-tabs.json").stat().st_mode) == 0o600


def test_record_follows_the_server_tab_order_not_chromes(tabs_dir, profile):
    """Chrome 157 listed tabs opened A, B, C, D as C, B, D, A (Target.getTargets)."""
    chrome_order = [("C", "https://x/c"), ("B", "https://x/b"), ("Z", "https://x/z"),
                    ("D", "https://x/d"), ("A", "https://x/a")]
    tab_record.save("main", profile, chrome_order, ["A", "B", "C", "D"], "C", "x", force=True)
    entry = _doc(tabs_dir)["profiles"][tab_record.profile_key(profile)]
    assert [t["url"] for t in entry["tabs"]] == ["https://x/a", "https://x/b", "https://x/c",
                                                 "https://x/d", "https://x/z"]
    assert entry["active"] == 2


def test_only_pages_worth_reopening_are_kept(tabs_dir, profile):
    pages = [("1", "chrome://newtab/"), ("2", "devtools://devtools/bundled/x"), ("3", "about:blank"),
             ("4", "chrome-extension://abc/popup.html"), ("5", "data:text/html,x"),
             ("6", "javascript:alert(1)"), ("7", "https://kept.example/"), ("8", "file:///tmp/x.html"),
             ("9", "blob:https://x/1")]
    tab_record.save("main", profile, pages, [], "3", "x", force=True)
    entry = _doc(tabs_dir)["profiles"][tab_record.profile_key(profile)]
    assert [t["url"] for t in entry["tabs"]] == ["https://kept.example/", "file:///tmp/x.html"]
    assert entry["active"] is None, "the selected page was not kept, so none is selected"


def test_an_empty_list_never_replaces_a_record(tabs_dir, profile):
    """A Quit closes every window before anything reads them: that is not 'no tabs'."""
    assert tab_record.save("main", profile, [], [], None, "x", force=True).startswith("not recorded")
    assert not (tabs_dir / "main-tabs.json").exists(), "nothing to save must not create a file"
    tab_record.save("main", profile, PAGES, KNOWN, "t1", "first", force=True)
    before = (tabs_dir / "main-tabs.json").read_text()
    for pages in ([], None, [("x", "about:blank"), ("y", "chrome://newtab/")]):
        out = tab_record.save("main", profile, pages, [], None, "later", force=True)
        assert out.startswith("not recorded") and "earlier record is kept" in out
        assert (tabs_dir / "main-tabs.json").read_text() == before


def test_each_profile_keeps_its_own_list(tabs_dir, tmp_path):
    real, fallback = tmp_path / "profile", tmp_path / "profile-pid4242"
    real.mkdir()
    fallback.mkdir()
    tab_record.save("main", real, PAGES[:1], [], None, "x", force=True)
    tab_record.save("main", fallback, PAGES[1:], [], None, "x", force=True)
    entry, _ = tab_record.load("main", real)
    assert [t["url"] for t in entry["tabs"]] == [PAGES[0][1]]
    entry, _ = tab_record.load("main", fallback)
    assert len(entry["tabs"]) == 2
    # A profile folder that is gone is dropped at the next write; the one written stays.
    fallback.rmdir()
    tab_record.save("main", real, PAGES, KNOWN, None, "x", force=True)
    assert list(_doc(tabs_dir)["profiles"]) == [tab_record.profile_key(real)]


def test_the_background_writer_leaves_an_unchanged_list_alone(tabs_dir, profile):
    tab_record.save("main", profile, PAGES, KNOWN, "t1", "x", force=True)
    path = tabs_dir / "main-tabs.json"
    before = path.read_text()
    assert tab_record.save("main", profile, list(reversed(PAGES)), KNOWN, "t1", "y",
                           force=False) == "unchanged"
    assert path.read_text() == before
    assert tab_record.save("main", profile, PAGES, KNOWN, "t2", "y", force=False).startswith("recorded")


def test_a_record_names_only_its_own_instance(tabs_dir, profile):
    with pytest.raises(ValueError):
        tab_record.record_path("../escape")
    assert tab_record.save("..", profile, PAGES, KNOWN, None, "x", force=True).startswith("not recorded")
    assert not tabs_dir.exists() or not any(tabs_dir.iterdir())


def test_shown_urls_hide_tokens():
    assert "s3cret" not in tab_record.shown_url("https://x/cb?code=1&access_token=s3cret&a=b")
    # An OAuth authorization code is exchanged for tokens; a SAML response and a CAS ticket
    # are bearer credentials too. Only in a URL: a JSON "code" is usually an error number.
    for url in ("https://x/cb?code=s3cret&state=xyz", "https://x/cb#code=s3cret&state=xyz",
                "https://x/acs?SAMLResponse=s3cret", "https://x/login?ticket=s3cret",
                "https://x/token?assertion=s3cret", "https://x/cb?authorization_code=s3cret"):
        shown = tab_record.shown_url(url)
        assert "s3cret" not in shown and "***" in shown, shown
    assert "state=xyz" in tab_record.shown_url("https://x/cb?code=s3cret&state=xyz")
    from mcp_stealth_chrome import helpers
    assert helpers.mask_body('{"code": 404, "message": "not found"}') == '{"code": 404, "message": "not found"}'
    assert helpers.mask_body("code=1&a=2") == "code=1&a=2"
    assert tab_record.shown_url("https://x/cb?a=1&b=2") == "https://x/cb?a=1&b=2"
    assert "s3cret" not in tab_record.shown_url("https://x/cb#access_token=s3cret&state=1")
    assert tab_record.shown_url("https://x/page#section-2") == "https://x/page#section-2"


# ── server side ───────────────────────────────────────────────────────────


def test_ephemeral_privacy_and_attached_keep_no_tabs(tabs_dir, profile, monkeypatch):
    assert server._tab_record_scope("main", None, False) is None, "persistent=False"
    assert server._tab_record_scope("main", profile, True) is None, "privacy=True"
    monkeypatch.setattr(server, "_ATTACHED_BROWSERS", {"att"})
    assert server._tab_record_scope("att", profile, False) is None, "attached"
    assert server._tab_record_scope("main", profile, False) == profile

    async def pages(_browser):
        return list(PAGES)

    monkeypatch.setattr(server, "_page_targets", pages)
    browser = SimpleNamespace()
    for prof, privacy in ((None, False), (profile, True)):
        out = asyncio.run(server._record_tabs_before_close("main", browser, [], 0, prof, privacy, "x"))
        assert out.startswith("not recorded")
    assert not tabs_dir.exists() or not any(tabs_dir.iterdir())
    # The background writer is not even scheduled for them.
    monkeypatch.setattr(server, "_TAB_RECORD_DIRTY", {})
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    for prof, privacy in ((None, False), (profile, True)):
        monkeypatch.setattr(server.BrowserState, "current_profile_dir", prof, raising=False)
        monkeypatch.setattr(server.BrowserState, "current_privacy", privacy, raising=False)
        server._mark_tabs_changed("navigate")
        assert server._TAB_RECORD_DIRTY == {}


def test_a_close_waits_at_most_about_a_second_for_the_tabs(tabs_dir, profile, monkeypatch):
    tab_record.save("main", profile, PAGES, KNOWN, "t1", "earlier", force=True)
    before = (tabs_dir / "main-tabs.json").read_text()

    async def hangs(_browser):
        await asyncio.sleep(30)

    monkeypatch.setattr(server, "_page_targets", hangs)
    started = time.monotonic()
    out = asyncio.run(server._record_tabs_before_close("main", SimpleNamespace(), [], 0, profile,
                                                       False, "browser_close"))
    assert time.monotonic() - started < 1.5
    assert out.startswith("not recorded")
    assert (tabs_dir / "main-tabs.json").read_text() == before


def _tab(target_id):
    return SimpleNamespace(target=SimpleNamespace(target_id=target_id))


def test_close_records_in_tab_order_with_the_selected_tab(tabs_dir, profile, monkeypatch):
    async def pages(_browser):
        return [("t3", PAGES[2][1]), ("t1", PAGES[0][1]), ("t2", PAGES[1][1])]

    monkeypatch.setattr(server, "_page_targets", pages)
    out = asyncio.run(server._record_tabs_before_close(
        "main", SimpleNamespace(), [_tab("t1"), _tab("t2"), _tab("t3")], 2, profile, False,
        "browser_close"))
    assert out == "recorded 3 tab(s)"
    entry, _ = tab_record.load("main", profile)
    assert [t["url"] for t in entry["tabs"]] == [u for _, u in PAGES] and entry["active"] == 2


def test_the_background_writer_skips_a_list_that_is_changing(tabs_dir, profile, monkeypatch):
    """Windows closing one by one on a Quit must not be written over the full list."""
    tab_record.save("main", profile, PAGES, KNOWN, "t1", "earlier", force=True)
    before = (tabs_dir / "main-tabs.json").read_text()
    reads = iter([list(PAGES), list(PAGES[:1])])

    async def pages(_browser):
        return next(reads)

    browser = SimpleNamespace()
    monkeypatch.setattr(server, "_page_targets", pages)
    monkeypatch.setattr(server, "_TAB_RECORD_DIRTY", {})
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [_tab("t1")], raising=False)
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", profile, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_privacy", False, raising=False)
    key = server.BrowserState.session_key()
    iid = server.BrowserState.current_instance_id
    assert asyncio.run(server._record_tabs_in_background(key, iid)) == "changing"
    assert (tabs_dir / "main-tabs.json").read_text() == before
    assert (key, iid) in server._TAB_RECORD_DIRTY, "looked at again later"
    # A browser being closed is left to the close.
    reads = iter([list(PAGES[:2])] * 2)
    setattr(browser, server._CLOSING_ATTR, time.monotonic())
    assert asyncio.run(server._record_tabs_in_background(key, iid)) == "skipped"
    delattr(browser, server._CLOSING_ATTR)
    assert asyncio.run(server._record_tabs_in_background(key, iid)) == "recorded 2 tab(s)"


def test_a_close_that_failed_keeps_its_record_however_long_chrome_lingers(tabs_dir, profile, monkeypatch):
    """A close records the full list, then the browser does not end (its handle is kept for a
    retry). Neither the background writer, 30 s or 10 minutes later, nor the retried close may
    write the half-closed list over it."""
    reads = {"pages": list(PAGES)}

    async def pages(_browser):
        return list(reads["pages"])

    browser = SimpleNamespace()
    monkeypatch.setattr(server, "_page_targets", pages)
    monkeypatch.setattr(server, "_TAB_RECORD_DIRTY", {})
    assert asyncio.run(server._record_tabs_before_close(
        "main", browser, [_tab("t1"), _tab("t2"), _tab("t3")], 0, profile, False,
        "browser_close")) == "recorded 3 tab(s)"
    full = (tabs_dir / "main-tabs.json").read_text()
    reads["pages"] = list(PAGES[:1])  # the windows went one by one, Chrome itself did not
    monkeypatch.setattr(server.BrowserState, "browser", browser, raising=False)
    monkeypatch.setattr(server.BrowserState, "tabs", [_tab("t1")], raising=False)
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", profile, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_privacy", False, raising=False)
    key, iid = server.BrowserState.session_key(), server.BrowserState.current_instance_id
    for later in (31, 600):  # the close began this long ago
        setattr(browser, server._CLOSING_ATTR, time.monotonic() - later)
        assert asyncio.run(server._record_tabs_in_background(key, iid)) == "skipped"
    assert asyncio.run(server._record_tabs_before_close(
        "main", browser, [_tab("t1")], 0, profile, False, "browser_close")).startswith("not recorded")
    assert (tabs_dir / "main-tabs.json").read_text() == full
    # A new browser for the instance is recorded as usual.
    fresh = SimpleNamespace()
    monkeypatch.setattr(server.BrowserState, "browser", fresh, raising=False)
    assert asyncio.run(server._record_tabs_in_background(key, iid)) == "recorded 1 tab(s)"


def test_a_tool_call_schedules_a_write_and_nothing_else(tabs_dir, profile, monkeypatch):
    monkeypatch.setattr(server, "_TAB_RECORD_DIRTY", {})
    monkeypatch.setattr(server.BrowserState, "browser", SimpleNamespace(), raising=False)
    monkeypatch.setattr(server.BrowserState, "current_profile_dir", profile, raising=False)
    monkeypatch.setattr(server.BrowserState, "current_privacy", False, raising=False)
    started = time.perf_counter()
    for _ in range(1000):
        server._mark_tabs_changed("click")
    per_call = (time.perf_counter() - started) / 1000
    assert list(server._TAB_RECORD_DIRTY) == [(server.BrowserState.session_key(),
                                               server.BrowserState.current_instance_id)]
    assert per_call < 0.001, f"{per_call * 1e6:.1f} µs per tool call"
    assert not tabs_dir.exists(), "a tool call must not write by itself"
    assert server._mark_tabs_changed in server._app_module.AFTER_TOOL_HOOKS


class _JsonList(http.server.BaseHTTPRequestHandler):
    body = b"[]"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


def test_stdin_eof_saves_from_the_devtools_endpoint(tabs_dir, profile):
    """At stdin EOF the event loop is gone: the list comes from /json/list over plain HTTP."""
    listing = [{"id": "t2", "type": "page", "url": PAGES[1][1]},
               {"id": "sw", "type": "service_worker", "url": "https://b.example/sw.js"},
               {"id": "t1", "type": "page", "url": PAGES[0][1]},
               {"id": "t9", "type": "page", "url": "chrome://newtab/"}]
    handler = type("H", (_JsonList,), {"body": json.dumps(listing).encode()})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        browser = SimpleNamespace(config=SimpleNamespace(host="127.0.0.1", port=srv.server_port),
                                  stopped=False)
        out = server._record_tabs_at_exit("main", browser, [_tab("t1"), _tab("t2")], 1, profile, False)
    finally:
        srv.shutdown()
    assert out == "recorded 2 tab(s)"
    entry, _ = tab_record.load("main", profile)
    assert [t["url"] for t in entry["tabs"]] == [PAGES[0][1], PAGES[1][1]]
    assert entry["active"] == 1 and entry["reason"] == "server exit"


def test_stdin_eof_with_chrome_gone_keeps_the_earlier_record(tabs_dir, profile):
    tab_record.save("main", profile, PAGES, KNOWN, "t1", "earlier", force=True)
    before = (tabs_dir / "main-tabs.json").read_text()
    browser = SimpleNamespace(config=SimpleNamespace(host="127.0.0.1", port=9), stopped=False)
    started = time.monotonic()
    server._record_tabs_at_exit("main", browser, [], 0, profile, False)
    assert time.monotonic() - started < 2.5
    assert (tabs_dir / "main-tabs.json").read_text() == before


# ── restore ───────────────────────────────────────────────────────────────


class _FakeBrowser:
    def __init__(self, main):
        self._tabs = [main]

    @property
    def tabs(self):
        return list(self._tabs)

    async def update_targets(self):
        return None

    async def get(self, _url, new_tab=False):
        tab = _tab(f"new{len(self._tabs)}")
        tab.activate = _noop
        self._tabs.append(tab)
        return tab


async def _noop(*_a, **_kw):
    return None


@pytest.fixture
def restore_env(monkeypatch):
    loaded = []

    async def load(tab, url):
        loaded.append((tab.target.target_id, url))
        if "down" in url:
            return {"final_url": "chrome-error://chromewebdata/", "load_failure": "net::ERR_CONNECTION_REFUSED"}
        return {"final_url": url, "status": 200, "ready_state": "complete"}

    monkeypatch.setattr(server, "_load_saved_tab", load)
    monkeypatch.setattr(server, "_apply_fingerprint_to_tab", _noop)
    return loaded


def _reopen(profile, url="about:blank", privacy=False, prof="same"):
    main = _tab("main")
    main.activate = _noop
    browser = _FakeBrowser(main)
    result = asyncio.run(server._reopen_saved_tabs(
        "main", browser, main, profile if prof == "same" else prof, privacy, True, {}, url))
    return result


def test_restore_reopens_in_order_and_selects_the_saved_tab(tabs_dir, profile, restore_env):
    tab_record.save("main", profile, PAGES, KNOWN, "t2", "browser_close", force=True)
    tabs, select, report, suffix = _reopen(profile)
    assert [t.target.target_id for t in tabs] == ["main", "new1", "new2"]
    assert restore_env == [("main", PAGES[0][1]), ("new1", PAGES[1][1]), ("new2", PAGES[2][1])]
    assert select == 1 and report["selected"] == 1
    assert [r["tab"] for r in report["reopened"]] == [0, 1, 2] and not report["failed"]
    assert report["reopened"][0]["status"] == 200
    assert "reopened 3 of 3 saved tab(s)" in suffix and "tab 1 is selected" in suffix


def test_restore_with_a_launch_url_keeps_it_first_and_selected(tabs_dir, profile, restore_env):
    tab_record.save("main", profile, PAGES, KNOWN, "t3", "browser_close", force=True)
    tabs, select, report, _ = _reopen(profile, url=PAGES[1][1])
    assert select == 0, "the page the caller asked for stays selected"
    assert [u for _, u in restore_env] == [PAGES[0][1], PAGES[2][1]]
    assert restore_env[0][0] != "main", "the launch url's tab is not navigated away"
    assert report["skipped"][0]["note"] == "already open as the launch url"
    assert [r["tab"] for r in report["reopened"]] == [1, 2]


def test_restore_reports_each_failure_and_never_raises(tabs_dir, profile, restore_env):
    pages = [("a", "https://ok.example/"), ("b", "https://down.example/?token=s3cret"),
             ("c", "https://ok.example/2")]
    tab_record.save("main", profile, pages, ["a", "b", "c"], "b", "browser_close", force=True)
    tabs, select, report, suffix = _reopen(profile)
    assert len(report["reopened"]) == 2 and len(report["failed"]) == 1
    failed = report["failed"][0]
    assert "ERR_CONNECTION_REFUSED" in failed["error"] and failed["tab"] == 1
    assert "s3cret" not in json.dumps(report) + suffix, "a token reached the tool output"
    assert "1 failed" in suffix


def test_restore_masks_every_url_it_shows(tabs_dir, profile, restore_env):
    pages = [("a", "https://x.example/cb?access_token=s3cret1"),
             ("b", "https://y.example/#id_token=s3cret2&state=1")]
    tab_record.save("main", profile, pages, ["a", "b"], "a", "x", force=True)
    _tabs, _select, report, suffix = _reopen(profile)
    shown = json.dumps(report) + suffix
    assert "s3cret1" not in shown and "s3cret2" not in shown
    assert "access_token=***" in shown and "id_token=***" in shown


def test_restore_says_plainly_when_there_is_nothing(tabs_dir, profile, restore_env, monkeypatch):
    _tabs, select, report, suffix = _reopen(profile)
    assert select == 0 and "no tabs were saved for instance 'main'" in suffix
    tabs_dir.mkdir(parents=True, exist_ok=True)
    (tabs_dir / "main-tabs.json").write_text("{not json")
    _tabs, _s, _r, suffix = _reopen(profile)
    assert "could not be read" in suffix
    (tabs_dir / "main-tabs.json").unlink()
    other = profile.parent / "other"
    other.mkdir()
    tab_record.save("main", other, PAGES, KNOWN, None, "x", force=True)
    _tabs, _s, _r, suffix = _reopen(profile)
    assert "no tabs were saved for this profile" in suffix
    for prof, privacy, words in ((None, False, "persistent=False"), (profile, True, "privacy")):
        _tabs, _s, _r, suffix = _reopen(profile, privacy=privacy, prof=prof)
        assert "keeps no tabs" in suffix and words in suffix
    # An old list is named and left closed.
    tab_record.save("main", profile, PAGES, KNOWN, None, "x", force=True)
    doc = json.loads((tabs_dir / "main-tabs.json").read_text())
    doc["profiles"][tab_record.profile_key(profile)]["saved_at"] = time.time() - 40 * 86400
    (tabs_dir / "main-tabs.json").write_text(json.dumps(doc))
    _tabs, _s, report, suffix = _reopen(profile)
    assert "40 days ago" in suffix and "MCP_STEALTH_RESTORE_TABS_MAX_DAYS" in suffix
    assert restore_env == [], "nothing was opened"


def test_a_tampered_record_opens_only_web_and_file_pages(tabs_dir, profile, restore_env):
    tab_record.save("main", profile, PAGES[:1], [], None, "x", force=True)
    doc = json.loads((tabs_dir / "main-tabs.json").read_text())
    doc["profiles"][tab_record.profile_key(profile)]["tabs"].append({"url": "javascript:alert(1)"})
    (tabs_dir / "main-tabs.json").write_text(json.dumps(doc))
    _tabs, _s, report, _ = _reopen(profile)
    assert [u for _, u in restore_env] == [PAGES[0][1]]
    assert "only http, https and file" in report["failed"][0]["error"]


# ── forgetting ────────────────────────────────────────────────────────────


def test_forget_tabs_removes_the_list(tabs_dir, profile, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    tab_record.save("main", profile, PAGES, KNOWN, None, "x", force=True)
    tab_record.save("main", other, PAGES, KNOWN, None, "x", force=True)
    assert "removed" in server._forget_tabs_note("main", profile, False)
    assert tab_record.load("main", profile)[0] is None
    assert tab_record.load("main", other)[0] is not None, "another profile's list stays"
    assert "no saved tab list" in server._forget_tabs_note("main", profile, False)
    assert tab_record.forget_instance("main") and not (tabs_dir / "main-tabs.json").exists()


def test_removing_a_fallback_profile_removes_its_saved_tabs(tmp_path, monkeypatch, tabs_dir):
    from mcp_stealth_chrome import state
    from mcp_stealth_chrome.tools import profiles

    data = tmp_path / "data"
    fallback = data / "profile-pid999999"
    fallback.mkdir(parents=True)
    keep = data / "profile"
    keep.mkdir()
    monkeypatch.setenv(state.ENV_DATA_DIR, str(data))
    monkeypatch.setattr(state, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda p: [])
    if sys.platform == "win32":
        monkeypatch.setattr(profiles.sys, "platform", "linux")
    tab_record.save("main", fallback, PAGES, KNOWN, None, "x", force=True)
    tab_record.save("main", keep, PAGES[:1], [], None, "x", force=True)
    preview = profiles._remove_fallback("profile-pid999999", False)
    assert preview["would_lose"]["saved_tabs"] == 3
    done = profiles._remove_fallback("profile-pid999999", True)
    assert done["removed"] and done["saved_tab_lists_removed"] == 1
    assert tab_record.load("main", fallback)[0] is None
    assert tab_record.load("main", keep)[0] is not None


@pytest.mark.parametrize("tool", ["browser_launch", "spawn_browser"])
def test_a_launch_records_its_tabs_before_it_returns(tabs_dir, profile, monkeypatch, tool):
    """The quit watcher ends a browser once its last window has gone, and then nothing is left
    to read. A Quit in the first seconds, before the background writer's first pass (2 s after
    the call, two reads 0.3 s apart), lost the list; the launch now records it as it returns."""
    from test_focus_restore import _fake_launch
    _fake_launch(monkeypatch)

    async def pages(_browser):
        return [("T1", "https://a.example/one")]

    monkeypatch.setattr(server, "_page_targets", pages)
    monkeypatch.setattr(server, "_target_key", lambda tab: "T1")
    monkeypatch.setattr(server, "_TAB_RECORD_DEBOUNCE", 0)  # no background writer in this test
    try:
        if tool == "browser_launch":
            fn = server.mcp._tool_manager._tools["browser_launch"].fn
            out = asyncio.run(fn(user_data_dir=str(profile), headless=True, auto_verify=False,
                                 url="https://a.example/one"))
            assert "launched" in out and not out.startswith("Error"), out
            iid = "main"
        else:
            ok_, out = asyncio.run(server._launch_browser_instance(
                instance_id="inst", url="https://a.example/one", headless=True, proxy=None,
                user_agent=None, window_width=800, window_height=600, persistent=True,
                lang="en-US", extra_args=None, storage_state_path=None, idle_timeout=0,
                profile_dir_override=str(profile)))
            assert ok_, out
            iid = "inst"
    finally:
        server.BrowserState.reset()
        server.BrowserState.instances.pop("inst", None)
    entry, _ = tab_record.load(iid, profile)
    assert entry and [t["url"] for t in entry["tabs"]] == ["https://a.example/one"], entry
