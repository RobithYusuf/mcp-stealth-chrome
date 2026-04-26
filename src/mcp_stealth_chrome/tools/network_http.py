"""Section 22 — Dual-mode HTTP (curl_cffi TLS-perfect) + behavioral helpers.

UNIQUE in MCP ecosystem — combine browser (for login/rendering) with
curl_cffi (for high-volume API scraping with real browser JA3/JA4 fingerprint).

Tools:
- http_request: TLS-perfect HTTP via curl_cffi
- http_session_cookies: inspect which browser cookies a request would send
- session_warmup: visit homepage / referer chain / natural-browse before target
- detect_anti_bot: classify Cloudflare / DataDome / PerimeterX / Akamai / etc.

Internal helpers re-exported by server.py for backward compat:
- _http_session_state
- _get_browser_cookies_for_url
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Optional

from .._app import mcp
from ..helpers import err, get_url, ok, parse_json
from ..humanize import humanized_move
from ..state import BrowserState


_http_session_state: dict[str, Any] = {"cookies": [], "last_origin": None}


async def _get_browser_cookies_for_url(url: str) -> list[dict]:
    """Extract cookies from browser that apply to URL."""
    if not BrowserState.browser:
        return []
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        target_host = parsed.hostname or ""
        all_cookies = await BrowserState.browser.cookies.get_all()
        matching = []
        for c in all_cookies:
            domain = (c.domain or "").lstrip(".")
            if target_host == domain or target_host.endswith("." + domain):
                matching.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                })
        return matching
    except Exception:
        return []


@mcp.tool()
async def http_request(
    url: str,
    method: str = "GET",
    impersonate: str = "chrome",
    use_browser_cookies: bool = True,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[str] = None,
    json_body: Optional[dict] = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    return_mode: str = "auto",
) -> str:
    """HTTP request with TLS-perfect browser fingerprint via curl_cffi.
    Use for API scraping after browser login — same stealth as real Chrome's JA3/JA4.

    Args:
        url, method: target URL and HTTP verb
        impersonate: chrome, chrome124, firefox, safari, edge (default chrome)
        use_browser_cookies: auto-inject cookies from active browser tab
        headers, params: extra headers/query params
        data: raw body string (form-urlencoded or custom)
        json_body: JSON body dict (sets Content-Type automatically)
        timeout, follow_redirects: usual HTTP options
        return_mode: auto (json if parseable else text), json, text, or meta (status+headers only)
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return err("curl-cffi not installed — pip install curl-cffi")

    cookies_dict = {}
    if use_browser_cookies:
        browser_cookies = await _get_browser_cookies_for_url(url)
        for c in browser_cookies:
            cookies_dict[c["name"]] = c["value"]

    hdrs = dict(headers or {})

    try:
        async with AsyncSession(impersonate=impersonate) as session:
            kwargs = {
                "timeout": timeout,
                "allow_redirects": follow_redirects,
            }
            if params:
                kwargs["params"] = params
            if hdrs:
                kwargs["headers"] = hdrs
            if cookies_dict:
                kwargs["cookies"] = cookies_dict
            if data is not None:
                kwargs["data"] = data
            if json_body is not None:
                kwargs["json"] = json_body

            resp = await session.request(method.upper(), url, **kwargs)

            body_text = ""
            if return_mode != "meta":
                try:
                    body_text = resp.text
                except Exception:
                    body_text = "<binary>"
                if len(body_text) > 20_000:
                    body_text = body_text[:20_000] + f"\n\n[truncated — full size {len(resp.content)} bytes]"

            elapsed_ms = None
            if hasattr(resp, "elapsed") and resp.elapsed is not None:
                try:
                    # curl_cffi returns float seconds OR timedelta depending on version
                    e = resp.elapsed
                    elapsed_ms = int(e.total_seconds() * 1000) if hasattr(e, "total_seconds") else int(float(e) * 1000)
                except Exception:
                    elapsed_ms = None
            meta = {
                "status": resp.status_code,
                "url": str(resp.url),
                "elapsed_ms": elapsed_ms,
                "headers": dict(resp.headers),
                "cookies_sent": len(cookies_dict),
                "impersonate": impersonate,
            }

            if return_mode == "json":
                try:
                    return ok(json.dumps({"meta": meta, "body": resp.json()}, indent=2, default=str)[:25000])
                except Exception:
                    return ok(json.dumps({"meta": meta, "body_text": body_text}, indent=2, default=str)[:25000])
            if return_mode == "text":
                return ok(f"{json.dumps(meta, indent=2, default=str)}\n\n--- BODY ---\n{body_text}")
            if return_mode == "meta":
                return ok(json.dumps(meta, indent=2, default=str))
            # auto mode
            try:
                parsed = resp.json()
                return ok(json.dumps({"meta": meta, "body": parsed}, indent=2, default=str)[:25000])
            except Exception:
                return ok(f"{json.dumps(meta, indent=2, default=str)}\n\n--- BODY (text) ---\n{body_text}")
    except Exception as e:
        return err(f"http_request: {e}")


@mcp.tool()
async def http_session_cookies(url: str) -> str:
    """⭐ Inspect which browser cookies would be sent with a request to URL.

    Helpful to verify session sharing works before making requests.
    """
    cookies = await _get_browser_cookies_for_url(url)
    return ok(json.dumps({
        "url": url,
        "count": len(cookies),
        "cookies": [{"name": c["name"], "domain": c["domain"], "path": c["path"]} for c in cookies],
    }, indent=2))


@mcp.tool()
async def session_warmup(
    target_url: str,
    pattern: Literal["homepage_first", "referer_chain", "natural_browse"] = "homepage_first",
    dwell_seconds: float = 2.0,
) -> str:
    """Warm up session by navigating naturally before hitting target URL.
    Anti-bot systems score trust by session history — direct deep-URL hits look suspicious.

    Patterns:
      - homepage_first: goto origin → wait → goto target
      - referer_chain: goto origin → find link to target → click
      - natural_browse: homepage → scroll → random click → scroll → target
    """
    # Late import: mouse_drift lives in server.py (precision mouse section).
    # By call time, server.py module is fully initialized.
    from ..server import mouse_drift  # noqa: E402
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        tab = BrowserState.active_tab()
        actions = []

        if pattern == "homepage_first":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds)
            # mouse drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=3)
            actions.append("mouse drifted")
            await tab.get(target_url)
            actions.append(f"navigated to {target_url}")

        elif pattern == "referer_chain":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds)
            # find link that leads closer to target
            link_data = await tab.evaluate(
                f"""
                (() => {{
                  const t = {json.dumps(target_url)};
                  const links = Array.from(document.querySelectorAll('a[href]'));
                  const hit = links.find(a => t.startsWith(a.href) || a.href.includes(new URL(t).pathname.split('/')[1] || ''));
                  if (!hit) return null;
                  const r = hit.getBoundingClientRect();
                  return JSON.stringify({{
                    href: hit.href,
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    visible: r.width > 0 && r.height > 0,
                  }});
                }})()
                """,
                return_by_value=True,
            )
            ldata = parse_json(link_data, None)
            if isinstance(ldata, dict) and ldata.get("visible"):
                await humanized_move(tab, ldata["x"] + 100, ldata["y"] - 50,
                                      ldata["x"], ldata["y"])
                await tab.mouse_click(ldata["x"], ldata["y"])
                actions.append(f"clicked link to {ldata['href']}")
                await asyncio.sleep(dwell_seconds)
                # if still not at target, nav directly
                cur = await get_url(tab)
                if target_url not in cur:
                    await tab.get(target_url)
                    actions.append(f"direct nav to {target_url}")
            else:
                await tab.get(target_url)
                actions.append(f"no link found, direct nav to {target_url}")

        elif pattern == "natural_browse":
            await tab.get(origin)
            actions.append(f"visited {origin}")
            await asyncio.sleep(dwell_seconds / 2)
            # scroll
            await tab.evaluate("window.scrollBy(0, 400)", return_by_value=True)
            actions.append("scrolled 400px")
            await asyncio.sleep(dwell_seconds / 2)
            # drift
            await mouse_drift(duration_seconds=dwell_seconds, segments=4)
            actions.append("mouse drifted")
            # random visible link click
            rand_link = await tab.evaluate(
                """
                (() => {
                  const links = Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                      const r = a.getBoundingClientRect();
                      return r.width > 30 && r.height > 10 && a.href.startsWith(location.origin);
                    });
                  if (links.length === 0) return null;
                  const pick = links[Math.floor(Math.random() * Math.min(links.length, 5))];
                  const r = pick.getBoundingClientRect();
                  return JSON.stringify({href: pick.href, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)});
                })()
                """,
                return_by_value=True,
            )
            rdata = parse_json(rand_link, None)
            if isinstance(rdata, dict):
                await humanized_move(tab, rdata["x"] + 150, rdata["y"] - 60,
                                      rdata["x"], rdata["y"])
                await tab.mouse_click(rdata["x"], rdata["y"])
                actions.append(f"random click → {rdata['href']}")
                await asyncio.sleep(dwell_seconds)
                await tab.evaluate("window.scrollBy(0, 300)", return_by_value=True)
                actions.append("scrolled on intermediate page")
                await asyncio.sleep(dwell_seconds / 2)
            await tab.get(target_url)
            actions.append(f"final nav to {target_url}")

        return ok("session warmup complete:\n  " + "\n  ".join(actions))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def detect_anti_bot() -> str:
    """⭐ Analyze current page + HTTP headers to identify anti-bot system.

    Detects: Cloudflare, DataDome, PerimeterX/HUMAN, Akamai Bot Manager,
    Kasada, Imperva/Incapsula, F5 Shape, none. Returns system + recommended
    bypass strategy from our toolkit.
    """
    try:
        if not BrowserState.is_up():
            return err("browser_launch first")
        tab = BrowserState.active_tab()

        # JS probes — look for telltale script names, objects, cookies
        probes = await tab.evaluate(
            """
            (() => {
              const out = {};
              // Cookies (accessible from JS if not HttpOnly)
              out.cookies = document.cookie;
              // Page HTML signature
              out.html_head = document.documentElement.outerHTML.slice(0, 5000);
              // Known globals
              out.has_turnstile = !!window.turnstile;
              out.has_grecaptcha = !!window.grecaptcha;
              out.has_hcaptcha = !!window.hcaptcha;
              out.has_px = !!window._pxAppId || !!window._pxCID;
              out.has_kasada = !!window.KPSDK;
              out.has_imperva = !!window._impervasecure;
              return JSON.stringify(out);
            })()
            """,
            return_by_value=True,
        )
        data = parse_json(probes, {})
        cookies = str(data.get("cookies", ""))
        html = str(data.get("html_head", ""))

        detections = []
        strategies = []

        # Cloudflare signatures
        if ("__cf_bm" in cookies or "cf_clearance" in cookies or
            "cdn-cgi" in html or "challenges.cloudflare.com" in html or
            "cf-beacon" in html or data.get("has_turnstile")):
            detections.append("Cloudflare")
            strategies.append("click_turnstile() or verify_cf() for challenges")
            strategies.append("http_request(impersonate='chrome') for API calls")

        # DataDome
        if "datadome" in cookies.lower() or "dd_s" in cookies or "datadome" in html.lower():
            detections.append("DataDome")
            strategies.append("⚠️ DataDome is HARDEST — use mouse_drift + session_warmup + residential proxy")
            strategies.append("mouse_record + mouse_replay of real human session")

        # PerimeterX / HUMAN
        if (data.get("has_px") or "_px" in cookies or "perimeterx" in html.lower() or
            "_pxhd" in cookies):
            detections.append("PerimeterX/HUMAN")
            strategies.append("storage_state_load (session reuse) is most reliable")
            strategies.append("mouse_behavior_profile + mobile proxy for new sessions")

        # Akamai Bot Manager
        if ("_abck" in cookies or "bm_sz" in cookies or "akamai" in html.lower() or
            "ak-bm-api" in html):
            detections.append("Akamai Bot Manager")
            strategies.append("http_request with impersonate='chrome' for TLS match")
            strategies.append("session_warmup(pattern='natural_browse')")

        # Kasada
        if data.get("has_kasada") or "kpsdk" in html.lower() or "kasada" in html.lower():
            detections.append("Kasada")
            strategies.append("⚠️ Kasada is VERY HARD — requires residential proxy + real browser")
            strategies.append("consider CapSolver or commercial service for this target")

        # Imperva / Incapsula
        if data.get("has_imperva") or "incap_ses" in cookies or "visid_incap" in cookies:
            detections.append("Imperva/Incapsula")
            strategies.append("http_request + cookie persistence after warmup")

        # reCAPTCHA / hCaptcha presence
        if data.get("has_grecaptcha"):
            detections.append("reCAPTCHA (v2 or v3)")
            strategies.append("solve_recaptcha_ai() (Claude vision) or solve_captcha(kind='recaptcha_v2')")
        if data.get("has_hcaptcha"):
            detections.append("hCaptcha")
            strategies.append("solve_captcha(kind='hcaptcha', ...) via CapSolver")

        if not detections:
            detections.append("none detected")
            strategies.append("proceed with normal automation — site has no/low anti-bot")

        return ok(json.dumps({
            "detected": detections,
            "recommended_tools": strategies,
            "cookies_found": [c.split("=")[0].strip() for c in cookies.split(";") if "=" in c][:20],
        }, indent=2))
    except Exception as e:
        return err(str(e))
