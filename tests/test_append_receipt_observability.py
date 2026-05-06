"""Tests for observability_tier stamping in append_receipt_internals.payload.

Covers:
- _stamp_observability_tier adds tier for receipts with a known provider
- _stamp_observability_tier does not overwrite existing observability_tier
- _stamp_observability_tier is a no-op for receipts without provider field
- Each expected provider gets the correct tier stamped
- Unknown provider gets a safe default (no crash)
- Gemini tier follows VNX_GEMINI_STREAM env var
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from append_receipt_internals.payload import _stamp_observability_tier


class TestStampObservabilityTier:
    def test_stamps_claude_as_tier_1(self):
        receipt = {"provider": "claude", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 1

    def test_stamps_codex_as_tier_1(self):
        receipt = {"provider": "codex", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 1

    def test_stamps_gemini_streaming_as_tier_1(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        receipt = {"provider": "gemini", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 1

    def test_stamps_gemini_legacy_as_tier_3(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "0")
        receipt = {"provider": "gemini", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 3

    def test_stamps_ollama_as_tier_2(self):
        receipt = {"provider": "ollama", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 2

    def test_stamps_litellm_as_tier_1(self):
        receipt = {"provider": "litellm", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 1

    def test_does_not_overwrite_existing_tier(self):
        receipt = {"provider": "claude", "observability_tier": 3}
        _stamp_observability_tier(receipt)
        assert receipt["observability_tier"] == 3

    def test_does_not_stamp_when_no_provider(self):
        receipt = {"event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert "observability_tier" not in receipt

    def test_does_not_stamp_when_provider_empty(self):
        receipt = {"provider": "", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        assert "observability_tier" not in receipt

    def test_unknown_provider_does_not_crash(self):
        receipt = {"provider": "unknown-provider-xyz", "event_type": "task_complete"}
        _stamp_observability_tier(receipt)
        # Should not raise; tier may or may not be stamped

    def test_case_insensitive_provider(self):
        receipt_upper = {"provider": "CLAUDE", "event_type": "task_complete"}
        receipt_lower = {"provider": "claude", "event_type": "task_complete"}
        _stamp_observability_tier(receipt_upper)
        _stamp_observability_tier(receipt_lower)
        assert receipt_upper.get("observability_tier") == receipt_lower.get("observability_tier")

    def test_does_not_mutate_receipt_on_noop(self):
        receipt = {"event_type": "task_complete", "terminal": "T1"}
        original_keys = set(receipt.keys())
        _stamp_observability_tier(receipt)
        assert set(receipt.keys()) == original_keys

    def test_observability_tier_value_is_integer(self):
        receipt = {"provider": "claude"}
        _stamp_observability_tier(receipt)
        assert isinstance(receipt.get("observability_tier"), int)


class TestAdapterConstantsMatchRegistry:
    """Verify that adapter-level OBSERVABILITY_TIER constants match the registry."""

    def test_codex_adapter_tier_matches_registry(self):
        from adapters.codex_adapter import OBSERVABILITY_TIER
        from observability_tier import ADAPTER_DEFAULT_TIERS
        assert OBSERVABILITY_TIER == ADAPTER_DEFAULT_TIERS["codex"]

    def test_gemini_adapter_tier_matches_registry(self):
        from adapters.gemini_adapter import OBSERVABILITY_TIER
        from observability_tier import ADAPTER_DEFAULT_TIERS
        assert OBSERVABILITY_TIER == ADAPTER_DEFAULT_TIERS["gemini"]

    def test_litellm_adapter_tier_matches_registry(self):
        from adapters.litellm_adapter import OBSERVABILITY_TIER
        from observability_tier import ADAPTER_DEFAULT_TIERS
        assert OBSERVABILITY_TIER == ADAPTER_DEFAULT_TIERS["litellm"]

    def test_ollama_adapter_tier_matches_registry(self):
        from adapters.ollama_adapter import OBSERVABILITY_TIER
        from observability_tier import ADAPTER_DEFAULT_TIERS
        assert OBSERVABILITY_TIER == ADAPTER_DEFAULT_TIERS["ollama"]

    def test_claude_adapter_tier_matches_registry(self):
        from adapters.claude_adapter import OBSERVABILITY_TIER
        from observability_tier import ADAPTER_DEFAULT_TIERS
        assert OBSERVABILITY_TIER == ADAPTER_DEFAULT_TIERS["claude"]

    def test_streaming_drainer_mixin_declares_tier(self):
        from _streaming_drainer import StreamingDrainerMixin
        assert hasattr(StreamingDrainerMixin, "provider_observability_tier")
        assert StreamingDrainerMixin.provider_observability_tier == 1
