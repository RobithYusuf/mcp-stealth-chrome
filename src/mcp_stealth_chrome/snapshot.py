"""DOM snapshot JS — IIFE strings compatible across Chromium via nodriver.evaluate().

Three modes:
  SNAPSHOT_JS       — full (default) — matches mcp-camoufox, computed-style visibility check
  SNAPSHOT_JS_FAST  — skip getComputedStyle & attribute enumeration — 2-3× faster
  SNAPSHOT_JS_VIEWPORT — full fidelity but filter to elements inside current viewport — 5-10× for long pages
"""
from __future__ import annotations

import hashlib

# IIFE — page.evaluate executes the expression, return value is serialized.
# Design identical to mcp-camoufox for cross-package consistency.
SNAPSHOT_JS = r"""(() => {
  var sels = 'button, a, input:not([type="hidden"]), textarea, select, '
    + '[role="button"], [role="link"], [role="textbox"], [role="checkbox"], '
    + '[role="radio"], [role="tab"], [role="menuitem"], [contenteditable="true"], '
    + 'img[alt], h1, h2, h3, h4, h5, h6, label, [role="dialog"], [role="alert"], [role="status"]';
  var els = document.querySelectorAll(sels);
  var results = [];
  var refId = 0;
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var ref = 'e' + refId++;
    el.setAttribute('data-mcp-ref', ref);
    var entry = {
      ref: ref,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 100),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      href: el.tagName === 'A' ? (el.href || '').slice(0, 500) : '',
      checked: el.checked || false,
      disabled: el.disabled || false
    };
    var clean = {};
    var keys = Object.keys(entry);
    for (var j = 0; j < keys.length; j++) {
      var k = keys[j], v = entry[k];
      if (v !== '' && v !== false && v !== undefined) clean[k] = v;
    }
    results.push(clean);
  }
  return JSON.stringify(results);
})()"""


# Fast mode — no getComputedStyle, no attribute enumeration, minimal payload.
# Trades completeness for speed: good for "is this element still here" checks.
SNAPSHOT_JS_FAST = r"""(() => {
  var sels = 'button, a, input:not([type="hidden"]), textarea, select, '
    + '[role="button"], [role="link"], [contenteditable="true"], h1, h2, h3';
  var els = document.querySelectorAll(sels);
  var results = [];
  var refId = 0;
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    var ref = 'e' + refId++;
    el.setAttribute('data-mcp-ref', ref);
    results.push({
      ref: ref,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || '').trim().slice(0, 80)
    });
  }
  return JSON.stringify(results);
})()"""


# Viewport-only — same fidelity as full, but skip elements outside the current
# scroll viewport. 5-10× speedup on long pages (feeds, search results).
SNAPSHOT_JS_VIEWPORT = r"""(() => {
  var vh = window.innerHeight, vw = window.innerWidth;
  var sels = 'button, a, input:not([type="hidden"]), textarea, select, '
    + '[role="button"], [role="link"], [role="textbox"], [role="checkbox"], '
    + '[role="radio"], [role="tab"], [role="menuitem"], [contenteditable="true"], '
    + 'img[alt], h1, h2, h3, h4, h5, h6, label, [role="dialog"], [role="alert"], [role="status"]';
  var els = document.querySelectorAll(sels);
  var results = [];
  var refId = 0;
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    // Skip if fully outside viewport (keep partial overlaps)
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var ref = 'e' + refId++;
    el.setAttribute('data-mcp-ref', ref);
    var entry = {
      ref: ref,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 100),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      href: el.tagName === 'A' ? (el.href || '').slice(0, 500) : '',
      checked: el.checked || false,
      disabled: el.disabled || false
    };
    var clean = {};
    var keys = Object.keys(entry);
    for (var j = 0; j < keys.length; j++) {
      var k = keys[j], v = entry[k];
      if (v !== '' && v !== false && v !== undefined) clean[k] = v;
    }
    results.push(clean);
  }
  return JSON.stringify(results);
})()"""


def snapshot_hash(elements: list[dict]) -> str:
    """Stable fingerprint of snapshot content — used for diff detection.

    Only hashes keys that matter for UI state (tag/text/disabled/checked/href),
    skips volatile fields like ref (reassigned each call).
    """
    keys = ("tag", "role", "text", "href", "checked", "disabled")
    rows = []
    for el in elements:
        rows.append("|".join(str(el.get(k, "")) for k in keys))
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def format_snapshot(
    elements: list[dict],
    url: str,
    title: str,
    mode: str = "full",
    unchanged_from: str | None = None,
) -> str:
    """Pretty-print snapshot for LLM consumption."""
    if not elements:
        elements = []
    header = [
        f"Page: {title}",
        f"URL: {url}",
    ]
    if mode != "full":
        header.append(f"Mode: {mode}")
    if unchanged_from:
        header.append(f"DOM hash: {unchanged_from} (unchanged)")
    header.extend(["", f"Interactive elements ({len(elements)}):", ""])
    lines = list(header)
    for el in elements:
        parts = [f"[{el.get('tag', '?')}]"]
        if el.get("role"):
            parts.append(f"role={el['role']}")
        if el.get("type"):
            parts.append(f"type={el['type']}")
        if el.get("text"):
            parts.append(f'"{el["text"][:80]}"')
        if el.get("placeholder"):
            parts.append(f'placeholder="{el["placeholder"]}"')
        if el.get("aria"):
            parts.append(f'aria="{el["aria"]}"')
        if el.get("href"):
            parts.append(f'href="{el["href"][:60]}"')
        if el.get("checked"):
            parts.append("checked")
        if el.get("disabled"):
            parts.append("disabled")
        lines.append(f"  ref={el.get('ref')}  {' '.join(parts)}")
    return "\n".join(lines)
