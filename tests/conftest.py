"""Keeps the suite away from the person's real ~/.mcp-stealth.

MCP_STEALTH_DOCK_ICON=1 makes headed launches run from Chrome Beta. Servers the tests start
have it off, and launches inside the pytest process skip it, unless the run sets
MCP_TEST_DOCK_ICON=1: then every server and in-process launch has it on, which is how the
suites are run against Chrome Beta. `test_dock_icon.py` tests the real functions either way.
The folder for an old Chrome copy is a temp folder, so no server removes anything real.
"""
import os
import shutil
import tempfile

import pytest

from mcp_stealth_chrome import dock_icon

_BETA_RUN = os.environ.get("MCP_TEST_DOCK_ICON") == "1"


@pytest.fixture(scope="session", autouse=True)
def _dock_icon_switch_and_app_folder():
    previous = os.environ.get(dock_icon.ENV_APP_DIR)
    folder = tempfile.mkdtemp(prefix="mcp-stealth-app-")
    os.environ[dock_icon.ENV_APP_DIR] = folder
    # Servers the tests start inherit this: off, unless the run asks for Chrome Beta.
    switch = os.environ.get(dock_icon.ENV_SWITCH)
    os.environ[dock_icon.ENV_SWITCH] = "1" if _BETA_RUN else "0"
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


@pytest.fixture(scope="session", autouse=True)
def _no_profile_cleanup_on_real_data():
    """Every server a test starts runs main(), and main() starts the leftover-profile pass.
    Pointed at the real ~/.mcp-stealth, it would act on the person's own folders. Servers
    inherit it off; a test about the pass turns it on together with MCP_STEALTH_DATA_DIR."""
    from mcp_stealth_chrome import profile_gc
    previous = os.environ.get(profile_gc.ENV_SWITCH)
    os.environ[profile_gc.ENV_SWITCH] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(profile_gc.ENV_SWITCH, None)
        else:
            os.environ[profile_gc.ENV_SWITCH] = previous


@pytest.fixture(scope="session", autouse=True)
def _quit_watch_files_in_a_temp_folder():
    """The quit watcher's state files for every server the suite starts."""
    previous = os.environ.get("MCP_STEALTH_QUIT_WATCH_DIR")
    folder = tempfile.mkdtemp(prefix="mcp-stealth-quit-watch-")
    os.environ["MCP_STEALTH_QUIT_WATCH_DIR"] = folder
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop("MCP_STEALTH_QUIT_WATCH_DIR", None)
        else:
            os.environ["MCP_STEALTH_QUIT_WATCH_DIR"] = previous
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _saved_tab_lists_in_a_temp_folder():
    """Tab lists (restore_tabs) for every server the suite starts and every in-process launch.
    A test launching a temporary user_data_dir would otherwise write its fixture URLs into the
    person's ~/.mcp-stealth/window-state."""
    previous = os.environ.get("MCP_STEALTH_TABS_DIR")
    folder = tempfile.mkdtemp(prefix="mcp-stealth-tabs-")
    os.environ["MCP_STEALTH_TABS_DIR"] = folder
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop("MCP_STEALTH_TABS_DIR", None)
        else:
            os.environ["MCP_STEALTH_TABS_DIR"] = previous
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _cookie_backups_in_a_temp_folder(monkeypatch, tmp_path_factory):
    """An in-process launch that moves cookies to Chrome 158's encryption writes its backup and
    database copy through server.STORAGE_STATE_DIR. Under a test that is a temporary folder: a
    fake Chrome 158 in test_profile_version once left six database copies in the person's
    ~/.mcp-stealth/storage-states. A test that needs its own folder patches it again."""
    import sys
    server = sys.modules.get("mcp_stealth_chrome.server")
    if server is not None:
        monkeypatch.setattr(server, "STORAGE_STATE_DIR", tmp_path_factory.mktemp("storage-states"))


@pytest.fixture(autouse=True)
def _no_dock_icon_in_process(request, monkeypatch):
    if request.module.__name__ != "test_dock_icon" and not _BETA_RUN:
        monkeypatch.setattr(dock_icon, "prepare", lambda *_a, **_kw: dock_icon.DockChrome())
