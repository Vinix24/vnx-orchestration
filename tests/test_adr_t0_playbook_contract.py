"""Contracttest voor het ADR over de bron van het T0-playbook (skills-port SP-0).

Dit is een contracttest op tekst, geen gedragsproef: hij legt vast dat het ADR de
woorden draagt waar SP-2, SP-3, SP-4, SP-5 en SP-7 op leunen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "governance" / "decisions"
ADR_NUMBER = "058"

SOURCES = (
    ("fabric", "false"),
    ("vnx_home", "false"),
    ("installed", "false"),
    ("project", "true"),
    ("legacy", "true"),
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


def test_adr_file_exists() -> None:
    path = _adr_path()
    assert path.exists()


def test_adr_is_nygard_and_accepted(adr_text: str) -> None:
    assert "**Status:** Aangenomen 08-11-2026" in adr_text
    assert "## Context" in adr_text
    assert "## Besluit" in adr_text
    assert "## Gevolgen" in adr_text


@pytest.mark.parametrize("source_id,degraded", SOURCES)
def test_adr_names_each_source_with_degraded_value(adr_text: str, source_id: str, degraded: str) -> None:
    rows = [line for line in adr_text.splitlines() if f"| `{source_id}` |" in line]
    assert len(rows) == 1, f"bronrij voor {source_id} ontbreekt of is dubbel"
    assert rows[0].rstrip().endswith(f"| `{degraded}` |"), rows[0]


@pytest.mark.parametrize("state", ("present", "stale_sha", "stale_source"))
def test_adr_names_freshness_states(adr_text: str, state: str) -> None:
    assert f"`{state}`" in adr_text


def test_adr_has_literal_missing_hook_line(adr_text: str) -> None:
    line = "T0 PLAYBOOK SOURCE MISSING: no playbook in sources (a)-(e); do not orchestrate from memory"
    assert line in adr_text
    for token in ('`playbook.status: "missing"`', "`source_missing: true`", "`probed: [...]`",
                  '`loading: "unverified"`', "`PLAYBOOK-SOURCE-MISSING`", "`PLAYBOOK-MECHANISM-GAP`"):
        assert token in adr_text, token


def test_adr_names_evidence_file_and_both_readers(adr_text: str) -> None:
    assert "playbook_loading.ndjson" in adr_text
    assert "`scripts/fleet_role_drift.py`" in adr_text
    assert "`scripts/commands/t0_role_audit.sh`" in adr_text
    assert "scripts/lib/playbook_loading_evidence.py" in adr_text


def test_adr_states_budget_claim_and_follow_ups(adr_text: str) -> None:
    assert "reference_metric_only" in adr_text
    assert "`unmeasured`" in adr_text
    assert "32768" in adr_text
    assert "F1" in adr_text and "F2" in adr_text
