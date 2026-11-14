"""Identity and session tools: fingerprint_rotate, identity_pin and identity_show, human-like
click and typing, storage_state_save and storage_state_load, solve_captcha and verify_cf.

Moved out of server.py. _apply_storage_state stays there because launch paths and the
os-cursor solver share it; code here reaches shared helpers as _srv.<name> at call time.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Literal, Optional

from .. import server as _srv
from .._app import mcp
from ..captcha import CapSolverError, solve as capsolver_solve
from ..helpers import PageReadError, confined_output_path, err, get_url, ok, ts_filename
from ..humanize import humanized_move
from ..state import STORAGE_STATE_DIR, BrowserState, ensure_dirs


# ══════════════════════════════════════════════════════════════════════════
# 19. DIFFERENTIATORS (vs vibheksoni/patchright-mcp-lite/puppeteer-real)
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def storage_state_save(filename: Optional[str] = None) -> str:
    """Save cookies + localStorage of current origin to JSON.

    DIFFERENTIATOR: Per research, session-reuse is THE most reliable way to
    bypass Cloudflare Turnstile — it never triggers if session valid.
    Login manually once → save state → reuse forever until expiry.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        ensure_dirs()
        fname = filename or ts_filename("state", "json")
        path = confined_output_path(STORAGE_STATE_DIR, fname)
        tab = BrowserState.active_tab()
        cookies = await BrowserState.browser.cookies.get_all()
        local_storage = await tab.get_local_storage()
        # Derive a real scheme://host[:port] origin (one get_url round-trip,
        # not two) — rsplit('/',1) mangled URLs with paths into a bad key.
        from urllib.parse import urlparse
        origin = ""
        storage_note = ""
        try:
            _p = urlparse(await get_url(tab, strict=True))
            origin = f"{_p.scheme}://{_p.netloc}" if _p.scheme and _p.netloc else ""
            if not origin:
                storage_note = "; localStorage NOT saved: this page has no web origin"
        except PageReadError as exc:
            storage_note = f"; localStorage NOT saved: {exc}"
        state = {
            "cookies": [{
                "name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires,
                "secure": c.secure, "http_only": c.http_only,
            } for c in cookies],
            "origins": {origin: local_storage} if origin else {},
            "saved_at": time.time(),
        }
        # Real cookies and localStorage: the same credentials cookie_export protects.
        _srv._write_private(path, json.dumps(state, indent=2, default=str))
        return ok(f"saved {len(state['cookies'])} cookies to {path}{storage_note}")
    except Exception as e:
        return err(str(e))


def _storage_tab():
    """The page storage_state_load navigates to each origin and writes localStorage in.

    The first tab when the server opened it, as always (so a browser whose tabs are all the
    server's is unchanged); else the active tab if tools may act on it (a person's tab picked with
    tab_select); else the first tab the server opened; else None. Never a person's tab the agent
    did not pick, and never an adopted one the agent is not on.
    """
    BrowserState.own_untracked()
    tabs, owned = BrowserState.tabs, BrowserState.owned_targets
    if tabs and BrowserState.tab_key(tabs[0]) in owned:
        return tabs[0]
    index = BrowserState.active_tab_index
    if 0 <= index < len(tabs) and BrowserState.acts_on(tabs[index]):
        return tabs[index]
    return next((t for t in tabs if BrowserState.tab_key(t) in owned), None)


@mcp.tool()
async def storage_state_load(file_path: str) -> str:
    """Load cookies + localStorage from a saved JSON file.

    Call BEFORE navigating to protected site so session is ready.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running — launch first")
        tab = _storage_tab()
        if tab is None:
            return err("storage_state_load: nothing was loaded. It navigates a tab to each saved origin, "
                       "and no tab here was opened by this server (they are a person's). tab_new opens "
                       "one of the agent's own; tab_select adopts one of theirs on purpose.")
        report = await _srv._apply_storage_state(BrowserState.browser, file_path, tab=tab)
        line = (f"storage state from {file_path}: {report['cookies_set']} cookies set, "
                f"{report['origins_set']} origins restored")
        if report["cookies_failed"] or report["origins_failed"]:
            line += (f"; NOT applied: {report['cookies_failed']} cookies, "
                     f"{report['origins_failed']} origins ({report['first_error']})")
        return ok(line)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def solve_captcha(
    kind: Literal["turnstile", "recaptcha_v2", "recaptcha_v3", "hcaptcha"],
    website_url: str,
    website_key: str,
    api_key: Optional[str] = None,
    inject_selector: Optional[str] = None,
    action: Optional[str] = None,
) -> str:
    """Solve a CAPTCHA via CapSolver HTTP API.

    kind: turnstile | recaptcha_v2 | recaptcha_v3 | hcaptcha
    Needs CAPSOLVER_KEY env var (or pass api_key). Returns solved token.
    If inject_selector given, also injects token into that form field
    (e.g. input[name='cf-turnstile-response']).
    """
    type_map = {
        "turnstile": "AntiTurnstileTaskProxyLess",
        "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
        "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
    }
    meta = {"action": action} if action else None
    try:
        token = await capsolver_solve(
            task_type=type_map[kind],
            website_url=website_url,
            website_key=website_key,
            api_key=api_key,
            metadata=meta,
        )
    except CapSolverError as e:
        return err(f"CapSolver: {e}")
    if not inject_selector:
        return ok(f"token: {token}")
    # A caller that asked for injection goes on to submit the form, so a token that never
    # reached it has to read as a failure. The token is kept: it was paid for.
    why_not = f"no element matches {inject_selector!r}"
    injected = False
    if not BrowserState.is_up():
        why_not = "no browser running"
    else:
        try:
            tab = BrowserState.active_tab()
            raw = await tab.evaluate(
                f'(() => {{ var el = document.querySelector({json.dumps(inject_selector)}); '
                f'if (el) {{ el.value = {json.dumps(token)}; '
                f'el.dispatchEvent(new Event("input",{{bubbles:true}})); '
                f'el.dispatchEvent(new Event("change",{{bubbles:true}})); return true; }} return false; }})()',
                return_by_value=True,
            )
            injected = (raw.value if hasattr(raw, "value") else raw) is True
        except Exception as exc:
            why_not = _srv._exc_text(exc)
    if injected:
        return ok(f"token: {token}\ninjected: true ({inject_selector})")
    return err(f"token: {token}\ninjected: false ({why_not})")


@mcp.tool()
async def verify_cf(template_image: Optional[str] = None) -> str:
    """Use nodriver's built-in Cloudflare challenge verification.

    Uses OpenCV template matching to find the Turnstile checkbox on a screenshot
    and click it. template_image is a path to a cropped image of the checkbox;
    without it, the bundled English default is used.

    Reports what the page shows after the click, and errors when no checkbox matched.
    Clicks go through CDP. A full-page "Just a moment..." challenge did not pass that way
    in our tests: use solve_turnstile_oscursor, or storage_state_load / solve_captcha.
    """
    try:
        tab = BrowserState.active_tab()
        clicked, what = await _srv._cf_template_click(tab, template_image)
        if not clicked:
            return err(f"verify_cf: nothing clicked, {what}")
        return ok(f"verify_cf: {what}; {await _srv._cf_after_click(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def identity_pin(
    instance_id: Optional[str] = None,
    fingerprint: Optional[dict] = None,
    proxy: Optional[str] = None,
) -> str:
    """Persist a stable fingerprint + proxy for one browser instance.

    Future browser_launch/spawn_browser calls for that instance apply the pin
    automatically. Omitted fields preserve an existing pin. Fields never set follow
    the real browser, so pinning only a proxy leaves the identity as it is.
    """
    try:
        iid = instance_id or BrowserState.current_instance_id
        foreign = BrowserState.foreign_instance(iid)
        if foreign:
            return err(foreign)
        ensure_dirs()
        existing = _srv._load_identity(iid) or {}
        pinned_fingerprint = dict(existing.get("fingerprint") or {})
        if fingerprint is not None:
            pinned_fingerprint.update(_srv._normalize_fingerprint(fingerprint))
        pinned_proxy = proxy if proxy is not None else existing.get("proxy")
        payload = {
            "instance_id": iid,
            "fingerprint": pinned_fingerprint,
            "proxy": pinned_proxy,
            "updated_at": int(time.time()),
        }
        path = _srv._identity_path(iid)
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        # Private before the first byte: a pin can hold a proxy URL with credentials.
        _srv._write_private(tmp, json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)
        return ok(json.dumps(_srv._identity_for_report(payload), indent=2))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def identity_show(instance_id: Optional[str] = None, reveal_values: bool = False) -> str:
    """Show the persisted identity pin for an instance. A proxy password reads as ***
    unless reveal_values=True."""
    try:
        iid = instance_id or BrowserState.current_instance_id
        foreign = BrowserState.foreign_instance(iid)
        if foreign:
            return err(foreign)
        identity = _srv._load_identity(iid)
        if identity is None:
            return err(f"no pinned identity for instance {iid!r}")
        return ok(json.dumps(_srv._identity_for_report(identity, reveal_values), indent=2))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def fingerprint_rotate(
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform: Optional[str] = None,
    timezone: Optional[str] = None,
) -> str:
    """Override fingerprint vectors for active tab: user_agent, accept_language,
    platform (Win32/MacIntel/Linux x86_64), timezone (Asia/Jakarta, etc).
    Applied via CDP. Persists until next tab creation. Client hints
    (navigator.userAgentData) are derived from the UA so they agree with it, and a UA
    without a platform gets the matching navigator.platform.
    """
    if not any((user_agent, accept_language, platform, timezone)):
        return err("fingerprint_rotate: pass at least one of user_agent, accept_language, "
                   "platform, timezone; nothing was changed")
    try:
        tab = BrowserState.active_tab()
        await _srv._apply_fingerprint_to_tab(tab, {
            "user_agent": user_agent or "",
            "accept_language": accept_language or "",
            "platform": platform or "",
            "timezone": timezone or "",
        })
        if timezone:
            seen = await _srv._eval_checked(tab, "Intl.DateTimeFormat().resolvedOptions().timeZone")
            if seen != timezone:
                return err(f"timezone {timezone!r} was not applied; the page reads {seen!r}")
        return ok("fingerprint overrides applied")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def humanize_click(ref: Optional[str] = None,
                          selector: Optional[str] = None,
                          x: Optional[int] = None,
                          y: Optional[int] = None) -> str:
    """Click with Bezier-curve mouse approach + randomized dwell.

    Targets an element (ref/selector) OR a raw viewport point (x/y, CSS px).
    The coordinate form approaches the point along a Bezier path before pressing,
    which also renders hover-only overlays on the way in.
    """
    if x is not None and y is not None:
        try:
            tab = BrowserState.active_tab()
            outside = await _srv._outside_viewport(tab, x, y)
            if outside:
                return err(f"humanize_click: NOT clicked — {outside}")
            # Approach from an offset so the path has a direction, like a hand does.
            await _srv._dispatch_input(tab, humanized_move(tab, x + 120, y + 90, x, y))
            if _srv._dialog_note(tab):
                return err(f"humanize_click: NOT clicked — moving the pointer there opened a "
                           f"dialog{_srv._dialog_note(tab)}")
            await asyncio.sleep(random.uniform(0.05, 0.18))
            await _srv._dispatch_input(tab, tab.mouse_click(x, y))
            return ok(f"clicked ({x},{y}) via Bezier approach{_srv._dialog_note(tab)}")
        except Exception as e:
            return err(str(e))
    if ref is None and selector is None:
        return err("pass ref=, selector=, or both x= and y=")
    return await _srv.click(ref=ref, selector=selector, humanize=True)


@mcp.tool()
async def humanize_type(text: str, mean_delay: float = 0.12) -> str:
    """Type with Gaussian-distributed keystroke delays. See type_text."""
    return await _srv.type_text(text=text, humanize=True, mean_delay=mean_delay)
