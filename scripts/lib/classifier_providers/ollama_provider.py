"""Local Ollama provider (subprocess only). Cost is $0 (local inference)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "llama3.1:8b"
_DEFAULT_TIMEOUT = 120


class OllamaProvider(ClassifierProvider):
    """Run classification via `ollama run <model>`."""

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

    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        cmd = ["ollama", "run", self.model]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            return ClassifierResult(
                raw_response="",
                parsed_json=None,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name,
                error=f"ollama CLI not found: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            return ClassifierResult(
                raw_response="",
                parsed_json=None,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name,
                error=f"timeout after {self.timeout_seconds}s: {exc}",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            return ClassifierResult(
                raw_response=proc.stdout or "",
                parsed_json=None,
                cost_usd=0.0,
                latency_ms=latency_ms,
                provider=self.name,
                error=f"exit {proc.returncode}: {(proc.stderr or '').strip()[:500]}",
            )
        raw = proc.stdout or ""
        return ClassifierResult(
            raw_response=raw,
            parsed_json=parse_json_block(raw),
            cost_usd=0.0,
            latency_ms=latency_ms,
            provider=self.name,
            extra={"model": self.model},
        )
