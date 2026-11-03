"""Declarative step engine behind workflow_run: variables, if, foreach, resume.

No expression language and no eval. A condition is a small dict of named operators
and a reference is a dotted path, so a workflow can only do what its allowlisted
tools can do. Kept free of browser and MCP imports so it is testable by itself.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

BRANCHES_VAR = "__branches__"
MAX_EXECUTED_STEPS = 2000
MAX_FOREACH_ITEMS = 1000
MAX_REPORTED_ENTRIES = 200
RESULT_PREVIEW = 500

_WHOLE_REF = re.compile(r"^\$\{([^{}]+)\}$")
_ANY_REF = re.compile(r"\$\{([^{}]+)\}")
_SEGMENT = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")

Dispatch = Callable[[str, dict], Awaitable[Any]]


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


def _dotted(path: list[int]) -> str:
    return ".".join(str(p) for p in path)


def _record(run: _Run, entry: dict) -> None:
    if not entry.get("ok") and not entry.get("ignored"):
        # Kept apart from the capped list, or a failure past the cap would vanish.
        run.failed_paths.append(str(entry.get("index")))
    if len(run.entries) < MAX_REPORTED_ENTRIES:
        run.entries.append(entry)
    else:
        run.omitted += 1


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


async def _run_tool(run: _Run, step: dict, path: list[int]) -> None:
    tool = step["tool"]
    if not run.allowed(tool):
        _fail(run, path, step, f"unknown or non-allowlisted tool: {tool}")
        return
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
        _fail(run, path, step, str(exc) or type(exc).__name__)
        return
    failed = text.startswith("Error:")
    value = step_value(text)
    run.variables["last"] = {"ok": not failed, "value": value, "text": text}
    if step.get("save_as"):
        run.variables[str(step["save_as"])] = value
    entry = {"index": path[0] if len(path) == 1 else _dotted(path), "tool": tool,
             "label": step.get("label", ""), "ok": not failed, "result": text[:RESULT_PREVIEW]}
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
        if run.failure is not None:
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
        if run.failure is not None:
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
) -> dict:
    """Run steps and return {completed, total, vars, ...}, plus failed_at on failure."""
    run = _Run(dispatch=dispatch, allowed=allowed, stop_on_error=stop_on_error,
               variables=dict(variables or {}))
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
    if run.failure is None:
        # Only a resume needs it, and a resume only follows a failure.
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
    return result
