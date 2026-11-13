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
- `src/mcp_stealth_chrome/identity.py` — key events, client hints and languages. `humanize.py` — pointer and typing input.
- `src/mcp_stealth_chrome/key_outcome.py`: what `press_key` reports a key did, and the torn-read rule clicks share.
- `src/mcp_stealth_chrome/dock_icon.py` — Chrome Beta with the project icon for headed launches, and removal of the 0.13.0 Chrome copy. `assets/dock_icon.png` is that icon.
- `src/mcp_stealth_chrome/profile_version.py` — which Chrome may open a profile: the version guard and `beta_verdict`.
- `src/mcp_stealth_chrome/quit_watch.py` — starts the per-browser quit watcher and keeps its state files; `_quit_watcher.py` is that watcher, a standalone script.
- `src/mcp_stealth_chrome/cookie_migration.py` — moving a profile's `v10` cookies to Chrome 158's `v11` before it loses them.
  `cookie_db.py` reads a cookie database for such a decision: a private copy with its journal rolled back.
- `src/mcp_stealth_chrome/tab_record.py`: the saved tab list behind `restore_tabs`: the file, its order, masking, `/json/list` at exit.
- `src/mcp_stealth_chrome/keep_alive.py`: browsers left running at exit (`keep_alive=True`): the record, the "is it ours" check, leaving one running at exit.
- `src/mcp_stealth_chrome/_app.py` — `StealthMCP`, the FastMCP boundary. `captcha.py`, `image_output.py`, `result_types.py` — their named helpers.
- `src/mcp_stealth_chrome/tools/` — `research.py`, `devtools.py`, `vision.py`, `network_http.py`, `profiles.py`,
  `storage.py` (cookies, web storage, IndexedDB, cache), `capture.py` (request interception, HAR, dry_run),
  `page.py` (snapshot, reading, screenshot, PDF, inspection, frames, scraping),
  `navigation.py` (history, tabs, handing over to a person; `navigate` itself stays in `server.py`),
  `interaction.py` (hover, mouse, drag, keys, select, upload, click_role, wait_for; the core click and typing
  tools stay in `server.py`),
  `mouse_kit.py` (precision mouse kit and `click_turnstile`),
  `stealth.py` (identity, fingerprint, human-like input, saved sessions, `solve_captcha`, `verify_cf`),
  `turnstile_oscursor.py`; each registers itself on import.
  `server.py` is being split into these one section at a time, with no change in behaviour. Code that still
  shares helpers with `server.py` reaches them as `_srv.<name>` at call time, so tests patching `server.<name>`
  keep working; a module-level `from ..server import x` would freeze a copy.
- `tests/test_registry.py` — the tool-surface gate. `tests/test_reliability_e2e.py` and
  `tests/test_agent_ergonomics.py` — behaviour, real browser and fast unit respectively.
  `tests/test_old_tools_e2e.py` and `tests/test_slow_start_e2e.py` — tools and launch paths on real Chrome.
- `README.md` — the authoritative tool list. `docs/` — install and troubleshooting, plus the proof images the README links.
- Runtime data lives outside the repo: `~/.mcp-stealth/{profile,profiles,screenshots,exports,
  storage-states,identities,window-state,app}`.

## Hard constraints

- Never commit or print a credential. A session cookie is one: `cookie_list` masks values unless
  `reveal_values=True`, because tool output lands in the transcript, and files holding them are mode 600. The PyPI token lives in the macOS Keychain
  (`security find-generic-password -s mcp-stealth-chrome-pypi -w`), never in a file in the tree.
  Cookie and token files used during a release go to `/tmp` mode `600` and are `shred`ed after.
- Captured requests follow the same rule. `network_get`, `intercept_log`, `wait_for_request`, `auth_capture`,
  `export_curl` and `http_request` responses (headers, JSON body, final URL, pending and status payloads) go
  through `helpers.mask_*`, with `reveal_values=True` as the way out where a caller needs the value. Everything
  under a secret key is hidden, except that a `session` object keeps its non-secret fields, and `mask_url` works
  on the raw text so an ordinary parameter stays byte for byte. It also hides userinfo (`***@host`, since a token
  can be the user name; also with no scheme, `user:pass@host/path`, but not `mailto:`) and secret pairs in the
  fragment, where OAuth puts `access_token`, also after a hash route. In a URL only, `code` (OAuth authorization
  code), `SAMLResponse`, `ticket` and `assertion` are credentials too (`_URL_SECRET_NAME`); in a JSON body `code` is
  usually an error number and stays readable. `mask_headers` passes `Location`,
  `Content-Location` and `Referer` through it. `http_session_cookies`, `session_warmup` and `http_request` (its
  errors, the redirect chain and the hop that failed) print URLs through it. `export_curl` stays useful because its `http_request` form takes cookies
  from the browser. Bodies other than JSON or forms cannot be read field by field and pass through.
- Where URLs are masked, settled by the developer: masking applies to the HTTP client's output (`http_request`
  and its status, retry and errors, `http_session_cookies`, `session_warmup`, the `Location`, `Content-Location` and
  `Referer` of a response) and to captured traffic (`network_get`, `intercept_log`, `wait_for_request`,
  `auth_capture`, `export_curl`, HAR). Navigation tools return the page's URL as it is: `get_url`, `navigate` and
  every navigation result, `tab_list`, URL waits and asserts, click and key outcomes, `expect_url`, workflow
  variables and the research envelopes. Agents work with that URL (a `${u}` saved from `get_url` is opened again),
  and masking it broke that. Mask only at the output boundary: a record keeps the real value (`_Tracked.hops`).
- `nodriver` stays capped at the newest version the E2E suites have passed on (`<0.51`): `uv.lock` hides
  breakage just as it does for `mcp`, and a fresh install gets the newest allowed. 0.50 rebuilt `Connection`
  and `Tab` around sessions, so code touching them must work on both designs (`_connect_to_target`).
- From 0.50 nodriver tracks targets only on `update_targets`, so `_refresh_tabs` must call it, and `Tab.find`
  also returns elements from child frames with frame-local coordinates. Those must never reach root-tab mouse
  input: `click_text` and `click_role` refuse them and point to snapshot refs.
- `Browser.stop()` on an attached browser only closes the socket from 0.50. `_ensure_browser_dead` sends
  `Browser.close` and checks that the debugging port stopped answering.
- A CDP override belongs to its session. nodriver 0.50's `Tab.get` opened a new session per navigation, which
  dropped overrides on a cross-site navigation; `patches.py` keeps one session per tab.
- E2E tests never use the real profile (`persistent=False`, or a temporary HOME with `--use-mock-keychain`). The
  suites do not pass that flag themselves, so running them under a temporary HOME shows the developer a macOS
  "Keychain Not Found" dialog whose "Reset To Defaults" can reset the login keychain. Run them with the real HOME.
  Holding `~/.mcp-stealth/profile` pushes other live sessions onto an empty fallback profile.
- `mcp` must stay pinned `<2`. Version 2.0.0 removed `mcp.server.fastmcp` entirely, and this server
  is built on FastMCP. `uv.lock` hides the breakage from the test suite, so a green suite proves
  nothing here — the failure only appears in a lock-free install (`uv tool install`, `uvx`, and
  therefore every PyPI user). Lift the cap only together with a port off FastMCP.
- Every `@mcp.tool()` is annotated `-> str` and returns through `ok()` / `err()`. Returning a dict
  raises a pydantic error inside FastMCP.
- That annotation makes FastMCP advertise an output schema requiring `{"result": str}` on all 172 tools, and
  `ClientSession.call_tool` raises on a reply without it. A `CallToolResult` built by hand (`ImageResult`,
  `StructuredResult`) must include it. `screenshot`, `screenshot_element` and `inspect_image` failed on every default
  call for this reason, and only a real client shows it: test through `tests/_protocol.py`.
- FastMCP parses a string as JSON unless the annotation is exactly `str`, so `Optional[str]` turned `'{"a":1}'` into a
  validation error and `'null'` into None. `patches._patch_fastmcp_keeps_text` stops that, and `server_status` reports it.
- Do not add a tool that can fail silently. If an action may not have taken effect, the response says
  so — see the honest-reporting invariants below.
- Do not weaken a security property to make a test pass: managed paths never escape their root,
  identity files are written `0600`, and a profile clone only ever opens the source read-only. So is every file
  taken from a page (screenshots, `save_pdf`, `screenshot_element`, `inspect_image`, traces, heap snapshots), through
  `helpers.write_private`: a fresh sibling opened `O_EXCL|O_NOFOLLOW` at 0600, then `os.replace`. A reader sees the
  old file or the whole new one, and a symlink at the target is replaced, not written through (opening the path
  itself followed a planted link out of the folder). A writer that opens the file itself, nodriver's
  `save_screenshot`, gets a path from `private_staging(path)`, which publishes it only when it holds something;
  `cv2.imwrite` is replaced by `cv2.imencode` plus `write_private`. Not fsynced: these are artifacts.
- Anything that deletes a profile (`clone_chrome_profile(overwrite=True)`, `profile_cleanup(confirm=True)`,
  privacy cleanup, `browser_recover`) needs an explicit request. The shared profile holds the developer's real logins.
  The one exception is `profile_gc`, the pass at server start, and it is narrow on purpose. It removes only
  `privacy-profiles/<name>` and `profile-pid<number>` (exact names, directly in the data folder, no links), only
  when no lock, no running process and no session of this server holds it and nothing changed in 10 minutes, and a
  `profile-pid<N>` only when PID N has ended or now runs something that is not this server (judged by how the
  command starts, `is_server_command`: a path through a folder called mcp-stealth-chrome is not a server) and it
  holds no cookies, logins (also `Login Data For Account`), history, autofill or cards (`Web Data`), top sites,
  sessions, local storage, IndexedDB, extensions or bookmarks. Unreadable means data. A fallback that holds
  something waits for `profile_cleanup` with `confirm=True`. It never touches `profile/`, `profiles/` or `app/`.
- Every function in `profile_gc` takes the data folder as an argument; only `main()` in a production server passes
  `data_root()`, and `run_once` refuses the real `~/.mcp-stealth` while pytest runs. Tests hand a folder under
  tmp_path, and a guard is proved with a stand-in built like the real folder, never by disabling it in place:
  a mutation check that replaced the pytest refusal with `if False`, in a test that unset `MCP_STEALTH_DATA_DIR`,
  ran one real pass and removed 13 privacy profiles and 54 empty fallbacks from the developer's own folder, beyond
  recovery except from Time Machine. `tests/conftest.py` turns the pass off for every server the suite starts;
  a test that needs it passes `MCP_STEALTH_PROFILE_GC=1` together with `MCP_STEALTH_DATA_DIR`, never a temporary
  HOME (Keychain dialog).

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
- `_page_sig` reads the page and then the element in two calls, and a form submitted by Enter can replace the
  document between them: old URL, empty element half. `press_key` called that "the focused element changed" on
  macOS CI; forced locally it did in 37 of 100 presses. An empty element half over an unchanged page is no answer
  yet (`key_outcome.element_unread`, `active` left out because a blur before the submit leaves focus on `<body>`):
  the next read shows the navigation. `_sig_after_action` uses the same rule within `_VERIFY_PAUSES`, and a click
  still torn at the end says the element could not be read. `key_outcome.watch` ends at `settle_ms` plus
  `READ_LIMIT` (5 s) even if a read never answers, and calls the effect unknown; no change still waits `settle_ms`.
- An empty `_click_probe` is an unanswered question, not a pass: `_blocked_reason({})` refuses. `humanize=True`
  presses a random spot rather than the tested centre, so that spot gets its own hit test first.
- `Element.click()` in nodriver is a JS `el.click()` (`isTrusted=false`) preceded by `flash()`, which
  inserts a node into the page. Never call it. Use `_click_and_report`, which dispatches real input at
  the settled position; a JS click only happens with `js_fallback=True` and the result says so.
- Wait for the element to stop moving before a hit test. `scroll_to` defaults to smooth, and a target
  measured mid-scroll passes the test and is then clicked where it no longer is.
- Refusing is not enough on its own. When the first text match is unusable, `click_text` looks for
  another element carrying the same text that is genuinely reachable (scrolling it into view if that
  is the only problem) before giving up. On github.com the fuzzy match is a hidden marketing `<span>`;
  refusing there was truthful and still left the caller unable to click "Issues".
- `_page_sig` carries a hash of the body HTML. Comparing only its length read a counter going from 1 to 2 as
  `NO page change`, and an agent clicked again. Focus falling back to `<body>` is not a reaction: it is what clicking
  anything unfocusable does. Focus arriving on another element is.
- After a click the page is read at 0, 50, 120, 220, 330 and 450 ms (`_VERIFY_PAUSES`) and the reading stops at the
  first one that differs, plus one more 60 ms later so a handler that then navigates or opens a tab is named.
  A handler has changed the page before the click is acknowledged, so a fixed 350 ms was wasted on every click
  that worked. Only "NO page change" takes the whole wait, which is longer than before on purpose: a handler
  reacting after 300 ms sat at the edge of the old one. Do not shorten the last pause to make a dead click faster.
- The hit test, the fingerprints and the challenge look run in the frame's isolated world (`refs.apply_isolated`,
  `refs.evaluate_isolated`). In the page's own world a page can replace `getBoundingClientRect`, `elementFromPoint`,
  `getComputedStyle` or `querySelector` and watch them: one click made 37 such calls. The DOM is shared between
  worlds, so the answers are the same. Page globals are not: `window.turnstile` reads undefined there. Both
  helpers fall back to the page's world when the isolated one cannot be used, and a context kept from before a
  navigation fails once and is made again. A new probe that only reads the DOM belongs there too.
- A native dialog blocks the renderer, so input never gets its acknowledgement and a click on a button calling `alert()`
  hung forever. `_dispatch_input` stops waiting once `_watch_dialogs` sees one open and the result names it. Pointer moves go
  through it too, because `onmousemove` can open a dialog before any press. `Page.enable` needs the renderer as well, so the
  watch is armed before input, and `dialog_handle` answers a dialog already open.
- Giving up the wait leaves the gesture's task running (a press blocked on mousedown still has to release), but marks it
  stopped (`humanize.CURRENT_GESTURE`, bound in that task only): `move_pointer` and `check_gesture` then send nothing more.
  Before, the step that opened the dialog finished once it was answered and the rest of the path went out after the tool
  had returned, into whatever page was open: 7 to 11 moves reached the next page, opened its alert while another tool ran,
  and the guard in `_app` cut that tool off ("<tool> stopped"), on the macOS and Windows runners now and then. A path and
  its press are separate pieces of input, so a dialog from the path means `NOT clicked`: `humanize_click` answered
  "clicked" with its press still queued behind the dialog. Input sent while a dialog is open is dropped by Chrome.
- Anything else needing the renderer also waits for ever while a dialog is open, timeout or not. `_app._run_unless_a_dialog_blocks`
  refuses or stops such a tool and names the dialog. A new tool that does not touch the page belongs in `_RUNS_DESPITE_DIALOG`.
  A dialog only counts once `_watch_dialogs` has armed that tab, so one on a tab no tool has touched yet is not seen.
- `Transaction` in nodriver up to 0.48 is a Future, so a reply arriving after `wait_for` cancelled it raised inside the CDP
  listener and killed it: every later call on that tab hung. `patches._patch_transaction_late_reply` closes that.
- `hover`, `click_element_offset` and `click_at_corner` go through `_prepare_target` like a click. Without it they answered
  `hovered` and `clicked` at points outside the viewport and on covered elements.
- A click that opens a tab changes nothing on its own page, so `_page_sig(pages=True)` lists the browser's tabs and
  `_click_outcome` names a new one. Without it a working popup button read as dead, and an agent clicks it again.
- `fill_form` fails when a field fails and does not click submit. `batch_actions` counts failures by the `Error:`
  prefix, so a tool that reports a failure through `ok()` is counted as a success. Input that is not a list, or an
  empty list, is an error rather than `0 actions OK`, and an item that is not an object is a counted failure.
- `_click_outcome` is only reached with `verify=True`, so an empty fingerprint before or after (`_page_sig` answers
  `{}` when the page cannot be read) says `the outcome could NOT be verified`, never a bare `clicked`.
- An empty result must be distinguishable from "not measured". `console_get` returns
  `{attached, capturing, since, total, messages}`; `intercept_log` reports `active` and `stop_reason`;
  `storage_snapshot` marks each area `measured` and collects failures in `errors`, because a snapshot
  that silently failed to read cookies makes `storage_diff` blame the action under test.
- Absence of evidence is not evidence. `_ensure_browser_dead()` sets `verified` only when a process
  was positively observed to be gone; with no PID and no profile to scan it stays false and the
  caller must not claim a clean close.
- `server_status` measures `os_cursor` on a thread started with the server and answers `still being measured`
  (`available: null`) until it has a result. Importing Quartz and AppKit cold takes seconds while profile_gc and the
  Dock check run, and a timed wait answered `not measured (TimeoutError)` on the first call.
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
  drops the only handle that knew the process existed. So a close with anything in `still_alive` is an error
  and keeps the handle (`_close_incomplete`), and the privacy profile stays until the process is gone.
- `is_up()` false with a handle means a half-open Chrome: `browser_launch` ends it before launching, or the handle is
  overwritten and a privacy profile is deleted under a live process that writes it back.
- `_ensure_browser_dead` snapshots the PID's descendants before stopping it, because a launcher that does not exec is
  the recorded PID and ending it alone orphans Chrome. Only descendants whose command line names the profile count:
  Chrome also wakes `GoogleUpdater`, the machine-wide updater, which outlives the browser by a few seconds. Counted
  as family it made 4 closes in 5 say "CDP stop did not end the process" and was sent SIGTERM mid-update. The wait
  for the processes to end is a poll (`_all_gone`), not a sleep: the fixed 1.5 s made every close take 1.6 s. `verified` still means every one of them was seen gone.
  It skips a PID whose command line no longer names the profile: that number was recycled. Once death is verified it removes nodriver's `uc_*` temp profile, which nodriver otherwise
  leaves for an exit hook, one per launch, that at exit on 0.48.1 stops after the first (it prints to the closed stdout).
- Windows has no `ps`, so the descendants and command lines come from `state.process_table` there (a Toolhelp snapshot and
  `NtQueryInformationProcess(ProcessCommandLineInformation)`); parent numbers are reused there, so the walk keeps a seen
  set. Before, the family was empty on Windows. Measured on the runner: a Chrome ended with TerminateProcess leaves its
  8 to 9 helpers running for 0.2 to 2 s, holding the profile's files (`WinError 32` on `Default\Extension Rules\000003.log`
  in 8 of 8 rounds); a Chrome that quits on `Browser.close` took up to 4 s. A close that had to signal Chrome then
  deleted a privacy profile under them, gave up after about a second, and the next launch refused.
- `_remove_privacy_profile` deletes nothing while a process names the profile on its command line (`state.processes_naming`:
  Chrome hands `--user-data-dir` to every helper, and crashpad gets `--database=<profile>\Crashpad`); it waits for them.
  It asks `state.processes_holding`, which adds every browser process whose command line Windows would not give
  (it may be a helper of this profile), and treats a process list it cannot read as in use, never as empty.
  `processes_naming` itself lists only processes that name the profile: the close's family check compares against
  it, and counting another browser's unreadable helper there failed it on the Windows runner.
  Then it retries for `MCP_STEALTH_PROFILE_REMOVE_WAIT` (15 s on Windows, where an open handle blocks a delete and a
  deleted file stays until its last handle closes; elsewhere the three tries it always made). A folder that still cannot
  be removed is an error naming why and who holds it: Restart Manager on Windows (`state._win_file_holders`, the API behind
  Explorer's "the file is open in"), with the process table's command line when it gives no name (an exiting process).
- A browser this server started is asked to quit with `Browser.close` (`_request_browser_close`) and given
  `_CLOSE_WAIT` (5 s, `MCP_STEALTH_CLOSE_WAIT`) to exit, polled with `_all_gone`, before `stop()` and the signals.
  nodriver's `stop()` disconnects and sends SIGTERM, and Chrome then exits without writing its cookie store, which it
  writes about every 30 s: three cookies set and closed at once left 0 rows. With `Browser.close` they were written
  and the close took 0.75 s. A lost reply counts as asked when the socket dropped, since Chrome closes it on its way
  out; whether it really went is decided by watching the process, and `verified` keeps its meaning.
- nodriver stops probing for Chrome after about 2.3 seconds. `_start_chrome` waits for that same process and adopts it
  (`config.host`/`port` set means connect, not spawn), moving `_process` across or a close would only drop the socket.
  Only one that never answers is ended and respawned, with host and port restored first.
- A headed Chrome must not take the keyboard. Started normally it is the frontmost app within 300 ms, and giving focus back
  afterwards cannot undo keys that already went into it. So it starts with `--no-startup-window` and its first window comes
  from `Target.createTarget(newWindow, background)` (`_open_background_window`). `--disable-backgrounding-occluded-windows`
  is what keeps that window `visibilityState: visible`; without it a covered window reads hidden. `tab_new` opens its own
  background window for the same reason, because a background tab in the same window is hidden. `tab_select` does not
  activate unless `focus=True`; `navigate` defaults to `focus=False`. `tab_focus` and `request_human_help` still raise it.
- `tab_new(new_window=False)` headed opens a background tab (`createTarget(background=True)`) and turns on
  `Emulation.setFocusEmulationEnabled` for it (`_keep_painting_behind`). Measured on Chrome 157, 3 rounds each: that
  tab reads `visible` with requestAnimationFrame at full rate and Chrome is frontmost 0 ms. A bare background tab
  reads `hidden` with no frames; `Page.setWebLifecycleState(active)` does not change that. `Target.activateTarget`,
  `Page.bringToFront` and `background=False` all show the page but make Chrome the front app for about 1.6 to 2.1 s
  and hide the tab it replaced. Two costs keep it opt-in: the page reads `document.hasFocus()` true while Chrome is
  not in front, and Chrome picks the window. No CDP call names one, and it used the browser's first window in 2 of 2
  tries whichever tab was selected, since windows opened behind are never activated. So the result reads back
  the tab's window (`Browser.getWindowForTarget`) and the page's own `visibilityState`, and says "reads as hidden"
  when the emulation did not take. `tab_select` without focus says so too when the selected page reads hidden.
  The quit watcher counts page targets, not windows, so tabs sharing a window do not change what it sees.
- `window.open` activates Chrome and no flag or CDP call stops that. `_win_focus_back_from_popup` sends focus back as soon as
  this browser's PID is frontmost, about 130 ms measured, so keys typed in that moment still reach Chrome. It compares the
  PID, because a person's own Chrome has the same bundle path. `tests/test_headed_focus.py` measures all this, with
  `MCP_HEADED=1`, and counts this browser's PID so the person's other apps cannot skew it.
- The Dock icon is opt-in: only `MCP_STEALTH_DOCK_ICON=1` (or true, yes, on) turns it on. Off changes nothing: Google
  Chrome as before, nothing in the launch result, nothing written to Chrome Beta, and `dock_icon: off` in `server_status`,
  whose detail only offers the switch as working when Beta is installed.
- With it on, a headed launch on macOS runs from `/Applications/Google Chrome Beta.app`, which carries the project icon as a
  custom Finder icon (`Icon\r` plus the FinderInfo flag at the bundle root, and `org.mcp-stealth.icon` on `Icon\r` naming
  the icon's sha256). Nothing under `Contents/` is written, so `codesign --verify --deep` passes (`--strict` fails on the
  installed Chrome too, over its xattrs). Beta has its own bundle id and registers with GoogleUpdater as
  `com.google.chrome.beta` at its own path: three 60-second headed runs left the `com.google.chrome` entry unchanged.
  That is why it replaced the 0.13.0 clone of Chrome, whose runs re-registered `com.google.chrome` at the clone and let
  the updater update the clone instead of `/Applications/Google Chrome.app`. Chrome calls the updater about 20 s after it
  starts and then every few hours, so a short test run proves nothing about registration.
- `put_icon_on` only ever writes to a bundle named `Google Chrome Beta.app` that does not resolve to
  `/Applications/Google Chrome.app`. The person's Chrome is never written to.
- Beta only opens a profile it cannot damage (`profile_version.beta_verdict`): none (`persistent=False`), a privacy
  profile, a folder that does not exist or is empty, or one only Beta can open (last opened by a newer major than Google
  Chrome, no newer than Beta, and no `v10` cookie in any `*/Cookies` or `*/Network/Cookies`). Anything else, a
  `--user-data-dir` in `extra_args`, a link, an unreadable folder or cookie database, means Google Chrome and a note
  naming why. The choice (`_launch_choice`) is made before `clean_profile_state`, the window-state wipe or `mkdir`, which
  write to the folder. The reason is the Chrome 158 fact below: Beta deletes the cookies of a profile Chrome 157 used.
- An update of Beta replaces the bundle and the icon with it. `prepare` checks with a stat and an xattr read (0.012 ms)
  and never waits: a missing icon goes back on in a background thread (0.3 s through JXA) while Beta starts. The custom-icon
  flag left without `Icon\r` shows a folder in the Dock, so `prepare` clears a dangling flag first (two syscalls, 0.14 ms)
  and that launch shows Beta's own icon. An icon that cannot be set is remembered per Beta `Info.plist` and icon, and
  Beta is still used with its own icon and a note. A running app's icon follows the bundle at once.
- Headless, `attach_to_chrome` and the `solve_turnstile_oscursor` sidecar use Google Chrome. The sidecar refuses at once
  when the main browser's Chrome major differs from its own, since a clearance bound to the user agent cannot be moved.
- `dock_icon` never fails a launch or the server: any problem is a note in the launch result or in `server_status`
  (`last_note`). `status` starts nothing and creates nothing.
- The 0.13.0 copy is only removed now. `discard_copy` deletes, so it is narrow on purpose: only in the configured
  `app_dir()`, never the home folder or anything above it, never through a link, only a folder holding this module's
  `.lock`, and only four names (`Stealth Chrome.app`, its marker, `.lock`, `.building-*`). It leaves the copy while a
  process runs from it, and an unreadable process list counts as in use. It runs on a thread when any server starts and
  the folder exists, and takes the copy out of LaunchServices (`lsregister -u`) before deleting it.
- Beta has its own bundle path, but anything telling this browser from another still compares PIDs
  (`_win_focus_back_from_popup`, `_pid_recycled` by `--user-data-dir`): the person may run Beta too. Process filters in the
  headed tests match both `/Google Chrome ` and `/Google Chrome Beta `. `tests/conftest.py` sets `MCP_STEALTH_DOCK_ICON=0`
  and a temporary `MCP_STEALTH_APP_DIR` for every server the suite starts and stubs `prepare` in the pytest process;
  `MCP_TEST_DOCK_ICON=1` runs the whole suite with the switch on instead. `test_dock_icon_e2e.py` (`MCP_HEADED=1`) sets
  the icon on the real Beta, takes it off once to stand in for an update, and checks Chrome.app is unchanged.
- A launch that would open a profile with a Chrome older than the one that last opened it is refused
  (`profile_version.refusal`), before anything writes to the profile. Chrome writes its full version to `Last Version` in
  the user-data folder and to `Local State` `user_experience_metrics.stability.stats_version` at every start (measured
  for Chrome 157 and Beta 158); the newer of the two counts, and the Chrome's own version is read from its Info.plist.
  So a profile Beta made is refused to Google Chrome 157, headless too, until Chrome 158. A launch that moves a profile
  to a newer major says so once in its result.
- A stdio `browser_launch` sets `current_idle_timeout = 0`. Leaving the reaper off is not enough: any `spawn_browser`
  starts it, and it then closed the browser the person was looking at, 15 minutes after its last tool call.
- Every tool call is use of its session's current instance: `StealthMCP.call_tool` sets `current_last_active` before
  and after it, and the reaper leaves that instance alone while a call runs (`_app.calls_in_flight`). Before, only
  `active_tab()` counted, so `tab_new`, `tab_select` and `switch_instance` did not, and on Windows CI an instance with
  `idle_timeout_seconds=2` was closed during a `tab_new` while the event loop stalled (the `ps` and `update_targets`
  facts under Chrome, CDP and nodriver): 14 of 32 runs failed, `tab_new` erring or hanging past 60 s, or `tab_select`
  reading the empty list as "tab 0 out of range". A sweep that closes an instance registers it in
  `_app.INSTANCE_CLOSING`: a call on it, `switch_instance` to it and `close_instance` of it wait for the close, and the
  sweep checks after each await that its candidate is still where it found it (`_still_held`). Switched in mid-close,
  the close's `reset()` wiped whichever instance was current by then. `close_instance`, `close_all_instances` and
  `browser_close` register too, before their first await, and both sweeps skip an instance already registered: a
  stored instance could otherwise be closed twice at once. A close still running after 60 s is an error naming it
  (`wait_while_closing` returns why); going ahead let the caller act on a state the close then changed. A tool answering from the tab list goes through
  `_tab_list_problem`, which names why the browser went (`window_closed` carries the idle note) and refuses a list the
  browser did not confirm (`_refresh_tabs` returns False).
- `is_up()` false does not mean Chrome stopped: closing the last tab empties `tabs` while the process runs and
  holds the profile. Close paths branch on `BrowserState.browser is None`, never on `is_up()`.
- Chrome started with `--no-startup-window` does not exit on Quit: the quit Apple Event closes every window and
  the process stays, with its Dock icon and the profile lock (still running 10 s later; without the flag it exits in
  0.3 s). CDP cannot tell a Quit from closing every window by hand. Ending it from inside the server failed whenever
  the server was not healthy: a stdio server exits on a `/mcp` reconnect and left its Chrome with no watcher, and one
  started before `uv tool install --force` kept running old code for hours. So each headed browser gets its own
  watcher process (`quit_watch.start`, `_quit_watcher.py`): started detached (own session), from the base
  interpreter with `-I` and the source on stdin, standard library only, imports at the top, so it reads nothing
  from the installed package after it starts. It polls `/json/list` every 0.5 s; after `_WINDOWLESS_GRACE` seconds
  with no `page` target it writes the `.quit` verdict, sends `Browser.close` over the browser websocket, waits
  `_CLOSE_WAIT` for the PID, and only then signals that one PID, each time after checking its command line still
  names the same `--user-data-dir`. It exits when Chrome is gone and never acts on another process. An unanswered
  endpoint never counts as no windows. Chrome's DevTools HTTP server keeps the connection open whatever the client
  asks, so the body is read to its Content-Length: read to EOF, every poll timed out and read as unanswered.
- `tab_close` writes `<key>.tool-closed` BEFORE closing the last tab, or the watcher can read the empty browser
  first. A close that raises takes it back (`unmark_tool_closed`) unless Chrome no longer lists the tab. It counts only while the server that wrote it is the watcher's parent (`getppid`, no signal): nobody can
  use an empty browser whose server is gone. A tool close (`_ensure_browser_dead`) stops the watcher first. The
  in-process `_sweep_windowless` never closes while a watcher runs; it records `window_closed` as soon as the
  `.quit` verdict appears and cleans up the handle once the watcher has exited, and it ends the browser itself
  only when no watcher runs (failed to start, died, or not POSIX). `BrowserState.active_tab` reads the verdict
  too, so a call in the second between the watcher's close and the sweep says "last window", not "crashed".
  The state files live in `$TMPDIR/mcp-stealth-quit-watch` (0700, files 0600; `MCP_STEALTH_QUIT_WATCH_DIR`), keyed by
  Chrome PID and port, and the verdict names the profile. `tests/test_headed_quit.py` quits real ones with the
  server alive, killed, and with its venv deleted and rebuilt.
- Nothing starts a browser on its own. Every launch goes through `browser_launch`, `spawn_browser`, the cookie
  reader of `cookie_migration` (headless, closed before it returns) or `attach_to_chrome`. A Chrome seen running
  again on the same port and profile after it ended, with parent PID 1 and no CDP client, was not started by a
  server (nodriver's child would have the live server as parent).
- `clean_profile_state` and `browser_recover` leave a locked profile completely alone, Preferences included.
  The lock can belong to another instance or another session, and `browser_recover` sweeps every profile dir.
- Window placement is remembered per instance in `~/.mcp-stealth/window-state/`. Validation applies
  the saved rect and reads it BACK — Chrome clamps a window onto a connected display, so a mismatched
  read-back is itself proof the monitor is gone. No display API is queried. Implausible sizes and
  minimised/fullscreen states are refused; `wipe_window_state()` still clears Chrome's own copy,
  which is the cure for the corrupt-placement invisible-window bug.
- Profile affinity: a process stays on the profile it settled on. Without it, `browser_recover` frees
  the shared default and the next launch silently moves to a different cookie jar, so every login
  looks wiped while the data sits untouched in the old directory.

### Saved tabs (restore_tabs)

- Chrome's own session restore stays off (`wipe_window_state`, `--disable-restore-session-state`); restored window
  placement is what opened invisible windows. The server keeps its own list instead: `tab_record`, one
  `<instance>-tabs.json` per instance in the window-state folder (`MCP_STEALTH_TABS_DIR`), one entry per resolved
  profile path, so a fallback profile never replaces the list of the real one. Written 0600 through a temporary file,
  `fsync` and `os.replace`. `tests/conftest.py` points `MCP_STEALTH_TABS_DIR` at a temporary folder for every server and
  in-process launch: a test with a temporary `user_data_dir` would otherwise write fixture URLs into the real folder.
- Only a profile that outlives the close is recorded (`_tab_record_scope`): never `persistent=False` (no profile path),
  `privacy=True` or an attached browser.
- An empty list never replaces a record. A Quit closes every window before anything can read them, a failed read says
  nothing, and a browser holding only about:blank or chrome:// pages has nothing worth reopening. `forget_tabs=True` on
  `browser_close` and `close_instance`, and `profile_cleanup(remove_fallback)`, are the ways to clear one.
- Written at five moments. (1) Every close this server makes of a browser it started (`browser_close`, `close_instance`,
  `close_all_instances`, `_end_current_instance`/`_end_stored_instance` for the idle reaper and the window sweep), before
  `_ensure_browser_dead`, with a 1 s cap on the read. The sweep's branch for a browser the quit watcher already ended
  passes no reason: Chrome is gone. Only the first close of a Browser object records: a close that fails keeps the
  handle, Chrome can linger half closed for minutes, and a retried close read the windows that were left. (2) The
  background writer, about 2 s after any tool call (`_app.AFTER_TOOL_HOOKS`, a dict write measured at 2 µs) and every
  30 s for every recordable instance, since a person can navigate a visible window with no tool call. It reads twice
  0.3 s apart and writes only when both agree, and never touches a browser once a close has started (`_CLOSING_ATTR`,
  no expiry; it used to lapse after 30 s, and a failed close then had its list written over): windows closing one by
  one must not be written over the close's list. An unchanged list is not rewritten. (3) As a launch returns
  (`_record_tabs_at_launch`, one read): the quit watcher acts only once no page is left, so there is nothing to read
  then, and a Quit in the first two seconds lost the list. (4) stdin EOF, `_stop_all_browsers`: the event loop has
  closed by then, so the list comes from `/json/list` over plain HTTP with the quit watcher's reader (to
  Content-Length). (5) Nothing else: a killed server leaves what (2) last wrote, and pages a person changes in the
  last couple of seconds before a Quit are not in it.
- Order is the server's tab list (`BrowserState.tabs`, what `tab_list` shows), not Chrome's: see the target-order fact
  below. Active is the server's selected tab, not whichever tab a window happens to show.
- Restore (`_reopen_saved_tabs`) re-filters every URL (http, https, file only) because the file can be edited, opens
  each extra page as `tab_new` does (its own background window headed, a tab headless) with the instance fingerprint,
  loads them concurrently, and reports each one through `tab_record.shown_url` (`mask_url`, which covers the fragment,
  where OAuth puts `access_token`). It never fails the launch. A record older than `MCP_STEALTH_RESTORE_TABS_MAX_DAYS` (30)
  is named and not opened. The reuse path of `browser_launch` says restore was not applied.
- `tests/test_restore_tabs_e2e.py` closes through each path on real Chrome (the EOF case with the background writer
  off, so only the exit path can write) and, with `MCP_HEADED=1` (needs `--extra os-cursor` for AppKit), quits a
  visible browser and checks the restored windows took 0 ms of frontmost time.

### Kept browsers (keep_alive)

- Per launch, never a setting: many agents share the machine. `keep_alive=True` on `browser_launch` or `spawn_browser`
  leaves that browser running at stdin EOF; every other one still closes there. Left unset on a reattach it stays as it
  was (whoever kept it wanted it kept); `False` then means it closes with this server.
- Only a profile a later server can find: `persistent=False`, `privacy=True`, Windows and `MCP_STEALTH_QUIT_WATCHER=0`
  ignore it and say so. A kept browser always has a watcher that outlives the server, or nothing would end it: no
  watcher, no keep.
- The record (`keep_alive.py`, `<data>/keep-alive/<sha of real path>.json`, `MCP_STEALTH_KEEP_ALIVE_DIR`, 0600) is written
  when the browser starts (`held`), so a killed server still leaves it findable, and at exit (`kept`, with the server's tab
  order). It stores `--user-data-dir` exactly as Chrome got it: on macOS `/var/folders` and `/private/var/folders` are one
  folder, and only one is on the command line, which the watcher and the check compare. `tests/conftest.py` points the folder
  at a temporary one for every server.
- Reattach (`keep_alive.check`) needs positive evidence on every point, or the launch goes on as before and the result names
  why: the record's PID holds SingletonLock; its command line is a browser main process naming exactly that `--user-data-dir`,
  the recorded `--remote-debugging-port` and `OUR_FLAGS` (`_quit_watcher.has_flag`: a value ends at the end of the line or
  where the next flag or URL starts, since ps drops quoting); the process start time equals the recorded `pid_start`;
  `/json/version` there gives the recorded Chrome version, which the record must have; and `SystemInfo.getProcessInfo` there
  names the same PID as its `browser` process (the port could be another Chrome's). The server that wrote the record must be
  gone (`is_server_command`; unreadable counts as alive); this server's own is refused. Chrome does not write
  `DevToolsActivePort` when the port is given on the command line (measured, Chrome 157), so that file proves nothing here.
- Checking and connecting are not atomic. `check(hold=True)` takes an flock on `<hash>.lock` (released by the kernel when its
  holder dies) and `_reattach_kept` keeps it until the record names this server or is gone, so two servers that both found
  the browser free never both connect. After connecting, `_verify_connected` asks over that very connection for the browser
  PID and version and repeats the lock, command line and start-time check; any change drops the connection
  (`_NotTheKeptBrowser`) and touches nothing. Then, before any watcher is touched, the record is claimed (`server_pid`
  = this server): if that write fails, or the previous watcher cannot be ended or proved to be it, the reattach stops
  (`_ReattachAborted`) and the claim is given back. A record still naming the dead server after the lock went let a third
  server take the same browser, and a second watcher beside a live one made two closers.
- The record never holds the proxy (a proxy URL can carry a password); `write` drops the key even from an old record, and a
  reattach reads `--proxy-server` from the running Chrome's command line (`proxy_from_command`).
- `profile_gc` asks `still_running`, not "is the PID alive": a PID is handed out again. A record whose Chrome is positively gone
  (no process, a process not naming the profile, another lock holder, another start time) is removed; unreadable keeps it.
- A reattach starts nothing, so the version guard, `beta_verdict` and the cookie move do not run; the result says so and
  names a pending cookie move. It reorders `browser.targets` in place by the saved tab order (Chrome lists targets in no
  useful order, and `_refresh_tabs` copies `browser.tabs`), reapplies the recorded fingerprint to every tab (CDP overrides
  died with the old connection) and opens a `url` in a new tab, so a kept page is never navigated away from.
- At exit (`_leave_or_end_at_exit`, no event loop) a kept browser is released, not stopped: nodriver's atexit hook stops
  every registered browser and asyncio's subprocess transport kills a running child when it is closed or collected
  (`BaseSubprocessTransport.close`), so `keep_alive.release` drops it from the registry and clears `transport._proc`. A
  reattached browser has no `_process`, so nodriver's `stopped` reads True and `stop()` does nothing: one that is not kept is
  ended with `Browser.close` over a stdlib websocket and SIGTERM to its one PID (`close_at_exit`). Check `_browser_exited`,
  never `stopped`, for such a browser.
- The stdio server ignores SIGTERM and every child inherits that, Chrome and the quit watcher included. `quit_watch.stop`'s
  `terminate()` did nothing until the watcher reset SIGTERM to default. A watcher started by an older server still ignores
  it, so `end_old_watcher` follows with SIGKILL, only for a process whose command line is the watcher of that Chrome PID
  and port.
- Every signal, server or watcher, goes through an identity check right before it (`_quit_watcher.signal_if_same`,
  `server._signal_verified`): exact profile, browser main process where it applies, and the start time recorded for that
  process (`quit_watch.START_ATTR`, read at launch in `_start_chrome`, else at the start of the close). `identity` is
  same, different, gone or unknown, and unknown is never signalled: `_ensure_browser_dead` then reports the PID in
  `still_alive`, unverified, and keeps the handle. On Linux the check runs on a pidfd opened first, so the signal cannot
  reach a process that took the PID after it. Windows has no start time here; the exact command line is the identity.
  The quit watcher's state files carry the generation (`chrome-<pid>-<port>-<hash of start>`) and its verdict the start
  time, so a later Chrome on the same PID, port and profile never shares an earlier one's files or verdict, and a watcher
  that sees another start time stops ("pid reused"). Descendants count only when they name the profile with a path
  boundary (`processes_naming`), and each is checked against its own start time before a signal. A PID found only by
  the profile scan (no PID recorded) goes by the start time read at the scan, and is not signalled without one.
- Start times: Linux reads clock ticks from `/proc/<pid>/stat`; macOS has `ps lstart`, whole seconds, so a PID reused
  within the same second by a process with the same command line would pass, and its check and `kill` are a few system
  calls apart (no pidfd). Windows has no start time here and goes by the exact command line. These are known residuals,
  not guarantees; keep_alive itself is POSIX only.
- A watcher that cannot identify the browser after its last window went (command line or start time unreadable) tries
  `UNIDENTIFIED_TRIES` (3) times, then stops and leaves `not_ended` in its verdict file, which outlives it. `quit_time`
  does not read that as a Quit; `BrowserState.active_tab` reports it ("the quit watcher stopped without ending Chrome
  PID N"). Before, it retried after every grace for ever and nothing said so.
- An unreadable process list is unknown, never empty. `_win_process_table` returns None when `Process32FirstW` fails or
  `Process32NextW` stops on anything but `ERROR_NO_MORE_FILES`; `_remove_privacy_profile` waits and then refuses on
  None rather than deleting under a helper it could not see.
- `MCP_STEALTH_KEEP_ALIVE_MAX_HOURS` is positive and finite, at most a year (`MAX_HOURS_LIMIT`); anything else falls back or is
  cut and `max_hours_problem` names it in the launch result. "inf" would have quietly withdrawn the cap.
- `keep_alive` left unset reads False on a new browser and the result says so; on the browser already running,
  `browser_launch(keep_alive=...)` applies the change (`_keep_alive_on_reuse`).
- The watcher ends a kept browser on Quit (headed) and once it has had no server for `MCP_STEALTH_KEEP_ALIVE_MAX_HOURS`,
  counted from when `getppid()` stopped being its server. Headless (`pages_matter=False`) skips the window logic: nobody can
  quit it, and an empty headless browser may still be reattached. It removes the record once Chrome is gone, only if the
  record names that PID. `profile_gc` refuses a profile whose record names a live PID.
- `tests/test_keep_alive_e2e.py` proves it on real Chrome with a temporary data folder: EOF, reattach with the typed value,
  the tab list at exit, `spawn_browser`, the time cap, a forged record over a foreign Chrome, a browser a live server holds,
  and (`MCP_HEADED=1`) reattach without taking focus, then Quit with no server.

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
- A field carries its label as `label="..."` (aria-labelledby, then `<label for>` or a wrapping label, read
  without the text of the field it wraps). In `interactive_compact` and `viewport` the label's own row is left
  out and counted, but only when its field has a row in the same snapshot: a label for a hidden input, as
  styled checkboxes have, is the thing to click and keeps its row. `full` keeps every label row. On a form of
  158 elements the whole form went from 2,689 tokens to 2,188, and the default 100-element cap no longer cuts
  off the submit button. `fast` is unchanged.
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
- `http_request` follows redirects itself (`allow_redirects=False` per hop, 30 at most, then curl's error 47 as
  before). curl carried every caller header to whatever host a redirect named: `X-Api-Key` reached a third party,
  and only Cookie and Authorization were dropped, by curl's own rule. Caller headers now go only to the first
  origin (scheme, host, effective port); `headers_not_forwarded` names what stayed behind and `redirects` lists
  the hops. Browser cookies are passed on the first hop only and the session's jar carries them, host-only, which
  is what curl did: sent to the same host on another port, never to another host. 303 (not HEAD) and 301/302 after
  a POST continue as a GET without the body; only http and https Locations are followed. A failure on a later hop
  reports the chain and `failed_at` (masked), and is never `not_executed`: the first server got the request.

### Request interception

- `Fetch.enable` pauses EVERY matching request until something answers it, and a handler that fails
  to answer does not raise — it hangs the page silently. Every path through the handler, including
  the exception path, ends in exactly one `continue`/`fail`/`fulfill`.
- Interception disables itself on a deadline, and `_clear_tab_scoped_state` releases it when its
  target closes. A stale intercept would otherwise pause requests on the next browser launched.
- The deadline runs `_intercept_disable` inside the watchdog task, so it must not cancel the current task, or
  the next await raises and `active` stays true forever. Disabling removes the `RequestPaused` handler, or a
  restart answers each pause twice, except after a failed `Fetch.disable`: requests may still be pausing and
  nothing else would answer them, so `intercept_start` removes it instead.
- A failed `Fetch.disable` is not a stop. `_intercept_disable` returns the failure and keeps `active`, `tab` and the
  handler, which switches to letting every request through. `intercept_stop` is then an error and retries on the
  next call, `intercept_log` shows `disable_error`, and the deadline retries (`_DISABLE_RETRY_DELAYS`). Before, it
  answered `stopped`, and every later stop said "not active" with the handler and Fetch still live.
- `intercept_start` on another tab after such a failure is refused and keeps the old state: dropping the old
  handler left Fetch on there with nothing to answer its pauses. On the same tab the restart goes ahead, which is
  safe because nothing awaits between removing the old handler and adding the new one.

## Chrome, CDP and nodriver facts

Platform truths, not preferences. Each one cost a release.

- Chrome 158 on macOS encrypts with a different key in any profile outside its default user-data folder, which is every
  profile this server opens. With `KeychainKeyProviderUserDataDirProtection` (new in 158, `ENABLED_BY_DEFAULT`, Chromium
  CL 8603400, "Use alternate key for non-default user data dirs"), `browser_process_impl.cc` registers the old `v10` key
  as unavailable and writes `v11` (same "Chrome Safe Storage" Keychain item, salt "pepper"). Measured on Beta 158: it
  reads none of the `v10` cookies Chrome 157 wrote and deletes them, 1172 to 0 rows on a copy of a real profile. With
  `--disable-features=KeychainKeyProviderUserDataDirProtection` (the CL's kill switch) it read `v10` and kept it. Google
  Chrome 157 reads only `v10`. The Keychain ACL (which lists `com.google.Chrome.beta`, no dialog) is not the problem.
  The framework of 157.0.8103.39 holds no string naming the feature and 158.0.8127.18 holds two, so `applies` decides
  by the major version from Info.plist (free) rather than reading the framework (1 to 2.4 s). Stable 158 was due
  17 Nov 2026, with the feature on at the tip of its branch and no field trial for it.
- Moving cookies to `v11` (`cookie_migration`, `_prepare_cookie_move`, `_commit_cookie_move`, `_finish_cookie_move`):
  when Chrome 158+ on macOS is about to open a profile outside Chrome's default folders whose launched sub-profile
  holds a `v10` cookie. Preparing writes nothing in the profile and never opens it: its cookie databases and journals
  are copied to `storage-states/pre-v11-migration-*-db` (source unchanged across the copy, `integrity_check` on the
  copy, every file and folder fsynced), a headless Chrome with the flag (`_cookie_flag_arg`, built by
  `_disable_features_arg` so nodriver's list survives) reads every cookie from a temporary user-data folder holding
  only that copy's `v10` rows (measured: 5 of 5 read, the Keychain key does not depend on the folder), the backup is
  written durably (`write_durable`: O_EXCL and O_NOFOLLOW temporary file, fsync, rename, folder fsync) and read back,
  and the profile's databases must still match the decision's fingerprint. Then `_cookie_preflight` (read only), and
  only then `_commit_cookie_move` writes: the marker (`.mcp-stealth-v11-migration.json`, `pending`, `version` 2, with
  the profile folder's real path, the sub-profile, the backup's sha256 and count, durably), and beside `v11` rows the
  `v10` rows go (`strip`: every database checked first, then each in a `BEGIN IMMEDIATE` transaction that checks
  again that every row it deletes is in the backup by key and is the very row of the database copy, by a digest of
  every column but `last_access_utc`). Only after
  that do `clean_profile_state`, the window-state wipe and mkdir run, and Chrome starts without the flag, sets the
  cookies with `Storage.setCookies` and compares keys and sha256 digests of value, flags and partition, and expiry
  (within a second, or capped by Chrome's 400 days); all equal marks it `done`, anything else is an error naming the
  keys, never a value, and stays `pending`.
- The order is the guarantee, proved by `test_a_crash_at_any_step_of_a_mixed_move_loses_no_cookie` (a `BaseException`
  thrown at ten points, then the next launch must end with every cookie) and a real-Chrome kill after the marker. A
  crash before the marker leaves the profile as it was; from the marker on, the next launch resumes: the backup must
  match the marker's digest and count and hold every loaded `v10` row still in the profile by name, host, path and
  partition (counted, so two partitioned cookies of one name are two), or the move is made again, carrying over what
  only the pending backup holds (rows a half-done strip already took out). A pending marker is used only when it is
  version 2, names this profile folder and the sub-profile being launched (a move of Default resumed for Profile 1
  set Default's cookies there); one for another sub-profile refuses until that one is launched, and one from v0.16.1
  or earlier (no digest, count or binding) refuses. A pending marker whose backup is missing or not the recorded
  file refuses, never a new move: part of the profile may be stripped already. Each such refusal names the database
  copy and whether it passes `integrity_check`, with the steps to put it back. A failure while preparing means the flag when the profile
  holds no `v11` cookie (nothing was touched, so the launch's own check passes), and a refusal when it does. `v10`
  again after a `done` by the same major means this Chrome writes `v10` there (the kill switch was turned on
  remotely), so the flag is used and nothing moves, instead of moving on every launch. Beta never opens a profile with
  data (`beta_verdict`), so it never migrates; tests use Beta 158 or newer as Chrome 158 through a `chrome` script on
  the server's PATH, shorter than Google Chrome's path because nodriver takes the shortest candidate.
- A refusal writes nothing in the profile: `clean_profile_state`, the window-state wipe and mkdir wait until the cookie
  checks and the commit have passed. A sub-profile or `Network` folder that is a link, or a marker that is one, is
  never read through or written through (`candidate_databases`, `all_databases`, `read_marker`).
- A backup holds every cookie value in plain text. `cookie_migration.expire_backups` removes a backup and its `-db` copy
  once the profile's marker says `done` (every cookie read back) and `finished_at` is older than
  `MCP_STEALTH_MIGRATION_BACKUP_DAYS` (14), at server start on a thread and after each finished move; anything else
  stays, and what stays is made 0600. Under a test it refuses the person's own folder, and `tests/conftest.py` points
  `server.STORAGE_STATE_DIR` at a temporary folder for every in-process test: a fake Chrome 158 in
  `test_profile_version` once left six database copies in the real `~/.mcp-stealth/storage-states`.
- The flag is not a safe default. Measured on Beta 159 with the real Keychain: three `v11` cookies, opened with the flag,
  read 0 and left 0 rows. A store holding both kinds is worse: 5 `v10` and 2 `v11` cookies of one host read 0 and left 0
  rows with the flag and without it, while the `v10` rows alone read 5 with the flag and the `v11` rows alone 2 without.
  So the flag is used only for a profile read to hold no `v11` (`Plan.v11`), a store holding both is split (the
  reading Chrome gets a copy with the `v10` rows alone, the launch the profile with the `v11` rows alone once every
  `v10` cookie is in the backup), and a database that cannot be read with certainty refuses the launch (`refuse`):
  nothing starts and nothing is written. The refusal says what to do: close every Chrome on the profile and launch
  again, clone_chrome_profile to work meanwhile, restore a damaged database from a backup; persistent=False does not
  keep these logins.
- Chrome keeps cookies in SQLite with a rollback journal, not a WAL (Chrome 157 and Beta 159: header says rollback,
  `Cookies-journal` beside the file). Beta 159 killed 3 s after setting cookies left a hot journal of 12,824 bytes, and
  Beta 159 moved `Default/Cookies` to `Default/Network/Cookies` between two starts. `mode=ro&immutable=1` reads the file
  as it lies: a killed transaction that deleted 40 `v10` rows read as 0 rows, and a row only in a `-wal` was not seen.
  `mode=ro` refuses a hot journal (rolling it back writes). So every decision reads through `cookie_db.read`: the
  database with its `-journal` and `-wal` copied to a private 0700 folder, opened normally so SQLite rolls back or
  replays there, `integrity_check` ok, and each file's size, times and inode unchanged across the copy.
- A decision holds only until something else touches the profile. `Plan.fingerprint` records the launched
  sub-profile's cookie databases and companions (`cookie_db.fingerprint`, absent files included; another sub-profile
  changing does not matter to this launch), and `_cookie_preflight` runs right before the commit: a Chrome holding the
  lock or any change refuses the launch, since neither the flag nor its absence is then known to be safe. Nothing of
  the server's own writes the profile before it. A row written between the preflight and the strip stops the strip
  (its transaction checks). The lock is read three ways (`state.chrome_lock_state`): a `SingletonLock` that names no
  readable PID is unknown and refuses, never free. After the commit (the fingerprint re-taken after its own strip)
  and `clean_profile_state`, `_cookie_preflight` runs once more right before `_start_chrome`. What remains is the
  known limit: the time between that look and Chrome taking the lock, about half a second, since no lock Chrome
  honours can be held across it.
- A move that did not finish after Chrome started (`_finish_cookie_move` false, also when comparing or writing the
  done marker raises) closes that browser (`_close_after_cookie_failure`) and `spawn_browser` does not register it:
  left running, the next launch reused it and never resumed the move. At server start, besides expired backups,
  `expire_backups` removes a backup nothing refers to (no marker in its profile, or a done marker naming a later
  backup) once older than the retention; while a profile's move is pending none of its backups or database copies
  goes, since a retry carries cookies over from the backup before and a damaged newest one leaves the older as the
  only copy. `remove_reader_leftovers` removes `mcp-stealth-reader-*` folders a killed server left in the temporary folder
  (older than an hour, named by no process; unknown keeps them).
- Chrome lists page targets in neither tab nor creation order. Measured on Chrome 157, four tabs opened A, B, C, D (C
  then activated): `Target.getTargets` gave C, B, D, A and `/json/list` C, D, B, A (it sorts by last activity), while
  nodriver's `browser.tabs` kept A, B, C, D. Anything that must keep tab order orders by the server's own list.
  A page that failed to load reads `chrome-error://chromewebdata/` as `location.href`, but its `TargetInfo.url` keeps
  the URL that failed, so a saved tab list keeps it.
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

- `Element.select_option()` takes no argument and selects the `<option>` it is called on, so calling it on a `<select>`
  with a value raises. `select_option` shipped that way from 0.1.0. CDP input also wants `MouseButton`, not a string.
- `send_keys` types character by character and a newline typed that way adds nothing. `fill` presses Enter between
  lines, only in a textarea or contenteditable, because in a single-line field Enter submits the form.
- `tab.evaluate` returns `ExceptionDetails` when the script throws, rather than raising. Anything reporting the
  result checks for it, or the error reads as the value.
- `tab.evaluate(expr, return_by_value=True)` can STILL return a `RemoteObject`. Unwrap via
  `v.value if hasattr(v, "value") ...` before casting. It also always asks for deep serialization, which leaves
  `value` empty for any object or array, so the `evaluate` tool calls `Runtime.evaluate` itself. `_eval_checked` is the
  helper for a script whose exception must not read as its result.
- `Tab.wait(t)` is only `sleep(t)`. `wait_for_navigation` slept its whole timeout and said `navigated`; use `_wait_load_complete`.
- `Tab.get` up to 0.48 ends in `await self`, the same half-second sleep, on every navigation. From 0.50 our
  patch waited up to half a second for the target's URL to change, so reopening the URL a tab is already on
  always paid it. `patches._patch_tab_get_keeps_session` now replaces `Tab.get` on both: `Page.navigate` answers
  once the navigation has committed, and a URL that already matches is not waited on. The class refuses plain
  assignment up to 0.48 (`type.__setattr__`), and `Tab.target` is read-only there (`_target`).
- `auto_verify` looks once after load (`_cf_first_look`) and returns at once on "none". It keeps the old 0.6 s
  second look only when something points at a challenge still on its way: the page is not `complete`, an inline
  script or a `link` names Turnstile, or a request to `challenges.cloudflare.com` was already made. A widget
  injected after load by a bundled script that leaves none of those traces is no longer waited for; the old
  wait only covered the first 0.6 s of that case, and cost every navigation 0.6 s.
- `Input.dispatchKeyEvent` with `nativeVirtualKeyCode` on a key the page does not consume holds every later mouse event for
  5 seconds on macOS, until the next navigation. `key_event_params` leaves it out. Raw CDP reproduction with one Chrome per
  scenario showed this; a shared Chrome hid it, because the stall persists across `reload`.
- `Element.send_keys` sends only `char` events: a keypress with an empty `key` and no keydown, which no keyboard produces.
  `_type_keys` presses real keys. macOS select-all also needs `commands=["selectAll"]`.
- `Tab.verify_cf` clicks the best template match however poor, so on a page with no widget it clicked something and
  reported success. `_cf_template_click` applies a threshold. A challenge click reports what `_cf_after_click` observes.
- A Turnstile checkbox vanishing after the click is not a pass. It goes while the page submits (`?__cf_chl_tk=...`),
  before verification and the redirect, so `solve_turnstile_oscursor` keeps its fixed 8 s of scoring. Shortening it
  needs a pass signal that holds on any site, not the look of one demo page.
- `solve_turnstile_oscursor` runs about 16 to 20 s standalone and 45 to 50 s inside the MCP server, where its native
  steps run slower. 35 s timed out during scoring, hence the default of 50.
- Chrome honours only the last `--disable-features`, and nodriver puts its own (`IsolateOrigins,site-per-process`)
  before ours, so a second one silently drops nodriver's list: `testing_mode` ran with site isolation back on.
  Build one flag with `_disable_features_arg`. Every launch adds `--disable-default-apps`: a new profile otherwise
  installs Google Docs Offline and Web Store Payments, two more renderers and about 0.2 GB, and `persistent=False`
  pays that on every launch. `--disable-background-networking` saved the same but also stops Safe Browsing and
  variations updates, so it stays in light mode only.
- Chrome 157 on macOS lists 199 speech voices: 180 local ones and 19 "Google ..." ones from its Network Speech
  component extension, headless or not, and the same in a Chrome with no CDP client at all. They arrive in two
  steps, so a list read at the first `voiceschanged` can hold only the 19; read it after it settles.
  `--disable-component-extensions-with-background-pages` saves about 0.27 GB but leaves the 180 local voices
  and no Google one, which pages can read, so it is light mode only and `fingerprint_audit` warns on it. No
  `--disable-features` name was found that turns off Gemini or Hangouts alone.
- Headless Chrome reports an 800x600 screen whatever `--window-size` says, so the default 1280x800 window was
  larger than its own screen, which no machine shows. `_headless_screen_arg` passes `--screen-info` with the
  main display's size and work area (`NSScreen` on macOS), or the smallest common size that holds the window.
  It sets nothing else: `devicePixelRatio` stays 1 and `colorDepth` 24, as headless reports them. Headed is
  untouched, and a caller's own `--screen-info` wins. `fingerprint_audit` warns when a window is larger than its
  screen.
- The first document after startup loads images despite `--blink-settings=imagesEnabled=false`; later ones obey it. Light
  and testing modes go through `about:blank` first.
- nodriver 0.50 does not pass `Config.lang` on, its `add_argument` refuses `--lang`, and macOS Chrome ignores the flag
  anyway. A `lang` other than en-US goes through the same Accept-Language override as a fingerprint.
- `Network.deleteCookies` answers success whether or not anything matched, and matches the exact stored domain, path and
  partition. `cookie_delete` reads what exists, deletes those, and reads back.
- libcurl's cookie engine drops a cookie for a host with no dot, so `localhost` reported `cookies_sent` that never left.
  Such hosts get a `Cookie` header. libcurl also speaks `file://`, which `http_request` refuses.
- `os.kill(pid, 0)` is no liveness probe on Windows: 0 is `CTRL_C_EVENT`, sent through `GenerateConsoleCtrlEvent`, and
  `signal.SIGKILL` does not exist. Every close there reported Chrome still running until `_process_exists` asked
  `OpenProcess`/`GetExitCodeProcess` (`state._win_pid_alive`). Probe through `_process_exists`, never `os.kill(pid, 0)`.
- Nothing on the event loop may shell out to `ps` on Windows. A person's machine has none, and the runner's is Git
  Bash's: `_process_command` ran it at every close and blocked the loop 1.1 s up to its 3 s timeout, measured with a
  thread dumping the main thread's stack whenever a 0.1 s heartbeat stopped. Command lines there come from
  `state.process_table()`.
- nodriver 0.48 starts `update_targets()` as tasks nobody awaits, on every target event and in `Browser.wait` and
  `Tab.sleep`. On a closing browser each one fails, asyncio logs "Task exception was never retrieved", and FastMCP's
  Rich handler renders the traceback on the event loop: 1.1 to 1.3 s each on the Windows runner, five per close.
  `patches._patch_background_target_refresh` makes those tasks end quietly; an awaited `update_targets` still raises.
  The idle reaper read these stalls as idleness and closed an instance spawned a moment before.
- A module-level import of a POSIX-only module (`fcntl`) stopped the server from starting on Windows from 0.12.12 to
  0.15.0, and nothing noticed because no test ran there. CI's Windows jobs are the guard; `test_smoke` imports the
  server with `fcntl` hidden.
- Windows keeps no POSIX mode bits: files read back as `0o666` whatever was asked. Tests of `0600` skip there, and
  `http_transport.load_or_create_token` refuses its own token file from the second `--http` start (open).
- Windows CI (Google Chrome 156, system install): a profile outside the default user-data folder gets none of its
  cookies back on the next launch. They are written (`v10`, DPAPI key intact in `Local State`) and not deleted, and
  nodriver alone does the same, so it is Chrome's behaviour there; App-Bound reports `SupportLevel` 4 (not the default
  folder). Open. On Linux and macOS the same round trip passes (`test_cookie_close_e2e.py`).
- On Windows CI the first screenshot a server process takes (`screenshot`, or `click_turnstile`'s template match) does
  not answer within 60 s (150 s in a probe); every later one takes 1.2 s. Not the `cv2` import. Open; an `xfail` in
  `test_protocol_e2e.py` records it.
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
  removes or modifies a tool: the count in the opening line, the Tools table, new parameters.
  Bump the version in BOTH `pyproject.toml` and `src/mcp_stealth_chrome/__init__.py`, and update
  `tests/test_registry.py` (`EXPECTED_COUNT` and the name list).
- The PyPI `description` in `pyproject.toml` carries its own tool count. It has drifted twice.
- `tests/test_registry.py` fails when a registered tool is missing from the README's Tools table.
- Write docs in plain language: no star ratings or emoji in headings, no em dashes, no promotional claims. Check marks in lists and result tables are welcome. A result names what was measured. Name no specific AI model in docs or tool descriptions, since they age: write `your-vision-model`.
- Record a decision here when it is non-obvious and would otherwise be re-litigated or re-broken.

## Verification and debugging

```bash
uv run pytest tests/ -q                 # fast: unit + registry, browser tests skipped
MCP_E2E=1 uv run pytest tests/ -q       # adds the real-Chrome suites
uv build                                # produce dist/*.whl + *.tar.gz
uv tool install --force .               # install this tree as the PROD MCP
uv run mcp-stealth-chrome --http        # one server for many clients (token required)
```

- `.github/workflows/ci.yml` runs the fast suite on macOS, Ubuntu and Windows (Python 3.11 and 3.13), a lock-free
  install of the built wheel that checks the `mcp<2` and `nodriver<0.51` caps and calls `server_status`, and the
  headless real-Chrome suites on all three. A test that cannot hold on one OS skips there with a reason naming the code.
- `tests/test_protocol.py` and `tests/test_protocol_e2e.py` drive the server over stdio with `session.call_tool`, so
  FastMCP's argument parsing and the client's output validation run. `MCP_TEST_SERVER_PYTHON` and `MCP_TEST_SRC` run
  them against another interpreter or source tree, which is how to cover both nodriver and mcp versions.
- A green unit suite proves nothing about the paths it skips. The E2E suites only run under
  `MCP_E2E=1`, and a message assertion inside one of them stayed wrong for a full release because
  only the fast suite was re-run after the message changed.
- `test_registry.py` proves a tool exists, not that it works, and a fake proves only the call shape. `select_option`
  and `drag_and_drop` failed on every call for the project's whole life with all of that green. Exercise a tool on
  real Chrome (`tests/test_old_tools_e2e.py`) before calling it covered.
- The E2E suites need a module-scoped event loop. A per-test `asyncio.run()` hangs: the CDP
  connection is bound to the loop that opened it.
- Moving code out of `server.py` silently disarms a test's `monkeypatch.setattr(server, X)` or `server.X = ...`: the moved
  code reads its own import of `X` (`srv.B64_INLINE_LIMIT = 500` stopped reaching `screenshot` this way). A fixture patching `server.STORAGE_STATE_DIR` let the `cookie_export` tests write into the real
  `~/.mcp-stealth/storage-states` after the cookie tools moved. Before each move, grep the tests for every module-level
  name the moved code reads and point those patches at the new module, then check the real folders stayed untouched.
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
