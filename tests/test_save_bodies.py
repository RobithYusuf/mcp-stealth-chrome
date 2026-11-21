"""network_start(save_bodies=...) on fakes: the extract grammar, masking, the append helper,
accounting. The real-Chrome side is tests/test_save_bodies_e2e.py."""
import asyncio
import base64
import json
import os
import sys
import threading
import time

import pytest

from mcp_stealth_chrome import body_saver, helpers

posix_only = pytest.mark.skipif(os.name == "nt", reason="append_private_line's O_NOFOLLOW, dir_fd and "
                                "flock guarantees are POSIX; Windows has its own checks (docstring)")


# ── the extract grammar ─────────────────────────────────────────────────────

BODY = {
    "data": {
        "item": {"title": "Kopi", "models": [{"name": "a", "price": 1, "stock": 3, "x": 0},
                                            {"name": "b", "price": 2}]},
        "shop_detailed": {"name": "Toko", "rating_star": 4.8, "follower_count": 7},
        "empty": [],
        "grid": [[1, 2], [3]],
    },
}


@pytest.mark.parametrize("path, value", [
    ("data.item.title", "Kopi"),
    ("data.item.models[].{name,price,stock}", [{"name": "a", "price": 1, "stock": 3},
                                               {"name": "b", "price": 2, "stock": None}]),
    ("data.shop_detailed.{name,rating_star}", {"name": "Toko", "rating_star": 4.8}),
    ("data.item.models[].name", ["a", "b"]),
    ("data.item.models.[].price", [1, 2]),
    ("data.empty[]", []),
    ("data.grid[].[]", [[1, 2], [3]]),
    ("{data}", {"data": BODY["data"]}),
    (" data.item.title ", "Kopi"),
])
def test_extract_paths_find_what_they_name(path, value):
    out, missing = body_saver.extract(BODY, [(path.strip(), body_saver.parse_path(path))])
    assert out == {path.strip(): value} and missing == []


@pytest.mark.parametrize("path", ["data.nope", "data.item.title.more", "data.item[]",
                                  "data.item.models[].nope", "data.shop_detailed.{a,b}",
                                  "data.item.title[]"])
def test_a_path_that_finds_nothing_is_null_and_named(path):
    out, missing = body_saver.extract(BODY, [(path, body_saver.parse_path(path))])
    assert out == {path: None} and missing == [path]


@pytest.mark.parametrize("path", ["", "   ", "a..b", ".a", "a.", "{x,y}.a", "a[b]", "a[0]", "a[][]",
                                  "a.{x,,y}", "a.{}", "a b", "a.{x y}", "$.a", "a.b()", "a|b",
                                  "data[?(@.x)]", "a.*", "x" * 301, 5, None, ["a"]])
def test_a_path_that_cannot_be_read_is_refused(path):
    with pytest.raises(ValueError):
        body_saver.parse_path(path)


def test_the_grammar_fits_in_three_lines_of_the_docstring():
    from mcp_stealth_chrome.tools import monitoring
    doc = " ".join((monitoring.network_start.__doc__ or "").split())
    assert ('extract paths: "a.b" a key at each dot, "a[]" each element of a list, "{x,y}" last '
            'keeps those keys; a path that finds nothing is null, named in meta.missing') in doc


# ── save_bodies entries ─────────────────────────────────────────────────────

def _entry(**kw):
    item = {"url_pattern": "*/api/*", "name": "api"}
    item.update(kw)
    return item


@pytest.mark.parametrize("value, words", [
    ("nope", "must be a list"),
    ([1], "must be an object"),
    ([_entry(urlpattern="*")], "unknown key"),
    ([_entry(url_pattern="")], "url_pattern"),
    ([_entry(name="../escape")], "file-safe word"),
    ([_entry(name=".hidden")], "file-safe word"),
    ([_entry(name="a/b")], "file-safe word"),
    ([_entry(name="x" * 65)], "file-safe word"),
    ([_entry(name="")], "file-safe word"),
    ([_entry(), _entry(name="API")], "used twice"),
    ([_entry(extract="data.title")], "must be a list"),
    ([_entry(extract=["data..x"])], "save_bodies[0]: cannot read extract path"),
    ([_entry(extract=[3])], "must be a string"),
    ([_entry()] * 21, "at most 20"),
])
def test_save_bodies_is_checked_before_anything_starts(value, words):
    with pytest.raises(ValueError) as caught:
        body_saver.parse_entries(value)
    assert words in str(caught.value), caught.value


def test_a_response_goes_to_the_first_entry_that_matches():
    entries = body_saver.parse_entries([_entry(url_pattern="*/api/v4/*", name="v4"),
                                        _entry(url_pattern="*/api/*", name="any")])
    url = "https://shop.test/api/v4/pdp/get_pc?item=1"
    assert next(e for e in entries if e.regex.match(url)).name == "v4"
    assert not entries[0].regex.match("https://shop.test/apix")


# ── one record: masking, then extract ───────────────────────────────────────

SECRET_BODY = {"session_info": None, "error": 0, "token": "tok-123456",
               "data": {"title": "t", "account": {"token": "inner-secret", "user": "u1"},
                        "credentials": {"user": "u2", "note": "under a secret parent"}}}
META = {"request_url": "https://shop.test/api?item=1&access_token=SECRETQ#id_token=FRAG",
        "page_url": "https://shop.test/p/1?code=OAUTHCODE", "ts": "t", "status": 200,
        "mime": "application/json"}


def _record(body, mode="none", paths=(), b64=False, max_bytes=10 * 2**20):
    parsed = [(p, body_saver.parse_path(p)) for p in paths]
    line, info = body_saver.make_record(META, body, b64, mode, parsed, max_bytes)
    return (json.loads(line) if line else None), info


def test_a_record_is_masked_as_network_get_shows_it():
    record, info = _record(json.dumps(SECRET_BODY))
    text = json.dumps(record)
    for secret in ("SECRETQ", "FRAG", "OAUTHCODE", "tok-123456", "inner-secret", "u2"):
        assert secret not in text, secret
    assert record["body"]["session_info"] is None  # an ordinary null stays as it is
    assert record["body"]["error"] == 0 and record["body"]["data"]["title"] == "t"
    assert record["body"]["data"]["account"]["user"] == "u1"
    assert record["body"]["data"]["credentials"]["user"].startswith("*** (")  # under a secret key
    assert record["body"]["token"].startswith("*** (")
    # the same as network_get's own masking of that body
    assert record["body"] == json.loads(helpers.mask_body(json.dumps(SECRET_BODY)))
    assert record["meta"]["request_url"] == helpers.mask_url(META["request_url"])
    assert info["body_keys"] == ["session_info", "error", "token", "data"]


def test_body_mode_reveals_values_under_a_secret_parent_only():
    record, _ = _record(json.dumps(SECRET_BODY), mode="body")
    assert record["body"]["data"]["credentials"]["user"] == "u2"
    assert record["body"]["data"]["account"]["token"].startswith("*** (")
    assert "SECRETQ" not in json.dumps(record["meta"])
    assert record["body"] == json.loads(helpers.mask_body(json.dumps(SECRET_BODY), own_keys_only=True))


def test_revealed_writes_everything_as_it_is():
    record, _ = _record(json.dumps(SECRET_BODY), mode="all")
    assert record["body"] == SECRET_BODY and record["meta"]["request_url"] == META["request_url"]


def test_extract_runs_on_the_masked_body():
    record, _ = _record(json.dumps(SECRET_BODY), paths=[
        "data.account.token", "data.account.{token,user}", "data.credentials.user",
        "session_info", "data.nope"])
    body = record["body"]
    assert body["data.account.token"].startswith("*** (")
    assert body["data.account.{token,user}"]["token"].startswith("*** (")
    assert body["data.account.{token,user}"]["user"] == "u1"
    assert body["data.credentials.user"].startswith("*** (")
    assert body["session_info"] is None and record["meta"]["missing"] == ["data.nope"]
    assert "inner-secret" not in json.dumps(record)


def test_text_that_is_not_json_is_kept_as_text_and_said_so():
    record, info = _record("access_token=abc&q=1")
    assert record["meta"]["json"] is False and record["body"] == "access_token=***&q=1"
    assert info["body_keys"] == "text, 20 characters"
    record, _ = _record("<html>hi</html>", paths=["a.b"])
    assert record["body"] == {"a.b": None} and record["meta"]["missing"] == ["a.b"]


def test_a_base64_body_is_decoded():
    raw = base64.b64encode(json.dumps({"a": {"b": 2}}).encode()).decode()
    record, _ = _record(raw, b64=True, paths=["a.b"])
    assert record["body"] == {"a.b": 2} and record["meta"]["base64"] is True


@pytest.mark.parametrize("body, b64, reason", [
    (base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\xff").decode(), True, "binary"),
    ("not base64 at all!", True, "undecodable"),
    (base64.b64encode(b"x" * 101).decode(), True, "too_large"),
    ("y" * 101, False, "too_large"),
])
def test_what_is_not_saved_says_why(body, b64, reason):
    record, why = _record(body, b64=b64, max_bytes=100)
    assert record is None and why == reason


# ── the append helper ───────────────────────────────────────────────────────

@posix_only
def test_lines_are_appended_to_a_private_file(tmp_path):
    assert helpers.append_private_line(tmp_path, "a.jsonl", b'{"n":1}\n')
    assert helpers.append_private_line(tmp_path, "a.jsonl", b'{"n":2}\n')
    path = tmp_path / "a.jsonl"
    assert path.read_bytes() == b'{"n":1}\n{"n":2}\n'
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    os.chmod(path, 0o644)  # a file made wider is narrowed again
    helpers.append_private_line(tmp_path, "a.jsonl", b"3\n")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("name, line", [("../x", b"1\n"), ("a/b", b"1\n"), ("..", b"1\n"),
                                        ("a", b"no newline"), ("a", b"two\nlines\n")])
def test_the_helper_takes_one_line_and_a_bare_name(tmp_path, name, line):
    with pytest.raises(ValueError):
        helpers.append_private_line(tmp_path, name, line)


@posix_only
def test_a_planted_link_is_refused_not_written_through(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    folder = tmp_path / "day"
    folder.mkdir()
    (folder / "pdp.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        helpers.append_private_line(folder, "pdp.jsonl", b"x\n")
    assert outside.read_text() == "keep"
    # a linked folder too
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        helpers.append_private_line(tmp_path / "linked", "pdp.jsonl", b"x\n")
    assert not (real / "pdp.jsonl").exists()
    # a hard link to a file elsewhere
    other = tmp_path / "other.jsonl"
    other.write_text("keep\n")
    os.link(other, folder / "hard.jsonl")
    with pytest.raises(OSError, match="links"):
        helpers.append_private_line(folder, "hard.jsonl", b"x\n")
    assert other.read_text() == "keep\n"


@posix_only
def test_a_fifo_at_the_name_does_not_block(tmp_path):
    os.mkfifo(tmp_path / "f.jsonl")
    t0 = time.monotonic()
    with pytest.raises(OSError):
        helpers.append_private_line(tmp_path, "f.jsonl", b"x\n")
    assert time.monotonic() - t0 < 2


@posix_only
def test_two_writers_never_mix_their_lines(tmp_path):
    def writer(tag: str):
        for i in range(300):
            line = json.dumps({"w": tag, "i": i, "pad": tag * 2000}).encode() + b"\n"
            assert helpers.append_private_line(tmp_path, "both.jsonl", line)
    threads = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "both.jsonl").read_bytes().splitlines()
    assert len(lines) == 600
    records = [json.loads(line) for line in lines]
    for tag in "ab":
        assert [r["i"] for r in records if r["w"] == tag] == list(range(300))


@posix_only
def test_a_full_file_is_left_for_the_next(tmp_path):
    assert helpers.append_private_line(tmp_path, "r.jsonl", b"a" * 60 + b"\n", max_bytes=100)
    assert not helpers.append_private_line(tmp_path, "r.jsonl", b"b" * 60 + b"\n", max_bytes=100)
    assert (tmp_path / "r.jsonl").stat().st_size == 61
    # one line bigger than the limit still goes into an empty file
    assert helpers.append_private_line(tmp_path, "s.jsonl", b"c" * 200 + b"\n", max_bytes=100)


@posix_only
def test_a_short_write_is_taken_back(tmp_path, monkeypatch):
    helpers.append_private_line(tmp_path, "w.jsonl", b"first\n")
    real = os.write
    monkeypatch.setattr(helpers.os, "write", lambda fd, data: real(fd, data[:3]))
    with pytest.raises(OSError, match="short write"):
        helpers.append_private_line(tmp_path, "w.jsonl", b"second line\n")
    monkeypatch.setattr(helpers.os, "write", real)
    assert (tmp_path / "w.jsonl").read_bytes() == b"first\n"


@posix_only
def test_a_lock_held_elsewhere_is_an_error_not_a_wait(tmp_path):
    import fcntl
    path = tmp_path / "l.jsonl"
    path.write_bytes(b"")
    os.chmod(path, 0o600)
    holder = os.open(path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        with pytest.raises(OSError, match="locked"):
            helpers.append_private_line(tmp_path, "l.jsonl", b"x\n", lock_timeout=0.3)
        assert time.monotonic() - t0 < 1.5
    finally:
        os.close(holder)
    assert path.read_bytes() == b""


@posix_only
def test_a_saver_rotates_past_the_limit_and_continues_the_last_file(tmp_path, monkeypatch):
    monkeypatch.setattr(body_saver, "ROTATE_BYTES", 150)
    saver = _saver(tmp_path)
    for i in range(5):
        saver._append("pdp", json.dumps({"i": i, "pad": "p" * 40}).encode() + b"\n")
    names = saver.files["pdp"]["names"]
    assert names == ["pdp.jsonl", "pdp.2.jsonl", "pdp.3.jsonl"], names
    # a later run on the same day goes on in the last file, never back to the first
    again = _saver(tmp_path)
    assert again.files["pdp"]["index"] == 3


# ── accounting ──────────────────────────────────────────────────────────────

def _saver(folder, mode="none", max_bytes=10 * 2**20, entries=None):
    entries = entries or body_saver.parse_entries([
        {"url_pattern": "*/api/*", "name": "pdp", "extract": ["a"]}])
    saver = body_saver.Saver("ws://127.0.0.1:9/devtools/browser/x", "main", "s-test", entries, mode,
                             folder, max_bytes, 64 * 2**20, runner=None)
    saver.starting = False
    return saver


def _invariant(saver):
    report = saver.report()
    for item in report["names"].values():
        assert item["matched"] == item["saved"] + item["pending"] + sum(item["not_saved"].values()), item
    return report


def _request(saver, sid, rid, url="https://x.test/api/1"):
    saver._on_event("Network.requestWillBeSent", {
        "requestId": rid, "request": {"url": url}, "documentURL": "https://x.test/p", "wallTime": 1.0,
        "type": "Fetch"}, sid)
    saver._on_event("Network.responseReceived", {
        "requestId": rid, "response": {"url": url, "status": 200, "mimeType": "application/json"}}, sid)


def test_every_matched_response_is_accounted_for(tmp_path):
    saver = _saver(tmp_path)
    replies = {"ok": {"result": {"body": '{"a": 1}', "base64Encoded": False}},
               "gone": {"error": {"message": "No resource with given identifier found"}},
               "big": {"error": {"message": "No data found for resource with given identifier"}},
               "odd": {"error": {"message": "Something else"}},
               "png": {"result": {"body": base64.b64encode(b"\x89PNG\x00\xff").decode(),
                                  "base64Encoded": True}}}

    async def send(method, params=None, sid=None, wait=True, timeout=10.0):
        assert method == "Network.getResponseBody"
        return replies[params["requestId"]]

    async def scenario():
        saver._send = send
        saver.sessions["S1"] = {"type": "page"}
        saver.sessions["S2"] = {"type": "page"}
        for rid in ("ok", "gone", "big", "odd", "png"):
            _request(saver, "S1", rid)
        saver._on_event("Network.dataReceived", {"requestId": "big", "dataLength": 11 * 2**20}, "S1")
        _request(saver, "S1", "failed")
        _request(saver, "S2", "closed")
        _request(saver, "S1", "loading")
        _request(saver, "S1", "other", url="https://x.test/elsewhere")  # not matched at all
        saver._on_event("Network.requestWillBeSent", {"requestId": "pre", "type": "Preflight",
                                                      "request": {"url": "https://x.test/api/1"}}, "S1")
        _invariant(saver)
        for rid in ("ok", "gone", "big", "odd", "png"):
            saver._on_event("Network.loadingFinished", {"requestId": rid}, "S1")
        saver._on_event("Network.loadingFailed", {"requestId": "failed", "errorText": "net::ERR_FAILED"}, "S1")
        saver._on_event("Target.detachedFromTarget", {"sessionId": "S2"}, "")
        while saver._reads or saver._writes:
            await asyncio.sleep(0.01)
        return _invariant(saver)

    report = asyncio.run(scenario())
    pdp = report["names"]["pdp"]
    assert pdp["matched"] == 8 and pdp["saved"] == 1 and pdp["pending"] == 1, pdp
    assert pdp["not_saved"] == {"body_gone": 1, "too_large": 1, "read_error": 1, "binary": 1,
                                "load_failed": 1, "tab_closed": 1}, pdp
    assert pdp["last"]["body_keys"] == ["a"]
    saver._end("test")  # what is still pending is counted as stopped, once
    saver._end("again")
    pdp = _invariant(saver)["names"]["pdp"]
    assert pdp["pending"] == 0 and pdp["not_saved"]["stopped"] == 1, pdp
    lines = (tmp_path / "pdp.jsonl").read_text().splitlines()
    assert [json.loads(line)["body"] for line in lines] == [{"a": 1}]
    saver._writer.shutdown()


def test_a_redirect_is_one_request_and_a_late_match_counts(tmp_path):
    saver = _saver(tmp_path)
    saver._on_event("Network.requestWillBeSent", {"requestId": "r", "request": {"url": "https://x.test/api/a"}}, "S")
    saver._on_event("Network.requestWillBeSent", {"requestId": "r", "request": {"url": "https://x.test/api/b"},
                                                  "redirectResponse": {}}, "S")
    saver._on_event("Network.requestWillBeSent", {"requestId": "q", "request": {"url": "https://x.test/go"}}, "S")
    saver._on_event("Network.requestWillBeSent", {"requestId": "q", "request": {"url": "https://x.test/api/c"},
                                                  "redirectResponse": {}}, "S")
    assert _invariant(saver)["names"]["pdp"]["matched"] == 2
    saver._writer.shutdown()


def test_a_body_being_read_when_its_tab_closes_is_left_to_its_read(tmp_path):
    saver = _saver(tmp_path)
    saver.sessions["S"] = {"type": "page"}
    _request(saver, "S", "r")
    saver.pending[("S", "r")].reading = True
    saver._on_event("Target.detachedFromTarget", {"sessionId": "S"}, "")
    assert _invariant(saver)["names"]["pdp"]["pending"] == 1
    saver._writer.shutdown()


def test_nothing_is_written_once_saving_stopped(tmp_path):
    saver = _saver(tmp_path)
    _request(saver, "S", "r")
    request = saver.pending[("S", "r")]
    saver._end("stopped by the test")
    saver._write(request, '{"a": 1}', False)
    assert not (tmp_path / "pdp.jsonl").exists()
    pdp = _invariant(saver)["names"]["pdp"]
    assert pdp["saved"] == 0 and pdp["not_saved"] == {"stopped": 1}
    _request(saver, "S", "late")  # after the end: not counted at all
    assert _invariant(saver)["names"]["pdp"]["matched"] == 1
    saver._writer.shutdown()


def test_a_write_error_is_counted(tmp_path, monkeypatch):
    saver = _saver(tmp_path)
    _request(saver, "S", "r")

    def refuse(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(body_saver, "append_private_line", refuse)
    saver._write(saver.pending[("S", "r")], '{"a": 1}', False)
    pdp = _invariant(saver)["names"]["pdp"]
    assert pdp["not_saved"] == {"write_error": 1} and "disk full" in pdp["why_not_saved"][0]
    saver._writer.shutdown()


def test_the_status_never_shows_a_value_of_the_body(tmp_path):
    saver = _saver(tmp_path, mode="all", entries=body_saver.parse_entries(
        [{"url_pattern": "*/api/*", "name": "pdp"}]))
    _request(saver, "S", "r", url="https://x.test/api/1?access_token=SEEN")
    saver._write(saver.pending[("S", "r")], json.dumps({"token": "SECRETVALUE", "n": 1}), False)
    text = json.dumps(saver.report())
    assert "SECRETVALUE" not in text and "SEEN" not in text, text  # masked in the transcript
    assert "SEEN" in (tmp_path / "pdp.jsonl").read_text()  # the files were asked for unmasked
    saver._writer.shutdown()


# ── registry, status and notices ────────────────────────────────────────────

def test_status_before_any_run_says_never_started():
    status = body_saver.status("s-none", "main")
    assert status["state"] == "never started" and status["instance"] == "main"


def test_a_stop_by_itself_tells_the_next_result_once(tmp_path):
    saver = _saver(tmp_path)
    saver.session_key = "s-notice"
    saver._end("watchdog: test")
    body_saver._forget(saver, notice=True)
    first = body_saver.result_notice("s-notice", '{"a": 1}')
    assert "body saving stopped" in json.loads(first)["body_saving_stopped"]
    assert body_saver.result_notice("s-notice", "x") == "x"


def test_the_saver_releases_through_the_shared_routine(monkeypatch):
    from mcp_stealth_chrome import target_release
    calls = []

    def fake(ws_url, budget, progress):
        calls.append(ws_url)
        with progress["lock"]:
            progress["seen"], progress["finished"] = [], True
    monkeypatch.setattr(target_release, "release_targets", fake)
    assert body_saver._release_every_target("ws://x", 1.0) == ""
    assert calls == ["ws://x"]


def test_the_server_exit_ends_every_saver_once(monkeypatch, tmp_path):
    released = []
    monkeypatch.setattr(body_saver, "_release_every_target",
                        lambda ws_url, budget: released.append(ws_url) or "")
    saver = _saver(tmp_path)
    with body_saver._LOCK:
        body_saver._LIVE[saver.ws_url] = saver
    body_saver.shutdown()
    body_saver.shutdown()
    assert released == [saver.ws_url] and saver.ended() and "exiting" in saver.end_reason
    assert saver.ws_url not in body_saver._LIVE


def test_stopping_the_servers_browsers_ends_saving_too(monkeypatch):
    from mcp_stealth_chrome import server
    order = []
    monkeypatch.setattr(body_saver, "shutdown", lambda *a, **k: order.append("bodies"))
    monkeypatch.setattr(server.BrowserState, "session_keys",
                        classmethod(lambda cls: order.append("browsers") or []))
    server._stop_all_browsers()
    assert order == ["bodies", "browsers"], order


def test_a_closing_browser_stops_its_saving_first(monkeypatch):
    from mcp_stealth_chrome import server
    stopped = []

    async def stop_for(browser, reason):
        stopped.append(reason)
    monkeypatch.setattr(body_saver, "stop_for", stop_for)
    asyncio.run(server._ensure_browser_dead(None))
    assert stopped == ["its browser was closed"]


# ── the tool's own checks, before any browser ───────────────────────────────

def _call(name, **kwargs):
    from mcp_stealth_chrome.server import mcp
    return asyncio.run(mcp._tool_manager._tools[name].fn(**kwargs))


def test_network_start_refuses_a_bad_entry_and_starts_nothing():
    out = _call("network_start", save_bodies=[{"url_pattern": "*", "name": "x", "extract": ["a..b"]}])
    assert out.startswith("Error: network_start: save_bodies[0]: cannot read extract path") and \
        "nothing was started" in out, out


def test_reveal_values_without_save_bodies_is_refused():
    out = _call("network_start", reveal_values=True)
    assert out.startswith("Error") and "save_bodies" in out, out


def test_save_bodies_is_refused_in_the_persons_browser(monkeypatch):
    from mcp_stealth_chrome import server
    good = [{"url_pattern": "*/api/*", "name": "api"}]
    monkeypatch.setattr(server, "_persons_browser", lambda browser: True)
    out = _call("network_start", save_bodies=good)
    assert out.startswith("Error") and "launcher" in out and "browser_launch" in out, out
    monkeypatch.setattr(server, "_persons_browser", lambda browser: False)
    monkeypatch.setattr(server, "_ATTACHED_BROWSERS", {server.BrowserState.current_instance_id})
    out = _call("network_start", save_bodies=good)
    assert out.startswith("Error") and "attach_to_chrome" in out and "nothing was started" in out, out


def test_network_save_status_is_small_and_reads_no_page():
    from mcp_stealth_chrome import _app
    from mcp_stealth_chrome.server import mcp
    import inspect
    description = inspect.cleandoc(mcp._tool_manager._tools["network_save_status"].description)
    assert len(description.encode()) < 400
    assert "network_save_status" in _app._RUNS_DESPITE_DIALOG
    assert json.loads(_call("network_save_status"))["state"] in ("never started", "stopped", "saving")


def test_a_request_is_counted_once_whoever_settles_it(tmp_path):
    """A read that ends after the stop, or a load failure after a tab close: the first outcome
    counts and later ones change nothing."""
    saver = _saver(tmp_path)
    _request(saver, "S", "r")
    request = saver.pending[("S", "r")]
    assert saver.settle(request, "tab_closed")
    assert not saver.settle(request, "load_failed")
    assert not saver.settle(request, "saved", summary={"meta": {}, "body_keys": []})
    pdp = _invariant(saver)["names"]["pdp"]
    assert pdp["matched"] == 1 and pdp["saved"] == 0 and pdp["not_saved"] == {"tab_closed": 1}, pdp
    saver._writer.shutdown()


def test_the_watchdog_guards_the_start_itself(monkeypatch, tmp_path):
    """A start that hangs the saver thread is ended by the watchdog within the stall time, not
    after start()'s own 20 s, and the caller hears why in its answer, not in a later notice."""
    monkeypatch.setenv("MCP_STEALTH_BODIES_STALL", "1")

    async def hang(self):
        time.sleep(6)  # the saver thread itself stops answering
    monkeypatch.setattr(body_saver.Saver, "start", hang)
    monkeypatch.setattr(body_saver, "_release_every_target", lambda ws_url, budget: "")

    class Browser:
        websocket_url = "ws://127.0.0.1:9/devtools/browser/hung-start"

    entries = body_saver.parse_entries([{"url_pattern": "*", "name": "x"}])

    async def scenario():
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as caught:
            await body_saver.start(Browser(), instance_id="main", session_key="s-hang",
                                   entries=entries, mode="none", export_dir=tmp_path,
                                   max_body_bytes=100, buffer_bytes=100)
        return str(caught.value), time.monotonic() - t0

    why, took = asyncio.run(scenario())
    assert took < 5, f"start waited {took:.1f} s for a start the watchdog should have ended"
    assert "watchdog" in why, why
    assert body_saver.status("s-hang", "main")["state"] == "stopped"
    assert body_saver.result_notice("s-hang", "x") == "x"


def test_a_failed_start_leaves_other_notices_of_the_session(monkeypatch, tmp_path):
    """The caller of a start that failed hears it in its own answer; a notice already waiting
    for the session (another instance's saving that stopped) is still given."""
    with body_saver._LOCK:
        body_saver._NOTICES["s-other"] = ["body saving stopped at 10:00:00 in instance 'b' (watchdog)"]

    async def refuse(self):
        raise RuntimeError("Target.setAutoAttach: refused")
    monkeypatch.setattr(body_saver.Saver, "start", refuse)
    monkeypatch.setattr(body_saver, "_release_every_target", lambda ws_url, budget: "")

    class Browser:
        websocket_url = "ws://127.0.0.1:9/devtools/browser/refused"

    entries = body_saver.parse_entries([{"url_pattern": "*", "name": "x"}])
    with pytest.raises(RuntimeError, match="refused"):
        asyncio.run(body_saver.start(Browser(), instance_id="a", session_key="s-other",
                                     entries=entries, mode="none", export_dir=tmp_path,
                                     max_body_bytes=100, buffer_bytes=100))
    text = body_saver.result_notice("s-other", "x")
    assert "instance 'b'" in text and "instance 'a'" not in text, text


def test_starts_and_stops_at_the_same_time_leave_one_saver_reachable(monkeypatch, tmp_path):
    """Two network_start(save_bodies=...) in flight at once (FastMCP runs calls concurrently)
    while a run is live: one saver ends up live and reachable, every other one is stopped and
    counted as replaced, and save_bodies=[] then leaves none. Before the per-browser lock both
    calls awaited the old run's stop, then both registered, and the first saver stayed live
    where save_bodies=[], browser_close and shutdown() could not reach it."""
    made = []
    real_init = body_saver.Saver.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        made.append(self)

    async def connected(self):  # the stubbed connection: no Chrome, a moment to set up
        await asyncio.sleep(0.05)

    monkeypatch.setattr(body_saver.Saver, "__init__", init)
    monkeypatch.setattr(body_saver.Saver, "start", connected)
    monkeypatch.setattr(body_saver, "_release_every_target",
                        lambda ws_url, budget: time.sleep(0.05) or "")

    class Browser:
        websocket_url = "ws://127.0.0.1:9/devtools/browser/race"

    browser = Browser()
    entries = body_saver.parse_entries([{"url_pattern": "*/api/*", "name": "race"}])

    def begin():
        return body_saver.start(browser, instance_id="main", session_key="s-race", entries=entries,
                                mode="none", export_dir=tmp_path, max_body_bytes=100,
                                buffer_bytes=100)

    def live():
        return [s for s in made if not s.ended()]

    async def scenario():
        first = await begin()
        a, b = await asyncio.gather(begin(), begin())
        after_two = live()
        reachable = body_saver.saver_for(browser)
        # a stop sent while a start is under way ends the run that start leaves behind
        _c, stopped = await asyncio.gather(begin(), body_saver.stop_for(browser, "save_bodies=[]"))
        after_stop = live()
        return first, a, b, after_two, reachable, stopped, after_stop

    first, a, b, after_two, reachable, stopped, after_stop = asyncio.run(scenario())
    assert len(after_two) == 1 and after_two[0] is reachable, [s.end_reason for s in made]
    for saver in (first, a, b):
        if saver is not reachable:
            assert saver.ended() and saver.end_reason.startswith("replaced by a new network_start"), \
                saver.end_reason
    assert reachable.replaced is not None, "the live one does not say which run it replaced"
    assert stopped is not None and stopped["stop_reason"] == "save_bodies=[]", stopped
    assert after_stop == [] and body_saver.saver_for(browser) is None, \
        [(s.ended(), s.end_reason) for s in made]
    assert not any(s in body_saver._OPEN for s in made), "a stopped saver is still open"
    assert body_saver.status("s-race", "main")["state"] == "stopped"
    for saver in made:
        saver._writer.shutdown()


def test_the_server_exit_ends_a_saver_that_is_no_longer_live(monkeypatch, tmp_path):
    """shutdown() ends every saver registered and not yet forgotten, not only the one _LIVE
    names: one that was replaced but has not finished stopping, or one a future path left
    behind, still holds targets until it is ended."""
    released = []
    monkeypatch.setattr(body_saver, "_release_every_target",
                        lambda ws_url, budget: released.append(ws_url) or "")
    behind, current = _saver(tmp_path), _saver(tmp_path)
    behind.ws_url = current.ws_url = "ws://127.0.0.1:9/devtools/browser/exit-open"
    with body_saver._LOCK:
        body_saver._OPEN.update({behind, current})
        body_saver._LIVE[current.ws_url] = current
    body_saver.shutdown()
    assert behind.ended() and current.ended(), (behind.end_reason, current.end_reason)
    assert released == [current.ws_url] * 2
    assert behind not in body_saver._OPEN and current not in body_saver._OPEN


def test_a_saver_registered_without_the_lock_is_ended_not_left_behind(monkeypatch, tmp_path):
    """The second guard: a live saver that start() did not stop (one registered some other way)
    is ended when a new one takes its place, never left holding the browser unreachable."""
    async def connected(self):
        await asyncio.sleep(0.01)
    monkeypatch.setattr(body_saver.Saver, "start", connected)
    monkeypatch.setattr(body_saver, "_release_every_target", lambda ws_url, budget: "")
    monkeypatch.setattr(body_saver, "saver_for", lambda browser: None)  # start() sees no old run

    class Browser:
        websocket_url = "ws://127.0.0.1:9/devtools/browser/unlocked"

    stray = _saver(tmp_path)
    stray.ws_url = Browser.websocket_url
    with body_saver._LOCK:
        body_saver._LIVE[stray.ws_url] = stray
        body_saver._OPEN.add(stray)
    entries = body_saver.parse_entries([{"url_pattern": "*", "name": "x"}])
    new = asyncio.run(body_saver.start(Browser(), instance_id="main", session_key="s-unlocked",
                                       entries=entries, mode="none", export_dir=tmp_path,
                                       max_body_bytes=100, buffer_bytes=100))
    assert stray.ended() and "raced" in stray.end_reason, stray.end_reason
    assert not new.ended() and body_saver._LIVE[stray.ws_url] is new
    asyncio.run(new.stop("test over"))
