"""Snapshot refs held on the server, and the frame walk that produces them.

A ref used to be a ``data-mcp-ref`` attribute written into the page, which any
site script could see through a MutationObserver or a plain attribute check. It
is now an entry in a server-side map: ref -> (CDP connection, backendNodeId). The
snapshot itself runs in an isolated world, so nothing is written to the DOM and
the walk cannot be observed or fooled by page-level JS.

Facts this module is built on, each checked against a real Chrome:

- A cross-origin iframe is its own CDP target (OOPIF) even though nodriver passes
  ``--disable-features=IsolateOrigins,site-per-process``. It is absent from the
  parent's ``Page.getFrameTree`` and needs its own connection.
- Content quads are relative to the viewport of the node's own target: already
  page-global for an in-process frame, but local for an OOPIF. Crossing into an
  OOPIF needs the owner element's content box added, or a click lands elsewhere.
- ``DOM.describeNode`` keeps answering for a node from a previous document. Only
  ``DOM.resolveNode`` + ``isConnected`` proves a node is still usable.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from nodriver import Element, Tab, cdp
from nodriver.core.element import Position

from .helpers import target_key, transport_lost
from .snapshot import build_snapshot_js
from .state import BrowserState

MAX_FRAMES = 25
_FRAME_TIMEOUT = 5.0
# All child frames together, so that hung ones cannot use up the caller's whole
# timeout and lose the main frame's result along with them.
_CHILD_FRAMES_BUDGET = 12.0
_RESOLVE_TIMEOUT = 10.0
_group_ids = itertools.count(1)

COUNT_KEYS = (
    "matched", "measured", "eligible", "returned", "omitted",
    "hidden_skipped", "offscreen_skipped",
)


class StaleRefError(ValueError):
    """A ref that can no longer be acted on, with what the caller should do next."""


@dataclass
class RefEntry:
    backend_node_id: int
    conn: Any
    root: Any
    frame_id: str
    frame_label: str = ""
    # (parent connection, owner <iframe> backendNodeId), outermost first, one per
    # OOPIF boundary between the root target and this node.
    owners: tuple = ()


# target key of the root tab -> ref -> entry. Replaced wholesale by each snapshot.
_REF_MAPS: dict[str, dict[str, RefEntry]] = {}
# target key of the root tab -> OOPIF target id -> connection.
_FRAME_CONNS: dict[str, dict[str, Tab]] = {}
# (connection target key, frame id) -> (loader id, isolated world context id).
_WORLDS: dict[tuple[str, str], tuple[str, int]] = {}


async def frame_offset(owners: tuple) -> tuple[float, float]:
    """Translation from an OOPIF's viewport to the root viewport."""
    dx = dy = 0.0
    for parent, owner_backend in owners:
        box = await parent.send(cdp.dom.get_box_model(backend_node_id=owner_backend))
        dx += box.content[0]
        dy += box.content[1]
    return dx, dy


class RefElement(Element):
    """An Element that reports root-viewport coordinates wherever it lives.

    Every tool positioning the mouse does so through get_position() and the root
    tab, so correcting the position here fixes all of them for OOPIF content.

    The entry lives on `_ref_entry` because Element.__setattr__ forwards any name
    without a leading underscore to the HTML attributes, silently.
    """

    _ref_entry: Optional[RefEntry] = None

    @property
    def ref_entry(self) -> Optional[RefEntry]:
        return self._ref_entry

    async def get_position(self, abs: bool = False):
        pos = await super().get_position(abs)
        entry = self.ref_entry
        if pos is None or entry is None or not entry.owners:
            return pos
        dx, dy = await frame_offset(entry.owners)
        return Position([
            pos.left + dx, pos.top + dy,
            pos.right + dx, pos.top + dy,
            pos.right + dx, pos.bottom + dy,
            pos.left + dx, pos.bottom + dy,
        ])


def _ref_range(refs: dict[str, RefEntry]) -> str:
    return f"e0..e{len(refs) - 1}" if refs else "none"


async def resolve_ref(ref: str, tab: Optional[Tab] = None) -> RefElement:
    """Turn a snapshot ref into a live element, or say exactly why it cannot be."""
    tab = tab if tab is not None else BrowserState.active_tab()
    refs = _REF_MAPS.get(target_key(tab))
    if refs is None:
        raise StaleRefError(
            f"ref {ref} is unknown: no snapshot has been taken on this tab — "
            "call browser_snapshot first."
        )
    entry = refs.get(ref)
    if entry is None:
        raise StaleRefError(
            f"ref {ref} is not in the latest snapshot of this tab "
            f"({_ref_range(refs)}). Every snapshot replaces all refs — "
            "use one from the most recent browser_snapshot."
        )

    async def _load() -> RefElement:
        node = await entry.conn.send(
            cdp.dom.describe_node(backend_node_id=entry.backend_node_id, depth=-1)
        )
        # tree=node: Element.get_position reads .parent, which raises with no tree.
        el = RefElement(node, entry.conn, tree=node)
        el._ref_entry = entry
        connected = await apply_isolated(el, "(e) => e.isConnected")
        if connected is not True:
            raise LookupError("the element was removed from the document")
        return el

    try:
        return await asyncio.wait_for(_load(), timeout=_RESOLVE_TIMEOUT)
    except asyncio.TimeoutError:
        raise StaleRefError(
            f"ref {ref} did not answer within {_RESOLVE_TIMEOUT:.0f}s — its frame may "
            "be gone. Take a new browser_snapshot."
        ) from None
    except Exception as exc:
        if entry.conn is tab and transport_lost(str(exc)):
            raise
        raise StaleRefError(
            f"ref {ref} is stale: the element no longer exists ({exc}). The page "
            "changed or navigated — take a new browser_snapshot."
        ) from exc


def forget_refs(tab_keys) -> None:
    """Drop everything held for root tabs that no longer exist."""
    for key in tab_keys:
        _REF_MAPS.pop(key, None)
        conns = _FRAME_CONNS.pop(key, {})
        owners = {key, *conns}
        for world_key in [w for w in _WORLDS if w[0] in owners]:
            _WORLDS.pop(world_key, None)
        for conn in conns.values():
            _close_soon(conn)


def _close_soon(conn: Any) -> None:
    try:
        asyncio.get_running_loop().create_task(close_connection(conn))
    except RuntimeError:
        pass


async def close_connection(conn: Any) -> None:
    """Close a CDP connection without touching the browser process."""
    # nodriver renamed this: disconnect() up to 0.48, aclose() from 0.50.
    close = getattr(conn, "aclose", None) or getattr(conn, "disconnect", None)
    try:
        if close is not None:
            await close()
    except Exception:
        pass


async def _frame_conn(root: Tab, frame_id: str) -> Optional[Tab]:
    """Connection to the OOPIF target hosting frame_id. None when there is none."""
    root_key = target_key(root)
    conns = _FRAME_CONNS.setdefault(root_key, {})
    if frame_id in conns:
        return conns[frame_id]
    targets = await root.send(cdp.target.get_targets())
    info = next(
        (t for t in targets if str(t.target_id) == str(frame_id) and t.type_ == "iframe"),
        None,
    )
    if info is None:
        return None
    browser = root.browser
    conn = _connect_to_target(browser, info)
    conns[frame_id] = conn
    return conn


def _connect_to_target(browser: Any, info: Any) -> Tab:
    """A connection to one CDP target, on whichever design this nodriver has.

    Up to 0.48 a Tab is built from its own /devtools/page/<id> URL. From 0.50 it opens the
    browser endpoint and attaches to the target as a session, with the Browser as parent.
    """
    if "websocket_url" in inspect.signature(Tab.__init__).parameters:
        return Tab(
            f"ws://{browser.config.host}:{browser.config.port}/devtools/page/{info.target_id}",
            target=info,
            browser=browser,
        )
    return Tab(target=info, parent=browser, auto_attach=False)


def _flatten_frames(node: Any) -> list[Any]:
    frames = [node.frame]
    for child in node.child_frames or []:
        frames.extend(_flatten_frames(child))
    return frames


async def _isolated_world(conn: Any, frame: Any) -> int:
    key = (target_key(conn), str(frame.id_))
    cached = _WORLDS.get(key)
    if cached and cached[0] == str(frame.loader_id):
        return cached[1]
    ctx = await conn.send(cdp.page.create_isolated_world(frame_id=frame.id_))
    _WORLDS[key] = (str(frame.loader_id), ctx)
    return ctx


def _world_of(el: Any) -> tuple[Any, str]:
    """The connection and frame an element lives in."""
    entry: Optional[RefEntry] = getattr(el, "ref_entry", None)
    conn = entry.conn if entry is not None else el.tab
    # In Chrome the main frame's id is its target's id, for a page and for an OOPIF alike.
    frame_id = entry.frame_id if entry is not None and entry.frame_id else str(conn.target.target_id)
    return conn, str(frame_id)


async def _world_context(conn: Any, frame_id: str, fresh: bool = False) -> int:
    """The isolated world of a frame, without asking for the frame tree first.

    A context kept from before a navigation no longer exists, which the next use reports;
    the caller then asks again with fresh=True. The empty loader id makes the snapshot,
    which knows the real one, replace this entry rather than trust it.
    """
    key = (target_key(conn), frame_id)
    cached = _WORLDS.get(key)
    if cached and not fresh:
        return cached[1]
    ctx = await conn.send(cdp.page.create_isolated_world(frame_id=cdp.page.FrameId(frame_id)))
    _WORLDS[key] = ("", ctx)
    return ctx


async def apply_isolated(el: Any, js_function: str) -> Any:
    """Element.apply in the frame's isolated world, by value. Raises when the script throws.

    Element.apply runs in the page's own world, where the page can have replaced
    getBoundingClientRect, elementFromPoint or getComputedStyle with its own and so watch
    every hit test: 18 such calls for one click. The DOM is shared between the worlds and
    the page's replacements are not, so the answers are the same and the page sees nothing.
    Falls back to the page's world when the isolated one cannot be used.
    """
    try:
        conn, frame_id = _world_of(el)
    except Exception:
        return await el.apply(js_function)
    for fresh in (False, True):
        group = new_object_group()
        try:
            ctx = await _world_context(conn, frame_id, fresh)
            remote = await conn.send(cdp.dom.resolve_node(
                backend_node_id=el.backend_node_id, object_group=group, execution_context_id=ctx))
            result, exc = await conn.send(cdp.runtime.call_function_on(
                js_function, object_id=remote.object_id,
                arguments=[cdp.runtime.CallArgument(object_id=remote.object_id)],
                return_by_value=True,
            ))
        except Exception:
            continue
        finally:
            await release_group(conn, group)
        if exc is not None:
            detail = getattr(exc.exception, "description", None) if exc.exception else None
            raise RuntimeError(detail or exc.text or "script threw")
        return result.value
    return await el.apply(js_function)


async def evaluate_isolated(tab: Any, expression: str) -> Any:
    """Evaluate in the isolated world of a tab's main frame, by value.

    For reading the page without calling anything the page can have replaced. Falls back
    to the page's world when the isolated one cannot be used.
    """
    for fresh in (False, True):
        try:
            ctx = await _world_context(tab, str(tab.target.target_id), fresh)
            result, exc = await tab.send(cdp.runtime.evaluate(
                expression, context_id=ctx, return_by_value=True))
        except Exception:
            continue
        if exc is not None:
            detail = getattr(exc.exception, "description", None) if exc.exception else None
            raise RuntimeError(detail or exc.text or "script threw")
        return result.value
    value = await tab.evaluate(expression, return_by_value=True)
    return value.value if hasattr(value, "value") and not isinstance(value, str) else value


async def _props(conn: Any, object_id: Any) -> dict[str, Any]:
    result = await conn.send(cdp.runtime.get_properties(object_id, own_properties=True))
    return {p.name: p.value for p in result[0] if p.value is not None}


def new_object_group() -> str:
    """A group of its own for one operation: releasing a shared one frees the handles
    of whatever else is running."""
    return f"mcp-{next(_group_ids)}"


async def release_group(conn: Any, group: str) -> None:
    try:
        await conn.send(cdp.runtime.release_object_group(group))
    except Exception:
        pass


async def evaluate_parts(conn: Any, expression: str, object_group: str, **kwargs: Any) -> dict[str, Any]:
    """Evaluate an expression returning an object, keeping its properties as handles.

    This is how an element crosses from JS to the server without being tagged in
    the DOM: the handle IS the reference. The caller releases object_group.
    """
    remote, exc = await conn.send(cdp.runtime.evaluate(
        expression, object_group=object_group, return_by_value=False, **kwargs,
    ))
    if exc is not None:
        detail = getattr(exc.exception, "description", None) if exc.exception else None
        raise RuntimeError(detail or exc.text or "script threw")
    if remote.object_id is None:
        raise ValueError(f"script returned no object: {remote.value!r}")
    return await _props(conn, remote.object_id)


async def _backend_ids(conn: Any, array_handle: Any) -> list[Optional[Any]]:
    """One describeNode per element, concurrently. None marks a failed lookup."""
    if array_handle is None or array_handle.object_id is None:
        return []
    items = await _props(conn, array_handle.object_id)
    handles = [items[str(i)] for i in range(sum(1 for k in items if k.isdigit()))]
    nodes = await asyncio.gather(
        *(conn.send(cdp.dom.describe_node(object_id=h.object_id)) for h in handles),
        return_exceptions=True,
    )
    return [None if isinstance(n, BaseException) else n for n in nodes]


_OWNERS_JS = """(() => {
  const acc = [];
  const visit = (root) => {
    root.querySelectorAll('iframe, frame').forEach((f) => acc.push(f));
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = w.nextNode())) if (n.shadowRoot) visit(n.shadowRoot);
  };
  visit(document);
  return {owners: acc};
})()"""


async def frame_world(conn: Any, frame: Any) -> int:
    """An isolated world for one frame, reused until the frame navigates."""
    return await _isolated_world(conn, frame)


async def all_frames(tab: Tab) -> list[dict]:
    """Every frame under a tab with the connection that can reach it.

    Page.getFrameTree stops at process boundaries, so each frame's <iframe> owners
    are checked for frames the tree did not list. A frame that cannot be reached is
    listed with conn=None and its scan_error rather than left out.
    """
    out: list[dict] = []

    async def visit(conn: Any, base_depth: int) -> None:
        async def walk(node: Any, depth: int, local_ids: set[str]) -> None:
            record = {"conn": conn, "frame": node.frame, "depth": depth, "scan_error": None}
            out.append(record)
            remote_ids: list[str] = []
            group = new_object_group()
            try:
                ctx = await _isolated_world(conn, node.frame)
                parts = await evaluate_parts(conn, _OWNERS_JS, group, context_id=ctx)
                owners = await _backend_ids(conn, parts.get("owners"))
                remote_ids = [
                    str(o.frame_id) for o in owners
                    if o is not None and o.frame_id and str(o.frame_id) not in local_ids
                ]
            except Exception as exc:
                # Frames under this one may exist and go unlisted, so it is reported.
                record["scan_error"] = str(exc)[:160] or type(exc).__name__
            finally:
                await release_group(conn, group)
            for child in node.child_frames or []:
                await walk(child, depth + 1, local_ids)
            for frame_id in remote_ids:
                try:
                    child_conn = await _frame_conn(tab, frame_id)
                    if child_conn is None:
                        raise LookupError("no CDP target for this frame")
                    await asyncio.wait_for(visit(child_conn, depth + 1), _FRAME_TIMEOUT)
                except Exception as exc:
                    out.append({"conn": None, "frame": None, "frame_id": frame_id,
                                "depth": depth + 1,
                                "scan_error": str(exc)[:160] or type(exc).__name__})

        tree = await conn.send(cdp.page.get_frame_tree())
        ids = {str(f.id_) for f in _flatten_frames(tree)}
        await walk(tree, base_depth, ids)

    await visit(tab, 0)
    return out


def parse_frame_payload(raw: Any) -> dict:
    """Validate what the snapshot script returned for one frame."""
    try:
        payload = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"snapshot script returned invalid JSON: {e}") from None
    if not isinstance(payload, dict):
        raise ValueError("snapshot script returned an invalid element list")
    if payload.get("ok") is False:
        raise ValueError(payload.get("error") or "snapshot script refused the scope")
    elements = payload.get("elements")
    if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
        raise ValueError("snapshot script returned an invalid element list")
    return payload


@dataclass
class SnapshotWalk:
    elements: list[dict] = field(default_factory=list)
    refs: dict[str, RefEntry] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=lambda: {
        **{k: 0 for k in COUNT_KEYS},
        "truncated": False,
        "excluded": {"unnamed_decorative": 0, "role": 0, "name": 0, "label": 0},
        "container": {"requested": False, "selector": "", "matched": 0, "found": False},
        "unresolved": 0,
    })
    frames: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    skipped: int = 0
    loaders: list[str] = field(default_factory=list)

    def doc_token(self) -> str:
        return "|".join(self.loaders)


def _merge_counts(walk: SnapshotWalk, payload: dict) -> None:
    counts = walk.counts
    for key in COUNT_KEYS:
        counts[key] += int(payload.get(key) or 0)
    counts["truncated"] = counts["truncated"] or bool(payload.get("truncated"))
    for key, value in (payload.get("excluded") or {}).items():
        counts["excluded"][key] = counts["excluded"].get(key, 0) + int(value or 0)
    container = payload.get("container") or {}
    merged = counts["container"]
    merged["requested"] = merged["requested"] or bool(container.get("requested"))
    merged["selector"] = container.get("selector") or merged["selector"]
    merged["matched"] += int(container.get("matched") or 0)
    merged["found"] = merged["found"] or bool(container.get("found"))


async def _walk_frame(
    root: Any, conn: Any, frame: Any, owners: tuple, options: dict,
    walk: SnapshotWalk, is_root: bool,
) -> list[tuple[Any, Any]]:
    """Measure one frame into `walk`; return its (owner backend id, frame id) pairs."""
    budget = None
    if options["max_elements"] > 0:
        budget = max(0, options["max_elements"] - len(walk.elements))
    expression = build_snapshot_js(options, budget=budget)
    group = new_object_group()
    context_id: Optional[int] = None
    try:
        try:
            context_id = await _isolated_world(conn, frame)
            parts = await evaluate_parts(conn, expression, group, context_id=context_id)
        except Exception as exc:
            # A cached world dies with its execution context; rebuild it once.
            _WORLDS.pop((target_key(conn), str(frame.id_)), None)
            if context_id is None or transport_lost(str(exc)):
                raise
            context_id = await _isolated_world(conn, frame)
            parts = await evaluate_parts(conn, expression, group, context_id=context_id)
        payload_handle = parts.get("json")
        payload = parse_frame_payload(payload_handle.value if payload_handle else None)
        nodes = await _backend_ids(conn, parts.get("els"))
        owner_nodes = await _backend_ids(conn, parts.get("owners"))
    finally:
        await release_group(conn, group)

    label = "" if is_root else f"f{len(walk.frames)}"
    walk.frames.append({"label": label or "main", "url": str(frame.url or "")[:200]})
    walk.loaders.append(str(frame.loader_id))
    _merge_counts(walk, payload)
    entries = payload["elements"]
    if len(nodes) != len(entries):
        raise ValueError(
            f"snapshot returned {len(entries)} elements but {len(nodes)} handles"
        )
    for entry, node in zip(entries, nodes):
        if node is None:
            # The element vanished between the walk and the lookup. Counted, so the
            # report still accounts for it, and never given a ref that cannot work.
            walk.counts["unresolved"] += 1
            walk.counts["returned"] -= 1
            continue
        ref = f"e{len(walk.elements)}"
        entry["ref"] = ref
        if label:
            entry["frame"] = label
        walk.elements.append(entry)
        walk.refs[ref] = RefEntry(
            backend_node_id=node.backend_node_id, conn=conn, root=root,
            frame_id=str(frame.id_), frame_label=label, owners=owners,
        )
    return [
        (node.backend_node_id, str(node.frame_id))
        for node in owner_nodes if node is not None and node.frame_id
    ]


async def collect_snapshot(tab: Tab, options: dict) -> SnapshotWalk:
    """Walk the tab's frames and return elements plus their server-side refs.

    The main frame failing is an error. A child frame failing is not — it is
    recorded in `failed` and reported, so a page never reads as having nothing in
    a frame that simply could not be measured.
    """
    walk = SnapshotWalk()
    root_key = target_key(tab)
    used_conns: set[str] = set()
    deadline = time.monotonic() + _CHILD_FRAMES_BUDGET

    async def visit(conn: Any, owners: tuple) -> None:
        tree = await asyncio.wait_for(conn.send(cdp.page.get_frame_tree()), _FRAME_TIMEOUT)
        frames = _flatten_frames(tree)
        local_ids = {str(f.id_) for f in frames}
        oopifs: list[tuple[Any, str]] = []
        for frame in frames:
            is_root = conn is tab and frame is frames[0]
            if not is_root and not options.get("include_frames", True):
                continue
            remaining = deadline - time.monotonic()
            if len(walk.frames) >= MAX_FRAMES or (not is_root and remaining <= 0):
                walk.skipped += 1
                continue
            try:
                walking = _walk_frame(tab, conn, frame, owners, options, walk, is_root)
                # The main frame answers to the caller's timeout alone; it is what
                # the snapshot is for and may legitimately be slow.
                pairs = await (walking if is_root else asyncio.wait_for(
                    walking, timeout=min(_FRAME_TIMEOUT, remaining)))
            except Exception as exc:
                if is_root:
                    raise
                walk.failed.append({
                    "url": str(frame.url or "")[:200],
                    "error": str(exc)[:160] or type(exc).__name__,
                })
                continue
            for owner_backend, frame_id in pairs:
                if frame_id not in local_ids:
                    oopifs.append((owner_backend, frame_id))
        if not options.get("include_frames", True):
            return
        for owner_backend, frame_id in oopifs:
            if len(walk.frames) >= MAX_FRAMES or deadline <= time.monotonic():
                walk.skipped += 1
                continue
            try:
                child = await _frame_conn(tab, frame_id)
                if child is None:
                    raise LookupError("no CDP target for this frame")
                used_conns.add(frame_id)
                await visit(child, owners + ((conn, owner_backend),))
            except Exception as exc:
                walk.failed.append({
                    "url": f"frame {frame_id[:12]}",
                    "error": str(exc)[:160] or type(exc).__name__,
                })

    await visit(tab, ())

    # Frames that went away keep a websocket open until told otherwise.
    conns = _FRAME_CONNS.get(root_key, {})
    for frame_id in [f for f in conns if f not in used_conns]:
        _close_soon(conns.pop(frame_id))
        for world_key in [w for w in _WORLDS if w[0] == frame_id]:
            _WORLDS.pop(world_key, None)
    _REF_MAPS[root_key] = walk.refs
    return walk


async def outer_blocker(el: Any, x: float, y: float) -> Optional[str]:
    """What stops a click at root-viewport (x, y) from entering the element's frame.

    The hit-test inside a frame cannot see an overlay drawn by the parent page, so
    for frame content the root target is asked too. Main-frame elements are fully
    covered by the local probe and return None without a CDP call.
    """
    entry: Optional[RefEntry] = getattr(el, "ref_entry", None)
    if entry is None or not entry.frame_label:
        return None
    # DOM.getNodeForLocation takes DOCUMENT coordinates, unlike every other position
    # here. Passing viewport ones hit-tests the wrong spot once the page is scrolled.
    try:
        scroll_x, scroll_y = json.loads(await evaluate_isolated(
            entry.root, "JSON.stringify([window.scrollX, window.scrollY])"))
        located = await entry.root.send(cdp.dom.get_node_for_location(
            int(x + scroll_x), int(y + scroll_y), include_user_agent_shadow_dom=False,
        ))
    except Exception:
        return f"point ({int(x)},{int(y)}) is outside the page viewport"
    backend, frame_id = located[0], located[1]
    if entry.owners:
        expected = entry.owners[0][1]
        if backend == expected:
            return None
    elif str(frame_id) == entry.frame_id:
        return None
    try:
        node = await entry.root.send(cdp.dom.describe_node(backend_node_id=backend))
        name = node.local_name or node.node_name
    except Exception:
        name = "another element"
    return f"frame {entry.frame_label} is obscured by <{name}> at ({int(x)},{int(y)})"


async def ensure_in_root_view(el: Any) -> bool:
    """Bring frame content into the page's viewport. True if it had to scroll.

    A frame's own probe says "in viewport" for an element whose whole frame sits
    below the fold, because it only knows about the frame.
    """
    entry: Optional[RefEntry] = getattr(el, "ref_entry", None)
    if entry is None or not entry.frame_label:
        return False
    pos = await el.get_position()
    width, height = json.loads(await evaluate_isolated(
        entry.root, "JSON.stringify([innerWidth, innerHeight])"))
    if pos is not None and (0 <= pos.left + pos.width / 2 <= width
                            and 0 <= pos.top + pos.height / 2 <= height):
        return False
    for parent, owner_backend in entry.owners:
        await parent.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=owner_backend))
    await entry.conn.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=entry.backend_node_id))
    return True


async def active_element(tab: Any) -> Optional[Element]:
    """The focused element, as a handle — replaces tagging it with an attribute."""
    group = new_object_group()
    remote, exc = await tab.send(cdp.runtime.evaluate(
        "(() => { let a = document.activeElement;"
        " while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;"
        " return a; })()",
        object_group=group, return_by_value=False,
    ))
    if exc is not None or remote.object_id is None:
        return None
    try:
        node = await tab.send(cdp.dom.describe_node(object_id=remote.object_id, depth=-1))
        return Element(node, tab, tree=node)
    finally:
        await release_group(tab, group)
