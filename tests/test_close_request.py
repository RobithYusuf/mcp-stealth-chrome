"""Browser.close reaches Chrome on both nodriver designs.

Up to 0.48 a Browser holds its websocket in `.connection`. From 0.50 the Browser is a Connection
itself and `.connection` stays None, so asking only `.connection` never sent Browser.close on 0.50:
a launched Chrome was ended by stop()'s SIGTERM before writing its cookie store (0 rows), and a
reattached keep_alive Chrome was signalled directly ("CDP stop did not end the process").
"""
import asyncio
import types

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
