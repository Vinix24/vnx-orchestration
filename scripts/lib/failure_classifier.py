#!/usr/bin/env python3
"""
VNX Failure Classifier — Structured failure classification for delivery failures.

Classifies delivery failure reasons into actionable categories so T0 can
deterministically decide whether to retry, reroute, or escalate.

Classification taxonomy:
  - invalid_skill:           Skill not found or misconfigured (non-retryable)
  - stale_lease:             Lease generation mismatch / expired (retryable)
  - runtime_state_divergence: LeaseManager vs broker state disagreement (non-retryable)
  - worker_handoff_failure:  Worker-side rejection during execution handoff (retryable)
  - hook_feedback_interruption: Hook or feedback-loop failure after terminal reset (retryable)
  - tmux_transport_failure:  tmux send-keys / paste-buffer / Enter failure (retryable)

Design:
  - Classification is derived from the reason string passed to release_on_delivery_failure.
  - Each classification carries a retryable flag and operator-readable summary.
  - Unknown reasons default to tmux_transport_failure (retryable) for safety.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Failure class constants
# ---------------------------------------------------------------------------

INVALID_SKILL = "invalid_skill"
STALE_LEASE = "stale_lease"
RUNTIME_STATE_DIVERGENCE = "runtime_state_divergence"
WORKER_HANDOFF_FAILURE = "worker_handoff_failure"
HOOK_FEEDBACK_INTERRUPTION = "hook_feedback_interruption"
TMUX_TRANSPORT_FAILURE = "tmux_transport_failure"

# Retryable classifications — T0 may re-dispatch to the same or different terminal.
_RETRYABLE = frozenset({
    STALE_LEASE,
    WORKER_HANDOFF_FAILURE,
    HOOK_FEEDBACK_INTERRUPTION,
    TMUX_TRANSPORT_FAILURE,
})

# Non-retryable classifications — require operator intervention or config fix.
_NON_RETRYABLE = frozenset({
    INVALID_SKILL,
    RUNTIME_STATE_DIVERGENCE,
})


# ---------------------------------------------------------------------------
# Canonical failure code registry (DFL-LOG-4, Contract 160 Section 2)
# Maps every delivery failure code to (failure_class, retryable, retry_decision, operator_summary)
# ---------------------------------------------------------------------------

FAILURE_CODE_REGISTRY: dict[str, tuple[str, bool, str, str]] = {
    # Phase 0: Pre-delivery (no lease held)
    "pre_executor_resolution": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "No terminal available for the target track. Retry when a terminal is free."),
    "pre_mode_configuration": (HOOK_FEEDBACK_INTERRUPTION, True, "auto_retry", "Terminal mode configuration failed (clear/switch/modal). Terminal may need operator reset."),
    "pre_skill_empty": (INVALID_SKILL, False, "manual_fix", "Dispatch role has no skill mapping. Fix the Role field in the dispatch."),
    "pre_skill_registry": (INVALID_SKILL, False, "manual_fix", "Skill not found in the skills registry. Fix the Role or Skill field."),
    "pre_instruction_empty": (INVALID_SKILL, False, "manual_fix", "Dispatch contains no instruction content. Rework the dispatch body."),
    "pre_terminal_resolution": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Terminal ID resolution failed. Transient — retry on next cycle."),
    "pre_canonical_lease_busy": (STALE_LEASE, True, "defer", "Terminal is occupied by another dispatch. Deferred until lease is released."),
    "pre_canonical_lease_expired": (STALE_LEASE, True, "auto_retry", "Lease expired or recovering. Retry after reconciler runs."),
    "pre_canonical_check_error": (STALE_LEASE, True, "auto_retry", "Lease check failed (transient I/O). Safe to retry."),
    "pre_canonical_acquire_failed": (STALE_LEASE, True, "auto_retry", "Lease acquisition contention. Safe to retry."),
    "pre_legacy_lock_busy": (STALE_LEASE, True, "defer", "Terminal lock held by prior dispatch. Deferred until lock clears."),
    "pre_claim_failed": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Terminal claim acquisition failed. Transient — retry."),
    "pre_duplicate_delivery": (STALE_LEASE, True, "defer", "Duplicate delivery prevented — prior attempt still holds lease."),
    "pre_validation_empty_role": (INVALID_SKILL, False, "manual_fix", "Dispatch has no role. Set a valid Role field."),
    "pre_validation_command_failed": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Intelligence validation command failed. Runtime dependency — retry when resolved."),
    "pre_gather_command_failed": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Intelligence gathering command failed. Runtime dependency — retry when resolved."),
    # Phase 1: Post-lease, pre-transport
    "post_input_mode_blocked": (HOOK_FEEDBACK_INTERRUPTION, True, "auto_retry", "Terminal pane is in non-interactive mode (copy/search). Recovery failed — retry after operator resets pane."),
    "post_process_exit": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Dispatcher process exited during delivery setup. Lease released by cleanup trap. Safe to retry."),
    # Phase 2: Transport (tmux active)
    "tx_send_skill": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to type skill command into terminal. Transient tmux issue — retry."),
    "tx_load_buffer": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to load instruction into tmux buffer. Transient — retry."),
    "tx_paste_buffer": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to paste instruction into terminal. Transient — retry."),
    "tx_send_enter": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to submit dispatch with Enter. Transient — retry."),
    "tx_load_buffer_codex": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to load combined content into tmux buffer. Transient — retry."),
    "tx_paste_buffer_codex": (TMUX_TRANSPORT_FAILURE, True, "auto_retry", "Failed to paste combined content into terminal. Transient — retry."),
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureClassification:
    """Structured classification of a delivery failure."""

    failure_class: str
    """One of the failure class constants above."""

    retryable: bool
    """True if T0 may retry dispatch without operator intervention."""

    operator_summary: str
    """One-sentence explanation for operator/T0 consumption."""

    reason: str
    """Original reason string that was classified."""

    def to_dict(self) -> dict:
        return {
            "failure_class": self.failure_class,
            "retryable": self.retryable,
            "operator_summary": self.operator_summary,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Classification rules (ordered by specificity)
# ---------------------------------------------------------------------------

_CLASSIFICATION_RULES: list[tuple[list[str], str, str]] = [
    # (keywords, failure_class, operator_summary_template)
    (
        ["skill_invalid", "skill_not_found", "invalid_skill", "skill not found",
         "not found in skills", "not found in registry"],
        INVALID_SKILL,
        "Skill configuration error — skill not found or invalid. "
        "Fix the skill reference in the dispatch before retrying.",
    ),
    (
        ["stale_lease", "stale lease", "generation mismatch", "generation guard",
         "lease_expired", "lease expired"],
        STALE_LEASE,
        "Lease was stale or expired at delivery time. "
        "Safe to retry — a fresh lease will be acquired.",
    ),
    (
        ["runtime_state_divergence", "state divergence", "zombie_lease",
         "ghost_dispatch", "reconciliation_failed", "mismatch"],
        RUNTIME_STATE_DIVERGENCE,
        "Runtime state is inconsistent between LeaseManager and broker. "
        "Run reconciliation before retrying.",
    ),
    (
        ["rejected_execution_handoff", "worker_rejected", "handoff_failure",
         "handoff failure", "worker rejected"],
        WORKER_HANDOFF_FAILURE,
        "Worker rejected the dispatch during execution handoff. "
        "Safe to retry on the same or a different terminal.",
    ),
    (
        ["prompt_loop_interrupted", "hook_interrupted", "feedback_loop",
         "clear_context", "hook failure", "context reset",
         "hook_feedback_interruption"],
        HOOK_FEEDBACK_INTERRUPTION,
        "Hook or feedback loop was interrupted after terminal reset. "
        "Safe to retry — terminal should be re-initialized.",
    ),
    (
        ["tmux", "paste-buffer", "send-keys", "load-buffer", "Enter failed",
         "transport", "tmux delivery"],
        TMUX_TRANSPORT_FAILURE,
        "tmux transport failed during dispatch delivery. "
        "Safe to retry — transient terminal communication issue.",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_failure(reason: str) -> FailureClassification:
    """Classify a delivery failure reason string into a structured classification.

    Matches against known keyword patterns in priority order.
    Unknown reasons default to tmux_transport_failure (retryable).

    Args:
        reason: The failure reason string from release_on_delivery_failure.

    Returns:
        FailureClassification with failure_class, retryable flag, and operator_summary.
    """
    reason_lower = reason.lower()

    for keywords, failure_class, summary in _CLASSIFICATION_RULES:
        for keyword in keywords:
            if keyword.lower() in reason_lower:
                return FailureClassification(
                    failure_class=failure_class,
                    retryable=failure_class in _RETRYABLE,
                    operator_summary=summary,
                    reason=reason,
                )

    # Default: treat unknown as tmux transport failure (retryable, safe default)
    return FailureClassification(
        failure_class=TMUX_TRANSPORT_FAILURE,
        retryable=True,
        operator_summary=(
            "Delivery failed for an unclassified reason. "
            "Treated as retryable tmux transport failure by default."
        ),
        reason=reason,
    )


def classify_failure_code(code: str) -> Optional[FailureClassification]:
    """Classify a canonical failure code via direct lookup (DFL-LOG-4).

    Returns None if the code is not in the registry (caller should fall back
    to keyword-based classify_failure()).
    """
    entry = FAILURE_CODE_REGISTRY.get(code)
    if entry is None:
        return None
    failure_class, retryable, retry_decision, operator_summary = entry
    return FailureClassification(
        failure_class=failure_class,
        retryable=retryable,
        operator_summary=operator_summary,
        reason=code,
    )


def classify_failure_with_code(reason: str) -> FailureClassification:
    """Classify a failure reason, trying direct code lookup first (DFL-LOG-4).

    If the reason matches a delivery_failed:{code} pattern, extracts the code
    and looks it up directly. Falls back to keyword-based classification.
    """
    if reason.startswith("delivery_failed:"):
        code = reason[len("delivery_failed:"):]
        result = classify_failure_code(code)
        if result is not None:
            return result
    result = classify_failure_code(reason)
    if result is not None:
        return result
    return classify_failure(reason)


def get_retry_decision(code: str) -> str:
    """Return the retry decision for a failure code: auto_retry, defer, or manual_fix."""
    entry = FAILURE_CODE_REGISTRY.get(code)
    if entry is None:
        return "auto_retry"
    return entry[2]


def is_retryable(failure_class: str) -> bool:
    """Return True if the failure class is retryable."""
    return failure_class in _RETRYABLE
