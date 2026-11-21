"""Pair CDP's page and wire events inside one target and one capture generation."""
from __future__ import annotations

from dataclasses import dataclass


PAGE_HEADERS_NOTE = "Wire request headers were not observed for this hop; showing page headers."


@dataclass(slots=True)
class _Chain:
    entry: dict | None = None
    expected: int = 1  # The current hop, plus redirects Chrome says had ExtraInfo.
    received: int = 0
    latest: dict | None = None
    finished: bool = False


class RequestHeaders:
    def __init__(self, limit: int):
        self._limit = max(0, limit)
        self._chains: dict[str, _Chain] = {}

    def _chain(self, rid: str) -> _Chain:
        chain = self._chains.get(rid)
        if chain is None:
            chain = self._chains[rid] = _Chain()
            # Includes ExtraInfo that arrives without a page request. Evict oldest first.
            if len(self._chains) > self._limit:
                del self._chains[next(iter(self._chains))]
        return chain

    def request(self, rid: str, entry: dict, previous: dict | None, redirected: bool,
                redirect_had_extra: bool) -> None:
        chain = self._chain(rid)
        if redirected:
            # Capture started mid-chain, or the earlier hop was evicted. Chrome reuses the
            # id; assigning the next ExtraInfo could put another origin's cookie here.
            if previous is None or chain.entry is not previous:
                self._chains.pop(rid, None)
                return
            chain.expected += int(redirect_had_extra)
        chain.entry = entry

    def extra(self, rid: str, headers: dict) -> None:
        chain = self._chain(rid)
        chain.received += 1
        # Only the last hop lives in network_index. Never queue all headers of a redirect chain.
        chain.latest = headers
        self._publish(rid, chain)

    def finish(self, rid: str, entry: dict, has_extra: bool) -> None:
        chain = self._chains.get(rid)
        if chain is None or chain.entry is not entry:
            return
        if not has_extra:
            del self._chains[rid]
            return
        chain.finished = True
        self._publish(rid, chain)

    def _publish(self, rid: str, chain: _Chain) -> None:
        # ExtraInfo may arrive before requestWillBeSent or after responseReceived. Until
        # the hop ends we cannot distinguish its headers from those of a later redirect.
        if (chain.finished and chain.entry is not None and chain.latest is not None
                and chain.received == chain.expected):
            # HTTP/2 pseudo-headers are method/URL metadata, not request header fields;
            # :path in particular can repeat a secret query outside the URL masker.
            chain.entry['request_headers'] = {k: v for k, v in chain.latest.items() if not k.startswith(':')}
            chain.entry['headers_source'] = 'wire'
            chain.entry.pop('headers_note', None)
            del self._chains[rid]
