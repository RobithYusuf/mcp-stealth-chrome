# Sharing a browser with a person

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

A kept, headed browser may be the person's everyday browser too. Measured with a second CDP client standing in for
the person (the same target events as Cmd+T, a followed link, a closed tab).

- Tools act on the active tab only when the server opened or took it over (`BrowserState.owned_targets`): the
  launch's tabs, `tab_new`, reopened tabs, every tab of a kept browser on reattach and of an attached browser, and any
  page whose `openerId` is one of those (`_own_pages_opened_by_ours`, in `_refresh_tabs`; Chrome sets an opener for
  `rel=noopener` and keyboard-opened popups too). A browser never counted (owned set empty) takes every tab it holds
  as its own on the first look (`own_untracked`), so a single-user browser behaves exactly as before. `active_tab()`
  refuses any other tab by name (`not_ours_note`), and so do the two ways around it: `helpers.select_tab` (every
  `tab=` argument: evaluate, wait_for_function, get_text, screenshot, screenshot_element, inspect_image, and the
  same through batch_actions and workflow_run) and `tab_close(index=)`. Without them `evaluate(tab=N)` changed a
  person's page and `tab_close(index=N)` closed it. `tab_select` is the explicit request: it adopts the tab
  (`adopted_targets`) and its result says the server did not open it. `tab_list` marks such rows. A new tool that
  takes a tab index resolves it through `select_tab`, never `BrowserState.tabs[i]`.
- A browser the person opened from the launcher (`mcp-stealth-chrome open`, the Dock app; record `opened_by:
  launcher`) holds the person's tabs. A reattach takes over only the tabs it opens itself (url=, or the window it
  opens when there is none) and those an earlier agent session owned, saved at exit as the record's `owned`
  (`mark_kept(owned=)`; the launcher's own process saves none). It never reuses a person's tab for url=, selects an
  agent tab when there is one, and says how many tabs are the person's. `BrowserState.COUNTED` in `owned_targets`
  means "counted, none ours": an empty set would mean never counted and the first look would take every tab. A kept
  browser an agent launched is taken over whole, as before. On such a person's browser (`_persons_browser`) nothing of
  the agent's acts on every tab or ends it: `storage_state_load` navigates only the first tab when the server opened
  it, else the active tab the tools may use, else a server tab, else refuses naming `tab_new` (it used `browser.tabs[0]`,
  a person's tab); the record's identity is reapplied to the agent's tabs only; `block_ads` is not attached; the idle
  timeout is 0 (the reuse path and `spawn_browser` too). Browser-wide cookie reads stay: one cookie jar per profile is
  what sharing a browser means, and the reattach result says so. Every other `browser.tabs`, `browser.targets`,
  `tabs[0]`, `main_tab` and `_first_live_tab` use is bookkeeping (tab lists, target counts, window bounds, the tab
  record) or acts on a browser the launch just started.
  Measured with the real `open` command
  (`test_launcher_tabs_e2e.py`).
- An ownership refusal is `state.TabNotUsable` (a RuntimeError), raised by `active_tab()` and `select_tab` when the
  tab is not the server's or none is selected. The browser is fine then, and code tells it from a lost connection by
  its type, never its text: the reuse path of `browser_launch` treated it as a stale handle and ended the browser,
  the person's own on a launcher browser. Now it leaves the browser as it is and names tab_new and tab_select, or
  opens the caller's url in a new agent tab. `browser_recover` does not end a person's browser that answers CDP.
  Nothing else that closes or resets a browser (idle reaper, window sweep, close tools, failed launches) reacts to
  an `active_tab()` failure; `_tab_list_problem` only reports its text.
- An active tab closed outside the tools leaves no tab selected (`active_tab_index = -1`, `active_lost`), and the
  next tool says so. Before, the index stayed where it was and the next tool acted on whichever tab slid there, a
  person's included; a stale Tab answered "server rejected WebSocket connection: HTTP 500". `_check_active_tab_alive`
  runs before every tool outside `_RUNS_DESPITE_DIALOG` (`_app.BEFORE_TOOL_HOOKS`): one `Target.getTargetInfo`,
  which needs no renderer, so 0.50 notices a close before `update_targets`. Code reading
  `tabs[active_tab_index]` directly must bound-check it: -1 reads the last tab.
- `tab_close` of the active tab keeps its old choice (the tab now at its place, or the last) but only among tabs the
  tools may use (`_next_own_tab`). It waits up to 3 s for Chrome to stop listing the closed target and drops it from
  the list: `Target.closeTarget` answers first, and a list read in between made the next tool talk to a closing page
  and hang (1 in 3 runs).
- `tab_list` reads each tab with a 2 s limit and falls back to the target list's URL and title, naming the reason.
  It sits in `_RUNS_DESPITE_DIALOG` yet evaluates in every tab, so one alert in a tab no tool had touched hung it for
  ever. The server cannot answer a dialog in a tab it never armed (`dialog_handle` times out); the person can.
- nodriver adds `--remote-allow-origins=*`; `patches._patch_no_remote_allow_origins` removes it unless the caller
  passes it. A caller's value is honoured and named: in the `browser_launch` and `spawn_browser` result
  (`remote_allow_origins=*: kept because extra_args asked for it`) and per instance in
  `server_status.remote_allow_origins` (`off`, the value, read from the command line for a reattached browser, or
  "not started by this server" for an attached one). With it, a page on a local origin (a dev server, `file://`) that knows the browser websocket URL could
  drive the browser; without it Chrome answers 403 to any websocket with an Origin header. No client here sends one
  (nodriver, the quit watcher, keep_alive's close, block_ads, the Turnstile sidecar). Public pages are blocked by
  Local Network Access either way, and DNS rebinding by Chrome's Host check. Any local process still owns the port:
  a scan of every loopback port took 8.3 s and `Storage.getCookies` then returns plain text without a Keychain prompt.
  Only `--remote-debugging-pipe` held by a broker closes that; Chrome exits within 0.1 s of the pipe's holder dying,
  so the broker, not the server, would have to hold it for keep_alive to work.
- Typing interleaves: `type_text` while the person types in the same field gave `PagentEwritesRSONTYPING`. Overrides
  (`lang`, `fingerprint`) are per tab, so a tab the person opens shows the real identity.
