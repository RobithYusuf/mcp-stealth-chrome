"""Open the agent's browser without an agent: `mcp-stealth-chrome open`, and a Dock launcher.

`mcp-stealth-chrome open [--browser brave] [--url URL]` does what a click on the launcher does:

- the agent browser of that profile is running: bring it forward, and open the URL in a new tab;
- it is not: start it exactly as `browser_launch(browser=..., keep_alive=True)` headed would,
  through that very function in this process, then leave it running the way a server does at
  stdin EOF (`_stop_all_browsers`). So the flags, the profile folder, the debugging port, the quit
  watcher and the keep_alive record are the server's own, and a later browser_launch from any
  server reattaches to it through every check keep_alive makes.

It never starts a second browser on a profile: one click at a time (a file lock), and a profile
whose lock is held is only ever brought forward, never launched over. Nothing here needs a
server or an agent to be running.

`mcp-stealth-chrome install-launcher [--browser brave]` builds `~/Applications/Stealth Brave.app`
(MCP_STEALTH_LAUNCHER_DIR moves it): an AppleScript applet made by the system's own osacompile,
with the project icon, signed ad hoc, that runs `open` through the installed command. It can be
pinned in the Dock, and it declares http and https (CFBundleURLTypes), so the person can choose
it as the default browser in System Settings; this module never changes the default.

What a click starts is a browser with a DevTools port on 127.0.0.1, which any program running as
this user can drive, with nobody asking. The launcher says so once before its first start.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import _quit_watcher as _watcher
from . import brave as brave_mode
from . import keep_alive as _keep_alive
from . import launch_lock
from . import state as _state

COMMANDS = ("open", "install-launcher")
ENV_DIR = "MCP_STEALTH_LAUNCHER_DIR"
ENV_EXTRA_ARGS = "MCP_STEALTH_LAUNCHER_EXTRA_ARGS"
ENV_ACTIVATE = "MCP_STEALTH_LAUNCHER_ACTIVATE"
APP_NAMES = {"brave": "Stealth Brave.app", "chrome": "Stealth Chrome Agent.app"}
TITLES = {"brave": "Stealth Brave", "chrome": "Stealth Chrome Agent"}
BUNDLE_IDS = {"brave": "org.mcp-stealth.launcher.brave", "chrome": "org.mcp-stealth.launcher.chrome"}
# What a launcher built in a test, or for another data folder, carries into the browser it opens.
# An app started by the Dock or by a link gets no shell environment.
CARRIED_ENV = ("MCP_STEALTH_DATA_DIR", "MCP_STEALTH_KEEP_ALIVE_DIR", "MCP_STEALTH_QUIT_WATCH_DIR",
               "MCP_STEALTH_TABS_DIR", "MCP_STEALTH_BRAVE_PATH", "MCP_STEALTH_KEEP_ALIVE_MAX_HOURS",
               ENV_EXTRA_ARGS, ENV_ACTIVATE,
               # The test switch of --use-mock-keychain (server._mock_keychain_refusal): only in an
               # app built while a test runs, so its clicks may carry the mock Keychain.
               "PYTEST_CURRENT_TEST",
               # A test run's mark (run_marker), so the suite finds and ends what a click started.
               "MCP_STEALTH_TEST_RUN")
LOCK_WAIT = 90.0
HOST = "127.0.0.1"
EXIT_OK, EXIT_FAILED, EXIT_NOT_HERE, EXIT_ACKNOWLEDGE = 0, 1, 2, 3

FIRST_RUN_NOTE = (
    "This opens the agent's browser without any agent asking for it. It runs with a DevTools "
    "debugging port on 127.0.0.1, so any program running as you on this Mac can control it, "
    "read its pages and use its logins, without asking. It stays open when no agent is "
    "running, until you Quit it. It never opens your own browser or its profile. Open it?")


def _out(text: str) -> None:
    with contextlib.suppress(Exception):
        print(text, flush=True)


def _err(text: str) -> None:
    with contextlib.suppress(Exception):
        print(text, file=sys.stderr, flush=True)


# ── which profile, and who holds it ───────────────────────────────────────────

def profile_for(browser: str) -> Path:
    """The main instance's profile, as browser_launch(browser=...) resolves it with nothing held."""
    root = _state.data_root()
    return brave_mode.main_profile(root) if browser == brave_mode.BRAVE else root / "profile"


@dataclass
class Holder:
    """Who holds a profile. kind: free, ours, stale, foreign or unknown."""
    kind: str
    pid: Optional[int] = None
    port: Optional[int] = None
    why: str = ""


def _spellings(profile: Path) -> list[str]:
    return list(dict.fromkeys([str(profile), os.path.realpath(profile)]))


def _is_main(command: str) -> bool:
    """A browser's main process, judged by its executable's own name. state._is_browser_main_process
    also matches any helper inside "Brave Browser Beta.app/" that has no --type=, such as the crash
    handler a killed Brave Beta leaves running for a while (measured)."""
    executable = command.split(" --", 1)[0].strip()
    return "--type=" not in command and _state._is_browser_main_process(
        "/" + os.path.basename(executable) + " ")


def holder(profile: Path) -> Holder:
    """Whether a browser of ours runs on this profile. Reads only: the lock, the command line of
    its holder, and the process list."""
    lock, pid = _state.chrome_lock_state(profile)
    if lock == "free":
        holders = _state.processes_naming(profile)
        if holders is None:
            return Holder("unknown", why="the process list could not be read")
        mains = [(p, c) for p, c in holders if _is_main(c)]
        if not mains:
            return Holder("free")
        pid, command = mains[0]
        return _judge(profile, pid, command)
    if lock == "unknown" or pid is None:
        return Holder("unknown", why=f"the lock of {profile} cannot be read")
    command = _watcher.process_command(pid)
    if command is None:
        return holder(profile) if _state.chrome_lock_state(profile)[0] == "free" else \
            Holder("unknown", why=f"PID {pid} holding {profile} could not be looked at")
    if command == "":
        return Holder("unknown", why=f"the command line of PID {pid}, which holds {profile}, could not be read")
    if not any(_watcher.names_profile(command, s) for s in _spellings(profile)):
        # The lock names a PID that now runs something else: a browser that is gone, for
        # example before a restart. Only when no process at all names the profile.
        others = _state.processes_naming(profile)
        if others is None:
            return Holder("unknown", why="the process list could not be read")
        if others:
            return Holder("unknown", pid=others[0][0],
                          why=f"PID {others[0][0]} uses {profile} without holding its lock")
        return Holder("stale", pid=pid, why=f"the lock names PID {pid}, which now runs something else")
    return _judge(profile, pid, command)


def _judge(profile: Path, pid: int, command: str) -> Holder:
    if not _is_main(command):
        return Holder("unknown", pid=pid, why=f"PID {pid} names {profile} but is not a browser")
    found = re.search(r"--remote-debugging-port=(\d+)", command)
    if found and all(re.search(re.escape(flag) + r"(?=\s|$)", command) for flag in _keep_alive.OUR_FLAGS):
        return Holder("ours", pid=pid, port=int(found.group(1)))
    return Holder("foreign", pid=pid, why=(
        f"{profile} is open in a browser this server did not start (PID {pid}). Close it first; "
        "the launcher never opens a second browser on a profile"))


def clear_stale_lock(profile: Path, seen_pid: Optional[int]) -> bool:
    """Remove a SingletonLock that names a PID now running something else. Only the link, only
    while it still names that PID; Chrome itself replaces it on start."""
    lock = Path(profile) / "SingletonLock"
    try:
        if not lock.is_symlink():
            return False
        target = os.readlink(lock)
        if not target.endswith(f"-{seen_pid}"):
            return False
        lock.unlink()
        return True
    except OSError:
        return False


# ── a running browser: forward, and the URL in a new tab ─────────────────────────

_ACTIVATE_JXA = (
    'ObjC.import("AppKit");'
    "function run(argv){"
    "var app=$.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0]));"
    'if(app.isNil())return "none";'
    'return app.activateWithOptions($.NSApplicationActivateAllWindows)?"ok":"refused";}'
)


def activating() -> bool:
    """MCP_STEALTH_LAUNCHER_ACTIVATE=0 leaves the browser behind: for tests, which must not take
    the person's focus. A click brings it forward."""
    return os.environ.get(ENV_ACTIVATE, "1").strip().lower() not in ("0", "false", "no", "off")


def activate_pid(pid: int) -> str:
    """Bring exactly this process to the front (macOS). What NSRunningApplication answered."""
    if not activating():
        return "skipped (MCP_STEALTH_LAUNCHER_ACTIVATE=0)"
    if sys.platform != "darwin":
        return "not macOS"
    try:
        done = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _ACTIVATE_JXA,
                               str(int(pid))], capture_output=True, text=True, timeout=10)
        return (done.stdout or done.stderr or "").strip()[:200] or f"exit {done.returncode}"
    except Exception as exc:
        return f"{type(exc).__name__}"


def _pages(port: int) -> list[dict]:
    body = _watcher.http_get(HOST, port, "/json/list")
    try:
        rows = json.loads((body or b"[]").decode("utf-8", "replace"))
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("type") == "page"]


def bring_forward(found: Holder, url: Optional[str]) -> tuple[int, str]:
    """Show the running browser, with `url` in a new tab when given. Over its own DevTools port,
    which `holder` proved belongs to this profile's browser."""
    port, pid = int(found.port or 0), int(found.pid or 0)
    browser_pid = _keep_alive._browser_pid(HOST, port)
    if browser_pid != pid:
        return EXIT_FAILED, (f"the browser on port {port} did not answer as PID {pid}, so nothing "
                             "was sent to it")
    target = None
    if url:
        made = _watcher.browser_call(HOST, port, "Target.createTarget", params={"url": url, "background": not activating()})
        target = (made or {}).get("targetId")
        if not target:
            return EXIT_FAILED, f"the running browser (PID {pid}) did not open {url}"
    else:
        pages = _pages(port)
        target = pages[0].get("id") if pages else None
        if target is None:
            made = _watcher.browser_call(HOST, port, "Target.createTarget",
                                         params={"url": "about:blank", "newWindow": True,
                                                 "background": not activating()})
            target = (made or {}).get("targetId")
            if not target:
                return EXIT_FAILED, (f"the running browser (PID {pid}) has no window, and opening "
                                     "one failed")
    opened = f"opened {url} in a new tab" if url else "it has a window"
    if not activating():
        return EXIT_OK, (f"already running (PID {pid}, port {port}): {opened}, left behind "
                         f"(MCP_STEALTH_LAUNCHER_ACTIVATE=0)")
    if _watcher.browser_call(HOST, port, "Target.activateTarget", params={"targetId": target}) is None:
        return EXIT_FAILED, (f"already running (PID {pid}, port {port}): {opened}, but Chrome did not "
                             f"answer Target.activateTarget, so it was not brought to the front")
    shown = activate_pid(pid)
    if shown != "ok":
        # Reported as it is: running, but not shown in front.
        return EXIT_FAILED, (f"already running (PID {pid}, port {port}): {opened}, but it could not "
                             f"be brought to the front (macOS answered {shown!r})")
    what = f"opened {url} in a new tab" if url else "brought it forward"
    return EXIT_OK, f"already running (PID {pid}, port {port}): {what}"


# ── a cold start, through browser_launch itself ──────────────────────────────────

def cold_launch(browser: str, url: Optional[str], move_to_beta: bool) -> tuple[int, str, Optional[int]]:
    """browser_launch(browser, keep_alive=True) headed in this process, then the exit path a
    server takes at stdin EOF, which leaves a kept browser running. (code, text, pid)."""
    import asyncio

    import logging

    from . import brave_beta, dock_icon
    from . import server
    if move_to_beta:
        brave_beta.ALLOW_PROFILE_MOVE = True
    # The person opened this one: recorded, and given the longest time cap (keep_alive.cap_hours).
    _keep_alive.OPENED_BY = _keep_alive.LAUNCHER
    logging.getLogger("nodriver").setLevel(logging.WARNING)

    async def launch() -> str:
        return await server.browser_launch(
            url=url or "about:blank", headless=False, keep_alive=True, browser=browser,
            profile_conflict="fail", extra_args=_extra_args() or None)

    try:
        text = asyncio.run(launch())
    finally:
        # What a stdio server does when its client goes: a kept browser stays, any other ends.
        server._stop_all_browsers()
        # An icon an update took off goes back on in a thread; this process ends right after.
        dock_icon.wait(30)
    if text.startswith("Error"):
        return EXIT_FAILED, text, None
    record = _keep_alive.read(profile_for(browser)) or {}
    if record.get("state") != "kept":
        return EXIT_FAILED, (f"the browser started but was not left running for later: {text}"), None
    return EXIT_OK, text, record.get("pid")


def _extra_args() -> list[str]:
    """Extra browser flags from MCP_STEALTH_LAUNCHER_EXTRA_ARGS (a JSON list). For tests, which
    pass --use-mock-keychain so a test browser never asks the person's Keychain."""
    raw = os.environ.get(ENV_EXTRA_ARGS, "").strip()
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{ENV_EXTRA_ARGS} must be a JSON list of strings")
    return value


# ── the open command ──────────────────────────────────────────────────────────────

def _ack_path(browser: str) -> Path:
    return _state.data_root() / f"launcher-acknowledged-{browser}"


def acknowledged(browser: str) -> bool:
    return _ack_path(browser).is_file()


def acknowledge(browser: str) -> None:
    path = _ack_path(browser)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{time.time()}\n")


def open_browser(browser: str, url: Optional[str] = None, *, move_to_beta: bool = False
                 ) -> tuple[int, str]:
    """The whole of `open`, in this process. (exit code, what happened)."""
    why = _keep_alive.supported()
    if why:
        return EXIT_NOT_HERE, f"open is not available here: {why}"
    if browser == brave_mode.BRAVE and brave_mode.find_brave() is None:
        return EXIT_NOT_HERE, f"Brave is not installed here. {brave_mode.install_hint()}"
    profile = profile_for(browser)
    # The same reservation browser_launch and spawn_browser take (launch_lock): a double click, or
    # a click while a server launches on this profile, waits and then finds that browser running.
    with launch_lock.hold([profile], LOCK_WAIT):
        found = holder(profile)
        if found.kind == "ours":
            return bring_forward(found, url)
        if found.kind in ("foreign", "unknown"):
            return EXIT_FAILED, f"nothing was started: {found.why}"
        if found.kind == "stale" and not clear_stale_lock(profile, found.pid):
            return EXIT_FAILED, f"nothing was started: {found.why}, and its lock could not be cleared"
        code, text, pid = cold_launch(browser, url, move_to_beta)
        if code != EXIT_OK:
            # An agent may have started it in the same moment: show that one instead.
            again = holder(profile)
            if again.kind == "ours":
                return bring_forward(again, url)
            return code, text
        shown = activate_pid(pid) if pid else "no PID"
        if activating() and shown != "ok":
            return EXIT_FAILED, (f"started (PID {pid}), but it could not be brought to the front "
                             f"(macOS answered {shown!r}): {text}")
        return EXIT_OK, f"started (PID {pid}): {text}"


def _detach() -> Optional[int]:
    """Run the rest in a child in a session of its own, so the browser it starts never belongs to
    a terminal or to the app that asked: closing either sends it nothing. The parent waits for
    the child and returns its exit code; the child returns None."""
    if os.name != "posix":
        return None
    try:
        if os.getsid(0) == os.getpid():
            return None  # already a session leader
    except OSError:
        return None
    pid = os.fork()
    if pid == 0:
        os.setsid()
        return None
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _url_from(args: argparse.Namespace) -> Optional[str]:
    """The URL to open, or None when it is refused: http and https only, as given. Nothing else
    reaches the browser, and no scheme is guessed (file:, chrome:, brave:, a bare host)."""
    url = (args.url or "").strip()
    return url if re.match(r"^https?://[^\s]", url, re.IGNORECASE) else None


def _open_command(args: argparse.Namespace) -> int:
    browser = args.browser
    if args.url is not None and _url_from(args) is None:
        _err(f"refused: {args.url!r} is not an http:// or https:// URL; nothing was opened")
        return EXIT_FAILED
    if args.acknowledge:
        acknowledge(browser)
    elif args.from_app and not acknowledged(browser):
        _err(FIRST_RUN_NOTE)
        return EXIT_ACKNOWLEDGE
    if args.dock_icon:
        os.environ["MCP_STEALTH_DOCK_ICON"] = "1"
    if args.from_app:
        from . import dock_icon
        dock_icon.ICON_WRITES = False  # an app may not modify another app's bundle (App Management)
    code = _detach()
    if code is not None:
        return code
    try:
        code, text = open_browser(browser, _url_from(args), move_to_beta=args.move_to_beta)
    except TimeoutError as exc:
        code, text = EXIT_FAILED, f"nothing was started: {exc}"
    except Exception as exc:  # the app shows this; a traceback helps nobody there
        code, text = EXIT_FAILED, f"nothing was started: {type(exc).__name__}: {exc}"
    _log(browser, code, text)
    (_out if code == EXIT_OK else _err)(text)
    os._exit(code)  # the child: no interpreter teardown that could reach the kept browser


def _log(browser: str, code: int, text: str) -> None:
    """One line per open in <data>/launcher.log (0600), kept under a megabyte."""
    with contextlib.suppress(Exception):
        path = _state.data_root() / "launcher.log"
        if path.exists() and path.stat().st_size > 1 << 20:
            path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            handle.write(f"{stamp} {browser} exit={code} {_keep_alive_masked(text)}\n")


def _keep_alive_masked(text: str) -> str:
    from .helpers import mask_url
    return mask_url(text.replace("\n", " "))[:2000]


# ── the app ──────────────────────────────────────────────────────────────────────

def launcher_dir() -> Path:
    override = os.environ.get(ENV_DIR, "").strip()
    return Path(override).expanduser() if override else Path.home() / "Applications"


_APPLESCRIPT = r'''
on run
	my launchWith({})
end run

on open location theURL
	my launchWith({"--url", theURL})
end open location

on launchWith(theArgs)
	set runner to quoted form of (POSIX path of (path to me) & "Contents/Resources/launch.sh")
	set argText to ""
	repeat with anArg in theArgs
		set argText to argText & " " & quoted form of (anArg as text)
	end repeat
	try
		do shell script runner & argText
	on error errText number errNum
		if errNum is 3 then
			set answer to display dialog errText buttons {"Cancel", "Open"} default button "Open" with title "__TITLE__" with icon caution
			if button returned of answer is "Open" then
				try
					do shell script runner & " --acknowledge" & argText
				on error errText2
					display dialog errText2 buttons {"OK"} default button "OK" with title "__TITLE__" with icon stop
				end try
			end if
		else
			display dialog errText buttons {"OK"} default button "OK" with title "__TITLE__" with icon stop
		end if
	end try
end launchWith
'''

_LAUNCH_SH = r'''#!/bin/sh
# Finds mcp-stealth-chrome and asks it to open the agent browser. Written by install-launcher.
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$here/env.sh" ] && . "$here/env.sh"
cli=""
for candidate in "$(cat "$here/cli-path" 2>/dev/null)" "$HOME/.local/bin/mcp-stealth-chrome" \
        /opt/homebrew/bin/mcp-stealth-chrome /usr/local/bin/mcp-stealth-chrome; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then cli="$candidate"; break; fi
done
if [ -z "$cli" ]; then
    cli="$(/bin/zsh -lc 'command -v mcp-stealth-chrome' 2>/dev/null | tail -n 1)"
fi
if [ -z "$cli" ] || [ ! -x "$cli" ]; then
    echo "mcp-stealth-chrome is not installed where this launcher looks ($(cat "$here/cli-path" 2>/dev/null), ~/.local/bin, /opt/homebrew/bin, /usr/local/bin, your shell PATH). Install it with: uv tool install mcp-stealth-chrome. Then run: mcp-stealth-chrome install-launcher" >&2
    exit 127
fi
exec "$cli" open --browser "$(cat "$here/browser")" --from-app --dock-icon "$@"
'''

_LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
               "LaunchServices.framework/Support/lsregister")


def _cli_path() -> str:
    """The command this launcher runs: the installed entry point, as found now."""
    found = shutil.which("mcp-stealth-chrome")
    if found:
        return found  # the shim uv keeps up to date across reinstalls, not what it points at now
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    return str(candidate) if candidate and candidate.name == "mcp-stealth-chrome" else ""


def _is_ours(app: Path, browser: str) -> bool:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier") == BUNDLE_IDS[browser]
    except Exception:
        return False


def _make_icns(png: Path, out: Path) -> None:
    """The project PNG as an .icns, with the system's sips and iconutil."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale),
                                str(png), "--out", str(iconset / name)],
                               check=True, capture_output=True, timeout=30)
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       check=True, capture_output=True, timeout=30)


def build_app(browser: str, folder: Optional[Path] = None, cli: Optional[str] = None) -> Path:
    """Build the launcher app in `folder` (launcher_dir()), replacing only a launcher of ours.

    Built beside its final place and renamed in, so a half-built app is never left under the
    name. Raises with the reason when it cannot be built."""
    from .dock_icon import ICON_PATH
    if sys.platform != "darwin":
        raise RuntimeError("the Dock launcher is macOS only; `mcp-stealth-chrome open` works here")
    folder = Path(folder or launcher_dir()).expanduser()
    target = folder / APP_NAMES[browser]
    if target.is_symlink() or (target.exists() and not _is_ours(target, browser)):
        raise RuntimeError(f"{target} exists and is not a launcher this command built; left alone")
    folder.mkdir(parents=True, exist_ok=True)
    cli = cli if cli is not None else _cli_path()
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=str(folder)))
    try:
        app = staging / APP_NAMES[browser]
        with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as src:
            src.write(_APPLESCRIPT.replace("__TITLE__", TITLES[browser]))
        try:
            subprocess.run(["/usr/bin/osacompile", "-o", str(app), src.name], check=True,
                           capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(src.name)
        resources = app / "Contents" / "Resources"
        _make_icns(ICON_PATH, resources / "applet.icns")
        with contextlib.suppress(FileNotFoundError):
            (resources / "Assets.car").unlink()  # its own icon would win over applet.icns
        info_path = app / "Contents" / "Info.plist"
        with open(info_path, "rb") as handle:
            info = plistlib.load(handle)
        info.pop("CFBundleIconName", None)
        info.update({
            "CFBundleIdentifier": BUNDLE_IDS[browser],
            "CFBundleName": TITLES[browser],
            "CFBundleDisplayName": TITLES[browser],
            "CFBundleIconFile": "applet",
            "LSUIElement": True,  # it exits in a moment: only the browser shows in the Dock
            "CFBundleURLTypes": [{"CFBundleURLName": "Web site URL",
                                  "CFBundleURLSchemes": ["http", "https"]}],
            "NSHumanReadableCopyright": "mcp-stealth-chrome launcher",
        })
        with open(info_path, "wb") as handle:
            plistlib.dump(info, handle)
        (resources / "launch.sh").write_text(_LAUNCH_SH, encoding="utf-8")
        os.chmod(resources / "launch.sh", 0o755)
        (resources / "cli-path").write_text(cli + "\n", encoding="utf-8")
        (resources / "browser").write_text(browser + "\n", encoding="utf-8")
        carried = [f"export {name}={_sh_quote(os.environ[name])}" for name in CARRIED_ENV
                   if os.environ.get(name)]
        (resources / "env.sh").write_text("\n".join(carried) + ("\n" if carried else ""),
                                          encoding="utf-8")
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
                       check=True, capture_output=True, text=True, timeout=60)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                       check=True, capture_output=True, text=True, timeout=60)
        if target.exists():
            _unregister(target)
            shutil.rmtree(target)
        os.replace(app, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    with contextlib.suppress(Exception):
        subprocess.run([_LSREGISTER, "-f", str(target)], capture_output=True, timeout=30)
    return target


def remove_app(browser: str, folder: Optional[Path] = None) -> str:
    target = Path(folder or launcher_dir()).expanduser() / APP_NAMES[browser]
    if target.is_symlink() or not target.exists():
        return f"{target} is not there"
    if not _is_ours(target, browser):
        return f"{target} is not a launcher this command built; left alone"
    _unregister(target)
    shutil.rmtree(target)
    return f"removed {target}"


def _unregister(app: Path) -> None:
    with contextlib.suppress(Exception):
        subprocess.run([_LSREGISTER, "-u", str(app)], capture_output=True, timeout=30)


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


INSTALL_NOTE = (
    "Built {app}.\n"
    "- Pin it: drag it from {folder} to the Dock. A click opens the agent's {name} (or brings it "
    "forward), and the launcher exits at once; while it runs the Dock shows only the browser.\n"
    "- Default browser: System Settings > Desktop & Dock > Default web browser > {title}. Links "
    "from other apps then open in the agent's {name}. Nothing was changed for you.\n"
    "- It starts a browser with a DevTools port on 127.0.0.1 that any program running as you "
    "can control, without an agent asking; it asks you once before its first start.")


def _install_command(args: argparse.Namespace) -> int:
    if args.remove:
        _out(remove_app(args.browser, args.dir))
        return EXIT_OK
    try:
        app = build_app(args.browser, args.dir)
    except subprocess.CalledProcessError as exc:
        _err(f"the launcher could not be built: {' '.join(map(str, exc.cmd))[:200]} failed: "
             f"{(exc.stderr or exc.stdout or '').strip()[:400]}")
        return EXIT_FAILED
    except Exception as exc:
        _err(f"the launcher could not be built: {exc}")
        return EXIT_NOT_HERE if sys.platform != "darwin" else EXIT_FAILED
    name = "Brave" if args.browser == brave_mode.BRAVE else "Chrome"
    _out(INSTALL_NOTE.format(app=app, folder=app.parent, name=name, title=TITLES[args.browser]))
    if not (app / "Contents" / "Resources" / "cli-path").read_text().strip():
        _out("- Note: the installed mcp-stealth-chrome command was not found on PATH; the launcher "
             "looks in ~/.local/bin and your shell PATH when clicked.")
    return EXIT_OK


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mcp-stealth-chrome")
    sub = parser.add_subparsers(dest="command", required=True)
    opener = sub.add_parser("open", help="open the agent browser, or bring it forward")
    opener.add_argument("--browser", choices=brave_mode.BROWSERS, default=brave_mode.CHROME)
    opener.add_argument("--url", help="open this URL (a new tab when the browser is running)")
    opener.add_argument("--dock-icon", action="store_true",
                        help="run from the Beta channel with the project icon (MCP_STEALTH_DOCK_ICON=1)")
    opener.add_argument("--move-to-beta", action="store_true",
                        help="let Brave Beta take a profile Brave made (one way; its cookies are moved)")
    opener.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    opener.add_argument("--acknowledge", action="store_true", help=argparse.SUPPRESS)
    installer = sub.add_parser("install-launcher", help="build the Dock launcher app (macOS)")
    installer.add_argument("--browser", choices=brave_mode.BROWSERS, default=brave_mode.BRAVE)
    installer.add_argument("--dir", type=Path, default=None,
                           help=f"where to put it (default ~/Applications, or ${ENV_DIR})")
    installer.add_argument("--remove", action="store_true", help="remove the launcher this built")
    args = parser.parse_args(argv)
    if args.command == "open":
        return _open_command(args)
    return _install_command(args)
