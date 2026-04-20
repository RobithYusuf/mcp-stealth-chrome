# Installation Guide

Step-by-step setup for `mcp-stealth-chrome` on Claude Code, Claude Desktop, and Cursor.

## Prerequisites

### 1. Install `uv` (Python package manager)

```bash
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell):
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify:
```bash
uv --version
# uv 0.11.3 (or newer)
```

### 2. Install Chrome or Chromium

nodriver auto-detects Chrome at standard locations. If you don't have it:

- **macOS**: [google.com/chrome](https://www.google.com/chrome/)
- **Windows**: Install from chrome.com
- **Linux**: `sudo apt install chromium-browser` or `google-chrome`

Verify auto-detection by running mcp-stealth-chrome once (see Step 3 below).

### 3. Python 3.11+ (managed by uv automatically)

No manual Python install needed — `uv` downloads Python 3.11.15 on first use.

---

## Claude Code

### Option A — via CLI (recommended)

**Global (all projects):**
```bash
claude mcp add stealth-chrome --scope user -- uvx mcp-stealth-chrome@latest
```

**Project only (current directory):**
```bash
claude mcp add stealth-chrome -- uvx mcp-stealth-chrome@latest
```

### Option B — via config file

Add to `~/.claude/config.json` (global) or `.mcp.json` (project):

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

### Verify

Start Claude Code, then ask:
> "Use stealth-chrome to navigate to https://example.com and take a screenshot."

Expected: Chrome window opens, loads example.com, screenshot saved to `~/.mcp-stealth/screenshots/`.

---

## Claude Desktop

### macOS

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

### Windows

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

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

### Linux

Edit `~/.config/Claude/claude_desktop_config.json` (same JSON as above).

**After editing, restart Claude Desktop completely.**

### Verify

In Claude Desktop chat, ask:
> "What MCP tools do you have access to from stealth-chrome?"

Expected: list of 97 tools including `browser_launch`, `click_turnstile`, etc.

---

## Cursor

### Global (all projects)

Edit `~/.cursor/mcp.json`:

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

### Per-project

Edit `.cursor/mcp.json` in project root (same JSON as above).

Restart Cursor. Open Agent panel, verify stealth-chrome tools appear.

---

## Optional API Keys

For premium features, set environment variables in the `env` block:

```json
{
  "mcpServers": {
    "stealth-chrome": {
      "command": "uvx",
      "args": ["mcp-stealth-chrome@latest"],
      "env": {
        "CAPSOLVER_KEY": "CAP-xxxxxxxxxxxxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxxxxxxxxxxxx",
        "BROWSER_IDLE_TIMEOUT": "900"
      }
    }
  }
}
```

| Variable | What it enables | Get key |
|----------|----------------|---------|
| `CAPSOLVER_KEY` | `solve_captcha` tool (Turnstile, reCAPTCHA, hCaptcha) | [capsolver.com](https://capsolver.com) |
| `OPENAI_API_KEY` | `solve_recaptcha_ai` via OpenAI or any OpenAI-compatible API | [platform.openai.com](https://platform.openai.com) |
| `OPENAI_BASE_URL` | Custom endpoint for OpenAI-compat providers (Groq, Together, Ollama, custom gateway) | Your provider |
| `OPENAI_MODEL` | Vision-capable model name (see "multimodal required" note below) | — |
| `ANTHROPIC_API_KEY` | `solve_recaptcha_ai` via Claude | [console.anthropic.com](https://console.anthropic.com) |
| `ANTHROPIC_MODEL` | Claude model name (default `claude-opus-4-7`) | — |
| `BROWSER_IDLE_TIMEOUT` | Auto-close idle browsers after N seconds (default 600) | — |
| `BROWSER_IDLE_REAPER_INTERVAL` | Reaper check frequency (default 60) | — |

### ⚠️ Model Must Support Multimodal (Vision)

`OPENAI_MODEL` and `ANTHROPIC_MODEL` must be **vision-capable** — text-only models will fail silently.

✅ Examples: `gpt-4o`, `gpt-5.x`, `claude-opus-4-7`, `llava`, `llama-3.2-90b-vision-preview`
❌ Won't work: `gpt-3.5-turbo`, `llama3` (non-vision), `claude-3-haiku`

### Example: OpenAI-compatible provider (any /v1/chat/completions)

```json
{
  "mcpServers": {
    "stealth-chrome": {
      "command": "uvx",
      "args": ["mcp-stealth-chrome@latest"],
      "env": {
        "OPENAI_BASE_URL": "https://your-provider.example.com/v1",
        "OPENAI_API_KEY":  "your-key-here",
        "OPENAI_MODEL":    "model-name-with-vision"
      }
    }
  }
}
```

Works with any provider — Groq, Together.ai, Fireworks, DeepInfra,
Anyscale, custom LLM gateways, self-hosted vLLM/Ollama, etc. Uses OpenAI
SDK standard env convention (`OPENAI_API_KEY`, `OPENAI_BASE_URL`).

### Example: Local Ollama with llava vision (free, offline)

```json
"env": {
  "OPENAI_BASE_URL": "http://localhost:11434/v1",
  "OPENAI_API_KEY":  "ollama",
  "OPENAI_MODEL":    "llava:latest"
}
```

---

## Development Setup (from source)

If you want to contribute or modify:

```bash
git clone https://github.com/RobithYusuf/mcp-stealth-chrome
cd mcp-stealth-chrome
uv sync                         # installs all deps in .venv
uv run mcp-stealth-chrome       # run stdio server locally (exit with Ctrl+C)
```

Test with local path instead of PyPI:

```bash
# From your MCP client config:
"command": "uvx",
"args": ["--from", "/absolute/path/to/mcp-stealth-chrome", "mcp-stealth-chrome"]
```

Build distributable wheel:
```bash
uv build                        # outputs to dist/
```

---

## Troubleshooting

### "Chrome not found" or "browser launch failed"

nodriver couldn't find Chrome binary. Solutions:
1. Install Chrome from chrome.com
2. Or set custom path via `extra_args` in `browser_launch`:
   ```
   browser_launch(url="...", extra_args=["--binary-path=/your/chrome/path"])
   ```

### "Restore pages?" dialog intercepts navigation

Rare — if Chrome shows restore bubble on first launch despite our flags:
```bash
# Clear profile manually:
rm -rf ~/.mcp-stealth/profile
```

Next launch creates fresh profile.

### CAPTCHA solver returns "API key not set"

Double-check env var is passed to the MCP server process (not just shell):
```json
"env": {
  "CAPSOLVER_KEY": "CAP-actualkeyhere"
}
```

Test outside MCP:
```bash
CAPSOLVER_KEY=CAP-xxx uvx mcp-stealth-chrome
```

### Multiple Chrome windows stuck open

Kill orphans:
```bash
pkill -f "Google Chrome"                # macOS/Linux
taskkill /IM chrome.exe /F              # Windows
```

Then clear locks:
```bash
rm -f ~/.mcp-stealth/profile/Singleton*
```

### Tool fails with "Browser not running"

You forgot to call `browser_launch` (or `spawn_browser`) first. Every session starts without a browser.

### Profile grows large over time

Each session accumulates cookies, cache, etc. Clean up periodically:
```bash
# Nuke all profiles + fresh start:
rm -rf ~/.mcp-stealth/profile ~/.mcp-stealth/profiles
```

### `uvx` fails with "no such command"

Make sure `~/.local/bin` (where uv installs) is in your PATH:
```bash
# macOS/Linux:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc

# Windows:
# uv installer adds to PATH automatically; restart terminal.
```

### Server silently exits / tools don't appear

Run the server manually to see stderr:
```bash
uvx mcp-stealth-chrome
```

Common causes:
- Missing deps → `uv sync` from source
- Port conflict → nodriver picks free port automatically, retry
- Corrupted venv → `rm -rf ~/.cache/uv && uvx mcp-stealth-chrome@latest` (forces fresh install)

---

## Verification Checklist

After installing, verify in Claude:

- [ ] "List all tools from stealth-chrome" → should show 97 tools
- [ ] "Launch browser and navigate to bot.sannysoft.com" → Chrome opens, no "bot detected" warnings
- [ ] "Take a screenshot and save as test.png" → file saved to `~/.mcp-stealth/screenshots/`
- [ ] "Close browser" → Chrome window closes cleanly

If all 4 pass, installation is complete.

---

## Uninstall

```bash
# Remove from Claude Code:
claude mcp remove stealth-chrome

# Remove cached venv:
rm -rf ~/.cache/uv

# Remove data directory (cookies, screenshots, etc):
rm -rf ~/.mcp-stealth
```
