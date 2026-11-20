"""The stealth-chrome Claude Code skill: it ships in the package, installs where Claude Code reads
skills, never replaces a copy the person edited, and stays true to the tools that exist."""
import re
import sys

import pytest

from mcp_stealth_chrome import launcher, skill_install

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_registry import EXPECTED_TOOLS  # noqa: E402

TEXT = skill_install.bundled().read_text(encoding="utf-8")


def test_the_skill_ships_with_a_name_and_a_description():
    front = TEXT.split("---")[1]
    assert re.search(r"^name: stealth-chrome$", front, re.M)
    description = re.search(r"^description: (.+)$", front, re.M).group(1)
    assert "stealth" in description and len(description) < 1024


def test_every_tool_the_skill_names_exists():
    """A tool renamed or removed must not live on in the skill as advice."""
    called = set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)\(", TEXT))
    assert {"browser_launch", "solve_recaptcha_ai", "request_human_help"} <= called
    unknown = sorted(called - EXPECTED_TOOLS)
    assert not unknown, f"the skill names tools that do not exist: {unknown}"


def test_the_skill_carries_no_key_and_names_no_model():
    """BYOK: every user brings their own vision provider. AGENTS.md: name no specific AI model."""
    lowered = TEXT.lower()
    for word in ("patungin", "sanggar", "sk-", "bearer "):
        assert word not in lowered
    assert not re.search(r"\b(gpt-\d|claude-\w|gemini-\d|llama-?\d)", lowered)
    assert "—" not in TEXT  # docs: no em dashes


def test_install_writes_the_skill_and_is_idempotent(tmp_path):
    code, msg = skill_install.install(directory=str(tmp_path))
    target = tmp_path / "stealth-chrome" / "SKILL.md"
    assert code == 0 and "installed" in msg and target.read_text() == TEXT
    code, msg = skill_install.install(directory=str(tmp_path))
    assert code == 0 and "already installed" in msg


def test_an_edited_copy_is_kept_unless_forced(tmp_path):
    target = tmp_path / "stealth-chrome" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own notes")
    code, msg = skill_install.install(directory=str(tmp_path))
    assert code == 2 and "refused" in msg and target.read_text() == "my own notes"
    code, msg = skill_install.install(directory=str(tmp_path), force=True)
    assert code == 0 and target.read_text() == TEXT
    assert (target.parent / "SKILL.md.bak").read_text() == "my own notes"


def test_where_it_goes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert skill_install.skills_root() == tmp_path / "cfg" / "skills"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert skill_install.skills_root().parts[-2:] == (".claude", "skills")
    monkeypatch.chdir(tmp_path)
    assert skill_install.skills_root(project=True) == tmp_path / ".claude" / "skills"


def test_the_command_is_reached_from_the_entry_point(tmp_path, capsys):
    assert "install-skill" in launcher.COMMANDS
    assert launcher.cli(["install-skill", "--dir", str(tmp_path)]) == 0
    assert "/stealth-chrome" in capsys.readouterr().out
    assert launcher.cli(["install-skill", "--dir"]) == 2
