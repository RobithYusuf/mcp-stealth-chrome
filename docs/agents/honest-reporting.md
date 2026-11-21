# Honest reporting

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

This is the spine of the project. A tool that reports a rosier state than reality is the worst
failure mode for an agent: the caller cannot tell, so a wrong "success" spreads into wrong
conclusions. Each rule below replaced a real lie.

## Before a click

- A click result must reflect what happened. `click`, `click_text` and `click_role` hit-test the target first
  (`elementFromPoint` on its own root node: a shadow root answers for itself, `document` stops at the host) and refuse,
  naming the blocker, if the point belongs to something else.
- An empty `_click_probe` is an unanswered question, not a pass: `_blocked_reason({})` refuses. `humanize=True` presses
  a random spot rather than the tested centre, so that spot gets its own hit test first.
- Never call nodriver's `Element.click()`: it is a JS `el.click()` (`isTrusted=false`) after `flash()`, which inserts a
  node into the page. `_click_and_report` sends real input at the settled position; a JS click happens only with
  `js_fallback=True`, and the result says so.
- Hit-test only once the element stops moving: `scroll_to` is smooth by default, and a target measured mid-scroll
  passes, then is clicked where it no longer is.
- `hover`, `click_element_offset` and `click_at_corner` use `_prepare_target` like a click; without it they reported
  `hovered` and `clicked` outside the viewport and on covered elements.
- `mouse_click_xy` does not refuse a covered point (coordinates are the caller's choice) but reports what is there just
  before the press, from the isolated world (`_what_is_at`): tag, id, name, some text; for an iframe, the URL of the
  frame it shows (`DOM.describeNode` frameId matched in `all_frames`, so a form-posted frame with no `src` is named) and
  the point inside it. Frame URLs are navigation URLs, not masked. A 25 px layout shift once sent a click into the next
  field, reported only as "clicked (x,y)".
- Refusing is not enough (on github.com the first match for "Issues" is a hidden marketing `<span>`): before giving up,
  `click_text` looks for another reachable element with the same text, scrolling it into view if that is all it needs.
  The result says another element was clicked ("the first text match was unreachable (why), so a reachable element with
  the same text was clicked"), except when it was the first match, only out of view, and the one reachable element found
  (out-of-view ones count): then just "(scrolled into view)", since the old "used a visible one of 1 candidates" read as
  a different element. `found` counts reachable candidates only: a hidden decoy plus the real control is found == 1 and
  still needs explaining.

## After a click

- With `verify=True` (default) a fingerprint before and after decides; no change says
  `clicked but NO page change detected`. It includes the element's own tree and state (else an iframe, shadow tree or
  checkbox change reads as none) and `_page_sig` hashes the body HTML (by length alone, a counter going from 1 to 2 read
  as `NO page change` and an agent clicked again).
- Focus counts only when it lands on another element: a real click focuses its own target, and clicking anything
  unfocusable leaves focus on `<body>`.
- `_click_outcome` runs only with `verify=True`; an empty fingerprint on either side (`_page_sig` gives `{}` for an
  unreadable page) says `the outcome could NOT be verified`, never a bare `clicked`.
- A click that opens a tab changes nothing on its own page: `_page_sig(pages=True)` lists the tabs and `_click_outcome`
  names the new one, or a working popup button reads as dead and gets clicked again.
- Reads come at 0, 50, 120, 220, 330 and 450 ms (`_VERIFY_PAUSES`) and stop at the first that differs, plus one 60 ms
  later to name a navigation or new tab that follows. Handlers change the page before the click is acknowledged, so the
  old fixed 350 ms was wasted on every working click. Only "NO page change" waits the whole time, longer than before on
  purpose (a handler reacting after 300 ms sat at the old edge): do not shorten the last pause to speed up dead clicks.
- `_page_sig` reads page and element in two calls, and an Enter-submitted form can replace the document in between: old
  URL, empty element half. `press_key` called that "the focused element changed" on macOS CI (37 of 100 presses when
  forced locally). An empty element half over an unchanged page is no answer yet (`key_outcome.element_unread`; `active`
  left out, since a blur before submit leaves focus on `<body>`); the next read shows the navigation.
  `_sig_after_action` applies the same rule within `_VERIFY_PAUSES`; a click still torn at the end says the element
  could not be read. `key_outcome.watch` ends at `settle_ms` plus `READ_LIMIT` (5 s) even if a read never answers,
  calling the effect unknown; no change still waits `settle_ms`.

## Isolated worlds

- The hit test, fingerprints and challenge look run in the frame's isolated world (`refs.apply_isolated`,
  `refs.evaluate_isolated`): in the page's world a page can replace and watch `getBoundingClientRect`,
  `elementFromPoint`, `getComputedStyle` or `querySelector` (one click made 37 such calls). The DOM is shared, so
  answers match; page globals are not (`window.turnstile` is undefined there). Both helpers fall back to the page's
  world when the isolated one is unusable. New DOM-only probes go there too.
- A kept context usually fails once after a navigation and is remade, but context ids are unique only per renderer
  process: after a cross-site navigation the kept id named an about:blank ad iframe, navigate said
  `final_url: about:blank` for a loaded page (8 of 8 cross-site runs; same-site 0 of 9), and the fingerprint and
  `_what_is_at` read that iframe. So each world `refs` makes holds a random mark in its global (`_new_world`), and
  scripts in a kept one go through `refs.guard` or `guard_function`, which throw `STALE_WORLD` without the mark so the
  caller remakes the world. A new caller of `_world_context` must do the same.

## Dialogs

- A native dialog blocks the renderer, so input is never acknowledged (a click on an `alert()` button hung forever).
  `_dispatch_input`, used by pointer moves too (`onmousemove` can open one before any press), stops waiting once
  `_watch_dialogs` sees a dialog, and names it. The watch is armed before input, since `Page.enable` needs the renderer
  too; `dialog_handle` answers one already open.
- Giving up the wait leaves the gesture's task running (a press blocked on mousedown must still release) but marks it
  stopped (`humanize.CURRENT_GESTURE`, bound in that task only), so `move_pointer` and `check_gesture` send nothing
  more. Before, 7 to 11 leftover moves reached the next page after the tool returned and opened its alert under another
  tool, which the guard in `_app` then stopped (now and then, macOS and Windows runners). A dialog from the path means
  `NOT clicked`: path and press are separate input (`humanize_click` said "clicked" with its press queued behind the
  dialog). Chrome drops input sent while a dialog is open.
- Anything else needing the renderer waits forever under a dialog, timeout or not: `_app._run_unless_a_dialog_blocks`
  refuses or stops it and names the dialog. A new tool that does not touch the page goes in `_RUNS_DESPITE_DIALOG`. A
  dialog counts only once `_watch_dialogs` armed that tab, so one on a tab no tool touched is unseen.
- `Transaction` in nodriver up to 0.48 is a Future: a reply after `wait_for` cancelled it raised inside the CDP listener
  and killed it, hanging every later call on that tab. `patches._patch_transaction_late_reply` closes that.

## Empty, failed or not measured

- An empty result must differ from "not measured": `console_get` returns
  `{attached, capturing, since, total, messages}`, `intercept_log` reports `active` and `stop_reason`, and
  `storage_snapshot` marks each area `measured` and lists failures in `errors` (a snapshot that silently failed on
  cookies makes `storage_diff` blame the action under test).
- Absence of evidence is not evidence: `_ensure_browser_dead()` sets `verified` only when a process was positively
  observed gone; with no PID and no profile to scan it stays false, and no clean close may be claimed.
  `_close_incomplete` does not count that as a failure, so every result built on such a report adds `_kill_note`
  ("stop was requested, but no process could be checked to confirm it ended"): `browser_close`, and the failure
  paths of `browser_launch` after the start, where the cookie path says "asked to stop", not "was closed". A kept
  handle's note says to call browser_close to try again, never that it will end the browser.
- `fill_form` fails when a field fails, and then does not click submit. `batch_actions` counts failures by the `Error:`
  prefix, so a failure reported through `ok()` counts as success. Non-list or empty input is an error, not
  `0 actions OK`; a non-object item is a counted failure.
- `server_status` measures `os_cursor` on a thread started with the server and says `still being measured`
  (`available: null`) until done: a cold Quartz and AppKit import takes seconds while profile_gc and the Dock check run,
  and a timed wait answered `not measured (TimeoutError)` on the first call.
- `server_status` exposes `compat_patches`: a patch that fails to apply crashes nothing but kills the CDP listener at
  the first mismatched payload, so captures come back empty and look like a quiet page.
