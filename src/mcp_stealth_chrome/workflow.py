"""Declarative step engine behind workflow_run: variables, if, foreach, resume.

No expression language and no eval. A condition is a small dict of named operators
and a reference is a dotted path, so a workflow can only do what its allowlisted
tools can do. Kept free of browser and MCP imports so it is testable by itself.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .antibot import PAUSE_PREFIX, PAUSED, details as pause_details

BRANCHES_VAR = "__branches__"
MAX_EXECUTED_STEPS = 2000
MAX_FOREACH_ITEMS = 1000
MAX_REPORTED_ENTRIES = 200
# Failed or ignored steps past MAX_REPORTED_ENTRIES that a shorter report still shows, as many as
# failed_paths lists: past the cap their entries are otherwise gone.
MAX_LATE_FAILURES = 100
RESULT_PREVIEW = 500
# How far back from RESULT_PREVIEW a cut looks for a line break or ", " to end on.
PREVIEW_BREAK_WINDOW = 80
CUT_MARK = " ... [+{left} chars; the whole result is in vars when the step has save_as]"

# How much of a run the report shows. "full" is the report as it always was; the others are
# made from it by _shorten and show nothing it does not, except a last step past the entry cap.
OUTPUT_MODES = ("summary", "failures", "full")
# Steps whose result is what the caller came for. A summary keeps their result without save_as:
# "navigate, then read" is usually written without one, and dropping the read would lose it unsaid.
READERS = ("get_text", "get_attribute", "get_url", "get_links", "get_html", "query_selector_all",
           "extract_table", "extract_structured", "browser_snapshot", "describe_page", "search_page",
           "evaluate")
# The longest one sleep step may pause. The whole workflow is one MCP call, and a client stops
# waiting for a call after its own time limit, so a long wait belongs between calls.
SLEEP_CAP_SECONDS = 300
# The sleep step is not an MCP tool, so this stands in for the schema its arguments are checked against.
SLEEP_PARAMETERS = {"type": "object", "properties": {
    "seconds": {"type": "number"}, "min": {"type": "number"}, "max": {"type": "number"}}}
_pause = asyncio.sleep  # tests replace it, so no test waits for real

_WHOLE_REF = re.compile(r"^\$\{([^{}]+)\}$")
_ANY_REF = re.compile(r"\$\{([^{}]+)\}")
_SEGMENT = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")

Dispatch = Callable[[str, dict], Awaitable[Any]]
# Awaited before each tool step with (the tool of the step before it in this call, or None, the
# step's own tool): None to go on, a pause (text starting PAUSE_PREFIX) to stop before the step, or
# a warning line the report lists. The anti-bot tripwire is one: a click may send the page to a
# site's verification page, and the next step must not open more pages into it.
Checkpoint = Callable[[Optional[str], str], Awaitable[Optional[str]]]


class WorkflowError(ValueError):
    """A step that cannot run as written. Never reaches the browser."""


def lookup(path: str, variables: dict) -> Any:
    """Resolve `rows[0].name` against the variables. Raises when any part is missing."""
    segments = list(_SEGMENT.finditer(path.strip()))
    if not segments:
        raise WorkflowError(f"empty reference ${{{path}}}")
    head = segments[0].group(1)
    if head is None or head not in variables:
        raise WorkflowError(
            f"unknown variable ${{{path}}}; defined: {sorted(variables) or 'none'}"
        )
    value = variables[head]
    for seg in segments[1:]:
        key, index = seg.group(1), seg.group(2)
        try:
            value = value[int(index)] if index is not None else value[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise WorkflowError(
                f"${{{path}}}: no {key if index is None else f'[{index}]'} in "
                f"{type(value).__name__} value ({type(exc).__name__})"
            ) from None
    return value


def interpolate(value: Any, variables: dict) -> Any:
    """Substitute references. A string that is exactly one reference keeps its type."""
    if isinstance(value, str):
        whole = _WHOLE_REF.match(value)
        if whole:
            return lookup(whole.group(1), variables)

        def _text(match: re.Match) -> str:
            found = lookup(match.group(1), variables)
            return json.dumps(found, default=str) if isinstance(found, (dict, list)) else str(found)

        return _ANY_REF.sub(_text, value)
    if isinstance(value, dict):
        return {k: interpolate(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, variables) for v in value]
    return value


def _exists(path: Any, variables: dict) -> bool:
    text = str(path).strip()
    whole = _WHOLE_REF.match(text)
    try:
        lookup(whole.group(1) if whole else text, variables)
        return True
    except WorkflowError:
        return False


def _number(value: Any, op: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise WorkflowError(f"{op}: {value!r} is not a number") from None


def condition(spec: Any, variables: dict) -> bool:
    """Evaluate a condition dict. An unknown operator is an error, never just false.

    {"value": "${x}", "equals": 3}    equals, not_equals, contains, not_contains,
                                       matches, gt, gte, lt, lte, truthy, falsy
    {"exists": "${x.y}"}               the reference resolves
    {"all": [...]}, {"any": [...]}, {"not": {...}}
    """
    if not isinstance(spec, dict):
        raise WorkflowError(f"condition must be a dict, got {type(spec).__name__}")
    if "all" in spec:
        return all(condition(c, variables) for c in spec["all"])
    if "any" in spec:
        return any(condition(c, variables) for c in spec["any"])
    if "not" in spec:
        return not condition(spec["not"], variables)
    if "exists" in spec:
        return _exists(spec["exists"], variables)
    if "value" not in spec:
        raise WorkflowError(f"condition needs 'value' or all/any/not/exists: {spec}")
    value = interpolate(spec["value"], variables)
    ops = [k for k in spec if k != "value"]
    if len(ops) != 1:
        raise WorkflowError(f"condition needs exactly one operator: {spec}")
    op = ops[0]
    other = interpolate(spec[op], variables)
    if op == "equals":
        return value == other
    if op == "not_equals":
        return value != other
    if op == "contains":
        return other in value if isinstance(value, (list, dict)) else str(other) in str(value)
    if op == "not_contains":
        return other not in value if isinstance(value, (list, dict)) else str(other) not in str(value)
    if op == "matches":
        return re.search(str(other), str(value)) is not None
    if op in ("gt", "gte", "lt", "lte"):
        left, right = _number(value, op), _number(other, op)
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
    if op == "truthy":
        return bool(value) is bool(other)
    if op == "falsy":
        return (not value) is bool(other)
    raise WorkflowError(f"unknown condition operator {op!r}")


def step_value(text: str) -> Any:
    """What a tool's text means as data: parsed JSON, a bool/None, or the string."""
    stripped = text.strip()
    if stripped in ("True", "False"):
        return stripped == "True"
    if stripped == "None":
        return None
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        return stripped
    # evaluate("JSON.stringify(rows)") hands back a JSON string holding JSON.
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_start(start_at: Any) -> list[int]:
    """start_at is a top-level index, or a path like "3.2.1" from a failure report."""
    if isinstance(start_at, bool):
        raise WorkflowError("start_at must be an index or a path, not a bool")
    if isinstance(start_at, int):
        if start_at < 0:
            raise WorkflowError(f"start_at must not be negative, got {start_at}")
        return [start_at]
    if isinstance(start_at, (list, tuple)):
        parts = list(start_at)
    else:
        parts = str(start_at).split(".")
    try:
        path = [int(p) for p in parts]
    except (TypeError, ValueError):
        raise WorkflowError(f"start_at path must be dotted integers, got {start_at!r}") from None
    if not path or any(p < 0 for p in path):
        raise WorkflowError(f"start_at path is not valid: {start_at!r}")
    return path


@dataclass
class _Run:
    dispatch: Dispatch
    allowed: Callable[[str], bool]
    stop_on_error: bool
    variables: dict
    entries: list[dict] = field(default_factory=list)
    omitted: int = 0
    executed: int = 0
    failure: Optional[dict] = None
    failed_paths: list[str] = field(default_factory=list)
    # For the shorter reports: every step counted, also past the cap; the steps that stored
    # a value (by index); and the last step, even when it fell past the cap.
    tally: dict[str, dict[str, int]] = field(default_factory=dict)
    saved: set[str] = field(default_factory=set)
    last: Optional[dict] = None
    # Failed or ignored entries past the cap (the first MAX_LATE_FAILURES), and how many more.
    late_failures: list[dict] = field(default_factory=list)
    late_failures_omitted: int = 0
    # The anti-bot tripwire: the checkpoint, the tool of the last step run, the pause that stopped
    # the run (never an ordinary failure, whatever stop_on_error says), and warnings (warn mode).
    checkpoint: Optional[Checkpoint] = None
    previous_tool: Optional[str] = None
    pause: Optional[dict] = None
    warnings: list[dict] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return self.failure is not None or self.pause is not None


def _dotted(path: list[int]) -> str:
    return ".".join(str(p) for p in path)


def _record(run: _Run, entry: dict) -> None:
    if not entry.get("ok") and not entry.get("ignored") and not entry.get("paused"):
        # Kept apart from the capped list, or a failure past the cap would vanish.
        run.failed_paths.append(str(entry.get("index")))
    if "tool" in entry:  # a step, not the record of a branch taken
        run.last = entry
        name = entry["tool"] if isinstance(entry["tool"], str) else "(no tool)"
        kind = ("ok" if entry.get("ok") else "ignored" if entry.get("ignored")
                else "paused" if entry.get("paused") else "failed")
        if kind != "paused" or entry.get("ran"):  # a step stopped before it ran is no step run
            run.tally.setdefault(name, {"ok": 0, "failed": 0, "ignored": 0, "paused": 0})[kind] += 1
    if len(run.entries) < MAX_REPORTED_ENTRIES:
        run.entries.append(entry)
        return
    run.omitted += 1
    if not entry.get("ok"):  # failed or ignored: a shorter report must still show it
        if len(run.late_failures) < MAX_LATE_FAILURES:
            run.late_failures.append(entry)
        else:
            run.late_failures_omitted += 1


def _fail(run: _Run, path: list[int], step: Any, error: str) -> None:
    """Record a failure. Stops the run only under stop_on_error."""
    _note_failure(run, error)
    _record(run, {"index": path[0] if len(path) == 1 else _dotted(path),
                  "tool": step.get("tool") if isinstance(step, dict) else None,
                  "ok": False, "error": error})
    if run.stop_on_error and run.failure is None:
        run.failure = {"path": path, "step": step, "error": error}


def _note_failure(run: _Run, message: str) -> None:
    """${last} describes the step that just ran, or a branch on ${last.ok} after a failed
    step reads the success before it."""
    run.variables["last"] = {"ok": False, "value": None, "text": message}


def _preview(text: str) -> str:
    """A step's result as the report shows it. A short one is whole. A longer one ends at the last
    line break or ", " in the final PREVIEW_BREAK_WINDOW characters before RESULT_PREVIEW (else at
    the limit) and says how much it left out: cut at 500 with no mark, mid-key ("redirect_c"), it
    read as the whole result."""
    if len(text) <= RESULT_PREVIEW:
        return text
    head = text[:RESULT_PREVIEW]
    start = RESULT_PREVIEW - PREVIEW_BREAK_WINDOW
    cut = max(head.rfind("\n", start), head.rfind(", ", start))
    if cut < 0:
        cut = RESULT_PREVIEW
    if 0xD800 <= ord(text[cut - 1]) <= 0xDBFF and 0xDC00 <= ord(text[cut]) <= 0xDFFF:
        cut -= 1  # never between the two halves of a surrogate pair: a lone half cannot be encoded
    return text[:cut] + CUT_MARK.format(left=len(text) - cut)


def _stop_at_pause(run: _Run, path: list[int], step: dict, text: str, ran: bool) -> None:
    """Stop the run at a verification page: before this step (ran=False, it runs on resume) or at
    this navigation step, which reached it (ran=True, run again on resume: it opens the page that
    stopped). Named as the pause, never counted as a failure, and no copy of the step's result: the
    report's antibot part says it once."""
    found = pause_details(text)
    found.pop("asked_for", None)  # the step itself says what it asked for
    first = text.splitlines()[0][len("Error: "):] if text.startswith("Error: ") else text
    # One line: where (the query and the rest are in the report's antibot part, once).
    one = (f"{PAUSED} at {found['page'].split('?', 1)[0].split(' [', 1)[0]}"
           if "page" in found else first)
    run.variables["last"] = {"ok": False, "value": None, "text": one}
    entry = {"index": path[0] if len(path) == 1 else _dotted(path), "tool": step.get("tool"),
             "paused": "antibot", "ran": ran}
    if step.get("label"):
        entry["label"] = step["label"]
    _record(run, entry)
    run.pause = {"path": path, "first": first, "details": found}


async def _run_tool(run: _Run, step: dict, path: list[int]) -> None:
    tool = step["tool"]
    if not run.allowed(tool):
        _fail(run, path, step, f"unknown or non-allowlisted tool: {tool}")
        return
    if run.checkpoint is not None and isinstance(tool, str):
        said = await run.checkpoint(run.previous_tool, tool)
        if said and said.startswith("Error:"):
            _fail(run, path, step, said)
            return
        if said and said.startswith(PAUSE_PREFIX):
            _stop_at_pause(run, path, step, said, ran=False)
            return
        if said and len(run.warnings) < MAX_LATE_FAILURES:
            run.warnings.append({"before": path[0] if len(path) == 1 else _dotted(path),
                                 "warning": said})
    run.executed += 1
    if run.executed > MAX_EXECUTED_STEPS:
        raise WorkflowError(f"workflow exceeded {MAX_EXECUTED_STEPS} executed steps")
    try:
        args = interpolate(step.get("args") or {}, run.variables)
        if not isinstance(args, dict):
            raise WorkflowError(f"args must be a dict, got {type(args).__name__}")
    except WorkflowError as exc:
        _fail(run, path, step, str(exc))
        return
    try:
        text = str(await run.dispatch(tool, args))
    except Exception as exc:
        run.previous_tool = tool
        _fail(run, path, step, str(exc) or type(exc).__name__)
        return
    run.previous_tool = tool
    if text.startswith(PAUSE_PREFIX):  # a navigation reached a verification page and said so
        _stop_at_pause(run, path, step, text, ran=True)
        return
    failed = text.startswith("Error:")
    value = step_value(text)
    run.variables["last"] = {"ok": not failed, "value": value, "text": text}
    entry = {"index": path[0] if len(path) == 1 else _dotted(path), "tool": tool,
             "label": step.get("label", ""), "ok": not failed, "result": _preview(text)}
    if step.get("save_as"):
        run.variables[str(step["save_as"])] = value
        run.saved.add(str(entry["index"]))
    if failed and step.get("continue_on_error"):
        entry["ignored"] = True
        _record(run, entry)
        return
    if failed:
        _record(run, entry)
        if run.stop_on_error and run.failure is None:
            run.failure = {"path": path, "step": step, "error": text}
        return
    _record(run, entry)


async def _run_block(run: _Run, steps: Any, prefix: list[int], resume: list[int]) -> None:
    if not isinstance(steps, list):
        raise WorkflowError(f"steps at {_dotted(prefix) or 'top level'} must be a list")
    start = resume[0] if resume else 0
    if resume and start >= len(steps):
        raise WorkflowError(
            f"start_at points at step {_dotted(prefix + [start])}, but there are only "
            f"{len(steps)} steps at that level"
        )
    for i in range(start, len(steps)):
        if run.stopped:
            return
        step = steps[i]
        path = prefix + [i]
        rest = resume[1:] if resume and i == start else []
        try:
            if rest and not (isinstance(step, dict) and ("foreach" in step or "if" in step)):
                raise WorkflowError(
                    f"start_at goes inside step {_dotted(path)}, which has no inner steps"
                )
            if not isinstance(step, dict):
                _fail(run, path, step, "step must be a dict")
            elif "foreach" in step:
                await _run_foreach(run, step, path, rest)
            elif "if" in step:
                branches = run.variables.setdefault(BRANCHES_VAR, {})
                taken = branches.get(_dotted(path)) if rest else None
                # Resuming inside a branch re-enters the one that ran. Its own failure may
                # have changed what the condition reads, and re-evaluating would then run
                # the other branch at the same index and report success.
                branch = taken or ("then" if condition(step["if"], run.variables) else "else")
                branches[_dotted(path)] = branch
                _record(run, {"index": _dotted(path), "if": branch, "ok": True,
                              "label": step.get("label", "")})
                await _run_block(run, step.get(branch) or [], path, rest)
            elif "tool" in step:
                await _run_tool(run, step, path)
            else:
                _fail(run, path, step, "step must have 'tool', 'if' or 'foreach'")
        except (WorkflowError, re.error, TypeError, ValueError, AttributeError, KeyError) as exc:
            # A bad regex or a malformed step, found partway through. Earlier steps have
            # already acted on the page, so their record must not be lost to a raise.
            _fail(run, path, step, f"{type(exc).__name__}: {exc}"
                  if not isinstance(exc, WorkflowError) else str(exc))


async def _run_foreach(run: _Run, step: dict, path: list[int], resume: list[int]) -> None:
    items = interpolate(step["foreach"], run.variables)
    if isinstance(items, str):
        parsed = step_value(items)
        items = parsed
    if not isinstance(items, list):
        raise WorkflowError(
            f"foreach needs a list, got {type(items).__name__}: {str(items)[:120]}"
        )
    if len(items) > MAX_FOREACH_ITEMS:
        raise WorkflowError(f"foreach over {len(items)} items exceeds {MAX_FOREACH_ITEMS}")
    name = str(step.get("as") or "item")
    start = resume[0] if resume else 0
    if resume and start >= len(items):
        raise WorkflowError(
            f"start_at points at item {start} of step {_dotted(path)}, which has "
            f"{len(items)} items"
        )
    for n in range(start, len(items)):
        if run.stopped:
            return
        run.variables[name] = items[n]
        run.variables[f"{name}_index"] = n
        await _run_block(
            run, step.get("steps") or [], path + [n],
            resume[1:] if resume and n == start else [],
        )


async def run_workflow(
    steps: list,
    dispatch: Dispatch,
    allowed: Callable[[str], bool],
    start_at: Any = 0,
    stop_on_error: bool = True,
    variables: Optional[dict] = None,
    output: str = "full",
    checkpoint: Optional[Checkpoint] = None,
) -> dict:
    """Run steps and return {completed, total, vars, ...}, plus failed_at on failure, or status
    antibot_paused, paused_at and resume_with when the checkpoint or a step stopped it at a site's
    verification page. output="summary" or "failures" returns the shorter form _shorten makes of
    that report."""
    if output not in OUTPUT_MODES:
        raise WorkflowError(f"output must be one of {', '.join(OUTPUT_MODES)}, got {output!r}; nothing ran")
    # Taken before any step runs: a step may change in place an object it was handed (a whole
    # ${ref} passes the object itself), and the run's vars then compare equal to the caller's.
    given = _snapshot(variables) if output != "full" else {}
    # Input objects reach steps by reference, so a step may change the caller's own object. Left
    # so on purpose: over MCP each call parses its own JSON, and no caller keeps that object.
    run = _Run(dispatch=dispatch, allowed=allowed, stop_on_error=stop_on_error,
               variables=dict(variables or {}), checkpoint=checkpoint)
    start = parse_start(start_at)
    await _run_block(run, steps, [], start)
    result: dict[str, Any] = {"completed": run.entries, "total": len(steps)}
    if run.omitted:
        result["completed_omitted"] = run.omitted
    if run.failed_paths:
        result["failed_count"] = len(run.failed_paths)
        result["failed_paths"] = run.failed_paths[:100]
    # `last` is included: resuming into a step or condition that reads it needs it.
    public_vars = dict(run.variables)
    if not run.stopped:
        # Only a resume needs it, and a resume only follows a failure or a pause.
        public_vars.pop(BRANCHES_VAR, None)
    if public_vars:
        result["vars"] = public_vars
    if run.failure is not None:
        path = run.failure["path"]
        where = path[0] if len(path) == 1 else _dotted(path)
        result.update({
            "failed_at": path[0],
            "failed_path": _dotted(path),
            "failure_context": {"step": run.failure["step"], "error": run.failure["error"]},
            "resume_with": (
                f"workflow_run(steps=..., start_at={where!r}"
                + (", vars=<the vars above>)" if public_vars else ")")
            ),
            "hint": "fix the underlying issue, then re-run with that start_at to retry "
                    "this step. Variables are not kept between calls, so pass vars back.",
        })
    if run.pause is not None:
        path = run.pause["path"]
        where = path[0] if len(path) == 1 else _dotted(path)
        result = {"status": PAUSED, **result}  # first, where a reader looks
        result.update({
            "paused_at": path[0],
            "paused_path": _dotted(path),
            "antibot": run.pause["details"] or {"said": run.pause["first"]},
            "resume_with": (
                f"workflow_run(steps=..., start_at={where!r}"
                + (", vars=<the vars above>)" if public_vars else ")")
            ),
        })
    if run.warnings:
        result["antibot_warnings"] = run.warnings
    return result if output == "full" else _shorten(result, run, output, given)


def _json_text(value: Any) -> Optional[str]:
    """value written as canonical JSON text, or None when that proves nothing: it could not be
    written (an exception: a cycle, mixed key types) or it needed repr, being no JSON data (an
    in-process caller's object, whose repr may stay the same while it changes)."""
    needed_repr = False

    def _repr(obj: Any) -> str:
        nonlocal needed_repr
        needed_repr = True
        return repr(obj)

    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          default=_repr)
    except Exception:
        return None
    return None if needed_repr else text


def _snapshot(variables: Optional[dict]) -> dict[str, Optional[str]]:
    """Each input var's JSON text before the run, to tell later which ones the run changed. The
    input is in memory once already; this adds its text once (one string per key), not a second
    object tree. Over MCP vars are JSON, so the comparison is exact; None never compares equal."""
    return {key: _json_text(value) for key, value in dict(variables or {}).items()}


def _unchanged(key: str, value: Any, given: dict[str, Optional[str]]) -> bool:
    """Proved unchanged: an input var whose JSON text after the run is the one it had before."""
    before = given.get(key)
    return before is not None and _json_text(value) == before


_NOTES = {
    "summary": "a summary: counts, every failed or ignored step, and the result of each step with "
               "save_as, each reader and the last step; vars only as this run set or changed them, "
               "last without the text its value holds unless a step failed or was ignored (after "
               "a stop at a failure: all vars, whole). "
               "output=\"full\" lists the steps (the first 200).",
    "failures": "failures only: counts and every failed or ignored step, no vars unless a resume "
                "needs them. output=\"summary\" adds results, output=\"full\" lists the steps (the first 200).",
}
# What a resume needs, kept whole whatever the output: resume_with says to pass these vars back.
_RESUME_KEYS = ("vars", "failed_at", "failed_path", "failure_context", "resume_with", "hint")
_PAUSE_KEYS = ("vars", "paused_at", "paused_path", "antibot", "resume_with")


def _shorten(full: dict, run: _Run, output: str, given: dict[str, Optional[str]]) -> dict:
    """The summary or failures form of a full report. The one place that decides what a shorter
    report keeps; it names its mode and counts what it leaves out."""
    reported = full["completed"]

    def kept(entry: dict) -> bool:
        if not entry.get("ok"):  # failed or ignored
            return True
        if output == "failures" or "tool" not in entry:
            return False
        return (entry is run.last or entry["tool"] in READERS
                or str(entry.get("index")) in run.saved)

    shown = [entry for entry in reported if kept(entry)]
    counts = _counts(run.tally)
    if run.pause is not None:
        # A pause is said short (the field's failed run was 8 KB): the stopped step is in shown,
        # what fired in antibot, so neither the note, the counts per tool, their sum nor a 0.
        counts = {k: v for k, v in counts.items() if k not in ("by_tool", "steps") and v}
        counts["paused"] = 1
        out: dict[str, Any] = {"status": PAUSED, "output": output, "total": full["total"],
                               "counts": counts, "shown": shown}
    else:
        out = {"output": output, "note": _NOTES[output], "total": full["total"],
               "counts": counts, "shown": shown}
    if len(shown) < len(reported) and run.pause is None:
        out["not_shown"] = len(reported) - len(shown)
    # Past the cap: the failed or ignored steps (the first MAX_LATE_FAILURES), and the last step
    # (in failures only when it failed). They ran after every reported entry, so order is kept.
    late, cut = list(run.late_failures), run.late_failures_omitted
    last = run.last
    if (last is not None and not any(e is last for e in reported) and not any(e is last for e in late)
            and (output == "summary" or not last.get("ok"))):
        late.append(last)
        if not last.get("ok"):
            cut -= 1  # a failed last step the bound had cut is shown after all
    shown.extend(late)
    if cut:
        out["failed_or_ignored_not_shown"] = cut
    for key in ("completed_omitted", "failed_count", "failed_paths", "antibot_warnings"):
        if key in full:
            out[key] = full[key]
    if "failed_at" in full:
        out.update((key, full[key]) for key in _RESUME_KEYS if key in full)
    elif run.pause is not None:
        out.update((key, full[key]) for key in _PAUSE_KEYS if key in full)
    elif output == "summary" and full.get("vars"):
        changed = {k: v for k, v in full["vars"].items() if not _unchanged(k, v, given)}
        # Any failed or ignored step, with or without a stop (failed_at): last stays whole.
        troubled = any(c["failed"] or c["ignored"] for c in run.tally.values())
        if isinstance(changed.get("last"), dict) and not troubled:
            changed["last"] = _last_once(changed["last"])
        if changed:
            out["vars"] = changed
        unchanged = [k for k in full["vars"] if k not in changed]
        if unchanged:
            out["vars_unchanged"] = unchanged  # named, so a reader knows they were not lost
    return out


def _last_once(last: dict) -> dict:
    """${last} for a successful summary: without its text when the value came from that text
    (parsed, or the text itself), which said the step's result twice. A value that did not come
    from it (a step refused before it ran: value None, text the reason) keeps both. A new dict:
    the run's own ${last} is not touched."""
    text = last.get("text")
    if set(last) == {"ok", "value", "text"} and isinstance(text, str) and step_value(text) == last["value"]:
        return {"ok": last["ok"], "value": last["value"]}
    return last


def _counts(tally: dict[str, dict[str, int]]) -> dict:
    totals = {kind: sum(c.get(kind, 0) for c in tally.values()) for kind in ("ok", "failed", "ignored")}
    paused = sum(c.get("paused", 0) for c in tally.values())
    by_tool = {name: {"ok": c["ok"], "failed": c["failed"],
                      **({"ignored": c["ignored"]} if c["ignored"] else {}),
                      **({"paused": c["paused"]} if c.get("paused") else {})}
               for name, c in tally.items()}
    return {"steps": sum(totals.values()) + paused, **totals, "by_tool": by_tool}


def _seconds(value: Any) -> Optional[float]:
    """A finite number of seconds, or None. A numeric string counts: "${x}s"-style text is a string."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


async def sleep_step(seconds: Any = None, min: Any = None, max: Any = None) -> str:  # noqa: A002
    """The sleep step of workflow_run and batch_actions: {"seconds": 3}, or {"min": 6, "max": 11}
    for a uniform random pause in that range. Refused, with the reason, rather than guessed at."""
    if seconds is not None and (min is not None or max is not None):
        return "Error: sleep takes seconds, or min and max, not both; nothing was done"
    if seconds is None and (min is None or max is None):
        given = "only min" if min is not None else "only max" if max is not None else "neither"
        return f"Error: sleep needs seconds, or both min and max; got {given}; nothing was done"
    wanted = {"seconds": seconds} if seconds is not None else {"min": min, "max": max}
    numbers = {}
    for name, value in wanted.items():
        number = _seconds(value)
        if number is None:
            return f"Error: sleep {name} must be a number of seconds, got {value!r}; nothing was done"
        if number < 0:
            return f"Error: sleep {name} must not be negative, got {value!r}; nothing was done"
        if number > SLEEP_CAP_SECONDS:
            return (f"Error: sleep {name}={value!r} is over the {SLEEP_CAP_SECONDS} s cap for one step "
                    "(the whole workflow is one call, and a client gives up on a call after its own "
                    "time limit); nothing was done")
        numbers[name] = number
    low, high = ((numbers["seconds"],) * 2 if "seconds" in numbers
                 else (numbers["min"], numbers["max"]))
    if low > high:
        return f"Error: sleep min {min!r} is greater than max {max!r}; nothing was done"
    pause = random.uniform(low, high) if high > low else low
    await _pause(pause)
    return json.dumps({"slept_seconds": round(pause, 3), "min": low, "max": high})
