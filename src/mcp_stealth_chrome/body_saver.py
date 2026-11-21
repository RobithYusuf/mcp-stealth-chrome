"""network_start(save_bodies=...): response bodies written to files, never to the transcript.

The data an agent wants is often the JSON a page loads for itself, behind headers only the
page's own scripts can sign. Each response whose URL matches an entry is written as one JSON
line to EXPORT_DIR/<date>/<name>.jsonl, masked as network_get shows it, and the tools answer
with counts and paths only.

Saving runs on a CDP connection of its own per browser, on its own thread and event loop, as
block_ads does (adblock.py), and not on the tab's session: a popup that opens the API URL
itself has its body in its FIRST document response, which only a client holding the new
target until Network is set up can see; the tab's own capture (network_get, keep_bodies,
intercept_log, export_har) stays as it is; and bodies Chrome keeps belong to the session that
asked for them. `Target.setAutoAttach(waitForDebuggerOnStart, flatten)` at the browser level
(pages) and on every page and frame session (out-of-process iframes) holds each new tab, popup
and frame until `Network.enable` with buffer sizes and `Network.configureDurableMessages` are in
place, with a deadline, and then lets it run on every path. Nothing is paused (no Fetch domain)
and workers are not attached. Durable messages keep a body past its document, so a page that
navigates as its fetch resolves loses nothing (measured, Chrome 157).

Nothing here may hold the browser. A watchdog thread ends saving when the saver's loop misses
its heartbeat or a held target waits longer than MCP_STEALTH_BODIES_STALL (3 s), shuts the
socket and starts every target over a new connection (target_release, shared with block_ads).

Every matched response is accounted for, per name: saved, pending, or not saved with a reason
(body_gone, too_large, binary, undecodable, load_failed, tab_closed, stopped, read_error,
write_error). matched == saved + pending + every reason, always.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import concurrent.futures
import contextlib
import datetime
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import target_release as _target_release
from .helpers import (append_private_line, cdp_glob, confined_output_path, mask_body,
                      mask_json_value, mask_url)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_ENTRIES = 20
MAX_PATHS = 20
MAX_PATH_LEN = 300
# A new file once one would pass this: <name>.jsonl, then <name>.2.jsonl, <name>.3.jsonl.
ROTATE_BYTES = 50 * 2**20
MAX_FILES = 999
FINISH_BUDGET = 5.0
SHUTDOWN_BUDGET = 4.0
READ_TIMEOUT = 10.0
REASONS = ("body_gone", "too_large", "binary", "undecodable", "load_failed", "tab_closed",
           "stopped", "read_error", "write_error")
MODES = {"none": "masked", "body": "reveal_values=body", "all": "revealed"}
# Chrome's answers to getResponseBody for a body it no longer has.
_GONE = ("No resource with given identifier", "No data found for resource")
_HELD_TYPES = {"page", "iframe"}


def stall_after() -> float:
    try:
        return max(0.5, float(os.environ.get("MCP_STEALTH_BODIES_STALL", "3")))
    except ValueError:
        return 3.0


def _now_iso(epoch: Optional[float] = None) -> str:
    moment = datetime.datetime.fromtimestamp(epoch if epoch else time.time()).astimezone()
    return moment.isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════════════════
# WHAT TO SAVE: entries and extract paths
# ══════════════════════════════════════════════════════════════════════════

# A key: letters, digits and _ - @ $ : (as in "@type", "$ref", "og:title"). Not "$" or "@" alone,
# which is JSONPath's root, and nothing that reads as an expression ("*", "a()", "?(...)").
_KEY = r"[A-Za-z0-9_@$:-]+"
_SEGMENT = re.compile(rf"^({_KEY})(\[\])?$")
_KEYS = re.compile(rf"^\{{\s*{_KEY}(?:\s*,\s*{_KEY})*\s*\}}$")


def parse_path(text: Any) -> list[tuple]:
    """An extract path as segments, or ValueError naming what could not be read.

    "a.b.c" a key at each dot; "a[]" or "[]" each element of a list; "{x,y}" as the last part
    keeps only those keys. Nothing else: no expression language, nothing evaluated."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"an extract path must be a non-empty string, not {text!r}")
    raw = text.strip()
    if len(raw) > MAX_PATH_LEN:
        raise ValueError(f"extract path longer than {MAX_PATH_LEN} characters")
    parts = raw.split(".")
    segments: list[tuple] = []
    for index, part in enumerate(parts):
        part = part.strip()
        if part == "[]":
            segments.append(("each",))
            continue
        if part.startswith("{"):
            if index != len(parts) - 1 or not _KEYS.match(part):
                raise ValueError(f"cannot read extract path {raw!r} at {part!r}: {{x,y}} names keys "
                                 "and comes last only")
            names = [n.strip() for n in part[1:-1].split(",")]
            segments.append(("keys", list(dict.fromkeys(names))))
            continue
        match = _SEGMENT.match(part)
        if not match or match.group(1) in ("$", "@"):
            raise ValueError(f"cannot read extract path {raw!r} at {part!r}: a part is a key "
                             "(letters, digits, _ - @ $ :), a key followed by [], [] alone, or "
                             "{x,y} last; no JSONPath, start at the first key")
        segments.append(("key", match.group(1)))
        if match.group(2):
            segments.append(("each",))
    return segments


def _resolve(node: Any, segments: list[tuple]) -> tuple[Any, bool]:
    if not segments:
        return node, True
    head, rest = segments[0], segments[1:]
    if head[0] == "key":
        if isinstance(node, dict) and head[1] in node:
            return _resolve(node[head[1]], rest)
        return None, False
    if head[0] == "each":
        if not isinstance(node, list):
            return None, False
        results = [_resolve(item, rest) for item in node]
        if node and not any(found for _value, found in results):
            return None, False
        return [value for value, _found in results], True
    if not isinstance(node, dict) or not any(name in node for name in head[1]):
        return None, False
    return {name: node.get(name) for name in head[1]}, True


def extract(body: Any, paths: list[tuple[str, list[tuple]]]) -> tuple[dict, list[str]]:
    """{path as written: value} for each path, and the paths that found nothing (null)."""
    out, missing = {}, []
    for text, segments in paths:
        value, found = _resolve(body, segments)
        out[text] = value if found else None
        if not found:
            missing.append(text)
    return out, missing


class Entry:
    """One save_bodies entry: which responses, which file, which fields."""
    __slots__ = ("name", "pattern", "regex", "paths")

    def __init__(self, name: str, pattern: str, paths: list[tuple[str, list[tuple]]]):
        self.name, self.pattern, self.paths = name, pattern, paths
        self.regex = cdp_glob(pattern)


def parse_entries(value: Any) -> list[Entry]:
    """save_bodies as entries, or ValueError saying what is wrong with it. Called before
    anything starts, so a path that cannot be read is refused then, not at save time."""
    if not isinstance(value, list):
        raise ValueError("save_bodies must be a list of {url_pattern, name, extract} objects")
    if len(value) > MAX_ENTRIES:
        raise ValueError(f"save_bodies takes at most {MAX_ENTRIES} entries, not {len(value)}")
    entries: list[Entry] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        where = f"save_bodies[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be an object with url_pattern, name and extract")
        unknown = sorted(set(item) - {"url_pattern", "name", "extract"})
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}; the keys "
                             "are url_pattern, name and extract")
        pattern, name = item.get("url_pattern"), item.get("name")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"{where}: url_pattern must be a glob as intercept_start takes, "
                             "such as \"*/api/v4/pdp/*\"")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"{where}: name must be a file-safe word (a letter or digit, then up "
                             f"to 63 letters, digits, '_' or '-'), not {name!r}")
        if name.lower() in seen:
            raise ValueError(f"{where}: the name {name!r} is used twice")
        seen.add(name.lower())
        raw_paths = item.get("extract")
        if raw_paths is None:
            raw_paths = []
        if not isinstance(raw_paths, list):
            raise ValueError(f"{where}: extract must be a list of paths, such as "
                             "[\"data.item.title\", \"data.item.models[].{name,price}\"]")
        if len(raw_paths) > MAX_PATHS:
            raise ValueError(f"{where}: extract takes at most {MAX_PATHS} paths")
        paths = []
        for path in raw_paths:
            try:
                paths.append((path.strip(), parse_path(path)))
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from None
            except AttributeError:
                raise ValueError(f"{where}: an extract path must be a string, not {path!r}") from None
        entries.append(Entry(name, pattern.strip(), paths))
    return entries


# ══════════════════════════════════════════════════════════════════════════
# ONE RECORD
# ══════════════════════════════════════════════════════════════════════════

def make_record(meta: dict, body: Optional[str], base64_encoded: bool, mode: str,
                paths: list[tuple[str, list[tuple]]], max_bytes: int) -> tuple[Optional[bytes], Any]:
    """The line to write for one response, masked as network_get would show it, and a summary
    of it for the status; or (None, reason) when it is not saved.

    mode is helpers.reveal_mode's: "none" hides everything under a secret-named key, "body"
    only values whose own key is secret-named, "all" nothing. The whole body is masked before
    extract, so a path under a secret key still reads as hidden. URLs go through mask_url
    unless mode is "all"."""
    meta = dict(meta)
    if mode != "all":
        for key in ("request_url", "page_url"):
            meta[key] = mask_url(meta.get(key))
    raw = body or ""
    if base64_encoded:
        meta["base64"] = True
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None, "undecodable"
        if len(data) > max_bytes:
            return None, "too_large"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, "binary"
    else:
        text = raw
        if len(text) > max_bytes and len(text.encode("utf-8", "surrogatepass")) > max_bytes:
            return None, "too_large"
    try:
        parsed: Any = json.loads(text)
        is_json = True
    except ValueError:
        parsed, is_json = None, False
    if is_json:
        if mode != "all":
            parsed = mask_json_value(parsed, own_keys_only=(mode == "body"))
    else:
        meta["json"] = False
        if mode != "all":
            text = mask_body(text, own_keys_only=(mode == "body"))
    value = parsed if is_json else text
    if paths:
        value, missing = extract(parsed if is_json else None, paths)
        if missing:
            meta["missing"] = missing
    record = {"meta": meta, "body": value}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    return line.encode("utf-8", "surrogatepass"), {"meta": meta, "body_keys": _shape(value)}


def _shape(value: Any) -> Any:
    """The top-level keys of a body, with no values."""
    if isinstance(value, dict):
        keys = list(value)
        return keys[:20] + ([f"... {len(keys) - 20} more"] if len(keys) > 20 else [])
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if isinstance(value, str):
        return f"text, {len(value)} characters"
    return type(value).__name__


# ══════════════════════════════════════════════════════════════════════════
# THE SAVER THREAD AND ITS WATCHDOG
# ══════════════════════════════════════════════════════════════════════════

class _Runner:
    """The saver thread: one event loop for every saver's CDP connection, and a heartbeat the
    watchdog reads. A runner the watchdog gave up on is never used again."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.lock = threading.Lock()
        self.beat = time.monotonic()
        self.broken: Optional[str] = None
        started = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(started.set)
            self.loop.create_task(self._heartbeat())
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, name="mcp-stealth-bodies", daemon=True)
        self.thread.start()
        started.wait(5)

    async def _heartbeat(self) -> None:
        while True:
            with self.lock:
                self.beat = time.monotonic()
            await asyncio.sleep(0.2)

    def problem(self) -> Optional[str]:
        if not self.thread.is_alive():
            return "the body saving thread ended"
        if not self.loop.is_running():
            return "the body saving event loop stopped"
        with self.lock:
            silent = time.monotonic() - self.beat
        if silent > stall_after():
            return f"the body saving thread stopped answering for {silent:.1f} s"
        return None

    def usable(self) -> bool:
        with self.lock:
            broken = self.broken
        return broken is None and self.problem() is None


_RUNNER: Optional[_Runner] = None
_RUNNER_LOCK = threading.Lock()


def _runner() -> _Runner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None or not _RUNNER.usable():
            _RUNNER = _Runner()
        return _RUNNER


async def _on_runner(runner: _Runner, coro, timeout: float) -> Any:
    """Run coro on the saver thread and wait for it from another loop, bounded."""
    future = asyncio.run_coroutine_threadsafe(coro, runner.loop)
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout)


# Live savers by browser websocket URL (one per Chrome run), each session's runs by instance,
# and the notices tool results still have to give. All behind _LOCK.
_LOCK = threading.Lock()
_LIVE: dict[str, "Saver"] = {}
_RUNS: dict[tuple[str, str], dict] = {}
_NOTICES: dict[str, list[str]] = {}
_WATCHDOG: dict[str, Any] = {"thread": None}
# Every saver from its registration until _forget, whatever _LIVE says: what shutdown() ends.
_OPEN: set = set()
# One asyncio.Lock per browser (and event loop): start, replace and stop_for run one at a time
# for a browser. Two network_start(save_bodies=...) at once (FastMCP runs calls concurrently,
# --http sessions too) both awaited the old saver's stop and then both registered, and the
# first saver stayed live where nothing could reach or stop it.
_BROWSER_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _ensure_watchdog() -> None:
    with _LOCK:
        thread = _WATCHDOG["thread"]
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_watchdog_loop, name="mcp-stealth-bodies-watchdog",
                                  daemon=True)
        _WATCHDOG["thread"] = thread
    thread.start()


def _watchdog_loop() -> None:
    while True:
        time.sleep(0.25)
        with _LOCK:
            savers = list(_LIVE.values())
        for saver in savers:
            with contextlib.suppress(Exception):
                why = saver.stall_reason()
                if why:
                    saver.fail_open(f"watchdog: {why}")


def _release_every_target(ws_url: str, budget: float) -> str:
    """Start every target this connection may still hold, over a new one. Never raises."""
    return _target_release.release_every_target(ws_url, budget, thread_name="bodies-release")


def _browser_answers(ws_url: str, timeout: float) -> bool:
    return _target_release.browser_answers(ws_url, timeout)


# ══════════════════════════════════════════════════════════════════════════
# THE SAVER: one CDP connection per browser
# ══════════════════════════════════════════════════════════════════════════

class _Request:
    __slots__ = ("entry", "sid", "rid", "url", "page_url", "wall", "status", "mime", "bytes",
                 "reading")

    def __init__(self, entry: Entry, sid: str, rid: str, url: str, page_url: str,
                 wall: Optional[float]):
        self.entry, self.sid, self.rid, self.url, self.page_url = entry, sid, rid, url, page_url
        self.wall, self.status, self.mime, self.bytes, self.reading = wall, None, None, 0, False


class Saver:
    """Body saving for one browser. Sessions and the CDP plumbing are touched on the saver
    thread only; what other threads read (counts, pending, the end) is behind self._lock."""

    SETUP_DEADLINE = 2.0  # under the watchdog's stall_after(): a slow setup lets its target go first
    SEND_DEADLINE = 2.0

    def __init__(self, ws_url: str, instance_id: str, session_key: str, entries: list[Entry],
                 mode: str, folder: Path, max_body_bytes: int, buffer_bytes: int,
                 runner: Optional[_Runner] = None):
        self.ws_url, self.instance_id, self.session_key = ws_url, instance_id, session_key
        self.entries, self.mode, self.folder = entries, mode, folder
        self.max_body_bytes, self.buffer_bytes = max_body_bytes, buffer_bytes
        self.runner = runner
        self.ws: Any = None
        self._sock: Any = None
        self.started_at = time.time()
        # Until start() returns: an end then is told in network_start's own answer, not later.
        self.starting = True
        # Set by start(), under the browser's lock: the final report of the run this one
        # replaced, and where each name's records go.
        self.replaced: Optional[dict] = None
        self.plan: list[dict] = []
        # saver thread only
        self.sessions: dict[str, dict] = {}
        self._next_id = 0
        self._replies: dict[int, Callable[[dict], None]] = {}
        self._reader: Optional[asyncio.Task] = None
        self._setups: set = set()
        self._reads: set = set()
        self._writes: set = set()
        # One thread writes, in the order bodies came: parsing, masking and the file are kept
        # off the loop that answers Chrome.
        self._writer = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="mcp-stealth-bodies-write")
        # behind self._lock
        self._lock = threading.Lock()
        # Held while a line is checked, written and counted, and by _end before it counts what
        # is left as stopped: a body is never written and also counted as not saved. Always
        # taken before self._lock.
        self._write_gate = threading.Lock()
        self.stopping = False
        self.ended_at: Optional[float] = None
        self.end_reason: Optional[str] = None
        self.pending: dict[tuple[str, str], _Request] = {}
        self.counts = {e.name: {"matched": 0, "saved": 0, "not_saved": collections.Counter()}
                       for e in entries}
        self.samples: dict[str, collections.deque] = {e.name: collections.deque(maxlen=3)
                                                      for e in entries}
        self.last: dict[str, Any] = {}
        self.files: dict[str, dict] = {e.name: _file_state(folder, e.name) for e in entries}
        self.covered: collections.Counter = collections.Counter()
        self.uncovered: list[dict] = []
        self.durable_unconfirmed = 0
        self.waiting_targets: dict[str, float] = {}

    # ── state shared across threads ──────────────────────────────────────
    def ended(self) -> bool:
        with self._lock:
            return self.ended_at is not None

    def _end(self, reason: str) -> bool:
        """Mark it ended; True only for the call that did. What is still pending then was
        never saved, and is counted as stopped."""
        with self._write_gate, self._lock:
            if self.ended_at is not None:
                return False
            self.stopping = True
            self.ended_at, self.end_reason = time.time(), reason
            for request in list(self.pending.values()):
                self._settle_locked(request, "stopped")
            return True

    def _note(self, text: str) -> None:
        if text:
            with self._lock:
                self.end_reason = f"{self.end_reason}; {text}"

    def _settle_locked(self, request: _Request, outcome: str, sample: str = "",
                       summary: Any = None) -> bool:
        if self.pending.pop((request.sid, request.rid), None) is None:
            return False  # settled already: each request counts once
        counts = self.counts[request.entry.name]
        if outcome == "saved":
            counts["saved"] += 1
            self.last[request.entry.name] = summary
        else:
            counts["not_saved"][outcome] += 1
            if sample:
                self.samples[request.entry.name].append(f"{outcome}: {sample}"[:160])
        return True

    def settle(self, request: _Request, outcome: str, sample: str = "", summary: Any = None) -> bool:
        with self._lock:
            return self._settle_locked(request, outcome, sample, summary)

    def stall_reason(self) -> Optional[str]:
        """Why the watchdog should end this saver, or None."""
        if self.ended():
            return None
        if self.runner is not None:
            problem = self.runner.problem()
            if problem:
                return problem
        with self._lock:
            oldest = min(self.waiting_targets.values(), default=None)
        if oldest is not None and time.monotonic() - oldest > stall_after():
            return f"a new tab or frame waited {time.monotonic() - oldest:.1f} s to start"
        return None

    def fail_open(self, reason: str) -> None:
        """End saving from any thread but the saver's own: hand back what the connection holds
        when the saver thread still answers, shut the socket, then start every target over a
        new connection, since a target held for a client that goes away stays held while the
        server lives (measured with block_ads: a window.open popup and its opener froze)."""
        if not self._end(reason):
            return
        runner = self.runner
        if runner is not None and runner.problem() is not None:
            with runner.lock:
                runner.broken = reason  # the next start gets a new thread
        elif runner is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._release_all(), runner.loop).result(2.0)
        self._shut_socket()
        self._note(_release_every_target(self.ws_url, 5.0))
        _forget(self, notice=True)

    def _shut_socket(self) -> None:
        sock = self._sock
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)

    # ── CDP plumbing (saver thread) ──────────────────────────────────────
    async def _send(self, method: str, params: Optional[dict] = None, sid: Optional[str] = None,
                    wait: bool = True, timeout: float = 10.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if sid:
            message["sessionId"] = sid
        future: Optional[asyncio.Future] = None
        if wait:
            future = asyncio.get_running_loop().create_future()

            def settle(reply: dict) -> None:
                if not future.done():
                    future.set_result(reply)
            self._replies[msg_id] = settle
        await asyncio.wait_for(self.ws.send(json.dumps(message)), self.SEND_DEADLINE)
        if future is None:
            return {}
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._replies.pop(msg_id, None)

    async def start(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # websockets < 13
            from websockets import connect  # type: ignore[no-redef]
        self.ws = await asyncio.wait_for(
            connect(self.ws_url, max_size=None, ping_interval=None, open_timeout=10), 15)
        with contextlib.suppress(Exception):
            self._sock = self.ws.transport.get_extra_info("socket")
        self._reader = asyncio.ensure_future(self._read())
        reply = await self._send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
            "filter": [{"type": "page"}]})
        if "error" in reply:
            raise RuntimeError(f"Target.setAutoAttach: {reply['error'].get('message')}")
        # The tabs already open are attached as part of that call; set up before returning.
        deadline = time.monotonic() + 5
        while self._setups and time.monotonic() < deadline:
            await asyncio.wait(list(self._setups), timeout=max(0.0, deadline - time.monotonic()))

    async def _read(self) -> None:
        reason = "its connection to the browser ended"
        try:
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    callback = self._replies.pop(message["id"], None)
                    if callback is not None:
                        with contextlib.suppress(Exception):
                            callback(message)
                    continue
                with contextlib.suppress(Exception):
                    self._on_event(message.get("method"), message.get("params") or {},
                                   message.get("sessionId") or "")
        except Exception as exc:
            reason = f"its connection to the browser failed ({type(exc).__name__}: {exc})"
        finally:
            for callback in list(self._replies.values()):
                with contextlib.suppress(Exception):
                    callback({"error": {"message": "connection closed"}})
            self._replies.clear()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(), 2)
            if self._end(reason):
                alive = await asyncio.to_thread(_browser_answers, self.ws_url, 1.0)
                if alive:
                    # Still there: what this connection held for the debugger is still held.
                    self._note(await asyncio.to_thread(_release_every_target, self.ws_url, 5.0))
                else:
                    self._note("the browser is gone")
                _forget(self, notice=alive)

    def _on_event(self, method: Optional[str], params: dict, sid: str) -> None:
        if method == "Network.requestWillBeSent":
            self._on_request(params, sid)
        elif method == "Network.responseReceived":
            request = self._pending(sid, params)
            if request is not None:
                response = params.get("response") or {}
                request.status, request.mime = response.get("status"), response.get("mimeType")
                request.url = response.get("url") or request.url
        elif method == "Network.dataReceived":
            request = self._pending(sid, params)
            if request is not None:
                request.bytes += int(params.get("dataLength") or 0)
        elif method == "Network.loadingFinished":
            request = self._pending(sid, params)
            if request is not None and not request.reading:
                request.reading = True
                task = asyncio.ensure_future(self._fetch(request))
                self._reads.add(task)
                task.add_done_callback(self._reads.discard)
        elif method == "Network.loadingFailed":
            request = self._pending(sid, params)
            if request is not None:
                self.settle(request, "load_failed", str(params.get("errorText") or ""))
        elif method == "Target.attachedToTarget":
            task = asyncio.ensure_future(self._on_attached(params))
            self._setups.add(task)
            task.add_done_callback(self._setups.discard)
        elif method == "Target.detachedFromTarget":
            gone = params.get("sessionId") or ""
            self.sessions.pop(gone, None)
            with self._lock:
                self.waiting_targets.pop(gone, None)
                # A body being read is left to its read: it may still arrive, and an error
                # then says tab_closed.
                closed = [r for (s, _rid), r in self.pending.items() if s == gone and not r.reading]
                for request in closed:
                    self._settle_locked(request, "tab_closed")

    def _pending(self, sid: str, params: dict) -> Optional[_Request]:
        with self._lock:
            return self.pending.get((sid, str(params.get("requestId") or "")))

    def _on_request(self, params: dict, sid: str) -> None:
        if params.get("type") == "Preflight":
            return  # a CORS preflight has no body of its own
        rid = str(params.get("requestId") or "")
        url = str((params.get("request") or {}).get("url") or "")
        with self._lock:
            if self.stopping:
                return
            known = self.pending.get((sid, rid))
            if known is not None:  # a redirect: the same request, a new URL
                known.url = url
                return
        entry = next((e for e in self.entries if e.regex.match(url)), None)
        if entry is None:
            return
        request = _Request(entry, sid, rid, url, str(params.get("documentURL") or ""),
                           params.get("wallTime"))
        with self._lock:
            if self.stopping:
                return
            self.pending[(sid, rid)] = request
            self.counts[entry.name]["matched"] += 1

    async def _fetch(self, request: _Request) -> None:
        """Read one finished body, then hand it to the writer thread."""
        try:
            reply = await self._send("Network.getResponseBody", {"requestId": request.rid},
                                     request.sid, timeout=READ_TIMEOUT)
        except Exception as exc:
            self.settle(request, "stopped" if self.ended() else "read_error",
                        f"{type(exc).__name__}: {exc}")
            return
        if "error" in reply:
            message = str((reply.get("error") or {}).get("message") or "")
            if request.sid not in self.sessions:
                reason = "tab_closed"
            elif any(text in message for text in _GONE):
                reason = "too_large" if request.bytes > self.max_body_bytes else "body_gone"
            elif message == "connection closed":
                reason = "stopped"
            else:
                reason = "read_error"
            self.settle(request, reason, message)
            return
        result = reply.get("result") or {}
        future = self._writer.submit(self._write, request, result.get("body"),
                                     bool(result.get("base64Encoded")))
        wrapped = asyncio.wrap_future(future)
        self._writes.add(wrapped)
        wrapped.add_done_callback(self._writes.discard)

    def _write(self, request: _Request, body: Optional[str], b64: bool) -> None:
        """On the writer thread: the record, masked, then one appended line."""
        try:
            meta = {"request_url": request.url, "page_url": request.page_url,
                    "ts": _now_iso(request.wall), "status": request.status, "mime": request.mime}
            line, info = make_record(meta, body, b64, self.mode, request.entry.paths,
                                     self.max_body_bytes)
            if line is None:
                self.settle(request, info)
                return
            with self._write_gate:
                with self._lock:
                    if (request.sid, request.rid) not in self.pending:
                        return  # stopped meanwhile and counted so; nothing is written after
                self._append(request.entry.name, line)
                self.settle(request, "saved", summary=info)
        except OSError as exc:
            self.settle(request, "write_error", f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # never left pending
            self.settle(request, "write_error", f"{type(exc).__name__}: {exc}")

    def _append(self, name: str, line: bytes) -> None:
        state = self.files[name]
        while True:
            filename = _file_name(name, state["index"])
            if append_private_line(self.folder, filename, line, ROTATE_BYTES):
                with self._lock:
                    if filename not in state["names"]:
                        state["names"].append(filename)
                return
            if state["index"] >= MAX_FILES:
                raise OSError(f"{MAX_FILES} files of {name} are full")
            state["index"] += 1

    async def _on_attached(self, params: dict) -> None:
        """Set a new target up before it runs: Network with kept bodies, then its frames, then
        let it start. The setup has a deadline, and the release happens on every path."""
        sid = params.get("sessionId") or ""
        info = params.get("targetInfo") or {}
        kind, url = info.get("type") or "", info.get("url") or ""
        if params.get("waitingForDebugger"):
            with self._lock:
                self.waiting_targets[sid] = time.monotonic()
        ours = kind in _HELD_TYPES and not self.stopping
        try:
            if ours:
                await asyncio.wait_for(self._set_up(sid, info), self.SETUP_DEADLINE)
        except Exception as exc:
            with self._lock:
                self.uncovered.append({"type": kind, "url": mask_url(url)[:200],
                                       "error": f"{type(exc).__name__}: {exc}"[:200]})
        finally:
            with contextlib.suppress(Exception):
                # Every target, waiting or not: Chrome holds a window.open popup until each
                # attached client says go, whatever waitingForDebugger says (adblock.md).
                await self._send("Runtime.runIfWaitingForDebugger", {}, sid, wait=False)
                if not ours:
                    await self._send("Target.detachFromTarget", {"sessionId": sid}, wait=False)
            with self._lock:
                self.waiting_targets.pop(sid, None)

    async def _set_up(self, sid: str, info: dict) -> None:
        kind = info.get("type") or ""
        self.sessions[sid] = {"type": kind, "target": info.get("targetId")}
        sizes = {"maxTotalBufferSize": self.buffer_bytes, "maxResourceBufferSize": self.max_body_bytes}
        reply = await self._send("Network.enable", sizes, sid)
        if "error" in reply:
            raise RuntimeError(f"Network.enable: {(reply['error'] or {}).get('message')}")
        durable = await self._send("Network.configureDurableMessages", sizes, sid)
        if "error" in durable:
            # An older Chrome: the flag on enable asks for the same; it does not confirm it.
            await self._send("Network.enable", {**sizes, "enableDurableMessages": True}, sid)
            with self._lock:
                self.durable_unconfirmed += 1
        with self._lock:
            self.covered[kind] += 1
        # Out-of-process frames of this page or frame, held the same way. Workers are not.
        await self._send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
            "filter": [{"type": "iframe"}]}, sid)

    async def _release_all(self) -> None:
        """Hand everything back over the connection, best effort: no target waiting, no
        session; the socket is shut after this either way."""
        with self._lock:
            waiting = list(self.waiting_targets)
        for sid in waiting:
            with contextlib.suppress(Exception):
                await self._send("Runtime.runIfWaitingForDebugger", {}, sid, wait=False)
        with contextlib.suppress(Exception):
            await self._send("Target.setAutoAttach", {"autoAttach": False,
                                                      "waitForDebuggerOnStart": False}, wait=False)
        for sid in list(self.sessions):
            with contextlib.suppress(Exception):
                await self._send("Target.detachFromTarget", {"sessionId": sid}, wait=False)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(), 1)

    async def _finish(self, budget: float) -> None:
        """Before a stop: no new matches; bodies whose loading finished are read and written
        first, within the budget."""
        with self._lock:
            self.stopping = True
        ends = time.monotonic() + budget
        while time.monotonic() < ends and (self._reads or self._writes):
            await asyncio.wait(list(self._reads | self._writes),
                               timeout=max(0.0, ends - time.monotonic()))

    async def stop(self, reason: str) -> None:
        """End saving from the server's loop: finish what is finishing, hand back every target,
        close, then start every target over a new connection. Bounded."""
        runner = self.runner
        if runner is not None and not self.ended() and runner.problem() is None:
            with contextlib.suppress(Exception):
                await _on_runner(runner, self._finish(FINISH_BUDGET), FINISH_BUDGET + 1)
        if not self._end(reason):
            return
        if runner is not None and runner.problem() is None:
            with contextlib.suppress(Exception):
                await _on_runner(runner, self._release_all(), 3)
        self._shut_socket()
        self._note(await asyncio.to_thread(_release_every_target, self.ws_url, 5.0))
        _forget(self, notice=False)

    # ── reporting (any thread) ───────────────────────────────────────────
    def report(self) -> dict:
        with self._lock:
            names = {}
            for entry in self.entries:
                counts = self.counts[entry.name]
                pending = sum(1 for r in self.pending.values() if r.entry is entry)
                item: dict[str, Any] = {
                    "pattern": entry.pattern, "matched": counts["matched"], "saved": counts["saved"],
                    "pending": pending, "not_saved": dict(counts["not_saved"])}
                if entry.paths:
                    item["extract"] = len(entry.paths)
                if self.samples[entry.name]:
                    item["why_not_saved"] = list(self.samples[entry.name])
                item["files"] = [_file_info(self.folder / n) for n in self.files[entry.name]["names"]]
                if not item["files"]:
                    item["files"] = [{"path": str(self.folder / _file_name(entry.name, 1)),
                                      "bytes": None, "note": "nothing written yet"}]
                last = self.last.get(entry.name)
                if last is not None:
                    item["last"] = _status_summary(last)
                names[entry.name] = item
            out: dict[str, Any] = {
                "state": "saving" if self.ended_at is None else "stopped",
                "instance": self.instance_id,
                "since": _now_iso(self.started_at),
                "mode": MODES.get(self.mode, self.mode),
                "targets_covered": dict(self.covered),
            }
            if self.uncovered:
                out["targets_not_covered"] = list(self.uncovered[-5:])
            if self.durable_unconfirmed:
                out["kept_bodies_unconfirmed"] = self.durable_unconfirmed
            if self.ended_at is not None:
                out["stopped_at"] = _now_iso(self.ended_at)
                out["stop_reason"] = self.end_reason
            out["names"] = names
        return out

    def saved_total(self) -> int:
        with self._lock:
            return sum(c["saved"] for c in self.counts.values())


def _status_summary(last: dict) -> dict:
    """The last record for the transcript: meta with its URLs masked whatever the files' mode
    (tool output lands in the transcript), and the body's top-level keys, no values."""
    meta = dict(last.get("meta") or {})
    for key in ("request_url", "page_url"):
        if isinstance(meta.get(key), str):
            meta[key] = mask_url(meta[key])[:200]
    return {"meta": meta, "body_keys": last.get("body_keys")}


def _file_name(name: str, index: int) -> str:
    return f"{name}.jsonl" if index <= 1 else f"{name}.{index}.jsonl"


def _file_state(folder: Path, name: str) -> dict:
    """Where a name's records go: the last of its files in the folder (an earlier run's file is
    appended to, never truncated), and which of them this run has written."""
    index = 1
    with contextlib.suppress(OSError):
        pattern = re.compile(rf"^{re.escape(name)}\.(\d{{1,3}})\.jsonl$")
        for item in os.listdir(folder):
            match = pattern.match(item)
            if match:
                index = max(index, int(match.group(1)))
    return {"index": index, "names": []}


def _file_info(path: Path) -> dict:
    try:
        return {"path": str(path), "bytes": path.stat().st_size}
    except OSError as exc:
        return {"path": str(path), "bytes": None, "error": f"{type(exc).__name__}: {exc}"[:120]}


def file_plan(folder: Path, entries: list[Entry]) -> list[dict]:
    """For network_start's answer: each name's file and whether it exists already."""
    plan = []
    for entry in entries:
        state = _file_state(folder, entry.name)
        path = folder / _file_name(entry.name, state["index"])
        try:
            size: Optional[int] = path.stat().st_size
        except OSError:
            size = None
        plan.append({"entry": entry, "path": path, "bytes": size})
    return plan


# ══════════════════════════════════════════════════════════════════════════
# REGISTRY AND HOOKS
# ══════════════════════════════════════════════════════════════════════════

def _browser_lock(ws_url: str) -> asyncio.Lock:
    """The lock that serialises start, replace and stop for one browser. Made under _LOCK (a
    threading lock, never held across an await); a lock belongs to the loop it first waited
    on, so a new loop gets a new one."""
    loop = asyncio.get_running_loop()
    with _LOCK:
        held = _BROWSER_LOCKS.get(ws_url)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Lock())
            _BROWSER_LOCKS[ws_url] = held
        if len(_BROWSER_LOCKS) > 64:  # browsers come and go; keep the ones in use
            for url, (_loop, lock) in list(_BROWSER_LOCKS.items()):
                if url != ws_url and url not in _LIVE and not lock.locked():
                    _BROWSER_LOCKS.pop(url, None)
        return held[1]


def _forget(saver: Saver, notice: bool = False) -> None:
    saver._writer.shutdown(wait=False)
    report = saver.report()
    with _LOCK:
        _OPEN.discard(saver)
        if _LIVE.get(saver.ws_url) is saver:
            _LIVE.pop(saver.ws_url, None)
        run = _RUNS.get((saver.session_key, saver.instance_id))
        if run is not None and run.get("saver") is saver:
            run["final"] = report
    if notice and not saver.starting:
        with saver._lock:
            when = time.strftime("%H:%M:%S", time.localtime(saver.ended_at or time.time()))
            reason = saver.end_reason
        saved = sum(item["saved"] for item in report["names"].values())
        text = (f"body saving stopped at {when} in instance {saver.instance_id!r} ({reason}); "
                f"{saved} bodies were saved. Responses after that are not saved; "
                f"network_save_status() has the final counts, network_start(save_bodies=[...]) "
                f"starts it again.")
        with _LOCK:
            _NOTICES.setdefault(saver.session_key, []).append(text)


def result_notice(session_key: str, result: str) -> str:
    """result, with the notice of a body saving that stopped by itself (the watchdog, or its
    connection lost while the browser lives) since this session's last call, given once."""
    with _LOCK:
        pending = _NOTICES.pop(session_key, None)
    if not pending:
        return result
    text = " ".join(pending)
    stripped = result.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data["body_saving_stopped"] = text
                return json.dumps(data, indent=2, default=str)
        except ValueError:
            pass
    return f"{result}\n[{text}]"


def _ws_url(browser: Any) -> Optional[str]:
    with contextlib.suppress(Exception):
        url = browser.websocket_url
        return str(url) if url else None
    return None


def saver_for(browser: Any) -> Optional[Saver]:
    url = _ws_url(browser) if browser is not None else None
    if not url:
        return None
    with _LOCK:
        found = _LIVE.get(url)
    return found if found is not None and not found.ended() else None


def day_folder(export_dir: Path, started: Optional[float] = None) -> Path:
    """EXPORT_DIR/<YYYY-MM-DD>, the day the run starts, made 0700 and confined to export_dir."""
    day = time.strftime("%Y-%m-%d", time.localtime(started or time.time()))
    export_dir = Path(export_dir)
    export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = confined_output_path(export_dir, day)
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise OSError(f"{folder} is not a folder of its own")
    return folder


async def start(browser: Any, *, instance_id: str, session_key: str, entries: list[Entry],
                mode: str, export_dir: Path, max_body_bytes: int, buffer_bytes: int) -> Saver:
    """Start saving in this browser, ending a run already there first. Raises with the reason
    when it could not start; nothing is left holding the browser then. The new saver carries
    the final report of the run it replaced (`replaced`) and its files' plan (`plan`), both
    read under the browser's lock, so they are this call's and not another's."""
    ws_url = _ws_url(browser)
    if not ws_url:
        raise RuntimeError("the browser's debugging address is unknown")
    async with _browser_lock(ws_url):
        return await _start_locked(browser, ws_url, instance_id, session_key, entries, mode,
                                   export_dir, max_body_bytes, buffer_bytes)


async def _start_locked(browser: Any, ws_url: str, instance_id: str, session_key: str,
                        entries: list[Entry], mode: str, export_dir: Path, max_body_bytes: int,
                        buffer_bytes: int) -> Saver:
    old = saver_for(browser)
    replaced = None
    if old is not None:
        await old.stop("replaced by a new network_start(save_bodies=...)")
        replaced = old.report()
    folder = day_folder(export_dir)
    for entry in entries:
        confined_output_path(folder, _file_name(entry.name, 1))
    plan = file_plan(folder, entries)
    runner = _runner()
    saver = Saver(ws_url, instance_id, session_key, entries, mode, folder, max_body_bytes,
                  buffer_bytes, runner)
    saver.replaced, saver.plan = replaced, plan
    with _LOCK:
        other = _LIVE.get(ws_url)
        run = _RUNS.get((session_key, instance_id))
        previous = None
        if run is not None:
            previous = run.get("final") or (run["saver"].report() if run.get("saver") else None)
        _RUNS[(session_key, instance_id)] = {"saver": saver, "final": None, "previous": previous}
        # Watched from before it starts: a start that hangs the saver thread is ended within
        # the stall time.
        _LIVE[ws_url] = saver
        _OPEN.add(saver)
    if other is not None and other is not old and not other.ended():
        # Under the browser's lock nothing else registers here; one that did anyway is ended
        # rather than left holding the browser where nothing reaches it.
        await asyncio.to_thread(other.fail_open,
                                "replaced by a new network_start(save_bodies=...) it raced with")
    _ensure_watchdog()
    started = asyncio.ensure_future(_on_runner(runner, saver.start(), 20))
    while not started.done() and not saver.ended():
        await asyncio.wait({started}, timeout=0.25)
    if not started.done():
        started.cancel()
    try:
        if saver.ended():
            raise RuntimeError(saver.end_reason)
        started.result()
    except BaseException as exc:
        why = str(exc) if saver.ended() else f"{type(exc).__name__}: {exc}"
        saver.fail_open(f"could not start: {why}")
        raise RuntimeError(why) from None
    saver.starting = False
    return saver


async def stop_for(browser: Any, reason: str) -> Optional[dict]:
    """End this browser's saving, if any; its final report, or None when none was running.
    Waits for a start or stop of the same browser in progress. Never raises."""
    ws_url = _ws_url(browser) if browser is not None else None
    if not ws_url:
        return None
    async with _browser_lock(ws_url):
        saver = saver_for(browser)
        if saver is None:
            return None
        with contextlib.suppress(Exception):
            await saver.stop(reason)
        return saver.report()


def status(session_key: str, instance_id: str) -> dict:
    """network_save_status: the calling session's current instance's run, live or final."""
    with _LOCK:
        run = _RUNS.get((session_key, instance_id))
    if run is None:
        return {"state": "never started", "instance": instance_id,
                "note": "no network_start(save_bodies=[...]) in this instance"}
    saver = run.get("saver")
    out = run.get("final") or (saver.report() if saver is not None else {})
    if run.get("previous"):
        previous = run["previous"]
        out = dict(out)
        out["previous"] = {
            "since": previous.get("since"), "stopped_at": previous.get("stopped_at"),
            "stop_reason": previous.get("stop_reason"),
            "names": {n: {k: v for k, v in item.items() if k in ("matched", "saved", "pending", "not_saved", "files")}
                      for n, item in (previous.get("names") or {}).items()}}
    return out


def shutdown(reason: str = "the server is exiting") -> None:
    """End every saver before the server lets its browsers go: shut each connection and start
    every target it may hold, once, over a new connection. Side by side, bounded by
    SHUTDOWN_BUDGET in all; for the server's exit path, after its event loop is gone."""
    ends = time.monotonic() + SHUTDOWN_BUDGET
    # Every saver registered and not yet forgotten, live or being replaced: registration is
    # under _LOCK too, so none slips between this list and the ends below. (No event loop runs
    # at exit, so the browsers' asyncio locks cannot be taken here, and no start can resume.)
    with _LOCK:
        savers = list(_OPEN | set(_LIVE.values()))
    running = []
    for saver in savers:
        if saver._end(reason):
            saver._shut_socket()
            result: dict[str, str] = {}

            def release(s: Saver = saver, out: dict = result) -> None:
                out["left"] = _release_every_target(s.ws_url, max(0.5, SHUTDOWN_BUDGET - 1.0))

            worker = threading.Thread(target=release, name="bodies-exit-release", daemon=True)
            worker.start()
            running.append((saver, worker, result))
    for saver, worker, result in running:
        worker.join(max(0.0, ends - time.monotonic()))
        left = result.get("left")
        if left is None:
            left = f"the release did not finish within the server's {SHUTDOWN_BUDGET:g} s exit"
        saver._note(left)
        _forget(saver, notice=False)
