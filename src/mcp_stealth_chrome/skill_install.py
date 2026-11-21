"""`mcp-stealth-chrome install-skill`: put the stealth-chrome Claude Code skill where Claude Code
reads skills, so `/stealth-chrome` works and agents learn the tools before using them.

The skill ships inside the package (assets/claude-skill/SKILL.md). It names no AI model and
carries no key: the vision provider for CAPTCHA tools is the user's own (BYOK).

Where it goes, first match wins:
  --dir PATH                     PATH/stealth-chrome/SKILL.md
  --project                      ./.claude/skills/stealth-chrome/SKILL.md
  $CLAUDE_CONFIG_DIR/skills      when that variable is set
  ~/.claude/skills               otherwise

A copy the person edited is never replaced without --force, and then the old one is kept beside it
as SKILL.md.bak.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

NAME = "stealth-chrome"

# sha256 of every SKILL.md an earlier release shipped. A copy that still matches one was never
# edited, so an upgrade replaces it without --force and keeps no .bak. Add the outgoing version's
# hash here whenever SKILL.md changes (tests/test_skill_install.py checks it is listed).
SHIPPED = {
    "11cef8e6829d86f2659cb40b7f7939404f1aa49e607bdc3a8d1a5e1475ed9472",  # 0.25.11
    "c1cf3b05784dcb4dcf0c25c8aebe08ecf87e8360bab86b858dc53103048a3008",  # 0.25.12 to 0.25.14
    "bacb72dddab1b30e54f5dfbbfff7f0ff6ffa38789ae4f999142e5c60209434de",  # 0.25.15 to 0.25.17
}


def bundled() -> Path:
    return Path(__file__).with_name("assets") / "claude-skill" / "SKILL.md"


def skills_root(project: bool = False, directory: Optional[str] = None) -> Path:
    if directory:
        return Path(directory).expanduser()
    if project:
        return Path.cwd() / ".claude" / "skills"
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "skills"


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def install(project: bool = False, directory: Optional[str] = None, force: bool = False) -> tuple[int, str]:
    """(exit code, message). 0 installed or already current, 2 refused."""
    text = bundled().read_text(encoding="utf-8")
    target = skills_root(project, directory) / NAME / "SKILL.md"
    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if current == text:
            return 0, f"already installed and current: {target}"
        if hashlib.sha256(current.encode("utf-8")).hexdigest() in SHIPPED:
            _write(target, text)
            return 0, f"updated {target} from an earlier version (it had not been edited)"
        if not force:
            return 2, (f"refused: {target} differs from this version's skill (edited, or from another "
                       "version). Run again with --force to replace it; the old file is kept as SKILL.md.bak.")
        _write(target.with_name("SKILL.md.bak"), current)
        _write(target, text)
        return 0, f"replaced {target} (the previous one is SKILL.md.bak beside it)"
    _write(target, text)
    return 0, f"installed {target}"


def cli(argv: list[str]) -> int:
    """`mcp-stealth-chrome install-skill [--project | --dir PATH] [--force] [--print]`."""
    args = list(argv)
    if "-h" in args or "--help" in args:
        print("usage: mcp-stealth-chrome install-skill [--project | --dir PATH] [--force] [--print]\n"
              "Installs the stealth-chrome skill for Claude Code. Then type /stealth-chrome in a new session.")
        return 0
    if "--print" in args:
        print(bundled().read_text(encoding="utf-8"), end="")
        return 0
    directory = None
    if "--dir" in args:
        i = args.index("--dir")
        if i + 1 >= len(args):
            print("refused: --dir needs a path")
            return 2
        directory = args[i + 1]
    code, message = install(project="--project" in args, directory=directory, force="--force" in args)
    print(message)
    if code == 0:
        print("Type /stealth-chrome in Claude Code (a new session picks it up). Agents also load it on their own "
              "when a task needs the stealth browser.")
    return code
