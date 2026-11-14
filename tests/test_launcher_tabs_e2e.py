"""Whose tabs an agent may act on after reattaching to a browser the person opened from the launcher.

A browser started by `mcp-stealth-chrome open` (the Dock launcher) is the person's: every tab in it was
opened by the person or the launcher. An agent that reattaches takes over only the tabs it opens
itself (url=, tab_new) and those an earlier agent session opened (saved in the keep_alive record at
its exit); the person's tabs need tab_select. A kept browser an agent launched is taken over whole,
as before.

The headless test stands the launcher's record in for the launcher (it runs on Linux CI). The headed
one runs the real `open` command. Both need MCP_E2E=1 and POSIX (keep_alive); every browser uses
--use-mock-keychain and a temporary data folder, and only processes naming that folder are ended.
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import state

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real Chrome; set MCP_E2E=1"),
    pytest.mark.skipif(sys.platform == "win32", reason="keep_alive is POSIX only (quit watcher)"),
]

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture
def world():
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-launcher-tabs-"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ns = type("World", (), {})()
    ns.root = root
    ns.base = f"http://127.0.0.1:{httpd.server_address[1]}/protocol.html"
    ns.env = {"MCP_STEALTH_DATA_DIR": str(root / "data"), "MCP_STEALTH_KEEP_ALIVE_DIR": str(root / "keep"),
              "MCP_STEALTH_TABS_DIR": "", "MCP_STEALTH_QUIT_WATCH_DIR": str(root / "qw"),
              "MCP_STEALTH_LAUNCHER_EXTRA_ARGS": json.dumps(SAFE_ARGS),
              "MCP_STEALTH_LAUNCHER_ACTIVATE": "0"}
    (root / "qw").mkdir(mode=0o700)
    try:
        yield ns
    finally:
        httpd.shutdown()
        for pid, _command in state.processes_naming(root) or []:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)


def _record(world) -> dict:
    files = list((world.root / "keep").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def _write_record(world, **fields) -> None:
    path = next((world.root / "keep").glob("*.json"))
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def _person_opens(world, url: str) -> None:
    """The person opens a tab (Cmd+T and a URL): a page target with no opener."""
    from urllib.request import Request, urlopen
    port = _record(world)["port"]
    urlopen(Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT"), timeout=5).read()


def _rows(client) -> list:
    return client.call("tab_list").text.splitlines()[1:]


def _index(client, needle) -> int:
    row = next(r for r in _rows(client) if needle in r)
    return int(row.split("]")[0].split("[")[1])


def _launch(client, url, headless):
    return client.call("browser_launch", _timeout=90, url=url, headless=headless, persistent=True,
                       keep_alive=True, extra_args=SAFE_ARGS)


def _agent_reattaches_and_checks(world, headless: bool) -> None:
    agent = Client(env=world.env)
    try:
        out = _launch(agent, world.base + "?agent=1", headless)
        assert "Reattached" in out.text, out.text
        assert "the person opened this browser from the launcher" in out.text, out.text
        rows = _rows(agent)
        for needle in ("?launcher", "?person"):
            assert "not opened by this server" in next(r for r in rows if needle in r), rows
        assert "not opened" not in next(r for r in rows if "?agent=1" in r), rows
        assert agent.call("get_url").text.endswith("?agent=1")
        refused = agent.call("evaluate", expression="document.title = 'agent'; 1", tab=_index(agent, "?person"))
        assert "not opened by this server" in refused.text, refused.text
        closed = agent.call("tab_close", index=_index(agent, "?launcher"))
        assert closed.failed and "not opened by this server" in closed.text, closed.text
    finally:
        agent.close()  # stdin EOF: the browser stays, and the agent's tab is saved as its own
    assert _record(world).get("owned"), _record(world)
    again = Client(env=world.env)
    try:
        out = _launch(again, "about:blank", headless)
        assert "Reattached" in out.text, out.text
        # The tab the earlier agent opened is the agent's again and is selected; the person's are not.
        assert again.call("get_url").text.endswith("?agent=1")
        rows = _rows(again)
        assert "not opened by this server" in next(r for r in rows if "?person" in r), rows
        assert "not opened" not in next(r for r in rows if "?agent=1" in r), rows
        picked = again.call("tab_select", index=_index(again, "?person"))
        assert "not opened by this server" in picked.text, picked.text
        assert again.call("get_url").text.endswith("?person=1")
        assert not again.call("browser_close", _timeout=60).failed
    finally:
        again.close()


def test_a_reattach_to_a_launcher_browser_takes_over_only_the_agents_tabs(world):
    first = Client(env=world.env)
    try:
        assert "Browser launched" in _launch(first, world.base + "?launcher=1", True).text
    finally:
        first.close()
    # What the launcher leaves: a kept browser it opened for the person, with no agent tab.
    _write_record(world, opened_by="launcher", owned=[])
    _person_opens(world, world.base + "?person=1")
    # An agent that asks for no page has no tab of its own: it acts on none of the person's.
    peek = Client(env=world.env)
    try:
        out = _launch(peek, "about:blank", True)
        assert "the selected tab is the person's" in out.text, out.text
        assert all("not opened by this server" in r for r in _rows(peek)), _rows(peek)
        refused = peek.call("get_url")
        assert refused.failed and "not opened by this server" in refused.text, refused.text
    finally:
        peek.close()
    assert _record(world).get("owned") == [], _record(world)
    _agent_reattaches_and_checks(world, headless=True)


def test_a_reattach_to_an_agents_kept_browser_takes_over_every_tab(world):
    first = Client(env=world.env)
    try:
        assert "Browser launched" in _launch(first, world.base + "?mine=1", True).text
        assert not first.call("tab_new", url=world.base + "?mine=2").failed
    finally:
        first.close()
    again = Client(env=world.env)
    try:
        out = _launch(again, "about:blank", True)
        assert "Reattached" in out.text and "from the launcher" not in out.text, out.text
        assert "not opened by this server" not in again.call("tab_list").text
        assert not again.call("browser_close", _timeout=60).failed
    finally:
        again.close()


@pytest.mark.skipif(not os.environ.get("MCP_HEADED"), reason="opens a visible browser: MCP_HEADED=1")
def test_the_open_command_then_an_agent(world):
    cli = str(pathlib.Path(sys.executable).parent / "mcp-stealth-chrome")
    env = {**os.environ, **world.env, "PYTHONPATH": str(pathlib.Path(__file__).parent.parent / "src")}
    done = subprocess.run([cli, "open", "--browser", "chrome", "--url", world.base + "?launcher=1"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _record(world).get("opened_by") == "launcher", _record(world)
    _person_opens(world, world.base + "?person=1")
    _agent_reattaches_and_checks(world, headless=False)
