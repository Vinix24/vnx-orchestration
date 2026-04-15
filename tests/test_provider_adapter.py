#!/usr/bin/env python3
"""tests/test_provider_adapter.py — Unit tests for provider_adapter + adapters package.

Covers:
  1. ClaudeAdapter capability set
  2. ClaudeAdapter.is_available() — mocked PATH check
  3. resolve_adapter() default → ClaudeAdapter
  4. resolve_adapter() unknown provider → ValueError
  5. Capability check: CODE mismatch blocks non-code providers
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/lib is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# 1. ClaudeAdapter capabilities
# ---------------------------------------------------------------------------

class TestClaudeAdapterCapabilities:

    def setup_method(self) -> None:
        from adapters.claude_adapter import ClaudeAdapter
        from provider_adapter import Capability
        self.adapter = ClaudeAdapter("T1")
        self.Capability = Capability

    def test_claude_adapter_capabilities(self) -> None:
        caps = self.adapter.capabilities()
        assert self.Capability.CODE in caps
        assert self.Capability.REVIEW in caps
        assert self.Capability.DECISION in caps
        assert self.Capability.DIGEST in caps

    def test_claude_adapter_name(self) -> None:
        assert self.adapter.name() == "claude"

    def test_claude_adapter_supports_code(self) -> None:
        assert self.adapter.supports(self.Capability.CODE) is True

    def test_claude_adapter_supports_review(self) -> None:
        assert self.adapter.supports(self.Capability.REVIEW) is True


# ---------------------------------------------------------------------------
# 2. ClaudeAdapter.is_available()
# ---------------------------------------------------------------------------

class TestClaudeAdapterAvailability:

    def setup_method(self) -> None:
        from adapters.claude_adapter import ClaudeAdapter
        self.adapter = ClaudeAdapter("T1")

    def test_claude_adapter_is_available_when_found(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert self.adapter.is_available() is True

    def test_claude_adapter_not_available_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            assert self.adapter.is_available() is False


# ---------------------------------------------------------------------------
# 3. resolve_adapter() — default returns ClaudeAdapter
# ---------------------------------------------------------------------------

class TestResolveAdapterDefault:

    def test_resolve_adapter_default_claude(self) -> None:
        from adapters import resolve_adapter
        from adapters.claude_adapter import ClaudeAdapter

        # Ensure no VNX_PROVIDER_T1 override is set
        env = {k: v for k, v in os.environ.items() if k != "VNX_PROVIDER_T1"}
        with patch.dict(os.environ, env, clear=True):
            adapter = resolve_adapter("T1")
        assert isinstance(adapter, ClaudeAdapter)

    def test_resolve_adapter_explicit_claude(self) -> None:
        from adapters import resolve_adapter
        from adapters.claude_adapter import ClaudeAdapter

        with patch.dict(os.environ, {"VNX_PROVIDER_T1": "claude"}):
            adapter = resolve_adapter("T1")
        assert isinstance(adapter, ClaudeAdapter)

    def test_resolve_adapter_case_insensitive(self) -> None:
        from adapters import resolve_adapter
        from adapters.claude_adapter import ClaudeAdapter

        with patch.dict(os.environ, {"VNX_PROVIDER_T2": "CLAUDE"}):
            adapter = resolve_adapter("T2")
        assert isinstance(adapter, ClaudeAdapter)


# ---------------------------------------------------------------------------
# 4. resolve_adapter() — unknown provider raises ValueError
# ---------------------------------------------------------------------------

class TestResolveAdapterUnknown:

    def test_resolve_adapter_unknown_raises(self) -> None:
        from adapters import resolve_adapter

        with patch.dict(os.environ, {"VNX_PROVIDER_T1": "openai"}):
            with pytest.raises(ValueError, match="Unknown provider"):
                resolve_adapter("T1")

    def test_resolve_adapter_unknown_message_contains_terminal(self) -> None:
        from adapters import resolve_adapter

        with patch.dict(os.environ, {"VNX_PROVIDER_T3": "gemini-future"}):
            with pytest.raises(ValueError, match="T3"):
                resolve_adapter("T3")


# ---------------------------------------------------------------------------
# 5. Capability check blocks mismatch
# ---------------------------------------------------------------------------

class TestCapabilityCheckBlocksMismatch:
    """Verify that a provider without CODE capability fails the supports() check."""

    def test_capability_check_blocks_mismatch(self) -> None:
        from provider_adapter import Capability, ProviderAdapter, AdapterResult

        class ReviewOnlyAdapter(ProviderAdapter):
            def name(self) -> str:
                return "review-only"

            def capabilities(self) -> set[Capability]:
                return {Capability.REVIEW, Capability.DIGEST}

            def execute(self, instruction: str, context: dict) -> AdapterResult:
                raise NotImplementedError

            def stream_events(self, instruction: str, context: dict):
                return iter([])

            def is_available(self) -> bool:
                return True

        adapter = ReviewOnlyAdapter()
        assert adapter.supports(Capability.CODE) is False
        assert adapter.supports(Capability.REVIEW) is True

    def test_capability_check_passes_for_claude(self) -> None:
        from adapters.claude_adapter import ClaudeAdapter
        from provider_adapter import Capability

        adapter = ClaudeAdapter("T1")
        assert adapter.supports(Capability.CODE) is True

    def test_capability_enum_values(self) -> None:
        from provider_adapter import Capability

        assert Capability.CODE.value == "code"
        assert Capability.REVIEW.value == "review"
        assert Capability.DECISION.value == "decision"
        assert Capability.DIGEST.value == "digest"
