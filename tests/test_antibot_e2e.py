"""The anti-bot tripwire on a real headless Chrome, against a local http.server: no real site.

The field case (0.27 field requests, a research session on Shopee): after about 230 pages in one
day the first product page of a new run was redirected to
/verify/captcha?anti_bot_tracking_id=<about 2 KB>&scene=crawler_item&next=/verify/traffic?...
The agent noticed only through its own assert, went on opening pages into the wall, and its failed
workflow result was 8 KB with the captcha URL four times.

The fixture serves five product pages; page 3 redirects to such a verification page until the
test marks it solved (the person's part). The built-in Shopee rule is the one that fires: the
tests give the same rule to the fixture's host 127.0.0.1 (antibot.BUILT_IN patched in process),
and one test uses a person's own rule from the antibot_rules setting instead.

Criteria from the field: (a) a 5-page workflow stops at that page, no navigation after it reaches
the fixture, output under 1 KB; (c) after the person is done, resume_with continues from the page
that stopped. The bar's request (a) is checked by the coordinator in a headed run: this browser is
headless, and its result says nobody can see a request.

    MCP_E2E=1 uv run pytest tests/test_antibot_e2e.py -v
"""
import asyncio
import http.server
import json
import os
import statistics
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_E2E"),
    reason="needs a real Chrome; set MCP_E2E=1 to run",
)

TRACKING = "A" * 2048  # the field URL carried about 2 KB of tracking id


def tool(name):
    from mcp_stealth_chrome.server import mcp
    return mcp._tool_manager._tools[name].fn


class Site:
    """What the fixture serves, and every path it was asked for."""

    def __init__(self):
        self.requests: list[str] = []
        self.solved = False
        self.lock = threading.Lock()

    def seen(self, prefix: str) -> list[str]:
        with self.lock:
            return [p for p in self.requests if p.startswith(prefix)]

    def reset(self):
        with self.lock:
            self.requests.clear()
        self.solved = False


SITE = Site()

PAGES = {
    "/blog/what-is-captcha": "<h1>What is a CAPTCHA?</h1><p>An article about captcha and verify.</p>",
    "/search": "<h1>Search</h1><p>Results for verify challenge captcha</p>",
    "/verify-email": "<h1>Verify your email</h1><p>We sent you a link.</p>",
    "/challenge/123": "<h1>Coding challenge 123</h1>",
    "/verify/captchas": "<h1>All the captchas we know</h1>",
    "/cf-login": ("<title>Sign in</title><form><input name=user><div class='cf-turnstile' "
                  "data-sitekey='0x4AAAAAAAAAAAAAAA'></div><input type=hidden "
                  "name='cf-turnstile-response' value=''><button>Sign in</button></form>"),
    "/start": "<h1>Start</h1><a id=go href='/p3'>to product 3</a>",
    "/frame-path": "<iframe src='/p3'></iframe>",
    "/frame-words": "<iframe src='/words'></iframe>",
    "/popup": "<a id=open target=_blank href='/verify/captcha'>Open</a>",
    "/late-text": ("<body><p>Loading</p><script>onload=()=>setTimeout(()=>"
                   "document.querySelector('p').textContent='Verifikasi untuk melanjutkan',180)</script>"),
    # A site merely served through Cloudflare: its bot-detection script and parameters on an
    # ordinary page (served from here, never from Cloudflare).
    "/cf-served": ("<title>Just a moment of your time</title><h1>Shop</h1><script>"
                   "window.__CF$cv$params={r:'8f',t:'MTc'};</script>"
                   "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"),
    # A challenge page whose widget was solved: the page goes on by itself, nothing to stop for.
    "/cf-solved": ("<title>Un instant...</title><script>window._cf_chl_opt={cvId:'3'};</script>"
                   "<input type=hidden name='cf-turnstile-response' value='0.abcdefghijklmnop'>"),
    # A page ABOUT Cloudflare (from the field: a product landing page with a table of bot-check
    # results): its text names Turnstile, quotes the challenge's words and a .cf-turnstile selector,
    # and holds no widget. auto_verify said "CF detected but no clickable widget found" on it.
    "/about-cf": ("<title>A stealth browser for agents</title><h1>Bot checks we tested</h1><table>"
                  "<tr><td>Cloudflare Turnstile</td><td>passed</td></tr>"
                  "<tr><td>The 'Just a moment...' page</td><td>Checking your browser, then "
                  "Verify you are human</td></tr><tr><td>Performing security verification</td>"
                  "<td>handled</td></tr></table><p>The widget sits in <code>.cf-turnstile</code> and "
                  "posts <code>cf-turnstile-response</code> from challenges.cloudflare.com.</p>"),
    # The other language-free mark: the challenge's own script, in Spanish.
    "/cf-orchestrate": ("<title>Un momento...</title><div><input type=hidden "
                        "name='cf-turnstile-response' value=''></div><script "
                        "src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8f'></script>"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with SITE.lock:
            SITE.requests.append(self.path)
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            return self._send(404, "")
        if path in ("/p1", "/p2", "/p3", "/p4", "/p5"):
            if path == "/p3" and not SITE.solved:
                self.send_response(302)
                self.send_header("Location", f"/verify/captcha?scene=crawler_item&anti_bot_tracking_id="
                                             f"{TRACKING}&next=/verify/traffic?redirect=/p3")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, f"<title>Product {path[2]}</title><h1>Product {path[2]}</h1>")
        if path == "/verify/captcha":
            return self._send(200, "<title>Shopee</title><p>Verifikasi untuk melanjutkan</p>"
                                   "<p>Geser untuk menyelesaikan puzzle</p>")
        if path == "/words":  # no URL rule fires here; only the page's words do
            return self._send(200, "<title>Shopee</title><div>Verifikasi untuk melanjutkan</div>")
        if path == "/cf-header":
            return self._send(403, "<title>Un instant</title>", (("cf-mitigated", "challenge"),))
        if path == "/cf-interstitial":
            # The marks of a Cloudflare challenge page that do not depend on its language: the
            # response header and its inline options script, with its widget not solved. The title
            # is in another language on purpose.
            return self._send(403, (
                "<title>Un instant...</title><script>window._cf_chl_opt={cvId:'3',cType:'managed'};"
                "</script><div id=w><input type=hidden name='cf-turnstile-response' value=''></div>"),
                headers=(("cf-mitigated", "challenge"),))
        if path in PAGES:
            return self._send(200, PAGES[path])
        return self._send(404, "<h1>not here</h1>")


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def loop(site):
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    # Never the real profile (persistent=False), headless: the person uses this screen.
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


@pytest.fixture
def rules(monkeypatch):
    """The built-in Shopee rule, given to the fixture's host as well; the tripwire at its default."""
    from mcp_stealth_chrome import antibot, server
    shopee = antibot.BUILT_IN[0]
    local = antibot.Rule("127.0.0.1", path=shopee.path, query=shopee.query, text=shopee.text)
    monkeypatch.setattr(antibot, "BUILT_IN", (local,) + antibot.BUILT_IN)
    monkeypatch.delenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", raising=False)
    monkeypatch.delenv("MCP_STEALTH_ANTIBOT_RULES", raising=False)
    SITE.reset()
    server._TRIPWIRE_SEEN.clear()
    server._TRIPWIRE_NOTES.clear()
    yield local
    SITE.reset()


def run(loop, coro):
    return loop.run_until_complete(coro)


def five_pages(site):
    return [{"tool": "navigate", "args": {"url": f"{site}/p{n}", "auto_verify": False}}
            for n in range(1, 6)]


def test_a_five_page_workflow_stops_at_the_verification_page_and_resumes(loop, site, rules):
    """(a) stops at page 3, no request for pages 4 and 5 reaches the fixture, under 1 KB; (c)
    after the person is done, resume_with continues from the page that stopped."""
    out = run(loop, tool("workflow_run")(steps=five_pages(site)))
    assert out.startswith("Error: {"), out
    report = json.loads(out[len("Error: "):])
    assert report["status"] == "antibot_paused", out
    assert report["paused_at"] == 2 and report["paused_path"] == "2", out
    assert "failed_at" not in report and "failed_count" not in report, out
    assert report["counts"] == {"ok": 2, "paused": 1}, out  # no count of 0 in a pause
    assert "path /verify/captcha" in report["antibot"]["trigger"], out
    assert "query cut: +" in report["antibot"]["page"] and "get_url gives it whole" in out, out
    assert "headless" in report["antibot"]["next"], out
    assert report["resume_with"].startswith("workflow_run(steps=..., start_at=2"), out
    assert TRACKING not in out and len(out.encode()) < 1024, (len(out.encode()), out)
    assert not SITE.seen("/p4") and not SITE.seen("/p5"), SITE.requests
    assert len(SITE.seen("/p3")) == 1, SITE.requests
    # The person passes the check; the same call continues from the page that stopped.
    SITE.solved = True
    again = run(loop, tool("workflow_run")(steps=five_pages(site), start_at=report["paused_at"],
                                           vars=report.get("vars")))
    assert not again.startswith("Error:"), again
    assert SITE.seen("/p4") and SITE.seen("/p5"), SITE.requests
    assert json.loads(again)["counts"]["ok"] == 3, again


def test_navigate_pauses_with_a_short_result_and_says_it_once(loop, site, rules):
    url = f"{site}/p3"
    out = run(loop, tool("navigate")(url, auto_verify=False, expect_status=200))
    first = out.splitlines()[0]
    assert first == "Error: antibot_paused: navigate reached a verification page, not the page asked for.", out
    assert "trigger: 127.0.0.1 built-in rule, path /verify/captcha (also: query scene=crawler_item)" in out
    assert "asked for: 127.0.0.1:" in out and "/p3" in out, out
    assert "expect_status" not in out, out  # the pause is the answer
    assert TRACKING not in out and len(out.encode()) < 1024, (len(out.encode()), out)
    # A second tool on the same page says it in one line.
    again = run(loop, tool("reload")())
    assert again.startswith("Error: antibot_paused: still on the verification page reported before"), again
    assert "\n" not in again and "headless" in again, again
    # get_url gives the whole URL: navigation tools never mask or cut it.
    assert TRACKING in run(loop, tool("get_url")())


def test_warn_mode_keeps_the_result_and_puts_one_line_first(loop, site, rules, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "warn")
    out = run(loop, tool("navigate")(f"{site}/p3", auto_verify=False))
    lines = out.splitlines()
    assert lines[0].startswith("antibot_warning: verification page (127.0.0.1 built-in rule"), out
    assert "antibot_tripwire=warn, nothing paused" in lines[0], out
    assert lines[1].startswith("Navigated: "), out
    out = run(loop, tool("workflow_run")(steps=five_pages(site)))
    assert not out.startswith("Error:"), out
    assert SITE.seen("/p5"), SITE.requests  # warn never stops a run


def test_a_click_onto_the_verification_page_stops_the_run_before_its_next_step(loop, site, rules):
    steps = [{"tool": "navigate", "args": {"url": f"{site}/start", "auto_verify": False}},
             {"tool": "click", "args": {"selector": "#go"}},
             {"tool": "navigate", "args": {"url": f"{site}/p4", "auto_verify": False}},
             {"tool": "navigate", "args": {"url": f"{site}/p5", "auto_verify": False}}]
    out = run(loop, tool("workflow_run")(steps=steps))
    report = json.loads(out[len("Error: "):])
    assert report["status"] == "antibot_paused" and report["paused_at"] == 2, out
    shown = {str(e["index"]): e for e in report["shown"]}
    assert shown["2"] == {"index": 2, "tool": "navigate", "paused": "antibot", "ran": False}, out
    assert not SITE.seen("/p4") and not SITE.seen("/p5"), SITE.requests
    assert len(out.encode()) < 1024, (len(out.encode()), out)
    # The click is not repeated on resume: the run starts at the step after it.
    assert "start_at=2" in report["resume_with"], out


def test_batch_actions_stops_too(loop, site, rules):
    actions = [{"type": "navigate", "url": f"{site}/p{n}", "auto_verify": False} for n in range(1, 6)]
    out = run(loop, tool("batch_actions")(actions))
    assert out.startswith("Error: antibot_paused: batch_actions stopped at action [2] (navigate)"), out
    assert "resume_with: batch_actions with actions[2:]" in out, out
    assert not SITE.seen("/p4") and not SITE.seen("/p5"), SITE.requests
    assert TRACKING not in out and len(out.encode()) < 1024, (len(out.encode()), out)


def test_the_page_words_fire_only_on_a_host_with_a_rule(loop, site, rules):
    out = run(loop, tool("navigate")(f"{site}/words", auto_verify=False))
    assert out.startswith("Error: antibot_paused:"), out
    assert "page text 'Verifikasi untuk melanjutkan'" in out, out


def test_a_persons_own_rule(loop, site, rules, monkeypatch):
    from mcp_stealth_chrome import antibot
    monkeypatch.setattr(antibot, "BUILT_IN", antibot.BUILT_IN[1:])  # only the Shopee one again
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_RULES", json.dumps(
        {"127.0.0.1": {"url": ["^/verify/captcha"], "text": ["Geser untuk"]}}))
    out = run(loop, tool("navigate")(f"{site}/p3", auto_verify=False))
    assert "trigger: 127.0.0.1 rule from your antibot_rules, url pattern '^/verify/captcha'" in out, out


def test_off_reads_nothing_and_changes_nothing(loop, site, rules, monkeypatch):
    from mcp_stealth_chrome import server
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "off")
    called = []
    real = server._tripwire_probe

    async def probe(*a, **k):
        called.append(a)
        return await real(*a, **k)

    monkeypatch.setattr(server, "_tripwire_probe", probe)
    out = run(loop, tool("navigate")(f"{site}/p3", auto_verify=False))
    assert out.startswith("Navigated: "), out
    out = run(loop, tool("workflow_run")(steps=five_pages(site)))
    assert not out.startswith("Error:") and SITE.seen("/p5"), out
    assert called == []


@pytest.mark.parametrize("path", ["/blog/what-is-captcha", "/search?q=verify+challenge&captcha=1",
                                  "/verify-email", "/challenge/123", "/verify/captchas", "/cf-login",
                                  "/cf-served", "/cf-solved"])
def test_ordinary_pages_with_the_words_never_fire(loop, site, rules, path):
    """A false stop of a long run is a defect too: these pages carry the words captcha, verify,
    challenge, or an embedded Turnstile in a login form, on the host with the rule."""
    steps = [{"tool": "navigate", "args": {"url": f"{site}{path}", "auto_verify": False}},
             {"tool": "get_url", "args": {}},
             {"tool": "get_text", "args": {}}]
    out = run(loop, tool("workflow_run")(steps=steps))
    assert not out.startswith("Error:") and "antibot" not in out, out


@pytest.mark.parametrize("auto_verify", [True, False])
def test_a_page_about_cloudflare_is_no_challenge(loop, site, rules, auto_verify):
    """Its text alone is no challenge: no pause, and auto_verify neither waits nor says it saw one."""
    started = time.monotonic()
    out = run(loop, tool("navigate")(f"{site}/about-cf", auto_verify=auto_verify))
    elapsed = time.monotonic() - started
    assert out.startswith("Navigated: "), out
    assert "auto-verify" not in out and "antibot" not in out, out
    assert elapsed < 1.5, elapsed  # the old text-only match waited 2 s and clicked at nothing
    after = run(loop, tool("workflow_run")(steps=[
        {"tool": "navigate", "args": {"url": f"{site}/about-cf", "auto_verify": auto_verify}},
        {"tool": "get_url", "args": {}}, {"tool": "get_text", "args": {}}]))
    assert not after.startswith("Error:") and "antibot" not in after, after


def test_a_cloudflare_challenge_page_in_another_language_fires(loop, site, rules):
    out = run(loop, tool("navigate")(f"{site}/cf-interstitial", auto_verify=False))
    assert out.startswith("Error: antibot_paused:"), out
    assert "a Cloudflare challenge page (its challenge script (_cf_chl_opt), response header " \
           "cf-mitigated: challenge)" in out, out
    assert "solve_turnstile_oscursor()" in out and "the person can pass it" in out, out


def test_the_challenge_script_mark_fires_between_steps(loop, site, rules):
    """No header and no auto_verify: the look between steps reads the challenge's own script."""
    steps = [{"tool": "navigate", "args": {"url": f"{site}/cf-orchestrate", "auto_verify": False}},
             {"tool": "get_url", "args": {}},
             {"tool": "navigate", "args": {"url": f"{site}/p5", "auto_verify": False}}]
    out = run(loop, tool("workflow_run")(steps=steps))
    report = json.loads(out[len("Error: "):])
    assert report["status"] == "antibot_paused" and report["paused_at"] == 1, out
    assert "its challenge script /cdn-cgi/challenge-platform/.../orchestrate/" in out, out
    assert not SITE.seen("/p5"), SITE.requests


def test_a_click_onto_the_page_says_it_first_and_is_no_error(loop, site, rules):
    run(loop, tool("navigate")(f"{site}/start", auto_verify=False))
    out = run(loop, tool("click")(selector="#go"))
    assert out.startswith("antibot_warning: this led to a verification page (127.0.0.1 built-in rule"), out
    assert "headless" in out.splitlines()[0] and "\nclick '#go': clicked" in out, out


def test_the_check_between_steps_costs_little_on_a_page_with_no_rule(loop, site, rules):
    """The number goes in the report: one isolated evaluate per step on a host with no rule."""
    from mcp_stealth_chrome import antibot, server
    monkeypatch_rules = (antibot.BUILT_IN[1],)  # Shopee only: 127.0.0.1 has no rule here
    run(loop, tool("navigate")(f"{site}/p1", auto_verify=False))
    original = antibot.BUILT_IN
    antibot.BUILT_IN = monkeypatch_rules
    try:
        times = []
        for _ in range(40):
            start = time.perf_counter()
            said = run(loop, server._tripwire_step("click", "get_text"))
            times.append((time.perf_counter() - start) * 1000)
            assert said is None
    finally:
        antibot.BUILT_IN = original
    times.sort()
    print(f"\ntripwire between steps, host with no rule: median {statistics.median(times):.2f} ms, "
          f"p90 {times[int(len(times) * 0.9)]:.2f} ms, max {times[-1]:.2f} ms")
    assert statistics.median(times) < 20


@pytest.mark.parametrize("path,mark", [("/frame-path", "path /verify/captcha"),
                                       ("/frame-words", "page text"), ("/late-text", "page text")])
def test_verification_in_loaded_document(loop, site, rules, path, mark):
    out = run(loop, tool("navigate")(site + path, auto_verify=False))
    assert out.startswith("Error: antibot_paused:") and mark in out, out
    if path.startswith("/frame"):
        assert "in child frame" in out, out


def test_login_with_default_verification_is_left_alone(loop, site, rules):
    out = run(loop, tool("navigate")(site + "/cf-login"))
    assert out.startswith("Navigated:") and "auto-verify" not in out and "antibot" not in out, out


def test_popup_stops_batch_before_next_request(loop, site, rules):
    out = run(loop, tool("batch_actions")([{"type": "navigate", "url": site + "/popup", "auto_verify": False},
                                        {"type": "click", "selector": "#open"},
                                        {"type": "navigate", "url": site + "/p4"}]))
    assert out.startswith("Error: antibot_paused:") and "in opened tab" in out, out
    assert "actions[2:]" in out and not SITE.seen("/p4"), out


@pytest.mark.parametrize("launch", ["spawn_browser", "browser_launch"])
def test_fresh_launch_reports_current_challenge_header(loop, site, rules, launch, monkeypatch):
    from mcp_stealth_chrome.tools import instances
    if launch == "spawn_browser":
        monkeypatch.setattr(instances, "ok", lambda msg: "OK: " + msg)
        try:
            out = run(loop, tool(launch)(instance_id="tripwire", headless=True, persistent=False,
                                         url=site + "/cf-header"))
        finally:
            run(loop, tool("close_instance")(instance_id="tripwire"))
    else:
        run(loop, tool("browser_close")())  # exercise a fresh launch, not reuse
        out = run(loop, tool(launch)(headless=True, persistent=False, auto_verify=False, url=site + "/cf-header"))
    assert out.startswith(f"Error: antibot_paused: {launch} reached") and "running" in out, out
    assert "response header cf-mitigated: challenge" in out, out
