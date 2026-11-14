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


@pytest.fixture(scope="session", autouse=True)
def _keep_alive_records_in_a_temp_folder():
    """keep_alive records for every server the suite starts and every in-process launch, so no
    test reads or writes the person's ~/.mcp-stealth/keep-alive, and none reattaches to a
    browser the person kept."""
    previous = os.environ.get("MCP_STEALTH_KEEP_ALIVE_DIR")
    folder = tempfile.mkdtemp(prefix="mcp-stealth-keep-alive-")
    os.environ["MCP_STEALTH_KEEP_ALIVE_DIR"] = folder
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop("MCP_STEALTH_KEEP_ALIVE_DIR", None)
        else:
            os.environ["MCP_STEALTH_KEEP_ALIVE_DIR"] = previous
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


@pytest.fixture(scope="session", autouse=True)
def _adblock_lists_in_a_temp_folder():
    """block_ads keeps its filter lists and engine here for every server the suite starts and
    every in-process launch, so no test writes into the person's ~/.mcp-stealth/adblock."""
    previous = os.environ.get("MCP_STEALTH_ADBLOCK_DIR")
    folder = tempfile.mkdtemp(prefix="mcp-stealth-adblock-")
    os.environ["MCP_STEALTH_ADBLOCK_DIR"] = folder
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop("MCP_STEALTH_ADBLOCK_DIR", None)
        else:
            os.environ["MCP_STEALTH_ADBLOCK_DIR"] = previous
        shutil.rmtree(folder, ignore_errors=True)


# ── Browsers the run started are ended by the run, and a leak fails it ──────────────────────
#
# A headed Chrome a block_ads test started stayed for 8 hours: parent PID 1, a uc_* temp profile,
# no CDP client, in the person's Dock. Every Chrome the suite starts carries this run's own flag
# (run_marker), which is how it is found: never by application name, since the person's browsers
# and those of another test run on the same machine run beside it.
# - after each test (MCP_E2E runs): a browser of this run whose starter is gone (its server was
#   killed, or exited without ending it) is ended, and recorded as left by that test;
# - after each module (MCP_E2E runs): every browser of this run still running is ended and
#   recorded (module fixtures holding one have been torn down by then);
# - at the end: anything left is ended, and the run fails naming every browser recorded.
# A process list, command line or start time that cannot be read fails the run too, naming what,
# and a process whose identity is not proved is never signalled.
# A test ends what it starts in its own finalizer; this is the net under that, and it reports.

import secrets  # noqa: E402

import _leftovers  # noqa: E402
from mcp_stealth_chrome import run_marker  # noqa: E402

_RUN = secrets.token_hex(8)
_LEFT: list[str] = []
# A browser being closed when the check runs (an EOF close, a SIGTERM a fixture just sent) is
# given this long to go on its own before it counts.
_GRACE = float(os.environ.get("MCP_TEST_LEFTOVER_GRACE", "8"))


@pytest.fixture(scope="session", autouse=True)
def _browsers_of_this_run_are_marked():
    previous = os.environ.get(run_marker.ENV)
    os.environ[run_marker.ENV] = _RUN
    try:
        yield _RUN
    finally:
        if previous is None:
            os.environ.pop(run_marker.ENV, None)
        else:
            os.environ[run_marker.ENV] = previous


def _sweep(where: str, only_orphans: bool) -> None:
    # A process list or identity that cannot be read is recorded too: unknown is never gone.
    _LEFT.extend(_leftovers.sweep(_RUN, where, only_orphans, _GRACE))


# One process list per test costs about 60 ms; only runs that start browsers pay it.
_PER_TEST = bool(os.environ.get("MCP_E2E"))


@pytest.fixture(scope="module", autouse=True)
def _a_module_ends_the_browsers_it_started(request):
    yield
    if _PER_TEST:
        _sweep(f"module {request.node.nodeid}", only_orphans=False)


@pytest.fixture(autouse=True)
def _a_test_ends_the_browsers_it_orphaned(request):
    yield
    if _PER_TEST:
        # An orphan from an earlier test was caught by that test's own sweep.
        _sweep(request.node.nodeid, only_orphans=True)


def pytest_sessionfinish(session, exitstatus):
    _sweep("end of the run", only_orphans=False)
    if not _LEFT:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = [f"{len(_LEFT)} browser(s) this run started were left running, or could not be "
             f"checked:", *_LEFT]
    if reporter is not None:
        reporter.write_sep("=", "browsers left running", red=True)
        for line in lines:
            reporter.write_line(line)
    else:
        print("\n".join(lines))
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
