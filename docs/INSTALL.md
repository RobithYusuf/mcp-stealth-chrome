# Installation and troubleshooting

The [README](../README.md) has the install commands and the config file for each client. This page covers checking the install, running from source, and fixing common problems.

## Check the install

Restart your client after changing its config, then ask it:

1. "List the tools from stealth-chrome." You should see 172 tools.
2. "Launch the browser and open bot.sannysoft.com." Chrome opens and loads the page.
3. "Take a screenshot." The file lands in `~/.mcp-stealth/screenshots/`.
4. "Close the browser." The window closes.

`server_status` reports the version that is actually running. Check it first when a change seems to have no effect.

For Claude Code, the project config file is `.mcp.json`. Use `claude mcp add --scope user` for all projects.

## Run from source

```bash
git clone https://github.com/RobithYusuf/mcp-stealth-chrome
cd mcp-stealth-chrome
uv sync
uv run mcp-stealth-chrome
```

To point a client at your checkout, use `"command": "uvx"` with `"args": ["--from", "/absolute/path/to/mcp-stealth-chrome", "mcp-stealth-chrome"]`. `uv build` writes a wheel to `dist/`.

## Troubleshooting

### Chrome is not found

Install Chrome in its default location, or make it available on `PATH` as `google-chrome` or `chromium`. A wrapper script on `PATH` should use `exec` so that it becomes Chrome.

### A browser is stuck, or the launch reports "CDP unreachable"

Run `browser_recover()`. It ends only the Chrome processes started against this server's profiles, so your daily browser is safe, and it clears stale lock files. Then launch again.

Avoid `pkill "Google Chrome"`, which also closes your own browser.

### A launch fails with "Failed to connect to browser"

Chrome did not answer in time. The server waits for it and tries once more, so this usually means the machine is very busy, for example many browsers starting together. Try again shortly.

### macOS shows "A keychain cannot be found to store Chrome"

Click Cancel, never "Reset To Defaults", which can reset your login keychain. The dialog appears when Chrome runs with a temporary `HOME`, as a test setup might. Pass `--use-mock-keychain` in `extra_args` in that setup.

### A "Restore pages?" bubble covers the page

Run `browser_recover()`, then launch again. Your logins stay.

### The profile has grown large

`profile_report()` shows the size and use of every profile. `profile_cleanup()` previews what can go and removes it with `confirm=True`. It clears regenerable caches and keeps logins.

Avoid deleting `~/.mcp-stealth/profile` by hand. It holds your logins.

### A solver says the API key is not set

The key has to reach the server process, so put it in the `env` block of the client config and not only in your shell. See [API keys](../README.md#api-keys).

### `click_turnstile` does nothing useful

It handles the embedded widget. Cloudflare's full-page "Just a moment..." challenge needs `solve_captcha` with a `CAPSOLVER_KEY`, or a session restored with `storage_state_load`.

### "Browser not running"

Call `browser_launch` or `spawn_browser` first. Every session starts without a browser, and a `spawn_browser` instance closes after `BROWSER_IDLE_TIMEOUT` when idle.

### `uvx` is not found

Add `~/.local/bin` to your `PATH` and restart the terminal:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

On Windows the uv installer adds it for you.

### The client says "Connected" but shows no tools

The first run downloads about 200 MB of dependencies, which can outlast the client's 30 second handshake. Pick one fix:

- Raise the timeout with `export MCP_TIMEOUT=90000`, then restart the client.
- Fill the cache before starting the client: `echo '' | uvx mcp-stealth-chrome@latest`.
- Use `"args": ["mcp-stealth-chrome"]` without `@latest`, which skips the version check once cached.

### The server exits at once

Run `uvx mcp-stealth-chrome` in a terminal to see the error. For a damaged install, run `uv cache clean mcp-stealth-chrome` and start it again.

## Uninstall

```bash
claude mcp remove stealth-chrome
uv cache clean mcp-stealth-chrome
```

`~/.mcp-stealth` holds your profiles, logins, screenshots and exports. Remove it by hand only if you no longer need them.
