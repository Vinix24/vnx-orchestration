"""Documentation contract for ADR-074 (track impactselectie-in-schaduw, PR 1).

The selector, the shadow step and the collector of PRs 2 to 4 build against this ADR.
Each assert is a concrete string the ADR must carry, plus the registry entry of the hook.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))


def _adr_path() -> Path:
    matches = sorted(DECISIONS_DIR.glob("ADR-074-*.md"))
    assert matches, "ADR-074-*.md ontbreekt"
    assert len(matches) == 1, f"meer dan één ADR-074: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def adr() -> str:
    return _adr_path().read_text(encoding="utf-8")


def test_adr_title_and_header_fields(adr):
    assert adr.startswith("# ADR-074 - "), "titelregel '# ADR-074 - ' ontbreekt"
    for field in ("**Status:**", "**Datum:**", "**Besloten door:**", "**Cross-refs:**"):
        assert field in adr, f"veld {field} ontbreekt"


def test_adr_sections(adr):
    for heading in ("## Context", "## Besluit", "## Gevolgen", "## Terugweg"):
        assert heading in adr, f"kop {heading} ontbreekt"
    decision = adr.split("## Besluit", 1)[1].split("\n## Gevolgen", 1)[0]
    assert "### 1. " in decision and "### 13. " in decision, "besluiten zijn niet genummerd"


@pytest.mark.parametrize(
    "needle",
    [
        "scope: full|impact",
        "niet onderzocht",
        "onreconstrueerbaar",
        "run_attempt",
        "run_id",
        "VNX_CI_IMPACT_CMD",
        "ADR-053",
        "ADR-073",
        "REFUTED",
        "state/impact_shadow.ndjson",
        "scripts/ci/impact_select.py",
        "|S| < |U − X|",
        "ADR-007",
    ],
)
def test_concrete_strings(adr, needle):
    assert needle in adr, f"ADR-074 noemt {needle!r} niet"


def test_threshold_is_zero_misses_over_fifteen_red_runs(adr):
    assert "0 missers over minstens 15 rode runs" in adr


def test_registry_entry_for_the_hook():
    import config_registry

    entry = config_registry.CONFIG_REGISTRY["VNX_CI_IMPACT_CMD"]
    assert entry.default == ""
    assert entry.requires_approval is True
    assert entry.read_site_wired is False
