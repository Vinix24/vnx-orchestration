#!/usr/bin/env python3
"""adapters/ollama_adapter.py — OllamaAdapter for decision and digest tasks.

Routes requests to a local Ollama endpoint via HTTP (no subprocess, no SDK).
Supports DECISION (structured JSON verdict) and DIGEST (narrative text).

Streaming: uses an HTTP-line drain that emits CanonicalEvent objects live.
Tier-2 baseline (text-only); Tier-1 when tool_calls are detected in the stream.

Config via env vars:
  VNX_OLLAMA_HOST    — Ollama base URL  (default: http://localhost:11434)
  VNX_OLLAMA_MODEL   — Model to use     (default: gemma3:27b)
  VNX_OLLAMA_TIMEOUT — Request timeout  (default: 60s)

BILLING SAFETY: No Anthropic SDK. HTTP-only via urllib.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event import CanonicalEvent
from provider_adapter import AdapterResult, Capability, ProviderAdapter
from _streaming_drainer import StreamingDrainerMixin

logger = logging.getLogger(__name__)

_DEFAULT_HOST    = "http://localhost:11434"
_DEFAULT_MODEL   = "gemma3:27b"
_DEFAULT_TIMEOUT = 60

# Observability tier labels per spec W7-F
_TIER_FULL     = 1  # tool_use detected (OpenAI tool-trained model)
_TIER_BASELINE = 2  # text-only streaming (default for Ollama)


class OllamaAdapter(StreamingDrainerMixin, ProviderAdapter):
    """Provider adapter for local Ollama endpoint (decision and digest only).

    Calls the Ollama HTTP API directly via urllib — no subprocess, no SDK.
    Emits CanonicalEvent objects live via HTTP line-by-line drain.
    Returns structured JSON for DECISION tasks and raw narrative for DIGEST.

    Fallback behaviour: any HTTP/network error → AdapterResult(status="failed",
    output="ollama_unavailable") so callers can gracefully degrade.
    """

    # StreamingDrainerMixin contract
    provider_name = "ollama"

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id
        self._host  = os.environ.get("VNX_OLLAMA_HOST",  _DEFAULT_HOST).rstrip("/")
        self._model = os.environ.get("VNX_OLLAMA_MODEL", _DEFAULT_MODEL)
        # Context used by _normalize() when called through the mixin's drain_stream()
        self._current_dispatch_id: str = ""
        self._current_terminal_id: str = terminal_id

    # ------------------------------------------------------------------
    # StreamingDrainerMixin._normalize() implementation
    # ------------------------------------------------------------------

    def _normalize(self, raw_chunk: Dict[str, Any]) -> CanonicalEvent:
        """Satisfy StreamingDrainerMixin contract — delegates to static normalizer."""
        return self._normalize_ollama_event(
            raw_chunk,
            dispatch_id=self._current_dispatch_id,
            terminal_id=self._current_terminal_id,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "ollama"

    def capabilities(self) -> set[Capability]:
        return {Capability.DECISION, Capability.DIGEST}

    def is_available(self) -> bool:
        """Return True when Ollama /api/tags responds with HTTP 200."""
        url = f"{self._host}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """POST instruction to /api/generate via HTTP streaming and collect events.

        Uses HTTP line-by-line drain internally to emit CanonicalEvents live.
        Accumulates text across text and complete events to build output.

        context keys used:
          capability  : str        — "decision" or "digest" (log only)
          timeout     : int        — override VNX_OLLAMA_TIMEOUT
          terminal_id : str        — terminal for EventStore writes
          dispatch_id : str        — dispatch for EventStore writes
          event_store : EventStore — live event storage (optional)
        """
        timeout = int(
            context.get("timeout")
            or os.environ.get("VNX_OLLAMA_TIMEOUT", str(_DEFAULT_TIMEOUT))
        )
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "")
        event_store = context.get("event_store")

        t0 = time.monotonic()
        canonical_events: List[CanonicalEvent] = []
        text_parts: List[str] = []

        for event in self._drain_http_stream(
            instruction=instruction,
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            event_store=event_store,
            timeout=timeout,
        ):
            canonical_events.append(event)
            if event.event_type == "text":
                text_parts.append(event.data.get("text", ""))
            elif event.event_type == "complete":
                # Final chunk may carry the last token on generate API
                if "text" in event.data:
                    text_parts.append(event.data["text"])

        duration = time.monotonic() - t0
        output = "".join(text_parts)

        error_events = [e for e in canonical_events if e.event_type == "error"]
        if error_events and not output:
            status = "failed"
            output = "ollama_unavailable"
        else:
            status = "done"

        return AdapterResult(
            status=status,
            output=output,
            events=[e.to_dict() for e in canonical_events],
            event_count=len(canonical_events),
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="ollama",
            model=self._model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """POST with stream=True and yield each CanonicalEvent as a dict.

        Uses the HTTP line-by-line drain for live per-event delivery.
        Falls back to {"type": "error", "data": {"reason": "..."}} on failure.
        """
        timeout = int(
            context.get("timeout")
            or os.environ.get("VNX_OLLAMA_TIMEOUT", str(_DEFAULT_TIMEOUT))
        )
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "")
        event_store = context.get("event_store")

        for event in self._drain_http_stream(
            instruction=instruction,
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            event_store=event_store,
            timeout=timeout,
        ):
            yield event.to_dict()

    # ------------------------------------------------------------------
    # HTTP streaming drain (HTTP-line variant of StreamingDrainerMixin)
    # ------------------------------------------------------------------

    def _drain_http_stream(
        self,
        instruction: str,
        terminal_id: str,
        dispatch_id: str,
        event_store: Optional[Any] = None,
        timeout: float = 60.0,
    ) -> Iterator[CanonicalEvent]:
        """POST to /api/generate with stream=True, yield CanonicalEvents line-by-line.

        Reads NDJSON lines from the HTTP response, maps each via
        _normalize_ollama_event(), and writes to EventStore live (not post-hoc).
        Emits a synthetic error event on connection failure mid-stream.
        """
        # Set instance context for _normalize() (satisfies mixin contract)
        self._current_dispatch_id = dispatch_id
        self._current_terminal_id = terminal_id

        payload = json.dumps({
            "model": self._model,
            "prompt": instruction,
            "stream": True,
        }).encode("utf-8")

        url = f"{self._host}/api/generate"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                yield from self._drain_response_lines(
                    resp=resp,
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    event_store=event_store,
                )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            err = CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="ollama",
                event_type="error",
                data={"reason": f"connection failed: {exc}"},
                observability_tier=_TIER_BASELINE,
            )
            _append_to_store(event_store, terminal_id, err, dispatch_id)
            yield err

    def _drain_response_lines(
        self,
        resp: Any,
        terminal_id: str,
        dispatch_id: str,
        event_store: Optional[Any] = None,
    ) -> Iterator[CanonicalEvent]:
        """Read HTTP response lines, parse JSON, yield CanonicalEvents.

        Writes each event to EventStore immediately (live, not post-hoc).
        Yields a synthetic error event for malformed JSON lines.
        Stops after emitting the complete event (done=True chunk).
        """
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as exc:
                err = CanonicalEvent(
                    dispatch_id=dispatch_id,
                    terminal_id=terminal_id,
                    provider="ollama",
                    event_type="error",
                    data={"reason": str(exc), "raw": line[:500]},
                    observability_tier=_TIER_BASELINE,
                )
                _append_to_store(event_store, terminal_id, err, dispatch_id)
                yield err
                continue

            event = self._normalize_ollama_event(chunk, dispatch_id, terminal_id)
            _append_to_store(event_store, terminal_id, event, dispatch_id)
            yield event

            if event.event_type == "complete":
                break

    # ------------------------------------------------------------------
    # Event normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_ollama_event(
        raw: Dict[str, Any],
        dispatch_id: str = "",
        terminal_id: str = "",
    ) -> CanonicalEvent:
        """Map one Ollama HTTP-stream JSON chunk to a CanonicalEvent.

        Supports both /api/generate (response field) and /api/chat (message field).
        Tier-2 baseline for text-only streaming; Tier-1 when tool_calls detected.

        Ollama chunk fields:
          response   : str  — token text (/api/generate format)
          message    : dict — {"role":"assistant","content":"...","tool_calls":[...]}
          done       : bool — True on the final chunk
          eval_count : int  — total token count (only on final done=True chunk)
        """
        message = raw.get("message")
        response = raw.get("response")
        done = bool(raw.get("done", False))
        eval_count = raw.get("eval_count")

        # Detect tool-use: OpenAI tool-use shape on tool-trained models (e.g. llama3.1-tools)
        tool_calls = None
        if isinstance(message, dict):
            tool_calls = message.get("tool_calls")

        tier = _TIER_FULL if tool_calls else _TIER_BASELINE

        if done:
            data: Dict[str, Any] = {"done": True}
            if eval_count is not None:
                data["token_count"] = int(eval_count)
            # Final chunk may carry the last token on generate API
            last_text = ""
            if response is not None:
                last_text = response
            elif isinstance(message, dict):
                last_text = message.get("content", "")
            if last_text:
                data["text"] = last_text
            return CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="ollama",
                event_type="complete",
                data=data,
                observability_tier=tier,
            )

        if tool_calls:
            return CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="ollama",
                event_type="tool_use",
                data={"tool_calls": tool_calls},
                observability_tier=_TIER_FULL,
            )

        # Text token
        text = ""
        if response is not None:
            text = response
        elif isinstance(message, dict):
            text = message.get("content", "")

        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="ollama",
            event_type="text",
            data={"text": text},
            observability_tier=tier,
        )


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _append_to_store(
    event_store: Optional[Any],
    terminal_id: str,
    event: CanonicalEvent,
    dispatch_id: str,
) -> None:
    """Write event to EventStore; swallow all errors so the drain stays live."""
    if event_store is None:
        return
    try:
        event_store.append(terminal_id, event, dispatch_id=dispatch_id)
    except Exception:
        logger.exception("OllamaAdapter: EventStore.append failed for %s", terminal_id)
