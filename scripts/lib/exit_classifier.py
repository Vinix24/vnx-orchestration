#!/usr/bin/env python3
"""Exit classifier for VNX headless CLI runs.

Maps subprocess exit conditions to named failure classes used by the
headless run registry and operator tooling.

Failure classes:
  SUCCESS      — exit code 0, no error conditions
  TIMEOUT      — subprocess exceeded its time limit
  TOOL_FAIL    — transient tool/network error (retryable)
  INFRA_FAIL   — infrastructure problem: binary missing, OOM, etc.
  NO_OUTPUT    — subprocess produced no output (hung or crashed silently)
  INTERRUPTED  — terminated by a signal (SIGTERM, SIGKILL, etc.)
  PROMPT_ERR   — prompt rejected by the CLI (not retryable)
  UNKNOWN      — unrecognised exit condition

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Failure class constants
# ---------------------------------------------------------------------------

FC_SUCCESS = "SUCCESS"
FC_TIMEOUT = "TIMEOUT"
FC_TOOL_FAIL = "TOOL_FAIL"
FC_INFRA_FAIL = "INFRA_FAIL"
FC_NO_OUTPUT = "NO_OUTPUT"
FC_INTERRUPTED = "INTERRUPTED"
FC_PROMPT_ERR = "PROMPT_ERR"
FC_UNKNOWN = "UNKNOWN"

# Operator hints per failure class
_OPERATOR_HINTS = {
    FC_SUCCESS: "Run completed successfully. Check output artifact for results.",
    FC_TIMEOUT: "Increase VNX_HEADLESS_TIMEOUT or simplify the prompt.",
    FC_TOOL_FAIL: "Transient error — retry is safe. Check stderr for details.",
    FC_INFRA_FAIL: "Infrastructure problem — check CLI binary install or system resources.",
    FC_NO_OUTPUT: "Subprocess hung silently. Check for deadlocks or oversized prompts.",
    FC_INTERRUPTED: "Run was killed by a signal. Check for resource limits or manual termination.",
    FC_PROMPT_ERR: "Prompt was rejected by the CLI. Review and fix the prompt before retrying.",
    FC_UNKNOWN: "Unknown failure. Check exit code and stderr for clues.",
}

# Retryable by default per failure class
_RETRYABLE = {
    FC_SUCCESS: False,
    FC_TIMEOUT: True,
    FC_TOOL_FAIL: True,
    FC_INFRA_FAIL: True,
    FC_NO_OUTPUT: True,
    FC_INTERRUPTED: False,
    FC_PROMPT_ERR: False,
    FC_UNKNOWN: True,
}

# Stderr patterns → (failure_class, reason, retryable)
# Checked in order; first match wins.
_STDERR_PATTERNS: list[tuple[re.Pattern, str, str, bool]] = [
    (
        re.compile(r"rate.limit|429|too many requests", re.IGNORECASE),
        FC_TOOL_FAIL, "API rate limit or HTTP 429 detected in stderr", True,
    ),
    (
        re.compile(r"connection.refused|connection.reset|connection.error|network.error|ECONNREFUSED", re.IGNORECASE),
        FC_TOOL_FAIL, "Network/connection error detected in stderr", True,
    ),
    (
        re.compile(r"timeout|timed.out", re.IGNORECASE),
        FC_TOOL_FAIL, "Timeout keyword in stderr", True,
    ),
    (
        re.compile(r"invalid.prompt|bad.prompt|prompt.error|malformed.prompt", re.IGNORECASE),
        FC_PROMPT_ERR, "Invalid or malformed prompt rejected by CLI", False,
    ),
    (
        re.compile(r"out.of.memory|OOM|killed|cannot.allocate", re.IGNORECASE),
        FC_INFRA_FAIL, "Out-of-memory or resource exhaustion detected in stderr", True,
    ),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    """Result of exit code / stderr classification."""
    failure_class: str
    retryable: bool = False
    signal: Optional[int] = None
    exit_code: Optional[int] = None
    stderr_tail: str = ""
    classification_reason: str = ""
    operator_hint: str = ""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_exit(
    *,
    exit_code: Optional[int],
    timed_out: bool = False,
    stderr: str = "",
    binary_not_found: bool = False,
    no_output_detected: bool = False,
) -> ClassificationResult:
    """Classify a headless subprocess exit into a named failure class.

    Priority order (first match wins):
      1. binary_not_found → INFRA_FAIL
      2. no_output_detected → NO_OUTPUT
      3. timed_out → TIMEOUT
      4. exit_code < 0 → INTERRUPTED (signal termination)
      5. exit_code == 0 → SUCCESS
      6. stderr pattern match → class from pattern table
      7. fallback → UNKNOWN
    """
    stderr_tail = (stderr or "")[-500:].strip()

    if binary_not_found:
        return ClassificationResult(
            failure_class=FC_INFRA_FAIL,
            retryable=_RETRYABLE[FC_INFRA_FAIL],
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="CLI binary not found in PATH",
            operator_hint=_OPERATOR_HINTS[FC_INFRA_FAIL],
        )

    if no_output_detected:
        return ClassificationResult(
            failure_class=FC_NO_OUTPUT,
            retryable=_RETRYABLE[FC_NO_OUTPUT],
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="Subprocess produced no output within the hang threshold",
            operator_hint=_OPERATOR_HINTS[FC_NO_OUTPUT],
        )

    if timed_out:
        return ClassificationResult(
            failure_class=FC_TIMEOUT,
            retryable=_RETRYABLE[FC_TIMEOUT],
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="Subprocess exceeded its configured timeout",
            operator_hint=_OPERATOR_HINTS[FC_TIMEOUT],
        )

    if exit_code is not None and exit_code < 0:
        sig = abs(exit_code)
        return ClassificationResult(
            failure_class=FC_INTERRUPTED,
            retryable=_RETRYABLE[FC_INTERRUPTED],
            signal=sig,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason=f"Subprocess terminated by signal {sig}",
            operator_hint=_OPERATOR_HINTS[FC_INTERRUPTED],
        )

    if exit_code == 0:
        return ClassificationResult(
            failure_class=FC_SUCCESS,
            retryable=_RETRYABLE[FC_SUCCESS],
            exit_code=0,
            stderr_tail=stderr_tail,
            classification_reason="Subprocess exited with code 0",
            operator_hint=_OPERATOR_HINTS[FC_SUCCESS],
        )

    # Stderr pattern matching
    if stderr:
        for pattern, fc, reason, retryable in _STDERR_PATTERNS:
            if pattern.search(stderr):
                return ClassificationResult(
                    failure_class=fc,
                    retryable=retryable,
                    exit_code=exit_code,
                    stderr_tail=stderr_tail,
                    classification_reason=reason,
                    operator_hint=_OPERATOR_HINTS[fc],
                )

    return ClassificationResult(
        failure_class=FC_UNKNOWN,
        retryable=_RETRYABLE[FC_UNKNOWN],
        exit_code=exit_code,
        stderr_tail=stderr_tail,
        classification_reason=f"Unrecognised failure: exit_code={exit_code}",
        operator_hint=_OPERATOR_HINTS[FC_UNKNOWN],
    )
