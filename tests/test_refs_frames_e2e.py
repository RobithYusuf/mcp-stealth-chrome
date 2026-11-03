"""End-to-end proof for server-side refs, trusted clicks, frames and shadow DOM.

Serves a page on http://localhost:<port> whose iframes come from
http://127.0.0.1:<port>: a different origin, so Chrome puts them in their own
process and the parent's frame tree does not list them. That is the case that
breaks any approach built on a single CDP session.

Skipped by default because it needs a real browser:

    MCP_E2E=1 uv run pytest tests/test_refs_frames_e2e.py -v
"""
import asyncio
import functools
import http.server
import json
import os
import pathlib
import re
import threading

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_E2E"),
    reason="needs a real Chrome; set MCP_E2E=1 to run",
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/frames_main.html":
            host, port = self.server.server_address
            body = (FIXTURES / "frames_main.html").read_text().replace(
                "__CROSS__", f"http://127.0.0.1:{self.server.server_port}"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def server_url():
    handler = functools.partial(_Handler, directory=str(FIXTURES))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_port}/frames_main.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    # A throwaway profile: per-host zoom saved in a real one changes devicePixelRatio
    # for localhost, and these tests must not write into it either.
    lp.run_until_complete(tool("browser_launch")(headless=True, persistent=False))
    try:
        yield lp
    finally:
        try:
            lp.run_until_complete(tool("browser_close")())
        except Exception:
            pass
        try:
            lp.run_until_complete(asyncio.sleep(0.4))
            pending = [t for t in asyncio.all_tasks(lp) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                lp.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        lp.close()


@pytest.fixture()
def page(loop, server_url):
    loop.run_until_complete(tool("navigate")(server_url))
    loop.run_until_complete(asyncio.sleep(0.7))
    return loop


def run(lp, name, *args, **kwargs):
    return lp.run_until_complete(tool(name)(*args, **kwargs))


def snap(lp, **kwargs):
    kwargs.setdefault("max_elements", 0)
    kwargs.setdefault("diff_from_last", False)
    return run(lp, "browser_snapshot", **kwargs)


def ref_of(snapshot, needle):
    """Ref of the snapshot line containing needle."""
    for line in snapshot.splitlines():
        if needle in line:
            found = re.search(r"ref=(e\d+)", line)
            if found:
                return found.group(1)
    raise AssertionError(f"{needle!r} not in snapshot:\n{snapshot}")


def page_value(lp, expr):
    return run(lp, "evaluate", expr)


def frame_value(lp, name, expr):
    return run(lp, "frame_evaluate", f"name={name}", expr)


# ── Task 1: honest clicks ─────────────────────────────────────────────────


@pytest.mark.parametrize("by", ["ref", "selector"])
def test_click_is_trusted_and_leaves_no_trace_in_the_dom(page, by):
    """nodriver's Element.click is a JS click plus a flash() marker in the page."""
    if by == "ref":
        out = run(page, "click", ref=ref_of(snap(page), '"Free"'))
    else:
        out = run(page, "click", selector="#free")
    assert "page changed" in out
    assert "free:trusted=true" in page_value(page, "document.getElementById('out').textContent")
    mutations = json.loads(page_value(page, "JSON.stringify(window.__mutations)"))
    # The handler rewriting #out is the only mutation allowed.
    assert mutations == ["childList:DIV"], mutations


def test_click_refuses_overlay_and_names_it(page):
    out = run(page, "click", selector="#covered")
    assert out.startswith("Error:") and "NOT clicked" in out
    assert "cover" in out
    assert "idle" in page_value(page, "document.getElementById('out').textContent")


def test_js_fallback_only_on_request_and_says_so(page):
    out = run(page, "click", selector="#covered", js_fallback=True)
    assert "JS FALLBACK" in out and "isTrusted=false" in out and "obscured" in out
    assert "covered:trusted=false" in page_value(page, "document.getElementById('out').textContent")


def test_click_scrolls_offscreen_target_into_view(page):
    out = run(page, "click", selector="#far")
    assert "scrolled into view" in out
    assert "far:trusted=true" in page_value(page, "document.getElementById('out').textContent")


def test_click_with_no_effect_is_reported(page):
    out = run(page, "click", selector="#dead")
    assert "NO page change detected" in out


def test_checkbox_toggle_is_not_reported_as_no_change(page):
    """The fingerprint read the document only, and .checked never touches markup."""
    out = run(page, "click", selector="#box")
    assert "NO page change" not in out and "page changed" in out


# ── Task 2: refs without touching the DOM ─────────────────────────────────


def test_snapshot_writes_nothing_to_the_page(page):
    page_value(page, "window.__mutations.length = 0")
    snap(page)
    assert page_value(page, "JSON.stringify(window.__mutations)").strip() in ("[]", '"[]"')
    assert page_value(page, "!!document.querySelector('[data-mcp-ref]')") == "False"
    assert page_value(page, "document.documentElement.hasAttribute('data-mcp-doc')") == "False"
    assert "[]" in frame_value(page, "cross", "JSON.stringify(document.querySelectorAll('[data-mcp-ref]').length ? ['x'] : [])")


def test_ref_tools_write_no_marker_either(page):
    shot = snap(page)
    page_value(page, "window.__mutations.length = 0")
    assert run(page, "click", ref=ref_of(shot, 'aria="Plain field"'), verify=False).startswith("click")
    assert run(page, "type_text", "abc") == "typed"
    run(page, "smart_fill", {"Plain field": "xyz"})
    mutations = json.loads(page_value(page, "JSON.stringify(window.__mutations)"))
    assert not [m for m in mutations if "data-mcp" in m], mutations


def test_unknown_superseded_and_stale_refs_fail_clearly(page):
    assert "not in the latest snapshot" in run(page, "click", ref="e99999")
    shot = snap(page)
    ref = ref_of(shot, '"Removable"')
    page_value(page, "document.getElementById('removable').remove()")
    out = run(page, "click", ref=ref)
    assert out.startswith("Error:") and "stale" in out and "browser_snapshot" in out
    free = ref_of(shot, '"Free"')
    run(page, "reload")
    page.run_until_complete(asyncio.sleep(0.5))
    out = run(page, "click", ref=free)
    assert out.startswith("Error:") and "stale" in out


def test_ref_tools_keep_working(page):
    shot = snap(page)
    plain = ref_of(shot, 'aria="Plain field"')
    assert run(page, "get_attribute", "data-x", ref=plain) == "attr-value"
    assert run(page, "fill", ref=plain, value="hello") == "filled"
    assert "hello" in page_value(page, "document.getElementById('plain').value")
    assert "pasted" in run(page, "paste_text", "pasted-text", ref=plain)
    assert "pasted-text" in page_value(page, "document.getElementById('plain').value")
    assert "scrolled" in run(page, "scroll_to", ref=ref_of(shot, '"Far Below"'))
    assert "Free" in run(page, "get_text", ref=ref_of(shot, '"Free"'))
    assert "hovered" == run(page, "hover", ref=ref_of(shot, '"Free"'))
    assert "button" in run(page, "inspect_element", ref=ref_of(shot, '"Free"'))
    result = run(page, "check", ref=ref_of(shot, "type=checkbox"))
    assert result.startswith("checked") and "cdp_mouse" in result, result
    assert "already checked" == run(page, "check", ref=ref_of(shot, "type=checkbox"))


def test_fill_reports_a_value_the_page_rejected(page):
    out = run(page, "fill", selector="#capped", value="too long")
    assert out.startswith("Error:") and "rather than" in out


def test_styled_checkbox_says_it_needed_js(page):
    out = run(page, "check", selector="#styled")
    assert out.startswith("checked") and "cdp_mouse on its label" in out, out
    assert page_value(page, "document.getElementById('styled').checked") == "True"


# ── Task 3: frames and shadow DOM ─────────────────────────────────────────


def test_snapshot_reaches_frames_and_open_shadow_roots(page):
    shot = snap(page)
    assert "Frames: 5 measured" in shot, shot
    for needle in ("Inner same", "Inner cross", "Frame Shadow cross", "Shadow One", "Shadow Deep"):
        assert needle in shot
    assert "Closed Secret" not in shot
    line = next(l for l in shot.splitlines() if "Inner cross" in l)
    assert "frame=f" in line
    line = next(l for l in shot.splitlines() if "Shadow Deep" in l)
    assert "shadow" in line and "frame=" not in line


@pytest.mark.parametrize("frame_name", ["same", "cross"])
def test_click_and_fill_inside_a_frame(page, frame_name):
    shot = snap(page)
    out = run(page, "click", ref=ref_of(shot, f"Inner {frame_name}"))
    assert "page changed" in out, out
    assert "hit:true" in frame_value(page, frame_name, "document.getElementById('inner').textContent")

    lines = [l for l in shot.splitlines() if 'aria="Frame field"' in l]
    assert len(lines) == 4
    frame_label = next(l for l in shot.splitlines() if f"Inner {frame_name}" in l).split("frame=")[1].split()[0]
    field = next(l for l in lines if f"frame={frame_label}" in l)
    ref = re.search(r"ref=(e\d+)", field).group(1)
    assert run(page, "fill", ref=ref, value=f"typed-{frame_name}") == "filled"
    assert f"typed-{frame_name}" in frame_value(page, frame_name, "document.getElementById('field').value")


def test_click_lands_inside_oopif_past_its_border(page):
    """The fixture's cross-origin frame has a 7px border; the box must account for it."""
    shot = snap(page)
    assert "hit" not in frame_value(page, "cross", "document.getElementById('inner').textContent")
    run(page, "click", ref=ref_of(shot, "Frame Shadow cross"))
    assert "fs:true" in frame_value(
        page, "cross",
        "document.getElementById('child-host').shadowRoot.getElementById('frame-shadow').textContent",
    )


def test_shadow_elements_are_clickable_and_fillable(page):
    shot = snap(page)
    assert "page changed" in run(page, "click", ref=ref_of(shot, "Shadow Deep"))
    assert "shadow-deep:trusted=true" in page_value(page, "document.getElementById('out').textContent")
    assert run(page, "fill", ref=ref_of(shot, 'aria="Shadow field"'), value="in-shadow") == "filled"


def test_parent_overlay_over_a_frame_is_refused(page):
    """The frame's own hit-test cannot see an overlay drawn by the parent page."""
    shot = snap(page)
    out = run(page, "click", ref=ref_of(shot, "Inner crosscovered"))
    assert out.startswith("Error:") and "NOT clicked" in out and "obscured" in out, out
    assert "hit" not in frame_value(page, "crosscovered", "document.getElementById('inner').textContent")


def test_max_elements_covers_the_whole_page(page):
    full = snap(page)
    capped = snap(page, max_elements=5)
    assert len(re.findall(r"ref=e\d+", capped)) == 5
    assert "TRUNCATED" in capped
    assert len(re.findall(r"ref=e\d+", full)) > 5
    assert "Inner cross" not in capped


def test_frames_can_be_switched_off(page):
    shot = snap(page, include_frames=False)
    assert "frame=f" not in shot and "Inner cross" not in shot
    assert "main frame only" in shot
    assert "Shadow Deep" in shot


def test_list_frames_includes_out_of_process_frames(page):
    frames = json.loads(run(page, "list_frames"))
    assert len(frames) == 5
    assert sum(1 for f in frames if f["out_of_process"]) == 3


def test_element_screenshot_inside_an_oopif(page):
    shot = snap(page)
    out = run(page, "screenshot_element", ref=ref_of(shot, "Inner cross"))
    assert not str(out).startswith("Error"), out




# ── Task 5: workflow_run on a real page ───────────────────────────────────


def test_workflow_fills_a_form_per_row_from_page_data(page):
    """The plan's acceptance case: one call, rows read by an earlier step."""
    page_value(page, """(() => {
        window.rows = [{name: 'ann', tick: true}, {name: 'bob', tick: false}];
        window.saved = [];
        document.getElementById('free').onclick = () => window.saved.push({
          name: document.getElementById('plain').value,
          ticked: document.getElementById('box').checked,
        });
    })()""")
    out = run(page, "workflow_run", [
        {"tool": "evaluate", "args": {"expression": "JSON.stringify(window.rows)"}, "save_as": "rows"},
        {"foreach": "${rows}", "as": "row", "steps": [
            {"tool": "fill", "args": {"selector": "#plain", "value": "${row.name}"}},
            {"if": {"value": "${row.tick}", "truthy": True},
             "then": [{"tool": "check", "args": {"selector": "#box"}}],
             "else": [{"tool": "uncheck", "args": {"selector": "#box"}}]},
            {"tool": "click", "args": {"selector": "#free"}},
        ]},
    ])
    assert not out.startswith("Error:"), out
    saved = json.loads(page_value(page, "JSON.stringify(window.saved)"))
    assert saved == [{"name": "ann", "ticked": True}, {"name": "bob", "ticked": False}]


def test_workflow_failure_can_be_resumed_with_start_at(page):
    steps = [
        {"tool": "evaluate", "args": {"expression": "JSON.stringify(['#plain', '#nope'])"}, "save_as": "targets"},
        {"foreach": "${targets}", "as": "t", "steps": [
            {"tool": "fill", "args": {"selector": "${t}", "value": "x"}},
        ]},
    ]
    out = run(page, "workflow_run", steps)
    assert out.startswith("Error:")
    report = json.loads(out[len("Error: "):])
    assert report["failed_at"] == 1 and report["failed_path"] == "1.1.0"
    # Fix the page, then pick up exactly where it stopped.
    page_value(page, "(() => { const i = document.createElement('input'); i.id='nope'; document.body.prepend(i); })()")
    resumed = run(page, "workflow_run", steps, start_at=report["failed_path"], vars=report["vars"])
    assert not resumed.startswith("Error:"), resumed
    assert "x" in page_value(page, "document.getElementById('nope').value")


# ── Task 9: light mode ────────────────────────────────────────────────────


def test_light_mode_flags_cannot_alter_rendering_fingerprints(loop):
    from mcp_stealth_chrome.server import LIGHT_MODE_FLAGS

    assert "--blink-settings=imagesEnabled=false" in LIGHT_MODE_FLAGS
    for flag in LIGHT_MODE_FLAGS:
        # GPU/canvas flags change what fingerprinters read, and a second
        # --disable-features would override nodriver's own.
        assert not flag.startswith(("--disable-features", "--disable-gpu", "--use-gl", "--disable-webgl"))


# ── Found by review ───────────────────────────────────────────────────────


def test_a_click_is_exactly_one_press_and_release(page):
    """Tab.mouse_move sends a mouseReleased, which pages see as a stray mouseup."""
    page_value(page, "window.mouseEvents.length = 0")
    run(page, "click", selector="#free")
    assert json.loads(page_value(page, "JSON.stringify(window.mouseEvents)")) == [
        "mousedown", "mouseup", "click"]


def test_frame_click_on_a_scrolled_page(page):
    """getNodeForLocation takes document coordinates, unlike everything else."""
    shot = snap(page)
    page_value(page, "window.scrollTo(0, 120)")
    out = run(page, "click", ref=ref_of(shot, '"Inner cross"'))
    assert "page changed" in out, out
    assert "hit:true" in frame_value(page, "cross", "document.getElementById('inner').textContent")


def test_overlay_over_a_frame_is_still_refused_when_scrolled(page):
    shot = snap(page)
    page_value(page, "window.scrollTo(0, 120)")
    out = run(page, "click", ref=ref_of(shot, "Inner crosscovered"))
    assert out.startswith("Error:") and "obscured" in out, out


def test_frame_below_the_fold_is_scrolled_to_before_clicking(page):
    """Its own probe says in view: it only knows about the frame."""
    shot = snap(page)
    assert page_value(page, "scrollY") in ("0", "0.0")
    out = run(page, "click", ref=ref_of(shot, "Inner below"))
    assert "scrolled into view" in out, out
    assert "hit:true" in frame_value(page, "below", "document.getElementById('inner').textContent")


def test_assert_clickable_agrees_with_click_about_frames(page):
    shot = snap(page)
    verdict = json.loads(run(page, "assert_clickable", ref=ref_of(shot, "Inner crosscovered")))
    assert verdict["clickable"] is False and "obscured" in verdict["reason"]


@pytest.mark.parametrize("verb,start,end", [("check", "false", "true"), ("uncheck", "true", "false")])
def test_role_checkbox_reads_and_toggles_by_aria(page, verb, start, end):
    """.checked is undefined here, which used to read as "unchecked" forever."""
    page_value(page, f"document.getElementById('fake-box').setAttribute('aria-checked', '{start}')")
    out = run(page, verb, selector="#fake-box")
    assert not out.startswith("Error:") and "cdp_mouse" in out, out
    assert end in page_value(page, "document.getElementById('fake-box').getAttribute('aria-checked')")
    assert out != f"already {verb}ed"


def test_non_checkbox_is_refused_rather_than_reported_unchecked(page):
    assert "no checked state" in run(page, "check", selector="#free")


def test_frame_evaluate_reports_a_throw_as_an_error(page):
    out = run(page, "frame_evaluate", "name=same", "nonexistentFunction()")
    assert out.startswith("Error:") and "ReferenceError" in out
    out = run(page, "frame_evaluate", "name=cross", "nonexistentFunction()")
    assert out.startswith("Error:") and "ReferenceError" in out


def test_concurrent_snapshots_do_not_free_each_others_handles(page):
    async def both():
        return await asyncio.gather(*(tool("browser_snapshot")(max_elements=0, diff_from_last=False)
                                      for _ in range(3)))
    for shot in page.run_until_complete(both()):
        assert "Unresolved" not in shot and not shot.startswith("Error:")
