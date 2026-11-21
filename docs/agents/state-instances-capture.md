# State, instances and capture

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## Sessions and instances

- `BrowserState.tabs` and friends reflect the CURRENT instance of the CALLING MCP session; other instances live in
  `instances` as snapshots swapped by `switch_to()`. A metaclass routes those names to per-session state chosen by a
  ContextVar that `StealthMCP.call_tool` binds; a stdio server has one session, the primary, so there every call site
  behaves as if the state were global. A background task must bind a session itself (`in_session`), as the idle reaper
  does, or it only ever sees the primary.
- Each stdio client has its own server process, so sessions matter only over `--http`, and subagents sharing a
  connection are one session: session isolation does not separate them.
- A session that disconnects moves its browsers to the pool rather than dropping them: losing the handle orphans a live
  Chrome. Tools taking an `instance_id` refuse another session's (`foreign_instance`).
- `close_instance` of the current instance leaves that id current, not running; switching to main would let the next
  tool act on main's browser, which may be the person's. The next `browser_launch` starts that instance again on its own
  profile (`profiles/<id>`, `brave-profiles/<id>`); v0.19.0 did so naming neither, and an agent expecting main got
  another cookie jar. `instance_notes` wraps both tools: the close says the id stays current, and a launch of any
  instance but main names it and its profile on the summary line (the JSON body after it still parses).
- The HTTP transport always requires the token (never printed) and refuses non-loopback binds without `--allow-remote`.
  Use pure ASGI middleware: `BaseHTTPMiddleware` buffers the MCP event stream.
- An instance holding no browser names no removed folder: once a privacy profile is removed, the instance that
  still names it forgets it (`_forget_removed_profile`, called by `_remove_privacy_profile`). `list_instances` and
  `server_status` gave the removed folder as the profile after a `browser_close` and after a failed launch. A new
  browser's uptime starts when `_launch_record_browser` records it: `current_created_at` was never set there, so
  `list_instances` showed 84 s for a browser just launched.
- Per-tab caches are keyed by `_target_key(tab)` (the CDP target id), never `id(tab)`: the object address changes when
  nodriver recreates a `Tab` for the same target, and can be reused for another tab after GC.

## Capture buffers

- Capture buffers pass between snapshot and restore **by reference, never copied**: a CDP handler binds its sink when
  capture is armed, so a copy orphans it, and later events land in a list nobody reads while the one the user reads
  stops growing.
- Handlers bind their sink at arm time and never read `BrowserState.console_logs` at event time, which would file
  instance A's messages under whichever instance is current.
- Re-arming clears buffers **in place**: a fresh list leaves the installed handler writing to the old one (a second
  `network_start()` stopped capturing silently).
- Capture buffers are ring buffers (`CAPTURE_MAX_EVENTS`, `CAPTURE_MAX_INDEXED`, both env-tunable).
- `console_start`, `network_start`, `wait_for_network_idle` and `wait_for_request` return an error when their domain
  cannot be enabled: no event will come, and carrying on turns the failure into a quiet page. `console.clear()` fires a
  `consoleAPICalled` of type `clear`, which is not a message; keeping it left one in the buffer `console_clear` had just
  emptied.
- Subscribe before the action: a fast request finishes before `click` returns, so `click_and_wait` arms its watch first.
  CDP does not list requests already running; one started before arming is seen only through its later events, the
  stated limit of `wait_for_network_idle`.

## Response bodies

- Chrome forgets a response body when the document that loaded it goes (a 324-byte JSON reply read after a navigation
  answered "No resource with given identifier found"), and reading bodies eagerly at `loadingFinished` lost the same
  race on a page that navigates as its fetch resolves (Chrome 157). Keeping bodies is opt-in, settled by the developer:
  `network_start(keep_bodies=True)` turns on durable messages with `Network.configureDurableMessages`
  (`CAPTURE_BODY_BUFFER_BYTES`, 64 MB, the oldest dropped; `CAPTURE_BODY_MAX_BYTES`, 10 MB), which answers, so the
  result says "keeps" and states the cost. A Chrome without that command gets `Network.enable(enableDurableMessages)`,
  and the result says it asked but Chrome did not confirm.
- A plain `network_start` sends the plain `Network.enable` origin/main sent and keeps nothing; on a tab that kept bodies
  before, it sends `configureDurableMessages({})` so new ones stop being kept. A body lost to a navigation then reads
  "the page navigated ... network_start(keep_bodies=True) keeps bodies" (the request's `loaderId` against the tab's
  current main-frame document), not the raw CDP error.
- Kept bodies survive a same-site and a cross-site navigation, a later plain `Network.enable` from another tool does not
  turn keeping off, and 300 fetches loaded no slower (0.18 to 0.20 s kept, 0.17 to 0.25 s not, two runs each, two
  sessions). On Chrome 157 nothing drops bodies already kept: not `configureDurableMessages({})` (it only stops keeping
  new ones), not smaller sizes, not `Network.disable`. They belong to the tab's DevTools session: another session cannot
  read them (measured), and Chrome holds them until that session ends (tab or browser closed; that the memory is then
  freed is not measured).
- Every `network_start` forgets the request ids, so an old body cannot be looked up later, and bumps
  `_NETWORK_GENERATION`: a `network_get` still waiting on Chrome across a restart returns an error, not a body from
  before it.
- `network_get` never slices its JSON (`[:50000]` cut a body in half and returned text that did not parse): past 50,000
  characters it returns `{entries, omitted, note}` with the oldest left out, the whole envelope counted. With
  reveal_values="body" the warning is `reveal_note`, so the size `note` never replaces it (it did). URLs longer than
  `max_url_len` (200) are cut after masking, with `url_cut` saying how much and how to get it whole; `list_frames` does
  the same.
