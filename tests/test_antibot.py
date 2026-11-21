"""The anti-bot tripwire without a browser: the rules, the words a result says, the workflow and
batch stop, the request for the person, and its withdrawal.

From the field (0.27 field requests, a research session on Shopee): after about 230 pages in one
day a product page was redirected to /verify/captcha?anti_bot_tracking_id=<about 2 KB>&
scene=crawler_item&next=/verify/traffic?..., and the agent learnt it only from its own asserts.
Criteria: (a) a 5-page workflow stops at that page, no navigation after it, output under 1 KB, the
bar shows the request; (c) after Done, resume_with continues from the page that stopped. The same
on real Chrome: tests/test_antibot_e2e.py."""
import asyncio
import json
import subprocess
import sys

import pytest

from mcp_stealth_chrome import antibot, presence, server, settings, workflow
from mcp_stealth_chrome.state import BrowserState
from mcp_stealth_chrome.tools import batch
from mcp_stealth_chrome.tools import navigation as nav

TRACKING = "T" * 2048
FIELD_URL = (f"https://shopee.co.id/verify/captcha?anti_bot_tracking_id={TRACKING}"
             "&scene=crawler_item&next=/verify/traffic?redirect=%2Fproduct%2F1%2F2")
KEY = "TRIPWIRE-TAB"


# ── The rules ─────────────────────────────────────────────────────────────────────────────────

def test_the_field_url_fires_the_built_in_shopee_rule():
    verdict = antibot.url_verdict(FIELD_URL, antibot.BUILT_IN)
    assert verdict.trigger == ("shopee.co.id built-in rule, path /verify/captcha "
                               "(also: query scene=crawler_item)")
    assert verdict.place == "shopee.co.id /verify/captcha"
    for url in ("https://shopee.co.id/verify/traffic", "https://shopee.co.id/verify/traffic/x",
                "https://mall.shopee.co.id/verify/captcha?x=1"):
        assert antibot.url_verdict(url, antibot.BUILT_IN) is not None, url
    query = antibot.url_verdict("https://shopee.co.id/product/1/2?scene=crawler_item", antibot.BUILT_IN)
    assert query.trigger == "shopee.co.id built-in rule, query scene=crawler_item"


@pytest.mark.parametrize("url", [
    # The words alone, in an ordinary path or query, on the host with the rule and off it.
    "https://shopee.co.id/search?keyword=captcha+verify+challenge",
    "https://shopee.co.id/verify-email", "https://shopee.co.id/verify/captchas",
    "https://shopee.co.id/verify", "https://shopee.co.id/blog/verify/captcha",
    "https://shopee.co.id/product/1/2?scene=homepage", "https://shopee.co.id/x?xscene=crawler_item",
    "https://shopee.co.id/x?q=scene%3Dcrawler_item",
    "https://example.com/verify/captcha?scene=crawler_item", "https://blog.example.com/what-is-captcha",
    "https://example.com/challenge/123?verify=1", "https://notshopee.co.id/verify/captcha",
    "https://shopee.co.id.example.com/verify/captcha", "about:blank", "chrome://newtab/",
    "data:text/html,<p>/verify/captcha</p>", "ftp://shopee.co.id/verify/captcha", "", None,
])
def test_no_rule_fires_on_a_word_alone(url):
    assert antibot.url_verdict(url, antibot.BUILT_IN) is None


def test_the_page_words_fire_only_for_the_host_of_their_rule():
    words = ["verifikasi untuk melanjutkan"]
    assert antibot.text_verdict("https://shopee.co.id/p", words, antibot.BUILT_IN).trigger == (
        "shopee.co.id built-in rule, page text 'Verifikasi untuk melanjutkan'")
    assert antibot.text_verdict("https://example.com/p", words, antibot.BUILT_IN) is None
    assert antibot.text_verdict("https://shopee.co.id/p", [], antibot.BUILT_IN) is None
    assert antibot.wants_text("https://shopee.co.id/p", antibot.BUILT_IN)
    assert not antibot.wants_text("https://example.com/p", antibot.BUILT_IN)
    assert antibot.text_words(antibot.BUILT_IN) == {
        "shopee.co.id": ["verifikasi untuk melanjutkan", "geser untuk menyelesaikan puzzle"]}


def test_a_persons_rules_search_path_and_query_never_the_host():
    rules = antibot.person_rules({"example.com": {"url": ["^/verify/", "scene=crawler_"],
                                                  "text": ["Verify to continue"]}})
    hit = antibot.url_verdict("https://www.example.com/verify/x?scene=crawler_1", rules)
    assert hit.trigger == ("example.com rule from your antibot_rules, url pattern '^/verify/' "
                           "(also: url pattern 'scene=crawler_')")
    assert antibot.url_verdict("https://verify.example.org/x", rules) is None
    assert antibot.url_verdict("https://example.com/a?b=verify", rules) is None
    assert antibot.text_words(rules) == {"example.com": ["verify to continue"]}

def test_person_url_regex_input_is_bounded(monkeypatch):
    seen = []
    real_search = antibot.re.search

    def search(pattern, target):
        seen.append(target)
        return real_search(pattern, target)

    monkeypatch.setattr(antibot.re, "search", search)
    rules = (antibot.Rule("example.com", url=("^/never",)),)
    assert antibot.url_verdict("https://example.com/" + "x" * 10000, rules) is None
    assert seen and max(map(len, seen)) <= antibot.URL_MATCH_LENGTH


def test_the_cloudflare_verdict_names_its_marks():
    v = antibot.cloudflare_verdict("https://x.test/a", "its challenge script (_cf_chl_opt)", "challenge")
    assert v.kind == "cloudflare"
    assert v.trigger == ("a Cloudflare challenge page (its challenge script (_cf_chl_opt), response "
                         "header cf-mitigated: challenge)")
    assert "cf-mitigated" not in antibot.cloudflare_verdict("https://x.test/a", "m", None).trigger


def test_the_page_line_cuts_the_query_and_says_where_the_whole_url_is():
    line = antibot.place(FIELD_URL)
    assert line.startswith("shopee.co.id/verify/captcha?") and TRACKING not in line
    assert line.endswith("; get_url gives it whole]") and "[query cut: +" in line
    cut = int(line.split("[query cut: +")[1].split(" ")[0])
    shown = line.split("?", 1)[1].split("... [", 1)[0]
    assert len(shown) + cut == len(FIELD_URL.split("?", 1)[1])
    assert antibot.place("https://a.test/p?x=1#token") == "a.test/p?x=1 [#fragment left out]"
    assert antibot.place("https://a.test/" + "p" * 300).endswith("[path cut: +181 chars]")
    assert antibot.place(None) == "(unknown)"


def test_the_first_line_goes_before_the_result_and_after_error():
    assert antibot.with_first_line("Navigated: x", "L") == "L\nNavigated: x"
    assert antibot.with_first_line("Error: navigate postcondition failed", "L") == (
        "Error: L\nnavigate postcondition failed")


# ── The settings ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sfile(tmp_path, monkeypatch):
    path = tmp_path / "data" / "settings.json"
    monkeypatch.setenv(settings.ENV_FILE, str(path))
    for s in settings.SETTINGS:
        monkeypatch.delenv(s.env, raising=False)
    return path


def test_the_tripwire_setting_is_a_choice_with_pause_by_default(sfile, monkeypatch):
    assert settings.resolve("antibot_tripwire") == {"value": "pause", "source": "default"}
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "WARN")
    assert settings.get("antibot_tripwire") == "warn"
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "stop")
    assert settings.get("antibot_tripwire") == "pause"  # not valid: skipped with a note


def test_a_persons_rules_are_taken_whole_or_not_at_all(sfile, monkeypatch):
    good = {"Example.COM": {"url": ["^/verify/"], "text": ["Verify to continue"]}}
    assert settings.parse_rules(json.dumps(good)) == (
        True, {"example.com": {"url": ["^/verify/"], "text": ["Verify to continue"]}})
    for bad in ('{"example.com": {"url": ["(unclosed"]}}', '{"example.com": {"urls": ["^/x"]}}',
                '{"example.com": {}}', '{"example.com": {"url": [""]}}', '{"exa mple": {"url": ["a"]}}',
                '{"example.com": {"text": "not a list"}}', '[1]', 'not json',
                json.dumps({f"h{i}.test": {"url": ["a"]} for i in range(settings.RULE_HOSTS + 1)}),
                json.dumps({"a.test": {"url": ["a"] * (settings.RULE_PATTERNS + 1)}}),
                json.dumps({"a.test": {"text": ["w" * (settings.RULE_LENGTH + 1)]}})):
        assert settings.parse_rules(bad) == (False, None), bad
    settings.set_value("antibot_rules", json.dumps(good))
    assert settings.resolve("antibot_rules")["source"] == "file"
    assert settings._shown(settings.get("antibot_rules")) == "1 host"
    with pytest.raises(ValueError, match="a JSON object"):
        settings.set_value("antibot_rules", '{"a.test": {"url": ["("]}}')
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_RULES", "{broken")
    resolved = settings.resolve("antibot_rules")
    assert resolved["source"] == "file" and "MCP_STEALTH_ANTIBOT_RULES" in resolved["note"]
    mode, rules = server._tripwire_settings()
    assert mode == "pause" and rules[-1].host == "example.com" and rules[:-1] == antibot.BUILT_IN


# ── The workflow engine and batch_actions ─────────────────────────────────────────────────────

def _pause_text(page=FIELD_URL, asked="https://shopee.co.id/product/1/3", where="bar"):
    verdict = antibot.url_verdict(page, antibot.BUILT_IN)
    return (f"Error: {antibot.PAUSED}: navigate reached a verification page, not the page asked for.\n"
            + antibot.pause_lines(verdict, asked, antibot.next_line(where, "run resume_with", False)))


class Site:
    """Five product pages; page 3 answers with the verification page until the person is done."""

    def __init__(self):
        self.opened: list[str] = []
        self.solved = False
        self.checks: list = []

    async def dispatch(self, tool, args):
        if tool == "navigate":
            self.opened.append(args["url"])
            if args["url"].endswith("/3") and not self.solved:
                return _pause_text()
            return f"Navigated: {args['url']} [status 200]"
        return f"{tool} ok"


def _steps(n=5):
    return [{"tool": "navigate", "args": {"url": f"https://shopee.co.id/product/1/{i}"}}
            for i in range(1, n + 1)]


def run(steps, site, **kw):
    return asyncio.run(workflow.run_workflow(steps, site.dispatch, lambda name: True, **kw))


@pytest.mark.parametrize("stop_on_error", [True, False])
def test_a_five_page_workflow_stops_at_the_verification_page_and_resumes(stop_on_error):
    """(a) and (c), whatever stop_on_error says, and continue_on_error does not go past it."""
    site = Site()
    steps = _steps()
    steps[2]["continue_on_error"] = True
    report = run(steps, site, stop_on_error=stop_on_error, output="summary")
    assert site.opened == [f"https://shopee.co.id/product/1/{i}" for i in (1, 2, 3)]  # none after it
    assert report["status"] == "antibot_paused" and list(report)[0] == "status"
    assert report["paused_at"] == 2 and report["paused_path"] == "2"
    assert report["counts"] == {"ok": 2, "paused": 1}
    assert "failed_at" not in report and "failed_count" not in report and "failed_paths" not in report
    assert report["shown"] == [{"index": 2, "tool": "navigate", "paused": "antibot", "ran": True}]
    assert report["antibot"]["trigger"].startswith("shopee.co.id built-in rule, path /verify/captcha")
    assert "query cut: +" in report["antibot"]["page"] and "asked_for" not in report["antibot"]
    assert report["vars"]["last"] == {"ok": False, "value": None,
                                      "text": "antibot_paused at shopee.co.id/verify/captcha"}
    assert report["resume_with"] == "workflow_run(steps=..., start_at=2, vars=<the vars above>)"
    text = "Error: " + json.dumps(report, indent=2, ensure_ascii=False)
    assert TRACKING not in text and len(text.encode()) < 1024, len(text.encode())
    site.solved = True  # the person did it; the same call continues from the page that stopped
    again = run(steps, site, start_at=report["paused_at"], variables=report["vars"], output="summary")
    assert "status" not in again and again["counts"]["ok"] == 3
    assert site.opened[3:] == [f"https://shopee.co.id/product/1/{i}" for i in (3, 4, 5)]


def test_a_checkpoint_stops_before_the_next_step_and_resume_starts_there():
    site = Site()

    async def checkpoint(previous, tool):
        site.checks.append((previous, tool))
        return _pause_text().replace("navigate reached", "the tab is on") if previous == "click" else None

    steps = [{"tool": "navigate", "args": {"url": "https://shopee.co.id/start"}},
             {"tool": "click", "args": {"selector": "#go"}},
             {"tool": "navigate", "args": {"url": "https://shopee.co.id/product/1/4"}}]
    report = run(steps, site, checkpoint=checkpoint)
    assert site.checks == [(None, "navigate"), ("navigate", "click"), ("click", "navigate")]
    assert site.opened == ["https://shopee.co.id/start"]  # step 2 never ran
    assert report["status"] == "antibot_paused" and report["paused_at"] == 2
    assert report["completed"][-1] == {"index": 2, "tool": "navigate", "paused": "antibot", "ran": False}
    assert "start_at=2" in report["resume_with"]


def test_a_paused_run_keeps_whole_vars_and_the_branches_a_resume_needs():
    site = Site()
    steps = [{"tool": "get_url", "args": {}, "save_as": "u"},
             {"if": {"value": "${u}", "contains": "ok"}, "then": _steps(5)}]
    report = run(steps, site, variables={"given": [1, 2]}, output="failures")
    assert report["paused_path"] == "1.2" and report["vars"]["given"] == [1, 2]
    assert report["vars"][workflow.BRANCHES_VAR] == {"1": "then"} and report["vars"]["u"] == "get_url ok"


def test_warn_lines_are_listed_and_the_run_goes_on():
    site = Site()
    site.solved = True

    async def checkpoint(previous, tool):
        return "antibot_warning: verification page (x)" if previous == "navigate" and tool == "click" else None

    steps = _steps(1) + [{"tool": "click", "args": {}}, {"tool": "get_url", "args": {}}]
    report = run(steps, site, checkpoint=checkpoint, output="summary")
    assert report["counts"]["ok"] == 3 and "status" not in report
    assert report["antibot_warnings"] == [{"before": 1, "warning": "antibot_warning: verification page (x)"}]


def test_a_batch_stops_at_the_verification_page(monkeypatch):
    site = Site()
    calls = []

    async def step_check(previous, tool):
        calls.append((previous, tool))
        return None

    async def navigate(url, **_k):
        return await site.dispatch("navigate", {"url": url})

    monkeypatch.setattr(server, "_register_workflow_tools", lambda: None)
    monkeypatch.setattr(server, "_WORKFLOW_TOOLS", {"navigate": navigate})
    monkeypatch.setattr(server, "_inner_arguments_error", lambda tool, args: None)
    monkeypatch.setattr(server, "_tripwire_step", step_check)
    monkeypatch.setattr(BrowserState, "browser", None)
    out = asyncio.run(batch.batch_actions(
        [{"type": "navigate", "url": f"https://shopee.co.id/product/1/{i}"} for i in range(1, 6)]))
    assert out.startswith("Error: antibot_paused: batch_actions stopped at action [2] (navigate): "
                          "it reached a verification page.\ntrigger: shopee.co.id built-in rule"), out
    assert "resume_with: batch_actions with actions[2:], once the person is done (action [2] runs again)" in out
    assert "2 of 5 actions ran before it, 0 failed" in out and out.endswith("[2] navigate: paused (antibot): "
                                                                            "it reached a verification page")
    assert len(site.opened) == 3 and calls[0] == (None, "navigate")


@pytest.mark.parametrize("runner", ["batch", "workflow"])
def test_checks_takeover_before_tripwire_checkpoint(monkeypatch, runner):
    checks = []

    async def should_not_probe(*_args):
        checks.append("tripwire")
        return None

    monkeypatch.setattr(server, "_register_workflow_tools", lambda: None)
    monkeypatch.setattr(server, "_WORKFLOW_TOOLS", {"navigate": should_not_probe})
    monkeypatch.setattr(server, "_inner_arguments_error", lambda tool, args: None)
    monkeypatch.setattr(server, "_tripwire_step", should_not_probe)
    monkeypatch.setattr(batch.presence, "refusal", lambda *_args: "Error: the person took over")
    monkeypatch.setattr(BrowserState, "browser", object())
    from mcp_stealth_chrome.tools.action_kit import workflow_run
    out = asyncio.run(batch.batch_actions([{"type": "navigate", "url": "https://example.test"}])
                      if runner == "batch" else workflow_run([{"tool": "navigate", "args": {"url": "https://example.test"}}]))
    assert "the person took over" in out
    assert checks == []
def test_workflow_rejects_disallowed_step_before_checkpoint(monkeypatch):
    from mcp_stealth_chrome.tools.action_kit import workflow_run
    checks = []

    async def checkpoint(*_args):
        checks.append("read")
        return None

    async def refusal(*_args):
        return "Error: the person took over"

    monkeypatch.setattr(server, "_WORKFLOW_TOOLS", {"get_url": lambda **_: "ok"})
    monkeypatch.setattr(server, "_inner_arguments_error", lambda *_args: None)
    monkeypatch.setattr(batch.presence, "refusal", refusal)
    monkeypatch.setattr(server, "_tripwire_step", checkpoint)
    out = asyncio.run(workflow_run([{"tool": "server_status", "args": {}}]))
    assert "unknown or non-allowlisted tool: server_status" in out
    assert checks == []


def test_late_text_probe_rechecks_takeover(monkeypatch, page):
    tab = page("https://shopee.co.id/product/1", words=[])
    probes = []
    held = [False]

    async def probe(*_args):
        probes.append(True)
        return {"href": tab.url, "words": [], "frames": []}

    def refusal(*_args):
        return "Error: the person took over" if held[0] else None

    monkeypatch.setattr(server, "_tripwire_probe", probe)
    monkeypatch.setattr(server.presence, "refusal", refusal)
    async def pause(_seconds):
        held[0] = True
    monkeypatch.setattr(server.asyncio, "sleep", pause)
    assert asyncio.run(server._tripwire_look(
        tab, url=tab.url, settle_text=True, want_cf=False)) is None
    assert probes == [True]


def test_a_batch_warning_leaves_a_failed_action_counted(monkeypatch):
    async def step_check(previous, tool):
        return "antibot_warning: verification page (x)" if previous else None

    async def fails(**_k):
        return "Error: element not found"

    monkeypatch.setattr(server, "_register_workflow_tools", lambda: None)
    monkeypatch.setattr(server, "_WORKFLOW_TOOLS", {"click": fails})
    monkeypatch.setattr(server, "_inner_arguments_error", lambda tool, args: None)
    monkeypatch.setattr(server, "_tripwire_step", step_check)
    monkeypatch.setattr(BrowserState, "browser", None)
    out = asyncio.run(batch.batch_actions([{"type": "click", "selector": "#a"},
                                           {"type": "click", "selector": "#b"}]))
    assert out.startswith("2 actions, 2 failed"), out
    assert "[1] click: Error: element not found\n    antibot_warning: verification page (x)" in out, out


# ── The page side, on a fake tab ──────────────────────────────────────────────────────────────

class _Target:
    target_id = KEY


class Tab:
    """A tab whose probe answers what the test says the page shows."""

    def __init__(self, url, browser, words=(), cf=""):
        self.target = _Target()
        self.browser = browser
        self.url, self.words, self.cf = url, list(words), cf
        self.probes = 0

    def sig(self):
        return {"url": self.url, "nodes": 10, "len": 370, "hash": self.url}


class _Connection:
    closed = False


class _Browser:
    def __init__(self):
        self.connection = _Connection()


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def page(monkeypatch, tmp_path, sfile):
    """A fake tab and browser in front of the tripwire, request_human_help and wait_for_human;
    overlay=True gives the browser an overlay handle on a stand-in process."""
    made = []

    def setup(url, *, overlay=False, headless=False, **kw):
        browser = _Browser()
        if overlay:
            folder = tmp_path / f"overlay{len(made)}"
            folder.mkdir()
            handle = presence.Overlay(browser, 4242, folder, subprocess.Popen(SLEEPER), False)
            setattr(browser, presence.ATTR, handle)
            made.append(handle)
        tab = Tab(url, browser, **kw)
        monkeypatch.setattr(BrowserState, "tabs", [tab])
        monkeypatch.setattr(BrowserState, "browser", browser)
        monkeypatch.setattr(BrowserState, "current_headless", headless)
        monkeypatch.setattr(BrowserState, "active_tab", classmethod(lambda cls: tab))
        monkeypatch.setattr(BrowserState, "is_up", classmethod(lambda cls: True))
        return tab

    async def probe(tab, rules, want_cf):
        tab.probes += 1
        return {"href": tab.url, "words": tab.words, **({"cf": tab.cf} if want_cf else {})}

    async def page_sig(tab, *_a, **_k):
        return tab.sig()

    async def in_front(_tab):
        return ["tab activated"]

    async def page_targets(_browser):
        return [(KEY, "x")]

    monkeypatch.setattr(server, "_HUMAN_HELP", {})
    monkeypatch.setattr(server, "_TRIPWIRE_SEEN", {})
    monkeypatch.setattr(server, "_TRIPWIRE_NOTES", {})
    monkeypatch.setattr(server, "_tripwire_probe", probe)
    monkeypatch.setattr(server, "_page_sig", page_sig)
    monkeypatch.setattr(server, "_page_targets", page_targets)
    monkeypatch.setattr(nav, "_foreground_for_human", in_front)
    yield setup
    for handle in made:
        handle.proc.kill()
        handle.proc.wait()


def look(tab, **kw):
    return asyncio.run(server._tripwire_look(tab, **kw))


def press_done(overlay):
    asked = float(overlay.agent.get("asked_at") or 0)
    tmp = overlay.control_file.with_name("control.json.test")
    tmp.write_text(json.dumps({"holder": "agent", "reason": "help done", "help_done": asked + 1, "answered_id": overlay.agent["request_id"]}))
    tmp.replace(overlay.control_file)


def test_a_tripwire_request_waits_as_an_agents_does_and_done_completes_it(page):
    """The request goes to the bar with every field wait_for_human reads; Done ends the wait."""
    tab = page(FIELD_URL, overlay=True)
    overlay = presence._handle(tab.browser)
    trip = look(tab, url=FIELD_URL)
    assert trip.where == "bar" and tab.probes == 0  # the URL rule needed no read of the page
    record = server._HUMAN_HELP[KEY]
    assert record["by"] == "tripwire" and record["was_shown"] and record["timeout"] == 300.0
    assert record["shown_at"] == record["asked_at"] and record["baseline"]["url"] == FIELD_URL
    assert {"objective", "requested_at", "requested_clock"} <= set(record)
    assert "verification page (shopee.co.id)" in overlay.agent["help"]
    text = server._tripwire_render(trip, "navigate", "Navigated: x", asked_for="https://shopee.co.id/p")
    assert text.startswith("Error: antibot_paused: navigate reached a verification page, not the page "
                           "asked for.\ntrigger: shopee.co.id built-in rule, path /verify/captcha")
    assert "next: the person was asked in the overlay's bar (Done); the window was not brought forward" in text
    press_done(overlay)
    result = asyncio.run(nav.wait_for_human(timeout=5))
    assert result.startswith("human handoff completed after") and "pressed Done" in result, result
    assert KEY not in server._HUMAN_HELP
    # It hands over, it does not judge: still on the page after Done means asked again.
    trip = look(tab, url=FIELD_URL)
    assert trip.again and trip.where == "bar" and server._HUMAN_HELP[KEY]["by"] == "tripwire"
    assert "again after the request for it was answered" in server._tripwire_render(trip, "navigate", "")




def test_popup_tripwire_waits_on_popup_and_done_clears_popup_record(page, monkeypatch):
    tab = page("https://shopee.co.id/product/1", overlay=True)
    popup = Tab(FIELD_URL, tab.browser)
    popup.target = type("Target", (), {"target_id": "POPUP-TAB"})()
    monkeypatch.setattr(BrowserState, "tabs", [tab, popup])
    trip = look(popup, url=FIELD_URL)
    assert trip is not None
    assert "POPUP-TAB" in server._HUMAN_HELP
    press_done(presence._handle(tab.browser))
    result = asyncio.run(nav.wait_for_human(timeout=5))
    assert result.startswith("human handoff completed")
    assert "POPUP-TAB" not in server._HUMAN_HELP


@pytest.mark.parametrize("at_click,later,pauses", [
    (FIELD_URL, FIELD_URL, True), (FIELD_URL, "https://shopee.co.id/product/2", False),
    ("about:blank", FIELD_URL, False), ("https://shopee.co.id/product/2", FIELD_URL, False),
])
def test_click_checks_only_a_popup_url_that_matched_at_click_time(page, monkeypatch, at_click, later, pauses):
    parent = page("https://example.test/", headless=True)
    popup = Tab(later, parent.browser, words=["verifikasi untuk melanjutkan"])
    popup.target = type("Target", (), {"target_id": "popup"})()
    monkeypatch.setattr(BrowserState, "tabs", [parent, popup])
    async def targets(browser):
        return [("popup", later)]
    monkeypatch.setattr(server, "_page_targets", targets)
    run = server._TRIPWIRE_RUN.set({"tool": "batch_actions"})
    try:
        before = {**parent.sig(), "pages": {KEY: parent.url}}
        after = {**before, "pages": {**before["pages"], "popup": at_click}, "opened_here": ["popup"]}
        clicked = server._click_outcome("clicked", "#open", before, after)
        assert ("in opened tab" in clicked) == (at_click == FIELD_URL)
        result = asyncio.run(server._tripwire_step("click", "navigate"))
        assert bool(result and result.startswith("Error: antibot_paused:")) == pauses
        assert popup.probes == 0
    finally:
        server._TRIPWIRE_RUN.reset(run)
def test_a_second_look_says_it_in_one_line(page):
    tab = page(FIELD_URL, overlay=True)
    look(tab, url=FIELD_URL)
    trip = look(tab, url=FIELD_URL)
    assert trip.repeat
    text = server._tripwire_render(trip, "reload", "Reloaded: x")
    assert text == ("Error: antibot_paused: still on the verification page reported before "
                    "(shopee.co.id /verify/captcha); the request is pending in the overlay's bar: "
                    "wait_for_human.")
    launched = server._tripwire_render(trip, "browser_launch", "x", launched=True)
    assert "the browser is running and stays open on it" in launched and "\n" not in launched


def test_headless_records_nothing_and_says_nobody_can_see_it(page):
    tab = page(FIELD_URL, headless=True)
    trip = look(tab, url=FIELD_URL)
    assert trip.where == "headless" and KEY not in server._HUMAN_HELP
    text = server._tripwire_render(trip, "navigate", "x")
    assert "this browser is headless, so nobody sees a request" in text


def test_the_launch_says_the_browser_is_running(page):
    tab = page(FIELD_URL, headless=True)
    text = server._tripwire_render(look(tab, url=FIELD_URL), "browser_launch", "Browser launched",
                                   asked_for="https://shopee.co.id/p", launched=True)
    assert text.splitlines()[0] == ("Error: antibot_paused: browser_launch reached a verification page; "
                                    "the browser is running and stays open on it.")


def test_leaving_the_page_withdraws_only_the_tripwires_own_request(page):
    tab = page(FIELD_URL, overlay=True)
    overlay = presence._handle(tab.browser)
    look(tab, url=FIELD_URL)
    assert overlay.agent["help"]
    tab.url = "https://shopee.co.id/product/1/3"
    assert look(tab, url=tab.url) is None
    assert KEY not in server._HUMAN_HELP and overlay.agent["help"] is None
    note = server._tripwire_result_note(BrowserState.session_key(), "Navigated: x")
    assert note == ("Navigated: x [antibot_tripwire: the tab is no longer on the verification page "
                    "(shopee.co.id /verify/captcha), so the request for help the tripwire made was "
                    "withdrawn.]")
    # One the agent made is never withdrawn.
    asyncio.run(nav.request_human_help("Log in"))
    assert server._HUMAN_HELP[KEY]["by"] == "agent"
    assert look(tab, url=tab.url) is None
    assert server._HUMAN_HELP[KEY]["by"] == "agent" and overlay.agent["help"] == "Log in"
    # And on a verification page with the agent's request pending, it is left as it is.
    tab.url = FIELD_URL
    trip = look(tab, url=FIELD_URL)
    assert trip.where == "pending" and server._HUMAN_HELP[KEY]["objective"] == "Log in"


def test_a_request_answered_with_done_is_said_closed_not_withdrawn(page):
    tab = page(FIELD_URL, overlay=True)
    look(tab, url=FIELD_URL)
    press_done(presence._handle(tab.browser))
    tab.url = "https://shopee.co.id/product/1/3"
    assert look(tab, url=tab.url) is None and KEY not in server._HUMAN_HELP
    note = server._tripwire_result_note(BrowserState.session_key(), "x")
    assert "the person had pressed Done on the tripwire's request, which is now closed" in note
    assert "withdrawn" not in note


def test_an_unread_page_withdraws_nothing(page, monkeypatch):
    tab = page(FIELD_URL, overlay=True)
    look(tab, url=FIELD_URL)

    async def unread(*_a):
        return None

    monkeypatch.setattr(server, "_tripwire_probe", unread)
    assert look(tab) is None and server._HUMAN_HELP[KEY]["by"] == "tripwire"


def test_the_words_and_cloudflare_are_read_only_when_needed(page):
    tab = page("https://example.com/a")
    assert look(tab, url=tab.url) is None and tab.probes == 0  # no host rule, no probe
    tab = page("https://shopee.co.id/product/1", words=["verifikasi untuk melanjutkan"])
    trip = look(tab, url=tab.url)
    assert tab.probes == 1 and "page text 'Verifikasi untuk melanjutkan'" in trip.verdict.trigger
    tab = page("https://example.com/a", cf="its challenge script (_cf_chl_opt)")
    assert look(tab, url=tab.url) is None  # not asked: no look at Cloudflare
    trip = look(tab, url=tab.url, want_cf=True, header="challenge")
    assert trip.verdict.kind == "cloudflare"
    text = server._tripwire_render(trip, "navigate", "x")
    assert ("the person can pass it (wait_for_human), or, where the user is entitled to it, "
            "solve_turnstile_oscursor()") in text


@pytest.mark.parametrize("suffix, looks", [
    ("", False), (" [auto-verify: DOM@(1, 2); challenge gone]", False),
    (" [auto-verify: DOM@(1, 2); challenge still present: CDP clicks did not pass it; ...]", True),
    (" [auto-verify: CF detected but no clickable widget found; ...]", True),
])
def test_navigate_looks_at_cloudflare_only_where_auto_verify_left_a_challenge(page, suffix, looks):
    tab = page("https://example.com/a", cf="its challenge script (_cf_chl_opt)")
    trip = asyncio.run(server._tripwire_after_navigation(tab, {"final_url": tab.url}, verify_suffix=suffix))
    assert (trip is not None) is looks
    if looks:  # auto_verify's own words on what was tried and the tools that fit are kept
        text = server._tripwire_render(trip, "navigate", "x", asked_for=tab.url)
        inner = suffix.strip()[len("[auto-verify: "):-1]
        assert f"\nauto_verify: {inner}\nnext: the person can pass it" in text, text
    header = asyncio.run(server._tripwire_after_navigation(
        tab, {"final_url": tab.url}, doc_state={"cf_mitigated": "challenge"}))
    assert header is not None  # the response said so: looked whatever auto_verify did


def test_off_reads_nothing(page, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "off")
    tab = page(FIELD_URL, overlay=True, cf="x")
    assert look(tab, url=FIELD_URL, want_cf=True) is None and look(tab, want_cf=True) is None
    assert tab.probes == 0 and not server._HUMAN_HELP
    assert server._tripwire_after_action("clicked; navigated to x", {"url": FIELD_URL}) == \
        "clicked; navigated to x"


def test_warn_mode_records_nothing_and_says_it_once(page, monkeypatch):
    monkeypatch.setenv("MCP_STEALTH_ANTIBOT_TRIPWIRE", "warn")
    tab = page(FIELD_URL, overlay=True)
    first = server._tripwire_render(look(tab, url=FIELD_URL), "navigate", "Navigated: x")
    assert first.startswith("antibot_warning: verification page (shopee.co.id built-in rule") and \
        first.endswith("antibot_tripwire=warn, nothing paused.\nNavigated: x")
    second = server._tripwire_render(look(tab, url=FIELD_URL), "reload", "Error: x")
    assert second == ("Error: antibot_warning: still on the verification page reported before "
                      "(shopee.co.id /verify/captcha).\nx")
    assert not server._HUMAN_HELP


def test_a_click_onto_the_page_says_it_first_and_asks_in_pause_mode(page):
    tab = page("https://shopee.co.id/product/1", overlay=True)
    out = server._tripwire_after_action("click '#go': clicked; navigated to x", {"url": FIELD_URL})
    assert out.startswith("antibot_warning: this led to a verification page (shopee.co.id built-in rule")
    assert "the person was asked in the overlay's bar (Done): call wait_for_human" in out
    assert out.endswith("\nclick '#go': clicked; navigated to x")
    assert server._HUMAN_HELP[KEY]["by"] == "tripwire"
    assert server._HUMAN_HELP[KEY]["baseline"] == {"url": FIELD_URL}
    # A click elsewhere, or one that failed, is left as it was.
    assert server._tripwire_after_action("x", {"url": "https://shopee.co.id/p"}) == "x"
    assert server._tripwire_after_action("Error: NOT clicked", {"url": FIELD_URL}) == "Error: NOT clicked"


def test_the_step_check_skips_only_a_first_navigation(page):
    tab = page(FIELD_URL, headless=True)
    assert asyncio.run(server._tripwire_step(None, "navigate")) is None and tab.probes == 0
    said = asyncio.run(server._tripwire_step("navigate", "click"))
    assert said.startswith("Error: antibot_paused: the tab is on a verification page after the step "
                           "before (navigate); the run stopped before its next step.\ntrigger:")
    # Said whole again: the run's report needs what fired, also for a page said before.
    again = asyncio.run(server._tripwire_step(None, "click"))
    assert "\ntrigger: shopee.co.id built-in rule" in again


def test_batch_budget_keeps_resume_with_long_unicode_details():
    text = _pause_text().replace("trigger: ", "trigger: " + "界" * 1200)
    out = batch._paused_batch((100, "navigate", text, True), ["界" * 160] * 101, 2, 105)
    assert len(out.encode()) < 1024 and "actions[100:]" in out
    assert "2 failed" in out and "next:" in out and "previews omitted" in out


def test_spawned_browser_handoff_belongs_to_its_instance(page, monkeypatch):
    from types import SimpleNamespace
    tab = page(FIELD_URL, overlay=True)
    other = _Browser()
    monkeypatch.setattr(BrowserState, 'browser', other)
    monkeypatch.setattr(BrowserState, 'instances', {'work': SimpleNamespace(browser=tab.browser, tabs=[tab])})
    look(tab, url=FIELD_URL, headless=False)
    assert server._HUMAN_HELP[KEY]['instance_id'] == 'work'
    assert not nav._pending_human_request()[2]
    monkeypatch.setattr(BrowserState, 'current_instance_id', 'work')
    monkeypatch.setattr(BrowserState, 'browser', tab.browser)
    monkeypatch.setattr(BrowserState, 'tabs', [])
    out = asyncio.run(nav.wait_for_human(timeout=0, done_url_contains='verify'))
    assert 'unavailable tab' in out and KEY in out and 'no active tab was used' in out


def test_done_is_for_the_displayed_request_not_its_replacement(page, monkeypatch):
    parent = page(FIELD_URL, overlay=True)
    popup = Tab(FIELD_URL, parent.browser)
    popup.target = type('Target', (), {'target_id': 'POPUP'})()
    monkeypatch.setattr(BrowserState, 'tabs', [parent, popup])
    bar = presence._handle(parent.browser)
    look(parent, url=FIELD_URL)
    a = server._HUMAN_HELP[KEY]
    look(popup, url=FIELD_URL)
    b = server._HUMAN_HELP['POPUP']
    assert a['replaced_by'] == b['request_id'] == bar.agent['request_id']
    assert nav._pending_human_request()[0] is popup
    # A click on the old button delivered late, even after the second request's timestamp.
    bar.control_file.write_text(json.dumps({'answered_id': a['request_id'], 'help_done': b['shown_at'] + 2}))
    assert presence.answered(parent.browser, b['shown_at'], b['request_id']) is None
    assert presence._done_pressed(bar) is None
    press_done(bar)
    stale = asyncio.run(nav._wait_for_human_loop(parent, KEY, parent.browser, a, '', 1, None, None))
    assert 'replaced' in stale and bar.agent['help'] and server._HUMAN_HELP['POPUP'] is b
    assert 'pressed Done' in asyncio.run(nav.wait_for_human(timeout=1))
    assert not server._HUMAN_HELP and not bar.agent['help']


@pytest.mark.parametrize('tab_result', [
    'Error: antibot_paused: tab_new reached a verification page',
    'Error: Opened tab [2], now active: the page did not answer within 30s; the tab may still be loading',
])
def test_reuse_preserves_the_new_tabs_outcome(monkeypatch, tab_result):
    async def opened(**kwargs):
        return tab_result
    monkeypatch.setattr(nav, 'tab_new', opened)
    out = asyncio.run(server._reuse_without_an_agent_tab('not ours', FIELD_URL, None, None, None))
    assert out.startswith(tab_result) and 'could not be opened' not in out


def test_fallback_launch_pause_keeps_profile_warning(page):
    from types import SimpleNamespace
    tab = page(FIELD_URL, headless=True)
    req = SimpleNamespace(url=FIELD_URL, headless=True, privacy=False, expect_url=None, expect_status=None)
    plan = SimpleNamespace(used_fallback=True, persistent=True, identity_source='default', dock_note='',
                           profile_info={'session_state_source': 'FALLBACK profile', 'holder': 'other Chrome',
                                         'desired_profile': '/profiles/desired'})
    out = server._launch_result(req, plan, {'final_url': FIELD_URL}, suffix=' [fallback profile: temp]',
                                verify_suffix='', health_suffix='', place_suffix='', restore_suffix='',
                                trip=look(tab, url=FIELD_URL))
    assert out.startswith('Error: antibot_paused:') and 'WARNING: EMPTY FALLBACK PROFILE' in out
    assert 'do not log in again here' in out and '/profiles/desired' in out and 'temp' in out


def test_checkpoint_on_an_unruled_host_reads_once_per_step(page):
    tab = page('https://example.test/', headless=True)
    assert asyncio.run(server._tripwire_after_navigation(tab, {'final_url': tab.url})) is None
    assert tab.probes == 0
    assert asyncio.run(server._tripwire_step('navigate', 'get_text')) is None
    assert tab.probes == 1
    assert asyncio.run(server._tripwire_step('get_text', 'get_url')) is None
    assert tab.probes == 2
def test_wait_refreshes_before_declaring_popup_target_unavailable(page, monkeypatch):
    parent = page("https://example.test/", overlay=True)
    popup = Tab(FIELD_URL, parent.browser)
    popup.target = type("Target", (), {"target_id": "late-popup"})()
    monkeypatch.setattr(BrowserState, "tabs", [parent])
    look(popup, url=FIELD_URL)
    refreshed = []

    async def refresh():
        refreshed.append(True)
        monkeypatch.setattr(BrowserState, "tabs", [parent, popup])
        return True

    monkeypatch.setattr(server, "_refresh_tabs", refresh)
    out = asyncio.run(nav.wait_for_human(timeout=0))
    assert refreshed == [True]
    assert "unavailable tab" not in out


def test_closed_popup_withdraws_only_its_tripwire_request(page, monkeypatch):
    parent = page("https://example.test/", overlay=True)
    popup = Tab(FIELD_URL, parent.browser)
    popup.target = type("Target", (), {"target_id": "closed-popup"})()
    monkeypatch.setattr(BrowserState, "tabs", [parent, popup])
    look(popup, url=FIELD_URL)
    assert "closed-popup" in server._HUMAN_HELP

    async def targets(_browser):
        return [(server._target_key(parent), parent.url)]

    monkeypatch.setattr(server, "_page_targets", targets)
    token = server._TRIPWIRE_RUN.set({"popup": server._Trip(
        "pause", antibot.url_verdict(FIELD_URL, antibot.BUILT_IN), target="closed-popup")})
    try:
        assert asyncio.run(server._tripwire_step("click", "navigate")) is None
    finally:
        server._TRIPWIRE_RUN.reset(token)
    assert "closed-popup" not in server._HUMAN_HELP
    note = server._tripwire_result_note(BrowserState.session_key(), "next")
    assert "no longer on the verification page" in note


def test_closed_popup_keeps_request_when_target_list_is_unreadable(page, monkeypatch):
    parent = page("https://example.test/", overlay=True)
    popup = Tab(FIELD_URL, parent.browser)
    popup.target = type("Target", (), {"target_id": "unread-popup"})()
    monkeypatch.setattr(BrowserState, "tabs", [parent, popup])
    look(popup, url=FIELD_URL)

    async def unread(_browser):
        raise ConnectionError("target list unreadable")

    monkeypatch.setattr(server, "_page_targets", unread)
    token = server._TRIPWIRE_RUN.set({"popup": server._Trip(
        "pause", antibot.url_verdict(FIELD_URL, antibot.BUILT_IN), target="unread-popup")})
    try:
        assert asyncio.run(server._tripwire_step("click", "navigate")) is None
    finally:
        server._TRIPWIRE_RUN.reset(token)
    assert "unread-popup" in server._HUMAN_HELP
def test_tripwire_does_not_replace_explicit_agent_request(page, monkeypatch):
    parent = page("https://example.test/", overlay=True)
    popup = Tab(FIELD_URL, parent.browser)
    popup.target = type("Target", (), {"target_id": "manual-popup"})()
    monkeypatch.setattr(BrowserState, "tabs", [parent, popup])
    asyncio.run(nav.request_human_help("Log in"))
    manual = server._HUMAN_HELP[server._target_key(parent)]
    trip = look(popup, url=FIELD_URL)
    assert trip.where == "agent_pending" and trip.target == "manual-popup"
    assert list(server._HUMAN_HELP.values()) == [manual]
    out = server._tripwire_render(trip, "navigate", "x", asked_for=FIELD_URL)
    assert "explicit request made with request_human_help is still pending" in out
    assert "popup tab manual-popup" in out
