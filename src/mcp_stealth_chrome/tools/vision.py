"""Section 21 + 25 (vision parts) — AI vision captcha + element locator.

Two MCP tools:
- solve_recaptcha_ai: vision-LLM tile picker for reCAPTCHA v2 image grids
- vision_locate: NL → element coordinates via vision LLM

Both share the provider-resolution helper _resolve_vision_provider, which
auto-detects OpenAI-compat vs Anthropic from env (OPENAI_API_KEY /
OPENAI_BASE_URL / OPENAI_MODEL or ANTHROPIC_API_KEY / ANTHROPIC_MODEL),
or accepts explicit overrides per call.

Module-private helpers (still imported by tests / re-exported from
server.py for backward compat):
- _PROMPT_TEMPLATE              — tile-grid prompt for solve_recaptcha_ai
- _VISION_LOCATE_PROMPT         — NL-locator prompt
- _parse_tile_indices           — legacy bare-array fallback
- _parse_vision_response        — robust JSON extraction
- _claude_vision_pick_tiles     — Anthropic call
- _openai_compat_vision_pick_tiles — OpenAI-compat call
- _resolve_vision_provider      — env / arg resolution
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import json
import os
import re as _re
from pathlib import Path
from typing import Optional

import httpx

from .._app import mcp
from ..helpers import err, ok, parse_json, ts_filename
from ..humanize import humanized_move
from ..state import SCREENSHOT_DIR, BrowserState, ensure_dirs


_PROMPT_TEMPLATE = (
    "Image analysis task. The screenshot contains a tile grid overlay.\n"
    "At the top a blue banner states the target category.\n\n"
    "Two possible layouts:\n"
    "  3x3 layout — 9 separate photos (banner says 'all IMAGES with <X>')\n"
    "  4x4 layout — 16 segments of one photo (banner says 'all SQUARES with <X>')\n\n"
    "Identify the layout and return indices of every tile that visibly contains\n"
    "the target category. Include partial/edge matches.\n\n"
    "Tile indexing (row-major, 0-based, top-left = 0):\n"
    "  3x3: 0 1 2 / 3 4 5 / 6 7 8\n"
    "  4x4: 0 1 2 3 / 4 5 6 7 / 8 9 10 11 / 12 13 14 15\n\n"
    "Respond with ONLY this JSON (no explanation):\n"
    '  {\"grid\":\"3x3\",\"tiles\":[0,2,4]}\n'
    "  or\n"
    '  {\"grid\":\"4x4\",\"tiles\":[5,6,9,10]}\n\n'
    'If no grid overlay is visible: {\"grid\":\"unknown\",\"tiles\":[]}'
)

_VISION_LOCATE_PROMPT = (
    "You are an image locator. The screenshot shows a webpage at {viewport_w}x{viewport_h} pixels.\n"
    "Find the element best matching this description: {description}\n\n"
    "Reply with ONLY this JSON (no explanation):\n"
    '  {{"found": true, "x": 540, "y": 320, "confidence": "high"}}\n'
    "  or\n"
    '  {{"found": false}}\n\n'
    "Coordinates are pixel positions of the element CENTER in the screenshot. "
    "If multiple match, pick the most prominent / topmost. Confidence is "
    "'high' (clear single match), 'medium' (multiple plausible), or 'low' "
    "(uncertain)."
)


def _parse_tile_indices(text: str) -> list[int]:
    """Legacy: extract JSON array of ints from response (kept for compat)."""
    try:
        match = _re.search(r"\[[\d\s,]*\]", text)
        if not match:
            return []
        out = json.loads(match.group(0))
        return [int(x) for x in out if isinstance(x, (int, float)) and 0 <= int(x) < 100]
    except Exception:
        return []


def _parse_vision_response(text: str) -> tuple[str, list[int]]:
    """Parse {'grid': '3x3'|'4x4', 'tiles': [...]} from LLM response text.

    Returns (grid, tiles). Falls back to legacy array-only parse assuming 3x3.
    """
    obj_match = _re.search(r'\{[^{}]*"grid"[^{}]*"tiles"[^{}]*\}', text)
    if not obj_match:
        obj_match = _re.search(r'\{[^{}]*"tiles"[^{}]*"grid"[^{}]*\}', text)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            grid = str(parsed.get("grid", "3x3")).lower().strip()
            if grid not in ("3x3", "4x4"):
                grid = "3x3"
            tiles_raw = parsed.get("tiles", [])
            max_idx = 9 if grid == "3x3" else 16
            tiles = [int(x) for x in tiles_raw
                     if isinstance(x, (int, float)) and 0 <= int(x) < max_idx]
            return grid, tiles
        except Exception:
            pass
    tiles = _parse_tile_indices(text)
    return "3x3", tiles


async def _claude_vision_pick_tiles(
    api_key: str, target: str, image_b64: str,
    grid: str = "3x3", model: str = "claude-opus-4-7",
) -> tuple[str, list[int]]:
    """Anthropic Claude vision tile picker. Returns (grid_detected, tiles)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                        {"type": "text", "text": _PROMPT_TEMPLATE},
                    ],
                }],
            },
        )
        data = resp.json()
        text = (data.get("content", [{}])[0]).get("text", "[]").strip()
        return _parse_vision_response(text)


async def _openai_compat_vision_pick_tiles(
    api_key: str, base_url: str, model: str,
    target: str, image_b64: str, grid: str = "3x3",
) -> tuple[str, list[int]]:
    """OpenAI-compatible vision tile picker. Returns (grid_detected, tiles).

    Works with: OpenAI (gpt-4o, gpt-5.x), Groq (llama3.2-vision),
    Ollama (llava local), Together.ai, custom LLM gateways — any /v1/chat/completions
    endpoint that supports image_url content.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1500,
                "temperature": 0,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT_TEMPLATE},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }],
            },
        )
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(c.get("text", "") for c in text if isinstance(c, dict))
        except (KeyError, IndexError, TypeError):
            return "3x3", []
        return _parse_vision_response(str(text))


def _resolve_vision_provider(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve provider config from explicit args → env vars → defaults.

    Resolution priority:
      1. Explicit args to solve_recaptcha_ai(provider=, base_url=, api_key=, model=)
      2. OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL         (standard — OpenAI SDK convention)
      3. AI_VISION_API_KEY / AI_VISION_BASE_URL / AI_VISION_MODEL (DEPRECATED — removed in v0.2.0)
      4. ANTHROPIC_API_KEY / ANTHROPIC_MODEL                      (Claude)

    ⚠️ Model MUST be multimodal (vision-capable):
      - OpenAI: gpt-4o, gpt-4o-mini, gpt-4-vision-preview, gpt-5.x
      - Anthropic: claude-opus-4-7, claude-sonnet-*
      - Local Ollama: llava, llava-llama3, bakllava, llama3.2-vision
      - Groq: llama-3.2-90b-vision-preview

    Returns (provider, base_url, api_key, model).
    Raises ValueError if no key is available anywhere.
    """
    import warnings

    legacy_key = os.environ.get("AI_VISION_API_KEY")
    legacy_url = os.environ.get("AI_VISION_BASE_URL")
    legacy_model = os.environ.get("AI_VISION_MODEL")
    legacy_prov = os.environ.get("AI_VISION_PROVIDER")
    if any([legacy_key, legacy_url, legacy_model, legacy_prov]) and not os.environ.get("OPENAI_API_KEY"):
        warnings.warn(
            "AI_VISION_* env vars are deprecated in v0.1.4 — migrate to "
            "OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL "
            "(OpenAI SDK standard). Legacy vars still work but will be "
            "removed in v0.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    prov = (provider or legacy_prov or "").lower().strip()

    if not prov:
        has_openai = os.environ.get("OPENAI_API_KEY") or legacy_key or os.environ.get("OPENAI_BASE_URL") or legacy_url
        has_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        if has_openai:
            prov = "openai"
        elif has_anthropic:
            prov = "anthropic"

    if prov in ("anthropic", "claude"):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set and no api_key passed")
        resolved_model = (
            model
            or os.environ.get("ANTHROPIC_MODEL")
            or legacy_model
            or "claude-opus-4-7"
        )
        return ("anthropic",
                base_url or "https://api.anthropic.com",
                key,
                resolved_model)

    if prov in ("openai", "openai-compat", "generic"):
        key = (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or legacy_key
            or ""
        )
        url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or legacy_url
            or "https://api.openai.com/v1"
        )
        mdl = (
            model
            or os.environ.get("OPENAI_MODEL")
            or legacy_model
            or "gpt-4o"
        )
        if not key:
            raise ValueError(
                "No API key found. Set OPENAI_API_KEY (standard) or "
                "ANTHROPIC_API_KEY, or pass api_key= to the tool."
            )
        return ("openai", url, key, mdl)

    raise ValueError(
        "No vision provider configured. Set one of:\n"
        "  • OPENAI_API_KEY (+ optional OPENAI_BASE_URL, OPENAI_MODEL) — OpenAI-compat\n"
        "  • ANTHROPIC_API_KEY (+ optional ANTHROPIC_MODEL) — Claude\n"
        "Model must support multimodal/vision input (gpt-4o, claude-opus-4-7, llava, etc.)"
    )


@mcp.tool()
async def solve_recaptcha_ai(
    api_key: Optional[str] = None,
    max_rounds: int = 3,
    wait_between: float = 2.5,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Solve reCAPTCHA v2 image challenge using a vision-enabled LLM.

    Supports Anthropic (Claude) OR any OpenAI-compatible API (gpt-4o, gpt-5.x,
    Groq llama3.2-vision, local Ollama llava, Together.ai, Fireworks, etc).

    ⚠️ MODEL MUST BE MULTIMODAL (vision-capable) — text-only models fail silently.
    ✅ Supported: gpt-4o, gpt-5.x, claude-opus-4-7, llava, llama-3.2-90b-vision-preview
    ❌ NOT: gpt-3.5-turbo, llama3 (non-vision), claude-3-haiku

    Env vars (OpenAI SDK standard — priority checked if args omitted):
        OPENAI_API_KEY + OPENAI_BASE_URL + OPENAI_MODEL  → OpenAI-compat
        ANTHROPIC_API_KEY + ANTHROPIC_MODEL              → Claude
        AI_VISION_* (legacy, DEPRECATED — removed v0.2.0) → backward-compat

    Explicit override:
        provider="anthropic" | "openai"
        base_url="https://your-provider.example.com/v1"
        api_key="..."
        model="gpt-4o" | "claude-opus-4-7" | ...

    Cost: varies by provider (~$0.005-0.03 per solve).
    """
    try:
        resolved_provider, resolved_base_url, resolved_key, resolved_model = \
            _resolve_vision_provider(provider, base_url, api_key, model)
    except ValueError as e:
        return err(str(e))
    def _unwrap(v):
        """nodriver sometimes returns RemoteObject; extract .value if needed."""
        if hasattr(v, "value") and not isinstance(v, (str, int, float, bool, list, dict)):
            return v.value
        return v

    try:
        tab = BrowserState.active_tab()
        # Pre-flight: detect quota-exhausted reCAPTCHA pages so we fail fast
        # instead of burning 3 model calls on empty grids.
        quota_msg = _unwrap(await tab.evaluate(
            "(() => { const t = (document.body && document.body.innerText || '').toLowerCase(); "
            "return t.includes('exceeding') && t.includes('quota') ? 'quota_exhausted' : ''; })()",
            return_by_value=True,
        ))
        if quota_msg == "quota_exhausted":
            return err(
                "reCAPTCHA quota exhausted on this page (Google Enterprise free tier). "
                "Test on a real protected site, not a rate-limited demo."
            )

        for round_num in range(1, max_rounds + 1):
            # Step 1: locate challenge iframe (bframe). If hidden, auto-click
            # the anchor checkbox first so callers don't need a separate
            # mouse_click_xy step before invoking solve_recaptcha_ai.
            async def _find_bframe():
                return _unwrap(await tab.evaluate(
                    """
                    (() => {
                      const f = Array.from(document.querySelectorAll('iframe'))
                        .find(x => x.src.includes('recaptcha/api2/bframe') ||
                                   x.src.includes('recaptcha/enterprise/bframe'));
                      if (!f) return 'no_challenge';
                      const r = f.getBoundingClientRect();
                      // Hidden = too small OR positioned off-screen. reCAPTCHA
                      // parks the bframe at top:-9999 / left:-9999 before the
                      // user clicks the anchor checkbox.
                      if (r.width < 50 || r.height < 50) return 'challenge_hidden';
                      if (r.top < -1000 || r.left < -1000) return 'challenge_hidden';
                      if (r.bottom < 0 || r.right < 0) return 'challenge_hidden';
                      return JSON.stringify({
                        left: Math.round(r.left), top: Math.round(r.top),
                        width: Math.round(r.width), height: Math.round(r.height),
                      });
                    })()
                    """,
                    return_by_value=True,
                ))

            frame_info = str(await _find_bframe() or "")

            if frame_info == "challenge_hidden":
                # Auto-click the "I'm not a robot" anchor checkbox to open the
                # image challenge. The checkbox is a fixed offset inside the
                # anchor iframe (left+30, top+40 — calibrated for v2 default).
                anchor_raw = _unwrap(await tab.evaluate(
                    """
                    (() => {
                      const f = Array.from(document.querySelectorAll('iframe'))
                        .find(x => x.src.includes('recaptcha/api2/anchor') ||
                                   x.src.includes('recaptcha/enterprise/anchor'));
                      if (!f) return 'no_anchor';
                      const r = f.getBoundingClientRect();
                      return JSON.stringify({
                        left: Math.round(r.left), top: Math.round(r.top),
                        width: Math.round(r.width), height: Math.round(r.height),
                      });
                    })()
                    """,
                    return_by_value=True,
                ))
                anchor_info = parse_json(str(anchor_raw or ""), None)
                if not isinstance(anchor_info, dict):
                    return err("challenge hidden and no anchor iframe found")
                ax = int(anchor_info["left"] + 30)
                ay = int(anchor_info["top"] + anchor_info["height"] // 2)
                # Direct CDP click (no humanize) — humanize_move + mouse_click
                # races with the anchor iframe's load state in some sessions,
                # ending up registered as no-click. Raw mouse_click is reliable.
                await tab.mouse_click(ax, ay)
                await asyncio.sleep(wait_between)
                # Re-probe — challenge should now be visible
                frame_info = str(await _find_bframe() or "")

            if frame_info in ("no_challenge", "challenge_hidden", ""):
                token_raw = _unwrap(await tab.evaluate(
                    '(() => { var t = document.querySelector("textarea[name=g-recaptcha-response]"); return t && t.value ? t.value.length : 0; })()',
                    return_by_value=True,
                ))
                if isinstance(token_raw, (int, float)) and token_raw > 0:
                    return ok(f"solved on round {round_num} (token length={int(token_raw)}, no challenge needed)")
                return err(f"no reCAPTCHA challenge iframe visible (state: {frame_info!r})")

            finfo = parse_json(frame_info, {})
            if not finfo or finfo.get("width", 0) < 50:
                return err(f"bframe too small to screenshot: {finfo}")

            # Step 2: full-page screenshot. (Tried CDP clip-cropping in 0.2.10-dev
            # but the bframe's reported rect is unreliable right after click —
            # ends up clipping a 300×150 white box instead of the 480×580
            # challenge. Full-page screenshot is reliable; the model handles
            # the surrounding page content fine.)
            ensure_dirs()
            shot_path = SCREENSHOT_DIR / ts_filename(f"recaptcha-r{round_num}", "png")
            await tab.save_screenshot(filename=str(shot_path), format="png")
            try:
                img_bytes = open(str(shot_path), "rb").read()
                if not img_bytes:
                    return err(f"screenshot file empty: {shot_path}")
                img_b64 = _b64.b64encode(img_bytes).decode()
            except Exception as e:
                return err(f"screenshot read failed: {e}")

            # Step 3: target — skip cross-origin DOM read (unreliable across origins).
            # Prompt tells vision model to READ target from the challenge header itself.
            target = "the category shown in the blue header banner of the reCAPTCHA modal"

            # Step 4: ask vision model (returns grid + tile indices).
            # If empty, try refreshing the challenge up to max_refresh times —
            # model may refuse / under-identify ambiguous ones but next challenge works.
            grid_detected, tiles = "3x3", []
            max_refresh = 3
            for refresh_attempt in range(max_refresh + 1):
                if resolved_provider == "anthropic":
                    grid_detected, tiles = await _claude_vision_pick_tiles(
                        resolved_key, target, img_b64, model=resolved_model,
                    )
                else:
                    grid_detected, tiles = await _openai_compat_vision_pick_tiles(
                        resolved_key, resolved_base_url, resolved_model,
                        target, img_b64,
                    )
                if tiles:
                    break  # got valid picks, proceed
                if refresh_attempt < max_refresh:
                    reload_x = int(finfo["left"] + 25)
                    reload_y = int(finfo["top"] + finfo["height"] - 30)
                    await humanized_move(tab, reload_x + 60, reload_y - 40, reload_x, reload_y)
                    await asyncio.sleep(0.2)
                    await tab.mouse_click(reload_x, reload_y)
                    await asyncio.sleep(2.5)
                    shot_path = SCREENSHOT_DIR / ts_filename(
                        f"recaptcha-r{round_num}-refresh{refresh_attempt+1}", "png"
                    )
                    await tab.save_screenshot(filename=str(shot_path), format="png")
                    try:
                        img_bytes = open(str(shot_path), "rb").read()
                        img_b64 = _b64.b64encode(img_bytes).decode()
                    except Exception:
                        pass  # keep previous img_b64

            if not tiles:
                return err(
                    f"round {round_num}: {resolved_provider} ({resolved_model}) "
                    f"returned no tiles after {max_refresh} refresh attempts "
                    f"(grid={grid_detected!r})"
                )

            # Step 5: dynamic grid math (supports 3x3 images OR 4x4 squares)
            n = 4 if grid_detected == "4x4" else 3
            max_valid = n * n
            grid_top = finfo["top"] + 120
            grid_bottom = finfo["top"] + finfo["height"] - 70
            grid_left = finfo["left"] + 10
            grid_right = finfo["left"] + finfo["width"] - 10
            tile_w = (grid_right - grid_left) / n
            tile_h = (grid_bottom - grid_top) / n

            # Filter invalid indices (model may overshoot when it thinks grid
            # is 4x4 but actual is 3x3 — index 9 in a 3x3 doesn't exist).
            valid_tiles = [
                idx for idx in tiles
                if isinstance(idx, int) and 0 <= idx < max_valid
            ]
            if not valid_tiles:
                pass  # fall through to verify click; let response steer next round
            clicked = []
            for idx in valid_tiles:
                row, col = idx // n, idx % n
                cx = int(grid_left + tile_w * col + tile_w / 2)
                cy = int(grid_top + tile_h * row + tile_h / 2)
                # Direct mouse_click without humanize_move — humanize was
                # racing with reCAPTCHA's per-tile fade-in animation, causing
                # some clicks to land in boundary regions which Google
                # treats as "click outside grid = clear selection".
                await tab.mouse_click(cx, cy)
                clicked.append(idx)
                # 700ms pause: long enough for reCAPTCHA's tile-selected
                # animation to finalize before the next click — too fast and
                # adjacent clicks can deselect previous picks.
                await asyncio.sleep(0.7)

            # Step 6: click Verify (bottom-right of iframe)
            verify_x = int(finfo["left"] + finfo["width"] - 50)
            verify_y = int(finfo["top"] + finfo["height"] - 30)
            await humanized_move(tab, verify_x - 100, verify_y - 50, verify_x, verify_y)
            await asyncio.sleep(0.2)
            await tab.mouse_click(verify_x, verify_y)
            await asyncio.sleep(wait_between)

            # Check if solved — properly unwrap RemoteObject
            token_len_raw = _unwrap(await tab.evaluate(
                '(() => { var t = document.querySelector("textarea[name=g-recaptcha-response]"); return t && t.value ? t.value.length : 0; })()',
                return_by_value=True,
            ))
            try:
                token_len = int(token_len_raw) if token_len_raw is not None else 0
            except (TypeError, ValueError):
                token_len = 0

            if token_len > 0:
                return ok(
                    f"solved on round {round_num}: picked tiles {clicked}, token={token_len}ch"
                )
            # Not solved — loop retries with fresh challenge

        return err(f"not solved after {max_rounds} rounds (last picked: {clicked})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def vision_locate(
    description: str,
    click: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """⭐ Find an element by natural-language description using a vision LLM.

    Uses the same provider as solve_recaptcha_ai (OPENAI_* / ANTHROPIC_* env).
    Reuses solve_recaptcha_ai's vision plumbing so any vision-capable model
    works (gpt-4o, gpt-5.x, claude, llava, llama-3.2-vision).

    Args:
        description: NL description, e.g. "the red Create button at bottom right"
        click: if True, also dispatches a CDP mouse_click at the located point
        api_key/base_url/model/provider: explicit overrides (else from env)

    Returns JSON: {"found":true/false, "x":int, "y":int, "confidence":"high|medium|low"}.
    Use when CSS selectors are unreliable (visual-only differentiator, dynamic IDs).
    """
    # Late import of _wait — defined in server.py and only available after
    # server.py finishes its module body. Vision module is imported at the
    # end of server.py, so by call time _wait is bound on server.
    from ..server import _wait  # noqa: E402
    try:
        tab = BrowserState.active_tab()
        try:
            resolved_provider, resolved_base_url, resolved_key, resolved_model = \
                _resolve_vision_provider(provider, base_url, api_key, model)
        except ValueError as e:
            return err(str(e))
        # Get viewport for prompt context
        vp_raw = await _wait(tab.evaluate(
            "JSON.stringify({w: innerWidth, h: innerHeight})",
            return_by_value=True,
        ), what="vision_locate viewport")
        vp = parse_json(str(vp_raw.value if hasattr(vp_raw, "value") else vp_raw), {"w": 1280, "h": 800})
        # Take screenshot
        ensure_dirs()
        shot_path = SCREENSHOT_DIR / ts_filename("vision-locate", "png")
        await _wait(tab.save_screenshot(filename=str(shot_path), format="png"),
                    what="vision_locate screenshot")
        img_b64 = _b64.b64encode(Path(str(shot_path)).read_bytes()).decode()
        prompt = _VISION_LOCATE_PROMPT.format(
            viewport_w=vp.get("w", 1280),
            viewport_h=vp.get("h", 800),
            description=description,
        )
        # Send to vision API
        if resolved_provider == "anthropic":
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": resolved_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": resolved_model,
                        "max_tokens": 600,
                        "messages": [{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64",
                                "media_type": "image/png", "data": img_b64}},
                            {"type": "text", "text": prompt},
                        ]}],
                    },
                )
                data = resp.json()
                response_text = (data.get("content", [{}])[0]).get("text", "").strip()
        else:
            url = resolved_base_url.rstrip("/") + "/chat/completions"
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {resolved_key}",
                             "Content-Type": "application/json"},
                    json={
                        "model": resolved_model,
                        "max_tokens": 1500,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{img_b64}"}},
                        ]}],
                    },
                )
                data = resp.json()
                try:
                    response_text = data["choices"][0]["message"]["content"]
                    if isinstance(response_text, list):
                        response_text = "".join(c.get("text", "") for c in response_text
                                                 if isinstance(c, dict))
                except (KeyError, IndexError, TypeError):
                    response_text = ""
        # Parse response — extract JSON from possibly-wrapped text
        m = _re.search(r'\{[^{}]*"found"[^{}]*\}', str(response_text))
        if not m:
            return err(f"vision model returned no JSON: {str(response_text)[:200]}")
        result = parse_json(m.group(0), {})
        if not isinstance(result, dict) or not result.get("found"):
            return err(f"element not found: {description!r}")
        try:
            x = int(result["x"])
            y = int(result["y"])
        except (KeyError, TypeError, ValueError):
            return err(f"vision response missing coords: {result}")
        # Optionally click
        if click:
            try:
                await tab.mouse_click(x, y)
                BrowserState.last_mouse_xy = {"x": x, "y": y}
            except Exception as e:
                return err(f"located ({x},{y}) but click failed: {e}")
        result["clicked"] = bool(click)
        return ok(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        return err(str(e))
