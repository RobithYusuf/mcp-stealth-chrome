"""Reading the page: snapshot, text, HTML, links, attributes, screenshot, PDF, element
inspection, frames and scraping.

Moved out of server.py unchanged. The snapshot writes nothing to the page: it runs in an
isolated world and refs live on the server.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Literal, Optional

from nodriver.cdp import runtime as cdp_runtime

from .. import image_output as _image_output
from .._app import mcp
from ..helpers import confined_output_path, err, get_url, ok, parse_json, select_tab, ts_filename
from ..refs import MAX_FRAMES, all_frames, collect_snapshot, frame_world, resolve_ref
from ..result_types import structured_json
from ..server import (
    _eval_checked,
    _js_exception_text,
    _page_facts_for_report,
    _target_key,
    _wait,
)
from ..snapshot import (
    element_fingerprint,
    format_snapshot,
    snapshot_hash,
    snapshot_options,
    snapshot_scope_signature,
)
from ..state import EXPORT_DIR, SCREENSHOT_DIR, BrowserState, ensure_dirs

# Ceiling for base64 image bodies inlined into a tool result. Clients reject
# oversized results outright, which throws away the whole call — better to hand
# back the file path and tell the caller how to shrink the shot.
B64_INLINE_LIMIT = int(os.environ.get("B64_INLINE_LIMIT", "60000"))


# ══════════════════════════════════════════════════════════════════════════
# 3. DOM / CONTENT
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def browser_snapshot(
    mode: Literal["interactive_compact", "full", "fast", "viewport"] = "interactive_compact",
    diff_from_last: bool = True,
    delta: bool = True,
    max_elements: int = 100,
    container_selector: Optional[str] = None,
    viewport_only: bool = False,
    role: Optional[str] = None,
    name_contains: Optional[str] = None,
    include_hidden: bool = False,
    include_frames: bool = True,
) -> str:
    """Ref-indexed list of the interactive elements on the active tab.

    Reaches into iframes (cross-origin included) and open shadow roots; those
    elements are marked `frame=fN` / `shadow` and their refs work with click,
    fill and the rest like any other. Closed shadow roots cannot be measured.

    Refs (e0, e1, ...) live on the server, mapped to CDP node ids; the snapshot
    runs in an isolated world and writes nothing to the page. EVERY snapshot
    replaces all refs. A ref is valid until the next snapshot, and one that was
    superseded, or whose element is gone, fails with an error saying so instead
    of hitting the wrong element.

    A field carries its label as label="..." (from <label for>, a label around it, or
    aria-labelledby), and name_contains matches it.

    Modes:
      interactive_compact — default. Full fidelity, minus non-interactive
                  elements that carry no accessible name (the nameless
                  ``<img alt="">`` that made a real admin page report ~190
                  elements), and minus the row of a label whose field is listed.
                  Every drop is counted in the report.
      full      — same shape as mcp-camoufox; keeps the unnamed decorative
                  elements and the label rows compact drops.
      fast      — reduced selector, no getComputedStyle, minimal attrs (2-3×
                  faster, less info per element)
      viewport  — full fidelity, only elements inside the current scroll
                  viewport (pair with scroll for segment-by-segment reading)

    Scoping (all compose, all reported):
      max_elements      cap on RETURNED elements; 0 = no cap. Elements past the
                        cap are counted as `omitted`, never dropped in silence.
      container_selector  measure only inside the elements matching this CSS.
                        A selector that matches nothing is reported as an empty
                        scope, not as an empty page. Invalid CSS is an error —
                        note that `:has-text()` is a Playwright-ism, not CSS.
      viewport_only     drop elements fully outside the current viewport.
      role              comma-separated roles to keep, matched against the
                        explicit role or the implicit one ("button,link").
      name_contains     case-insensitive substring of the accessible name
                        (aria-label / alt / title / text / value / placeholder).
      include_hidden    also return elements that are not rendered, each marked
                        `hidden`. They are counted either way.
      include_frames    False measures the main frame only. max_elements covers
                        the whole page, main frame first. viewport_only judges
                        frame content against its own frame's viewport.

    The report always states matched / measured / eligible / returned / omitted
    for the same DOM walk that produced the list, so a filtered result can never
    be read as "this page has nothing on it".

    diff_from_last=True (default) caches the DOM hash per tab AND per option
    scope; when the same scope on the same document and URL hashes the same, the
    element list is not re-serialized. It is scoped per option set because a
    compact and a full snapshot measure different sets — sharing one cache entry
    would answer "unchanged" to a call that would have returned a different list.
    The cache also keys on each frame's loader id, so a reload or a return to the
    same URL never reports "unchanged": the refs the caller holds died with the
    old document even when the hash is identical.

    delta=True prefixes elements added or changed since this tab's previous
    snapshot IN THE SAME SCOPE with ``*``. A scope's first snapshot marks
    everything.
    """
    try:
        tab = BrowserState.active_tab()
        options = snapshot_options(
            mode=mode,
            container_selector=container_selector,
            viewport_only=viewport_only,
            role=role,
            name_contains=name_contains,
            include_hidden=include_hidden,
            max_elements=max_elements,
            include_frames=include_frames,
        )
        walk = await _wait(collect_snapshot(tab, options), what="browser_snapshot")
        elements = walk.elements
        counts = dict(walk.counts)
        counts["frames"] = {
            "walked": walk.frames, "failed": walk.failed,
            "skipped": walk.skipped, "limit": MAX_FRAMES,
        }
        url, title = await _page_facts_for_report(tab)
        h = snapshot_hash(elements)
        doc_token = walk.doc_token()
        cache_key = _target_key(tab)
        scope_sig = snapshot_scope_signature(options)
        scopes = _SNAPSHOT_CACHE.setdefault(cache_key, {})
        last = scopes.get(scope_sig)
        # A matching hash on a DIFFERENT document is not "unchanged": the page
        # was reloaded or navigated, every ref was reissued, and answering
        # "unchanged; details omitted" would leave the caller holding refs that
        # no longer exist while believing nothing moved.
        same_document = bool(doc_token) and bool(last) and last.get("doc") == doc_token
        if (
            diff_from_last
            and last
            and same_document
            and last["url"] == url
            and last["hash"] == h
        ):
            return ok(format_snapshot(
                [], url, title, mode=mode, unchanged_from=h,
                changed_fingerprints=set() if delta else None,
                total_count=int(last.get("count", len(elements))),
                options=options,
            ))
        fingerprints = {element_fingerprint(el) for el in elements}
        # Only compare against a previous snapshot of the SAME document; after a
        # reload every element is legitimately new.
        previous = set(last.get("fingerprints", ())) if same_document else set()
        changed = fingerprints - previous if same_document else fingerprints
        scopes[scope_sig] = {
            "url": url,
            "hash": h,
            "doc": doc_token,
            "fingerprints": fingerprints,
            "count": len(elements),
        }
        # Bound the per-target cache: distinct option sets are cheap to create
        # and would otherwise accumulate for the life of the tab.
        while len(scopes) > _SNAPSHOT_CACHE_SCOPES_PER_TAB:
            scopes.pop(next(iter(scopes)))
        return ok(format_snapshot(
            elements,
            url,
            title,
            mode=mode,
            changed_fingerprints=changed if delta else None,
            counts=counts,
            options=options,
        ))
    except Exception as e:
        return err(str(e))


# Per-tab snapshot state for diff_from_last (cleared on browser_close / reset).
# Two levels: target key -> option-scope signature -> {url, hash, doc,
# fingerprints, count}. _clear_tab_scoped_state pops the whole target.
_SNAPSHOT_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
_SNAPSHOT_CACHE_SCOPES_PER_TAB = 8


@mcp.tool()
async def screenshot(
    filename: Optional[str] = None,
    full_page: bool = False,
    return_base64: bool = False,
    format: Literal["auto", "png", "jpeg"] = "auto",
    quality: Optional[int] = None,
    region: Optional[dict] = None,
    region_selector: Optional[str] = None,
    max_dimension: int = 1920,
    output_mode: Literal["artifact", "inline_image", "path", "base64"] = "artifact",
    tab: Optional[int] = None,
) -> str:
    """Screenshot active tab. Saves to ~/.mcp-stealth/screenshots/.

    Args:
        filename: output name (default timestamped)
        full_page: stitch entire page height (slower, larger file)
        return_base64: append base64 body to response (useful for vision models).
            Refused when the encoded body would exceed B64_INLINE_LIMIT — you get
            the path plus advice instead of a rejected tool result.
        format: "auto" (from extension, default), "png" (lossless), or "jpeg" (smaller)
        quality: JPEG quality 1-100 (default 80) — ignored for PNG
        region: clip to {x, y, width, height} in **CSS pixels relative to the
                viewport** — the same coordinate space as getBoundingClientRect()
                and mouse_click_xy, NOT device/captured pixels. Uses CDP
                Page.captureScreenshot clip (2-5× faster for small crops)
        region_selector: CSS selector to crop to — the element's bounding box is
                measured for you. Prefer this over `region`: it removes the whole
                class of manual-coordinate mistakes. Wins over `region` if both given.
        max_dimension: if either width or height exceeds this (px), the image is
            resized proportionally via OpenCV INTER_AREA. Default 1920 keeps output
            under the 2000 px per-side limit that LLM image tools (Claude/GPT) enforce
            — prevents "image exceeds dimension limit" failures on long full_page
            captures or hi-DPR device emulation. Pass 0 to disable resizing.
    """
    try:
        if output_mode not in ("artifact", "inline_image", "path", "base64"):
            return err("output_mode must be artifact, inline_image, path, or base64")
        tab = select_tab(tab)
        ensure_dirs()
        fname = filename or ts_filename("shot", "png")
        path = confined_output_path(SCREENSHOT_DIR, fname)
        ext = path.suffix.lower().lstrip(".")
        if format == "auto":
            fmt = "png" if ext == "png" else "jpeg"
        else:
            fmt = format
        # Resolve region_selector → region. Measuring the box here is what makes
        # the "which coordinate space?" question disappear for the caller.
        if region_selector:
            expr = ("(() => { const e = document.querySelector(%s); if (!e) return '';"
                    " const r = e.getBoundingClientRect();"
                    " return JSON.stringify({x: r.left, y: r.top, width: r.width,"
                    " height: r.height}); })()" % json.dumps(region_selector))
            v = await tab.evaluate(expr, return_by_value=True)
            v = v.value if hasattr(v, "value") and not isinstance(v, str) else v
            if not v:
                return err(f"region_selector not found: {region_selector}")
            box = json.loads(v)
            if box["width"] <= 0 or box["height"] <= 0:
                return err(f"region_selector {region_selector!r} has zero size "
                           f"({box['width']}×{box['height']}) — element is not rendered")
            region = box

        # Region clip → use raw CDP; nodriver's save_screenshot doesn't expose clip
        if region:
            from nodriver.cdp import page as cdp_page
            # capture_beyond_viewport makes the clip PAGE space, and region is viewport
            # space, so on a scrolled page the raw numbers capture some other strip.
            page_box = _image_output.viewport_clip(
                region, await _image_output.viewport_context(tab))
            clip = cdp_page.Viewport(**page_box, scale=1.0)
            kwargs: dict[str, Any] = {"format_": fmt, "clip": clip, "capture_beyond_viewport": True}
            if fmt == "jpeg" and quality is not None:
                kwargs["quality"] = int(quality)
            b64 = await _wait(tab.send(cdp_page.capture_screenshot(**kwargs)),
                              what="screenshot (region)")
            data = base64.b64decode(b64)
            path.write_bytes(data)
        else:
            # Multi-strategy capture — pages with stuck JS / heavy SPAs can
            # make tab.save_screenshot hang because nodriver's wrapper waits
            # for paint stability and file IO. We try fastest viewport
            # capture first, then full_page if requested.
            save_kwargs: dict[str, Any] = {
                "filename": str(path),
                "format": fmt,
                "full_page": full_page,
            }
            if fmt == "jpeg" and quality is not None:
                save_kwargs["quality"] = int(quality)
            # Collect per-strategy errors so the final message tells the
            # caller exactly which paths failed and how — no more single
            # "last error" mystery.
            strategy_errors: list[str] = []
            captured = False
            try:
                await _wait(tab.save_screenshot(**save_kwargs),
                            timeout=15.0, what="screenshot (nodriver)")
                captured = True
            except Exception as e1:
                strategy_errors.append(f"nodriver(15s): {type(e1).__name__}: {e1}")
            if not captured:
                # Fallback 1: raw CDP Page.captureScreenshot — bypasses
                # nodriver's paint-wait hooks.
                try:
                    from nodriver.cdp import page as cdp_page
                    cdp_kwargs: dict[str, Any] = {"format_": fmt,
                                                    "capture_beyond_viewport": bool(full_page)}
                    if fmt == "jpeg" and quality is not None:
                        cdp_kwargs["quality"] = int(quality)
                    b64 = await _wait(
                        tab.send(cdp_page.capture_screenshot(**cdp_kwargs)),
                        timeout=10.0, what="screenshot (CDP raw)",
                    )
                    path.write_bytes(base64.b64decode(b64))
                    captured = True
                except Exception as e2:
                    strategy_errors.append(f"cdp_raw(10s): {type(e2).__name__}: {e2}")
            if not captured:
                # Fallback 2: viewport-only JPEG (smallest payload, no
                # full-page stitching). Last resort before giving up.
                try:
                    from nodriver.cdp import page as cdp_page
                    b64 = await _wait(
                        tab.send(cdp_page.capture_screenshot(
                            format_="jpeg", quality=60,
                            capture_beyond_viewport=False,
                        )),
                        timeout=8.0, what="screenshot (viewport JPEG)",
                    )
                    path.write_bytes(base64.b64decode(b64))
                    captured = True
                except Exception as e3:
                    strategy_errors.append(f"viewport_jpeg(8s): {type(e3).__name__}: {e3}")
            if not captured:
                # Probe whether JS is actually responsive — if evaluate()
                # works, the screenshot path itself is the bottleneck (CDP /
                # GPU / profile-state issue), not page JS.
                hint = "browser_recover() to force-restart, or browser_close() + browser_launch(persistent=False)"
                current_url = ""
                try:
                    current_url = await _wait(tab.evaluate("location.href"),
                                              timeout=2.0, what="url probe") or ""
                except Exception:
                    pass
                try:
                    rs = await _wait(tab.evaluate("document.readyState"),
                                      timeout=2.0, what="js probe")
                    if rs:
                        # about:blank has no paintable content — common foot-gun
                        # right after browser_launch with default URL.
                        if current_url in ("about:blank", "", "chrome://newtab/"):
                            hint = (f"page is {current_url or 'about:blank'} — nothing to paint. "
                                    "navigate() to a real URL first, then screenshot().")
                        else:
                            hint = (f"page JS responsive (readyState={rs}, url={current_url}) — "
                                    f"CDP screenshot path is stuck. {hint}")
                    else:
                        hint = f"page JS unresponsive too. {hint}"
                except Exception:
                    hint = f"page JS also unresponsive (evaluate timed out). {hint}"
                return err(
                    "screenshot failed across all 3 strategies. "
                    + " | ".join(strategy_errors) + ". " + hint
                )

        # Auto-downscale if either dimension exceeds max_dimension.
        # Uses cv2 (already a dep via opencv-python). INTER_AREA = best for shrinking.
        resized_info = ""
        if max_dimension and max_dimension > 0:
            try:
                import cv2
                img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                if img is not None:
                    h, w = img.shape[:2]
                    longest = max(h, w)
                    if longest > max_dimension:
                        scale = max_dimension / longest
                        new_w = int(round(w * scale))
                        new_h = int(round(h * scale))
                        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                        if fmt == "jpeg":
                            cv2.imwrite(str(path), resized,
                                        [cv2.IMWRITE_JPEG_QUALITY, int(quality or 85)])
                        else:
                            cv2.imwrite(str(path), resized)
                        resized_info = f" [resized {w}×{h} → {new_w}×{new_h}]"
            except Exception:
                # Resizing is best-effort; original file is already saved.
                pass
        data = path.read_bytes()
        mode = "base64" if return_base64 else output_mode
        if mode == "base64":
            inline, inline_mime, note = _image_output.shrink_for_inline(
                data, "image/png" if fmt == "png" else "image/jpeg", limit=B64_INLINE_LIMIT
            )
            if inline is None:
                return _image_output.image_response(
                    path, data, mode="path", notes=[note, "base64 omitted after bounded downscaling"]
                )
            notes = [resized_info.strip()] if resized_info else []
            if note:
                notes.insert(0, "auto-shrunk: " + note)
            return _image_output.image_response(
                path, data, mode="base64", mime=inline_mime, b64_body=inline, notes=notes
            )
        return _image_output.image_response(
            path, data, mode=mode, mime=("image/png" if fmt == "png" else "image/jpeg"),
            facts={"full_page": full_page, "region": bool(region), "tab": tab},
            notes=[resized_info.strip()] if resized_info else None,
        )
    except Exception as e:
        return err(str(e))


async def _visible_text_of(tab, el) -> str:
    """innerText of a resolved element — excludes script/style/hidden nodes.

    nodriver's `Element.text_all` is textContent-based, so on script-heavy pages
    it returns inlined JS/JSON that a human never sees. innerText is the
    rendering-aware equivalent. Falls back to text_all if the JS call fails.
    """
    try:
        v = await el.apply("function() { return this.innerText; }")
        v = v.value if hasattr(v, "value") and not isinstance(v, str) else v
        if v is not None:
            return str(v)
    except Exception:
        pass
    return el.text_all or ""


@mcp.tool()
async def get_text(
    selector: Optional[str] = None,
    ref: Optional[str] = None,
    raw: bool = False,
    tab: Optional[int] = None,
) -> str:
    """Return visible text without changing active tab selection or focus."""
    try:
        page = select_tab(tab)
        if ref:
            el = await resolve_ref(ref, page)
            if el is None:
                return err(f"ref {ref} not found")
            if raw:
                return ok(el.text_all or "")
            return ok(await _visible_text_of(page, el))
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return err(f"selector not found: {selector}")
            if raw:
                return ok(el.text_all or "")
            return ok(await _visible_text_of(page, el))
        expr = "document.body.textContent" if raw else "document.body.innerText"
        result = await _wait(page.evaluate(expr, return_by_value=True), what="get_text")
        text = result.value if hasattr(result, "value") and not isinstance(result, str) else result
        return ok(str(text or ""))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_html(selector: Optional[str] = None, outer: bool = False) -> str:
    """Return innerHTML of an element, its outerHTML with outer=True, or the whole document."""
    try:
        tab = BrowserState.active_tab()
        if selector:
            el = await tab.query_selector(selector)
            if el is None:
                return err(f"selector not found: {selector}")
            html = await el.apply(
                "function() { return this.%s; }" % ("outerHTML" if outer else "innerHTML")
            )
            return ok(html or "")
        html = await tab.get_content()
        return ok(html or "")
    except Exception as e:
        return err(str(e))


@mcp.tool(name="get_url")
async def get_current_url() -> str:
    """Return current URL of active tab."""
    try:
        tab = BrowserState.active_tab()
        return ok(await get_url(tab, strict=True))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def save_pdf(filename: Optional[str] = None, landscape: bool = False) -> str:
    """Save current page as PDF via CDP Page.printToPDF."""
    try:
        tab = BrowserState.active_tab()
        ensure_dirs()
        fname = filename or ts_filename("page", "pdf")
        path = confined_output_path(EXPORT_DIR, fname)
        from nodriver.cdp import page as cdp_page
        result = await tab.send(cdp_page.print_to_pdf(landscape=landscape))
        # result[0] is base64 data per CDP spec
        data = result[0] if isinstance(result, tuple) else result
        path.write_bytes(base64.b64decode(data))
        return ok(str(path))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 11. INSPECTION
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def inspect_element(ref: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Return tag, attributes, position, text for an element."""
    try:
        tab = BrowserState.active_tab()
        el = await resolve_ref(ref) if ref else (await tab.query_selector(selector) if selector else None)
        if el is None:
            return err("element not found")
        pos = await el.get_position()
        info = {
            "tag": el.tag_name,
            "attributes": dict(el.attrs) if el.attrs else {},
            "text": (el.text_all or "")[:200],
            "position": {
                "x": pos.left, "y": pos.top,
                "width": pos.width, "height": pos.height,
            } if pos else None,
        }
        return ok(json.dumps(info, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_attribute(
    name: str, ref: Optional[str] = None, selector: Optional[str] = None,
) -> str:
    """Get attribute value of element."""
    try:
        tab = BrowserState.active_tab()
        if ref:
            el = await resolve_ref(ref)
            value = await el.apply(
                f"function() {{ var v = this.getAttribute({json.dumps(name)}); "
                f"return v === null ? '__MCP_NULL__' : v; }}"
            )
            return ok("(attribute not set)" if value == "__MCP_NULL__" else str(value))
        sel_for_js = selector
        if not sel_for_js:
            return err("ref or selector required")
        # IIFE: null-check the element (raw querySelector(...).getAttribute
        # throws on a missing element) and map JS null to a non-falsy sentinel
        # so nodriver doesn't hand back a RemoteObject we'd str()-dump.
        js = (
            f"(() => {{ var el = document.querySelector({json.dumps(sel_for_js)}); "
            f"if (!el) return '__MCP_NO_EL__'; "
            f"var v = el.getAttribute({json.dumps(name)}); "
            f"return v === null ? '__MCP_NULL__' : v; }})()"
        )
        result = await tab.evaluate(js, return_by_value=True)
        if hasattr(result, "value") and not isinstance(result, (str, int, float, bool, list, dict)):
            result = result.value
        if result == "__MCP_NO_EL__":
            return err("element not found")
        if result == "__MCP_NULL__" or result is None:
            return ok("null")
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def query_selector_all(selector: str, limit: int = 50) -> str:
    """Return count + attrs of all elements matching CSS selector."""
    try:
        tab = BrowserState.active_tab()
        result = await _eval_checked(
            tab,
            f"(() => {{ var els = document.querySelectorAll({json.dumps(selector)}); "
            f"var out = []; for(var i=0;i<Math.min(els.length,{limit});i++){{"
            "var el=els[i]; var r=el.getBoundingClientRect(); "
            "out.push({tag:el.tagName.toLowerCase(), text:(el.innerText||'').slice(0,80), "
            "href:el.href||'', id:el.id||'', class:el.className||'', x:r.x, y:r.y}); }"
            "return JSON.stringify({count:els.length,items:out}); })()",
        )
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def get_links(same_origin: bool = False, limit: int = 200) -> str:
    """List all <a> links on page."""
    try:
        tab = BrowserState.active_tab()
        js = (
            "(() => { var origin = location.origin; var links = "
            "[...document.querySelectorAll('a[href]')].map(a=>({"
            "text:(a.innerText||'').trim().slice(0,100), href:a.href}))"
            f".filter(l => l.href && (!{str(same_origin).lower()} || l.href.startsWith(origin)))"
            f".slice(0, {limit}); return JSON.stringify(links); }})()"
        )
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 12. FRAMES
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def list_frames() -> str:
    """List every frame on the active tab, cross-origin (out-of-process) ones included."""
    try:
        tab = BrowserState.active_tab()
        frames = await _wait(all_frames(tab), what="list_frames")
        rows = []
        for f in frames:
            if f["frame"] is None:
                rows.append({"id": f["frame_id"], "url": None, "depth": f["depth"],
                             "error": f"not reachable: {f['scan_error']}"})
                continue
            row = {"id": str(f["frame"].id_), "url": f["frame"].url, "depth": f["depth"],
                   "out_of_process": f["conn"] is not tab}
            if f["scan_error"]:
                row["error"] = f"child frames not checked: {f['scan_error']}"
            rows.append(row)
        return ok(json.dumps(rows, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def frame_evaluate(frame_url_pattern: str, expression: str) -> str:
    """Run JS inside the first child frame whose URL matches a regex.

    A same-origin frame runs it in the page's own context. A cross-origin frame
    (reCAPTCHA bframe, payment widgets, third-party embeds) refuses that, so it
    runs in an isolated world instead: the DOM is the same, but the frame's own
    JS globals are not visible. The result says which one happened.
    """
    try:
        tab = BrowserState.active_tab()
        pattern = re.compile(frame_url_pattern)
        # Build the matcher from a string via new RegExp(...) so slashes and
        # other regex/JS-special chars in the pattern can't break the JS literal.
        # Awaited, or a rejected promise stringifies to "{}" and reads as a result.
        result = await tab.evaluate(
            f"(async () => {{ var re = new RegExp({json.dumps(frame_url_pattern)}); "
            f"var iframes = document.querySelectorAll('iframe'); "
            f"for (var f of iframes) {{ if (re.test(f.src)) {{ "
            f"try {{ return JSON.stringify(await f.contentWindow.eval({json.dumps(expression)})); }} "
            f"catch(e){{ return 'ERR:'+e.name+':'+e.message; }} }} }} return 'no frame matched'; }})()",
            await_promise=True, return_by_value=True,
        )
        if isinstance(result, cdp_runtime.ExceptionDetails):
            return err(f"frame_evaluate could not run: {_js_exception_text(result)}")
        result = result.value if hasattr(result, "value") and not isinstance(result, str) else result
        text = str(result)
        cross_origin = text.startswith("ERR:") and (
            "cross-origin" in text.lower() or "SecurityError" in text)
        if not cross_origin and text != "no frame matched":
            if text.startswith("ERR:"):
                return err(f"frame script threw: {text[4:]}")
            return ok(text)
        for found in await _wait(all_frames(tab), what="frame_evaluate"):
            if found["frame"] is None:
                continue
            if found["depth"] == 0 or not pattern.search(str(found["frame"].url or "")):
                continue
            ctx = await frame_world(found["conn"], found["frame"])
            remote, exc = await _wait(found["conn"].send(cdp_runtime.evaluate(
                expression, context_id=ctx, return_by_value=True, await_promise=True,
            )), what="frame_evaluate")
            if exc is not None:
                detail = getattr(exc.exception, "description", None) if exc.exception else None
                return err(f"frame script threw: {detail or exc.text}")
            return ok(f"{json.dumps(remote.value, default=str)}\n"
                      f"(ran in an isolated world of {found['frame'].url}: DOM shared, "
                      f"the frame's own JS globals not visible)")
        return err(f"no frame matched {frame_url_pattern!r}")
    except Exception as e:
        return err(str(e))


# ══════════════════════════════════════════════════════════════════════════
# 18. SCRAPING
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def detect_content_pattern() -> str:
    """Heuristically detect the most likely repeating container on page.

    Useful for scraping job listings, product cards, search results.
    Returns top-3 candidate CSS selectors ranked by child-similarity.
    """
    try:
        tab = BrowserState.active_tab()
        js = r"""
        (() => {
          var groups = {};
          var els = document.querySelectorAll('div,li,article,section');
          for (var el of els) {
            var parent = el.parentElement;
            if (!parent || parent.children.length < 3) continue;
            var sig = parent.tagName + '>' + el.tagName + '.' + (el.className||'').split(' ').slice(0,2).join('.');
            groups[sig] = groups[sig] || {count: 0, sample: el, parent: parent};
            groups[sig].count++;
          }
          var ranked = Object.entries(groups)
            .filter(([,v]) => v.count >= 3)
            .sort((a,b) => b[1].count - a[1].count).slice(0,3);
          return JSON.stringify(ranked.map(([sig,v]) => ({
            signature: sig, count: v.count,
            sample_selector: v.sample.tagName.toLowerCase() +
              (v.sample.className ? '.'+v.sample.className.split(' ').slice(0,2).join('.') : ''),
            parent_selector: v.parent.tagName.toLowerCase() +
              (v.parent.id?'#'+v.parent.id:'') +
              (v.parent.className ? '.'+v.parent.className.split(' ').slice(0,2).join('.') : '')
          })));
        })()
        """
        result = await tab.evaluate(js, return_by_value=True)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def extract_structured(
    container_selector: str,
    fields: list[dict],
    limit: int = 100,
) -> str:
    """Extract structured data from repeating containers.

    fields: [{name: "title", selector: ".job-title", attribute: "text|href|src|..."}]
    Only direct text nodes of element are captured for "text" (prevents child-field mixing).
    """
    if not isinstance(fields, list):
        fields = parse_json(fields, [])
    try:
        tab = BrowserState.active_tab()
        fields_json = json.dumps(fields)
        js = r"""
        (() => {
          var SELECT = """ + json.dumps(container_selector) + r""";
          var FIELDS = """ + fields_json + r""";
          var LIMIT = """ + str(limit) + r""";

          // Filter top-level — skip containers nested inside another same-selector match
          var all = Array.from(document.querySelectorAll(SELECT));
          var tops = all.filter(el => {
            var p = el.parentElement;
            while (p) { if (all.includes(p)) return false; p = p.parentElement; }
            return true;
          }).slice(0, LIMIT);

          function directText(el) {
            var out = '';
            for (var n of el.childNodes) if (n.nodeType === 3) out += n.textContent;
            return out.trim();
          }

          var rows = tops.map(container => {
            var row = {};
            for (var f of FIELDS) {
              var target = container.querySelector(f.selector);
              if (!target) { row[f.name] = null; continue; }
              var attr = f.attribute || 'text';
              if (attr === 'text') row[f.name] = (target.innerText || '').trim();
              else if (attr === 'direct_text_only') row[f.name] = directText(target);
              else if (attr === 'html') row[f.name] = target.innerHTML;
              else row[f.name] = target.getAttribute(attr);
            }
            return row;
          });
          return JSON.stringify(rows);
        })()
        """
        result = await _eval_checked(tab, js)
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def extract_table(selector: str = "table", include_headers: bool = True) -> str:
    """Extract a <table> as JSON rows with optional header keys."""
    try:
        tab = BrowserState.active_tab()
        js = r"""
        (() => {
          var t = document.querySelector(""" + json.dumps(selector) + r""");
          if (!t) return JSON.stringify({error:'table not found'});
          if (t.tagName !== 'TABLE') return JSON.stringify({error:'not a <table> but <' + t.tagName.toLowerCase() + '>'});
          var rows = [...t.querySelectorAll('tr')];
          var headers = [];
          var out = [];
          rows.forEach((r, i) => {
            var cells = [...r.children].map(c => c.innerText.trim());
            if (i === 0 && """ + ('true' if include_headers else 'false') + r""") headers = cells;
            else if (headers.length) {
              var obj = {}; cells.forEach((c, j) => obj[headers[j]||`col${j}`] = c);
              out.push(obj);
            } else out.push(cells);
          });
          return JSON.stringify({headers: headers, rows: out});
        })()
        """
        result = await _eval_checked(tab, js)
        problem = (parse_json(result, {}) or {}).get("error")
        if problem:
            return err(f"extract_table: {problem} ({selector})")
        return ok(str(result))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def scrape_page(
    only_main_content: bool = True,
    max_chars: int = 8000,
    return_mode: Literal["auto", "json", "text", "markdown"] = "auto",
) -> str:
    """Extract readable content, preserving JSON endpoints as native structured data.

    ``return_mode=json`` requires a valid JSON document and returns its parsed value;
    ``auto`` selects that mode when the document content type is JSON. ``text`` and
    ``markdown`` retain the readable envelope used by existing callers.
    """
    if return_mode not in ("auto", "json", "text", "markdown"):
        return err("return_mode must be auto, json, text, or markdown")
    try:
        tab = BrowserState.active_tab()
        raw = await _wait(tab.evaluate(
            "(() => { const root = " + ("document.querySelector(\'main,article,[role=main]\') || document.body" if only_main_content else "document.body") + "; return JSON.stringify({contentType: document.contentType || \'\', title: document.title, url: location.href, body: root ? root.innerText : \'\'}); })()",
            return_by_value=True,
        ), what="scrape_page")
        raw = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        page = parse_json(raw, {})
        if not isinstance(page, dict):
            return err("scrape_page returned invalid page metadata")
        content_type = str(page.get("contentType") or "").lower()
        body = str(page.get("body") or "")
        wants_json = return_mode == "json" or (return_mode == "auto" and "application/json" in content_type)
        if wants_json:
            value = parse_json(body)
            if value is None and body.strip() not in ("null",):
                return err("JSON response could not be parsed; use return_mode='text' to inspect it")
            return structured_json({
                "data": value,
                "status": None,
                "headers": {},
                "url": page.get("url"),
                "content_type": page.get("contentType"),
                "status_measured": False,
            })
        if return_mode == "markdown":
            return ok(body[:max_chars] if max_chars > 0 else body)
        text = body
        if max_chars > 0 and len(text) > max_chars:
            cut = text.rfind("\n", 0, max_chars)
            if cut <= 0:  # no newline in range: -1 would slice almost the whole body back
                cut = max_chars
            text = text[:cut] + f"\n\n[truncated at {cut}/{len(body)} chars]"
        return ok(json.dumps({
            "title": page.get("title"), "url": page.get("url"), "text": text,
            "content_type": page.get("contentType"), "status": None,
            "status_measured": False,
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))
