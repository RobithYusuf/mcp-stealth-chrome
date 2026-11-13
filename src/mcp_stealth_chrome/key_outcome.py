"""What a key press did, from page fingerprints read before and after it.

`server._page_sig` reads the page first and then the focused element's own tree, in two
CDP calls. A form that Enter submits can replace the document between them: the page half
still shows the old URL and the element half comes back empty, because the element's node
went with the old document. Read as a change of the focused element, that reported a form
submission as "the focused element changed" (macOS CI, now and then). Forced locally by
reading 1 ms after the key instead of 50 ms, it did in 37 of 100 presses: the page read
answered from keys.html and the element read, sent next, failed once the result page
committed. Chrome holds a read sent after the navigation started until the commit, which
is why a read 50 ms later on a fast machine always showed the new page.

So an element that cannot be read while the page half is unchanged is no answer yet. Its
node is gone, which a handler removing it would show in the page half too, so the document
was replaced and the next read shows where the page went.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

# Pause before each read. A test shortens it to land the first read on the commit.
POLL = 0.05
# How long a read may take once settle_ms has passed. The page half of _page_sig gives up
# after 5 s, the element half has no limit of its own, so watch() ends by settle_ms plus
# this at the latest and calls the effect unknown.
READ_LIMIT = 5.0

# Not `active`: a handler that blurs the field before it submits puts focus on <body>, and
# that read is just as torn. Clicks add `pages`, so a tab the click opened is not hidden.
_PAGE_FIELDS = ("url", "len", "hash", "nodes", "pages")


def key_effect(before: dict, after: dict) -> Optional[str]:
    """What changed between two fingerprints, in words. None when nothing did."""
    if not before or not after:
        return None
    if before.get("url") != after.get("url"):
        return f"navigated to {after.get('url')}"
    if (before.get("len") != after.get("len") or before.get("hash") != after.get("hash")
            or before.get("nodes") != after.get("nodes")):
        return f"page changed (nodes {before.get('nodes')}→{after.get('nodes')})"
    if before.get("active") != after.get("active"):
        return f"focus moved to <{(after.get('active') or '?').lower()}>"
    if before.get("element") != after.get("element"):
        return "the focused element changed"
    return None


def element_unread(before: dict, after: dict) -> bool:
    """The element half came back empty while the page half shows no change at all.

    Shared with the click path (`server._sig_after_action`), which reads the same way.
    """
    return (bool(before) and bool(after) and bool(before.get("element"))
            and after.get("element") == {}
            and all(before.get(f) == after.get(f) for f in _PAGE_FIELDS))


async def watch(read: Callable[[], Awaitable[dict]], before: dict,
                settle_ms: int) -> tuple[Optional[str], dict]:
    """Read until something changes or settle_ms passes: (effect in words or None, last read).

    A read whose element half is empty over an unchanged page is not reported until a later
    read explains it. If none does by the deadline, the effect says the element could not be
    read, rather than that it changed. A read still unanswered at settle_ms + READ_LIMIT ends
    the watch with ({} as the last read): the effect is unknown, not "no change".
    """
    deadline = time.monotonic() + max(0, settle_ms) / 1000
    hard_deadline = deadline + READ_LIMIT
    after: dict = {}
    while True:
        await asyncio.sleep(POLL)
        try:
            after = await asyncio.wait_for(read(), max(0.0, hard_deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return None, {}
        if not element_unread(before, after):
            effect = key_effect(before, after)
            if effect:
                return effect, after
        if time.monotonic() >= deadline:
            break
    if element_unread(before, after):
        return ("the focused element could no longer be read, while the page itself shows "
                "no change (it may be navigating)"), after
    return None, after
