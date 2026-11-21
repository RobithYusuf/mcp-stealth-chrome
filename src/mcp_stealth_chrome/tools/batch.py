"""Batch tools: batch_actions (a flat list of actions), fill_form and navigate_and_snapshot, and the
table that maps the six legacy action names to their tools.

Moved out of server.py. _inner_arguments_error, _WORKFLOW_TOOLS and _register_workflow_tools stay
there and are reached as _srv.<name> at call time, so a test that patches server.<name> still
reaches the code.
"""

from typing import Optional

from .. import antibot
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
    A site's verification page (antibot_tripwire, default pause) stops the batch before its next action:
    an error starting "antibot_paused:" with what fired and resume_with.
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
    previous = None  # the tool of the last action run, for the anti-bot tripwire
    paused = None    # (index, tool, the pause's text, whether that action ran)
    run = _srv._TRIPWIRE_RUN.set({"tool": "batch_actions"})
    try:
        for i, act in enumerate(actions):
            if not isinstance(act, dict):
                results.append(f"[{i}] ?: ERR not an action object but {type(act).__name__}: "
                               f"{str(act)[:80]}")
                continue
            atype = act.get("type")
            try:
                tool, args = _batch_action_call(act)
                warning = ""
                if tool is not None:
                    # A click before may have sent the page to a verification page: stop before
                    # this action, whatever stop_on_error says.
                    said = await _srv._tripwire_step(previous, tool)
                    if said and said.startswith(antibot.PAUSE_PREFIX):
                        paused = (i, atype, said, False)
                        results.append(f"[{i}] {atype}: not run, paused (antibot) before it")
                        break
                    warning = f"\n    {said}" if said else ""
                if tool is None:
                    r = err(f"unknown action type: {atype}")
                else:
                    r = (_srv._inner_arguments_error(tool, args) or presence.refusal(tool, BrowserState.browser)
                         or await _srv._WORKFLOW_TOOLS[tool](**args))
                    previous = tool
                if isinstance(r, str) and r.startswith(antibot.PAUSE_PREFIX):
                    paused = (i, atype, r, True)
                    results.append(f"[{i}] {atype}: paused (antibot): it reached a verification page")
                    break
                # A warning (antibot_tripwire=warn) goes after the line: a failure stays countable.
                results.append(f"[{i}] {atype}: {str(r)[:160]}{warning}")
                # A tool can return an "Error: ..." string WITHOUT raising — honor
                # stop_on_error for those too, not just for exceptions.
                if isinstance(r, str) and r.startswith("Error:") and act.get("stop_on_error"):
                    break
            except Exception as e:
                results.append(f"[{i}] {atype}: ERR {e}")
                if act.get("stop_on_error"):
                    break
    finally:
        _srv._TRIPWIRE_RUN.reset(run)
    n_failed = sum(1 for ln in results if ": Error:" in ln or ": ERR " in ln)
    if paused is not None:
        return _paused_batch(paused, results, n_failed, len(actions))
    header = f"{len(results)} actions, {n_failed} failed" if n_failed else f"{len(results)} actions OK"
    return ok(header + "\n" + "\n".join(results))


def _utf8_prefix(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - 3].decode("utf-8", "ignore") + "..."


def _paused_batch(paused: tuple, results: list, n_failed: int, total: int) -> str:
    """Render a pause with a hard whole-response bound, including every action line."""
    index, atype, text, ran = paused
    lines = text.splitlines()
    detail = "\n".join(lines[1:])
    if ran:
        head = f"batch_actions stopped at action [{index}] ({atype}): it reached a verification page"
        resume = (f"resume_with: batch_actions with actions[{index}:], once the person is done "
                  f"(action [{index}] runs again)")
    else:
        head = (f"batch_actions stopped before action [{index}] ({atype}): the tab is on a "
                "verification page")
        resume = (f"resume_with: batch_actions with actions[{index}:], once the person is done "
                  f"(the actions before it are not repeated)")
    if not detail:
        head += f" ({lines[0][len(antibot.PAUSE_PREFIX):].strip()})"
    # Preserve resume and counts even when a person's rule has a long Unicode description.
    # Keep the whole text if it fits; otherwise the counts stand in for all action previews.
    header = f"{antibot.PAUSED}: {_utf8_prefix(head, 230)}."
    footer = f"{resume}\n{index} of {total} actions ran before it, {n_failed} failed"
    summary = "/ action previews omitted"
    budget = 1023 - len(err(f"{header}\n\n{footer}\n{summary}").encode())
    if len(detail.encode()) > budget:
        fields = antibot.details(text)
        detail = "\n".join(f"{k}: {_utf8_prefix(fields[k], cap)}" for k, cap in
                           (("trigger", 170), ("page", 160), ("next", 170)) if k in fields)
    base = err(f"{header}\n{_utf8_prefix(detail, budget)}\n{footer}")
    if sum(len(line.encode()) + 1 for line in results) + len(base.encode()) < 1024:
        return base + "\n" + "\n".join(results)
    return base + "\n" + summary


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
