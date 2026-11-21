# Ad blocking (block_ads)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- `browser_launch(block_ads=True)` and `spawn_browser(block_ads=True)` block requests that EasyList and EasyPrivacy
  match, with Brave's adblock-rust (the `adblock` package, capped `<0.7` because the stored engine is its format).
  It is the optional extra `adblock`, never a dependency: 0.6.0 has no wheel for musllinux or Windows ARM64, and a
  build from its sdist needs Rust, so as a dependency it made the whole package fail to install there, block_ads used
  or not. Without it, `block_ads=True` launches with `block_ads: OFF` and the install command (`helpers.ADBLOCK_MISSING`),
  and `server_status.block_ads.package` says the same (found with `find_spec`, nothing imported). The fast CI job
  installs no extra and the tests that need the engine skip (`needs_engine`); the e2e job installs it and runs them.
  `installed_smoke.py` checks a plain install has no adblock and still launches, and checks the engine with the extra.
  Network rules only: cosmetic rules hide elements, which writes to the page.
- The lists are GPLv3 / CC BY-SA and are never bundled. `adblock.py` downloads them on first use into
  `~/.mcp-stealth/adblock` (`MCP_STEALTH_ADBLOCK_DIR`; files 0600, folder 0700), over https only (a redirect to
  anything else is refused), checks them again at most once a day with a conditional request in a background
  thread, and works offline from the last copy. A download that is not an Adblock Plus list (a captive portal
  answers 200) never replaces a copy. Each download is capped in bytes (Content-Length and the body,
  `MCP_STEALTH_ADBLOCK_MAX_BYTES`, 16 MB) and in total time, not per socket read (`MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT`,
  20 s): `urlopen(timeout=)` is per read, so a server sending a byte at a time kept it going for ever, and the socket
  timeout follows what is left of the deadline before each read, or one read started near the end ran past it. No
  lock the launch needs is held over the network. `tests/conftest.py` points `MCP_STEALTH_ADBLOCK_DIR` at a temporary folder.
- A list name becomes a file name, so it must match `^[a-z0-9][a-z0-9_-]{0,63}$` and its path must sit directly in
  the folder: `MCP_STEALTH_ADBLOCK_LIST_URLS="../../escape=..."` wrote outside it, and an absolute name replaced the
  root (`Path / "/abs"`). An invalid name turns block_ads OFF with the reason.
- No list, no `adblock` package or a failed start is `block_ads: OFF, <why>` in the launch result, never a failed
  launch. Some lists missing is `block_ads: PARTIAL`, naming them and why, and `coverage: partial` in server_status:
  "on" with EasyPrivacy missing let every tracker through while reading as full blocking.
- The engine is stored serialized (`engine.dat`, keyed by the lists' sha256, the package version and
  `ENGINE_SCHEMA`), and handed to the native parser only when its size is under `MCP_STEALTH_ADBLOCK_MAX_ENGINE_BYTES`
  and its bytes match the sha256 written beside it; anything else is rebuilt. Measured with the real lists (109,487
  network rules): 32 to 46 ms to load against about 210 ms to build.
- Blocking has its own CDP connection to the browser endpoint, on its own thread and event loop
  (`mcp-stealth-adblock`), which owns every piece of blocker state: sessions, the frame map, the engine and
  intercept_start's rule run there only. What other threads read (counts, end, the waiting maps) is behind the
  blocker's lock, and the registry behind `_REGISTRY_LOCK`. Its reader never awaits a reply while handling an event:
  a handler awaiting a reply only the reader can read deadlocks, which the first draft did.
- Fail open, never closed. A watchdog thread, independent of the adblock thread and armed before the blocker
  starts (a start that hangs the adblock thread is caught like anything later), ends a blocker when the adblock
  thread is gone, its loop stopped or missed its heartbeat for `MCP_STEALTH_ADBLOCK_STALL` (3 s), or a paused request
  or a held target waited that long. It disables Fetch over the connection when the loop still answers, shuts the
  socket, and then starts every target over a new connection (`_release_every_target`: attach,
  `Runtime.runIfWaitingForDebugger`, detach; scanned again until a pass finds nothing new, so a popup that opens
  meanwhile is caught). The release runs on a thread of its own joined with its budget, so a connect, send, receive
  or close that never returns cannot hold the caller; when the budget runs out, the end reason names how many targets
  were not released and their ids, or says which are left is unknown when no target list was read. Every end of the blocker's
  connection runs that release exactly once (`_end` decides who): the watchdog, a clean EOF or a transport error
  while the browser still answers on its debugging port, and the server's own exit (`adblock.shutdown()`, first thing
  in `_stop_all_browsers`, every blocker released side by side within `SHUTDOWN_BUDGET`, 4 s in all, so the server's
  exit is never held). The next tool result of that session says block_ads stopped and why
  (`_app.RESULT_FILTERS`; a JSON object gets a `block_ads` key, so it still parses).
- What was measured on Chrome 157, so it is not argued again:

  | Question | Measured | Consequence |
  |---|---|---|
  | Does a client that goes away release a target it held for the debugger? | Paused requests: yes. A held `window.open` popup: no, while the server lives; it stayed frozen, and its opener with it, after the blocker's socket was shut | Every end path releases over a new connection |
  | Does SIGKILL of the whole server leave anything held? | No. A popup opened before the kill, one loading during it, and one opened while the server was frozen with SIGSTOP all loaded, and the opener went on (`test_a_killed_server_never_freezes_the_page_or_a_popup`) | Nothing to do; no Python runs then anyway |
  | Can blocking work without holding targets (`waitForDebuggerOnStart` false)? | Not for out-of-process frames: Fetch.enable on the frame's session was in place before its document committed, yet none of its requests were paused. Ad requests in a cross-site frame went out 20 of 20 runs at 0 ms and 16 of 20 at 50, 100, 200 and 500 ms. Popups 0/20, shared and service workers 0/20 | Targets are held until their setup ran (2 s deadline) |
  | Does `waitForDebuggerOnStart` false stop Chrome holding a popup? | No. A `window.open` popup waits for `Runtime.runIfWaitingForDebugger` from every attached client even when the attach event says `waitingForDebugger: false`; without it the popup never loaded | Every attached target is told to run after its setup |

- Coverage comes from `Target.setAutoAttach(waitForDebuggerOnStart, flatten)` on the browser session (filter: page,
  service_worker, shared_worker) and again on every page, frame and worker session: new tabs, popups, out-of-process
  iframes and workers, nested ones included, are held until `Fetch.enable` is in place. A page or iframe is attached
  whatever its URL: the first tab reads `chrome://newtab/` when attached, and skipping it left the whole browser
  unblocked. Each setup has a 2 s deadline (shorter than the stall) and tells its target to run on every path; a
  target that could not be set up is listed under `targets_not_covered`. Chrome's own targets are let go and detached.
- A target held for the debugger answers no `Page` command: `Page.getFrameTree` before the release hung every
  setup until the watchdog fired. It is read after the release.
- Every paused request is answered exactly once: the answer is decided inside `try` and sent once in `finally`, with
  continue as the default, and the send itself is bounded.
- The document a tab is navigated to is never blocked: `resourceType` Document whose `frameId` is the page target id
  or the root frame read from `Page.getFrameTree`. On Chrome 157 the two are equal on every page measured (navigate,
  link, target=_blank, redirect, tab_new, popup); a page where they differ is counted as `main_frame_mismatch`.
  Sub-frame documents are blocked, which is most of the gain on news sites. The source URL of a request is its
  frame's document, learned from the Document requests seen, because `$third-party` and `$domain=` depend on it.
- One Fetch owner per request. Two sessions with Fetch enabled on one target are chained by Chrome in an order not
  to rely on (nodriver's tab session came first in the server, a flattened session first in a raw experiment), so
  `intercept_start` on a block_ads browser enables no Fetch domain: it hands its rule (`capture._broker_rule`) to the
  blocker, which asks it for the tab's requests after its own decision and sends one answer. The rule writes the
  log under `capture._INTERCEPT_LOCK`. `intercept_stop` and the deadline take the rule off, and ad blocking stays on.
  Without block_ads, interception keeps its own Fetch session and the stop and retry behaviour of `_intercept_disable`.
- A launch with `block_ads=False` never loads `adblock.py` or the `adblock` package: server.py imports it on first
  use (`_adblock_module`), and the result filter, server_status, intercept_start and the exit path only look it up in
  `sys.modules` (`_adblock_if_loaded`). `test_a_launch_without_block_ads_never_loads_it` checks a new server process
  after a real launch, an interception, server_status, a close and the exit path.
- With `browser=brave`, Shields blocks ads, and `block_ads` does nothing and says so (`is_brave` on the executable).
- With keep_alive, `adblock.shutdown()` still runs first in `_stop_all_browsers`, before `_leave_or_end_at_exit` leaves a
  kept browser running: the kept browser goes on unblocked and nothing of the blocker holds it (measured: its popup
  opened after the exit loaded, its opener went on, and an ad request went out). A reattach starts no Chrome, so it
  attaches a blocker of its own only when asked (`_block_ads_on_reattach`, both `browser_launch` and `spawn_browser`);
  a reattached Brave stays a no-op naming Shields, told apart by `brave.product_of` since its Config names nodriver's
  Chrome;
  `block_ads` is not in `_NEW_CHROME_ONLY`, since a running browser can take it.
- Counts per browser are in `server_status.block_ads`: requests seen, blocked, allowed, by type and host, the
  last blocked URLs through `mask_url`, the targets covered and any that could not be, coverage, answer errors.
  `Invalid InterceptionId.` is not one: it is Chrome's reply to an answer for a request the page cancelled while it
  was paused, and it is counted as `gone_before_answer`. Measured on Chrome 157 by answering a held request late: a
  fetch the page aborted, an iframe or image removed and a navigation away each give exactly that, with the page
  already settled (the fetch rejected with AbortError); a redirect and a target closed meanwhile give no error.
  Nothing is left held then. `answer_errors` counts every other refusal, with samples (detik.com showed 2 of 399).
