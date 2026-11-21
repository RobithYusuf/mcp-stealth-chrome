"""The person's settings: one small file, changed where they are seen, read at call time.

Order, highest first: the environment (an MCP server's config, a test), `~/.mcp-stealth/settings.json`
(`MCP_STEALTH_SETTINGS` names another file), the default. An environment value is the explicit
choice of whoever configured that server, so it wins; the file is what the person picks from the
overlay's menu or with `mcp-stealth-chrome settings`, and it applies without a restart.

Only the settings a person would change are here. Paths, test switches and tuning limits stay
environment variables (README, "Advanced settings").

Standard library only: the overlay process reads the same file and has no access to this package.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ENV_FILE = "MCP_STEALTH_SETTINGS"
TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Setting:
    key: str
    env: str
    kind: str  # "bool", "choice", "number" or "rules"
    default: Any
    help: str
    choices: tuple = ()
    low: float = 0.0
    high: float = 0.0


SETTINGS = (
    Setting("agent_overlay", "MCP_STEALTH_AGENT_OVERLAY", "bool", False,
            "Show the overlay on a visible browser the agent drives: a light, a bar, Take over (macOS)"),
    Setting("overlay_bar", "MCP_STEALTH_OVERLAY_BAR", "choice", "full",
            "The overlay's bar: full, compact, hover (only while the pointer is over the window) or off",
            choices=("full", "compact", "hover", "off")),
    Setting("overlay_light", "MCP_STEALTH_OVERLAY_LIGHT", "bool", True,
            "The light around the window while the agent works"),
    Setting("overlay_lock", "MCP_STEALTH_OVERLAY_LOCK", "bool", True,
            "Keep clicks off the page while the agent works (always off when the bar is off)"),
    Setting("agent_cursor", "MCP_STEALTH_AGENT_CURSOR", "bool", False,
            "Show the agent's pointer in the overlay"),
    Setting("overlay_hide_after", "MCP_STEALTH_OVERLAY_HIDE_AFTER", "number", 90.0,
            "Seconds without a tool call before the overlay fades out", low=1.0, high=86400.0),
    Setting("dock_icon", "MCP_STEALTH_DOCK_ICON", "bool", False,
            "Run visible browsers from the Beta channel: Chrome Beta with the project icon, Brave Beta "
            "with its own (macOS)"),
    Setting("antibot_tripwire", "MCP_STEALTH_ANTIBOT_TRIPWIRE", "choice", "pause",
            "A verification page (a site's bot check) after a navigation or between workflow steps: pause "
            "(stop and ask the person), warn (say so and go on) or off",
            choices=("pause", "warn", "off")),
    Setting("antibot_rules", "MCP_STEALTH_ANTIBOT_RULES", "rules", {},
            "Your own verification pages, per host: "
            '{"example.com": {"url": ["^/verify/"], "text": ["Verify to continue"]}}; '
            "url holds regular expressions searched in the path and query, text plain words of the page"),
)
BY_KEY = {s.key: s for s in SETTINGS}


def path() -> Path:
    given = os.environ.get(ENV_FILE, "").strip()
    if given:
        return Path(given).expanduser()
    from .state import data_root
    return data_root() / "settings.json"


def parse(setting: Setting, raw: Any) -> tuple[bool, Any]:
    """(ok, value) for a raw value from the environment, the file or the command line."""
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return True, raw
        text = str(raw).strip().lower()
        if text in TRUE:
            return True, True
        if text in FALSE:
            return True, False
        return False, None
    if setting.kind == "choice":
        text = str(raw).strip().lower()
        return (True, text) if text in setting.choices else (False, None)
    if setting.kind == "rules":
        return parse_rules(raw)
    if setting.kind == "number":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False, None
        if value != value or not setting.low <= value <= setting.high:  # NaN, or out of range
            return False, None
        return True, value
    return False, None


RULE_HOSTS = 50        # at most this many hosts in antibot_rules
RULE_PATTERNS = 20     # patterns in one host's url list, and in its text list
RULE_LENGTH = 200      # characters in one pattern
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")


def parse_rules(raw: Any) -> tuple[bool, Any]:
    """antibot_rules: a JSON object (or its text) {host: {"url": [regex], "text": [words]}}. The
    whole value is refused when any part is wrong: a rule taken in half would stop runs, or miss
    pages, in ways nobody asked for. Hosts come back lower case; the lists as given."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return False, None
    if not isinstance(data, dict) or len(data) > RULE_HOSTS:
        return False, None
    out: dict = {}
    for host, rule in data.items():
        name = str(host).strip().lower()
        if not _HOST.match(name) or not isinstance(rule, dict) or not rule:
            return False, None
        if set(rule) - {"url", "text"}:
            return False, None
        kept: dict = {}
        for key in ("url", "text"):
            items = rule.get(key, [])
            if not isinstance(items, list) or len(items) > RULE_PATTERNS:
                return False, None
            for item in items:
                if not isinstance(item, str) or not item.strip() or len(item) > RULE_LENGTH:
                    return False, None
                if key == "url":
                    try:
                        re.compile(item)
                    except re.error:
                        return False, None
            if items:
                kept[key] = list(items)
        if not kept:
            return False, None
        out[name] = kept
    return True, out


def read_file(target: Optional[Path] = None) -> dict:
    """The file's settings, or {}: missing, a link, not a regular file, someone else's, or not a
    JSON object all read as no settings, never as an error."""
    target = target or path()
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return data if isinstance(data, dict) else {}


def resolve(key: str, file_data: Optional[dict] = None) -> dict:
    """{"value", "source", "note"?}: the environment, then the file, then the default. A value
    that does not parse is skipped with a note, never applied."""
    setting = BY_KEY[key]
    notes = []
    raw_env = os.environ.get(setting.env, "").strip()
    if raw_env:
        ok, value = parse(setting, raw_env)
        if ok:
            return {"value": value, "source": f"env {setting.env}"}
        notes.append(f"{setting.env}={raw_env!r} is not valid, ignored")
    data = read_file() if file_data is None else file_data
    if key in data:
        ok, value = parse(setting, data[key])
        if ok:
            out = {"value": value, "source": "file"}
            if notes:
                out["note"] = "; ".join(notes)
            return out
        notes.append(f"{data[key]!r} in the settings file is not valid, ignored")
    out = {"value": setting.default, "source": "default"}
    if notes:
        out["note"] = "; ".join(notes)
    return out


def get(key: str) -> Any:
    return resolve(key)["value"]


def report() -> dict:
    """Every setting with its value and where it came from, for server_status and the command."""
    data = read_file()
    return {"file": str(path()), **{s.key: resolve(s.key, data) for s in SETTINGS}}


@contextlib.contextmanager
def _one_writer(target: Path):
    """An exclusive flock beside the file for the whole read, merge and rename: two writers at
    once (the menu of two overlays, or the menu and the command) each read the old file and the
    second rename dropped the first one's choice. The overlay process takes the same lock.
    Without fcntl (Windows) writes go on unlocked, as before."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    fd = os.open(target.parent / f".{target.name}.lock",
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write(changes: dict, target: Optional[Path] = None) -> None:
    """Merge `changes` (key -> value, None removes it) into the file: a fresh 0600 file beside it,
    then a rename, so a reader sees the old file or the whole new one."""
    target = target or path()
    folder = target.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise OSError(f"{target} is a link; not written through")
    with _one_writer(target):
        _merge_and_replace(changes, target)


def _merge_and_replace(changes: dict, target: Path) -> None:
    data = read_file(target)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    folder = target.parent
    tmp = folder / f".{target.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_value(key: str, raw: Any) -> str:
    """Store one setting. Returns what to tell the person; raises ValueError when it is refused."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise ValueError(f"unknown setting {key!r}; the settings are: {', '.join(BY_KEY)}")
    ok, value = parse(setting, raw)
    if not ok:
        allowed = ("true or false" if setting.kind == "bool" else ", ".join(setting.choices)
                   if setting.kind == "choice" else
                   'a JSON object {"host": {"url": ["regex"], "text": ["words"]}} (at most '
                   f"{RULE_HOSTS} hosts, {RULE_PATTERNS} patterns a list, {RULE_LENGTH} characters a "
                   "pattern, each regex valid)" if setting.kind == "rules"
                   else f"a number from {setting.low:g} to {setting.high:g}")
        raise ValueError(f"{raw!r} is not a valid {key}: use {allowed}")
    write({key: value})
    shadowed = os.environ.get(setting.env, "").strip()
    note = (f"; {setting.env} is set in this environment and still wins where it is set"
            if shadowed else "")
    return f"{key} = {_shown(value)} saved to {path()}{note}"


def _shown(value: Any) -> str:
    if isinstance(value, dict):
        return f"{len(value)} host" + ("" if len(value) == 1 else "s")
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def cli(argv: list[str]) -> int:
    """`mcp-stealth-chrome settings [list | set KEY VALUE | reset [KEY]]`."""
    action = argv[0] if argv else "list"
    if action in ("list", "show"):
        info = report()
        print(f"Settings file: {info['file']}  (an environment variable wins over it)")
        print("Values as this terminal sees them. A server's MCP config can set the same variables")
        print("for that server only; its server_status.settings shows what it uses.\n")
        width = max(len(s.key) for s in SETTINGS)
        for s in SETTINGS:
            r = info[s.key]
            line = f"  {s.key:<{width}}  {_shown(r['value']):<8} ({r['source']})"
            if r.get("note"):
                line += f"  ! {r['note']}"
            print(line)
            print(f"  {'':<{width}}  {s.help}")
        print("\nChange one:  mcp-stealth-chrome settings set overlay_bar compact")
        print("Back to the default:  mcp-stealth-chrome settings reset overlay_bar  (or reset with no name for all)")
        return 0
    if action == "set" and len(argv) == 3:
        try:
            print(set_value(argv[1], argv[2]))
        except (ValueError, OSError) as exc:
            print(f"refused: {exc}")
            return 2
        return 0
    if action == "reset" and len(argv) in (1, 2):
        keys = [argv[1]] if len(argv) == 2 else list(BY_KEY)
        unknown = [k for k in keys if k not in BY_KEY]
        if unknown:
            print(f"refused: unknown setting {unknown[0]!r}; the settings are: {', '.join(BY_KEY)}")
            return 2
        write({k: None for k in keys})
        print(f"back to the default: {', '.join(keys)}")
        return 0
    print("usage: mcp-stealth-chrome settings [list | set KEY VALUE | reset [KEY]]")
    return 2
