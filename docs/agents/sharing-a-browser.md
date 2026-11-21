# Sharing a browser with a person

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

A kept, headed browser may be the person's everyday browser too. Measured with a second CDP client standing in for
the person (the same target events as Cmd+T, a followed link, a closed tab).

## Which tabs a tool may use

- Tools act only on tabs the server opened or took over (`BrowserState.owned_targets`): the launch's, `tab_new`'s,
  reopened ones, every tab of a kept browser on reattach or of an attached browser, and pages whose `openerId` is one of
  those (`_own_pages_opened_by_ours` in `_refresh_tabs`; Chrome sets an opener for `rel=noopener` and keyboard-opened
  popups too). A browser never counted (owned set empty) owns every tab at the first look (`own_untracked`), so a
  single-user browser behaves as before.
- `active_tab()` refuses any other tab by name (`not_ours_note`), as do the two ways around it, `helpers.select_tab`
  (every `tab=` argument: evaluate, wait_for_function, get_text, screenshot, screenshot_element, inspect_image, also
  through batch_actions and workflow_run) and `tab_close(index=)`; without them `evaluate(tab=N)` changed a person's
  page and `tab_close(index=N)` closed it. `tab_select` is the explicit request: it adopts the tab (`adopted_targets`),
  saying the server did not open it, and `tab_list` marks such rows. A new tool taking a tab index resolves it through
  `select_tab`, never `BrowserState.tabs[i]`.
- An ownership refusal is `state.TabNotUsable` (a RuntimeError) from `active_tab()` or `select_tab`: the tab is not the
  server's, or none is selected. The browser is fine, so code tells it from a lost connection by type, never text: the
  reuse path of `browser_launch` took it for a stale handle and ended the browser, on a launcher browser the person's
  own. It now leaves the browser as it is and names tab_new and tab_select, or opens the caller's url in a new agent
  tab. `browser_recover` does not end a person's browser that answers CDP. Nothing else that closes or resets a browser
  (idle reaper, window sweep, close tools, failed launches) reacts to an `active_tab()` failure; `_tab_list_problem`
  only reports its text.
- An active tab closed outside the tools leaves none selected (`active_tab_index = -1`, `active_lost`) and the next tool
  says so; a kept index made it act on whichever tab slid there, a person's included, and a stale Tab answered
  "server rejected WebSocket connection: HTTP 500". `_check_active_tab_alive` runs
  before every tool outside `_RUNS_DESPITE_DIALOG` (`_app.BEFORE_TOOL_HOOKS`): one `Target.getTargetInfo`, which needs
  no renderer, so 0.50 notices a close before `update_targets`. Code reading `tabs[active_tab_index]` must bound-check
  it: -1 reads the last tab.
- `tab_close` of the active tab picks as before (the tab now in its place, or the last) but only among usable tabs
  (`_next_own_tab`). It waits up to 3 s for Chrome to stop listing the closed target and drops it from the list:
  `Target.closeTarget` answers first, and a list read in between sent the next tool to a closing page, where it hung (1
  in 3 runs).
- `tab_list` reads each tab within 2 s, else falls back to the target list's URL and title and says why: it is in
  `_RUNS_DESPITE_DIALOG` yet evaluates in every tab, and one alert in a tab no tool had touched hung it for ever. The
  server cannot answer a dialog in a tab it never armed (`dialog_handle` times out); the person can.

## A browser the person opened from the launcher

- A browser opened from the launcher (`mcp-stealth-chrome open`, the Dock app; record `opened_by: launcher`) holds the
  person's tabs. A reattach takes over only the tabs it opens (url=, or a window when there is none) and those an
  earlier agent session owned, saved at exit as the record's `owned` (`mark_kept(owned=)`; the launcher's own process
  saves none). It never reuses a person's tab for url=, selects an agent tab if there is one, and says how many tabs are
  the person's. `BrowserState.COUNTED` in `owned_targets` means "counted, none ours": an empty set means never counted,
  and the first look would take every tab. A kept browser an agent launched is taken over whole, as before. Measured
  with the real `open` command (`test_launcher_tabs_e2e.py`).
- On such a person's browser (`_persons_browser`) nothing the agent does acts on every tab or ends it:
  `storage_state_load` navigates the first tab only if the server opened it, else the usable active tab, else a server
  tab, else refuses naming `tab_new` (it used `browser.tabs[0]`, a person's tab); the record's identity is reapplied to
  the agent's tabs only; `block_ads` is not attached; the idle timeout is 0 (also on the reuse path and in
  `spawn_browser`). Browser-wide cookie reads stay, since one cookie jar per profile is what sharing a browser means,
  and the reattach result says so. Every other use of `browser.tabs`, `browser.targets`, `tabs[0]`, `main_tab` and
  `_first_live_tab` is bookkeeping (tab lists, target counts, window bounds, the tab record) or acts on a browser the
  launch just started.

## The debugging port and input

- nodriver adds `--remote-allow-origins=*`; `patches._patch_no_remote_allow_origins` removes it unless the caller passed
  it. A caller's value is honoured and named in the `browser_launch` and `spawn_browser` result
  (`remote_allow_origins=*: kept because extra_args asked for it`) and per instance in
  `server_status.remote_allow_origins` (`off`, the value, read from the command line for a reattached browser, or "not
  started by this server" for an attached one). With it, a page on a local origin (a dev server, `file://`) that knows
  the browser websocket URL could drive the browser; without it Chrome answers 403 to any websocket with an Origin
  header, which no client here sends (nodriver, the quit watcher, keep_alive's close, block_ads, the Turnstile sidecar).
  Public pages are blocked by Local Network Access either way, and DNS rebinding by Chrome's Host check.
- Known limit: any local process still owns the port (a scan of every loopback port took 8.3 s, and `Storage.getCookies`
  then returns plain text without a Keychain prompt). Only `--remote-debugging-pipe` held by a broker closes that, and
  since Chrome exits within 0.1 s of the pipe's holder dying, the broker, not the server, would have to hold it for
  keep_alive to work.
- Typing interleaves: `type_text` while the person types in the same field gave `PagentEwritesRSONTYPING`. Overrides
  (`lang`, `fingerprint`) are per tab, so a tab the person opens shows the real identity.
