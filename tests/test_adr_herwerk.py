"""Documentation contract for the rework ADR (track herwerk-per-model-en-reviewer, row H0).

Rows H1 to H7 build against this ADR, and code may not overtake a decision. The
ADR number is assigned by the T0, so the file is found by glob, not by number.
Each assert below is a concrete string the ADR or the docs index must carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
DOCS_INDEX = REPO_ROOT / "docs" / "DOCS_INDEX.md"


def _adr_path() -> Path:
    matches = sorted(DECISIONS_DIR.glob("ADR-*-herwerk-per-model-en-stoel.md"))
    assert matches, "ADR-*-herwerk-per-model-en-stoel.md ontbreekt"
    assert len(matches) == 1, f"meer dan een herwerk-ADR: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def adr() -> str:
    return _adr_path().read_text(encoding="utf-8")


def test_adr_number_is_071_and_nygard(adr):
    assert _adr_path().name.startswith("ADR-071-"), "ADR-nummer is niet 071"
    assert adr.startswith("# ADR-071 - "), "titelregel '# ADR-071 - ' ontbreekt"
    for heading in ("## Context", "## Besluit", "## Consequenties", "## Terugdraaien"):
        assert heading in adr, f"kop {heading} ontbreekt"


@pytest.mark.parametrize("kind", ["blame", "round", "oi_fix", "pr_fix"])
def test_edge_kinds(adr, kind):
    assert f"`{kind}`" in adr, f"randsoort `{kind}` ontbreekt"


@pytest.mark.parametrize(
    "needle",
    [
        "parent_dispatch",
        "dispatch_metadata",
        "origin_oi",
        "origin_pr",
        "fill-once",
        "resolve_parent_dispatch",
        "com.vnx.rework-attribution",
        "05:30",
        "rework_attribution_watermark.json",
        "degraded",
        "incomplete",
        "pr_merged",
        "merge_source",
        "unattributed",
        "pr_join_conflict",
        "immature",
        "model=conflict",
        "model=unknown",
        "doorgelaten herwerk",
        "rework_edges",
    ],
)
def test_decision_strings(adr, needle):
    assert needle in adr, f"'{needle}' ontbreekt in het ADR"


def test_churn_versus_rework_type_lists(adr):
    assert "`fix` of `revert`" in adr, "typelijst herwerk (fix, revert) ontbreekt"
    assert "`05_debugging`" in adr, "taakklasse 05_debugging ontbreekt"
    for churn_type in ("`feat`", "`refactor`", "`docs`", "`test`", "`chore`"):
        assert churn_type in adr, f"churn-type {churn_type} ontbreekt"


def test_three_measures_window_and_n_rule(adr):
    for measure in ("First-pass yield", "Herwerk binnen 14 dagen", "Gemist per stoel"):
        assert measure in adr, f"maat '{measure}' ontbreekt"
    assert "14 dagen" in adr, "het 14-dagenvenster ontbreekt"
    assert "`n=20`" in adr and "`n >= 20`" in adr, "de n-regel (n=20) ontbreekt"


def test_denominator_exclusions(adr):
    for excluded in ("4.846", "*-gate-pr*", "panel-*", "plan-gate-*", "plan-tiebreak-*", "noise_reason"):
        assert excluded in adr, f"uitsluiting '{excluded}' ontbreekt"


def test_adr_007_key(adr):
    assert "ADR-007" in adr, "verwijzing naar ADR-007 ontbreekt"
    assert "(project_id, rework_dispatch_id, origin_dispatch_id, kind)" in adr, (
        "de primaire sleutel over project_id ontbreekt"
    )
    assert "project_id" in adr, "project_id ontbreekt"


def test_proposals_only_and_thresholds(adr):
    assert "## Voorstellen" in adr, "de sectienaam '## Voorstellen' van het rapport ontbreekt"
    for target in ("routing_recommendations.yaml", "project_config", "provider_constraints.yaml"):
        assert target in adr, f"verboden schrijfdoel {target} ontbreekt"
    assert "5 procentpunt" in adr, "voorsteldrempel van 5 procentpunt ontbreekt"
    assert "gepaarde koppen" in adr, "gepaarde koppen voor stoelvoorstellen ontbreken"


def test_rollback_paragraph(adr):
    section = adr.split("## Terugdraaien", 1)[1]
    assert "com.vnx.rework-attribution" in section, "terugdraaien noemt de nachtjob niet"
    assert "geen datamigratie" in section.lower(), "'geen datamigratie' ontbreekt"


def test_docs_index_lists_adr():
    index = DOCS_INDEX.read_text(encoding="utf-8")
    assert "ADR-071" in index, "DOCS_INDEX noemt ADR-071 niet"
    assert "rework_edges" in index, "DOCS_INDEX-regel noemt rework_edges niet"
