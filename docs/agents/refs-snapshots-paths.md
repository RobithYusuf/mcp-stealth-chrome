# Refs, snapshots and paths

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- Nothing is written to the page. The snapshot runs in an isolated world and returns element handles, which `refs.py`
  maps to `backendNodeId` on the server. Any `MutationObserver` sees a `data-mcp-ref` attribute or a `flash()` dot, so
  do not reintroduce a marker to find an element again: hold a handle (`evaluate_parts`, `active_element`).
- A cross-origin iframe is its own CDP target even with nodriver's site-isolation flag, absent from the parent's
  `Page.getFrameTree`. Its quads are local to its own viewport; `RefElement.get_position` adds the owner `<iframe>`'s
  content box. The frame's own hit test cannot see an overlay the parent draws, which is what `outer_blocker` is for.
- `DOM.describeNode` keeps answering for a node from a previous document; staleness is proved by `DOM.resolveNode` plus
  `isConnected`, as `resolve_ref` does.
- Set attributes on a nodriver `Element` only with a leading underscore: `Element.__setattr__` silently forwards any
  other name to the HTML attributes, so `el.foo = x` reads back as `None`.
- A field carries its label as `label="..."` (aria-labelledby, then `<label for>` or a wrapping label, read without the
  text of the field it wraps). In `interactive_compact` and `viewport` the label's own row is left out and counted, but
  only when its field has a row in the same snapshot: a label for a hidden input, as styled checkboxes have, is the
  thing to click and keeps its row. `full` keeps every label row; `fast` is unchanged. A form of 158 elements went from
  2,689 tokens to 2,188, and the default 100-element cap no longer cuts off its submit button.
- `delta=True` marks elements added or changed since the tab's previous snapshot with `*`. The fingerprint deliberately
  EXCLUDES the ref: refs are regenerated every snapshot, so including them would mark everything changed, every time.
- An unchanged `diff_from_last` result keeps the measured element count and says `unchanged; details omitted`:
  `Interactive elements (0)` read as "this page has no interactive elements".
- A quiet DOM is not a rendered page: a single-page app whose bundle has not run yet is stable and empty, and
  `describe_page(wait_stable=True)` answered `stable` with no headings, fields or actions on a real shop. An empty
  summary reads the body text and says `nothing is rendered yet`; a snapshot that matched nothing says so and names
  `wait_for`. A frame walked with no match is listed under `Frames with nothing readable` (a hosted card field keeps its
  input in a closed shadow root, and `Frames: 7 measured` read as seven empty frames). The frames line shows the host
  and the last path part (`_frame_hint`), since one widget's frames differ only at the end.
- Closed shadow roots are read through CDP, since no script can enter one: card fields in closed roots inside
  cross-origin frames were missing from the snapshot, and filling them by position sent three clicks into a `<div>`
  after a layout shift. `refs._hand_over_closed_roots` runs `DOM.describeNode(depth=-1, pierce=True)` on the frame's
  document and resolves each `closed` root (never `user-agent`: every `<input>` has one) into the frame's isolated
  world, in that world's own `__mcpClosedRoots` Map (host to root), which the walker's `shadowOf` reads; the page sees
  nothing. Only documents of at most `MCP_STEALTH_CLOSED_PROBE_MAX_NODES` (5000) elements are read whole, so a large
  page pays one count. Child frames' documents are not followed (`content_document`); they are walked on their own. A
  probe that fails leaves the walk as before. The report says `Closed shadow roots: N read`. A `container_selector`
  applies in every frame, so `main` on a checkout hides the card frames' fields: that is the selector's meaning, not a
  bug.
- `get_text` reads a frame only when its document is rendered (`body.getClientRects()`): the innerText of an unrendered
  document (a `display:none` iframe) is its textContent, and Google Pay's hidden payframe read as a page of minified JS.
  Such frames are named on one line; `raw=True` is textContent on purpose and reads them.
- `fill` reads the field back. A value that differs only by separators (spaces, `/`, `.`, `-`, parentheses; never a
  newline or tab) is `filled; the page formatted it as '...'`: a card mask turned 16 digits into `4111 1111 1111 1111`
  and the expiry into `12/30`, and an error there made a working checkout look failed. Any character dropped or changed
  (maxlength, a rejected key) is still an error; a password field shows only its length.
- A frame URL inside a sentence goes through `helpers.short_url` (`frame_evaluate`, `wait_for(frame_url=)`,
  `mouse_click_xy`): a payment frame's URL runs past a thousand characters and was printed whole on every call.
- A window size passed to `browser_launch` wins over the saved placement, whose position is still restored
  (`_restore_window_bounds(size=)`, `_DEFAULT_WINDOW`); the saved 1280x800 used to undo an asked-for 1400x950.
- Every managed output path goes through `confined_output_path` / `_direct_child`: `..` once passed the identity regex,
  so `clone_chrome_profile(target_instance_id="..", overwrite=True)` resolved to `~/.mcp-stealth` and would have deleted
  every profile, screenshot and identity in it.
