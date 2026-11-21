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


# ── output: summary (default of the tool), failures, full ──────────────────

import inspect  # noqa: E402
import re  # noqa: E402
import pathlib  # noqa: E402

from mcp_stealth_chrome import workflow  # noqa: E402
from mcp_stealth_chrome.workflow import (  # noqa: E402
    READERS,
    SLEEP_CAP_SECONDS,
    SLEEP_PARAMETERS,
    sleep_step,
)

_GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "workflow_full.json"
_LONG_TEXT = "Price: 10. " + "Long product description without separators. " * 20
_CUT = re.compile(r" \.\.\. \[\+\d+ chars; the whole result is in vars when the step has save_as\]")
_GOLDEN_STEPS = [
    {"tool": "navigate", "args": {"url": "https://shop.test/${start}"}, "label": "open"},
    {"tool": "get_text", "args": {"selector": "#price"}, "save_as": "price"},
    {"tool": "scroll", "args": {"direction": "down"}, "continue_on_error": True},
    {"foreach": "${rows}", "as": "row", "steps": [
        {"tool": "fill", "args": {"selector": "#q", "value": "${row.name}"}},
        {"if": {"value": "${row.go}", "truthy": True}, "then": [
            {"tool": "click", "args": {"selector": "#go"}}]},
    ]},
]
_GOLDEN_VARS = {"start": "a", "rows": [{"name": "x", "go": False}, {"name": "y", "go": True}]}


def _fake(answer):
    async def _fn(**kw):
        return answer(kw) if callable(answer) else answer
    return _fn


def _install_fakes(monkeypatch, flaky_click):
    from mcp_stealth_chrome import server
    server._register_workflow_tools()
    for name, answer in {
        "navigate": lambda kw: json.dumps({"navigated": kw["url"], "status": 200}),
        "get_text": _LONG_TEXT,  # past RESULT_PREVIEW, no break near it: cut at the limit
        "fill": lambda kw: f"filled {kw['selector']} with {kw['value']}",
        "click": (lambda kw: "Error: boom") if flaky_click else "clicked",
        "scroll": "Error: nothing to scroll",
    }.items():
        monkeypatch.setitem(server._WORKFLOW_TOOLS, name, _fake(answer))


def test_full_output_is_the_old_report_but_for_the_cut_marker(monkeypatch):
    """output="full" through the tool, for a run that succeeds, the same run failing and the resume
    of that failure. Each text is pinned, and once the cut marker of a long result is removed it is
    byte for byte what 0.26.0 (5a6521b) printed: that marker is the only change to "full"."""
    from mcp_stealth_chrome.tools import action_kit
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    _install_fakes(monkeypatch, flaky_click=False)
    succeeded = asyncio.run(action_kit.workflow_run(_GOLDEN_STEPS, vars=_GOLDEN_VARS, output="full"))
    _install_fakes(monkeypatch, flaky_click=True)
    failed = asyncio.run(action_kit.workflow_run(_GOLDEN_STEPS, vars=_GOLDEN_VARS, output="full"))
    report = json.loads(failed[len("Error: "):])
    _install_fakes(monkeypatch, flaky_click=False)
    resumed = asyncio.run(action_kit.workflow_run(
        _GOLDEN_STEPS, start_at=report["failed_path"], vars=report["vars"], output="full"))
    texts = {"ok": succeeded, "fail": failed, "resume": resumed}
    for name, text in texts.items():
        assert text == golden["now"][name], name
        assert _CUT.sub("", text) == golden["at_5a6521b"][name], name
    assert len(_CUT.findall(succeeded)) == 1 and len(_CUT.findall(failed)) == 1  # not vacuous


def test_the_tool_answers_with_a_summary_unless_asked_for_more(monkeypatch):
    from mcp_stealth_chrome.tools import action_kit
    _install_fakes(monkeypatch, flaky_click=False)
    doc = json.loads(asyncio.run(action_kit.workflow_run(_GOLDEN_STEPS, vars=_GOLDEN_VARS)))
    assert doc["output"] == "summary" and "completed" not in doc
    assert inspect.signature(action_kit.workflow_run).parameters["output"].default == "summary"


_MIXED = [
    {"tool": "navigate"},                              # 0: plain success: not shown
    {"tool": "get_text"},                              # 1: a reader, no save_as: shown
    {"tool": "click", "save_as": "clicked"},           # 2: save_as: shown
    {"tool": "scroll", "continue_on_error": True},     # 3: ignored failure: shown
    {"tool": "press_key"},                             # 4: plain success: not shown
    {"tool": "fill"},                                  # 5: the last step: shown
]


def test_summary_keeps_reads_saved_results_failures_and_the_last_step():
    responses = {"get_text": "Price: 10", "scroll": "Error: no wheel"}
    full, _ = run(_MIXED, responses)
    summary, calls = run(_MIXED, responses, output="summary")
    assert len(calls) == 6
    assert summary["output"] == "summary" and summary["note"].startswith("a summary")
    assert [e["index"] for e in summary["shown"]] == [1, 2, 3, 5]
    assert summary["not_shown"] == 2
    # A shown step is exactly its entry in the full report.
    assert summary["shown"] == [full["completed"][i] for i in (1, 2, 3, 5)]
    assert summary["counts"] == {
        "steps": 6, "ok": 5, "failed": 0, "ignored": 1,
        "by_tool": {"navigate": {"ok": 1, "failed": 0}, "get_text": {"ok": 1, "failed": 0},
                    "click": {"ok": 1, "failed": 0}, "scroll": {"ok": 0, "failed": 0, "ignored": 1},
                    "press_key": {"ok": 1, "failed": 0}, "fill": {"ok": 1, "failed": 0}},
    }


@pytest.mark.parametrize("reader", READERS)
def test_summary_keeps_every_reader_without_save_as(reader):
    """Agents run "navigate, then read" without save_as: dropping the read would lose it unsaid."""
    summary, _ = run([{"tool": "navigate"}, {"tool": reader}, {"tool": "click"}], output="summary")
    assert [e["tool"] for e in summary["shown"]] == [reader, "click"]


def test_summary_vars_leave_out_input_the_run_did_not_change():
    rows = [{"sku": i} for i in range(20)]
    summary, _ = run(
        [{"tool": "read", "save_as": "price"},
         {"foreach": "${pages}", "as": "page", "steps": [{"tool": "navigate", "args": {"url": "${page}"}}]},
         {"tool": "count", "save_as": "seen"}],
        {"read": "10", "count": "3"},
        variables={"rows": rows, "pages": ["a", "b"], "seen": 0}, output="summary",
    )
    assert summary["vars"]["seen"] == 3  # an input var the run changed is kept
    assert set(summary["vars"]) == {"price", "page", "page_index", "last", "seen"}
    assert summary["vars_unchanged"] == ["rows", "pages"]  # named, not repeated


@pytest.mark.parametrize("output", ["summary", "failures"])
def test_a_failure_keeps_everything_a_resume_needs(output):
    steps = [{"tool": "read", "save_as": "items"},
             {"foreach": "${items}", "as": "item", "steps": [
                 {"tool": "first"}, {"tool": "second", "args": {"v": "${item}"}}]}]
    given = {"rows": [{"sku": i} for i in range(20)]}

    def second(args):
        return "Error: boom" if args["v"] == 2 else "ok"

    responses = {"read": "[1, 2, 3]", "second": second}
    full, _ = run(steps, responses, variables=given)
    short, _ = run(steps, responses, variables=given, output=output)
    for key in ("vars", "failed_at", "failed_path", "failure_context", "resume_with", "hint",
                "failed_count", "failed_paths"):
        assert short[key] == full[key], key
    assert short["vars"]["rows"] == given["rows"] and "vars_unchanged" not in short
    resumed, calls = run(steps, {"second": "ok"}, start_at=short["failed_path"],
                         variables=short["vars"], output=output)
    assert "failed_at" not in resumed
    assert calls == [("second", {"v": 2}), ("first", {}), ("second", {"v": 3})]


def test_failures_output_keeps_counts_and_failed_or_ignored_steps_only():
    steps = [{"tool": "get_text", "save_as": "t"}, {"tool": "a", "continue_on_error": True},
             {"tool": "b"}, {"tool": "c"}]
    result, _ = run(steps, {"a": "Error: no", "b": "Error: also no"}, output="failures",
                    stop_on_error=False)
    assert result["output"] == "failures"
    assert [(e["index"], e.get("ignored", False)) for e in result["shown"]] == [(1, True), (2, False)]
    assert result["not_shown"] == 2 and result["failed_paths"] == ["2"]
    assert result["counts"]["ok"] == 2 and result["counts"]["failed"] == 1
    assert result["counts"]["ignored"] == 1
    assert "vars" not in result and "failed_at" not in result


def test_counts_and_the_last_step_outlive_the_report_cap():
    steps = [{"tool": "step"}] * 250 + [{"tool": "bad", "continue_on_error": True}, {"tool": "end"}]
    full, _ = run(steps, {"bad": "Error: no", "end": "done"})
    summary, _ = run(steps, {"bad": "Error: no", "end": "done"}, output="summary")
    assert full["completed_omitted"] == summary["completed_omitted"] == 52
    assert summary["counts"]["steps"] == 252 and summary["counts"]["by_tool"]["step"]["ok"] == 250
    assert summary["counts"]["ignored"] == 1
    # The last step ran past the cap, so even the full report leaves it out; a summary shows it.
    assert summary["shown"][-1]["index"] == 251 and summary["shown"][-1]["result"] == "done"
    assert summary["not_shown"] == 200


def test_an_unknown_output_mode_is_refused_before_anything_runs(monkeypatch):
    calls = []

    async def dispatch(tool, args):
        calls.append(tool)
        return "ok"

    with pytest.raises(WorkflowError, match="output must be one of summary, failures, full"):
        asyncio.run(run_workflow([{"tool": "a"}], dispatch, lambda name: True, output="brief"))
    assert calls == []
    from mcp_stealth_chrome.tools import action_kit
    _install_fakes(monkeypatch, flaky_click=False)
    out = asyncio.run(action_kit.workflow_run([{"tool": "navigate", "args": {"url": "x"}}], output="all"))
    assert out.startswith("Error: output must be one of") and "nothing ran" in out


# ── the sleep step ──────────────────────────────────────────────────────────


@pytest.fixture
def pauses(monkeypatch):
    seen = []

    async def pause(seconds):
        seen.append(seconds)

    monkeypatch.setattr(workflow, "_pause", pause)
    return seen


def test_sleep_pauses_a_uniform_random_time_inside_the_range(pauses):
    async def many():
        return [await sleep_step(min=6, max=11) for _ in range(200)]

    texts = asyncio.run(many())
    assert all(6 <= p <= 11 for p in pauses) and len(set(pauses)) > 100
    assert min(pauses) < 7 and max(pauses) > 10  # spread over the range, not one end of it
    assert json.loads(texts[0]) == {"slept_seconds": round(pauses[0], 3), "min": 6, "max": 11}


def test_sleep_seconds_pauses_exactly_that_long(pauses):
    assert json.loads(asyncio.run(sleep_step(seconds=3))) == {"slept_seconds": 3, "min": 3, "max": 3}
    assert json.loads(asyncio.run(sleep_step(seconds="1.5")))["slept_seconds"] == 1.5
    assert pauses == [3, 1.5]


def test_the_sleep_cap_is_300_seconds_and_300_is_allowed(pauses):
    assert SLEEP_CAP_SECONDS == 300
    assert not asyncio.run(sleep_step(seconds=300)).startswith("Error")
    assert not asyncio.run(sleep_step(min=0, max=300)).startswith("Error")
    assert pauses[0] == 300 and 0 <= pauses[1] <= 300


@pytest.mark.parametrize("args,reason", [
    ({"seconds": -1}, "seconds must not be negative"),
    ({"min": -2, "max": 5}, "min must not be negative"),
    ({"min": 9, "max": 6}, "min 9 is greater than max 6"),
    ({"seconds": 3, "min": 1, "max": 2}, "not both"),
    ({"seconds": 3, "max": 2}, "not both"),
    ({}, "got neither"),
    ({"min": 1}, "got only min"),
    ({"max": 1}, "got only max"),
    ({"seconds": 301}, "over the 300 s cap"),
    ({"min": 1, "max": 300.5}, "max=300.5 is over the 300 s cap"),
    ({"seconds": "soon"}, "must be a number of seconds"),
    ({"seconds": True}, "must be a number of seconds"),
    ({"seconds": float("nan")}, "must be a number of seconds"),
    ({"min": 0, "max": float("inf")}, "must be a number of seconds"),
])
def test_sleep_refuses_with_the_reason_and_does_not_pause(pauses, args, reason):
    text = asyncio.run(sleep_step(**args))
    assert text.startswith("Error: sleep") and reason in text and "nothing was done" in text, text
    assert pauses == []


def test_a_sleep_step_runs_in_a_workflow_and_a_refused_one_fails_there(pauses):
    from mcp_stealth_chrome.tools import action_kit
    doc = json.loads(asyncio.run(action_kit.workflow_run([{"tool": "sleep", "args": {"min": 1, "max": 2}}])))
    assert doc["counts"]["by_tool"] == {"sleep": {"ok": 1, "failed": 0}}
    slept = json.loads(doc["shown"][0]["result"])  # the last step, shown with its result
    assert 1 <= slept["slept_seconds"] <= 2 and (slept["min"], slept["max"]) == (1, 2)
    out = asyncio.run(action_kit.workflow_run([{"tool": "sleep", "args": {"min": 5, "max": 1}},
                                               {"tool": "sleep", "args": {"seconds": 1}}]))
    assert out.startswith("Error:")
    doc = json.loads(out[len("Error: "):])
    assert doc["failed_at"] == 0 and "greater than max" in doc["failure_context"]["error"]
    assert len(pauses) == 1  # the refused step and the one after it never paused


def test_a_sleep_argument_it_does_not_take_is_refused_by_name(pauses):
    """sleep is no MCP tool, so the check against an advertised schema found none and let any
    name through; the step then died on a TypeError instead of the refusal every tool gives."""
    from mcp_stealth_chrome.tools import action_kit, batch
    out = asyncio.run(action_kit.workflow_run([{"tool": "sleep", "args": {"secs": 3}}]))
    assert "sleep does not take 'secs'; nothing was done" in out and "did you mean 'seconds'" in out, out
    out = asyncio.run(batch.batch_actions([{"type": "sleep", "secs": 3}, {"type": "sleep", "seconds": 1}]))
    assert out.startswith("2 actions, 1 failed") and "sleep does not take 'secs'" in out, out
    assert pauses == [1]


def test_the_sleep_schema_is_the_sleep_steps_own_arguments():
    assert set(SLEEP_PARAMETERS["properties"]) == set(inspect.signature(sleep_step).parameters)


def test_every_step_name_has_its_arguments_checked():
    from mcp_stealth_chrome import server
    server._register_workflow_tools()
    unchecked = [name for name in server._WORKFLOW_TOOLS
                 if server._inner_arguments_error(name, {"no_such_argument": 1}) is None]
    assert not unchecked
    # sleep is a step, not a tool: the tool count does not change.
    assert "sleep" in server._WORKFLOW_TOOLS and server.mcp._tool_manager.get_tool("sleep") is None


def test_every_reader_a_summary_keeps_is_a_step():
    from mcp_stealth_chrome import server
    server._register_workflow_tools()
    assert set(READERS) <= set(server._WORKFLOW_TOOLS)


def test_mouse_drift_is_a_workflow_and_batch_step(monkeypatch):
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import action_kit, batch
    server._register_workflow_tools()
    assert server._WORKFLOW_TOOLS["mouse_drift"] is server.mcp._tool_manager.get_tool("mouse_drift").fn
    seen = []

    async def drift(**kw):
        seen.append(kw)
        return "drifted"

    monkeypatch.setitem(server._WORKFLOW_TOOLS, "mouse_drift", drift)
    out = asyncio.run(action_kit.workflow_run(
        [{"tool": "mouse_drift", "args": {"duration_seconds": 1.5, "segments": 3}}]))
    assert not out.startswith("Error:"), out
    out = asyncio.run(batch.batch_actions([{"type": "mouse_drift"}]))
    assert out.startswith("1 actions OK"), out
    assert seen == [{"duration_seconds": 1.5, "segments": 3}, {}]


# ── ${last} in a successful summary: the step's result once ────────────────


@pytest.mark.parametrize("answer,value", [
    ('[{"title": "a", "price": 1}]', [{"title": "a", "price": 1}]),  # parsed from the text
    ("True", True),
    ("clicked the button", "clicked the button"),                   # the text itself: kept once
])
def test_a_successful_summary_says_the_last_result_once(answer, value):
    summary, _ = run([{"tool": "navigate"}, {"tool": "extract"}], {"extract": answer}, output="summary")
    assert summary["vars"]["last"] == {"ok": True, "value": value}
    full, _ = run([{"tool": "navigate"}, {"tool": "extract"}], {"extract": answer})
    assert full["vars"]["last"] == {"ok": True, "value": value, "text": answer}  # full unchanged


def test_last_keeps_its_text_when_the_value_did_not_come_from_it():
    """A step refused before it ran leaves value None and the reason in text: dropping the text
    would lose the only words saying why."""
    summary, _ = run([{"tool": "a"}, {"tool": "forbidden"}], output="summary", stop_on_error=False)
    assert "failed_at" not in summary
    last = summary["vars"]["last"]
    assert last["value"] is None and "non-allowlisted" in last["text"]


@pytest.mark.parametrize("output", ["summary", "failures", "full"])
def test_a_failed_run_keeps_last_whole(output):
    steps = [{"tool": "extract"}, {"tool": "click"}]
    responses = {"extract": '{"rows": 2}', "click": "Error: covered"}
    full, _ = run(steps, responses)
    result, _ = run(steps, responses, output=output)
    assert result["failed_at"] == 1
    assert result["vars"]["last"] == full["vars"]["last"] == {
        "ok": False, "value": "Error: covered", "text": "Error: covered"}


def test_the_runs_own_last_is_not_trimmed_in_place():
    """_last_once builds a new dict: a later step or a caller holding ${last} sees it whole."""
    last = {"ok": True, "value": [1], "text": "[1]"}
    assert workflow._last_once(last) == {"ok": True, "value": [1]}
    assert last == {"ok": True, "value": [1], "text": "[1]"}


# ── a cut result preview says it was cut ───────────────────────────────────

from mcp_stealth_chrome.workflow import RESULT_PREVIEW, _preview  # noqa: E402


@pytest.mark.parametrize("length", [0, 10, RESULT_PREVIEW])
def test_a_result_that_fits_is_shown_whole(length):
    text = "x" * length
    assert _preview(text) == text


def _cut_at(preview, text):
    """Where the preview stopped, read back from it, and the count its marker gives."""
    found = _CUT.search(preview)
    assert found and preview.endswith(found.group(0)), preview[-120:]
    kept = preview[: found.start()]
    assert text.startswith(kept)
    left = int(re.search(r"\+(\d+) chars", found.group(0)).group(1))
    assert left == len(text) - len(kept)  # the marker counts exactly what was left out
    return len(kept)


def test_a_long_result_without_a_break_is_cut_at_the_limit_and_says_so():
    text = "y" * (RESULT_PREVIEW + 1)
    assert _cut_at(_preview(text), text) == RESULT_PREVIEW


@pytest.mark.parametrize("sep", ["\n", ", "])
def test_a_long_result_ends_at_the_last_break_near_the_limit(sep):
    text = "a" * 450 + sep + "b" * 30 + sep + "c" * 300
    assert _cut_at(_preview(text), text) == 450 + len(sep) + 30


def test_the_later_of_a_line_break_and_a_comma_wins():
    text = "a" * 430 + "\n" + "b" * 20 + ", " + "c" * 300
    assert _cut_at(_preview(text), text) == 451


def test_a_break_far_before_the_limit_is_not_used():
    text = "a" * 100 + "\n" + "b" * 600
    assert _cut_at(_preview(text), text) == RESULT_PREVIEW


def test_a_cut_navigate_report_ends_on_a_whole_line_not_mid_key():
    """The Shopee session saw 'redirect_c': cut at 500 in the middle of a JSON key."""
    report = json.dumps({"requested_url": "https://shop.test/p/" + "q" * 300, "final_url": "https://shop.test/",
                         "redirect_chain": ["https://a.test/" + "r" * 40], "redirected": True,
                         "status": 200, "ready_state": "complete"}, indent=2)
    text = "Navigated: https://shop.test/ [status 200]\n" + report
    kept = _cut_at(_preview(text), text)
    assert kept < RESULT_PREVIEW and text[kept] in "\n,"


def test_a_cut_step_keeps_its_whole_result_in_vars_with_save_as():
    long = "z" * 900
    result, _ = run([{"tool": "read", "save_as": "page"}], {"read": long})
    entry = result["completed"][0]
    assert _cut_at(entry["result"], long) == RESULT_PREVIEW
    assert result["vars"]["page"] == long and result["vars"]["last"]["text"] == long


# ── review of the branch: failed or ignored steps past the report cap ──────


@pytest.mark.parametrize("output", ["summary", "failures"])
def test_a_failure_past_the_cap_is_shown_not_only_counted(output):
    """Counted in failed_count, listed in failed_paths, but its entry (and error) was in no list."""
    steps = [{"tool": "step"}] * 200 + [{"tool": "bad"}, {"tool": "skip", "continue_on_error": True},
                                        {"tool": "end"}]
    responses = {"bad": "Error: failed here", "skip": "Error: ignored here", "end": "done"}
    full, _ = run(steps, responses, stop_on_error=False)
    short, _ = run(steps, responses, stop_on_error=False, output=output)
    assert full["completed_omitted"] == 3 and len(full["completed"]) == 200  # full as it was
    late = [(e["index"], e["result"]) for e in short["shown"] if e["index"] in (200, 201)]
    assert late == [(200, "Error: failed here"), (201, "Error: ignored here")]
    assert short["counts"]["failed"] == 1 and short["counts"]["ignored"] == 1
    assert [e["index"] for e in short["shown"]][-1] == (202 if output == "summary" else 201)
    assert "failed_or_ignored_not_shown" not in short


def test_late_failures_are_bounded_and_the_bound_says_so():
    steps = [{"tool": "step"}] * 200 + [{"tool": "bad"}] * 130 + [{"tool": "end"}]
    short, _ = run(steps, {"bad": "Error: no", "end": "done"}, stop_on_error=False, output="failures")
    late = [e for e in short["shown"] if e["index"] >= 200]
    assert len(late) == 100 and late[0]["index"] == 200 and late[-1]["index"] == 299
    assert short["failed_or_ignored_not_shown"] == 30 and short["failed_count"] == 130


def test_a_failed_last_step_cut_by_the_bound_is_still_shown():
    steps = [{"tool": "step"}] * 200 + [{"tool": "bad"}] * 101
    short, _ = run(steps, {"bad": "Error: no"}, stop_on_error=False, output="summary")
    assert short["shown"][-1]["index"] == 300
    assert "failed_or_ignored_not_shown" not in short  # the one the bound cut is the one shown
    assert sum(1 for e in short["shown"] if not e["ok"]) == 101


# ── review of the branch: a cut never splits a surrogate pair ──────────────

# A string can hold an emoji as its two UTF-16 code units (text read unit by unit from a page).
_SPLIT_PAIR = chr(0xD83D) + chr(0xDE00)


def test_a_cut_steps_back_rather_than_split_a_surrogate_pair():
    # A real pair: written out with surrogatepass it is exactly the emoji's UTF-16.
    assert len(_SPLIT_PAIR) == 2
    assert _SPLIT_PAIR.encode("utf-16-le", "surrogatepass") == chr(0x1F600).encode("utf-16-le")
    text = "x" * (RESULT_PREVIEW - 1) + _SPLIT_PAIR + "z"
    preview = _preview(text)
    kept = _cut_at(preview, text)  # also checks +N counts from the corrected cut
    assert kept == RESULT_PREVIEW - 1 and chr(0xD83D) not in preview
    assert "+3 chars" in preview
    # The pair just before the limit is whole; only the one across it moves the cut.
    text = "x" * (RESULT_PREVIEW - 2) + _SPLIT_PAIR + "z" * 10
    assert _cut_at(_preview(text), text) == RESULT_PREVIEW


# ── review of the branch: what "set or changed" compares against ───────────


def test_an_input_var_changed_in_place_by_a_step_is_reported_as_changed():
    """A whole ${input} hands the step the object itself; comparing the run's vars with the
    caller's dict compared that object with itself and called it unchanged."""
    async def dispatch(tool, args):
        args["obj"]["items"].append(2)
        return "ok"

    given = {"input": {"items": [1]}, "other": [5]}
    summary = asyncio.run(run_workflow([{"tool": "grow", "args": {"obj": "${input}"}}], dispatch,
                                       lambda name: True, variables=given, output="summary"))
    assert summary["vars"]["input"] == {"items": [1, 2]}
    assert summary["vars_unchanged"] == ["other"]


def test_vars_that_cannot_be_copied_are_never_called_unchanged():
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise TypeError("no")

    summary, _ = run([{"tool": "a"}], variables={"odd": Uncopyable()}, output="summary")
    assert "odd" in summary["vars"] and "vars_unchanged" not in summary


def test_branch_records_stay_internal_in_a_successful_summary_and_whole_after_a_failure():
    steps = [{"if": {"value": "${go}", "truthy": True}, "then": [{"tool": "a"}], "else": [{"tool": "b"}]},
             {"tool": "c"}]
    # A fresh run, and a resume that was handed the record: neither summary names it.
    for given in ({"go": True}, {"go": True, "__branches__": {"0": "then"}}):
        summary, _ = run(steps, variables=given, output="summary")
        assert "__branches__" not in summary.get("vars", {})
        assert "__branches__" not in summary.get("vars_unchanged", [])
    full, _ = run(steps, {"c": "Error: no"}, variables={"go": True})
    for output in ("summary", "failures"):
        short, _ = run(steps, {"c": "Error: no"}, variables={"go": True}, output=output)
        assert short["vars"]["__branches__"] == full["vars"]["__branches__"] == {"0": "then"}
