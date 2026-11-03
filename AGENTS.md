# mcp-stealth-chrome — Agent Guide

This guide applies to the whole repository and is the shared source of instructions for coding
agents. It is not here to govern every decision — it exists to preserve the invariants that are
easy to miss, almost all of which were learned from a shipped bug. Developer requests may override
working defaults; hard constraints may only be overridden when the developer explicitly asks.

## Product and repository map

An MCP server (stdio JSON-RPC) that gives an agent a stealth browser: 172 tools over
[nodriver](https://github.com/ultrafunkamsterdam/nodriver) (direct CDP, no WebDriver), plus a
browserless HTTP path with a real Chrome TLS fingerprint. Published on PyPI as `mcp-stealth-chrome`.

```
uvx mcp-stealth-chrome → Python 3.11+ → FastMCP → nodriver → Chrome/Chromium (CDP)
                                              └→ curl_cffi (Chrome JA3/JA4) → browserless HTTP
                                              └→ opencv-python → template matching / vision
```

- `src/mcp_stealth_chrome/server.py` — entry plus most tools, sectioned by `══════` banners.
- `src/mcp_stealth_chrome/state.py` — `BrowserState` (multi-instance), profile discovery, Chrome
  lookup, lock/PID helpers.
- `src/mcp_stealth_chrome/snapshot.py` — the snapshot script template and `format_snapshot`.
- `src/mcp_stealth_chrome/refs.py` — server-side refs, the frame walk, `resolve_ref`.
- `src/mcp_stealth_chrome/workflow.py` — the `workflow_run` engine, free of browser imports.
- `src/mcp_stealth_chrome/http_transport.py` — `--http`: token, bind rules, Mcp-Session-Id.
- `src/mcp_stealth_chrome/helpers.py` — `ok`/`err`, tab selection, path confinement, JSON parsing.
- `src/mcp_stealth_chrome/patches.py` — runtime monkey-patches for nodriver/CDP drift.
- `src/mcp_stealth_chrome/tools/` — `research.py`, `devtools.py`, `vision.py`, `network_http.py`, `profiles.py`;
  each registers itself on import.
- `tests/test_registry.py` — the tool-surface gate. `tests/test_reliability_e2e.py` and
  `tests/test_agent_ergonomics.py` — behaviour, real browser and fast unit respectively.
- `README.md` — the authoritative tool list. `docs/` — install, demo, research method, proof images.
- Runtime data lives outside the repo: `~/.mcp-stealth/{profile,profiles,screenshots,exports,
  storage-states,identities,window-state}`.

## Hard constraints

- Never commit or print a credential. A session cookie is one: `cookie_list` masks values unless
  `reveal_values=True`, because tool output lands in the transcript, and files holding them are mode 600. The PyPI token lives in the macOS Keychain
  (`security find-generic-password -s mcp-stealth-chrome-pypi -w`), never in a file in the tree.
  Cookie and token files used during a release go to `/tmp` mode `600` and are `shred`ed after.
- `nodriver` must stay pinned `<0.49` until this code is ported: 0.50 rebuilt `Connection` and `Tab` around
  sessions, breaking the OOPIF connection and the identity override, and `uv.lock` hides it just as it does
  for `mcp`. Run the E2E suites in a lock-free venv before lifting it.
- E2E tests never use the real profile (`persistent=False`, or a temporary HOME with `--use-mock-keychain`).
  Holding `~/.mcp-stealth/profile` pushes other live sessions onto an empty fallback profile.
- `mcp` must stay pinned `<2`. Version 2.0.0 removed `mcp.server.fastmcp` entirely, and this server
  is built on FastMCP. `uv.lock` hides the breakage from the test suite, so a green suite proves
  nothing here — the failure only appears in a lock-free install (`uv tool install`, `uvx`, and
  therefore every PyPI user). Lift the cap only together with a port off FastMCP.
- Every `@mcp.tool()` is annotated `-> str` and returns through `ok()` / `err()`. Returning a dict
  raises a pydantic error inside FastMCP.
- Do not add a tool that can fail silently. If an action may not have taken effect, the response says
  so — see the honest-reporting invariants below.
- Do not weaken a security property to make a test pass: managed paths never escape their root,
  identity files are written `0600`, and a profile clone only ever opens the source read-only.
- Anything that deletes a profile (`clone_chrome_profile(overwrite=True)`, `profile_cleanup(confirm=True)`,
  privacy cleanup, `browser_recover`) needs an explicit request. The shared profile holds the developer's real logins.

## Working defaults

- Understand the affected flow, then make the smallest coherent change that solves it.
- Prefer the existing shared helpers (`_target_key`, `_click_probe`, `_page_sig`, `_wait`,
  `confined_output_path`, `parse_json`) over a local variant. A tool that hand-rolls one of these
  drifts away from the fixes the others already carry.
- Verify claims by running them. Most defects in this repo were found by using the server, not by
  reading it — including a destructive path escape and a dependency cap that broke every fresh
  install while all tests passed.
- Report honestly: if a check was skipped, say so; if a test crashed rather than passed, say that.
- The developer manages releases. Bump, publish and push only when asked.

## Architecture invariants

### Honest reporting

This is the spine of the project. A tool that reports a rosier state than reality is the worst
failure mode for an agent: the caller cannot tell, so a wrong "success" spreads into wrong
conclusions. Each rule below replaced a real lie.

- A click result must reflect what happened. `click` / `click_text` / `click_role` hit-test the target
  with `elementFromPoint` on its own root node (a shadow root answers for itself; `document` stops at the
  host) before clicking and refuse when the point belongs to something else, naming the blocker. With
  `verify=True` (default) they compare a fingerprint before and after and say
  `clicked but NO page change detected` when nothing moved. The fingerprint includes the element's own
  tree and state, or an iframe, shadow tree or checkbox change reads as no change. A real click focuses its
  target by itself, so focus landing on the clicked element is not a change.
- `Element.click()` in nodriver is a JS `el.click()` (`isTrusted=false`) preceded by `flash()`, which
  inserts a node into the page. Never call it. Use `_click_and_report`, which dispatches real input at
  the settled position; a JS click only happens with `js_fallback=True` and the result says so.
- Wait for the element to stop moving before a hit test. `scroll_to` defaults to smooth, and a target
  measured mid-scroll passes the test and is then clicked where it no longer is.
- Refusing is not enough on its own. When the first text match is unusable, `click_text` looks for
  another element carrying the same text that is genuinely reachable (scrolling it into view if that
  is the only problem) before giving up. On github.com the fuzzy match is a hidden marketing `<span>`;
  refusing there was truthful and still left the caller unable to click "Issues".
- An empty result must be distinguishable from "not measured". `console_get` returns
  `{attached, capturing, since, total, messages}`; `intercept_log` reports `active` and `stop_reason`;
  `storage_snapshot` marks each area `measured` and collects failures in `errors`, because a snapshot
  that silently failed to read cookies makes `storage_diff` blame the action under test.
- Absence of evidence is not evidence. `_ensure_browser_dead()` sets `verified` only when a process
  was positively observed to be gone; with no PID and no profile to scan it stays false and the
  caller must not claim a clean close.
- `server_status` exposes `compat_patches`. A patch that fails to apply does not crash anything — it
  makes the CDP listener die on the first mismatched payload, so captures return empty and look like
  a quiet page.

### State, instances and capture

- `BrowserState.tabs` and friends reflect the CURRENT instance of the CALLING MCP session; others live
  in `instances` as snapshots, swapped by `switch_to()`. A metaclass routes those names to a per-session
  state selected by a ContextVar, which `StealthMCP.call_tool` binds. A stdio server has one session, the
  primary, so every call site behaves as if the state were global. A background task must bind a session
  explicitly (`in_session`), as the idle reaper does, or it only ever sees the primary.
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
- Handlers bind their sink at arm time and never read `BrowserState.console_logs` at event time.
  Reading the class attribute inside a handler files instance A's messages under whichever instance
  happens to be current.
- Re-arming clears buffers **in place**. Rebinding to a fresh list leaves the already-installed
  handler writing to the old one — a second `network_start()` used to stop capturing silently.
- Per-tab caches are keyed by `_target_key(tab)` (the CDP target id), never `id(tab)`. The Python
  object address changes when nodriver recreates a `Tab` for the same target and can be recycled onto
  a different tab after GC.
- Capture buffers are ring buffers (`CAPTURE_MAX_EVENTS`, `CAPTURE_MAX_INDEXED`, both env-tunable).

### Browser lifecycle

- `browser_launch` reuses a running browser only after CDP actually answers. `is_up()` proves a
  handle exists, not that the socket is alive, and a stale handle used to pass. The reuse path also
  honours the caller's `url` — answering "already running" while ignoring it left callers on the
  wrong page believing the navigation had happened.
- Starting a second Chrome against the same profile is what produces "CDP unreachable". A half-dead
  browser is ended before relaunching rather than raced.
- `browser.stop()` speaks CDP and can return **without raising** when the socket is gone. Close paths
  go through `_ensure_browser_dead()`, which verifies the process really died and signals the PID
  nodriver recorded at launch when it did not. Reporting a clean close is what creates an orphan: it
  drops the only handle that knew the process existed.
- Window placement is remembered per instance in `~/.mcp-stealth/window-state/`. Validation applies
  the saved rect and reads it BACK — Chrome clamps a window onto a connected display, so a mismatched
  read-back is itself proof the monitor is gone. No display API is queried. Implausible sizes and
  minimised/fullscreen states are refused; `wipe_window_state()` still clears Chrome's own copy,
  which is the cure for the corrupt-placement invisible-window bug.
- Profile affinity: a process stays on the profile it settled on. Without it, `browser_recover` frees
  the shared default and the next launch silently moves to a different cookie jar, so every login
  looks wiped while the data sits untouched in the old directory.

### Refs, snapshots and paths

- Nothing is written to the page. The snapshot runs in an isolated world and returns element handles,
  which `refs.py` maps to `backendNodeId` on the server. A `data-mcp-ref` attribute or a `flash()` dot is
  visible to any `MutationObserver`, so do not reintroduce a marker to find an element again: hold a handle
  (`evaluate_parts`, `active_element`).
- A cross-origin iframe is its own CDP target even with nodriver's site-isolation flag, and is absent
  from the parent's `Page.getFrameTree`. Its quads are local to its own viewport; `RefElement.get_position`
  adds the owner `<iframe>`'s content box. The frame's own hit test cannot see an overlay drawn by the
  parent, which is what `outer_blocker` is for.
- `DOM.describeNode` keeps answering for a node from a previous document. Staleness is proved by
  `DOM.resolveNode` plus `isConnected`, as `resolve_ref` does.
- Set attributes on a nodriver `Element` only with a leading underscore. `Element.__setattr__` silently
  forwards any other name to the HTML attributes, so `el.foo = x` reads back as `None`.
- `delta=True` marks elements added or changed since the tab's previous snapshot with `*`. The
  fingerprint deliberately EXCLUDES the ref: refs are regenerated every snapshot, so including them
  would mark everything as changed, every time.
- An unchanged `diff_from_last` result keeps the measured element count and says
  `unchanged; details omitted`. Printing `Interactive elements (0)` read as "this page has no
  interactive elements".
- Every managed output path goes through `confined_output_path` / `_direct_child`. `..` once passed
  the identity regex, so `clone_chrome_profile(target_instance_id="..", overwrite=True)` resolved to
  `~/.mcp-stealth` and would have deleted every profile, screenshot and identity in it.

### The browserless HTTP path

- `curl_cffi` presents a real Chrome JA3/JA4, matching the browser this server drives. `http_request`,
  `scrape_markdown` and `smart_fetch` never launch a browser; `smart_fetch` escalates only when the
  page looks blocked or SPA-empty.
- `export_curl` emits both a `curl` string and an `http_request(impersonate='chrome')` call, and
  recommends the second. Replaying a captured request with plain curl sends a curl TLS fingerprint —
  the request that worked in the browser then fails on the command line for reasons no header diff
  explains.
- Volatile headers (`Content-Length`, `Host`, `:authority`) are dropped rather than replayed stale;
  a mismatched Content-Length is itself a bot signal.

### Request interception

- `Fetch.enable` pauses EVERY matching request until something answers it, and a handler that fails
  to answer does not raise — it hangs the page silently. Every path through the handler, including
  the exception path, ends in exactly one `continue`/`fail`/`fulfill`.
- Interception disables itself on a deadline, and `_clear_tab_scoped_state` releases it when its
  target closes. A stale intercept would otherwise pause requests on the next browser launched.

## Chrome, CDP and nodriver facts

Platform truths, not preferences. Each one cost a release.

- `Network.setUserAgentOverride` needs `user_agent_metadata`, or `navigator.userAgentData.brands` comes back
  EMPTY, which no real Chrome reports. It also requires a UA even to change only the language. Identity
  defaults to the real browser and machine in every mode, and `_apply_fingerprint_to_tab` derives the hints
  from whatever UA is in force. Never invent a default identity: a random OS or Chrome version over a real
  engine contradicts itself. Overrides are per launch, through `fingerprint`.
- `Network.setUserAgentOverride(acceptLanguage)` is split on commas for `navigator.languages` and also
  generates the weighted header. Pass the bare tags: passing `id-ID,id;q=0.9` reports a language named
  `id;q=0.9`. Weights Chrome would not generate go through `setExtraHTTPHeaders`.
- A key event with only `key` set fires keydown but no keypress, so Enter never submits a form. Use
  `key_event_params`, which fills `code`, the virtual key code and `text`.
- Do not call `Tab.mouse_move`. It interpolates from (0,0), not from the cursor, sweeping a diagonal across
  the page that fires hover handlers, then sends a `mouseReleased` the page sees as a stray mouseup. Use
  `move_pointer`, or `humanized_move` for a path.
- `DOM.getNodeForLocation` takes document coordinates; every other position in CDP here is viewport-relative.
  Add the scroll offset, or the hit test checks the wrong spot on a scrolled page.
- `window.devicePixelRatio` includes browser zoom, and a persistent profile remembers zoom per host, so a
  host once zoomed to 90% reports 0.9. A clip still rasterises at CSS size there. E2E tests use
  `persistent=False` for this reason, and so as not to write into the real profile.

- `tab.evaluate(expr, return_by_value=True)` can STILL return a `RemoteObject`. Unwrap via
  `v.value if hasattr(v, "value") ...` before casting.
- `tab.evaluate` does not await Promises unless `await_promise=True`. Without it every async
  expression returns `null`, silently.
- nodriver's `Connection.backup()`-style CDP calls are not the problem, but SQLite's
  `Connection.backup()` is: against a database a live browser keeps writing it blocks inside C, ignores
  signals, and hangs indefinitely. Copy the file plus its `-wal`/`-shm` companions and integrity-check
  the copy instead.
- Chrome omits CDP fields it used to send, and nodriver indexes them directly. `sameParty` (cookies)
  and `privateNetworkRequestPolicy` (`ClientSecurityState`) both raise KeyError **inside the event
  listener**, killing it — captures then return empty rather than erroring. `patches.py` fills the
  documented defaults; `patches.status()` reports what applied.
- Chromium locks the **user-data root**, not `Default`/`Profile N`. Checking the sub-profile for a
  lock reports every profile as free while the browser is running.
- Cookies live at `Default/Cookies` on older profiles and `Default/Network/Cookies` on newer ones.
  Counting only the first makes a busy profile look empty.
- `navigator.clipboard.writeText` needs BOTH a trusted gesture and OS window focus. `mouse_click_xy`
  supplies the gesture; a JS `.click()` does not, and even a real click is refused when the window is
  not frontmost — call `tab_focus()` first.
- Hover before pressing. Menus and overlays that only render on hover (Canva, Figma, Notion) are
  unreachable with a bare `mouse_click_xy`; `move_first=True` is the default for that reason.
- `Element.text_all` is `textContent`-based and includes `<script>` bodies. Use innerText semantics
  for anything a human is supposed to read.
- `:has-text()` is a Playwright-ism, not valid CSS. `querySelector` throws on it, which surfaced as a
  bare "DOM Error while querying".
- SERP scraping soft-blocks on datacenter IPs and cold headless profiles. Google and Bing bot-wall a
  fresh profile; a warm persistent profile in a real browser passes, and `engine="brave"` returns the
  richest results with relative dates. An empty SERP is usually the environment, not the code.

## Documentation on demand

- `README.md` is the authoritative tool list and must be updated in the same change that adds,
  removes or modifies a tool: hero count, comparison table, the category tables, new parameters.
  Bump the version in BOTH `pyproject.toml` and `src/mcp_stealth_chrome/__init__.py`, and update
  `tests/test_registry.py` (`EXPECTED_COUNT` and the name list).
- The PyPI `description` in `pyproject.toml` carries its own tool count. It has drifted twice.
- Audit the tool tables against `mcp._tool_manager._tools` rather than counting by eye.
- Record a decision here when it is non-obvious and would otherwise be re-litigated or re-broken.

## Verification and debugging

```bash
uv run pytest tests/ -q                 # fast: unit + registry, browser tests skipped
MCP_E2E=1 uv run pytest tests/ -q       # adds the real-Chrome suites
uv build                                # produce dist/*.whl + *.tar.gz
uv tool install --force .               # install this tree as the PROD MCP
uv run mcp-stealth-chrome --http        # one server for many clients (token required)
```

- A green unit suite proves nothing about the paths it skips. The E2E suites only run under
  `MCP_E2E=1`, and a message assertion inside one of them stayed wrong for a full release because
  only the fast suite was re-run after the message changed.
- The E2E suites need a module-scoped event loop. A per-test `asyncio.run()` hangs: the CDP
  connection is bound to the loop that opened it.
- Mutation-check a new guard: reintroduce the bug and confirm the test fails. Two tests in this repo
  passed against a deliberately reintroduced defect and had to be rewritten.
- After editing `src/`, reinstall (`uv tool install --force .`) and restart the MCP client — a
  registered server points at the installed package, so an un-reinstalled change means you are
  testing the old build. `server_status` reports the version actually running.
- Release order: tests → version bump in both files → docs sync → `uv build` → verify the wheel
  contents → push → `uv publish` → verify by installing the PUBLISHED package into a throwaway venv
  → reinstall PROD.
- Before publishing, confirm the built wheel actually contains the change and that
  `Requires-Dist: mcp` still carries the `<2` cap.

## Environment notes

- The PROD MCP server runs the **installed** package, not this tree: `uv tool install --force .`
  then restart the client. `server_status` reports the version actually running — check it before
  concluding a change did not work.
- Real logins often live in Brave or Edge rather than Chrome. `list_chrome_profiles()` surveys every
  Chromium-family browser; pick the profile with the largest `cookies_kb` and pass `browser=...`
  explicitly to `clone_chrome_profile`, or an almost-empty Chrome will be chosen for you.
- Cloning a running browser is supported (`allow_running=True`): each SQLite file is copied with its
  `-wal`/`-shm` companions and the copy is integrity-checked.
- `Browser.main_tab` can name a target Chrome has already replaced during startup, and connecting then fails
  with `server rejected WebSocket connection: HTTP 404`. Launch paths use `_first_live_tab`.
- `get_url` / `get_title` return "" on a failed read only for callers polling through a navigation.
  Anything reporting the value passes `strict=True`, or a failure reads as a page with no URL. A lost CDP
  connection raises in both modes.
- The PyPI token lives in the macOS Keychain, project-scoped to this package:
  `uv publish --token "$(security find-generic-password -s mcp-stealth-chrome-pypi -w)"`.
  If it is revoked, recreate it through the browser: import the PyPI session cookies, open
  `/manage/account/token/`, fill the form **inside the `select[name=token_scope]` form** (a loose
  `input[type=submit]` selector hits the SEARCH button instead), submit, then call `tab_focus()`
  before clicking "Copy token" with `mouse_click_xy`.

## Maintaining this guide

Add a rule only when it records a non-obvious invariant, prevents an expensive failure, or addresses
a repeated correction. Prefer the specific symptom over the general principle — "an empty
`console_get` with `attached: false` means nothing was monitored, not that the page was quiet" is
useful; "be careful with capture" is not. When a rule becomes specific to one module, move it next to
that module instead of growing this file.
