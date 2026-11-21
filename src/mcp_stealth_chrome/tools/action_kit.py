"""The action kit for agent workflows: describe_page, smart_fill, the assert_* checks,
storage_snapshot and storage_diff, workflow_run, detect_and_bypass, paste_text, auth_capture,
http_request_with_session, wait_for_request, click_and_wait and form_introspect.

Moved out of server.py unchanged (its sections 25 and 26). What code staying in server.py also uses
stays there and is reached here as _srv.<name> at call time: the workflow step table
(_WORKFLOW_TOOLS, _register_workflow_tools, which tools/batch.py shares), the in-flight request
tracker (_arm_inflight_tracker, _inflight_quiet, _add_all_handlers, which tools/devtools.py uses),
and the click and input helpers. get_url, evaluate_parts, release_group, detect_anti_bot and
http_request are read through server too, so a test that patches server.<name> reaches this code.
The named storage snapshots live here, and their session-gone hook is registered once, on import.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Literal, Optional, Union

from nodriver.cdp import runtime as cdp_runtime

from .. import presence
from .._app import mcp
from ..helpers import (
    BODY_REVEAL_NOTE, err, get_title, is_secret_name, mask_request_entry, ok, parse_json, reveal_mode,
)
from ..identity import key_event_params
from ..refs import new_object_group, resolve_ref
from ..state import PRIMARY_SESSION, SESSION_GONE_HOOKS, BrowserState
from ..workflow import WorkflowError, run_workflow
from .. import server as _srv

# Tools designed for AI-agent workflows: token-efficient page summaries,
# label-fuzzy form filling, NL vision targeting, verification primitives,
# state-diff debugging, resumable workflows, and one-shot detect+bypass.


# ── describe_page ──────────────────────────────────────────────────────────

_DESCRIBE_PAGE_JS = """
(() => {
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    return true;
  };
  // Best-effort label resolution: <label for=ID>, wrapping <label>, aria-label,
  // aria-labelledby, placeholder, name attribute. Returns first non-empty.
  const labelOf = (el) => {
    if (!el) return '';
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    if (el.getAttribute) {
      const al = el.getAttribute('aria-label');
      if (al) return al.trim();
      const alb = el.getAttribute('aria-labelledby');
      if (alb) {
        const ref = document.getElementById(alb);
        if (ref && ref.innerText) return ref.innerText.trim();
      }
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      const nm = el.getAttribute('name');
      if (nm) return nm.trim();
    }
    return '';
  };
  const fieldType = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select') return 'select';
    if (tag !== 'input') return tag;
    const t = (el.type || 'text').toLowerCase();
    return t;
  };
  // Form fields
  const fields = [];
  const inputs = [...document.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden');
  for (const el of inputs.slice(0, 60)) {
    const type = fieldType(el);
    let value;
    if (type === 'checkbox' || type === 'radio') value = !!el.checked;
    else if (type === 'select') value = el.value;
    else if (el.type === 'password') value = el.value ? '(password set, not shown)' : '';
    else value = el.value || '';
    fields.push({
      label: labelOf(el),
      type,
      required: !!el.required,
      value: typeof value === 'string' ? value.slice(0, 200) : value,
      name: el.name || null,
      id: el.id || null,
    });
  }
  // Buttons + submit-like actions
  const actions = [];
  const btns = [...document.querySelectorAll(
    'button, [role=button], input[type=submit], input[type=button], a[href][role=button]'
  )].filter(visible);
  for (const el of btns.slice(0, 30)) {
    const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (txt) actions.push({
      text: txt.slice(0, 80),
      kind: el.tagName.toLowerCase(),
      disabled: !!el.disabled,
    });
  }
  // Headings (page intent signal)
  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .filter(visible).slice(0, 10)
    .map(h => ({level: h.tagName.toLowerCase(), text: h.innerText.trim().slice(0, 120)}));
  // Visible error messages — common patterns
  const errors = [];
  const errorEls = [...document.querySelectorAll(
    '[role=alert], .error, .alert-error, .field-error, [class*="error" i]:not(button), [aria-invalid=true]'
  )].filter(visible).slice(0, 10);
  for (const el of errorEls) {
    const t = (el.innerText || '').trim();
    if (t && t.length < 300) errors.push(t);
  }
  // Top-level navigation links (max 12, same-origin only)
  const origin = location.origin;
  const navLinks = [];
  const navContainer = document.querySelector('nav, header, [role=navigation]');
  if (navContainer) {
    for (const a of navContainer.querySelectorAll('a[href]')) {
      if (!visible(a)) continue;
      const t = (a.innerText || '').trim();
      if (!t) continue;
      try {
        const u = new URL(a.href, origin);
        if (u.origin === origin) navLinks.push(t.slice(0, 60));
      } catch {}
      if (navLinks.length >= 12) break;
    }
  }
  return JSON.stringify({
    title: document.title || '',
    url: location.href,
    headings,
    fields,
    actions,
    errors,
    navigation: [...new Set(navLinks)],
  });
})()
"""


_WAIT_DOM_STABLE_JS = """
((max_ms, stable_ms) => new Promise((resolve) => {
  const start = Date.now();
  let last = start;
  let obs;
  try {
    obs = new MutationObserver(() => { last = Date.now(); });
    obs.observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, characterData: true,
    });
  } catch (e) { resolve('observer_failed'); return; }
  const tick = () => {
    const now = Date.now();
    if (now - last >= stable_ms) { obs.disconnect(); resolve('stable'); }
    else if (now - start >= max_ms) { obs.disconnect(); resolve('timeout'); }
    else setTimeout(tick, 80);
  };
  setTimeout(tick, 80);
}))
"""


@mcp.tool()
async def describe_page(
    wait_stable: bool = False,
    max_wait: float = 2.5,
    stable_ms: int = 400,
) -> str:
    """Compact page summary for an agent, in place of accessibility_snapshot: JSON with the page's intent and
    interactable surface in ~10× fewer tokens than a full a11y dump. Use it BEFORE smart_fill to learn which
    labels exist.

    Args:
        wait_stable: if True, wait (MutationObserver) until the DOM has been quiet for `stable_ms`, at most
            `max_wait` seconds, before reading: on SPA / lazy-rendered pages, to see the final state rather
            than a half-hydrated one. Typically ~50-200ms.
        max_wait: outer cap for the stability wait (default 2.5s)
        stable_ms: required quiet window in ms (default 400)

    Output shape:
      {"title": "...", "url": "...",
       "headings": [{"level":"h1","text":"..."}],
       "fields": [{"label":"...","type":"text|email|...","required":bool,"value":"...","name":"...","id":"..."}],
       "actions": [{"text":"Submit","kind":"button","disabled":false}],
       "errors": ["..."], "navigation": ["Dashboard","Settings",...],
       "stability": "stable|timeout|skipped"}  (stability only if wait_stable=True)
    """
    try:
        tab = BrowserState.active_tab()
        stability = "skipped"
        if wait_stable:
            try:
                stab_raw = await _srv._wait(
                    tab.evaluate(
                        f"{_WAIT_DOM_STABLE_JS}({int(max_wait * 1000)}, {int(stable_ms)})",
                        await_promise=True, return_by_value=True,
                    ),
                    timeout=max_wait + 2.0,
                    what="describe_page wait_stable",
                )
                stability = str(stab_raw.value if hasattr(stab_raw, "value")
                                 and not isinstance(stab_raw, str) else stab_raw) or "stable"
            except Exception:
                stability = "wait_failed"
        raw = await _srv._wait(
            tab.evaluate(_DESCRIBE_PAGE_JS, return_by_value=True),
            what="describe_page",
        )
        text = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        data = parse_json(str(text), {})
        if not isinstance(data, dict):
            return err("describe_page: failed to parse page summary")
        if wait_stable:
            data["stability"] = stability
        if not (data.get("headings") or data.get("fields") or data.get("actions")
                or data.get("navigation") or data.get("errors")):
            # A quiet DOM is not a rendered page: a single-page app whose script is still
            # loading is "stable" and empty, and the summary alone reads as a blank site.
            try:
                seen = await _srv._wait(tab.evaluate(
                    "JSON.stringify([document.readyState, "
                    "(document.body ? document.body.innerText.trim().length : 0)])",
                    return_by_value=True), timeout=3, what="describe_page empty check")
                ready, chars = parse_json(str(seen.value if hasattr(seen, "value")
                                              and not isinstance(seen, str) else seen), ["", 0])
            except Exception:
                ready, chars = "", None
            if not chars:
                data["note"] = (
                    "nothing is rendered yet"
                    + (f" (ready_state {ready})" if ready else "")
                    + ": no visible text, headings, fields or actions. A single-page app that "
                    "is still loading looks like this; wait_for(text=...) and call again."
                    if chars == 0 else
                    "no headings, fields or actions were found, and the page text could not be read"
                )
        return ok(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── smart_fill ─────────────────────────────────────────────────────────────

_SMART_FILL_FIND_JS = """
(label_query) => {
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const labelOf = (el) => {
    if (!el) return '';
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    const alb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (alb) {
      const ref = document.getElementById(alb);
      if (ref && ref.innerText) return ref.innerText.trim();
    }
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph) return ph.trim();
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return nm.trim();
    return '';
  };
  const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const want = norm(label_query);
  // Score candidates: exact > prefix > substring > token-overlap
  const inputs = [...document.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden' && !el.disabled && !el.readOnly);
  const scored = [];
  const candidates = [];
  for (const el of inputs) {
    const lbl = labelOf(el);
    const lblN = norm(lbl);
    candidates.push(lbl);
    if (!lblN) continue;
    let score = 0;
    if (lblN === want) score = 1000;
    else if (lblN.startsWith(want)) score = 700;
    else if (lblN.includes(want)) score = 500;
    else {
      const tw = want.split(' ').filter(Boolean);
      const lw = lblN.split(' ').filter(Boolean);
      const overlap = tw.filter(t => lw.includes(t)).length;
      if (overlap > 0) score = 100 + overlap * 50;
    }
    if (score > 0) {
      scored.push({score, label: lbl, el, type: el.tagName.toLowerCase(),
        input_type: (el.type || '').toLowerCase()});
    }
  }
  scored.sort((a, b) => b.score - a.score);
  const top = scored[0] || null;
  let best = null;
  if (top) {
    // Measure only the winner, after bringing it on screen: a point outside the
    // viewport would send the mouse click nowhere.
    top.el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    const rect = top.el.getBoundingClientRect();
    best = {score: top.score, label: top.label, type: top.type, input_type: top.input_type,
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2)};
  }
  return {json: JSON.stringify({best, candidates: [...new Set(candidates)].filter(Boolean).slice(0, 30)}),
          el: top ? top.el : null};
}
"""


@mcp.tool()
async def smart_fill(fields: dict, submit_label: Optional[str] = None) -> str:
    """Fill form fields by label text (fuzzy match). LLM-friendly alt to
    fill_form which requires DOM refs.

    Args:
        fields: {"Label": "value", ...} — keys match form field labels
            (case-insensitive, fuzzy: exact > prefix > substring > token).
            Labels resolved from <label>, aria-label, placeholder, name.
        submit_label: optional button text to click after filling
            (e.g., "Create", "Sign in"). Fuzzy-matched on action button text.

    Behavior:
        - Each field: locates input → focus → clear → type value
        - Returns per-field result + list of available labels if missing
        - On miss: error includes candidates so the LLM can retry with
          the correct label name.
    """
    try:
        tab = BrowserState.active_tab()
        if not isinstance(fields, dict):
            return err("smart_fill: fields must be a dict {label: value}")
        if not fields:
            return err("smart_fill: no fields given")
        results: list[dict] = []
        for label, value in fields.items():
            if not isinstance(label, str):
                continue
            group = new_object_group()
            parts = await _srv._wait(
                _srv.evaluate_parts(tab, f"({_SMART_FILL_FIND_JS})({json.dumps(label)})", group),
                what=f"smart_fill find '{label}'",
            )
            data = parse_json(parts["json"].value if "json" in parts else "", {})
            best = data.get("best") if isinstance(data, dict) else None
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            if not best:
                results.append({
                    "label": label, "ok": False,
                    "error": "no field matched",
                    "did_you_mean": candidates[:10],
                })
                await _srv.release_group(tab, group)
                continue
            # Click into the field then type. Using mouse_click + JS fill is
            # more reliable than .focus() across React/Vue components that
            # listen for native events.
            try:
                # Not for a <select>: clicking one opens a native popup that stays open and
                # takes the next click, and its value is set directly below.
                if best.get("type") != "select":
                    await _srv._dispatch_input(tab, tab.mouse_click(int(best["x"]), int(best["y"])))
                    await asyncio.sleep(0.1)
                # Native value setter so React/Vue see the change. Each element type has its
                # own: HTMLInputElement's throws on a <select> or <textarea>.
                setter_js = f"""
                function() {{
                  const wanted = {json.dumps(str(value))};
                  if (this.tagName === 'SELECT') {{
                    const options = [...this.options];
                    const lower = wanted.trim().toLowerCase();
                    const option = options.find(o => o.value === wanted)
                      || options.find(o => o.text.trim().toLowerCase() === lower);
                    if (!option) return this.value;
                    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')
                      .set.call(this, option.value);
                    this.dispatchEvent(new Event('input', {{bubbles: true}}));
                    this.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return this.value === option.value ? wanted : this.value;
                  }}
                  const proto = this.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                  // A setter ignores maxlength, which typing cannot exceed.
                  const text = this.maxLength >= 0 ? wanted.slice(0, this.maxLength) : wanted;
                  Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, text);
                  this.dispatchEvent(new Event('input', {{bubbles: true}}));
                  this.dispatchEvent(new Event('change', {{bubbles: true}}));
                  return this.value;
                }}
                """
                remote, exc = await _srv._wait(tab.send(cdp_runtime.call_function_on(
                    setter_js, object_id=parts["el"].object_id, return_by_value=True,
                )), what=f"smart_fill set '{label}'")
                if exc is not None:
                    raise RuntimeError(exc.text or "value setter threw")
                actual = remote.value
                if str(actual) != str(value):
                    kind, _ = await _srv._wait(tab.send(cdp_runtime.call_function_on(
                        "function() { return this.type; }", object_id=parts["el"].object_id,
                        return_by_value=True,
                    )), what=f"smart_fill read '{label}'")
                    secret = kind.value == "password" or is_secret_name(label)
                    reads = f"{len(str(actual))} chars" if secret else repr(str(actual)[:80])
                    results.append({
                        "label": label, "matched": best["label"], "ok": False,
                        "error": f"field reads {reads} after setting it",
                    })
                    continue
                results.append({"label": label, "matched": best["label"], "ok": True})
            except Exception as e:
                results.append({"label": label, "ok": False, "error": str(e)})
            finally:
                await _srv.release_group(tab, group)
        any_failed = any(not r.get("ok") for r in results)
        # Optional submit
        submit_result = None
        if submit_label and any_failed:
            # Submitting a half-filled form is an action the caller did not ask for.
            submit_result = "NOT clicked: a field above failed"
        elif submit_label:
            try:
                # Reuse click_text fuzzy logic
                r = str(await _srv.click_text(submit_label, exact=False))
                submit_result = r[:200]
                any_failed = r.startswith("Error:")
            except Exception as e:
                submit_result = f"submit failed: {e}"
                any_failed = True
        out = {"results": results}
        if submit_result is not None:
            out["submit"] = submit_result
        return (err if any_failed else ok)(json.dumps(out, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── assert_* primitives ────────────────────────────────────────────────────


@mcp.tool()
async def assert_text_present(text: str, timeout: float = 5.0) -> str:
    """Verify text appears anywhere on page within timeout.
    Returns ok(found) or err(not found + sample of body text)."""
    try:
        tab = BrowserState.active_tab()
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_body = ""
        while asyncio.get_event_loop().time() < deadline:
            raw = await _srv._wait(tab.evaluate(
                f"(() => {{ const t = (document.body && document.body.innerText) || ''; "
                f"return JSON.stringify({{found: t.includes({json.dumps(text)}), sample: t.slice(0, 500)}}); }})()",
                return_by_value=True,
            ), what="assert_text_present")
            txt = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
            data = parse_json(str(txt), {})
            if isinstance(data, dict):
                if data.get("found"):
                    return ok(f"text present: {text!r}")
                last_body = data.get("sample", "")
            await asyncio.sleep(0.4)
        return err(
            f"text not found within {timeout}s: {text!r}\nbody sample: {last_body[:300]}"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def assert_url_matches(pattern: str, timeout: float = 5.0) -> str:
    """Verify current URL matches regex within timeout.
    Returns ok(current_url) or err(timeout + last_url)."""
    import re as _re
    try:
        tab = BrowserState.active_tab()
        try:
            rx = _re.compile(pattern)
        except _re.error as e:
            return err(f"invalid regex: {e}")
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_url = ""
        while asyncio.get_event_loop().time() < deadline:
            last_url = await _srv.get_url(tab)
            if rx.search(last_url):
                return ok(f"url matches {pattern!r}: {last_url}")
            await asyncio.sleep(0.3)
        return err(f"url did not match {pattern!r} within {timeout}s. last: {last_url}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def assert_element_visible(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    timeout: float = 5.0,
) -> str:
    """Verify an element is visible (rendered, non-zero size, not hidden).
    Pass selector OR text — text uses fuzzy contains match.
    Returns ok(rect) or err(timeout)."""
    if not selector and not text:
        return err("pass selector= or text=")
    try:
        tab = BrowserState.active_tab()
        if selector:
            # An invalid selector can never become visible; waiting would misreport it.
            await _srv._eval_checked(tab, f"document.querySelector({json.dumps(selector)}) && 1")
        deadline = asyncio.get_event_loop().time() + max(0.5, timeout)
        last_state = "not found"
        while asyncio.get_event_loop().time() < deadline:
            if selector:
                expr = f"""
                (() => {{
                  const el = document.querySelector({json.dumps(selector)});
                  if (!el) return JSON.stringify({{state: 'not_found'}});
                  const r = el.getBoundingClientRect();
                  const cs = getComputedStyle(el);
                  const visible = r.width > 0 && r.height > 0 &&
                    cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
                  return JSON.stringify({{state: visible ? 'visible' : 'hidden',
                    rect: {{x: Math.round(r.left), y: Math.round(r.top),
                            width: Math.round(r.width), height: Math.round(r.height)}}}});
                }})()
                """
            else:
                expr = f"""
                (() => {{
                  const want = {json.dumps(text)}.toLowerCase();
                  const candidates = [...document.querySelectorAll('button, a, [role=button], h1,h2,h3, span, div, label, p')];
                  for (const el of candidates) {{
                    const t = (el.innerText || '').toLowerCase().trim();
                    if (!t.includes(want)) continue;
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    const visible = r.width > 0 && r.height > 0 &&
                      cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
                    if (visible) return JSON.stringify({{state: 'visible', tag: el.tagName.toLowerCase(),
                      rect: {{x: Math.round(r.left), y: Math.round(r.top),
                              width: Math.round(r.width), height: Math.round(r.height)}}}});
                  }}
                  return JSON.stringify({{state: 'not_found'}});
                }})()
                """
            raw = await _srv._wait(tab.evaluate(expr, return_by_value=True),
                              what="assert_element_visible")
            txt = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
            data = parse_json(str(txt), {})
            if isinstance(data, dict):
                last_state = data.get("state", "not found")
                if last_state == "visible":
                    return ok(json.dumps(data, ensure_ascii=False))
            await asyncio.sleep(0.3)
        return err(f"element not visible within {timeout}s (last_state={last_state})")
    except Exception as e:
        return err(str(e))


# ── storage_diff ───────────────────────────────────────────────────────────

# In-memory snapshots keyed by user-supplied name. Lives only for current
# MCP session (not persisted across launches — use storage_state_save for that).
_STORAGE_SNAPSHOTS: dict[Any, dict] = {}


def _snapshot_key(name: str) -> Any:
    """Names are per session: diffing against another session's snapshot would blame the
    action under test for the difference between two browsers. A tuple, because a joined
    string lets the primary session name a snapshot "session-2::x" and reach session-2's.
    """
    key = BrowserState.session_key()
    return name if key == PRIMARY_SESSION else (key, name)


def _forget_session_snapshots(session: str) -> None:
    for name in list(_STORAGE_SNAPSHOTS):
        owned = isinstance(name, str) if session == PRIMARY_SESSION else (
            isinstance(name, tuple) and name[0] == session)
        if owned:
            _STORAGE_SNAPSHOTS.pop(name, None)


SESSION_GONE_HOOKS.append(_forget_session_snapshots)


def _own_snapshot_names() -> list[str]:
    key = BrowserState.session_key()
    if key == PRIMARY_SESSION:
        return [n for n in _STORAGE_SNAPSHOTS if isinstance(n, str)]
    return [n[1] for n in _STORAGE_SNAPSHOTS if isinstance(n, tuple) and n[0] == key]


async def _take_storage_snapshot(tab) -> dict:
    """Capture cookies + localStorage + sessionStorage + url + title.

    Every area records whether it was actually MEASURED. An unreadable area used
    to fall back to its empty default, so a snapshot that failed to read cookies
    was indistinguishable from a page that genuinely had none — and storage_diff
    would then report every real cookie as "removed". Failures are collected in
    `errors` and the area is marked unmeasured instead of being invented.
    """
    snap: dict = {"url": "", "title": "", "cookies": [],
                   "localStorage": {}, "sessionStorage": {},
                   "errors": {}, "measured": {
                       "url": False, "cookies": False,
                       "localStorage": False, "sessionStorage": False,
                   }}
    try:
        snap["url"] = await _srv.get_url(tab, strict=True)
        snap["title"] = await get_title(tab, strict=True)
        snap["measured"]["url"] = True
    except Exception as e:
        snap["errors"]["url"] = f"{type(e).__name__}: {e}"
    try:
        from nodriver.cdp import network as cdp_network
        cookies = await _srv._wait(tab.send(cdp_network.get_cookies()),
                              what="storage_snapshot cookies")
        snap["cookies"] = [{
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", ""),
            "domain": getattr(c, "domain", ""),
            "path": getattr(c, "path", "/"),
        } for c in (cookies or [])]
        snap["measured"]["cookies"] = True
    except Exception as e:
        snap["errors"]["cookies"] = f"{type(e).__name__}: {e}"
    for area in ("localStorage", "sessionStorage"):
        try:
            raw = await _srv._wait(tab.evaluate(
                f"JSON.stringify(Object.fromEntries(Object.entries({area})))",
                return_by_value=True,
            ), what=f"storage_snapshot {area}")
            value = str(raw.value if hasattr(raw, "value") else raw)
            parsed = parse_json(value, None)
            if not isinstance(parsed, dict):
                # Storage is often blocked outright (file://, sandboxed frames,
                # third-party cookie policy). That is a real answer, not "empty".
                raise ValueError(f"unparseable {area} payload")
            snap[area] = parsed
            snap["measured"][area] = True
        except Exception as e:
            snap["errors"][area] = f"{type(e).__name__}: {e}"
    return snap


@mcp.tool()
async def storage_snapshot(name: str = "default") -> str:
    """Capture cookies + localStorage + sessionStorage + URL into a named slot
    for later diffing. Use BEFORE an action you want to inspect.

    Areas that could not be read are reported as unmeasured rather than empty:
    a snapshot that silently failed to read cookies would make storage_diff
    announce every existing cookie as "removed" by the action under test.
    """
    try:
        tab = BrowserState.active_tab()
        snap = await _take_storage_snapshot(tab)
        _STORAGE_SNAPSHOTS[_snapshot_key(name)] = snap
        if len(_STORAGE_SNAPSHOTS) > _srv._STORAGE_SNAPSHOT_LIMIT:
            for stale in list(_STORAGE_SNAPSHOTS)[:-_srv._STORAGE_SNAPSHOT_LIMIT]:
                _STORAGE_SNAPSHOTS.pop(stale, None)

        def _area(label: str, count: int) -> str:
            return f"{count} {label}" if snap["measured"].get(
                "cookies" if label == "cookies" else label) else f"{label} UNMEASURED"

        parts = [
            _area("cookies", len(snap["cookies"])),
            _area("localStorage", len(snap["localStorage"])),
            _area("sessionStorage", len(snap["sessionStorage"])),
        ]
        line = f"snapshot '{name}': {', '.join(parts)} at {snap['url'] or '(url unmeasured)'}"
        if snap["errors"]:
            return ok(line + "\nUNMEASURED areas (values below are NOT zero, they "
                             "are unknown): " + json.dumps(snap["errors"]))
        return ok(line)
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def storage_diff(name: str = "default", reveal_values: bool = False) -> str:
    """Compare current state vs an earlier storage_snapshot. Returns JSON
    showing what changed (added/removed/modified per area + url change).

    Diffing across a login is exactly when new values are session tokens, and this
    output lands in the transcript. So values read "*** (N chars)" unless
    reveal_values=True; whether something changed is reported either way.

    Pattern:
      storage_snapshot("before")
      <do an action e.g. click login>
      storage_diff("before")  → returns what the action actually changed
    """
    try:
        if _snapshot_key(name) not in _STORAGE_SNAPSHOTS:
            return err(
                f"no snapshot named {name!r}. Available: "
                f"{_own_snapshot_names() or '(none)'}"
            )
        tab = BrowserState.active_tab()
        before = _STORAGE_SNAPSHOTS[_snapshot_key(name)]
        after = await _take_storage_snapshot(tab)

        def _shown(value: Any, cap: int = 200) -> Any:
            if reveal_values:
                return value[:cap] if isinstance(value, str) else value
            return f"*** ({len(str(value or ''))} chars)"

        def _kvdiff(b: dict, a: dict) -> dict:
            keys_b, keys_a = set(b), set(a)
            return {
                "added": {k: _shown(a[k]) for k in keys_a - keys_b},
                "removed": list(keys_b - keys_a),
                "modified": {k: {"before": _shown(b[k]), "after": _shown(a[k])}
                              for k in keys_b & keys_a if b[k] != a[k]},
            }

        # Cookies — by (name, domain) tuple
        # Path is part of a cookie's identity: SID at /admin and SID at / are two cookies,
        # and keying without it let one overwrite the other, hiding a change.
        b_cookies = {(c["name"], c["domain"], c.get("path", "/")): c["value"]
                     for c in before["cookies"]}
        a_cookies = {(c["name"], c["domain"], c.get("path", "/")): c["value"]
                     for c in after["cookies"]}
        cookie_diff = {
            "added": [{"name": n, "domain": d, "path": p, "value": _shown(a_cookies[(n, d, p)], 60)}
                       for (n, d, p) in set(a_cookies) - set(b_cookies)],
            "removed": [{"name": n, "domain": d, "path": p}
                        for (n, d, p) in set(b_cookies) - set(a_cookies)],
            "modified": [{"name": n, "domain": d, "path": p}
                          for (n, d, p) in set(b_cookies) & set(a_cookies)
                          if b_cookies[(n, d, p)] != a_cookies[(n, d, p)]],
        }

        diff = {
            "url_changed": (before["url"] != after["url"]),
            "url_before": before["url"],
            "url_after": after["url"],
            "title_changed": (before["title"] != after["title"]),
            "cookies": cookie_diff,
            "localStorage": _kvdiff(before["localStorage"], after["localStorage"]),
            "sessionStorage": _kvdiff(before["sessionStorage"], after["sessionStorage"]),
        }
        # An area that could not be read on either side produces a diff that
        # LOOKS decisive and is not: unreadable cookies before + readable after
        # renders every existing cookie as "added" by the action under test.
        # Name those areas so the numbers are not taken at face value.
        unreliable = sorted({
            area
            for side in (before, after)
            for area, was_measured in (side.get("measured") or {}).items()
            if not was_measured
        })
        if unreliable:
            diff["unreliable_areas"] = unreliable
            diff["warning"] = (
                "These areas were NOT measured on at least one side, so their "
                "diff is meaningless — differences shown for them reflect a "
                "failed read, not a change made by the action."
            )
            diff["measurement_errors"] = {
                "before": before.get("errors") or {},
                "after": after.get("errors") or {},
            }
        return ok(json.dumps(diff, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ── workflow_run (resumable) ───────────────────────────────────────────────

@mcp.tool()
async def workflow_run(
    steps: list[dict],
    start_at: int | str = 0,
    stop_on_error: bool = True,
    vars: Optional[dict] = None,
) -> str:
    """Run many tool steps in one call, with branching, loops and earlier results.

    A tool step: {"tool": "fill", "args": {...}, "save_as": "x", "label": "...", "continue_on_error": false}

    save_as stores a step's result, parsed as JSON when it is JSON. Any string in args may then reference it
    as ${x}, ${x.field} or ${x[0].name}; a string that is exactly one reference keeps its type. ${last.ok},
    ${last.value} and ${last.text} describe the previous step. A reference to something undefined fails the
    step and lists what is defined.

    Loop: {"foreach": "${rows}", "as": "row", "steps": [...]} sets ${row} and ${row_index} for each item.

    Branch: {"if": <condition>, "then": [...], "else": [...]}. Conditions are {"value": "${x}", "<op>": y}
    with op one of equals, not_equals, contains, not_contains, matches (regex), gt, gte, lt, lte, truthy,
    falsy; or {"exists": "${x.y}"}, {"all": [...]}, {"any": [...]}, {"not": {...}}. There is no expression
    language and nothing is evaluated as code on the server.

    Example, submitting a form once per row of data taken from the page:
      [{"tool": "evaluate", "args": {"expression": "JSON.stringify(window.rows)"}, "save_as": "rows"},
       {"foreach": "${rows}", "as": "row", "steps": [
         {"tool": "fill", "args": {"selector": "#name", "value": "${row.name}"}},
         {"if": {"value": "${row.active}", "truthy": true}, "then": [
           {"tool": "check", "args": {"selector": "#active"}}]},
         {"tool": "click", "args": {"selector": "#save"}}]}]

    Args:
        steps: the steps above
        start_at: where to begin: a top-level index, or a dotted path from a failure report ("3.2.1" is step
            3, item 2 of its loop, inner step 1). One that does not exist is refused.
        stop_on_error: stop at the first failure (default). If False, keep going and report every result.
        vars: starting variables. Pass the `vars` of a failure report when resuming: nothing is kept between
            calls.

    Returns JSON with completed (capped at 200 entries, the rest counted), total and vars; on failure also
    failed_at, failed_path, failure_context and resume_with. With stop_on_error=False, failed_count and
    failed_paths list every failure even past that cap. Steps the failed step depended on are not re-run on
    resume, so check that any state they left on the page still holds.

    Tools: navigate, reload, go_back/forward, click, click_text, click_role, fill, fill_form, type_text,
    paste_text, press_key, select_option, check, uncheck, hover, scroll, scroll_to, mouse_click_xy,
    smart_fill, wait_for*, screenshot, evaluate, get_text, get_attribute, get_url, get_links, get_html,
    search_page, query_selector_all, extract_table, extract_structured, browser_snapshot, describe_page,
    vision_locate, assert_*, storage_*, cookie_import, storage_state_load.
    """
    _srv._register_workflow_tools()
    steps = parse_json(steps, None) if isinstance(steps, str) else steps
    if not isinstance(steps, list):
        return err("steps must be a list of dicts")

    async def dispatch(tool: str, args: dict) -> Any:
        # Each step meets the checks a call from the client meets, Take over included: the
        # person may take the browser between two steps.
        refused = _srv._inner_arguments_error(tool, args) or presence.refusal(tool, BrowserState.browser)
        if refused:
            return refused
        return await _srv._WORKFLOW_TOOLS[tool](**args)

    try:
        result = await run_workflow(
            steps, dispatch, lambda name: name in _srv._WORKFLOW_TOOLS,
            start_at=start_at, stop_on_error=stop_on_error, variables=vars,
        )
    except WorkflowError as e:
        return err(str(e))
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    return err(text) if "failed_at" in result else ok(text)


# ── detect_and_bypass ──────────────────────────────────────────────────────


@mcp.tool()
async def detect_and_bypass() -> str:
    """One-shot: detect anti-bot wall on current page and apply the best
    bypass we have. Returns JSON with detection + bypass result.

    Bypass routing:
      - Cloudflare Turnstile / interstitial → _auto_verify_cf (DOM + OpenCV)
      - Other walls (DataDome, PerimeterX, Akamai, Imperva, Kasada) →
        return detection + recommended-action list (no auto-bypass since
        those need session reuse / proxies / paid solvers).
      - No wall detected → returns ok with empty bypass.
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        tab = BrowserState.active_tab()
        # Reuse detect_anti_bot's classification
        det_str = await _srv.detect_anti_bot()
        det_text = str(det_str)
        # Strip the "ok: " prefix if present
        if det_text.startswith("Error:"):
            return det_text  # propagate
        # Detect specific systems by inspecting the response text — cheaper
        # than re-running the JS probes.
        det_lower = det_text.lower()
        result: dict = {"detection": det_text[:1000], "bypassed": False, "method": None,
                         "next_steps": []}
        if "cloudflare" in det_lower:
            # Try our automated CF Turnstile flow.
            verify = await _srv._auto_verify_cf(tab, max_attempts=2)
            if verify:
                result["method"] = "auto_verify_cf"
                result["details"] = verify.strip()
                # The page decides. Every outcome of _auto_verify_cf, including "no clickable
                # widget found", says auto-verify, and a click attempt is not a solved wall.
                still = await _srv._cf_challenge_state(tab, active=True)
                result["challenge_still_present"] = still  # None: could not be checked
                result["bypassed"] = still is False and "no clickable widget" not in verify
            if not result["bypassed"]:
                result["next_steps"].append(
                    "If still blocked: solve_captcha(kind='turnstile', ...) with CAPSOLVER_KEY, "
                    "or storage_state_load a previously-saved session."
                )
        if "datadome" in det_lower:
            result["next_steps"].append(
                "DataDome: no automated bypass. Use mouse_record→mouse_replay of a real session, "
                "session_warmup, residential proxy, and storage_state reuse."
            )
        # Match only "perimeterx" — detect_anti_bot always labels this vendor
        # "PerimeterX/HUMAN", so the bare "human" token only added DataDome
        # false positives (its message mentions "a real session" → "human"-free,
        # but other detections containing the substring would mis-fire).
        if "perimeterx" in det_lower:
            result["next_steps"].append(
                "PerimeterX/HUMAN: storage_state_load is most reliable. New sessions need "
                "mobile proxy + click(humanize=True)."
            )
        if "akamai" in det_lower:
            result["next_steps"].append(
                "Akamai: requires _abck cookie sensor data — solve_captcha(kind='akamai', ...) "
                "if your provider supports it, or session reuse."
            )
        if "imperva" in det_lower or "kasada" in det_lower:
            result["next_steps"].append(
                "Imperva/Kasada: paid solver via solve_captcha or session reuse."
            )
        if "(no anti-bot system detected)" in det_lower or "none detected" in det_lower:
            return ok(json.dumps(
                # Nothing was bypassed, because nothing was in the way.
                {"detection": "no anti-bot detected", "bypassed": False, "needed": False,
                 "method": "n/a", "next_steps": []},
                indent=2, ensure_ascii=False,
            ))
        return ok(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 26. NETWORK + AUTH + WAIT — paste, click_and_wait, auth_capture,
#     http_request_with_session, wait_for_request, form_introspect
# ══════════════════════════════════════════════════════════════════════════
#
# Closes gaps surfaced by real-world automation: response body retrieval
# on captured requests, modern-framework paste-event handlers, browser
# session bridge for authenticated API calls, and click outcome
# disambiguation.


# ── paste_text ─────────────────────────────────────────────────────────────


@mcp.tool()
async def paste_text(
    text: str,
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    submit: bool = False,
) -> str:
    """Set field value by simulating a real paste event sequence.

    Use when fill / type_text don't register on modern frameworks (SolidJS
    runes, Svelte 5 runes, some Qwik forms) that ONLY listen for paste
    events or beforeinput with inputType:'insertFromPaste'.

    Sequence dispatched (mimics a real Cmd-V):
      1. focus
      2. ClipboardEvent('paste', {clipboardData: 'text/plain': text})
      3. InputEvent('beforeinput', {inputType: 'insertFromPaste', data: text})
      4. native value setter (HTMLInputElement / HTMLTextAreaElement)
      5. InputEvent('input', {inputType: 'insertFromPaste', data: text})
      6. Event('change')

    Args:
        text: value to paste
        ref: element ref from browser_snapshot
        selector: CSS selector
        submit: if True, simulate Enter keypress after paste
    """
    try:
        tab = BrowserState.active_tab()
        if ref:
            el = await resolve_ref(ref)
        elif selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err("paste failed: element not found")
        else:
            return err("paste_text: pass ref= or selector=")

        # Single-shot JS that does the full event dance — keeps timing tight.
        result_raw = await _srv._wait(el.apply(
            f"""
            function() {{
              const el = this;
              const text = {json.dumps(text)};
              const noText = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'color', 'range'];
              const editable = el.isContentEditable;
              const isField = el.tagName === 'TEXTAREA'
                || (el.tagName === 'INPUT' && !noText.includes(el.type));
              if (!isField && !editable) return JSON.stringify({{ok: false,
                error: '<' + el.tagName.toLowerCase() + '> does not take text'}});
              if (el.disabled) return JSON.stringify({{ok: false, error: 'the field is disabled'}});
              if (el.readOnly) return JSON.stringify({{ok: false, error: 'the field is read-only'}});
              try {{ el.focus(); }} catch (e) {{}}
              // 1. Paste event with DataTransfer
              try {{
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const pasteEv = new ClipboardEvent('paste', {{
                  clipboardData: dt, bubbles: true, cancelable: true,
                }});
                el.dispatchEvent(pasteEv);
              }} catch (e) {{}}
              // 2. beforeinput
              try {{
                const beforeEv = new InputEvent('beforeinput', {{
                  inputType: 'insertFromPaste', data: text,
                  bubbles: true, cancelable: true,
                }});
                el.dispatchEvent(beforeEv);
              }} catch (e) {{}}
              // 3. Native value setter so React/Vue/Solid see the change
              if (editable) {{
                // A contenteditable has no value to set: the text goes in at the selection.
                const range = document.createRange();
                range.selectNodeContents(el);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                if (!document.execCommand('insertText', false, text)) el.textContent = text;
              }} else try {{
                const proto = el.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, text);
              }} catch (e) {{ el.value = text; }}
              // 4. input event
              try {{
                el.dispatchEvent(new InputEvent('input', {{
                  inputType: 'insertFromPaste', data: text, bubbles: true,
                }}));
              }} catch (e) {{
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
              }}
              // 5. change event for blur-validators
              el.dispatchEvent(new Event('change', {{bubbles: true}}));
              const landed = editable ? el.textContent : el.value;
              const unify = (v) => v.split('\\r\\n').join('\\n').split('\\r').join('\\n');
              if (unify(landed) !== unify(text)) {{
                return JSON.stringify({{ok: false, error: 'the field reads ' + landed.length
                  + ' chars after the paste, not the ' + text.length + ' sent (mask, maxlength or framework state)'}});
              }}
              return JSON.stringify({{ok: true, value:
                el.type === 'password' ? `(password, ${{landed.length}} chars)` : landed}});
            }}
            """,
        ), what="paste_text")
        if isinstance(result_raw, cdp_runtime.ExceptionDetails):
            return err(f"paste failed: {_srv._js_exception_text(result_raw)}")
        result_text = result_raw.value if hasattr(result_raw, "value") and not isinstance(result_raw, str) else result_raw
        data = parse_json(str(result_text), {})
        if not isinstance(data, dict) or not data.get("ok"):
            return err(f"paste failed: {data.get('error') if isinstance(data, dict) else result_text}")
        if submit:
            try:
                await _srv._dispatch_input(tab, _srv._dispatch_key(tab, key_event_params("Enter")))
            except Exception:
                with contextlib.suppress(Exception):
                    await el.apply("function() { this.form?.requestSubmit?.(); }")
        return ok(f"pasted {len(text)} chars; field value={str(data.get('value', ''))[:80]}")
    except Exception as e:
        return err(str(e))


# ── auth_capture ───────────────────────────────────────────────────────────


@mcp.tool()
async def auth_capture(
    filter_url_pattern: str,
    count: int = 1,
    timeout: float = 10.0,
    include_response_headers: bool = False,
    reveal_values: Union[bool, Literal["body"]] = False,
) -> str:
    """Intercept the next N requests whose URL contains a pattern and return their headers (Authorization,
    X-CSRF-*, etc.): for SPAs that hold bearer tokens in JS memory, never in localStorage. Values are hidden
    unless reveal_values=True (tool output lands in the transcript). After network_start,
    http_request_with_session reuses a token seen on the same origin without showing it.

    Args:
        filter_url_pattern: case-sensitive URL substring, e.g. "/api/" or "graphql" (for regex, network_get)
        count: stop capturing after this many matches (default 1)
        timeout: max seconds to wait (default 10)
        include_response_headers: also wait for and return response headers
        reveal_values: return credential values as they are. "body" shows request body values under a
            secret-named parent and keeps headers, URL secrets and body values whose own key is secret-named
            hidden; the reply is then {reveal_values, note, entries}.

    Returns JSON array of {url, method, request_headers, request_body, [response_headers, status]}.
    """
    try:
        reveal = reveal_mode(reveal_values)
    except ValueError as exc:
        return err(f"auth_capture: {exc}; nothing was captured")
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_network
        try:
            await tab.send(cdp_network.enable())
        except Exception:
            pass
        captured: list[dict] = []
        pending_response: dict[str, dict] = {}
        done = asyncio.Event()
        active = {"flag": True}

        async def on_req(event):
            if not active["flag"]:
                return
            try:
                req = event.request
                url = getattr(req, "url", "")
                if filter_url_pattern not in url:
                    return
                rid = str(event.request_id)
                entry = {
                    "url": url,
                    "method": getattr(req, "method", ""),
                    "request_headers": dict(getattr(req, "headers", None) or {}),
                    "request_body": getattr(req, "post_data", None),
                }
                if include_response_headers:
                    pending_response[rid] = entry
                else:
                    captured.append(entry)
                    if len(captured) >= count:
                        active["flag"] = False
                        done.set()
            except Exception:
                pass

        async def on_res(event):
            if not active["flag"]:
                return
            try:
                rid = str(event.request_id)
                if rid not in pending_response:
                    return
                entry = pending_response.pop(rid)
                resp = event.response
                entry["status"] = getattr(resp, "status", None)
                entry["response_headers"] = dict(getattr(resp, "headers", None) or {})
                captured.append(entry)
                if len(captured) >= count:
                    active["flag"] = False
                    done.set()
            except Exception:
                pass

        tab.add_handler(cdp_network.RequestWillBeSent, on_req)
        if include_response_headers:
            tab.add_handler(cdp_network.ResponseReceived, on_res)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            active["flag"] = False
            # Unregister our own closures (filter the list; nodriver's
            # remove_handler would drop the whole event's handlers).
            for _evt, _fn in ((cdp_network.RequestWillBeSent, on_req),
                              (cdp_network.ResponseReceived, on_res)):
                try:
                    tab.handlers[_evt].remove(_fn)
                except (KeyError, ValueError):
                    pass
        if not captured:
            return err(
                f"auth_capture: no requests matched {filter_url_pattern!r} within {timeout}s"
            )
        if reveal != "all":
            captured = [mask_request_entry(c, body_mode=reveal == "body") for c in captured]
        return ok(_srv._bounded_entries(captured, _srv._BODY_MODE_EXTRA if reveal == "body" else None))
    except Exception as e:
        return err(str(e))


# ── http_request_with_session ──────────────────────────────────────────────


def _latest_auth_header_for(url: str) -> Optional[str]:
    """Pick the most recent Authorization header from network_index for requests to the
    target's exact origin. Used by http_request_with_session as a fallback when the
    caller doesn't pass headers explicitly.

    Origin, not host: a token seen on https://x must not travel to http://x:8080 in
    plaintext, or to another service on that host."""
    try:
        from urllib.parse import urlparse

        def _origin(address: str) -> tuple:
            parts = urlparse(address)
            port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
            return parts.scheme, parts.hostname or "", port

        target = _origin(url)
        if not target[1]:
            return None
        # Iterate in insertion order — index is dict keyed by request_id and
        # Python dicts preserve insertion order, so the last matching entry
        # is the freshest.
        latest = None
        for entry in BrowserState.network_index.values():
            if _origin(entry.get("url", "")) != target:
                continue
            headers = entry.get("request_headers") or {}
            for k, v in headers.items():
                if k.lower() == "authorization" and v:
                    latest = str(v)
                    break
        return latest
    except Exception:
        return None


@mcp.tool()
async def http_request_with_session(
    url: str,
    method: str = "GET",
    json_body: Optional[dict] = None,
    data: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    auth_header: Optional[str] = None,
    impersonate: str = "chrome",
    return_mode: str = "auto",
    timeout: float = 30.0,
    wait_timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    reveal_values: bool = False,
) -> str:
    """Authenticated HTTP request on the BROWSER's session: cookies from the active tab
    (use_browser_cookies=True in http_request) plus an Authorization header, auth_header or the one from
    network_index (the most recent same-host request captured via network_start). A page that holds its bearer
    token in JS memory only emits it after navigate or an interaction, so call network_start once at session
    begin.

    Args:
        url, method, json_body, data, impersonate, return_mode, timeout, wait_timeout, connect_timeout: same as
            http_request, including the tracked lifecycle: a slow call hands back a request_id for
            http_request_status
        extra_headers: merged on top of auto-detected ones
        auth_header: explicit bearer / basic value, e.g. "Bearer eyJ..."

    Returns same shape as http_request.
    """
    headers = dict(extra_headers or {})
    if auth_header:
        headers.setdefault("Authorization", auth_header)
    elif not any(k.lower() == "authorization" for k in headers):
        # Case-fold BOTH sides. Comparing "Authorization" against a lowercased
        # key set never matched, so a caller who passed their own Authorization
        # in extra_headers had it silently overwritten by the sniffed token.
        candidate = _latest_auth_header_for(url)
        if candidate:
            headers["Authorization"] = candidate
    if not headers and not BrowserState.network_index:
        # Soft warning — still useful for cookie-only auth
        pass
    return await _srv.http_request(
        url=url, method=method, impersonate=impersonate,
        use_browser_cookies=True, headers=headers,
        data=data, json_body=json_body, timeout=timeout,
        return_mode=return_mode, wait_timeout=wait_timeout,
        connect_timeout=connect_timeout, reveal_values=reveal_values,
    )


# ── wait_for_request ───────────────────────────────────────────────────────


async def _arm_request_watch(
    tab: Any, url_pattern: str, method: Optional[str], require_response: bool,
) -> dict:
    """Subscribe for a matching request NOW. Returns {match, pending, done, disarm}.

    Separate from the waiting so click_and_wait can subscribe before it clicks: a fast API
    call completes before anything subscribed afterwards can see it. Every matching request
    is tracked, because the first one to get a response is the answer, and it need not be
    the last one to have started. Raises when Network cannot be enabled, since no event
    would ever arrive and the wait would read as "nothing matched".
    """
    from nodriver.cdp import network as cdp_network

    await tab.send(cdp_network.enable())
    match: dict = {}
    pending: dict[str, dict] = {}
    done = asyncio.Event()

    async def on_req(event):
        if done.is_set():
            return
        try:
            req = event.request
            url = getattr(req, "url", "")
            if url_pattern not in url:
                return
            if method and getattr(req, "method", "").upper() != method.upper():
                return
            rid = str(event.request_id)
            pending[rid] = {
                "request_id": rid,
                "url": url,
                "method": getattr(req, "method", ""),
                "request_headers": dict(getattr(req, "headers", None) or {}),
                "request_body": getattr(req, "post_data", None),
            }
            if not require_response:
                match.update(pending[rid])
                done.set()
        except Exception:
            pass

    async def on_res(event):
        if done.is_set():
            return
        try:
            rid = str(event.request_id)
            if rid not in pending:
                return
            resp = event.response
            match.update(pending[rid])
            match["status"] = getattr(resp, "status", None)
            match["response_headers"] = dict(getattr(resp, "headers", None) or {})
            match["mime"] = getattr(resp, "mime_type", None)
            done.set()
        except Exception:
            pass

    handlers = [(cdp_network.RequestWillBeSent, on_req)]
    if require_response:
        handlers.append((cdp_network.ResponseReceived, on_res))
    _srv._add_all_handlers(tab, handlers)
    return {"match": match, "pending": pending, "done": done,
            "disarm": lambda: _srv._drop_handlers(tab, handlers)}


async def _await_request_watch(
    watch: dict, url_pattern: str, method: Optional[str], timeout: float,
    reveal_values: Union[bool, str] = False,
) -> str:
    reveal = reveal_mode(reveal_values)
    try:
        await asyncio.wait_for(watch["done"].wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pending = watch["pending"]
        what = (f"{len(pending)} request(s) matched {url_pattern!r} but none got a response"
                if pending else f"no request matched {url_pattern!r}")
        return err(what + (f" method={method}" if method else "") + f" within {timeout}s")
    match = (watch["match"] if reveal == "all"
             else mask_request_entry(watch["match"], body_mode=reveal == "body"))
    if reveal == "body":
        match = {"reveal_values": "body", "reveal_note": BODY_REVEAL_NOTE, **match}
    return ok(json.dumps(match, indent=2, default=str))


@mcp.tool()
async def wait_for_request(
    url_pattern: str,
    method: Optional[str] = None,
    timeout: float = 15.0,
    require_response: bool = True,
    reveal_values: Union[bool, Literal["body"]] = False,
) -> str:
    """Block until a network request matching url_pattern is observed.
    Replaces the setTimeout(2000)+poll anti-pattern.

    Args:
        url_pattern: substring match
        method: optional HTTP verb filter (GET/POST/...)
        timeout: max seconds to wait
        require_response: also wait for the response phase (default True)
        reveal_values: return credential headers and fields as they are, not "*** (N chars)".
            "body" shows request body values under a secret-named parent and keeps headers,
            URL secrets and body values whose own key is secret-named hidden.

    Returns JSON of the matching entry (url/method/status/request_headers/
    response_headers).
    """
    try:
        reveal_mode(reveal_values)
    except ValueError as exc:
        return err(f"wait_for_request: {exc}; nothing was waited for")
    try:
        tab = BrowserState.active_tab()
        watch = await _arm_request_watch(tab, url_pattern, method, require_response)
        try:
            return await _await_request_watch(watch, url_pattern, method, timeout, reveal_values)
        finally:
            watch["disarm"]()
    except Exception as e:
        return err(str(e))


# ── click_and_wait ─────────────────────────────────────────────────────────


@mcp.tool()
async def click_and_wait(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    expect: str = "auto",
    expect_url_pattern: Optional[str] = None,
    expect_text: Optional[str] = None,
    expect_selector: Optional[str] = None,
    expect_request_pattern: Optional[str] = None,
    timeout: float = 8.0,
) -> str:
    """Click, then wait for the side-effect to land, telling a successful action from a silent failure (e.g.
    an invalid form, where click() returns success but submit never happened).

    Args:
        ref / selector / text: element to click (passed to the click tools; text uses click_text fuzzy match)
        expect: what to wait for after the click. One of:
            "navigation"    URL changes
            "url"           URL matches expect_url_pattern (regex)
            "text"          page contains expect_text
            "selector"      expect_selector becomes visible
            "request"       outgoing request matches expect_request_pattern
            "network_idle"  no in-flight requests for 500ms
            "auto"          try navigation→network_idle→nothing; passing one expect_* selects its mode instead
        expect_*: target for the matching expect mode
        timeout: per-mode max wait

    Returns JSON {clicked, observed: {what, evidence}, elapsed_ms}.
    """
    import time as _time
    if not (ref or selector or text):
        return err("click_and_wait: pass ref=, selector=, or text=")
    # Before the click. An error reply after it invites the caller to fix the argument and
    # retry, and that retry submits twice.
    if expect not in ("auto", "navigation", "url", "text", "selector", "request", "network_idle"):
        return err(f"click_and_wait: unknown expect={expect!r}; nothing was clicked")
    if expect == "auto":
        # A target with no matching mode used to be ignored, so the call succeeded without
        # ever looking for it.
        given = [mode for mode, target in (("url", expect_url_pattern), ("text", expect_text),
                                           ("selector", expect_selector),
                                           ("request", expect_request_pattern)) if target]
        if len(given) > 1:
            return err(f"click_and_wait: pass one expect_* target, got {', '.join(given)}; "
                       f"nothing was clicked")
        if given:
            expect = given[0]
    required = {"url": ("expect_url_pattern", expect_url_pattern),
                "text": ("expect_text", expect_text),
                "selector": ("expect_selector", expect_selector),
                "request": ("expect_request_pattern", expect_request_pattern)}
    if expect in required and not required[expect][1]:
        return err(f"expect={expect!r} requires {required[expect][0]}=; nothing was clicked")
    request_watch = None
    disarm_inflight = None
    try:
        tab = BrowserState.active_tab()
        url_before = await _srv.get_url(tab, strict=True)
        # Also before the click: whatever it triggers can finish before the call returns.
        if expect == "request":
            request_watch = await _arm_request_watch(tab, expect_request_pattern, None, True)
        inflight = None
        if expect in ("auto", "network_idle"):
            try:
                inflight, disarm_inflight = await _srv._arm_inflight_tracker(tab)
            except Exception as exc:
                if expect == "network_idle":
                    return err(f"network_idle cannot be measured ({_srv._exc_text(exc)}); "
                               f"nothing was clicked")
        t0 = _time.monotonic()
        # Dispatch the click — reuse existing tools
        if text:
            click_res = await _srv.click_text(text, exact=False)
        else:
            click_res = await _srv.click(ref=ref, selector=selector)
        click_str = str(click_res)
        if click_str.startswith("Error:"):
            return err(f"click failed: {click_str}")

        observed: dict = {"what": None, "evidence": None}
        modes_to_try = [expect] if expect != "auto" else ["navigation", "network_idle"]

        for mode in modes_to_try:
            try:
                if mode == "navigation":
                    deadline = _time.monotonic() + timeout
                    while _time.monotonic() < deadline:
                        url_now = await _srv.get_url(tab)
                        # Blank means the read failed mid-navigation, not a new URL.
                        if url_now and url_now != url_before:
                            observed = {"what": "navigation",
                                          "evidence": {"from": url_before, "to": url_now}}
                            break
                        await asyncio.sleep(0.2)
                elif mode == "url":
                    res = await assert_url_matches(expect_url_pattern, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "url", "evidence": str(res)[:200]}
                elif mode == "text":
                    res = await assert_text_present(expect_text, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "text", "evidence": str(res)[:200]}
                elif mode == "selector":
                    res = await assert_element_visible(selector=expect_selector, timeout=timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "selector", "evidence": str(res)[:200]}
                elif mode == "request":
                    res = await _await_request_watch(
                        request_watch, expect_request_pattern, None, timeout)
                    if not str(res).startswith("Error:"):
                        observed = {"what": "request", "evidence": str(res)[:300]}
                elif mode == "network_idle":
                    # From real request and finish events, armed before the click. The log
                    # length used here stays flat with capture off, requests in flight or not.
                    if inflight is None:
                        observed["network_idle"] = "not measured: Network could not be enabled"
                        continue
                    deadline = _time.monotonic() + timeout
                    while _time.monotonic() < deadline:
                        if _srv._inflight_quiet(inflight, 0.5):
                            observed = {"what": "network_idle",
                                        "evidence": f"{inflight['seen']} request(s) since the "
                                                    f"click, none in flight for 500ms"}
                            break
                        await asyncio.sleep(0.05)
                    if not observed.get("what"):
                        observed["network_idle"] = (
                            f"never idle: {len(inflight['inflight'])} request(s) still in flight")
            except Exception as e:
                observed["error"] = str(e)
            if observed.get("what"):
                break

        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if not observed.get("what") and expect != "auto":
            return err(json.dumps({
                "clicked": click_str[:200],
                "observed": "no matching expect signal",
                **({"detail": observed["network_idle"]} if "network_idle" in observed else {}),
                "elapsed_ms": elapsed_ms,
                "hint": "the click registered but no side-effect was detected — "
                        "form may be invalid; check describe_page errors[]",
            }, indent=2))
        return ok(json.dumps({
            "clicked": click_str[:200],
            "observed": observed,
            "elapsed_ms": elapsed_ms,
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))
    finally:
        if request_watch is not None:
            request_watch["disarm"]()
        if disarm_inflight is not None:
            disarm_inflight()


# ── form_introspect ────────────────────────────────────────────────────────


_FORM_INTROSPECT_JS = """
((selector) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const labelOf = (el) => {
    const id = el.id;
    if (id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL' && p.innerText.trim()) return p.innerText.trim();
    }
    if (el.getAttribute) {
      const al = el.getAttribute('aria-label');
      if (al) return al.trim();
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      const nm = el.getAttribute('name');
      if (nm) return nm.trim();
    }
    return '';
  };
  const detectFramework = (el) => {
    const out = [];
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber') || k.startsWith('__reactProps')) out.push('react');
      if (k.startsWith('__vnode') || k === '__vue__') out.push('vue');
      if (k.startsWith('$$') || k.startsWith('$0')) out.push('solid_or_svelte');
      if (k.startsWith('lit-')) out.push('lit');
    }
    return [...new Set(out)];
  };
  const root = selector ? document.querySelector(selector) : document;
  if (!root) return JSON.stringify({error: 'form not found'});
  const inputs = [...root.querySelectorAll('input,textarea,select')]
    .filter(el => visible(el) && el.type !== 'hidden');
  const fields = inputs.map(el => {
    const tag = el.tagName.toLowerCase();
    return {
      label: labelOf(el),
      tag,
      type: tag === 'input' ? (el.type || 'text') : tag,
      name: el.name || null,
      id: el.id || null,
      value: el.type === 'password'
        ? (el.value ? '(password set, not shown)' : '')
        : (typeof el.value === 'string' ? el.value.slice(0, 200) : el.value),
      required: !!el.required,
      disabled: !!el.disabled,
      readonly: !!el.readOnly,
      pattern: el.pattern || null,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      minlength: el.minLength > 0 ? el.minLength : null,
      validation_message: el.validity ? (el.validationMessage || null) : null,
      valid: el.validity ? el.validity.valid : null,
      aria_invalid: el.getAttribute('aria-invalid') === 'true',
      aria_describedby: el.getAttribute('aria-describedby') || null,
      framework: detectFramework(el),
    };
  });
  // Submit / reset buttons inside the form
  const buttons = [...root.querySelectorAll(
    'button, input[type=submit], input[type=reset], [role=button]'
  )].filter(visible).map(el => ({
    text: (el.innerText || el.value || '').trim().slice(0, 80),
    type: el.type || 'button',
    disabled: !!el.disabled,
  }));
  // Form-level metadata
  const formEl = root.tagName === 'FORM' ? root : root.querySelector('form');
  const meta = formEl ? {
    action: formEl.getAttribute('action') || '',
    method: formEl.getAttribute('method') || 'get',
    enctype: formEl.enctype || '',
    novalidate: !!formEl.noValidate,
  } : null;
  return JSON.stringify({fields, buttons, meta});
})
"""


@mcp.tool()
async def form_introspect(form_selector: Optional[str] = None) -> str:
    """Detailed form analysis in a single call. Returns label,
    framework binding (react/vue/solid_or_svelte/lit), validation state,
    and constraints (pattern, min/max length, required) per field.

    Args:
        form_selector: CSS selector for a specific form (default: scan
            whole document for visible inputs)
    """
    try:
        tab = BrowserState.active_tab()
        sel = json.dumps(form_selector) if form_selector else "null"
        raw = await _srv._wait(tab.evaluate(
            f"({_FORM_INTROSPECT_JS})({sel})", return_by_value=True,
        ), what="form_introspect")
        text = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        data = parse_json(str(text), {})
        if isinstance(data, dict) and data.get("error"):
            return err(data["error"])
        return ok(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        return err(str(e))
