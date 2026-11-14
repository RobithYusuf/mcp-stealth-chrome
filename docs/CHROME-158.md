# Chrome 158 and saved logins (macOS)

Chrome 158 on macOS encrypts with a new key in any profile outside its own default folder, which is every profile this server uses. It does not read the cookies Chrome 157 stored there, and deletes them. The server moves them first, once per profile, the first time Chrome 158 or newer opens it:

1. It copies the profile's cookie databases to `~/.mcp-stealth/storage-states`, checks the copy, and reads every old cookie from that copy with a headless Chrome started with `--disable-features=KeychainKeyProviderUserDataDirProtection`, which keeps the old cookies readable. The profile itself is not opened or changed at this point.
2. It writes them to `~/.mcp-stealth/storage-states/pre-v11-migration-<profile>-<time>.json`, readable by you only, and makes sure the file is on disk. `storage_state_load` accepts that file.
3. Only then does it mark the move as pending in the profile, start the launch you asked for normally, set the cookies again, and read them back, comparing values without printing them.

If the server or the machine stops at any point, the next launch either finds the profile as it was, or finds the pending move and finishes it from the backup.

The launch result says `migrated N cookies to Chrome 158 encryption; backup at <path>, removed after 14 days`. If fewer come back, the launch result is an error that names the missing cookies and the backup, and the next launch tries again. If anything fails before the backup is complete, that launch keeps the protection turned off so nothing is lost, and the next launch tries again. If Chrome reads fewer cookies than the profile holds, the copied databases are put back. Measured with Chrome Beta 158 and 159 standing in for Chrome 158, on profiles made by Chrome 157.

The backup holds every cookie value in plain text. Once the move is done and every cookie was read back, it is kept 14 days (`MCP_STEALTH_MIGRATION_BACKUP_DAYS`) in case something is missed, then removed with its database copy, when a server starts or finishes another move. A backup whose move is not done stays.

Turning the protection off is not safe for every profile: Chrome 158 then deletes the cookies it already stored with the new key (measured on Chrome Beta 159: 3 of 3), and a cookie store holding both kinds loses all of them either way. So the protection is turned off only for a profile read to hold no new-key cookie, a store holding both is moved in two steps, and a launch whose cookie database cannot be read for certain, or changes between the check and the start, is refused with the reason and starts nothing.

Only cookies move. Saved passwords, card and address data and other values Chrome 157 encrypted in that profile stay unreadable to Chrome 158 there. Only the profile folder being launched is moved (`Default`, or the one `--profile-directory` names). A profile in Chrome's own default folder keeps the old key and is left alone. A profile Chrome 158 has opened is refused to Chrome 157.

Two kinds of profile get left behind: `privacy-profiles/<name>`, when a server stops before it could delete one, and `profile-pid<N>` (or `brave-profile-pid<N>` for Brave), which a server uses when another one holds `profile/` (or `brave-profile/`). A few seconds after it starts, the server checks both on a background thread and removes a folder only when all of these hold:

- ✅ It is a real folder directly in its place, with exactly that name, not a link.
- ✅ No Chrome holds its lock, no running program names it, and this server does not use it.
- ✅ Nothing in it changed in the last 10 minutes.
- ✅ For `profile-pid<N>` and `brave-profile-pid<N>`: the server with PID N has ended, and the profile holds none of these: cookies, saved passwords, history, autofill or cards, top sites, saved tabs, local storage, IndexedDB, extensions, bookmarks. A file it cannot read counts as holding something.

It never touches `profile/`, `profiles/`, `brave-profile/`, `brave-profiles/`, `app/` or anything else. `server_status` shows what the last run removed and what it kept, under `profile_gc`. A `profile-pid<N>` that holds something stays: `profile_report` lists what is in each one (counts and cookie domains, no values), and `profile_cleanup(action="remove_fallback", names=[...], confirm=True)` removes the ones you name. Removal is permanent.
