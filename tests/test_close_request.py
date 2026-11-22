"""Browser.close reaches Chrome on both nodriver designs.

Up to 0.48 a Browser holds its websocket in `.connection`. From 0.50 the Browser is a Connection
itself and `.connection` stays None, so asking only `.connection` never sent Browser.close on 0.50:
a launched Chrome was ended by stop()'s SIGTERM before writing its cookie store (0 rows), and a
reattached keep_alive Chrome was signalled directly ("CDP stop did not end the process").
"""
import asyncio
import types

import pytest

from mcp_stealth_chrome import server


class _Sent(list):
    async def send(self, call):
        self.append(next(call)["method"])
        return None


def _browser_048(closed=False):
    connection = _Sent()
    connection.closed = closed
    return types.SimpleNamespace(connection=connection), connection


class _Browser050(_Sent):
    """nodriver 0.50: the Browser is a Connection; .connection is an attribute left at None."""

    def __init__(self, close_code=None, socket=True):
        super().__init__()
        self.connection = None
        self.socket = types.SimpleNamespace(close_code=close_code) if socket else None


def test_browser_close_is_sent_on_nodriver_050():
    browser = _Browser050()
    assert asyncio.run(server._request_browser_close(browser)) is True
    assert browser == ["Browser.close"]


def test_browser_close_is_sent_on_nodriver_050_before_any_socket_was_opened():
    # send() opens the socket itself on 0.50.
    browser = _Browser050(socket=False)
    assert asyncio.run(server._request_browser_close(browser)) is True
    assert browser == ["Browser.close"]


def test_browser_close_is_sent_on_nodriver_048():
    browser, connection = _browser_048()
    assert asyncio.run(server._request_browser_close(browser)) is True
    assert connection == ["Browser.close"]


def test_a_dropped_socket_is_not_asked():
    browser = _Browser050(close_code=1006)
    assert asyncio.run(server._request_browser_close(browser)) is False
    assert browser == []
    old, connection = _browser_048(closed=True)
    assert asyncio.run(server._request_browser_close(old)) is False
    assert connection == []


def test_a_reply_lost_as_chrome_closes_its_socket_counts_as_asked():
    class Dropping(_Browser050):
        async def send(self, call):
            self.append(next(call)["method"])
            self.socket.close_code = 1006
            raise ConnectionError("Connection closed")

    browser = Dropping()
    assert asyncio.run(server._request_browser_close(browser)) is True


def test_a_failed_send_on_a_live_socket_is_not_asked():
    class Refusing(_Browser050):
        async def send(self, call):
            raise RuntimeError("refused")

    assert asyncio.run(server._request_browser_close(Refusing())) is False


@pytest.mark.parametrize("lifetime,survives", [(0.6, False), (float("inf"), True)])
def test_failed_start_waits_for_an_exiting_process(monkeypatch, lifetime, survives):
    # An exiting child can still occupy its PID after its command line is unreadable.
    # Advance virtual time only while the reaper yields. No real process is probed or signalled.
    elapsed = 0.0

    async def sleep(delay):
        nonlocal elapsed
        elapsed += delay
        assert elapsed < 10, "the reaper did not bound its wait"

    class Exiting:
        config = object()
        _process_pid = 12345

        def stop(self):
            pass

    child = Exiting()
    monkeypatch.setattr(server.nodriver.core.util, "get_registered_instances", lambda: {child})
    monkeypatch.setattr(server.quit_watch, "pid_start", lambda *_: None)
    monkeypatch.setattr(server, "_identify", lambda *_: "unknown")
    monkeypatch.setattr(server, "_process_exists", lambda pid: elapsed < lifetime)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    report = asyncio.run(server._reap_failed_start(child.config))
    assert report["still_alive"] == ([child._process_pid] if survives else [])
    assert report["verified"] is not survives
    assert not report["killed"]


def test_an_exit_during_signal_verification_is_not_reported_alive(monkeypatch):
    class Browser:
        _process_pid = 12345
        config = types.SimpleNamespace(user_data_dir="/test-profile")

        def stop(self):
            pass

    alive = True
    signalled = False

    def signal_verified(*_args):
        nonlocal alive, signalled
        if signalled:
            alive = False  # gone between the liveness check and signal verification
            return False
        signalled = True
        return True

    async def no_wait(_):
        pass

    monkeypatch.setattr(server, "_identify", lambda *_: "same")
    monkeypatch.setattr(server.quit_watch, "pid_start", lambda *_: None)
    monkeypatch.setattr(server, "_descendant_pids", lambda *_: [])
    monkeypatch.setattr(server, "_process_exists", lambda pid: alive)
    monkeypatch.setattr(server, "_signal_verified", signal_verified)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    report = asyncio.run(server._ensure_browser_dead(Browser()))
    assert report["verified"] and not report["still_alive"], report
