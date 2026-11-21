# Chrome, CDP and nodriver facts

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

Platform truths, not preferences. Each one cost a release.

## Chrome 158 cookie key and the move to `v11`

- Chrome 158 on macOS encrypts with another key outside its default user-data folder, so in every profile this server
  opens: `KeychainKeyProviderUserDataDirProtection` (new in 158, `ENABLED_BY_DEFAULT`, Chromium CL 8603400) makes
  `browser_process_impl.cc` mark `v10` unavailable and write `v11` (same "Chrome Safe Storage" item, salt "pepper").
  Beta 158 read none of Chrome 157's `v10` cookies and deleted them (1172 to 0 rows, copy of a real profile); the kill
  switch `--disable-features=KeychainKeyProviderUserDataDirProtection` kept them. Chrome 157 reads only `v10`. Not the
  Keychain ACL (lists `com.google.Chrome.beta`, no dialog). `applies` reads the major from Info.plist (free), not the
  framework (1 to 2.4 s; 157.0.8103.39 names the feature nowhere, 158.0.8127.18 twice). Stable 158 due 17 Nov 2026,
  feature on at its branch tip, no field trial.
- `cookie_migration` moves cookies to `v11` when Chrome 158+ on macOS opens a profile outside Chrome's default folders
  whose launched sub-profile holds a `v10` cookie, in this order:
  1. `_prepare_cookie_move` never opens or writes the profile: it copies its cookie databases and journals to
     `storage-states/pre-v11-migration-*-db` (source unchanged across the copy, `integrity_check` on it, every file and
     folder fsynced); a headless Chrome with the flag (`_cookie_flag_arg`, via `_disable_features_arg` so nodriver's
     list survives) reads every cookie from a temporary user-data folder holding only the copy's `v10` rows (5 of 5: the
     Keychain key does not depend on the folder); the backup goes through `write_durable` (O_EXCL and O_NOFOLLOW
     temporary file, fsync, rename, folder fsync) and is read back; the databases must still match the decision's
     fingerprint.
  2. `_cookie_preflight`, read only.
  3. `_commit_cookie_move`: the marker, durably (`.mcp-stealth-v11-migration.json`, `pending`, `version` 2, the profile
     folder's real path, the sub-profile, the backup's sha256 and count); then `strip` deletes the `v10` rows beside the
     `v11` ones: every database checked first, then each in a `BEGIN IMMEDIATE` transaction rechecking that each row is
     in the backup by key and is the copy's very row (digest of every column but `last_access_utc`).
  4. Only now `clean_profile_state`, the window-state wipe and mkdir, so a refusal writes nothing in the profile. Chrome
     starts without the flag; `_finish_cookie_move` sets the cookies (`Storage.setCookies`) and compares keys and sha256
     digests of value, flags and partition, and expiry (within a second, or capped by Chrome's 400 days). All equal is
     `done`; else an error naming the keys, never a value, and it stays `pending`.
- The order is the guarantee (`test_a_crash_at_any_step_of_a_mixed_move_loses_no_cookie`: a `BaseException` at ten
  points, then the next launch ends with every cookie; also a real-Chrome kill after the marker). A crash before the
  marker leaves the profile as it was. After it, the next launch resumes if the backup matches the marker's digest and
  count and holds every loaded `v10` row still in the profile by name, host, path and partition (counted: two
  partitioned cookies of one name are two); else it moves again, carrying over rows only the pending backup holds (a
  half-done strip took them out).
- A pending marker is used only if it is version 2 and names this profile folder and the launched sub-profile (a move of
  Default resumed for Profile 1 set Default's cookies there). Refused: one for another sub-profile, until that one is
  launched; one from v0.16.1 or earlier (no digest, count or binding); one whose backup is missing or not the recorded
  file, which never starts a new move either, since part of the profile may be stripped already. Each refusal names the
  database copy, whether it passes `integrity_check`, and the steps to put it back.
- A failure while preparing: the flag if the profile holds no `v11` cookie (nothing touched, so the launch's own check
  passes), else a refusal. `v10` again after a `done` by the same major means this Chrome writes `v10` there (kill
  switch turned on remotely): the flag and no move, instead of a move on every launch.
- The flag is not a safe default (Beta 159, real Keychain): three `v11` cookies opened with it read 0 and left 0 rows; 5
  `v10` and 2 `v11` of one host read 0 and left 0 with and without it, while the `v10` rows alone read 5 with it and the
  `v11` rows alone 2 without. So the flag only for a profile read to hold no `v11` (`Plan.v11`); a mixed store is split
  (the reading Chrome gets a copy with only the `v10` rows, the launch the profile with only the `v11` rows once every
  `v10` cookie is in the backup); a database not read with certainty refuses the launch (`refuse`), nothing started or
  written. The refusal says what to do: close every Chrome on the profile and launch again, clone_chrome_profile to work
  meanwhile, restore a damaged database from a backup; persistent=False does not keep these logins.
- A decision holds only until something else touches the profile. `Plan.fingerprint` records the launched sub-profile's
  cookie databases and companions (`cookie_db.fingerprint`, absent files included; other sub-profiles do not matter).
  `_cookie_preflight`, right before the commit, refuses on a Chrome holding the lock or any change, since then neither
  the flag nor its absence is known safe; the server writes nothing in the profile before it, and a row written between
  it and the strip stops the strip. The lock is read three ways (`state.chrome_lock_state`): a `SingletonLock` naming no
  readable PID is unknown and refuses, never free. After the commit (fingerprint re-taken after its own strip) and
  `clean_profile_state`, `_cookie_preflight` runs again just before `_start_chrome`. Known limit: about half a second
  from that look to Chrome taking the lock, which no lock Chrome honours can span.
- A move unfinished after Chrome started (`_finish_cookie_move` false, or comparing or writing the done marker raised)
  closes that browser (`_close_after_cookie_failure`) and `spawn_browser` does not register it: left running, the next
  launch reused it and never resumed the move.
- Never read or write through a sub-profile, `Network` folder or marker that is a link (`candidate_databases`,
  `all_databases`, `read_marker`).
- A backup holds every cookie value in plain text. `cookie_migration.expire_backups` (at server start on a thread, and
  after each finished move) removes a backup and its `-db` copy once the marker says `done` (every cookie read back) and
  `finished_at` is older than `MCP_STEALTH_MIGRATION_BACKUP_DAYS` (14); at start `expire_backups` also removes one
  nothing refers to (no marker in its profile, or a done marker naming a later backup) past the retention. Nothing of a
  pending move goes: a retry carries cookies over from the backup before, and a damaged newest one leaves the older as
  the only copy. What stays is made 0600. Under a test it refuses the person's own folder, and `tests/conftest.py`
  points `server.STORAGE_STATE_DIR` at a temporary folder for every in-process test (a fake Chrome 158 in
  `test_profile_version` once wrote database copies into the real `~/.mcp-stealth/storage-states`).
- `remove_reader_leftovers` removes `mcp-stealth-reader-*` folders a killed server left in the temporary folder (older
  than an hour, named by no process; unknown keeps them).
- Beta never opens a profile with data (`beta_verdict`), so it never migrates. Tests use Beta 158 or newer as Chrome 158
  via a `chrome` script on the server's PATH, shorter than Google Chrome's path because nodriver takes the shortest
  candidate.
- Saved passwords are not moved (`cookie_migration` moves cookies only), so a cloned or server profile loses them to
  Chrome or Brave 158 unless they are moved too. Measured only with `--use-mock-keychain` (the mechanism, not real
  Keychain behaviour): a profile Brave 157 wrote outside its default folder, one `v10` login and one `v10` cookie,
  opened by Brave Beta 158.1.101.47: `passwordsPrivate.getSavedPasswordList` lists 0 (Brave 157 on a copy: 1), the
  `Login Data` row stays `v10` and unread, the cookie row is deleted. With
  `--disable-features=KeychainKeyProviderUserDataDirProtection` both read.

## Cookies on disk

- Cookie databases are SQLite with a rollback journal, not a WAL (Chrome 157, Beta 159: header says rollback,
  `Cookies-journal` beside it); a Beta 159 killed after setting cookies left a hot journal of 12,824 bytes.
  `mode=ro&immutable=1` reads the file as it lies (a killed transaction that deleted 40 `v10` rows read as 0 rows; a row
  only in a `-wal` went unseen); `mode=ro` refuses a hot journal (rolling back writes). So every decision reads through
  `cookie_db.read`: database, `-journal` and `-wal` copied to a private 0700 folder, opened normally so SQLite rolls
  back or replays there, `integrity_check` ok, each file's size, times and inode unchanged across the copy.
- Cookies are at `Default/Cookies` on older profiles, `Default/Network/Cookies` on newer ones (Beta 159 moved them
  between two starts); counting only the first makes a busy profile look empty.
- Chromium locks the **user-data root**, not `Default`/`Profile N`; checking a sub-profile reports every profile free
  while the browser runs.
- SQLite's `Connection.backup()` (not nodriver's CDP calls) on a database a live browser writes blocks inside C, ignores
  signals and hangs indefinitely: copy the file with its `-wal`/`-shm` companions and integrity-check the copy.
- `Network.deleteCookies` reports success whether or not anything matched and matches the exact stored domain, path and
  partition, so `cookie_delete` reads what exists, deletes those, and reads back.
- curl_cffi names a cookie given without a domain after the request host, and a host with no dot becomes
  `localhost.local`, so it was never sent on `localhost`; one put in the jar with the host itself (`localhost`, `::1`)
  is. libcurl also speaks `file://`, which `http_request` refuses.

## Identity and launch flags

- `Network.setUserAgentOverride` needs `user_agent_metadata`, or `navigator.userAgentData.brands` comes back EMPTY (no
  real Chrome's does), and a UA even to change only the language. Identity defaults to the real browser and machine in
  every mode; `_apply_fingerprint_to_tab` derives the hints from the UA in force. Never invent a default identity: a
  random OS or Chrome version over a real engine contradicts itself. Overrides are per launch, through `fingerprint`.
- `Network.setUserAgentOverride(acceptLanguage)` is split on commas for `navigator.languages` and generates the weighted
  header: pass bare tags (`id-ID,id;q=0.9` reports a language named `id;q=0.9`). Weights Chrome would not generate go
  through `setExtraHTTPHeaders`.
- nodriver 0.50 does not pass `Config.lang` on, its `add_argument` refuses `--lang`, and macOS Chrome ignores the flag
  anyway: a `lang` other than en-US goes through the Accept-Language override, as a fingerprint does.
- Headless Chrome reports an 800x600 screen whatever `--window-size` says, smaller than the default 1280x800 window,
  which no machine shows. `_headless_screen_arg` passes only `--screen-info`: the main display's size and work area
  (`NSScreen` on macOS), or the smallest common size holding the window; `devicePixelRatio` stays 1 and `colorDepth` 24,
  as headless reports. Headed is untouched, a caller's own `--screen-info` wins, and `fingerprint_audit` warns on a
  window larger than its screen.
- Chrome 157 on macOS lists 199 speech voices, 180 local and 19 "Google ..." from its Network Speech component
  extension, headless or not, even with no CDP client. They arrive in two steps (the first `voiceschanged` may hold only
  the 19): read the list once it settles. `--disable-component-extensions-with-background-pages` saves about 0.27 GB but
  leaves the 180 local voices and no Google one, which pages can read: light mode only, and `fingerprint_audit` warns on
  it. No `--disable-features` name was found that turns off Gemini or Hangouts alone.
- Chrome honours only the last `--disable-features`, and nodriver puts its own (`IsolateOrigins,site-per-process`)
  first, so a second one silently drops nodriver's list: build one with `_disable_features_arg`.
- Site isolation is on in every browser this server starts. nodriver 0.48 and 0.50 pass
  `--disable-features=IsolateOrigins,site-per-process`, but `site-per-process` is a switch, not a feature
  (`SitePerProcess` is), so Chrome ignores it. Chrome 157 and Brave 157, fresh profiles, a page with a cross-site and a
  same-site iframe: with or without the flag the cross-site iframe is its own `iframe` target, same renderer count
  (Chrome 10, Brave 4); only `--disable-site-isolation-trials` or `--disable-features=SitePerProcess` put it in process
  (8 and 2). The full E2E suite passed on both with the flag removed, no failure caused by it. It stays:
  `IsolateOrigins` with an origin list can come from a field trial on a profile with a variations seed, which no
  headless test profile fetched, so removing it is not proven to change nothing there. Refs, the frame walk and hit
  tests are built for out-of-process frames.
- Every launch adds `--disable-default-apps`: otherwise a new profile installs Google Docs Offline and Web Store
  Payments (two more renderers, about 0.2 GB), paid on every `persistent=False` launch.
  `--disable-background-networking` saved the same but also stops Safe Browsing and variations updates: light mode only.
- The first document after startup loads images despite `--blink-settings=imagesEnabled=false` (later ones obey), so
  light and testing modes go through `about:blank` first.

## Tabs, navigation and evaluate

- Chrome lists page targets in neither tab nor creation order: keep tab order by the server's own list. Chrome 157, tabs
  opened A, B, C, D, then C activated: `Target.getTargets` gave C, B, D, A, `/json/list` C, D, B, A (by last activity),
  nodriver's `browser.tabs` A, B, C, D. A page that failed to load reads `chrome-error://chromewebdata/` as
  `location.href` but keeps the failed URL in `TargetInfo.url`, so a saved tab list keeps it.
- `patches._patch_tab_get_keeps_session` replaces `Tab.get` on 0.48 and 0.50: `Page.navigate` answers once the
  navigation has committed, and a URL that already matches is not waited on. Up to 0.48 `Tab.get` ends in `await self`,
  a half-second sleep, on every navigation; our earlier 0.50 patch waited up to half a second for the URL to change,
  always paid when reopening the current URL. Up to 0.48 the class refuses plain assignment (`type.__setattr__`) and
  `Tab.target` is read-only (`_target`).
- `Tab.wait(t)` is only `sleep(t)`: `wait_for_navigation` slept its whole timeout and said `navigated`. Use
  `_wait_load_complete`.
- `tab.evaluate` returns `ExceptionDetails` when the script throws instead of raising: check for it (`_eval_checked`),
  or the error reads as the value. `tab.evaluate(expr, return_by_value=True)` can STILL return a `RemoteObject`: unwrap
  via `v.value if hasattr(v, "value") ...` before casting. Its deep serialization leaves `value` empty for any object or
  array, so the `evaluate` tool calls `Runtime.evaluate` itself. Without `await_promise=True` every async expression
  returns `null`, silently.
- Chrome omits CDP fields it used to send, which nodriver indexes directly: a missing `sameParty` (cookies) or
  `privateNetworkRequestPolicy` (`ClientSecurityState`) raises KeyError **inside the event listener**, killing it, so
  captures come back empty instead of erroring. `patches.py` fills the documented defaults; `patches.status()` reports
  what applied.

## Keys, mouse and page input

- A key event with only `key` fires keydown but no keypress, so Enter never submits. `key_event_params` fills `code`,
  the virtual key code and `text`, and leaves out `nativeVirtualKeyCode`: with it, `Input.dispatchKeyEvent` on a key the
  page does not consume holds every later mouse event for 5 seconds on macOS, until the next navigation (a Chrome shared
  between test scenarios hides it: the stall survives `reload`).
- `send_keys` types character by character: `Element.send_keys` sends only `char` events (a keypress with an empty
  `key`, no keydown; no keyboard does that), and a newline sent so adds nothing. `_type_keys` presses real keys; macOS
  select-all also needs `commands=["selectAll"]`. `fill` presses Enter between lines only in a textarea or
  contenteditable, since in a single-line field Enter submits the form.
- Do not call `Tab.mouse_move`: it interpolates from (0,0), not the cursor, sweeping a diagonal that fires hover
  handlers, then sends a `mouseReleased` the page sees as a stray mouseup. Use `move_pointer`, or `humanized_move` for a
  path.
- `DOM.getNodeForLocation` takes document coordinates, every other CDP position here is viewport-relative: add the
  scroll offset, or a scrolled page is hit-tested at the wrong spot.
- `window.devicePixelRatio` includes zoom, which a persistent profile remembers per host (a host once zoomed to 90%
  reports 0.9); a clip still rasterises at CSS size. E2E tests use `persistent=False` for this, and to stay out of the
  real profile.
- `Element.select_option()` takes no argument and selects the `<option>` it is called on, so on a `<select>` with a
  value it raises (the `select_option` tool did). CDP input wants `MouseButton`, not a string.
- Hover before pressing: menus and overlays that render only on hover (Canva, Figma, Notion) are unreachable with a bare
  `mouse_click_xy`, hence `move_first=True` by default.
- `navigator.clipboard.writeText` needs BOTH a trusted gesture (`mouse_click_xy` gives one, a JS `.click()` does not)
  and OS window focus (even a real click is refused when the window is not frontmost: `tab_focus()` first).
- `Element.text_all` is `textContent`-based and includes `<script>` bodies: use innerText semantics for what a human
  reads.
- `:has-text()` is a Playwright-ism, not valid CSS: `querySelector` throws on it (it surfaced as a bare "DOM Error while
  querying").

## Cloudflare, Turnstile and search pages

- `auto_verify` looks once after load (`_cf_first_look`) and returns at once on "none". It keeps the old 0.6 s second
  look only when a challenge may still be coming: the page is not `complete`, an inline script or `link` names
  Turnstile, or a request to `challenges.cloudflare.com` was already made. A widget a bundled script injects after load
  without those traces is no longer waited for; the old wait covered only its first 0.6 s and cost every navigation 0.6
  s.
- `Tab.verify_cf` clicks the best template match however poor, so on a page with no widget it clicked something and
  reported success. `_cf_template_click` applies a threshold; a challenge click reports what `_cf_after_click` observes.
- A Turnstile checkbox vanishing after the click is not a pass: it goes while the page submits (`?__cf_chl_tk=...`),
  before verification and the redirect. So `solve_turnstile_oscursor` keeps its fixed 8 s of scoring until a pass signal
  holds on any site, not just one demo page's look. It takes about 16 to 20 s standalone, 45 to 50 s in the MCP server
  (native steps run slower there); 35 s timed out during scoring, hence the default of 50.
- SERP scraping soft-blocks on datacenter IPs and cold headless profiles: Google and Bing bot-wall a fresh profile, a
  warm persistent one in a real browser passes, and `engine="brave"` gives the richest results, with relative dates. An
  empty SERP is usually the environment, not the code.

## Windows

- `os.kill(pid, 0)` is no liveness probe on Windows (0 is `CTRL_C_EVENT`, sent via `GenerateConsoleCtrlEvent`;
  `signal.SIGKILL` does not exist): every close reported Chrome still running. Probe through `_process_exists`
  (`OpenProcess`/`GetExitCodeProcess`, `state._win_pid_alive`), never `os.kill(pid, 0)`.
- Nothing on the event loop may shell out to `ps` on Windows: a person's machine has none, the runner's is Git Bash's.
  `_process_command` ran it at every close, blocking the loop 1.1 s up to its 3 s timeout. Command lines come from
  `state.process_table()`.
- nodriver 0.48 starts `update_targets()` as tasks nobody awaits, on every target event and in `Browser.wait` and
  `Tab.sleep`. On a closing browser each fails, asyncio logs "Task exception was never retrieved", and FastMCP's Rich
  handler renders the traceback on the event loop: 1.1 to 1.3 s each on the Windows runner, five per close, which the
  idle reaper took for idleness, closing an instance just spawned. `patches._patch_background_target_refresh` ends those
  tasks quietly; an awaited `update_targets` still raises.
- A module-level import of POSIX-only `fcntl` kept the server from starting on Windows, unnoticed with no test there.
  CI's Windows jobs guard it; `test_smoke` imports the server with `fcntl` hidden.
- Windows keeps no POSIX mode bits (files read back `0o666`): tests of `0600` skip there, and
  `http_transport.load_or_create_token` refuses its own token file from the second `--http` start (open).
- Windows CI (Google Chrome 156, system install): a profile outside the default user-data folder gets none of its
  cookies back on the next launch, though they are written (`v10`, DPAPI key intact in `Local State`) and not deleted;
  nodriver alone does the same, so it is Chrome. App-Bound reports `SupportLevel` 4 (not the default folder). Open;
  Linux and macOS pass the round trip (`test_cookie_close_e2e.py`).
- Windows CI: a server process's first screenshot (`screenshot`, or `click_turnstile`'s template match) does not answer
  within 60 s (150 s in a probe); later ones take 1.2 s. Not the `cv2` import. Open; an `xfail` in
  `test_protocol_e2e.py` records it.
