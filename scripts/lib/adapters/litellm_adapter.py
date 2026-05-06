#!/usr/bin/env python3
"""adapters/litellm_adapter.py — LiteLLMAdapter PoC via OpenAI-shaped streaming.

Routes dispatches through a litellm subprocess shim to reach Bedrock, Mistral,
Vertex, Azure, Groq via one OpenAI-compatible surface. Proof-of-concept scope —
does not replace any existing adapter.

Provider chain format: litellm/<provider>/<model>
  e.g. litellm/bedrock/claude-sonnet-4-6
       litellm/anthropic/claude-sonnet-4-6
       litellm/groq/llama-3.1-70b

Capabilities: CODE, REVIEW (no ORCHESTRATE for v0).
Observability: Tier-1 when streaming SSE works; Tier-2 when only [DONE] reachable.

BILLING SAFETY: No Anthropic SDK imports. Delegates to _litellm_runner.py subprocess.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_adapter import AdapterResult, Capability, ProviderAdapter
from canonical_event import CanonicalEvent
from _streaming_drainer import StreamingDrainerMixin

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_CHUNK_TIMEOUT = 60.0
_DEFAULT_TOTAL_DEADLINE = 300.0
_TIER_STREAMING = 1
_TIER_FINAL_ONLY = 2

# Path to the one-shot runner helper (sibling to this file)
_RUNNER_PATH = Path(__file__).resolve().parent / "_litellm_runner.py"


def _normalize_litellm_event(
    chunk: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Map an OpenAI-shaped NDJSON chunk to a CanonicalEvent.

    Priority order:
      1. error_type key (runner error) -> error
      2. delta.tool_calls non-empty    -> tool_use
      3. finish_reason non-null        -> complete
      4. delta.role == "assistant"
         with empty content            -> init
      5. all other                     -> text
    """
    error_type = chunk.get("error_type")
    if error_type:
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type="error",
            data={"error_type": error_type, "message": chunk.get("message", "")},
            observability_tier=_TIER_STREAMING,
        )

    choices = chunk.get("choices") or []
    choice = choices[0] if choices else {}
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    if delta.get("tool_calls"):
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type="tool_use",
            data={"tool_calls": delta["tool_calls"]},
            observability_tier=_TIER_STREAMING,
        )

    if finish_reason in ("stop", "tool_calls", "end_turn", "length"):
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type="complete",
            data={"finish_reason": finish_reason, "model": chunk.get("model", "")},
            observability_tier=_TIER_STREAMING,
        )

    if delta.get("role") == "assistant" and not delta.get("content"):
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            provider="litellm",
            event_type="init",
            data={"model": chunk.get("model", "")},
            observability_tier=_TIER_STREAMING,
        )

    return CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider="litellm",
        event_type="text",
        data={"content": delta.get("content") or ""},
        observability_tier=_TIER_STREAMING,
    )


class LiteLLMAdapter(StreamingDrainerMixin, ProviderAdapter):
    """Provider adapter for litellm shim subprocess (CODE + REVIEW, v0 PoC).

    Spawns _litellm_runner.py with model + instruction, drains OpenAI-shaped
    NDJSON via StreamingDrainerMixin, and maps chunks to CanonicalEvents.

    Thread-safety note: _dispatch_id is instance-level state mutated before
    each drain_stream() call. Do not share a single instance across threads.
    """

    provider_name = "litellm"

    def __init__(self, terminal_id: str, litellm_model: str = "") -> None:
        self._terminal_id = terminal_id
        self._litellm_model = (
            litellm_model
            or os.environ.get("VNX_LITELLM_MODEL", _DEFAULT_MODEL)
        )
        self._dispatch_id = ""

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "litellm"

    def capabilities(self) -> set[Capability]:
        return {Capability.CODE, Capability.REVIEW}

    def is_available(self) -> bool:
        """Return True when the litellm package is importable."""
        try:
            import litellm as _  # noqa: F401, PLC0415
            return True
        except ImportError:
            return bool(shutil.which("litellm"))

    def health_check(self) -> bool:
        """Run litellm --health-check stub; returns True if CLI exits 0."""
        if not shutil.which("litellm"):
            return False
        try:
            result = subprocess.run(
                ["litellm", "--health-check"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """Spawn _litellm_runner.py, drain NDJSON stream, return AdapterResult."""
        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "")
        model = context.get("model") or self._litellm_model
        chunk_timeout = float(context.get("chunk_timeout", _DEFAULT_CHUNK_TIMEOUT))
        total_deadline = float(context.get("total_deadline", _DEFAULT_TOTAL_DEADLINE))
        event_store = context.get("event_store")

        self._dispatch_id = dispatch_id

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": instruction}],
        })

        runner = context.get("_runner_path", str(_RUNNER_PATH))
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", runner],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return AdapterResult(
                status="failed",
                output=str(exc),
                events=[],
                event_count=0,
                duration_seconds=0.0,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="litellm",
                model=model,
            )

        if proc.stdin:
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.close()

        t0 = time.monotonic()
        events: List[CanonicalEvent] = []
        status = "done"
        output_parts: List[str] = []

        for event in self.drain_stream(
            process=proc,
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            event_store=event_store,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            events.append(event)
            if event.event_type == "text":
                output_parts.append(event.data.get("content", ""))
            elif event.event_type == "error":
                status = "failed"

        duration = time.monotonic() - t0
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (None, 0) and status != "failed":
            status = "failed"

        return AdapterResult(
            status=status,
            output="".join(output_parts),
            events=[e.to_dict() for e in events],
            event_count=len(events),
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="litellm",
            model=model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Stream CanonicalEvent dicts as subprocess emits them."""
        result = self.execute(instruction, context)
        yield from result.events

    def _normalize(self, raw_chunk: Dict[str, Any]) -> CanonicalEvent:
        """Map raw litellm NDJSON chunk to CanonicalEvent (StreamingDrainerMixin hook)."""
        return _normalize_litellm_event(
            chunk=raw_chunk,
            dispatch_id=self._dispatch_id,
            terminal_id=self._terminal_id,
        )
