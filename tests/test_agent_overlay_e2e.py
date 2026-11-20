"""agent_overlay on a real, visible Chrome, through a real server and the MCP boundary.

Opens windows, so it only runs with MCP_HEADED=1 on macOS, with the os-cursor extra:
`MCP_HEADED=1 uv run --extra os-cursor pytest tests/test_agent_overlay_e2e.py`.
The bar's button is pressed with SIGUSR2 to the overlay process (what a click on it does), so
nothing moves the person's mouse. request_human_help brings the window forward, as it always
does.
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import threading
import time

import pytest

from _protocol import SAFE_ARGS, Client
from mcp_stealth_chrome import state

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens visible windows")
FIXTURES = str(pathlib.Path(__file__).parent / "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _wait(check, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(step)
    return check()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _windows(pid: int) -> list:
    import Quartz
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid:
            b = w["kCGWindowBounds"]
            out.append((int(w["kCGWindowLayer"]), (b["X"], b["Y"], b["Width"], b["Height"])))
    return out


def _in_front(ours: int, browser: int) -> bool:
    """Is a window of ours in front of the browser's main window (front-to-back order)?"""
    import Quartz
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        owner, b = w.get("kCGWindowOwnerPID"), w["kCGWindowBounds"]
        if owner == ours:
            return True
        if owner == browser and w.get("kCGWindowLayer") == 0 and b["Width"] >= 300:
            return False
    return False


def _bar_shown(pid: int) -> bool:
    """The bar: the overlay's window 40 points high, on screen."""
    return any(b[3] == 40 for _layer, b in _windows(pid))


def _read(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def test_the_overlay_follows_the_window_and_only_its_button_hands_over():
    pytest.importorskip("Quartz")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=FIXTURES))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = pathlib.Path(tempfile.mkdtemp(prefix="mcp-agent-overlay-"))
    overlays = data / "overlay"
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data), "MCP_STEALTH_OVERLAY_DIR": str(overlays),
                         "MCP_STEALTH_OVERLAY_HIDE_AFTER": "3"})
    try:
        launched = client.call("browser_launch", _timeout=90, headless=False, persistent=False,
                               agent_overlay=True, auto_verify=False, extra_args=SAFE_ARGS,
                               url=f"http://127.0.0.1:{srv.server_port}/protocol.html")
        assert not launched.failed, launched.text
        # Said on only once the overlay said it was up.
        assert "agent_overlay: on:" in launched.text and "not confirmed" not in launched.text, launched.text
        status = json.loads(client.call("server_status").text)["agent_overlay"]["instances"]["main"]
        assert status["on"] and status["holder"] == "agent", status
        overlay_pid = status["overlay_pid"]
        folder = next(overlays.iterdir())
        agent_file, control_file = folder / "agent.json", folder / "control.json"
        chrome_pid = _read(agent_file)["pid"]

        # The light sits on the browser window's four edges, in windows of its own.
        def strips_on_the_window():
            chrome = [b for layer, b in _windows(chrome_pid) if layer == 0 and b[2] >= 300]
            ours = [b for _layer, b in _windows(overlay_pid)]
            if not chrome or len(ours) < 5:
                return None
            x, y, w, h = max(chrome, key=lambda b: b[2] * b[3])
            edges = [b for b in ours if b[2] == w or b[3] == h - 2 * min(b[2], b[3])]
            if len(edges) < 4:
                return None
            box = (min(b[0] for b in edges), min(b[1] for b in edges),
                   max(b[0] + b[2] for b in edges), max(b[1] + b[3] for b in edges))
            return box if box == (x, y, x + w, y + h) else None
        assert _wait(strips_on_the_window, 10), (_windows(chrome_pid), _windows(overlay_pid))

        # The agent works: its click is reported to the overlay, with where the page sits.
        clicked = client.call("click", selector="#inc")
        assert not clicked.failed, clicked.text
        agent = _wait(lambda: (lambda a: a if a.get("click_at") and a.get("viewport") else None)(_read(agent_file)), 5)
        assert agent, _read(agent_file)

        # Take over: every tool that acts on or reads the page is refused, and does nothing.
        os.kill(overlay_pid, signal.SIGUSR2)
        assert _wait(lambda: _read(control_file).get("holder") == "person", 5), _read(control_file)
        for name, args in (("click", {"selector": "#inc"}), ("get_text", {}), ("tab_list", {})):
            refused = client.call(name, **args)
            assert refused.failed and "took over this browser" in refused.text, (name, refused.text)
        assert not client.call("server_status").failed
        # The hold outlasts the quiet time (3 s here): Resume agent stays on screen.
        time.sleep(4.5)
        assert _bar_shown(overlay_pid), "the bar with Resume agent faded out"

        # The overlay dies while the person holds the browser: it is started again, still held.
        os.kill(overlay_pid, signal.SIGKILL)
        time.sleep(0.5)  # a zombie until the server reaps it, which the next call does
        refused = client.call("click", selector="#inc")
        assert refused.failed and "took over this browser" in refused.text, refused.text
        status = json.loads(client.call("server_status").text)["agent_overlay"]["instances"]["main"]
        assert status["on"] and status["restarts"] == 1 and status["holder"] == "person", status
        overlay_pid = status["overlay_pid"]
        assert _wait(lambda: _bar_shown(overlay_pid), 5), "the restarted overlay shows no bar"

        # wait_for_human ends when the person presses Resume agent; the refused clicks did nothing.
        threading.Timer(1.0, os.kill, (overlay_pid, signal.SIGUSR2)).start()
        resumed = client.call("wait_for_human", _timeout=30, timeout=20)
        assert not resumed.failed and "Resume agent" in resumed.text, resumed.text
        assert client.evaluate("document.getElementById('count').textContent").strip() == "1"

        # A request for help shows in the bar; Done there ends the wait.
        asked = client.call("request_human_help", objective="Tick Agree")
        assert not asked.failed and "Done" in asked.text, asked.text
        # That brought Chrome to the front: the light is still in front of it.
        assert _wait(lambda: _in_front(overlay_pid, chrome_pid), 2), "Chrome covered the overlay"
        assert _wait(lambda: _read(agent_file).get("help") == "Tick Agree", 5)
        time.sleep(4.5)  # longer than the quiet time: the request and its Done stay
        assert _bar_shown(overlay_pid), "the bar with Done faded out"
        threading.Timer(1.0, os.kill, (overlay_pid, signal.SIGUSR2)).start()
        done = client.call("wait_for_human", _timeout=30, timeout=20)
        assert not done.failed and "pressed Done" in done.text, done.text
        assert _read(agent_file).get("help") is None

        # A key the person pressed pauses the agent's typing, and the result says so.
        assert not client.call("click", selector="#name").failed
        control = _read(control_file)
        control["person_last_input"] = time.time()
        tmp = control_file.with_name("control.json.test")
        tmp.write_text(json.dumps(control))
        os.replace(tmp, control_file)
        typed = client.call("type_text", text="abc")
        assert not typed.failed, typed.text
        assert "the person typed in the browser" in typed.text, typed.text

        # Closing the browser ends the overlay and removes its files.
        assert not client.call("browser_close", _timeout=40).failed
        assert _wait(lambda: not _alive(overlay_pid), 5), "the overlay outlived its browser"
        assert not folder.exists()
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        srv.shutdown()
        for pid, _command in state.processes_naming(data) or []:
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(data, ignore_errors=True)


def test_without_the_option_nothing_is_started():
    data = pathlib.Path(tempfile.mkdtemp(prefix="mcp-agent-overlay-off-"))
    overlays = data / "overlay"
    client = Client(env={"MCP_STEALTH_DATA_DIR": str(data), "MCP_STEALTH_OVERLAY_DIR": str(overlays)})
    try:
        launched = client.call("browser_launch", _timeout=90, headless=False, persistent=False,
                               auto_verify=False, extra_args=SAFE_ARGS)
        assert not launched.failed, launched.text
        assert "agent_overlay" not in launched.text
        status = json.loads(client.call("server_status").text)["agent_overlay"]
        assert status["instances"]["main"] == {"on": False}, status
        assert not overlays.exists() or not any(overlays.iterdir())
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
        for pid, _command in state.processes_naming(data) or []:
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(data, ignore_errors=True)
