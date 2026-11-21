# Brave Beta and the Dock launcher

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## Brave Beta

- `brave_beta.py` applies Chrome Beta's rule to Brave: headed, `MCP_STEALTH_DOCK_ICON=1`, macOS,
  `/Applications/Brave Browser Beta.app` (`com.brave.Browser.beta`), with
  `profile_version.beta_verdict(names=("Brave", "Brave Beta"))` deciding before anything writes to the profile. The
  person's Brave is never written to, attached to or read. `brave.is_brave_executable` knows "Brave Browser Beta", or
  block_ads would run beside Shields.
- Brave Beta keeps its own icon and nothing is written into it: neither `brave_beta.prepare` nor
  `dock_icon.start_in_background` at server start starts an icon job (the first draft's server start still did). With
  the project icon, which is Chrome's, the pinned launcher and the running browser looked alike, so the person pinned
  Brave Beta itself, which opens plain Beta (20 Nov 2026). An icon an older version put there goes with Beta's next
  update. `dock_icon.put_icon_on` still accepts only `Google Chrome Beta.app` and `Brave Browser Beta.app` and refuses
  anything resolving to `/Applications/Google Chrome.app` or `/Applications/Brave Browser.app`.
- Brave on macOS keeps its updater state in Sparkle's defaults domain of each bundle id: `com.brave.Browser`
  (`LastRunAppBundlePath`, `SULastCheckTime`) and `com.brave.Browser.beta`. Running Beta writes only its own domain.
- Brave Beta is not on the ACL of the "Brave Safe Storage" Keychain item Brave made: with the real Keychain it asks for
  the person's login password (a SecurityAgent dialog on their screen). Unanswered, Beta waits (the cookie reader timed
  out), and the next launch with the protection off read none of the 3 cookies and dropped them. So every Brave Beta
  start in a test or a measurement passes `--use-mock-keychain`, never the real Keychain, and the launch result names
  the dialog whenever Beta runs without the mock. `_read_v10_cookies` passes the launch's `--use-mock-keychain` on, or
  the reader asks the real Keychain and reads nothing the mock launch wrote.
- With the mock on both sides: Brave 157 wrote 3 `v10` cookies, Brave Beta 158 opened raw dropped all 3, and through the
  server the cookie move kept all 3 as `v11`, after which Brave 157 was refused the profile. Mock-keychain cookies prove
  the move, not the real Keychain's behaviour.
- `--use-mock-keychain` is refused outside the test switch (`_mock_keychain_refusal`: `PYTEST_CURRENT_TEST`, which
  servers a test starts inherit and a test-built launcher app carries in its `env.sh`). A Brave cookie reader that
  fails, or reads fewer cookies than the profile holds, refuses the launch (`_cookie_move_failed(product="brave")`)
  instead of the flag fallback: without Keychain access Brave Beta cannot decrypt them with the flag either. Chrome
  keeps the documented fallback.
- `ALLOW_PROFILE_MOVE` (the launcher's `--move-to-beta`, in its own one-shot process only, never a tool argument; only
  for a real folder Brave made, both versions read and Beta not older, anything unknown refused) is the one way Beta
  takes a profile Brave made, and the cookie move then runs. It is one way: Brave older than Beta is refused afterwards.

## `mcp-stealth-chrome open` and the launch reservation

- `mcp-stealth-chrome open` starts a browser only through
  `server.browser_launch(keep_alive=True, profile_conflict="fail")` in its own process, then `_stop_all_browsers()`, the
  stdio exit path, so flags, profile, port, watcher and record are the server's own and every reattach check passes
  unchanged. It first forks into a session of its own (a closing terminal must not SIGHUP Chrome) and leaves with
  `os._exit`.
- A held profile is never launched over: `launcher.holder` reads the lock and the process list, and `_is_main` judges by
  the executable's own name (`state._is_browser_main_process` also matches the crash handler inside
  `Brave Browser Beta.app`, which a killed Beta leaves running). A lock naming a PID that runs something else is removed
  only when no process names the profile; unknown is never free.
- Every launch path reserves the profiles it may open from its first look at who holds them until the browser holds its
  own lock and the record is written (`launch_lock`: one lock per profile beside the keep_alive records, flock on POSIX
  and a one-byte `msvcrt.locking` on Windows, both released by the system when the holder dies; reentrant within a
  process, because `open` holds it and calls browser_launch). A lock file that cannot be made or opened, such as in a
  read-only keep-alive folder, lets the launch go on unreserved as before, and the result says so
  (`launch_lock.note()`). A SingletonLock check is no reservation: Chrome takes it about half a second after starting.
- `bring_forward` reports as a failure (exit 1) a window it could not open, a `Target.activateTarget` Chrome did not
  answer, or an activation macOS refused, and so does a cold start that could not be brought to the front.
- A browser the launcher opened is recorded `opened_by: "launcher"` and gets `keep_alive.cap_hours` = a year unless
  `MCP_STEALTH_KEEP_ALIVE_MAX_HOURS` is set; a reattach keeps `opened_by`, so an agent leaving does not cut it to 24 h.

## The Dock app

- The app is an osacompile applet (`LSUIElement`: no Dock icon of its own, so the pinned item and Brave Beta are what
  the Dock shows), signed ad hoc, declaring http and https. An app in a temporary folder is registered but left out of
  `LSCopyAllHandlersForURLScheme` ("in-temp-dir"); built elsewhere it is listed. It runs `launch.sh`, which finds the
  command and passes `--from-app`: the first start needs `--acknowledge` (exit 3 shows the safety note), and
  `dock_icon.ICON_WRITES` is off, because App Management blocks an app from writing into another app's bundle. Never
  change the default browser for the person.
- The handler passes only `http://` and `https://` URLs, as given (no guessed scheme, no `file:`, no document handler),
  checked before the acknowledgement is written. `ICON_WRITES` is consulted before any write, `_clear_dangling_flag`
  included, in both `prepare` functions.
- Tests: the launcher e2e opens the app with `open -g` and `MCP_STEALTH_LAUNCHER_ACTIVATE=0`, and
  `MCP_STEALTH_LAUNCHER_EXTRA_ARGS` carries `--use-mock-keychain`. `tests/conftest.py` points `brave_beta.BETA_APP` at a
  missing folder in-process (except the files in `_REAL_BRAVE_BETA`): a fake icon writer in `test_dock_icon` once wrote
  its stand-in `Icon\r` into the real Brave Beta.
