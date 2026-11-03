"""Optional HTTP transport: one server, many agents, one session per Mcp-Session-Id.

This server drives browsers that are logged in to real accounts, so anyone who can
reach it can act as that person. Hence a bearer token is always required, it binds
to loopback unless told otherwise, and the MCP SDK's Host/Origin checks stay on to
block DNS rebinding from a web page. stdio remains the default and is unaffected.
"""
from __future__ import annotations

import hmac
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any, Optional

from .state import HOME, set_session_liveness

TOKEN_ENV = "MCP_STEALTH_TOKEN"
TOKEN_FILE = HOME / ".mcp-stealth" / "http-token"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def load_or_create_token(path: Optional[Path] = None) -> tuple[str, str]:
    """Return (token, where it came from). The token itself is never printed.

    A new one is created with O_EXCL and mode 0600, so it cannot be readable by
    anyone else even for a moment. An existing file that other users can read is
    refused rather than quietly trusted.
    """
    import secrets

    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env, f"environment variable {TOKEN_ENV}"
    path = path or TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{path} is readable by other users (mode {mode:o}). "
                f"Run: chmod 600 {path}"
            ) from None
        token = path.read_text().strip()
        if not token:
            raise ValueError(f"{path} is empty; delete it to generate a new token") from None
        return token, f"file {path}"
    with os.fdopen(fd, "w") as handle:
        token = secrets.token_urlsafe(32)
        handle.write(token + "\n")
    return token, f"new file {path}"


class BearerAuth:
    """Pure ASGI wrapper. BaseHTTPMiddleware would buffer the MCP event stream."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                supplied = value
                break
        scheme, _, credential = supplied.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(credential.strip(), self._token):
            body = b'{"error":"unauthorized","detail":"send Authorization: Bearer <token>"}'
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_app(
    mcp: Any, token: str, host: str = "127.0.0.1", port: int = 8765,
    allowed_hosts: Optional[list[str]] = None,
) -> Any:
    """The authenticated ASGI app. Settings are read lazily by the SDK, so set first."""
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.host = host
    mcp.settings.port = port
    if host not in _LOOPBACK or allowed_hosts:
        # The SDK only arms its allowlist for loopback hosts at construction. A remote
        # bind needs the names clients use added, not the check switched off.
        names = [n for n in (allowed_hosts or []) if n]
        if host not in _LOOPBACK and host not in ("0.0.0.0", "::"):
            names.append(f"[{host}]" if ":" in host else host)
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        for name in names:
            hosts += [name, f"{name}:*"]
            origins += [f"http://{name}", f"http://{name}:*", f"https://{name}", f"https://{name}:*"]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    return BearerAuth(mcp.streamable_http_app(), token)


def serve(
    mcp: Any, host: str, port: int, allow_remote: bool,
    allowed_hosts: Optional[list[str]] = None,
) -> int:
    """Run until interrupted. Returns the process exit code."""
    import uvicorn

    if host not in _LOOPBACK and not allow_remote:
        print(
            f"refusing to listen on {host}: this server controls logged-in browsers. "
            f"Use an SSH tunnel, or pass --allow-remote if you really mean it "
            f"(the token then travels unencrypted unless a TLS proxy fronts it).",
            file=sys.stderr,
        )
        return 2
    if host in ("0.0.0.0", "::") and not allowed_hosts:
        print(
            f"--host {host} accepts any address, so every request would fail the Host "
            f"check. Name what clients will use with --allowed-host (repeatable), "
            f"such as your LAN address or the proxy's hostname.",
            file=sys.stderr,
        )
        return 2
    try:
        token, source = load_or_create_token()
    except (OSError, ValueError) as exc:
        print(f"cannot load the access token: {exc}", file=sys.stderr)
        return 2
    print(
        f"mcp-stealth-chrome: MCP over HTTP on http://{host}:{port}/mcp\n"
        f"  token: {source} (send as Authorization: Bearer <token>)",
        file=sys.stderr,
    )
    # uvicorn re-raises the signal after shutting down, and the default SIGTERM action
    # would end the process before the caller could stop its browsers.
    def _exit(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    app = build_app(mcp, token, host, port, allowed_hosts)
    set_session_liveness(lambda sid: sid in getattr(
        mcp.session_manager, "_server_instances", {sid: None}))
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
