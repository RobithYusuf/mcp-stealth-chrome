"""Debug and meta tools: server_status, get_page_errors, fingerprint_audit and export_curl, with
the OS cursor check that server_status reports.

Moved out of server.py unchanged. The OS cursor probe thread and its lock stay in server.py,
where the server starts the probe; this module reads them as _srv.<name> at call time.
"""

import asyncio
import contextlib
import ipaddress
import json
import time
from typing import Any, Literal, Optional, Union

from .. import __version__
from .. import brave as brave_mode
from .. import patches as _patches
from .. import presence
from .. import profile_gc
from .._app import mcp
from ..helpers import (
    adblock_package_note,
    BODY_REVEAL_NOTE,
    err,
    mask_body,
    mask_headers,
    mask_url,
    masked_value,
    reveal_mode,
    ok,
)
from ..identity import chrome_version
from ..state import BrowserState
from . import capture as _capture
from .. import server as _srv


def _status_profile() -> str:
    """The profile the current browser really uses, not the default it would get."""
    if BrowserState.current_profile_dir:
        return str(BrowserState.current_profile_dir)
    browser = BrowserState.browser
    if browser is None:
        return str(_srv.PROFILE_DIR)  # through server: tests point that name at a temporary folder
    if getattr(browser, "_process", None) is None and not getattr(browser, "_process_pid", None):
        return "(attached Chrome: profile not known)"
    data_dir = getattr(getattr(browser, "config", None), "user_data_dir", None)
    return f"(ephemeral) {data_dir}" if data_dir else "(ephemeral)"


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _cdp_endpoint(browser: Any) -> Optional[dict]:
    """The current browser's DevTools endpoint: host, port and the browser's own websocket URL, or
    None with no browser. A launch binds 127.0.0.1; attach_to_chrome(host=...) can name another
    machine, and then a note says so rather than leave it to be noticed."""
    if browser is None:
        return None
    config = getattr(browser, "config", None)
    host, port = getattr(config, "host", None), getattr(config, "port", None)
    ws = None
    with contextlib.suppress(Exception):
        ws = browser.websocket_url  # /json/version's webSocketDebuggerUrl, read at connect
    endpoint: dict[str, Any] = {"host": host, "port": port, "browser_ws": str(ws) if ws else None}
    if host is not None and not _loopback(str(host)):
        endpoint["note"] = (f"{host} is not a loopback address: this browser's DevTools port is "
                            "reached over the network (attach_to_chrome named that host)")
    return endpoint


@mcp.tool()
async def server_status() -> str:
    """Diagnostic info about the server, browser, and CDP compatibility patches.

    `compat_patches`: read it when captures come back empty. A nodriver patch that failed to apply crashes
    nothing but leaves console/network capture empty, which looks like a quiet page.

    `dock_icon`: whether visible browsers get their own Dock icon (macOS): off, beta-ready (they run from
    Chrome Beta with the project icon), beta-missing, icon-missing or unavailable, with the reason. Reading
    it changes nothing.

    `block_ads`: per browser launched with block_ads=True, the requests looked at, blocked and allowed by
    type and host, the targets covered and any that could not be, and the filter lists' age.

    `cdp`: the current browser's DevTools endpoint, {host, port, browser_ws} (its own websocket URL), or
    null with no browser. A host that is not a loopback address carries a note saying so.

    `profile_gc`: the startup pass over leftover privacy-profiles/* and profile-pid<N> folders: what it
    removed, kept because it holds data, found in use, and errors. It never removes a fallback with cookies,
    logins, storage, extensions or bookmarks; `fallback_profiles_left` counts those for profile_report and
    profile_cleanup.
    """
    if BrowserState.browser is not None:
        # A tab the page opened is unknown until asked, which reported one tab too few.
        with contextlib.suppress(Exception):
            await _srv._refresh_tabs()
    status = {
        "version": __version__,
        "session": BrowserState.session_key(),
        "browser_running": BrowserState.is_up(),
        "browser": brave_mode.product_of(BrowserState.browser) if BrowserState.browser is not None else None,
        "instance": BrowserState.current_instance_id,
        "cdp": _cdp_endpoint(BrowserState.browser),
        "tabs": len(BrowserState.tabs),
        "active_tab": BrowserState.active_tab_index,
        "console_capture": BrowserState.capture_console,
        "network_capture": BrowserState.capture_network,
        "console_logs": len(BrowserState.console_logs),
        "network_logs": len(BrowserState.network_logs),
        "network_indexed": len(BrowserState.network_index),
        "page_errors": len(BrowserState.page_errors),
        "intercepting": bool(_capture._INTERCEPT.get("active")),
        "profile_dir": _status_profile(),
        "compat_patches": _patches.status(),
        "remote_allow_origins": await asyncio.to_thread(_srv._allow_origins_by_instance),
        "os_cursor": await _os_cursor_status(),
        "dock_icon": await _srv._dock_icon_status(),
        "dock_icon_brave": await _srv._brave_beta_status(),
        # Each personal setting with where its value came from (env, the settings file, the default):
        # a setting that "does not change" is usually one an environment variable fixes.
        "settings": _srv._settings_report(),
        "brave": {**(await asyncio.to_thread(_srv._brave_status)), "lean_running": _srv._brave_lean_running()},
        "profile_gc": profile_gc.status(),
        "block_ads": (_srv._adblock_if_loaded().status([BrowserState.browser] + [
            snap.browser for snap in BrowserState.instances.values()]) if _srv._adblock_if_loaded()
            else {"browsers": [], "package": adblock_package_note(),
                  "note": "no launch in this process asked for block_ads"}),
        "agent_overlay": {
            "default": "on" if presence.wanted(None) else "off (the agent_overlay setting)",
            "available": presence.unavailable() or "yes",
            "style": presence.style(),
            "change": "the bar's ... menu, or mcp-stealth-chrome settings; see settings",
            "instances": {iid: presence.status(b) for iid, b in [
                (BrowserState.current_instance_id, BrowserState.browser)] + [
                (i, snap.browser) for i, snap in BrowserState.instances.items()] if b is not None},
        },
    }
    return ok(json.dumps(status, indent=2, default=str))


_OS_CURSOR_REMEASURE = 60.0


async def _os_cursor_status() -> dict:
    """Whether solve_turnstile_oscursor could run here, checked without any input.

    Missing the [os-cursor] extra otherwise shows only when the tool is called. Never waits
    long: an answer not ready yet is reported as still being measured, not as a failure.
    """
    if _srv._OS_CURSOR_PROBE["thread"] is None:
        _srv._start_os_cursor_probe()
    thread = _srv._OS_CURSOR_PROBE["thread"]
    if _srv._OS_CURSOR_PROBE["result"] is None and thread is not None:
        await asyncio.to_thread(thread.join, 0.5)  # a warm measurement takes milliseconds
    result, measured_at = _srv._OS_CURSOR_PROBE["result"], _srv._OS_CURSOR_PROBE["measured_at"]
    if result is None:
        waited = time.monotonic() - (_srv._OS_CURSOR_PROBE["started"] or time.monotonic())
        return {"available": None,
                "detail": f"still being measured (started {waited:.1f} s ago); ask again shortly"}
    if measured_at is not None and time.monotonic() - measured_at > _OS_CURSOR_REMEASURE:
        _srv._start_os_cursor_probe()  # permissions can change; this answer stands until the next one
    available, reason = result
    return {"available": bool(available), "detail": reason}


@mcp.tool()
async def get_page_errors() -> str:
    """Uncaught JS exceptions captured since console_start(), WITH the capture status.

    Returns {attached, total, errors}. `attached` false + empty means NOT MONITORED, not
    "no errors": exceptions are only seen on a tab console_start() has armed.
    """
    try:
        tab = BrowserState.active_tab()
    except Exception:
        tab = None
    attached = tab is not None and _srv._target_key(tab) in _srv._CONSOLE_ARMED_TAB_IDS
    payload: dict[str, Any] = {"attached": attached, "total": len(BrowserState.page_errors),
                               "errors": BrowserState.page_errors}
    if not attached:
        payload["note"] = ("NOT listening on the active tab. An empty list here means nothing "
                           "was monitored. Call console_start().")
    return ok(json.dumps(payload, indent=2, default=str))


# Headers Chrome recomputes per connection. Replaying them verbatim is worse
# than omitting them: a stale :authority or a Content-Length that no longer
# matches the body is itself a bot signal.
_CURL_SKIP_HEADERS = {
    "content-length", "host", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "upgrade", "te", "trailer", "expect",
}


def _shq(value: str) -> str:
    """POSIX single-quote a value so it survives the shell verbatim."""
    return "'" + str(value).replace("'", "'\\''") + "'"


# Runs in-page. Gathers the signals bot-detectors actually read, plus the
# cross-field pairs that expose a spoof: detectors flag INCONSISTENCY (a Windows
# UA on a MacIntel platform) far more reliably than any single "bot-like" value.
_FINGERPRINT_AUDIT_JS = r"""
(() => {
  const out = {};
  const nav = navigator;
  out.webdriver = nav.webdriver === undefined ? "undefined" : String(nav.webdriver);
  out.user_agent = nav.userAgent;
  out.platform = nav.platform;
  out.languages = (nav.languages || []).join(",");
  out.language = nav.language;
  out.hardware_concurrency = nav.hardwareConcurrency;
  out.device_memory = nav.deviceMemory === undefined ? null : nav.deviceMemory;
  out.plugins = nav.plugins ? nav.plugins.length : 0;
  out.mime_types = nav.mimeTypes ? nav.mimeTypes.length : 0;
  out.has_chrome_object = typeof window.chrome === "object" && window.chrome !== null;
  out.chrome_runtime = !!(window.chrome && window.chrome.runtime);
  out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.timezone_offset_min = new Date().getTimezoneOffset();
  out.screen = screen.width + "x" + screen.height;
  out.avail = screen.availWidth + "x" + screen.availHeight;
  out.outer = window.outerWidth + "x" + window.outerHeight;
  out.inner = window.innerWidth + "x" + window.innerHeight;
  out.color_depth = screen.colorDepth;
  out.pixel_ratio = window.devicePixelRatio;
  out.touch_points = nav.maxTouchPoints;
  out.pdf_viewer = nav.pdfViewerEnabled === undefined ? null : nav.pdfViewerEnabled;
  out.secure_context = window.isSecureContext;
  out.navigator_brave = typeof nav.brave === "object" && nav.brave !== null;
  out.ua_data = nav.userAgentData
    ? {brands: nav.userAgentData.brands.map(b => b.brand + "/" + b.version),
       platform: nav.userAgentData.platform, mobile: nav.userAgentData.mobile}
    : null;

  try {
    const c = document.createElement("canvas");
    const gl = c.getContext("webgl") || c.getContext("experimental-webgl");
    if (gl) {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      out.webgl_vendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null;
      out.webgl_renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;
    } else { out.webgl_vendor = null; out.webgl_renderer = null; }
  } catch (e) { out.webgl_vendor = null; out.webgl_renderer = null; }

  try {
    // Same input twice: a canvas that changes between draws is a randomiser,
    // which is itself detectable as non-human.
    const draw = () => {
      const c = document.createElement("canvas");
      c.width = 200; c.height = 40;
      const ctx = c.getContext("2d");
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60"; ctx.fillRect(0, 0, 100, 20);
      ctx.fillStyle = "#069"; ctx.fillText("mcp-stealth-audit", 2, 15);
      return c.toDataURL();
    };
    const a = draw(), b = draw();
    out.canvas_stable = a === b;
    out.canvas_hash = a.length + ":" + a.slice(-24);
  } catch (e) { out.canvas_stable = null; }

  try {
    out.permissions_consistent = null;
    if (nav.permissions && nav.permissions.query) {
      out.permissions_query_present = true;
    }
  } catch (e) {}

  // Classic automation leaks
  const leaks = [];
  for (const k of Object.keys(window)) {
    if (/^(cdc_|\$cdc|__webdriver|__selenium|__nightmare|__fxdriver|_phantom|callPhantom|domAutomation)/.test(k)) {
      leaks.push(k);
    }
  }
  out.automation_globals = leaks;
  out.error_stack_has_puppeteer = (() => {
    try { null.f(); } catch (e) {
      return /puppeteer|playwright|nodriver/i.test(String(e.stack || ""));
    }
    return false;
  })();
  return JSON.stringify(out);
})()
"""


_SPEECH_VOICES_JS = """
new Promise((resolve) => {
  const report = () => {
    const v = speechSynthesis.getVoices();
    return JSON.stringify({total: v.length, google: v.filter((x) => x.name.startsWith("Google")).length});
  };
  if (speechSynthesis.getVoices().length) return resolve(report());
  speechSynthesis.addEventListener("voiceschanged", () => resolve(report()), {once: true});
  setTimeout(() => resolve(report()), 2500);
})
"""


@mcp.tool()
async def fingerprint_audit(url: Optional[str] = None) -> str:
    """Audit THIS browser's own fingerprint for the leaks detectors look for.

    Self-diagnostic for the stealth claim: after fingerprint_rotate, behind a
    proxy, or in headless, is this browser still coherent? Runs entirely
    in-page — no third-party detector required, nothing about you leaves the
    machine.

    Reports findings by severity, and is explicit about what it CANNOT see from
    inside the page (TLS/JA3 and IP-vs-timezone need a network peer), so a clean
    report is never mistaken for a total guarantee.

    Args:
        url: navigate here first (e.g. a real target site) so the audit reflects
            that page's context. Omit to audit the current page.

    Weighs cross-field consistency heavily: detectors flag a Windows UA on a
    MacIntel platform far more reliably than any single suspicious value.
    """
    try:
        tab = BrowserState.active_tab()
        if url:
            await _srv._wait(tab.get(url), what="fingerprint_audit navigate")
            await asyncio.sleep(1.0)
        raw = await _srv._wait(tab.evaluate(_FINGERPRINT_AUDIT_JS, return_by_value=True),
                          what="fingerprint_audit")
        raw = raw.value if hasattr(raw, "value") and not isinstance(raw, str) else raw
        fp = json.loads(raw) if raw else {}
        if not fp:
            return err("audit script returned nothing — is a page loaded?")

        critical: list[str] = []
        warnings: list[str] = []
        passed: list[str] = []

        def check(is_ok: bool, label: str, detail: str, severity: str = "critical"):
            if is_ok:
                passed.append(label)
            else:
                (critical if severity == "critical" else warnings).append(
                    f"{label}: {detail}")

        check(fp.get("webdriver") in ("undefined", "false"), "navigator.webdriver",
              f"exposed as {fp.get('webdriver')!r} — the single most-checked flag")
        check(not fp.get("automation_globals"), "automation globals",
              f"leaked {fp.get('automation_globals')}")
        check(not fp.get("error_stack_has_puppeteer"), "error stack",
              "stack traces name an automation library")
        check(bool(fp.get("has_chrome_object")), "window.chrome",
              "missing — real Chrome always defines it")
        check((fp.get("plugins") or 0) > 0, "navigator.plugins",
              "empty plugin array is a headless tell", "warning")
        check(bool(fp.get("languages")), "navigator.languages",
              "empty — real browsers always report at least one", "warning")

        ua = (fp.get("user_agent") or "")
        platform = (fp.get("platform") or "")
        ua_os = ("windows" if "Windows" in ua else
                 "mac" if "Macintosh" in ua or "Mac OS" in ua else
                 "linux" if "Linux" in ua or "X11" in ua else "unknown")
        plat_os = ("windows" if platform.startswith("Win") else
                   "mac" if platform.startswith("Mac") else
                   "linux" if "Linux" in platform else "unknown")
        check(ua_os == plat_os or "unknown" in (ua_os, plat_os),
              "UA vs navigator.platform",
              f"UA says {ua_os}, platform says {plat_os} ({platform!r}) — "
              f"detectors weight this mismatch heavily")

        check("HeadlessChrome" not in ua, "headless marker in UA",
              "the User-Agent announces HeadlessChrome")
        not_checked_hints: list[str] = []
        # Brave is told apart by navigator.brave, which it defines on secure pages, and by the
        # executable this server started. Its farbling is Brave working as intended.
        on_brave = bool(fp.get("navigator_brave")) or brave_mode.product_of(tab) == brave_mode.BRAVE
        own_brand = brave_mode.brand(brave_mode.BRAVE if on_brave else brave_mode.CHROME)
        if on_brave:
            passed.append(
                "Brave farbling: expected, not a leak. Canvas, audio, hardwareConcurrency and "
                "plugins differ per site and per session by design, as in every Brave")
        ua_data = fp.get("ua_data")
        ua_version = chrome_version(ua)
        if ua_version and ua_data is None:
            # userAgentData only exists on secure contexts, so this is unmeasured
            # here rather than clean.
            not_checked_hints.append(
                "Client hints (navigator.userAgentData) — unavailable on this page "
                "because it is not a secure context; audit an https:// page")
        elif ua_version:
            brands = ua_data.get("brands") or []
            check(bool(brands), "userAgentData.brands",
                  "empty on a Chrome UA — real Chrome always lists its brands")
            major = ua_version.split(".")[0]
            chrome_brand = next((b for b in brands if b.startswith(f"{own_brand}/")), None)
            if brands:
                check(chrome_brand == f"{own_brand}/{major}",
                      "UA version vs client hints",
                      f"UA says Chrome {major} on {own_brand}, brands say {brands}")
            if on_brave:
                check(not any(b.startswith("Google Chrome/") for b in brands),
                      "Brave vs client hints",
                      f"brands name Google Chrome ({brands}) while navigator.brave says Brave; "
                      f"no real browser reports both")
            hint_os = {"Windows": "windows", "macOS": "mac", "Linux": "linux"}.get(
                ua_data.get("platform") or "")
            if hint_os:
                check(hint_os == ua_os or ua_os == "unknown", "UA vs client hints platform",
                      f"UA says {ua_os}, userAgentData.platform says {ua_data.get('platform')!r}")

        renderer = (fp.get("webgl_renderer") or "")
        if renderer:
            gpu_os = ("mac" if "Apple" in renderer else
                      "windows" if ("Direct3D" in renderer or "ANGLE" in renderer
                                    and "Apple" not in renderer) else "unknown")
            check(gpu_os == "unknown" or gpu_os == plat_os or plat_os == "unknown",
                  "WebGL renderer vs platform",
                  f"renderer {renderer!r} does not match platform {platform!r}")
        else:
            warnings.append("WebGL renderer: unavailable — cannot cross-check GPU")

        check(fp.get("canvas_stable") is not False, "canvas stability",
              "canvas output changes between identical draws — a randomiser is "
              "itself a detectable signal", "warning")

        # Voices load asynchronously, so they are read apart from the synchronous audit script.
        # Every Google Chrome lists "Google ..." voices from its Network Speech component; light
        # mode turns that component off, and a page reading the list sees them missing.
        voices = None
        with contextlib.suppress(Exception):
            raw_voices = await _srv._wait(tab.evaluate(_SPEECH_VOICES_JS, await_promise=True, return_by_value=True),
                                     timeout=5, what="fingerprint_audit voices")
            raw_voices = raw_voices.value if hasattr(raw_voices, "value") and not isinstance(raw_voices, str) else raw_voices
            voices = json.loads(raw_voices) if raw_voices else None
        if not voices or not voices.get("total"):
            not_checked_hints.append("speechSynthesis voices: the list did not load, so it was not compared")
        else:
            fp["speech_voices"] = voices
            if "Google Chrome" in json.dumps((ua_data or {}).get("brands") or []) or (
                    ua_data is None and "Chrome/" in ua and "Edg/" not in ua and "OPR/" not in ua):
                check(voices.get("google", 0) > 0, "speechSynthesis voices",
                      f"{voices['total']} voices and none from Google; Google Chrome lists its Google "
                      f"voices, so the list reads as a different browser (light mode turns them off)",
                      "warning")

        try:
            ow, oh = (fp.get("outer") or "0x0").split("x")
            check(int(ow) > 0 and int(oh) > 0, "window.outer dimensions",
                  f"{fp.get('outer')} — zeros are a classic headless giveaway")
        except Exception:
            pass
        with contextlib.suppress(Exception):
            ow, oh = (int(v) for v in (fp.get("outer") or "").split("x"))
            sw, sh = (int(v) for v in (fp.get("screen") or "").split("x"))
            aw, ah = (int(v) for v in (fp.get("avail") or "").split("x"))
            if ow > 0 and oh > 0 and sw > 0 and sh > 0:
                check(ow <= sw and oh <= sh, "window vs screen size",
                      f"window {fp.get('outer')} is larger than its screen {fp.get('screen')}; "
                      f"headless Chrome reports an 800x600 screen unless --screen-info sets one",
                      "warning")
            if sw > 0 and sh > 0:
                check(aw <= sw and ah <= sh, "screen.avail vs screen size",
                      f"available area {fp.get('avail')} is larger than the screen {fp.get('screen')}")

        verdict = ("clean" if not critical and not warnings else
                   "warnings only" if not critical else "leaks detected")
        return ok(json.dumps({
            "verdict": verdict,
            "critical": critical,
            "warnings": warnings,
            "passed": passed,
            "fingerprint": fp,
            "not_checked": [
                *not_checked_hints,
                "TLS/JA3 fingerprint — needs a network peer; use "
                "http_request(impersonate=...) against a TLS-echo endpoint",
                "IP vs timezone/locale coherence — needs the exit IP's geo",
                "Behavioural signals (mouse/typing cadence) — see mouse_drift / humanize_*",
            ],
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def export_curl(
    request_id: Optional[str] = None,
    url_contains: Optional[str] = None,
    method: Optional[str] = None,
    include_cookies: bool = True,
    reveal_values: Union[bool, Literal["body"]] = False,
) -> str:
    """Turn a captured browser request into a runnable curl AND an http_request call.

    After network_start() and the action you care about, get the command that reproduces a request outside
    the browser. Use the `http_request` form: anti-bot systems key on plain curl's TLS fingerprint and block
    a request that passed in the browser; http_request(impersonate="chrome") keeps Chrome's JA3. Credentials
    are hidden unless reveal_values=True (tool output lands in the transcript); the http_request form still
    replays the session, since it takes the cookies from the browser.

    Args:
        request_id: id from network_get(). Omit to search by url_contains.
        url_contains: pick the most recent captured request whose URL contains this substring.
        method: also require this HTTP method (e.g. "POST") when searching.
        include_cookies: attach the browser's current cookies for that URL; off gives a shareable command with
            no session in it.
        reveal_values: real cookie and credential values in the output, so the curl string runs as shown.
            "body" shows the request body's values under a secret-named parent; headers, cookies, URL secrets
            and body values whose own key is secret-named stay hidden.

    Returns the matched request, a `curl` string, and an `http_request` call.
    """
    try:
        body_mode = reveal_mode(reveal_values) == "body"
    except ValueError as exc:
        return err(f"export_curl: {exc}; nothing was built")
    # From here on reveal_values means "everything": "body" only changes the body's masking.
    reveal_values = reveal_values is True
    try:
        index = BrowserState.network_index or {}
        if not index:
            return err(
                "no captured requests — call network_start() first, then trigger "
                "the action you want to reproduce, then retry."
            )
        entry = None
        if request_id:
            entry = index.get(str(request_id))
            if entry is None:
                return err(f"request_id {request_id!r} not found among "
                           f"{len(index)} captured requests")
        else:
            if not url_contains:
                return err("pass request_id= or url_contains= to pick a request")
            matches = [
                e for e in index.values()
                if url_contains in (e.get("url") or "")
                and (method is None
                     or (e.get("method") or "").upper() == method.upper())
            ]
            if not matches:
                shown = method.upper() + " " if method else ""
                return err(f"no captured request matching {shown}{url_contains!r} "
                           f"(searched {len(index)})")
            entry = matches[-1]  # most recent wins

        target_url = entry.get("url") or ""
        http_method = (entry.get("method") or "GET").upper()
        headers = {
            k: v for k, v in (entry.get("request_headers") or {}).items()
            if k.lower() not in _CURL_SKIP_HEADERS and not k.startswith(":")
        }
        body = entry.get("request_body")

        cookie_header = None
        for header_name in list(headers):
            if header_name.lower() == "cookie":
                cookie_header = headers.pop(header_name)
                break
        cookie_status = "disabled" if not include_cookies else "captured_header"
        cookie_error = None
        if include_cookies and not cookie_header:
            # The captured headers often omit Cookie (Chrome attaches it later),
            # so ask Chrome for exactly the cookies applicable to this URL.
            # Filtering Storage.getCookies ourselves is unsafe: suffix matching
            # can leak an example.com cookie to notexample.com, and path/secure/
            # partition rules are easy to reproduce incorrectly.
            try:
                tab = BrowserState.active_tab()
                from nodriver.cdp import network as cdp_network
                jar = await tab.send(cdp_network.get_cookies(urls=[target_url]))
                pairs = [f"{c.name}={c.value}" for c in (jar or [])]
                if pairs:
                    cookie_header = "; ".join(pairs)
                    cookie_status = "live_url_jar"
                else:
                    cookie_status = "live_url_jar_empty"
            except Exception as e:
                cookie_status = "error"
                cookie_error = f"{type(e).__name__}: {e}"
        if not include_cookies:
            cookie_header = None

        shown_url = target_url if reveal_values else mask_url(target_url)
        shown_headers = headers if reveal_values else mask_headers(headers)
        shown_body = body if reveal_values else mask_body(body, own_keys_only=body_mode)
        shown_cookie = cookie_header
        if cookie_header and not reveal_values:
            shown_cookie = masked_value(cookie_header)

        parts = ["curl", "-X", http_method, _shq(shown_url)]
        for key, value in shown_headers.items():
            parts += ["-H", _shq(f"{key}: {value}")]
        if shown_cookie:
            parts += ["-H", _shq(f"Cookie: {shown_cookie}")]
        if shown_body:
            parts += ["--data-raw", _shq(shown_body)]
        parts += ["--compressed"]

        replay_headers = dict(shown_headers)
        if cookie_header and reveal_values:
            replay_headers["Cookie"] = cookie_header
        # With cookies hidden the browser supplies them. Otherwise they are in the headers
        # or were declined, and http_request's default would add the session regardless.
        browser_cookies = include_cookies and not reveal_values
        http_call = (
            "http_request(\n"
            f"  url={shown_url!r},\n"
            f"  method={http_method!r},\n"
            f"  headers={json.dumps(replay_headers)},\n"
            + (f"  data={shown_body!r},\n" if shown_body else "")
            + f"  use_browser_cookies={browser_cookies},\n"
            + "  impersonate='chrome',\n)"
        )
        hidden = [k for k, v in shown_headers.items() if v != headers.get(k)]
        if cookie_header and not reveal_values:
            hidden.append("Cookie")
        if shown_body != body:
            hidden.append("(body fields)")
        if shown_url != target_url:
            hidden.append("(query parameters)")

        return ok(json.dumps({
            **({"reveal_values": "body", "reveal_note": BODY_REVEAL_NOTE} if body_mode else {}),
            "request_id": entry.get("request_id"),
            "url": shown_url,
            "method": http_method,
            "status": entry.get("status"),
            "header_count": len(headers),
            "has_body": bool(body),
            "cookies_attached": bool(cookie_header),
            "cookie_status": cookie_status,
            **({"cookie_error": cookie_error} if cookie_error else {}),
            "curl": " ".join(parts),
            "http_request": http_call,
            **({"values_hidden": hidden,
                "hidden_note": ("These read *** here, so the curl string does not run as shown. "
                                "http_request takes the session cookies from the browser; "
                                "reveal_values=True returns every real value.")}
               if hidden else {}),
            "note": (
                "Prefer the http_request form: plain curl carries a curl TLS "
                "fingerprint, which anti-bot systems flag even when every header "
                "matches. impersonate='chrome' replays it with a real Chrome JA3."
            ),
        }, indent=2, default=str))
    except Exception as e:
        return err(str(e))
