#!/usr/bin/env python3
"""CanonicalEvent — unified event schema for all provider adapters.

Every adapter (Claude/Codex/Gemini/LiteLLM/Ollama) must emit CanonicalEvent
instances. Legacy dict events are accepted via from_legacy() for backwards compat.
Per-provider mapper functions live here; from_provider_event() is the single
enforcement point for the unified shape (Wave 4.6 PR-4.6.6).

BILLING SAFETY: No Anthropic SDK imports. No external network calls.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

VALID_PROVIDERS = frozenset({"claude", "codex", "gemini", "kimi", "litellm", "ollama"})
VALID_EVENT_TYPES = frozenset({"init", "text", "tool_use", "tool_result", "thinking", "complete", "error"})
VALID_TIERS = frozenset({1, 2, 3})

# Legacy "result" emitted by subprocess_adapter maps to canonical "complete"
_LEGACY_TYPE_MAP: Dict[str, str] = {"result": "complete"}


class EventShapeError(Exception):
    """Raised when an event does not conform to the CanonicalEvent schema."""


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
    # Unified shape fields added in Wave 4.6 PR-4.6.6
    sub_provider: Optional[str] = None
    sequence: int = 0
    schema_version: int = 1
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    tokens_cache_read: Optional[int] = None
    tokens_cache_write: Optional[int] = None
    model: Optional[str] = None

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

    def validate_shape(self) -> None:
        """Validate canonical shape beyond __post_init__ checks.

        Raises EventShapeError when:
        - schema_version is not 1
        - token counts are set but negative
        """
        if self.schema_version != 1:
            raise EventShapeError(
                f"Unsupported schema_version {self.schema_version!r}; expected 1"
            )
        for fname, fval in [
            ("tokens_input", self.tokens_input),
            ("tokens_output", self.tokens_output),
            ("tokens_cache_read", self.tokens_cache_read),
            ("tokens_cache_write", self.tokens_cache_write),
        ]:
            if fval is not None and (not isinstance(fval, int) or fval < 0):
                raise EventShapeError(
                    f"{fname} must be a non-negative int or None; got {fval!r}"
                )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "event_id": self.event_id,
            "dispatch_id": self.dispatch_id,
            "terminal_id": self.terminal_id,
            "provider": self.provider,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "data": self.data,
            "observability_tier": self.observability_tier,
            "provider_meta": self.provider_meta,
            "schema_version": self.schema_version,
        }
        if self.sub_provider is not None:
            d["sub_provider"] = self.sub_provider
        if self.sequence != 0:
            d["sequence"] = self.sequence
        if self.tokens_input is not None:
            d["tokens_input"] = self.tokens_input
        if self.tokens_output is not None:
            d["tokens_output"] = self.tokens_output
        if self.tokens_cache_read is not None:
            d["tokens_cache_read"] = self.tokens_cache_read
        if self.tokens_cache_write is not None:
            d["tokens_cache_write"] = self.tokens_cache_write
        if self.model is not None:
            d["model"] = self.model
        return d

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
            sub_provider=d.get("sub_provider"),
            sequence=int(d.get("sequence", 0)),
            schema_version=int(d.get("schema_version", 1)),
            tokens_input=d.get("tokens_input"),
            tokens_output=d.get("tokens_output"),
            tokens_cache_read=d.get("tokens_cache_read"),
            tokens_cache_write=d.get("tokens_cache_write"),
            model=d.get("model"),
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

    @classmethod
    def from_provider_event(
        cls,
        provider: str,
        raw: Dict[str, Any],
        dispatch_id: str = "",
        terminal_id: str = "",
        sub_provider: Optional[str] = None,
    ) -> "CanonicalEvent":
        """Map a raw provider event dict to a CanonicalEvent.

        Dispatches to the per-provider mapper function. Raises ValueError for
        unknown providers. Callers can wrap with EventStore.append() for full
        schema enforcement.
        """
        if provider not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid provider {provider!r}; must be one of {sorted(VALID_PROVIDERS)}"
            )
        if provider == "claude":
            return _from_claude_event(raw, dispatch_id, terminal_id)
        if provider == "codex":
            return _from_codex_event(raw, dispatch_id, terminal_id)
        if provider == "gemini":
            return _from_gemini_event(raw, dispatch_id, terminal_id)
        if provider == "kimi":
            return _from_kimi_event(raw, dispatch_id, terminal_id)
        if provider == "litellm":
            return _from_litellm_event(raw, dispatch_id, terminal_id, sub_provider=sub_provider)
        # ollama fallback
        return _from_ollama_event(raw, dispatch_id, terminal_id)


# ---------------------------------------------------------------------------
# Per-provider mapper functions — single source of truth for raw→canonical
# ---------------------------------------------------------------------------

def _from_claude_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map a raw Claude subprocess event dict to a CanonicalEvent."""
    raw_type = (raw.get("type") or "text")
    event_type = _LEGACY_TYPE_MAP.get(raw_type, raw_type)
    provider_meta: Dict[str, Any] = {}
    if event_type not in VALID_EVENT_TYPES:
        provider_meta["legacy_type"] = raw_type
        event_type = "error"
    data = raw.get("data", {})
    if not isinstance(data, dict):
        data = {"value": data}
    return CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider="claude",
        event_type=event_type,
        data=data,
        model=raw.get("model"),
        provider_meta=provider_meta,
    )


def _from_codex_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map a raw Codex NDJSON event dict to a CanonicalEvent (Tier-1)."""
    def make(et: str, d: Dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            provider="codex", event_type=et, data=d, observability_tier=1,
        )

    top_etype = (raw.get("type") or "")
    payload: Dict[str, Any] = raw
    event_msg = raw.get("event_msg")
    if isinstance(event_msg, dict):
        inner = event_msg.get("payload")
        payload = inner if isinstance(inner, dict) else event_msg

    etype = (payload.get("type") or "") if isinstance(payload, dict) else ""
    if top_etype and (
        top_etype.startswith("item.")
        or top_etype.startswith("thread.")
        or top_etype.startswith("turn.")
    ):
        etype = top_etype
        payload = raw

    item: Dict[str, Any] = {}
    raw_item = raw.get("item") or (payload.get("item") if payload is not raw else None)
    if isinstance(raw_item, dict):
        item = raw_item
    item_type = item.get("type", "")

    if etype in ("thread.started", "session_start"):
        return make("init", {"raw_type": etype})
    if etype == "agent_message":
        text = payload.get("text") or payload.get("content") or payload.get("message") or ""
        return make("text", {"text": str(text)})
    if etype == "item.completed" and item_type == "agent_message":
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return make("text", {"text": str(content)})
    if etype in ("item.started", "item.updated") and item_type == "command_execution":
        cmd = item.get("command") or item.get("cmd") or item.get("args") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(a) for a in cmd)
        return make("tool_use", {"command": str(cmd), "raw_type": etype})
    if etype == "item.completed" and item_type == "command_execution":
        output = item.get("output") or item.get("result") or ""
        return make("tool_result", {"output": str(output), "exit_code": item.get("exit_code", 0)})
    if etype == "turn.completed":
        return make("complete", {})
    if etype in ("result", "message"):
        content = payload.get("content") or payload.get("text") or payload.get("output") or ""
        return make("complete", {"text": str(content)} if content else {})
    if etype == "error":
        msg = payload.get("message") or payload.get("error") or payload.get("text") or ""
        return make("error", {"message": str(msg) if msg else str(payload)[:200]})
    return make("error", {
        "reason": f"unrecognized codex event: {etype!r}",
        "raw": str(raw)[:300],
    })


def _from_gemini_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map a raw Gemini stream-json event dict to a CanonicalEvent (Tier-1)."""
    def make(et: str, d: Dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            provider="gemini", event_type=et, data=d, observability_tier=1,
        )

    etype = (raw.get("type") or "")
    if etype in ("session_start", "init"):
        return make("init", {"raw_type": etype})
    if etype in ("message", "text", "content"):
        return make("text", {"text": str(raw.get("text") or raw.get("content") or "")})
    if etype in ("tool_use", "tool_call", "function_call"):
        args = raw.get("args") or raw.get("input") or raw.get("arguments") or {}
        if not isinstance(args, dict):
            args = {"raw": str(args)}
        return make("tool_use", {"name": str(raw.get("name") or ""), "args": args})
    if etype in ("tool_result", "tool_response", "function_response"):
        output = raw.get("output") or raw.get("result") or raw.get("content") or ""
        return make("tool_result", {"output": str(output)})
    if etype in ("result", "done", "complete", "finish"):
        data: Dict[str, Any] = {}
        text = raw.get("text") or raw.get("content") or ""
        if text:
            data["text"] = str(text)
        return make("complete", data)
    if etype == "error":
        msg = raw.get("message") or raw.get("error") or raw.get("text") or ""
        return make("error", {"message": str(msg) if msg else str(raw)[:200]})
    return make("error", {
        "reason": f"unrecognized gemini event: {etype!r}",
        "raw": str(raw)[:300],
    })


def _from_litellm_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
    sub_provider: Optional[str] = None,
) -> CanonicalEvent:
    """Map an OpenAI-shaped LiteLLM NDJSON chunk to a CanonicalEvent (Tier-1)."""
    def make(et: str, d: Dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            provider="litellm", event_type=et, data=d,
            observability_tier=1, sub_provider=sub_provider,
        )

    error_type = raw.get("error_type")
    if error_type:
        return make("error", {"error_type": error_type, "message": str(raw.get("message") or "")})

    choices = raw.get("choices") or []
    choice = choices[0] if choices else {}
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    if delta.get("tool_calls"):
        return make("tool_use", {"tool_calls": delta["tool_calls"]})
    if finish_reason in ("stop", "tool_calls", "end_turn", "length"):
        return make("complete", {"finish_reason": finish_reason, "model": str(raw.get("model") or "")})
    if delta.get("role") == "assistant" and not delta.get("content"):
        return make("init", {"model": str(raw.get("model") or "")})
    return make("text", {"content": delta.get("content") or ""})


def _from_kimi_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map a raw Kimi CLI stream-json event dict to a CanonicalEvent (Tier-1)."""
    def make(et: str, d: Dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            provider="kimi", event_type=et, data=d, observability_tier=1,
        )

    event_type = (raw.get("event_type") or raw.get("type") or "")

    if event_type in ("assistant_text", "text"):
        return make("text", {"text": str(raw.get("content", ""))})
    if event_type == "tool_call":
        return make("tool_use", {
            "name": str(raw.get("name", "")),
            "input": raw.get("input", {}),
            "id": str(raw.get("id", "")),
        })
    if event_type == "tool_result":
        return make("tool_result", {
            "tool_use_id": str(raw.get("tool_call_id", "")),
            "content": str(raw.get("output", "")),
        })
    if event_type == "usage_complete":
        usage = raw.get("usage") or {}
        token_count = {
            "input_tokens": int((usage.get("prompt_tokens") or 0)),
            "output_tokens": int((usage.get("completion_tokens") or 0)),
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        return make("text", {"text": "", "token_count": token_count})
    if event_type == "complete":
        return make("complete", {})
    if event_type == "error":
        msg = raw.get("message") or raw.get("error") or ""
        return make("error", {"message": str(msg) if msg else str(raw)[:200]})
    return make("error", {
        "reason": f"unrecognized kimi event_type: {event_type!r}",
        "raw": str(raw)[:300],
    })


def _from_ollama_event(
    raw: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map a raw Ollama event dict to a CanonicalEvent (Tier-1 fallback)."""
    raw_type = (raw.get("type") or "text")
    event_type = _LEGACY_TYPE_MAP.get(raw_type, raw_type)
    if event_type not in VALID_EVENT_TYPES:
        event_type = "error"
    data = raw.get("data", {})
    if not isinstance(data, dict):
        data = {"value": data}
    return CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider="ollama",
        event_type=event_type,
        data=data,
        observability_tier=1,
    )
