<div align="center">

# MCP Stealth Chrome

**94 tools** for AI agents that bypass Cloudflare, Turnstile, reCAPTCHA, and modern anti-bot systems.

[![PyPI version](https://img.shields.io/pypi/v/mcp-stealth-chrome.svg)](https://pypi.org/project/mcp-stealth-chrome/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

Browser stealth when you need eyes. TLS-perfect HTTP when you need speed.

</div>

---

Built on [nodriver](https://github.com/ultrafunkamsterdam/nodriver) (direct CDP, no WebDriver leak) + [curl_cffi](https://github.com/lexiforest/curl_cffi) (TLS fingerprint spoofing) + [FastMCP](https://github.com/modelcontextprotocol/python-sdk).

One-line install with `uvx`:

```bash
claude mcp add stealth-chrome -- uvx mcp-stealth-chrome@latest
```

## Proven on Real Sites

| Site | Challenge | Result |
|------|-----------|--------|
| `bot.sannysoft.com` | All fingerprint tests | ✅ 100% pass |
| `dash.cloudflare.com/login` | Turnstile visible | ✅ Passed via `click_turnstile()` |
| `tls.browserleaks.com` | TLS JA3/JA4 fingerprint | ✅ Real Chrome/Firefox/Safari |
| `httpbin.org` | Multi-instance isolation | ✅ Two browsers parallel |

## Key Differentiators

Compared to the leading Python stealth MCP ([vibheksoni/stealth-browser-mcp](https://github.com/vibheksoni/stealth-browser-mcp), 476⭐):

| Feature | mcp-stealth-chrome | vibheksoni |
|---------|:-------------------:|:-----------:|
| Tools | **94** | 90 |
| `click_turnstile` one-liner | ✅ **Proven bypass** | ❌ |
| Dual-mode HTTP (curl_cffi TLS) | ✅ **Unique** | ❌ |
| AI Vision reCAPTCHA solver (Claude) | ✅ **Unique** | ❌ |
| Precision Mouse Kit (11 tools) | ✅ **Unique** | ❌ |
| Multi-instance + idle reaper | ✅ | ✅ |
| Install | `uvx` zero-setup | `git clone + pip` |
| Sister Firefox package | ✅ [mcp-camoufox](https://github.com/RobithYusuf/mcp-camoufox) | ❌ |
| Network interception hooks | ⚠️ basic | ✅ **AI-generated Python hooks** |
| Pixel-perfect element cloning | ⚠️ basic | ✅ **300+ CSS + events** |

**Different niches**: we focus on anti-bot bypass, they focus on UI reverse-engineering. Both MCPs work great together.

## Quick Install

See [INSTALL.md](INSTALL.md) for detailed per-client setup. TL;DR:

<details>
<summary><b>Claude Code CLI</b></summary>

```bash
# Global (available in all projects):
claude mcp add stealth-chrome --scope user -- uvx mcp-stealth-chrome@latest

# Project only:
claude mcp add stealth-chrome -- uvx mcp-stealth-chrome@latest
```
</details>

<details>
<summary><b>Claude Desktop</b></summary>

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "stealth-chrome": {
      "command": "uvx",
      "args": ["mcp-stealth-chrome@latest"]
    }
  }
}
```
</details>

<details>
<summary><b>Cursor</b></summary>

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "stealth-chrome": {
      "command": "uvx",
      "args": ["mcp-stealth-chrome@latest"]
    }
  }
}
```
</details>

### Optional: API Keys for CAPTCHA Solvers

`solve_recaptcha_ai` works with **Claude OR any OpenAI-compatible vision API** (gpt-4o, gpt-5.x, Groq, Ollama local, patungin.id, Together.ai, etc). Pick one:

**Anthropic Claude:**
```json
"env": {
  "ANTHROPIC_API_KEY": "sk-ant-xxxxx",
  "CAPSOLVER_KEY": "CAP-xxxxx"
}
```

**OpenAI-compatible (any provider with `/v1/chat/completions`):**
```json
"env": {
  "AI_VISION_BASE_URL": "https://ai.patungin.id/v1",
  "AI_VISION_API_KEY":  "your-key-here",
  "AI_VISION_MODEL":    "gpt-5.4"
}
```

Or vanilla OpenAI (inherits default base URL):
```json
"env": {
  "OPENAI_API_KEY": "sk-xxxxx",
  "AI_VISION_MODEL": "gpt-4o"
}
```

Provider priority: explicit args to tool > `AI_VISION_*` env > `OPENAI_API_KEY` > `ANTHROPIC_API_KEY`.

## Requirements

- Python 3.11+
- `uv` installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Chrome or Chromium browser (auto-detected by nodriver)

## Tool Categories (94)

### ⭐⭐⭐ Dual-Mode HTTP (unique)
| Tool | Purpose |
|------|---------|
| `http_request` | TLS-perfect HTTP via curl_cffi (chrome/firefox/safari impersonation) |
| `http_session_cookies` | Inspect which browser cookies match a URL |
| `session_warmup` | Natural browsing pattern (homepage/referer/scroll) before target |
| `detect_anti_bot` | Identify CF/DataDome/PerimeterX/Kasada/Imperva on current page |

### ⭐⭐ Precision Mouse Kit (unique)
| Tool | Purpose |
|------|---------|
| `click_turnstile` | One-liner CF Turnstile bypass (proven) |
| `click_element_offset` | Click at % position inside element (not center) |
| `click_at_corner` | Click top-left/right/bottom-left/right of element |
| `find_by_image` | OpenCV template match → coordinates |
| `click_at_image` | Find image + click its center |
| `mouse_drift` | Random Bezier wandering (pass behavioral ML) |
| `mouse_record` / `mouse_replay` | Capture real human mouse patterns, replay |

### ⭐⭐ AI Vision Solver (unique)
| Tool | Purpose |
|------|---------|
| `solve_recaptcha_ai` | Claude vision picks matching tiles — solve image challenges |

### ⭐ Stealth Toolkit
| Tool | Purpose |
|------|---------|
| `storage_state_save` / `storage_state_load` | Portable session export — bypass Turnstile via reuse |
| `solve_captcha` | CapSolver API — Turnstile/reCAPTCHA/hCaptcha |
| `verify_cf` | Cloudflare checkbox via OpenCV template match |
| `fingerprint_rotate` | UA/lang/platform/timezone via CDP |
| `humanize_click` / `humanize_type` | Bezier+Gaussian for single actions |

### Multi-Instance
| Tool | Purpose |
|------|---------|
| `spawn_browser` | New named instance (parallel profiles) |
| `list_instances` / `switch_instance` | Manage multiple browsers |
| `close_instance` / `close_all_instances` | Clean shutdown |

### Standard Browser Automation (lifecycle/navigation/DOM/interaction/scraping)
| Count | Examples |
|-------|----------|
| Lifecycle: 2 | browser_launch, browser_close |
| Navigation: 4 | navigate, go_back, go_forward, reload |
| DOM/Content: 6 | browser_snapshot, screenshot, get_text, get_html, get_url, save_pdf |
| Interaction: 9 | click, click_text, click_role, hover, fill, select_option, check, uncheck, upload_file |
| Keyboard: 2 | type_text, press_key |
| Mouse: 3 | mouse_click_xy, mouse_move, drag_and_drop |
| Wait: 4 | wait_for, wait_for_navigation, wait_for_url, wait_for_response |
| Tabs: 4 | tab_list, tab_new, tab_select, tab_close |
| Cookies/Storage: 8 | cookie_list/set/delete, localstorage_get/set/clear, sessionstorage_get/set |
| JavaScript: 2 | evaluate, inject_init_script |
| Inspection: 4 | inspect_element, get_attribute, query_selector_all, get_links |
| Frames: 2 | list_frames, frame_evaluate |
| Batch: 3 | batch_actions, fill_form, navigate_and_snapshot |
| Viewport/Scroll/Dialog/A11y: 5 | get/set_viewport_size, scroll, dialog_handle, accessibility_snapshot |
| Console/Network: 4 | console_start/get, network_start/get |
| Debug: 3 | server_status, get_page_errors, export_har |
| Scraping: 4 | detect_content_pattern, extract_structured, extract_table, scrape_page |

## Example Workflows

### One-liner Cloudflare Turnstile bypass

```
browser_launch(url="https://site-with-turnstile.com")
mouse_drift(duration_seconds=2)                    # natural behavior
click_turnstile()                                  # ✅ proven bypass
# Login button now enabled, fill form, submit
```

### Bypass Turnstile via saved session (most reliable)

```
# Once — manual:
browser_launch(url="https://target.com/login", headless=false)
# [user logs in manually in browser window]
storage_state_save(filename="target-session.json")
browser_close()

# Every time after — automated:
browser_launch(
  url="https://target.com/dashboard",
  headless=true,                                   # can go headless
  storage_state_path="~/.mcp-stealth/storage-states/target-session.json"
)
# Turnstile never triggers — session is valid
```

### Solve reCAPTCHA v2 image challenge via Claude

```
browser_launch(url="https://site-with-recaptcha.com")
click_element_offset(ref="recaptcha-checkbox-ref", x_percent=8)
# Image challenge appears
solve_recaptcha_ai(max_rounds=3)                   # uses ANTHROPIC_API_KEY
# Token injected, form ready to submit
```

### Multi-account scraping in parallel

```
browser_launch(url="https://site.com", headless=true)   # main instance
spawn_browser("account_2", url="https://site.com", headless=true)
spawn_browser("account_3", url="https://site.com", headless=true)

list_instances()                                   # see all 3 running
switch_instance("account_2")
# All subsequent tool calls target account_2
click(ref="login-btn")
...
switch_instance("main")                            # back to main
```

### Browser login + fast API scraping

```
# Login with browser (renders JS, solves challenges)
browser_launch(url="https://api-site.com/login")
click_turnstile()
fill(ref="email-ref", value="you@example.com")
fill(ref="password-ref", value="...")
click(ref="submit-ref")

# Scrape API 10x faster with TLS-perfect HTTP
http_request(
  url="https://api-site.com/v1/data",
  impersonate="chrome",
  use_browser_cookies=true                         # reuse login session
)
```

### Auto-detect anti-bot + recommended strategy

```
browser_launch(url="https://unknown-site.com")
detect_anti_bot()
# Returns: {"detected": ["Cloudflare", "reCAPTCHA"],
#           "recommended_tools": [...]}
```

## Architecture

```
uvx mcp-stealth-chrome → Python 3.11 → FastMCP → nodriver → Chrome/Chromium
                                                  ↓
                                          curl_cffi (TLS)
```

Data locations:
- Profile (main): `~/.mcp-stealth/profile/`
- Profiles (multi-instance): `~/.mcp-stealth/profiles/<instance_id>/`
- Screenshots: `~/.mcp-stealth/screenshots/`
- Exports (PDF, HAR): `~/.mcp-stealth/exports/`
- Storage states: `~/.mcp-stealth/storage-states/`

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BROWSER_IDLE_TIMEOUT` | `600` | Auto-close browsers after idle seconds (0 = never) |
| `BROWSER_IDLE_REAPER_INTERVAL` | `60` | How often reaper checks idle state |
| `CAPSOLVER_KEY` | — | Enable `solve_captcha` tool |
| `ANTHROPIC_API_KEY` | — | `solve_recaptcha_ai` via Claude |
| `AI_VISION_BASE_URL` | — | `solve_recaptcha_ai` via OpenAI-compat API |
| `AI_VISION_API_KEY` | — | API key for OpenAI-compat provider |
| `AI_VISION_MODEL` | `claude-opus-4-7` or `gpt-4o` | Vision model name |
| `AI_VISION_PROVIDER` | auto-detect | Force `anthropic` or `openai` |
| `OPENAI_API_KEY` | — | Shortcut for OpenAI (default base URL) |

## Stealth Details

Underlying tech stack:
- **nodriver** — Python CDP client with no WebDriver/Runtime.Enable leaks
- **curl_cffi** — libcurl with CFFI, matches Chrome/Firefox/Safari TLS handshake exactly (JA3/JA4 authenticity)
- **OpenCV** — template matching for visual CAPTCHA checkbox detection

Bypass layer vs detection:

| Detection | Bypass |
|-----------|--------|
| `navigator.webdriver` | nodriver doesn't set it |
| `Runtime.Enable` CDP leak | nodriver avoids it |
| Automation flags | No `--enable-automation` |
| Headless fingerprint | `headless=false` recommended for hard targets |
| TLS/JA3/JA4 | `http_request(impersonate='chrome')` |
| Turnstile checkbox | `click_turnstile()` |
| reCAPTCHA v2 image | `solve_recaptcha_ai()` or `solve_captcha()` |
| Behavioral ML | `mouse_drift`, `mouse_record/replay`, `humanize_click/type` |

**Honest limits** — these are HARDEST OSS bypass targets and require commercial services for production:
- DataDome (real-time behavioral ML across 50+ signals)
- Kasada (proprietary JS, rotates daily)
- PerimeterX/HUMAN (ML-based request scoring)
- ChatGPT managed Turnstile (checks React internal state)

For these, `storage_state_save/load` (manual-login-once, reuse) is the most reliable OSS approach.

## Sister Package

[mcp-camoufox](https://github.com/RobithYusuf/mcp-camoufox) — Firefox stealth with same API. Use when you need:
- Hardest anti-bot bypass (Camoufox C++ level patches = stealth score 6% CreepJS)
- Firefox-specific rendering
- Node.js ecosystem

Both packages share tool names, snapshot format, ref system — switch seamlessly.

## Development

```bash
git clone https://github.com/RobithYusuf/mcp-stealth-chrome
cd mcp-stealth-chrome
uv sync
uv run mcp-stealth-chrome       # run stdio server locally
```

Testing:
```bash
uv run python /tmp/smoke-test.py      # full smoke test (see /tmp/ examples)
```

## Credits

- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) by ultrafunkamsterdam — undetected Chrome via CDP
- [curl_cffi](https://github.com/lexiforest/curl_cffi) by lexiforest — TLS browser impersonation
- [FastMCP](https://github.com/modelcontextprotocol/python-sdk) — Python MCP SDK
- [Camoufox](https://github.com/daijro/camoufox) by daijro — sister Firefox stealth (via [mcp-camoufox](https://github.com/RobithYusuf/mcp-camoufox))
- [CapSolver](https://capsolver.com) — CAPTCHA solving API
- [vibheksoni/stealth-browser-mcp](https://github.com/vibheksoni/stealth-browser-mcp) — complementary MCP for UI cloning & network hooks

## License

MIT — see [LICENSE](LICENSE).
