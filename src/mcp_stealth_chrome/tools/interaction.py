"""Pointer, keyboard and form tools beyond the core click: hover, mouse moves and clicks at
a point, drag and drop, press_key, select_option, uncheck, upload_file, click_role,
assert_clickable, and the wait_for family.

Moved out of server.py. click, click_text, fill, type_text and check stay there with the
hit test, dialog watch and outcome reporting they share; code here reaches those as
_srv.<name> at call time, so a test that patches server.<name> still reaches it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from .. import server as _srv
from .._app import mcp
from ..helpers import err, get_url, ok
from ..humanize import humanized_move, move_pointer
from ..identity import key_event_params
from ..refs import active_element, outer_blocker, resolve_ref
from ..state import BrowserState


# ══════════════════════════════════════════════════════════════════════════
# 4. INTERACTION
# ══════════════════════════════════════════════════════════════════════════


_IMPLICIT_ROLE_CSS = {
    "button": 'button, input[type="button"], input[type="submit"], input[type="reset"], summary',
    "link": "a[href], area[href]",
    "textbox": ('input:not([type]), input[type="text"], input[type="email"], input[type="tel"], '
                'input[type="url"], input[type="password"], textarea'),
    "searchbox": 'input[type="search"]',
    "checkbox": 'input[type="checkbox"]',
    "radio": 'input[type="radio"]',
    "combobox": "select",
    "listbox": "select[multiple]",
    "heading": "h1, h2, h3, h4, h5, h6",
    "img": "img",
    "listitem": "li",
    "navigation": "nav",
    "main": "main",
    "spinbutton": 'input[type="number"]',
    "slider": 'input[type="range"]',
}
_ACCESSIBLE_NAME_JS = """
function() {
  const labels = Array.from(this.labels || []).map(l => l.innerText);
  return [this.getAttribute('aria-label'), this.getAttribute('title'), this.getAttribute('alt'),
          this.getAttribute('placeholder'), this.innerText, this.value, ...labels]
    .filter(Boolean).join(' ');
}
"""


@mcp.tool()
async def click_role(role: str, name: Optional[str] = None, verify: bool = True) -> str:
    """Click by ARIA role (e.g. button, link, textbox), optional accessible name.

    Same honesty guarantees as click_text: reports the resolved element, refuses
    when the hit-test says the click can't land, and flags a no-op click.
    """
    try:
        tab = BrowserState.active_tab()
        # `:has-text()` is a Playwright-ism, not valid CSS — querySelector throws
        # on it and used to surface as a bare "DOM Error while querying". Query
        # only valid selectors, then narrow by accessible name in Python.
        el = None
        tried: list[str] = []
        # Most controls have their role implicitly: a <button> has no role attribute.
        role_css = f'[role="{role}"]'
        if _IMPLICIT_ROLE_CSS.get(role):
            role_css += ", " + _IMPLICIT_ROLE_CSS[role]
        tried.append(role_css + (f" named {name!r}" if name else ""))
        try:
            candidates = await tab.query_selector_all(role_css)
        except Exception:
            candidates = []
        first_rendered = None
        for candidate in candidates:
            if name:
                # A name was asked for, so the first element of that role will not do:
                # click_role("button", name="Save") must never reach a Delete button.
                try:
                    accessible = await candidate.apply(_ACCESSIBLE_NAME_JS)
                except Exception:
                    continue
                if name.lower() not in str(accessible or "").lower():
                    continue
            probe = await _srv._click_probe(candidate)
            if first_rendered is None:
                first_rendered = candidate
            if not probe.get("hidden") and probe.get("sized"):
                el = candidate
                break
        el = el or first_rendered
        if el is None and name:
            el = await tab.find(name, best_match=True)
            if el is not None and _srv._element_is_in_frame(tab, el):
                return err(_srv._FRAME_TEXT_HINT.format(text=name))
        if el is None:
            return err(
                f"no element found for role={role!r}"
                + (f" name={name!r}" if name else "")
                + f" — selectors tried: {', '.join(tried)}"
            )
        label = f"click_role {role!r}" + (f" name={name!r}" if name else "")
        return await _srv._click_and_report(tab, el, label, verify)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def assert_clickable(selector: Optional[str] = None, ref: Optional[str] = None,
                           x: Optional[int] = None, y: Optional[int] = None) -> str:
    """Check whether a click would actually reach the target — WITHOUT clicking.

    Answers "why did my click do nothing?" before you waste a call on it. Give a
    selector/ref to hit-test that element's centre, or x/y to ask what sits at a
    raw viewport point.

    Returns the resolved element, its rect, the hit-test verdict, and — when the
    point is covered — the element that is in the way.
    """
    try:
        tab = BrowserState.active_tab()
        if x is not None and y is not None:
            expr = """
            (() => {
              const d = (el) => {
                if (!el) return null;
                let s = el.tagName.toLowerCase();
                if (el.id) s += '#' + el.id;
                if (el.className && typeof el.className === 'string') {
                  const c = el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 3);
                  if (c.length) s += '.' + c.join('.');
                }
                return s;
              };
              const el = document.elementFromPoint(%d, %d);
              return JSON.stringify({
                point: {x: %d, y: %d},
                topmost: d(el),
                text: el ? (el.innerText || '').trim().slice(0, 80) : null,
                in_viewport: %d >= 0 && %d >= 0 && %d <= innerWidth && %d <= innerHeight
              });
            })()
            """ % (x, y, x, y, x, y, x, y)
            v = await tab.evaluate(expr, return_by_value=True)
            v = v.value if hasattr(v, "value") and not isinstance(v, str) else v
            info = json.loads(v) if v else {}
            if not info.get("in_viewport"):
                return ok(json.dumps({"clickable": False,
                                      "reason": "point is outside the viewport",
                                      **info}, indent=2))
            return ok(json.dumps({"clickable": info.get("topmost") is not None,
                                  "reason": None if info.get("topmost") else "nothing at that point",
                                  **info}, indent=2))
        el = None
        if ref:
            el = await resolve_ref(ref)
            if el is None:
                return err(f"ref {ref} not found")
        elif selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err(f"selector not found: {selector}")
        else:
            return err("pass selector=, ref=, or both x= and y=")
        probe = await _srv._click_probe(el)
        if not probe:
            return err("probe failed — could not evaluate the element")
        reason = _srv._blocked_reason(probe)
        if reason is None:
            # Frame content is covered by the page around it too, as click sees it.
            pos = await el.get_position()
            if pos is not None:
                reason = await outer_blocker(
                    el, pos.left + pos.width / 2, pos.top + pos.height / 2)
        return ok(json.dumps({"clickable": reason is None, "reason": reason, **probe},
                             indent=2))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def hover(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Hover over element."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos, probe, reason, scrolled = await _srv._prepare_target(el)
        # A disabled control still shows its tooltip, so that alone does not stop a hover.
        if reason == "element is disabled":
            pos, reason = await _srv._settled_position(el), None
        if reason is not None or pos is None:
            return err(f"NOT hovered — {reason or 'element has no on-screen position'}. "
                       f"target=<{probe.get('target') or 'unknown'}> rect={probe.get('rect')}.")
        await _srv._dispatch_input(tab, move_pointer(tab, int(pos.left + pos.width / 2), int(pos.top + pos.height / 2)))
        return ok(("hovered (scrolled into view)" if scrolled else "hovered") + _srv._dialog_note(tab))
    except Exception as e:
        return err(str(e))


def _key_effect(before: dict, after: dict) -> Optional[str]:
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


@mcp.tool()
async def press_key(key: str, verify: bool = True, settle_ms: int = 1000) -> str:
    """Press a key and report what was observed, not only that it was sent.

    Args:
        key: a character, a name (Enter, Tab, Escape, Backspace, ArrowDown, Home, F5)
            or a combination (Control+a, Shift+Tab).
        verify: compare the page before and after. False sends the key and says the
            effect was not checked.
        settle_ms: how long to watch for an effect. A form submitted by Enter takes a
            moment to navigate, so this stops as soon as something changes.

    "NO page change detected" means nothing this tool can see moved within settle_ms.
    The key was still delivered, and a handler may have acted without touching the DOM.
    """
    try:
        tab = BrowserState.active_tab()
        try:
            params = key_event_params(key)
        except ValueError as e:
            return err(str(e))
        focused = None
        with contextlib.suppress(Exception):
            focused = await active_element(tab)
        target = "<unknown>"
        if focused is not None:
            ident = (focused.attrs.get("id") if focused.attrs else None) or ""
            target = f"<{focused.node_name.lower()}{'#' + ident if ident else ''}>"
        before = await _srv._page_sig(tab, focused) if verify else {}
        await _srv._dispatch_input(tab, _srv._dispatch_key(tab, params))
        if _srv._dialog_note(tab):
            return ok(f"pressed {key} — sent to {target}{_srv._dialog_note(tab)}")
        if not verify:
            return ok(f"pressed {key} — sent to {target}; effect not checked (verify=False)")
        deadline = time.monotonic() + max(0, settle_ms) / 1000
        after: dict = {}
        while True:
            await asyncio.sleep(0.05)
            after = await _srv._page_sig(tab, focused)
            effect = _key_effect(before, after)
            if effect:
                return ok(f"pressed {key} — sent to {target}; {effect}")
            if time.monotonic() >= deadline:
                break
        if before and not after:
            return ok(f"pressed {key} — sent to {target}; the page stopped answering "
                      f"within {settle_ms}ms, so the effect is unknown (it may be navigating)")
        if not before:
            return ok(f"pressed {key} — sent to {target}; the page could not be read "
                      f"beforehand, so the effect is unknown")
        return ok(f"pressed {key} but NO page change detected within {settle_ms}ms — "
                  f"sent to {target}. The page shows no reaction (no DOM, URL or focus "
                  f"change).")
    except Exception as e:
        return err(str(e))


_SELECT_OPTION_JS = """
function() {
  const wantValue = %s, wantLabel = %s;
  if (this.tagName !== 'SELECT') {
    return JSON.stringify({error: 'not a <select> but <' + this.tagName.toLowerCase() + '>'});
  }
  if (this.disabled) return JSON.stringify({error: 'the <select> is disabled'});
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const options = [...this.options];
  const hit = options.find((o) => wantValue !== null
    ? o.value === wantValue
    : clean(o.label) === clean(wantLabel) || clean(o.text) === clean(wantLabel));
  if (!hit) {
    return JSON.stringify({error: 'no such option', options: options.slice(0, 30).map(
      (o) => ({value: o.value, label: clean(o.text)}))});
  }
  if (hit.disabled) return JSON.stringify({error: 'that option is disabled'});
  const changed = !hit.selected;
  hit.selected = true;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return JSON.stringify({value: hit.value, label: clean(hit.text), changed});
}
"""


@mcp.tool()
async def select_option(
    ref: Optional[str] = None, selector: Optional[str] = None,
    value: Optional[str] = None, label: Optional[str] = None,
) -> str:
    """Select an <option> of a <select> by value or by its visible label.

    The native dropdown cannot be driven with page input, so this sets the option from JS
    and fires input and change. Those events have isTrusted=false, which the result states.
    Reads the select back afterwards, because a page can reject the choice.
    """
    try:
        if value is None and label is None:
            return err("select_option: pass value= or label=")
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        outcome = json.loads(await el.apply(_SELECT_OPTION_JS % (json.dumps(value), json.dumps(label))))
        if outcome.get("error"):
            listed = outcome.get("options")
            return err(f"select_option: {outcome['error']}"
                       + (f". Options: {json.dumps(listed, ensure_ascii=False)}" if listed else ""))
        await asyncio.sleep(0.05)
        now = await el.apply("function() { return this.value; }")
        if now != outcome["value"]:
            return err(f"select_option: selected {outcome['value']!r} but the select now reads "
                       f"{now!r}. The page changed it back.")
        return ok(f"selected value={outcome['value']!r} label={outcome['label']!r} — "
                  f"method=js_set (input and change events, isTrusted=false)"
                  + ("" if outcome["changed"] else "; it was already selected"))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def uncheck(ref: Optional[str] = None, selector: Optional[str] = None,
                  js_fallback: bool = False) -> str:
    """Untick a checkbox, confirming the state changed. See check."""
    return await _srv._set_checked(ref, selector, False, js_fallback)


@mcp.tool()
async def upload_file(
    file_path: str,
    ref: Optional[str] = None, selector: Optional[str] = None,
) -> str:
    """Upload a file via <input type=file>."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        # Chrome accepts any path here, and a page then receives an empty file of that name.
        path = Path(file_path).expanduser()
        if not path.is_file():
            return err(f"upload_file: {file_path} is not a file on this machine; nothing was set")
        await el.send_file(str(path.resolve()))
        count = await el.apply("function() { return this.files ? this.files.length : 0; }")
        if count != 1:
            return err(f"upload_file: the input reports {count} file(s) after setting {path.name}")
        return ok(f"uploaded {path}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 5. MOUSE XY + DRAG
# ══════════════════════════════════════════════════════════════════════════


def _mouse_button(name: str):
    from nodriver.cdp.input_ import MouseButton
    return MouseButton(name)


@mcp.tool()
async def mouse_click_xy(x: int, y: int, button: str = "left",
                         move_first: bool = True) -> str:
    """Click at raw viewport coordinates (CSS px, relative to the viewport).

    Args:
        x, y: target point in CSS pixels, viewport-relative (same space as
            getBoundingClientRect()).
        button: "left" | "right" | "middle"
        move_first: send a mousemove to the point before pressing (default True).
            Real users hover before clicking, and menus/overlays that only render
            on hover (Canva, Figma, Notion) are unreachable without it. Set False
            only if a hover handler would interfere.
    """
    if button not in ("left", "right", "middle"):
        return err(f"mouse_click_xy: button must be left, right or middle, not {button!r}")
    try:
        tab = BrowserState.active_tab()
        outside = await _srv._outside_viewport(tab, x, y)
        if outside:
            return err(f"mouse_click_xy: NOT clicked — {outside}")
        if move_first:
            # Hover first so hover-rendered targets exist before mousedown.
            try:
                await _srv._dispatch_input(tab, move_pointer(tab, x, y))
            except Exception:
                pass  # best-effort; the click below is what matters
            if _srv._dialog_note(tab):
                return err(f"mouse_click_xy: NOT clicked — moving the pointer there opened a "
                           f"dialog{_srv._dialog_note(tab)}")
        await _srv._dispatch_input(tab, tab.mouse_click(x, y, button=_mouse_button(button)))
        return ok(f"clicked ({x},{y}){' after hover' if move_first else ''}{_srv._dialog_note(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def mouse_move(x: int, y: int, humanize: bool = False) -> str:
    """Move cursor to raw coordinates. humanize=True uses Bezier path."""
    try:
        tab = BrowserState.active_tab()
        outside = await _srv._outside_viewport(tab, x, y)
        if outside:
            return err(f"mouse_move: not moved — {outside}")
        if humanize:
            # Start from a random offset; nodriver has no current-pos getter
            await _srv._dispatch_input(tab, humanized_move(tab, x + 100, y + 100, x, y))
        else:
            await _srv._dispatch_input(tab, move_pointer(tab, x, y))
        return ok(f"moved to ({x},{y}){_srv._dialog_note(tab)}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def drag_and_drop(start_x: int, start_y: int, end_x: int, end_y: int) -> str:
    """Drag from (start_x, start_y) to (end_x, end_y) with real mouse input.

    Reports whether the page changed, since nothing else says the drop landed.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import input_ as cdp_input
        left = cdp_input.MouseButton.LEFT
        before = await _srv._page_sig(tab)

        async def gesture():
            await move_pointer(tab, start_x, start_y)
            await tab.send(cdp_input.dispatch_mouse_event(
                type_="mousePressed", x=start_x, y=start_y, button=left, buttons=1, click_count=1,
            ))
            x, y = start_x, start_y
            try:
                # Intermediate steps for natural drag
                steps = 20
                for i in range(1, steps + 1):
                    t = i / steps
                    x = int(start_x + (end_x - start_x) * t)
                    y = int(start_y + (end_y - start_y) * t)
                    await tab.send(cdp_input.dispatch_mouse_event(
                        type_="mouseMoved", x=x, y=y, button=left, buttons=1,
                    ))
                    await asyncio.sleep(0.02)
            finally:
                # Always let go: a button left down turns every later move into a drag.
                await tab.send(cdp_input.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y, button=left, buttons=0, click_count=1,
                ))
            BrowserState.last_mouse_xy.update(x=x, y=y)

        await _srv._dispatch_input(tab, gesture())
        if _srv._dialog_note(tab):
            return ok(f"drag from ({start_x},{start_y}) interrupted{_srv._dialog_note(tab)}. "
                      f"It finishes once the dialog is answered.")
        await asyncio.sleep(0.35)
        return _srv._click_outcome(
            f"dragged ({start_x},{start_y}) to ({end_x},{end_y})", "method=cdp_mouse",
            before, await _srv._page_sig(tab),
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for(selector: Optional[str] = None, text: Optional[str] = None,
                    timeout: float = 10.0) -> str:
    """Wait until selector exists or text is visible on the page. With neither, just sleep."""
    try:
        tab = BrowserState.active_tab()
        if selector:
            await tab.wait_for(selector=selector, timeout=timeout)
            return ok(f"{selector} appeared")
        if text:
            # Rendered text. tab.find also matches display:none nodes and <script> bodies.
            needle = json.dumps(text)
            expr = (f"[window, ...Array.from(window.frames)].some(w => {{ try {{ "
                    f"return !!w.document.body && w.document.body.innerText.includes({needle}); "
                    f"}} catch (e) {{ return false; }} }})")
            deadline = time.monotonic() + timeout
            while True:
                if await _srv._eval_checked(tab, expr) is True:
                    return ok(f"{text!r} found")
                if time.monotonic() >= deadline:
                    return err(f"{text!r} is not visible on the page after {timeout}s")
                await asyncio.sleep(0.2)
        await asyncio.sleep(timeout)
        return ok(f"slept {timeout}s")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_change(selector: Optional[str] = None, text: Optional[str] = None,
                          url_contains: Optional[str] = None,
                          gone: bool = False,
                          timeout: float = 10.0, poll: float = 0.25) -> str:
    """Wait until the page actually CHANGES, and report what changed.

    The "click, then wait for the result" pattern belongs in one call. With no
    arguments it waits for any structural change from the moment it is called —
    useful right after a click when you don't know what the result looks like.

    Args:
        selector: wait until this CSS selector appears (or disappears if gone=True)
        text: wait until this text appears in the visible page (or disappears)
        url_contains: wait until the URL contains this substring
        gone: invert selector/text — wait for it to go away instead
        timeout: seconds to wait before giving up (returns an error saying so)
        poll: seconds between checks

    Returns what satisfied the wait and how long it took, so a timeout is never
    mistaken for a silent success.
    """
    try:
        tab = BrowserState.active_tab()
        loop = asyncio.get_event_loop()
        started = loop.time()
        baseline = await _srv._page_sig(tab) if not (selector or text or url_contains) else {}

        def _cond_js() -> Optional[str]:
            if selector:
                return ("(() => JSON.stringify({hit: !!document.querySelector(%s)}))()"
                        % json.dumps(selector))
            if text:
                return ("(() => JSON.stringify({hit: (document.body ? document.body.innerText : '')"
                        ".indexOf(%s) !== -1}))()" % json.dumps(text))
            return None

        expr = _cond_js()
        while loop.time() - started < timeout:
            elapsed = round(loop.time() - started, 2)
            if url_contains:
                cur = await get_url(tab)
                if url_contains in (cur or ""):
                    return ok(f"url now contains {url_contains!r} after {elapsed}s — {cur}")
            elif expr:
                v = await _srv._eval_checked(tab, expr)
                hit = bool(json.loads(v).get("hit")) if v else False
                if hit != gone:  # gone=False → want hit; gone=True → want !hit
                    what = selector or repr(text)
                    verb = "disappeared" if gone else "appeared"
                    return ok(f"{what} {verb} after {elapsed}s")
            else:
                cur = await _srv._page_sig(tab)
                if baseline and cur and (
                    cur.get("url") != baseline.get("url")
                    or cur.get("nodes") != baseline.get("nodes")
                    or cur.get("len") != baseline.get("len")
                    or cur.get("hash") != baseline.get("hash")
                ):
                    if cur.get("url") != baseline.get("url"):
                        return ok(f"page navigated after {elapsed}s → {cur.get('url')}")
                    return ok(
                        f"DOM changed after {elapsed}s "
                        f"(nodes {baseline.get('nodes')}→{cur.get('nodes')}, "
                        f"html {baseline.get('len')}→{cur.get('len')} chars)"
                    )
            await asyncio.sleep(poll)

        target = (selector or (repr(text) if text else None)
                  or (f"url containing {url_contains!r}" if url_contains else "any DOM change"))
        return err(f"timeout after {timeout}s waiting for {target} — nothing changed")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_navigation(timeout: float = 15.0) -> str:
    """Wait until document.readyState is complete, or report that it did not get there."""
    try:
        tab = BrowserState.active_tab()
        await asyncio.sleep(min(0.25, max(0.0, timeout)))  # let a navigation just started commit
        loaded = await _srv._wait_load_complete(tab, timeout)
        url, _title = await _srv._page_facts_for_report(tab)
        if not loaded:
            return err(f"the page at {url} had not finished loading after {timeout}s")
        return ok(f"navigated to {url}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_url(pattern: str, timeout: float = 15.0) -> str:
    """Wait until URL matches a regex pattern."""
    try:
        tab = BrowserState.active_tab()
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = await get_url(tab)
            if regex.search(cur):
                return ok(f"URL matches: {cur}")
            await asyncio.sleep(0.3)
        return err(f"timeout waiting for URL {pattern}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_response(url_pattern: str, timeout: float = 15.0) -> str:
    """Wait for a network response whose URL matches regex."""
    try:
        tab = BrowserState.active_tab()
        regex = re.compile(url_pattern)
        matched: dict[str, Any] = {}
        from nodriver.cdp import network as cdp_network
        # CDP does NOT dispatch Network.responseReceived unless the Network
        # domain is enabled on this tab — without this the handler never fires
        # and the tool always (falsely) times out. Mirrors network_start /
        # network_get / auth_capture / wait_for_request. Idempotent.
        try:
            await tab.send(cdp_network.enable())
        except Exception:
            pass

        async def handler(event):
            if hasattr(event, "response") and regex.search(event.response.url):
                matched["url"] = event.response.url
                matched["status"] = event.response.status

        tab.add_handler(cdp_network.ResponseReceived, handler)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and "url" not in matched:
                await asyncio.sleep(0.2)
        finally:
            # Drop ONLY our handler. nodriver's remove_handler(evt, fn) deletes
            # the whole handler list for that event (clobbering a concurrent
            # network_start/auth_capture capture); filter the list directly.
            try:
                tab.handlers[cdp_network.ResponseReceived].remove(handler)
            except (KeyError, ValueError):
                pass
        if "url" in matched:
            return ok(f"response {matched['status']} {matched['url']}")
        return err("timeout")
    except Exception as e:
        return err(str(e))
