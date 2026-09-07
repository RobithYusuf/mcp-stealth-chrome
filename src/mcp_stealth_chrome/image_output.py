"""Managed image output — MCP-native delivery, exact element capture, native assets.

Three problems this module exists to fix, all of them observed in real use:

1. A screenshot came back as a viewport-sized PNG of a *browser window showing*
   a 4:5 asset, not the asset. `inspect_image` fetches the bytes the page
   fetched and reports the native width/height/MIME/size/checksum, so a review
   looks at the pixels that were shipped, not at Chrome's wrapper.
2. `Page.captureScreenshot`'s `clip` is in PAGE coordinates (scroll included) —
   verified against a real Chrome, not inferred. Passing a
   `getBoundingClientRect()` rect straight through silently captures the wrong
   strip as soon as the page is scrolled, and the result looks like a valid
   screenshot. `element_clip()` is the single place that conversion happens.
3. A tool result carrying a raw base64 body is unusable for review and burns
   the context it lands in. `ImageResult` keeps the *text* compact and hands the
   bytes over through MCP's own content blocks (an image block, or a resource
   link into the managed screenshot/asset directories).

Nothing here degrades quietly: a capture that comes back viewport-sized when an
element or full page was requested is reported as a failure with the partial
file named, never as a success.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Any, Literal, Optional

from ._app import mcp
from .helpers import confined_output_path, err, ok, select_tab, ts_filename
from .state import SCREENSHOT_DIR, ensure_dirs

# Native assets fetched by inspect_image live beside the screenshots, under the
# same ~/.mcp-stealth root. Declared here rather than in state.py so this module
# stays additive.
ASSET_DIR = SCREENSHOT_DIR.parent / "assets"

# The only directories an image resource / vision scope may read from. Both are
# managed output roots; every name that reaches them goes through
# confined_output_path first, so `..` cannot walk out.
MANAGED_IMAGE_ROOTS: dict[str, Path] = {
    "screenshots": SCREENSHOT_DIR,
    "assets": ASSET_DIR,
}

IMAGE_RESOURCE_SCHEME = "stealth-image"

# Ceiling for an inline image content block, in base64 characters. Clients
# reject oversized results outright and the whole call is wasted, so the inline
# copy is downscaled to fit while the saved file keeps its full resolution.
INLINE_IMAGE_B64_LIMIT = int(os.environ.get("INLINE_IMAGE_B64_LIMIT", "1400000"))
# Longest side of an inline copy. 1568 px is the point above which the major
# vision APIs downscale anyway, so anything larger is bytes spent for nothing.
INLINE_IMAGE_MAX_DIM = int(os.environ.get("INLINE_IMAGE_MAX_DIM", "1568"))
# Hard ceiling on an asset pulled through CDP. Marshalling a huge body as
# base64 over the websocket is how you hang a session.
ASSET_MAX_BYTES = int(os.environ.get("ASSET_MAX_BYTES", str(40 * 1024 * 1024)))

OutputMode = Literal["artifact", "inline_image", "path", "base64"]

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".avif": "image/avif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/avif": ".avif",
    "image/svg+xml": ".svg", "image/x-icon": ".ico",
}


def ensure_image_dirs() -> None:
    ensure_dirs()
    ASSET_DIR.mkdir(parents=True, exist_ok=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


# ── format sniffing ────────────────────────────────────────────────────────
# Header parsing rather than a decode: it is exact, costs nothing, and works
# for formats OpenCV cannot read (SVG, AVIF) where a decode would just report
# "unknown" and make a real asset look unmeasurable.

def sniff_mime(data: bytes) -> Optional[str]:
    """MIME from magic bytes — the only trustworthy source for fetched bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    head = data[:400].lstrip()
    if head[:5] == b"<?xml" and b"<svg" in data[:2000] or head[:4] == b"<svg":
        return "image/svg+xml"
    return None


def image_dimensions(data: bytes, mime: Optional[str] = None) -> tuple[Optional[int], Optional[int], str]:
    """Native pixel size of an encoded image. Returns (width, height, source)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h), "png header"
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h), "gif header"
        if data[:2] == b"BM" and len(data) >= 26:
            w, h = struct.unpack("<ii", data[18:26])
            return int(w), abs(int(h)), "bmp header"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X" and len(data) >= 30:
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h, "webp header"
            if chunk == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h, "webp header"
            if chunk == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "webp header"
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(data[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h, "jpeg header"
                i += 2 + seg_len
    except Exception:
        pass
    # OpenCV as a fallback only — it decodes the whole image and still cannot
    # read SVG/AVIF, so it is the slower and narrower of the two paths.
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is not None:
            return int(img.shape[1]), int(img.shape[0]), "opencv decode"
    except Exception:
        pass
    return None, None, "unmeasured"


def b64_len(n_bytes: int) -> int:
    return (n_bytes + 2) // 3 * 4


def shrink_for_inline(
    data: bytes,
    mime: str,
    limit: int = INLINE_IMAGE_B64_LIMIT,
    max_dim: int = INLINE_IMAGE_MAX_DIM,
) -> tuple[Optional[str], str, str]:
    """Base64 body for an inline image block. Returns (b64, mime, note).

    The saved file is never touched — only the inlined copy shrinks. Returns
    (None, mime, reason) when even the smallest encoding cannot fit, so the
    caller reports that instead of emitting a block the client will reject.
    """
    if b64_len(len(data)) <= limit:
        w, h, _ = image_dimensions(data, mime)
        if not max_dim or not w or not h or max(w, h) <= max_dim:
            return base64.b64encode(data).decode(), mime, ""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None, mime, f"cannot re-encode {mime} to fit the inline limit"
        h0, w0 = img.shape[:2]
        for dim, quality in ((max_dim or 1568, 85), (1280, 75), (1024, 65),
                             (800, 60), (640, 50), (480, 40)):
            longest = max(h0, w0)
            if longest > dim:
                scale = dim / longest
                small = cv2.resize(img, (max(1, int(w0 * scale)), max(1, int(h0 * scale))),
                                   interpolation=cv2.INTER_AREA)
            else:
                small = img
            enc_ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not enc_ok:
                continue
            candidate = buf.tobytes()
            if b64_len(len(candidate)) <= limit:
                sh, sw = small.shape[:2]
                note = ""
                if (sw, sh) != (w0, h0) or mime != "image/jpeg":
                    note = (f"inline copy re-encoded to {sw}×{sh} jpeg q{quality} "
                            f"to fit the {limit:,}-char inline limit; "
                            f"the saved file is untouched")
                return base64.b64encode(candidate).decode(), "image/jpeg", note
    except Exception as exc:
        return None, mime, f"inline downscale failed: {type(exc).__name__}: {exc}"
    return None, mime, (f"~{b64_len(len(data)):,} base64 chars still exceeds the "
                        f"{limit:,} inline limit even after downscaling")


# ── managed resource plumbing ──────────────────────────────────────────────

def managed_area(path: Path) -> Optional[str]:
    """Which managed root holds this file, if any."""
    resolved = Path(path).resolve()
    for area, root in MANAGED_IMAGE_ROOTS.items():
        try:
            if root.resolve() in resolved.parents:
                return area
        except OSError:
            continue
    return None


def resource_uri_for(path: Path) -> Optional[str]:
    area = managed_area(path)
    if not area:
        return None
    return f"{IMAGE_RESOURCE_SCHEME}://{area}/{urllib.parse.quote(Path(path).name)}"


def read_managed_image(area: str, name: str) -> bytes:
    """Read one file out of a managed image root. Rejects any path escape."""
    root = MANAGED_IMAGE_ROOTS.get(area)
    if root is None:
        raise ValueError(f"unknown image area {area!r}; expected one of "
                         f"{', '.join(sorted(MANAGED_IMAGE_ROOTS))}")
    target = confined_output_path(root, urllib.parse.unquote(name))
    if target.parent.resolve() != root.resolve():
        raise ValueError(f"image name must be a direct child of {root}: {name!r}")
    if not target.is_file():
        raise ValueError(f"no such managed image: {area}/{name}")
    return target.read_bytes()


def _register_image_resource(suffix: str, mime: str, fn_name: str) -> None:
    """One resource template per extension — the read result's mimeType is fixed
    at registration time, so a single template would have to lie about JPEGs."""

    @mcp.resource(
        f"{IMAGE_RESOURCE_SCHEME}://{{area}}/{{name}}{suffix}",
        name=fn_name,
        description=(f"A managed {mime} image under ~/.mcp-stealth/<area>/ "
                     f"(area is 'screenshots' or 'assets'). Read-only; names "
                     f"cannot escape their directory."),
        mime_type=mime,
    )
    def _read(area: str, name: str) -> bytes:  # pragma: no cover - thin wrapper
        return read_managed_image(area, f"{name}{suffix}")

    _read.__name__ = fn_name


for _suffix, _mime in ((".png", "image/png"), (".jpg", "image/jpeg"),
                       (".jpeg", "image/jpeg"), (".webp", "image/webp"),
                       (".gif", "image/gif")):
    _register_image_resource(_suffix, _mime, f"managed_image{_suffix.replace('.', '_')}")


class ImageResult(str):
    """A tool result that is text first and an image second.

    Every tool in this server is annotated `-> str` and returns through
    `ok()`/`err()`; that contract is what keeps FastMCP from raising, so this is
    a `str` subclass rather than a new return type. The string body is the
    compact, human-readable summary — path, dimensions, size, checksum — and
    never a base64 blob. `to_mcp_content()` is what turns it into real MCP
    content blocks (the app layer calls it when it sees one of these).
    """

    __slots__ = ("path", "mime", "width", "height", "size_bytes", "sha256",
                 "delivery", "inline_b64", "resource_uri", "facts")

    def __new__(
        cls,
        text: str,
        *,
        path: Optional[str] = None,
        mime: str = "image/png",
        width: Optional[int] = None,
        height: Optional[int] = None,
        size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        delivery: str = "artifact",
        inline_b64: Optional[str] = None,
        resource_uri: Optional[str] = None,
        facts: Optional[dict[str, Any]] = None,
    ) -> "ImageResult":
        obj = super().__new__(cls, text)
        obj.path = path
        obj.mime = mime
        obj.width = width
        obj.height = height
        obj.size_bytes = size_bytes
        obj.sha256 = sha256
        obj.delivery = delivery
        obj.inline_b64 = inline_b64
        obj.resource_uri = resource_uri
        obj.facts = facts or {}
        return obj

    def to_mcp_content(self) -> list[Any]:
        """MCP content blocks: metadata text, then the image itself.

        - inline_image → TextContent + ImageContent (bytes in the image block,
          never in the text).
        - artifact     → TextContent + ResourceLink to the managed file, so the
          client can fetch the pixels on demand and the transcript stays small.
        - path/base64  → TextContent only; those modes are already self-contained.
        """
        from mcp.types import ImageContent, ResourceLink, TextContent

        blocks: list[Any] = [TextContent(type="text", text=str(self))]
        if self.delivery == "inline_image" and self.inline_b64:
            blocks.append(ImageContent(type="image", data=self.inline_b64,
                                       mimeType=self.mime))
        elif self.delivery == "artifact" and self.resource_uri:
            name = Path(self.path).name if self.path else "image"
            description = f"{self.width or '?'}×{self.height or '?'} {self.mime}"
            if self.facts.get("scope"):
                description += f" · {self.facts['scope']}"
            blocks.append(ResourceLink(
                type="resource_link",
                uri=self.resource_uri,
                name=name,
                mimeType=self.mime,
                description=description,
                size=self.size_bytes,
            ))
        return blocks


def image_response(
    path: Path,
    data: bytes,
    *,
    mode: str = "artifact",
    mime: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    facts: Optional[dict[str, Any]] = None,
    notes: Optional[list[str]] = None,
    b64_body: Optional[str] = None,
) -> str:
    """Render one saved image as a tool result in the requested output mode.

    The first line is always the file path: every existing caller (and test)
    that greps the result for a filename keeps working.
    """
    mime = mime or sniff_mime(data) or _MIME_BY_EXT.get(path.suffix.lower(), "image/png")
    if width is None or height is None:
        width, height, _ = image_dimensions(data, mime)
    facts = dict(facts or {})
    notes = list(notes or [])

    detail = (f"image: {width or '?'}×{height or '?'} px · {mime} · "
              f"{human_bytes(len(data))} · sha256:{sha256_hex(data)[:16]}")
    lines = [str(path), detail]
    if facts:
        lines.append(" · ".join(f"{k}={v}" for k, v in facts.items()))
    uri = resource_uri_for(path)

    if mode == "path":
        lines.extend(notes)
        return ok("\n".join(lines))

    if mode == "base64":
        body = b64_body if b64_body is not None else base64.b64encode(data).decode()
        lines.extend(notes)
        return ok("\n".join(lines) + f"\n---base64---\n{body}")

    if mode == "inline_image":
        inline, inline_mime, note = shrink_for_inline(data, mime)
        if note:
            notes.append(note)
        if inline is None:
            notes.append("delivered as an artifact link instead of an inline image")
            lines.extend(notes)
            if uri:
                lines.append(f"resource: {uri}")
            return ImageResult(
                "\n".join(lines), path=str(path), mime=mime, width=width,
                height=height, size_bytes=len(data), sha256=sha256_hex(data),
                delivery="artifact", resource_uri=uri, facts=facts,
            )
        lines.extend(notes)
        if uri:
            lines.append(f"resource: {uri}")
        return ImageResult(
            "\n".join(lines), path=str(path), mime=inline_mime, width=width,
            height=height, size_bytes=len(data), sha256=sha256_hex(data),
            delivery="inline_image", inline_b64=inline, resource_uri=uri, facts=facts,
        )

    # artifact (default): compact text + a link the client can fetch.
    if uri:
        lines.append(f"resource: {uri}")
    else:
        notes.append("file is outside the managed image roots — no resource link")
    lines.extend(notes)
    return ImageResult(
        "\n".join(lines), path=str(path), mime=mime, width=width, height=height,
        size_bytes=len(data), sha256=sha256_hex(data), delivery="artifact",
        resource_uri=uri, facts=facts,
    )


# ── capture geometry ───────────────────────────────────────────────────────

_BOX_JS = """
(() => {
  const el = %(expr)s;
  if (!el) return JSON.stringify({found: false});
  if (%(scroll)s) {
    const root = document.documentElement;
    const prev = root.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    try { el.scrollIntoView({block: 'center', inline: 'center'}); }
    catch (e) { el.scrollIntoView(); }
    root.style.scrollBehavior = prev;
  }
  const r = el.getBoundingClientRect();
  const vv = window.visualViewport;
  const body = document.body;
  return JSON.stringify({
    found: true,
    x: r.left, y: r.top, w: r.width, h: r.height,
    pageLeft: vv ? vv.pageLeft : window.scrollX,
    pageTop: vv ? vv.pageTop : window.scrollY,
    dpr: window.devicePixelRatio || 1,
    innerW: window.innerWidth, innerH: window.innerHeight,
    docW: Math.max(root_w(), body ? body.scrollWidth : 0),
    docH: Math.max(root_h(), body ? body.scrollHeight : 0),
    tag: (el.tagName || '').toLowerCase(),
    naturalW: el.naturalWidth || null, naturalH: el.naturalHeight || null,
    src: el.currentSrc || el.src || null,
    hidden: !(r.width > 0 && r.height > 0)
  });
  function root_w() { return document.documentElement.scrollWidth; }
  function root_h() { return document.documentElement.scrollHeight; }
})()
"""


def _unwrap(value: Any) -> Any:
    """nodriver hands back a RemoteObject even with return_by_value=True."""
    return value.value if hasattr(value, "value") and not isinstance(value, str) else value


async def measure_box(
    tab: Any,
    *,
    selector: Optional[str] = None,
    ref: Optional[str] = None,
    scroll_into_view: bool = True,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Measure an element's box plus the scroll/DPR context a clip needs."""
    from .server import _wait

    if ref and not selector:
        expr = f'document.querySelector({json.dumps(f"[data-mcp-ref={ref!r}]".replace(chr(39), chr(34)))})'
    elif selector:
        expr = f"document.querySelector({json.dumps(selector)})"
    else:
        raise ValueError("selector or ref is required")
    js = _BOX_JS % {"expr": expr, "scroll": "true" if scroll_into_view else "false"}
    raw = _unwrap(await _wait(tab.evaluate(js, return_by_value=True),
                              timeout=timeout, what="element box measurement"))
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"could not measure element box: {exc}; got {str(raw)[:160]!r}")


async def viewport_context(tab: Any, timeout: Optional[float] = None) -> dict[str, Any]:
    """Scroll offsets, DPR and document size — the inputs a page-space clip needs."""
    from .server import _wait

    js = ("JSON.stringify({pageLeft: window.visualViewport ? window.visualViewport.pageLeft"
          " : window.scrollX, pageTop: window.visualViewport ? window.visualViewport.pageTop"
          " : window.scrollY, dpr: window.devicePixelRatio || 1,"
          " innerW: window.innerWidth, innerH: window.innerHeight,"
          " docW: document.documentElement.scrollWidth,"
          " docH: document.documentElement.scrollHeight})")
    raw = _unwrap(await _wait(tab.evaluate(js, return_by_value=True),
                              timeout=timeout, what="viewport measurement"))
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return {"pageLeft": 0, "pageTop": 0, "dpr": 1, "innerW": 0, "innerH": 0}


def element_clip(box: dict[str, Any], *, exact_bounds: bool = True,
                 padding: float = 0.0) -> dict[str, float]:
    """Viewport-space rect + scroll offset → the PAGE-space clip CDP expects.

    Verified against Chrome (see the module docstring): a clip built from a bare
    getBoundingClientRect() captures the wrong strip the moment the page is
    scrolled, and the wrong strip still looks like a valid screenshot.
    """
    x = float(box["x"]) + float(box.get("pageLeft", 0) or 0)
    y = float(box["y"]) + float(box.get("pageTop", 0) or 0)
    w = float(box["w"])
    h = float(box["h"])
    if not exact_bounds:
        x, y = x - padding, y - padding
        w, h = w + 2 * padding, h + 2 * padding
        x, y = float(int(x)), float(int(y))
        w, h = float(int(w + 0.999)), float(int(h + 0.999))
    return {"x": max(0.0, x), "y": max(0.0, y), "width": w, "height": h}


def viewport_clip(region: dict[str, Any], context: dict[str, Any]) -> dict[str, float]:
    """Caller-supplied viewport-relative region → page-space clip."""
    return {
        "x": max(0.0, float(region.get("x", 0)) + float(context.get("pageLeft", 0) or 0)),
        "y": max(0.0, float(region.get("y", 0)) + float(context.get("pageTop", 0) or 0)),
        "width": float(region["width"]),
        "height": float(region["height"]),
    }


async def capture_clip(
    tab: Any,
    clip: dict[str, float],
    *,
    fmt: str = "png",
    quality: Optional[int] = None,
    scale: float = 1.0,
    timeout: Optional[float] = None,
) -> bytes:
    """Capture one page-space clip. Coordinates must already be page-space."""
    from nodriver.cdp import page as cdp_page

    from .server import _wait

    viewport = cdp_page.Viewport(
        x=float(clip["x"]), y=float(clip["y"]),
        width=float(clip["width"]), height=float(clip["height"]),
        scale=float(scale),
    )
    kwargs: dict[str, Any] = {
        "format_": fmt, "clip": viewport, "capture_beyond_viewport": True,
    }
    if fmt == "jpeg" and quality is not None:
        kwargs["quality"] = int(quality)
    body = await _wait(tab.send(cdp_page.capture_screenshot(**kwargs)),
                       timeout=timeout, what="clipped screenshot")
    return base64.b64decode(body)


def verify_capture(
    data: bytes,
    clip: dict[str, float],
    dpr: float,
    scale: float,
    context: dict[str, Any],
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Did we get the box we asked for? Returns (width, height, complaint).

    Chrome scales a clipped capture by the device pixel ratio (measured, not
    assumed), so the expected size is clip × dpr × scale. A capture that comes
    back viewport-sized is the failure mode this guards: it looks like a good
    screenshot and is not the element.
    """
    width, height, _ = image_dimensions(data)
    if width is None or height is None:
        return width, height, None
    expected_w = round(clip["width"] * dpr * scale)
    expected_h = round(clip["height"] * dpr * scale)
    if abs(width - expected_w) <= 2 and abs(height - expected_h) <= 2:
        return width, height, None
    vw = round(float(context.get("innerW") or 0) * dpr)
    vh = round(float(context.get("innerH") or 0) * dpr)
    if vw and vh and abs(width - vw) <= 2 and abs(height - vh) <= 2:
        return width, height, (
            f"capture came back viewport-sized ({width}×{height}px) instead of the "
            f"requested {expected_w}×{expected_h}px clip — this is a viewport "
            f"screenshot, not the region you asked for"
        )
    return width, height, (
        f"capture is {width}×{height}px but the requested clip is "
        f"{clip['width']:.0f}×{clip['height']:.0f} CSS px at dpr {dpr:g} "
        f"(expected {expected_w}×{expected_h}px)"
    )


# ── scope capture (shared with the vision tools) ───────────────────────────

async def capture_scope(
    tab: Any,
    *,
    scope: str = "viewport",
    selector: Optional[str] = None,
    ref: Optional[str] = None,
    image_path: Optional[str] = None,
    fmt: str = "png",
    quality: Optional[int] = None,
    timeout: Optional[float] = None,
) -> tuple[bytes, str, dict[str, Any]]:
    """Bytes for one review scope. Returns (data, mime, meta).

    scope="image" reads a file that is already in a managed root — that is how a
    native asset from `inspect_image` gets reviewed instead of a screenshot of
    the browser window wrapped around it.
    """
    from nodriver.cdp import page as cdp_page

    from .server import _wait

    meta: dict[str, Any] = {"scope": scope}
    if scope == "image":
        if not image_path:
            raise ValueError("scope='image' needs image_path")
        path = Path(image_path).expanduser()
        area = managed_area(path)
        if area is None:
            raise ValueError(
                f"image_path must be inside a managed image root "
                f"({', '.join(str(p) for p in MANAGED_IMAGE_ROOTS.values())}); "
                f"capture it with screenshot()/inspect_image() first"
            )
        data = read_managed_image(area, path.name)
        mime = sniff_mime(data) or _MIME_BY_EXT.get(path.suffix.lower(), "image/png")
        meta.update({"source": str(path), "area": area})
        return data, mime, meta

    if scope == "element":
        if not (selector or ref):
            raise ValueError("scope='element' needs selector or ref")
        box = await measure_box(tab, selector=selector, ref=ref, timeout=timeout)
        if not box.get("found"):
            raise ValueError(f"element not found: {selector or ref!r}")
        if box.get("hidden"):
            raise ValueError(
                f"element {selector or ref!r} has zero size "
                f"({box['w']}×{box['h']}) — nothing to capture")
        clip = element_clip(box)
        data = await capture_clip(tab, clip, fmt=fmt, quality=quality, timeout=timeout)
        width, height, complaint = verify_capture(
            data, clip, float(box.get("dpr") or 1), 1.0, box)
        if complaint:
            raise RuntimeError(complaint)
        meta.update({"selector": selector or ref, "css_box": f"{box['w']:.0f}×{box['h']:.0f}",
                     "captured": f"{width}×{height}", "dpr": box.get("dpr")})
        return data, "image/png" if fmt == "png" else "image/jpeg", meta

    full = scope == "full_page"
    kwargs: dict[str, Any] = {"format_": fmt, "capture_beyond_viewport": full}
    if fmt == "jpeg" and quality is not None:
        kwargs["quality"] = int(quality)
    body = await _wait(tab.send(cdp_page.capture_screenshot(**kwargs)),
                       timeout=timeout, what=f"{scope} screenshot")
    data = base64.b64decode(body)
    width, height, _ = image_dimensions(data)
    meta.update({"captured": f"{width}×{height}"})
    if full:
        context = await viewport_context(tab, timeout=timeout)
        dpr = float(context.get("dpr") or 1)
        expected_h = round(float(context.get("docH") or 0) * dpr)
        viewport_h = round(float(context.get("innerH") or 0) * dpr)
        if (height and expected_h and viewport_h and expected_h - viewport_h > 4
                and abs(height - viewport_h) <= 2 and abs(height - expected_h) > 4):
            raise RuntimeError(
                f"full_page capture came back viewport-sized ({width}×{height}px); "
                f"the document is {context.get('docH')} CSS px tall — the capture "
                f"silently fell back to the viewport")
    return data, "image/png" if fmt == "png" else "image/jpeg", meta


# ══════════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def screenshot_element(
    selector: Optional[str] = None,
    ref: Optional[str] = None,
    exact_bounds: bool = True,
    padding: int = 0,
    scroll_into_view: bool = True,
    filename: Optional[str] = None,
    format: Literal["png", "jpeg"] = "png",
    quality: Optional[int] = None,
    scale: float = 1.0,
    tab: Optional[int] = None,
    output: OutputMode = "artifact",
) -> str:
    """⭐ Capture exactly one element's box — scroll- and DPR-correct.

    `screenshot(region_selector=...)` measures the element for you but the clip
    is built here, in page coordinates: CDP's clip is page-space (scroll
    included), so a bare getBoundingClientRect() rect captures the wrong strip
    on any scrolled page — and the wrong strip still looks like a screenshot.

    The result is verified against the box that was requested. If the capture
    comes back viewport-sized (the classic silent degradation), this fails and
    says so instead of handing you a browser-window screenshot labelled as an
    element.

    Args:
        selector: CSS selector for the element. Or pass `ref` from browser_snapshot.
        exact_bounds: clip to the element's exact border box (default). False
            rounds outward to whole CSS pixels and honours `padding`.
        padding: extra CSS px of context on each side (needs exact_bounds=False)
        scroll_into_view: scroll the element to the middle of the viewport first.
            Off-viewport elements still capture correctly either way; scrolling
            mainly matters for lazy-loaded images and hover/sticky layouts.
        scale: multiplies the captured resolution on top of the page's DPR
        tab: tab index (from tab_list) to capture WITHOUT switching to it.
            Chrome throttles background renderers, so a background tab may hand
            back its last painted frame.
        output: "artifact" (default: compact text + a resource link),
            "inline_image" (an MCP image block), "path", or "base64".

    Returns the saved path, the native pixel size, the CSS box, and the checksum.
    """
    from .server import TOOL_ACTION_TIMEOUT

    try:
        if not (selector or ref):
            return err("screenshot_element needs a selector or a snapshot ref")
        try:
            target = select_tab(tab)
        except ValueError as exc:
            return err(str(exc))
        ensure_image_dirs()
        fmt = format
        fname = filename or ts_filename("element", "png" if fmt == "png" else "jpg")
        path = confined_output_path(SCREENSHOT_DIR, fname)

        box = await measure_box(target, selector=selector, ref=ref,
                                scroll_into_view=scroll_into_view)
        if not box.get("found"):
            return err(f"element not found: {selector or ref!r}")
        if box.get("hidden"):
            return err(f"element {selector or ref!r} has zero size "
                       f"({box['w']}×{box['h']}) — it is not rendered, so there is "
                       f"nothing to capture")
        clip = element_clip(box, exact_bounds=exact_bounds, padding=float(padding))
        data = await capture_clip(target, clip, fmt=fmt, quality=quality,
                                  scale=scale, timeout=TOOL_ACTION_TIMEOUT)
        dpr = float(box.get("dpr") or 1)
        width, height, complaint = verify_capture(data, clip, dpr, float(scale), box)
        path.write_bytes(data)
        if complaint:
            return err(f"{complaint}. The partial capture is saved at {path} so you "
                       f"can look at it, but do NOT treat it as "
                       f"{selector or ref!r}.")
        facts = {
            "scope": f"element {selector or ('ref=' + str(ref))}",
            "css_box": f"{box['w']:.0f}×{box['h']:.0f}",
            "dpr": f"{dpr:g}",
            "page_clip": f"({clip['x']:.0f},{clip['y']:.0f})",
        }
        if tab is not None:
            facts["tab"] = tab
        return image_response(path, data, mode=output, width=width, height=height,
                              facts=facts)
    except Exception as exc:
        return err(str(exc))


@mcp.tool()
async def inspect_image(
    url: Optional[str] = None,
    selector: Optional[str] = None,
    tab: Optional[int] = None,
    filename: Optional[str] = None,
    fit: Literal["none", "contain"] = "none",
    max_dimension: int = 0,
    output: OutputMode = "artifact",
    max_bytes: int = ASSET_MAX_BYTES,
    timeout_s: float = 60.0,
) -> str:
    """⭐ Fetch an image's NATIVE bytes — not a screenshot of the page showing it.

    Opening a PNG in Chrome and calling screenshot() gives you a viewport-sized
    picture of a browser window with the asset scaled inside it: the wrapper is
    in the pixels and the asset's real size is nowhere in the result. This
    fetches the bytes the page fetched (in-page `fetch` with the session's
    cookies, so authenticated assets work) and reports what they actually are.

    fit="none" (default) saves the bytes byte-for-byte: the checksum in the
    result is the checksum of the file on disk and of the asset itself. Nothing
    is resized, re-encoded, or re-compressed. fit="contain" only affects an
    inline copy, never the saved file.

    Args:
        url: absolute or page-relative image URL. `data:` and `file:` work too.
        selector: CSS selector of an <img> (or any element with src/href) to
            read the URL from — also reports displayed vs native size.
        tab: tab index to read from WITHOUT switching to it
        filename: name under ~/.mcp-stealth/assets/ (default derived from the URL)
        fit: "none" keeps every pixel (default); "contain" allows the inline copy
            to be downscaled to max_dimension
        max_dimension: longest side for the inline copy when fit="contain"
        output: "artifact" (default), "inline_image", "path", or "base64"
        max_bytes: refuse assets larger than this rather than marshalling them
            through CDP

    Returns: source URL, HTTP status, MIME (from the bytes, not just the header),
    native width×height, byte size, sha256, and the saved path.
    """
    from .server import TOOL_ACTION_TIMEOUT

    try:
        try:
            target = select_tab(tab)
        except ValueError as exc:
            return err(str(exc))
        ensure_image_dirs()

        source, displayed, hints = await _resolve_image_source(target, url, selector)
        if not source:
            return err(hints)

        data, fetch_meta = await _fetch_image_bytes(
            target, source, max_bytes=max_bytes, timeout_s=timeout_s)
        if isinstance(fetch_meta, str):
            return err(fetch_meta)

        sniffed = sniff_mime(data)
        header_mime = (fetch_meta.get("mime") or "").split(";")[0].strip()
        if sniffed is None:
            preview = data[:80]
            return err(
                f"{source} did not return an image: {len(data)} bytes, "
                f"content-type {header_mime or 'unknown'!r}, status "
                f"{fetch_meta.get('status', '?')}, starts with {preview!r}. "
                f"Nothing was saved."
            )
        mime = sniffed
        width, height, dim_source = image_dimensions(data, mime)

        stem = _asset_filename(source, mime)
        path = confined_output_path(ASSET_DIR, filename or stem)
        path.write_bytes(data)

        facts: dict[str, Any] = {
            "source": source,
            "status": fetch_meta.get("status", "?"),
            "native": f"{width or '?'}×{height or '?'}",
            "fit": fit,
            "dims_from": dim_source,
        }
        if tab is not None:
            facts["tab"] = tab
        notes: list[str] = []
        if header_mime and header_mime != mime:
            notes.append(f"content-type header said {header_mime}, the bytes are {mime}")
        if displayed:
            facts["displayed"] = f"{displayed.get('dispW')}×{displayed.get('dispH')}"
            nat_w = displayed.get("naturalW")
            nat_h = displayed.get("naturalH")
            if nat_w and width and int(nat_w) != int(width):
                notes.append(f"the DOM reports naturalWidth {nat_w} but the bytes decode "
                             f"to {width} — the page may be showing a different asset")
            elif nat_w and nat_h:
                facts["dom_natural"] = f"{nat_w}×{nat_h}"
        if fit == "none":
            notes.append("fit=none: saved byte-for-byte, checksum is the asset's own")
        elif max_dimension:
            notes.append(f"fit=contain: the saved file is still untouched; only an "
                         f"inline copy is limited to {max_dimension}px")
        if hints:
            notes.append(hints)
        return image_response(path, data, mode=output, mime=mime, width=width,
                              height=height, facts=facts, notes=notes)
    except Exception as exc:
        return err(str(exc))


_SOURCE_JS = """
(() => {
  const out = {docUrl: location.href, contentType: document.contentType || '',
               imgCount: (document.images || []).length};
  const first = (document.images || [])[0];
  if (first) out.first = {src: first.currentSrc || first.src || '',
                          naturalW: first.naturalWidth || null,
                          naturalH: first.naturalHeight || null,
                          dispW: first.clientWidth, dispH: first.clientHeight};
  return JSON.stringify(out);
})()
"""

_SELECTOR_SRC_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return JSON.stringify({found: false});
  let src = el.currentSrc || el.src || el.href || '';
  if (!src) {
    const bg = getComputedStyle(el).backgroundImage || '';
    const m = bg.match(/url\\(["']?(.*?)["']?\\)/);
    if (m) src = m[1];
  }
  return JSON.stringify({found: true, src: src ? new URL(src, location.href).href : '',
                         naturalW: el.naturalWidth || null, naturalH: el.naturalHeight || null,
                         dispW: el.clientWidth, dispH: el.clientHeight,
                         tag: el.tagName.toLowerCase()});
})()
"""


async def _resolve_image_source(
    tab: Any, url: Optional[str], selector: Optional[str],
) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
    """Work out which image to fetch. Returns (url, displayed_info, note_or_error)."""
    from .server import _wait

    if selector:
        raw = _unwrap(await _wait(
            tab.evaluate(_SELECTOR_SRC_JS % json.dumps(selector), return_by_value=True),
            what="image selector lookup"))
        info = json.loads(str(raw))
        if not info.get("found"):
            return None, None, f"selector not found: {selector!r}"
        if not info.get("src"):
            return None, None, (f"{selector!r} ({info.get('tag')}) has no src/href/"
                                f"background-image to fetch")
        return info["src"], info, ""
    if url:
        if url.startswith(("http://", "https://", "data:", "file://", "blob:")):
            return url, None, ""
        page_url = _unwrap(await _wait(tab.evaluate("location.href", return_by_value=True),
                                       what="page url"))
        try:
            return urllib.parse.urljoin(str(page_url), url), None, ""
        except Exception:
            return url, None, ""

    raw = _unwrap(await _wait(tab.evaluate(_SOURCE_JS, return_by_value=True),
                              what="image document probe"))
    info = json.loads(str(raw))
    content_type = str(info.get("contentType") or "")
    first = info.get("first") or {}
    if content_type.startswith("image/") and first.get("src"):
        return first["src"], first, ""
    if info.get("imgCount") == 1 and first.get("src"):
        return first["src"], first, ("no url/selector given — used the page's only "
                                     "<img>; pass selector= to be explicit")
    detail = content_type or "not an image document"
    return None, None, (
        f"nothing to inspect: {info.get('docUrl')} is {detail} and has "
        f"{info.get('imgCount', 0)} <img> elements. Pass url= or selector=."
    )


_FETCH_JS = """
(async () => {
  const url = %(url)s;
  try {
    const resp = await fetch(url, {credentials: 'include'});
    const buf = new Uint8Array(await resp.arrayBuffer());
    if (buf.length > %(max)d) {
      return JSON.stringify({ok: false, tooBig: true, len: buf.length,
                             status: resp.status});
    }
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < buf.length; i += CH) {
      s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
    }
    window.__mcpAssetB64 = btoa(s);
    return JSON.stringify({ok: true, status: resp.status, len: buf.length,
                           b64len: window.__mcpAssetB64.length,
                           mime: resp.headers.get('content-type') || '',
                           finalUrl: resp.url});
  } catch (e) {
    return JSON.stringify({ok: false, error: String(e && e.message || e)});
  }
})()
"""

_B64_CHUNK = 700_000


async def _fetch_image_bytes(
    tab: Any, source: str, *, max_bytes: int, timeout_s: float,
) -> tuple[bytes, Any]:
    """Fetch the asset's real bytes. Returns (data, meta) or (b"", error string)."""
    from .server import _wait

    if source.startswith("data:"):
        head, _, body = source.partition(",")
        try:
            data = base64.b64decode(body) if ";base64" in head else urllib.parse.unquote_to_bytes(body)
        except Exception as exc:
            return b"", f"could not decode the data: URI: {exc}"
        mime = head[5:].split(";")[0]
        return data, {"status": "data:", "mime": mime, "final_url": "data:"}

    if source.startswith("file://"):
        path = Path(urllib.parse.unquote(urllib.parse.urlparse(source).path))
        if not path.is_file():
            return b"", f"no such file: {path}"
        size = path.stat().st_size
        if size > max_bytes:
            return b"", f"{path} is {human_bytes(size)}, over the {human_bytes(max_bytes)} limit"
        return path.read_bytes(), {"status": "file://", "mime": "", "final_url": source}

    raw = _unwrap(await _wait(
        tab.evaluate(_FETCH_JS % {"url": json.dumps(source), "max": int(max_bytes)},
                     await_promise=True, return_by_value=True),
        timeout=timeout_s, what="in-page asset fetch"))
    try:
        meta = json.loads(str(raw))
    except (TypeError, ValueError):
        return b"", f"in-page fetch returned no JSON: {str(raw)[:200]!r}"
    if meta.get("tooBig"):
        return b"", (f"{source} is {human_bytes(int(meta['len']))}, over the "
                     f"{human_bytes(max_bytes)} max_bytes limit — raise max_bytes "
                     f"if you really want it")
    if not meta.get("ok"):
        return b"", (f"in-page fetch of {source} failed: {meta.get('error', 'unknown')}"
                     f" — a cross-origin asset without CORS headers cannot be read "
                     f"this way; open it in a tab and inspect_image() there")

    total = int(meta.get("b64len") or 0)
    chunks: list[str] = []
    for start in range(0, total, _B64_CHUNK):
        piece = _unwrap(await _wait(
            tab.evaluate(f"window.__mcpAssetB64.slice({start},{start + _B64_CHUNK})",
                         return_by_value=True),
            timeout=timeout_s, what="asset chunk read"))
        chunks.append(str(piece or ""))
    try:
        await tab.evaluate("void (delete window.__mcpAssetB64)", return_by_value=True)
    except Exception:
        pass
    body = "".join(chunks)
    if len(body) != total:
        return b"", (f"asset transfer incomplete: got {len(body)} of {total} base64 "
                     f"chars from {source} — nothing was saved")
    try:
        data = base64.b64decode(body)
    except Exception as exc:
        return b"", f"asset base64 from {source} did not decode: {exc}"
    if len(data) != int(meta.get("len") or len(data)):
        return b"", (f"asset size mismatch: the page read {meta.get('len')} bytes but "
                     f"{len(data)} arrived — nothing was saved")
    return data, {"status": meta.get("status", "?"), "mime": meta.get("mime", ""),
                  "final_url": meta.get("finalUrl", source)}


def _asset_filename(source: str, mime: str) -> str:
    """A managed, collision-free name derived from the source URL."""
    parsed = urllib.parse.urlparse(source)
    stem = Path(urllib.parse.unquote(parsed.path or "")).stem or "asset"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[:60].strip("-.") or "asset"
    ext = _EXT_BY_MIME.get(mime, ".bin")
    return f"{stem}-{int(time.time() * 1000)}{ext}"
