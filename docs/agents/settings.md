# Settings (settings.py)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- The person's preferences are `settings.SETTINGS`: `agent_overlay`, `overlay_bar`, `overlay_light`, `overlay_lock`,
  `agent_cursor`, `overlay_hide_after`, `dock_icon`. Order: the environment, then `~/.mcp-stealth/settings.json`
  (`MCP_STEALTH_SETTINGS`, else `data_root()`), then the default, read at call time. An MCP config is the explicit
  choice of whoever set that server up, so it wins; the file is what the person picks from the overlay's menu or with
  `mcp-stealth-chrome settings` (list, set, reset). Asked about putting everything in the environment, the developer
  chose this split (20 Nov 2026): a person should not have to edit an MCP config and restart for a look-and-feel choice.
- Only settings a person would change go there. Paths, limits, test switches and API keys stay environment variables,
  in the README's collapsed "Advanced and internal variables". A new personal switch is a `Setting`, read through
  `settings.get`, never `os.environ` directly, or the file and the menu silently do nothing for it.
- A value that does not parse is skipped with a note (`resolve`), never applied; the file is read with `O_NOFOLLOW`,
  only as a regular file owned by this user, and anything else reads as no settings. It is written 0600 in a 0700
  folder through a fresh `O_EXCL` file and `os.replace`, never through a link, under an flock on `.settings.json.lock`
  taken by both writers (`settings._one_writer` and the overlay's `write_settings`): without it, two writers each read
  the old file and the second rename dropped the first one's choice (75 of 120 lost in the test).
- The overlay applies `overlay_bar`, `overlay_light`, `overlay_lock`, `overlay_hide_after` and `agent_cursor` from the
  file as they change; a value the environment fixed comes in `fixed` and wins. The menu's cursor item saves
  `agent_cursor`; the bar's arrow button toggles the cursor for that browser alone, and an unchanged file never undoes it.
- `server_status.settings` lists each value and its source (`default`, `file`, `env <NAME>`): a setting that "does
  not change" is usually one an environment variable fixes.
- `_agent_overlay.py` has its own copy of the reader and of the order (it runs with `-I` and imports nothing of the
  package); `tests/test_settings.py` takes those functions out of its source with `ast` and checks they decide as
  `presence.style()` does. `tests/conftest.py` points `MCP_STEALTH_SETTINGS` at a file of the run's own, so the
  developer's own preferences never reach a test.
