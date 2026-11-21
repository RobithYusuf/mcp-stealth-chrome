"""Batch tools: batch_actions (a flat list of actions), fill_form and navigate_and_snapshot, and the
table that maps the six legacy action names to their tools.

Moved out of server.py. _inner_arguments_error, _WORKFLOW_TOOLS and _register_workflow_tools stay
there and are reached as _srv.<name> at call time, so a test that patches server.<name> still
reaches the code.
"""

from typing import Optional

from .. import server as _srv
from .._app import mcp
from . import page as _page
from .. import presence
from ..helpers import err, ok, parse_json
from ..state import BrowserState


@mcp.tool()
async def batch_actions(actions: list[dict]) -> str:
    """Execute a flat list of actions in order and report each one.

    Each action: {type: click|fill|type|wait|press|navigate, ...params}, or the name
    of any workflow_run tool with its arguments alongside, such as
    {"type": "select_option", "selector": "#c", "value": "x"}, or
    {"type": "sleep", "seconds": 2} ({"min": 6, "max": 11} pauses a random time in that range, at most 300 s).
    The six short names (click, fill, type, press, wait, navigate) pass every other key on
    to their tool, and a key the tool does not take fails that action by name.
    Add "stop_on_error": true to an action to stop there if it fails.
    Example: [{"type":"click","ref":"e3"},{"type":"fill","ref":"e4","value":"x"}]

    For branching, loops or using one step's result in another, use workflow_run.
    """
    if not isinstance(actions, list):
        actions = parse_json(actions, None)
    # A default of [] turned unparseable input into "0 actions OK": nothing ran, reported
    # as a batch that worked.
    if not isinstance(actions, list):
        return err("batch_actions: actions must be a JSON list of action objects; nothing ran")
    if not actions:
        return err("batch_actions: no actions given; nothing ran")
    _srv._register_workflow_tools()
    results = []
    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            results.append(f"[{i}] ?: ERR not an action object but {type(act).__name__}: "
                           f"{str(act)[:80]}")
            continue
        atype = act.get("type")
        try:
            tool, args = _batch_action_call(act)
            if tool is None:
                r = err(f"unknown action type: {atype}")
            else:
                r = (_srv._inner_arguments_error(tool, args) or presence.refusal(tool, BrowserState.browser)
                     or await _srv._WORKFLOW_TOOLS[tool](**args))
            results.append(f"[{i}] {atype}: {str(r)[:160]}")
            # A tool can return an "Error: ..." string WITHOUT raising — honor
            # stop_on_error for those too, not just for exceptions.
            if isinstance(r, str) and r.startswith("Error:") and act.get("stop_on_error"):
                break
        except Exception as e:
            results.append(f"[{i}] {atype}: ERR {e}")
            if act.get("stop_on_error"):
                break
    n_failed = sum(1 for ln in results if ": Error:" in ln or ": ERR " in ln)
    header = f"{len(results)} actions, {n_failed} failed" if n_failed else f"{len(results)} actions OK"
    return ok(header + "\n" + "\n".join(results))


# The six legacy batch names and the defaults they always had. Every other key of the action
# is passed on too: "wait" dropped frame_url and "navigate" expect_url, unsaid.
_BATCH_ALIASES = {
    "click": ("click", {"ref": None, "selector": None, "humanize": False}),
    "fill": ("fill", {"ref": None, "selector": None, "value": ""}),
    "type": ("type_text", {"text": "", "humanize": False}),
    "press": ("press_key", {"key": "Enter"}),
    "wait": ("wait_for", {"selector": None, "text": None, "timeout": 5.0}),
    "navigate": ("navigate", {"url": None}),
}


def _batch_action_call(act: dict) -> tuple[Optional[str], dict]:
    """Map a batch action to (tool, args). The six legacy names keep their defaults."""
    atype = act.get("type")
    rest = {k: v for k, v in act.items() if k not in ("type", "stop_on_error")}
    if atype in _BATCH_ALIASES:
        tool, defaults = _BATCH_ALIASES[atype]
        return tool, {**defaults, **rest}
    if atype in _srv._WORKFLOW_TOOLS:
        return atype, rest
    return None, {}


@mcp.tool()
async def fill_form(fields: list[dict], submit_ref: Optional[str] = None) -> str:
    """Fill multiple fields then optionally submit.

    fields: [{ref: "e1", value: "..."}, {selector: "#email", value: "..."}]
    """
    if not isinstance(fields, list):
        fields = parse_json(fields, [])
    if not fields:
        return err("fill_form: no fields given")
    results = []
    failed = 0
    for f in fields:
        if "value" not in f:
            # Filling with a default would silently empty a field the caller never meant to.
            failed += 1
            results.append(f"{f.get('ref') or f.get('selector')}: Error: no value given")
            continue
        r = str(await _srv.fill(ref=f.get("ref"), selector=f.get("selector"), value=f["value"]))
        failed += r.startswith("Error:")
        results.append(f"{f.get('ref') or f.get('selector')}: {r}")
    if failed:
        if submit_ref:
            results.append("submit: not clicked, because a field failed")
        return err(f"{failed} of {len(fields)} field(s) failed\n" + "\n".join(results))
    if submit_ref:
        r = str(await _srv.click(ref=submit_ref))
        results.append(f"submit: {r}")
        if r.startswith("Error:"):
            return err("the fields were filled but the submit failed\n" + "\n".join(results))
    return ok("\n".join(results))


@mcp.tool()
async def navigate_and_snapshot(
    url: str,
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Navigate then immediately snapshot — common pattern.

    The navigation's observed postconditions are kept above the snapshot: a
    snapshot of a page you were redirected to reads exactly like a snapshot of
    the page you asked for. A failed expect_url / expect_status aborts before
    the snapshot rather than describing the wrong page. See navigate().
    """
    nav = await _srv.navigate(url, expect_url=expect_url, expect_status=expect_status)
    if str(nav).startswith("Error:"):
        return nav
    snapshot = await _page.browser_snapshot()
    if str(snapshot).startswith("Error:"):
        # A failed snapshot does not make the navigation unobserved — keep its
        # postconditions rather than dropping them with the failure.
        return err(f"navigated, then the snapshot failed.\n{nav}\n{snapshot}")
    return ok(f"{nav}\n\n{snapshot}")
