#!/usr/bin/env python3
"""Week 1 skill checks (PR 1.5), asserted against this repo's layout.

The file used to compute its root as ``parents[3]`` (a vendored layout), so
every path it checked was missing and its tests returned True/False without
an assert: they always passed (OI-2818).

Layout facts these tests rely on:
- Skills live at ``.claude/skills/<name>/SKILL.md``. The ``@<name>.md`` names
  and ``.claude/vnx-system/`` never existed in this repo's git history
  (``git log --all -S'@planner.md'`` finds no non-test commit), so there is
  no rename commit to cite.
- ``dispatcher_v7_compilation.sh`` was never tracked here; the dispatcher is
  ``scripts/dispatcher_minimal.sh``.
- The skill-vs-template token-reduction test and the skill-first/template
  fallback test are deleted: ``.claude/terminals/library/templates/agents/``
  and the ``load_agent_content`` fallback never existed in this repo either.
  The related template scaffolds were retired in 0f91e46b4 (B5, #2261).
"""
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
DISPATCHER = PROJECT_ROOT / "scripts" / "dispatcher_minimal.sh"

REQUIRED_SKILLS = [
    "planner",
    "backend-developer",
    "api-developer",
    "frontend-developer",
    "test-engineer",
    "reviewer",
    "architect",
]


def count_tokens_estimate(content: str) -> int:
    """Conservative estimate: 1 token is about 4 characters."""
    return len(content) // 4 if content else 0


def test_project_root_is_this_repo():
    assert (PROJECT_ROOT / "scripts").is_dir()
    assert SKILLS_DIR.is_dir(), f"skills dir missing: {SKILLS_DIR}"


@pytest.mark.parametrize("skill", REQUIRED_SKILLS)
def test_skill_loading(skill):
    skill_path = SKILLS_DIR / skill / "SKILL.md"
    assert skill_path.is_file(), f"skill not found: {skill_path}"
    tokens = count_tokens_estimate(skill_path.read_text())
    assert tokens >= 300, f"{skill}: {tokens} tokens (below 300 minimum)"


def test_planning_mode():
    """The planner skill exists and the dispatcher detects planning mode."""
    assert (SKILLS_DIR / "planner" / "SKILL.md").is_file()
    assert DISPATCHER.is_file(), f"dispatcher not found: {DISPATCHER}"
    content = DISPATCHER.read_text()
    assert "extract_mode()" in content
    assert '"$mode" = "planning"' in content
    assert "Planning mode detected" in content
