"""Tests for gate_stack_resolver.py — dispatch admission via tier gating.

Covers:
- allow when adapter_tier <= required_tier
- reject when adapter_tier > required_tier
- coding-strict rejects Tier-2 adapters
- business-light rejects Tier-3 adapters but allows Tier-2
- Gemini legacy (Tier 3) rejected under coding-strict
- min_tier_override works correctly
- build_rejection_receipt produces valid structure
- TierAdmissionResult.to_dict() round-trips
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from gate_stack_resolver import (
    TierAdmissionResult,
    build_rejection_receipt,
    check_tier_admission,
)


class TestTierAdmissionAllow:
    def test_claude_allowed_under_coding_strict(self):
        result = check_tier_admission("claude", "coding-strict")
        assert result.decision == "allow"
        assert result.adapter_tier == 1
        assert result.required_tier == 1

    def test_codex_allowed_under_coding_strict(self):
        result = check_tier_admission("codex", "coding-strict")
        assert result.decision == "allow"

    def test_ollama_allowed_under_business_light(self):
        # Ollama Tier 2 meets business-light Tier 2 requirement
        result = check_tier_admission("ollama", "business-light")
        assert result.decision == "allow"
        assert result.adapter_tier == 2
        assert result.required_tier == 2

    def test_gemini_streaming_allowed_under_coding_strict(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        result = check_tier_admission("gemini", "coding-strict")
        assert result.decision == "allow"
        assert result.adapter_tier == 1

    def test_litellm_streaming_allowed_under_coding_strict(self):
        result = check_tier_admission("litellm", "coding-strict", streaming_enabled=True)
        assert result.decision == "allow"
        assert result.adapter_tier == 1

    def test_litellm_fallback_allowed_under_business_light(self):
        result = check_tier_admission("litellm", "business-light", streaming_enabled=False)
        assert result.decision == "allow"
        assert result.adapter_tier == 2
        assert result.required_tier == 2

    def test_tier_3_adapter_allowed_under_minimal(self):
        result = check_tier_admission("gemini", "minimal", streaming_enabled=False)
        # Gemini legacy is Tier 3; minimal requires Tier 3
        assert result.decision == "allow"

    def test_is_allowed_true_for_allow(self):
        result = check_tier_admission("claude", "default")
        assert result.is_allowed() is True


class TestTierAdmissionReject:
    def test_ollama_rejected_under_coding_strict(self):
        # Ollama baseline is Tier 2; coding-strict requires Tier 1
        result = check_tier_admission("ollama", "coding-strict")
        assert result.decision == "reject"
        assert result.adapter_tier == 2
        assert result.required_tier == 1

    def test_gemini_legacy_rejected_under_coding_strict(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "0")
        result = check_tier_admission("gemini", "coding-strict")
        assert result.decision == "reject"
        assert result.adapter_tier == 3
        assert result.required_tier == 1

    def test_gemini_legacy_rejected_under_business_light(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "0")
        result = check_tier_admission("gemini", "business-light")
        assert result.decision == "reject"
        assert result.adapter_tier == 3
        assert result.required_tier == 2

    def test_litellm_fallback_rejected_under_coding_strict(self):
        result = check_tier_admission("litellm", "coding-strict", streaming_enabled=False)
        assert result.decision == "reject"
        assert result.adapter_tier == 2

    def test_is_allowed_false_for_reject(self):
        result = check_tier_admission("ollama", "coding-strict")
        assert result.is_allowed() is False

    def test_reject_reason_is_informative(self):
        result = check_tier_admission("ollama", "coding-strict")
        assert "ollama" in result.reason
        assert "tier" in result.reason.lower()


class TestMinTierOverride:
    def test_override_allows_tier_2_provider_at_tier_2(self):
        result = check_tier_admission("ollama", "coding-strict", min_tier_override=2)
        assert result.decision == "allow"
        assert result.required_tier == 2

    def test_override_rejects_tier_2_provider_at_tier_1(self):
        result = check_tier_admission("ollama", "business-light", min_tier_override=1)
        assert result.decision == "reject"
        assert result.required_tier == 1

    def test_override_takes_precedence_over_variant(self):
        result = check_tier_admission("claude", "business-light", min_tier_override=3)
        # Claude is Tier 1, override min is 3 — Tier 1 <= 3, so allowed
        assert result.decision == "allow"
        assert result.required_tier == 3


class TestTierAdmissionResultInterface:
    def test_to_dict_contains_required_keys(self):
        result = check_tier_admission("codex", "default")
        d = result.to_dict()
        assert "decision" in d
        assert "provider" in d
        assert "adapter_tier" in d
        assert "required_tier" in d
        assert "governance_variant" in d
        assert "reason" in d

    def test_to_dict_values_match_fields(self):
        result = check_tier_admission("codex", "coding-strict")
        d = result.to_dict()
        assert d["decision"] == result.decision
        assert d["provider"] == result.provider
        assert d["adapter_tier"] == result.adapter_tier
        assert d["required_tier"] == result.required_tier

    def test_result_is_frozen(self):
        result = check_tier_admission("claude", "default")
        with pytest.raises((AttributeError, TypeError)):
            result.decision = "reject"  # type: ignore[misc]

    def test_case_normalization(self):
        r1 = check_tier_admission("Claude", "Coding-Strict")
        r2 = check_tier_admission("claude", "coding-strict")
        assert r1.decision == r2.decision
        assert r1.provider == r2.provider
        assert r1.governance_variant == r2.governance_variant


class TestBuildRejectionReceipt:
    def test_rejection_receipt_has_required_fields(self):
        result = check_tier_admission("ollama", "coding-strict")
        receipt = build_rejection_receipt(result, "test-dispatch-001", "T1")
        assert receipt["event_type"] == "task_failed"
        assert receipt["status"] == "rejected"
        assert receipt["dispatch_id"] == "test-dispatch-001"
        assert receipt["terminal"] == "T1"
        assert "timestamp" in receipt
        assert "tier_admission" in receipt

    def test_rejection_receipt_tier_admission_matches_result(self):
        result = check_tier_admission("ollama", "coding-strict")
        receipt = build_rejection_receipt(result, "d-001")
        ta = receipt["tier_admission"]
        assert ta["decision"] == "reject"
        assert ta["provider"] == "ollama"
        assert ta["adapter_tier"] == 2
        assert ta["required_tier"] == 1

    def test_rejection_receipt_reason_is_tier_violation(self):
        result = check_tier_admission("gemini", "coding-strict")
        receipt = build_rejection_receipt(result, "d-002")
        assert receipt["rejection_reason"] == "min_observability_tier_violation"

    def test_rejection_receipt_timestamp_is_iso(self):
        result = check_tier_admission("ollama", "coding-strict")
        receipt = build_rejection_receipt(result, "d-003")
        ts = receipt["timestamp"]
        # ISO 8601 format check
        assert "T" in ts
        assert ts.endswith("Z")
