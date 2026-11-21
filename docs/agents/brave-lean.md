# Brave lean (brave_lean)

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

## When lean applies

- `brave_lean.py`: `brave_lean=None` is lean only on a profile this server made empty: a folder the launch chose itself
  (`server_chosen`, false for `user_data_dir`, `--user-data-dir` and spawn's `profile_dir`) that is empty, then
  recorded, or whose record still matches it. The proof is a record in the server's data folder
  (`<data>/brave-lean/<sha256 of the real path>.json`, `MCP_STEALTH_BRAVE_LEAN_DIR`, 0600 in a 0700 folder) binding the
  folder's real path, device and inode while it was empty; `.mcp-stealth-brave-agent-profile.json` in the profile is
  only a hint and must match too. A copied or moved folder, a copy put back at the same path, or a hand-written mark
  does not match, and the default stays off, saying why. A clone in `brave-profiles/<id>` sits where the server puts
  profiles but holds someone's settings, so the place proves nothing.
- Under the default, anything uncertain (a mark or lean record that does not parse, a write that stopped) applies
  nothing, not even the `--disable-features` flag. Records of folders that are gone are dropped at the next claim. True
  asks for lean anywhere but the person's Brave folder and does not mark; False puts back. `server.py` only calls
  `_apply_brave_lean` beside the vertical-tabs write, in `browser_launch` after the cookie commit and before the
  tidy-up (see brave.md). Chrome gets nothing, and the result says so only when `brave_lean`
  was passed. A reattach applies nothing and says so whatever brave_lean was (`_lean_on_reattach`).

## What lean turns off

- No enterprise policy: Brave's policies are machine-wide managed preferences that change the person's own Brave, and
  Chromium's `--policy=` switch is Android-only (`CommandLinePolicyProvider::CreateIfAllowed` returns null on the
  desktop; on Brave 157 for macOS the switch left brave://rewards offering Rewards). Do not add a `defaults write` or a
  managed plist for this either.
- Features go through `_disable_features_arg` (one flag, nodriver's list kept), each name proved on the running browser,
  not taken from a list: `AIChat` (isLeoAssistantAllowed false, brave://leo-ai an error page), `BraveVPN`,
  `Speedreader`; `Playlist` is already off on the desktop and is named to keep it off. `BraveRewards` exists on Android
  only (`rewards_util.cc`), so it is not passed: Chromium ignores a feature name it does not know without a word.
  Rewards, Ads and the wallet page can only be removed by policy; Rewards and Ads stay off until a person opts in.
- Brave 157 migrates obsolete Preferences names at start (`brave.new_tab_page.show_branded_background_image` became
  `brave.brave_ads.sponsored.enabled`; `hide_all_widgets` is split per widget and cleared), rewriting or dropping a key
  written under an old name. Read a key's current name in brave-core at the Brave version in use, and check it survives
  a close (`read_back`).
- Idle 90 s on a fresh profile (net-log): without lean Brave contacted usage-ping.brave.com, star-randsrv.bsg.brave.com
  and collector.bsg.brave.com; with lean none of them, while componentupdater, go-updater, redirector, brave-core-ext.s3
  (filter lists and components), variations and safebrowsing.brave.com stay.

## Writing, putting back, reading back

- Local State (P3A, usage ping, Tor) and the sub-profile's Preferences are written with the vertical-tabs rules
  (`_preferences_target`, `_refuse_unless_free`; unknown means not written, never a guess), never into Brave's own
  default user-data folders (`person_brave_folders`, compared by real path), and never raise into the launch: what was
  not written is in the result. A key under `protection.macs` (Secure Preferences) is tracked and Brave would reset it,
  so it is not written and is named.
- A parent holding a string, number or list where lean expects settings is never replaced: that key is not written and
  is named (`_BLOCKED`). The profile folder itself must not be a link.
- `.mcp-stealth-brave-lean.json` in the profile records, per file (`Local State`, `<sub>/Preferences`), each key's value
  before lean, or the value set since lean last wrote it (putting back the first would undo a later choice).
  `brave_lean=False` walks every recorded file whichever sub-profile launches, puts back only keys still holding what
  lean wrote, leaves and names the rest, and keeps the part of the record it could not put back. A record of another
  shape is not acted on. The record is written before the settings, so a crash between them leaves values that match
  `before`.
- The empty and free checks are a snapshot: `_checkpoint` reads the lock again (tri-state: unknown stops) and checks the
  folder is still not a link and still the same inode right before the claim record, the mark, the lean record, each
  settings file and the removal or rewrite of the lean record. A change stops lean, and the result says what was and was
  not written.
- Everything from the first look at the profile to the last write runs inside the profile's launch reservation
  (`launch_lock.hold([root], 0)` in `brave_lean._reserved`): reentrant when the launch calling lean already holds it,
  taken there for a profile the launch did not reserve (a fallback, a privacy or temporary folder), and a refusal that
  writes nothing when another launch holds it. So no launch path of this project (browser_launch, spawn_browser, the
  launcher's `open`) can start a browser on the profile in the middle of lean's writes. Known limit: a Brave the person
  starts by hand on the agent's profile between the last re-check and a write (a few milliseconds) honours no
  reservation and is not seen; the re-checks narrow that, they cannot close it.
- brave://settings is readable over CDP:
  `cr.sendWithPromise('getP3AEnabled' | 'getStatsUsagePingEnabled' | 'brave_tor.isTorEnabled')`, `loadTimeData`
  booleans, and `chrome.settingsPrivate.getPref` for the profile preferences that page lists (not `brave.ai_chat.*` once
  AIChat is off); `test_brave_lean_e2e.py` reads lean back that way. Toolbar buttons and the sidebar cannot be read
  outside the UI; for those the preference file is the evidence.
