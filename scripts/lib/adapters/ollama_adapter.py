#!/usr/bin/env python3
"""adapters/ollama_adapter.py — OllamaAdapter for decision and digest tasks.

Routes requests to a local Ollama endpoint via HTTP (no subprocess, no SDK).
Supports DECISION (structured JSON verdict) and DIGEST (narrative text).

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
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_adapter import AdapterResult, Capability, ProviderAdapter

logger = logging.getLogger(__name__)

_DEFAULT_HOST    = "http://localhost:11434"
_DEFAULT_MODEL   = "gemma3:27b"
_DEFAULT_TIMEOUT = 60


class OllamaAdapter(ProviderAdapter):
    """Provider adapter for local Ollama endpoint (decision and digest only).

    Calls the Ollama HTTP API directly via urllib — no subprocess, no SDK.
    Returns structured JSON for DECISION tasks and raw narrative for DIGEST.

    Fallback behaviour: any HTTP error → AdapterResult(status="failed",
    output="ollama_unavailable") so callers can gracefully degrade.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id
        self._host  = os.environ.get("VNX_OLLAMA_HOST",  _DEFAULT_HOST).rstrip("/")
        self._model = os.environ.get("VNX_OLLAMA_MODEL", _DEFAULT_MODEL)

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
        """POST instruction to /api/generate and return structured result.

        context keys used:
          capability : str  — "decision" or "digest" (affects log verbosity only)
          timeout    : int  — override VNX_OLLAMA_TIMEOUT for this call

        For DECISION tasks the caller is expected to parse output as JSON via
        _parse_llm_response(); OllamaAdapter returns the raw LLM text extracted
        from Ollama's {"response": "..."} wrapper.

        Returns AdapterResult(status="failed", output="ollama_unavailable") on
        any network/timeout error so callers can fall back without crashing.
        """
        timeout = int(
            context.get("timeout")
            or os.environ.get("VNX_OLLAMA_TIMEOUT", str(_DEFAULT_TIMEOUT))
        )
        capability = context.get("capability", "digest")

        payload = json.dumps({
            "model": self._model,
            "prompt": instruction,
            "stream": False,
        }).encode("utf-8")

        url = f"{self._host}/api/generate"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            duration = time.monotonic() - t0
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            duration = time.monotonic() - t0
            logger.warning(
                "OllamaAdapter [%s] request failed (%s)", capability, exc
            )
            return AdapterResult(
                status="failed",
                output="ollama_unavailable",
                events=[],
                event_count=0,
                duration_seconds=duration,
                committed=False,
                commit_hash=None,
                report_path=None,
                provider="ollama",
                model=self._model,
            )

        response_text = self._extract_response(body)
        return AdapterResult(
            status="done",
            output=response_text,
            events=[{"type": "result", "data": response_text}],
            event_count=1,
            duration_seconds=duration,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="ollama",
            model=self._model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """POST with stream=true and yield each chunk as an event.

        Ollama streaming format: each line is JSON {"response": "...", "done": bool}.
        Yields {"type": "chunk", "data": "<text>"} for each token.
        Yields {"type": "done"} when the stream ends.

        Falls back to {"type": "error", "data": "ollama_unavailable"} on failure.
        """
        timeout = int(
            context.get("timeout")
            or os.environ.get("VNX_OLLAMA_TIMEOUT", str(_DEFAULT_TIMEOUT))
        )

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
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = chunk.get("response", "")
                    if token:
                        yield {"type": "chunk", "data": token}
                    if chunk.get("done"):
                        yield {"type": "done"}
                        return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning("OllamaAdapter stream failed: %s", exc)
            yield {"type": "error", "data": "ollama_unavailable"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_response(body: str) -> str:
        """Extract LLM text from Ollama's JSON wrapper.

        Ollama non-streaming response: {"model":..., "response":"<text>", "done":true, ...}
        Returns the inner response text, or raw body if parsing fails.
        """
        try:
            data = json.loads(body)
            if isinstance(data, dict) and "response" in data:
                return str(data["response"])
        except (json.JSONDecodeError, ValueError):
            pass
        return body.strip()
