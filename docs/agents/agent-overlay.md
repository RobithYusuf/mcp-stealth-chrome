# Agent overlay (agent_overlay)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- Per launch (`browser_launch`, `spawn_browser`, both reattaches), off unless asked or the `agent_overlay` setting is on; macOS,
  headed, PyObjC from the `os-cursor` extra (found with `find_spec`, nothing imported). Headless, a browser the person opened
  from the launcher (a lock over their window would keep them from their own tabs) or no known Chrome PID: the result says
  OFF and why. `tests/conftest.py` sets it off and `MCP_STEALTH_OVERLAY_DIR` to a temporary folder for every server.
- The overlay is its own process (`python -I -`, source on stdin, like the quit watcher, but with `sys.executable`: it
  needs the venv's PyObjC). It shares two 0600 files in a 0700 folder: `agent.json` (the server writes: status, active_at,
  help, the agent's tab's window bounds, the page's place in it, the pointer in page coordinates) and `control.json` (the
  overlay writes: holder, since, reason, person_last_input, help_done, show_cursor). It exits on `ended`, when Chrome's PID
  is gone, or when its parent is no longer the server (`getppid`). `_ensure_browser_dead` stops it first, `_stop_all_browsers`
  stops them all. SIGUSR1 toggles the cursor and SIGUSR2 presses the bar's button: that is how tests press it.
- Only the bar's button changes who holds the browser. A click or scroll on the page while the agent works lands on the lock
  (a fully transparent window over the viewport; it takes clicks only because `ignoresMouseEvents` is set to False
  explicitly, checked with `windowNumberAtPoint`) and the bar says the page is locked. A key cannot be held back without
  taking the keyboard from the browser: the overlay records it, `presence.before_input` (in `_dispatch_input`,
  `_type_keys`, `move_pointer`) waits until 1.5 s of quiet (10 s at most) and the result says the person's keys reached the
  page. Enter can still act on the page first; that is the stated limit.
- While the person holds it, `_app.CALL_GATES` refuses every tool not in `presence.WHILE_PERSON_HOLDS`, reading tools
  included (they may be typing a password), naming Resume agent and `wait_for_human`. A gate runs for every tool, also those
  in `_RUNS_DESPITE_DIALOG` (tab_list reads titles). Mid-tool, `before_input` raises `PersonHasBrowser`. `wait_for_human`
  ends on Done or on Resume agent pressed after it was asked, and ignores page changes while the person holds the browser:
  their own clicks change the page.
- Clicks reach the overlay's click ring through one wrapper on nodriver's `Tab.mouse_click` (`patches.
  _patch_mouse_click_reports_pointer`), since sixteen places press through it; it also runs `before_input`. So do every
  step of a drag (the button is still let go in its `finally`), every chunk of `humanized_scroll` (outside its retry, which
  would send the wheel event again, and once more before the retry and the JS `scrollBy` fallback), `scroll`'s instant
  wheel and its top and bottom, `scroll_to`, every JS click fallback, and each step of `batch_actions` and `workflow_run`
  (`presence.refusal`). `fill`'s fallback to a JS value re-raises `PersonHasBrowser` instead of writing the value anyway.
- Fails closed (from a review of the first version): `refusal` and `before_input` act on the overlay's handle whether its
  process runs or not. A dead one is started again (`revive`, at most `RESTARTS` 3, only while Chrome's PID still runs the process
  with the start time recorded at attach: a PID is handed out again) and
  keeps who held the browser, since the new process reads `control.json` first. Held with no overlay to show Resume agent,
  every tool is refused except `browser_launch(agent_overlay=False)`, which stops it: the one way out, said in the refusal.
  `attach` is async and says on only once the process wrote its own PID into `control.json` after its windows exist
  (`READY_WAIT` 3 s); otherwise it stops the process and says OFF, naming the last line of `overlay.log` when it died. `agent_overlay=False` on a browser that has one stops it.
- Stopping: the stdio server ignores SIGTERM and children inherit that, so `_agent_overlay.py` resets it to the default,
  and `Overlay.stop` follows `ended` with terminate and then kill, removing the folder only once the process is gone.
  `_safe_stop_browser` stops it too, for launches that fail after it started. A process left by a killed server removes
  its own folder when it sees the browser or the server gone, on `ended`, or when its PID's start time is no longer the
  one it was given. Folder names carry the PID and its generation (`chrome-<pid>-<generation>-*`); `attach` prunes those
  whose PID is gone or runs a process of another generation. A process whose code could not be written to it is killed. State files
  are read with `O_NOFOLLOW` and only as regular files, the log is created 0600, and a state root that is a link is refused.
- The handoff bar outlives quiet: help and the person's hold are chosen before `HIDE_AFTER`, so Done and Resume agent never
  fade out while they are the only way back. `MCP_STEALTH_OVERLAY_HIDE_AFTER` sets the quiet time (tests use 3 s).
- Window identity is by bounds: CDP's window id is not a CGWindowID, and window titles need Screen Recording. Two windows of
  the browser at exactly the same rect cannot be told apart; the overlay follows the front one. Coordinates are right on
  every display: CG and AppKit global coordinates both start at the main display (`screens()[0]`), so its height converts
  them; a review that called this wrong for other monitors was checked against that.
- No blinking, each from a cause seen in the prototype: the bar keeps one width per window width (resizing its background
  flashed and moved it on every status change) and cross-fades its text; the light dims between tool calls instead of going
  out (`QUIET_AFTER` 2.5 s, to 70%: half read as switched off; out after `HIDE_AFTER` 90 s); a missing window must
  stay missing 0.6 s before anything hides, counted from the first scan that missed it: the cached bounds stand in between
  scans only while the window is there (they used to reset the count every tick, so after a Quit the light and the lock
  stayed 3.35 s, until the process left; now about 1.1 s). The lock, being invisible, goes at the first missed scan
  (about 0.5 s), so clicks where the window was are never swallowed;
  the window followed is kept while on screen (a Chrome popup over 300x200 moved the overlay); and while no other window
  covers the browser the overlay sits in the floating level (3). At level 0 just above Chrome, clicking an inactive Chrome
  brought its windows over the overlay until the next tick. Covered, it drops to level 0 just above Chrome. App switches
  and Space changes run a tick at once (NSWorkspace notifications).
- The floating level also held the overlay over the app a Cmd+Tab went to until a window scan saw it: 182 to 257 ms with
  the browser in front, 199 to 231 ms (once 1003) from another app, measured with an app window activated over the
  browser. So it floats only when no switch can leave it there (`wants_floating`): nothing covers the browser, another
  app is in front (the browser in front: level 0 just above its window looks the same, and the next app covers both
  at once) and no app came to the front in the last `SWITCH_SETTLE` (0.3 s, before the window list shows it).
  Measured after: 0 ms in 16 of 16 switches. A third condition, Command not held, shipped in 0.25.14 and was taken
  out: it dropped the floating level during a Cmd+Tab TO the browser as well, which then covered the overlay.
- Coming TO the browser, its windows are raised over an overlay at level 0, and the overlay came back only when a
  window scan saw it (the list is not yet changed when the activation notice arrives): 241 to 266 ms under the
  browser's window on 0.25.15, 243 to 256 ms on 0.25.13, measured with another app's window over the browser and a
  click on its tab strip (the person: "the tab is there, the overlay comes late"). `switched_` now sets `raise_until`
  when the app that came to the front is the browser, and for `RAISE_FOR` (0.5 s) every tick, at the moving rate,
  orders the overlay back over the browser's window without asking the window list. Measured after: 0 ms in 8 of 8,
  and still 0 ms in 8 of 8 switches away.
- Cost, measured with a benchmark against no overlay at all (macOS 26, 120 Hz display, 15 s runs alternated): WindowServer
  +35 points and the overlay 8 to 9% CPU for the first design, about +10 points and 3% after. What mattered: the light in
  four edge windows under a nine-slice mask drawn once (no live blur, which three blurred rings were), animations asked
  for a capped rate (`preferredFrameRateRange`), the bar without a behind-window blur (it re-blurred the moving light every frame),
  reading only the windows above the browser's four times a second (each value read from a CGWindowList dictionary goes
  through the PyObjC bridge: the full list was 1.2 ms and the per-tick scan most of the CPU), and updating at 20 Hz (WindowServer's
  cost follows the number of distinct frames more than their area). The light's rate is a trade, measured against no
  overlay: about +5 points at 20 fps, +10 at 30 and +19 at 60, the overlay process 2.5 to 3.5% in all three. At 20 fps a
  9 s turn moved a window corner 25 points a frame and the soft colours stepped visibly, so it runs at 30 fps with a 16 s
  turn (about 9 points a frame), keeps turning while dimmed and stops only once it has faded out. The agent's cursor costs about
  +18 points while it moves, in a small window or a full one alike, so it is off by default. Pausing the light while other
  windows cover the browser made no difference (WindowServer skips them already) and is not done.
- The light's corners follow the window's (`corner_shape`, `corner_path`). macOS 26 draws windows with a continuous
  corner, a superellipse: measured from the window's own image (alpha) on 26.6.1, Chrome and Brave Beta alike fit n=4
  over a 37 pt box within 0.31 pt rms; the best circle (21 pt) was 0.86 pt off. With the old 12 pt circle the light's
  edge stood up to 15 pt off the window's corner (rms 3.75 pt); now rms 0.45 pt, worst 2 pt. Earlier macOS: a 10 pt
  circle. `MCP_STEALTH_OVERLAY_CORNER` ("<box>" or "<box>,<n>") overrides. In full screen the corners are square and
  so is the light (a second mask, swapped without drawing): a full-screen Chrome is several windows (the content one
  starts below the toolbar strip), so the bounds alone do not say it; `presence.refresh_window` sends CDP's
  `windowState`, honoured only while the window spans its screen's width, so a stale word never squares a normal window.
- The light reaches 64 pt into the window as one smooth curve (`light_at`: a crisp edge, a short halo, a long body
  that is exactly 0 at DEPTH), drawn once as nested frames whose alphas add up to it (`ring_image`), so no seam shows.
  The 0.25.4 draft blurred four stroked layers with shadows: the person saw separate bands, and the widest blur ran past
  the 96 pt strips and ended in a straight line with a square corner inside the window. STRIP is DEPTH + 8. Cost,
  measured against the 34 pt light for 15 s with the light on: the overlay process 1.7 to 2.0% either way,
  WindowServer within its own noise.
- The light breathes between THIN (32 pt) and DEPTH, BREATHE (4 s) each way (asked for, 21 Nov 2026; it replaced an
  opacity pulse of 0.82 to 1). The mask is two layers drawn once: the thin ring (`thin_at`) and what the full light adds
  over it (`extra_at`, so thin over extra at full strength is exactly `light_at`), and only the extra layer's opacity is
  animated, on the GPU. `extra_at` rises from the edge and falls, so `bands` draws every profile as nested bands, one
  per level, none meeting another edge to edge (no seam); the 64 pt image is identical to the one before, and thin
  over extra differs by at most 3% alpha at a pixel (8-bit rounding of two images). Animations inside a mask stop
  with the masked layer's pause (measured), so a faded light costs nothing. Cost against the old light, 3 runs each,
  alternated: WindowServer 26.7% against 27.2% (noise), the overlay process 2.2 to 2.4% against 2.0 to 2.3%.
- The overlay goes when the window goes. Each tick reads the browser window's alpha (`window_alpha`): gone (a Quit)
  or under 0.5 (macOS fading it out as it closes, about 0.3 s, still listed meanwhile) hides at once; MISSING_FOR stays
  for a window still there but unseen (another Space, a minimize). Panels have no system animation
  (`setAnimationBehavior_(None)`), which shrank them for a third of a second. Measured: gone 0.13 s after a Quit
  (3.35 s before), 0.31 s after a window close, with the close animation. While the window moves the tick runs at
  TICK_MOVING (60 a second, `schedule`), 20 otherwise: at 20 the light trailed a drag.
- The lock follows the page, not the last measurement (OMP review and measurements, 20 Nov 2026). The server reads the
  window and the page (`refresh_window`) before each call, right after each call (`after_tool` starts it, so a launch has
  its lock at once; before, none until the next call), and about once a second while the overlay shows
  (`_follow_window`; not while the person holds the browser or after the overlay faded), and writes only what changed.
  The overlay applies the page's insets from its window to the window's live frame every tick (`page_rect`): a window
  widened from 1000 to 1250 pt between calls left the right 250 pt unlocked; now the lock follows 0.001 s after the light.
  In full screen the window followed is Chrome's content view (the tab strip and toolbar are windows of their own), so
  the lock is that window and the light goes round the screen less its safe-area band (`screen_frame_of`: 32 pt under
  the camera housing, 0 on an external display; measured on both). Insets measured in full screen are not kept for the
  normal window. Panels are a `Panel` subclass that AppKit does not constrain: it pushed the full-screen light's top
  33 pt down, out of the menu bar's band.
- The page is measured in screen points from the device-pixel `layoutViewport` (`presence.page_size`), not the CSS
  one: CSS pixels shrink with the page's zoom, and at 125% the lock was 800x442 at an offset over a 1000x553 page.
  `zoom` goes to the overlay for the agent's cursor, which is in CSS pixels.
- The followed window is kept when the person moves or resizes it; another window is followed only when it matches the
  server's last bounds, checked once per new measurement (`want_seen`). Dropping it on any mismatch jumped to the
  largest window of the browser and read every window on screen four times a second; minimize hid in 0.55 s that way,
  now 0.02 s. Between scans the followed window's own bounds are read every tick (`followed_bounds`: one window 0.12 ms
  against 0.47 ms for all on screen), so a drag or resize is followed in 0.03 to 0.12 s instead of the next scan.
- A call in flight is not quiet: `before_tool` marks the overlay busy (`in_flight`, and `_CALL_OVERLAY` carries that
  overlay to the same call's `after_tool` whatever instance is current by then), and the overlay counts no quiet time
  while `busy`. A call longer than `HIDE_AFTER` (a long wait_for) took the light, the bar and the lock away mid-call.
- An agent that stopped calling tools left the lock up for HIDE_AFTER (90 s), so a person whose agent had finished had
  to press Take over to use their own page, and no agent heard it (20 Nov 2026). A working agent's steps came at most
  14.7 s apart, median 4.3 s (60 calls of a research run). Past IDLE_AFTER (20 s, `MCP_STEALTH_OVERLAY_IDLE_AFTER`)
  with no call `choose_mode` says `resting`: no lock, the light out, the bar "Agent idle" with Take over. What the
  person clicks or scrolls over the browser, or types with it in front, while it rests or the overlay has faded is
  written to control.json (`person_used`, `person_used_at`) and the agent's next call carries it once (`_pass_on`),
  so it reads the page again. The real clicks were measured on a test browser: lock off at idle, the page took the
  click, the next call named it, the lock came back with that call.
- The bar tells the person what the agent knows of their Take over (`told`): not yet ("learns it at its next step"),
  told (a refusal or a stopped input writes `told_at`), going on without the browser (a tool in `presence.OUTSIDE`
  while they hold it writes `outside_at`, and its result says they hold it), or waiting (`waiting_human`, set while
  `wait_for_human` runs). Before, the bar said "The agent is paused" whatever the agent was doing.
- A note line above the bar (0.25.15 to 0.25.17: a text field for the person, sent with Resume agent or Done) was
  taken out at the person's request (21 Nov 2026): it made the bar busy, and they talk to the agent in its chat. The
  bar carries buttons only. Do not add a text field to it again without asking.
- A request the person answered with Done is `noted` (`choose_mode(answered=)`: `help_done` at or after the request's
  `asked_at`) until a call of the agent takes it. Before, the bar went on asking for help with its Done button and the
  note line stayed, holding the keyboard, and nothing told an agent that read the page instead of calling
  `wait_for_human`. Now the bar says "Done, the agent goes on at its next step", and the first result of any tool says
  the person pressed Done and closes the request (`_done_pressed` in `result_note`); `wait_for_human` started
  afterwards returns at once (0.0 s). The headed test presses Done by signal and reads the windows.
- A window whose alpha falls under 0.5 is closing and is never followed again until it is gone or back at 0.95
  (`closing`): it stays listed and on screen for its 0.3 s close animation, and the same tick took it back and showed
  the overlay over it again. Measured: last seen 0.03 s after the close, never again.
- The first tick runs as the overlay starts (the timer's first one is skipped as calm).
- Full screen is recognised from the browser's own windows too (`full_screen(..., others=)`): a window spanning the
  screen's width down to its bottom, with another window of the browser above it from the top of the screen down to
  it, is a full-screen content view. The lock no longer waits for the server's `windowState` (0.6 to 0.8 s after the
  window settled): measured, it is on the content view at 0.58 s, as Chrome's content window appears (0.67 s, the
  macOS animation), with no blink entering or leaving.
- A window gone from the screen with no Space change in the last `SPACE_GRACE` (1.5 s, from
  NSWorkspaceActiveSpaceDidChangeNotification) was minimized or hidden: the overlay goes after `MISSING_QUICK` (0.1 s).
  `MISSING_FOR` stays for a Space change, full screen included (a Space of its own), where the window is missing for a
  moment and hiding would blink. Minimize measured 0.003 to 0.04 s, and 0.42 s when macOS reports the window gone only
  at the end of its minimize animation (before: 0.55 to 0.62 s).
- A light that has faded out takes its four windows off the screen (`Glow.out`; `back` makes the overlay restack when
  it turns on again): they stayed listed, transparent, while the overlay was idle.
- The start-time check of a Chrome whose window has been missing for 3 s (`chrome_start`, which runs `ps`) runs on a
  thread, so the tick that also hides the overlay never waits for a shell.
- `presence.style()` reads the person's choice (the `overlay_bar`, `overlay_light` and `overlay_lock` settings) and passes
  it to the overlay: bar full, compact (30 pt bar, first words of the status, no cursor switch), hover (shown while the
  pointer is over the visible browser window, `HOVER_LINGER` after) or off; light and lock on/off. The overlay reads the
  settings file again about once a second (a stat) and applies a change at once; compact rebuilds the bar at the
  overlay's current level (a new panel starts at the normal level, under the floating lock, which then took its clicks).
  The bar's **⋯** menu writes the file the way `settings.write` does; a setting the server's environment fixed is
  passed in `fixed`, wins there too, and its menu item is disabled. The tick timer runs in the common run-loop modes,
  or it stopped while the menu was open and a browser closed meanwhile left the overlay standing.
  The bar's button is the only way to take over, so help (Done) and the person's hold (Resume agent) always show it,
  and a bar that is off turns the lock off: a lock with no Take over in reach would shut the person out of the page.
  `server_status.agent_overlay.style` reports it.
- `tests/test_agent_overlay.py` (fast) and `tests/test_agent_overlay_e2e.py` (`MCP_HEADED=1`, real Chrome, through the MCP
  boundary): the strips on the window's edges, refusals while held, Resume and Done ending `wait_for_human`, the typing
  pause, the overlay still in front after `request_human_help` brings Chrome forward, and the overlay ending with the browser.
