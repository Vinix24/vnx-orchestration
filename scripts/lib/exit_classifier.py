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
  INTERRUPTED  — terminated by a graceful/user signal (SIGINT, SIGTERM, SIGHUP).
                 SIGKILL(-9) is NOT interrupted — it falls through (usually OOM/infra).
  PROMPT_ERR   — prompt rejected by the CLI (not retryable)
  UNKNOWN      — unrecognised exit condition

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

# Retryable by default per failure class (HEADLESS_RUN_CONTRACT.md §4.1).
# INTERRUPTED is retryable (a SIGINT/SIGTERM is a transient/manual stop); UNKNOWN
# is NOT retryable (do not blindly re-run an unclassified failure).
_RETRYABLE = {
    FC_SUCCESS: False,
    FC_TIMEOUT: True,
    FC_TOOL_FAIL: True,
    FC_INFRA_FAIL: True,
    FC_NO_OUTPUT: True,
    FC_INTERRUPTED: True,
    FC_PROMPT_ERR: False,
    FC_UNKNOWN: False,
}

# Stderr patterns → (failure_class, reason, retryable)
# Checked in order; first match wins.
# Signal numbers classified as INTERRUPTED — graceful/user terminations only.
# SIGKILL (9) and other signals are NOT here: they fall through to stderr/UNKNOWN
# (a SIGKILL is usually an OOM-killer or hard infra kill, not a user interrupt).
# Contract: HEADLESS_RUN_CONTRACT.md §4.1.
_INTERRUPT_SIGNALS: frozenset[int] = frozenset({1, 2, 15})  # SIGHUP, SIGINT, SIGTERM

# stderr → failure-class patterns, evaluated first-match-wins. ORDER MATTERS:
# INFRA before TOOL before PROMPT, so a line matching both ("Permission denied,
# API error" → INFRA) resolves to the more-fundamental class (§4.2).
_STDERR_PATTERNS: list[tuple[re.Pattern, str, str, bool]] = [
    # --- INFRA_FAIL: host / binary / resource (infra beats tool) ---
    (
        re.compile(r"command not found", re.IGNORECASE),
        FC_INFRA_FAIL, "CLI binary missing / command not found", True,
    ),
    (
        re.compile(r"permission denied|EACCES", re.IGNORECASE),
        FC_INFRA_FAIL, "Permission denied", True,
    ),
    (
        re.compile(r"no space left|disk full|ENOSPC", re.IGNORECASE),
        FC_INFRA_FAIL, "Disk full / no space left on device", True,
    ),
    (
        re.compile(r"out.of.memory|\bOOM\b|cannot.allocate|\bkilled\b", re.IGNORECASE),
        FC_INFRA_FAIL, "Out-of-memory or resource exhaustion", True,
    ),
    # --- TOOL_FAIL: transient API / network ---
    (
        re.compile(r"rate.limit|\b429\b|too many requests", re.IGNORECASE),
        FC_TOOL_FAIL, "API rate limit or HTTP 429", True,
    ),
    (
        re.compile(r"api error|model overloaded|\b50[0-9]\b|service unavailable", re.IGNORECASE),
        FC_TOOL_FAIL, "API/service error (5xx / overloaded)", True,
    ),
    (
        # Auth is a tool/API error but NON-retryable: a blind retry won't fix a
        # 401/403 without credential rotation and just burns tokens/API calls.
        re.compile(r"\b401\b|\b403\b|unauthorized|forbidden", re.IGNORECASE),
        FC_TOOL_FAIL, "Auth error (401/403) — rotate credentials (non-retryable)", False,
    ),
    (
        re.compile(r"connection.refused|connection.reset|connection.error|network.error|ECONNREFUSED", re.IGNORECASE),
        FC_TOOL_FAIL, "Network/connection error", True,
    ),
    (
        # A timeout keyword in stderr WITHOUT the timed_out flag (the flag is
        # caught earlier as TIMEOUT); keep it retryable to preserve auto-recovery.
        re.compile(r"\btimeout\b|timed.out|deadline exceeded", re.IGNORECASE),
        FC_TOOL_FAIL, "Timeout/deadline keyword in stderr", True,
    ),
    # --- PROMPT_ERR: non-retryable input problem ---
    (
        # Context/token-limit is an input-size problem, not a transient tool error:
        # re-running the same oversized prompt fails again → PROMPT_ERR (fix the prompt).
        re.compile(r"context.length|context.window|token.limit", re.IGNORECASE),
        FC_PROMPT_ERR, "Context/token limit exceeded — reduce prompt size", False,
    ),
    (
        re.compile(r"invalid.prompt|bad.prompt|prompt.error|malformed.prompt|prompt too long", re.IGNORECASE),
        FC_PROMPT_ERR, "Invalid / malformed / oversized prompt rejected by CLI", False,
    ),
    (
        re.compile(r"malformed json|invalid json", re.IGNORECASE),
        FC_PROMPT_ERR, "Malformed JSON input", False,
    ),
    (
        re.compile(r"schema validation|schema error", re.IGNORECASE),
        FC_PROMPT_ERR, "Schema validation error on input", False,
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

    Priority order (first match wins) — HEADLESS_RUN_CONTRACT.md §4.2:
      1. exit_code == 0 → SUCCESS (a clean exit wins over any flag/stderr)
      2. timed_out → TIMEOUT (beats signals, binary_not_found and stderr)
      3. no_output_detected → NO_OUTPUT
      4. exit_code < 0 with signal in _INTERRUPT_SIGNALS → INTERRUPTED
         (SIGKILL/-9 and other signals fall through — usually OOM/infra, not a user interrupt)
      5. binary_not_found → INFRA_FAIL
      6. stderr pattern match → class from pattern table (INFRA > TOOL > PROMPT)
      7. fallback → UNKNOWN
    """
    stderr_tail = (stderr or "")[-500:].strip()
    sig = abs(exit_code) if (exit_code is not None and exit_code < 0) else None

    if exit_code == 0:
        return ClassificationResult(
            failure_class=FC_SUCCESS,
            retryable=_RETRYABLE[FC_SUCCESS],
            exit_code=0,
            stderr_tail=stderr_tail,
            classification_reason="Subprocess exited with code 0",
            operator_hint=_OPERATOR_HINTS[FC_SUCCESS],
        )

    if timed_out:
        return ClassificationResult(
            failure_class=FC_TIMEOUT,
            retryable=_RETRYABLE[FC_TIMEOUT],
            signal=sig,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="Subprocess exceeded its configured timeout",
            operator_hint=_OPERATOR_HINTS[FC_TIMEOUT],
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

    if sig is not None and sig in _INTERRUPT_SIGNALS:
        return ClassificationResult(
            failure_class=FC_INTERRUPTED,
            retryable=_RETRYABLE[FC_INTERRUPTED],
            signal=sig,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason=f"Subprocess terminated by signal {sig}",
            operator_hint=_OPERATOR_HINTS[FC_INTERRUPTED],
        )

    if binary_not_found:
        return ClassificationResult(
            failure_class=FC_INFRA_FAIL,
            retryable=_RETRYABLE[FC_INFRA_FAIL],
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            classification_reason="CLI binary not found in PATH",
            operator_hint=_OPERATOR_HINTS[FC_INFRA_FAIL],
        )

    # Stderr pattern matching (INFRA > TOOL > PROMPT, first-match-wins).
    if stderr:
        for pattern, fc, reason, retryable in _STDERR_PATTERNS:
            if pattern.search(stderr):
                return ClassificationResult(
                    failure_class=fc,
                    retryable=retryable,
                    signal=sig,
                    exit_code=exit_code,
                    stderr_tail=stderr_tail,
                    classification_reason=reason,
                    operator_hint=_OPERATOR_HINTS[fc],
                )

    return ClassificationResult(
        failure_class=FC_UNKNOWN,
        retryable=_RETRYABLE[FC_UNKNOWN],
        signal=sig,
        exit_code=exit_code,
        stderr_tail=stderr_tail,
        classification_reason=f"Unrecognised failure: exit_code={exit_code}",
        operator_hint=_OPERATOR_HINTS[FC_UNKNOWN],
    )
