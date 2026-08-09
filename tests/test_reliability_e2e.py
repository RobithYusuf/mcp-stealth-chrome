"""End-to-end proof that the honest-reporting fixes actually hold.

Drives a real headless Chrome against tests/fixtures/reliability_probe.html,
which reproduces the situations that used to produce wrong-but-confident
results: a button under a transparent overlay, an async expression, a page whose
<script> dwarfs its visible text, console output plus an uncaught exception, and
a hover-only control.

Every check names the defect it guards against, so a failure here means a real
regression in reporting honesty — not just a changed string.

Skipped by default because it needs a real browser. Run it with:

    MCP_E2E=1 uv run pytest tests/test_reliability_e2e.py -v
"""
import asyncio
import json
import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_E2E"),
    reason="needs a real Chrome; set MCP_E2E=1 to run",
)

PROBE = pathlib.Path(__file__).parent / "fixtures" / "reliability_probe.html"


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


@pytest.fixture(scope="module")
def loop():
    """One event loop for the whole module.

    The CDP connection is bound to the loop that opened it, so a per-test
    asyncio.run() would hand each test a fresh loop and hang on the first call.
    """
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    lp.run_until_complete(tool("browser_launch")(headless=True))
    lp.run_until_complete(tool("navigate")(PROBE.as_uri()))
    lp.run_until_complete(asyncio.sleep(0.6))
    try:
        yield lp
    finally:
        try:
            lp.run_until_complete(tool("browser_close")())
        except Exception:
            pass
        # nodriver tears the websocket down in background tasks; closing the loop
        # out from under them produces a wall of "Event loop is closed" noise that
        # buries the actual result. Let them finish, then cancel the stragglers.
        try:
            lp.run_until_complete(asyncio.sleep(0.4))
            pending = [t for t in asyncio.all_tasks(lp) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                lp.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        lp.close()


def test_evaluate_awaits_promises(loop):
    """Used to return null for every async expression, silently losing the value."""
    out = loop.run_until_complete(tool("evaluate")(
        "(async () => { await new Promise(r => setTimeout(r, 300)); return 'AWAITED_OK'; })()"))
    assert "AWAITED_OK" in out
    assert "2" in loop.run_until_complete(tool("evaluate")("1+1"))


def test_get_text_excludes_script_payload(loop):
    """textContent used to drag in inlined JS/JSON no human ever sees."""
    body = loop.run_until_complete(tool("get_text")(selector="body"))
    assert "HUMAN_VISIBLE_SENTINEL" in body
    assert "SCRIPT_PAYLOAD_SENTINEL" not in body
    raw = loop.run_until_complete(tool("get_text")(selector="body", raw=True))
    assert "SCRIPT_PAYLOAD_SENTINEL" in raw


def test_click_text_refuses_obscured_and_names_blocker(loop):
    """The core defect: reporting a click that never reached the element."""
    out = loop.run_until_complete(tool("click_text")("Covered Button"))
    assert "NOT clicked" in out
    assert "obscured" in out.lower() and "overlay" in out.lower()
    # Prove the old "clicked" would have been a lie — the page never reacted.
    state = loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent"))
    assert "idle" in state


def test_click_text_succeeds_and_confirms_change(loop):
    out = loop.run_until_complete(tool("click_text")("Free Button"))
    assert "clicked" in out and ("page changed" in out or "navigated" in out)
    state = loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent"))
    assert "FREE CLICK" in state


def test_click_text_skips_hidden_decoy_and_reaches_the_real_control(loop):
    """Honest refusal is not enough — it must still get the job done.

    The fixture puts a hidden <span> with the same text first in document order,
    which is what fuzzy find returns. Refusing there would be truthful but
    useless; the tool must fall through to the visible button.
    """
    loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent='idle'"))
    out = loop.run_until_complete(tool("click_text")("Rescue Target"))
    assert "clicked" in out and "NOT clicked" not in out
    assert "unreachable" in out  # says plainly that it had to look past the decoy
    state = loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent"))
    assert "RESCUED" in state


def test_assert_clickable_diagnoses_without_clicking(loop):
    covered = json.loads(loop.run_until_complete(
        tool("assert_clickable")(selector="#realbtn")))
    assert covered["clickable"] is False and "overlay" in covered["reason"]
    free = json.loads(loop.run_until_complete(
        tool("assert_clickable")(selector="#freebtn")))
    assert free["clickable"] is True


def test_console_capture_reports_status_and_survives_navigation(loop):
    """An empty [] could mean 'no errors' or 'never listening'. Now it says which."""
    loop.run_until_complete(tool("console_start")())
    loop.run_until_complete(tool("navigate")(PROBE.as_uri()))
    loop.run_until_complete(asyncio.sleep(1.2))
    d = json.loads(loop.run_until_complete(tool("console_get")()))
    assert d["attached"] is True
    texts = " ".join(m["text"] for m in d["messages"])
    assert "probe-console-line" in texts
    assert any(m["type"] == "exception" for m in d["messages"])


def test_screenshot_region_selector(loop):
    out = loop.run_until_complete(tool("screenshot")(
        filename="t_region.png", region_selector="#freebtn"))
    assert "t_region.png" in out and "error" not in out.lower()
    missing = loop.run_until_complete(tool("screenshot")(
        filename="t_missing.png", region_selector="#nope"))
    assert "not found" in missing.lower()


def test_screenshot_base64_guard(loop):
    import mcp_stealth_chrome.server as srv
    prev = srv.B64_INLINE_LIMIT
    srv.B64_INLINE_LIMIT = 500
    try:
        out = loop.run_until_complete(tool("screenshot")(
            filename="t_b64.png", return_base64=True))
        assert "base64 omitted" in out and "max_dimension" in out
    finally:
        srv.B64_INLINE_LIMIT = prev


def test_wait_for_change_detects_and_times_out_honestly(loop):
    loop.run_until_complete(tool("evaluate")(
        "setTimeout(()=>{const p=document.createElement('p');p.id='late2';"
        "document.body.appendChild(p);},600)"))
    assert "appeared" in loop.run_until_complete(
        tool("wait_for_change")(selector="#late2", timeout=5))
    timed_out = loop.run_until_complete(
        tool("wait_for_change")(selector="#never-exists", timeout=1.2))
    assert "timeout" in timed_out.lower()


def test_mouse_click_xy_hovers_first(loop):
    box = json.loads(loop.run_until_complete(tool("evaluate")(
        "JSON.stringify((()=>{const r=document.getElementById('hoverzone')"
        ".getBoundingClientRect();return{x:Math.round(r.left+30),"
        "y:Math.round(r.top+30)};})())")))
    out = loop.run_until_complete(tool("mouse_click_xy")(x=box["x"], y=box["y"]))
    assert "after hover" in out


def test_humanize_click_by_coordinates(loop):
    loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent='idle'"))
    box = json.loads(loop.run_until_complete(tool("evaluate")(
        "JSON.stringify((()=>{const r=document.getElementById('freebtn')"
        ".getBoundingClientRect();return{x:Math.round(r.left+r.width/2),"
        "y:Math.round(r.top+r.height/2)};})())")))
    out = loop.run_until_complete(tool("humanize_click")(x=box["x"], y=box["y"]))
    assert "Bezier" in out
    state = loop.run_until_complete(tool("evaluate")(
        "document.getElementById('out').textContent"))
    assert "FREE CLICK" in state


def test_profile_affinity_survives_recover():
    """browser_recover freeing the default profile must not migrate the session."""
    import mcp_stealth_chrome.state as st
    orig = st.is_chrome_profile_locked
    try:
        st.is_chrome_profile_locked = lambda p: p == st.PROFILE_DIR   # default busy
        first = st.resolve_default_profile(True)
        st.set_profile_affinity(first)
        st.is_chrome_profile_locked = lambda p: False                 # default freed
        assert st.resolve_default_profile(True) == first
    finally:
        st.is_chrome_profile_locked = orig
        st.set_profile_affinity(None)
