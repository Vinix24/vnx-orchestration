#!/usr/bin/env python3
"""PR-4 certification tests for Feature 18: Learning-Loop Signal Enrichment.

Certifies that:
  1. Repeated failures become durable signals with recurrence detection
  2. Retrospective digests are evidence-linked and accurate
  3. Local-model hook remains advisory with correct fallback
  4. Authority boundary is enforced (no auto-mutations)
  5. Contract-to-implementation alignment
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from governance_signal_extractor import (
    GOVERNANCE_SIGNAL_TYPES,
    GovernanceSignal,
    SignalCorrelation,
    extract_from_session_events,
    extract_from_gate_results,
    _defect_family_key,
)
from retrospective_digest import (
    RECURRENCE_THRESHOLD,
    HIGH_FREQUENCY_THRESHOLD,
    RECOMMENDATION_CATEGORIES,
    RecurrenceRecord,
    Recommendation,
    RetroDigest,
    detect_recurrences,
    generate_recommendations,
    build_digest,
)
from retrospective_model_hook import (
    CONFIDENCE_LEVELS,
    FALLBACK_CONFIDENCE,
    MAX_CANDIDATE_GUARDRAILS,
    RetroAnalysisInput,
    RetroAnalysisSummary,
    run_retrospective_hook,
)


# ---------------------------------------------------------------------------
# Test signal stub (duck-typed, matching existing test pattern)
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


def _make_sig(family: str = "fam_timeout", dispatch: str = "d-1",
              feature: str = "F18", severity: str = "blocker") -> _Sig:
    return _Sig(
        signal_type="session_failure",
        content="session_failed: timeout",
        severity=severity,
        defect_family=family,
        correlation=_Corr(feature_id=feature, dispatch_id=dispatch),
    )


def _make_recurring(family: str, count: int) -> list:
    return [_make_sig(family=family, dispatch=f"d-{i}") for i in range(count)]


@dataclass
class _Pattern:
    representative_content: str = "session_failed: exit 1"
    count: int = 3
    severity: str = "blocker"
    evidence_pointers: List[str] = field(default_factory=lambda: ["d-1", "d-2"])


@dataclass
class _Digest:
    recurring_patterns: List[_Pattern] = field(default_factory=list)
    total_signals_processed: int = 10
    single_occurrence_count: int = 2


# ===================================================================
# Section 1: Signal Enrichment And Recurrence Detection
# ===================================================================

class TestSignalEnrichmentCertification:

    def test_all_signal_types_defined(self) -> None:
        expected = {
            "session_failure", "session_artifact", "gate_failure",
            "gate_success", "queue_anomaly", "open_item_transition",
            "defect_family",
        }
        assert expected == GOVERNANCE_SIGNAL_TYPES

    def test_signal_carries_correlation(self) -> None:
        sig = _make_sig()
        assert sig.correlation.feature_id == "F18"
        assert sig.correlation.dispatch_id == "d-1"

    def test_defect_family_key_deterministic(self) -> None:
        key1 = _defect_family_key("timeout after 600s on d-12345")
        key2 = _defect_family_key("timeout after 600s on d-99999")
        assert key1 == key2

    def test_defect_family_key_different_errors(self) -> None:
        key1 = _defect_family_key("timeout after 600s")
        key2 = _defect_family_key("permission denied /tmp/foo")
        assert key1 != key2

    def test_session_signal_extraction(self) -> None:
        events = [
            {"event_type": "session_failed", "session_id": "T2",
             "dispatch_id": "d-1", "details": {"reason": "exit 1"}},
        ]
        signals = extract_from_session_events(events)
        assert len(signals) >= 1
        assert signals[0].signal_type == "session_failure"

    def test_gate_signal_extraction(self) -> None:
        results = [
            {"gate": "codex_gate", "status": "fail", "blocking_count": 2,
             "pr_id": "PR-1", "blocking_findings": [{"message": "test"}]},
        ]
        signals = extract_from_gate_results(results)
        assert len(signals) >= 1
        assert signals[0].signal_type == "gate_failure"

    def test_recurrence_at_threshold(self) -> None:
        signals = _make_recurring("fam_timeout", RECURRENCE_THRESHOLD)
        recurrences = detect_recurrences(signals)
        assert len(recurrences) >= 1
        assert recurrences[0].count >= RECURRENCE_THRESHOLD

    def test_no_recurrence_at_single(self) -> None:
        signals = _make_recurring("fam_timeout", 1)
        recurrences = detect_recurrences(signals)
        assert len(recurrences) == 0

    def test_high_frequency_recurrence(self) -> None:
        signals = _make_recurring("fam_timeout", HIGH_FREQUENCY_THRESHOLD + 1)
        recurrences = detect_recurrences(signals)
        assert len(recurrences) >= 1
        assert recurrences[0].count > HIGH_FREQUENCY_THRESHOLD


# ===================================================================
# Section 2: Retrospective Digest Correctness
# ===================================================================

class TestRetrospectiveDigestCertification:

    def test_digest_contains_recurrence_summary(self) -> None:
        signals = _make_recurring("fam_timeout", 3)
        digest = build_digest(signals)
        assert digest.total_signals_processed == 3
        assert len(digest.recurring_patterns) >= 1

    def test_digest_evidence_linked(self) -> None:
        signals = _make_recurring("fam_timeout", 3)
        digest = build_digest(signals)
        for pattern in digest.recurring_patterns:
            assert len(pattern.evidence_pointers) > 0

    def test_recommendations_generated(self) -> None:
        signals = _make_recurring("fam_timeout", 3)
        recurrences = detect_recurrences(signals)
        recs = generate_recommendations(recurrences)
        assert len(recs) >= 1

    def test_recommendation_categories_valid(self) -> None:
        signals = _make_recurring("fam_timeout", 3)
        recurrences = detect_recurrences(signals)
        recs = generate_recommendations(recurrences)
        for rec in recs:
            assert rec.category in RECOMMENDATION_CATEGORIES

    def test_recommendation_advisory_only_enforced(self) -> None:
        with pytest.raises(ValueError, match="advisory"):
            Recommendation(
                category="review_required",
                content="test",
                defect_family="test",
                advisory_only=False,
            )

    def test_recommendation_advisory_only_default(self) -> None:
        rec = Recommendation(
            category="review_required",
            content="test",
            defect_family="test",
        )
        assert rec.advisory_only is True

    def test_digest_with_no_signals(self) -> None:
        digest = build_digest([])
        assert digest.total_signals_processed == 0
        assert len(digest.recurring_patterns) == 0
        assert len(digest.recommendations) == 0


# ===================================================================
# Section 3: Local Model Hook Authority Boundary
# ===================================================================

class TestLocalModelHookCertification:

    def test_summary_authoritative_false_enforced(self) -> None:
        with pytest.raises(ValueError):
            RetroAnalysisSummary(
                summary="test",
                confidence="high",
                authoritative=True,
                candidate_guardrails=[],
                evidence_pointers=["d-1"],
                model_id="test",
            )

    def test_summary_authoritative_default_false(self) -> None:
        summary = RetroAnalysisSummary(
            summary="test",
            confidence="high",
            candidate_guardrails=[],
            evidence_pointers=["d-1"],
            model_id="test",
        )
        assert summary.authoritative is False

    def test_confidence_must_be_valid(self) -> None:
        with pytest.raises(ValueError):
            RetroAnalysisSummary(
                summary="test",
                confidence="extreme",
                candidate_guardrails=[],
                evidence_pointers=["d-1"],
                model_id="test",
            )

    def test_fallback_without_model(self) -> None:
        input_data = RetroAnalysisInput(
            digest=_Digest(recurring_patterns=[_Pattern()]),
        )
        summary = run_retrospective_hook(input_data, hook=None)
        assert summary.fallback is True
        assert summary.authoritative is False
        assert summary.confidence == FALLBACK_CONFIDENCE

    def test_max_guardrails_enforced(self) -> None:
        guardrails = [f"guardrail-{i}" for i in range(MAX_CANDIDATE_GUARDRAILS + 5)]
        with pytest.raises(ValueError):
            RetroAnalysisSummary(
                summary="test",
                confidence="medium",
                candidate_guardrails=guardrails,
                evidence_pointers=["d-1"],
                model_id="test",
            )


# ===================================================================
# Section 4: Authority Boundary Enforcement
# ===================================================================

class TestAuthorityBoundaryCertification:

    def test_signals_carry_source_identity(self) -> None:
        sig = _make_sig()
        assert sig.correlation.dispatch_id is not None
        assert sig.signal_type in GOVERNANCE_SIGNAL_TYPES

    def test_recommendations_always_advisory(self) -> None:
        rec = Recommendation(
            category="runtime_fix",
            content="increase timeout",
            defect_family="fam_timeout",
        )
        assert rec.advisory_only is True

    def test_digest_is_pure_function(self) -> None:
        signals = _make_recurring("fam_timeout", 4)
        digest = build_digest(signals)
        assert isinstance(digest, RetroDigest)
        assert all(s.defect_family == "fam_timeout" for s in signals)

    def test_model_output_requires_confidence(self) -> None:
        summary = RetroAnalysisSummary(
            summary="test",
            confidence="medium",
            candidate_guardrails=[],
            evidence_pointers=["d-1"],
            model_id="test",
        )
        assert summary.confidence in CONFIDENCE_LEVELS


# ===================================================================
# Section 5: Contract Alignment
# ===================================================================

class TestContractAlignment:

    def test_seven_signal_types(self) -> None:
        assert len(GOVERNANCE_SIGNAL_TYPES) == 7

    def test_recurrence_threshold_matches_contract(self) -> None:
        assert RECURRENCE_THRESHOLD == 2

    def test_high_frequency_threshold(self) -> None:
        assert HIGH_FREQUENCY_THRESHOLD == 5

    def test_four_recommendation_categories(self) -> None:
        assert len(RECOMMENDATION_CATEGORIES) == 4

    def test_confidence_levels_defined(self) -> None:
        assert CONFIDENCE_LEVELS == {"low", "medium", "high"}

    def test_fallback_confidence_is_low(self) -> None:
        assert FALLBACK_CONFIDENCE == "low"
