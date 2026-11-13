"""Section 22 — Dual-mode HTTP (curl_cffi TLS-perfect) + behavioral helpers.

UNIQUE in MCP ecosystem — combine browser (for login/rendering) with
curl_cffi (for high-volume API scraping with real browser JA3/JA4 fingerprint).

Tools:
- http_request: TLS-perfect HTTP via curl_cffi, tracked and pollable
- http_request_status: poll / list a tracked request
- http_request_cancel: stop waiting on one (client-side only — never a rollback)
- http_request_retry: explicit, opt-in replay; nothing here retries on its own
- http_session_cookies: inspect which browser cookies a request would send
- session_warmup: visit homepage / referer chain / natural-browse before target
- detect_anti_bot: classify Cloudflare / DataDome / PerimeterX / Akamai / etc.

Internal helpers re-exported by server.py for backward compat:
- _get_browser_cookies_for_url

Concurrency
-----------
Every request gets its OWN curl_cffi AsyncSession, so N http_request calls run
in parallel without interfering — nothing in the transfer path shares a browser
tab or the single CDP connection. That is the opposite of the browser tools,
which all drive one tab and must not be fanned out silently. The one place that
DOES touch the browser is the cookie snapshot, and it is taken once, up front,
under a lock — see _snapshot_browser_cookies.

Honest outcomes
---------------
A failed HTTP call is not one thing. `connect_timeout` and `dns_error` mean the
server never saw the request, so replaying it cannot duplicate a side effect.
`read_timeout` means the request WAS delivered and the reply never came back:
the outcome is genuinely unknown, and this module says `unknown` rather than
guessing. Cancelling only stops the client from waiting; no rollback is
performed or claimed.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import urljoin, urlparse

from .._app import mcp
from urllib.parse import urlsplit

from ..helpers import err, get_url, mask_body, mask_headers, mask_json_value, mask_url, ok, parse_json
from ..humanize import humanized_move
from ..state import SESSION_GONE_HOOKS, BrowserState

# ── Bounds ─────────────────────────────────────────────────────────────────
# Tracked requests outlive the tool call that started them, so every one of
# these is a ceiling on memory or on how long a forgotten request may run.
HTTP_TRACK_MAX = int(os.environ.get("HTTP_TRACK_MAX", "50"))        # retained records
HTTP_TRACK_TTL = float(os.environ.get("HTTP_TRACK_TTL", "900"))     # settled-record age
HTTP_MAX_INFLIGHT = int(os.environ.get("HTTP_MAX_INFLIGHT", "16"))  # concurrent tracked
HTTP_BODY_MAX = int(os.environ.get("HTTP_BODY_MAX", "20000"))       # retained body chars
HTTP_MAX_TIMEOUT = float(os.environ.get("HTTP_MAX_TIMEOUT", "900"))  # per-request cap
HTTP_DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_DEADLINE_GRACE = float(os.environ.get("HTTP_DEADLINE_GRACE", "10"))
HTTP_COOKIE_TIMEOUT = float(os.environ.get("HTTP_COOKIE_TIMEOUT", "5"))

# Retrying these cannot duplicate a side effect even when the first attempt's
# outcome is unknown. Everything else needs the caller to say so explicitly.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# libcurl error codes, split by what they prove about the SERVER, not about us.
# not-executed: the bytes never left for a server that could act on them.
_CURL_NOT_EXECUTED = {1, 3, 5, 6, 7, 8, 9, 35, 53, 54, 58, 59, 60, 66, 77, 83,
                      90, 91, 97, 98, 101}
# unknown: the connection was up and the request may well have been processed.
_CURL_UNKNOWN = {18, 23, 26, 27, 47, 52, 55, 56, 92, 95, 96}


# ── Local / private targets take a direct transport ────────────────────────

_DIRECT_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".home.arpa")


def _is_direct_target(url: str) -> tuple[bool, str]:
    """True when `url` points at loopback / a private network / a local name.

    Those must not be proxied. libcurl reads http_proxy / HTTPS_PROXY / ALL_PROXY
    straight out of the environment whenever CURLOPT_PROXY is unset, so a
    localhost call made from a shell that exports a corporate proxy is dialled
    at the proxy and dies on a connect timeout with nothing to show for it —
    exactly the "three parallel localhost requests timed out, outcome unknown"
    report this came from.
    """
    try:
        host = (urlparse(url).hostname or "").strip("[]")
    except Exception:
        return False, ""
    if not host:
        return False, ""
    low = host.lower()
    if low == "localhost" or low.endswith(_DIRECT_SUFFIXES):
        return True, f"local hostname ({host})"
    try:
        ip = ipaddress.ip_address(low)
    except ValueError:
        return False, ""
    if ip.is_loopback:
        return True, f"loopback address ({host})"
    if ip.is_link_local:
        return True, f"link-local address ({host})"
    if ip.is_private:
        return True, f"private address ({host})"
    return False, ""


# ── Browser cookie snapshot ────────────────────────────────────────────────

# The CDP connection is single — N parallel http_request calls would otherwise
# fire N concurrent cookies.get_all() at the same socket. Queue them instead.
_COOKIE_LOCK = asyncio.Lock()


async def _get_browser_cookies_for_url(url: str) -> list[dict]:
    """Extract cookies from browser that apply to URL.

    A failed read raises. Returning [] made it "no cookies apply": an authenticated
    request then went out without them, reported as measured.
    """
    if not BrowserState.browser:
        return []
    parsed = urlparse(url)
    all_cookies = await BrowserState.browser.cookies.get_all()
    matching = []
    for c in all_cookies:
        if _cookie_applies(c, parsed):
            matching.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
            })
    # Most specific path first, as a browser sends them: a name-keyed jar keeps the
    # first, so /admin's cookie is not replaced by the site-wide one of that name.
    matching.sort(key=lambda c: len(c["path"]), reverse=True)
    return matching


def _cookie_applies(cookie: Any, parsed_url: Any) -> bool:
    """Would a browser send this cookie to this URL? RFC 6265 domain, path and Secure."""
    host = (parsed_url.hostname or "").lower()
    raw_domain = (cookie.domain or "").lower()
    domain = raw_domain.lstrip(".")
    if raw_domain.startswith("."):
        if not (host == domain or host.endswith("." + domain)):
            return False
    elif host != domain:  # no leading dot: a host-only cookie, never for subdomains
        return False
    cookie_path = cookie.path or "/"
    request_path = parsed_url.path or "/"
    if not (request_path == cookie_path
            or request_path.startswith(cookie_path.rstrip("/") + "/")):
        return False
    return not getattr(cookie, "secure", False) or parsed_url.scheme == "https"


async def _snapshot_browser_cookies(url: str) -> tuple[dict[str, str], dict]:
    """Read the browser's cookies for `url` ONCE, before the request starts.

    Taken up front and BY VALUE. A tracked request outlives the tool call that
    started it, and switch_instance() rebinds BrowserState's class attributes
    mid-flight — reading cookies from inside the request task would hand
    instance B's jar to a request that belongs to instance A. Snapshotting here
    pins the jar to the instance that was current when the caller asked.

    Returns (cookie dict, meta). `measured` distinguishes "no cookies apply"
    from "we never got to look".
    """
    meta = {
        "measured": False,
        "instance": BrowserState.current_instance_id,
        "count": 0,
        "reason": None,
    }
    if not BrowserState.browser:
        meta["reason"] = "no browser running — cookie-less request"
        return {}, meta
    try:
        async with _COOKIE_LOCK:
            cookies = await asyncio.wait_for(
                _get_browser_cookies_for_url(url), timeout=HTTP_COOKIE_TIMEOUT
            )
    except asyncio.TimeoutError:
        meta["reason"] = (
            f"browser cookie read timed out after {HTTP_COOKIE_TIMEOUT}s — "
            "request sent WITHOUT browser cookies"
        )
        return {}, meta
    except Exception as e:
        meta["reason"] = f"browser cookie read failed: {e} — request sent WITHOUT them"
        return {}, meta
    jar: dict[str, str] = {}
    for c in cookies:
        jar.setdefault(c["name"], c["value"])
    meta["measured"] = True
    meta["count"] = len(jar)
    return jar, meta


# ── Failure classification ─────────────────────────────────────────────────


def _classify_http_error(exc: BaseException, may_have_redirected: bool = False) -> dict:
    """Turn a curl_cffi failure into what it actually proves about the server.

    may_have_redirected: curl reports the LAST hop. A connect failure after following a
    redirect does not show the first server never got the request, so a POST it already
    processed must not be called safe to replay.

    curl reports both timeout phases as error 28; only libcurl's own message
    separates them, and that separation is the whole point — a connect timeout
    is safe to replay, a read timeout is not.
    """
    code = getattr(exc, "code", None)
    msg = str(exc)
    low = msg.lower()
    kind = "request_error"
    server_reached: Optional[bool] = None
    outcome = "unknown"

    if code == 28 or "timed out" in low:
        if "connection timed out" in low or "resolving timed out" in low:
            kind, server_reached, outcome = "connect_timeout", False, "not_executed"
        else:
            # "Operation timed out after N ms with M bytes received"
            kind, server_reached, outcome = "read_timeout", True, "unknown"
    elif code in _CURL_NOT_EXECUTED:
        server_reached, outcome = False, "not_executed"
        kind = {5: "proxy_error", 6: "dns_error", 7: "connect_failed",
                97: "proxy_error"}.get(code, "connect_failed")
        if code in (35, 53, 54, 58, 59, 60, 66, 77, 83, 90, 91, 98, 101):
            kind = "tls_error"
    elif code in _CURL_UNKNOWN:
        kind, server_reached, outcome = "transport_error", True, "unknown"

    if may_have_redirected and outcome == "not_executed":
        server_reached, outcome = None, "unknown"
    retry_safe = outcome == "not_executed"
    return {
        "error_class": kind,
        "curl_code": code,
        "server_reached": server_reached,
        "outcome": outcome,
        "retry_safe_any_method": retry_safe,
        "message": msg,
    }


# ── Redirects ──────────────────────────────────────────────────────────────

_REDIRECT_CODES = {301, 302, 303, 307, 308}
# curl_cffi's default for the single transfer this loop replaced: 30 followed, the 31st an error.
_MAX_REDIRECTS = 30
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _mask_urls_in(text: str, *urls: str) -> str:
    """`text` with each of these URLs, and its origin, replaced by mask_url's form of it.

    An exception message can quote the URL it failed on, userinfo included.
    """
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        text = text.replace(url, mask_url(url))
        parts = urlsplit(url)
        if "@" in parts.netloc:
            text = text.replace(parts.netloc, mask_url(f"//{parts.netloc}")[2:])
    return text


def _origin(url: str) -> tuple:
    """(scheme, host, effective port): what has to match for a header to follow a redirect."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        port = parts.netloc.rpartition(":")[2]
    return scheme, (parts.hostname or "").lower(), port


# ── Tracked request lifecycle ──────────────────────────────────────────────


@dataclass
class _Tracked:
    """One in-flight or settled http_request, addressable by request_id."""

    request_id: str
    method: str
    url: str
    created_at: float
    operation_deadline: float          # seconds the request may keep running
    spec: dict = field(default_factory=dict)   # everything needed to replay it
    session: str = ""                  # MCP session that made it; others cannot see it
    state: str = "pending"             # pending | succeeded | failed | cancelled
    outcome: str = "pending"           # pending | completed | not_executed | unknown
    result: Optional[str] = None       # the settled ok()/err() payload
    detail: dict = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    sent: bool = False                 # request handed to curl at least once
    hops: list = field(default_factory=list)   # redirects followed so far, real URLs
    hop_url: str = ""                  # the URL of the request in flight, the last hop's
    finished_at: Optional[float] = None

    def shown_url(self) -> str:
        """The URL for tool output, which lands in the transcript."""
        return self.url if self.spec.get("reveal_values") else mask_url(self.url)

    def shown_hops(self) -> list:
        """The redirect chain for tool output: masked here, at the boundary, not in the record."""
        if self.spec.get("reveal_values"):
            return [dict(hop) for hop in self.hops]
        return [{**hop, "from": mask_url(hop["from"]), "to": mask_url(hop["to"])} for hop in self.hops]

    def redirect_report(self) -> tuple[dict, str]:
        """(fields, words) naming the chain and the hop that failed; empty with no redirect.

        Every failure path uses it, a deadline or a cancel included: without it a failure after
        a redirect read like one on the first request, which the first server did receive.
        """
        if not self.hops:
            return {}, ""
        at = self.hop_url if self.spec.get("reveal_values") else mask_url(self.hop_url)
        return ({"redirects": self.shown_hops(), "failed_at": at},
                f" after {len(self.hops)} redirect(s), at {at}")

    def public(self) -> dict:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "url": self.shown_url(),
            "state": self.state,
            "outcome": self.outcome,
            "age_s": round(time.time() - self.created_at, 2),
            "operation_deadline_s": round(self.operation_deadline, 1),
            "detail": self.detail or None,
        }


_TRACKED: dict[str, _Tracked] = {}


def _forget_session_requests(session: str) -> None:
    for request_id, rec in list(_TRACKED.items()):
        if rec.session == session:
            if rec.state == "pending" and rec.task is not None:
                rec.task.cancel()
            _TRACKED.pop(request_id, None)


SESSION_GONE_HOOKS.append(_forget_session_requests)


def _own_tracked() -> list[_Tracked]:
    """Records of the calling session. A request can carry its cookies and tokens."""
    return [r for r in _TRACKED.values() if r.session == BrowserState.session_key()]


def _tracked_for_caller(request_id: str) -> Optional[_Tracked]:
    rec = _TRACKED.get(request_id)
    return rec if rec is not None and rec.session == BrowserState.session_key() else None


def _prune_tracked() -> None:
    """Keep the registry bounded — settled records age out, newest kept."""
    now = time.time()
    for rid, rec in list(_TRACKED.items()):
        if rec.state != "pending" and rec.finished_at and (
            now - rec.finished_at > HTTP_TRACK_TTL
        ):
            _TRACKED.pop(rid, None)
    overflow = len(_TRACKED) - HTTP_TRACK_MAX
    if overflow > 0:
        # Evict oldest SETTLED records only; a pending one still owns a task and
        # a caller holding its id. Dicts preserve insertion order.
        for rid, rec in list(_TRACKED.items()):
            if overflow <= 0:
                break
            if rec.state != "pending":
                _TRACKED.pop(rid, None)
                overflow -= 1


def _inflight() -> int:
    return sum(1 for r in _TRACKED.values() if r.state == "pending")


async def _perform(rec: _Tracked) -> str:
    """Run one curl_cffi request and render its settled response string."""
    from curl_cffi.requests import AsyncSession

    spec = rec.spec
    connect_timeout = float(spec["connect_timeout"])
    read_budget = max(0.1, float(spec["timeout"]) - connect_timeout)
    session_kwargs: dict = {"impersonate": spec["impersonate"]}
    if spec["direct"]:
        # Explicit CURLOPT_PROXY="" — the ONLY thing that stops libcurl reading
        # http_proxy out of the environment. trust_env=False does not: it is
        # curl_cffi's own flag and never reaches this decision (verified).
        session_kwargs["proxies"] = {"all": ""}

    started = time.monotonic()
    reveal = bool(spec.get("reveal_values"))
    shown_url = (lambda u: u) if reveal else mask_url
    async with AsyncSession(**session_kwargs) as session:
        caller_headers = dict(spec.get("headers") or {})
        cookie_kwargs: dict = {}
        if spec.get("cookies"):
            # libcurl's cookie engine drops a cookie for a host with no dot, so on localhost
            # it reported sending cookies that never left. That host gets a Cookie header,
            # which goes with the caller's headers and so only to the first origin.
            if "." in (urlsplit(spec["url"]).hostname or "."):
                # Given on the first hop only: curl scopes them to that host, and the
                # session's jar carries them, host-only, through the redirects after it.
                cookie_kwargs["cookies"] = spec["cookies"]
            else:
                caller_headers["Cookie"] = "; ".join(
                    f"{k}={v}" for k, v in spec["cookies"].items())

        method, url = rec.method, spec["url"]
        first_origin = _origin(url)
        keep_body = True
        hops: list[dict] = rec.hops  # shared, so a failure on a later hop can report the chain
        dropped: set[str] = set()
        note = None
        rec.sent = True
        while True:
            rec.hop_url = url
            remaining = read_budget - (time.monotonic() - started)
            kwargs: dict = {
                "timeout": (connect_timeout, max(0.1, remaining)),
                # Followed here, not by curl: curl carried every caller header to whatever
                # host a redirect named, so an X-Api-Key reached a third party.
                "allow_redirects": False,
            }
            if not hops:
                if spec.get("params"):
                    kwargs["params"] = spec["params"]
                kwargs.update(cookie_kwargs)
            headers = dict(caller_headers)
            if not keep_body:
                headers = {k: v for k, v in headers.items()
                           if k.lower() not in ("content-type", "content-length")}
            if _origin(url) != first_origin:
                dropped.update(headers)
                headers = {}
            if headers:
                kwargs["headers"] = headers
            # json_body takes precedence — curl_cffi overwrites the data-derived
            # body with the JSON body anyway, so make that explicit and never
            # pass both (which would also clobber the Content-Type).
            if keep_body:
                if spec.get("json_body") is not None:
                    kwargs["json"] = spec["json_body"]
                elif spec.get("data") is not None:
                    kwargs["data"] = spec["data"]

            resp = await session.request(method, url, **kwargs)
            if not spec["follow_redirects"] or resp.status_code not in _REDIRECT_CODES:
                break
            location = resp.headers.get("location")
            if not location:
                break
            target = urljoin(str(resp.url or url), location)
            if urlsplit(target).scheme.lower() not in ("http", "https"):
                note = (f"redirect not followed: the {resp.status_code} points to a "
                        f"{urlsplit(target).scheme}: URL, and only http and https are followed")
                break
            if len(hops) >= _MAX_REDIRECTS:
                from curl_cffi.requests.exceptions import TooManyRedirects
                raise TooManyRedirects(
                    f"Maximum ({_MAX_REDIRECTS}) redirects followed; the last pointed to "
                    f"{shown_url(target)}", 47)
            hops.append({"status": resp.status_code, "from": str(resp.url or url), "to": target})
            # As browsers do: 303 turns anything but HEAD into a GET, and 301 and 302 turn
            # a POST into one. 307 and 308 repeat the method with its body.
            if (resp.status_code == 303 and method != "HEAD") or (
                    resp.status_code in (301, 302) and method == "POST"):
                method, keep_body = "GET", False
            url = target

        return_mode = spec["return_mode"]
        body_text = ""
        if return_mode != "meta":
            try:
                body_text = resp.text
            except Exception:
                body_text = "<binary>"
            if len(body_text) > HTTP_BODY_MAX:
                body_text = (
                    body_text[:HTTP_BODY_MAX]
                    + f"\n\n[truncated — full size {len(resp.content)} bytes]"
                )

        elapsed_ms = None
        if not hops and hasattr(resp, "elapsed") and resp.elapsed is not None:  # last hop only
            try:
                # curl_cffi returns float seconds OR timedelta depending on version
                e = resp.elapsed
                elapsed_ms = (int(e.total_seconds() * 1000)
                              if hasattr(e, "total_seconds") else int(float(e) * 1000))
            except Exception:
                elapsed_ms = None
        if elapsed_ms is None:
            elapsed_ms = int((time.monotonic() - started) * 1000)

        # Tokens come back in JSON bodies and redirect URLs as well as Set-Cookie, and all
        # of this lands in the transcript.
        if not reveal:
            body_text = mask_body(body_text)
        meta = {
            "status": resp.status_code,
            "url": str(resp.url) if reveal else mask_url(str(resp.url)),
            "elapsed_ms": elapsed_ms,
            "headers": dict(resp.headers) if reveal else mask_headers(dict(resp.headers)),
            "cookies_sent": len(spec.get("cookies") or {}),
            "cookie_snapshot": spec.get("cookie_meta"),
            "impersonate": spec["impersonate"],
            "request_id": rec.request_id,
            "outcome": "completed",
            "transport": spec["transport"],
        }
        if hops:
            meta["redirects"] = rec.shown_hops()
            if method != rec.method:
                meta["final_method"] = method
        if dropped:
            meta["headers_not_forwarded"] = {
                "names": sorted(dropped),
                "why": "a redirect left the original origin (scheme, host, port); your "
                       "headers go only to that origin",
            }
        if note:
            meta["redirect_note"] = note

        shown = (lambda parsed: parsed) if reveal else mask_json_value
        if return_mode == "json":
            try:
                return ok(_bounded_result(meta, "body", shown(resp.json())))
            except Exception:
                return ok(_bounded_result(meta, "body_text", body_text))
        if return_mode == "text":
            return ok(f"{json.dumps(meta, indent=2, default=str)}\n\n--- BODY ---\n{body_text}")
        if return_mode == "meta":
            return ok(json.dumps(meta, indent=2, default=str))
        # auto mode
        try:
            parsed = shown(resp.json())
            return ok(_bounded_result(meta, "body", parsed))
        except Exception:
            return ok(f"{json.dumps(meta, indent=2, default=str)}"
                      f"\n\n--- BODY (text) ---\n{body_text}")


_RESULT_LIMIT = 25000


def _bounded_result(meta: dict, key: str, body: Any) -> str:
    """{"meta", key: body} as JSON, at most _RESULT_LIMIT chars and always parseable.

    Slicing the document cut it mid-string with no marker, so a large response came back
    as invalid JSON. Over the limit the body becomes a shortened string under body_text,
    and meta says so.
    """
    whole = json.dumps({"meta": meta, key: body}, indent=2, default=str)
    if len(whole) <= _RESULT_LIMIT:
        return whole
    raw = body if isinstance(body, str) else json.dumps(body, default=str)
    meta = {**meta, "truncated": True, "original_length": len(raw), "returned_length": len(raw)}
    # Escaping can more than double a string, so fit the ENCODED form, shrinking in
    # proportion. Each pass is strictly smaller, because the encoded length exceeded room.
    frame = json.dumps({"meta": meta, "body_text": ""}, indent=2, default=str)
    room = max(0, _RESULT_LIMIT - len(frame))
    keep = min(len(raw), room)
    while keep > 0 and len(json.dumps(raw[:keep])) > room:
        keep = keep * room // len(json.dumps(raw[:keep]))
    meta["returned_length"] = keep
    return json.dumps({"meta": meta, "body_text": raw[:keep]}, indent=2, default=str)


async def _run_tracked(rec: _Tracked) -> str:
    """Own one request end to end and record what actually happened."""
    try:
        result = await asyncio.wait_for(_perform(rec), timeout=rec.operation_deadline)
    except asyncio.CancelledError:
        rec.state = "cancelled"
        # Cancelling stops US waiting. If the bytes already went out, the server
        # may have acted on them and nothing here undid that.
        rec.outcome = "unknown" if rec.sent else "not_executed"
        chain, where = rec.redirect_report()
        rec.detail = {
            "error_class": "cancelled",
            "server_reached": True if rec.sent else False,
            "outcome": rec.outcome,
            "note": ("cancelled client-side; NO rollback was performed or requested "
                     "of the server" if rec.sent else
                     "cancelled before the request was handed to curl"),
            **chain,
        }
        rec.result = err(
            f"http_request {rec.request_id} cancelled{where} — client stopped waiting.\n"
            + json.dumps(rec.detail, indent=2, default=str)
        )
        rec.finished_at = time.time()
        raise
    except asyncio.TimeoutError:
        rec.state = "failed"
        rec.outcome = "unknown"
        chain, where = rec.redirect_report()
        rec.detail = {
            "error_class": "operation_deadline",
            "server_reached": rec.sent,
            "outcome": "unknown",
            "message": (f"request exceeded its operation deadline of "
                        f"{rec.operation_deadline:.1f}s and was abandoned"),
            **chain,
        }
        rec.result = err(
            f"http_request {rec.request_id} hit its operation deadline "
            f"({rec.operation_deadline:.1f}s){where} — outcome UNKNOWN.\n"
            + json.dumps(rec.detail, indent=2, default=str)
        )
    except ImportError:
        rec.state = "failed"
        rec.outcome = "not_executed"
        rec.detail = {"error_class": "missing_dependency", "outcome": "not_executed"}
        rec.result = err("curl-cffi not installed — pip install curl-cffi")
    except Exception as e:
        # The hops are known now: a failure after one does not show the first server never
        # got the request, whatever the method.
        detail = _classify_http_error(e, may_have_redirected=bool(rec.hops))
        rec.state = "failed"
        rec.outcome = detail["outcome"]
        reveal = bool(rec.spec.get("reveal_values"))
        if not reveal:
            detail["message"] = _mask_urls_in(detail["message"], rec.url, rec.hop_url)
        chain, where = rec.redirect_report()
        detail.update(chain)
        detail["request_id"] = rec.request_id
        detail["next"] = (
            "http_request_retry(request_id=...) — safe, the server never saw this"
            if detail["outcome"] == "not_executed" else
            "http_request_retry(request_id=..., confirm_side_effects=True) — the "
            "server may already have processed the first attempt"
        )
        rec.detail = detail
        rec.result = err(
            f"http_request: {detail['error_class']} ({rec.method} {rec.shown_url()}){where} — "
            f"outcome {detail['outcome']}.\n"
            + json.dumps(detail, indent=2, default=str)
        )
    else:
        rec.state = "succeeded"
        rec.outcome = "completed"
        rec.result = result
        rec.detail = {}
    finally:
        if rec.finished_at is None:
            rec.finished_at = time.time()
    return rec.result or ""


@mcp.tool()
async def http_request(
    url: str,
    method: str = "GET",
    impersonate: str = "chrome",
    use_browser_cookies: bool = True,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[str] = None,
    json_body: Optional[dict] = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    return_mode: str = "auto",
    wait_timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    direct: Optional[bool] = None,
    reveal_values: bool = False,
) -> str:
    """HTTP request with TLS-perfect browser fingerprint via curl_cffi.
    Use for API scraping after browser login — same stealth as real Chrome's JA3/JA4.

    Each call gets its own curl session, so parallel calls are safe — nothing
    shares a browser tab. Localhost / private-network targets bypass the
    environment proxy automatically.

    Every call is tracked: if it does not settle within `wait_timeout` you get a
    request_id back instead of a dead end, the request keeps running until
    `timeout`, and http_request_status / http_request_cancel take it from there.

    Args:
        url, method: target URL and HTTP verb
        impersonate: chrome, chrome124, firefox, safari, edge (default chrome)
        use_browser_cookies: snapshot cookies from the active browser instance
            BEFORE the request starts (a later switch_instance cannot leak in)
        headers, params: extra headers/query params
        data: raw body string (form-urlencoded or custom)
        json_body: JSON body dict (sets Content-Type automatically)
        timeout: total request budget in seconds, and the operation deadline —
            the request keeps running this long even after wait_timeout returns
        follow_redirects: usual HTTP option
        return_mode: auto (json if parseable else text), json, text, or meta (status+headers only)
        wait_timeout: how long THIS call blocks. Default None = the whole
            timeout. Set it lower to get a request_id back early and poll.
        connect_timeout: connect-phase budget (default 10s, capped by timeout).
            Separating it is what lets a failure say "never reached the server"
            instead of a bare timeout.
        direct: force (True) or forbid (False) the proxy-bypassing direct
            transport. Default None auto-detects loopback / private targets.
        reveal_values: True returns credential headers, JSON fields and query parameters
            as they are. By default their values read "*** (N chars)", because tool output
            lands in the transcript. Bodies other than JSON or forms pass through.
    """
    try:
        from curl_cffi.requests import AsyncSession  # noqa: F401
    except ImportError:
        return err("curl-cffi not installed — pip install curl-cffi")

    # libcurl also speaks file:// and others, which would hand any local file to the caller.
    scheme = urlsplit(url).scheme.lower()
    if scheme and scheme not in ("http", "https"):
        return err(f"http_request: only http and https are allowed, not {scheme}://; nothing was sent")
    method = method.upper()
    timeout = max(0.1, min(float(timeout), HTTP_MAX_TIMEOUT))
    conn = HTTP_DEFAULT_CONNECT_TIMEOUT if connect_timeout is None else float(connect_timeout)
    conn = max(0.1, min(conn, timeout * 0.9))
    wait = timeout if wait_timeout is None else max(0.0, float(wait_timeout))

    auto_direct, direct_reason = _is_direct_target(url)
    use_direct = auto_direct if direct is None else bool(direct)
    if use_direct:
        transport = ("curl_cffi direct — proxy bypassed"
                     + (f" ({direct_reason})" if direct_reason else " (forced)")
                     + "; independent session, safe in parallel")
    else:
        transport = ("curl_cffi via environment proxy settings if any; "
                     "independent session, safe in parallel")

    _prune_tracked()
    if _inflight() >= HTTP_MAX_INFLIGHT:
        return err(
            f"{_inflight()} http_request calls are already in flight "
            f"(limit HTTP_MAX_INFLIGHT={HTTP_MAX_INFLIGHT}). Nothing was sent. "
            "Cancel one with http_request_cancel or wait for it to settle."
        )

    cookies_dict: dict[str, str] = {}
    cookie_meta: dict = {"measured": False, "reason": "use_browser_cookies=False"}
    if use_browser_cookies:
        cookies_dict, cookie_meta = await _snapshot_browser_cookies(url)

    request_id = f"hreq_{uuid.uuid4().hex[:12]}"
    rec = _Tracked(
        request_id=request_id,
        method=method,
        url=url,
        created_at=time.time(),
        operation_deadline=timeout + HTTP_DEADLINE_GRACE,
        spec={
            "url": url,
            "impersonate": impersonate,
            "headers": dict(headers or {}),
            "params": params,
            "data": data,
            "json_body": json_body,
            "cookies": cookies_dict,
            "cookie_meta": cookie_meta,
            "timeout": timeout,
            "connect_timeout": conn,
            "follow_redirects": follow_redirects,
            "return_mode": return_mode,
            "direct": use_direct,
            "transport": transport,
            "use_browser_cookies": use_browser_cookies,
            "reveal_values": reveal_values,
        },
    )
    rec.session = BrowserState.session_key()
    _TRACKED[request_id] = rec
    rec.task = asyncio.ensure_future(_run_tracked(rec))

    done, _ = await asyncio.wait({rec.task}, timeout=wait)
    if rec.task in done:
        try:
            return rec.task.result()
        except asyncio.CancelledError:
            return rec.result or err(f"http_request {request_id} cancelled")
        except Exception as e:  # _run_tracked handles its own; belt and braces
            return err(f"http_request: {_mask_urls_in(str(e), url) if not reveal_values else e}")

    return err(
        f"http_request still running after {wait:.1f}s — outcome UNKNOWN, not failed.\n"
        + json.dumps({
            "request_id": request_id,
            "state": "pending",
            "outcome": "unknown",
            "method": method,
            "url": rec.shown_url(),
            "waited_s": round(wait, 2),
            "operation_deadline_s": round(rec.operation_deadline, 1),
            "note": ("the request is still in flight and keeps running until the "
                     "deadline; poll for the eventual response instead of resending"),
            "transport": transport,
            "next": [
                f'http_request_status(request_id="{request_id}", wait_timeout=30)',
                f'http_request_cancel(request_id="{request_id}")',
            ],
        }, indent=2, default=str)
    )


@mcp.tool()
async def http_request_status(
    request_id: Optional[str] = None,
    wait_timeout: float = 0.0,
    forget: bool = False,
) -> str:
    """Poll a tracked http_request — or list every one this session remembers.

    An http_request that outran its wait_timeout is still running. This is how
    you collect its eventual response instead of firing a duplicate.

    Args:
        request_id: which request. Omit to list all tracked requests.
        wait_timeout: block up to this long for a pending request to settle
            (0 = read the current state and return immediately)
        forget: drop the record after returning it (frees the buffer early)
    """
    _prune_tracked()
    if request_id is None:
        return ok(json.dumps({
            "tracked": len(_own_tracked()),
            "in_flight": _inflight(),
            "retention": {"max_records": HTTP_TRACK_MAX, "ttl_s": HTTP_TRACK_TTL,
                          "max_in_flight": HTTP_MAX_INFLIGHT},
            "requests": [r.public() for r in _own_tracked()],
        }, indent=2, default=str))

    rec = _tracked_for_caller(request_id)
    if rec is None:
        return err(
            f"unknown request_id {request_id!r} — it settled more than "
            f"{HTTP_TRACK_TTL:.0f}s ago, was forgotten, or never existed. "
            "The request's outcome is NOT knowable from here."
        )

    if rec.state == "pending" and wait_timeout > 0 and rec.task is not None:
        await asyncio.wait({rec.task}, timeout=wait_timeout)

    if rec.state == "pending":
        return err(
            f"http_request {request_id} still pending after "
            f"{time.time() - rec.created_at:.1f}s — outcome UNKNOWN.\n"
            + json.dumps(rec.public(), indent=2, default=str)
        )

    payload = rec.result or err(f"http_request {request_id}: no result recorded")
    if forget:
        _TRACKED.pop(request_id, None)
    return payload


@mcp.tool()
async def http_request_cancel(request_id: str) -> str:
    """Stop waiting on a tracked http_request.

    This is a CLIENT-SIDE cancel and nothing more. If the request already went
    out, the server may have processed it in full; no rollback is performed,
    requested, or implied. The reply says which of the two it was.
    """
    rec = _tracked_for_caller(request_id)
    if rec is None:
        return err(f"unknown request_id {request_id!r} — nothing to cancel")
    if rec.state != "pending":
        return ok(json.dumps({
            "request_id": request_id,
            "cancelled": False,
            "reason": f"already {rec.state}",
            "state": rec.state,
            "outcome": rec.outcome,
            "hint": f'http_request_status(request_id="{request_id}") for the result',
        }, indent=2, default=str))

    sent = rec.sent
    if rec.task is not None:
        rec.task.cancel()
        try:
            await asyncio.wait({rec.task}, timeout=5.0)
        except Exception:
            pass
    return ok(json.dumps({
        "request_id": request_id,
        "cancelled": True,
        "scope": "client-side only",
        "request_was_sent": sent,
        "server_effect": (
            "UNKNOWN — the request had already been handed to the network; it may "
            "have been received and executed. No rollback was performed."
            if sent else
            "none — the request was never handed to the network"
        ),
        "outcome": rec.outcome,
        **rec.redirect_report()[0],  # the chain and the hop it stopped on, masked
    }, indent=2, default=str))


@mcp.tool()
async def http_request_retry(
    request_id: str,
    confirm_side_effects: bool = False,
    wait_timeout: Optional[float] = None,
) -> str:
    """Explicitly replay a tracked http_request. Nothing here ever auto-retries.

    Refused when the first attempt's outcome is UNKNOWN and the method can have
    side effects — a POST that timed out on read may already have been applied,
    and replaying it silently would double it. Pass confirm_side_effects=True to
    say you have decided that is acceptable.

    Browser cookies are re-snapshotted so an expired session does not replay a
    stale jar; everything else (headers, body, transport) is reused verbatim.

    Returns a NEW request_id — the original record is left untouched.
    """
    rec = _tracked_for_caller(request_id)
    if rec is None:
        return err(f"unknown request_id {request_id!r} — nothing to retry")
    if rec.state == "pending":
        return err(
            f"http_request {request_id} is still in flight — poll it with "
            "http_request_status, or cancel it before retrying"
        )

    risky = rec.outcome != "not_executed" and rec.method not in _SAFE_METHODS
    if risky and not confirm_side_effects:
        return err(
            f"refusing to replay {rec.method} {rec.shown_url()}: the first attempt's outcome "
            f"is {rec.outcome!r}, so the server may already have applied it. "
            "Re-send only if a duplicate is acceptable: "
            f'http_request_retry(request_id="{request_id}", confirm_side_effects=True)'
        )

    spec = rec.spec
    return await http_request(
        url=spec["url"],
        method=rec.method,
        impersonate=spec["impersonate"],
        use_browser_cookies=spec["use_browser_cookies"],
        reveal_values=spec.get("reveal_values", False),
        headers=spec["headers"] or None,
        params=spec["params"],
        data=spec["data"],
        json_body=spec["json_body"],
        timeout=spec["timeout"],
        follow_redirects=spec["follow_redirects"],
        return_mode=spec["return_mode"],
        wait_timeout=wait_timeout,
        connect_timeout=spec["connect_timeout"],
        direct=spec["direct"],
    )


@mcp.tool()
async def http_session_cookies(url: str) -> str:
    """Inspect which browser cookies would be sent with a request to URL.

    Helpful to verify session sharing works before making requests. `measured`
    separates "no cookies apply to this URL" from "the browser never answered",
    which is the same distinction http_request records on the request itself.
    """
    if not BrowserState.is_up():
        return err("browser_launch first")
    jar, meta = await _snapshot_browser_cookies(url)
    if not meta["measured"]:
        return err(f"cookies not measured for {mask_url(url)}: {meta['reason']}")
    try:
        cookies = await _get_browser_cookies_for_url(url)
    except Exception as e:
        return err(f"cookies not measured for {mask_url(url)}: {_mask_urls_in(str(e), url)}")
    return ok(json.dumps({
        "url": mask_url(url),
        "instance": meta["instance"],
        "measured": True,
        "count": len(jar),
        "cookies": [{"name": c["name"], "domain": c["domain"], "path": c["path"]}
                    for c in cookies],
    }, indent=2))


@mcp.tool()
async def session_warmup(
    target_url: str,
    pattern: Literal["homepage_first", "referer_chain", "natural_browse"] = "homepage_first",
    dwell_seconds: float = 2.0,
) -> str:
    """Warm up session by navigating naturally before hitting target URL.
    Anti-bot systems score trust by session history — direct deep-URL hits look suspicious.

    Patterns:
      - homepage_first: goto origin → wait → goto target
      - referer_chain: goto origin → find link to target → click
      - natural_browse: homepage → scroll → random click → scroll → target
    """
    # Late import: mouse_drift lives in tools/mouse_kit.py.
    # By call time, server.py module is fully initialized.
    from .mouse_kit import mouse_drift
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        if not (target_url or "").startswith(("http://", "https://")):
            return err(f"session_warmup: target_url must be http or https, not {mask_url(target_url)!r}")
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # The actions and errors land in the transcript: no userinfo, query or fragment secret.
        shown, shown_origin = mask_url(target_url), mask_url(origin)

        tab = BrowserState.active_tab()
        actions = []

        if pattern == "homepage_first":
            await tab.get(origin)
            actions.append(f"visited {shown_origin}")
            await asyncio.sleep(dwell_seconds)
            # mouse drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=3)
            actions.append("mouse drifted")
            await tab.get(target_url)
            actions.append(f"navigated to {shown}")

        elif pattern == "referer_chain":
            await tab.get(origin)
            actions.append(f"visited {shown_origin}")
            await asyncio.sleep(dwell_seconds)
            # find link that leads closer to target
            link_data = await tab.evaluate(
                f"""
                (() => {{
                  const t = {json.dumps(target_url)};
                  const links = Array.from(document.querySelectorAll('a[href]'));
                  const seg = new URL(t).pathname.split('/')[1] || '';
                  const hit = links.find(a => t.startsWith(a.href) || (seg && a.href.includes(seg)));
                  if (!hit) return null;
                  const r = hit.getBoundingClientRect();
                  return JSON.stringify({{
                    href: hit.href,
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    visible: r.width > 0 && r.height > 0,
                  }});
                }})()
                """,
                return_by_value=True,
            )
            ldata = parse_json(link_data, None)
            if isinstance(ldata, dict) and ldata.get("visible"):
                await humanized_move(tab, ldata["x"] + 100, ldata["y"] - 50,
                                      ldata["x"], ldata["y"])
                await tab.mouse_click(ldata["x"], ldata["y"])
                actions.append(f"clicked link to {mask_url(ldata['href'])}")
                await asyncio.sleep(dwell_seconds)
                # if still not at target, nav directly
                cur = await get_url(tab)
                if target_url not in cur:
                    await tab.get(target_url)
                    actions.append(f"direct nav to {shown}")
            else:
                await tab.get(target_url)
                actions.append(f"no link found, direct nav to {shown}")

        elif pattern == "natural_browse":
            await tab.get(origin)
            actions.append(f"visited {shown_origin}")
            await asyncio.sleep(dwell_seconds / 2)
            # scroll
            await tab.evaluate("window.scrollBy(0, 400)", return_by_value=True)
            actions.append("scrolled 400px")
            await asyncio.sleep(dwell_seconds / 2)
            # drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=4)
            actions.append("mouse drifted")
            # random visible link click
            rand_link = await tab.evaluate(
                """
                (() => {
                  const links = Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                      const r = a.getBoundingClientRect();
                      return r.width > 30 && r.height > 10 && a.href.startsWith(location.origin);
                    });
                  if (links.length === 0) return null;
                  const pick = links[Math.floor(Math.random() * Math.min(links.length, 5))];
                  const r = pick.getBoundingClientRect();
                  return JSON.stringify({href: pick.href, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)});
                })()
                """,
                return_by_value=True,
            )
            rdata = parse_json(rand_link, None)
            if isinstance(rdata, dict):
                await humanized_move(tab, rdata["x"] + 150, rdata["y"] - 60,
                                      rdata["x"], rdata["y"])
                await tab.mouse_click(rdata["x"], rdata["y"])
                actions.append(f"random click → {mask_url(rdata['href'])}")
                await asyncio.sleep(dwell_seconds)
                await tab.evaluate("window.scrollBy(0, 300)", return_by_value=True)
                actions.append("scrolled on intermediate page")
                await asyncio.sleep(dwell_seconds / 2)
            await tab.get(target_url)
            actions.append(f"final nav to {shown}")

        final = await get_url(tab, strict=True)
        if final.startswith("chrome-error:"):
            return err(f"session warmup ended on an error page: {shown} did not load. "
                       f"Steps tried:\n  " + "\n  ".join(actions))
        return ok("session warmup complete:\n  " + "\n  ".join(actions))
    except Exception as e:
        return err(_mask_urls_in(str(e), target_url))


@mcp.tool()
async def detect_anti_bot() -> str:
    """Analyze current page + HTTP headers to identify anti-bot system.

    Detects: Cloudflare, DataDome, PerimeterX/HUMAN, Akamai Bot Manager,
    Kasada, Imperva/Incapsula, F5 Shape, none. Returns system + recommended
    bypass strategy from our toolkit.
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        tab = BrowserState.active_tab()

        # JS probes — look for telltale script names, objects, cookies
        probes = await tab.evaluate(
            """
            (() => {
              const out = {};
              // Cookies (accessible from JS if not HttpOnly)
              out.cookies = document.cookie;
              // Page HTML signature
              out.html_head = document.documentElement.outerHTML.slice(0, 5000);
              // Known globals
              out.has_turnstile = !!window.turnstile;
              out.has_grecaptcha = !!window.grecaptcha;
              out.has_hcaptcha = !!window.hcaptcha;
              out.has_px = !!window._pxAppId || !!window._pxCID;
              out.has_kasada = !!window.KPSDK;
              out.has_imperva = !!window._impervasecure;
              return JSON.stringify(out);
            })()
            """,
            return_by_value=True,
        )
        data = parse_json(probes, {})
        cookies = str(data.get("cookies", ""))
        html = str(data.get("html_head", ""))

        detections = []
        strategies = []

        # Cloudflare signatures
        if ("__cf_bm" in cookies or "cf_clearance" in cookies or
            "cdn-cgi" in html or "challenges.cloudflare.com" in html or
            "cf-beacon" in html or data.get("has_turnstile")):
            detections.append("Cloudflare")
            strategies.append("click_turnstile() or verify_cf() for challenges")
            strategies.append("http_request(impersonate='chrome') for API calls")

        # DataDome
        if "datadome" in cookies.lower() or "dd_s" in cookies or "datadome" in html.lower():
            detections.append("DataDome")
            strategies.append("⚠️ DataDome is HARDEST — use mouse_drift + session_warmup + residential proxy")
            strategies.append("mouse_record + mouse_replay of real human session")

        # PerimeterX / HUMAN
        if (data.get("has_px") or "_px" in cookies or "perimeterx" in html.lower() or
            "_pxhd" in cookies):
            detections.append("PerimeterX/HUMAN")
            strategies.append("storage_state_load (session reuse) is most reliable")
            strategies.append("mouse_behavior_profile + mobile proxy for new sessions")

        # Akamai Bot Manager
        if ("_abck" in cookies or "bm_sz" in cookies or "akamai" in html.lower() or
            "ak-bm-api" in html):
            detections.append("Akamai Bot Manager")
            strategies.append("http_request with impersonate='chrome' for TLS match")
            strategies.append("session_warmup(pattern='natural_browse')")

        # Kasada
        if data.get("has_kasada") or "kpsdk" in html.lower() or "kasada" in html.lower():
            detections.append("Kasada")
            strategies.append("⚠️ Kasada is VERY HARD — requires residential proxy + real browser")
            strategies.append("consider CapSolver or commercial service for this target")

        # Imperva / Incapsula
        if data.get("has_imperva") or "incap_ses" in cookies or "visid_incap" in cookies:
            detections.append("Imperva/Incapsula")
            strategies.append("http_request + cookie persistence after warmup")

        # reCAPTCHA / hCaptcha presence
        if data.get("has_grecaptcha"):
            detections.append("reCAPTCHA (v2 or v3)")
            strategies.append("solve_recaptcha_ai() (Claude vision) or solve_captcha(kind='recaptcha_v2')")
        if data.get("has_hcaptcha"):
            detections.append("hCaptcha")
            strategies.append("solve_captcha(kind='hcaptcha', ...) via CapSolver")

        if not detections:
            detections.append("none detected")
            strategies.append("proceed with normal automation — site has no/low anti-bot")

        return ok(json.dumps({
            "detected": detections,
            "recommended_tools": strategies,
            "cookies_found": [c.split("=")[0].strip() for c in cookies.split(";") if "=" in c][:20],
        }, indent=2))
    except Exception as e:
        return err(str(e))
