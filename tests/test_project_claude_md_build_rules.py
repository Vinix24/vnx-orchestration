"""The four build rules must reach workers that only read the worktree CLAUDE.md.

Harness-lane (glm, deepseek), kimi and codex workers never load the operator's
global ~/.claude/CLAUDE.md, so the rules live in the project file, outside the
managed bootstrap block.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

BEGIN = "<!-- VNX:BEGIN BOOTSTRAP -->"
END = "<!-- VNX:END BOOTSTRAP -->"

# Bootstrap block as it stands on main d92e8f17 (lines 1-13, newline-joined).
BOOTSTRAP_LINES = 13
BOOTSTRAP_SHA256 = "a408bb4e7f08ec2c40b0e91165081d8bf4cf2ab8fe1c66c882b2aff7c713bfa0"


def _text() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def _bootstrap_block(text: str) -> str:
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    return text[start:end]


def _build_rules_section() -> str:
    text = _text()
    outside = text.replace(_bootstrap_block(text), "")
    match = re.search(r"^## Build rules[ \t]*\n(.*?)(?=^## |\Z)", outside, re.S | re.M)
    assert match, "CLAUDE.md has no '## Build rules' section outside the bootstrap block"
    return match.group(1)


def test_b1_build_rules_section_carries_the_four_rules():
    section = _build_rules_section()
    bullets = [line for line in section.splitlines() if line.lstrip().startswith("- ")]
    assert len(bullets) == 4, f"expected four bullets, found {len(bullets)}"
    for needle in ("TODO", "mock", "tests/", "_v2"):
        assert needle in section, f"build rule marker {needle!r} missing from section"


def test_b1_section_is_not_inside_the_bootstrap_block():
    text = _text()
    assert "## Build rules" not in _bootstrap_block(text)


def test_b2_bootstrap_block_is_unchanged():
    block = _bootstrap_block(_text())
    lines = block.split("\n")
    assert len(lines) == BOOTSTRAP_LINES
    assert lines[0] == BEGIN
    assert lines[-1] == END
    assert hashlib.sha256(block.encode("utf-8") + b"\n").hexdigest() == BOOTSTRAP_SHA256
