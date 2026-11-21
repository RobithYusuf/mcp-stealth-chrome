# State, instances and capture

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- `BrowserState.tabs` and friends reflect the CURRENT instance of the CALLING MCP session; others live
  in `instances` as snapshots, swapped by `switch_to()`. A metaclass routes those names to a per-session
  state selected by a ContextVar, which `StealthMCP.call_tool` binds. A stdio server has one session, the
  primary, so every call site behaves as if the state were global. A background task must bind a session
  explicitly (`in_session`), as the idle reaper does, or it only ever sees the primary.
- `close_instance` of the current instance leaves that id current, not running; it is not switched to main,
  because a tool call after the close would then act on main's browser, which may be the person's. The next
  `browser_launch` starts that instance again on its own profile (`profiles/<id>`, `brave-profiles/<id>`).
  v0.19.0 did that answering "Browser launched ...", naming neither, and an agent expecting main got another
  cookie jar. `instance_notes` wraps both tools: the close says the id stays current, a launch of any instance
  but main names it and its profile on the summary line (the JSON body after it still parses).
- Each stdio client has its own server process, so sessions matter only over `--http`. Subagents
  sharing a connection are one session to the server: session isolation does not separate them.
- A session that disconnects moves its browsers to the pool rather than dropping them: losing the handle
  orphans a live Chrome. Tools that take an `instance_id` refuse another session's through
  `foreign_instance`.
- The HTTP transport always requires the token and refuses non-loopback binds without `--allow-remote`.
  The token is never printed. Use pure ASGI middleware: `BaseHTTPMiddleware` buffers the MCP event stream.
- Capture buffers are handed between snapshot and restore **by reference, never copied**. A CDP
  handler binds its sink when capture is armed; copying on switch orphans that handler, so the
  instance's later events land in a list nobody reads while the list the user reads stops growing.
- `console_start` and `network_start` return an error when their domain cannot be enabled, and
  `wait_for_network_idle` and `wait_for_request` do too. No event will ever arrive, so carrying on turns the
  failure into a quiet page. `console.clear()` fires a `consoleAPICalled` of type `clear`, which is not a
  message; keeping it left one behind in the buffer `console_clear` had just emptied.
- Subscribe before the action, not after. A fast request finishes before `click` returns, so
  `click_and_wait` arms its watch first. CDP does not list requests already running: a request started before
  arming is seen only through its later events, which is the stated limit of `wait_for_network_idle`.
- Handlers bind their sink at arm time and never read `BrowserState.console_logs` at event time.
  Reading the class attribute inside a handler files instance A's messages under whichever instance
  happens to be current.
- Re-arming clears buffers **in place**. Rebinding to a fresh list leaves the already-installed
  handler writing to the old one — a second `network_start()` used to stop capturing silently.
- Per-tab caches are keyed by `_target_key(tab)` (the CDP target id), never `id(tab)`. The Python
  object address changes when nodriver recreates a `Tab` for the same target and can be recycled onto
  a different tab after GC.
- Capture buffers are ring buffers (`CAPTURE_MAX_EVENTS`, `CAPTURE_MAX_INDEXED`, both env-tunable).
- Chrome forgets a response body when the document that loaded it goes: a 324-byte JSON reply read after the page
  navigated answered "No resource with given identifier found". Reading bodies eagerly at `loadingFinished` lost the
  same race on a page that navigates as soon as its fetch resolves (measured, Chrome 157). Keeping bodies is opt-in,
  settled by the developer: `network_start(keep_bodies=True)` turns on durable messages with
  `Network.configureDurableMessages` (sizes `CAPTURE_BODY_BUFFER_BYTES`, 64 MB, the oldest dropped, and
  `CAPTURE_BODY_MAX_BYTES`, 10 MB), which answers, so the result says "keeps" and states the cost; on a Chrome
  without that command it falls back to `Network.enable(enableDurableMessages)` and says it asked but Chrome did
  not confirm. A plain `network_start` sends the plain `Network.enable` origin/main sent and keeps nothing; on a tab
  that kept bodies before it sends `configureDurableMessages({})` so new ones stop being kept. A body lost to a
  navigation then reads "the page navigated ... network_start(keep_bodies=True) keeps bodies" (the request's
  `loaderId` against the tab's current main-frame document), not the raw CDP error. Kept bodies survive a same-site
  and a cross-site navigation, a later plain `Network.enable` from another tool does not turn keeping off, and 300
  fetches loaded no slower (0.18 to 0.20 s kept against 0.17 to 0.25 s not kept, two runs each, two sessions).
  Measured on Chrome 157, no command drops bodies already kept: not `configureDurableMessages({})` (it stops keeping
  new ones), not smaller sizes, not `Network.disable`. They belong to the tab's DevTools session: another session
  cannot read them (measured), and Chrome holds them until that session ends (tab or browser closed; that the
  memory is freed then is not measured). Every `network_start` forgets the request ids, so later calls cannot look
  an old body up, and bumps `_NETWORK_GENERATION`: a `network_get` still waiting on Chrome across a restart returns
  an error instead of a body from before it.
- `network_get` never slices its JSON: past 50,000 characters it returns `{entries, omitted, note}` with the oldest
  left out, the whole envelope counted. With reveal_values="body" the warning is `reveal_note`, so the size `note`
  never replaces it (it did). `[:50000]` cut a body in half and returned text that did not parse. URLs longer than `max_url_len`
  (200) are cut after masking, with `url_cut` saying how much and how to get it whole; `list_frames` does the same.
