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
- `src/mcp_stealth_chrome/brave_beta.py`: a headed Brave from Brave Beta, with Beta's own icon (`MCP_STEALTH_DOCK_ICON=1`). `launcher.py`: `mcp-stealth-chrome open` and the Dock launcher app (`install-launcher`).
- `src/mcp_stealth_chrome/brave.py`: `browser="brave"`: finding Brave, its profiles, the profile binding, vertical tabs.
- `src/mcp_stealth_chrome/tab_record.py`: the saved tab list behind `restore_tabs`: the file, its order, masking, `/json/list` at exit.
- `src/mcp_stealth_chrome/keep_alive.py`: browsers left running at exit (`keep_alive=True`): the record, the "is it ours" check, leaving one running at exit.
- `src/mcp_stealth_chrome/adblock.py`: `block_ads`: the filter lists, the stored engine, and the blocker's own CDP connection.
- `src/mcp_stealth_chrome/presence.py`: `agent_overlay`, the server's side: when it starts, the refusal while the person holds
  the browser, the pause for their typing. `_agent_overlay.py` is the overlay process, a standalone script (PyObjC).
- `src/mcp_stealth_chrome/assets/claude-skill/SKILL.md`: the Claude Code skill (`/stealth-chrome`); `skill_install.py` installs it
  (`mcp-stealth-chrome install-skill`). `tests/test_skill_install.py` fails when it names a tool that is not registered,
  names an AI model, or carries a key: the vision provider is the user's own (BYOK). Update it when a tool it names changes.
  When SKILL.md changes, add the outgoing version's sha256 to `skill_install.SHIPPED`: a copy matching an earlier release
  was never edited and is replaced without `--force`; a test lists every tagged release whose skill is missing there.
  hCaptcha is handed to the person there (`request_human_help`): its terms forbid bots and AI that bypass its challenges.
- `src/mcp_stealth_chrome/settings.py`: the person's settings (`~/.mcp-stealth/settings.json`, the environment over it) and
  `mcp-stealth-chrome settings`.
- `src/mcp_stealth_chrome/_app.py` — `StealthMCP`, the FastMCP boundary. `captcha.py`, `image_output.py`, `result_types.py` — their named helpers.
- `src/mcp_stealth_chrome/tools/` — one module per group of tools, each registering itself on import:
  `research.py`, `devtools.py`, `vision.py`, `network_http.py`, `profiles.py`,
  `storage.py` (cookies, web storage, IndexedDB, cache), `capture.py` (request interception, HAR, dry_run),
  `page.py` (snapshot, reading, screenshot, PDF, inspection, frames, scraping),
  `navigation.py` (history, tabs, handing over to a person),
  `interaction.py` (hover, mouse, drag, keys, select, upload, click_role, wait_for),
  `mouse_kit.py` (precision mouse kit and `click_turnstile`),
  `stealth.py` (identity, fingerprint, human-like input, saved sessions, `solve_captcha`, `verify_cf`),
  `turnstile_oscursor.py`, `scripting.py` (`evaluate`, init scripts, `wait_for_function`),
  `batch.py` (`batch_actions`, `fill_form`, `navigate_and_snapshot`), `viewport.py` (viewport size, scroll),
  `dialogs.py` (dialogs, `accessibility_snapshot`), `monitoring.py` (console and network capture),
  `diagnostics.py` (`server_status`, `get_page_errors`, `export_curl`, `fingerprint_audit`),
  `chrome_profiles.py` (`list_chrome_profiles`, `clone_chrome_profile`),
  `action_kit.py` (`describe_page`, `smart_fill`, the assert and storage-snapshot tools, `workflow_run`,
  `wait_for_request`, `auth_capture`), `instances.py` (`spawn_browser`, the instance tools and
  `_launch_browser_instance`), `attach.py` (`attach_to_chrome`, `detach`, `list_external_chrome`).
- `server.py` keeps nine tools (`browser_launch`, `browser_close`, `browser_recover`, `navigate`, `click`,
  `click_text`, `fill`, `type_text`, `check`) and what the modules share: the launch phases (`_launch_*`),
  closing a browser, the click path, focus, per-tab state. How a module uses it:
  - It reaches a `server.py` name as `_srv.<name>` at call time (`from .. import server as _srv`), so tests
    patching `server.<name>` keep working. A module-level `from ..server import x` freezes a copy.
  - A name tests patch on `server` (`PROFILES_ROOT`, `PROFILE_DIR`, `ensure_dirs`, the profile lock lookups) is
    read as `_srv.<name>` too, even though `server.py` only imports it. With its own import a module no longer
    sees the patch: the tests that keep it away from the real `~/.mcp-stealth` still pass while it acts there.
    `tests/test_server_patches.py` fails on a new case.
  - `server.py` never imports a name back from a tools module to share it (that made an import cycle that only
    showed when the tools module was imported first). A name both sides use stays in `server.py`.
  - The order tools register in is not a contract; nothing may depend on it.
- `tests/test_registry.py` — the tool-surface gate. `tests/test_reliability_e2e.py` and
  `tests/test_agent_ergonomics.py` — behaviour, real browser and fast unit respectively.
  `tests/test_old_tools_e2e.py` and `tests/test_slow_start_e2e.py` — tools and launch paths on real Chrome.
- `README.md` — the authoritative tool list. `docs/` — install and troubleshooting, plus the proof images the README links.
- `docs/agents/` — the invariants of each area, one file per topic: see "Topic guides" below. Part of this guide.
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
- A rule that kept "harmless" values readable under a secret parent (by name and shape) was tried and reverted:
  a review read `"ABCDEF"` under `name`, a passphrase under `label`, 4-digit codes under `count`, `birthDate` and
  `cardholderName` through it. The default stays everything under a secret key. The way out is explicit:
  `reveal_values="body"` on `network_get`, `intercept_log`, `wait_for_request`, `auth_capture` and `export_curl`
  (`helpers.reveal_mode`, `mask_body(own_keys_only=True)`): credential headers and URL secrets stay masked, a body
  value whose own key is secret-named stays masked at any depth, and values under a secret-named parent show. The
  reply names the mode and says those values were revealed at the caller's request (`BODY_REVEAL_NOTE`).
  `Union[bool, Literal["body"]]` reaches the tool as given over a real client on mcp 1.21.1, 1.27 and 1.30: `true`,
  `false`, `"body"`; `"BODY"` and `null` are validation errors, and `"yes"`, `"true"` and `1` read as True
  (pydantic's lax bool, as for every bool argument).
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
- FastMCP's argument model ignores a name it does not know, on every tool: `screenshot(path="x.jpg", quality=45)`
  saved `shot-<ts>.png` and said nothing. `StealthMCP.call_tool` refuses unknown names before the tool runs
  (`_app.unknown_arguments_error`, from the advertised schema, 0.14 µs a call), naming them and the valid ones.
  `batch_actions` and `workflow_run` call the functions directly, so each step goes through the same check
  (`_inner_arguments_error`) and a refused step is a counted failure with the same message. The six legacy batch
  names (`click`, `fill`, `type`, `press`, `wait`, `navigate`) keep their defaults and pass every other key on:
  `wait` dropped `frame_url` and `navigate` dropped `expect_url` and `expect_status`.
- What a client lists is not the tool's own schema and text: `StealthMCP.list_tools` leaves out pydantic's made-up
  titles (`_app.without_titles`; "Js Fallback" for `js_fallback`), which were 30% of the input schemas a client hands
  to its model (15.9 of 53.4 KB), and the docstring's indentation (`inspect.cleandoc`; 9% of the description text).
  `tool.parameters` and `tool.description` keep them. Say what a parameter means in the docstring: a title or a
  `Field(description=...)` is not where this project documents one, and nothing reads it.
- Do not add a tool that can fail silently. If an action may not have taken effect, the response says
  so: see `docs/agents/honest-reporting.md`.
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
  `privacy-profiles/<name>` and `profile-pid<number>` or Brave's `brave-profile-pid<number>` (exact names, directly
  in the data folder, no links, the same checks for both), only
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

## Topic guides: read before you edit

The invariants of each area live in `docs/agents/`, one file per topic. They are part of this guide and
bind like the rest of it: almost every line replaced a shipped bug or a wrong claim. Before you change
code in the left column, read the whole file on the right, and when you learn something new about that
area, write it there. This file stays short on purpose: it is loaded into every agent session, and at
176 KB it crowded out the work. A rule you did not read is a rule you will break, so read first.

| Before you change | Read |
|---|---|
| any tool's result text; clicks, hit tests and verification (`server.py`'s click path, `key_outcome.py`, `tools/interaction.py`, `tools/mouse_kit.py`); dialogs; what a status tool reports | [`docs/agents/honest-reporting.md`](docs/agents/honest-reporting.md) |
| `state.py` (`BrowserState`, sessions, instances), console and network capture buffers, `http_transport.py`, per-tab caches | [`docs/agents/state-instances-capture.md`](docs/agents/state-instances-capture.md) |
| launching and closing a browser (`browser_launch`, `spawn_browser`, `_ensure_browser_dead`), headed focus and windows, `quit_watch.py`, `_quit_watcher.py`, `dock_icon.py`, `profile_version.py`, window placement, the idle reaper | [`docs/agents/browser-lifecycle.md`](docs/agents/browser-lifecycle.md) |
| `brave.py`, anything behind `browser="brave"` | [`docs/agents/brave.md`](docs/agents/brave.md) |
| `brave_beta.py`, `launcher.py`, the Dock launcher app | [`docs/agents/brave-beta-launcher.md`](docs/agents/brave-beta-launcher.md) |
| `brave_lean.py` | [`docs/agents/brave-lean.md`](docs/agents/brave-lean.md) |
| `tab_record.py`, `restore_tabs`, the tab list written at close | [`docs/agents/saved-tabs.md`](docs/agents/saved-tabs.md) |
| `keep_alive.py`, reattaching, taking over an orphan, every signal sent to a process, the fallback-profile warning | [`docs/agents/keep-alive.md`](docs/agents/keep-alive.md) |
| which tabs a tool may act on (`owned_targets`, `helpers.select_tab`, `TabNotUsable`), the launcher's browser, `--remote-allow-origins` | [`docs/agents/sharing-a-browser.md`](docs/agents/sharing-a-browser.md) |
| `refs.py`, `snapshot.py`, frames and shadow roots, `fill`'s read-back, managed output paths | [`docs/agents/refs-snapshots-paths.md`](docs/agents/refs-snapshots-paths.md) |
| `tools/network_http.py`, `tools/research.py`, `export_curl` | [`docs/agents/http-path.md`](docs/agents/http-path.md) |
| `adblock.py`, `block_ads` | [`docs/agents/adblock.md`](docs/agents/adblock.md) |
| `settings.py`, any new personal switch | [`docs/agents/settings.md`](docs/agents/settings.md) |
| `presence.py`, `_agent_overlay.py` | [`docs/agents/agent-overlay.md`](docs/agents/agent-overlay.md) |
| `intercept_start`/`intercept_stop` in `tools/capture.py` | [`docs/agents/request-interception.md`](docs/agents/request-interception.md) |
| anything that talks CDP or nodriver or depends on how Chrome behaves: `patches.py`, `identity.py`, `cookie_migration.py`, `cookie_db.py`, key and mouse input, `evaluate` | [`docs/agents/chrome-cdp-nodriver-facts.md`](docs/agents/chrome-cdp-nodriver-facts.md) |

### Honest reporting, in one paragraph

This is the spine of the project. A tool that reports a rosier state than reality is the worst
failure mode for an agent: the caller cannot tell, so a wrong "success" spreads into wrong
conclusions. Each rule below replaced a real lie.

In short: never return success for an action that may not have taken effect; an empty result must be
told apart from "not measured"; absence of evidence is not evidence. The rules behind this are in
[`docs/agents/honest-reporting.md`](docs/agents/honest-reporting.md).

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
  The `lockfree` job installs the wheel with no uv.lock (the newest every range allows, as uvx gets), prints what
  differs from uv.lock (`.github/scripts/lock_drift.py`) and runs the fast suite and the HTTP, cookie and close
  suites there. Only that job shows a dependency that changed behaviour without breaking an import (curl_cffi 0.17).
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
- Every Chrome a test run starts carries `--mcp-stealth-test-run=<token>` (`run_marker`; `_start_chrome` adds it
  while `MCP_STEALTH_TEST_RUN` is set, and a test starting Chrome itself adds `run_mark()`). `tests/conftest.py`
  ends what a test orphaned (its starter gone) and what a module left (MCP_E2E runs), and at the end of every run
  fails it naming each browser it had to end. Signals go by PID with the flag and start time checked right
  before; never by application name, since the person's browsers and other runs' run beside. Unknown is never
  gone: a process list, command line or start time that cannot be read fails the run naming what, and a process
  whose identity is not proved is never signalled (each layer, `end` and `_signal`, refuses it on its own). Why it matters: a
  Chrome started in-process outlives its event loop, because asyncio's subprocess transport raises "Event loop is
  closed" in `close()` before it reaches `kill()`, and at pytest's exit it is reparented to PID 1. One stayed 8
  hours in the person's Dock.
- A probe that posts synthetic clicks or keys (CGEventPost) acts on the person's screen. Before each click it
  hit-tests the point (the topmost window there must belong to the process under test, past the Dock's full-screen
  layer and the overlay's light strips) and refuses otherwise; keys go out only when the focus is known to be in the
  test's own process, and never Enter. A probe without that check sent 32 clicks at fixed coordinates into the
  person's own app, because the test browser opens behind it (21 Nov 2026). The Dock owns a full-screen window at
  layer 20 that a hit test has to pass over, so a click is also kept inside `NSScreen.visibleFrame`: three clicks
  low on the screen went to the Dock's band. Read the app in front with `lsappinfo front`:
  `NSWorkspace.frontmostApplication()` keeps its first answer in a script that runs no run loop, which made three
  probes conclude that no app could be brought to the front (it can: `tab_focus` and a click on a hit-tested window
  both do). Another server's browser may sit at the default window position, over the test window.
- Tool calls sent in one message run one after another here (the MCP call, then the shell probe), not at the same
  time: a probe that ran "during" `wait_for_human` started after its timeout and found the bar gone. Start such a
  probe in the background first and let it say when it is ready.
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
a repeated correction. A rule about one area goes into that area's file in `docs/agents/`; this file
takes only what every change must know (hard constraints, how to verify, the topic table). Prefer the
specific symptom over the general principle — "an empty `console_get` with `attached: false` means
nothing was monitored, not that the page was quiet" is useful; "be careful with capture" is not.
Say it once and briefly: a rule nobody finishes reading protects nothing.
