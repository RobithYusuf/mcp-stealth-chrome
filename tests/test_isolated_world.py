"""A kept isolated world is checked before a script runs in it.

A context id is unique only within one renderer process. After a cross-site navigation the
tab's new process numbers its contexts again, and the id kept for the tab named an about:blank
ad iframe there: navigate reported final_url about:blank for a page that had loaded. The fake
below reproduces that renumbering; tests/test_protocol_e2e.py proves it on real Chrome.
"""
import asyncio
import re
from types import SimpleNamespace

import pytest

from mcp_stealth_chrome import refs


class _Renderer:
    """Contexts of one tab, numbered from 1 by each new process, as Chrome does."""

    def __init__(self):
        self.contexts: dict[int, dict] = {}
        self.next_id = 1
        self.created = 0

    def new_process(self, *hrefs):
        """A cross-site navigation: the page's main world and its iframes, numbered from 1."""
        self.contexts = {}
        self.next_id = 1
        for href in hrefs:
            self.contexts[self.next_id] = {"href": href, "mark": None}
            self.next_id += 1

    def handle(self, method, params):
        if method == "Page.createIsolatedWorld":
            self.created += 1
            ctx = self.next_id
            self.next_id += 1
            self.contexts[ctx] = {"href": self.contexts[1]["href"], "mark": None}
            return {"executionContextId": ctx}
        if method == "Runtime.evaluate":
            ctx = self.contexts.get(params.get("contextId"))
            if ctx is None:
                raise RuntimeError("Cannot find context with specified id")
            expression = params["expression"]
            set_mark = re.match(r'globalThis\.__mcpWorldMark = "(\w+)"; 1$', expression)
            if set_mark:
                ctx["mark"] = set_mark.group(1)
                return {"result": {"type": "number", "value": 1}}
            check = re.match(r'if \(globalThis\.__mcpWorldMark !== "(\w+)"\)', expression)
            if check and ctx["mark"] != check.group(1):
                return {"result": {"type": "object"},
                        "exceptionDetails": {"exceptionId": 1, "text": "Uncaught", "lineNumber": 0,
                                             "columnNumber": 0,
                                             "exception": {"type": "object",
                                                           "description": f"Error: {refs.STALE_WORLD}"}}}
            return {"result": {"type": "string", "value": ctx["href"]}}
        raise AssertionError(method)


class _Tab:
    def __init__(self, renderer):
        self.renderer = renderer
        self.target = SimpleNamespace(target_id="T1")

    async def send(self, cmd):
        request = next(cmd)
        response = self.renderer.handle(request["method"], request.get("params", {}))
        try:
            cmd.send(response)
        except StopIteration as done:
            return done.value
        raise AssertionError("command did not finish")

    async def evaluate(self, *_a, **_k):
        raise AssertionError("fell back to the page's own world")


@pytest.fixture(autouse=True)
def _no_kept_worlds():
    refs._WORLDS.clear()
    yield
    refs._WORLDS.clear()


def test_a_kept_world_whose_id_now_names_an_iframe_is_made_again():
    renderer = _Renderer()
    tab = _Tab(renderer)
    renderer.new_process("https://www.cnbcindonesia.com/tech/indeks/12")
    first = asyncio.run(refs.evaluate_isolated(tab, "location.href"))
    assert first == "https://www.cnbcindonesia.com/tech/indeks/12"
    kept = refs._WORLDS[("T1", "T1")][1].ctx

    # Kompas in a new process: id `kept` is now one of its about:blank ad slots.
    renderer.new_process(*(["https://tekno.kompas.com/read/x"] + ["about:blank"] * 4))
    assert renderer.contexts[kept]["href"] == "about:blank"
    second = asyncio.run(refs.evaluate_isolated(tab, "location.href"))

    assert second == "https://tekno.kompas.com/read/x"
    assert renderer.created == 2


def test_a_world_still_valid_is_reused_without_a_new_one():
    renderer = _Renderer()
    tab = _Tab(renderer)
    renderer.new_process("https://example.test/")
    for _ in range(3):
        assert asyncio.run(refs.evaluate_isolated(tab, "location.href")) == "https://example.test/"
    assert renderer.created == 1


def test_the_guard_keeps_the_value_of_a_statement_list():
    world = refs.World(1, "abc")
    script = refs.guard("const a = 1;\na + 1", world)
    assert script.splitlines()[0].startswith('if (globalThis.__mcpWorldMark !== "abc") throw')
    assert script.endswith("\nconst a = 1;\na + 1")
    assert "((el) => el.id\n).apply(this, args)" in refs.guard_function("(el) => el.id;", world)
