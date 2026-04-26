"""Section 24 — DevTools & Testing tools.

- performance_trace_start/stop → JSON trace for chrome://tracing / Perfetto
- performance_metrics / performance_timeline → runtime metrics
- coverage_start/stop → unused JS/CSS %
- memory_heap_snapshot → .heapsnapshot for DevTools Memory panel
- emulate_network / emulate_cpu / emulate_device → throttle / device mode
- web_vitals → LCP/CLS/INP/FCP/TTFB via web-vitals v4
- wait_for_network_idle → robust load detection
- console_clear → reset captured console buffer

All tools call BrowserState.active_tab() and handle missing browser via
the standard ok()/err() pair."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal, Optional

from .._app import mcp
from ..helpers import err, ok, parse_json, ts_filename
from ..state import EXPORT_DIR, BrowserState, ensure_dirs


# Device emulation presets — dimensions match Chrome DevTools device mode.
# width/height are CSS pixels; DPR is devicePixelRatio.
_DEVICE_PRESETS: dict[str, dict] = {
    "iphone-15": {
        "width": 393, "height": 852, "dpr": 3.0, "mobile": True,
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.0 Mobile/15E148 Safari/604.1",
    },
    "iphone-se": {
        "width": 375, "height": 667, "dpr": 2.0, "mobile": True,
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.0 Mobile/15E148 Safari/604.1",
    },
    "pixel-8": {
        "width": 412, "height": 915, "dpr": 2.625, "mobile": True,
        "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Mobile Safari/537.36",
    },
    "galaxy-s23": {
        "width": 360, "height": 780, "dpr": 3.0, "mobile": True,
        "ua": "Mozilla/5.0 (Linux; Android 14; SM-S911B) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Mobile Safari/537.36",
    },
    "ipad": {
        "width": 820, "height": 1180, "dpr": 2.0, "mobile": True,
        "ua": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.0 Mobile/15E148 Safari/604.1",
    },
    "desktop": {
        "width": 1280, "height": 800, "dpr": 1.0, "mobile": False,
        "ua": None,  # let Chrome use real UA
    },
}

# Network preset throughputs in bytes/sec, latency in ms — match DevTools presets.
_NETWORK_PRESETS: dict[str, dict] = {
    "offline": {"offline": True, "latency": 0, "download": 0, "upload": 0},
    "slow-3g": {"offline": False, "latency": 400, "download": 50_000, "upload": 50_000},
    "3g": {"offline": False, "latency": 300, "download": 187_500, "upload": 93_750},
    "slow-4g": {"offline": False, "latency": 150, "download": 180_000, "upload": 90_000},
    "4g": {"offline": False, "latency": 20, "download": 1_500_000, "upload": 750_000},
    "wifi": {"offline": False, "latency": 2, "download": 30_000_000, "upload": 15_000_000},
    "no-throttle": {"offline": False, "latency": 0, "download": -1, "upload": -1},
}

# Single global trace session (CDP Tracing can only have one at a time).
# Holds a reference to the DataCollected handler so we can remove it on stop
# (avoids leaking closures if user starts/stops traces repeatedly).
_TRACE_BUFFER: list[Any] = []
_TRACE_ACTIVE: dict[str, Any] = {
    "tab_id": None, "started_at": 0.0, "categories": "", "handler": None,
}

# Coverage session state — single active session, tagged with tab id so we can
# reject a stop() call that came from a different tab than the start().
_COVERAGE_ACTIVE: dict[str, Any] = {"tab_id": None, "js": False, "css": False}


@mcp.tool()
async def performance_trace_start(
    categories: Optional[str] = None,
    screenshots: bool = False,
) -> str:
    """Start Chrome DevTools performance trace on the active tab.

    Use stop() to save the .json file. Only one trace can be active at a time.

    Args:
        categories: comma-separated trace categories. Default covers DevTools'
            Performance panel view:
              "devtools.timeline,v8.execute,disabled-by-default-devtools.timeline,
               disabled-by-default-devtools.timeline.frame,loading,latencyInfo,
               blink.user_timing"
        screenshots: include screenshot frames in trace (bigger file, lets you
            scrub through frames in DevTools Performance panel)
    """
    if _TRACE_ACTIVE["tab_id"] is not None:
        return err("trace already active — call performance_trace_stop first")
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import tracing as cdp_tracing
        cats = categories or (
            "devtools.timeline,v8.execute,"
            "disabled-by-default-devtools.timeline,"
            "disabled-by-default-devtools.timeline.frame,"
            "loading,latencyInfo,blink.user_timing"
        )
        if screenshots:
            cats += ",disabled-by-default-devtools.screenshot"
        _TRACE_BUFFER.clear()

        def on_data(event):
            try:
                _TRACE_BUFFER.extend(event.value or [])
            except Exception:
                pass

        tab.add_handler(cdp_tracing.DataCollected, on_data)
        await tab.send(cdp_tracing.start(
            categories=cats,
            transfer_mode="ReportEvents",
        ))
        _TRACE_ACTIVE.update({
            "tab_id": id(tab),
            "started_at": time.time(),
            "categories": cats,
            "handler": on_data,
        })
        return ok(f"trace started (categories: {len(cats.split(','))})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def performance_trace_stop(filename: Optional[str] = None) -> str:
    """Stop the active performance trace and save to ~/.mcp-stealth/exports/.

    Output is a JSON array compatible with chrome://tracing and
    DevTools Performance panel (drag-drop to import).
    """
    if _TRACE_ACTIVE["tab_id"] is None:
        return err("no trace active — call performance_trace_start first")
    from nodriver.cdp import tracing as cdp_tracing
    handler = _TRACE_ACTIVE.get("handler")
    try:
        tab = BrowserState.active_tab()
        if id(tab) != _TRACE_ACTIVE["tab_id"]:
            return err(
                "active tab is not the one trace was started on — "
                "switch back with switch_instance / tab_select before stop"
            )
        # Event-based drain: listen for TracingComplete instead of sleep() —
        # reliable on slow machines and large traces.
        complete = asyncio.Event()

        def on_complete(_event):
            complete.set()

        tab.add_handler(cdp_tracing.TracingComplete, on_complete)
        await tab.send(cdp_tracing.end())
        try:
            await asyncio.wait_for(complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            # Fall through with whatever events we've received so far
            pass
        finally:
            try:
                tab.remove_handler(cdp_tracing.TracingComplete, on_complete)
            except Exception:
                pass
            if handler is not None:
                try:
                    tab.remove_handler(cdp_tracing.DataCollected, handler)
                except Exception:
                    pass
        fname = filename or ts_filename("trace", "json")
        path = EXPORT_DIR / fname
        ensure_dirs()
        payload = {"traceEvents": list(_TRACE_BUFFER)}
        path.write_text(json.dumps(payload))
        event_count = len(_TRACE_BUFFER)
        duration = time.time() - _TRACE_ACTIVE["started_at"]
        _TRACE_BUFFER.clear()
        _TRACE_ACTIVE.update({"tab_id": None, "started_at": 0.0,
                               "categories": "", "handler": None})
        return ok(
            f"{path}\ntrace: {event_count} events over {duration:.2f}s "
            f"(drop into chrome://tracing or DevTools Performance panel)"
        )
    except Exception as e:
        _TRACE_ACTIVE.update({"tab_id": None, "started_at": 0.0,
                               "categories": "", "handler": None})
        return err(str(e))


@mcp.tool()
async def performance_metrics() -> str:
    """Return Chrome's runtime Performance metrics (Nodes, JSHeap, FPS, etc).

    Wraps CDP Performance.getMetrics — use it for snapshots during a test run
    (before/after interaction) to detect regressions.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import performance as cdp_perf
        # enable is idempotent in practice; swallow if already enabled
        try:
            await tab.send(cdp_perf.enable())
        except Exception:
            pass
        metrics = await tab.send(cdp_perf.get_metrics())
        # metrics is List[Metric]; each has .name and .value
        lines = [f"{m.name}: {m.value}" for m in metrics]
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def performance_timeline() -> str:
    """Read Navigation/Resource/Paint timing via Performance API.

    Returns TTFB, DOMContentLoaded, load, LCP candidate, FCP, resource count,
    and slowest 5 resources. Fast (no trace capture) — best for smoke tests.
    """
    try:
        tab = BrowserState.active_tab()
        raw = await tab.evaluate(
            r"""
            (() => {
              const nav = performance.getEntriesByType('navigation')[0] || {};
              const paint = performance.getEntriesByType('paint');
              const fcp = paint.find(p => p.name === 'first-contentful-paint');
              const resources = performance.getEntriesByType('resource');
              const slowest = [...resources]
                .sort((a, b) => b.duration - a.duration)
                .slice(0, 5)
                .map(r => ({ url: r.name.slice(0, 120), duration: Math.round(r.duration), size: r.transferSize }));
              return JSON.stringify({
                ttfb_ms: nav.responseStart ? Math.round(nav.responseStart - nav.requestStart) : null,
                dom_content_loaded_ms: nav.domContentLoadedEventEnd ? Math.round(nav.domContentLoadedEventEnd - nav.startTime) : null,
                load_ms: nav.loadEventEnd ? Math.round(nav.loadEventEnd - nav.startTime) : null,
                fcp_ms: fcp ? Math.round(fcp.startTime) : null,
                transfer_size: nav.transferSize || 0,
                encoded_body_size: nav.encodedBodySize || 0,
                decoded_body_size: nav.decodedBodySize || 0,
                resource_count: resources.length,
                slowest_resources: slowest,
              });
            })()
            """,
            return_by_value=True,
        )
        data = parse_json(raw, None)
        if not isinstance(data, dict):
            return err(f"no timeline data: {raw}")
        lines = [
            f"TTFB: {data.get('ttfb_ms')} ms",
            f"FCP:  {data.get('fcp_ms')} ms",
            f"DOMContentLoaded: {data.get('dom_content_loaded_ms')} ms",
            f"load: {data.get('load_ms')} ms",
            f"transfer: {data.get('transfer_size')} bytes "
            f"(encoded {data.get('encoded_body_size')} / decoded {data.get('decoded_body_size')})",
            f"resources: {data.get('resource_count')}",
            "",
            "Slowest 5 resources:",
        ]
        for r in data.get("slowest_resources", []):
            lines.append(f"  {r['duration']:>5} ms  {r['size']:>8} B  {r['url']}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def web_vitals(timeout: float = 8.0) -> str:
    """Collect Core Web Vitals (LCP, CLS, INP, FCP, TTFB) via web-vitals v4.

    Injects Google's official web-vitals library from CDN, listens for each
    metric, returns after all expected metrics fire or timeout elapses.

    Best practice: call after the page has been interacted with (scrolled,
    clicked) so INP and CLS have real signal.
    """
    try:
        tab = BrowserState.active_tab()
        # Inject + wait; web-vitals v4 CDN is jsdelivr/unpkg — resolve once,
        # metrics fire asynchronously as events happen.
        script = r"""
        (async () => {
          if (!window.__mcp_vitals) {
            window.__mcp_vitals = {};
            try {
              const mod = await import('https://unpkg.com/web-vitals@4?module');
              const set = (m) => { window.__mcp_vitals[m.name] = { value: m.value, rating: m.rating }; };
              mod.onLCP(set);
              mod.onCLS(set);
              mod.onINP(set);
              mod.onFCP(set);
              mod.onTTFB(set);
            } catch (e) {
              window.__mcp_vitals.__error = String(e);
            }
          }
          return JSON.stringify(window.__mcp_vitals);
        })()
        """
        # Poll for values up to timeout
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            raw = await tab.evaluate(script, await_promise=True, return_by_value=True)
            last = parse_json(raw, {}) or {}
            if isinstance(last, dict) and "__error" in last:
                return err(f"web-vitals load failed: {last['__error']}")
            # Stop once we have 4+ of the 5 (INP requires interaction)
            if len([k for k in last if not k.startswith("__")]) >= 4:
                break
            await asyncio.sleep(0.5)
        if not last:
            return err("no vitals captured (page needs interaction for INP/CLS)")
        lines = ["Core Web Vitals:"]
        for key in ("LCP", "FCP", "CLS", "INP", "TTFB"):
            m = last.get(key)
            if m is None:
                lines.append(f"  {key}: —")
            else:
                val = m["value"] if isinstance(m, dict) else m
                rating = (m.get("rating") if isinstance(m, dict) else "") or ""
                unit = "" if key == "CLS" else " ms"
                lines.append(f"  {key}: {val:.2f}{unit} ({rating})" if isinstance(val, (int, float))
                             else f"  {key}: {val}{unit} ({rating})")
        return ok("\n".join(lines))
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def emulate_network(
    preset: Literal["offline", "slow-3g", "3g", "slow-4g", "4g", "wifi", "no-throttle"] = "4g",
    latency_ms: Optional[float] = None,
    download_bps: Optional[float] = None,
    upload_bps: Optional[float] = None,
) -> str:
    """Throttle network via CDP Network.emulateNetworkConditions.

    Presets match Chrome DevTools device mode (offline/slow-3g/3g/slow-4g/4g/wifi).
    Pass preset="no-throttle" to reset. Override individual knobs with
    latency_ms / download_bps / upload_bps.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import network as cdp_net
        cfg = dict(_NETWORK_PRESETS[preset])
        if latency_ms is not None:
            cfg["latency"] = float(latency_ms)
        if download_bps is not None:
            cfg["download"] = float(download_bps)
        if upload_bps is not None:
            cfg["upload"] = float(upload_bps)
        await tab.send(cdp_net.enable())
        await tab.send(cdp_net.emulate_network_conditions(
            offline=bool(cfg["offline"]),
            latency=float(cfg["latency"]),
            download_throughput=float(cfg["download"]),
            upload_throughput=float(cfg["upload"]),
        ))
        return ok(
            f"network: preset={preset} offline={cfg['offline']} "
            f"latency={cfg['latency']}ms down={cfg['download']}B/s up={cfg['upload']}B/s"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def emulate_cpu(rate: float = 4.0) -> str:
    """Throttle CPU via CDP Emulation.setCPUThrottlingRate.

    rate=1 is no throttle; rate=4 makes CPU ~4× slower (matches DevTools default
    "4x slowdown"). rate=6 simulates low-end mobile. Pass 1 to reset.
    """
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import emulation as cdp_emu
        await tab.send(cdp_emu.set_cpu_throttling_rate(rate=float(rate)))
        return ok(f"cpu throttling: {rate}× (1=normal, 4=DevTools default, 6=low-end mobile)")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def emulate_device(preset: str = "desktop") -> str:
    """Emulate a device via CDP Emulation.setDeviceMetricsOverride + UA override.

    Presets: iphone-15, iphone-se, pixel-8, galaxy-s23, ipad, desktop (reset).
    Also sets the matching User-Agent string so UA-sniffing backends respond
    with the mobile variant.
    """
    preset_lc = preset.lower().replace("_", "-")
    if preset_lc not in _DEVICE_PRESETS:
        return err(
            f"unknown preset '{preset}'. Options: "
            f"{', '.join(_DEVICE_PRESETS.keys())}"
        )
    try:
        tab = BrowserState.active_tab()
        from nodriver.cdp import emulation as cdp_emu
        from nodriver.cdp import network as cdp_net
        p = _DEVICE_PRESETS[preset_lc]
        if preset_lc == "desktop":
            # Clear all overrides — back to real device
            await tab.send(cdp_emu.clear_device_metrics_override())
            return ok("device: reset to desktop (overrides cleared)")
        await tab.send(cdp_emu.set_device_metrics_override(
            width=int(p["width"]),
            height=int(p["height"]),
            device_scale_factor=float(p["dpr"]),
            mobile=bool(p["mobile"]),
        ))
        if p.get("ua"):
            await tab.send(cdp_net.set_user_agent_override(user_agent=p["ua"]))
        return ok(
            f"device: {preset_lc} {p['width']}×{p['height']} "
            f"DPR={p['dpr']} mobile={p['mobile']}"
        )
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def coverage_start(js: bool = True, css: bool = True) -> str:
    """Start collecting JS/CSS code coverage.

    Wraps CDP Profiler.startPreciseCoverage + CSS.startRuleUsageTracking.
    Call coverage_stop() to get the usage report (bytes used vs unused).
    """
    if _COVERAGE_ACTIVE["tab_id"] is not None:
        return err("coverage already active — call coverage_stop first")
    try:
        tab = BrowserState.active_tab()
        if js:
            from nodriver.cdp import profiler as cdp_prof
            await tab.send(cdp_prof.enable())
            await tab.send(cdp_prof.start_precise_coverage(
                call_count=False, detailed=True,
            ))
        if css:
            from nodriver.cdp import css as cdp_css
            from nodriver.cdp import dom as cdp_dom
            await tab.send(cdp_dom.enable())
            await tab.send(cdp_css.enable())
            await tab.send(cdp_css.start_rule_usage_tracking())
        _COVERAGE_ACTIVE.update({"tab_id": id(tab), "js": js, "css": css})
        return ok(f"coverage tracking started (js={js}, css={css})")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def coverage_stop() -> str:
    """Stop coverage collection and return used/unused breakdown.

    Returns per-file summary: bytes used, bytes total, unused %. Sorted by
    largest unused byte count (biggest dead-code wins first).
    """
    if _COVERAGE_ACTIVE["tab_id"] is None:
        return err("no coverage session — call coverage_start first")
    try:
        tab = BrowserState.active_tab()
        if id(tab) != _COVERAGE_ACTIVE["tab_id"]:
            return err(
                "active tab is not the one coverage was started on — "
                "switch back before calling coverage_stop"
            )
        lines = ["Coverage report:"]
        if _COVERAGE_ACTIVE["js"]:
            from nodriver.cdp import profiler as cdp_prof
            result = await tab.send(cdp_prof.take_precise_coverage())
            # result is Tuple[List[ScriptCoverage], float]
            scripts = result[0] if isinstance(result, tuple) else result
            await tab.send(cdp_prof.stop_precise_coverage())
            js_rows = []
            for sc in scripts or []:
                url = getattr(sc, "url", "") or "<anon>"
                if not url or url.startswith("chrome-extension://"):
                    continue
                total_bytes = 0
                used_bytes = 0
                for func in getattr(sc, "functions", []) or []:
                    for rng in getattr(func, "ranges", []) or []:
                        span = rng.end_offset - rng.start_offset
                        total_bytes += span
                        if rng.count and rng.count > 0:
                            used_bytes += span
                if total_bytes == 0:
                    continue
                unused = total_bytes - used_bytes
                js_rows.append((unused, used_bytes, total_bytes, url))
            js_rows.sort(reverse=True)
            lines.append(f"\nJS ({len(js_rows)} files):")
            for unused, used, total, url in js_rows[:20]:
                pct = (unused / total * 100) if total else 0
                lines.append(
                    f"  unused {unused:>7}B / {total:>7}B ({pct:5.1f}% dead)  {url[:100]}"
                )
        if _COVERAGE_ACTIVE["css"]:
            from nodriver.cdp import css as cdp_css
            result = await tab.send(cdp_css.stop_rule_usage_tracking())
            await tab.send(cdp_css.disable())
            # result is List[RuleUsage]
            css_rows: dict[int, dict[str, int]] = {}
            for ru in result or []:
                sid = ru.style_sheet_id
                row = css_rows.setdefault(sid, {"used": 0, "total": 0})
                span = ru.end_offset - ru.start_offset
                row["total"] += span
                if ru.used:
                    row["used"] += span
            lines.append(f"\nCSS ({len(css_rows)} stylesheets):")
            for sid, row in sorted(css_rows.items(),
                                    key=lambda x: (x[1]["total"] - x[1]["used"]),
                                    reverse=True)[:20]:
                unused = row["total"] - row["used"]
                pct = (unused / row["total"] * 100) if row["total"] else 0
                lines.append(
                    f"  unused {unused:>7}B / {row['total']:>7}B ({pct:5.1f}% dead)  "
                    f"stylesheet-id={sid}"
                )
        _COVERAGE_ACTIVE.update({"tab_id": None, "js": False, "css": False})
        return ok("\n".join(lines))
    except Exception as e:
        _COVERAGE_ACTIVE.update({"tab_id": None, "js": False, "css": False})
        return err(str(e))


@mcp.tool()
async def memory_heap_snapshot(
    filename: Optional[str] = None,
    stable_ms: int = 400,
    max_wait: float = 30.0,
) -> str:
    """Capture a V8 heap snapshot (.heapsnapshot) — drag into DevTools Memory panel.

    Large pages produce 50-200MB snapshots. Saved to ~/.mcp-stealth/exports/.

    Args:
        filename: output name (default timestamped)
        stable_ms: consider snapshot complete after no new chunks for this many ms
        max_wait: hard cap on wait even if chunks keep arriving
    """
    from nodriver.cdp import heap_profiler as cdp_heap
    chunks: list[str] = []
    last_chunk_at = [time.time()]

    def on_chunk(ev):
        try:
            chunks.append(ev.chunk)
            last_chunk_at[0] = time.time()
        except Exception:
            pass

    tab = None
    try:
        tab = BrowserState.active_tab()
        tab.add_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)
        await tab.send(cdp_heap.enable())
        await tab.send(cdp_heap.collect_garbage())
        await tab.send(cdp_heap.take_heap_snapshot(
            report_progress=False,
            treat_global_objects_as_roots=True,
            capture_numeric_value=False,
        ))
        # Drain: wait for no new chunks for `stable_ms` (or hit max_wait).
        deadline = time.time() + max_wait
        while time.time() < deadline:
            idle_ms = (time.time() - last_chunk_at[0]) * 1000
            if chunks and idle_ms >= stable_ms:
                break
            await asyncio.sleep(0.05)
        fname = filename or ts_filename("heap", "heapsnapshot")
        path = EXPORT_DIR / fname
        ensure_dirs()
        path.write_text("".join(chunks))
        size_mb = path.stat().st_size / 1024 / 1024
        return ok(
            f"{path}\nsize: {size_mb:.1f}MB ({len(chunks)} chunks) — "
            f"drag into DevTools → Memory → Load"
        )
    except Exception as e:
        return err(str(e))
    finally:
        if tab is not None:
            try:
                tab.remove_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)
            except Exception:
                pass


@mcp.tool()
async def wait_for_network_idle(
    idle_ms: int = 500,
    timeout: float = 30.0,
) -> str:
    """Wait until no network request has been in-flight for idle_ms.

    More robust than wait_for(selector) for JS-heavy SPAs. Implementation polls
    performance.getEntriesByType('resource') + a custom fetch/XHR tracker
    injected once per tab.
    """
    try:
        tab = BrowserState.active_tab()
        # Install per-tab tracker (idempotent). Uses a per-XHR flag so
        # reused XHR instances (axios, jQuery) don't register duplicate listeners.
        await tab.evaluate(
            r"""
            (() => {
              if (window.__mcp_netidle) return;
              window.__mcp_netidle = { active: 0, last_active: performance.now() };
              const tracker = window.__mcp_netidle;
              const origFetch = window.fetch;
              window.fetch = function(...args) {
                tracker.active++; tracker.last_active = performance.now();
                return origFetch.apply(this, args).finally(() => {
                  tracker.active--; tracker.last_active = performance.now();
                });
              };
              const origSend = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.send = function(...args) {
                // Attach loadend listener exactly once per XHR instance
                // (reused instances would otherwise get N listeners after N opens).
                if (!this.__mcp_netidle_tracked) {
                  this.__mcp_netidle_tracked = true;
                  this.addEventListener('loadend', () => {
                    tracker.active--; tracker.last_active = performance.now();
                  });
                }
                tracker.active++; tracker.last_active = performance.now();
                return origSend.apply(this, args);
              };
            })()
            """,
            return_by_value=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await tab.evaluate(
                "JSON.stringify({"
                "active:(window.__mcp_netidle||{}).active||0,"
                "idle_ms:performance.now()-((window.__mcp_netidle||{}).last_active||0)"
                "})",
                return_by_value=True,
            )
            info = parse_json(raw, {"active": 0, "idle_ms": 0})
            if (isinstance(info, dict) and info.get("active", 0) == 0
                    and info.get("idle_ms", 0) >= idle_ms):
                return ok(
                    f"network idle for {int(info['idle_ms'])}ms "
                    f"(active=0)"
                )
            await asyncio.sleep(0.1)
        return err(f"timeout after {timeout}s — network never idled for {idle_ms}ms")
    except Exception as e:
        return err(str(e))


@mcp.tool()
async def console_clear() -> str:
    """Clear the captured console buffer + call console.clear() in the page."""
    try:
        tab = BrowserState.active_tab()
        # Reset nodriver's console buffer if exposed
        if hasattr(tab, "_console_events"):
            try:
                tab._console_events.clear()  # type: ignore[attr-defined]
            except Exception:
                pass
        await tab.evaluate("console.clear()", return_by_value=True)
        return ok("console cleared")
    except Exception as e:
        return err(str(e))
