# Agent overlay (agent_overlay)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## When it runs

- Per launch (`browser_launch`, `spawn_browser`, both reattaches), off unless asked or the `agent_overlay` setting is
  on. Needs macOS, headed, PyObjC from the `os-cursor` extra (`find_spec`, nothing imported). OFF with the reason when
  headless, with no known Chrome PID, or in a browser the person opened from the launcher (a lock would keep them from
  their own tabs). `tests/conftest.py` sets it off and `MCP_STEALTH_OVERLAY_DIR` to a temporary folder for every
  server.
- `attach` is async and says on only once the process wrote its PID into `control.json` after its windows exist
  (`READY_WAIT` 3 s); else it stops the process and says OFF, naming the last line of `overlay.log` if it died.
  `agent_overlay=False` on a browser that has one stops it.

## Who holds the browser

- Only the bar's button changes the holder. A click or scroll on the page while the agent works lands on the lock, a
  fully transparent window over the viewport (it takes clicks only because `ignoresMouseEvents` is set to False
  explicitly; checked with `windowNumberAtPoint`), and the bar says the page is locked.
- Keys cannot be held back without taking the keyboard from the browser: the overlay records them,
  `presence.before_input` (in `_dispatch_input`, `_type_keys`, `move_pointer`) waits for 1.5 s of quiet (10 s at
  most), and the result says the person's keys reached the page. Stated limit: Enter can act on the page first.
- While the person holds it, `_app.CALL_GATES` refuses every tool not in `presence.WHILE_PERSON_HOLDS`, reading tools
  too (they may be typing a password), naming Resume agent and `wait_for_human`; it also gates the tools in
  `_RUNS_DESPITE_DIALOG` (tab_list reads titles). Mid-tool, `before_input` raises `PersonHasBrowser`. `wait_for_human`
  ends on Done or on Resume agent pressed after it was asked, and ignores page changes while the person holds the
  browser (their own clicks change it).
- While the bar shows a request for help (`presence.shows_request`: the overlay runs and `ask` returned the
  request's time), a page change does not end `wait_for_human`; only Done, Resume agent, `done_selector` or
  `done_url_contains` do. The bar shows that request with Done even with `overlay_bar` off (`wants_bar`). An
  overlay whose process exits during the wait is started again first (`presence.keep_running`, `revive`), checked at
  the top of every pass, so also while the person holds the browser and on a selector or URL wait; only one that
  cannot come back lets a page change count, and the result says the bar ended. A hold whose overlay cannot come
  back ends the wait as an error (Resume agent is gone with it). A browser or tab that is gone ends the wait as an
  error (`_gone`); a tab counts as gone only when a non-empty list of Chrome's pages, read then or `TARGETS_AGAIN`
  (0.3 s) later, lacks it, since an empty or unread list proves nothing. Shopee
  moves its login page to `/buyer/login/qr` by itself a moment after loading, and a wait measured from the
  request called that a finished handoff after 0.0 s. A timeout takes the request off the bar, so the person never
  reads "Your help is needed" while the agent does something else, but the agent keeps it (`shown_at` None): the
  next `wait_for_human` shows it again with the same text and counts as the same handoff, and a Done counts from
  that new showing on. Only a request the bar showed (`was_shown`) is shown again: one handed over while no overlay
  ran stays without a bar, even if one runs by the next wait.
- `before_input` runs in one wrapper on nodriver's `Tab.mouse_click` (`patches._patch_mouse_click_reports_pointer`,
  which also feeds the click ring; sixteen places press through it), each drag step (the button is still let go in its
  `finally`), each `humanized_scroll` chunk (outside its retry, which would resend the wheel event, and once more
  before the retry and the JS `scrollBy` fallback), `scroll`'s instant wheel, top and bottom, `scroll_to`, every JS
  click fallback, and each step of `batch_actions` and `workflow_run` (`presence.refusal`). `fill`'s JS-value fallback
  re-raises `PersonHasBrowser` instead of writing the value anyway.
- Fails closed: `refusal` and `before_input` act on the overlay's handle whether its process runs or not. A dead one
  is restarted (`revive`, at most `RESTARTS` 3 attempts, a failed start counting too and followed by `REVIVE_BACKOFF`
  3 s without one, only while Chrome's PID still runs with the start time recorded at attach, as PIDs are handed out
  again) and keeps the holder, since the new process reads `control.json` first. Held
  with no overlay to show Resume agent, every tool is refused except `browser_launch(agent_overlay=False)`, which
  stops it: the one way out, named in the refusal.
- The button is the only way to take over, so help (Done) and the person's hold (Resume agent) always show it and are
  chosen before `HIDE_AFTER`, never fading while they are the only way back. A bar set off turns the lock off: a lock
  with no Take over in reach would shut the person out of the page.
- The bar says what the agent knows of the Take over (`told`), not "The agent is paused" whatever it does: not yet
  ("learns it at its next step"); told (a refusal or stopped input writes `told_at`); going on without the browser (a
  tool in `presence.OUTSIDE` while they hold it writes `outside_at`, and its result says they hold it); waiting
  (`waiting_human`, while `wait_for_human` runs).
- A request answered with Done is `noted` (`choose_mode(answered=)`: `help_done` at or after the request's `asked_at`)
  until an agent call takes it: the bar says "Done, the agent goes on at its next step", any tool's first result says
  the person pressed Done and closes the request (`_done_pressed` in `result_note`), and a later `wait_for_human`
  returns at once (0.0 s). Before, the bar kept asking, the note line held the keyboard, and an agent that read the
  page instead of calling `wait_for_human` was never told. The headed test presses Done by signal and reads the
  windows.
- Buttons only on the bar: do not add a text field again without asking. Its note line (0.25.15 to 0.25.17, sent with
  Resume agent or Done) was taken out at the person's request: it made the bar busy, and they talk to the agent in its
  chat.

## Busy, quiet and idle

- A call in flight is never quiet: `before_tool` marks the overlay busy (`in_flight`; `_CALL_OVERLAY` carries it to
  the same call's `after_tool` whatever instance is current by then) and no quiet time counts while `busy`. A call
  longer than `HIDE_AFTER` (a long wait_for) lost the light, bar and lock mid-call.
- Between calls the light dims rather than going out (`QUIET_AFTER` 2.5 s, to 70%; half read as switched off); it goes
  out after `HIDE_AFTER` 90 s (`MCP_STEALTH_OVERLAY_HIDE_AFTER`; tests use 3 s).
- Past IDLE_AFTER (20 s, `MCP_STEALTH_OVERLAY_IDLE_AFTER`) with no call, `choose_mode` says `resting`: no lock, the
  light out, the bar "Agent idle" with Take over. Without it a finished agent left the lock up for HIDE_AFTER (90 s),
  and the person had to press Take over, unheard by any agent, to use their own page. A working agent's steps came at
  most 14.7 s apart, median 4.3 s (60 calls of a research run).
- Clicks or scrolls over the browser, or typing with it in front, while it rests or after the overlay faded go to
  control.json (`person_used`, `person_used_at`); the agent's next call carries that once (`_pass_on`), so it reads
  the page again. Checked with real clicks on a test browser: at idle the page took the click, and the next call named
  it and brought the lock back.

## Process and files

- A process of its own (`python -I -`, source on stdin like the quit watcher, but `sys.executable` for the venv's
  PyObjC), sharing two 0600 files in a 0700 folder: `agent.json` from the server (status, active_at, help, the agent's
  tab's window bounds, the page's place in it, the pointer in page coordinates) and `control.json` from the overlay
  (holder, since, reason, person_last_input, help_done, show_cursor). SIGUSR1 toggles the cursor and SIGUSR2 presses
  the bar's button, which is how tests press it.
- It exits on `ended`, when Chrome's PID is gone, or when its parent is no longer the server (`getppid`).
  `_ensure_browser_dead` stops it first, `_stop_all_browsers` all of them, `_safe_stop_browser` one whose launch
  failed after it started. Children inherit the stdio server's ignored SIGTERM, so `_agent_overlay.py` resets it to
  the default; `Overlay.stop` follows `ended` with terminate, then kill, and removes the folder only once the process
  is gone. A process whose code could not be written to it is killed.
- A process left by a killed server removes its own folder when it sees the browser or the server gone, on `ended`, or
  when its PID's start time is no longer the one it was given. Folder names carry PID and generation
  (`chrome-<pid>-<generation>-*`); `attach` prunes those whose PID is gone or runs a process of another generation.
- State files are read with `O_NOFOLLOW`, as regular files only; the log is created 0600; a state root that is a link
  is refused.

## Following the window and the page

- Windows are told apart by bounds (CDP's window id is not a CGWindowID; titles need Screen Recording). Two browser
  windows at exactly the same rect cannot be told apart; the front one is followed. CG and AppKit global coordinates
  both start at the main display (`screens()[0]`), whose height converts them, so every display is right (a review
  that called this wrong for other monitors was checked against it).
- The followed window is kept while on screen, also when the person moves or resizes it (a Chrome popup over 300x200
  moved the overlay). Another is followed only when it matches the server's last bounds, checked once per new
  measurement (`want_seen`): dropping it on any mismatch jumped to the browser's largest window and read every window
  on screen four times a second (minimize hid in 0.55 s, now 0.02 s). Between scans its own bounds are read every tick
  (`followed_bounds`: 0.12 ms, against 0.47 ms for all on screen), so a drag or resize is followed in 0.03 to 0.12 s.
- The lock follows the page, not the last measurement. The server reads window and page (`refresh_window`) before each
  call, right after it (`after_tool` starts it, so a launch has its lock at once) and about once a second while the
  overlay shows (`_follow_window`; not while the person holds the browser or after the overlay faded), writing only
  what changed. Every tick the overlay applies the page's insets to the window's live frame (`page_rect`): a window
  widened from 1000 to 1250 pt between calls had left the right 250 pt unlocked; now the lock follows 0.001 s after
  the light.
- The page is measured in screen points from the device-pixel `layoutViewport` (`presence.page_size`), not the CSS
  one, which shrinks with zoom (at 125% the lock was 800x442 at an offset over a 1000x553 page). `zoom` goes to the
  overlay for the agent's cursor, which is in CSS pixels.
- Under a device metrics override (`emulate_device`) Chrome draws the page at the top-left of the content area, 1 CSS
  px to 1 pt, clipped to the window, and the layout metrics give the emulated size, not where it is drawn. The
  centred guess put an iphone-15 page (393x852 in a 500x900 window) at (53.5, 48, 393, 852), 39 pt over the toolbar;
  it is at (0, 87, 393, 813) (Chrome 157). `emulate_device` reads the page's corner before the first override
  (`presence.before_device_override`) and records the override per tab (`presence._OVERRIDES`, forgotten with the
  tab and by the desktop preset); `page_place` puts the lock there. The cursor's zoom is then the emulated width over
  the CSS layout width: a page with no viewport meta tag lays out 980 px wide, zoom 0.401, and the cursor sat on the
  clicked button (measured). Limits: no overlay attached when the override began means no corner, so the centred
  guess stays; a toolbar change during the override is not seen until the desktop preset; after a resize during
  one the lock's width is wrong until the next window read (about a second).
- The agent's cursor is drawn only inside the page (`cursor_point` returns None outside, the edges inside). It is the
  agent's last point: after `set_viewport_size(390, 900)` a click made at x 1227 on the desktop layout was drawn 724
  pt outside the window, over another app. A click outside is taken without a ring, so it never rings later.
- Each tick reads the window's alpha (`window_alpha`): gone (a Quit) or under 0.5 (macOS fading it out on close, about
  0.3 s, still listed) hides at once; MISSING_FOR stays for a window still there but unseen on another Space (a
  minimized one goes sooner, see `MISSING_QUICK` below). Gone 0.13 s after a Quit (3.35 s before), 0.31 s after a
  window close, close animation included. Panels have no system animation (`setAnimationBehavior_(None)`); it shrank
  them for a third of a second.
- A window under 0.5 alpha is closing (`closing`) and is never followed again until gone or back at 0.95: it stays
  listed and on screen through its 0.3 s close animation, and the same tick took it back and showed the overlay over
  it again. Now last seen 0.03 s after the close, never again.
- A window gone from the screen with no Space change in the last `SPACE_GRACE` (1.5 s, from
  NSWorkspaceActiveSpaceDidChangeNotification) was minimized or hidden: the overlay goes after `MISSING_QUICK`
  (0.1 s). `MISSING_FOR` stays for a Space change, full screen included (its own Space), where the window is missing a
  moment and hiding would blink. Minimize: 0.003 to 0.04 s, and 0.42 s when macOS reports the window gone only at the
  end of its minimize animation (before: 0.55 to 0.62 s).
- A missing window must stay missing 0.6 s before anything hides, counted from the first scan that missed it; cached
  bounds stand in between scans only while the window is there (they used to reset the count every tick, keeping the
  light and lock 3.35 s after a Quit, until the process left; now about 1.1 s). The invisible lock goes at the first
  missed scan (about 0.5 s), so clicks where the window was are never swallowed.
- Ticks run at TICK_MOVING (60 a second, `schedule`) while the window moves, else 20 (at 20 the light trailed a drag).
  The first runs at start (the timer's first is skipped as calm); app switches and Space changes run one at once
  (NSWorkspace notifications). The start-time check of a Chrome whose window has been missing for 3 s (`chrome_start`,
  which runs `ps`) runs on a thread, so a tick that also hides the overlay never waits for a shell.

## Full screen

- A full-screen Chrome is several windows (tab strip and toolbar are windows of their own; the content one starts
  below the toolbar strip), so bounds alone do not show it. It is recognised from the browser's own windows
  (`full_screen(..., others=)`): one spanning the screen's width down to its bottom, under another browser window
  reaching from the top of the screen down to it, is the content view. The lock no longer waits for the server's
  `windowState` (0.6 to 0.8 s after the window settled): it is on the content view at 0.58 s, as Chrome's content
  window appears (0.67 s, the macOS animation), with no blink entering or leaving.
- `presence.refresh_window` sends CDP's `windowState` too, honoured only while the window spans its screen's width, so
  a stale word never squares a normal window. Full screen squares the corners and the light (a second mask, swapped
  without drawing).
- In full screen the lock is the content view and the light goes round the screen less its safe-area band
  (`screen_frame_of`: 32 pt under the camera housing, 0 on an external display; measured on both). Insets measured in
  full screen are not kept for the normal window. Panels are a `Panel` subclass AppKit does not constrain: it pushed
  the full-screen light's top 33 pt down, out of the menu bar's band.

## Window level and app switches

- The overlay takes the floating level (3) only when no switch can leave it over another app (`wants_floating`):
  nothing covers the browser, another app is in front (with the browser in front, level 0 just above its window looks
  the same and the next app covers both at once), and no app came to the front in the last `SWITCH_SETTLE` (0.3 s,
  before the window list shows it). Otherwise level 0 just above Chrome. Measured after: 0 ms in 16 of 16 switches.
- Why: at level 0 uncovered, clicking an inactive Chrome brought its windows over the overlay until the next tick;
  always floating left the overlay over the app a Cmd+Tab went to until a window scan saw it (182 to 257 ms with the
  browser in front, 199 to 231 ms, once 1003, from another app; an app window activated over the browser). A "Command
  not held" condition shipped in 0.25.14 and was taken out: it also dropped the floating level during a Cmd+Tab TO the
  browser, which then covered the overlay.
- Coming TO the browser raises its windows over a level-0 overlay before the window list changes. So `switched_` sets
  `raise_until` when the app that came to the front is the browser, and for `RAISE_FOR` (0.5 s) every tick, at the
  moving rate, orders the overlay back over the browser's window without asking the window list. Waiting for a scan
  had taken 241 to 266 ms on 0.25.15, 243 to 256 ms on 0.25.13 (another app's window over the browser, a click on its
  tab strip); now 0 ms in 8 of 8, and still 0 ms in 8 of 8 switches away.

## Drawing and cost

- No blinking: the bar keeps one width per window width (resizing its background flashed and moved it at every status
  change) and cross-fades its text.
- Cost against no overlay (macOS 26, 120 Hz display): WindowServer +35 points and the overlay 8 to 9% CPU for the
  first design, now about +10 points and 3%. What mattered: the light in four edge windows under a nine-slice mask
  drawn once (no live blur, which three blurred rings were); a capped animation rate (`preferredFrameRateRange`); no
  behind-window blur on the bar (it re-blurred the moving light every frame); reading only the windows above the
  browser's, four times a second (each CGWindowList value crosses the PyObjC bridge: the full list took 1.2 ms, the
  per-tick scan most of the CPU); updates at 20 Hz (WindowServer's cost follows the number of distinct frames more
  than their area).
- The light's rate is a trade, against no overlay: about +5 points at 20 fps, +10 at 30, +19 at 60, the overlay 2.5 to
  3.5% in all three. At 20 fps a 9 s turn moved a window corner 25 points a frame and the soft colours stepped
  visibly, so it runs at 30 fps with a 16 s turn (about 9 points a frame), turns while dimmed, and stops only once
  faded out. Pausing it while other windows cover the browser changed nothing (WindowServer skips them already) and is
  not done. The agent's cursor costs about +18 points while it moves, in any window size, so it is off by default.
- Corners follow the window's (`corner_shape`, `corner_path`). macOS 26 draws a continuous corner, a superellipse: on
  26.6.1, Chrome and Brave Beta alike fit n=4 over a 37 pt box within 0.31 pt rms; the best circle (21 pt) was 0.86 pt
  off. The old 12 pt circle stood up to 15 pt off the corner (rms 3.75 pt); now rms 0.45 pt, worst 2 pt. Earlier
  macOS: a 10 pt circle. `MCP_STEALTH_OVERLAY_CORNER` ("<box>" or "<box>,<n>") overrides.
- The light reaches 64 pt in as one smooth curve (`light_at`: a crisp edge, a short halo, a long body exactly 0 at
  DEPTH), drawn once as nested frames whose alphas add up to it (`ring_image`), so no seam shows. STRIP is DEPTH + 8.
  The 0.25.4 draft's four blurred stroked layers showed as separate bands, and its widest blur ran past the 96 pt
  strips into a straight line with a square corner inside the window. Against the 34 pt light (light on): the overlay
  1.7 to 2.0% either way, WindowServer within its noise.
- The light breathes between THIN (32 pt) and DEPTH, BREATHE (4 s) each way (asked for; it replaced an opacity pulse
  of 0.82 to 1). The mask is two layers drawn once, the thin ring (`thin_at`) and what the full light adds
  (`extra_at`; thin over extra at full strength is exactly `light_at`); only the extra layer's opacity animates, on
  the GPU. `extra_at` rises from the edge and falls, so `bands` draws each profile as nested bands, one per level,
  none meeting another edge to edge (no seam). The 64 pt image is unchanged; thin over extra differs by at most 3%
  alpha at a pixel (8-bit rounding of two images). Animations inside a mask stop with the masked layer's pause
  (measured), so a faded light costs nothing. Against the old light, 3 runs each, alternated: WindowServer 26.7%
  against 27.2% (noise), the overlay 2.2 to 2.4% against 2.0 to 2.3%.
- A faded-out light takes its four windows off the screen (`Glow.out`; `back` restacks the overlay when it turns on
  again): they had stayed listed, transparent, while the overlay was idle.

## The person's settings

- `presence.style()` hands the person's `overlay_bar`, `overlay_light` and `overlay_lock` settings to the overlay: bar
  full, compact (30 pt bar, first words of the status, no cursor switch), hover (shown while the pointer is over the
  visible browser window, `HOVER_LINGER` after) or off; light and lock on or off. `server_status.agent_overlay.style`
  reports it. The overlay re-reads the settings file about once a second (a stat) and applies a change at once.
- Compact rebuilds the bar at the overlay's current level: a new panel starts at the normal level, under the floating
  lock, which then took its clicks.
- The bar's **⋯** menu writes the file as `settings.write` does; a setting fixed by the server's environment comes in
  `fixed`, wins there too, and its menu item is disabled. The tick timer runs in the common run-loop modes, or it
  stopped while the menu was open and a browser closed meanwhile left the overlay standing.

## Tests

- `tests/test_agent_overlay.py` (fast) and `tests/test_agent_overlay_e2e.py` (`MCP_HEADED=1`, real Chrome, through the
  MCP boundary): the edge strips, refusals while held, Resume and Done ending `wait_for_human`, the typing pause, the
  overlay still in front after `request_human_help` brings Chrome forward, the overlay ending with the browser.
