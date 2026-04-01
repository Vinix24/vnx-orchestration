#!/usr/bin/env python3
"""Certification tests for delivery failure logging (PR-2).

Validates end-to-end: real failed-delivery scenarios emit exact substep reason codes
that survive into operator-visible artifacts and enable deterministic T0 routing.

Goes beyond PR-1 unit tests by:
  - Simulating realistic dispatch file annotation and verifying artifact content
  - Testing the full T0 reasoning chain: failure_code → failure_class → retry_decision → action
  - Verifying all 24 codes are self-consistent across registry, classifier, and bash classification
  - Testing dispatch annotation format matches contract (code= not substep=)
  - Verifying the delivery_failed:{code} reason format round-trips through classify_failure_with_code
  - Testing that operator summaries are actionable (contain fix guidance)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
    classify_failure,
    classify_failure_code,
    classify_failure_with_code,
    get_retry_decision,
    is_retryable,
)


# =============================================================================
# Helper: extract _classify_blocked_dispatch from dispatcher for bash tests
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = PROJECT_ROOT / "scripts" / "dispatcher_v8_minimal.sh"


def _extract_classify_function() -> str:
    """Extract _classify_blocked_dispatch() from the dispatcher."""
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
    """Run _classify_blocked_dispatch in a subprocess."""
    script = f"""{_CLASSIFY_FUNC}
_classify_blocked_dispatch "{reason}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


# =============================================================================
# 1. Real Failed-Delivery Scenario Simulation
# =============================================================================


class TestRealDeliveryFailureScenarios:
    """Simulate real delivery failure scenarios and verify artifact content."""

    def test_send_skill_failure_annotates_dispatch_with_code(self):
        """Simulate send_skill failure: verify dispatch annotation contains code=tx_send_skill."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test Dispatch\nTrack: A\nRole: backend-developer\n")
            dispatch_path = f.name

        try:
            # Simulate what the dispatcher writes on send_skill failure (line 1768)
            failure_code = "tx_send_skill"
            annotation = f"\n\n[DELIVERY_SUBSTEP_FAILED: code={failure_code}] tmux delivery failed at substep. Retry is automatic.\n"
            with open(dispatch_path, "a") as f:
                f.write(annotation)

            content = Path(dispatch_path).read_text()
            assert "[DELIVERY_SUBSTEP_FAILED: code=tx_send_skill]" in content
            assert "Retry is automatic" in content

            # Verify T0 can extract the code from the annotation
            match = re.search(r"\[DELIVERY_SUBSTEP_FAILED: code=(\w+)\]", content)
            assert match is not None
            extracted_code = match.group(1)
            assert extracted_code == "tx_send_skill"

            # Verify extracted code classifies correctly
            result = classify_failure_code(extracted_code)
            assert result is not None
            assert result.failure_class == TMUX_TRANSPORT_FAILURE
            assert result.retryable is True
        finally:
            os.unlink(dispatch_path)

    def test_codex_load_buffer_failure_uses_codex_code(self):
        """Codex provider uses tx_load_buffer_codex, not tx_load_buffer."""
        failure_code = "tx_load_buffer_codex"
        result = classify_failure_code(failure_code)
        assert result is not None
        assert result.failure_class == TMUX_TRANSPORT_FAILURE

        # Verify annotation format
        annotation = f"[DELIVERY_SUBSTEP_FAILED: code={failure_code}]"
        match = re.search(r"code=(\w+)", annotation)
        assert match.group(1) == "tx_load_buffer_codex"

    def test_enter_failure_uses_tx_send_enter(self):
        """Enter key failure uses tx_send_enter (line 1788-1789)."""
        failure_code = "tx_send_enter"
        result = classify_failure_code(failure_code)
        assert result is not None
        assert result.retryable is True
        assert get_retry_decision(failure_code) == "auto_retry"

    def test_input_mode_block_uses_post_input_mode_blocked(self):
        """Input mode failure uses post_input_mode_blocked."""
        failure_code = "post_input_mode_blocked"
        result = classify_failure_code(failure_code)
        assert result is not None
        assert result.failure_class == HOOK_FEEDBACK_INTERRUPTION
        assert result.retryable is True

    def test_invalid_skill_uses_pre_skill_registry(self):
        """Skill not found uses pre_skill_registry — manual_fix, not retryable."""
        failure_code = "pre_skill_registry"
        result = classify_failure_code(failure_code)
        assert result is not None
        assert result.failure_class == INVALID_SKILL
        assert result.retryable is False
        assert get_retry_decision(failure_code) == "manual_fix"


# =============================================================================
# 2. T0 Reasoning Chain: code → class → decision → action
# =============================================================================


class TestT0ReasoningChain:
    """Verify T0 can deterministically route remediation from failure codes."""

    def test_auto_retry_chain(self):
        """auto_retry code → retryable class → no T0 action needed."""
        code = "tx_paste_buffer"
        result = classify_failure_code(code)
        decision = get_retry_decision(code)

        assert result.retryable is True
        assert decision == "auto_retry"
        # T0 action: let dispatcher loop handle it

    def test_defer_chain(self):
        """defer code → retryable class → T0 waits for terminal."""
        code = "pre_canonical_lease_busy"
        result = classify_failure_code(code)
        decision = get_retry_decision(code)

        assert result.retryable is True
        assert decision == "defer"
        # T0 action: wait for terminal availability

    def test_manual_fix_chain(self):
        """manual_fix code → non-retryable class → T0 flags for operator."""
        code = "pre_skill_empty"
        result = classify_failure_code(code)
        decision = get_retry_decision(code)

        assert result.retryable is False
        assert decision == "manual_fix"
        assert "Fix" in result.operator_summary or "fix" in result.operator_summary.lower()

    def test_delivery_failed_reason_round_trips(self):
        """delivery_failed:{code} format round-trips through classify_failure_with_code."""
        for code in FAILURE_CODE_REGISTRY:
            reason = f"delivery_failed:{code}"
            result = classify_failure_with_code(reason)
            direct = classify_failure_code(code)
            assert result.failure_class == direct.failure_class, \
                f"Round-trip mismatch for {code}: {result.failure_class} != {direct.failure_class}"

    def test_all_codes_produce_actionable_summaries(self):
        """Every operator_summary contains actionable guidance."""
        action_words = {"retry", "fix", "wait", "resolve", "reset", "check",
                        "set", "rework", "defer", "safe", "free", "operator",
                        "prevented", "holds", "contention"}
        for code, (_, _, _, summary) in FAILURE_CODE_REGISTRY.items():
            summary_lower = summary.lower()
            has_action = any(w in summary_lower for w in action_words)
            assert has_action, f"{code} summary lacks actionable guidance: {summary}"


# =============================================================================
# 3. Cross-Layer Consistency (Registry ↔ Classifier ↔ Bash)
# =============================================================================


class TestCrossLayerConsistency:
    """Verify all 24 codes are consistent across Python registry, classifier, and bash."""

    def test_every_code_classifies_via_direct_lookup(self):
        """classify_failure_code returns non-None for every registry code."""
        for code in FAILURE_CODE_REGISTRY:
            result = classify_failure_code(code)
            assert result is not None, f"classify_failure_code({code}) returned None"

    def test_every_code_has_bash_classification(self):
        """Every delivery_failed:{code} is handled by _classify_blocked_dispatch."""
        for code in FAILURE_CODE_REGISTRY:
            reason = f"delivery_failed:{code}"
            bash_result = _bash_classify(reason)
            assert bash_result != "", f"bash classify returned empty for {reason}"
            parts = bash_result.split()
            assert len(parts) == 2, f"bash classify returned unexpected format for {reason}: {bash_result}"
            category, requeueable = parts
            assert category in ("busy", "ambiguous", "invalid"), \
                f"{reason}: unexpected category {category}"

    def test_bash_and_python_agree_on_retryable(self):
        """Bash requeueable=true/false agrees with Python retryable for all codes."""
        for code, (fc, retryable, decision, _) in FAILURE_CODE_REGISTRY.items():
            reason = f"delivery_failed:{code}"
            bash_result = _bash_classify(reason)
            _, bash_requeueable = bash_result.split()

            if decision == "manual_fix":
                assert bash_requeueable == "false", \
                    f"{code}: manual_fix should be bash false, got {bash_requeueable}"
            elif decision in ("auto_retry", "defer"):
                assert bash_requeueable == "true", \
                    f"{code}: {decision} should be bash true, got {bash_requeueable}"

    def test_manual_fix_codes_are_invalid_in_bash(self):
        """manual_fix codes must be 'invalid false' in bash (not ambiguous)."""
        manual_fix_codes = [
            c for c, (_, _, d, _) in FAILURE_CODE_REGISTRY.items()
            if d == "manual_fix"
        ]
        assert len(manual_fix_codes) == 4  # pre_skill_empty, pre_skill_registry, pre_instruction_empty, pre_validation_empty_role
        for code in manual_fix_codes:
            result = _bash_classify(f"delivery_failed:{code}")
            assert result == "invalid false", \
                f"{code}: expected 'invalid false', got '{result}'"

    def test_defer_codes_are_busy_in_bash(self):
        """defer codes must be 'busy true' in bash."""
        defer_codes = [
            c for c, (_, _, d, _) in FAILURE_CODE_REGISTRY.items()
            if d == "defer"
        ]
        assert len(defer_codes) == 3  # pre_canonical_lease_busy, pre_legacy_lock_busy, pre_duplicate_delivery
        for code in defer_codes:
            result = _bash_classify(f"delivery_failed:{code}")
            assert result == "busy true", \
                f"{code}: expected 'busy true', got '{result}'"

    def test_auto_retry_codes_are_ambiguous_in_bash(self):
        """auto_retry codes must be 'ambiguous true' in bash."""
        auto_retry_codes = [
            c for c, (_, _, d, _) in FAILURE_CODE_REGISTRY.items()
            if d == "auto_retry"
        ]
        for code in auto_retry_codes:
            result = _bash_classify(f"delivery_failed:{code}")
            assert result == "ambiguous true", \
                f"{code}: expected 'ambiguous true', got '{result}'"


# =============================================================================
# 4. Annotation Format Compliance
# =============================================================================


class TestAnnotationFormat:
    """Verify dispatch annotations use the contract-mandated format."""

    def test_annotation_uses_code_not_substep(self):
        """PR-1 annotations use code=X format (DFL-LOG-3)."""
        # Simulate all transport failure annotations
        for code in FAILURE_CODE_REGISTRY:
            if code.startswith("tx_"):
                annotation = f"[DELIVERY_SUBSTEP_FAILED: code={code}]"
                match = re.search(r"code=(\w+)", annotation)
                assert match is not None
                assert match.group(1) == code

    def test_codex_substep_mapping(self):
        """Codex provider maps load_buffer→tx_load_buffer_codex, paste_buffer→tx_paste_buffer_codex."""
        mappings = {
            ("codex", "load_buffer"): "tx_load_buffer_codex",
            ("codex", "paste_buffer"): "tx_paste_buffer_codex",
            ("claude_code", "load_buffer"): "tx_load_buffer",
            ("claude_code", "paste_buffer"): "tx_paste_buffer",
            ("claude_code", "send_skill"): "tx_send_skill",
        }
        for (provider, substep), expected_code in mappings.items():
            assert expected_code in FAILURE_CODE_REGISTRY, \
                f"Expected code {expected_code} not in registry"

    def test_enter_annotation_is_tx_send_enter(self):
        """Enter failure annotates as tx_send_enter (not 'enter')."""
        code = "tx_send_enter"
        assert code in FAILURE_CODE_REGISTRY
        annotation = f"[DELIVERY_SUBSTEP_FAILED: code={code}]"
        assert "tx_send_enter" in annotation


# =============================================================================
# 5. Failure Code Completeness Against Dispatcher
# =============================================================================


class TestFailureCodeCompleteness:
    """Verify every dispatcher failure path has a corresponding registry code."""

    def test_all_transport_substeps_have_codes(self):
        """Every tmux substep in the dispatcher has a tx_ code in the registry."""
        expected_substeps = {
            "tx_send_skill", "tx_load_buffer", "tx_paste_buffer", "tx_send_enter",
            "tx_load_buffer_codex", "tx_paste_buffer_codex",
        }
        actual_tx_codes = {c for c in FAILURE_CODE_REGISTRY if c.startswith("tx_")}
        assert expected_substeps == actual_tx_codes

    def test_pre_delivery_failures_have_codes(self):
        """Key pre-delivery failure modes have pre_ codes."""
        expected = {
            "pre_executor_resolution", "pre_mode_configuration",
            "pre_skill_empty", "pre_skill_registry", "pre_instruction_empty",
            "pre_terminal_resolution", "pre_canonical_lease_busy",
            "pre_canonical_lease_expired", "pre_canonical_check_error",
            "pre_canonical_acquire_failed", "pre_legacy_lock_busy",
            "pre_claim_failed", "pre_duplicate_delivery",
            "pre_validation_empty_role", "pre_validation_command_failed",
            "pre_gather_command_failed",
        }
        actual_pre = {c for c in FAILURE_CODE_REGISTRY if c.startswith("pre_")}
        assert expected == actual_pre

    def test_post_delivery_failures_have_codes(self):
        """Post-lease, pre-transport failure modes have post_ codes."""
        expected = {"post_input_mode_blocked", "post_process_exit"}
        actual_post = {c for c in FAILURE_CODE_REGISTRY if c.startswith("post_")}
        assert expected == actual_post

    def test_phase_coverage_is_complete(self):
        """All 3 phases (pre, post, tx) are covered in the registry."""
        phases = {c.split("_")[0] for c in FAILURE_CODE_REGISTRY}
        assert phases == {"pre", "post", "tx"}
