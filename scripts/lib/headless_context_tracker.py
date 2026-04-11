#!/usr/bin/env python3
"""HeadlessContextTracker — token-based context rotation trigger for headless dispatches.

Consumes task_progress events from the Claude CLI stream-json output and signals
when the model context has crossed the rotation threshold.

BILLING SAFETY: No Anthropic SDK. CLI-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HeadlessContextTracker:
    """Track cumulative token usage and signal when rotation is needed.

    Attributes:
        model_context_limit: Total token capacity of the model (default: 200K for Sonnet).
        rotation_threshold_pct: Percentage of context used before rotation is triggered.
    """

    model_context_limit: int = 200_000  # Sonnet ~200K tokens
    rotation_threshold_pct: float = 65.0
    _total_tokens: int = field(default=0, init=False, repr=False)

    def update(self, event_payload: dict) -> None:
        """Extract total_tokens from a task_progress event payload.

        Handles both top-level task_progress and system/task_progress subtypes.
        The ``usage.total_tokens`` field is the running cumulative count emitted
        by the CLI; we always take the latest value (not accumulate).
        """
        event_type = event_payload.get("type", "")
        event_subtype = event_payload.get("subtype", "")

        is_task_progress = (
            event_type == "task_progress"
            or (event_type == "system" and event_subtype == "task_progress")
        )
        if not is_task_progress:
            return

        usage = event_payload.get("usage", {})
        if not isinstance(usage, dict):
            return

        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            self._total_tokens = total

    @property
    def context_used_pct(self) -> float:
        """Percentage of model context consumed so far."""
        return (self._total_tokens / self.model_context_limit) * 100

    @property
    def should_rotate(self) -> bool:
        """True when context usage has reached or exceeded the rotation threshold."""
        return self.context_used_pct >= self.rotation_threshold_pct

    def snapshot(self) -> dict:
        """Return a serialisable summary of the current tracking state."""
        return {
            "total_tokens": self._total_tokens,
            "context_used_pct": round(self.context_used_pct, 1),
            "remaining_pct": round(100 - self.context_used_pct, 1),
            "model_context_limit": self.model_context_limit,
            "threshold_pct": self.rotation_threshold_pct,
        }
