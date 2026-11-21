# Settings (settings.py)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- The person's preferences are `settings.SETTINGS` (`agent_overlay`, `overlay_bar`, `overlay_light`, `overlay_lock`,
  `agent_cursor`, `overlay_hide_after`, `dock_icon`), read at call time from the environment, then
  `~/.mcp-stealth/settings.json` (`MCP_STEALTH_SETTINGS`, else `data_root()`), then the default. An MCP config is the
  explicit choice of whoever set up that server, so it wins; the file holds what the person picks in the overlay's menu
  or with `mcp-stealth-chrome settings` (list, set, reset). The developer chose this split over everything in the
  environment (20 Nov 2026): a look-and-feel choice should not need an MCP config edit and a restart.
- Only settings a person would change go there; paths, limits, test switches and API keys stay environment variables
  (the README's collapsed "Advanced and internal variables"). A new personal switch is a `Setting` read through
  `settings.get`, never `os.environ` directly, or the file and the menu silently do nothing for it.
- A value that does not parse is skipped with a note (`resolve`), never applied. The file is read with `O_NOFOLLOW`,
  only as a regular file owned by this user, else as no settings. It is written 0600 in a 0700 folder through a fresh
  `O_EXCL` file and `os.replace`, never through a link, under an flock on `.settings.json.lock` that both writers take
  (`settings._one_writer`, the overlay's `write_settings`): without it the second rename dropped the first writer's
  choice (75 of 120 lost in the test).
- The overlay applies `overlay_bar`, `overlay_light`, `overlay_lock`, `overlay_hide_after` and `agent_cursor` from the
  file as they change; a value the environment fixed arrives in `fixed` and wins. The menu's cursor item saves
  `agent_cursor`; the bar's arrow button toggles the cursor for that browser alone, and an unchanged file never undoes
  it.
- `server_status.settings` lists each value and its source (`default`, `file`, `env <NAME>`): a setting that "does not
  change" is usually fixed by an environment variable.
- `_agent_overlay.py` has its own copy of the reader and the order (it runs with `-I` and imports nothing of the
  package); `tests/test_settings.py` takes those functions out of its source with `ast` and checks they decide as
  `presence.style()` does. `tests/conftest.py` points `MCP_STEALTH_SETTINGS` at the run's own file, keeping the
  developer's preferences out of every test.
