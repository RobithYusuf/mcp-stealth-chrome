# Browser lifecycle

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

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
  From nodriver 0.50 the Browser is its own Connection and `.connection` stays None: asking only `.connection`
  never sent `Browser.close` there, so on every fresh install (0.50.99) a close ended Chrome with SIGTERM and lost
  its cookies, and closing a reattached keep_alive Chrome said "CDP stop did not end the process; signalled ...".
  uv.lock held 0.48.1, so every suite passed. CI runs the close paths on 0.50 as well (`uv run --with`).
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
- Focus moves by PID, never by application. `request_human_help`, the focus return after a launch and the one
  after a popup ran `open -a <bundle>`, which names an application: with several Chromes running from one bundle
  (the person's own, another project's browser, this server's) macOS chose among them. Measured on 0.25.17 with
  another project's browser running: after `request_human_help` the app in front was that browser (its PID), while
  the result said "in the foreground ... macOS app activated" (the person: "the focus is wrong, another project's
  window comes forward"). `_activate_pid` brings one process to the front with ctypes, no PyObjC: first the Process
  Manager (`_front_process`: `GetProcessForPID`, `SetFrontProcessWithOptions`), then AppKit's
  `NSRunningApplication activateWithOptions:`. AppKit alone was not enough: it is a request macOS sat on for about
  a second when Chrome had just come to the front by a popup (1033 and 1039 ms in front in `test_headed_focus`, the
  limit is 500, and `open -a` passed), while the Process Manager took the front back within the limit. From a
  background process it takes 17 to 21 ms either way (`open -a`: 21 to 29 ms, after its own start).
  `_win_focus_back_from_popup` hands the front back until it has stayed away for `_FOCUS_STAYED` (0.4 s), since
  Chrome takes it again while the popup opens.
  `_give_focus_back` and `_win_focus_back_from_popup` take the PID that had the front (`_frontmost_pid`, one
  lsappinfo read per input where two were made). `_foreground_for_human` relies on `Page.bringToFront`, which
  raises its own process, checks `_frontmost_pid`, activates its own PID when another app is still in front, and
  says `NOT the app in front` when that did not work. `tests/test_focus_restore.py` fails if `"open", "-a"` comes
  back into server.py or tools/navigation.py.
- The Dock icon is opt-in: only the `dock_icon` setting turns it on (`MCP_STEALTH_DOCK_ICON=1`, or true, yes, on, or the
  settings file; see Settings). Off changes nothing: Google
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
