"""One session per MCP connection, and the security of the HTTP transport.

Connection tests use real MCP client sessions, in memory or over real HTTP against
a subprocess whose HOME is temporary. Real-Chrome tests need MCP_E2E=1.
"""
import asyncio
import json
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session as connect

from mcp_stealth_chrome import http_transport, state
from mcp_stealth_chrome.server import mcp as server_mcp
from mcp_stealth_chrome.state import BrowserState, InstanceSnapshot

TOKEN = "unit-test-token-value"
E2E = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")


@pytest.fixture(autouse=True)
def fresh_sessions():
    """In-memory clients take the primary state, so put it back after each test."""
    yield
    for key in list(state._SESSIONS):
        if key not in (state.PRIMARY_SESSION, state.POOL_SESSION):
            state._SESSIONS.pop(key)
    state._SESSIONS[state.PRIMARY_SESSION] = state._SessionState()
    state._SESSIONS[state.POOL_SESSION].instances.clear()
    state._HTTP_SESSIONS.clear()
    state._primary_taken = False
    state.set_session_liveness(None)


def text(result):
    return result.content[0].text


async def call(session, name, **kwargs):
    return text(await session.call_tool(name, kwargs))


async def instances(session):
    return json.loads(await call(session, "list_instances"))


# ── Routing and ownership, over real MCP sessions in memory ───────────────


def test_each_connection_has_its_own_state_and_ownership_is_enforced():
    async def scenario():
        async with connect(server_mcp._mcp_server) as a, connect(server_mcp._mcp_server) as b:
            a_info, b_info = await instances(a), await instances(b)
            a_key, b_key = a_info["session"], b_info["session"]
            assert a_key != b_key
            a_current = next(i for i in a_info["instances"] if i["current"])["instance_id"]
            b_current = next(i for i in b_info["instances"] if i["current"])["instance_id"]
            assert a_current != b_current

            # Switching in one session leaves the other where it was.
            assert "switched" in await call(a, "switch_instance", instance_id="a-only")
            again = await instances(b)
            assert next(i for i in again["instances"] if i["current"])["instance_id"] == b_current

            for tool_name, kwargs in (
                ("switch_instance", {"instance_id": "a-only"}),
                ("close_instance", {"instance_id": "a-only"}),
                ("instance_describe", {"instance_id": "a-only", "desc": "x"}),
                ("spawn_browser", {"instance_id": "a-only"}),
                ("instance_release", {"instance_id": "a-only"}),
                ("attach_to_chrome", {"instance_id": "a-only", "port": 1}),
            ):
                out = await call(b, tool_name, **kwargs)
                assert out.startswith("Error:") and "another MCP session" in out, (tool_name, out)

    asyncio.run(scenario())


def test_a_released_instance_moves_to_the_session_that_claims_it():
    async def scenario():
        async with connect(server_mcp._mcp_server) as a, connect(server_mcp._mcp_server) as b:
            a_key = (await instances(a))["session"]
            state._SESSIONS[a_key].instances["handoff"] = InstanceSnapshot(instance_id="handoff")

            out = await call(b, "switch_instance", instance_id="handoff")
            assert "another MCP session" in out
            assert "released" in await call(a, "instance_release", instance_id="handoff")
            assert (await instances(b))["released_claimable"] == ["handoff"]
            assert "switched" in await call(b, "switch_instance", instance_id="handoff")
            assert (await instances(b))["released_claimable"] == []
            assert next(i for i in (await instances(b))["instances"] if i["current"])[
                "instance_id"] == "handoff"
            # And now the original owner is the outsider.
            assert "another MCP session" in await call(a, "switch_instance", instance_id="handoff")

    asyncio.run(scenario())


def test_current_instance_cannot_be_released():
    async def scenario():
        async with connect(server_mcp._mcp_server) as a:
            current = next(i for i in (await instances(a))["instances"] if i["current"])
            assert "current instance" in await call(
                a, "instance_release", instance_id=current["instance_id"])

    asyncio.run(scenario())


def test_direct_calls_stay_on_the_primary_session():
    """What a stdio server and every existing call site see."""
    assert BrowserState.session_key() == state.PRIMARY_SESSION
    assert BrowserState.current_instance_id == "main"


def test_session_routing_covers_read_write_and_delete():
    token = BrowserState.in_session(state.POOL_SESSION)
    try:
        BrowserState.current_desc = "pool only"
        assert BrowserState.current_desc == "pool only"
    finally:
        BrowserState.unbind_session(token)
    assert BrowserState.current_desc != "pool only"
    token = BrowserState.in_session(state.POOL_SESSION)
    try:
        del BrowserState.current_desc
        assert BrowserState.current_desc == ""
    finally:
        BrowserState.unbind_session(token)


def _bind(http_id):
    token = BrowserState.bind_session(object(), http_id)
    key = BrowserState.session_key()
    BrowserState.unbind_session(token)
    return key


def test_http_sessions_are_keyed_by_mcp_session_id():
    assert _bind("id-a") == _bind("id-a")
    assert _bind("id-a") != _bind("id-b")


def test_a_departed_http_session_leaves_its_browsers_in_the_pool():
    """Detected by polling: tasks pin the session object, so GC never says it ended."""
    state._primary_taken = True
    alive = {"id-a", "id-b"}
    state.set_session_liveness(lambda sid: sid in alive)
    key_a, key_b = _bind("id-a"), _bind("id-b")
    state._SESSIONS[key_a].instances["stored"] = InstanceSnapshot(instance_id="stored")
    state._SESSIONS[key_a].browser = object()

    assert BrowserState.collect_departed_sessions() == []
    alive.discard("id-a")
    assert BrowserState.collect_departed_sessions() == [key_a]
    assert key_a not in state._SESSIONS and key_b in state._SESSIONS
    assert {"stored", key_a} <= set(state._SESSIONS[state.POOL_SESSION].instances)
    assert BrowserState.collect_departed_sessions() == []


def test_a_new_primary_gets_the_browser_the_last_one_left():
    alive = {"id-a"}
    state.set_session_liveness(lambda sid: sid in alive)
    assert _bind("id-a") == state.PRIMARY_SESSION
    marker = object()
    state._SESSIONS[state.PRIMARY_SESSION].browser = marker
    alive.clear()
    BrowserState.collect_departed_sessions()
    assert "main" in state._SESSIONS[state.POOL_SESSION].instances

    alive.add("id-b")
    assert _bind("id-b") == state.PRIMARY_SESSION
    assert state._SESSIONS[state.PRIMARY_SESSION].browser is marker
    assert "main" not in state._SESSIONS[state.POOL_SESSION].instances


def test_the_pool_never_overwrites_a_live_handle():
    first, second = InstanceSnapshot(instance_id="main"), InstanceSnapshot(instance_id="main")
    state._pool_put("main", first)
    state._pool_put("main", second)
    pool = state._SESSIONS[state.POOL_SESSION].instances
    assert len(pool) == 2 and pool["main"] is first
    assert second.instance_id != "main" and pool[second.instance_id] is second


def test_second_session_gets_its_own_profile_and_cannot_repoint_affinity(tmp_path, monkeypatch):
    """It used to share main's resolver and overwrite the process-wide pin."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", None)
    path, info = server._resolve_launch_profile(
        "session-2", persistent=True, profile_conflict="fallback")
    assert path == tmp_path / "profiles" / "session-2" and "error" not in info


@pytest.mark.parametrize("policy,expect", [
    ("fail", "held by Chrome"),
    ("bogus", "must be 'fallback', 'fail' or 'clone'"),
])
def test_profile_conflict_is_honoured(tmp_path, monkeypatch, policy, expect):
    """Documented for releases while nothing called the code that reads it."""
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", None)
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242)
    _, info = server._resolve_launch_profile("main", persistent=True, profile_conflict=policy)
    assert expect in info["error"]


def test_browser_launch_actually_calls_the_resolver():
    import inspect

    from mcp_stealth_chrome import server

    assert "_resolve_launch_profile(" in inspect.getsource(server.browser_launch)


def test_spawning_one_name_from_two_sessions_is_refused(monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setitem(server._SPAWNING, "racing", "session-9")
    out = asyncio.run(server.mcp._tool_manager._tools["spawn_browser"].fn("racing"))
    assert out.startswith("Error:") and "being launched by another session" in out


def test_storage_snapshots_and_dialog_settings_are_per_session():
    from mcp_stealth_chrome import server

    token = BrowserState.in_session(state.POOL_SESSION)
    try:
        other_key = server._snapshot_key("before")
    finally:
        BrowserState.unbind_session(token)
    assert server._snapshot_key("before") == "before" != other_key


def test_a_snapshot_name_cannot_reach_another_session():
    """A joined string let the primary name a snapshot "pool::before" and read that one."""
    from mcp_stealth_chrome import server

    token = BrowserState.in_session(state.POOL_SESSION)
    try:
        other_key = server._snapshot_key("before")
    finally:
        BrowserState.unbind_session(token)
    assert server._snapshot_key(f"{state.POOL_SESSION}::before") != other_key
    assert server._snapshot_key(str(other_key)) != other_key


def test_dialog_policy_follows_the_instance_through_a_handoff():
    """The new owner was told its setting applied while the old handler kept accepting."""
    from mcp_stealth_chrome import server

    token = BrowserState.in_session(state.POOL_SESSION)
    try:
        BrowserState.current_instance_id = "handed-over"
        cfg_old_owner = server._instance_dialog_cfg("auto")
    finally:
        BrowserState.unbind_session(token)
    previous = BrowserState.current_instance_id
    BrowserState.current_instance_id = "handed-over"
    try:
        assert server._instance_dialog_cfg("auto") is cfg_old_owner
    finally:
        BrowserState.current_instance_id = previous
    assert server._instance_dialog_cfg("auto") is not cfg_old_owner


@pytest.mark.parametrize("owner_state", ["foreign", "locked", "running"])
def test_clone_overwrite_never_deletes_a_profile_in_use(tmp_path, monkeypatch, owner_state):
    """overwrite=True used to rmtree the target without asking who was using it."""
    from mcp_stealth_chrome import server

    source_root = tmp_path / "source"
    (source_root / "Default").mkdir(parents=True)
    (source_root / "Default" / "Preferences").write_text("{}")
    target = tmp_path / "profiles" / "victim"
    (target / "Default").mkdir(parents=True)
    (target / "Default" / "Cookies").write_text("live")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(server, "chrome_user_data_root", lambda _browser=None: source_root)
    monkeypatch.setattr(server, "is_chrome_profile_locked", lambda _p: False)
    deleted = []
    monkeypatch.setattr(server.shutil, "rmtree", lambda p, *a, **k: deleted.append(p))
    if owner_state == "foreign":
        state._SESSIONS["session-9"] = state._SessionState(current_instance_id="victim")
    elif owner_state == "locked":
        monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242 if p == target else None)
    else:
        monkeypatch.setattr(state, "find_chrome_pids_by_profile", lambda p: [4242] if p == target else [])
    out = asyncio.run(server.mcp._tool_manager._tools["clone_chrome_profile"].fn(
        source_profile="Default", target_instance_id="victim", overwrite=True))
    assert out.startswith("Error:") and "not overwriting" in out, out
    assert target not in deleted
    assert (target / "Default" / "Cookies").read_text() == "live"


@pytest.mark.parametrize("cls_attrs,expected", [
    ({"connection": type("C", (), {"closed": False})()}, True),
    ({"connection": type("C", (), {"closed": True})()}, False),
    ({"socket": type("S", (), {"close_code": None})()}, True),
    ({"socket": type("S", (), {"close_code": 1006})()}, False),
    ({"socket": None}, False),
    ({}, False),
])
def test_attached_browser_liveness_on_both_nodriver_designs(cls_attrs, expected):
    assert state.connection_alive(type("B", (), cls_attrs)()) is expected


def test_tracked_requests_are_invisible_to_other_sessions():
    from mcp_stealth_chrome.tools import network_http as nh

    rec = nh._Tracked(request_id="r1", method="GET", url="http://x", created_at=0.0,
                      operation_deadline=1.0, session=state.POOL_SESSION)
    nh._TRACKED["r1"] = rec
    try:
        assert nh._tracked_for_caller("r1") is None and rec not in nh._own_tracked()
        token = BrowserState.in_session(state.POOL_SESSION)
        try:
            assert nh._tracked_for_caller("r1") is rec
        finally:
            BrowserState.unbind_session(token)
    finally:
        nh._TRACKED.pop("r1", None)


def test_reaper_sweeps_every_session(monkeypatch):
    from mcp_stealth_chrome import server

    seen = []

    async def sweep():
        seen.append(BrowserState.session_key())
        if len(seen) == 1:
            raise RuntimeError("one bad session")

    monkeypatch.setattr(server, "_sweep_idle_instances", sweep)
    monkeypatch.setattr(server, "IDLE_REAPER_INTERVAL", 0)
    key = "session-reaped"
    state._SESSIONS[key] = state._SessionState(current_instance_id=key)

    async def scenario():
        task = asyncio.create_task(server._idle_reaper_loop())
        while key not in seen:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(asyncio.wait_for(scenario(), 5))
    finally:
        state._SESSIONS.pop(key, None)
    assert state.PRIMARY_SESSION in seen and state.POOL_SESSION in seen and key in seen


def test_launch_lock_is_per_session():
    from mcp_stealth_chrome.server import _PerSessionLock

    async def scenario():
        lock = _PerSessionLock()
        async with lock:
            assert lock.locked()
            token = BrowserState.in_session(state.POOL_SESSION)
            try:
                assert not lock.locked()
            finally:
                BrowserState.unbind_session(token)

    asyncio.run(scenario())


# ── Token file ────────────────────────────────────────────────────────────


def test_new_token_file_is_private_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv(http_transport.TOKEN_ENV, raising=False)
    path = tmp_path / "sub" / "http-token"
    token, source = http_transport.load_or_create_token(path)
    assert len(token) >= 32 and token not in source
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert http_transport.load_or_create_token(path)[0] == token


def test_token_file_readable_by_others_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv(http_transport.TOKEN_ENV, raising=False)
    path = tmp_path / "http-token"
    path.write_text("exposed\n")
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        http_transport.load_or_create_token(path)


def test_empty_token_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv(http_transport.TOKEN_ENV, raising=False)
    path = tmp_path / "http-token"
    path.write_text("")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        http_transport.load_or_create_token(path)


def test_env_token_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(http_transport.TOKEN_ENV, "from-env")
    assert http_transport.load_or_create_token(tmp_path / "unused")[0] == "from-env"
    assert not (tmp_path / "unused").exists()


# ── Real server process over HTTP ─────────────────────────────────────────


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(home, port, *extra, token=TOKEN):
    env = {**os.environ, "HOME": str(home)}
    env.pop(http_transport.TOKEN_ENV, None)
    if token:
        env[http_transport.TOKEN_ENV] = token
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from mcp_stealth_chrome.server import main; main()",
         "--http", "--port", str(port), *extra],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def wait_ready(port, proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited: {proc.communicate()[1]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not start")


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    port = free_port()
    proc = start_server(tmp_path_factory.mktemp("home"), port)
    wait_ready(port, proc)
    yield f"http://127.0.0.1:{port}/mcp", proc
    proc.terminate()
    proc.wait(timeout=20)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "t", "version": "1"}}}
JSON_SSE = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN},
    {"Authorization": "Bearer"},
])
def test_requests_without_the_token_get_401(http_server, headers):
    url, _ = http_server
    response = httpx.post(url, json=INIT, headers={**JSON_SSE, **headers})
    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers
    assert TOKEN not in response.text


def test_every_path_requires_the_token(http_server):
    base = http_server[0].rsplit("/", 1)[0]
    for path in ("/", "/mcp", "/docs", "/sse"):
        assert httpx.get(base + path).status_code == 401


def test_dns_rebinding_host_is_rejected_even_with_the_token(http_server):
    url, _ = http_server
    response = httpx.post(url, json=INIT, headers={
        **JSON_SSE, "Authorization": f"Bearer {TOKEN}", "Host": "evil.example"})
    assert response.status_code == 421


def test_foreign_origin_is_rejected_even_with_the_token(http_server):
    url, _ = http_server
    response = httpx.post(url, json=INIT, headers={
        **JSON_SSE, "Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"})
    assert response.status_code == 403


def test_token_is_not_logged(http_server):
    # Read what has been written so far without closing the pipe.
    _, proc = http_server
    os.set_blocking(proc.stderr.fileno(), False)
    output = proc.stderr.read() or ""
    assert TOKEN not in output


def test_two_http_clients_get_separate_sessions(http_server):
    url, _ = http_server

    async def scenario():
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(url, headers=headers) as (r1, w1, get_id1):
            async with ClientSession(r1, w1) as a:
                await a.initialize()
                async with streamablehttp_client(url, headers=headers) as (r2, w2, get_id2):
                    async with ClientSession(r2, w2) as b:
                        await b.initialize()
                        assert get_id1() and get_id2() and get_id1() != get_id2()
                        a_info, b_info = await instances(a), await instances(b)
                        assert a_info["session"] != b_info["session"]
                        await call(a, "switch_instance", instance_id="mine")
                        out = await call(b, "switch_instance", instance_id="mine")
                        assert "another MCP session" in out

    asyncio.run(scenario())


def test_non_loopback_bind_is_refused_without_the_explicit_flag(tmp_path):
    proc = start_server(tmp_path, free_port(), "--host", "0.0.0.0")
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 2 and "refusing to listen" in stderr


def test_wildcard_bind_needs_named_hosts(tmp_path):
    proc = start_server(tmp_path, free_port(), "--host", "0.0.0.0", "--allow-remote")
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 2 and "--allowed-host" in stderr


def test_allowed_host_passes_the_check_and_others_do_not(tmp_path):
    port = free_port()
    proc = start_server(tmp_path, port, "--allowed-host", "proxy.example")
    try:
        wait_ready(port, proc)
        url = f"http://127.0.0.1:{port}/mcp"
        auth = {**JSON_SSE, "Authorization": f"Bearer {TOKEN}"}
        ok = httpx.post(url, json=INIT, headers={
            **auth, "Host": "proxy.example", "Origin": "https://proxy.example"})
        assert ok.status_code == 200
        bad = httpx.post(url, json=INIT, headers={**auth, "Host": "evil.example"})
        assert bad.status_code == 421
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_sigterm_exits_cleanly(tmp_path):
    port = free_port()
    proc = start_server(tmp_path, port)
    wait_ready(port, proc)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0


def test_default_bind_is_loopback_only():
    assert http_transport.build_app.__defaults__[0] == "127.0.0.1"
    from mcp_stealth_chrome.server import _parse_cli

    cli = _parse_cli(["--http"])
    assert cli.host == "127.0.0.1" and cli.allow_remote is False
    assert _parse_cli([]).http is False


def test_generated_token_lands_in_a_private_file_and_is_not_printed(tmp_path):
    port = free_port()
    proc = start_server(tmp_path, port, token=None)
    try:
        wait_ready(port, proc)
        path = tmp_path / ".mcp-stealth" / "http-token"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        secret = path.read_text().strip()
        assert httpx.post(f"http://127.0.0.1:{port}/mcp", json=INIT,
                          headers=JSON_SSE).status_code == 401
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert secret not in proc.stderr.read()


# ── Two agents, one server, real browsers ─────────────────────────────────


@E2E
def test_two_clients_drive_separate_browsers_in_parallel(tmp_path):
    port = free_port()
    proc = start_server(tmp_path, port)
    wait_ready(port, proc)
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def agent(session, name):
        # Persistent: the profile lives under the temporary HOME, which is what lets the
        # leak check below find these processes. Ephemeral ones live in the system temp.
        # --use-mock-keychain: with a temporary HOME, macOS would otherwise ask the person
        # to "Reset To Defaults" a keychain that Chrome cannot find.
        launched = await call(session, "browser_launch", headless=True,
                              extra_args=["--use-mock-keychain"])
        assert not launched.startswith("Error:"), launched
        for i in range(4):
            await call(session, "navigate",
                       url=f"data:text/html,<title>{name}-{i}</title><button id=b>{name}</button>")
            await asyncio.sleep(0.05)
            assert f"{name}-{i}" in await call(session, "evaluate", expression="document.title")
            shot = await call(session, "browser_snapshot", diff_from_last=False)
            assert f'"{name}"' in shot, shot
            clicked = await call(session, "click", ref="e0")
            assert "clicked" in clicked, clicked
        return await call(session, "server_status")

    async def scenario():
        async with streamablehttp_client(url, headers=headers) as (r1, w1, _):
            async with ClientSession(r1, w1) as a:
                await a.initialize()
                async with streamablehttp_client(url, headers=headers) as (r2, w2, _):
                    async with ClientSession(r2, w2) as b:
                        await b.initialize()
                        a_status, b_status = await asyncio.gather(
                            agent(a, "alpha"), agent(b, "beta"))
                        assert json.loads(a_status)["session"] != json.loads(b_status)["session"]
                        # Each has its own profile rather than falling back or colliding.
                        profiles = {i["profile"] for s in (a, b)
                                    for i in (await instances(s))["instances"] if i["current"]}
                        assert len(profiles) == 2 and all(str(tmp_path) in p for p in profiles)
                        # One session closing must not touch the other.
                        await call(a, "browser_close")
                        assert "beta" in await call(b, "evaluate", expression="document.title")

    try:
        asyncio.run(asyncio.wait_for(scenario(), 180))
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    time.sleep(1.5)
    leftover = subprocess.run(["pgrep", "-f", f"user-data-dir={tmp_path}"], capture_output=True, text=True)
    assert not leftover.stdout.strip(), "server left a browser behind after SIGTERM"
    assert proc.returncode == 0


def test_failed_intercept_start_leaves_another_sessions_interception(monkeypatch):
    """B had no browser; its failure disabled A's protective interception."""
    from mcp_stealth_chrome import server

    disabled = []

    async def fake_disable(reason="stopped"):
        disabled.append(reason)

    from mcp_stealth_chrome.tools import capture

    monkeypatch.setattr(capture, "_intercept_disable", fake_disable)
    monkeypatch.setitem(capture._INTERCEPT, "active", True)
    monkeypatch.setitem(capture._INTERCEPT, "session", "session-other")
    monkeypatch.setattr(BrowserState, "active_tab",
                        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("Browser not running"))))
    out = asyncio.run(server.mcp._tool_manager._tools["intercept_start"].fn())
    assert out.startswith("Error:") and disabled == []


def test_next_client_does_not_inherit_requests_or_snapshots():
    """The string "primary" passes on; the browser may, the records must not."""
    from mcp_stealth_chrome import server
    from mcp_stealth_chrome.tools import network_http as nh

    alive = {"first"}
    state.set_session_liveness(lambda sid: sid in alive)
    assert _bind("first") == state.PRIMARY_SESSION
    server._STORAGE_SNAPSHOTS["secret"] = {"cookies": ["x"]}
    nh._TRACKED["r9"] = nh._Tracked(request_id="r9", method="GET", url="http://x", created_at=0.0,
                                    operation_deadline=1.0, session=state.PRIMARY_SESSION)
    alive.clear()
    BrowserState.collect_departed_sessions()
    alive.add("second")
    assert _bind("second") == state.PRIMARY_SESSION
    assert "secret" not in server._STORAGE_SNAPSHOTS
    assert "r9" not in nh._TRACKED


def test_identity_tools_refuse_another_sessions_instance():
    from mcp_stealth_chrome import server

    state._SESSIONS["session-8"] = state._SessionState(current_instance_id="theirs")
    for name in ("identity_show", "identity_pin"):
        out = asyncio.run(server.mcp._tool_manager._tools[name].fn(instance_id="theirs"))
        assert out.startswith("Error:") and "another MCP session" in out
