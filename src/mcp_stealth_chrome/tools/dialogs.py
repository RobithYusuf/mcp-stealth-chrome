"""Dialog and accessibility tools: dialog_handle, dialog_auto_handle and accessibility_snapshot,
with the per-tab and per-instance dialog settings they keep.

Moved out of server.py. _clear_tab_scoped_state there clears the per-tab registrations kept here,
so server.py re-exports _DIALOG_AUTO_CFG and _dialog_pre_action. The other helpers stay in
server.py and are reached as _srv.<name> at call time.
"""

import json
from typing import Any, Optional

from .. import server as _srv
from .._app import mcp
from ..helpers import err, ok
from ..state import BrowserState


# Read-at-fire-time config so re-arming with a new action/text updates the
# response without stacking another CDP handler. Keyed by CDP target id.
_dialog_pre_action: dict[str, Any] = {
    "action": None, "text": None, "_registered_tab_ids": set(),
}


def _instance_dialog_cfg(kind: str) -> dict:
    """Dialog settings of the current instance.

    A handler captures this when armed and is armed once per tab. Keyed by instance, not
    by session: otherwise an instance handed to another session keeps obeying its previous
    owner, while the new owner is told its setting was applied. Instance names are unique
    across sessions, so no session can reach another's settings.
    """
    return _DIALOG_INSTANCE_CFG.setdefault((BrowserState.current_instance_id, kind), {
        "action": "accept", "text": "", "enabled": False, "types": None,
    })


_DIALOG_INSTANCE_CFG: dict[tuple[str, str], dict] = {}


@mcp.tool()
async def dialog_handle(action: str = "accept", text: Optional[str] = None) -> str:
    """Answer the next alert/confirm/prompt. Call BEFORE the action that triggers it.

    A click that opened a dialog with nothing armed reports it; call this then too, and
    the dialog that is already open gets the answer.
    """
    if action not in ("accept", "dismiss"):
        return err(f"dialog_handle: action must be 'accept' or 'dismiss', not {action!r}")
    pre_cfg = _instance_dialog_cfg("pre")
    try:
        tab = BrowserState.active_tab()
        answered = await _srv._answer_open_dialog(tab, action, text)
        if answered:
            return answered
        pre_cfg["action"] = action
        pre_cfg["text"] = text
        pre_cfg["pending"] = True
        from nodriver.cdp import page as cdp_page
        tid = _srv._target_key(tab)
        if tid in _dialog_pre_action["_registered_tab_ids"]:
            # Handler already armed on this tab — config updated above; don't
            # stack a second handler (which would accumulate every call).
            return ok(f"dialog handler re-armed ({action})")

        async def handle(_event):
            # Armed for the NEXT dialog only. Answering every later one would accept a
            # destructive confirm nobody decided on.
            if not pre_cfg.get("pending"):
                return
            pre_cfg["pending"] = False
            try:
                await tab.send(cdp_page.handle_java_script_dialog(
                    accept=(pre_cfg["action"] == "accept"),
                    prompt_text=pre_cfg["text"] or "",
                ))
            except Exception:
                pass

        # nodriver up to 0.48 enabled the domain for a registered handler; 0.50 does not,
        # and without this the dialog event never arrives. Bounded, because the renderer
        # cannot answer while a dialog this server never saw is open.
        await _srv._wait(tab.send(cdp_page.enable()), timeout=5.0, what="dialog_handle")
        tab.add_handler(cdp_page.JavascriptDialogOpening, handle)
        _dialog_pre_action["_registered_tab_ids"].add(tid)
        return ok(f"dialog handler armed ({action})")
    except Exception as e:
        return err(str(e))


# ── dialog_auto_handle (persistent, type-filterable) ──────────────────────
# Read-at-fire-time config so the LLM can update action/types without
# re-registering the handler. Keyed by CDP target id to avoid duplicate handlers
# on the same tab.
_DIALOG_AUTO_CFG: dict = {
    "enabled": False, "action": "accept", "text": "",
    "types": None,  # None=all; or set like {"beforeunload"}
    "_registered_tab_ids": set(),
}


@mcp.tool()
async def dialog_auto_handle(
    action: str = "accept",
    enabled: bool = True,
    types: Optional[list[str]] = None,
    text: str = "",
) -> str:
    """Install a PERSISTENT auto-handler for native browser dialogs.
    Unlike dialog_handle (one-shot, action baked in at arm time), this
    one stays armed across many dialogs and reads its config at fire
    time — call again with new action/types to update without re-arming.

    Args:
        action: "accept" (Leave / OK) or "dismiss" (Cancel / Stay)
        enabled: True to arm, False to disable (config preserved)
        types: optional list to scope handling — any of:
               ["alert", "confirm", "prompt", "beforeunload"]
               None (default) = handle all types.
        text: prompt response when action="accept" on prompt() dialogs;
              also basic-auth "user:pass" for HTTP 401.

    Common patterns:
        # Form pages with "unsaved changes" guard — auto-leave forever
        dialog_auto_handle(action="accept", types=["beforeunload"])

        # Pages that spam alert() — auto-OK
        dialog_auto_handle(action="accept", types=["alert"])

        # Disable when done
        dialog_auto_handle(enabled=False)

    Native dialogs only (Chrome's own card UI). HTML/CSS modal overlays
    are regular DOM — use click_text("Cancel") / click_role for those.
    """
    if action not in ("accept", "dismiss"):
        return err(f"dialog_auto_handle: action must be 'accept' or 'dismiss', not {action!r}")
    unknown = set(types or ()) - {"alert", "confirm", "prompt", "beforeunload"}
    if unknown:
        return err(f"dialog_auto_handle: unknown dialog type(s) {sorted(unknown)}; "
                   f"use alert, confirm, prompt or beforeunload")
    auto_cfg = _instance_dialog_cfg("auto")
    auto_cfg["enabled"] = bool(enabled)
    auto_cfg["action"] = action
    auto_cfg["text"] = text or ""
    auto_cfg["types"] = set(types) if types else None

    if not enabled:
        return ok("dialog auto-handle disabled (config preserved — re-enable to resume)")

    try:
        tab = BrowserState.active_tab()
        tab_id = _srv._target_key(tab)
        await _srv._answer_open_dialog(tab, action, text)
        if tab_id in _DIALOG_AUTO_CFG["_registered_tab_ids"]:
            return ok(
                f"auto-handle config updated (action={action}, "
                f"types={list(types) if types else 'all'}); already armed on this tab"
            )

        from nodriver.cdp import page as cdp_page

        async def auto_handler(event):
            cfg = auto_cfg
            if not cfg["enabled"]:
                return
            ev_type = getattr(event, "type_", None) or getattr(event, "type", None)
            type_str = getattr(ev_type, "value", str(ev_type)) if ev_type else ""
            if cfg["types"] and type_str not in cfg["types"]:
                return
            try:
                await tab.send(cdp_page.handle_java_script_dialog(
                    accept=(cfg["action"] == "accept"),
                    prompt_text=cfg["text"] or "",
                ))
            except Exception:
                pass

        await _srv._wait(tab.send(cdp_page.enable()), timeout=5.0, what="dialog_auto_handle")
        tab.add_handler(cdp_page.JavascriptDialogOpening, auto_handler)
        _DIALOG_AUTO_CFG["_registered_tab_ids"].add(tab_id)
        return ok(
            f"dialog auto-handle armed (action={action}, "
            f"types={list(types) if types else 'all'})"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def accessibility_snapshot(interesting_only: bool = True) -> str:
    """Return ARIA accessibility tree of current page."""
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import accessibility as cdp_a11y
        result = await _srv._wait(tab.send(cdp_a11y.get_full_ax_tree()), what="accessibility_snapshot")
        # Filter to meaningful nodes
        nodes = result if isinstance(result, list) else []
        filtered = []
        for n in nodes[:500]:
            node_dict = {
                "role": getattr(getattr(n, "role", None), "value", None),
                "name": getattr(getattr(n, "name", None), "value", None),
                "value": getattr(getattr(n, "value", None), "value", None),
            }
            if interesting_only:
                if not node_dict["name"] and not node_dict["value"]:
                    continue
            filtered.append(node_dict)
        return ok(json.dumps(filtered, indent=2, default=str))
    except Exception as e:
        return err(str(e))
