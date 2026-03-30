#!/usr/bin/env python3
"""
VNX Exit Classifier — Classify headless run exit outcomes per Contract Section 4.

Implements the failure class decision tree (Section 4.2, Appendix B) to produce
structured classification evidence (Section 4.3) for every headless run outcome.

Failure classes:
  SUCCESS      — exit code 0, output persisted
  TIMEOUT      — subprocess exceeded VNX_HEADLESS_TIMEOUT
  NO_OUTPUT    — process alive but no output for > threshold
  INTERRUPTED  — SIGINT, SIGTERM, SIGHUP received
  INFRA_FAIL   — binary not found, permission denied, disk full, OOM
  TOOL_FAIL    — exit code != 0, stderr indicates tool/API error
  PROMPT_ERR   — exit code != 0, stderr indicates prompt/input issue
  UNKNOWN      — none of the above patterns match

Classification order matters: first match wins (Section 4.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Failure class constants (match headless_run_registry.FAILURE_CLASSES)
# ---------------------------------------------------------------------------

SUCCESS = "SUCCESS"
TIMEOUT = "TIMEOUT"
NO_OUTPUT = "NO_OUTPUT"
INTERRUPTED = "INTERRUPTED"
INFRA_FAIL = "INFRA_FAIL"
TOOL_FAIL = "TOOL_FAIL"
PROMPT_ERR = "PROMPT_ERR"
UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Pattern sets for stderr classification
# ---------------------------------------------------------------------------

INFRA_ERROR_PATTERNS = [
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"disk full|no space left on device", re.IGNORECASE),
    re.compile(r"out of memory|oom|cannot allocate memory", re.IGNORECASE),
    re.compile(r"errno \d+", re.IGNORECASE),
]

TOOL_ERROR_PATTERNS = [
    re.compile(r"api[_ ]?error|api[_ ]?failure", re.IGNORECASE),
    re.compile(r"rate[_ ]?limit|429|too many requests", re.IGNORECASE),
    re.compile(r"context[_ ]?(limit|length|window).*exceeded", re.IGNORECASE),
    re.compile(r"authentication|unauthorized|403|401", re.IGNORECASE),
    re.compile(r"connection[_ ]?(refused|reset|timed?[_ ]?out)", re.IGNORECASE),
    re.compile(r"service[_ ]?unavailable|503|502|500", re.IGNORECASE),
    re.compile(r"model[_ ]?(not[_ ]?found|unavailable)", re.IGNORECASE),
    re.compile(r"token[_ ]?limit|max[_ ]?tokens", re.IGNORECASE),
]

PROMPT_ERROR_PATTERNS = [
    re.compile(r"invalid[_ ]?(prompt|input|request)", re.IGNORECASE),
    re.compile(r"prompt[_ ]?(too[_ ]?long|empty|missing)", re.IGNORECASE),
    re.compile(r"malformed[_ ]?(prompt|input|json|yaml)", re.IGNORECASE),
    re.compile(r"schema[_ ]?validation[_ ]?(error|fail)", re.IGNORECASE),
    re.compile(r"parse[_ ]?error.*input", re.IGNORECASE),
]

# Signals that indicate interruption
INTERRUPT_SIGNALS = frozenset({1, 2, 15})  # SIGHUP, SIGINT, SIGTERM


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    """Structured classification evidence per Contract Section 4.3."""
    failure_class: str
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    stderr_tail: str = ""
    classification_reason: str = ""
    retryable: bool = False
    operator_hint: str = ""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_exit(
    *,
    exit_code: Optional[int],
    timed_out: bool = False,
    no_output_detected: bool = False,
    stderr: str = "",
    binary_not_found: bool = False,
) -> ClassificationResult:
    """Classify a headless run exit outcome per Contract Section 4.2.

    Decision tree (first match wins):
      1. exit_code == 0           -> SUCCESS
      2. timed_out                -> TIMEOUT
      3. no_output_detected       -> NO_OUTPUT
      4. signal-terminated        -> INTERRUPTED
      5. binary not found / infra -> INFRA_FAIL
      6. stderr matches tool/API  -> TOOL_FAIL
      7. stderr matches prompt    -> PROMPT_ERR
      8. else                     -> UNKNOWN

    Args:
        exit_code:          Subprocess exit code (None if not available).
        timed_out:          True if subprocess.TimeoutExpired was raised.
        no_output_detected: True if no output was produced within threshold.
        stderr:             Captured stderr content.
        binary_not_found:   True if the CLI binary was not found in PATH.
    """
    stderr_tail = stderr[-500:] if stderr else ""

    # 1. Success
    if exit_code == 0 and not timed_out:
        return ClassificationResult(
            failure_class=SUCCESS,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="exit code 0",
            retryable=False,
            operator_hint="",
        )

    # 2. Timeout
    if timed_out:
        return ClassificationResult(
            failure_class=TIMEOUT,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="subprocess exceeded timeout",
            retryable=True,
            operator_hint="Consider increasing VNX_HEADLESS_TIMEOUT or simplifying the prompt",
        )

    # 3. No output hang
    if no_output_detected:
        return ClassificationResult(
            failure_class=NO_OUTPUT,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="process produced no output within threshold",
            retryable=True,
            operator_hint="Inspect prompt, check upstream dependencies",
        )

    # 4. Signal-terminated (negative exit codes on Unix = killed by signal)
    signal_num = None
    if exit_code is not None and exit_code < 0:
        signal_num = abs(exit_code)
        if signal_num in INTERRUPT_SIGNALS:
            return ClassificationResult(
                failure_class=INTERRUPTED,
                exit_code=exit_code,
                signal=signal_num,
                stderr_tail=stderr_tail,
                classification_reason=f"process killed by signal {signal_num}",
                retryable=True,
                operator_hint="Check what sent the signal, retry",
            )

    # 5. Infrastructure failure
    if binary_not_found:
        return ClassificationResult(
            failure_class=INFRA_FAIL,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="CLI binary not found in PATH",
            retryable=True,
            operator_hint="Install or fix PATH for the CLI binary",
        )
    if _matches_any(stderr, INFRA_ERROR_PATTERNS):
        return ClassificationResult(
            failure_class=INFRA_FAIL,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="stderr matches infrastructure error pattern",
            retryable=True,
            operator_hint="Fix infrastructure issue, then retry",
        )

    # 6. Tool/API failure
    if _matches_any(stderr, TOOL_ERROR_PATTERNS):
        return ClassificationResult(
            failure_class=TOOL_FAIL,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="stderr matches tool/API error pattern",
            retryable=True,
            operator_hint="Check tool availability, retry with backoff",
        )

    # 7. Prompt error
    if _matches_any(stderr, PROMPT_ERROR_PATTERNS):
        return ClassificationResult(
            failure_class=PROMPT_ERR,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="stderr matches prompt/input error pattern",
            retryable=False,
            operator_hint="Fix prompt or dispatch bundle before retrying",
        )

    # 8. Unknown
    return ClassificationResult(
        failure_class=UNKNOWN,
        exit_code=exit_code,
        signal=signal_num,
        stderr_tail=stderr_tail,
        classification_reason=f"exit code {exit_code}, no matching pattern",
        retryable=False,
        operator_hint="Inspect logs, classify manually",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches_any(text: str, patterns: list) -> bool:
    """Return True if text matches any pattern in the list."""
    if not text:
        return False
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False
