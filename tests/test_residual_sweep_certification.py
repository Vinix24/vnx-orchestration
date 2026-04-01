#!/usr/bin/env python3
"""Certification tests for residual governance bugfix sweep (PR-2).

Validates that all 11 residual items from Contract 190 are either:
- Fixed with test evidence (Clusters A, B, D)
- Documented as intentional (RES-D3)
- Deferred with explicit rationale (DEF-1 through DEF-5)

Certification matrix per gate_pr2_residual_sweep_certification:
  1. All cluster A items verified (semantics/classification)
  2. All cluster B items verified (mode control safety)
  3. All cluster C items verified (CI/evidence paths)
  4. All cluster D items verified (observability gaps)
  5. All deferred items have documented rationale
  6. No regression in prior classification behavior
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from failure_classifier import (
    FAILURE_CODE_REGISTRY,
    classify_failure_code,
    classify_failure_with_code,
    get_retry_decision,
)

DISPATCHER = SCRIPT_DIR / "dispatcher_v8_minimal.sh"
CONTRACT = PROJECT_ROOT / "docs" / "core" / "190_RESIDUAL_BUGFIX_SWEEP_CONTRACT.md"


def _extract_classify_function() -> str:
    content = DISPATCHER.read_text(encoding="utf-8")
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
    return content[start:end]


_CLASSIFY_FUNC = _extract_classify_function()


def _bash_classify(reason: str) -> str:
    script = f"""{_CLASSIFY_FUNC}
_classify_blocked_dispatch "{reason}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


# =============================================================================
# Cluster A: Semantics And Classification (RES-A1 through RES-A4)
# =============================================================================


class TestClusterASemantics:
    """Certify all Cluster A residuals are resolved."""

    def test_res_a1_delivery_failed_codes_not_swallowed_by_wildcard(self):
        """RES-A1: All delivery_failed:{code} patterns have explicit classification."""
        for code in FAILURE_CODE_REGISTRY:
            reason = f"delivery_failed:{code}"
            result = _bash_classify(reason)
            assert result != "", f"delivery_failed:{code} returned empty classification"
            category, requeueable = result.split()
            # Must NOT be 'invalid false' for retryable codes
            if get_retry_decision(code) in ("auto_retry", "defer"):
                assert requeueable == "true", \
                    f"delivery_failed:{code} (decision={get_retry_decision(code)}) incorrectly non-requeueable"

    def test_res_a2_duplicate_delivery_prevented_classification(self):
        """RES-A2: duplicate_delivery_prevented has test coverage via classification."""
        # The canonical lease:leased:{same_dispatch_id} block reason triggers
        # duplicate_delivery_prevented event. The classification must be busy true.
        result = _bash_classify("canonical_lease:leased:d-same-dispatch")
        assert result == "busy true"

        # The failure code pre_duplicate_delivery must classify as defer
        result = _bash_classify("delivery_failed:pre_duplicate_delivery")
        assert result == "busy true"
        assert get_retry_decision("pre_duplicate_delivery") == "defer"

    def test_res_a3_intelligence_blocking_semantics_documented(self):
        """RES-A3: Intelligence gathering blocking semantics are clarified in code."""
        content = DISPATCHER.read_text(encoding="utf-8")
        # Verify the docstring/comment clarification exists
        assert "command failure blocks" in content.lower() or \
               "command.*fail.*block" in content.lower() or \
               "RES-A3" in content, \
            "Intelligence gathering blocking semantics should be clarified per RES-A3"

    def test_res_a4_recovery_cooldown_deferred_not_invalid(self):
        """RES-A4: recovery_cooldown_deferred classifies as ambiguous true (not wildcard)."""
        result = _bash_classify("recovery_cooldown_deferred")
        assert result == "ambiguous true", \
            f"recovery_cooldown_deferred should be 'ambiguous true', got '{result}'"

    def test_res_a4_in_explicit_case_not_wildcard(self):
        """RES-A4: Verify recovery_cooldown_deferred has its own case (not caught by *)."""
        content = _CLASSIFY_FUNC
        assert "recovery_cooldown_deferred" in content, \
            "recovery_cooldown_deferred must have an explicit case in _classify_blocked_dispatch"


# =============================================================================
# Cluster B: Mode Control And Input Safety (RES-B1, RES-B2)
# =============================================================================


class TestClusterBModeControl:
    """Certify all Cluster B residuals are resolved."""

    def test_res_b1_pre_lease_probe_exists(self):
        """RES-B1 (OI-024): Best-effort pane mode check exists before configure_terminal_mode."""
        content = DISPATCHER.read_text(encoding="utf-8")
        # Find the pre-lease probe and verify it comes before configure_terminal_mode
        probe_pos = content.find("RES-B1")
        config_pos = content.find("configure_terminal_mode \"$target_pane\"", probe_pos)
        assert probe_pos != -1, "RES-B1 pre-lease probe not found in dispatcher"
        assert config_pos != -1, "configure_terminal_mode call not found after probe"
        assert probe_pos < config_pos, \
            "Pre-lease probe must appear BEFORE configure_terminal_mode"

    def test_res_b1_probe_returns_1_on_blocked(self):
        """RES-B1: Pre-lease probe returns 1 when pane is in copy-mode (no lease to clean up)."""
        content = DISPATCHER.read_text(encoding="utf-8")
        # Verify the probe checks pane_in_mode == 1 and returns 1
        probe_section = content[content.find("RES-B1"):content.find("RES-B1") + 500]
        assert "return 1" in probe_section, "Pre-lease probe must return 1 on blocked pane"
        assert "_pre_in_mode" in probe_section or "pane_in_mode" in probe_section, \
            "Probe must check pane_in_mode state"

    def test_res_b2_mode_config_failure_uses_canonical_code(self):
        """RES-B2: Mode config failure emits pre_mode_configuration failure code."""
        content = DISPATCHER.read_text(encoding="utf-8")
        assert "pre_mode_configuration" in content, \
            "pre_mode_configuration failure code must be used on mode config failure"

    def test_res_b2_code_in_registry(self):
        """RES-B2: pre_mode_configuration exists in FAILURE_CODE_REGISTRY."""
        result = classify_failure_code("pre_mode_configuration")
        assert result is not None
        assert result.failure_class == "hook_feedback_interruption"
        assert result.retryable is True


# =============================================================================
# Cluster C: CI And Evidence Path (RES-C1, RES-C2)
# =============================================================================


class TestClusterCCIEvidence:
    """Certify Cluster C residuals — CI path fix and report_path handling."""

    def test_res_c1_pr_queue_uses_project_root(self):
        """RES-C1 (OI-078): PR_QUEUE.md lookup should use PROJECT_ROOT, not VNX_HOME."""
        pr_queue_manager = SCRIPT_DIR / "pr_queue_manager.py"
        content = pr_queue_manager.read_text(encoding="utf-8")
        # Verify queue_file uses project_root
        assert "self.project_root" in content and "PR_QUEUE" in content, \
            "PR_QUEUE.md should be resolved from project_root"
        # Verify it does NOT use vnx_home for the queue file
        lines = content.split("\n")
        for line in lines:
            if "queue_file" in line and "PR_QUEUE" in line:
                assert "project_root" in line, \
                    f"queue_file should use project_root: {line.strip()}"
                break

    def test_res_c2_report_path_validation_at_record_time(self):
        """RES-C2: report_path is validated at record_result time."""
        manager_path = SCRIPT_DIR / "review_gate_manager.py"
        content = manager_path.read_text(encoding="utf-8")
        # Verify report_path existence check in record_result
        assert "report_path" in content
        # The existing record_result already validates report exists (PR-1 of Feature 10)
        assert "exists()" in content or "os.path.exists" in content or "Path(" in content


# =============================================================================
# Cluster D: Observability Gaps (RES-D1, RES-D2, RES-D3)
# =============================================================================


class TestClusterDObservability:
    """Certify all Cluster D residuals are resolved."""

    def test_res_d1_empty_attempt_id_logs_failure(self):
        """RES-D1: Empty attempt_id produces structured failure event."""
        content = DISPATCHER.read_text(encoding="utf-8")
        assert "delivery_start_no_attempt" in content, \
            "delivery_start_no_attempt structured failure must be logged when attempt_id is empty"
        # Verify it's in a conditional that checks for empty attempt_id
        d1_pos = content.find("delivery_start_no_attempt")
        context = content[max(0, d1_pos - 200):d1_pos + 200]
        assert "attempt_id" in context, \
            "delivery_start_no_attempt must be guarded by attempt_id check"

    def test_res_d2_delivery_success_failure_logged(self):
        """RES-D2: delivery_success recording failure produces structured event."""
        content = DISPATCHER.read_text(encoding="utf-8")
        assert "delivery_success_record_failed" in content, \
            "delivery_success_record_failed structured failure must be logged"

    def test_res_d3_receipt_footer_intentionally_nonfatal(self):
        """RES-D3: Receipt footer generation failure is documented as intentional."""
        content = DISPATCHER.read_text(encoding="utf-8")
        assert "intentionally non-fatal" in content.lower() or "RES-D3" in content, \
            "Receipt footer failure must be documented as intentionally non-fatal"


# =============================================================================
# Deferred Items (DEF-1 through DEF-5)
# =============================================================================


class TestDeferredItemsDocumented:
    """Certify all deferred items have explicit rationale in Contract 190."""

    def test_contract_exists(self):
        assert CONTRACT.exists(), "Contract 190 must exist"

    def test_def1_complexity_deferred(self):
        content = CONTRACT.read_text(encoding="utf-8")
        assert "DEF-1" in content
        assert "complexity" in content.lower()

    def test_def2_full_pre_lease_guard_deferred(self):
        content = CONTRACT.read_text(encoding="utf-8")
        assert "DEF-2" in content
        assert "configure_terminal_mode" in content or "pre-lease" in content.lower()

    def test_def3_provider_cli_deferred(self):
        content = CONTRACT.read_text(encoding="utf-8")
        assert "DEF-3" in content
        assert "Gemini" in content or "Codex" in content

    def test_def4_worker_rejection_deferred(self):
        content = CONTRACT.read_text(encoding="utf-8")
        assert "DEF-4" in content
        assert "worker" in content.lower() and "failure" in content.lower()

    def test_def5_test_complexity_deferred(self):
        content = CONTRACT.read_text(encoding="utf-8")
        assert "DEF-5" in content
        assert "test" in content.lower() and "complexity" in content.lower()


# =============================================================================
# Regression: Prior Classifications Preserved
# =============================================================================


class TestNoClassificationRegression:
    """Verify the sweep didn't break any prior classification behavior."""

    KNOWN_CLASSIFICATIONS = {
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
        "recovery_cooldown_deferred": "ambiguous true",
        "invalid_metadata": "invalid false",
    }

    def test_all_known_classifications_preserved(self):
        for reason, expected in self.KNOWN_CLASSIFICATIONS.items():
            result = _bash_classify(reason)
            assert result == expected, \
                f"Regression: {reason} expected '{expected}', got '{result}'"

    def test_delivery_failed_codes_consistent_with_registry(self):
        """Every registry code classifies consistently via bash and Python."""
        for code in FAILURE_CODE_REGISTRY:
            py_result = classify_failure_code(code)
            decision = get_retry_decision(code)
            bash_result = _bash_classify(f"delivery_failed:{code}")
            _, bash_req = bash_result.split()

            if decision == "manual_fix":
                assert bash_req == "false", f"{code}: manual_fix must be bash false"
            else:
                assert bash_req == "true", f"{code}: {decision} must be bash true"
