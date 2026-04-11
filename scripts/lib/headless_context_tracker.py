#!/usr/bin/env python3
"""headless_context_tracker.py — Token tracking and rotation trigger for headless subprocess agents.

Tracks context window usage via task_progress events emitted by the Claude CLI.
Triggers rotation when usage exceeds the configured threshold percentage.

BILLING SAFETY: No Anthropic SDK. No api.anthropic.com calls. CLI-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HeadlessContextTracker:
    """Accumulates token usage from task_progress events and signals when rotation is needed.

    Usage:
        tracker = HeadlessContextTracker()
        tracker.update(payload)  # call for each parsed CLI event payload
        if tracker.should_rotate:
            # write handover and stop subprocess
    """

    model_context_limit: int = 200_000  # sonnet ~200K tokens
    rotation_threshold_pct: float = 65.0
    _total_tokens: int = field(default=0, init=False, repr=False)

    def update(self, event_payload: dict) -> None:
        """Extract total_tokens from task_progress events.

        Handles both top-level system/task_progress events and nested usage
        dicts.  No-ops on all other event shapes.
        """
        event_type = event_payload.get("type", "")
        event_subtype = event_payload.get("subtype", "")

        is_task_progress = (
            (event_type == "system" and event_subtype == "task_progress")
            or event_type == "task_progress"
        )
        if not is_task_progress:
            return

        usage = event_payload.get("usage", {})
        if not isinstance(usage, dict):
            return

        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            # Always take the latest reported total (not cumulative sum)
            self._total_tokens = total

    @property
    def context_used_pct(self) -> float:
        """Percentage of model context window consumed."""
        return (self._total_tokens / self.model_context_limit) * 100

    @property
    def should_rotate(self) -> bool:
        """True when context usage has reached or exceeded the rotation threshold."""
        return self.context_used_pct >= self.rotation_threshold_pct

    def snapshot(self) -> dict:
        """Return a serialisable snapshot of current tracker state."""
        return {
            "total_tokens": self._total_tokens,
            "context_used_pct": round(self.context_used_pct, 1),
            "remaining_pct": round(100 - self.context_used_pct, 1),
            "model_context_limit": self.model_context_limit,
            "threshold_pct": self.rotation_threshold_pct,
        }
