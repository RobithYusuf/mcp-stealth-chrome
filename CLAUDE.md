# CLAUDE.md — Project Context

## What This Project Does

Python MCP server for stealth Chrome automation via nodriver. **160 tools**. Published on PyPI as `mcp-stealth-chrome`. Sister package of mcp-camoufox (Node/Firefox).

## Architecture

```
uvx mcp-stealth-chrome → Python 3.11 → FastMCP → nodriver → Chrome/Chromium
                                                  ↓
                                          curl_cffi (TLS-perfect HTTP)
                                                  ↓
                                          opencv-python (template matching)
```

Package layout:

```
src/mcp_stealth_chrome/
├── __init__.py       # __version__ (currently 0.10.0) + module docstring
├── server.py         # FastMCP entry + tool implementations/registration
├── state.py          # Multi-instance BrowserState + find_chrome_binary()
├── snapshot.py       # SNAPSHOT_JS IIFE + format_snapshot()
├── helpers.py        # ok/err string helpers, resolve_ref, get_url, parse_json
├── humanize.py       # Bezier mouse path, Gaussian typing delays
├── captcha.py        # CapSolver HTTP client (via httpx)
└── patches.py        # Runtime monkey-patches for nodriver bugs
```

Global state (multi-instance):
- `BrowserState.current_instance_id` — pointer to active instance ("main" default)
- `BrowserState.instances: dict[str, InstanceSnapshot]` — other running instances
- Class-level `browser`, `tabs`, `active_tab_index` etc reflect current instance
- Switch between instances via `BrowserState.switch_to(id)` — swaps state
- Idle reaper task auto-closes instances after `BROWSER_IDLE_TIMEOUT` (default 600s)
- Instance snapshots also preserve `desc`, headed/headless state, and privacy cleanup metadata

Persistent paths:
- Main profile: `~/.mcp-stealth/profile/`
- Named profiles: `~/.mcp-stealth/profiles/<instance_id>/`
- Screenshots: `~/.mcp-stealth/screenshots/`
- Exports (PDF, HAR): `~/.mcp-stealth/exports/`
- Storage states: `~/.mcp-stealth/storage-states/`

## Key Design Decisions

- **FastMCP auto-wraps str return as TextContent** — ALL tools annotated `-> str`, return strings via `ok()` / `err()` helpers. Never return dicts (pydantic error).
- **SNAPSHOT_JS IIFE string identical to mcp-camoufox** — `data-mcp-ref` refs, same tag list + formatter. Cross-package muscle memory preserved.
- **RemoteObject unwrap** — nodriver `tab.evaluate()` sometimes returns RemoteObject wrapper instead of plain value. Unwrap via `v.value if hasattr(v, 'value') ... else v` before casting.
- **JS click fallback** — `click()` tries `el.click()`, falls back to `tab.evaluate("...click()")` for overlay-blocked elements.
- **get_url tool uses `name="get_url"` decorator** — function named `get_current_url` to avoid conflict with helper import.
- **Single-file server.py** — organized by section header `══════`. Matches mcp-camoufox `src/index.ts` pattern.
- **type_text uses `data-mcp-focused` marker attribute** — nodriver can't return active element handle directly; we set/remove marker via evaluate, then query_selector.
- **CapSolver via httpx directly** — no hard dep on capsolver-python SDK (pinned at old version). Raw HTTP createTask + poll.
- **cookie patch monkey-patches nodriver 0.48.1** — Chrome 147+ omits `sameParty` cookie field causing KeyError that kills CDP listener. `patches.apply_all()` runs at server import.
- **Chrome auto-detect cross-platform** — `find_chrome_binary()` in state.py checks macOS/Linux/Windows paths before nodriver.start(). Friendly error if missing.
- **Multi-instance via swap-current design** — class-level attrs always reflect current; others stored in `instances` dict as snapshots; swap on `switch_to()`.

## Differentiator Tools (competitive moat — DO NOT remove without discussion)

### Dual-Mode HTTP (unique, no other stealth MCP has this)
- `http_request` — curl_cffi TLS-perfect (chrome/firefox/safari JA3)
- `http_session_cookies` — inspect browser cookies for URL
- `session_warmup` — homepage→category→target trust building
- `detect_anti_bot` — auto-identify CF/DataDome/PX/Kasada/Imperva

### Precision Mouse Kit (unique)
- `click_turnstile` — proven Cloudflare Turnstile bypass
- `click_element_offset` / `click_at_corner` — non-center clicks
- `find_by_image` / `click_at_image` — OpenCV template matching
- `mouse_drift` — idle wandering (pass behavioral ML)
- `mouse_record` / `mouse_replay` — capture+replay real human patterns

### AI Vision Solver (unique — 5/5 PROVEN on reCAPTCHA v2)
- `solve_recaptcha_ai` — Claude OR any OpenAI-compatible vision API
- Neutral prompt language (avoids "CAPTCHA" keyword that triggers LLM safety filter)
- Auto-refresh (click ↻) when model returns empty for ambiguous challenges
- Grid auto-detect (3x3 images vs 4x4 segments)
- Dynamic tile click math based on detected grid

### Stealth Toolkit
- `storage_state_save` / `storage_state_load` — portable session export
- `solve_captcha` — CapSolver adapter for Turnstile/reCAPTCHA/hCaptcha
- `verify_cf` — nodriver's OpenCV template-match CF checkbox
- `fingerprint_rotate` — UA/lang/platform/timezone via CDP
- `identity_pin` / `identity_show` — persisted fingerprint + proxy affinity per instance
- `privacy=True` at launch — randomized fingerprint + disposable zero-residue profile
- `humanize_click` / `humanize_type` — wrapper for Bezier+Gaussian single-actions

### Multi-Instance
- `spawn_browser` — create named parallel instance
- `instance_describe` — semantic purpose memory survives switching
- `list_instances` / `switch_instance` / `close_instance` / `close_all_instances`
- Idle reaper auto-close (`BROWSER_IDLE_TIMEOUT` env, default 600s)

## Build & Publish

```bash
cd /Users/macbook/Projects/mcp-stealth-chrome
uv sync                                # install deps
uv run mcp-stealth-chrome              # test stdio server locally
uv build                               # produce dist/*.whl + *.tar.gz
uv publish --token pypi-...            # publish to PyPI
```

Version bump: update both `pyproject.toml` AND `src/mcp_stealth_chrome/__init__.py` in one commit.

Local install for `uvx` testing without PyPI:
```bash
uvx --from /Users/macbook/Projects/mcp-stealth-chrome mcp-stealth-chrome
```

## Common Pitfalls

- Returning `dict` from a tool → FastMCP pydantic error. Always return `str`.
- `tab.evaluate(expr)` without `return_by_value=True` → returns RemoteObject wrapper.
- `tab.evaluate(...)` with `return_by_value=True` STILL sometimes returns RemoteObject — unwrap via `.value`.
- `nodriver.start()` is async, returns Browser; `browser.get(url)` returns Tab.
- nodriver Element methods: `send_keys` (per-char) vs `set_text`/`set_value` (direct DOM). Pick carefully.
- `browser.cookies` is a manager — `await browser.cookies.get_all()`, not raw list.
- `tab.find(text)` is fuzzy by default (`best_match=True`) — pass `best_match=False` for exact.
- Active element detection requires `data-mcp-focused` marker workaround.
- nodriver 0.48.1 `sameParty` cookie bug — patched via `patches.apply_all()` at import.
- Chrome 147+ response may have `content-encoding: zstd` — curl_cffi handles automatically.
- reCAPTCHA modal bframe iframe: position can be floating/fixed — crop by iframe coords fails. Use full-page screenshot.
- Vision model safety filter triggers on "CAPTCHA"/"reCAPTCHA" — use neutral "image grid analysis" phrasing.

## Tool-Parity with mcp-camoufox

Target: same tool name, same params, same behavior, same ref format.
- Same: browser_launch, browser_snapshot, click, fill, type_text, scrape_page, extract_structured, etc.
- Exclusive to stealth-chrome: click_turnstile, http_request, solve_recaptcha_ai, session_warmup, detect_anti_bot, mouse_drift/record/replay, fingerprint_rotate, storage_state_save/load, spawn_browser multi-instance
- Removed (Camoufox-only): humanize=True at launch (use per-action), geoip (use fingerprint_rotate)

## README Sync Rule

**Every code change that adds/removes/modifies tools MUST update docs.** Check:
- `README.md` — tool count (currently **160**), comparison table, categories, BYOK section
- `INSTALL.md` — env vars, troubleshooting
- `docs/DEMO.md` — feature demos
- `pyproject.toml` + `__init__.py` — bump version

## Security Rules

- NEVER commit API keys (CAPSOLVER_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, AI_VISION_API_KEY)
- Example docs use placeholders like `sk-ant-xxxxx` or `your-key-here`
- Generic URLs in examples: `https://your-provider.example.com/v1` (never specific third-party)
- `.gitignore` covers `.env`, `.venv/`, `dist/`, `*.log`, `/tmp/`
- Session tokens (PyPI, etc) go to `/tmp/*.txt` with mode 600, deleted after use
