# Settings (settings.py)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- The person's preferences are `settings.SETTINGS` (`agent_overlay`, `overlay_bar`, `overlay_light`, `overlay_lock`,
  `agent_cursor`, `overlay_hide_after`, `dock_icon`, `antibot_tripwire`, `antibot_rules`), read at call time from the
  environment, then `~/.mcp-stealth/settings.json` (`MCP_STEALTH_SETTINGS`, else `data_root()`), then the default. An
  MCP config is the explicit choice of whoever set up that server, so it wins; the file holds what the person picks in
  the overlay's menu or with `mcp-stealth-chrome settings` (list, set, reset). The developer chose this split over
  everything in the environment (20 Nov 2026): a look-and-feel choice should not need an MCP config edit and a
  restart.
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
- `antibot_tripwire` is a choice (pause, warn, off; default pause), read once per look together with
  `antibot_rules` (one read of the file for both). Off reads no page at all.
- `antibot_rules` is the one setting of kind "rules": a JSON object `{"example.com": {"url": ["^/verify/"], "text":
  ["Verify to continue"]}}`, in the file as an object and in `MCP_STEALTH_ANTIBOT_RULES` as JSON text. `url` holds
  regular expressions searched in the path and query, never the host; `text` holds words of the page, any case, read
  only on that host; a host covers its subdomains. `settings.parse_rules` refuses the whole value when any part is
  wrong (an unknown key, an empty list, a regex that does not compile, more than 50 hosts, 20 patterns a list or 200
  characters a pattern): a rule taken in half would stop runs, or miss pages, in ways nobody asked for. `settings
  list` shows it as "N hosts"; the overlay's menu does not show it.
- A person's regex runs on every URL of its host and Python's `re` has no time limit: a pattern with nested
  repetition can be slow on a long URL. That is the person's own rule; nothing checks it for that.
