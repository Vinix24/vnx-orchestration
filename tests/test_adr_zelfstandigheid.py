"""Contracttest voor ADR-056 (zelfstandigheid meten, track `zelfstandigheid-meten`, PR-1).

Een contracttest op tekst, geen gedragsproef: hij legt vast dat de ADR de woorden
draagt waar PR-2 tot en met PR-8b van de track tegenaan bouwen.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "governance" / "decisions"
ADR_NUMBER = "056"

SECTIONS = (
    "## Norm",
    "## Tenant-veilige sleutel",
    "## Periode en noemer",
    "## Drempel",
    "## Wie mag promoveren",
    "## Write-once uitkomstmarker",
)
ACTOR_CLASSES = ("operator", "t0", "worker", "automation", "unknown")
SOURCES = ("merge", "override", "delivery add", "prompt")
OPERATOR_DECISION = (
    "zelfstandigheid-meten: promote ja, leren voorstellen. De T0 promoveert zelf de "
    "leveringen van een plan dat de plan-gate passeerde. Learning- en "
    "dreaming-uitkomsten blijven voorstellen die Vincent goedkeurt."
)


def _adr_path() -> Path:
    matches = sorted(DECISIONS.glob(f"ADR-{ADR_NUMBER}-*.md"))
    assert matches, f"ADR-{ADR_NUMBER} bestaat niet in {DECISIONS}"
    assert len(matches) == 1, f"ADR-{ADR_NUMBER} is niet uniek: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def adr_text() -> str:
    path = _adr_path()
    assert path.exists()
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n") + 1
    rest = text[start + len(heading) + 1:]
    nxt = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_adr_exists() -> None:
    assert _adr_path().exists()


def test_adr_is_nygard_with_status_and_date(adr_text: str) -> None:
    assert adr_text.startswith("# ADR-056")
    assert "**Status:**" in adr_text
    assert "**Datum:** 2026-11-08" in adr_text
    for heading in ("## Context", "## Consequenties"):
        assert f"\n{heading}\n" in adr_text


@pytest.mark.parametrize("heading", SECTIONS)
def test_adr_has_required_section(adr_text: str, heading: str) -> None:
    assert f"\n{heading}\n" in adr_text
    assert len(_section(adr_text, heading).strip()) > 200


def test_adr_carries_the_literal_operator_decision(adr_text: str) -> None:
    flat = " ".join(adr_text.split())
    assert OPERATOR_DECISION in flat
    assert "07-11-2026" in adr_text
    assert "beslissing 2" in adr_text


@pytest.mark.parametrize("actor_class", ACTOR_CLASSES)
def test_adr_names_each_actor_class(adr_text: str, actor_class: str) -> None:
    assert f"`{actor_class}`" in _section(adr_text, "## Norm")


def test_adr_names_each_source_in_the_key_section(adr_text: str) -> None:
    section = _section(adr_text, "## Tenant-veilige sleutel").lower()
    assert "project_id" in section and "delivery_id" in section
    for source in SOURCES:
        assert source in section
    assert "session_id" in section and "cwd" in section


def test_adr_freezes_the_threshold_as_a_number(adr_text: str) -> None:
    section = _section(adr_text, "## Drempel")
    assert re.search(r"\bN >= 50\b", section)
    assert "amendement" in section.lower()


def test_adr_defines_period_denominator_and_no_data(adr_text: str) -> None:
    section = _section(adr_text, "## Periode en noemer")
    for word in ("withdrawn", "expired", "no_data", "completed"):
        assert word in section


def test_adr_keeps_learning_and_dreaming_as_proposals(adr_text: str) -> None:
    section = _section(adr_text, "## Wie mag promoveren").lower()
    assert "learning" in section and "dreaming" in section
    assert "voorstel" in section


def test_adr_marks_write_once_marker(adr_text: str) -> None:
    section = _section(adr_text, "## Write-once uitkomstmarker")
    assert "write-once" in section.lower()
    assert "PR-7b" in section
