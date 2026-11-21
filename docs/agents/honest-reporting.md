# Honest reporting

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

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
  The result says another element was clicked ("the first text match was unreachable (why), so a reachable element
  with the same text was clicked") unless it was the first match itself: only out of view, and the one reachable
  element found (an element only out of view counts among them). Then "(scrolled into view)" alone; the old
  "used a visible one of 1 candidates" read as a different element. `found` counts reachable candidates, not every
  element with the text: a hidden decoy plus the real control is found == 1 and must still be explained.
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
- A context id is unique only within one renderer process, so a kept one does not always fail. A cross-site
  navigation moves the tab to a new process that numbers its contexts again, and the id kept for the tab named an
  about:blank ad iframe there: navigate said `final_url: about:blank` for a loaded page (8 of 8 runs from CNBC to a
  Kompas article; same-site 0 of 9), and the page fingerprint and `_what_is_at` read that iframe too. Every world
  `refs` makes holds a random mark in its own global (`_new_world`), and a script run in a kept one goes through
  `refs.guard` or `guard_function`, which throws `STALE_WORLD` when the mark is not there; the caller then makes the
  world again. A new caller of `_world_context` must do the same.
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
- `mouse_click_xy` takes coordinates as an explicit choice and does not refuse a covered point, but it reads what
  is there just before the press, in the isolated world (`_what_is_at`): tag, id, name, a little text, and for an
  iframe the URL of the frame it shows (`DOM.describeNode` frameId matched in `all_frames`, so a form-posted frame
  with no `src` is named) and the point inside it. Frame URLs are navigation URLs and are not masked. A layout that
  moved 25 px sent a click into the next field while the result said only "clicked (x,y)".
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
