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


def test_screenshot_base64_shrinks_to_fit_then_refuses_honestly(loop):
    """Shrink the inline copy rather than burning the call on a refusal.

    Refusing was truthful but still cost a round trip, and a smaller image is
    nearly always acceptable. Only when even the smallest encoding cannot fit
    should it decline — and then it must say that downscaling was already tried,
    so the caller does not retry the same thing.
    """
    import mcp_stealth_chrome.server as srv

    prev = srv.B64_INLINE_LIMIT
    try:
        # Room for a small image: the inline copy must be shrunk and delivered.
        srv.B64_INLINE_LIMIT = 20000
        out = loop.run_until_complete(tool("screenshot")(
            filename="t_b64_fit.png", return_base64=True))
        assert "---base64---" in out, "a shrunken copy should still be inlined"
        assert "auto-shrunk" in out
        body = out.split("---base64---")[1].strip()
        assert 0 < len(body) <= srv.B64_INLINE_LIMIT

        # Physically impossible budget (~370 bytes): refuse, and say why.
        srv.B64_INLINE_LIMIT = 500
        out = loop.run_until_complete(tool("screenshot")(
            filename="t_b64_tiny.png", return_base64=True))
        assert "base64 omitted" in out
        assert "even after downscaling" in out
        assert "---base64---" not in out
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


def test_snapshot_delta_marks_only_new_elements(loop):
    """The second stable snapshot is quiet; one injected control is the only delta."""
    loop.run_until_complete(tool("navigate")(PROBE.as_uri()))
    first = loop.run_until_complete(tool("browser_snapshot")())
    assert "changed)" in first and "* ref=" in first
    second = loop.run_until_complete(tool("browser_snapshot")())
    assert ", 0 changed)" in second
    assert "* ref=" not in second
    loop.run_until_complete(tool("evaluate")(
        "(()=>{const b=document.createElement('button');b.id='delta-only';"
        "b.textContent='DELTA ONLY';document.body.appendChild(b);return true})()"))
    third = loop.run_until_complete(tool("browser_snapshot")())
    marked = [line for line in third.splitlines() if line.startswith("* ref=")]
    assert len(marked) == 1
    assert "DELTA ONLY" in marked[0]
    assert ", 1 changed)" in third


def test_instance_descriptions_survive_switch_roundtrip(loop):
    suffix = str(os.getpid())
    one, two = f"desc_one_{suffix}", f"desc_two_{suffix}"
    try:
        assert "Error:" not in loop.run_until_complete(tool("spawn_browser")(
            one, url=PROBE.as_uri(), headless=True, privacy=True,
            desc="shopping account for Jakarta",
        ))
        assert "Error:" not in loop.run_until_complete(tool("spawn_browser")(
            two, url=PROBE.as_uri(), headless=True, privacy=True,
            desc="support dashboard for Acme",
        ))
        listing = json.loads(loop.run_until_complete(tool("list_instances")()))
        descriptions = {row["instance_id"]: row["desc"] for row in listing["instances"]}
        assert descriptions[one] == "shopping account for Jakarta"
        assert descriptions[two] == "support dashboard for Acme"
        loop.run_until_complete(tool("instance_describe")(
            one, "uses the loyalty login", append=True))
        loop.run_until_complete(tool("switch_instance")(one))
        loop.run_until_complete(tool("switch_instance")(two))
        loop.run_until_complete(tool("switch_instance")(one))
        listing = json.loads(loop.run_until_complete(tool("list_instances")()))
        descriptions = {row["instance_id"]: row["desc"] for row in listing["instances"]}
        assert descriptions[one] == "shopping account for Jakarta; uses the loyalty login"
    finally:
        loop.run_until_complete(tool("close_instance")(one))
        loop.run_until_complete(tool("close_instance")(two))
        loop.run_until_complete(tool("switch_instance")("main"))


def test_human_handoff_focuses_headed_and_times_out_honestly(loop):
    iid = f"human_{os.getpid()}"
    try:
        launched = loop.run_until_complete(tool("spawn_browser")(
            iid, url=PROBE.as_uri(), headless=False, privacy=True,
            desc="temporary local human handoff",
        ))
        assert "Error:" not in launched
        loop.run_until_complete(tool("switch_instance")(iid))
        prompt = loop.run_until_complete(tool("request_human_help")(
            "Complete the local verification step", timeout=2))
        assert "foreground" in prompt.lower()
        assert "wait_for_human" in prompt
        timed_out = loop.run_until_complete(tool("wait_for_human")(
            done_selector="#x", timeout=2))
        assert "timeout" in timed_out.lower()
        assert "nothing changed" in timed_out.lower()
    finally:
        loop.run_until_complete(tool("close_instance")(iid))
        loop.run_until_complete(tool("switch_instance")("main"))


def test_identity_pin_is_reapplied_on_relaunch(loop):
    from mcp_stealth_chrome.server import _identity_path

    iid = f"pin_e2e_{os.getpid()}"
    fingerprint = {
        "user_agent": "Mozilla/5.0 IdentityPinE2E Chrome/147.0.0.0",
        "accept_language": "en-GB,en;q=0.9",
        "platform": "Win32",
        "timezone": "Asia/Tokyo",
    }

    def reported():
        raw = loop.run_until_complete(tool("evaluate")(
            "JSON.stringify({ua:navigator.userAgent,platform:navigator.platform,"
            "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})"))
        return json.loads(raw)

    try:
        pinned = loop.run_until_complete(tool("identity_pin")(
            instance_id=iid, fingerprint=fingerprint))
        assert fingerprint["user_agent"] in pinned
        for _ in range(2):
            launched = loop.run_until_complete(tool("spawn_browser")(
                iid, url=PROBE.as_uri(), headless=True, persistent=False))
            assert "identity=pinned" in launched
            loop.run_until_complete(tool("switch_instance")(iid))
            values = reported()
            assert values == {
                "ua": fingerprint["user_agent"],
                "platform": fingerprint["platform"],
                "timezone": fingerprint["timezone"],
            }
            shown = loop.run_until_complete(tool("identity_show")())
            assert fingerprint["timezone"] in shown
            loop.run_until_complete(tool("close_instance")(iid))
            loop.run_until_complete(tool("switch_instance")("main"))
    finally:
        loop.run_until_complete(tool("close_instance")(iid))
        loop.run_until_complete(tool("switch_instance")("main"))
        _identity_path(iid).unlink(missing_ok=True)


def test_privacy_launch_rotates_and_removes_profiles(loop):
    iid = f"privacy_e2e_{os.getpid()}"
    fingerprints = []
    profiles = []
    try:
        for _ in range(2):
            launched = loop.run_until_complete(tool("spawn_browser")(
                iid, url=PROBE.as_uri(), headless=True, privacy=True))
            assert "privacy=True" in launched
            loop.run_until_complete(tool("switch_instance")(iid))
            listing = json.loads(loop.run_until_complete(tool("list_instances")()))
            row = next(item for item in listing["instances"] if item["instance_id"] == iid)
            profile = pathlib.Path(row["profile"])
            profiles.append(profile)
            assert row["privacy"] is True and profile.exists()
            fingerprints.append(loop.run_until_complete(tool("evaluate")(
                "navigator.userAgent + '|' + navigator.platform + '|' + "
                "Intl.DateTimeFormat().resolvedOptions().timeZone")))
            closed = loop.run_until_complete(tool("close_instance")(iid))
            assert "Error:" not in closed
            assert not profile.exists()
        assert fingerprints[0] != fingerprints[1]
        assert profiles[0] != profiles[1]
    finally:
        loop.run_until_complete(tool("close_instance")(iid))
        loop.run_until_complete(tool("switch_instance")("main"))


def test_dry_run_blocks_mutations_and_captures_what_they_would_send(loop):
    """The point of dry-run: learn what a destructive call sends, without sending it.

    Uses an unresolvable host so the check needs no network — the assertions are
    on OUR interception log, which records the decision before the request ever
    leaves the browser.
    """
    import json as _json

    try:
        started = _json.loads(loop.run_until_complete(tool("dry_run")()))
        assert "POST" in started["blocking"]

        loop.run_until_complete(tool("evaluate")("""
            (async () => {
              try { await fetch('https://blocked.invalid/api/items', {
                      method: 'POST', body: '{"delete_many":[1,2,3]}' }); } catch (e) {}
              try { await fetch('https://blocked.invalid/api/items'); } catch (e) {}
              return 'done';
            })()"""))

        seen = _json.loads(loop.run_until_complete(tool("intercept_log")()))
        assert seen["active"] is True
        posts = [r for r in seen["requests"] if r["method"] == "POST"]
        assert posts, "the POST should have been intercepted"
        assert posts[0]["outcome"] == "blocked"
        # The body is the whole reason to do this rather than just watch traffic.
        assert "delete_many" in (posts[0]["body"] or "")
        # A GET is not a mutation, so it must be waved through untouched.
        gets = [r for r in seen["requests"] if r["method"] == "GET"]
        assert gets and gets[0]["outcome"] == "passed"
    finally:
        loop.run_until_complete(tool("intercept_stop")())

    # An idle log must not read as "nothing happened".
    after = _json.loads(loop.run_until_complete(tool("intercept_log")()))
    assert after["active"] is False and "note" in after


def test_intercept_log_distinguishes_idle_from_silent(loop):
    """An empty list means 'not watching' here, and says so."""
    import json as _json

    loop.run_until_complete(tool("intercept_stop")())
    idle = _json.loads(loop.run_until_complete(tool("intercept_log")()))
    assert idle["active"] is False
    assert "NOT intercepting" in idle["note"]
