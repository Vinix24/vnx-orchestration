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

# Public adapter-level tier constant: effective tier when streaming SSE works.
# Falls back to _TIER_FINAL_ONLY (2) when only [DONE] is reachable.
OBSERVABILITY_TIER = _TIER_STREAMING

# Path to the one-shot runner helper (sibling to this file)
_RUNNER_PATH = Path(__file__).resolve().parent / "_litellm_runner.py"


def _normalize_litellm_event(
    chunk: Dict[str, Any],
    dispatch_id: str,
    terminal_id: str,
) -> CanonicalEvent:
    """Backward-compat shim; delegates to normalize_litellm_event in litellm_spawn.

    Note: original signature was (chunk, dispatch_id, terminal_id); litellm_spawn uses
    (chunk, terminal_id, dispatch_id) matching codex/gemini convention — args are swapped.
    """
    from provider_spawns.litellm_spawn import normalize_litellm_event
    return normalize_litellm_event(chunk, terminal_id, dispatch_id)


class LiteLLMAdapter(StreamingDrainerMixin, ProviderAdapter):
    """Provider adapter for litellm shim subprocess (CODE + REVIEW, v0 PoC).

    Spawns _litellm_runner.py with model + instruction, drains OpenAI-shaped
    NDJSON via StreamingDrainerMixin, and maps chunks to CanonicalEvents.

    Thread-safety note: _dispatch_id is instance-level state mutated before
    each drain_stream() call. Do not share a single instance across threads.
    """

    provider_name = "litellm"
    provider_observability_tier = OBSERVABILITY_TIER

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
        """Spawn _litellm_runner.py, drain NDJSON stream, return AdapterResult.

        Delegates spawn+stream to spawn_litellm(); byte-identical to pre-delegation output.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from provider_spawns.litellm_spawn import spawn_litellm

        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "")
        model = context.get("model") or self._litellm_model
        chunk_timeout = float(context.get("chunk_timeout", _DEFAULT_CHUNK_TIMEOUT))
        total_deadline = float(context.get("total_deadline", _DEFAULT_TOTAL_DEADLINE))
        event_store = context.get("event_store")
        runner_path = context.get("_runner_path", str(_RUNNER_PATH))

        self._dispatch_id = dispatch_id

        collected_dicts: List[dict] = []
        status = "done"
        t0 = time.monotonic()

        def _writer(tid: str, event_dict: dict, dispatch_id: str = "") -> None:
            collected_dicts.append(event_dict)

        result = spawn_litellm(
            prompt=instruction,
            model=model,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            event_writer=_writer,
            event_store=event_store,
            runner_path=runner_path,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        )

        duration = time.monotonic() - t0
        output_parts: List[str] = []
        for event_dict in collected_dicts:
            evt_type = event_dict.get("event_type", "")
            if evt_type == "text":
                output_parts.append((event_dict.get("data") or {}).get("content", ""))
            elif evt_type == "error":
                status = "failed"

        if result.returncode not in (None, 0) and status != "failed":
            status = "failed"

        return AdapterResult(
            status=status,
            output="".join(output_parts),
            events=collected_dicts,
            event_count=result.events_written,
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
        """Map raw litellm NDJSON chunk to CanonicalEvent (StreamingDrainerMixin hook).

        Delegates to normalize_litellm_event for byte identity with spawn_litellm.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from provider_spawns.litellm_spawn import normalize_litellm_event
        return normalize_litellm_event(
            chunk=raw_chunk,
            terminal_id=self._terminal_id,
            dispatch_id=self._dispatch_id,
        )
