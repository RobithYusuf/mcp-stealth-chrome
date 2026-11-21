# Request interception

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- `Fetch.enable` pauses EVERY matching request until it is answered; a handler that fails to answer hangs the page
  silently instead of raising. Every path through the handler, the exception path included, ends in exactly one
  `continue`/`fail`/`fulfill`.
- Interception disables itself on a deadline, and `_clear_tab_scoped_state` releases it when its target closes; a stale
  intercept would pause requests in the next browser launched.
- The deadline runs `_intercept_disable` inside the watchdog task, so it must not cancel the current task, or the next
  await raises and `active` stays true forever. Disabling removes the `RequestPaused` handler, or a restart answers each
  pause twice, except after a failed `Fetch.disable`: pauses may still come with nothing else to answer them, so
  `intercept_start` removes it instead.
- A failed `Fetch.disable` is not a stop: `_intercept_disable` returns the failure and keeps `active`, `tab` and the
  handler, which then lets every request through. `intercept_stop` is an error and retries on the next call,
  `intercept_log` shows `disable_error`, and the deadline retries (`_DISABLE_RETRY_DELAYS`). Answering `stopped` there
  made every later stop say "not active" while the handler and Fetch were live.
- After such a failure `intercept_start` on another tab is refused and keeps the old state: dropping the old handler
  left Fetch on there with nothing to answer its pauses. On the same tab the restart goes ahead, safe because nothing
  awaits between removing the old handler and adding the new one.
