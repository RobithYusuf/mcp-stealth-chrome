"""Native solver boundaries. Real desktop/Cloudflare results are documented separately."""
import asyncio
import json
import sys
import threading
import time

import cv2
import numpy as np
import pytest
from nodriver.core import util

from mcp_stealth_chrome.tools import turnstile_oscursor as solver


@pytest.mark.parametrize("ratio,dark", [(1, False), (2, False), (1, True), (2, True)])
def test_checkbox_maps_capture_pixels_to_screen_points(tmp_path, ratio, dark):
    template = cv2.imdecode(np.frombuffer(util.get_cf_template(), np.uint8), cv2.IMREAD_COLOR)
    image = np.full((400, 600, 3), 255, dtype=np.uint8)
    height, width = template.shape[:2]
    image[130:130 + height, 180:180 + width] = template
    if dark:
        image = 255 - image
    image = cv2.resize(image, (600 * ratio, 400 * ratio), interpolation=cv2.INTER_NEAREST)
    shot = tmp_path / "desktop.png"
    cv2.imwrite(str(shot), image)
    x, y, score = solver._checkbox(shot, (-80, 60, 600, 400))
    assert x == pytest.approx(-80 + 180 + width / 2, abs=1)
    assert y == pytest.approx(60 + 130 + height / 2, abs=1)
    assert score >= 0.7


def test_no_checkbox_is_not_permission_to_click(tmp_path):
    shot = tmp_path / "desktop.png"
    cv2.imwrite(str(shot), np.full((400, 600, 3), 127, dtype=np.uint8))
    with pytest.raises(solver._Failure, match="nothing clicked"):
        solver._checkbox(shot, (60, 60, 600, 400))


@pytest.mark.parametrize("domain,expires,partition", [
    ("notexample.test", -1, None), ("example.test.evil.test", -1, None),
    (".example.test", 1, None), (".example.test", -1, {"topLevelSite": "https://other.test"}),
])
def test_foreign_expired_or_partitioned_clearance_is_not_usable(domain, expires, partition):
    cookie = {"name": "cf_clearance", "value": "private", "domain": domain,
              "expires": expires, "partitionKey": partition}
    selected, clearance, rejected = solver._site_cookies([cookie], "https://example.test/demo")
    assert selected == []
    assert not clearance and not rejected


def test_rejection_cookie_prevents_clearance_success():
    cookies = [{"name": name, "value": "private", "domain": ".example.test", "expires": time.time() + 100}
               for name in ("cf_clearance", "cf_chl_rc_ni")]
    selected, clearance, rejected = solver._site_cookies(cookies, "https://sub.example.test/demo")
    assert selected == cookies
    assert clearance and rejected  # the caller must require clearance AND NOT rejected


@pytest.mark.skipif(sys.platform == "win32", reason="open-file unlink and symlink semantics are POSIX")
def test_private_cleanup_overwrites_files_without_following_symlinks(tmp_path):
    outside = tmp_path / "other-profile"
    outside.mkdir()
    credential = outside / "cookies"
    credential.write_bytes(b"do not touch")
    owned = tmp_path / "owned"
    owned.mkdir()
    secret = owned / "state.json"
    secret.write_bytes(b"private credential")
    (owned / "link-to-file").symlink_to(credential)
    (owned / "link-to-profile").symlink_to(outside, target_is_directory=True)
    # An open descriptor still sees the old inode after unlink, proving overwrite.
    with secret.open("rb", buffering=0) as handle:
        solver._shred_tree(owned)
        assert handle.read() == bytes(len(b"private credential"))
    assert not owned.exists()
    assert credential.read_bytes() == b"do not touch"


def test_desktop_lock_refuses_concurrent_solver(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.tempfile, "gettempdir", lambda: str(tmp_path))
    with solver._desktop_lock():
        with pytest.raises(solver._Failure, match="owns this desktop"):
            with solver._desktop_lock():
                pytest.fail("second caller acquired the same desktop")
    with solver._desktop_lock():
        pass  # reusable after release, without removing the lock inode


def test_cancellation_waits_for_native_input_to_finish():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def native_input():
        started.set()
        assert release.wait(5)
        finished.set()

    async def scenario():
        task = asyncio.create_task(solver._native(native_input))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cleanup must not race a still-running OS input thread"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_cookie_error_cannot_leak_credentials_to_tool_output(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.tempfile, "gettempdir", lambda: str(tmp_path))

    async def rejected(*args):
        raise RuntimeError("CDP rejected value=private-clearance")

    monkeypatch.setattr(solver, "_attempt", rejected)
    reply = asyncio.run(solver.solve_turnstile_oscursor("https://example.test"))
    assert reply.startswith("Error: ")
    assert "private-clearance" not in reply
    report = json.loads(reply.removeprefix("Error: "))
    assert report["success"] is False
    assert report["sidecar"] is None and report["main"] is None


def test_cleanup_failure_cannot_be_reported_as_solver_success(tmp_path, monkeypatch):
    monkeypatch.setattr(solver.tempfile, "gettempdir", lambda: str(tmp_path))
    owned = tmp_path / "owned"
    owned.mkdir()

    async def completed(url, timeout_s, report, resources):
        resources["root"] = owned
        return True

    def cannot_erase(root):
        raise OSError("still busy")

    monkeypatch.setattr(solver, "_attempt", completed)
    monkeypatch.setattr(solver, "_shred_tree", cannot_erase)
    reply = asyncio.run(solver.solve_turnstile_oscursor("https://example.test"))
    report = json.loads(reply.removeprefix("Error: "))
    assert reply.startswith("Error: ") and report["success"] is False
    assert "NOT complete" in report["cleanup"]
    assert owned.exists()


@pytest.mark.parametrize("rejected,sidecar_ua", [(True, "same-agent"), (False, "different-agent")])
def test_sidecar_rejection_or_ua_mismatch_never_reaches_handoff(tmp_path, monkeypatch, rejected, sidecar_ua):
    from mcp_stealth_chrome import server

    tab = object()
    monkeypatch.setattr(server.BrowserState, "is_up", classmethod(lambda cls: True))
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    monkeypatch.setattr(server.BrowserState, "current_proxy", None)
    monkeypatch.setattr(solver, "find_chrome_binary", lambda: "/unused/chrome")
    monkeypatch.setattr(solver.tempfile, "mkdtemp", lambda **kw: str(tmp_path))

    class Process:
        pid = 123

        def poll(self):
            return None

    monkeypatch.setattr(solver.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(solver.os_input, "is_available", lambda: (True, "test desktop"))
    monkeypatch.setattr(solver.os_input, "bring_app_frontmost", lambda pid: None)
    monkeypatch.setattr(solver.os_input, "window_bounds", lambda pid: (60, 60, 1280, 900))
    monkeypatch.setattr(solver.os_input, "capture_region", lambda *a: None)
    monkeypatch.setattr(solver, "_checkbox", lambda *a: (270, 480, .9))
    phase = {"clicked": False, "scored": False}

    def click(*args):
        phase["clicked"] = True

    async def sleep(seconds):
        if seconds == 8:
            assert phase["clicked"]
            phase["scored"] = True

    async def evaluate(*args):
        return "same-agent"

    async def cookies(port):
        assert phase["scored"], "CDP contacted the sidecar before its scoring window ended"
        names = ["cf_clearance", "cf_chl_rc_ni"] if rejected else ["cf_clearance"]
        return sidecar_ua, [{"name": name, "value": "private", "domain": ".example.test"} for name in names]

    async def transfer(*args):
        pytest.fail("rejected or UA-mismatched clearance reached the main browser")

    monkeypatch.setattr(solver.os_input, "move_and_click", click)
    monkeypatch.setattr(solver.asyncio, "sleep", sleep)
    monkeypatch.setattr(server, "_eval_checked", evaluate)
    monkeypatch.setattr(solver, "_sidecar_cookies", cookies)
    monkeypatch.setattr(server, "_apply_storage_state", transfer)
    report, resources = {}, {}
    assert asyncio.run(solver._attempt("https://example.test/demo", 30, report, resources)) is False
    assert report["sidecar"]["cf_clearance_present"] is True
    assert not (tmp_path / "state.json").exists()
