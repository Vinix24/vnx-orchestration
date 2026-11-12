"""Documentation contract for the learning-capture routing ADR (capture PR-0).

The code of PR-1 to PR-4 of track ``learning-capture-fragmentation`` reads its
routing table from this ADR, and code may not overtake a decision. The ADR
number is assigned by the T0, so the file is found by glob, not by number.
Each assert below is a concrete string the ADR or the working copy must carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
WORKING_COPY = REPO_ROOT / "docs" / "core" / "LEARNING_CAPTURE_ROUTING.md"
DOCS_INDEX = REPO_ROOT / "docs" / "DOCS_INDEX.md"

BUCKETS = ("open item", "memory-notitie", "build-log-nugget", "leerlus")


def _adr_path() -> Path:
    matches = sorted(DECISIONS_DIR.glob("ADR-*-learning-capture-routing.md"))
    assert matches, "ADR-*-learning-capture-routing.md ontbreekt"
    assert len(matches) == 1, f"meer dan één routing-ADR: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def adr() -> str:
    return _adr_path().read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def working() -> str:
    assert WORKING_COPY.is_file(), "docs/core/LEARNING_CAPTURE_ROUTING.md ontbreekt"
    return WORKING_COPY.read_text(encoding="utf-8")


def test_adr_number_is_067():
    assert _adr_path().name.startswith("ADR-067-")


def test_adr_is_nygard_dutch(adr):
    assert adr.startswith("# ADR-067 - ")
    for heading in ("## Context", "## Besluit", "## Consequenties", "## Terugdraaien"):
        assert heading in adr, heading


@pytest.mark.parametrize("text_name", ["adr", "working"])
def test_four_buckets_with_home_order(text_name, request):
    text = request.getfixturevalue(text_name).lower()
    for bucket in BUCKETS:
        assert bucket in text, bucket
    # tie-break order of decision 3: open item, then note, then nugget
    assert "open item, dan notitie, dan nugget" in text


@pytest.mark.parametrize("text_name", ["adr", "working"])
def test_reference_grammar(text_name, request):
    text = request.getfixturevalue(text_name)
    assert "OI-<n>" in text
    assert "[[naam]]" in text
    assert "OI-[1-9][0-9]*" in text
    assert "een nugget is nooit een doel" in text.lower()


def test_window_convention(adr):
    assert "(project_id, since, until)" in adr
    for source in ("explicit", "chain", "first24h"):
        assert f"`{source}`" in adr, source
    for field in ("anchors_total", "backlog_open_pairs", "open_pairs"):
        assert field in adr, field
    assert "geen lege nul" in adr.lower()


def test_pair_kinds_and_method(adr):
    for kind in ("oi_mem", "oi_nug", "mem_nug"):
        assert kind in adr, kind
    for const in ("TOKEN_MIN_LEN = 4", "JACCARD_MIN = 0.34", "SHARED_MIN = 2"):
        assert const in adr, const


@pytest.mark.parametrize("text_name", ["adr", "working"])
def test_three_disposition_states(text_name, request):
    text = request.getfixturevalue(text_name)
    for state in ("`linked`", "`dismissed`", "`open`"):
        assert state in text, state
    assert "capture_dispositions.ndjson" in text
    assert "capture_audit.py dismiss" in text


def test_audit_is_read_model_not_rule_queue(adr):
    assert "capture_audit.json" in adr
    assert "ingest_approved_rules" in adr
    assert "pending_rules.json" in adr
    assert "geen handoff-sleutel" in adr.lower()
    assert "geen sidecar" in adr.lower()


def test_project_binding_and_privacy(adr):
    assert "capture_sources.json" in adr
    assert "project_id_from_state_dir" in adr
    assert "`unbound`" in adr
    assert "~/.claude/projects/" in adr
    assert "foreign_slug_skipped" in adr
    assert "Geen titel, geen kop, geen body, geen bestandsnaam" in adr


def test_rollback_paragraph(adr):
    section = adr.split("## Terugdraaien", 1)[1]
    assert "nachtelijke aanroep" in section
    assert "bindingsbestand" in section
    assert "geen datamigratie" in section.lower()


def test_working_copy_has_table_adoption_order_and_waarom(working):
    assert "## Adoptievolgorde" in working
    assert "## Waarom" in working
    waarom = working.split("## Waarom", 1)[1]
    lines = [ln for ln in waarom.splitlines() if ln.startswith("- **")]
    assert len(lines) == 4, lines
    adoption = working.split("## Adoptievolgorde", 1)[1].split("\n## ", 1)[0]
    order = ["nachtelijke run", "report", "dismiss", "volgende run"]
    pos = [adoption.find(step) for step in order]
    assert all(p >= 0 for p in pos), pos
    assert pos == sorted(pos), pos


def test_working_copy_table_rows(working):
    rows = [ln for ln in working.splitlines() if re.match(r"^\| (Open item|Memory-notitie|Build-log-nugget|Leerlus) ", ln)]
    assert len(rows) == 4, rows


def test_docs_index_lists_both():
    index = DOCS_INDEX.read_text(encoding="utf-8")
    assert "docs/core/LEARNING_CAPTURE_ROUTING.md" in index
    assert "ADR-067" in index
