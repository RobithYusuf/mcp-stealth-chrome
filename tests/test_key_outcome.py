"""key_outcome: what press_key and a click make of a torn read, without a browser."""
import asyncio
import time

import pytest

from mcp_stealth_chrome import key_outcome

PAGE = {"url": "http://x/keys.html", "len": 500, "hash": 1, "nodes": 9, "active": "INPUT"}
FIELD = {"len": 554, "nodes": 8, "connected": True, "focused": True, "value": 7}
BEFORE = {**PAGE, "element": FIELD}
# The read seen on macOS CI: page half from the old document, element half after the commit.
TORN = {**PAGE, "element": {}}
RESULT = {"url": "http://x/keys_result.html?q=monitor", "len": 27, "hash": 2, "nodes": 5,
          "active": "BODY", "element": {}}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(key_outcome, "POLL", 0)


def _watch(reads, settle_ms=1000):
    queue = list(reads)

    async def read():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return asyncio.run(key_outcome.watch(read, BEFORE, settle_ms))


def test_a_torn_read_waits_for_the_next_one_which_shows_the_navigation():
    effect, after = _watch([TORN, RESULT])
    assert effect == f"navigated to {RESULT['url']}"
    assert after is RESULT


def test_a_torn_read_alone_is_not_called_a_change_of_the_element():
    effect, _ = _watch([TORN], settle_ms=30)
    assert effect is not None and "could no longer be read" in effect
    assert "changed" not in effect


def test_a_real_change_of_the_element_is_still_reported_at_once():
    typed = {**PAGE, "element": {**FIELD, "value": 8}}
    assert _watch([typed, RESULT])[0] == "the focused element changed"


def test_a_removed_element_with_a_changed_page_is_a_page_change():
    removed = {**PAGE, "nodes": 8, "hash": 3, "element": {}}
    assert _watch([removed])[0].startswith("page changed")


def test_nothing_changed_is_none():
    assert _watch([BEFORE], settle_ms=20)[0] is None


def test_unreadable_page_is_not_a_torn_read():
    assert not key_outcome.element_unread(BEFORE, {})
    assert not key_outcome.element_unread({**PAGE, "element": {}}, TORN)  # never read before
    assert key_outcome.element_unread(BEFORE, TORN)


def test_a_torn_read_with_focus_back_on_body_still_waits_for_the_navigation():
    # A handler that blurs the field and then submits: old page, focus on <body>, no element.
    effect, _ = _watch([{**TORN, "active": "BODY"}, RESULT])
    assert effect == f"navigated to {RESULT['url']}"


def test_a_read_that_never_answers_ends_at_a_hard_limit(monkeypatch):
    monkeypatch.setattr(key_outcome, "READ_LIMIT", 0.2)

    async def hang():
        await asyncio.sleep(3600)

    async def scenario():
        started = time.monotonic()
        result = await asyncio.wait_for(key_outcome.watch(hang, BEFORE, 100), timeout=5)
        return result, time.monotonic() - started

    (effect, after), took = asyncio.run(scenario())
    assert effect is None and after == {}  # unknown, not "NO page change"
    assert took < 0.1 + 0.2 + 0.3, took


def test_no_change_still_takes_the_whole_wait(monkeypatch):
    monkeypatch.setattr(key_outcome, "POLL", 0.01)
    started = time.monotonic()
    assert _watch([BEFORE], settle_ms=150)[0] is None
    assert time.monotonic() - started >= 0.15


# ── The click path shares the rule ─────────────────────────────────────────

class _Tab:
    class target:
        target_id = "torn-test-tab"


def _after_click(monkeypatch, reads):
    from mcp_stealth_chrome import server
    queue = list(reads)
    seen = []

    async def page_sig(tab, el=None, pages=False):
        seen.append(None)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(server, "_page_sig", page_sig)
    after = asyncio.run(server._sig_after_action(_Tab(), BEFORE, el=object()))
    return after, len(seen)


def test_a_click_does_not_stop_on_a_torn_read(monkeypatch):
    from mcp_stealth_chrome import server
    after, _ = _after_click(monkeypatch, [TORN, TORN, RESULT])
    assert after["url"] == RESULT["url"]
    assert "navigated to" in server._click_outcome("clicked", "x", BEFORE, after)


def test_a_click_whose_reads_stay_torn_says_the_element_could_not_be_read(monkeypatch):
    from mcp_stealth_chrome import server
    after, reads = _after_click(monkeypatch, [TORN])
    assert reads == len(server._VERIFY_PAUSES)  # the schedule is not lengthened
    out = server._click_outcome("clicked", "x", BEFORE, after)
    assert "could no longer be read" in out and "page changed" not in out, out


def test_a_torn_settle_read_does_not_replace_the_change_already_seen(monkeypatch):
    from mcp_stealth_chrome import server
    spinner = {**PAGE, "nodes": 10, "hash": 4, "element": FIELD}
    after, _ = _after_click(monkeypatch, [spinner, TORN])
    assert after is spinner
