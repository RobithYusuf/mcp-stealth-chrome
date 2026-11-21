"""Starting every target a CDP client may hold for the debugger, over a new connection.

A client that attaches with `Target.setAutoAttach(waitForDebuggerOnStart)` holds each new tab,
popup and frame until it sends `Runtime.runIfWaitingForDebugger`. When that client's connection
goes away while the server lives, Chrome keeps a held `window.open` popup (and with it its
opener) frozen (measured, Chrome 157). This releases them: attach, run, detach, for every
target, over a fresh connection. block_ads (adblock.py) and body saving (body_saver.py) both
hold targets and both end with this. It imports nothing of either, nor the adblock package.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

# Targets that can be held: pages, frames and workers. "other", "browser_ui", "tab" and the
# like are Chrome's own.
RELEASE_TYPES = {"page", "iframe", "worker", "shared_worker", "service_worker"}


def release_targets(ws_url: str, budget: float, progress: dict) -> None:
    """The release itself: attach, Runtime.runIfWaitingForDebugger (a no-op on a running
    target), detach, for every target, scanning again until a pass finds none it has not
    released, since a popup can open meanwhile. Writes what it saw and did into progress."""
    from websockets.sync.client import connect
    ends = time.monotonic() + budget
    next_id = [0]

    def call(ws, method: str, params: Optional[dict] = None, sid: Optional[str] = None) -> dict:
        next_id[0] += 1
        message: dict[str, Any] = {"id": next_id[0], "method": method, "params": params or {}}
        if sid:
            message["sessionId"] = sid
        ws.send(json.dumps(message))
        while True:
            left = ends - time.monotonic()
            if left <= 0:
                raise TimeoutError("release budget spent")
            reply = json.loads(ws.recv(timeout=left))
            if reply.get("id") == next_id[0]:
                return reply

    lock = progress["lock"]
    with connect(ws_url, max_size=None, open_timeout=max(0.05, min(3.0, budget)),
                 close_timeout=1) as ws:
        tried: set = set()
        while True:
            infos = (call(ws, "Target.getTargets").get("result") or {}).get("targetInfos") or []
            ids = [t.get("targetId") for t in infos if t.get("type") in RELEASE_TYPES]
            with lock:
                progress["seen"] = list(dict.fromkeys((progress["seen"] or []) + ids))
            new = [i for i in ids if i not in tried]
            if not new:
                break
            for target_id in new:
                tried.add(target_id)
                try:
                    attached = call(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
                    sid = (attached.get("result") or {}).get("sessionId")
                    if sid:
                        call(ws, "Runtime.runIfWaitingForDebugger", {}, sid)
                        call(ws, "Target.detachFromTarget", {"sessionId": sid})
                    with lock:
                        progress["released"].add(target_id)
                except TimeoutError:
                    raise
                except Exception:
                    pass
        with lock:
            progress["finished"] = True  # what is left is the close, which changes nothing


def release_every_target(ws_url: str, budget: float,
                         release: Optional[Callable[[str, float, dict], None]] = None,
                         thread_name: str = "cdp-release") -> str:
    """Start every target that may still be held, over a new connection. Bounded as a whole:
    it runs on a thread of its own, so a connect, send, receive or close that never returns
    cannot keep the caller past the budget. Never raises. Returns "" when every target it saw
    was released, or how many and which were not, or that it could not tell which.

    `release` is the routine that does it (release_targets unless a caller binds its own
    name for it, as adblock.py does so its tests can replace it)."""
    progress: dict[str, Any] = {"lock": threading.Lock(), "seen": None, "released": set(),
                                "finished": False, "error": None}
    do_release = release or release_targets

    def run() -> None:
        try:
            do_release(ws_url, budget, progress)
        except Exception as exc:
            with progress["lock"]:
                progress["error"] = f"{type(exc).__name__}: {exc}"[:120]

    worker = threading.Thread(target=run, name=thread_name, daemon=True)
    worker.start()
    worker.join(budget + 0.5)
    with progress["lock"]:
        seen, released = progress["seen"], set(progress["released"])
        finished, error = progress["finished"], progress["error"]
    left = [i for i in (seen or []) if i not in released]
    if finished and not left:
        return ""
    why = error or (f"not finished within {budget:g} s" if worker.is_alive() else "stopped")
    if seen is None:
        return f"could not release held targets ({why}); which are left is unknown, no target list was read"
    shown = ", ".join(left[:10]) + (f" and {len(left) - 10} more" if len(left) > 10 else "")
    return f"could not release every held target ({why}): {len(left)} target(s) not released: {shown}"


def browser_answers(ws_url: str, timeout: float) -> bool:
    """Whether the browser behind ws_url still answers on its debugging port. Blocking."""
    parts = urlsplit(ws_url)
    try:
        with urllib.request.urlopen(f"http://{parts.netloc}/json/version", timeout=timeout) as reply:
            return reply.status == 200
    except Exception:
        return False
