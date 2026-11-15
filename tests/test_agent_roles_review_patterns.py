"""The review-patterns section reaches each builder (read as text, no process, no network)."""

from pathlib import Path

import pytest

from scripts.lib.prompt_assembler import PromptAssembler
from scripts.lib.skill_context import _has_prompt_assembler_role

REPO = Path(__file__).resolve().parents[1]
ASSEMBLER_ROLES = ["backend-developer", "security-engineer"]
LEGACY_ROLES = ["quality-engineer"]
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


def _section_of(text: str) -> str:
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _assembler_text(role: str) -> str:
    content, resolved = PromptAssembler()._load_role(role)
    assert resolved == role
    return content


def _legacy_text(role: str) -> str:
    return (REPO / "agents" / role / "CLAUDE.md").read_text()


def _reaching(role: str) -> str:
    return _assembler_text(role) if role in ASSEMBLER_ROLES else _legacy_text(role)


def _all_sources() -> dict[str, str]:
    sources = {f"loader:{r}": _assembler_text(r) for r in ASSEMBLER_ROLES}
    for r in ASSEMBLER_ROLES + LEGACY_ROLES:
        sources[f"agents:{r}"] = _legacy_text(r)
    return sources


@pytest.mark.parametrize("role", ASSEMBLER_ROLES)
def test_loader_role_text_carries_section(role):
    assert HEADING in _assembler_text(role)


@pytest.mark.parametrize("role", LEGACY_ROLES)
def test_legacy_role_has_no_assembler_prompt_and_file_carries_section(role):
    assert _has_prompt_assembler_role(role) is False
    assert HEADING in _legacy_text(role)


@pytest.mark.parametrize("role", ASSEMBLER_ROLES + LEGACY_ROLES)
def test_section_has_six_patterns_in_order_with_proof(role):
    lines = [ln for ln in _section_of(_reaching(role)).splitlines() if ln.startswith("- ")]
    assert len(lines) == 6
    for line, lead in zip(lines, LEADS):
        assert line.startswith(lead)
        assert "Proof:" in line


@pytest.mark.parametrize("role", ASSEMBLER_ROLES + LEGACY_ROLES)
def test_section_closing_lines(role):
    section = _section_of(_reaching(role))
    for closer in CLOSERS:
        assert closer in section


@pytest.mark.parametrize("role", ASSEMBLER_ROLES + LEGACY_ROLES)
def test_agents_copy_directly_under_constraints_and_short(role):
    text = _legacy_text(role)
    before = text.split(HEADING, 1)[0]
    assert before.rstrip().splitlines()[-1].startswith("- No Anthropic SDK")
    assert len(text.splitlines()) < 60


def test_section_identical_in_all_five_files():
    sections = {name: _section_of(t).strip() for name, t in _all_sources().items()}
    assert len(sections) == 5
    assert len(set(sections.values())) == 1, sorted(sections)
