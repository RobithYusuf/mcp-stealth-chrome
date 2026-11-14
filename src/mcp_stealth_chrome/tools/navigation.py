"""History (go_back, go_forward, reload), tabs, and handing the browser to a person.

Moved out of server.py. navigate and the navigation helpers stay there because launch
paths and other tools share them; code here reaches them as _srv.<name> at call time,
so a test that patches server.<name> still reaches this code.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from nodriver import Tab

from .. import server as _srv
from .._app import mcp
from ..helpers import err, get_url, ok
from ..state import BrowserState


# ══════════════════════════════════════════════════════════════════════════
# 2. NAVIGATION
# ══════════════════════════════════════════════════════════════════════════

# ── Navigation postconditions ─────────────────────────────────────────────
# Reporting the URL a caller ASKED for is the same class of lie as a click that
# reports "clicked" without hit-testing: browser_launch said it had loaded
# /admin while the tab had already been redirected to /, and every read after
# that described the wrong page with no way to tell. So every navigation entry
# point reports what was OBSERVED afterwards — final_url, the redirect chain,
# the document status, readyState — and marks a field `unmeasured` rather than
# guessing when it could not see it. An unmeasured status is never treated as a
# passing expect_status.


@mcp.tool()
async def tab_focus() -> str:
    """Bring the active tab's browser window to the OS foreground.

    Programmatic CDP navigation does not raise Chrome to the front, so
    on a desktop where the user has their own Chrome already open you
    may see only the original window even though MCP successfully
    drove a different window/tab. Call this when you want to *see*
    what MCP is doing.

    Common reasons MCP's window is hidden:
      - Per-PID profile fallback: another Chrome already held
        ~/.mcp-stealth/profile/, so MCP launched into
        ~/.mcp-stealth/profile-pid<N>/ — that's a SEPARATE Chrome window.
        Run server_status to confirm (profile_dir field).
      - OAuth popup opened a new tab/window MCP now drives.
      - macOS Spaces / minimized window / behind other apps.
    """
    try:
        tab = BrowserState.active_tab()
        focused_actions = []
        try:
            await tab.activate()
            focused_actions.append("activate")
        except Exception as e:
            focused_actions.append(f"activate_failed:{type(e).__name__}")
        try:
            await tab.bring_to_front()
            focused_actions.append("bring_to_front")
        except Exception as e:
            focused_actions.append(f"front_failed:{type(e).__name__}")
        if not any(
            action in {"activate", "bring_to_front"}
            for action in focused_actions
        ):
            return err(
                "could not confirm that the browser was focused; "
                f"actions: {', '.join(focused_actions)}"
            )
        url, title = await _srv._page_facts_for_report(tab)
        profile = (
            BrowserState.current_profile_dir.name
            if BrowserState.current_profile_dir else "(default)"
        )
        hint = ""
        if profile.startswith("profile-pid"):
            hint = (
                "\nNOTE: MCP is using a per-PID profile fallback "
                f"({profile}) — this is a SEPARATE Chrome window from your "
                "personal Chrome. Look for the new window in Mission "
                "Control / Alt-Tab."
            )
        return ok(
            f"focused: {title!r} @ {url}\n"
            f"actions: {', '.join(focused_actions)}\n"
            f"profile: {profile}{hint}"
        )
    except Exception as e:
        return err(str(e))


async def _foreground_for_human(tab: Tab) -> list[str]:
    """Best-effort CDP + OS foregrounding for a local headed browser."""
    actions: list[str] = []
    try:
        await tab.activate()
        actions.append("tab activated")
    except Exception:
        pass
    try:
        await tab.bring_to_front()
        actions.append("window brought to front")
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            executable = Path(str(BrowserState.browser.config.browser_executable_path))
            app = next((p for p in executable.parents if p.suffix == ".app"), None)
            if app is not None:
                completed = subprocess.run(
                    ["open", "-a", str(app)],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
                if completed.returncode == 0:
                    actions.append("macOS app activated")
        except Exception:
            pass
    return actions


@mcp.tool()
async def request_human_help(objective: str, timeout: float = 300) -> str:
    """Hand a headed local browser to the human without blocking the MCP call.

    The caller should immediately follow this with wait_for_human, optionally
    naming a completion URL fragment or selector.
    """
    try:
        tab = BrowserState.active_tab()
        if BrowserState.current_headless:
            return err(
                "human help unavailable: this browser is headless, so there is no "
                "window a person can operate. Close it and relaunch with headless=False, "
                "then call request_human_help again."
            )
        actions = await _foreground_for_human(tab)
        if not actions:
            return err(
                "human help unavailable: no foreground action succeeded, so the "
                "browser window could not be confirmed visible"
            )
        key = _srv._target_key(tab)
        _srv._HUMAN_HELP[key] = {
            "objective": objective,
            "baseline": await _srv._page_sig(tab),
            "requested_at": time.time(),
            "timeout": max(0.0, float(timeout)),
        }
        return ok(
            f"Browser is visible and in the foreground ({', '.join(actions) or 'focus requested'}).\n"
            f"Human: {objective}\n"
            "When finished, leave the page in its completed state. The agent must call "
            "wait_for_human(done_url_contains=... or done_selector=...); without a "
            "specific signal it will wait for a URL/DOM change from this handoff."
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def wait_for_human(
    timeout: Optional[float] = None,
    done_url_contains: Optional[str] = None,
    done_selector: Optional[str] = None,
) -> str:
    """Wait for objective evidence that a local human handoff completed.

    Args:
        timeout: seconds to wait. Defaults to the timeout given to
            request_human_help, or 300s when there is no pending handoff.
        done_url_contains: finish as soon as the URL contains this substring
        done_selector: finish as soon as this CSS selector appears

    With neither signal, any URL/DOM change since the handoff counts as done.
    A timeout is reported as an error — never as a silent success.
    """
    try:
        tab = BrowserState.active_tab()
        key = _srv._target_key(tab)
        request = _srv._HUMAN_HELP.get(key, {})
        # None means "inherit". Keying off the literal 300 would silently
        # override a caller who genuinely asked for 300 seconds.
        if timeout is None:
            timeout = (
                float(request["timeout"])
                if "timeout" in request
                else 300.0
            )
        baseline = request.get("baseline") or await _srv._page_sig(tab)
        loop = asyncio.get_event_loop()
        started = loop.time()
        selector_expr = (
            "(() => JSON.stringify({hit: !!document.querySelector(%s)}))()"
            % json.dumps(done_selector)
            if done_selector else None
        )
        while loop.time() - started < max(0.0, timeout):
            elapsed = round(loop.time() - started, 2)
            if done_url_contains:
                current_url = await get_url(tab)
                if done_url_contains in current_url:
                    _srv._HUMAN_HELP.pop(key, None)
                    return ok(
                        f"human handoff completed after {elapsed}s: URL contains "
                        f"{done_url_contains!r}"
                    )
            if selector_expr:
                value = await tab.evaluate(selector_expr, return_by_value=True)
                value = value.value if hasattr(value, "value") and not isinstance(value, str) else value
                if value and bool(json.loads(value).get("hit")):
                    _srv._HUMAN_HELP.pop(key, None)
                    return ok(
                        f"human handoff completed after {elapsed}s: selector "
                        f"{done_selector!r} appeared"
                    )
            if not done_url_contains and not done_selector:
                current = await _srv._page_sig(tab)
                if baseline and current and (
                    current.get("url") != baseline.get("url")
                    or current.get("nodes") != baseline.get("nodes")
                    or current.get("len") != baseline.get("len")
                    or current.get("hash") != baseline.get("hash")
                ):
                    _srv._HUMAN_HELP.pop(key, None)
                    return ok(f"human handoff completed after {elapsed}s: page changed")
            await asyncio.sleep(0.25)
        # Drop the pending handoff so a later wait re-baselines against the page
        # as it is now, instead of measuring against a stale pre-handoff snapshot.
        _srv._HUMAN_HELP.pop(key, None)
        return err(
            f"timeout after {timeout}s waiting for human completion — nothing changed "
            "and no completion signal was observed. Call request_human_help again "
            "if the person still needs to act."
        )
    except Exception as e:
        return err(str(e))


async def _history_navigation(
    action: str,
    move,
    requested_url: Optional[str],
    expect_url: Optional[str],
    expect_status: Optional[int],
) -> str:
    """Shared body for go_back / go_forward / reload.

    History moves are navigations like any other and get the same observed
    postconditions. A back/forward served from the back-forward cache emits no
    document request at all, so its status comes back explicitly unmeasured
    rather than silently absent.
    """
    if not BrowserState.is_up():
        return err("Browser not running. Call browser_launch first.")
    tab = BrowserState.active_tab()
    doc_state, disarm = await _srv._arm_document_watch(tab)
    try:
        await move(tab)
        try:
            await _srv._wait_load_complete(tab, 10)
        except Exception:
            pass  # settle is best-effort; the postcondition probe is the truth
        observed = await _srv._observe_navigation(tab, requested_url, disarm())
        return _srv._nav_result(
            action, observed, expect_url=expect_url, expect_status=expect_status,
        )
    except Exception as e:
        return err(str(e))
    finally:
        disarm()


@mcp.tool()
async def go_back(
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Go back in history. Reports final_url / redirect_chain / status /
    ready_state observed afterwards; requested_url is null because history
    decides the destination. See navigate() for the expect_* rules."""
    return await _history_navigation(
        "Went back", lambda tab: tab.back(), None, expect_url, expect_status,
    )


@mcp.tool()
async def go_forward(
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Go forward in history. Reports the observed postconditions; see
    navigate() for the expect_* rules."""
    return await _history_navigation(
        "Went forward", lambda tab: tab.forward(), None, expect_url, expect_status,
    )


@mcp.tool()
async def reload(
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
) -> str:
    """Reload the active tab. Reports the observed postconditions — a reload
    that lands on a login redirect is otherwise indistinguishable from one that
    reloaded the page you were on. See navigate() for the expect_* rules."""
    if not BrowserState.is_up():
        return err("Browser not running. Call browser_launch first.")
    try:
        before = await get_url(BrowserState.active_tab())
    except Exception:
        before = ""
    return await _history_navigation(
        "Reloaded", lambda tab: tab.reload(), before or None, expect_url, expect_status,
    )


# ══════════════════════════════════════════════════════════════════════════
# 7. TABS
# ══════════════════════════════════════════════════════════════════════════


# How long tab_list waits for one tab's page before reading Chrome's target list instead.
_TAB_READ_TIMEOUT = 2.0


def _unanswered_reason(tab) -> str:
    """Why a tab's page did not answer tab_list, as far as the server knows."""
    found = _srv._OPEN_DIALOGS.get(_srv._target_key(tab))
    if found:
        return (f"page did not answer: a native {found['type']} dialog is open there "
                f"({found['message'][:60]!r}); tab_select it, then dialog_handle")
    return (f"page did not answer in {_TAB_READ_TIMEOUT:g} s: a native dialog is probably open "
            f"there (this server only watches tabs its tools have used, so it cannot answer "
            f"one in a tab it never touched; the person can, in the window), or the page is busy")


@mcp.tool()
async def tab_list() -> str:
    """List all open tabs with index, URL, title."""
    try:
        problem = await _srv._tab_list_problem()
        if problem:
            return err(problem)
        BrowserState.own_untracked()
        active = BrowserState.active_tab_index
        lines = [f"Active tab: {active}" if active >= 0 else
                 f"Active tab: none. {BrowserState.active_lost or 'tab_select picks one.'}"]
        for i, t in enumerate(BrowserState.tabs):
            # One combined evaluate per tab (was two: get_url + get_title). A native dialog in
            # a tab (often one a person opened, on a tab no tool has touched) freezes its page,
            # and an unbounded evaluate there hung tab_list for ever. Bounded, the row falls back
            # to what Chrome's target list says, which needs no renderer.
            note = ""
            try:
                r = await asyncio.wait_for(t.evaluate(
                    "JSON.stringify([window.location.href, document.title])",
                    return_by_value=True,
                ), timeout=_TAB_READ_TIMEOUT)
                r = r.value if hasattr(r, "value") and not isinstance(r, str) else r
                url, title = json.loads(r)
                url, title = (url or ""), (title or "")
            except asyncio.TimeoutError:
                info = getattr(t, "target", None)
                url, title = str(getattr(info, "url", "") or ""), str(getattr(info, "title", "") or "")
                note = f"  [{_unanswered_reason(t)}; URL and title from Chrome's target list]"
            except Exception:
                url, title = "", ""
            if BrowserState.opened_outside(t):
                key = _srv._target_key(t)
                note += ("  [not opened by this server; selected with tab_select]"
                         if key in BrowserState.adopted_targets else "  [not opened by this server]")
            marker = "*" if i == active else " "
            lines.append(f"{marker}[{i}] {title[:40]} | {url}{note}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


def _tab_new_mode(headless: bool, new_window: Optional[bool]) -> str:
    """'window' (its own window), 'tab' (a tab, headless) or 'behind' (a tab behind, headed).

    Headed defaults to its own window: a tab brought to the front activates Chrome, and a
    tab left behind reads as hidden unless _keep_painting_behind succeeds.
    """
    if new_window is None:
        new_window = not headless
    if new_window:
        return "window"
    return "tab" if headless else "behind"


async def _behind_note(browser, new_tab, before_tab, emulation_error: Optional[str]) -> str:
    """Where a tab opened behind another one ended up, and what its page reads."""
    new_window = await _srv._window_of(browser, new_tab)
    old_window = await _srv._window_of(browser, before_tab) if before_tab is not None else None
    if new_window is not None and old_window is not None and new_window != old_window:
        place = "in another existing window, behind its current tab (Chrome picks the window)"
    elif new_window is not None and old_window is not None:
        place = "in the same window, behind the tab it shows"
    else:
        place = "behind the tab its window shows (Chrome would not say which window)"
    state = await _srv._page_visibility(new_tab)
    if state == "visible":
        return (f"{place}; the page reads as visible and has focus as far as it can tell "
                f"(focus emulation), while the window keeps showing the other tab")
    why = f" (focus emulation failed: {emulation_error})" if emulation_error else ""
    if state == "hidden":
        return (f"{place}; the page reads as hidden until it is shown{why}: tab_select with "
                f"focus=True or tab_focus shows it, and brings Chrome to the front")
    return f"{place}; its visibility could not be read{why}, so it may be hidden"


@mcp.tool()
async def tab_new(
    url: str = "about:blank",
    expect_url: Optional[str] = None,
    expect_status: Optional[int] = None,
    new_window: Optional[bool] = None,
) -> str:
    """Open a new tab and make it the active one for every later tool.

    Reports the same observed postconditions as navigate() — a new tab that
    redirects is otherwise reported as sitting on the URL you asked for.
    See navigate() for the expect_* rules.

    new_window: where it opens. Left unset: headed, its own window behind the app the
        person is using; headless, a tab. True: its own window in both. False: a tab in
        an existing window. Headed, that tab stays behind the tab its window shows, so
        Chrome never comes to the front, and focus emulation keeps the page reading as
        visible (and focused). Chrome picks the window, usually the browser's first one
        rather than the active tab's, so the result says where it landed and what the
        page reads, and says so when the page reads as hidden.
    """
    try:
        if not BrowserState.browser:
            return err("browser not running")
        browser = BrowserState.browser
        mode = _tab_new_mode(BrowserState.current_headless, new_window)
        before_tab = None
        if mode == "behind":
            with contextlib.suppress(Exception):
                before_tab = BrowserState.active_tab()
        # Headed, a foreground tab activates Chrome, and a background tab in the same window
        # is hidden from the page. Its own window behind the person's app is neither.
        if mode == "tab":
            new_tab = await browser.get("about:blank", new_tab=True)
        elif mode == "window":
            new_tab = await _srv._open_background_window(browser)
        else:
            new_tab = await _srv._open_background_tab(browser)
        # Apply the instance identity before the destination sees any script.
        await _srv._apply_fingerprint_to_tab(new_tab, BrowserState.current_fingerprint)
        emulation_error = await _srv._keep_painting_behind(new_tab) if mode == "behind" else None
        doc_state, disarm = await _srv._arm_document_watch(new_tab)
        try:
            if url != "about:blank":
                await new_tab.get(url)
                try:
                    await _srv._wait_load_complete(new_tab, 10)
                except Exception:
                    pass
            observed = await _srv._observe_navigation(new_tab, url, disarm())
        finally:
            disarm()
        problem = await _srv._tab_list_problem()
        if problem:
            return err(f"a tab was opened, but {problem}")
        if new_tab not in BrowserState.tabs:
            return err("a tab was opened, but the browser's tab list does not show it, so it was "
                       "not made the active tab")
        BrowserState.own([new_tab])
        BrowserState.active_tab_index = BrowserState.tabs.index(new_tab)
        BrowserState.active_lost = ""
        if mode == "behind":
            where = await _behind_note(browser, new_tab, before_tab, emulation_error)
        elif mode == "window":
            where = ("in its own window" if BrowserState.current_headless
                     else "in its own window, behind the app in use")
        else:
            where = "as a tab"
        return _srv._nav_result(
            f"opened tab [{BrowserState.active_tab_index}] {where}", observed,
            expect_url=expect_url, expect_status=expect_status,
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def tab_select(index: int, focus: bool = False) -> str:
    """Make the tab at index (from tab_list) the one every later tool acts on.

    Does not raise the window, since that takes the keyboard from whatever the person is
    doing, so a headed browser may keep showing another tab. focus=True, or tab_focus,
    brings this one to the front.
    """
    try:
        problem = await _srv._tab_list_problem()
        if problem:
            return err(f"tab {index} not selected: {problem}")
        if index < 0 or index >= len(BrowserState.tabs):
            return err(f"tab {index} out of range: {len(BrowserState.tabs)} tab(s), "
                       f"0..{len(BrowserState.tabs) - 1}")
        chosen = BrowserState.tabs[index]
        BrowserState.active_tab_index = index
        BrowserState.active_lost = ""
        # Picking a tab by index is the explicit request that lets tools act on a tab this
        # server did not open; the result says so, since it may be the one a person is using.
        outside = ""
        if BrowserState.opened_outside(chosen):
            BrowserState.adopted_targets.add(_srv._target_key(chosen))
            outside = ("; this tab was not opened by this server (a person opened it, or a page "
                       "with no opener did), and tools now act on it because you selected it")
        if focus or BrowserState.current_headless:
            # Headless has no foreground to take, and an unselected tab there stops painting.
            await chosen.activate()
            return ok(f"switched to tab {index}{outside}")
        # Headed and left where it is: a tab behind another one in its window reads as hidden
        # unless tab_new kept it painting, and an agent must not take its page for a shown one.
        if await _srv._page_visibility(chosen) == "hidden":
            return ok(f"switched to tab {index}{outside}; its page reads as hidden, because another "
                      f"tab is shown in its window: focus=True or tab_focus shows it, and brings "
                      f"Chrome to the front")
        return ok(f"switched to tab {index}{outside}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def tab_close(index: Optional[int] = None) -> str:
    """Close tab at index (defaults to active)."""
    try:
        problem = await _srv._tab_list_problem()
        idx = index if index is not None else BrowserState.active_tab_index
        if problem:
            return err(f"tab {idx} not closed: {problem}")
        if idx < 0 or idx >= len(BrowserState.tabs):
            return err(f"tab {idx} out of range: {len(BrowserState.tabs)} tab(s), "
                       f"0..{len(BrowserState.tabs) - 1}")
        closed_tab = BrowserState.tabs[idx]
        closing_active = idx == BrowserState.active_tab_index
        browser = BrowserState.browser
        marked = len(BrowserState.tabs) == 1
        if marked:
            # Said before the close: the quit watcher polls on its own and must never see this
            # browser empty without knowing a tool emptied it.
            _srv._window_watch_tool_closed(browser)
        try:
            await closed_tab.close()
        except Exception as e:
            # The mark must not outlive a close that did not happen: the watcher would then
            # wait on an empty browser for as long as this server runs. Only a target Chrome
            # no longer lists counts as closed; unknown takes the mark back.
            gone = await _target_gone(browser, closed_tab)
            if not gone:
                if marked:
                    _srv._window_watch_tool_closed_undo(browser)
                return err(f"tab {idx} not closed: {e}")
        try:
            _srv._clear_tab_scoped_state([closed_tab])
            # Target.closeTarget answers before the target is gone, and a list read in between
            # still holds it: the next tool then talked to a closing page and could hang.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not await _target_gone(browser, closed_tab):
                await asyncio.sleep(0.05)
            await _srv._refresh_tabs()
            closed_key = _srv._target_key(closed_tab)
            still = BrowserState.active_tab_index
            active_key = (_srv._target_key(BrowserState.tabs[still])
                          if 0 <= still < len(BrowserState.tabs) else None)
            BrowserState.tabs = [t for t in BrowserState.tabs if _srv._target_key(t) != closed_key]
            if active_key is not None and active_key != closed_key:
                BrowserState.active_tab_index = next(
                    i for i, t in enumerate(BrowserState.tabs) if _srv._target_key(t) == active_key)
            if not BrowserState.tabs:
                _srv._window_watch_tool_closed(browser)
            if closing_active:
                # The tool closed it, so this is no outside close: take the tab now at its place
                # (or the last), as always, but never one this server did not open.
                BrowserState.active_lost = ""
                BrowserState.active_tab_index = _next_own_tab(min(idx, len(BrowserState.tabs) - 1))
            elif BrowserState.active_tab_index >= len(BrowserState.tabs):
                BrowserState.active_tab_index = max(0, len(BrowserState.tabs) - 1)
        except Exception as e:
            return err(f"closed tab {idx}, but reading the tabs afterwards failed: {e}; "
                       "tab_list reads them again")
        return ok(f"closed tab {idx}")
    except Exception as e:
        return err(str(e))


def _next_own_tab(start: int) -> int:
    """The tab at `start` if the tools may act on it, else the nearest one they may; -1 for none."""
    tabs = BrowserState.tabs
    if start < 0 or not tabs:
        return 0 if not tabs else -1
    order = sorted(range(len(tabs)), key=lambda i: (abs(i - start), -i))
    for i in order:
        if BrowserState.acts_on(tabs[i]):
            return i
    BrowserState.active_lost = ("The active tab was closed, and every tab left was opened outside "
                                "this server. tab_select picks one to act on it on purpose.")
    return -1


async def _target_gone(browser, tab) -> bool:
    """True only when Chrome answered and no longer lists the tab's page."""
    try:
        pages = await asyncio.wait_for(_srv._page_targets(browser), timeout=2)
    except Exception:
        return False
    key = _srv._target_key(tab)
    return key is not None and all(target_id != key for target_id, _url in pages)


# Cookie, storage, IndexedDB and cache tools live in tools/storage.py. These two helpers
# stay here because tools across this file use them.
