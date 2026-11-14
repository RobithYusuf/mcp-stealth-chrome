"""block_ads: network ad and tracker blocking for one launch, with Brave's adblock-rust.

The filter lists (EasyList and EasyPrivacy) are GPL and CC BY-SA, so they are never shipped:
they are downloaded on first use into ~/.mcp-stealth/adblock (files 0600), checked again at
most once a day, and the last good copy is used offline. A launch with no list says ad
blocking is off and why, and one with only some of the lists says PARTIAL and which are
missing; it never fails over either. The engine built from the lists is kept serialized
next to them, so a launch loads it in milliseconds instead of parsing 109k rules.

Blocking runs on its own CDP connection to the browser endpoint, on its own thread and event
loop, which owns every piece of blocker state. `Target.setAutoAttach(waitForDebuggerOnStart)`
at the browser level, and again on every page, frame and worker session, attaches to every
tab, popup, out-of-process iframe and worker before its first request, and `Fetch.enable`
pauses each request until this module answers it, exactly once.

Holding a target until its setup ran is what makes out-of-process frames work at all: without
it their requests went unpaused (measured, see AGENTS.md). Chrome also holds a window.open popup
for every attached client until it says go, so every target is told to run after its setup.

Nothing here may hold the browser for long. A watchdog thread, independent of the adblock
thread and armed before the blocker starts, ends a blocker whose loop misses its heartbeat,
whose thread is gone, or whose paused request or held target waits too long. Every end of the
connection (the watchdog, an EOF or a transport error while the browser lives, the server's
exit) runs the same release once: Fetch.disable when the loop still answers, the socket shut,
then every target started over a new connection, because a target held for a client that goes
away stays held while the server lives. When the whole server dies (SIGKILL), Chrome lets
everything go by itself (measured). That is fail open: pages carry on unblocked, and the next
tool result says block_ads stopped and why.

intercept_start on a browser with block_ads does not enable a second Fetch domain: it
registers with the blocker, which then makes both decisions for a request and answers once.

Only requests are blocked. Cosmetic filtering would write into the page, which this server
never does. Brave blocks ads itself with Shields, so block_ads there changes nothing.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .helpers import ADBLOCK_MISSING, adblock_package_note, mask_url

# Lists in the order they are added to the engine. Both are published under GPLv3 or
# CC BY-SA 3.0 by the EasyList authors (https://easylist.to), which is why they are fetched
# at run time and never bundled.
LISTS: dict[str, str] = {
    "easylist": "https://easylist.to/easylist/easylist.txt",
    "easyprivacy": "https://easylist.to/easylist/easyprivacy.txt",
}
ENV_DIR = "MCP_STEALTH_ADBLOCK_DIR"
# name=url,name=url. Tests serve their own list over local http; nothing else should set it.
ENV_LIST_URLS = "MCP_STEALTH_ADBLOCK_LIST_URLS"
REFRESH_AFTER = 24 * 3600.0
# Bumped when the way the engine is built changes, so an old serialized engine is rebuilt.
ENGINE_SCHEMA = 1
_META = "lists.json"
_ENGINE_FILE = "engine.dat"
# A list name becomes a file name in the adblock folder, so it is a plain word, never a path.
_LIST_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# CDP resource types to the request types adblock-rust knows.
_REQUEST_TYPES = {
    "Document": "sub_frame", "Stylesheet": "stylesheet", "Image": "image", "Media": "media",
    "Font": "font", "Script": "script", "TextTrack": "other", "XHR": "xmlhttprequest",
    "Fetch": "xmlhttprequest", "EventSource": "xmlhttprequest", "WebSocket": "websocket",
    "Manifest": "web_manifest", "Ping": "ping", "CSPViolationReport": "csp_report",
    "Prefetch": "other", "Preflight": "other", "SignedExchange": "other", "Other": "other",
}
# Targets whose requests are looked at. "other", "browser_ui", "tab" and the like are
# Chrome's own pages and component extensions.
_FETCH_TYPES = {"page", "iframe", "worker", "shared_worker", "service_worker"}
_PAGE_TYPES = {"page", "iframe"}
_OWN_SCHEMES = ("chrome://", "chrome-extension://", "chrome-untrusted://", "devtools://",
                "chrome-search://", "edge://", "brave://")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def download_deadline() -> float:
    """Seconds one list download may take in all, connecting and reading included."""
    return _env_float("MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT", 20.0)


def max_list_bytes() -> int:
    return int(_env_float("MCP_STEALTH_ADBLOCK_MAX_BYTES", 16 * 1024 * 1024))


def max_engine_bytes() -> int:
    return int(_env_float("MCP_STEALTH_ADBLOCK_MAX_ENGINE_BYTES", 64 * 1024 * 1024))


def stall_after() -> float:
    """Seconds the watchdog lets the blocker go unanswered before it lets everything through."""
    return _env_float("MCP_STEALTH_ADBLOCK_STALL", 3.0)


def adblock_dir() -> Path:
    """The folder holding the lists and the engine, read at call time."""
    override = os.environ.get(ENV_DIR)
    if override:
        return Path(override).expanduser()
    from .state import data_root
    return data_root() / "adblock"


def _configured() -> list[tuple[str, str]]:
    raw = os.environ.get(ENV_LIST_URLS, "").strip()
    if not raw:
        return list(LISTS.items())
    pairs = []
    for part in raw.split(","):
        name, _, url = part.partition("=")
        if url.strip():
            pairs.append((name.strip(), url.strip()))
    return pairs or list(LISTS.items())


def list_urls() -> dict[str, str]:
    """The lists with a usable name."""
    return {n: u for n, u in _configured() if _LIST_NAME.match(n)}


def invalid_list_names() -> list[str]:
    return [n for n, _u in _configured() if not _LIST_NAME.match(n)]


def _list_path(folder: Path, name: str) -> Path:
    """The file of a list, which is always directly in the folder."""
    if not _LIST_NAME.match(name):
        raise ValueError(f"{name!r} is not a valid list name")
    path = folder / f"{name}.txt"
    if path.parent != folder:
        raise ValueError(f"{name!r} is not a valid list name")
    return path


def is_brave(executable: Optional[str]) -> bool:
    return "brave" in Path(str(executable or "")).name.lower() or "brave browser" in str(
        executable or "").lower()


# ══════════════════════════════════════════════════════════════════════════
# LISTS AND ENGINE
# ══════════════════════════════════════════════════════════════════════════

def _write_private(path: Path, data: bytes) -> None:
    """Replace path with data, readable by its owner only, never half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_meta(folder: Path) -> dict:
    try:
        data = json.loads((folder / _META).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(folder: Path, meta: dict) -> None:
    _write_private(folder / _META, json.dumps(meta, indent=1, sort_keys=True).encode("utf-8"))


def _valid_list(text: str) -> Optional[str]:
    """Why text is not a filter list, or None. A captive portal or an error page answers 200
    too, and storing it would replace the last good copy."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("[Adblock Plus"):
        return "the download is not an Adblock Plus filter list"
    if not any(not ln.startswith(("!", "[")) for ln in lines):
        return "the download holds no rules"
    return None


def _http_allowed() -> bool:
    return bool(os.environ.get(ENV_LIST_URLS))


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https, or to the scheme it came from in a test run: an https
    list redirected to http would otherwise be read over plain http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url).scheme, urlsplit(newurl).scheme
        if new != "https" and not (new == old == "http" and _http_allowed()):
            raise urllib.error.URLError(f"refused a redirect from {old} to {new}: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _set_read_timeout(response: Any, seconds: float) -> None:
    with contextlib.suppress(Exception):
        response.fp.raw._sock.settimeout(max(0.05, seconds))


def _download(name: str, url: str, entry: dict,
              deadline: Optional[float] = None) -> tuple[Optional[bytes], dict, str]:
    """(body or None, updated entry, what happened). Conditional, so an unchanged list costs
    one small response. Bounded in size and in total time, not only per socket read: a server
    sending one byte at a time would otherwise keep it going for ever."""
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and _http_allowed()):
        return None, entry, f"{name}: refused {scheme or 'a'} URL, lists are fetched over https only"
    from . import __version__
    limit = deadline if deadline is not None else download_deadline()
    cap = max_list_bytes()
    ends = time.monotonic() + limit
    request = urllib.request.Request(url, headers={"User-Agent": f"mcp-stealth-chrome/{__version__}"})
    if entry.get("etag"):
        request.add_header("If-None-Match", entry["etag"])
    if entry.get("last_modified"):
        request.add_header("If-Modified-Since", entry["last_modified"])
    now = time.time()
    entry = {**entry, "url": url, "checked_at": now}
    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    try:
        with opener.open(request, timeout=max(0.05, limit)) as response:
            final = urlsplit(response.geturl()).scheme
            if final != "https" and not (final == "http" and _http_allowed()):
                return None, entry, f"{name}: refused, the list was served over {final}"
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > cap:
                return None, entry, f"{name}: refused, {declared} bytes is larger than {cap}"
            chunks, total = [], 0
            while True:
                left = ends - time.monotonic()
                if left <= 0:
                    return None, entry, f"{name}: refused, not complete within its {limit:g} s deadline"
                # The socket's own timeout is per read; it follows what is left of the deadline,
                # or one read started near the end ran past it by up to that timeout.
                _set_read_timeout(response, left)
                try:
                    chunk = response.read1(65536)
                except TimeoutError:
                    return None, entry, f"{name}: refused, not complete within its {limit:g} s deadline"
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    return None, entry, f"{name}: refused, the body is larger than {cap} bytes"
                chunks.append(chunk)
            body = b"".join(chunks)
            headers = response.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, entry, f"{name}: unchanged"
        return None, entry, f"{name}: HTTP {exc.code}"
    except Exception as exc:
        return None, entry, f"{name}: {type(exc).__name__}: {exc}"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, entry, f"{name}: the download is not UTF-8 text"
    problem = _valid_list(text)
    if problem:
        return None, entry, f"{name}: {problem}"
    entry.update({
        "fetched_at": now, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified"),
    })
    return body, entry, f"{name}: downloaded {len(body)} bytes"


# _PREP_LOCK guards the folder's files and the engine cache, and is never held over the network.
# _DOWNLOAD_LOCK keeps one download pass at a time in this process.
_PREP_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_ENGINE_CACHE: dict[str, Any] = {"key": None, "engine": None}


def _engine_key(meta: dict, names: list[str]) -> str:
    import adblock
    parts = [f"schema={ENGINE_SCHEMA}", f"adblock={getattr(adblock, '__version__', '?')}"]
    parts += [f"{n}={meta.get(n, {}).get('sha256')}" for n in names]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _build_engine(folder: Path, names: list[str]):
    import adblock
    filters = adblock.FilterSet()
    rules = 0
    for name in names:
        text = _list_path(folder, name).read_text(encoding="utf-8")
        rules += sum(1 for ln in text.splitlines()
                     if ln.strip() and not ln.startswith(("!", "[")) and "##" not in ln
                     and "#@#" not in ln and "#?#" not in ln)
        # Network rules only: cosmetic rules hide elements, which means writing to the page.
        filters.add_filter_list(text, rule_types="networkonly")
    return adblock.Engine(filter_set=filters, optimize=True), rules


def refresh_lists(force: bool = False) -> list[str]:
    """Download the lists that are missing or were last checked more than a day ago."""
    folder = adblock_dir()
    notes = [f"{n!r} is not a valid list name, skipped" for n in invalid_list_names()]
    with _DOWNLOAD_LOCK:
        with _PREP_LOCK:
            meta = _read_meta(folder)
        todo = []
        for name, url in list_urls().items():
            entry = dict(meta.get(name) or {})
            present = _list_path(folder, name).is_file()
            if entry.get("url") not in (None, url):
                entry, present = {}, False  # another source: the old copy is not this list
            if present and not force and time.time() - float(entry.get("checked_at") or 0) < REFRESH_AFTER:
                continue
            todo.append((name, url, entry if present else {}))
        results = [(name, *_download(name, url, entry)) for name, url, entry in todo]
        if not results:
            return notes
        with _PREP_LOCK:
            meta = _read_meta(folder)
            for name, body, entry, note in results:
                if body is not None:
                    _write_private(_list_path(folder, name), body)
                meta[name] = entry
                notes.append(note)
            _write_meta(folder, meta)
    return notes


def load_engine(download: bool = True) -> tuple[Any, dict]:
    """(engine or None, info). info["reason"] says why there is no engine, info["coverage"]
    whether every list is in it. Blocking: run it on a thread."""
    info: dict[str, Any] = {"folder": str(adblock_dir())}
    try:
        import adblock  # noqa: F401
    except ImportError:
        info["reason"] = ADBLOCK_MISSING
        return None, info
    except Exception as exc:
        info["reason"] = f"the adblock package could not be imported ({type(exc).__name__}: {exc})"
        return None, info
    bad = invalid_list_names()
    if bad:
        info["reason"] = (f"{', '.join(repr(n) for n in bad)} is not a valid list name: letters, "
                          f"digits, - and _ only, since the name becomes a file in {adblock_dir()}")
        return None, info
    folder = adblock_dir()
    names = list(list_urls())
    missing = [n for n in names if not _list_path(folder, n).is_file()]
    stale = False
    if download and missing:
        info["download"] = refresh_lists()
    elif download:
        meta = _read_meta(folder)
        stale = any(time.time() - float((meta.get(n) or {}).get("checked_at") or 0) > REFRESH_AFTER
                    for n in names)
    if stale:
        # The copy on disk works now; a newer one is for the next launch.
        threading.Thread(target=_refresh_quietly, name="adblock-list-refresh", daemon=True).start()
        info["refresh"] = "started in the background; this launch uses the copy on disk"
    present = [n for n in names if _list_path(folder, n).is_file()]
    if not present:
        why = "; ".join(info.get("download") or []) or "no list has been downloaded yet"
        info["reason"] = f"no filter list is available ({why})"
        return None, info
    with _PREP_LOCK:
        meta = _read_meta(folder)
        key = _engine_key(meta, present)
        info["lists"] = {n: _list_info(meta.get(n) or {}) for n in present}
        missing = [n for n in names if n not in present]
        info["coverage"] = "partial" if missing else "full"
        if missing:
            info["missing_lists"] = missing
            failed = [d for d in info.get("download") or [] if d.split(":")[0] in missing]
            info["missing_why"] = "; ".join(failed) or "never downloaded"
        if _ENGINE_CACHE["key"] == key:
            info["engine"] = "in memory"
            info["rules"] = (meta.get("engine") or {}).get("rules")
            return _ENGINE_CACHE["engine"], info
        engine = _stored_engine(folder, meta.get("engine") or {}, key, info)
        if engine is None:
            t0 = time.perf_counter()
            try:
                engine, rules = _build_engine(folder, present)
            except Exception as exc:
                info["reason"] = f"the filter lists could not be read ({type(exc).__name__}: {exc})"
                return None, info
            info["engine"] = f"built in {(time.perf_counter() - t0) * 1000:.0f} ms"
            info["rules"] = rules
            with contextlib.suppress(Exception):
                data = engine.serialize()
                if len(data) <= max_engine_bytes():
                    _write_private(folder / _ENGINE_FILE, data)
                    meta["engine"] = {"key": key, "built_at": time.time(), "rules": rules,
                                      "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    _write_meta(folder, meta)
        _ENGINE_CACHE.update(key=key, engine=engine)
        return engine, info


def _stored_engine(folder: Path, stored: dict, key: str, info: dict) -> Any:
    """The serialized engine, only when it is the one these lists made, no larger than the cap,
    and exactly the bytes written: the native parser gets nothing else."""
    path = folder / _ENGINE_FILE
    if stored.get("key") != key or not path.is_file() or path.is_symlink():
        return None
    t0 = time.perf_counter()
    try:
        size = path.stat().st_size
        if size > max_engine_bytes():
            info["engine_reload"] = f"stored engine too large ({size} bytes), rebuilt"
            return None
        data = path.read_bytes()
        if len(data) != stored.get("bytes") or hashlib.sha256(data).hexdigest() != stored.get("sha256"):
            info["engine_reload"] = "stored engine does not match what was written, rebuilt"
            return None
        import adblock
        engine = adblock.Engine(adblock.FilterSet(), optimize=True)
        engine.deserialize(data)
    except Exception as exc:
        info["engine_reload"] = f"stored engine unusable ({type(exc).__name__}), rebuilt"
        return None
    info["engine"] = f"loaded from disk in {(time.perf_counter() - t0) * 1000:.0f} ms"
    info["rules"] = stored.get("rules")
    return engine


def _refresh_quietly() -> None:
    with contextlib.suppress(Exception):
        refresh_lists()


def _list_info(entry: dict) -> dict:
    fetched = entry.get("fetched_at")
    return {"fetched": time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched)) if fetched else None,
            "bytes": entry.get("bytes")}


# ══════════════════════════════════════════════════════════════════════════
# THE ADBLOCK THREAD AND ITS WATCHDOG
# ══════════════════════════════════════════════════════════════════════════

class _Runner:
    """The adblock thread: one event loop that owns every blocker's state, and a heartbeat the
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

        self.thread = threading.Thread(target=run, name="mcp-stealth-adblock", daemon=True)
        self.thread.start()
        started.wait(5)

    async def _heartbeat(self) -> None:
        while True:
            with self.lock:
                self.beat = time.monotonic()
            await asyncio.sleep(0.2)

    def silent_for(self) -> float:
        with self.lock:
            return time.monotonic() - self.beat

    def problem(self) -> Optional[str]:
        if not self.thread.is_alive():
            return "the adblock thread ended"
        if not self.loop.is_running():
            return "the adblock event loop stopped"
        silent = self.silent_for()
        if silent > stall_after():
            return f"the adblock thread stopped answering for {silent:.1f} s"
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
    """Run coro on the adblock thread and wait for it from another loop, bounded."""
    future = asyncio.run_coroutine_threadsafe(coro, runner.loop)
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout)


# Blockers by browser websocket URL (unique per Chrome run), the last ended ones, and the
# notices tool results still have to give, per MCP session. All behind _REGISTRY_LOCK: the
# adblock thread, the watchdog and the server's loop all touch them.
_REGISTRY_LOCK = threading.Lock()
_BLOCKERS: dict[str, "Blocker"] = {}
_ENDED: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_NOTICES: dict[str, list[str]] = {}
_WATCHDOG: dict[str, Any] = {"thread": None}


def _ensure_watchdog() -> None:
    with _REGISTRY_LOCK:
        thread = _WATCHDOG["thread"]
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_watchdog_loop, name="mcp-stealth-adblock-watchdog",
                                  daemon=True)
        _WATCHDOG["thread"] = thread
    thread.start()


def _watchdog_loop() -> None:
    while True:
        time.sleep(0.25)
        with _REGISTRY_LOCK:
            blockers = list(_BLOCKERS.values())
        for blocker in blockers:
            with contextlib.suppress(Exception):
                why = blocker.stall_reason()
                if why:
                    blocker.fail_open(f"watchdog: {why}")


# ══════════════════════════════════════════════════════════════════════════
# THE BLOCKER: one CDP connection per browser
# ══════════════════════════════════════════════════════════════════════════

# Chrome's answer to Fetch.continueRequest or failRequest for a request it no longer holds. Measured
# on Chrome 157 by answering a held request late: a fetch the page aborted, an iframe or image
# removed, and a navigation away all give exactly this, and the page had already settled (the
# fetch rejected with AbortError). Redirects and a target closed while a request waited gave none.
_GONE_BEFORE_ANSWER = "Invalid InterceptionId"


class _Session:
    __slots__ = ("sid", "target_id", "type", "url", "fetch", "root_frame")

    def __init__(self, sid: str, target_id: str, kind: str, url: str):
        self.sid, self.target_id, self.type, self.url = sid, target_id, kind, url
        self.fetch = False
        self.root_frame: Optional[str] = None


class Blocker:
    """Ad blocking for one browser. Everything but the fields behind self._lock is touched on
    the adblock thread only: sessions, frame_urls, the engine, the interceptor's calls."""

    # Shorter than the watchdog's stall_after(), so a slow setup releases its own target first.
    SETUP_DEADLINE = 2.0
    SEND_DEADLINE = 2.0

    def __init__(self, engine: Any, ws_url: str, instance_id: str, session_key: str, info: dict,
                 runner: Optional[_Runner] = None):
        self.engine = engine
        self.ws_url = ws_url
        self.instance_id = instance_id
        self.session_key = session_key
        self.info = info
        self.runner = runner
        self.ws: Any = None
        self._sock: Any = None
        self.started_at = time.time()
        # adblock thread only
        self.sessions: dict[str, _Session] = {}
        # frameId -> document URL, from the Document requests seen. The source of a request
        # is its frame's document, which decides $third-party and $domain= rules.
        self.frame_urls: "collections.OrderedDict[str, str]" = collections.OrderedDict()
        self.interceptor: Optional[tuple[str, Callable]] = None
        self._next_id = 0
        self._pending: dict[int, Callable[[dict], None]] = {}
        self._reader: Optional[asyncio.Task] = None
        self._setups: set = set()
        # behind self._lock: read by the server's loop and the watchdog
        self._lock = threading.Lock()
        self.ended_at: Optional[float] = None
        self.end_reason: Optional[str] = None
        self.counts = {"seen": 0, "blocked": 0, "allowed": 0, "answer_errors": 0,
                       "gone_before_answer": 0, "main_frame_mismatch": 0}
        self.blocked_types: collections.Counter = collections.Counter()
        self.blocked_hosts: collections.Counter = collections.Counter()
        self.recent: collections.deque = collections.deque(maxlen=20)
        self.covered: collections.Counter = collections.Counter()
        self.uncovered: list[dict] = []
        self.answer_error_samples: collections.deque = collections.deque(maxlen=5)
        self.waiting_pauses: dict[str, float] = {}
        self.waiting_targets: dict[str, float] = {}

    # ── state shared across threads ──────────────────────────────────────
    def ended(self) -> bool:
        with self._lock:
            return self.ended_at is not None

    def _end(self, reason: str) -> bool:
        """Mark it ended; True only for the call that did."""
        with self._lock:
            if self.ended_at is not None:
                return False
            self.ended_at, self.end_reason = time.time(), reason
            return True

    def stall_reason(self) -> Optional[str]:
        """Why the watchdog should end this blocker, or None."""
        if self.ended():
            return None
        if self.runner is not None:
            problem = self.runner.problem()
            if problem:
                return problem
        limit, now = stall_after(), time.monotonic()
        with self._lock:
            oldest_pause = min(self.waiting_pauses.values(), default=None)
            oldest_target = min(self.waiting_targets.values(), default=None)
        if oldest_pause is not None and now - oldest_pause > limit:
            return f"a paused request waited {now - oldest_pause:.1f} s for its answer"
        if oldest_target is not None and now - oldest_target > limit:
            return f"a new tab, frame or worker waited {now - oldest_target:.1f} s to start"
        return None

    def fail_open(self, reason: str) -> None:
        """Let everything through, from any thread but the adblock one: release what the
        connection holds when the adblock thread still answers, then shut the socket, after
        which Chrome continues every paused request and starts every held target."""
        if not self._end(reason):
            return
        runner = self.runner
        if runner is not None and runner.problem() is not None:
            with runner.lock:
                runner.broken = reason  # the next launch gets a new adblock thread
        elif runner is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._release_all(), runner.loop).result(2.0)
        self._shut_socket()
        # Chrome continues the paused requests of a client that goes away, but a target it held
        # for the debugger stays held while the server lives (measured: a window.open popup, and
        # with it its opener, stayed frozen). A fresh connection releases every target.
        self._note_release(_release_every_target(self.ws_url, 5.0))
        _forget(self, notice=True)

    def _note_release(self, left: str) -> None:
        if left:
            with self._lock:
                self.end_reason = f"{self.end_reason}; {left}"

    def _shut_socket(self) -> None:
        sock = self._sock
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)

    # ── decisions (adblock thread) ───────────────────────────────────────
    def _source_for(self, frame_id: Optional[str], session: Optional[_Session]) -> str:
        if frame_id and frame_id in self.frame_urls:
            return self.frame_urls[frame_id]
        return (session.url if session else "") or ""

    def decide(self, url: str, resource_type: str, frame_id: Optional[str],
               session: Optional[_Session] = None) -> Optional[str]:
        """The request type when url is to be blocked, else None. Never raises."""
        try:
            if not url.startswith(("http://", "https://")):
                return None
            top = (resource_type == "Document" and session is not None and session.type == "page"
                   and frame_id is not None and frame_id in (session.target_id, session.root_frame))
            if resource_type == "Document" and frame_id:
                self.frame_urls[frame_id] = url
                self.frame_urls.move_to_end(frame_id)
                while len(self.frame_urls) > 4000:
                    self.frame_urls.popitem(last=False)
            if top:
                return None  # what the caller navigates to is never blocked
            kind = _REQUEST_TYPES.get(resource_type or "Other", "other")
            source = (self._source_for(frame_id, session) if resource_type != "Document"
                      else self._parent_source(session))
            if not source.startswith(("http://", "https://")):
                source = url
            result = self.engine.check_network_urls(url, source, kind)
            return kind if result.matched else None
        except Exception:
            return None

    def _parent_source(self, session: Optional[_Session]) -> str:
        # A frame's own document request: its source is the document holding the frame.
        if session is None:
            return ""
        return (self.frame_urls.get(session.root_frame or "") or self.frame_urls.get(session.target_id)
                or session.url or "")

    def record(self, url: str, kind: Optional[str]) -> None:
        with self._lock:
            self.counts["seen"] += 1
            if kind is None:
                self.counts["allowed"] += 1
                return
            self.counts["blocked"] += 1
            self.blocked_types[kind] += 1
            self.blocked_hosts[urlsplit(url).hostname or "?"] += 1
            self.recent.append({"url": mask_url(url)[:300], "type": kind})

    # ── CDP plumbing (adblock thread) ────────────────────────────────────
    async def _send(self, method: str, params: Optional[dict] = None, sid: Optional[str] = None,
                    wait: bool = True, timeout: float = 10.0,
                    on_reply: Optional[Callable[[dict], None]] = None) -> dict:
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
            self._pending[msg_id] = settle
        else:
            self._pending[msg_id] = on_reply or (lambda _reply: None)
        await asyncio.wait_for(self.ws.send(json.dumps(message)), self.SEND_DEADLINE)
        if future is None:
            return {}
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    def _count_answer_error(self, reply: dict) -> None:
        if "error" in reply:
            message = str((reply["error"] or {}).get("message"))
            with self._lock:
                if message.startswith(_GONE_BEFORE_ANSWER):
                    # The page cancelled the request while it was paused; Chrome holds nothing.
                    self.counts["gone_before_answer"] += 1
                    return
                self.counts["answer_errors"] += 1
                self.answer_error_samples.append(message[:120])

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
            "filter": [{"type": "page"}, {"type": "service_worker"}, {"type": "shared_worker"}],
        })
        if "error" in reply:
            raise RuntimeError(f"Target.setAutoAttach: {reply['error'].get('message')}")
        # Targets that already exist are attached as part of that call; their Fetch.enable must
        # be in place before the caller navigates.
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
                    callback = self._pending.pop(message["id"], None)
                    if callback is not None:
                        with contextlib.suppress(Exception):
                            callback(message)
                    continue
                method = message.get("method")
                params = message.get("params") or {}
                sid = message.get("sessionId")
                if method == "Fetch.requestPaused":
                    await self._on_paused(params, sid)
                elif method == "Target.attachedToTarget":
                    task = asyncio.ensure_future(self._on_attached(params))
                    self._setups.add(task)
                    task.add_done_callback(self._setups.discard)
                elif method == "Target.detachedFromTarget":
                    self.sessions.pop(params.get("sessionId"), None)
                    with self._lock:
                        self.waiting_targets.pop(params.get("sessionId"), None)
        except Exception as exc:
            reason = f"its connection to the browser failed ({type(exc).__name__}: {exc})"
        finally:
            for callback in list(self._pending.values()):
                with contextlib.suppress(Exception):
                    callback({"error": {"message": "connection closed"}})
            self._pending.clear()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(), 2)  # Chrome lets what it holds continue
            if self._end(reason):
                # Fetch went with the connection, so nothing is blocked any more; that is news
                # only while the browser is still there, and then a target this connection held
                # for the debugger is still held, so it is released over a new one.
                alive = await asyncio.to_thread(_browser_answers, self.ws_url, 1.0)
                if alive:
                    self._note_release(await asyncio.to_thread(_release_every_target, self.ws_url, 5.0))
                _forget(self, notice=alive)

    async def _on_paused(self, params: dict, sid: Optional[str]) -> None:
        """Answer one paused request, exactly once, whatever happens while deciding. With
        intercept_start registered for this tab, its rule is asked here too: one decision,
        one answer, whoever wanted the request."""
        request_id = params.get("requestId") or ""
        with self._lock:
            self.waiting_pauses[request_id] = time.monotonic()
        method, extra = "Fetch.continueRequest", {}
        try:
            request = params.get("request") or {}
            url = request.get("url") or ""
            session = self.sessions.get(sid) if sid else None
            rtype = params.get("resourceType") or "Other"
            kind = self.decide(url, rtype, params.get("frameId"), session)
            self.record(url, kind)
            if kind is not None:
                method, extra = "Fetch.failRequest", {"errorReason": "BlockedByClient"}
            interceptor = self.interceptor
            if interceptor is not None and session is not None and session.target_id == interceptor[0]:
                chosen = interceptor[1]({
                    "url": url, "method": request.get("method") or "",
                    "headers": request.get("headers") or {}, "post_data": request.get("postData"),
                    "resource_type": rtype}, kind)
                if chosen is not None and kind is None:
                    method, extra = chosen
        except Exception:
            method, extra = "Fetch.continueRequest", {}
        finally:
            with contextlib.suppress(Exception):
                # Not waited on: the reader must keep reading. A failed send means the
                # connection is gone, and then Chrome continues the request itself.
                await self._send(method, {"requestId": request_id, **extra}, sid, wait=False,
                                 on_reply=self._count_answer_error)
            with self._lock:
                self.waiting_pauses.pop(request_id, None)

    async def _on_attached(self, params: dict) -> None:
        """Set a new target up before it runs: Fetch first, then its own children, then let
        it start. The setup has a deadline, and the release happens on every path."""
        sid = params.get("sessionId") or ""
        info = params.get("targetInfo") or {}
        kind, url = info.get("type") or "", info.get("url") or ""
        waiting = bool(params.get("waitingForDebugger"))
        if waiting:
            with self._lock:
                self.waiting_targets[sid] = time.monotonic()
        # A page or frame is attached whatever it shows now: the first tab reads chrome://newtab/
        # until the caller navigates it. A worker's URL is its script, which never changes.
        ours = kind in _FETCH_TYPES and (kind in _PAGE_TYPES or not url.startswith(_OWN_SCHEMES))
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
                # attached client says go, whatever waitingForDebugger says.
                await self._send("Runtime.runIfWaitingForDebugger", {}, sid, wait=False)
                if not ours:
                    await self._send("Target.detachFromTarget", {"sessionId": sid}, wait=False)
            with self._lock:
                self.waiting_targets.pop(sid, None)
            if ours and kind == "page":
                # After the release: a target held for the debugger answers no Page command.
                asyncio.ensure_future(self._read_root_frame(sid))

    async def _read_root_frame(self, sid: str) -> None:
        """The top-level document is told apart by its frame id. Chrome gives a page's main
        frame the page target's id; this reads it instead of assuming it, and counts any page
        where the two differ."""
        session = self.sessions.get(sid)
        with contextlib.suppress(Exception):
            tree = await self._send("Page.getFrameTree", {}, sid, timeout=5)
            root = (((tree.get("result") or {}).get("frameTree") or {}).get("frame") or {}).get("id")
            if root and session is not None:
                session.root_frame = root
                if root != session.target_id:
                    with self._lock:
                        self.counts["main_frame_mismatch"] += 1

    async def _set_up(self, sid: str, info: dict) -> None:
        kind, url = info.get("type") or "", info.get("url") or ""
        session = _Session(sid, info.get("targetId") or "", kind, url)
        self.sessions[sid] = session
        reply = await self._send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, sid)
        if "error" in reply:
            with self._lock:
                self.uncovered.append({"type": kind, "url": mask_url(url)[:200],
                                       "error": (reply["error"] or {}).get("message")})
        else:
            session.fetch = True
            with self._lock:
                self.covered[kind] += 1
        # Children of pages, frames and workers: frames, dedicated workers, nested workers.
        await self._send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, sid)

    async def _release_all(self) -> None:
        """Hand everything back over the connection, best effort: no Fetch domain, no target
        waiting, no session; the socket is shut after this either way."""
        for sid in list(self.sessions):
            with contextlib.suppress(Exception):
                await self._send("Fetch.disable", {}, sid, wait=False)
        with self._lock:
            waiting = list(self.waiting_targets)
        for sid in waiting:
            with contextlib.suppress(Exception):
                await self._send("Runtime.runIfWaitingForDebugger", {}, sid, wait=False)
        for sid in list(self.sessions):
            with contextlib.suppress(Exception):
                await self._send("Target.detachFromTarget", {"sessionId": sid}, wait=False)
        with contextlib.suppress(Exception):
            await self._send("Target.setAutoAttach", {"autoAttach": False,
                                                      "waitForDebuggerOnStart": False}, wait=False)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(), 1)

    async def stop(self, reason: str) -> None:
        self._end(reason)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(), 2)

    # ── intercept_start's broker (adblock thread) ────────────────────────
    async def _set_interceptor(self, target_id: Optional[str], handler: Optional[Callable]) -> None:
        self.interceptor = (target_id, handler) if handler is not None and target_id else None

    async def set_interceptor(self, target_id: Optional[str], handler: Optional[Callable]) -> bool:
        """Register (or with None, remove) intercept_start's rule for one tab. From the
        server's loop; False when the blocker no longer answers."""
        if self.ended() or self.runner is None:
            return False
        try:
            await _on_runner(self.runner, self._set_interceptor(target_id, handler), 2.0)
            return True
        except Exception:
            return False

    # ── reporting (any thread) ───────────────────────────────────────────
    def report(self) -> dict:
        with self._lock:
            counts = dict(self.counts)
            out = {
                "instance": self.instance_id,
                "active": self.ended_at is None,
                "coverage": self.info.get("coverage", "full"),
                "requests_seen": counts["seen"],
                "blocked": counts["blocked"],
                "allowed": counts["allowed"],
                "blocked_by_type": dict(self.blocked_types),
                "top_blocked_hosts": dict(self.blocked_hosts.most_common(10)),
                "recent_blocked": list(self.recent)[-10:],
                "targets_covered": dict(self.covered),
            }
            if counts["gone_before_answer"]:
                # Cancelled by the page before the answer arrived (aborted, its element or frame
                # removed, navigated away): nothing was left waiting.
                out["gone_before_answer"] = counts["gone_before_answer"]
            if counts["answer_errors"]:
                # Anything else Chrome refused: a real failure to answer.
                out["answer_errors"] = counts["answer_errors"]
                out["answer_error_samples"] = list(self.answer_error_samples)
            if counts["main_frame_mismatch"]:
                out["main_frame_mismatch"] = counts["main_frame_mismatch"]
            if self.uncovered:
                out["targets_not_covered"] = list(self.uncovered[-10:])
            if self.ended_at is not None:
                out["ended"] = self.end_reason
                out["ended_at"] = time.strftime("%H:%M:%S", time.localtime(self.ended_at))
        if self.info.get("missing_lists"):
            out["missing_lists"] = self.info["missing_lists"]
        out["lists"] = self.info.get("lists")
        out["rules"] = self.info.get("rules")
        return out


def _forget(blocker: Blocker, notice: bool = False) -> None:
    report = blocker.report()
    with _REGISTRY_LOCK:
        if _BLOCKERS.get(blocker.ws_url) is blocker:
            _BLOCKERS.pop(blocker.ws_url, None)
        _ENDED[blocker.ws_url] = report
        while len(_ENDED) > 10:
            _ENDED.popitem(last=False)
    if notice:
        _register_ended_for_notice(blocker, blocker.session_key)


def _register_ended_for_notice(blocker: Blocker, session_key: str) -> None:
    with blocker._lock:
        when = time.strftime("%H:%M:%S", time.localtime(blocker.ended_at or time.time()))
        reason = blocker.end_reason
    text = (f"block_ads stopped at {when} in instance {blocker.instance_id!r} ({reason}). "
            f"Ad requests are let through from then on; a new launch with block_ads=True "
            f"turns it on again.")
    with _REGISTRY_LOCK:
        _NOTICES.setdefault(session_key, []).append(text)


def result_notice(session_key: str, result: str) -> str:
    """result, with the notice of a block_ads that stopped since this session's last tool
    call, given once. A JSON object gets a "block_ads" key (or "block_ads_stopped" when it
    has one already), so it still parses."""
    with _REGISTRY_LOCK:
        pending = _NOTICES.pop(session_key, None)
    if not pending:
        return result
    text = " ".join(pending)
    stripped = result.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                key = "block_ads_stopped" if "block_ads" in data else "block_ads"
                data[key] = text
                return json.dumps(data, indent=2, default=str)
        except ValueError:
            pass
    return f"{result}\n[{text}]"


def _release_targets(ws_url: str, budget: float, progress: dict) -> None:
    """The release itself: attach, Runtime.runIfWaitingForDebugger (a no-op on a running
    target), detach, for every target, scanning again until a pass finds none it has not
    released, since a popup can open meanwhile. Writes what it saw and did into progress."""
    from websockets.sync.client import connect
    ends = time.monotonic() + budget
    next_id = [0]

    def call(ws, method: str, params: Optional[dict] = None, sid: Optional[str] = None) -> dict:
        next_id[0] += 1
        message: dict[str, Any] = {"id": next_id[0], "method": method, "params": params or {}}
        if sid:
            message["sessionId"] = sid
        ws.send(json.dumps(message))
        while True:
            left = ends - time.monotonic()
            if left <= 0:
                raise TimeoutError("release budget spent")
            reply = json.loads(ws.recv(timeout=left))
            if reply.get("id") == next_id[0]:
                return reply

    lock = progress["lock"]
    with connect(ws_url, max_size=None, open_timeout=max(0.05, min(3.0, budget)),
                 close_timeout=1) as ws:
        tried: set = set()
        while True:
            infos = (call(ws, "Target.getTargets").get("result") or {}).get("targetInfos") or []
            ids = [t.get("targetId") for t in infos if t.get("type") in _FETCH_TYPES]
            with lock:
                progress["seen"] = list(dict.fromkeys((progress["seen"] or []) + ids))
            new = [i for i in ids if i not in tried]
            if not new:
                break
            for target_id in new:
                tried.add(target_id)
                try:
                    attached = call(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
                    sid = (attached.get("result") or {}).get("sessionId")
                    if sid:
                        call(ws, "Runtime.runIfWaitingForDebugger", {}, sid)
                        call(ws, "Target.detachFromTarget", {"sessionId": sid})
                    with lock:
                        progress["released"].add(target_id)
                except TimeoutError:
                    raise
                except Exception:
                    pass
        with lock:
            progress["finished"] = True  # what is left is the close, which changes nothing


def _release_every_target(ws_url: str, budget: float) -> str:
    """Start every target that may still be held, over a new connection. Bounded as a whole:
    it runs on a thread of its own, so a connect, send, receive or close that never returns
    cannot keep the caller past the budget. Never raises. Returns "" when every target it saw
    was released, or how many and which were not, or that it could not tell which."""
    progress: dict[str, Any] = {"lock": threading.Lock(), "seen": None, "released": set(),
                                "finished": False, "error": None}

    def run() -> None:
        try:
            _release_targets(ws_url, budget, progress)
        except Exception as exc:
            with progress["lock"]:
                progress["error"] = f"{type(exc).__name__}: {exc}"[:120]

    worker = threading.Thread(target=run, name="adblock-release", daemon=True)
    worker.start()
    worker.join(budget + 0.5)
    with progress["lock"]:
        seen, released = progress["seen"], set(progress["released"])
        finished, error = progress["finished"], progress["error"]
    left = [i for i in (seen or []) if i not in released]
    if finished and not left:
        return ""
    why = error or (f"not finished within {budget:g} s" if worker.is_alive() else "stopped")
    if seen is None:
        return f"could not release held targets ({why}); which are left is unknown, no target list was read"
    shown = ", ".join(left[:10]) + (f" and {len(left) - 10} more" if len(left) > 10 else "")
    return f"could not release every held target ({why}): {len(left)} target(s) not released: {shown}"


def _browser_answers(ws_url: str, timeout: float) -> bool:
    """Whether the browser behind ws_url still answers on its debugging port. Blocking."""
    parts = urlsplit(ws_url)
    try:
        with urllib.request.urlopen(f"http://{parts.netloc}/json/version", timeout=timeout) as reply:
            return reply.status == 200
    except Exception:
        return False


def _ws_url(browser: Any) -> Optional[str]:
    with contextlib.suppress(Exception):
        return str(browser.websocket_url)
    return None


def blocker_for(browser: Any) -> Optional[Blocker]:
    url = _ws_url(browser) if browser is not None else None
    if not url:
        return None
    with _REGISTRY_LOCK:
        found = _BLOCKERS.get(url)
    return found if found is not None and not found.ended() else None


# ══════════════════════════════════════════════════════════════════════════
# LAUNCH HOOKS
# ══════════════════════════════════════════════════════════════════════════

class Pending:
    """What begin() started for one launch: the engine on its way, or why there is none."""

    def __init__(self, task: Optional[asyncio.Task], note: str = ""):
        self.task, self.note = task, note


def begin(block_ads: bool, executable: Optional[str]) -> Optional[Pending]:
    """Called before Chrome starts, so the lists load while it does. None when not asked."""
    if not block_ads:
        return None
    if is_brave(executable):
        return Pending(None, " [block_ads: nothing done, Brave blocks ads itself with Shields; "
                             "its own setting decides what is blocked]")
    return Pending(asyncio.ensure_future(asyncio.to_thread(load_engine)))


def _launch_note(info: dict, uncovered: int) -> str:
    lists = ", ".join(f"{name} {(entry or {}).get('fetched') or '?'}"
                      for name, entry in (info.get("lists") or {}).items())
    if info.get("coverage") == "partial":
        note = (f" [block_ads: PARTIAL, only {lists}: {', '.join(info['missing_lists'])} not "
                f"available ({info.get('missing_why')}), so what it lists is not blocked; "
                f"{info.get('rules') or '?'} rules")
    else:
        note = f" [block_ads: on, {info.get('rules') or '?'} rules ({lists})"
    if info.get("refresh"):
        note += "; lists are a day old, a refresh runs in the background"
    if uncovered:
        note += f"; {uncovered} target(s) not covered, see server_status"
    return note + "; counts in server_status]"


async def attach(pending: Optional[Pending], browser: Any, instance_id: str,
                 session_key: str) -> str:
    """Start blocking on a running browser. Returns a note for the launch result; never raises."""
    if pending is None:
        return ""
    if pending.task is None:
        return pending.note
    try:
        # The lists download one after the other on first use, each within its deadline.
        limit = download_deadline() * max(1, len(list_urls())) + 15
        engine, info = await asyncio.wait_for(asyncio.shield(pending.task), limit)
    except asyncio.TimeoutError:
        return " [block_ads: OFF, the filter lists were still loading; the next launch tries again]"
    except Exception as exc:
        return f" [block_ads: OFF, the filter lists could not be loaded ({type(exc).__name__}: {exc})]"
    if engine is None:
        return f" [block_ads: OFF, {info.get('reason')}]"
    ws_url = _ws_url(browser)
    if not ws_url:
        return " [block_ads: OFF, the browser's debugging address is unknown]"
    runner = _runner()
    blocker = Blocker(engine, ws_url, instance_id, session_key, info, runner)
    # Watched from before it starts: a start that hangs the adblock thread is ended within the
    # stall time like anything later, not after this call's own limit.
    with _REGISTRY_LOCK:
        _BLOCKERS[ws_url] = blocker
    _ensure_watchdog()
    started = asyncio.ensure_future(_on_runner(runner, blocker.start(), 20))
    while not started.done() and not blocker.ended():
        await asyncio.wait({started}, timeout=0.25)
    if not started.done():
        started.cancel()
    try:
        if blocker.ended():
            raise RuntimeError(blocker.end_reason)
        started.result()
    except Exception as exc:
        why = str(exc) if blocker.ended() else f"blocking could not start ({type(exc).__name__}: {exc})"
        blocker.fail_open(why)
        return f" [block_ads: OFF, {why}]"
    with blocker._lock:
        uncovered = len(blocker.uncovered)
    return _launch_note(info, uncovered)


# The whole exit path of block_ads, every blocker included: the server's exit never waits longer.
SHUTDOWN_BUDGET = 4.0


def shutdown(reason: str = "the server is exiting") -> None:
    """End every blocker before the server lets its browsers go (stdin EOF): shut each
    connection and start every target it may hold, once, over a new connection. The releases
    run side by side and the whole call is bounded by SHUTDOWN_BUDGET; for the server's exit
    path, after its event loop is gone."""
    ends = time.monotonic() + SHUTDOWN_BUDGET
    with _REGISTRY_LOCK:
        blockers = list(_BLOCKERS.values())
    running = []
    for blocker in blockers:
        if blocker._end(reason):
            blocker._shut_socket()
            result: dict[str, str] = {}

            def release(b: Blocker = blocker, out: dict = result) -> None:
                out["left"] = _release_every_target(b.ws_url, max(0.5, SHUTDOWN_BUDGET - 1.0))

            worker = threading.Thread(target=release, name="adblock-exit-release", daemon=True)
            worker.start()
            running.append((blocker, worker, result))
    for blocker, worker, result in running:
        worker.join(max(0.0, ends - time.monotonic()))
        left = result.get("left")
        if left is None:
            left = f"the release did not finish within the server's {SHUTDOWN_BUDGET:g} s exit"
        blocker._note_release(left)
        _forget(blocker, notice=True)


def reuse_note(block_ads: bool, browser: Any) -> str:
    """For a launch that reused a running browser."""
    if not block_ads or blocker_for(browser) is not None:
        return ""
    return "; block_ads NOT applied, it only takes effect on a new launch"


def status(browsers: Optional[list] = None) -> dict:
    """What server_status shows: the live blockers of these browsers (the caller's), and the
    last ended ones."""
    with _REGISTRY_LOCK:
        everyone = list(_BLOCKERS.values())
        ended = list(_ENDED.values())[-3:]
    if browsers is None:
        live = [b.report() for b in everyone if not b.ended()]
    else:
        found = [blocker_for(browser) for browser in browsers if browser is not None]
        live = [b.report() for b in found if b is not None]
    out: dict[str, Any] = {"browsers": live, "package": adblock_package_note(),
                           "folder": str(adblock_dir())}
    meta = _read_meta(adblock_dir())
    lists = {n: _list_info(meta[n]) for n in list_urls() if isinstance(meta.get(n), dict)}
    out["lists"] = lists or "none downloaded yet; the first block_ads=True launch downloads them"
    if invalid_list_names():
        out["invalid_list_names"] = invalid_list_names()
    if ended:
        out["ended"] = ended
    return out
