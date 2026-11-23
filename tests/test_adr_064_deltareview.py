#!/usr/bin/env python3
"""ADR-064 (deltareview, DR-0): het ADR moet de besluiten noemen waartegen DR-1 tot DR-8b bouwen.

Stdlib-only. Leest het ADR en het ADR-060-amendement als tekst en toetst dat elk
bindend onderdeel van het plan er met zijn naam in staat.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = VNX_ROOT / "docs" / "governance" / "decisions"
ADR_064 = DECISIONS / "ADR-064-deltareview.md"
ADR_060 = DECISIONS / "ADR-060-poortprofiel-per-project.md"


@pytest.fixture(scope="module")
def adr() -> str:
    assert ADR_064.is_file(), f"missing {ADR_064}"
    return ADR_064.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adr060() -> str:
    return ADR_060.read_text(encoding="utf-8")


@pytest.mark.parametrize("n", range(1, 7))
def test_names_each_decision_as_a_heading(adr: str, n: int) -> None:
    assert re.search(rf"^### Besluit {n}\. ", adr, re.M), f"Besluit {n} heading missing"


@pytest.mark.parametrize("name", ["U1", "U2"])
def test_names_each_starting_point(adr: str, name: str) -> None:
    assert re.search(rf"^### {name}\. ", adr, re.M), f"{name} heading missing"


def test_u2_table_covers_each_mechanism(adr: str) -> None:
    for needle in (
        "Deltareview (fase 1)",
        "Overgeërfde PASS",
        "Pruning",
        "parent_head_sha",
        "carry_rule_version",
        "unchanged: true",
    ):
        assert needle in adr, needle


@pytest.mark.parametrize("n", range(1, 11))
def test_names_each_of_the_ten_requirements(adr: str, n: int) -> None:
    assert re.search(rf"^{n}\. \*\*Eis {n}\.\*\*", adr, re.M), f"Eis {n} missing"


def test_threshold_is_zero_missed_in_twenty(adr: str) -> None:
    assert "0 gemiste echte blockers in 20 paren" in adr
    assert "10 van 10 geplante blockers" in adr
    assert "95%-interval" in adr


def test_switch_names_key_and_three_values(adr: str) -> None:
    assert "VNX_REVIEW_SCOPE" in adr
    for value in ("`full`", "`delta_shadow`", "`delta`"):
        assert value in adr, value
    assert "fail-closed" in adr


def test_rollback_is_the_key_on_full_without_revert(adr: str) -> None:
    m = re.search(r"^## Rollback\n(.+?)(?=^## )", adr, re.M | re.S)
    assert m, "Rollback section missing"
    body = m.group(1)
    assert "VNX_REVIEW_SCOPE" in body and "`full`" in body and "zonder revert" in body


def test_borrowed_sources_and_exclusions_are_named(adr: str) -> None:
    for needle in (
        "Gerrit",
        "LUCI CV",
        "Prow Tide",
        "Turborepo",
        "git patch-id --stable",
        "Kodiak",
        "bors-ng",
    ):
        assert needle in adr, needle


def test_open_questions_are_decided_and_revisable(adr: str) -> None:
    m = re.search(r"^## Open vragen[^\n]*\n(.+?)(?=^## )", adr, re.M | re.S)
    assert m, "open questions section missing"
    assert "herzienbaar" in m.group(1)
    assert "VNX_REVIEW_DELTA_MAX_CHAIN" in m.group(1)


def test_adr060_carries_the_amendment(adr060: str) -> None:
    m = re.search(r"^## Amendement [^\n]*`VNX_REVIEW_SCOPE`[^\n]*\n(.+)\Z", adr060, re.M | re.S)
    assert m, "ADR-060 amendment for VNX_REVIEW_SCOPE missing"
    body = m.group(1)
    for needle in ("`full`", "`delta_shadow`", "`delta`", "approval-id", "DR-5", "DR-7", "Rollback"):
        assert needle in body, needle
    assert re.search(r"onbekende waarde.*geven `full`", body, re.S)


def test_adr060_body_before_the_amendment_is_untouched(adr060: str) -> None:
    head = adr060.split("## Amendement 10-11-2026", 1)[0]
    assert "VNX_REVIEW_SCOPE" not in head
