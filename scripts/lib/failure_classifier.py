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


def is_retryable(failure_class: str) -> bool:
    """Return True if the failure class is retryable."""
    return failure_class in _RETRYABLE
