"""DOM snapshot JS — IIFE string compatible across Chromium via nodriver.evaluate()."""
from __future__ import annotations

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


def format_snapshot(elements: list[dict], url: str, title: str) -> str:
    """Pretty-print snapshot for LLM consumption."""
    if not elements:
        elements = []
    lines = [
        f"Page: {title}",
        f"URL: {url}",
        "",
        f"Interactive elements ({len(elements)}):",
        "",
    ]
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
