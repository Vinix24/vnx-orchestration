#!/usr/bin/env python3
"""Tests for recurrence detection, retrospective digests, and guarded recommendations (Feature 18, PR-2).

Covers:
  1. Recurrence detection — grouping, counting, threshold
  2. Evidence pointers — correlation integrity in records
  3. Digest surface — structure and counts
  4. Recommendation surface — advisory invariant, categories, evidence
  5. Recommendation advisory-only invariant (cannot be bypassed)
  6. End-to-end digest with multiple signal types
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from retrospective_digest import (
    HIGH_FREQUENCY_THRESHOLD,
    RECURRENCE_THRESHOLD,
    Recommendation,
    RecurrenceRecord,
    RetroDigest,
    build_digest,
    detect_recurrences,
    generate_recommendations,
)


# ---------------------------------------------------------------------------
# Test signal stub (matches GovernanceSignal duck type)
# ---------------------------------------------------------------------------

@dataclass
class _Corr:
    feature_id: str = ""
    pr_id: str = ""
    session_id: str = ""
    dispatch_id: str = ""
    provider_id: str = ""
    terminal_id: str = ""
    branch: str = ""


@dataclass
class _Sig:
    signal_type: str
    content: str
    severity: str
    defect_family: Optional[str] = None
    correlation: _Corr = field(default_factory=_Corr)
    count: int = 1


def _session_fail(family: str = "fam_exit1", provider: str = "", session: str = "T1",
                  dispatch: str = "d-1", feature: str = "F18", pr: str = "PR-1",
                  severity: str = "blocker") -> _Sig:
    return _Sig(
        signal_type="session_failure",
        content="session_failed: exit 1",
        severity=severity,
        defect_family=family,
        correlation=_Corr(
            feature_id=feature, pr_id=pr, session_id=session,
            dispatch_id=dispatch, provider_id=provider,
        ),
    )


def _gate_fail(family: str = "fam_gate", feature: str = "F18",
               pr: str = "PR-1", dispatch: str = "d-gate") -> _Sig:
    return _Sig(
        signal_type="gate_failure",
        content="gate gate_pr1 failed",
        severity="blocker",
        defect_family=family,
        correlation=_Corr(feature_id=feature, pr_id=pr, dispatch_id=dispatch),
    )


def _queue_anomaly(family: str = "fam_queue", terminal: str = "T1") -> _Sig:
    return _Sig(
        signal_type="queue_anomaly",
        content="delivery_failure: pane gone",
        severity="warn",
        defect_family=family,
        correlation=_Corr(terminal_id=terminal),
    )


def _open_item(family: str = "fam_oi", severity: str = "blocker",
               pr: str = "PR-1") -> _Sig:
    return _Sig(
        signal_type="open_item_transition",
        content="[blocker] OI-1: Tests fail (open->open)",
        severity=severity,
        defect_family=family,
        correlation=_Corr(pr_id=pr),
    )


# ---------------------------------------------------------------------------
# 1. Recurrence detection
# ---------------------------------------------------------------------------

class TestRecurrenceDetection:

    def test_single_occurrence_not_in_records(self) -> None:
        sigs = [_session_fail()]
        records = detect_recurrences(sigs)
        assert records == []

    def test_two_occurrences_meet_threshold(self) -> None:
        sigs = [_session_fail()] * RECURRENCE_THRESHOLD
        records = detect_recurrences(sigs)
        assert len(records) == 1
        assert records[0].count == RECURRENCE_THRESHOLD

    def test_count_matches_occurrences(self) -> None:
        sigs = [_session_fail()] * 5
        records = detect_recurrences(sigs)
        assert records[0].count == 5

    def test_no_family_signals_not_counted(self) -> None:
        sigs = [_Sig(signal_type="gate_success", content="ok", severity="info")]
        records = detect_recurrences(sigs)
        assert records == []

    def test_distinct_families_tracked_separately(self) -> None:
        sigs = (
            [_session_fail("family_a")] * 3 +
            [_gate_fail("family_b")] * 2
        )
        records = detect_recurrences(sigs)
        assert len(records) == 2
        families = {r.defect_family for r in records}
        assert "family_a" in families
        assert "family_b" in families

    def test_records_sorted_by_count_desc(self) -> None:
        sigs = (
            [_session_fail("fam_a")] * 2 +
            [_gate_fail("fam_b")] * 5
        )
        records = detect_recurrences(sigs)
        assert records[0].count >= records[1].count

    def test_severity_worst_across_members(self) -> None:
        sigs = [
            _session_fail("fam", severity="warn"),
            _session_fail("fam", severity="blocker"),
        ]
        records = detect_recurrences(sigs)
        assert records[0].severity == "blocker"

    def test_representative_content_from_first_member(self) -> None:
        first = _session_fail("fam")
        first.content = "session_failed: specific error"
        second = _session_fail("fam")
        second.content = "different content"
        records = detect_recurrences([first, second])
        assert records[0].representative_content == "session_failed: specific error"


# ---------------------------------------------------------------------------
# 2. Evidence pointers and correlation integrity
# ---------------------------------------------------------------------------

class TestEvidencePointers:

    def test_evidence_pointers_include_dispatch_ids(self) -> None:
        sigs = [
            _session_fail("fam", dispatch="d-1"),
            _session_fail("fam", dispatch="d-2"),
        ]
        records = detect_recurrences(sigs)
        ptrs = records[0].evidence_pointers
        assert "d-1" in ptrs
        assert "d-2" in ptrs

    def test_evidence_pointers_include_session_ids(self) -> None:
        sigs = [
            _session_fail("fam", session="T1"),
            _session_fail("fam", session="T2"),
        ]
        records = detect_recurrences(sigs)
        ptrs = records[0].evidence_pointers
        assert "T1" in ptrs
        assert "T2" in ptrs

    def test_impacted_features_deduplicated(self) -> None:
        sigs = [
            _session_fail("fam", feature="F18"),
            _session_fail("fam", feature="F18"),
            _session_fail("fam", feature="F17"),
        ]
        records = detect_recurrences(sigs)
        assert sorted(records[0].impacted_features) == ["F17", "F18"]

    def test_impacted_prs_deduplicated(self) -> None:
        sigs = [
            _session_fail("fam", pr="PR-1"),
            _session_fail("fam", pr="PR-2"),
            _session_fail("fam", pr="PR-1"),
        ]
        records = detect_recurrences(sigs)
        assert sorted(records[0].impacted_prs) == ["PR-1", "PR-2"]

    def test_providers_collected(self) -> None:
        sigs = [
            _session_fail("fam", provider="gemini"),
            _session_fail("fam", provider="codex_cli"),
        ]
        records = detect_recurrences(sigs)
        assert sorted(records[0].providers) == ["codex_cli", "gemini"]

    def test_signal_types_collected(self) -> None:
        sigs = [
            _session_fail("fam"),
            _gate_fail("fam"),
        ]
        records = detect_recurrences(sigs)
        assert "session_failure" in records[0].signal_types
        assert "gate_failure" in records[0].signal_types

    def test_empty_correlation_fields_excluded(self) -> None:
        sigs = [_session_fail("fam", dispatch="", session="")] * 2
        records = detect_recurrences(sigs)
        # evidence_pointers should not contain empty strings
        assert "" not in records[0].evidence_pointers


# ---------------------------------------------------------------------------
# 3. Digest surface
# ---------------------------------------------------------------------------

class TestDigestSurface:

    def test_digest_has_generated_at(self) -> None:
        d = build_digest([])
        assert d.generated_at
        assert "T" in d.generated_at  # ISO format

    def test_digest_total_signals_processed(self) -> None:
        sigs = [_session_fail()] * 3
        d = build_digest(sigs)
        assert d.total_signals_processed == 3

    def test_digest_recurring_patterns_populated(self) -> None:
        sigs = [_session_fail()] * 2
        d = build_digest(sigs)
        assert len(d.recurring_patterns) == 1
        assert d.recurring_patterns[0].count == 2

    def test_digest_single_occurrence_count(self) -> None:
        sigs = [
            _session_fail("fam_a"),          # 1 occurrence
            _gate_fail("fam_b"),             # 1 occurrence
            _queue_anomaly("fam_c"),         # 1 occurrence
        ]
        d = build_digest(sigs)
        assert d.single_occurrence_count == 3

    def test_digest_to_dict_structure(self) -> None:
        sigs = [_session_fail()] * 2
        d = build_digest(sigs)
        result = d.to_dict()
        assert "generated_at" in result
        assert "total_signals_processed" in result
        assert "recurring_pattern_count" in result
        assert "recurring_patterns" in result
        assert "recommendations" in result

    def test_empty_digest_still_valid(self) -> None:
        d = build_digest([])
        assert d.total_signals_processed == 0
        assert d.recurring_patterns == []
        assert d.single_occurrence_count == 0

    def test_custom_generated_at(self) -> None:
        ts = "2026-04-03T12:00:00+00:00"
        d = build_digest([], generated_at=ts)
        assert d.generated_at == ts


# ---------------------------------------------------------------------------
# 4. Recommendation surface
# ---------------------------------------------------------------------------

class TestRecommendations:

    def test_gate_failure_recurrence_produces_review_required(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=2,
            representative_content="gate failed",
            severity="blocker",
            signal_types=["gate_failure"],
            impacted_prs=["PR-1"],
            evidence_pointers=["d-1"],
        )]
        recs = generate_recommendations(records)
        assert any(r.category == "review_required" for r in recs)

    def test_queue_anomaly_recurrence_produces_policy_change(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=3,
            representative_content="delivery_failure: pane gone",
            severity="warn",
            signal_types=["queue_anomaly"],
            evidence_pointers=["d-1", "d-2"],
        )]
        recs = generate_recommendations(records)
        assert any(r.category == "policy_change" for r in recs)

    def test_single_provider_session_failure_produces_runtime_fix(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=2,
            representative_content="session_failed: exit 1",
            severity="blocker",
            signal_types=["session_failure"],
            providers=["codex_cli"],
            evidence_pointers=["d-1"],
        )]
        recs = generate_recommendations(records)
        assert any(r.category == "runtime_fix" for r in recs)

    def test_high_frequency_session_failure_produces_prompt_tuning(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=HIGH_FREQUENCY_THRESHOLD,
            representative_content="session_failed: assertion",
            severity="blocker",
            signal_types=["session_failure"],
            providers=["claude_code", "gemini"],  # multiple providers -> not runtime_fix
            evidence_pointers=["d-1"],
        )]
        recs = generate_recommendations(records)
        assert any(r.category == "prompt_tuning" for r in recs)

    def test_blocker_open_item_cycling_produces_review_required(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=2,
            representative_content="[blocker] OI-1: Tests fail",
            severity="blocker",
            signal_types=["open_item_transition"],
            impacted_prs=["PR-1"],
            evidence_pointers=["d-1"],
        )]
        recs = generate_recommendations(records)
        assert any(r.category == "review_required" for r in recs)

    def test_recommendations_sorted_blocker_first(self) -> None:
        records = [
            RecurrenceRecord("fam1", 2, "warn msg", "warn",
                             signal_types=["queue_anomaly"], evidence_pointers=["d-1"]),
            RecurrenceRecord("fam2", 2, "blocker msg", "blocker",
                             signal_types=["gate_failure"], impacted_prs=["PR-1"],
                             evidence_pointers=["d-2"]),
        ]
        recs = generate_recommendations(records)
        assert recs[0].severity == "blocker"

    def test_recommendations_include_evidence_basis(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=2,
            representative_content="gate failed",
            severity="blocker",
            signal_types=["gate_failure"],
            evidence_pointers=["d-1", "d-2", "d-3"],
        )]
        recs = generate_recommendations(records)
        assert len(recs[0].evidence_basis) > 0

    def test_recommendation_content_includes_count(self) -> None:
        records = [RecurrenceRecord(
            defect_family="fam", count=4,
            representative_content="gate failed",
            severity="blocker",
            signal_types=["gate_failure"],
            impacted_prs=["PR-2"],
            evidence_pointers=["d-1"],
        )]
        recs = generate_recommendations(records)
        assert "4" in recs[0].content

    def test_no_records_no_recommendations(self) -> None:
        recs = generate_recommendations([])
        assert recs == []


# ---------------------------------------------------------------------------
# 5. Advisory-only invariant
# ---------------------------------------------------------------------------

class TestAdvisoryOnlyInvariant:

    def test_recommendation_advisory_only_true_by_default(self) -> None:
        rec = Recommendation(
            category="review_required",
            content="something needs review",
        )
        assert rec.advisory_only is True

    def test_recommendation_rejects_advisory_only_false(self) -> None:
        with pytest.raises(ValueError, match="advisory_only"):
            Recommendation(
                category="review_required",
                content="attempt mutation",
                advisory_only=False,
            )

    def test_all_generated_recommendations_advisory_only(self) -> None:
        sigs = [_gate_fail()] * 3
        d = build_digest(sigs)
        for rec in d.recommendations:
            assert rec.advisory_only is True

    def test_recommendation_to_dict_includes_advisory_only(self) -> None:
        rec = Recommendation(category="policy_change", content="adjust policy")
        d = rec.to_dict()
        assert d["advisory_only"] is True

    def test_unknown_category_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown recommendation category"):
            Recommendation(category="auto_fix", content="fix it")


# ---------------------------------------------------------------------------
# 6. End-to-end digest
# ---------------------------------------------------------------------------

class TestEndToEndDigest:

    def test_mixed_sources_all_surfaced(self) -> None:
        sigs = (
            [_session_fail("fam_sess")] * 3 +
            [_gate_fail("fam_gate")] * 2 +
            [_queue_anomaly("fam_q")] * 2 +
            [_open_item("fam_oi")] * 2 +
            [_session_fail("fam_single")]  # 1 occurrence — not recurring
        )
        d = build_digest(sigs)
        assert d.total_signals_processed == 10
        assert len(d.recurring_patterns) == 4
        assert d.single_occurrence_count == 1
        assert len(d.recommendations) > 0

    def test_digest_recommendations_point_to_evidence(self) -> None:
        sigs = [
            _session_fail("fam", dispatch="d-abc", session="T2"),
            _session_fail("fam", dispatch="d-def", session="T3"),
        ]
        d = build_digest(sigs)
        for rec in d.recommendations:
            assert len(rec.evidence_basis) > 0

    def test_single_occurrence_only_no_recommendations(self) -> None:
        sigs = [_session_fail()]  # below threshold
        d = build_digest(sigs)
        assert d.recurring_patterns == []
        assert d.recommendations == []

    def test_digest_richer_than_receipt_text(self) -> None:
        """Digest includes structured fields receipt text alone cannot provide."""
        sigs = [
            _session_fail("fam", feature="F17", pr="PR-3", provider="gemini",
                          dispatch="20260403-104411-B"),
            _session_fail("fam", feature="F18", pr="PR-1", provider="gemini",
                          dispatch="20260403-125215-B"),
        ]
        d = build_digest(sigs)
        rec_pat = d.recurring_patterns[0]
        # Digest surfaces features, PRs, providers, and evidence pointers
        assert "F17" in rec_pat.impacted_features or "F18" in rec_pat.impacted_features
        assert rec_pat.providers == ["gemini"]
        assert len(rec_pat.evidence_pointers) >= 2  # dispatch IDs + session IDs

    def test_t0_consumable_to_dict(self) -> None:
        sigs = [_gate_fail("fam")] * 2
        d = build_digest(sigs)
        result = d.to_dict()
        assert result["recurring_pattern_count"] == 1
        assert result["recommendations"][0]["advisory_only"] is True
        assert result["recommendations"][0]["evidence_basis"]
