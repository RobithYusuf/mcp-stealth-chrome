"""Cookies, localStorage, sessionStorage, IndexedDB and the HTTP cache.

Moved out of server.py unchanged. Cookie values are credentials: every reader hides them
unless reveal_values=True, and files holding them are written mode 600.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .._app import mcp
from ..helpers import confined_output_path, err, get_url, ok, ts_filename
from ..server import _eval_checked, _write_private
from ..state import STORAGE_STATE_DIR, BrowserState, ensure_dirs


# ══════════════════════════════════════════════════════════════════════════
# 8. COOKIES
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def cookie_list(url: Optional[str] = None, reveal_values: bool = False) -> str:
    """List cookies (optionally filtered by URL), with their values hidden by default.

    A session cookie is a credential, and whatever this returns lands in the agent's
    transcript. So each value is replaced by "***" plus `value_length`, which is enough
    to see that a cookie exists and is non-empty.

    Args:
        url: only cookies that apply to this host or URL
        reveal_values: True returns the real values. Only when the value itself is
            needed; to reuse a session, cookie_export or http_request does not need it.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        cookies = await BrowserState.browser.cookies.get_all()
        if url:
            cookies = [c for c in cookies if _cookie_domain_match(c.domain, url)]
        data = [{**_cookie_value_fields(c.value, reveal_values), "name": c.name,
                 "domain": c.domain, "path": c.path, "expires": c.expires,
                 "http_only": c.http_only, "secure": c.secure} for c in cookies]
        return ok(json.dumps(data, indent=2, default=str))
    except Exception as e:
        return err(str(e))


def _cookie_value_fields(value: Any, reveal: bool) -> dict[str, Any]:
    """The value itself, or only the fact of it."""
    if reveal:
        return {"value": value}
    return {"value": "***", "value_masked": True, "value_length": len(str(value or ""))}


def _cookie_domain_match(cookie_domain: Optional[str], url_or_host: str) -> bool:
    """Match a cookie domain against a user-supplied host or full URL.

    Accepts a bare host ("example.com") or a URL ("https://example.com/x").
    Matches the exact host or a parent-domain (registrable-suffix) cookie,
    ignoring a leading dot on the cookie domain — so filtering by "example.com"
    no longer also matches "notexample.com" or "example.com.evil.test" the way
    a bare substring check did.
    """
    from urllib.parse import urlparse
    host = (urlparse(url_or_host).hostname or url_or_host).lower().strip().strip(".")
    cd = (cookie_domain or "").lower().strip().lstrip(".")
    if not cd or not host:
        return False
    return host == cd or host.endswith("." + cd)


def _active_tab_host() -> Optional[str]:
    """Best-effort hostname of the active tab — used as default cookie domain
    when raw_text doesn't include one."""
    try:
        index = BrowserState.active_tab_index
        if not BrowserState.browser or not 0 <= index < len(BrowserState.tabs):
            return None
        url = getattr(BrowserState.tabs[index], "url", "")
        if not url:
            return None
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        return f".{host}" if host and "." in host else host
    except Exception:
        return None


def _parse_cookie_text(text: str, default_domain: Optional[str]) -> list[dict]:
    """Auto-detect cookie text format and return list of cookie dicts.

    Handles:
      - JSON array: [{...}]
      - JSON object with cookies key: {"cookies": [...]} (storage_state)
      - Header string: "a=1; b=2" or "Cookie: a=1; b=2"
      - curl --cookie value (same as header)
      - Netscape cookies.txt (tab-separated, 7 columns)
    """
    text = text.strip()
    if not text:
        return []
    # 1. JSON
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
        except Exception:
            pass  # fall through to text parsing
    # 2. Netscape cookies.txt — tab-separated. The "#HttpOnly_" prefix is a real
    #    flag (curl/wget emit it), NOT a comment; every other "#" line is a comment.
    parsed_lines: list[tuple[str, bool]] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#HttpOnly_"):
            parsed_lines.append((s[len("#HttpOnly_"):], True))
        elif s.startswith("#"):
            continue
        else:
            parsed_lines.append((s, False))
    if parsed_lines and "\t" in parsed_lines[0][0]:
        out: list[dict] = []
        for ln, http_only in parsed_lines:
            parts = ln.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            try:
                exp = int(expires)
            except ValueError:
                exp = 0
            out.append({
                "name": name, "value": value,
                "domain": domain, "path": path,
                "secure": secure.lower() == "true",
                "httpOnly": http_only,
                "expires": exp,
            })
        if out:
            return out
    # 3. Header / curl format: "name=val; name=val2" (optional "Cookie:" prefix)
    s = text
    if ":" in s.split(";", 1)[0]:
        # "Cookie: a=1; b=2" → strip header name
        head, rest = s.split(":", 1)
        if head.strip().lower() in ("cookie", "set-cookie"):
            s = rest
    out = []
    for pair in s.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name:
            continue
        c: dict = {"name": name, "value": value}
        if default_domain:
            c["domain"] = default_domain
        out.append(c)
    return out


@mcp.tool()
async def cookie_set(name: str, value: str, domain: str, path: str = "/",
                     secure: bool = False, http_only: bool = False) -> str:
    """Set a cookie on the browser."""
    try:
        if not BrowserState.browser:
            return err("browser not running")
        if not name:
            return err("cookie_set: name must not be empty")
        from nodriver.cdp import network as cdp_network
        tab = BrowserState.active_tab()
        accepted = await tab.send(cdp_network.set_cookie(
            name=name, value=value, domain=domain, path=path,
            secure=secure, http_only=http_only,
        ))
        if accepted is False:
            return err(f"the browser refused cookie {name} on {domain}")
        return ok(f"cookie {name} set on {domain}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cookie_delete(name: str, domain: Optional[str] = None) -> str:
    """Delete cookies by name, then read back to prove they are gone.

    domain: only this domain, with or without a leading dot. Omit it to delete what
    applies to the active tab's host, including its parent domains.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        from nodriver.cdp import network as cdp_network
        from urllib.parse import urlsplit
        tab = BrowserState.active_tab()
        host = ""
        if not domain:
            host = (urlsplit(await get_url(tab, strict=True)).hostname or "").lower()
            if not host:
                return err("cookie_delete: the active tab has no host; pass domain=")

        def applies(cookie_domain: str) -> bool:
            bare = (cookie_domain or "").lstrip(".").lower()
            if domain:
                return bare == domain.lstrip(".").lower()
            return host == bare or host.endswith("." + bare)

        async def matching() -> list:
            found = await BrowserState.browser.cookies.get_all()
            return [c for c in found if c.name == name and applies(c.domain)]

        targets = await matching()
        if not targets:
            return err(f"no cookie named {name} on {domain or host}; nothing was deleted")
        for cookie in targets:
            # Network.deleteCookies matches the exact stored domain, path and partition, and
            # answers success whether or not anything matched.
            kwargs = {"name": name, "domain": cookie.domain, "path": cookie.path}
            partition = getattr(cookie, "partition_key", None)
            if partition is not None:
                kwargs["partition_key"] = partition
            await tab.send(cdp_network.delete_cookies(**kwargs))
        left = await matching()
        if left:
            where = ", ".join(sorted({f"{c.domain}{c.path}" for c in left}))
            return err(f"cookie {name} is still present on {where} after the delete")
        where = ", ".join(sorted({c.domain for c in targets}))
        return ok(f"cookie {name} deleted from {where} ({len(targets)})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cookie_import(
    cookies: Optional[list[dict]] = None,
    file_path: Optional[str] = None,
    raw_text: Optional[str] = None,
    default_domain: Optional[str] = None,
    clear_first: bool = False,
) -> str:
    """Bulk-import cookies. Three input modes — pick whichever is easiest:

    1. cookies=[{...}]            inline array of dicts (DevTools / EditThisCookie shape)
    2. file_path="..."            JSON file (array OR storage_state {"cookies":[...]})
    3. raw_text="..."             paste any of these and we auto-parse:
                                    • JSON array / object (same shapes as 1+2)
                                    • Header string: "name=val; name2=val2" or
                                      "Cookie: name=val; name2=val2"
                                    • Netscape cookies.txt (tab-separated)
                                    • curl --cookie / -b argument string
       NOTE: header / netscape formats lack domain — pass default_domain=".example.com"
       (or call this AFTER navigate so the active tab's URL provides it).

    Per-cookie fields (when JSON):
      {"name":"...", "value":"...", "domain":".example.com", "path":"/",
       "expires":1234567890, "secure":true, "httpOnly":false, "sameSite":"Lax"}

    Args:
        cookies: inline array
        file_path: JSON file
        raw_text: any cookie text — format auto-detected
        default_domain: fallback domain for header / netscape cookies (or auto-uses
            current tab URL's host)
        clear_first: wipe all existing cookies before import (default False)

    For full cookies + localStorage + sessionStorage restore, use storage_state_load.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        if cookies is None and not file_path and not raw_text:
            return err("pass cookies=[...], file_path='...', or raw_text='...'")
        # raw_text → cookies list (auto-detect)
        if raw_text and not cookies and not file_path:
            cookies = _parse_cookie_text(raw_text, default_domain or _active_tab_host())
            if not cookies:
                return err("could not parse raw_text — got 0 cookies")
        if file_path:
            raw = Path(file_path).read_text()
            parsed = json.loads(raw)
            # Accept either [...] or {"cookies": [...]}
            if isinstance(parsed, dict) and "cookies" in parsed:
                cookies = parsed["cookies"]
            elif isinstance(parsed, list):
                cookies = parsed
            else:
                return err("file content must be a list or {cookies: [...]}")
        if not isinstance(cookies, list) or not cookies:
            return err("no cookies to import")
        from nodriver.cdp import network as cdp_network
        tab = BrowserState.active_tab()

        # CDP type coercion — required because CookieParam.to_json() iterates
        # fields and calls .to_json() on each, which crashes for plain strings
        # in enum-typed fields (sameSite) and ints where floats are expected.
        def _coerce_same_site(v):
            if v is None or v == "":
                return None
            # Already a CookieSameSite enum?
            try:
                from enum import Enum
                if isinstance(v, Enum):
                    return v
            except Exception:
                pass
            # Map common strings to the enum (case-insensitive). "Unspecified"
            # → None (not a CDP value, will let Chrome decide).
            sv = str(v).strip().lower()
            mapping = {"strict": "Strict", "lax": "Lax", "none": "None"}
            canonical = mapping.get(sv)
            if canonical is None:
                return None
            try:
                return cdp_network.CookieSameSite(canonical)
            except Exception:
                return None

        params: list = []
        skipped = 0
        for c in cookies:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                skipped += 1
                continue
            kwargs: dict = {"name": str(c["name"]), "value": str(c["value"])}
            # Plain string fields
            for src, dst in (
                ("domain", "domain"), ("path", "path"), ("url", "url"),
            ):
                if src in c and c[src] is not None:
                    kwargs[dst] = str(c[src])
            # Bool fields
            for src, dst in (
                ("secure", "secure"),
                ("httpOnly", "http_only"), ("http_only", "http_only"),
            ):
                if src in c and c[src] is not None:
                    kwargs[dst] = bool(c[src])
            # TimeSinceEpoch — CDP class is float-subclass with .to_json();
            # CookieParam.to_json() calls self.expires.to_json(), so a raw float
            # crashes. Reject session cookies (expires=-1 / "Session" / 0).
            if "expires" in c and c["expires"] is not None:
                try:
                    exp = float(c["expires"])
                    if exp > 0:
                        kwargs["expires"] = cdp_network.TimeSinceEpoch(exp)
                except (TypeError, ValueError):
                    pass
            # Enum field — sameSite
            if "sameSite" in c or "same_site" in c:
                ss = _coerce_same_site(c.get("sameSite", c.get("same_site")))
                if ss is not None:
                    kwargs["same_site"] = ss
            # Enum field — priority (Low/Medium/High)
            pri_raw = c.get("priority")
            if pri_raw:
                try:
                    pmap = {"low": "Low", "medium": "Medium", "high": "High"}
                    canon = pmap.get(str(pri_raw).strip().lower())
                    if canon:
                        kwargs["priority"] = cdp_network.CookiePriority(canon)
                except Exception:
                    pass
            # Enum field — sourceScheme (Unset/NonSecure/Secure)
            ss_raw = c.get("sourceScheme") or c.get("source_scheme")
            if ss_raw:
                try:
                    smap = {"unset": "Unset", "nonsecure": "NonSecure", "secure": "Secure"}
                    canon = smap.get(str(ss_raw).strip().lower())
                    if canon:
                        kwargs["source_scheme"] = cdp_network.CookieSourceScheme(canon)
                except Exception:
                    pass
            try:
                params.append(cdp_network.CookieParam(**kwargs))
            except Exception:
                skipped += 1
        if not params:
            return err(f"no valid cookies (skipped {skipped} entries)"
                       + ("; nothing was cleared" if clear_first else ""))
        # Only now: malformed input must not cost the caller every login in the browser.
        if clear_first:
            await tab.send(cdp_network.clear_browser_cookies())
        await tab.send(cdp_network.set_cookies(cookies=params))
        msg = f"imported {len(params)} cookies (clear_first={clear_first})"
        if skipped:
            msg += f", skipped {skipped} invalid"
        return ok(msg)
    except Exception as e:
        return err(str(e))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


@mcp.tool()
async def cookie_export(filename: Optional[str] = None,
                         url: Optional[str] = None,
                         reveal_values: bool = True) -> str:
    """Export cookies to a JSON file. Cookies-only (use storage_state_save for full session).

    Output is a plain JSON array compatible with cookie_import / EditThisCookie /
    Playwright cookies format. Saved to ~/.mcp-stealth/storage-states/ with mode 600.
    This tool's response names the file and never contains a value.

    Args:
        reveal_values: True (default) writes real values, because a file without them
            cannot be imported. False writes "***" and value_length, for sharing what
            cookies exist; such a file cannot restore a session.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        cookies = await BrowserState.browser.cookies.get_all()
        if url:
            cookies = [c for c in cookies if _cookie_domain_match(c.domain, url)]
        data = [{
            **_cookie_value_fields(c.value, reveal_values), "name": c.name,
            "domain": c.domain, "path": c.path, "expires": c.expires,
            "httpOnly": c.http_only, "secure": c.secure,
            # The enum's value: default=str wrote "CookieSameSite.NONE", which cookie_import
            # cannot map, so the imported cookie silently lost its SameSite.
            "sameSite": _enum_value(getattr(c, "same_site", None)),
        } for c in cookies]
        ensure_dirs()
        fname = filename or ts_filename("cookies", "json")
        path = confined_output_path(STORAGE_STATE_DIR, fname)
        _write_private(path, json.dumps(data, indent=2, default=str))
        note = "" if reveal_values else " with values masked; this file cannot be imported"
        return ok(f"{path}\nexported {len(data)} cookies{note}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 9. STORAGE (local + session)
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def localstorage_get(key: Optional[str] = None) -> str:
    """Get localStorage — all keys or one specific key."""
    try:
        tab = BrowserState.active_tab()
        if key:
            result = await _eval_checked(tab, f"localStorage.getItem({json.dumps(key)})")
            return ok(str(result) if result is not None else "null")
        all_ls = await tab.get_local_storage()
        return ok(json.dumps(all_ls, indent=2))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def localstorage_set(key: str, value: str) -> str:
    """Set a localStorage entry."""
    try:
        tab = BrowserState.active_tab()
        await _eval_checked(tab, f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
        return ok("set")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def localstorage_clear() -> str:
    """Clear all localStorage for current origin."""
    try:
        tab = BrowserState.active_tab()
        await _eval_checked(tab, "localStorage.clear()")
        return ok("cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_get(key: Optional[str] = None) -> str:
    """Get sessionStorage — all keys or one."""
    try:
        tab = BrowserState.active_tab()
        if key:
            result = await _eval_checked(tab, f"sessionStorage.getItem({json.dumps(key)})")
            return ok(str(result) if result is not None else "null")
        # Enumerate all keys
        result = await _eval_checked(
            tab,
            "(() => {var o={}; for(var i=0;i<sessionStorage.length;i++){var k=sessionStorage.key(i); o[k]=sessionStorage.getItem(k);} return JSON.stringify(o);})()",
        )
        return ok(str(result or "{}"))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_set(key: str, value: str) -> str:
    """Set a sessionStorage entry."""
    try:
        tab = BrowserState.active_tab()
        await _eval_checked(tab, f"sessionStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
        return ok("set")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def sessionstorage_clear() -> str:
    """Clear all sessionStorage for current origin (parity with localstorage_clear)."""
    try:
        tab = BrowserState.active_tab()
        await _eval_checked(tab, "sessionStorage.clear()")
        return ok("cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def cache_clear() -> str:
    """Clear the browser HTTP cache (CDP Network.clearBrowserCache).

    Mirrors DevTools → Application → Clear storage → Clear site data (cache).
    Does NOT touch cookies, localStorage, or IndexedDB — use dedicated tools
    or browser_launch(persistent=False) for a full wipe.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        await tab.send(cdp_network.clear_browser_cache())
        return ok("browser HTTP cache cleared")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def indexeddb_list() -> str:
    """List IndexedDB databases for the current origin.

    Reads via CDP IndexedDB.requestDatabaseNames. Use indexeddb_delete(name)
    to drop one. Useful for clearing SPA state (many PWAs store auth / drafts
    in IndexedDB rather than localStorage).
    """
    try:
        tab = BrowserState.active_tab()
        url = await get_url(tab, strict=True)
        from urllib.parse import urlparse
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
        if not origin:
            return err(f"cannot derive origin from URL: {url}")
        from nodriver.cdp import indexed_db as cdp_idb
        await tab.send(cdp_idb.enable())
        names = await tab.send(cdp_idb.request_database_names(security_origin=origin))
        if not names:
            return ok(f"no IndexedDB databases for {origin}")
        lines = [f"IndexedDB databases for {origin}:"]
        for n in names:
            lines.append(f"  {n}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def indexeddb_delete(database_name: str) -> str:
    """Delete an IndexedDB database by name (scoped to current origin)."""
    try:
        tab = BrowserState.active_tab()
        url = await get_url(tab, strict=True)
        from urllib.parse import urlparse
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
        if not origin:
            return err(f"cannot derive origin from URL: {url}")
        from nodriver.cdp import indexed_db as cdp_idb
        await tab.send(cdp_idb.enable())
        names = await tab.send(cdp_idb.request_database_names(security_origin=origin))
        if database_name not in (names or []):
            return err(f"no IndexedDB database {database_name!r} for {origin}; nothing was deleted")
        await tab.send(cdp_idb.delete_database(
            database_name=database_name, security_origin=origin,
        ))
        return ok(f"deleted IndexedDB '{database_name}' for {origin}")
    except Exception as e:
        return err(str(e))
