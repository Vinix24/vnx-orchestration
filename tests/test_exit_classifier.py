#!/usr/bin/env python3
"""
Tests for PR-2: Exit Classifier — Contract Section 4.

Gate: gate_pr2_logs_and_classification
Covers:
  - All 8 failure classes are correctly classified
  - Decision tree ordering (first match wins)
  - Classification evidence structure (Section 4.3)
  - Edge cases: empty stderr, missing exit code, combined signals

Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 4
"""

import sys
import unittest
from pathlib import Path

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from exit_classifier import (
    classify_exit,
    ClassificationResult,
    SUCCESS,
    TIMEOUT,
    NO_OUTPUT,
    INTERRUPTED,
    INFRA_FAIL,
    TOOL_FAIL,
    PROMPT_ERR,
    UNKNOWN,
)


class TestSuccessClassification(unittest.TestCase):

    def test_exit_code_zero_is_success(self):
        result = classify_exit(exit_code=0)
        self.assertEqual(result.failure_class, SUCCESS)
        self.assertFalse(result.retryable)

    def test_exit_code_zero_with_stderr_still_success(self):
        result = classify_exit(exit_code=0, stderr="some warning")
        self.assertEqual(result.failure_class, SUCCESS)

    def test_exit_code_zero_overrides_no_output(self):
        """Success takes priority over no_output_detected flag."""
        result = classify_exit(exit_code=0, no_output_detected=True)
        self.assertEqual(result.failure_class, SUCCESS)


class TestTimeoutClassification(unittest.TestCase):

    def test_timeout_detected(self):
        result = classify_exit(exit_code=None, timed_out=True)
        self.assertEqual(result.failure_class, TIMEOUT)
        self.assertTrue(result.retryable)
        self.assertIn("timeout", result.classification_reason)

    def test_timeout_with_exit_code(self):
        result = classify_exit(exit_code=-9, timed_out=True)
        self.assertEqual(result.failure_class, TIMEOUT)

    def test_timeout_beats_stderr_patterns(self):
        """Timeout takes priority over tool error patterns in stderr."""
        result = classify_exit(
            exit_code=None,
            timed_out=True,
            stderr="API error: rate limit exceeded",
        )
        self.assertEqual(result.failure_class, TIMEOUT)


class TestNoOutputClassification(unittest.TestCase):

    def test_no_output_hang(self):
        result = classify_exit(exit_code=1, no_output_detected=True)
        self.assertEqual(result.failure_class, NO_OUTPUT)
        self.assertTrue(result.retryable)
        self.assertIn("no output", result.classification_reason)

    def test_no_output_beats_signal(self):
        """No-output takes priority over signal classification."""
        result = classify_exit(exit_code=-2, no_output_detected=True)
        self.assertEqual(result.failure_class, NO_OUTPUT)


class TestInterruptedClassification(unittest.TestCase):

    def test_sigint(self):
        result = classify_exit(exit_code=-2)  # SIGINT
        self.assertEqual(result.failure_class, INTERRUPTED)
        self.assertEqual(result.signal, 2)
        self.assertTrue(result.retryable)

    def test_sigterm(self):
        result = classify_exit(exit_code=-15)  # SIGTERM
        self.assertEqual(result.failure_class, INTERRUPTED)
        self.assertEqual(result.signal, 15)

    def test_sighup(self):
        result = classify_exit(exit_code=-1)  # SIGHUP
        self.assertEqual(result.failure_class, INTERRUPTED)
        self.assertEqual(result.signal, 1)

    def test_sigkill_not_interrupted(self):
        """SIGKILL (-9) is not in the interrupt signal set, falls through."""
        result = classify_exit(exit_code=-9)
        self.assertNotEqual(result.failure_class, INTERRUPTED)


class TestInfraFailClassification(unittest.TestCase):

    def test_binary_not_found_flag(self):
        result = classify_exit(exit_code=None, binary_not_found=True)
        self.assertEqual(result.failure_class, INFRA_FAIL)
        self.assertTrue(result.retryable)

    def test_command_not_found_stderr(self):
        result = classify_exit(exit_code=127, stderr="bash: claude: command not found")
        self.assertEqual(result.failure_class, INFRA_FAIL)

    def test_permission_denied_stderr(self):
        result = classify_exit(exit_code=1, stderr="Permission denied")
        self.assertEqual(result.failure_class, INFRA_FAIL)

    def test_disk_full_stderr(self):
        result = classify_exit(exit_code=1, stderr="No space left on device")
        self.assertEqual(result.failure_class, INFRA_FAIL)

    def test_oom_stderr(self):
        result = classify_exit(exit_code=1, stderr="Cannot allocate memory")
        self.assertEqual(result.failure_class, INFRA_FAIL)


class TestToolFailClassification(unittest.TestCase):

    def test_api_error(self):
        result = classify_exit(exit_code=1, stderr="API error: model overloaded")
        self.assertEqual(result.failure_class, TOOL_FAIL)
        self.assertTrue(result.retryable)

    def test_rate_limit(self):
        result = classify_exit(exit_code=1, stderr="Error 429: Too Many Requests")
        self.assertEqual(result.failure_class, TOOL_FAIL)

    def test_context_limit(self):
        result = classify_exit(exit_code=1, stderr="context length exceeded")
        self.assertEqual(result.failure_class, TOOL_FAIL)

    def test_connection_refused(self):
        result = classify_exit(exit_code=1, stderr="connection refused")
        self.assertEqual(result.failure_class, TOOL_FAIL)

    def test_service_unavailable(self):
        result = classify_exit(exit_code=1, stderr="503 Service Unavailable")
        self.assertEqual(result.failure_class, TOOL_FAIL)

    def test_auth_error(self):
        result = classify_exit(exit_code=1, stderr="401 Unauthorized")
        self.assertEqual(result.failure_class, TOOL_FAIL)


class TestPromptErrClassification(unittest.TestCase):

    def test_invalid_prompt(self):
        result = classify_exit(exit_code=1, stderr="Error: invalid prompt format")
        self.assertEqual(result.failure_class, PROMPT_ERR)
        self.assertFalse(result.retryable)

    def test_prompt_too_long(self):
        result = classify_exit(exit_code=1, stderr="prompt too long for model")
        self.assertEqual(result.failure_class, PROMPT_ERR)

    def test_malformed_json(self):
        result = classify_exit(exit_code=1, stderr="malformed JSON input")
        self.assertEqual(result.failure_class, PROMPT_ERR)

    def test_schema_validation_error(self):
        result = classify_exit(exit_code=1, stderr="schema validation error on input")
        self.assertEqual(result.failure_class, PROMPT_ERR)


class TestUnknownClassification(unittest.TestCase):

    def test_generic_failure(self):
        result = classify_exit(exit_code=1, stderr="something went wrong")
        self.assertEqual(result.failure_class, UNKNOWN)
        self.assertFalse(result.retryable)

    def test_empty_stderr(self):
        result = classify_exit(exit_code=1, stderr="")
        self.assertEqual(result.failure_class, UNKNOWN)

    def test_none_exit_code_no_flags(self):
        result = classify_exit(exit_code=None)
        self.assertEqual(result.failure_class, UNKNOWN)


class TestClassificationEvidence(unittest.TestCase):
    """Section 4.3: classification evidence structure."""

    def test_evidence_fields_present(self):
        result = classify_exit(exit_code=1, stderr="API error")
        self.assertIsNotNone(result.failure_class)
        self.assertIsNotNone(result.classification_reason)
        self.assertIsInstance(result.retryable, bool)
        self.assertIsInstance(result.operator_hint, str)

    def test_stderr_tail_truncated(self):
        long_stderr = "x" * 1000
        result = classify_exit(exit_code=1, stderr=long_stderr)
        self.assertLessEqual(len(result.stderr_tail), 500)

    def test_signal_captured_for_interrupted(self):
        result = classify_exit(exit_code=-2)
        self.assertEqual(result.signal, 2)

    def test_signal_none_for_non_signal(self):
        result = classify_exit(exit_code=0)
        self.assertIsNone(result.signal)


class TestDecisionTreeOrdering(unittest.TestCase):
    """Verify first-match-wins ordering from Section 4.2."""

    def test_success_beats_everything(self):
        """Exit code 0 is always SUCCESS regardless of other flags."""
        result = classify_exit(exit_code=0, stderr="Permission denied")
        self.assertEqual(result.failure_class, SUCCESS)

    def test_timeout_beats_signal(self):
        result = classify_exit(exit_code=-9, timed_out=True)
        self.assertEqual(result.failure_class, TIMEOUT)

    def test_timeout_beats_infra(self):
        result = classify_exit(exit_code=None, timed_out=True, binary_not_found=True)
        self.assertEqual(result.failure_class, TIMEOUT)

    def test_no_output_beats_infra(self):
        result = classify_exit(
            exit_code=1, no_output_detected=True, stderr="Permission denied"
        )
        self.assertEqual(result.failure_class, NO_OUTPUT)

    def test_infra_beats_tool_fail(self):
        """When stderr matches both infra and tool patterns, infra wins."""
        result = classify_exit(
            exit_code=1, stderr="Permission denied, API error"
        )
        self.assertEqual(result.failure_class, INFRA_FAIL)

    def test_tool_fail_beats_prompt_err(self):
        result = classify_exit(
            exit_code=1, stderr="API error: invalid prompt"
        )
        self.assertEqual(result.failure_class, TOOL_FAIL)


if __name__ == "__main__":
    unittest.main()
