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

    def update(self, event_payload) -> None:
        """Extract total_tokens from a task_progress event.

        Accepts either:

          * a plain ``dict`` matching the raw stream-json payload (what the
            unit tests pass), or
          * a ``StreamEvent``-like object from ``SubprocessAdapter`` exposing
            ``.type`` / ``.data`` attributes — production callers in
            ``deliver_via_subprocess`` pass these directly.  The catch-all
            normalisation in ``SubprocessAdapter._normalize_cli_event``
            preserves the raw payload (including ``usage``) inside
            ``event.data`` for unrecognised event types such as
            ``task_progress``.

        ``usage.total_tokens`` is the running cumulative count emitted by the
        CLI; we always take the latest non-zero value (not accumulate).
        Malformed inputs are silently ignored — never raises.
        """
        # Normalise input to a flat dict the rest of this method can read.
        if isinstance(event_payload, dict):
            payload: dict = event_payload
        elif event_payload is None:
            return
        else:
            event_type_attr = getattr(event_payload, "type", None)
            event_data_attr = getattr(event_payload, "data", None)
            if not isinstance(event_data_attr, dict):
                return
            # Build a flat view: data first, then layer on the event type so
            # the normalised StreamEvent's outer ``.type`` survives even when
            # the inner data dict has its own ``type`` field (the catch-all
            # normalisation copies the raw payload, so both usually agree).
            payload = dict(event_data_attr)
            if event_type_attr and "type" not in payload:
                payload["type"] = event_type_attr

        event_type = payload.get("type", "")
        event_subtype = payload.get("subtype", "")

        is_task_progress = (
            event_type == "task_progress"
            or (event_type == "system" and event_subtype == "task_progress")
        )
        if not is_task_progress:
            return

        usage = payload.get("usage", {})
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
