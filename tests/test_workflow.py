"""The workflow_run engine: references, branching, loops and resume, without a browser."""
import asyncio
import json

import pytest

from mcp_stealth_chrome.workflow import (
    WorkflowError,
    condition,
    interpolate,
    lookup,
    parse_start,
    run_workflow,
    step_value,
)


def run(steps, responses=None, **kwargs):
    calls = []

    async def dispatch(tool, args):
        calls.append((tool, args))
        answer = (responses or {}).get(tool, "ok")
        if isinstance(answer, Exception):
            raise answer
        return answer(args) if callable(answer) else answer

    kwargs.setdefault("allowed", lambda name: name != "forbidden")
    return asyncio.run(run_workflow(steps, dispatch, **kwargs)), calls


def test_references_keep_type_when_whole_and_stringify_when_embedded():
    data = {"rows": [{"name": "a", "n": 2}], "o": {"k": 1}}
    assert lookup("rows[0].name", data) == "a"
    assert interpolate("${rows[0].n}", data) == 2
    assert interpolate("n=${rows[0].n}", data) == "n=2"
    assert interpolate({"x": ["${o}"]}, data) == {"x": [{"k": 1}]}
    assert interpolate("o=${o}", data) == 'o={"k": 1}'


@pytest.mark.parametrize("path", ["missing", "rows[5]", "rows[0].nope", "rows.x"])
def test_bad_reference_is_an_error_naming_it(path):
    with pytest.raises(WorkflowError):
        lookup(path, {"rows": [{"name": "a"}]})


@pytest.mark.parametrize("spec,expected", [
    ({"value": 3, "equals": 3}, True),
    ({"value": 3, "not_equals": 3}, False),
    ({"value": "hello", "contains": "ell"}, True),
    ({"value": [1, 2], "contains": 2}, True),
    ({"value": "hello", "not_contains": "z"}, True),
    ({"value": "abc123", "matches": r"\d+$"}, True),
    ({"value": 5, "gt": 4}, True),
    ({"value": 5, "lte": 4}, False),
    ({"value": "${v}", "truthy": True}, True),
    ({"value": "", "falsy": True}, True),
    ({"exists": "${v}"}, True),
    ({"exists": "${nope}"}, False),
    ({"all": [{"value": 1, "equals": 1}, {"value": 2, "equals": 2}]}, True),
    ({"any": [{"value": 1, "equals": 2}, {"value": 2, "equals": 2}]}, True),
    ({"not": {"value": 1, "equals": 1}}, False),
])
def test_conditions(spec, expected):
    assert condition(spec, {"v": 1}) is expected


@pytest.mark.parametrize("spec", [
    {"value": 1, "bogus": 2},
    {"value": 1},
    {"value": 1, "equals": 1, "gt": 0},
    {"nothing": 1},
    "not a dict",
    {"value": "x", "gt": 1},
])
def test_bad_condition_raises_instead_of_reading_as_false(spec):
    with pytest.raises(WorkflowError):
        condition(spec, {})


def test_conditions_cannot_execute_code():
    """A condition string is only ever data."""
    assert condition({"value": "__import__('os').system('x')", "equals": "no"}, {}) is False


@pytest.mark.parametrize("text,expected", [
    ('[{"a": 1}]', [{"a": 1}]),
    ('"[1, 2]"', [1, 2]),
    ("True", True),
    ("None", None),
    ("3", 3),
    ("plain text", "plain text"),
])
def test_step_value(text, expected):
    assert step_value(text) == expected


def test_straight_steps_report_like_before():
    result, calls = run([{"tool": "a"}, {"tool": "b", "args": {"x": 1}, "label": "L"}])
    assert result["total"] == 2 and "failed_at" not in result
    assert [e["index"] for e in result["completed"]] == [0, 1]
    assert result["completed"][1]["label"] == "L"
    assert calls == [("a", {}), ("b", {"x": 1})]


def test_save_as_feeds_later_steps():
    result, calls = run(
        [{"tool": "read", "save_as": "data"},
         {"tool": "use", "args": {"name": "${data.user}", "ok": "${last.ok}"}}],
        {"read": '{"user": "ann"}'},
    )
    assert calls[1] == ("use", {"name": "ann", "ok": True})
    assert result["vars"]["data"] == {"user": "ann"}


def test_foreach_with_if_fills_a_form_per_row():
    rows = [{"name": "a", "active": True}, {"name": "b", "active": False}]
    result, calls = run(
        [{"tool": "read", "save_as": "rows"},
         {"foreach": "${rows}", "as": "row", "steps": [
             {"tool": "fill", "args": {"value": "${row.name}", "pos": "${row_index}"}},
             {"if": {"value": "${row.active}", "truthy": True},
              "then": [{"tool": "check"}], "else": [{"tool": "uncheck"}]},
             {"tool": "save"},
         ]}],
        {"read": json.dumps(rows)},
    )
    assert "failed_at" not in result
    assert [c[0] for c in calls] == ["read", "fill", "check", "save", "fill", "uncheck", "save"]
    assert calls[1][1] == {"value": "a", "pos": 0}
    assert calls[4][1] == {"value": "b", "pos": 1}


def test_failure_names_the_nested_path_and_resumes_there():
    steps = [{"tool": "read", "save_as": "rows"},
             {"foreach": "${rows}", "as": "row", "steps": [
                 {"tool": "first"},
                 {"tool": "second", "args": {"v": "${row}"}}]}]

    def second(args):
        return "Error: boom" if args["v"] == 2 else "ok"

    result, _ = run(steps, {"read": "[1, 2, 3]", "second": second})
    assert result["failed_at"] == 1
    assert result["failed_path"] == "1.1.1"
    assert "1.1.1" in result["resume_with"] and "vars" in result["resume_with"]
    assert result["vars"]["rows"] == [1, 2, 3]

    resumed, calls = run(steps, {"second": "ok"},
                         start_at=result["failed_path"], variables=result["vars"])
    assert "failed_at" not in resumed
    # Retries the failed step of item 1, then finishes item 2. Nothing earlier re-runs.
    assert calls == [("second", {"v": 2}), ("first", {}), ("second", {"v": 3})]


def test_top_level_start_at_stays_an_integer_index():
    result, calls = run([{"tool": "a"}, {"tool": "b"}, {"tool": "c"}], start_at=1)
    assert [c[0] for c in calls] == ["b", "c"]
    failed, _ = run([{"tool": "a"}, {"tool": "b"}], {"b": "Error: no"})
    assert failed["failed_at"] == 1 and failed["failed_path"] == "1"
    assert "start_at=1" in failed["resume_with"]


def test_stop_on_error_false_runs_everything_and_reports_failures():
    result, calls = run([{"tool": "a"}, {"tool": "b"}], {"a": "Error: no"}, stop_on_error=False)
    assert len(calls) == 2 and "failed_at" not in result
    assert [e["ok"] for e in result["completed"]] == [False, True]


def test_continue_on_error_is_per_step():
    result, calls = run([{"tool": "a", "continue_on_error": True},
                         {"if": {"value": "${last.ok}", "falsy": True}, "then": [{"tool": "cleanup"}]}],
                        {"a": "Error: no"})
    assert "failed_at" not in result and calls[-1][0] == "cleanup"
    assert result["completed"][0]["ignored"] is True


@pytest.mark.parametrize("steps,fragment", [
    ([{"tool": "forbidden"}], "non-allowlisted"),
    (["nope"], "must be a dict"),
    ([{"neither": 1}], "'tool', 'if' or 'foreach'"),
    ([{"foreach": 5, "steps": []}], "needs a list"),
    ([{"tool": "a", "args": {"x": "${undefined}"}}], "unknown variable"),
    ([{"if": {"value": 1, "bogus": 1}, "then": []}], "unknown condition"),
])
def test_invalid_steps_fail_at_their_index(steps, fragment):
    result, calls = run(steps)
    assert result["failed_at"] == 0 and fragment in result["failure_context"]["error"]
    assert calls == []


def test_exception_from_a_tool_is_a_step_failure():
    result, _ = run([{"tool": "a"}], {"a": RuntimeError("cdp gone")})
    assert "cdp gone" in result["failure_context"]["error"]


def test_runaway_loops_are_bounded():
    result, _ = run([{"foreach": list(range(5000)), "steps": [{"tool": "a"}]}])
    assert "exceeds" in result["failure_context"]["error"]


@pytest.mark.parametrize("bad", [True, -1, "a.b", "", [], "1.-2"])
def test_invalid_start_at_is_refused(bad):
    with pytest.raises(WorkflowError):
        parse_start(bad)


def test_batch_actions_still_runs_the_legacy_shapes(monkeypatch):
    from mcp_stealth_chrome import server

    server._register_workflow_tools()
    seen = []

    def fake(name):
        async def _fn(**kw):
            seen.append((name, kw))
            return f"{name} done"
        return _fn

    for name in ("click", "fill", "type_text", "press_key", "wait_for", "navigate", "select_option"):
        monkeypatch.setitem(server._WORKFLOW_TOOLS, name, fake(name))
    out = asyncio.run(server.mcp._tool_manager._tools["batch_actions"].fn([
        {"type": "click", "ref": "e1"},
        {"type": "fill", "selector": "#a", "value": "x"},
        {"type": "type", "text": "hi"},
        {"type": "press"},
        {"type": "wait", "selector": "#b"},
        {"type": "navigate", "url": "http://x"},
        {"type": "select_option", "selector": "#c", "value": "v", "stop_on_error": True},
        {"type": "nonsense"},
    ]))
    assert out.startswith("8 actions, 1 failed")
    assert seen[0] == ("click", {"ref": "e1", "selector": None, "humanize": False})
    assert seen[3] == ("press_key", {"key": "Enter"})
    assert seen[6] == ("select_option", {"selector": "#c", "value": "v"})
    assert "unknown action type" in out


@pytest.mark.parametrize("bad_step", [
    {"if": {"value": "x", "matches": "("}, "then": []},
    {"if": {"value": {"a": 1}, "contains": {}}, "then": []},
    {"tool": ["not", "a", "name"]},
    {"if": {"all": 3}, "then": []},
])
def test_a_bad_step_after_real_work_keeps_the_record(bad_step):
    """Raising here lost the report of steps that had already acted on the page."""
    result, calls = run([{"tool": "acted"}, bad_step])
    assert calls[0][0] == "acted" and result["failed_at"] == 1
    assert result["completed"][0]["ok"] is True


@pytest.mark.parametrize("start_at", ["0.9", "0.5", "1.0"])
def test_a_start_at_that_does_not_exist_is_refused(start_at):
    steps = [{"foreach": [1], "steps": [{"tool": "a"}]}, {"tool": "b"}]
    result, calls = run(steps, start_at=start_at)
    assert "start_at" in result["failure_context"]["error"] and calls == []


def test_start_at_past_the_top_level_is_an_error_not_an_empty_success():
    with pytest.raises(WorkflowError):
        run([{"tool": "a"}], start_at=99)


def test_failures_past_the_report_cap_stay_visible():
    steps = [{"tool": "ok"}] * 200 + [{"tool": "bad"}]
    result, _ = run(steps, {"bad": "Error: no"}, stop_on_error=False)
    assert result["completed_omitted"] == 1
    assert result["failed_count"] == 1 and result["failed_paths"] == ["200"]


def test_resume_can_read_last():
    failed, _ = run([{"tool": "a"}, {"tool": "b"}], {"b": "Error: no"})
    assert failed["vars"]["last"]["ok"] is False
    resumed, calls = run(
        [{"tool": "a"}, {"if": {"value": "${last.ok}", "falsy": True}, "then": [{"tool": "c"}]}],
        start_at=1, variables=failed["vars"],
    )
    assert "failed_at" not in resumed and calls == [("c", {})]


def test_resume_inside_a_branch_stays_in_that_branch():
    """Its own failure changed the condition; re-evaluating ran the other branch."""
    steps = [
        {"tool": "prepare"},
        {"if": {"value": "${last.ok}", "truthy": True},
         "then": [{"tool": "retry_me"}],
         "else": [{"tool": "wrong_branch"}]},
    ]
    failed, _ = run(steps, {"retry_me": "Error: flaky"})
    assert failed["failed_path"] == "1.0"
    resumed, calls = run(steps, {"retry_me": "ok"},
                         start_at=failed["failed_path"], variables=failed["vars"])
    assert "failed_at" not in resumed
    assert calls == [("retry_me", {})]


def test_branch_record_only_appears_when_a_resume_may_need_it():
    steps = [{"if": {"value": True, "truthy": True}, "then": [{"tool": "a"}]}]
    assert "__branches__" not in run(steps)[0].get("vars", {})
    failed, _ = run(steps, {"a": "Error: no"})
    assert failed["vars"]["__branches__"] == {"0": "then"}


def test_a_fresh_run_still_evaluates_its_conditions():
    steps = [{"if": {"value": "${x}", "truthy": True}, "then": [{"tool": "a"}], "else": [{"tool": "b"}]}]
    assert run(steps, variables={"x": True, "__branches__": {"0": "else"}})[1] == [("a", {})]
    assert run(steps, variables={"x": False})[1] == [("b", {})]


def test_last_describes_a_step_that_raised():
    """A raised failure left ${last.ok} true from the step before, so a guard passed."""
    steps = [{"tool": "first"}, {"tool": "boom"},
             {"if": {"value": "${last.ok}", "truthy": True},
              "then": [{"tool": "success_only"}], "else": [{"tool": "cleanup"}]}]
    _, calls = run(steps, {"boom": RuntimeError("no")}, stop_on_error=False)
    assert [c[0] for c in calls] == ["first", "boom", "cleanup"]


@pytest.mark.parametrize("bad_step", [
    {"tool": "forbidden"},          # the allowlist rejection
    "not a dict",
    {"neither": 1},
    {"tool": "a", "args": {"x": "${missing}"}},
    {"if": {"value": "${missing}", "truthy": True}, "then": []},
    {"foreach": 3, "steps": []},
])
def test_last_describes_a_step_that_was_rejected(bad_step):
    """A rejected step left ${last.ok} true from the success before it, so with
    stop_on_error=False the success branch ran."""
    steps = [{"tool": "first"}, bad_step,
             {"if": {"value": "${last.ok}", "truthy": True},
              "then": [{"tool": "success_only"}], "else": [{"tool": "cleanup"}]}]
    _, calls = run(steps, stop_on_error=False)
    names = [c[0] for c in calls]
    assert "success_only" not in names and names[-1] == "cleanup"
