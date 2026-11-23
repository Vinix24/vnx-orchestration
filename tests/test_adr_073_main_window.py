"""Documentation contract for ADR-073 and the CI-cost measurement (track ci-cost-and-merge-cadence, PR 1).

PR 2 to 4b build against this ADR. The number is assigned by the T0, so each assert is a
concrete string the ADR or the measurement document must carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_PATH = REPO_ROOT / "docs" / "governance" / "decisions" / "ADR-073-main-venster-in-plaats-van-wachten-per-merge.md"
METING_PATH = REPO_ROOT / "docs" / "operations" / "CI_COST_METING.md"
DOCS_INDEX = REPO_ROOT / "docs" / "DOCS_INDEX.md"


@pytest.fixture(scope="module")
def adr() -> str:
    assert ADR_PATH.is_file(), f"{ADR_PATH.name} ontbreekt"
    return ADR_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def meting() -> str:
    assert METING_PATH.is_file(), f"{METING_PATH.name} ontbreekt"
    return METING_PATH.read_text(encoding="utf-8")


def test_adr_header_and_sections(adr):
    assert adr.startswith("# ADR-073 - "), "titelregel '# ADR-073 - ' ontbreekt"
    for field in ("**Status:**", "**Datum:**", "**Besloten door:**", "**Cross-refs:**"):
        assert field in adr, f"veld {field} ontbreekt"
    for heading in ("## Context", "## Besluit", "## Gevolgen", "## Terugweg"):
        assert heading in adr, f"kop {heading} ontbreekt"


def test_decisions_are_numbered_in_order(adr):
    decision = adr.split("## Besluit", 1)[1].split("\n## Gevolgen", 1)[0]
    numbers = [int(m) for m in re.findall(r"^### (\d+)\. ", decision, flags=re.M)]
    assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) >= 9, f"besluiten niet genummerd: {numbers}"


@pytest.mark.parametrize(
    "needle",
    [
        "stale_run",
        "no_run",
        "history_incomplete",
        "unmeasurable",
        "mode: off",
        "--override-main",
        "--override-reason",
        "revert_or_fix_forward",
        "VNX_MAIN_UNVERIFIED_MAX",
        "PREFLIGHT_ORDER",
        "vnx main-window status",
        "merge_sha",
        "merge_group",
        "flock",
        "vermoedelijke",
        "integratiecandidate-en-merge-trein",
        "ADR-007 speelt niet",
    ],
)
def test_adr_names(adr, needle):
    assert needle in adr, f"ADR-073 noemt {needle!r} niet"


def test_adr_merge_queue_verdict_quotes_the_page(adr):
    assert "docs.github.com" in adr and "managing-a-merge-queue" in adr
    assert "owned by an organization" in adr, "de geciteerde zin ontbreekt"
    assert "`User`" in adr, "het eigenaartype van de repo ontbreekt"


def test_meting_carries_the_numbers(meting):
    for needle in ("19,4", "17 min", "19 min", "65,1", "onmeetbaar", "bovengrens"):
        assert needle.lower() in meting.lower(), f"meting noemt {needle!r} niet"


def test_meting_has_a_command_block_per_section(meting):
    sections = re.split(r"^## ", meting, flags=re.M)[1:]
    assert len(sections) >= 5
    measured = [s for s in sections if re.match(r"[1-5]\. ", s)]
    assert len(measured) == 5
    for section in measured:
        assert section.count("```") >= 2, f"geen codeblok in sectie {section.splitlines()[0]!r}"


def test_docs_index_lists_both():
    index = DOCS_INDEX.read_text(encoding="utf-8")
    assert "ADR-073" in index
    assert "docs/operations/CI_COST_METING.md" in index
