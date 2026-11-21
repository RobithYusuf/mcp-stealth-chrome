# Kept browsers (keep_alive)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- A browser of ours whose server went without closing it (killed, crashed, an older version) and that was never kept has
  no record, so every later launch on its profile fell back to an empty one and every login read as gone: on 20 Nov 2026
  Chrome PID 37250 held the main profile for five hours with parent PID 1, no watcher and no record. A launch on a
  profile the server chose itself (`_kept_candidates`, spawn without `profile_dir`) describes such a holder into a
  record of state `orphan` (`keep_alive.orphan_record`): only with no record already, a parent that is PID 1 or a
  subreaper (`systemd`, `init`; `_reaped`), a browser main process naming exactly that `--user-data-dir`, a debugging
  port, every `OUR_FLAGS` flag, and DevTools on that port naming the PID. `check` then proves it like a kept browser
  (lock, start time, version, no live server) and the takeover is the keep_alive reattach. Its plain quit watcher, found
  by its command line, is ended as a kept one's. `keep=False` in the record: left unset, keep_alive stays off and it
  closes with the server that took it; `_arm_keep_alive` writes `keep=True`, so keep_alive=True is not undone by the
  next reattach. A record made for a holder that does not pass is removed again, and a takeover that fails after the
  check gives the record back and lets the launch fall back as before (`_orphan_given_back`), rather than erroring on
  every launch until that Chrome ends. Only the folder the server chose itself (`_server_profile_for`): a
  `user_data_dir`, `--user-data-dir`, `profile_dir`, or a caller's folder that became main's affinity, is never taken.
- A browser this server took over (an orphan, or a kept browser reattached with keep_alive off) keeps a record naming
  this server (`state: held`, `keep: False`) for as long as it drives it (`_disarm_keep_alive`). Its parent stays PID 1,
  so without the record a third launch read it as an orphan and took it from under the server driving it (found in
  review before release, `test_a_browser_left_by_a_killed_server_is_taken_over_not_fallen_back` checks a third server).
  The record goes once Chrome is gone (close paths, exit). The server's own child removes it as before.
- `_reaped` refuses this server and a live server as an orphan's parent: a server running as PID 1 in a container is
  the parent of its own Chrome.
- `_orphan_given_back` falls back only when the takeover never completed (the record still names no server). A
  takeover that completed and whose url then failed (did not load, `expect_url`/`expect_status`) keeps the browser:
  falling back overwrote the handle and left the orphan driven by nobody.
- `_keep_on_reattach`: asked is as asked; left unset is as it was (no `keep` field: kept; an orphan: not), and the
  person's browser (`opened_by: launcher`) always stays, whatever `keep: False` an earlier agent's one-off
  `keep_alive=False` left in its record. `_server_holding` counts this very process as a live server whatever its
  command line (in-process tests, an embedded server).
- Known limit: a held record of a taken-over browser outlives a Quit after its server was killed (the plain watcher
  carries no record path), and the next orphan on that profile falls back once while `check` removes the dead record.
- A fallback leads the launch result: `WARNING: EMPTY FALLBACK PROFILE, your saved logins are NOT here (<who holds it>)`,
  and says not to log in again there. As a suffix after the URL and status it was missed, and a login made on the
  fallback splits the logins over two folders. `_holder_story` reads the profile's record first (a kept or taken-over
  browser has parent PID 1 whoever drives it), then the process table: this server, another stealth server, a leftover
  of ours, or a Chrome this server did not start. `profile_conflict='clone'` carries the logins and gets no warning.
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
- The record is no integrity boundary, and `owned` is not signed: any process running as this user can write the file,
  and that process can already drive the debugging port and read every cookie (see the port facts under Sharing a
  browser). So `owned` fails safe instead (`_owned_before`): an entry counts only when the record's `owned_for`
  equals its own generation (`keep_alive.generation`: pid, pid_start, port, which `check` proved against the live
  browser) and the target is a page that exists now; anything else is dropped and named in the result. Residual: a
  same-user process that writes a matching `owned_for` can make a person's tab count as the agent's.
- `opened_by` came with the launcher, in the same release (0.20.0, 49e521a); before it there was no launcher, and
  `write` merges, so a later reattach by any server keeps the field. A record without it is a pre-0.20.0 agent
  launch and is taken over whole. Measured: 0.20.0's own `launcher.cold_launch` leaves `opened_by: launcher` and no
  `owned`, which takes none of the tabs.
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
- A browser a person opened from the launcher has no server while they use it, so nothing saved its tabs: every
  start from the Dock came back with one blank tab. The quit watcher, which reads `/json/list` anyway, keeps the last
  list of a kept visible browser in `<keep-alive>/<hash>.tabs` (`_quit_watcher.TabKeeper`, 0600, written beside and
  renamed in). A list counts only after it stood still for `TABS_SETTLE` (3 s): a Quit closes the windows one after
  another in well under that, so a half-closed browser never replaces the full list, and blank or internal pages are
  not a list. `launcher.adopt_watched_tabs` makes it the saved list when it is newer than the one a server recorded,
  and `cold_launch` passes `restore_tabs=True` with `server.RESTORE_AS_TABS`: tabs in one window for a person, where
  an agent's restore keeps a window per page so each reads as visible. The suffix is `.tabs`, not `.json`, because
  `live_profiles` reads every `.json` there as a record. `_forget_tabs_note` drops it with the saved list.
- In a browser the person opened from the launcher, an agent's `url=` on reattach and a `tab_new` with
  `new_window` unset open as a tab of the person's window (`_open_background_tab`, focus emulation), not as a
  window of the agent's own. That window took the saved placement, sat exactly over the person's, and read as
  "my tabs are gone". `new_window=True` still gives a window; an agent's own browser keeps a window per tab.
- Brave reports itself as Google Chrome on Google's own sites: on google.com, accounts.google.com, youtube.com and
  workspace.google.com `navigator.brave` is undefined and the brands name "Google Chrome", in the person's own tabs
  too, while other sites in the same browser read `navigator.brave` and the "Brave" brand (measured on Brave Beta
  158.1.101.47, tab by tab over raw CDP). It is Brave's own compatibility behaviour, not an override of ours: do
  not "fix" the brand there, and do not judge Brave by a probe run on a Google page.
- A saved tab list keeps 30 pages (`MCP_STEALTH_RESTORE_TABS_MAX`), sized for an agent, whose restore opens a
  window per page. A person moved 48 tabs into the browser the launcher opened: a Quit would have brought back 30.
  A person's browser (`_persons_browser`, or the launcher's own process) saves up to `tab_record.MAX_TABS_PERSON`
  (300, `MCP_STEALTH_RESTORE_TABS_MAX_PERSON`) through `_tab_limit`, the watcher keeps as many, and a restore
  loads `_RESTORE_PARALLEL` pages at a time. Every `tab_record.save` in the server passes `limit=_tab_limit(...)`:
  one that did not would write 30 over the full list, and being newer it would win over the watcher's.
- Tools that did the same thing were folded into one, the old name kept for one release and removed in 0.25.0:
  `humanize_click` is `click(humanize=True)` or `mouse_click_xy(humanize=True)`; `localstorage_clear`,
  `sessionstorage_set` and `sessionstorage_clear` are `localstorage_set(area=, clear=)`. Measured first: each pair gave
  the same result on a local page. `inspect_element` reads a whole element and `get_attribute` one value, so they stay
  apart. The count went from 173 to 169 when they went; their definitions were about 1% of the tool list.
- `click_at_image` reports the page change as every click does (it said only "clicked image match"), `extract_structured`
  resolves href/src against the page (`raw:href` keeps the attribute as written), and `fingerprint_rotate` warns when the
  identity claims another OS than the machine: measured, a Windows UA on a Mac read the Apple GPU through WebGL beside
  platform Win32, and the tool said only "applied".
- The launcher did not pass `brave_lean`, and a profile older than lean is not one "the server made empty", so a
  person's agent Brave kept Leo and the rest. `install-launcher --lean` stores the choice in
  `<data>/launcher-lean-<browser>` and `open` reads it (`lean_choice`); unset, the server's own rule decides.
- `tests/test_keep_alive_e2e.py` proves it on real Chrome with a temporary data folder: EOF, reattach with the typed value,
  the tab list at exit, `spawn_browser`, the time cap, a forged record over a foreign Chrome, a browser a live server holds,
  and (`MCP_HEADED=1`) reattach without taking focus, then Quit with no server.
