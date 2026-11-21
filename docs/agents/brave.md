# Brave (browser="brave")

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## Profiles and the binding

- `brave.py` holds everything Brave (finding it, its profile folders, the binding, vertical tabs); `server.py` only
  passes `browser` through. Brave's profiles are `<data>/brave-profile` (main) and `<data>/brave-profiles/<id>`,
  resolved by the same `_resolve_launch_profile` without the profile affinity, which is Chrome main's.
- A profile belongs to the browser that made it, read from `Local State` (Brave writes `brave` and `brave_shields` at
  its first start). The check sits inside `_profile_version_refusal`, which every launch path calls before anything
  writes to the profile, so a new launch path gets it for free. It runs before the version check: Brave writes
  `Last Version` 157.1.99.55, newer than Chrome 157.0.8103.x, so Chrome on a Brave profile was otherwise refused as an
  "older Chrome", the wrong reason. An unreadable Local State is refused only to Brave.
- The binding is fail-closed: Local State and every sub-profile's Preferences are the evidence, all readable and in
  agreement. One unreadable file, two that disagree (also a Brave Local State beside a Chrome Preferences), or a
  non-empty folder with none of them is "unknown" and refused to both browsers; a check that raises is a refusal. Only a
  folder that does not exist or is empty is new.
- A `--user-data-dir` in `extra_args` is the profile that really opens (Chromium reads the last copy, nodriver puts its
  own first), so `_profile_from_extra_args` takes it out and makes it the launch's profile folder before any check: the
  binding, the version guard and the cookie plan see the folder the browser gets. Two different ones, or one without a
  value, are refused. `--profile-directory` must be one folder name, and copies must agree.
- A held Brave profile falls back to `brave-profile-pid<N>` (or `brave-profiles/<id>-pid<N>`). `profile_gc`,
  `profile_report` and `profile_cleanup` know `brave-profile`, `brave-profiles/*` and `brave-profile-pid<N>` exactly as
  they know Chrome's names, with the same checks; nothing else is matched.
- keep_alive: a Brave launch looks for a kept browser in Brave's folders only (`_kept_candidates(..., browser)`), and
  `_kept_verdict` refuses a kept profile the other browser made before taking the reattach lock, so a Brave launch is
  never handed a kept Chrome or the reverse, and the result names why. A reattached Browser's Config names nodriver's
  Chrome, so `_reattach_locked` marks it with its profile's maker (`_reattached_product`), or headless and `lang`
  overrides would name Google Chrome on a kept Brave. `vertical_tabs` is named as not applied on a reattach.
- The cookie move is release-fixes' algorithm; browser="brave" only passes `product` to `cookie_migration.plan`:
  `default_data_dirs(product)` skips Brave's own default folders for Brave, and a Brave on macOS whose version cannot be
  read is refused a profile holding `v10` cookies (`_unknown_brave_version`, kind `refuse`), since Brave 158 deletes
  them too (measured on Beta 158.1.101.48). Chrome's plan is unchanged: compared case by case with release-fixes'
  module, 70 of 70 identical.

## Identity

- Brave reports `Browser.getVersion` product `Chrome/157.0.8103.39` (the engine), but its page hints say Brave with
  every full version reduced to `157.0.0.0` (`fullVersionList`, `uaFullVersion`). `_apply_fingerprint_to_tab` passes
  `product` from the tab's executable (`brave.product_of`), or the headless and `lang` overrides would name Google
  Chrome next to `navigator.brave`, a pair no real browser shows.
- A browser's product is recorded on it (`brave.mark`). A launch records what it chose, whatever the executable is
  called (`MCP_STEALTH_BRAVE_PATH` may name a wrapper); attach records what it measured: the process on that port when
  its executable is Brave, or Google Chrome or Chromium by name (`_CHROME_EXECUTABLES`), then `navigator.brave` in an
  isolated world (an object means Brave), else "unknown", so a wrapper, Edge or Vivaldi is never named Chrome on a
  guess. Client hints are not overridden on "unknown" (`_brand_for` raises), attach says so in its result, and
  `emulate_device` passes the product too.
- Brave farbles window geometry (`screenX`, `outerWidth` and mouse `screenX` differ from the window's real ones), so the
  page cannot tell where the tab strip sits. `test_vertical_tabs_headed.py` paints the page one colour and captures the
  window with `screencapture -l <id>`, which works for a window behind others.

## Launch details

- `browser_launch` writes the vertical tabs and brave_lean settings after `_cookie_preflight` and
  `_commit_cookie_move`, so a refusal there writes neither (they ran before both and left the tab setting).
  A vertical_tabs failure now refuses after the commit, which a pending marker makes the next launch resume.
  `spawn_browser` still writes them before the preflight.
- Vertical tabs are Preferences keys written before launch (`brave.tabs.vertical_tabs_enabled`,
  `brave.tabs.vertical_tabs_on_right`; Chrome `vertical_tabs.enabled`), merged into the sub-profile's file through a
  temporary file and `os.replace`; a Preferences file that does not parse fails the launch rather than being replaced.
  vertical_tabs writes only into a sub-profile that is one folder name and not a link (also one `is_symlink` misses: the
  resolved path must be inside), in a profile positively seen free: no lock (an unreadable lock counts as one) and a
  process list that was read and names no process on it.
- Shields needs its filter lists, which a new profile fetches from Brave's component updater (about 11 to 12 s on this
  machine); `LOOPBACK_ONLY` (`MAP * ~NOTFOUND`) stops that fetch, so the Shields test maps only the ad host.
- The Dock icon is Chrome's: `_use_dock_icon_browser` returns at once for Brave, after pointing the config at Brave.
- On Linux `find_brave` takes `/opt/brave.com/brave/brave` before `/usr/bin/brave-browser`, a shell script whose
  recorded PID was not the Brave holding the profile lock, so keep_alive refused every reattach (Ubuntu CI).
