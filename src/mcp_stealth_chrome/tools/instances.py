"""Multi-instance browser management: spawn_browser, list_instances, instance_describe,
switch_instance, instance_release, close_instance and close_all_instances, with
_launch_browser_instance, the launch behind spawn_browser.

Moved out of server.py unchanged. What these share with browser_launch (starting Chrome, the
idle reaper, the window watch, keep_alive, the cookie move, the close helpers) stays in server.py
and is reached here as _srv.<name> at call time, so a test that patches server.<name> still
reaches the moved code. PROFILE_DIR, PROFILES_ROOT, ensure_dirs, chrome_lock_holder_pid and
find_chrome_binary are reached the same way, never imported here: tests patch them on server to
keep the launch away from the person's real data folder.
"""

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Literal, Optional

from nodriver import Browser, Config

from .. import server as _srv
from .._app import mcp
from .. import _app as _app_module
from .. import brave as brave_mode
from .. import instance_notes
from .. import keep_alive as _keep_alive
from .. import launch_lock
from .. import presence
from .. import antibot
from .. import profile_version
from .. import tab_record
from ..helpers import err, ok
from ..state import (
    DEFAULT_IDLE_TIMEOUT,
    BrowserState,
    InstanceSnapshot,
    chrome_install_hint,
    find_external_chrome_pids,
)


def _instance_profile_path(
    instance_id: str,
    *,
    persistent: bool,
    privacy: bool,
    profile_dir_override: Optional[str],
    browser: str = "chrome",
) -> Optional[Path]:
    """Resolve the user-data dir actually passed to a named Chrome launch."""
    if privacy:
        return _srv._new_privacy_profile(instance_id)
    if profile_dir_override:
        return Path(profile_dir_override).expanduser()
    if not persistent:
        return None
    if browser == brave_mode.BRAVE:
        return brave_mode.instance_profile(_srv._brave_data_root(), instance_id)
    return _srv.PROFILE_DIR if instance_id == "main" else _srv.PROFILES_ROOT / instance_id


async def _launch_browser_instance(
    instance_id: str,
    url: str,
    headless: bool,
    proxy: Optional[str],
    user_agent: Optional[str],
    window_width: int,
    window_height: int,
    persistent: bool,
    lang: str,
    extra_args: Optional[list[str]],
    storage_state_path: Optional[str],
    idle_timeout: int,
    profile_dir_override: Optional[str] = None,
    fingerprint: Optional[dict] = None,
    privacy: bool = False,
    desc: str = "",
    restore_tabs: bool = False,
    keep_alive: Optional[bool] = None,
    ignored: Optional[list[str]] = None,
    browser: str = "chrome",
    vertical_tabs: Optional[str] = None,
    block_ads: bool = False,
    brave_lean: Optional[bool] = None,
    agent_overlay: Optional[bool] = None,
) -> tuple[bool, str]:
    """Shared launcher for browser_launch + spawn_browser.

    Returns (success, message).
    """
    try:
        _srv._validate_instance_id(instance_id)
    except ValueError as e:
        return False, str(e)
    product, brave_executable, problem = brave_mode.check_request(browser, vertical_tabs)
    if problem:
        return False, problem
    profile_dir_override, extra_args, problem = _srv._profile_from_extra_args(
        profile_dir_override, extra_args, "profile_dir")
    if problem:
        return False, problem

    # Pre-flight: ensure Chrome is installed before calling nodriver
    chrome_path = brave_executable or _srv.find_chrome_binary()
    if chrome_path is None:
        return False, (
            "Chrome/Chromium not found on this system.\n"
            + chrome_install_hint()
            + "\n\nAfter installing, re-launch the MCP server."
        )

    _srv.ensure_dirs()

    if privacy and (profile_dir_override or any(
        (arg or "").startswith("--user-data-dir=") for arg in (extra_args or [])
    )):
        return False, "privacy mode cannot reuse profile_dir or --user-data-dir"
    if privacy:
        persistent = False
    try:
        effective_proxy, effective_fingerprint, identity_source = _srv._launch_identity(
            instance_id,
            proxy=proxy,
            user_agent=user_agent,
            fingerprint=fingerprint,
            privacy=privacy,
        )
    except Exception as e:
        return False, f"identity setup failed: {e}"

    # Determine profile dir per instance
    profile_path = _instance_profile_path(
        instance_id,
        persistent=persistent,
        privacy=privacy,
        profile_dir_override=profile_dir_override,
        browser=product,
    )
    privacy_profile = profile_path if privacy else None
    launch_choice: Optional[_srv._LaunchChoice] = None
    if profile_path is not None:
        if profile_path.exists():
            holder = _srv.chrome_lock_holder_pid(profile_path)
            if holder is not None:
                kept = None if privacy else _srv._kept_verdict(
                    profile_path, product,
                    adopt_orphan_for=instance_id if profile_dir_override is None else None)
                if kept and kept.get("ok"):
                    taken = await _spawn_by_reattach(instance_id, profile_path, kept, url=url,
                                                     keep_alive=keep_alive, ignored=ignored or [],
                                                     idle_timeout=idle_timeout, desc=desc,
                                                     block_ads=block_ads, agent_overlay=agent_overlay)
                    if taken[0] or not _srv._orphan_given_back(profile_path, kept):
                        return taken
                    return False, (f"profile {profile_path} is locked by Chrome PID {holder}, a "
                                   f"browser of ours left running when its server went, which could "
                                   f"not be taken over: {taken[1]}")
                return False, (
                    f"profile {profile_path} is locked by Chrome PID {holder}; "
                    "close that browser or clone the profile first"
                    + _srv._not_reattached_note(profile_path, kept)
                )
        # Before anything writes to it (mkdir and clean_profile_state wait for the cookie checks
        # below): whether the profile is new decides
        # whether Chrome Beta may open it, and a refused profile stays as it was.
        launch_choice = _srv._launch_choice(profile_path, headless, extra_args, brave_executable)
        refused = _srv._profile_version_refusal(profile_path, launch_choice.executable, product)
        if refused:
            await _srv._remove_privacy_profile(privacy_profile)
            return False, refused

    if lang and lang != "en-US" and not effective_fingerprint.get("accept_language"):
        effective_fingerprint = {**effective_fingerprint, "accept_language": lang}
    config = Config(
        user_data_dir=str(profile_path) if profile_path is not None else None,
        headless=headless,
        lang=(effective_fingerprint.get("accept_language", "").split(",")[0] or lang),
        browser_args=[*(extra_args or []), *_srv._profile_directory_args(profile_path, extra_args)],
    )
    config.add_argument("--hide-crash-restore-bubble")
    config.add_argument("--disable-session-crashed-bubble")
    config.add_argument("--disable-restore-session-state")
    config.add_argument("--no-default-browser-check")
    for _flag in _srv._LEAN_FLAGS:
        config.add_argument(_flag)
    if effective_fingerprint.get("user_agent"):
        config.add_argument(f"--user-agent={effective_fingerprint['user_agent']}")
    if effective_proxy:
        config.add_argument(f"--proxy-server={effective_proxy}")
    config.add_argument(f"--window-size={window_width},{window_height}")
    if headless:
        _screen_arg = _srv._headless_screen_arg(window_width, window_height, extra_args)
        if _screen_arg:
            config.add_argument(_screen_arg)

    browser: Optional[Browser] = None
    if launch_choice is None:
        launch_choice = _srv._launch_choice(profile_path, headless, extra_args, brave_executable)
    dock_note = await _srv._use_dock_icon_browser(config, headless, launch_choice)
    refused = _srv._profile_version_refusal(profile_path, config.browser_executable_path, product)
    if refused:
        await _srv._remove_privacy_profile(privacy_profile)
        return False, refused
    with contextlib.suppress(Exception):
        dock_note += profile_version.upgrade_note(profile_path, config.browser_executable_path)
    cookie_move = await _srv._prepare_cookie_move(profile_path, config)
    if cookie_move.refuse:
        await _srv._remove_privacy_profile(privacy_profile)
        return False, cookie_move.refuse
    if cookie_move.flag:
        config.add_argument(_srv._cookie_flag_arg(config))
    if not cookie_move.backup:
        dock_note += cookie_move.note
    try:
        dock_note += _srv._browser_note(config, product) + brave_mode.write_vertical_tabs(
            Path(config.user_data_dir), config.browser_args, product, vertical_tabs)
    except Exception as e:
        await _srv._remove_privacy_profile(privacy_profile)
        return False, (f"vertical_tabs could not be written into the profile, so nothing was "
                       f"started: {_srv._exc_text(e)}")
    dock_note += _srv._apply_brave_lean(config, product, brave_lean,
                                   server_chosen=not profile_dir_override)
    previous_app = None if headless else await asyncio.to_thread(_srv._frontmost_pid)
    if not headless:
        for _flag in _srv._BACKGROUND_WINDOW_FLAGS:
            config.add_argument(_flag)
    refused = await _srv._cookie_preflight(profile_path, cookie_move) \
        or await _srv._commit_cookie_move(profile_path, cookie_move)
    if refused:
        await _srv._remove_privacy_profile(privacy_profile)
        return False, refused
    if profile_path is not None:
        # Only now: clean_profile_state and mkdir write, and a refusal writes nothing.
        profile_path.mkdir(parents=True, exist_ok=True)
        if persistent or profile_dir_override:
            _srv.clean_profile_state(profile_path)
    refused = await _srv._cookie_preflight(profile_path, cookie_move)  # the last look; see browser_launch
    if refused:
        await _srv._remove_privacy_profile(privacy_profile)
        return False, refused
    ads_pending = _srv._adblock_module().begin(block_ads, config.browser_executable_path) if block_ads else None
    try:
        browser = await _srv._start_chrome(config)
        _srv._mark_allow_origins(browser, config)
        brave_mode.mark(browser, product)
        if not headless:
            await _srv._open_background_window(browser, window_width, window_height)
    except asyncio.TimeoutError:
        await _srv._safe_stop_browser(browser)
        await _srv._remove_privacy_profile(privacy_profile)
        return False, (
            f"launch timed out after {_srv.BROWSER_LAUNCH_TIMEOUT}s — profile "
            f"{profile_path or '(ephemeral)'} may be locked or Chrome is hung."
        )
    except asyncio.CancelledError:
        await _srv._safe_stop_browser(browser)
        await _srv._remove_privacy_profile(privacy_profile)
        raise
    except Exception as e:
        await _srv._safe_stop_browser(browser)
        await _srv._remove_privacy_profile(privacy_profile)
        return False, f"launch failed: {_srv._exc_text(e)}"
    if ads_pending is not None:
        dock_note += await _srv._adblock_module().attach(ads_pending, browser, instance_id,
                                                    BrowserState.session_key())
    dock_note += await presence.attach(browser, _srv._browser_process_pid(browser), ask=agent_overlay,
                                 headless=headless)

    if cookie_move.backup is not None:
        moved, moved_text = await _srv._finish_cookie_move(browser, Path(str(profile_path)), cookie_move)
        if not moved:
            # Not registered: a running instance would be reused and the move never tried again.
            return False, (f"instance {instance_id!r}: its cookies were not all moved: {moved_text}. "
                           f"{await _srv._close_after_cookie_failure(browser, profile_path)}")
        dock_note += moved_text

    if storage_state_path:
        try:
            await _srv._apply_storage_state(browser, storage_state_path)
        except asyncio.CancelledError:
            await _srv._safe_stop_browser(browser)
            await _srv._remove_privacy_profile(privacy_profile)
            raise
        except Exception as e:
            await _srv._safe_stop_browser(browser)
            await _srv._remove_privacy_profile(privacy_profile)
            return False, f"storage_state load failed: {e}"

    try:
        await asyncio.sleep(0.5)
        main_tab = await _srv._first_live_tab(browser)
        await _srv._apply_fingerprint_to_tab(main_tab, effective_fingerprint)
        doc_state, disarm = await _srv._arm_document_watch(main_tab)
        try:
            initial_observed = await _srv._commit_navigation(main_tab, url, doc_state)
            if initial_observed is None:
                await _srv._wait_load_complete(main_tab, 3)
                observed = await _srv._observe_navigation(main_tab, url, disarm())
            else:
                observed = initial_observed
        finally:
            disarm()
    except asyncio.CancelledError:
        await _srv._safe_stop_browser(browser)
        await _srv._remove_privacy_profile(privacy_profile)
        raise
    except Exception as e:
        await _srv._ensure_browser_dead(browser, profile_path)
        await _srv._remove_privacy_profile(privacy_profile)
        return False, f"initial nav failed: {_srv._exc_text(e)}"

    slot_tabs, slot_active, restore_text = [main_tab], 0, ""
    if restore_tabs and initial_observed is None:
        try:
            slot_tabs, slot_active, _report, _summary = await _srv._reopen_saved_tabs(
                instance_id, browser, main_tab, profile_path, privacy, headless,
                effective_fingerprint, url, window_width, window_height)
            restore_text = f"{_summary}\n{json.dumps({'restore_tabs': _report}, indent=2, default=str)}"
        except Exception as e:
            restore_text = f" [restore_tabs: failed: {_srv._exc_text(e)}]"

    # Write into the target instance slot
    if instance_id == BrowserState.current_instance_id:
        BrowserState.browser = browser
        BrowserState.tabs = slot_tabs
        BrowserState.own(BrowserState.tabs)  # opened or taken over by this server
        BrowserState.active_tab_index = slot_active
        BrowserState.current_profile_dir = profile_path
        BrowserState.current_desc = desc
        BrowserState.current_headless = headless
        BrowserState.current_privacy = privacy
        BrowserState.current_fingerprint = dict(effective_fingerprint)
        BrowserState.current_proxy = effective_proxy
        BrowserState.current_idle_timeout = idle_timeout
        BrowserState.current_last_active = time.time()
        BrowserState.current_created_at = time.time()
    else:
        # Store as snapshot without becoming current
        snap = InstanceSnapshot(
            instance_id=instance_id,
            browser=browser,
            tabs=slot_tabs,
            active_tab_index=slot_active,
            owned_targets={_srv._target_key(t) for t in slot_tabs},
            profile_dir=profile_path,
            desc=desc,
            headless=headless,
            privacy=privacy,
            fingerprint=dict(effective_fingerprint),
            proxy=effective_proxy,
            idle_timeout=idle_timeout,
            last_active=time.time(),
            created_at=time.time(),
        )
        BrowserState.instances[instance_id] = snap

    # Kick off the idle reaper (once)
    _srv._ensure_idle_reaper_running()
    BrowserState.window_closed.pop(instance_id, None)
    if not headless:
        _srv._watch_windows(browser)
    await _srv._record_tabs_at_launch(instance_id)
    _keep, _keep_note = _srv._keep_alive_decision(keep_alive, profile_path, privacy, headless)
    if _keep:
        _keep_note = _srv._arm_keep_alive(browser, profile_path, instance_id, headless,
                                           effective_fingerprint) or _keep_note
    dock_note += _keep_note
    dock_note += _srv._allow_origins_note(browser)
    await _srv._give_focus_back(previous_app)
    if initial_observed is not None:
        return False, _srv._nav_commit_timeout_message(
            f"Browser instance {instance_id!r} is running; switch_instance({instance_id!r}) to use it. First page",
            initial_observed)
    # The first page as the tripwire sees it (antibot_tripwire): spawn_browser observes no
    # auto-verify pass, so retain the document response and inspect the loaded page.
    trip = None
    if url and url != "about:blank":
        with contextlib.suppress(Exception):  # a look that failed leaves the launch's own result
            trip = await _srv._tripwire_after_navigation(
                main_tab, observed, doc_state=doc_state, headless=headless)
    return True, _srv._tripwire_render(trip, "spawn_browser", (
        f"instance {instance_id!r} launched (headless={headless}, "
        f"privacy={privacy}, identity={identity_source}, "
        f"profile={profile_path.name if profile_path else '(ephemeral Chrome temp)'})"
        f"{dock_note}{restore_text}"
    ), asked_for=url, launched=True)


async def _spawn_by_reattach(instance_id: str, profile: Path, verdict: dict, *, url: str,
                            keep_alive: Optional[bool], ignored: list[str], idle_timeout: int,
                            desc: str, block_ads: bool = False,
                            agent_overlay: Optional[bool] = None) -> tuple[bool, str]:
    """spawn_browser's reattach: the kept browser fills the instance slot, current or stored."""
    record = verdict["record"]
    ignored = [item for item in ignored if item != f"headless={bool(record.get('headless'))!r}"]
    try:
        browser, tabs, select, observed, summary, keep_text = await _srv._reattach_kept(
            instance_id, profile, verdict, url=url, keep_alive=keep_alive, ignored=ignored)
    except Exception as e:
        return False, (f"a browser kept alive on {profile} ({_keep_alive.describe(record)}) is "
                       f"ours and free, but connecting to it failed: {_srv._exc_text(e)}. It was left "
                       f"running as it was")
    owned = observed.pop("_owned", None)
    doc_state = observed.pop("_doc_state", None)
    owned = {BrowserState.COUNTED, *owned} if owned is not None else {_srv._target_key(t) for t in tabs}
    if _srv._persons_browser(browser) and idle_timeout:
        # The person's browser is theirs to Quit: never ended for the agent's idleness.
        idle_timeout = 0
        keep_text += " [idle_timeout: 0, since the person opened this browser from the launcher]"
    fields = dict(browser=browser, tabs=tabs, active_tab_index=select,
                  owned_targets=owned,
                  profile_dir=Path(_keep_alive.profile_key(profile)), desc=desc,
                  headless=bool(record.get("headless")), privacy=False,
                  fingerprint=dict(record.get("fingerprint") or {}), proxy=verdict.get("proxy"),
                  idle_timeout=idle_timeout, last_active=time.time(), created_at=time.time())
    if instance_id == BrowserState.current_instance_id:
        BrowserState.browser = browser
        BrowserState.tabs = tabs
        BrowserState.owned_targets, BrowserState.adopted_targets = set(owned), set()
        BrowserState.active_lost = ""
        BrowserState.active_tab_index = select
        BrowserState.current_profile_dir = fields["profile_dir"]
        BrowserState.current_desc = desc
        BrowserState.current_headless = fields["headless"]
        BrowserState.current_privacy = False
        BrowserState.current_fingerprint = fields["fingerprint"]
        BrowserState.current_proxy = fields["proxy"]
        BrowserState.current_idle_timeout = idle_timeout
        BrowserState.current_last_active = time.time()
        BrowserState.current_created_at = time.time()
    else:
        BrowserState.instances[instance_id] = InstanceSnapshot(instance_id=instance_id, **fields)
    _srv._ensure_idle_reaper_running()
    BrowserState.window_closed.pop(instance_id, None)
    _srv._ATTACHED_BROWSERS.discard(instance_id)
    keep_text += await _srv._block_ads_on_reattach(block_ads, browser, instance_id)
    keep_text += await presence.attach(browser, _srv._browser_process_pid(browser), ask=agent_overlay,
                                 headless=bool(verdict["record"].get("headless")),
                                 persons_browser=_srv._persons_browser(browser))
    if "commit_timeout_seconds" in observed:
        return False, _srv._nav_commit_timeout_message(f"Instance {instance_id!r} {summary}", observed)
    trip = (await _srv._tripwire_after_navigation(
                tabs[select], observed, doc_state=doc_state, headless=bool(record.get("headless")))
            if url and url != "about:blank" else None)
    return True, _srv._tripwire_render(trip, "spawn_browser", (
        f"instance {instance_id!r} {summary}{keep_text}\n"
        f"{json.dumps(observed, indent=2, default=str)}"), asked_for=url, launched=True)


@mcp.tool()
async def spawn_browser(
    instance_id: str,
    url: str = "about:blank",
    headless: bool = False,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    window_width: int = 1280,
    window_height: int = 800,
    persistent: bool = True,
    lang: str = "en-US",
    extra_args: Optional[list[str]] = None,
    storage_state_path: Optional[str] = None,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT,
    profile_dir: Optional[str] = None,
    desc: str = "",
    fingerprint: Optional[dict] = None,
    privacy: bool = False,
    restore_tabs: bool = False,
    keep_alive: Optional[bool] = None,
    browser: Literal["chrome", "brave"] = "chrome",
    vertical_tabs: Optional[str] = None,
    block_ads: bool = False,
    brave_lean: Optional[bool] = None,
    agent_overlay: Optional[bool] = None,
) -> str:
    """Create a named browser instance running in parallel with main, with its own profile, cookies, tabs and
    logs, for multi-account scraping or isolated sessions; switch_instance(id) routes later tool calls to it.
    A headed instance the person quits, or whose windows are all closed, is ended by the server, and later
    calls on it say so.

    Args:
        instance_id: unique name (e.g., "scraper_1", "acct_alice")
        idle_timeout_seconds: auto-close after idle (0 = never, default 600s)
        profile_dir: override profile path (default: ~/.mcp-stealth/profiles/<id>/)
        desc: natural-language purpose used to find the instance by meaning later
        fingerprint: explicit user_agent/accept_language/platform/timezone overrides
        privacy: empty profile deleted when closed; the identity stays the real browser and machine unless
            fingerprint overrides it. To use a logged-in account, leave this off
        restore_tabs: as browser_launch(restore_tabs=True): reopen the tabs it had when last closed
        keep_alive: as browser_launch(keep_alive=True): left running when this server exits, and a later
            spawn_browser or browser_launch on the profile reattaches. The idle timeout applies while a server
            holds it
        browser: "chrome" (default) or "brave"; a Brave profile defaults to ~/.mcp-stealth/brave-profiles/<id>/
        vertical_tabs: "left", "right" (Brave only) or "off"
        block_ads: as browser_launch(block_ads=True)
        brave_lean: Brave only (None: lean on a profile this server made empty, True: lean, False: as it ships)
        agent_overlay: the light, bar and Take over on this window (macOS, headed; None follows the
            agent_overlay setting)
        other args: same as browser_launch
    """
    try:
        _srv._validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    problem = _srv._mock_keychain_refusal(extra_args)
    if problem:
        return err(problem)
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    if instance_id in BrowserState.released_ids():
        return err(f"instance {instance_id!r} was released by another session. "
                   f"Take it with switch_instance({instance_id!r}).")
    if _srv._LAUNCH_LOCK.locked():
        return err("another launch is already in progress — wait for it to finish")
    # The name is only visible to other sessions once the launch finishes, so two
    # sessions spawning one name would both pass the check above and share a profile.
    holder = _srv._SPAWNING.get(instance_id)
    if holder is not None:
        return err(f"instance {instance_id!r} is being launched by another session ({holder}).")
    _srv._SPAWNING[instance_id] = BrowserState.session_key()
    try:
        async with _srv._LAUNCH_LOCK:
            if instance_id == BrowserState.current_instance_id:
                if BrowserState.is_up():
                    return err(
                        f"instance {instance_id!r} already running (current). "
                        "Use switch_instance instead."
                    )
                if BrowserState.current_privacy and BrowserState.current_profile_dir:
                    return err(
                        f"instance {instance_id!r} has pending privacy cleanup. "
                        "Retry close_instance() before relaunching."
                    )
            if instance_id in BrowserState.instances:
                state = (
                    "running" if BrowserState.instances[instance_id].is_running()
                    else "stopped or awaiting cleanup"
                )
                return err(
                    f"instance {instance_id!r} already exists ({state}). "
                    "Call close_instance() before reusing its name."
                )
            _reserved = _srv._launch_reservation(
                instance_id, (browser or "chrome").strip().lower(), persistent=bool(persistent),
                privacy=privacy, explicit=profile_dir, extra_args=extra_args)
            async with launch_lock.reserve(_reserved, _srv.BROWSER_LAUNCH_TIMEOUT) as _busy:
                if _busy:
                    return err(_busy)
                ok_flag, msg = await _launch_browser_instance(
                    instance_id=instance_id,
                    url=url,
                    headless=headless,
                    proxy=proxy,
                    user_agent=user_agent,
                    window_width=window_width,
                    window_height=window_height,
                    persistent=persistent,
                    lang=lang,
                    extra_args=extra_args,
                    storage_state_path=storage_state_path,
                    idle_timeout=idle_timeout_seconds,
                    profile_dir_override=profile_dir,
                    fingerprint=fingerprint,
                    privacy=privacy,
                    desc=desc,
                    restore_tabs=restore_tabs,
                    keep_alive=keep_alive,
                    ignored=_srv._reattach_ignored(locals()),
                    browser=browser,
                    vertical_tabs=vertical_tabs,
                    block_ads=block_ads,
                    brave_lean=brave_lean,
                    agent_overlay=agent_overlay,
                )
                if ok_flag and msg.startswith(antibot.PAUSE_PREFIX):
                    ok_flag, msg = False, msg.removeprefix("Error: ")
                elif ok_flag:
                    msg += launch_lock.note()
    finally:
        _srv._SPAWNING.pop(instance_id, None)
    if not ok_flag:
        return err(msg)
    return ok(msg)


@mcp.tool()
async def list_instances() -> str:
    """List all browser instances with status + last-active time.
    Also reports external (non-MCP) Chrome processes that may conflict with
    custom --user-data-dir launches."""
    if BrowserState.browser is not None:
        with contextlib.suppress(Exception):
            await _srv._refresh_tabs()
    snapshots = BrowserState.list_snapshots()
    out = []
    now = time.time()
    for s in snapshots:
        is_current = s.instance_id == BrowserState.current_instance_id
        idle_s = int(now - s.last_active) if s.is_running() else 0
        out.append({
            "instance_id": s.instance_id,
            "current": is_current,
            "running": s.is_running(),
            "tabs": len(s.tabs),
            "idle_seconds": idle_s,
            "idle_timeout": s.idle_timeout,
            "auto_close_in": max(0, s.idle_timeout - idle_s) if s.idle_timeout > 0 and s.is_running() else None,
            "profile": str(s.profile_dir) if s.profile_dir else None,
            "desc": s.desc,
            "headless": s.headless,
            "privacy": s.privacy,
            "browser": brave_mode.product_of(s.browser) if s.browser is not None else None,
            "uptime_seconds": int(now - s.created_at) if s.is_running() else 0,
        })
    external = find_external_chrome_pids()
    payload = {
        "instances": out,
        "session": BrowserState.session_key(),
        "released_claimable": BrowserState.released_ids(),
        "other_sessions": BrowserState.other_session_instances(),
        "external_chrome_pids": external,
        "warning": (
            f"{len(external)} external Chrome process(es) detected — "
            "custom --user-data-dir pointing to your daily Chrome profile will conflict."
        ) if external else None,
    }
    return ok(json.dumps(payload, indent=2, default=str))


@mcp.tool()
async def instance_describe(instance_id: str, desc: str, append: bool = False) -> str:
    """Set or append the natural-language purpose of a browser instance."""
    try:
        _srv._validate_instance_id(instance_id)
        foreign = BrowserState.foreign_instance(instance_id)
        if foreign:
            return err(foreign)
        if instance_id == BrowserState.current_instance_id:
            old = BrowserState.current_desc
            BrowserState.current_desc = (
                f"{old}; {desc}" if append and old and desc else (desc or old if append else desc)
            )
            updated = BrowserState.current_desc
        else:
            snap = BrowserState.instances.get(instance_id)
            if snap is None:
                return err(f"instance {instance_id!r} not found")
            old = snap.desc
            snap.desc = f"{old}; {desc}" if append and old and desc else (desc or old if append else desc)
            updated = snap.desc
        return ok(f"instance {instance_id!r} description: {updated}")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def switch_instance(instance_id: str) -> str:
    """Make instance_id the active one for subsequent tool calls.

    The previous current instance continues running in the background,
    cookies/tabs preserved. Swap back anytime.
    """
    try:
        _srv._validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    if instance_id == BrowserState.current_instance_id:
        return ok(f"already on {instance_id!r}")
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    # Being closed by a sweep (idle timeout, last window gone): switching it in now would hand
    # the caller a browser that is going, and the close would then reset the current instance.
    still = await _app_module.wait_while_closing(BrowserState.session_key(), instance_id)
    if still:
        return still
    # A launch can have begun while this waited: the gate saw none when the call came in.
    refused = _srv._launch_gate("switch_instance")
    if refused:
        return refused
    BrowserState.claim_released(instance_id)
    existed = instance_id in BrowserState.instances
    BrowserState.switch_to(instance_id)
    if not existed and not BrowserState.is_up():
        closed = BrowserState.window_closed.get(instance_id)
        if closed:
            return ok(f"switched to {instance_id!r}, which is not running: {closed} Start it again "
                      f"with spawn_browser or browser_launch")
        return ok(f"switched to {instance_id!r} (not yet running — call spawn_browser or browser_launch)")
    return ok(f"switched to {instance_id!r}")


@mcp.tool()
async def instance_release(instance_id: str) -> str:
    """Hand a running instance over so another MCP session can take it.

    Only meaningful in HTTP mode, where several sessions share one server. The
    instance keeps running, tabs and logins intact, until another session calls
    switch_instance on it or the idle timeout closes it. Switch away first if it is
    the current one.
    """
    try:
        _srv._validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    if instance_id == BrowserState.current_instance_id:
        return err(
            f"{instance_id!r} is this session's current instance. "
            "switch_instance to another name first, then release it."
        )
    if instance_id not in BrowserState.instances:
        return err(f"instance {instance_id!r} not found in this session")
    BrowserState.release_instance(instance_id)
    return ok(f"released {instance_id!r}; another session can take it with "
              f"switch_instance({instance_id!r})")


@mcp.tool()
@instance_notes.says_what_stays_current
async def close_instance(instance_id: str, forget_tabs: bool = False) -> str:
    """Close a specific browser instance (frees profile + memory).

    Its open tabs are saved first (persistent profiles only) for restore_tabs=True on the next
    launch of that instance. forget_tabs=True closes without saving and removes the saved list.
    Closing the current instance leaves that id current, not running: the next browser_launch
    starts it again on its own profile, and the result says so. switch_instance('main') to go
    back to main.
    """
    try:
        _srv._validate_instance_id(instance_id)
    except ValueError as e:
        return err(str(e))
    foreign = BrowserState.foreign_instance(instance_id)
    if foreign:
        return err(foreign)
    still = await _app_module.wait_while_closing(BrowserState.session_key(), instance_id)
    if still:
        return still
    # Said before the first await: a sweep picking the same instance would close it twice.
    with _srv._closing_in_background(instance_id):
        return await _close_instance_now(instance_id, forget_tabs)


async def _close_instance_now(instance_id: str, forget_tabs: bool) -> str:
    BrowserState.claim_released(instance_id)
    # Close current
    if instance_id == BrowserState.current_instance_id:
        # The handle, not is_up(): see browser_close.
        if BrowserState.browser is not None:
            _profile = BrowserState.current_profile_dir
            _privacy = BrowserState.current_privacy
            _tabs = list(BrowserState.tabs)
            # Remember the placement before the window goes away, so the next
            # launch can land on the same monitor instead of the primary one.
            try:
                await _srv._save_window_bounds(BrowserState.current_instance_id,
                                          BrowserState.tabs[0] if BrowserState.tabs else None)
            except Exception:
                pass
            if forget_tabs:
                _srv._mark_closing(BrowserState.browser)
            else:
                await _srv._record_tabs_before_close(
                    instance_id, BrowserState.browser, _tabs, BrowserState.active_tab_index,
                    _profile, _privacy, "close_instance")
            _kill = await _srv._ensure_browser_dead(BrowserState.browser, _profile)
            if _srv._close_incomplete(_kill):
                return err(f"close failed: {_srv._close_failure(_kill)} — instance retained")
            _forgot = _srv._forget_tabs_note(instance_id, _profile, _privacy) if forget_tabs else ""
            _iid = BrowserState.current_instance_id
            BrowserState.reset()
            _srv._clear_tab_scoped_state(_tabs)
            BrowserState.current_headless = False
            BrowserState.current_privacy = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
            if _privacy:
                if not await _srv._remove_privacy_profile(_profile):
                    BrowserState.current_profile_dir = _profile
                    BrowserState.current_privacy = True
                    return err(f"closed browser but failed to remove privacy profile {_profile}"
                               f"{_srv._removal_failure(_profile)}")
            elif _profile:
                _srv.clean_profile_state(_profile)
            _srv._ATTACHED_BROWSERS.discard(_iid)
            return ok(f"closed current instance {instance_id!r}" + _srv._kill_note(_kill) + _forgot)
        if BrowserState.current_privacy:
            _profile = BrowserState.current_profile_dir
            if not await _srv._remove_privacy_profile(_profile):
                return err(f"failed to remove stale privacy profile {_profile}{_srv._removal_failure(_profile)}")
            BrowserState.current_privacy = False
            BrowserState.current_headless = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
            return ok(f"current instance {instance_id!r} was not running; privacy profile removed")
        if forget_tabs:
            gone = tab_record.forget_instance(instance_id)
            return ok(f"current instance {instance_id!r} was not running."
                      + (" Removed its saved tab list." if gone else " It had no saved tab list."))
        return ok(f"current instance {instance_id!r} was not running")
    # Close stored
    snap = BrowserState.instances.get(instance_id)
    if snap is None:
        return err(f"instance {instance_id!r} not found")
    _tabs = list(snap.tabs)
    try:
        await _srv._save_window_bounds(instance_id, snap.tabs[0] if snap.tabs else None)
    except Exception:
        pass
    if forget_tabs:
        _srv._mark_closing(snap.browser)
    else:
        await _srv._record_tabs_before_close(instance_id, snap.browser, _tabs, snap.active_tab_index,
                                        snap.profile_dir, snap.privacy, "close_instance")
    _kill = await _srv._ensure_browser_dead(snap.browser, snap.profile_dir)
    if _srv._close_incomplete(_kill):
        return err(
            f"close failed for instance {instance_id!r}: {_srv._close_failure(_kill)}"
            f" — instance retained for retry"
        )
    _forgot = _srv._forget_tabs_note(instance_id, snap.profile_dir, snap.privacy) if forget_tabs else ""
    # Chrome is stopped even if residue cleanup below fails. Retain a minimal
    # snapshot in that case so a second close_instance call can retry cleanup.
    snap.browser = None
    snap.tabs = []
    _srv._clear_tab_scoped_state(_tabs)
    cleanup_ok = True
    if snap.privacy:
        cleanup_ok = await _srv._remove_privacy_profile(snap.profile_dir)
    elif snap.profile_dir:
        _srv.clean_profile_state(snap.profile_dir)
    if not cleanup_ok:
        return err(f"closed browser but failed to remove privacy profile {snap.profile_dir}"
                   f"{_srv._removal_failure(snap.profile_dir)}")
    BrowserState.instances.pop(instance_id, None)
    _srv._ATTACHED_BROWSERS.discard(instance_id)
    return ok(f"closed instance {instance_id!r}" + _srv._kill_note(_kill) + _forgot)


@mcp.tool()
async def close_all_instances() -> str:
    """Close every running browser instance. Useful for cleanup."""
    session = BrowserState.session_key()
    for key in [k for k in list(_app_module.INSTANCE_CLOSING) if k[0] == session]:
        still = await _app_module.wait_while_closing(*key)  # one closer per browser
        if still:
            return still
    # Said before the first await, for every instance: a sweep must not close one of them too.
    with contextlib.ExitStack() as closing:
        for iid in {BrowserState.current_instance_id, *BrowserState.instances}:
            closing.enter_context(_srv._closing_in_background(iid))
        return await _close_all_instances_now()


async def _close_all_instances_now() -> str:
    closed = []
    failures = []
    # Close stored
    for iid, snap in list(BrowserState.instances.items()):
        _tabs = list(snap.tabs)
        if snap.browser:
            await _srv._record_tabs_before_close(iid, snap.browser, _tabs, snap.active_tab_index,
                                            snap.profile_dir, snap.privacy, "close_all_instances")
            _kill = await _srv._ensure_browser_dead(snap.browser, snap.profile_dir)
            if _srv._close_incomplete(_kill):
                failures.append(f"{iid}: {_srv._close_failure(_kill)}; instance retained")
                continue
        snap.browser = None
        snap.tabs = []
        _srv._clear_tab_scoped_state(_tabs)
        if snap.privacy:
            if not await _srv._remove_privacy_profile(snap.profile_dir):
                failures.append(f"{iid}: privacy cleanup failed ({snap.profile_dir}"
                                f"{_srv._removal_failure(snap.profile_dir)})")
                continue
        elif snap.profile_dir:
            _srv.clean_profile_state(snap.profile_dir)
        BrowserState.instances.pop(iid, None)
        closed.append(iid)
    # Close current. The handle, not is_up(): see browser_close.
    if BrowserState.browser is not None:
        _current_profile = BrowserState.current_profile_dir
        _current_privacy = BrowserState.current_privacy
        _current_tabs = list(BrowserState.tabs)
        await _srv._record_tabs_before_close(
            BrowserState.current_instance_id, BrowserState.browser, _current_tabs,
            BrowserState.active_tab_index, _current_profile, _current_privacy,
            "close_all_instances")
        _kill = await _srv._ensure_browser_dead(BrowserState.browser, _current_profile)
        if _srv._close_incomplete(_kill):
            failures.append(
                f"{BrowserState.current_instance_id}: "
                f"{_srv._close_failure(_kill)}; instance retained"
            )
        else:
            _srv._clear_tab_scoped_state(_current_tabs)
            cleanup_ok = True
            if _current_privacy:
                cleanup_ok = await _srv._remove_privacy_profile(_current_profile)
            elif _current_profile:
                _srv.clean_profile_state(_current_profile)
            if cleanup_ok:
                closed.append(BrowserState.current_instance_id)
                BrowserState.reset()
                BrowserState.current_headless = False
                BrowserState.current_privacy = False
                BrowserState.current_fingerprint = {}
                BrowserState.current_proxy = None
            else:
                BrowserState.reset()
                BrowserState.current_profile_dir = _current_profile
                BrowserState.current_privacy = True
                failures.append(
                    f"{BrowserState.current_instance_id}: privacy cleanup failed "
                    f"({_current_profile}{_srv._removal_failure(_current_profile)})"
                )
    elif BrowserState.current_privacy:
        if await _srv._remove_privacy_profile(BrowserState.current_profile_dir):
            BrowserState.current_privacy = False
            BrowserState.current_headless = False
            BrowserState.current_fingerprint = {}
            BrowserState.current_proxy = None
        else:
            failures.append(
                f"{BrowserState.current_instance_id}: stale privacy cleanup failed "
                f"({BrowserState.current_profile_dir}"
                f"{_srv._removal_failure(BrowserState.current_profile_dir)})"
            )
    for _iid in closed:
        _srv._ATTACHED_BROWSERS.discard(_iid)
    if failures:
        return err(
            f"closed {len(closed)} instance(s): {closed}; "
            f"{len(failures)} failure(s): {' | '.join(failures)}"
        )
    return ok(f"closed {len(closed)} instance(s): {closed}")
