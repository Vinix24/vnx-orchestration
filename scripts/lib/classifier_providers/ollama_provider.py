"""Local Ollama provider (HTTP API). Cost is $0 (local inference).

Calls the Ollama HTTP API (`/api/generate`, non-streaming, `format=json`) rather
than `ollama run`. The interactive CLI writes TTY progress + line-wrap re-renders
into a piped stdout that duplicate and corrupt the model's JSON; the HTTP API
returns the completion verbatim in a clean envelope, so parsing is reliable.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "llama3.1:8b"
_DEFAULT_TIMEOUT = 120


def _api_host() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
    if not host.startswith("http"):
        host = "http://" + host
    return host.rstrip("/")


class OllamaProvider(ClassifierProvider):
    """Run classification via the local Ollama HTTP API."""

    name = "ollama"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_OLLAMA_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_OLLAMA_TIMEOUT", _DEFAULT_TIMEOUT)
        )

    def is_available(self) -> bool:
        return shutil.which("ollama") is not None

    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        url = _api_host() + "/api/generate"
        # format=json constrains generation to a valid JSON object (no prose, no
        # ```json fences), which is exactly the scout/tagger contract. temperature=0
        # keeps the ranking deterministic across A/B runs.
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            return ClassifierResult(
                raw_response="",
                parsed_json=None,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name,
                error=f"ollama API unreachable ({url}): {exc}",
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return ClassifierResult(
                raw_response="",
                parsed_json=None,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name,
                error=f"ollama API returned non-JSON envelope: {exc}",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        raw = payload.get("response", "") or ""
        return ClassifierResult(
            raw_response=raw,
            parsed_json=parse_json_block(raw),
            cost_usd=0.0,
            latency_ms=latency_ms,
            provider=self.name,
            error=payload.get("error"),
            extra={"model": self.model},
        )
