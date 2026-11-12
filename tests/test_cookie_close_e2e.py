"""A cookie set just before browser_close is still there at the next launch.

Chrome writes its cookie store to disk about every 30 seconds. Ended with SIGTERM, as nodriver's
stop() does, it exits without writing: three cookies set and closed at once left 0 rows. Asked with
Browser.close, it writes them first. Needs MCP_E2E=1; the profile is a temporary folder and the
Keychain is the real one, as a person's profile uses it.

The second test reads the profile back from a new server process and records how Chrome encrypted
the cookies on this platform. A profile outside Chrome's default folder must keep its cookies from
one launch to the next: on Linux without a keyring it is the fixed v10 key, on macOS the Keychain
(v10, v11 from Chrome 158), and on Windows DPAPI (v10), since App-Bound Encryption (v20) is off
outside the default folder. Windows does not read them back yet; see WINDOWS_COOKIES_NOT_READ_BACK.
"""
import sys
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

# Windows CI (Google Chrome 156, system install, headless and headed alike): cookies set in one
# launch are written (v10 rows, DPAPI key intact in Local State) and read back by none of the next
# launch; the rows are not deleted either, so this is not a failed decryption, which drops them.
# nodriver alone, with none of this server's code, does the same, so it is Chrome's behaviour on
# that machine. Chrome's own histograms in the relaunch: OSCrypt.AppBoundEncryption.SupportLevel 4
# (not the default user-data dir), DPAPIProvider.Status 0, one key available and one temporarily
# unavailable. Open: a persistent agent profile on Windows may lose its logins at every relaunch.
WINDOWS_COOKIES_NOT_READ_BACK = pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows CI: Chrome reads back none of a non-default profile's cookies on relaunch, with "
    "nodriver alone too (open, see the comment above)."))

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _database(profile: pathlib.Path) -> pathlib.Path | None:
    # Windows keeps it under Network/, other platforms beside the profile's other files.
    for database in (profile / "Default" / "Network" / "Cookies", profile / "Default" / "Cookies"):
        if database.exists():
            return database
    return None


def _read(profile: pathlib.Path, query: str) -> list:
    database = _database(profile)
    if database is None:
        return []
    with sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True) as db:
        return list(db.execute(query))


def _key_state(profile: pathlib.Path) -> str:
    """Which cookie keys Local State holds. On Windows the v10 key lives there (DPAPI-wrapped)."""
    import json
    try:
        crypt = json.loads((profile / "Local State").read_text(encoding="utf-8")).get("os_crypt", {})
    except (OSError, ValueError) as exc:
        return f"Local State unreadable: {exc}"
    return "os_crypt keys: " + ", ".join(f"{k}={len(str(v))}ch" for k, v in sorted(crypt.items()))


def _chrome_log(log: pathlib.Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "no Chrome log"
    hits = [ln for ln in lines if any(w in ln.lower() for w in ("crypt", "cookie", "dpapi", "app_bound", "sql", "database", "lock", "network"))]
    return "\n".join(hits[-40:]) or f"Chrome log has {len(lines)} lines, none about encryption"


def _rows(profile: pathlib.Path) -> list:
    return sorted(name for (name,) in _read(profile, "SELECT name FROM cookies"))


@pytest.fixture
def site():
    handler = functools.partial(_Quiet, directory=FIXTURES)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html"
    httpd.shutdown()


@WINDOWS_COOKIES_NOT_READ_BACK
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


# What Chrome may write in a profile outside its default folder. v20 (App-Bound) on Windows or
# anything new would mean the cookies are bound to something a reused or cloned profile lacks.
_EXPECTED_PREFIXES = {
    "win32": {"v10"},                # DPAPI; App-Bound reports kNotUsingDefaultUserDataDir
    "linux": {"v10", "v11", "v12"},  # fixed key without a keyring, keyring key, secret portal
    "darwin": {"v10", "v11"},        # Keychain; v11 from Chrome 158 outside the default folder
}


@WINDOWS_COOKIES_NOT_READ_BACK
def test_a_persistent_profile_keeps_its_cookies_in_a_new_server(site, capsys):
    profile = pathlib.Path(tempfile.mkdtemp(prefix="reuse-cookie-"))
    try:
        first = Client()
        try:
            launched = first.call("browser_launch", _timeout=120, url=site, headless=True,
                                  user_data_dir=str(profile), extra_args=[LOOPBACK_ONLY])
            assert not launched.failed, launched.text
            first.evaluate("['p','q'].forEach(n => document.cookie = n + '=kept; max-age=86400; path=/'), 'ok'")
            assert first.call("browser_close", _timeout=40).text.startswith("Browser closed")
        finally:
            first.call("close_all_instances", _timeout=40)
            first.close()

        stored = _read(profile, "SELECT name, value, encrypted_value FROM cookies ORDER BY name")
        assert [name for name, _v, _e in stored] == ["p", "q"], stored
        assert all(value == "" for _n, value, _e in stored), "a value was stored unencrypted"
        prefixes = {bytes(encrypted[:3]).decode("ascii", "replace") for _n, _v, encrypted in stored}
        expected = _EXPECTED_PREFIXES.get(sys.platform, {"v10", "v11"})
        assert prefixes <= expected, f"Chrome encrypted with {prefixes}, expected one of {expected}"

        keys_before = _key_state(profile)
        log = profile.parent / f"{profile.name}-chrome.log"
        second = Client()
        try:
            again = second.call("browser_launch", _timeout=120, url=site, headless=True,
                                user_data_dir=str(profile),
                                extra_args=[LOOPBACK_ONLY, "--enable-logging", "--v=1", f"--log-file={log}",
                                            "--allow-chrome-scheme-url"])
            assert not again.failed, again.text
            seen = sorted(second.evaluate("document.cookie").split("; "))
            if seen != ["p=kept", "q=kept"]:
                rows = _read(profile, "SELECT name, substr(encrypted_value, 1, 3) FROM cookies")
                second.call("navigate", url="chrome://histograms/")
                text = second.evaluate("document.body.innerText").splitlines()
                hist = [ln for i, ln in enumerate(text) for keep in [any(
                    w in "".join(text[max(0, i - 8):i + 1]) for w in ("Cookie.LoadProblem", "OSCrypt.", "Cookie.TimeBlocked", "Cookie.Startup", "Sql.", "Cookie.DB"))]
                    if keep and ("Histogram:" in ln or ln[:1].isdigit())][:80]
                pytest.fail(f"relaunch read {seen}; stored {sorted(prefixes)}; before relaunch {keys_before}; "
                            f"after relaunch {_key_state(profile)}; rows now {rows}\n"
                            + "\n".join(hist) + "\n" + _chrome_log(log))
            assert second.call("browser_close", _timeout=40).text.startswith("Browser closed")
        finally:
            second.call("close_all_instances", _timeout=40)
            second.close()
        assert _rows(profile) == ["p", "q"], "the relaunch dropped rows it could not decrypt"
        with capsys.disabled():
            print(f"\n[cookie reuse e2e] {sys.platform}: stored with {sorted(prefixes)}, read back by a new server")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        (profile.parent / f"{profile.name}-chrome.log").unlink(missing_ok=True)
