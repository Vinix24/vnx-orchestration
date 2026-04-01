#!/usr/bin/env python3
"""Tests for delivery failure code registry and structured logging (Contract 160).

Validates:
  - DFL-LOG-1: Every code in the registry maps to a valid failure class
  - DFL-LOG-4: classify_failure_code() returns correct class for all 24 codes
  - classify_failure_with_code() handles delivery_failed:{code} pattern
  - get_retry_decision() returns correct decision for all codes
  - Per-code _classify_blocked_dispatch classification (Section 4.4)
  - Retryable vs non-retryable is deterministic per code
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from failure_classifier import (
    FAILURE_CODE_REGISTRY,
    HOOK_FEEDBACK_INTERRUPTION,
    INVALID_SKILL,
    STALE_LEASE,
    TMUX_TRANSPORT_FAILURE,
    FailureClassification,
    classify_failure_code,
    classify_failure_with_code,
    get_retry_decision,
    is_retryable,
)


class TestFailureCodeRegistry:
    """DFL-LOG-1: Every code maps to a valid failure class."""

    VALID_CLASSES = frozenset({
        "invalid_skill", "stale_lease", "runtime_state_divergence",
        "worker_handoff_failure", "hook_feedback_interruption",
        "tmux_transport_failure",
    })

    def test_registry_has_all_24_codes(self):
        assert len(FAILURE_CODE_REGISTRY) == 24

    def test_all_codes_have_valid_failure_class(self):
        for code, (fc, retryable, decision, summary) in FAILURE_CODE_REGISTRY.items():
            assert fc in self.VALID_CLASSES, f"{code} has invalid failure_class: {fc}"

    def test_all_codes_have_operator_summary(self):
        for code, (fc, retryable, decision, summary) in FAILURE_CODE_REGISTRY.items():
            assert len(summary) > 10, f"{code} has empty/short operator_summary"

    def test_all_codes_have_valid_retry_decision(self):
        valid_decisions = {"auto_retry", "defer", "manual_fix"}
        for code, (fc, retryable, decision, summary) in FAILURE_CODE_REGISTRY.items():
            assert decision in valid_decisions, f"{code} has invalid decision: {decision}"

    def test_retryable_matches_failure_class(self):
        for code, (fc, retryable, decision, summary) in FAILURE_CODE_REGISTRY.items():
            expected_retryable = is_retryable(fc)
            # Codes with "busy" semantics (defer) may still be retryable
            if decision == "defer":
                continue
            assert retryable == expected_retryable, \
                f"{code}: retryable={retryable} but is_retryable({fc})={expected_retryable}"

    def test_code_naming_convention(self):
        valid_prefixes = {"pre", "post", "tx"}
        for code in FAILURE_CODE_REGISTRY:
            prefix = code.split("_")[0]
            assert prefix in valid_prefixes, f"{code} does not follow {{phase}}_{{operation}} format"


class TestClassifyFailureCode:
    """DFL-LOG-4: Direct code→class lookup."""

    def test_known_code_returns_classification(self):
        result = classify_failure_code("tx_send_skill")
        assert result is not None
        assert result.failure_class == TMUX_TRANSPORT_FAILURE
        assert result.retryable is True

    def test_unknown_code_returns_none(self):
        result = classify_failure_code("nonexistent_code")
        assert result is None

    def test_pre_skill_empty_is_not_retryable(self):
        result = classify_failure_code("pre_skill_empty")
        assert result is not None
        assert result.failure_class == INVALID_SKILL
        assert result.retryable is False

    def test_post_input_mode_blocked(self):
        result = classify_failure_code("post_input_mode_blocked")
        assert result is not None
        assert result.failure_class == HOOK_FEEDBACK_INTERRUPTION
        assert result.retryable is True

    def test_all_tx_codes_are_retryable(self):
        tx_codes = [c for c in FAILURE_CODE_REGISTRY if c.startswith("tx_")]
        assert len(tx_codes) > 0
        for code in tx_codes:
            result = classify_failure_code(code)
            assert result.retryable is True, f"{code} should be retryable"

    def test_all_manual_fix_codes_are_not_retryable(self):
        manual_fix_codes = [
            c for c, (_, _, decision, _) in FAILURE_CODE_REGISTRY.items()
            if decision == "manual_fix"
        ]
        for code in manual_fix_codes:
            result = classify_failure_code(code)
            assert result.retryable is False, f"{code} (manual_fix) should not be retryable"


class TestClassifyFailureWithCode:
    """classify_failure_with_code() handles delivery_failed:{code} patterns."""

    def test_delivery_failed_prefix_extracts_code(self):
        result = classify_failure_with_code("delivery_failed:tx_send_skill")
        assert result.failure_class == TMUX_TRANSPORT_FAILURE
        assert result.reason == "tx_send_skill"

    def test_delivery_failed_post_input_mode(self):
        result = classify_failure_with_code("delivery_failed:post_input_mode_blocked")
        assert result.failure_class == HOOK_FEEDBACK_INTERRUPTION

    def test_bare_code_without_prefix(self):
        result = classify_failure_with_code("pre_skill_empty")
        assert result.failure_class == INVALID_SKILL

    def test_unknown_reason_falls_back_to_keyword(self):
        result = classify_failure_with_code("tmux transport error")
        assert result.failure_class == TMUX_TRANSPORT_FAILURE

    def test_unknown_delivery_failed_code_falls_back(self):
        result = classify_failure_with_code("delivery_failed:unknown_code")
        # Falls back to keyword matching — "delivery_failed" doesn't match specific keywords
        assert result is not None


class TestGetRetryDecision:
    """get_retry_decision() maps codes to decisions."""

    def test_auto_retry_codes(self):
        assert get_retry_decision("tx_send_skill") == "auto_retry"
        assert get_retry_decision("pre_executor_resolution") == "auto_retry"
        assert get_retry_decision("post_input_mode_blocked") == "auto_retry"

    def test_defer_codes(self):
        assert get_retry_decision("pre_canonical_lease_busy") == "defer"
        assert get_retry_decision("pre_legacy_lock_busy") == "defer"
        assert get_retry_decision("pre_duplicate_delivery") == "defer"

    def test_manual_fix_codes(self):
        assert get_retry_decision("pre_skill_empty") == "manual_fix"
        assert get_retry_decision("pre_skill_registry") == "manual_fix"
        assert get_retry_decision("pre_instruction_empty") == "manual_fix"
        assert get_retry_decision("pre_validation_empty_role") == "manual_fix"

    def test_unknown_code_defaults_auto_retry(self):
        assert get_retry_decision("nonexistent") == "auto_retry"


class TestClassifyBlockedDispatchCodes:
    """Verify _classify_blocked_dispatch handles delivery_failed:{code} patterns (Section 4.4)."""

    @pytest.fixture(autouse=True)
    def setup_classify(self, tmp_path):
        """Extract _classify_blocked_dispatch from the dispatcher."""
        self.project_root = Path(__file__).resolve().parent.parent
        self.dispatcher = self.project_root / "scripts" / "dispatcher_v8_minimal.sh"
        # Extract just the function definition to avoid sourcing the full dispatcher
        # (which acquires a singleton lock and starts the main loop)
        content = self.dispatcher.read_text(encoding="utf-8")
        start = content.index("_classify_blocked_dispatch() {")
        # Find matching closing brace
        brace_depth = 0
        end = start
        for i, ch in enumerate(content[start:], start=start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i + 1
                    break
        self._func_def = content[start:end]

    def _classify(self, reason: str) -> str:
        """Run _classify_blocked_dispatch in a subprocess with just the extracted function."""
        script = f"""{self._func_def}
_classify_blocked_dispatch "{reason}"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()

    def test_tx_codes_are_ambiguous(self):
        assert self._classify("delivery_failed:tx_send_skill") == "ambiguous true"
        assert self._classify("delivery_failed:tx_load_buffer") == "ambiguous true"
        assert self._classify("delivery_failed:tx_paste_buffer") == "ambiguous true"
        assert self._classify("delivery_failed:tx_send_enter") == "ambiguous true"
        assert self._classify("delivery_failed:tx_load_buffer_codex") == "ambiguous true"
        assert self._classify("delivery_failed:tx_paste_buffer_codex") == "ambiguous true"

    def test_post_codes_are_ambiguous(self):
        assert self._classify("delivery_failed:post_input_mode_blocked") == "ambiguous true"
        assert self._classify("delivery_failed:post_process_exit") == "ambiguous true"

    def test_pre_skill_codes_are_invalid(self):
        assert self._classify("delivery_failed:pre_skill_empty") == "invalid false"
        assert self._classify("delivery_failed:pre_skill_registry") == "invalid false"

    def test_pre_instruction_empty_is_invalid(self):
        assert self._classify("delivery_failed:pre_instruction_empty") == "invalid false"

    def test_pre_validation_empty_role_is_invalid(self):
        assert self._classify("delivery_failed:pre_validation_empty_role") == "invalid false"

    def test_pre_lease_busy_is_busy(self):
        assert self._classify("delivery_failed:pre_canonical_lease_busy") == "busy true"
        assert self._classify("delivery_failed:pre_legacy_lock_busy") == "busy true"
        assert self._classify("delivery_failed:pre_duplicate_delivery") == "busy true"

    def test_other_pre_codes_are_ambiguous(self):
        assert self._classify("delivery_failed:pre_executor_resolution") == "ambiguous true"
        assert self._classify("delivery_failed:pre_mode_configuration") == "ambiguous true"
        assert self._classify("delivery_failed:pre_terminal_resolution") == "ambiguous true"
        assert self._classify("delivery_failed:pre_claim_failed") == "ambiguous true"

    def test_existing_classifications_preserved(self):
        assert self._classify("active_claim:T2") == "busy true"
        assert self._classify("canonical_lease:leased:d-other") == "busy true"
        assert self._classify("blocked_input_mode") == "ambiguous true"
        assert self._classify("recovery_failed") == "ambiguous true"
