"""Quit on a headed browser ends it. Opens a real window, so it only runs with MCP_HEADED=1 on macOS.

Chrome started with --no-startup-window, which keeps a headed launch from taking the keyboard,
closes its windows on Quit and keeps running, with its Dock icon. The server has to end it.
"""
import os
import subprocess
import sys
import time

import pytest

from _protocol import SAFE_ARGS, Client

pytestmark = pytest.mark.skipif(
    not os.environ.get("MCP_HEADED") or sys.platform != "darwin", reason="opens a visible window")


def _test_chrome_pids() -> set:
    listing = subprocess.run(["ps", "axo", "pid,args"], capture_output=True, text=True).stdout
    return {int(line.split()[0]) for line in listing.splitlines()
            if ("/Google Chrome " in line or "/Google Chrome Beta " in line) and "--type=" not in line
            and "EXCLUDE 127.0.0.1" in line and "--headless" not in line}


def _alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and "Z" not in state


@pytest.mark.parametrize("instance", ["main", "spawned"])
def test_quit_ends_the_browser_and_the_next_call_says_why(instance):
    AppKit = pytest.importorskip("AppKit")
    before = _test_chrome_pids()
    client = Client()
    try:
        if instance == "main":
            launched = client.call("browser_launch", _timeout=60, url="about:blank", headless=False,
                                   persistent=False, extra_args=SAFE_ARGS)
        else:
            launched = client.call("spawn_browser", _timeout=60, instance_id="quitme", url="about:blank",
                                   headless=False, privacy=True, extra_args=SAFE_ARGS)
            client.call("switch_instance", instance_id="quitme")
        assert not launched.failed, launched.text
        ours = _test_chrome_pids() - before
        assert len(ours) == 1, ours
        pid = ours.pop()

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        assert app is not None and app.terminate()  # what Cmd+Q and Dock > Quit send
        deadline = time.time() + 20
        while time.time() < deadline and _alive(pid):
            time.sleep(0.25)
        assert not _alive(pid), "Chrome still running after Quit"

        reply = client.call("get_url")
        assert reply.failed and "last window" in reply.text, reply.text
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()
    time.sleep(1)
