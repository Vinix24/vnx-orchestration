"""The builder role files carry the review-patterns section (read as text, no process)."""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROLES = ["backend-developer", "security-engineer", "quality-engineer"]
HEADING = "## What review finds most"
LEADS = [
    "- Fails open:",
    "- Race:",
    "- A new failure breaks an existing caller:",
    "- Append-only ledger write:",
    "- Wrong tenant, dispatch or file selected:",
    "- Unvalidated id or path:",
]
CLOSERS = [
    "Every new `except`, fallback or default has a test that shows the failure stays visible",
    "`# vnx-silent-except: <reason>` marker is for a path that must never raise",
    "When you add an exception or a new return value, list every caller (`rg`) in the report",
]


def _section(role: str) -> str:
    text = (REPO / "agents" / role / "CLAUDE.md").read_text()
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("role", ROLES)
def test_section_has_six_patterns_in_order_with_proof(role):
    lines = [ln for ln in _section(role).splitlines() if ln.startswith("- ")]
    assert len(lines) == 6
    for line, lead in zip(lines, LEADS):
        assert line.startswith(lead)
        assert "Proof:" in line


@pytest.mark.parametrize("role", ROLES)
def test_section_closing_lines(role):
    section = _section(role)
    for closer in CLOSERS:
        assert closer in section


@pytest.mark.parametrize("role", ROLES)
def test_section_directly_under_constraints_and_short(role):
    text = (REPO / "agents" / role / "CLAUDE.md").read_text()
    before = text.split(HEADING, 1)[0]
    assert before.rstrip().splitlines()[-1].startswith("- No Anthropic SDK")
    assert len(text.splitlines()) < 60


def test_section_identical_in_all_roles():
    assert len({_section(r).strip() for r in ROLES}) == 1
