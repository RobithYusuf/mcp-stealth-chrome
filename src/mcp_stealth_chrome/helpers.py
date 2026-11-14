"""Shared helper utilities."""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit
from typing import Any, Iterator

from nodriver import Tab

from .state import BrowserState


# Names whose values are credentials. Matched as whole words, so "author" and the
# ":authority" pseudo-header stay readable. Tool output lands in the agent's transcript.
_SECRET_NAME = re.compile(
    r"(?:^|[-_.\s\[\]])(?:auth|authentication|authorization|bearer|tokens?|jwt|secret|password"
    r"|passwd|passphrase|passcode|pwd|pass|otp|pin|cvv|cvc|api[-_]?key|apikey|csrf|xsrf|session"
    r"|sessionid|sid|cookies?|credentials?|signature)(?:$|[-_.\s\[\]])",
    re.IGNORECASE,
)


# Names that are credentials in a URL only. An OAuth authorization code is exchanged for
# tokens, a SAML response and a CAS ticket are bearer credentials, a JWT bearer grant sends
# an assertion. In a JSON body "code" is usually an error number, so these stay URL-only.
_URL_SECRET_NAME = re.compile(
    r"^(?:code|authorization[-_]?code|auth[-_]?code|samlresponse|samlrequest|ticket|assertion"
    r"|client[-_]?assertion)$",
    re.IGNORECASE,
)


def is_secret_name(name: Any) -> bool:
    """Whether a header, form or JSON field of this name carries a credential."""
    return bool(_SECRET_NAME.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", str(name))))


def masked_value(value: Any) -> str:
    """Enough to see that a value exists and is non-empty, as cookie_list does."""
    return f"*** ({len(str(value))} chars)"


# Headers whose value is a URL. Their names are not secret, but a redirect's Location or a
# Referer can carry a token in its query, fragment or userinfo.
_URL_HEADERS = {"location", "content-location", "referer"}


def mask_headers(headers: dict) -> dict:
    return {k: masked_value(v) if is_secret_name(k)
            else mask_url(v) if str(k).lower() in _URL_HEADERS else v
            for k, v in (headers or {}).items()}


def _mask_all(node: Any) -> Any:
    """Everything below a secret key: {"token": {"value": "x"}} is as secret as {"token": "x"}."""
    if isinstance(node, dict):
        return {k: _mask_all(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_all(item) for item in node]
    return node if node is None or isinstance(node, bool) else masked_value(node)


# A session object usually mixes the user and expiry with its tokens. Those stay readable, and
# any secret names inside are still caught field by field.
_CONTAINER_ONLY_NAMES = re.compile(r"^(?:session|sessions)$", re.IGNORECASE)


def _mask_json(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if not is_secret_name(k):
                out[k] = _mask_json(v)
            elif isinstance(v, (dict, list)):
                out[k] = _mask_json(v) if _CONTAINER_ONLY_NAMES.match(str(k)) else _mask_all(v)
            elif v is None or isinstance(v, bool):
                out[k] = v
            else:
                out[k] = masked_value(v)
        return out
    if isinstance(node, list):
        return [_mask_json(item) for item in node]
    return node


def mask_json_value(value: Any) -> Any:
    """Credential fields hidden in an already parsed JSON document."""
    return _mask_json(value)


def _mask_pairs(text: str, *, url: bool = False) -> str | None:
    """A query or form body with secret fields hidden, or None when it has none.

    Works on the raw pairs. Decoding and re-encoding the rest would rewrite ordinary
    parameters, and a captured URL has to stay byte for byte what was requested. `url`
    adds the names that are credentials only in a URL (_URL_SECRET_NAME).
    """
    pieces = text.split("&")
    found = False
    for index, piece in enumerate(pieces):
        name = unquote_plus(piece.partition("=")[0])
        if is_secret_name(name) or (url and _URL_SECRET_NAME.match(name.strip())):
            found = True
            pieces[index] = f"{piece.partition('=')[0]}=***"
    return "&".join(pieces) if found else None


def _mask_own_keys(node: Any, name: Any = None) -> Any:
    """reveal_values="body": only a value whose own key is secret-named is hidden; a value under
    a secret-named parent is shown. A list's items are under the list's key."""
    if isinstance(node, dict):
        return {k: _mask_own_keys(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_own_keys(item, name) for item in node]
    if name is None or node is None or isinstance(node, bool) or not is_secret_name(name):
        return node
    return masked_value(node)


def mask_body(body: Any, *, own_keys_only: bool = False) -> Any:
    """Hide credential fields in a JSON or form body; returned unchanged when it has none.

    Other formats cannot be read field by field, so they pass through as they are.
    own_keys_only is reveal_values="body": in JSON only values whose own key is secret-named
    stay hidden. A form body is masked by its own names either way.
    """
    if not isinstance(body, str) or not body:
        return body
    text = body.strip()
    if text[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return body
        masked = _mask_own_keys(parsed) if own_keys_only else _mask_json(parsed)
        return body if masked == parsed else json.dumps(masked, ensure_ascii=False)
    if "=" in text and "\n" not in text and not text.startswith("<"):
        return _mask_pairs(text) or body
    return body


# block_ads needs the adblock package, an optional extra: say how to get it wherever it is missing.
ADBLOCK_MISSING = ("the adblock package is not installed (block_ads needs the adblock extra): "
                   "uv tool install 'mcp-stealth-chrome[adblock]' or "
                   "pip install 'mcp-stealth-chrome[adblock]', then restart the server")


def adblock_package_note() -> str:
    """Whether block_ads's engine package is there, found without importing it."""
    import importlib.util
    try:
        found = importlib.util.find_spec("adblock") is not None
    except (ImportError, ValueError):
        found = False
    return "installed" if found else ADBLOCK_MISSING


def mask_url(url: Any) -> Any:
    """Hide what in a URL is a credential: userinfo, and secret query or fragment pairs.

    `user:password@` (or a token as the user name) becomes `***@`. Secret pairs such as
    ?access_token=, and the URL-only credentials such as an OAuth ?code=, are hidden in the
    query and in the fragment, where an OAuth redirect puts its token (#access_token=, also
    after a hash route like #/cb?id_token=). Works on the raw text, so every ordinary
    parameter stays byte for byte what was requested.
    """
    if not isinstance(url, str) or not any(mark in url for mark in ("?", "#", "@")):
        return url
    rest, hash_mark, fragment = url.partition("#")
    rest, query_mark, query = rest.partition("?")
    if rest.startswith("//"):  # scheme-relative
        scheme, sep, after = "", "//", rest[2:]
    else:
        scheme, sep, after = rest.partition("://")
    if sep:
        netloc, slash, path = after.partition("/")
        if "@" in netloc:
            netloc = "***@" + netloc.rpartition("@")[2]
        rest = f"{scheme}{sep}{netloc}{slash}{path}"
    else:
        # An authority with no scheme at all, `user:pass@host/path`, as a caller or a redirect
        # may hand over. `mailto:` addresses are not credentials, and a leading `@` is a path.
        head, slash, path = rest.partition("/")
        if head.find("@") > 0 and not head.lower().startswith("mailto:"):
            rest = f"***@{head.rpartition('@')[2]}{slash}{path}"
    if query_mark:
        query = _mask_pairs(query, url=True) or query
    if hash_mark and "=" in fragment:
        route, route_mark, pairs = fragment.rpartition("?")
        if route_mark:
            fragment = f"{route}?{_mask_pairs(pairs, url=True) or pairs}"
        else:
            fragment = _mask_pairs(fragment, url=True) or fragment
    return f"{rest}{query_mark}{query}{hash_mark}{fragment}"


def _private_sibling(path: Path) -> tuple[int, Path]:
    """A new file beside `path`, open for writing, readable by this user only.

    O_EXCL and O_NOFOLLOW: the name is fresh and random, so nothing planted there, a symlink
    included, is ever opened. The umask can only narrow 0600, and fchmod makes it exact.
    """
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0))
    for _ in range(16):
        tmp = path.with_name(f".{path.stem}.{secrets.token_hex(6)}.tmp{path.suffix}")
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            continue
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:  # Windows before Python 3.13
            os.chmod(tmp, 0o600)
        return fd, tmp
    raise FileExistsError(f"could not create a temporary file beside {path}")


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file readable by this user only, all at once.

    For anything taken from a page: a screenshot or PDF can show a logged-in account, a
    trace or heap snapshot holds its URLs, form text and tokens, and a 0644 file made under
    the usual umask is readable by every account on the machine. The data goes to a private
    file beside `path`, which then replaces it (os.replace): a reader sees the old file or
    the whole new one, never a truncated or half-written one, and a symlink at `path` is
    replaced, not followed, so nothing outside the folder is written. Not fsynced: these are
    artifacts, not records that must survive a power cut. Windows keeps no POSIX mode bits;
    the user profile's ACLs protect the file there.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, tmp = _private_sibling(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def private_staging(path: Path) -> Iterator[Path]:
    """A private temporary path for a writer this code does not control, published at `path`.

    nodriver's save_screenshot opens its filename itself. It writes the temporary file (0600,
    created like write_private's), and only a file that holds something replaces `path`
    when the block ends; an empty one raises, so a capture that wrote nothing publishes
    nothing. On an error the temporary file is removed and `path` is left as it was.
    """
    fd, tmp = _private_sibling(path)
    os.close(fd)
    try:
        yield tmp
        if tmp.stat().st_size == 0:
            raise RuntimeError(f"nothing was written for {path.name}")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def mask_request_entry(entry: dict, *, body_mode: bool = False) -> dict:
    """A copy of a captured request safe to return from a tool. body_mode is
    reveal_values="body": headers and the URL as always, bodies by their own keys only."""
    out = dict(entry)
    for key in ("headers", "request_headers", "response_headers"):
        if isinstance(out.get(key), dict):
            out[key] = mask_headers(out[key])
    for key in ("body", "request_body"):
        if key in out:
            out[key] = mask_body(out[key], own_keys_only=body_mode)
    if "url" in out:
        out["url"] = mask_url(out["url"])
    return out


# reveal_values on the tools that show captured traffic: False hides credentials, True shows
# everything, "body" shows body values under a secret-named parent but keeps credential headers,
# URL secrets and every body value whose own key is secret-named hidden.
BODY_REVEAL_NOTE = ("reveal_values='body': credential headers and URL secrets stay masked, and so "
                    "does every body value whose own key is secret-named; body values under a "
                    "secret-named parent were revealed at the caller's request")


def reveal_mode(value: Any) -> str:
    """"none", "all" or "body" for a reveal_values argument; ValueError for anything else."""
    if value is None or value is False:
        return "none"
    if value is True:
        return "all"
    if isinstance(value, str) and value.strip().lower() == "body":
        return "body"
    raise ValueError(f"reveal_values must be true, false or \"body\", not {value!r}")


def cut_url(entry: dict, max_len: int) -> dict:
    """The entry with its URL cut to max_len, saying how much went (`url_cut`). Callers that
    mask a URL do it before the cut, so a secret past the cut is hidden either way."""
    url = entry.get("url")
    if not max_len or not isinstance(url, str) or len(url) <= max_len:
        return entry
    out = dict(entry)
    out["url"] = url[:max_len] + "…"
    out["url_cut"] = f"{len(url) - max_len} of {len(url)} chars cut; max_url_len=0 for whole URLs"
    return out


def target_key(tab: Any) -> str:
    """Stable per-tab identity backed by the CDP target id when available."""
    target = getattr(tab, "target", None)
    return str(getattr(target, "target_id", None) or id(tab))


# Native dialogs open right now, per tab. A dialog blocks the renderer: input never gets its
# acknowledgement and anything that needs the page never answers.
OPEN_DIALOGS: dict[str, dict] = {}


def dialog_note(tab: Any) -> str:
    """Words for a dialog that is open on this tab, or "" when none is."""
    found = OPEN_DIALOGS.get(target_key(tab))
    if not found:
        return ""
    return (f"; a native {found['type']} dialog is open ({found['message']!r}) and blocks the "
            f"page until it is answered: call dialog_handle(action='accept' or 'dismiss')")


def select_tab(index: int | None = None) -> Tab:
    """Resolve a tab index without changing active selection or OS focus."""
    active = BrowserState.active_tab()
    if index is None:
        return active
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(BrowserState.tabs):
        raise ValueError(f"tab index {index!r} out of range (0..{len(BrowserState.tabs) - 1})")
    return BrowserState.tabs[index]


def transport_lost(text: str) -> bool:
    """Recognize CDP transport failures, not ordinary page JavaScript errors."""
    message = text.lower()
    return any(marker in message for marker in (
        "no close frame received", "connectionclosed", "websocket is closed",
        "websocket connection is closed", "session with given id not found",
        "cdp session lost", "cdp websocket lost", "-32001",
    ))


class PageReadError(RuntimeError):
    """The page's URL or title could not be read. Blank would claim that it has none."""


async def _read_page_string(tab: Tab, expression: str, what: str, strict: bool) -> str:
    try:
        result = await tab.evaluate(expression, return_by_value=True)
        # nodriver hands back a raw RemoteObject when the value is falsy —
        # unwrap so a blank result never becomes the wrapper's repr string.
        result = result.value if hasattr(result, "value") else result
    except Exception as exc:
        # A lost connection is never "no URL yet", even for a caller who is polling:
        # it would otherwise wait out its whole timeout against a dead browser.
        if transport_lost(str(exc)):
            raise
        if strict:
            raise PageReadError(f"could not read the page {what}: {exc}") from exc
        return ""
    return str(result) if result else ""


async def get_url(tab: Tab, strict: bool = False) -> str:
    """The page URL via evaluate — nodriver doesn't expose a direct property.

    strict=False returns "" when the read fails, for loops polling through a navigation
    where that is expected. Anything that REPORTS the URL passes strict=True.
    """
    return await _read_page_string(tab, "window.location.href", "URL", strict)


async def get_title(tab: Tab, strict: bool = False) -> str:
    """The page title. Blank is a real answer here: many pages have no title."""
    return await _read_page_string(tab, "document.title", "title", strict)


def ts_filename(prefix: str, ext: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}.{ext}"


def confined_output_path(root: Path, filename: str) -> Path:
    """Resolve an output filename without letting it escape its managed root."""
    base = root.resolve()
    target = (root / filename).resolve()
    if target == base or base not in target.parents:
        raise ValueError(f"output path must stay inside {base}: {filename!r}")
    return target


def ok(text: str) -> str:
    """Success response — FastMCP auto-wraps as TextContent."""
    return text


def err(text: str) -> str:
    """Error response with recovery guidance on the first transport failure."""
    if transport_lost(text) and "browser_recover()" not in text:
        text += (" — CDP transport lost. Call browser_recover() then browser_launch(); "
                 "persistent profiles and logins are preserved. The action outcome is unknown; "
                 "do not blindly retry side effects.")
    return f"Error: {text}"


def parse_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default
