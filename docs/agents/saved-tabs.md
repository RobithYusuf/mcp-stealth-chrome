# Saved tabs (restore_tabs)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- Chrome's own session restore stays off (`wipe_window_state`, `--disable-restore-session-state`); restored window
  placement is what opened invisible windows. The server keeps its own list instead: `tab_record`, one
  `<instance>-tabs.json` per instance in the window-state folder (`MCP_STEALTH_TABS_DIR`), one entry per resolved
  profile path, so a fallback profile never replaces the list of the real one. Written 0600 through a temporary file,
  `fsync` and `os.replace`. `tests/conftest.py` points `MCP_STEALTH_TABS_DIR` at a temporary folder for every server and
  in-process launch: a test with a temporary `user_data_dir` would otherwise write fixture URLs into the real folder.
- Only a profile that outlives the close is recorded (`_tab_record_scope`): never `persistent=False` (no profile path),
  `privacy=True` or an attached browser.
- An empty list never replaces a record. A Quit closes every window before anything can read them, a failed read says
  nothing, and a browser holding only about:blank or chrome:// pages has nothing worth reopening. `forget_tabs=True` on
  `browser_close` and `close_instance`, and `profile_cleanup(remove_fallback)`, are the ways to clear one.
- Written at five moments. (1) Every close this server makes of a browser it started (`browser_close`, `close_instance`,
  `close_all_instances`, `_end_current_instance`/`_end_stored_instance` for the idle reaper and the window sweep), before
  `_ensure_browser_dead`, with a 1 s cap on the read. The sweep's branch for a browser the quit watcher already ended
  passes no reason: Chrome is gone. Only the first close of a Browser object records: a close that fails keeps the
  handle, Chrome can linger half closed for minutes, and a retried close read the windows that were left. (2) The
  background writer, about 2 s after any tool call (`_app.AFTER_TOOL_HOOKS`, a dict write measured at 2 µs) and every
  30 s for every recordable instance, since a person can navigate a visible window with no tool call. It reads twice
  0.3 s apart and writes only when both agree, and never touches a browser once a close has started (`_CLOSING_ATTR`,
  no expiry; it used to lapse after 30 s, and a failed close then had its list written over): windows closing one by
  one must not be written over the close's list. An unchanged list is not rewritten. (3) As a launch returns
  (`_record_tabs_at_launch`, one read): the quit watcher acts only once no page is left, so there is nothing to read
  then, and a Quit in the first two seconds lost the list. (4) stdin EOF, `_stop_all_browsers`: the event loop has
  closed by then, so the list comes from `/json/list` over plain HTTP with the quit watcher's reader (to
  Content-Length). (5) Nothing else: a killed server leaves what (2) last wrote, and pages a person changes in the
  last couple of seconds before a Quit are not in it.
- Order is the server's tab list (`BrowserState.tabs`, what `tab_list` shows), not Chrome's: see the target-order fact
  below. Active is the server's selected tab, not whichever tab a window happens to show.
- Restore (`_reopen_saved_tabs`) re-filters every URL (http, https, file only) because the file can be edited, opens
  each extra page as `tab_new` does (its own background window headed, a tab headless) with the instance fingerprint,
  loads them concurrently, and reports each one through `tab_record.shown_url` (`mask_url`, which covers the fragment,
  where OAuth puts `access_token`). It never fails the launch. A record older than `MCP_STEALTH_RESTORE_TABS_MAX_DAYS` (30)
  is named and not opened. The reuse path of `browser_launch` says restore was not applied.
- `tests/test_restore_tabs_e2e.py` closes through each path on real Chrome (the EOF case with the background writer
  off, so only the exit path can write) and, with `MCP_HEADED=1` (needs `--extra os-cursor` for AppKit), quits a
  visible browser and checks the restored windows took 0 ms of frontmost time.
