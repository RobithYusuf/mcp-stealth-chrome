"""Redirects reuse a CDP request id; a cookie must never move to a different hop."""
import pytest

from mcp_stealth_chrome.request_headers import RequestHeaders


@pytest.mark.parametrize("redirect_had_extra", [True, False])
@pytest.mark.parametrize("extra_first", [True, False])
def test_final_headers_match_with_cached_redirect_and_reordered_events(redirect_had_extra, extra_first):
    tracker = RequestHeaders(5)
    first, final = {}, {"request_headers": {"X-Not-Sent": "page only"}}
    if redirect_had_extra:
        tracker.extra("id", {"Cookie": "first-hop-only"})
    wire = {"Accept": "text/html", ":path": "/?ticket=sensitive"}
    if extra_first:
        tracker.extra("id", wire)
    tracker.request("id", first, None, False, False)
    tracker.request("id", final, first, True, redirect_had_extra)
    tracker.finish("id", final, True)
    if not extra_first:
        assert final["request_headers"] == {"X-Not-Sent": "page only"}
        tracker.extra("id", wire)
    assert final["request_headers"] == {"Accept": "text/html"}
    assert final["headers_source"] == "wire"


def test_late_extra_after_eviction_cannot_attach_to_redirect_destination():
    tracker = RequestHeaders(1)
    first, final = {}, {"request_headers": {"Accept": "page"}}
    tracker.request("id", first, None, False, False)
    tracker.request("other", {}, None, False, False)
    tracker.extra("id", {"Cookie": "first-hop-only"})
    tracker.request("id", final, first, True, True)
    tracker.finish("id", final, True)
    assert final["request_headers"] == {"Accept": "page"}


def test_orphan_wire_headers_are_forgotten_at_the_capture_limit():
    tracker = RequestHeaders(2)
    for rid in ("old", "second", "third"):
        tracker.extra(rid, {"X-Wire": rid})
    entry = {"headers_source": "page"}
    tracker.request("old", entry, None, False, False)
    tracker.finish("old", entry, True)
    assert entry == {"headers_source": "page"}
    tracker.extra("old", {"X-Wire": "fresh"})
    assert entry["request_headers"] == {"X-Wire": "fresh"}


def test_active_chains_survive_when_orphans_fill_the_limit():
    tracker = RequestHeaders(3)
    active = {}
    tracker.request("active-1", active, None, False, False)
    tracker.request("active-2", {}, None, False, False)
    tracker.extra("orphan-1", {"X-Wire": "one"})
    tracker.extra("orphan-2", {"X-Wire": "two"})
    tracker.extra("orphan-3", {"X-Wire": "three"})
    tracker.finish("active-1", active, True)
    tracker.extra("active-1", {"X-Wire": "active"})
    assert active["headers_source"] == "wire"
    assert active["request_headers"] == {"X-Wire": "active"}


def test_active_eviction_marks_header_loss_instead_of_page_silence():
    tracker = RequestHeaders(1)
    active = {"headers_source": "page", "headers_note": "old"}
    tracker.request("active", active, None, False, False)
    tracker.request("new", {}, None, False, False)
    assert active["headers_source"] == "page"
    assert "discarded because" in active["headers_note"]
