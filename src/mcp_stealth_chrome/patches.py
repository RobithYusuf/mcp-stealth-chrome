"""Runtime monkey-patches for upstream behaviour in nodriver and FastMCP.

Apply by importing this module once at server startup. Patches are idempotent.

Failure is recorded rather than raised: a broken patch must not stop the server
from starting. But it must not be invisible either — the documented failure mode
is a dead CDP listener, which looks like "the site sent nothing" rather than a
crash. `status()` exposes what applied, and server_status surfaces it.
"""
from __future__ import annotations

import asyncio

# name -> True (applied) | str (why it did not)
_STATUS: dict[str, object] = {}


def status() -> dict:
    """What each compatibility patch did. Read by server_status."""
    failed = {k: v for k, v in _STATUS.items() if v is not True}
    return {
        "applied": sorted(k for k, v in _STATUS.items() if v is True),
        "failed": failed,
        "healthy": not failed,
        "note": (
            "A failed nodriver patch raises inside the CDP event listener, killing it, "
            "so captures return empty instead of erroring. A failed fastmcp_keeps_text "
            "makes text that looks like JSON fail validation. Change the nodriver or mcp "
            "version if this is non-empty."
        ) if failed else None,
    }


def _patch_cookie_same_party() -> None:
    """nodriver 0.48.1 Cookie.from_json crashes when Chrome 147+ omits sameParty.

    Chrome deprecated sameParty tracking and now returns cookies without it,
    causing KeyError in nodriver's CDP connection listener (fatal to the session).
    Patch: use .get() with safe default.
    """
    try:
        from nodriver.cdp import network as cdp_network
        if getattr(cdp_network.Cookie, "_mcp_sameparty_patched", False):
            return

        original_from_json = cdp_network.Cookie.from_json

        @classmethod  # type: ignore[misc]
        def patched_from_json(cls, json):
            # Inject safe defaults for any optional fields Chrome may omit
            json = dict(json)
            json.setdefault("sameParty", False)
            json.setdefault("sourcePort", -1)
            json.setdefault("sourceScheme", "Unset")
            return original_from_json(json)

        cdp_network.Cookie.from_json = patched_from_json
        cdp_network.Cookie._mcp_sameparty_patched = True
        _STATUS["cookie_same_party"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["cookie_same_party"] = f"{type(e).__name__}: {e}"


def _patch_client_security_state() -> None:
    """ClientSecurityState.from_json crashes when Chrome omits fields it once sent.

    Same failure mode as the sameParty cookie bug, one CDP struct over: Chrome
    147+ can emit a ClientSecurityState without `privateNetworkRequestPolicy`,
    nodriver indexes it directly, and the KeyError is raised inside the CDP event
    listener — which kills the listener for the rest of the session, so every
    later network event is silently lost. Observed live while capturing traffic
    on a plain https page.

    Patch: fill in the documented defaults instead of exploding.
    """
    try:
        from nodriver.cdp import network as cdp_network
        target = getattr(cdp_network, "ClientSecurityState", None)
        if target is None or getattr(target, "_mcp_css_patched", False):
            return

        original_from_json = target.from_json

        @classmethod  # type: ignore[misc]
        def patched_from_json(cls, json):
            json = dict(json)
            json.setdefault("initiatorIsSecureContext", False)
            json.setdefault("initiatorIPAddressSpace", "Unknown")
            json.setdefault("privateNetworkRequestPolicy", "Allow")
            json.setdefault("localNetworkAccessRequestPolicy", "Allow")
            return original_from_json(json)

        target.from_json = patched_from_json
        target._mcp_css_patched = True
        _STATUS["client_security_state"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["client_security_state"] = f"{type(e).__name__}: {e}"


def _same_page_url(a: str, b: str) -> bool:
    """Two URLs for the same document, ignoring the fragment and a trailing slash."""
    return a.split("#", 1)[0].rstrip("/") == b.split("#", 1)[0].rstrip("/")


def _patch_tab_get_keeps_session() -> None:
    """Tab.get without a new CDP session and without a fixed wait.

    nodriver 0.50's Tab.get opens a new CDP session after every navigation. Overrides belong
    to a session, so a cross-site navigation drops them: measured as navigator.platform
    returning to the real value while the UA stayed overridden. Navigating without
    re-attaching keeps one session per tab, as up to 0.48.

    Up to 0.48 Tab.get instead ends in `await self`, which is a sleep of half a second on
    every navigation. Page.navigate answers once the navigation has committed, so there is
    nothing left to sleep for; callers that need the load poll readyState themselves.
    """
    try:
        from nodriver.core.connection import Connection
        from nodriver.core.tab import Tab
        if getattr(Tab, "_mcp_get_patched", False):
            return
        from nodriver import cdp

        original_get = Tab.get
        reattaches = hasattr(Connection, "attach")

        async def patched_get(self, url="chrome://welcome", new_tab=False, new_window=False):
            if new_tab or new_window or not self.browser:
                return await original_get(self, url, new_tab, new_window)
            await self.send(cdp.page.navigate(url))
            # attach() (0.50) and the sleep (0.48) were also what refreshed Tab.url. Briefly,
            # since the target only reports the new URL once the navigation commits. A
            # navigation to the URL the tab is already on has nothing to wait for: waiting
            # for the URL to change cost half a second on every reload by URL.
            before = str(getattr(self.target, "url", "") or "")
            for _ in range(10):
                try:
                    info = await self.send(cdp.target.get_target_info(self.target.target_id))
                except Exception:
                    break
                try:
                    self.target = info
                except AttributeError:  # a read-only property over _target, up to 0.48
                    self._target = info
                if info.url and (info.url != before or _same_page_url(info.url, str(url))):
                    break
                await asyncio.sleep(0.05)
            return self

        # Up to 0.48 the class refuses plain attribute assignment through its metaclass.
        type.__setattr__(Tab, "get", patched_get)
        type.__setattr__(Tab, "_mcp_get_patched", True)
        _STATUS["tab_get_no_fixed_wait"] = True
        if reattaches:
            _STATUS["tab_get_keeps_session"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["tab_get_keeps_session"] = f"{type(e).__name__}: {e}"


def _patch_fastmcp_keeps_text() -> None:
    """FastMCP parses a string as JSON unless the parameter is annotated exactly `str`.

    So http_request(data='{"a":1}') reached validation as a dict and failed, on all 154
    Optional[str] parameters, and 'null' silently became None: wait_for(text='null') slept
    instead of waiting. A parameter that accepts text keeps the text the caller sent.
    """
    try:
        import types
        import typing
        from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
        if getattr(FuncMetadata, "_mcp_keeps_text", False):
            return

        def takes_text(annotation) -> bool:
            if typing.get_origin(annotation) not in (typing.Union, types.UnionType):
                return annotation is str
            members = [a for a in typing.get_args(annotation) if a is not type(None)]
            # A union that also takes a list or dict still wants JSON parsed for it.
            return str in members and not any(
                typing.get_origin(a) in (list, dict) or a in (list, dict) for a in members)

        original = FuncMetadata.pre_parse_json

        def pre_parse_json(self, data):
            parsed = original(self, data)
            for name, field in self.arg_model.model_fields.items():
                if takes_text(field.annotation):
                    for key in (name, field.alias):
                        if key in data and isinstance(data[key], str):
                            parsed[key] = data[key]
            return parsed

        FuncMetadata.pre_parse_json = pre_parse_json
        FuncMetadata._mcp_keeps_text = True
        _STATUS["fastmcp_keeps_text"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["fastmcp_keeps_text"] = f"{type(e).__name__}: {e}"


def _patch_transaction_late_reply() -> None:
    """nodriver up to 0.48 dies on a reply to a call nobody is waiting for any more.

    A Transaction is a Future, and asyncio.wait_for cancels it on timeout. When Chrome answers
    later, set_result raises InvalidStateError inside the CDP listener and kills it, so every
    later call on that tab times out. One slow evaluate wedged the whole browser. Absent from
    0.50, where Transaction is no longer a Future and there is nothing to patch.
    """
    try:
        from nodriver.core.connection import Transaction
        if not issubclass(Transaction, asyncio.Future) or getattr(Transaction, "_mcp_late_reply", False):
            return
        original_call = Transaction.__call__

        def patched_call(self, **response):
            if self.done():
                return None
            return original_call(self, **response)

        Transaction.__call__ = patched_call
        Transaction._mcp_late_reply = True
        _STATUS["transaction_late_reply"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["transaction_late_reply"] = f"{type(e).__name__}: {e}"


async def _refresh_nobody_awaits(coro):
    try:
        return await coro
    except Exception:
        return None  # the browser went; the next call that needs the list asks again


def _patch_background_target_refresh() -> None:
    """nodriver starts update_targets() as a task nobody awaits, on every target event and in
    Browser.wait and Tab.sleep. When the browser has gone (each target of a closing browser
    sends one), it fails with ConnectionRefusedError, asyncio logs "Task exception was never
    retrieved", and FastMCP's Rich handler renders that traceback on the event loop: 1.1 to
    1.3 s each on the Windows runner, several per close. The idle reaper read the stall as
    idleness and closed a freshly spawned instance. Those tasks now end quietly; an
    update_targets that is awaited (_refresh_tabs) still raises.
    """
    try:
        import types

        from nodriver.core import browser as nd_browser
        from nodriver.core import tab as nd_tab

        class _Asyncio(types.ModuleType):
            def __getattr__(self, name):
                return getattr(asyncio, name)

            @staticmethod
            def create_task(coro, *args, **kwargs):
                if getattr(coro, "__qualname__", "").endswith("Browser.update_targets"):
                    coro = _refresh_nobody_awaits(coro)
                return asyncio.create_task(coro, *args, **kwargs)

        for module in (nd_browser, nd_tab):
            current = getattr(module, "asyncio", None)
            if getattr(current, "_mcp_shim", False):
                continue
            if current is not asyncio:
                raise RuntimeError(f"{module.__name__} has no asyncio module to wrap")
            shim = _Asyncio("asyncio")
            shim._mcp_shim = True
            module.asyncio = shim
        _STATUS["background_target_refresh"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["background_target_refresh"] = f"{type(e).__name__}: {e}"


def apply_all() -> None:
    _patch_fastmcp_keeps_text()
    _patch_background_target_refresh()
    _patch_transaction_late_reply()
    _patch_cookie_same_party()
    _patch_client_security_state()
    _patch_tab_get_keeps_session()
