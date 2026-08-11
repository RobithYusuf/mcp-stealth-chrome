"""Runtime monkey-patches for upstream bugs in nodriver 0.48.x.

Apply by importing this module once at server startup. Patches are idempotent.

Failure is recorded rather than raised: a broken patch must not stop the server
from starting. But it must not be invisible either — the documented failure mode
is a dead CDP listener, which looks like "the site sent nothing" rather than a
crash. `status()` exposes what applied, and server_status surfaces it.
"""
from __future__ import annotations

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
            "A failed patch means nodriver will raise inside the CDP event "
            "listener, killing it — captures then return empty instead of "
            "erroring. Upgrade/downgrade nodriver if this is non-empty."
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
            return original_from_json(json)

        target.from_json = patched_from_json
        target._mcp_css_patched = True
        _STATUS["client_security_state"] = True
    except Exception as e:  # never block server startup on patch failure
        _STATUS["client_security_state"] = f"{type(e).__name__}: {e}"


def apply_all() -> None:
    _patch_cookie_same_party()
    _patch_client_security_state()
