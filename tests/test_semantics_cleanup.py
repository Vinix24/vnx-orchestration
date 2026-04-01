#!/usr/bin/env python3
"""Tests for residual semantics and classification cleanup (Contract 190).

Validates:
  - RES-A1: delivery_failed:{code} patterns classified correctly (regression test)
  - RES-A2: duplicate_delivery_prevented event classification
  - RES-A4: recovery_cooldown_deferred classified as ambiguous true
  - RES-B2: pre_mode_configuration failure code in registry
  - RES-D1: empty attempt_id scenario documentable
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
    classify_failure_code,
    classify_failure_with_code,
    get_retry_decision,
)


@pytest.fixture
def classify_func():
    """Extract _classify_blocked_dispatch from the dispatcher for subprocess testing."""
    dispatcher = SCRIPT_DIR / "dispatcher_v8_minimal.sh"
    content = dispatcher.read_text(encoding="utf-8")
    start = content.index("_classify_blocked_dispatch() {")
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
    func_def = content[start:end]

    def classify(reason: str) -> str:
        script = f"""{func_def}
_classify_blocked_dispatch "{reason}"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()

    return classify


class TestResA1DeliveryFailedRegression:
    """RES-A1: Verify delivery_failed:{code} patterns don't fall to wildcard."""

    def test_tx_codes_not_invalid(self, classify_func):
        """Transport codes must be ambiguous (requeueable), never invalid."""
        for code in ["tx_send_skill", "tx_load_buffer", "tx_paste_buffer", "tx_send_enter"]:
            result = classify_func(f"delivery_failed:{code}")
            assert result == "ambiguous true", f"delivery_failed:{code} should be ambiguous true, got {result}"

    def test_pre_skill_codes_are_invalid(self, classify_func):
        """Skill/instruction codes must be invalid (not requeueable)."""
        for code in ["pre_skill_empty", "pre_skill_registry", "pre_instruction_empty"]:
            result = classify_func(f"delivery_failed:{code}")
            assert result == "invalid false", f"delivery_failed:{code} should be invalid false, got {result}"

    def test_pre_lease_busy_codes_are_busy(self, classify_func):
        """Lease busy codes must be busy (deferred)."""
        for code in ["pre_canonical_lease_busy", "pre_legacy_lock_busy", "pre_duplicate_delivery"]:
            result = classify_func(f"delivery_failed:{code}")
            assert result == "busy true", f"delivery_failed:{code} should be busy true, got {result}"


class TestResA2DuplicateDeliveryClassification:
    """RES-A2: duplicate_delivery_prevented event type classification."""

    def test_active_claim_same_dispatch_is_busy(self, classify_func):
        """When the holder IS the same dispatch, classification is still busy."""
        result = classify_func("active_claim:20260401-123111-PR-0-C")
        assert result == "busy true"

    def test_duplicate_delivery_code_is_busy(self, classify_func):
        """delivery_failed:pre_duplicate_delivery should be busy true."""
        result = classify_func("delivery_failed:pre_duplicate_delivery")
        assert result == "busy true"

    def test_duplicate_delivery_retry_decision_is_defer(self):
        """pre_duplicate_delivery should have defer retry decision."""
        assert get_retry_decision("pre_duplicate_delivery") == "defer"


class TestResA4RecoveryCooldownDeferred:
    """RES-A4: recovery_cooldown_deferred classified as ambiguous true."""

    def test_cooldown_deferred_is_ambiguous(self, classify_func):
        result = classify_func("recovery_cooldown_deferred")
        assert result == "ambiguous true"

    def test_cooldown_deferred_not_invalid(self, classify_func):
        """Must NOT fall to wildcard (invalid false)."""
        result = classify_func("recovery_cooldown_deferred")
        assert "invalid" not in result


class TestResB2PreModeConfigurationCode:
    """RES-B2: pre_mode_configuration failure code exists in registry."""

    def test_code_in_registry(self):
        assert "pre_mode_configuration" in FAILURE_CODE_REGISTRY

    def test_code_classification(self):
        result = classify_failure_code("pre_mode_configuration")
        assert result is not None
        assert result.failure_class == "hook_feedback_interruption"
        assert result.retryable is True

    def test_retry_decision(self):
        assert get_retry_decision("pre_mode_configuration") == "auto_retry"


class TestResD1EmptyAttemptId:
    """RES-D1: Verify the failure code for empty attempt_id is classifiable."""

    def test_delivery_start_no_attempt_classifiable(self):
        """The delivery_start_no_attempt reason should classify via keywords."""
        result = classify_failure_with_code("delivery_start_no_attempt")
        assert result is not None
        assert result.retryable is True


class TestExistingClassificationsPreserved:
    """Regression: existing classifications must not change."""

    def test_existing_reasons_unchanged(self, classify_func):
        cases = {
            "active_claim:T2": "busy true",
            "status_claimed:T2:working": "busy true",
            "canonical_lease:lease_expired:T2": "ambiguous true",
            "canonical_check_error:parse": "ambiguous true",
            "canonical_check_parse_error": "ambiguous true",
            "canonical_lease_acquire_failed": "ambiguous true",
            "canonical_lease:leased:d-other": "busy true",
            "blocked_input_mode": "ambiguous true",
            "recovery_failed": "ambiguous true",
            "pane_dead": "ambiguous true",
            "probe_failed": "ambiguous true",
            "input_mode_blocked": "ambiguous true",
            "terminal_state_unreadable": "ambiguous true",
        }
        for reason, expected in cases.items():
            result = classify_func(reason)
            assert result == expected, f"{reason}: expected {expected}, got {result}"
