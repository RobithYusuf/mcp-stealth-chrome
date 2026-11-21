# Ad blocking (block_ads)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## When it is on

- `browser_launch(block_ads=True)` and `spawn_browser(block_ads=True)` block what EasyList and EasyPrivacy match, with
  Brave's adblock-rust (the `adblock` package, capped `<0.7`: the stored engine is its format). Network rules only;
  cosmetic rules write to the page.
- The package is the optional extra `adblock`, never a dependency: 0.6.0 has no musllinux or Windows ARM64 wheel and
  its sdist needs Rust, which broke every install there. Without it, `block_ads=True` launches with `block_ads: OFF`
  and the install command (`helpers.ADBLOCK_MISSING`), as does `server_status.block_ads.package` (`find_spec`, nothing
  imported). Fast CI installs no extra and skips the engine tests (`needs_engine`); the e2e job installs it and runs
  them. `installed_smoke.py` checks that a plain install has no adblock and still launches, and the engine with the
  extra.
- No list, no package or a failed start is `block_ads: OFF, <why>` in the launch result, never a failed launch. Some
  lists missing is `block_ads: PARTIAL` (which, why) and `coverage: partial` in server_status: "on" with EasyPrivacy
  missing let every tracker through while reading as full blocking.
- `block_ads=False` never loads `adblock.py` or the package: server.py imports it on first use (`_adblock_module`);
  the result filter, server_status, intercept_start and exit look only in `sys.modules` (`_adblock_if_loaded`).
  `test_a_launch_without_block_ads_never_loads_it` checks a new server process after a real launch, an interception,
  server_status, a close and exit.
- With `browser=brave`, Shields blocks ads; `block_ads` does nothing and says so (`is_brave` on the executable).

## Fail open, never closed

- The blocker owns a CDP connection, thread and event loop (`mcp-stealth-adblock`); sessions, the frame map, the
  engine and intercept_start's rule are touched only there. What other threads read (counts, end, waiting maps) is
  behind the blocker's lock, the registry behind `_REGISTRY_LOCK`. The reader never awaits a reply while handling an
  event: only it can read that reply (the first draft deadlocked).
- A watchdog thread, independent of the adblock thread and armed before the blocker starts (a hung start is caught),
  ends the blocker when that thread is gone, its loop stopped or missed its heartbeat for `MCP_STEALTH_ADBLOCK_STALL`
  (3 s), or a paused request or held target waited that long. It disables Fetch if the loop answers, shuts the socket,
  and starts every target over a new connection (`_release_every_target`: attach, `Runtime.runIfWaitingForDebugger`,
  detach; rescanned until a pass finds nothing new, catching popups opened meanwhile). A new connection, because a
  client that goes away releases its paused requests but not a held `window.open` popup: while the server lived, popup
  and opener stayed frozen after the socket was shut (Chrome 157).
- The release runs on its own thread joined with its budget, so a connect, send, receive or close that never returns
  cannot hold the caller. Out of budget, the end reason names how many targets were not released and their ids, or
  says which are left is unknown when no target list was read.
- Every end of the connection runs that release exactly once (`_end` decides who): the watchdog, a clean EOF or
  transport error while the browser still answers on its debugging port, and server exit (`adblock.shutdown()`, first
  in `_stop_all_browsers`, all blockers side by side within `SHUTDOWN_BUDGET`, 4 s in all, so exit is never held). The
  session's next tool result says block_ads stopped and why (`_app.RESULT_FILTERS`; a JSON object gets a `block_ads`
  key and still parses).
- SIGKILL of the whole server leaves nothing held, so nothing is done for it (no Python runs then anyway): popups
  opened before the kill, loading during it, or opened while the server was frozen with SIGSTOP all loaded and the
  opener went on (Chrome 157, `test_a_killed_server_never_freezes_the_page_or_a_popup`).

## Coverage and answers

- `Target.setAutoAttach(waitForDebuggerOnStart, flatten)` on the browser session (filter: page, service_worker,
  shared_worker) and every page, frame and worker session holds new tabs, popups, out-of-process iframes and workers,
  nested too, until `Fetch.enable` is in place, with a 2 s deadline per setup (under the stall). Without holding
  (`waitForDebuggerOnStart` false) out-of-process frames leak (Chrome 157): Fetch.enable on the frame's session before
  its document committed paused none of its requests, and a cross-site frame's ad requests went out in 20 of 20 runs
  at 0 ms and 16 of 20 at 50, 100, 200 and 500 ms. Popups 0/20, shared and service workers 0/20.
- Every attached target is told to run after its setup, on every path: a `window.open` popup waits for
  `Runtime.runIfWaitingForDebugger` from every attached client, even when the attach event says
  `waitingForDebugger: false`, and never loaded without it (Chrome 157). Targets not set up are listed in
  `targets_not_covered`; Chrome's own are let go and detached.
- Pages and iframes are attached whatever their URL: the first tab reads `chrome://newtab/` when attached, and
  skipping it left the whole browser unblocked.
- A held target answers no `Page` command: `Page.getFrameTree` before the release hung every setup until the watchdog
  fired, so it is read after.
- Every paused request is answered exactly once: decided in `try`, sent once in `finally`, continue by default, the
  send itself bounded.
- The document a tab navigates to is never blocked: `resourceType` Document whose `frameId` is the page target id or
  the root frame from `Page.getFrameTree` (equal on every page measured on Chrome 157: navigate, link, target=_blank,
  redirect, tab_new, popup; a mismatch counts as `main_frame_mismatch`). Sub-frame documents are blocked: most of the
  gain on news sites. A request's source URL (for `$third-party` and `$domain=`) is its frame's document, learned from
  the Document requests seen.
- One Fetch owner per request: Chrome chains two Fetch sessions on one target in no order to rely on (nodriver's tab
  session first in the server, a flattened one first in a raw experiment). So on a block_ads browser `intercept_start`
  enables no Fetch domain; its rule (`capture._broker_rule`) goes to the blocker, which asks it for the tab's requests
  after its own decision and sends one answer; the rule writes the log under `capture._INTERCEPT_LOCK`.
  `intercept_stop` and the deadline take the rule off; blocking stays on. Without block_ads, interception keeps its
  own Fetch session and `_intercept_disable`'s stop and retry.

## Lists and engine

- The lists (GPLv3 / CC BY-SA) are never bundled: `adblock.py` downloads them on first use into
  `~/.mcp-stealth/adblock` (`MCP_STEALTH_ADBLOCK_DIR`; files 0600, folder 0700; `tests/conftest.py` uses a temporary
  one), https only (a redirect elsewhere is refused), rechecks at most daily with a conditional request in the
  background, and works offline from the last copy. A download that is not an Adblock Plus list (a captive portal
  answers 200) never replaces a copy. No lock the launch needs is held over the network.
- Downloads are capped in bytes (Content-Length and body, `MCP_STEALTH_ADBLOCK_MAX_BYTES`, 16 MB) and in total time
  (`MCP_STEALTH_ADBLOCK_DOWNLOAD_TIMEOUT`, 20 s), the socket timeout set before each read to what is left:
  `urlopen(timeout=)` is per read, so a server sending a byte at a time kept it going for ever, and a read started
  near the end ran past the deadline.
- A list name becomes a file name: it must match `^[a-z0-9][a-z0-9_-]{0,63}$` and sit directly in the folder
  (`MCP_STEALTH_ADBLOCK_LIST_URLS="../../escape=..."` wrote outside it; an absolute name replaced the root,
  `Path / "/abs"`). An invalid name turns block_ads OFF with the reason.
- The engine is stored serialized (`engine.dat`, keyed by the lists' sha256, the package version and `ENGINE_SCHEMA`)
  and reaches the native parser only under `MCP_STEALTH_ADBLOCK_MAX_ENGINE_BYTES` and matching the sha256 stored
  beside it; else it is rebuilt. Real lists (109,487 network rules): load 32 to 46 ms, build about 210 ms.

## keep_alive and counts

- `adblock.shutdown()` runs first in `_stop_all_browsers` with keep_alive too, so a kept browser that
  `_leave_or_end_at_exit` leaves running goes on unblocked and unheld (measured: a popup opened after exit loaded, its
  opener went on, an ad request went out). A reattach starts no Chrome, so it attaches a blocker only when asked
  (`_block_ads_on_reattach`, in `browser_launch` and `spawn_browser`); a reattached Brave stays a no-op naming
  Shields, told apart by `brave.product_of` as its Config names nodriver's Chrome. `block_ads` is not in
  `_NEW_CHROME_ONLY`: a running browser can take it.
- `server_status.block_ads` counts per browser: requests seen, blocked, allowed, by type and host, the last blocked
  URLs through `mask_url`, targets covered and not, coverage, answer errors.
- `Invalid InterceptionId.` is not an answer error but `gone_before_answer`: Chrome's reply to answering a request the
  page cancelled while paused; nothing stays held. Measured on Chrome 157 by answering held requests late: an aborted
  fetch, a removed iframe or image and a navigation away each give exactly that, the page already settled (the fetch
  rejected with AbortError); a redirect or a target closed meanwhile gives none. `answer_errors` counts every other
  refusal, with samples (detik.com: 2 of 399).
