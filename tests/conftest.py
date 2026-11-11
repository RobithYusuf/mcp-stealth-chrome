"""Keeps the suite away from the person's real ~/.mcp-stealth/app.

A headed launch runs Chrome from a copy carrying the project's Dock icon, which a server
builds in the background as it starts. Servers the tests start have that turned off, and
would build in a folder made for this run if a test turned it on. Launches made inside the
pytest process are fakes or in-process E2E and skip the copy. `test_dock_icon.py` tests the
real functions and opts out of that.
"""
import os
import shutil
import tempfile

import pytest

from mcp_stealth_chrome import dock_icon


@pytest.fixture(scope="session", autouse=True)
def _dock_icon_copy_in_a_temp_folder():
    previous = os.environ.get(dock_icon.ENV_APP_DIR)
    folder = tempfile.mkdtemp(prefix="mcp-stealth-app-")
    os.environ[dock_icon.ENV_APP_DIR] = folder
    # A server builds the copy on a thread as it starts. Servers the tests start inherit this
    # and build nothing; a test that is about the copy turns it on for its own server.
    switch = os.environ.get(dock_icon.ENV_SWITCH)
    os.environ[dock_icon.ENV_SWITCH] = "0"
    try:
        yield folder
    finally:
        if switch is None:
            os.environ.pop(dock_icon.ENV_SWITCH, None)
        else:
            os.environ[dock_icon.ENV_SWITCH] = switch
        if previous is None:
            os.environ.pop(dock_icon.ENV_APP_DIR, None)
        else:
            os.environ[dock_icon.ENV_APP_DIR] = previous
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_dock_icon_copy_in_process(request, monkeypatch):
    if request.module.__name__ != "test_dock_icon":
        monkeypatch.setattr(dock_icon, "prepare", lambda *_a, **_kw: dock_icon.DockChrome())
