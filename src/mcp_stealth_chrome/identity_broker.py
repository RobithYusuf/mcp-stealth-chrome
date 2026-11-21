"""Apply the launch identity to every headless Chrome target.

A nodriver Tab override only covers the page session. Headless Chrome creates workers,
service workers and out-of-process frames with separate CDP sessions, so this small
browser-level connection holds each target before it runs and applies the same Network
and Emulation overrides. It is deliberately used only for headless launches: headed
browsers keep their existing launch path unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any, Optional

from .identity import (default_accept_language, navigator_languages, navigator_platform,
                       os_family, strip_headless, ua_metadata_fields)


class IdentityBroker:
    """Auto-attach target sessions and apply one resolved identity to each of them."""

    def __init__(self, websocket_url: str, fingerprint: dict[str, str], product: str):
        self.websocket_url = websocket_url
        self.fingerprint = dict(fingerprint or {})
        self.product = product
        self.ws: Any = None
        self.reader: Optional[asyncio.Task] = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._setups: set[asyncio.Task] = set()
        self._waiting: set[str] = set()
        self._ended = False
        self._version: Optional[dict[str, str]] = None
        self.errors: list[str] = []

    async def _send(self, method: str, params: Optional[dict] = None,
                    session_id: Optional[str] = None, timeout: float = 10.0) -> dict:
        msg_id = self._next_id
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        await self.ws.send(json.dumps(message))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def _send_nowait(self, method: str, params: Optional[dict] = None,
                           session_id: Optional[str] = None) -> None:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self.ws.send(json.dumps(message))

    async def start(self) -> None:
        try:
            from websockets.asyncio.client import connect  # type: ignore[import-not-found]
        except ImportError:
            from websockets import connect  # type: ignore[no-redef]
        self.ws = await asyncio.wait_for(
            connect(self.websocket_url, max_size=None, ping_interval=None, open_timeout=10), 15)
        self.reader = asyncio.create_task(self._read())
        version = await self._send("Browser.getVersion")
        result = version.get("result") or {}
        self._version = {"product": str(result.get("product") or ""),
                         "userAgent": str(result.get("userAgent") or "")}
        reply = await self._send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
        })
        if "error" in reply:
            raise RuntimeError(f"Target.setAutoAttach: {(reply['error'] or {}).get('message')}")
        deadline = asyncio.get_running_loop().time() + 5
        while self._setups and asyncio.get_running_loop().time() < deadline:
            await asyncio.wait(list(self._setups), timeout=max(0.0, deadline - asyncio.get_running_loop().time()))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if message.get("method") == "Target.attachedToTarget":
                    task = asyncio.create_task(self._on_attached(message.get("params") or {}))
                    self._setups.add(task)
                    task.add_done_callback(self._setups.discard)
                elif message.get("method") == "Target.detachedFromTarget":
                    self._waiting.discard(str((message.get("params") or {}).get("sessionId") or ""))
        except asyncio.CancelledError:
            return
        except Exception:
            # The browser close path owns the final result. A dead sidecar must not
            # turn a successful page action into an event-loop exception.
            return
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError("identity broker connection closed"))
            self._pending.clear()

    def _override(self) -> Optional[dict[str, Any]]:
        if self._version is None:
            return None
        real = self._version["userAgent"]
        product = "Brave" if self.product == "brave" else "Google Chrome"
        requested = self.fingerprint.get("user_agent") or ""
        language = self.fingerprint.get("accept_language") or ""
        platform = self.fingerprint.get("platform") or ""
        native = strip_headless(real)
        if requested:
            user_agent = requested
            if not platform:
                platform = navigator_platform(os_family(user_agent)) or ""
        else:
            user_agent = native
        needs_cleanup = not requested and native != real
        if not (requested or language or platform or needs_cleanup):
            return None
        kwargs: dict[str, Any] = {"userAgent": user_agent}
        if language:
            tags = navigator_languages(language)
            kwargs["acceptLanguage"] = ",".join(tags)
        if platform:
            kwargs["platform"] = platform
        version_match = re.search(r"(?:Headless)?Chrome/(\d+\.\d+\.\d+\.\d+)", real)
        real_version = version_match.group(1) if version_match else None
        hints = ua_metadata_fields(user_agent, platform, real_version, product=product)
        if hints:
            kwargs["userAgentMetadata"] = hints
        return kwargs

    async def _on_attached(self, params: dict) -> None:
        sid = str(params.get("sessionId") or "")
        if not sid:
            return
        if params.get("waitingForDebugger"):
            self._waiting.add(sid)
        try:
            override = self._override()
            if override is not None:
                reply = await self._send("Network.setUserAgentOverride", override, sid)
                if "error" in reply:
                    raise RuntimeError((reply["error"] or {}).get("message") or "override refused")
                language = self.fingerprint.get("accept_language") or ""
                if language:
                    tags = navigator_languages(language)
                    wanted = ",".join(p.strip() for p in language.split(",") if p.strip())
                    if wanted != default_accept_language(tags):
                        await self._send("Network.enable", {}, sid)
                        await self._send("Network.setExtraHTTPHeaders",
                                         {"headers": {"Accept-Language": wanted}}, sid)
            timezone = self.fingerprint.get("timezone")
            if timezone:
                reply = await self._send("Emulation.setTimezoneOverride",
                                         {"timezoneId": timezone}, sid)
                if "error" in reply:
                    raise RuntimeError((reply["error"] or {}).get("message") or "timezone refused")
        except Exception as exc:
            self.errors.append(f"{params.get('targetInfo', {}).get('type')}: {type(exc).__name__}: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await self._send_nowait("Runtime.runIfWaitingForDebugger", {}, sid)
            self._waiting.discard(sid)

    async def stop(self) -> None:
        if self._ended:
            return
        self._ended = True
        for sid in list(self._waiting):
            with contextlib.suppress(Exception):
                await self._send_nowait("Runtime.runIfWaitingForDebugger", {}, sid)
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self._send_nowait("Target.setAutoAttach", {
                    "autoAttach": False, "waitForDebuggerOnStart": False,
                })
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(), 1)
        if self.reader is not None and not self.reader.done():
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.ws = None
