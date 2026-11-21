# Chrome, CDP and nodriver facts

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

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
- Saved passwords are not moved. Measured with `--use-mock-keychain` only (the real Keychain was not used): a profile
  Brave 157 wrote outside Brave's default folder, holding one `v10` login and one `v10` cookie, opened by Brave Beta
  158.1.101.47: `passwordsPrivate.getSavedPasswordList` lists 0 (Brave 157 on a copy: 1), the `Login Data` row stays
  `v10` and unread, and the cookie row is deleted. With `--disable-features=KeychainKeyProviderUserDataDirProtection`
  both read. `cookie_migration` moves cookies only, so a cloned or server profile loses its saved passwords to
  Chrome or Brave 158 unless they are moved too. Mock-keychain rows prove the mechanism, not real Keychain behaviour.
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
  before ours, so a second one silently drops nodriver's list. Build one flag with `_disable_features_arg`.
- Site isolation is on in every browser this server starts. nodriver 0.48 and 0.50 both pass
  `--disable-features=IsolateOrigins,site-per-process`, and `site-per-process` is a switch name, not a feature name
  (the feature is `SitePerProcess`), so Chrome ignores it. Measured on Chrome 157 and Brave 157, fresh profiles, a
  page with a cross-site and a same-site iframe: with the flag and without it, the cross-site iframe is its own
  `iframe` target and the renderer count is the same (Chrome 10, Brave 4); only `--disable-site-isolation-trials` or
  `--disable-features=SitePerProcess` put it in process (8 and 2). The full E2E suite with the flag removed passed on
  Chrome and Brave with no failure caused by it. The flag stays: `IsolateOrigins` with an origin list can come from
  a field trial on a profile that has a variations seed, which a headless test profile never fetched, so removing
  it is not proven to change nothing there. Refs, the frame walk and hit tests are built for out-of-process frames. Every launch adds `--disable-default-apps`: a new profile otherwise
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
- curl_cffi names a cookie given without a domain after the request host, and a host with no dot becomes
  `localhost.local`, so curl never sent it on `localhost`. A cookie put in the jar with the host itself (`localhost`,
  `::1`) is sent. libcurl also speaks `file://`, which `http_request` refuses.
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
