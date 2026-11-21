# Browser lifecycle

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## Launch and reuse

- `browser_launch` reuses a running browser only once CDP answers (`is_up()` proves a handle exists, not a live
  socket; a stale handle used to pass), and honours the caller's `url`: "already running" with the url ignored left
  callers on the wrong page, believing they had navigated.
- A second Chrome on the same profile is what produces "CDP unreachable", so a half-dead browser is ended before
  relaunching, not raced. `is_up()` false with a handle is a half-open Chrome: `browser_launch` ends it first, or the
  handle is overwritten and a privacy profile is deleted under a live process that writes it back.
- nodriver stops probing for Chrome after about 2.3 seconds; `_start_chrome` waits for that same process and adopts it
  (`config.host`/`port` set means connect, not spawn), moving `_process` across, or a close would only drop the
  socket. Only one that never answers is ended and respawned, with host and port restored first.
- Nothing starts a browser on its own: launches go only through `browser_launch`, `spawn_browser`, the cookie reader
  of `cookie_migration` (headless, closed before it returns) or `attach_to_chrome`. A Chrome seen again on the same
  port and profile after it ended, with parent PID 1 and no CDP client, was not started by a server (nodriver's child
  would have the live server as parent).
- A launch that would open a profile with a Chrome older than the one that last opened it is refused
  (`profile_version.refusal`) before anything writes to the profile. At every start Chrome writes its full version to
  `Last Version` in the user-data folder and to `Local State` `user_experience_metrics.stability.stats_version`
  (measured for Chrome 157 and Beta 158); the newer of the two counts, and the Chrome's own version is read from its
  Info.plist. So a profile Beta made is refused to Google Chrome 157, headless too, until Chrome 158. A launch that
  moves a profile to a newer major says so once in its result.
- `clean_profile_state` and `browser_recover` leave a locked profile completely alone, Preferences included: the lock
  can be another instance's or session's, and `browser_recover` sweeps every profile dir.
- Profile affinity: a process stays on the profile it settled on. Otherwise, once `browser_recover` frees the shared
  default, the next launch silently moves to another cookie jar, and every login looks wiped while the data sits
  untouched in the old directory.
- Window placement is remembered per instance in `~/.mcp-stealth/window-state/`. Validation applies the saved rect and
  reads it BACK: Chrome clamps a window onto a connected display, so a mismatched read-back proves the monitor is gone
  (no display API is queried). Implausible sizes and minimised/fullscreen states are refused; `wipe_window_state()`
  still clears Chrome's own copy, the cure for the corrupt-placement invisible-window bug.
- A `browser_launch` refused after it made its privacy profile removes that profile on every path, the last
  `_cookie_preflight` before the start included (it returned and left the folder; `test_launch_cleanup.py`).
  After the start it goes only once the browser is verified gone. A failed storage_state or first page ends the
  browser through `_ensure_browser_dead` (`_launch_end_started_browser`), not `stop()` alone; one that could not
  be ended (there, or after a cookie move left unfinished) keeps the handle, naming the profile, so
  `browser_close` or the next launch ends it first, and the result says so. A cancelled launch still uses `stop()`.
  A privacy profile those paths could not remove is said after the original error, with the reason, and stays
  recorded on the instance (`_launch_remove_privacy`), as `browser_close` keeps its own, so the next launch removes it.

## Closing: never report a close that did not happen

- `is_up()` false does not mean Chrome stopped: closing the last tab empties `tabs` while the process runs and holds
  the profile. Close paths branch on `BrowserState.browser is None`, never on `is_up()`.
- `browser.stop()` speaks CDP and can return **without raising** when the socket is gone, so close paths go through
  `_ensure_browser_dead()`, which verifies the process died or signals the PID nodriver recorded at launch. A close
  wrongly reported clean creates an orphan, since it drops the only handle that knew the process: anything in
  `still_alive` makes the close an error that keeps the handle (`_close_incomplete`), and the privacy profile stays
  until the process is gone.
- A browser this server started is first asked to quit with `Browser.close` (`_request_browser_close`) and given
  `_CLOSE_WAIT` (5 s, `MCP_STEALTH_CLOSE_WAIT`, polled with `_all_gone`) before `stop()` and the signals: nodriver's
  `stop()` disconnects and sends SIGTERM, and Chrome then exits without writing its cookie store (written about every
  30 s). Three cookies set and closed at once left 0 rows; with `Browser.close` they were written, in a 0.75 s close.
  A lost reply counts as asked when the socket dropped (Chrome closes it on its way out); watching the process decides
  whether it went, so `verified` keeps its meaning.
- From nodriver 0.50 the Browser is its own Connection and `.connection` stays None: asking only `.connection` never
  sent `Browser.close`, so on every fresh install (0.50.99) a close ended Chrome with SIGTERM and lost its cookies,
  and closing a reattached keep_alive Chrome said "CDP stop did not end the process; signalled ...". uv.lock's 0.48.1
  kept every suite green; CI runs the close paths on 0.50 too (`uv run --with`).
- `_ensure_browser_dead` snapshots the PID's descendants before stopping it: a launcher that does not exec is the
  recorded PID, and ending it alone orphans Chrome. Only descendants naming the profile on their command line count:
  Chrome also wakes `GoogleUpdater`, the machine-wide updater, which outlives the browser by a few seconds; counted as
  family it made 4 closes in 5 say "CDP stop did not end the process" and got SIGTERM mid-update. A PID no longer
  naming the profile is skipped (its number was recycled). The wait is a poll (`_all_gone`), not a sleep (a fixed
  1.5 s made every close take 1.6 s); `verified` still means every one was seen gone.
- Once death is verified, `_ensure_browser_dead` removes nodriver's `uc_*` temp profile, which nodriver leaves, one
  per launch, to an exit hook that on 0.48.1 stops after the first (it prints to the closed stdout).
- Windows has no `ps`: descendants and command lines come from `state.process_table` (a Toolhelp snapshot and
  `NtQueryInformationProcess(ProcessCommandLineInformation)`); parent numbers are reused there, so the walk keeps a
  seen set. Before, the family was empty on Windows. Measured on the runner: a Chrome ended with TerminateProcess
  leaves its 8 to 9 helpers running for 0.2 to 2 s, holding the profile's files (`WinError 32` on
  `Default\Extension Rules\000003.log` in 8 of 8 rounds); one that quits on `Browser.close` took up to 4 s. A close
  that had to signal Chrome then deleted a privacy profile under them, gave up after about a second, and the next
  launch refused.
- `_remove_privacy_profile` deletes nothing while a process names the profile on its command line
  (`state.processes_naming`: Chrome hands `--user-data-dir` to every helper, crashpad gets
  `--database=<profile>\Crashpad`), and waits for them. It asks `state.processes_holding`, which adds every browser
  process whose command line Windows withholds (it may be this profile's helper) and treats an unreadable process list
  as in use, never empty. `processes_naming` itself lists only processes naming the profile: the close's family check
  compares against it, and counting another browser's unreadable helper there failed it on the Windows runner.
- `_remove_privacy_profile` then retries for `MCP_STEALTH_PROFILE_REMOVE_WAIT` (15 s on Windows, where an open handle
  blocks a delete and a deleted file stays until its last handle closes; elsewhere the three tries it always made). A
  folder that still cannot be removed is an error naming why and who holds it: Restart Manager on Windows
  (`state._win_file_holders`, the API behind Explorer's "the file is open in"), with the process table's command line
  when it gives no name (an exiting process).

## Quit and the quit watcher

- Chrome started with `--no-startup-window` does not exit on Quit: the quit Apple Event closes every window and the
  process stays, with its Dock icon and the profile lock (still running 10 s later; without the flag it exits in
  0.3 s), and CDP cannot tell a Quit from closing every window by hand. Ending it from inside the server failed
  whenever the server was unhealthy: a stdio server exits on a `/mcp` reconnect, leaving its Chrome unwatched, and one
  started before `uv tool install --force` ran old code for hours.
- So each headed browser gets its own watcher process (`quit_watch.start`, `_quit_watcher.py`): detached (own
  session), from the base interpreter with `-I` and the source on stdin, standard library only, imports at the top, so
  nothing is read from the installed package once it runs. It polls `/json/list` every 0.5 s; after
  `_WINDOWLESS_GRACE` seconds with no `page` target it writes the `.quit` verdict, sends `Browser.close` over the
  browser websocket, waits `_CLOSE_WAIT` for the PID, and only then signals that one PID, each step after checking its
  command line still names the same `--user-data-dir`. It exits when Chrome is gone and never acts on another process.
- An unanswered endpoint never counts as no windows. Chrome's DevTools HTTP server keeps the connection open whatever
  the client asks, so the body is read to its Content-Length: read to EOF, every poll timed out and read as
  unanswered.
- `tab_close` writes `<key>.tool-closed` BEFORE closing the last tab, or the watcher can read the empty browser first;
  a close that raises takes it back (`unmark_tool_closed`) unless Chrome no longer lists the tab. The marker counts
  only while the server that wrote it is the watcher's parent (`getppid`, no signal): nobody can use an empty browser
  whose server is gone. A tool close (`_ensure_browser_dead`) stops the watcher first.
- The in-process `_sweep_windowless` never closes while a watcher runs: it records `window_closed` as soon as the
  `.quit` verdict appears, cleans up the handle once the watcher has exited, and ends the browser itself only when no
  watcher runs (failed to start, died, or not POSIX). `BrowserState.active_tab` reads the verdict too, so a call in
  the second between the watcher's close and the sweep says "last window", not "crashed".
- The state files live in `$TMPDIR/mcp-stealth-quit-watch` (0700, files 0600; `MCP_STEALTH_QUIT_WATCH_DIR`), keyed by
  Chrome PID and port; the verdict names the profile. `tests/test_headed_quit.py` quits real ones with the server
  alive, killed, and with its venv deleted and rebuilt.

## The idle reaper and closing instances

- A stdio `browser_launch` sets `current_idle_timeout = 0`. Leaving the reaper off is not enough: any `spawn_browser`
  starts it, and it then closed the browser the person was looking at, 15 minutes after its last tool call.
- Every tool call is use of its session's current instance: `StealthMCP.call_tool` sets `current_last_active` before
  and after it, and the reaper spares that instance while a call runs (`_app.calls_in_flight`). When only
  `active_tab()` counted, `tab_new`, `tab_select` and `switch_instance` did not: on Windows CI an instance with
  `idle_timeout_seconds=2` was closed during a `tab_new` while the event loop stalled (the `ps` and `update_targets`
  facts under Chrome, CDP and nodriver), failing 14 of 32 runs (`tab_new` erring or hanging past 60 s, or `tab_select`
  reading the empty list as "tab 0 out of range").
- A sweep closing an instance registers it in `_app.INSTANCE_CLOSING`; a call on it, `switch_instance` to it and
  `close_instance` of it wait for the close. After each await the sweep checks its candidate is still where it found
  it (`_still_held`): with one switched in mid-close, the close's `reset()` wiped whichever instance was current.
  `close_instance`, `close_all_instances` and `browser_close` register too, before their first await, and both sweeps
  skip a registered instance, or a stored one could be closed twice at once. So does a `browser_launch` that fails
  after the start (`_launch_end_started_browser`): its close awaits, and a `switch_instance` let through meanwhile
  had the close reset the instance switched in, or give it the failed launch's profile. What the close finds is
  recorded on its own instance only; one no longer current is left alone and the result says so. A close still running after 60 s is an
  error naming it (`wait_while_closing` returns why): going ahead let the caller act on a state the close then
  changed.
- A tool answering from the tab list goes through `_tab_list_problem`, which names why the browser went
  (`window_closed` carries the idle note) and refuses a list the browser did not confirm (`_refresh_tabs` returns
  False).

## Who may change the current instance during a launch

- `browser_launch` awaits between its steps (the stale browser, the profile reservation, a reattach, the
  cookie move, the start, storage_state, the first navigation, the window, the saved tabs) and works on the
  current instance across them. A `switch_instance` let through meanwhile had the later steps and the failure
  cleanup act on the instance switched in (its browser closed, or the failed launch's profile written onto
  it), and the success path recorded the new browser there. So a launch marks its session (`_LAUNCHING`: the
  instance and the task) before its first await and clears the mark however it ends, a raise or a cancel
  included; a mark whose task is done counts as absent, so a mark left behind refuses nothing.
- While the mark stands, `_launch_gate` (one of `_app.CALL_GATES`, so before the tool runs) refuses
  `switch_instance`, `attach_to_chrome`, `detach`, `browser_close`, `close_all_instances`, `spawn_browser` and
  `close_instance` of the launching instance: "browser_launch of instance 'main' is in progress in this
  session; switch_instance was not done. Call it again once the launch has returned." Refused, not made to
  wait, as `spawn_browser` refuses a second launch, and the text promises nothing about when. Page tools are
  not refused. A second launch in the session is refused by `browser_launch` itself, `instance_release`
  refuses the current instance by itself, and `close_instance` of another instance acts on its stored
  snapshot. `spawn_browser`'s own refusal holds only while `_LAUNCH_LOCK` is held, which left the stale
  browser step open, so the gate refuses it too.
- `tests/test_launch_owner.py` finds every tool that can change the current instance or end its browser (an
  AST walk for writes of `BrowserState.browser` or `current_instance_id` and calls of `reset`, `switch_to`,
  `restore_from`, `release_instance`) and fails on one that is neither in `_REFUSED_DURING_LAUNCH` nor in
  `_ALLOWED_DURING_LAUNCH` with its reason. A new such tool goes into one of the two. None of them may become
  a workflow tool: `batch_actions` and `workflow_run` call tools without the gates.
- `browser_recover` is never refused: it is the way out of a stuck state, and a launch hung on an await
  nobody bounded would otherwise leave the session no exit but a restart. It does what it did (stops the
  current browser, signals its profile's PIDs, resets), marks the launch (`ended_by`) before its first
  await, and its result says the launch was still running. After every await `browser_launch` looks
  (`_launch_settle`): a launch cut short ends as an error naming `browser_recover`, ends the browser it
  started or took over through `_ensure_browser_dead`, once, removes its privacy profile, and records
  nothing more: no handle for a browser that ended, no tabs, no watcher, no keep_alive record. An error the
  step had already reached stays first; a success it reached does not count. The start does not begin once
  the launch is cut short. A browser that could not be ended keeps a handle on the instance the recover
  emptied, naming its profile, as every close path keeps one.
- Inside the steps each acts on the launch's own instance and browser (`plan.launching`, `plan.browser`),
  never on what is current by then: the failure cleanup (`_launch_end_started_browser`), a cancel
  (`_launch_cancelled`), the saved tabs (recorded only while the launch still holds its instance), the
  window placement. A reattach cut short before it recorded the kept browser gives it back as it was (the
  record names its old server again, nothing ends it): it may be the person's, opened from the launcher.
  Once recorded it is the launch's browser, and the recover ended it as the current one.
- The idle reaper and the window sweep skip the launching instance; their next tick sees it.
- A call on an instance being closed waits for the close (`INSTANCE_CLOSING`), `browser_recover` included.
  So a switch or a recover sent during the close of a failed launch runs after the launch has returned. A
  close still running after 60 s answers "still being closed ... or browser_recover if it never ends", and
  that recover meets the same wait: a gap left open, since a close is bounded by `_ensure_browser_dead`.

## Headed focus

- A headed Chrome must not take the keyboard: started normally it is frontmost within 300 ms, and focus given back
  cannot undo keys already typed into it. So it starts with `--no-startup-window`, its first window comes from
  `Target.createTarget(newWindow, background)` (`_open_background_window`), and
  `--disable-backgrounding-occluded-windows` keeps that window `visibilityState: visible` (covered, it would read
  hidden). `tab_new` opens its own background window for the same reason (a background tab in the same window is
  hidden). `tab_select` activates only with `focus=True`; `navigate` defaults to `focus=False`; `tab_focus` and
  `request_human_help` still raise it.
- `tab_new(new_window=False)` headed opens a background tab (`createTarget(background=True)`) with
  `Emulation.setFocusEmulationEnabled` on (`_keep_painting_behind`). Measured on Chrome 157, 3 rounds each: that tab
  reads `visible` with requestAnimationFrame at full rate and Chrome is frontmost 0 ms. A bare background tab reads
  `hidden` with no frames, and `Page.setWebLifecycleState(active)` does not change that. `Target.activateTarget`,
  `Page.bringToFront` and `background=False` all show the page but make Chrome the front app for about 1.6 to 2.1 s
  and hide the tab it replaced.
- Two costs keep it opt-in: the page reads `document.hasFocus()` true while Chrome is not in front, and Chrome picks
  the window (no CDP call names one; it used the browser's first window in 2 of 2 tries whichever tab was selected,
  since windows opened behind are never activated). So the result reads back the tab's window
  (`Browser.getWindowForTarget`) and the page's own `visibilityState`, saying "reads as hidden" when the emulation did
  not take; so does `tab_select` without focus when the selected page reads hidden. The quit watcher counts page
  targets, not windows, so tabs sharing a window change nothing for it.
- `window.open` activates Chrome and no flag or CDP call stops that. `_win_focus_back_from_popup` sends focus back as
  soon as this browser's PID is frontmost (about 130 ms measured, so keys typed in that moment still reach Chrome) and
  hands the front back until it has stayed away for `_FOCUS_STAYED` (0.4 s), since Chrome takes it again while the
  popup opens. `tests/test_headed_focus.py` measures all this (`MCP_HEADED=1`), counting this browser's PID so the
  person's other apps cannot skew it.
- Focus moves by PID, never by application: the person's own Chrome (or Beta) has the same bundle path as this one.
  `request_human_help` and the focus returns after a launch and after a popup ran `open -a <bundle>`, which names an
  application, and with several Chromes running from one bundle macOS chose among them: on 0.25.17, with another
  project's browser running, that browser (its PID) was in front after `request_human_help` while the result said it
  was in the foreground. `tests/test_focus_restore.py` fails if `"open", "-a"` comes back into server.py or
  tools/navigation.py.
- `_activate_pid` brings one process to the front with ctypes, no PyObjC: first the Process Manager (`_front_process`:
  `GetProcessForPID`, `SetFrontProcessWithOptions`), then AppKit's `NSRunningApplication activateWithOptions:`. AppKit
  alone was not enough: macOS sat on its request for about a second when Chrome had just come to the front by a popup
  (1033 and 1039 ms in front in `test_headed_focus`, the limit is 500, and `open -a` passed), while the Process
  Manager took the front back within the limit. From a background process it takes 17 to 21 ms either way (`open -a`:
  21 to 29 ms, after its own start).
- `_give_focus_back` and `_win_focus_back_from_popup` take the PID that had the front (`_frontmost_pid`, one lsappinfo
  read per input where two were made). `_frontmost_pid` asks the Process Manager in process (`_front_pid_in_process`,
  0.28 ms a call) and falls back to two lsappinfo processes (15.5 ms); it runs before every input on a visible browser
  and every 20 ms in the popup watch. It stays fresh without a run loop (6 of 6 switches agreed with lsappinfo), which
  `NSWorkspace.frontmostApplication()` does not.
- `_foreground_for_human` relies on `Page.bringToFront` (which raises its own process), then checks `_frontmost_pid`,
  activates its own PID when another app is still in front, and says `NOT the app in front` when that did not work.

## Dock icon and Chrome Beta

- The Dock icon is opt-in: only the `dock_icon` setting turns it on (`MCP_STEALTH_DOCK_ICON=1`, or true, yes, on, or
  the settings file; see Settings). Off changes nothing: Google Chrome as before, nothing in the launch result,
  nothing written to Chrome Beta, and `dock_icon: off` in `server_status`, whose detail offers the switch as working
  only when Beta is installed. It never fails a launch or the server: any problem is a note in the launch result or in
  `server_status` (`last_note`). `status` starts nothing and creates nothing.
- On, a headed launch on macOS runs from `/Applications/Google Chrome Beta.app`, which carries the project icon as a
  custom Finder icon (`Icon\r` plus the FinderInfo flag at the bundle root, and `org.mcp-stealth.icon` on `Icon\r`
  naming the icon's sha256). Nothing under `Contents/` is written, so `codesign --verify --deep` passes (`--strict`
  fails on the installed Chrome too, over its xattrs). `put_icon_on` only ever writes to a bundle named
  `Google Chrome Beta.app` that does not resolve to `/Applications/Google Chrome.app`: never to the person's Chrome.
- Beta has its own bundle id and registers with GoogleUpdater as `com.google.chrome.beta` at its own path: three
  60-second headed runs left the `com.google.chrome` entry unchanged. That is why it replaced the 0.13.0 clone of
  Chrome, whose runs re-registered `com.google.chrome` at the clone and let the updater update the clone instead of
  `/Applications/Google Chrome.app`. Chrome calls the updater about 20 s after it starts and then every few hours, so
  a short test run proves nothing about registration.
- Beta only opens a profile it cannot damage (`profile_version.beta_verdict`): none (`persistent=False`), a privacy
  profile, a missing or empty folder, or one only Beta can open (last opened by a newer major than Google Chrome, no
  newer than Beta, and no `v10` cookie in any `*/Cookies` or `*/Network/Cookies`). Anything else (a `--user-data-dir`
  in `extra_args`, a link, an unreadable folder or cookie database) means Google Chrome and a note naming why. The
  choice (`_launch_choice`) is made before `clean_profile_state`, the window-state wipe or `mkdir`, which write to the
  folder. The reason is the Chrome 158 fact (Chrome, CDP and nodriver facts): Beta deletes the cookies of a profile
  Chrome 157 used.
- An update of Beta replaces the bundle and the icon with it. `prepare` checks with a stat and an xattr read
  (0.012 ms) and never waits: a missing icon goes back on in a background thread (0.3 s through JXA) while Beta
  starts. The custom-icon flag without `Icon\r` shows a folder in the Dock, so `prepare` first clears a dangling flag
  (two syscalls, 0.14 ms) and that launch shows Beta's own icon. An icon that cannot be set is remembered per Beta
  `Info.plist` and icon, and Beta is still used, with its own icon and a note. A running app's icon follows the bundle
  at once.
- Headless, `attach_to_chrome` and the `solve_turnstile_oscursor` sidecar use Google Chrome. The sidecar refuses at
  once when the main browser's Chrome major differs from its own: a clearance bound to the user agent cannot be moved.
- Beta has its own bundle path, but telling this browser from another still compares PIDs
  (`_win_focus_back_from_popup`, `_pid_recycled` by `--user-data-dir`): the person may run Beta too. Process filters
  in the headed tests match both `/Google Chrome ` and `/Google Chrome Beta `.
- The 0.13.0 copy is only removed now. `discard_copy` deletes, so it is narrow on purpose: only in the configured
  `app_dir()`, never the home folder or anything above it, never through a link, only a folder holding this module's
  `.lock`, and only four names (`Stealth Chrome.app`, its marker, `.lock`, `.building-*`). It leaves the copy while a
  process runs from it (an unreadable process list counts as in use), runs on a thread when any server starts and the
  folder exists, and takes the copy out of LaunchServices (`lsregister -u`) before deleting it.
- `tests/conftest.py` sets `MCP_STEALTH_DOCK_ICON=0` and a temporary `MCP_STEALTH_APP_DIR` for every server the suite
  starts and stubs `prepare` in the pytest process; `MCP_TEST_DOCK_ICON=1` runs the whole suite with the switch on.
  `test_dock_icon_e2e.py` (`MCP_HEADED=1`) sets the icon on the real Beta, takes it off once to stand in for an
  update, and checks Chrome.app is unchanged.
