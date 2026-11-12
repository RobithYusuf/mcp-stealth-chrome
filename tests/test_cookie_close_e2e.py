"""A cookie set just before browser_close is still there at the next launch.

Chrome writes its cookie store to disk about every 30 seconds. Ended with SIGTERM, as nodriver's
stop() does, it exits without writing: three cookies set and closed at once left 0 rows. Asked with
Browser.close, it writes them first. Needs MCP_E2E=1; the profile is a temporary folder and the
Keychain is the real one, as a person's profile uses it.
"""
import functools
import http.server
import os
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import time
from urllib.parse import quote

import pytest

from _protocol import LOOPBACK_ONLY, Client

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome")

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _rows(profile: pathlib.Path) -> list:
    database = profile / "Default" / "Cookies"
    if not database.exists():
        return []
    with sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True) as db:
        return sorted(name for (name,) in db.execute("SELECT name FROM cookies"))


@pytest.fixture
def site():
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html"
    httpd.shutdown()


def test_a_cookie_set_right_before_close_survives_the_next_launch(site, capsys):
    profile = pathlib.Path(tempfile.mkdtemp(prefix="close-cookie-"))
    client = Client()
    try:
        launched = client.call("browser_launch", _timeout=120, url=site, headless=True,
                               user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
        assert not launched.failed, launched.text
        client.evaluate("['a','b','c'].forEach(n => document.cookie = n + '=1; max-age=86400; path=/'), 'ok'")
        started = time.monotonic()
        closed = client.call("browser_close", _timeout=40)
        took = time.monotonic() - started
        assert closed.text.startswith("Browser closed"), closed.text
        assert "STILL RUNNING" not in closed.text and "signalled" not in closed.text, closed.text
        assert _rows(profile) == ["a", "b", "c"], "written to disk by the close itself"

        again = client.call("browser_launch", _timeout=120, url=site, headless=True,
                            user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
        assert not again.failed, again.text
        assert sorted(client.evaluate("document.cookie").split("; ")) == ["a=1", "b=1", "c=1"]
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        with capsys.disabled():
            print(f"\n[close e2e] browser_close with Browser.close took {took:.2f}s")
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        shutil.rmtree(profile, ignore_errors=True)
