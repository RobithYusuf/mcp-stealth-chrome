"""DOM snapshot JS — IIFE strings compatible across Chromium via nodriver.evaluate().

The fixed-shape scripts, kept for compatibility:
  SNAPSHOT_JS       — full — matches mcp-camoufox, computed-style visibility check
  SNAPSHOT_JS_FAST  — skip getComputedStyle & attribute enumeration — 2-3× faster
  SNAPSHOT_JS_VIEWPORT — full fidelity but filter to elements inside current viewport — 5-10× for long pages

What browser_snapshot actually runs is build_snapshot_js(snapshot_options(...)),
which adds container scoping, role/name filtering and an element cap, and — the
part that matters most — returns the accounting (matched / measured / eligible /
returned / omitted) alongside the elements, so a short list is never mistaken
for a short page.
"""
from __future__ import annotations

import hashlib
import json

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
    keys = ("tag", "role", "text", "alt", "href", "checked", "disabled")
    rows = []
    for el in elements:
        rows.append("|".join(str(el.get(k, "")) for k in keys))
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def element_fingerprint(element: dict) -> str:
    """Stable identity for delta snapshots, excluding the regenerated ref."""
    keys = (
        "tag", "role", "text", "type", "name", "placeholder", "aria", "alt",
        "href", "checked", "disabled", "hidden",
    )
    row = "|".join(str(element.get(k, "")) for k in keys)
    return hashlib.sha1(row.encode("utf-8")).hexdigest()[:16]


def format_snapshot(
    elements: list[dict],
    url: str,
    title: str,
    mode: str = "full",
    unchanged_from: str | None = None,
    changed_fingerprints: set[str] | None = None,
    total_count: int | None = None,
    counts: dict | None = None,
    options: dict | None = None,
) -> str:
    """Pretty-print snapshot for LLM consumption.

    ``counts`` is the accounting the snapshot script returned for this very
    walk. When it is present the header states how many elements were measured
    and how many were withheld, so a filtered or capped list can never read as
    "this page has nothing on it".
    """
    if not elements:
        elements = []
    header = [
        f"Page: {title}",
        f"URL: {url}",
    ]
    if mode != "full":
        header.append(f"Mode: {mode}")
    if options is not None:
        header.append(f"Scope: {describe_scope(options)}")
    if unchanged_from:
        header.append(f"DOM hash: {unchanged_from} (unchanged)")
    if counts is not None and options is not None and not unchanged_from:
        header.extend(format_counts(counts, options))
    if unchanged_from and total_count is not None:
        # Both halves are load-bearing: the count keeps "unchanged" from reading
        # as an empty page, and the explicit 0 keeps it comparable with the
        # delta line every other snapshot prints.
        count_header = (
            f"Interactive elements ({total_count}, unchanged; details omitted)"
            f" — same scope as the previous snapshot"
            f" ({total_count} measured, 0 changed):"
        )
    elif changed_fingerprints is None:
        count_header = f"Interactive elements ({len(elements)}):"
    else:
        changed_count = sum(
            element_fingerprint(el) in changed_fingerprints for el in elements
        )
        eligible = (counts or {}).get("eligible")
        of_total = (
            f" of {eligible}"
            if eligible is not None and eligible != len(elements)
            else ""
        )
        count_header = (
            f"Interactive elements ({len(elements)}{of_total}, "
            f"{changed_count} changed):"
        )
    header.extend(["", count_header, ""])
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
        if el.get("alt"):
            parts.append(f'alt="{el["alt"][:60]}"')
        if el.get("hidden"):
            parts.append("hidden")
        if el.get("href"):
            parts.append(f'href="{el["href"][:60]}"')
        if el.get("checked"):
            parts.append("checked")
        if el.get("disabled"):
            parts.append("disabled")
        prefix = "* " if (
            changed_fingerprints is not None
            and element_fingerprint(el) in changed_fingerprints
        ) else "  "
        lines.append(f"{prefix}ref={el.get('ref')}  {' '.join(parts)}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# Scoped / compact snapshot builder
#
# The three constants above are the original fixed-shape scripts and stay for
# compatibility. Everything browser_snapshot actually runs now comes out of
# build_snapshot_js(), which folds container scoping, viewport clipping,
# role/name filtering and the element cap into ONE pass so the counts it
# reports (matched / measured / eligible / returned / omitted) describe the
# same walk that produced the elements — a second pass would let the numbers
# and the list disagree.
# ══════════════════════════════════════════════════════════════════════════

SNAPSHOT_SELECTORS_FULL = (
    'button, a, input:not([type="hidden"]), textarea, select, '
    '[role="button"], [role="link"], [role="textbox"], [role="checkbox"], '
    '[role="radio"], [role="tab"], [role="menuitem"], [contenteditable="true"], '
    'img[alt], h1, h2, h3, h4, h5, h6, label, [role="dialog"], [role="alert"], '
    '[role="status"]'
)

SNAPSHOT_SELECTORS_FAST = (
    'button, a, input:not([type="hidden"]), textarea, select, '
    '[role="button"], [role="link"], [contenteditable="true"], h1, h2, h3'
)

SNAPSHOT_MODES = ("interactive_compact", "full", "fast", "viewport")

_SNAPSHOT_JS_TEMPLATE = r"""(() => {
  var CFG = __CFG__;
  var out = {
    ok: true,
    doc: '',
    container: {requested: !!CFG.container, selector: CFG.container || '',
                matched: 0, found: true},
    matched: 0,
    hidden_skipped: 0,
    offscreen_skipped: 0,
    measured: 0,
    excluded: {unnamed_decorative: 0, role: 0, name: 0},
    eligible: 0,
    returned: 0,
    omitted: 0,
    truncated: false,
    elements: []
  };

  // Per-document token. It disappears with the document, so a reload or a
  // navigation back to the same URL cannot be mistaken for "nothing changed":
  // the refs the caller is holding died with the old document even though the
  // DOM hash is identical.
  var de = document.documentElement;
  var tok = de.getAttribute('data-mcp-doc');
  if (!tok) {
    tok = 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    de.setAttribute('data-mcp-doc', tok);
  }
  out.doc = tok;

  // Drop the previous snapshot's refs before handing out new ones. Left in
  // place, a scoped or capped snapshot leaves e0 on an old element AND puts e0
  // on a new one; resolve_ref's querySelector returns whichever comes first in
  // document order, so the caller silently acts on the wrong element.
  var stale = document.querySelectorAll('[data-mcp-ref]');
  for (var s = 0; s < stale.length; s++) stale[s].removeAttribute('data-mcp-ref');

  var roots = [document];
  if (CFG.container) {
    var found;
    try { found = document.querySelectorAll(CFG.container); }
    catch (e) {
      out.ok = false;
      out.error = 'container_selector is not valid CSS: ' +
                  (e && e.message ? e.message : String(e));
      return JSON.stringify(out);
    }
    out.container.matched = found.length;
    out.container.found = found.length > 0;
    roots = [];
    for (var c = 0; c < found.length; c++) roots.push(found[c]);
    if (!roots.length) return JSON.stringify(out);
  }

  function implicitRole(el) {
    var t = el.tagName.toLowerCase();
    if (t === 'a') return el.hasAttribute('href') ? 'link' : '';
    if (t === 'button') return 'button';
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (t === 'img') return 'img';
    if (t === 'label') return 'label';
    if (t.length === 2 && t.charAt(0) === 'h' && t >= 'h1' && t <= 'h6') return 'heading';
    if (t === 'input') {
      var ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'submit' || ty === 'button' || ty === 'reset' || ty === 'image')
        return 'button';
      if (ty === 'range') return 'slider';
      if (ty === 'search') return 'searchbox';
      return 'textbox';
    }
    return '';
  }

  function accName(el) {
    var parts = [
      el.getAttribute('aria-label'),
      el.getAttribute('alt'),
      el.getAttribute('title'),
      el.innerText,
      (typeof el.value === 'string' ? el.value : ''),
      el.getAttribute('placeholder'),
      el.getAttribute('name')
    ];
    for (var i = 0; i < parts.length; i++) {
      var v = (parts[i] || '').trim();
      if (v) return v;
    }
    return '';
  }

  var INTERACTIVE_ROLES = ['button', 'link', 'textbox', 'checkbox', 'radio',
                           'tab', 'menuitem', 'combobox', 'listbox', 'slider',
                           'searchbox'];

  function isInteractive(el) {
    var t = el.tagName.toLowerCase();
    if (t === 'button' || t === 'a' || t === 'input' || t === 'textarea' ||
        t === 'select') return true;
    if (el.getAttribute('contenteditable') === 'true') return true;
    var r = (el.getAttribute('role') || '').toLowerCase();
    return r ? INTERACTIVE_ROLES.indexOf(r) >= 0 : false;
  }

  var vh = window.innerHeight, vw = window.innerWidth;
  var useSet = (typeof Set === 'function');
  var setSeen = useSet ? new Set() : null;
  var arrSeen = useSet ? null : [];
  var kept = [];

  for (var ri = 0; ri < roots.length; ri++) {
    var els = roots[ri].querySelectorAll(CFG.selectors);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      // Overlapping containers can hand back the same element twice; counting
      // it twice would inflate every number in the report.
      if (setSeen) { if (setSeen.has(el)) continue; setSeen.add(el); }
      else { if (arrSeen.indexOf(el) >= 0) continue; arrSeen.push(el); }
      out.matched++;

      var r = el.getBoundingClientRect();
      var hidden = (r.width < 1 || r.height < 1);
      if (!hidden && CFG.computed_style) {
        var cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') hidden = true;
      }
      if (hidden) {
        out.hidden_skipped++;
        if (!CFG.include_hidden) continue;
      }
      if (CFG.viewport_only &&
          (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw)) {
        out.offscreen_skipped++;
        continue;
      }
      out.measured++;

      var role = el.getAttribute('role') || '';
      var irole = implicitRole(el);
      var nm = accName(el);

      if (CFG.drop_unnamed_decorative && !isInteractive(el) && !nm) {
        out.excluded.unnamed_decorative++;
        continue;
      }
      if (CFG.roles.length) {
        var rl = (role || irole).toLowerCase();
        if (CFG.roles.indexOf(rl) < 0) { out.excluded.role++; continue; }
      }
      if (CFG.name_contains &&
          nm.toLowerCase().indexOf(CFG.name_contains) < 0) {
        out.excluded.name++;
        continue;
      }

      out.eligible++;
      if (CFG.max_elements > 0 && kept.length >= CFG.max_elements) {
        out.omitted++;
        out.truncated = true;
        continue;
      }

      var entry;
      if (CFG.minimal) {
        entry = {
          ref: '',
          tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.value || '').trim().slice(0, CFG.text_cap)
        };
      } else {
        entry = {
          ref: '',
          tag: el.tagName.toLowerCase(),
          role: role,
          text: (el.innerText || el.value || '').trim().slice(0, CFG.text_cap),
          type: el.getAttribute('type') || '',
          name: el.getAttribute('name') || '',
          placeholder: el.getAttribute('placeholder') || '',
          aria: el.getAttribute('aria-label') || '',
          alt: el.getAttribute('alt') || '',
          href: el.tagName === 'A' ? (el.href || '').slice(0, 500) : '',
          checked: el.checked || false,
          disabled: el.disabled || false
        };
      }
      if (hidden) entry.hidden = true;
      kept.push([el, entry]);
    }
  }

  for (var k = 0; k < kept.length; k++) {
    var ref = 'e' + k;
    kept[k][0].setAttribute('data-mcp-ref', ref);
    var e = kept[k][1];
    e.ref = ref;
    var clean = {};
    var keys = Object.keys(e);
    for (var j = 0; j < keys.length; j++) {
      var kk = keys[j], v = e[kk];
      if (v !== '' && v !== false && v !== undefined && v !== null) clean[kk] = v;
    }
    out.elements.push(clean);
  }
  out.returned = out.elements.length;
  return JSON.stringify(out);
})()"""


def _mode_config(mode: str) -> dict:
    if mode == "fast":
        return {
            "selectors": SNAPSHOT_SELECTORS_FAST,
            "computed_style": False,
            "drop_unnamed_decorative": False,
            "minimal": True,
            "text_cap": 80,
            "force_viewport": False,
        }
    if mode == "interactive_compact":
        return {
            "selectors": SNAPSHOT_SELECTORS_FULL,
            "computed_style": True,
            # The admin page in the field report returned ~190 elements, dozens
            # of them nameless <img alt=""> that no caller can act on. They are
            # dropped here and counted, never silently.
            "drop_unnamed_decorative": True,
            "minimal": False,
            "text_cap": 80,
            "force_viewport": False,
        }
    if mode == "viewport":
        return {
            "selectors": SNAPSHOT_SELECTORS_FULL,
            "computed_style": True,
            "drop_unnamed_decorative": False,
            "minimal": False,
            "text_cap": 100,
            "force_viewport": True,
        }
    return {
        "selectors": SNAPSHOT_SELECTORS_FULL,
        "computed_style": True,
        "drop_unnamed_decorative": False,
        "minimal": False,
        "text_cap": 100,
        "force_viewport": False,
    }


def normalize_roles(role: str | None) -> list[str]:
    """Split a comma/space separated role filter into a lowercase list."""
    if not role:
        return []
    parts = [p.strip().lower() for p in role.replace(" ", ",").split(",")]
    return [p for p in parts if p]


def snapshot_options(
    mode: str = "interactive_compact",
    container_selector: str | None = None,
    viewport_only: bool = False,
    role: str | None = None,
    name_contains: str | None = None,
    include_hidden: bool = False,
    max_elements: int = 100,
) -> dict:
    """Resolve tool arguments into the config the snapshot script runs on."""
    mode = mode if mode in SNAPSHOT_MODES else "interactive_compact"
    cfg = _mode_config(mode)
    return {
        "mode": mode,
        "selectors": cfg["selectors"],
        "computed_style": cfg["computed_style"],
        "drop_unnamed_decorative": cfg["drop_unnamed_decorative"],
        "minimal": cfg["minimal"],
        "text_cap": cfg["text_cap"],
        "container": container_selector or "",
        "viewport_only": bool(viewport_only) or cfg["force_viewport"],
        "roles": normalize_roles(role),
        "name_contains": (name_contains or "").strip().lower(),
        "include_hidden": bool(include_hidden),
        "max_elements": max(0, int(max_elements or 0)),
    }


def build_snapshot_js(options: dict) -> str:
    """Render the snapshot IIFE for one resolved option set."""
    return _SNAPSHOT_JS_TEMPLATE.replace("__CFG__", json.dumps(options))


def snapshot_scope_signature(options: dict) -> str:
    """Identity of an option set, used to scope the diff_from_last cache.

    A compact snapshot and a full snapshot of the same tab measure different
    element sets. Comparing their hashes under one key would report "unchanged"
    for a call that would in fact have returned a different list — so the cache
    is keyed by target AND by this signature.
    """
    keys = (
        "mode", "container", "viewport_only", "roles", "name_contains",
        "include_hidden", "max_elements",
    )
    payload = json.dumps({k: options.get(k) for k in keys}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def describe_scope(options: dict) -> str:
    """Human-readable one-liner naming every filter that was applied."""
    bits = []
    if options.get("container"):
        bits.append(f'container="{options["container"]}"')
    if options.get("viewport_only"):
        bits.append("viewport only")
    if options.get("roles"):
        bits.append("role in [" + ",".join(options["roles"]) + "]")
    if options.get("name_contains"):
        bits.append(f'name contains "{options["name_contains"]}"')
    if options.get("include_hidden"):
        bits.append("hidden included")
    cap = options.get("max_elements") or 0
    bits.append(f"max_elements={cap}" if cap else "max_elements=0 (no cap)")
    return " · ".join(bits)


def format_counts(counts: dict, options: dict) -> list[str]:
    """Render the measured/returned/omitted accounting as report lines.

    Every number here comes from the single DOM walk that produced the element
    list. An omitted element is one that was measured and passed every filter
    and still did not make the cap — it is never conflated with one that was
    filtered out, because the caller's next move differs: raise max_elements
    versus relax the filter.
    """
    lines = []
    lines.append(
        "Counts: matched {matched} · measured {measured} · eligible {eligible} "
        "· returned {returned} · omitted {omitted}".format(
            matched=counts.get("matched", 0),
            measured=counts.get("measured", 0),
            eligible=counts.get("eligible", 0),
            returned=counts.get("returned", 0),
            omitted=counts.get("omitted", 0),
        )
    )
    excluded = counts.get("excluded") or {}
    parts = []
    if excluded.get("unnamed_decorative"):
        parts.append(
            f"{excluded['unnamed_decorative']} unnamed decorative "
            f"(non-interactive, no accessible name — mode=\"full\" keeps them)"
        )
    if excluded.get("role"):
        parts.append(f"{excluded['role']} by role filter")
    if excluded.get("name"):
        parts.append(f"{excluded['name']} by name filter")
    if parts:
        lines.append("Excluded: " + " · ".join(parts))
    skipped = []
    if counts.get("hidden_skipped"):
        state = "counted, included" if options.get("include_hidden") else "skipped"
        skipped.append(f"{counts['hidden_skipped']} not rendered ({state})")
    if counts.get("offscreen_skipped"):
        skipped.append(f"{counts['offscreen_skipped']} outside viewport")
    if skipped:
        lines.append("Not measured: " + " · ".join(skipped))
    if counts.get("truncated"):
        lines.append(
            f"TRUNCATED: {counts.get('omitted', 0)} eligible element(s) were not "
            f"returned because max_elements={options.get('max_elements')} — raise "
            f"it or narrow with container_selector/role/name_contains to see them."
        )
    container = counts.get("container") or {}
    if container.get("requested"):
        if not container.get("found"):
            lines.append(
                f"Container: NO element matched \"{container.get('selector')}\" — "
                f"nothing was measured (this is an empty scope, not an empty page)."
            )
        else:
            lines.append(
                f"Container: {container.get('matched')} element(s) matched "
                f"\"{container.get('selector')}\""
            )
    return lines
