# Saved tabs (restore_tabs)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- Chrome's own session restore stays off (`wipe_window_state`, `--disable-restore-session-state`): its restored window
  placement opened invisible windows. The server keeps its own list: `tab_record`, one `<instance>-tabs.json` per
  instance in the window-state folder (`MCP_STEALTH_TABS_DIR`), one entry per resolved profile path so a fallback
  profile never replaces the real one's list, written 0600 through a temporary file, `fsync` and `os.replace`.
  `tests/conftest.py` points `MCP_STEALTH_TABS_DIR` at a temporary folder for every server and in-process launch, or a
  test with a temporary `user_data_dir` writes fixture URLs into the real folder.
- Only a profile that outlives the close is recorded (`_tab_record_scope`): never `persistent=False` (no profile path),
  `privacy=True` or an attached browser.
- An empty list never replaces a record: a Quit closes every window before anything can read them, a failed read says
  nothing, and only about:blank or chrome:// pages are not worth reopening. Clear one with `forget_tabs=True` on
  `browser_close` and `close_instance`, or with `profile_cleanup(remove_fallback)`.
- Order is the server's tab list (`BrowserState.tabs`, what `tab_list` shows), not Chrome's (see the target-order fact
  in [chrome-cdp-nodriver-facts.md](chrome-cdp-nodriver-facts.md)); active is the server's selected tab, not whichever
  one a window shows.
- Written at five moments:
  1. Every close this server makes of a browser it started (`browser_close`, `close_instance`, `close_all_instances`,
     `_end_current_instance`/`_end_stored_instance` for the idle reaper and the window sweep), before
     `_ensure_browser_dead`, the read capped at 1 s; the sweep's branch for a browser the quit watcher already ended
     passes no reason, as Chrome is gone. Only a Browser object's first close records: a failed close keeps the handle,
     Chrome can linger half closed for minutes, and a retried close read the windows that were left.
  2. The background writer, about 2 s after any tool call (`_app.AFTER_TOOL_HOOKS`, a dict write measured at 2 µs) and
     every 30 s for every recordable instance, since a person can navigate a visible window without a tool call. It
     reads twice 0.3 s apart, writes only when both agree, and skips an unchanged list. It never touches a browser once
     a close has started (`_CLOSING_ATTR`, no expiry: a 30 s expiry let it write over a failed close's list), so windows
     closing one by one never overwrite the close's list.
  3. As a launch returns (`_record_tabs_at_launch`, one read): the quit watcher acts only once no page is left, too late
     to read, and a Quit in the first two seconds lost the list.
  4. stdin EOF, `_stop_all_browsers`: the event loop has closed, so the list comes from `/json/list` over plain HTTP
     with the quit watcher's reader (to Content-Length).
  5. Nothing else: a killed server leaves what step 2 last wrote, without pages a person changed in the last couple of
     seconds before a Quit.
- Restore (`_reopen_saved_tabs`) re-filters every URL (http, https, file only) since the file can be edited, opens each
  extra page as `tab_new` does (its own background window headed, a tab headless) with the instance fingerprint, loads
  them concurrently, and reports each through `tab_record.shown_url` (`mask_url`, which covers the fragment, where OAuth
  puts `access_token`). It never fails the launch. A record older than `MCP_STEALTH_RESTORE_TABS_MAX_DAYS` (30) is
  named, not opened. The reuse path of `browser_launch` says restore was not applied.
- `tests/test_restore_tabs_e2e.py` closes through each path on real Chrome (the EOF case with the background writer off,
  so only the exit path can write) and, with `MCP_HEADED=1` (needs `--extra os-cursor` for AppKit), quits a visible
  browser and checks the restored windows took 0 ms of frontmost time.
