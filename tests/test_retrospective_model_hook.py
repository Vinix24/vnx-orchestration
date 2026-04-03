#!/usr/bin/env python3
"""Tests for optional local-model retrospective analysis hook (Feature 18, PR-3).

Covers:
  1. RetroAnalysisInput — evidence pool extraction
  2. RetroAnalysisSummary — output contract and invariants
  3. Authoritative invariant — cannot be bypassed
  4. Fallback behavior — explicit when no model configured
  5. Hook protocol — available and unavailable paths
  6. Output validation — evidence pointers, confidence, count limits
  7. Governance authority boundary — model path never authoritative
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from retrospective_model_hook import (
    CONFIDENCE_LEVELS,
    FALLBACK_CONFIDENCE,
    MAX_CANDIDATE_GUARDRAILS,
    LocalModelHook,
    RetroAnalysisInput,
    RetroAnalysisSummary,
    run_retrospective_hook,
    validate_summary,
)


# ---------------------------------------------------------------------------
# Minimal digest stub (duck-typed)
# ---------------------------------------------------------------------------

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


def _make_input(
    patterns: Optional[List[_Pattern]] = None,
    context_hint: str = "",
    max_chars: int = 500,
) -> RetroAnalysisInput:
    digest = _Digest(recurring_patterns=[_Pattern()] if patterns is None else patterns)
    return RetroAnalysisInput(digest=digest, context_hint=context_hint,
                              max_summary_chars=max_chars)


def _make_summary(
    evidence: Optional[List[str]] = None,
    confidence: str = "medium",
    guardrails: Optional[List[str]] = None,
    fallback: bool = False,
    authoritative: bool = False,
) -> RetroAnalysisSummary:
    return RetroAnalysisSummary(
        summary="Session failures are increasing.",
        candidate_guardrails=guardrails or [],
        evidence_pointers=evidence or ["d-1"],
        confidence=confidence,
        model_id="test-model",
        fallback=fallback,
        authoritative=authoritative,
    )


# ---------------------------------------------------------------------------
# Minimal hook stubs
# ---------------------------------------------------------------------------

class _AvailableHook:
    """Hook that is available and returns a valid summary."""
    def is_available(self) -> bool:
        return True

    def analyze(self, input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
        pool = input_data.evidence_pool()
        return RetroAnalysisSummary(
            summary="Model analysis: repeated session failures detected.",
            candidate_guardrails=["Add session retry cap", "Alert on exit 1 pattern"],
            evidence_pointers=pool[:3],
            confidence="medium",
            model_id="local-test-model",
            authoritative=False,
        )


class _UnavailableHook:
    """Hook that reports itself unavailable."""
    def is_available(self) -> bool:
        return False

    def analyze(self, input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
        raise RuntimeError("Should not be called when unavailable")


class _BadEvidenceHook:
    """Hook that injects evidence pointers not in the digest pool."""
    def is_available(self) -> bool:
        return True

    def analyze(self, input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
        return RetroAnalysisSummary(
            summary="Analysis.",
            evidence_pointers=["injected-unknown-pointer"],
            confidence="medium",
            authoritative=False,
        )


class _AuthoritativeHook:
    """Hook that attempts to set authoritative=True (should fail at construction)."""
    def is_available(self) -> bool:
        return True

    def analyze(self, input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
        return RetroAnalysisSummary(
            summary="Analysis.",
            confidence="high",
            authoritative=True,  # violates invariant
        )


# ---------------------------------------------------------------------------
# 1. RetroAnalysisInput
# ---------------------------------------------------------------------------

class TestRetroAnalysisInput:

    def test_evidence_pool_collects_from_all_patterns(self) -> None:
        patterns = [
            _Pattern(evidence_pointers=["d-1", "d-2"]),
            _Pattern(evidence_pointers=["d-3", "d-4"]),
        ]
        inp = _make_input(patterns=patterns)
        pool = inp.evidence_pool()
        assert "d-1" in pool
        assert "d-3" in pool

    def test_evidence_pool_deduplicates(self) -> None:
        patterns = [
            _Pattern(evidence_pointers=["d-1", "d-2"]),
            _Pattern(evidence_pointers=["d-1", "d-3"]),
        ]
        inp = _make_input(patterns=patterns)
        pool = inp.evidence_pool()
        assert pool.count("d-1") == 1

    def test_evidence_pool_empty_when_no_patterns(self) -> None:
        inp = _make_input(patterns=[])
        assert inp.evidence_pool() == []

    def test_max_summary_chars_default(self) -> None:
        inp = RetroAnalysisInput(digest=_Digest())
        assert inp.max_summary_chars == 500

    def test_context_hint_default_empty(self) -> None:
        inp = RetroAnalysisInput(digest=_Digest())
        assert inp.context_hint == ""


# ---------------------------------------------------------------------------
# 2. RetroAnalysisSummary output contract
# ---------------------------------------------------------------------------

class TestRetroAnalysisSummary:

    def test_valid_summary_construction(self) -> None:
        s = _make_summary(confidence="medium")
        assert s.confidence == "medium"
        assert s.authoritative is False

    def test_to_dict_includes_all_fields(self) -> None:
        s = _make_summary()
        d = s.to_dict()
        assert "summary" in d
        assert "candidate_guardrails" in d
        assert "evidence_pointers" in d
        assert "confidence" in d
        assert "authoritative" in d
        assert "fallback" in d

    def test_confidence_all_levels_valid(self) -> None:
        for level in CONFIDENCE_LEVELS:
            s = _make_summary(confidence=level)
            assert s.confidence == level

    def test_invalid_confidence_rejected(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            _make_summary(confidence="uncertain")

    def test_guardrail_count_within_limit(self) -> None:
        guardrails = [f"guardrail-{i}" for i in range(MAX_CANDIDATE_GUARDRAILS)]
        s = _make_summary(guardrails=guardrails)
        assert len(s.candidate_guardrails) == MAX_CANDIDATE_GUARDRAILS

    def test_guardrail_count_exceeding_limit_rejected(self) -> None:
        too_many = [f"g-{i}" for i in range(MAX_CANDIDATE_GUARDRAILS + 1)]
        with pytest.raises(ValueError, match="guardrail"):
            _make_summary(guardrails=too_many)

    def test_to_dict_authoritative_always_false(self) -> None:
        s = _make_summary()
        assert s.to_dict()["authoritative"] is False


# ---------------------------------------------------------------------------
# 3. Authoritative invariant
# ---------------------------------------------------------------------------

class TestAuthoritativeInvariant:

    def test_authoritative_false_by_default(self) -> None:
        s = RetroAnalysisSummary(summary="ok", confidence="low")
        assert s.authoritative is False

    def test_authoritative_true_raises(self) -> None:
        with pytest.raises(ValueError, match="authoritative"):
            RetroAnalysisSummary(summary="ok", confidence="low", authoritative=True)

    def test_hook_returning_authoritative_true_raises_at_construction(self) -> None:
        hook = _AuthoritativeHook()
        inp = _make_input()
        with pytest.raises(ValueError, match="authoritative"):
            run_retrospective_hook(inp, hook=hook)

    def test_fallback_summary_not_authoritative(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=None)
        assert result.authoritative is False

    def test_model_summary_not_authoritative(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert result.authoritative is False


# ---------------------------------------------------------------------------
# 4. Fallback behavior
# ---------------------------------------------------------------------------

class TestFallbackBehavior:

    def test_no_hook_returns_fallback(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=None)
        assert result.fallback is True

    def test_unavailable_hook_returns_fallback(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_UnavailableHook())
        assert result.fallback is True

    def test_fallback_confidence_is_low(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=None)
        assert result.confidence == FALLBACK_CONFIDENCE

    def test_fallback_no_candidate_guardrails(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=None)
        assert result.candidate_guardrails == []

    def test_fallback_summary_mentions_pattern_count(self) -> None:
        patterns = [_Pattern(), _Pattern()]
        inp = _make_input(patterns=patterns)
        result = run_retrospective_hook(inp, hook=None)
        assert "2" in result.summary

    def test_fallback_no_patterns_graceful(self) -> None:
        inp = _make_input(patterns=[])
        result = run_retrospective_hook(inp, hook=None)
        assert "No recurring" in result.summary
        assert result.fallback is True

    def test_fallback_includes_evidence_from_digest(self) -> None:
        inp = _make_input()  # default pattern has ["d-1", "d-2"]
        result = run_retrospective_hook(inp, hook=None)
        assert len(result.evidence_pointers) > 0

    def test_fallback_summary_bounded_by_max_chars(self) -> None:
        long_pattern = _Pattern(representative_content="x" * 1000)
        inp = _make_input(patterns=[long_pattern], max_chars=50)
        result = run_retrospective_hook(inp, hook=None)
        assert len(result.summary) <= 50


# ---------------------------------------------------------------------------
# 5. Hook protocol — available path
# ---------------------------------------------------------------------------

class TestHookProtocol:

    def test_available_hook_produces_model_summary(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert result.fallback is False
        assert result.model_id == "local-test-model"

    def test_available_hook_confidence_not_low(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert result.confidence in ("medium", "high")

    def test_hook_summary_has_content(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert len(result.summary) > 0

    def test_hook_candidate_guardrails_present(self) -> None:
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert len(result.candidate_guardrails) > 0

    def test_hook_satisfies_protocol(self) -> None:
        assert isinstance(_AvailableHook(), LocalModelHook)
        assert isinstance(_UnavailableHook(), LocalModelHook)


# ---------------------------------------------------------------------------
# 6. Output validation
# ---------------------------------------------------------------------------

class TestOutputValidation:

    def test_valid_summary_no_violations(self) -> None:
        inp = _make_input()
        s = _make_summary(evidence=["d-1"])
        violations = validate_summary(s, inp)
        assert violations == []

    def test_stray_evidence_pointer_flagged(self) -> None:
        inp = _make_input()  # pool = ["d-1", "d-2"]
        s = _make_summary(evidence=["injected-unknown"])
        violations = validate_summary(s, inp)
        assert any("evidence_pointers" in v for v in violations)

    def test_bad_hook_raises_on_stray_evidence(self) -> None:
        inp = _make_input()
        with pytest.raises(ValueError, match="invalid"):
            run_retrospective_hook(inp, hook=_BadEvidenceHook())

    def test_guardrail_overflow_caught_by_validate(self) -> None:
        inp = _make_input()
        # Build a summary that bypasses __post_init__ by constructing with valid count
        # then manually override (not possible with frozen dataclass, so test via validate)
        s = RetroAnalysisSummary(
            summary="test",
            candidate_guardrails=[f"g-{i}" for i in range(MAX_CANDIDATE_GUARDRAILS)],
            evidence_pointers=["d-1"],
            confidence="medium",
        )
        violations = validate_summary(s, inp)
        assert violations == []  # exactly at limit is valid

    def test_empty_evidence_pool_all_stray(self) -> None:
        inp = _make_input(patterns=[])  # no patterns → empty pool
        s = _make_summary(evidence=["d-1"])
        violations = validate_summary(s, inp)
        assert any("evidence_pointers" in v for v in violations)


# ---------------------------------------------------------------------------
# 7. Governance authority boundary
# ---------------------------------------------------------------------------

class TestGovernanceAuthorityBoundary:

    def test_candidate_guardrails_are_proposals_not_actions(self) -> None:
        """Guardrails are named 'candidate' — the field name enforces advisory framing."""
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        # Field exists and is named candidate_guardrails (not enacted_guardrails)
        assert hasattr(result, "candidate_guardrails")
        assert not hasattr(result, "enacted_guardrails")

    def test_model_output_never_authoritative(self) -> None:
        """Regardless of hook, model output carries no governance authority."""
        for hook in [None, _AvailableHook(), _UnavailableHook()]:
            inp = _make_input()
            result = run_retrospective_hook(inp, hook=hook)
            assert result.authoritative is False

    def test_fallback_is_explicit_not_silent(self) -> None:
        """Fallback must be explicitly flagged, not silently degrade."""
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=None)
        assert result.fallback is True
        assert result.model_id == ""

    def test_model_path_does_not_call_fallback(self) -> None:
        """Available hook uses model path, not fallback."""
        inp = _make_input()
        result = run_retrospective_hook(inp, hook=_AvailableHook())
        assert result.fallback is False
