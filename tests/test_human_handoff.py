"""wait_for_human ends on evidence, without a browser: a fake tab whose page follows a script on a
fake clock, so the grace and settle times run at their real values and nothing sleeps.

From real use (a research run on Shopee, about 150 pages): browser_launch on /buyer/login, then
request_human_help, then wait_for_human() answered "human handoff completed after 0.0s: page
changed". The site moves /buyer/login to /buyer/login/qr a moment after it loads, and the baseline
was the page at request_human_help. The request was then dropped, so the next
wait_for_human(done_selector=...) said request_human_help had not been called."""
import asyncio
import json
import subprocess
import sys
import time

import pytest

from mcp_stealth_chrome import presence, server
from mcp_stealth_chrome.state import BrowserState
from mcp_stealth_chrome.tools import diagnostics
from mcp_stealth_chrome.tools import navigation as nav

LOGIN = "https://shopee.co.id/buyer/login"
QR = "https://shopee.co.id/buyer/login/qr"
HOME = "https://shopee.co.id/"
KEY = "HANDOFF-TAB"


class Clock:
    """The handoff's monotonic clock (navigation._clock) and its pause (navigation._pause): a pause
    moves the clock on at once, and a hook runs once the clock passes its time (a person pressing
    Done). It starts far from the wall clock on purpose; a wait that never ends stops loudly."""

    def __init__(self):
        self.start = self.t = 1000.0
        self.hooks: list = []
        self.pauses = 0

    def __call__(self) -> float:
        return self.t

    def elapsed(self) -> float:
        return self.t - self.start

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for hook in [h for h in self.hooks if self.elapsed() >= h[0]]:
            self.hooks.remove(hook)
            hook[1]()

    async def pause(self, seconds: float) -> None:
        self.pauses += 1
        if self.pauses > 20000:
            raise RuntimeError("the wait never ended on the handoff's clock")
        self.advance(seconds)


class _Target:
    target_id = KEY


class Page:
    """A tab whose page is the last step reached on the clock: (from second, url, elements).
    `selector_from` is when the done selector appears, None for never. Once `closed` is set (the
    browser quit or the tab was closed) every read fails as it does on a target that is gone."""

    def __init__(self, clock: Clock, *steps, browser=None):
        self.clock, self.steps, self.browser = clock, steps, browser
        self.target = _Target()
        self.selector_from = None
        self.reads = 0
        self.closed = None  # "browser" or "tab"

    def close(self, what: str) -> None:
        self.closed = what
        if what == "browser":
            self.browser.connection.closed = True

    def sig(self) -> dict:
        self.reads += 1
        if self.closed:
            return {}  # _page_sig's answer for a page it cannot read
        url, nodes = None, None
        for at, u, n in self.steps:
            if self.clock.elapsed() >= at:
                url, nodes = u, n
        return {"url": url, "nodes": nodes, "len": nodes * 37, "hash": f"{url}#{nodes}"}

    async def evaluate(self, _expr, return_by_value=True):
        if self.closed:
            raise ConnectionError("no close frame received or sent")
        hit = self.selector_from is not None and self.clock.elapsed() >= self.selector_from
        return json.dumps({"hit": hit})


class _Connection:
    closed = False


class _Browser:
    """An attached-looking browser (no _process), whose CDP connection is open until closed."""

    def __init__(self):
        self.connection = _Connection()


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(nav, "_clock", clock)
    monkeypatch.setattr(nav, "_pause", clock.pause)
    return clock


@pytest.fixture
def handoff(monkeypatch, clock, tmp_path):
    """Puts a Page in front of request_human_help and wait_for_human; with overlay=True the
    browser has an overlay handle on a process standing in for the overlay."""
    made = []

    def setup(*steps, overlay=False):
        browser = _Browser()
        if overlay:
            folder = tmp_path / f"overlay{len(made)}"
            folder.mkdir()
            handle = presence.Overlay(browser, 4242, folder, subprocess.Popen(SLEEPER), False)
            setattr(browser, presence.ATTR, handle)
            made.append(handle)
        page = Page(clock, *steps, browser=browser)
        monkeypatch.setattr(BrowserState, "browser", browser)
        monkeypatch.setattr(BrowserState, "current_headless", False)
        monkeypatch.setattr(BrowserState, "active_tab", classmethod(lambda cls: page))
        return page

    async def page_sig(tab, *_a, **_k):
        return tab.sig()

    async def in_front(_tab):
        return ["tab activated"]

    async def url_of(tab, *_a, **_k):
        return tab.sig().get("url") or ""

    async def page_targets(browser):
        page = BrowserState.active_tab()
        return [] if page.closed else [(KEY, page.sig().get("url") or "")]

    monkeypatch.setattr(server, "_HUMAN_HELP", {})
    monkeypatch.setattr(server, "_page_sig", page_sig)
    monkeypatch.setattr(nav, "_foreground_for_human", in_front)
    monkeypatch.setattr(nav, "get_url", url_of)
    monkeypatch.setattr(server, "_page_targets", page_targets)
    yield setup
    for handle in made:
        handle.proc.kill()
        handle.proc.wait()


def ask(objective="Log in", **kw):
    return asyncio.run(nav.request_human_help(objective, **kw))


def wait(**kw):
    return asyncio.run(nav.wait_for_human(**kw))


def _write_control(overlay: presence.Overlay, data: dict) -> None:
    """control.json as the overlay writes it: a new file, then replaced."""
    tmp = overlay.control_file.with_name("control.json.test")
    tmp.write_text(json.dumps(data))
    tmp.replace(overlay.control_file)


def press_done(overlay: presence.Overlay) -> None:
    """control.json as the overlay writes it when the person presses Done (a new file, replaced)."""
    asked = float(overlay.agent.get("asked_at") or 0)
    tmp = overlay.control_file.with_name("control.json.test")
    tmp.write_text(json.dumps({"holder": "agent", "reason": "help done", "help_done": asked + 1}))
    tmp.replace(overlay.control_file)


def test_shopee_a_redirect_inside_the_grace_is_no_handoff(handoff, clock):
    """The page moves to /qr 0.4 s after the request and then nothing happens: no completion, the
    request is still pending afterwards, and a later wait with a selector counts as the handoff."""
    page = handoff((0, LOGIN, 400), (0.4, QR, 420))
    asked = ask()
    assert "weak evidence" in asked and f"{nav.HANDOFF_GRACE:g}s after this request" in asked, asked
    requested_at = server._HUMAN_HELP[KEY]["requested_clock"]
    clock.advance(0.1)  # the agent's next call
    result = wait(timeout=12)
    assert result.startswith("Error: timeout after 12"), result
    assert "completed" not in result and "no page change held" in result, result
    assert "The request for help stays pending" in result, result
    assert server._HUMAN_HELP[KEY]["requested_clock"] == requested_at  # the same request
    page.selector_from = clock.elapsed() + 1.0
    result = wait(done_selector="#account", timeout=5)
    assert result.startswith("human handoff completed after"), result
    assert "selector '#account' appeared" in result and "no handoff was pending" not in result
    assert KEY not in server._HUMAN_HELP  # strong evidence closes the request


def test_with_the_overlay_a_page_change_never_ends_the_wait_and_done_does(handoff, clock):
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    asked = ask()
    assert "A page change alone does not end it" in asked, asked
    reads = page.reads  # request_human_help's baseline
    result = wait(timeout=15)  # the page changed at 6 s and held, which counts without an overlay
    assert result.startswith("Error: timeout after 15"), result
    assert "did not press Done" in result and "stays pending" in result, result
    assert page.reads == reads  # the wait did not even read the page: the bar answers
    # The next wait shows the request again, and a Done pressed on it ends that wait.
    clock.hooks.append((clock.elapsed() + 1.0, lambda: press_done(overlay)))
    result = wait(timeout=15)
    assert result.startswith("human handoff completed after 1.0s: the person pressed Done"), result
    assert KEY not in server._HUMAN_HELP and overlay.agent["help"] is None


def test_a_timeout_takes_the_request_off_the_bar_and_the_next_wait_shows_it_again(
        handoff, clock, monkeypatch):
    """After a timeout the person must not read "Your help is needed" while the agent does
    something else; the request stays a handoff for the agent, and its next wait shows it again.
    A Done counts from that new showing on (the wall clock is fake here, so stamps are exact)."""
    wall = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    page = handoff((0, LOGIN, 400), overlay=True)
    overlay = presence._handle(page.browser)
    ask()
    first = server._HUMAN_HELP[KEY]["asked_at"]
    assert overlay.agent["help"] == "Log in"
    result = wait(timeout=10)
    assert result.startswith("Error: timeout after 10"), result
    assert "The bar no longer shows the request" in result, result
    assert "wait_for_human shows it in the bar again" in result, result
    assert overlay.agent["help"] is None                      # the bar stopped asking
    assert server._HUMAN_HELP[KEY]["shown_at"] is None        # the agent still has the request
    wall[0] += 100
    # A Done stamped before the new showing (the bar did not show the request then) is no answer.
    _write_control(overlay, {"holder": "agent", "reason": "help done", "help_done": first + 50})
    clock.hooks.append((clock.elapsed() + 2.0, lambda: press_done(overlay)))
    record = server._HUMAN_HELP[KEY]
    result = wait(timeout=10)
    assert result.startswith("human handoff completed after 2.0s: the person pressed Done"), result
    assert "no handoff was pending" not in result
    assert record["asked_at"] == first and record["shown_at"] == first + 100  # first kept, shown anew
    assert overlay.agent["asked_at"] == first + 100


def test_with_the_overlay_done_ends_a_wait_the_page_changed_in(handoff, clock):
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    ask()
    clock.hooks.append((10.0, lambda: press_done(overlay)))
    result = wait(timeout=30)
    assert result.startswith("human handoff completed after"), result
    assert "pressed Done" in result and clock.elapsed() >= 10.0, result


def _kill(overlay: presence.Overlay) -> None:
    overlay.proc.kill()
    overlay.proc.wait()


def test_the_overlay_dies_mid_wait_and_comes_back_so_the_page_still_does_not_count(
        handoff, clock, monkeypatch):
    """Its process exits at 3 s and is started again (the new one reads agent.json, request
    included): the page that changed at 6 s and held is no answer, the Done at 12 s is."""
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    monkeypatch.setattr(presence, "_same_chrome", lambda pid, start: True)
    monkeypatch.setattr(presence, "_spawn", lambda *a, **k: subprocess.Popen(SLEEPER))
    ask()
    clock.hooks.append((3.0, lambda: _kill(overlay)))
    clock.hooks.append((12.0, lambda: press_done(overlay)))
    result = wait(timeout=30)
    assert result.startswith("human handoff completed after"), result
    assert "pressed Done" in result and clock.elapsed() >= 12.0, result
    assert overlay.restarts == 1 and overlay.alive()


def test_the_overlay_dies_mid_wait_and_cannot_come_back_so_a_held_change_is_weak(handoff, clock):
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    overlay.restarts = presence.RESTARTS  # no restart left
    ask()
    clock.hooks.append((3.0, lambda: _kill(overlay)))
    result = wait(timeout=30)
    assert result.startswith("page changed after"), result
    assert "weak evidence" in result and "completed" not in result, result
    assert "the overlay's bar showed this request but ended during the wait" in result, result
    assert "its process exited" in result and "no Done button" in result, result
    assert KEY in server._HUMAN_HELP


@pytest.mark.parametrize("what,overlay,signal", [
    ("browser", False, {}),
    ("tab", False, {}),
    ("browser", True, {}),                       # the overlay ends with its browser
    ("browser", False, {"done_selector": "#account"}),
    ("tab", False, {"done_url_contains": "/account"}),
])
def test_a_browser_or_tab_gone_mid_wait_ends_it_as_an_error(handoff, clock, monkeypatch,
                                                            what, overlay, signal):
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=overlay)
    if overlay:
        handle = presence._handle(page.browser)
        monkeypatch.setattr(presence, "_same_chrome", lambda pid, start: False)  # Chrome is gone
        clock.hooks.append((7.0, lambda: _kill(handle)))
    ask()
    clock.hooks.append((7.0, lambda: page.close(what)))
    result = wait(timeout=30, **signal)
    assert result.startswith("Error: wait_for_human stopped after"), result
    assert f"the {what} is gone" in result and "nothing was completed" in result, result
    assert "timeout" not in result and clock.elapsed() < 9.0
    assert KEY not in server._HUMAN_HELP


@pytest.mark.parametrize("asked", [True, False])
def test_take_over_then_resume_through_the_wait(handoff, clock, asked):
    """The person takes over at 3 s and changes the page at 6 s (it held, past the grace): no
    answer while they hold it. Resume agent at 10 s is."""
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    if asked:
        ask()
    clock.hooks.append((3.0, lambda: _write_control(overlay, {
        "holder": "person", "reason": "button", "since": time.time()})))
    clock.hooks.append((10.0, lambda: _write_control(overlay, {
        "holder": "agent", "reason": "button", "since": time.time() + 1})))
    result = wait(timeout=30)
    assert result.startswith("human handoff completed after"), result
    assert "pressed Resume agent" in result and clock.elapsed() >= 10.0, result
    assert KEY not in server._HUMAN_HELP


def _revivable(monkeypatch) -> list:
    """Overlay processes that can be started again: _spawn gives a stand-in, Chrome is the same."""
    spawned: list = []

    def spawn(*_a, **_k):
        spawned.append(subprocess.Popen(SLEEPER))
        return spawned[-1]
    monkeypatch.setattr(presence, "_same_chrome", lambda pid, start: True)
    monkeypatch.setattr(presence, "_spawn", spawn)
    return spawned


def test_the_overlay_is_started_again_while_the_person_holds_the_browser(handoff, clock, monkeypatch):
    """Take over at 3 s, the overlay's process exits at 4 s: the wait starts it again (Resume agent
    is on its bar), and Resume agent at 10 s ends the wait."""
    page = handoff((0, LOGIN, 400), overlay=True)
    overlay = presence._handle(page.browser)
    _revivable(monkeypatch)
    ask()
    clock.hooks.append((3.0, lambda: _write_control(overlay, {
        "holder": "person", "reason": "button", "since": time.time()})))
    clock.hooks.append((4.0, lambda: _kill(overlay)))
    during_hold: list = []
    clock.hooks.append((6.0, lambda: during_hold.append((overlay.restarts, overlay.alive()))))
    clock.hooks.append((10.0, lambda: _write_control(overlay, {
        "holder": "agent", "reason": "button", "since": time.time() + 1})))
    result = wait(timeout=30)
    assert result.startswith("human handoff completed after"), result
    assert "pressed Resume agent" in result, result
    assert during_hold == [(1, True)]  # started again while they held it, so they had Resume


def test_a_hold_whose_overlay_cannot_come_back_ends_the_wait_as_an_error(handoff, clock):
    page = handoff((0, LOGIN, 400), overlay=True)
    overlay = presence._handle(page.browser)
    overlay.restarts = presence.RESTARTS  # no restart left
    ask()
    clock.hooks.append((3.0, lambda: _write_control(overlay, {
        "holder": "person", "reason": "button", "since": time.time()})))
    clock.hooks.append((4.0, lambda: _kill(overlay)))
    result = wait(timeout=30)
    assert result.startswith("Error: wait_for_human stopped after 4.0"), result
    assert "Resume agent is no longer available" in result and "overlay ended" in result, result
    assert "agent_overlay=False" in result and "stays pending" in result, result
    assert KEY in server._HUMAN_HELP


def test_the_overlay_is_started_again_on_a_selector_wait(handoff, clock, monkeypatch):
    page = handoff((0, LOGIN, 400), overlay=True)
    overlay = presence._handle(page.browser)
    _revivable(monkeypatch)
    ask()
    page.selector_from = 8.0
    clock.hooks.append((3.0, lambda: _kill(overlay)))
    result = wait(done_selector="#account", timeout=30)
    assert result.startswith("human handoff completed after"), result
    assert overlay.restarts == 1 and overlay.alive()


@pytest.mark.parametrize("ends", [True, False])
def test_a_selector_wait_that_times_out_says_whether_the_bar_ended(handoff, clock, ends):
    page = handoff((0, LOGIN, 400), overlay=True)
    overlay = presence._handle(page.browser)
    ask()
    if ends:
        overlay.restarts = presence.RESTARTS
        clock.hooks.append((3.0, lambda: _kill(overlay)))
    result = wait(done_selector="#account", timeout=10)
    assert result.startswith("Error: timeout after 10"), result
    assert "selector '#account' never appeared" in result, result
    if ends:
        assert "the overlay's bar showed this request but ended during the wait" in result, result
    else:
        assert "the person did not press Done in the overlay's bar" in result, result


def test_a_revive_whose_start_fails_is_bounded_and_backs_off(handoff, clock, monkeypatch):
    """_spawn raises every time. Each attempt counts, failed ones too, and none comes within
    REVIVE_BACKOFF of the last; once RESTARTS are spent the bar has ended and stays ended, and only
    then may the page change (at 6 s, held) count, as weak evidence that names the ended bar."""
    page = handoff((0, LOGIN, 400), (6, HOME, 900), overlay=True)
    overlay = presence._handle(page.browser)
    tried: list = []

    def spawn(*_a, **_k):
        tried.append(clock.elapsed())
        raise OSError("no PyObjC here")
    monkeypatch.setattr(presence, "_same_chrome", lambda pid, start: True)
    monkeypatch.setattr(presence, "_spawn", spawn)
    monkeypatch.setattr(presence, "_monotonic", clock)
    ask()
    clock.hooks.append((3.0, lambda: _kill(overlay)))
    result = wait(timeout=60)
    assert len(tried) == presence.RESTARTS, tried
    assert all(b - a >= presence.REVIVE_BACKOFF for a, b in zip(tried, tried[1:])), tried
    assert result.startswith("page changed after"), result
    assert "the overlay's bar showed this request but ended during the wait" in result, result
    assert clock.elapsed() >= tried[-1] + nav.HANDOFF_SETTLE  # no verdict while it could come back
    assert overlay.recover() == "ended"
    result = wait(timeout=5)  # the same handoff: no new attempt
    assert len(tried) == presence.RESTARTS and result.startswith("Error: timeout after 5"), result


def test_without_an_overlay_a_change_that_does_not_hold_does_not_count(handoff, clock):
    """After the grace the page changes every second, as a carousel or a redirect chain would."""
    steps = [(0, LOGIN, 400)] + [(6 + i, f"{HOME}?step={i}", 500 + i) for i in range(20)]
    handoff(*steps)
    ask()
    result = wait(timeout=14)
    assert result.startswith("Error: timeout after 14"), result


def test_without_an_overlay_a_change_that_holds_counts_after_the_grace(handoff, clock):
    """A move inside the grace is the page's own (the baseline follows it); a move after it that
    holds HANDOFF_SETTLE ends the wait as weak evidence, measured from the page at the grace's end."""
    handoff((0, LOGIN, 400), (1, QR, 420), (7, HOME, 900))
    ask()
    result = wait(timeout=30)
    assert result.startswith("page changed after"), result
    assert f"the URL went from {QR!r} to {HOME!r}" in result, result
    assert f", and it held {nav.HANDOFF_SETTLE:g}s" in result and "weak evidence" in result
    assert "the content did not" not in result  # the whole page held
    assert "can be the page itself" in result and "completed" not in result
    assert clock.elapsed() >= 7 + nav.HANDOFF_SETTLE
    # The request stays, and the next wait measures from the page as reported.
    record = server._HUMAN_HELP[KEY]
    assert record["baseline"]["url"] == HOME
    result = wait(timeout=10)
    assert result.startswith("Error: timeout after 10"), result  # the same change is not news
    result = wait(done_url_contains="shopee.co.id/", timeout=5)
    assert result.startswith("human handoff completed after 0.0s: URL contains"), result
    assert KEY not in server._HUMAN_HELP


def test_a_new_url_counts_once_it_held_though_its_content_still_moves(handoff, clock):
    """A page the person reached keeps drawing (a feed, a clock): its URL holding is what counts."""
    steps = [(0, LOGIN, 400)] + [(7 + i / 2, HOME, 900 + i) for i in range(40)]
    handoff(*steps)
    ask()
    result = wait(timeout=30)
    assert result.startswith("page changed after"), result
    assert f"the URL went from {LOGIN!r} to {HOME!r}" in result, result
    assert "the URL held 2s but the content did not (it kept changing)" in result, result
    assert "and it held" not in result
    assert clock.elapsed() < 7 + nav.HANDOFF_SETTLE + 1


@pytest.mark.parametrize("jump", [3600.0, -3600.0])
def test_a_wall_clock_jump_moves_neither_the_grace_nor_the_timeout(handoff, clock, monkeypatch, jump):
    """The system time changes 0.2 s into the wait. On the wall clock a jump forward ended the
    grace and the timeout at once (the redirect to /qr then counted); one back held both an hour."""
    real = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + offset[0])
    handoff((0, LOGIN, 400), (0.4, QR, 420))
    ask()
    clock.hooks.append((0.2, lambda: offset.__setitem__(0, jump)))
    result = wait(timeout=12)
    assert result.startswith("Error: timeout after 12"), result
    assert "no page change held" in result, result
    assert 12 <= clock.elapsed() < 12.5


def test_a_content_change_names_what_was_measured(handoff, clock):
    handoff((0, LOGIN, 400), (8, LOGIN, 460))
    ask()
    result = wait(timeout=30)
    assert f"the page's content changed at {LOGIN!r} (elements 400 to 460" in result, result


def test_a_change_found_at_a_late_waits_first_read_says_when_is_not_known(handoff, clock):
    """The agent waited 30 s after the request: the page differed already, maybe by itself."""
    handoff((0, LOGIN, 400), (0.4, QR, 420))
    ask()
    clock.advance(30)
    result = wait(timeout=10)
    assert result.startswith("page changed after"), result
    assert "already differed at this wait's first read" in result, result


def test_without_a_request_the_grace_runs_from_the_call(handoff, clock):
    handoff((0, LOGIN, 400), (0.5, QR, 420))
    result = wait(timeout=10)
    assert result.startswith("Error: timeout after 10"), result
    assert "within 5s of this call" in result and "no handoff was pending" in result, result
    handoff((0, LOGIN, 400), (clock.elapsed() + 6, HOME, 900))
    result = wait(timeout=10)
    assert result.startswith("page changed after"), result
    assert "no handoff was pending on this tab" in result and "stays pending" not in result


def test_a_new_request_replaces_the_pending_one(handoff, clock):
    handoff((0, LOGIN, 400), (7, HOME, 900))
    ask("Log in")
    assert wait(timeout=30).startswith("page changed after")
    first = server._HUMAN_HELP[KEY]
    clock.advance(1)
    ask("Confirm the code")
    second = server._HUMAN_HELP[KEY]
    assert second is not first and second["objective"] == "Confirm the code"
    assert second["requested_clock"] > first["requested_clock"] and second["baseline"]["url"] == HOME


def test_the_docstring_gives_the_constants_it_runs_on():
    doc = nav.wait_for_human.__doc__
    assert f"{nav.HANDOFF_GRACE:g}s after request_human_help" in doc
    assert f"held {nav.HANDOFF_SETTLE:g}s" in doc
    assert "even with overlay_bar off" in doc


def test_the_bar_shows_a_request_only_while_its_overlay_runs_and_holds_it(tmp_path):
    folder = tmp_path / "overlay"
    folder.mkdir()
    browser = _Browser()
    overlay = presence.Overlay(browser, 4242, folder, subprocess.Popen(SLEEPER), False)
    setattr(browser, presence.ATTR, overlay)
    try:
        asked = presence.ask(browser, "Log in")
        assert presence.shows_request(browser, asked)
        assert not presence.shows_request(browser, None)          # ask found no overlay
        assert not presence.shows_request(browser, asked - 1)     # a later request replaced it
        overlay.proc.kill()
        overlay.proc.wait()
        assert not presence.shows_request(browser, asked)         # nobody can press Done
    finally:
        if overlay.proc.poll() is None:
            overlay.proc.kill()
    assert not presence.shows_request(_Browser(), asked)


class _Config:
    def __init__(self, host, port):
        self.host, self.port = host, port


class _CdpBrowser:
    def __init__(self, host, port):
        self.config = _Config(host, port)
        self.websocket_url = f"ws://{host}:{port}/devtools/browser/0f1e2d3c"


def _status() -> dict:
    return json.loads(asyncio.run(server.mcp._tool_manager._tools["server_status"].fn()))


def test_server_status_names_the_cdp_endpoint(monkeypatch):
    monkeypatch.setattr(BrowserState, "browser", None)
    assert _status()["cdp"] is None
    monkeypatch.setattr(BrowserState, "browser", _CdpBrowser("127.0.0.1", 9333))
    assert _status()["cdp"] == {"host": "127.0.0.1", "port": 9333,
                                "browser_ws": "ws://127.0.0.1:9333/devtools/browser/0f1e2d3c"}


@pytest.mark.parametrize("host,local", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True), ("127.0.0.2", True),
    ("10.0.0.5", False), ("chrome.internal", False), ("0.0.0.0", False),
])
def test_a_cdp_host_that_is_not_loopback_is_said(host, local):
    endpoint = diagnostics._cdp_endpoint(_CdpBrowser(host, 9222))
    assert endpoint["host"] == host and endpoint["port"] == 9222
    assert ("note" not in endpoint) is local, endpoint
    if not local:
        assert "not a loopback address" in endpoint["note"]


def test_a_browser_not_connected_yet_has_no_websocket_url():
    class Unconnected:
        config = _Config("127.0.0.1", 9333)

        @property
        def websocket_url(self):
            raise AttributeError("'NoneType' object has no attribute 'webSocketDebuggerUrl'")

    assert diagnostics._cdp_endpoint(Unconnected()) == {
        "host": "127.0.0.1", "port": 9333, "browser_ws": None}
