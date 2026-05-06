#!/usr/bin/env python3
"""CanonicalEvent — unified event schema for all provider adapters.

Every adapter (Claude/Codex/Gemini/LiteLLM/Ollama) must emit CanonicalEvent
instances. Legacy dict events are accepted via from_legacy() for backwards compat.

BILLING SAFETY: No Anthropic SDK imports. No external network calls.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

VALID_PROVIDERS = frozenset({"claude", "codex", "gemini", "litellm", "ollama"})
VALID_EVENT_TYPES = frozenset({"init", "text", "tool_use", "tool_result", "thinking", "complete", "error"})
VALID_TIERS = frozenset({1, 2, 3})

# Legacy "result" emitted by subprocess_adapter maps to canonical "complete"
_LEGACY_TYPE_MAP: Dict[str, str] = {"result": "complete"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class CanonicalEvent:
    """Unified schema for all agent stream events across providers."""

    dispatch_id: str
    terminal_id: str
    provider: str
    event_type: str
    data: Dict[str, Any]
    observability_tier: int = 2
    timestamp: str = field(default_factory=_now_iso)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider_meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid provider {self.provider!r}; must be one of {sorted(VALID_PROVIDERS)}"
            )
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Invalid event_type {self.event_type!r}; must be one of {sorted(VALID_EVENT_TYPES)}"
            )
        if self.observability_tier not in VALID_TIERS:
            raise ValueError(
                f"Invalid observability_tier {self.observability_tier!r}; must be 1, 2, or 3"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "dispatch_id": self.dispatch_id,
            "terminal_id": self.terminal_id,
            "provider": self.provider,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "data": self.data,
            "observability_tier": self.observability_tier,
            "provider_meta": self.provider_meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CanonicalEvent":
        """Reconstruct from a previously serialized to_dict() result."""
        return cls(
            dispatch_id=d.get("dispatch_id", ""),
            terminal_id=d.get("terminal_id", ""),
            provider=d.get("provider", "claude"),
            event_type=d.get("event_type", "text"),
            data=d.get("data", {}),
            observability_tier=int(d.get("observability_tier", 2)),
            timestamp=d.get("timestamp", _now_iso()),
            event_id=d.get("event_id", str(uuid.uuid4())),
            provider_meta=d.get("provider_meta", {}),
        )

    @classmethod
    def from_legacy(
        cls,
        provider: str,
        event: Dict[str, Any],
        dispatch_id: str = "",
        terminal_id: str = "",
        observability_tier: int = 2,
    ) -> "CanonicalEvent":
        """Construct from a legacy dict event emitted by existing adapters.

        Maps "result" -> "complete". Unknown types land in provider_meta as
        legacy_type and are coerced to "error" to preserve the stream.
        """
        raw_type = event.get("type", "text")
        event_type = _LEGACY_TYPE_MAP.get(raw_type, raw_type)

        provider_meta: Dict[str, Any] = {}
        if event_type not in VALID_EVENT_TYPES:
            provider_meta["legacy_type"] = raw_type
            event_type = "error"

        data = event.get("data", {})
        if not isinstance(data, dict):
            data = {"value": data}

        return cls(
            dispatch_id=dispatch_id or event.get("dispatch_id", ""),
            terminal_id=terminal_id or event.get("terminal", ""),
            provider=provider,
            event_type=event_type,
            timestamp=event.get("timestamp", _now_iso()),
            data=data,
            observability_tier=observability_tier,
            provider_meta=provider_meta,
        )
