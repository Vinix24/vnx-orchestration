#!/usr/bin/env python3
"""tests/test_adr005_scope_prompts.py: the ADR-005 scope amendment reaches the role prompts.

Operator decision 2026-09-26 (option C): ADR-005 covers decisions and transitions,
not derived state, and ``t0_receipts.ndjson`` is a valid canonical ledger. Three codex
gates on one day (#1921, #1923, #1924) warned on a stricter reading that lived in the
reviewer role prompts ("state mutations must be recorded as NDJSON events in
``.vnx-data/events/``"). These tests pin the amended wording so a later rewrite of a
prompt cannot fall back to the strict variant without a red test.

The prompts are read through ``PromptAssembler.assemble``, the path the gates take, so
the test sees what the reviewer sees and not only what is on disk.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from prompt_assembler import PromptAssembler  # noqa: E402

ADR_PATH = VNX_ROOT / "docs" / "governance" / "decisions" / "ADR-005-ndjson-audit-ledger-primary.md"
ROLES_DIR = SCRIPTS_DIR / "lib" / "prompts" / "roles"

# Roles whose prompt carries an ADR-005 section of its own.
ROLES_WITH_ADR005_SECTION = ("reviewer", "security-engineer", "database-engineer")

_ADR005_SECTION_RE = re.compile(
    r"\*\*ADR-005 — NDJSON audit ledger\*\*.*?(?=\n\s*\n|\n- \*\*|\Z)",
    re.DOTALL,
)


def adr005_section(text: str) -> str:
    """Return the ADR-005 section of a role prompt with whitespace collapsed (line wrapping
    is layout, not contract), or raise when the prompt has no such section."""
    match = _ADR005_SECTION_RE.search(text)
    if match is None:
        raise ValueError("no ADR-005 section found in role prompt text")
    return re.sub(r"\s+", " ", match.group(0))


def _assembled_context(role: str) -> str:
    return PromptAssembler().assemble({"role": role}, "review this diff").context


@pytest.mark.parametrize("role", ROLES_WITH_ADR005_SECTION)
def test_role_prompt_names_scope(role: str) -> None:
    section = adr005_section(_assembled_context(role)).lower()
    assert "t0_receipts" in section, f"{role}: ADR-005 section must name t0_receipts as a valid ledger"
    assert "derived" in section, f"{role}: ADR-005 section must exempt derived caches"
    assert "2026-09-26" in section, f"{role}: ADR-005 section must carry the amendment date"


def test_reviewer_keeps_warning_severity_for_unlogged_decision() -> None:
    section = adr005_section(_assembled_context("reviewer"))
    assert "severity: warning" in section
    assert "recorded in no canonical ledger" in section


def test_reviewer_states_the_one_sentence_test() -> None:
    section = adr005_section(_assembled_context("reviewer"))
    assert "Does this write drive a decision that is recorded in no canonical ledger?" in section


@pytest.mark.parametrize("role", ROLES_WITH_ADR005_SECTION)
def test_events_ring_buffer_is_not_a_required_destination(role: str) -> None:
    section = adr005_section(_assembled_context(role))
    assert "ring buffer" in section
    assert "not a required destination" in section


def test_no_role_prompt_mandates_the_events_directory_for_state_mutations() -> None:
    """The strict phrasings that caused OI-1867/1870/1871 must not come back."""
    strict = re.compile(
        r"(state mutations?|decisions? or state transitions?)[^\n.]*"
        r"(must|should)[^\n.]*(emit|be recorded|be written)[^\n.]*`\.vnx-data/events/`",
        re.IGNORECASE,
    )
    offenders = [
        path.name
        for path in sorted(ROLES_DIR.glob("*.md"))
        if strict.search(re.sub(r"\s+", " ", path.read_text()))
    ]
    assert offenders == [], f"role prompts fell back to the strict ADR-005 reading: {offenders}"


def test_adr_carries_the_scope_amendment_first() -> None:
    text = ADR_PATH.read_text()
    amendments = text.split("## Amendments", 1)[1].split("## Context", 1)[0]
    first = amendments.strip().splitlines()[0]
    assert first.startswith(
        "**2026-09-26 — Scope: decisions and transitions, not derived state (operator, option C).**"
    )
    for token in ("OI-1867", "OI-1870", "OI-1871", "t0_receipts.ndjson", "derived"):
        assert token in amendments, f"amendment must mention {token}"


def test_adr_ledger_list_points_at_the_real_register_path() -> None:
    text = ADR_PATH.read_text()
    decision = text.split("## Decision", 1)[1].split("## Reasoning", 1)[0]
    assert "`.vnx-data/state/dispatch_register.ndjson`" in decision
    assert "`.vnx-data/dispatch_register.ndjson`" not in decision


def test_adr005_section_raises_when_the_role_has_no_section() -> None:
    with pytest.raises(ValueError, match="no ADR-005 section"):
        adr005_section("# Role: Something\n\nNo architecture decision records here.\n")
