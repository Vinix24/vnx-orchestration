"""Gemini CLI provider (subprocess only). Cost is best-effort flat-rate."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "gemini-2.0-flash"
_DEFAULT_TIMEOUT = 60
_DEFAULT_FLAT_COST_USD = 0.0005


class GeminiProvider(ClassifierProvider):
    """Run classification via the local `gemini` CLI."""

    name = "gemini"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        flat_cost_usd: Optional[float] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_GEMINI_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_GEMINI_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        try:
            self.flat_cost_usd = float(
                flat_cost_usd
                if flat_cost_usd is not None
                else os.environ.get("VNX_GEMINI_FLAT_COST_USD", _DEFAULT_FLAT_COST_USD)
            )
        except (TypeError, ValueError):
            self.flat_cost_usd = _DEFAULT_FLAT_COST_USD

    def is_available(self) -> bool:
        return shutil.which("gemini") is not None

    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        cmd = ["gemini", "--model", self.model, "--prompt", prompt]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
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
                error=f"gemini CLI not found: {exc}",
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
            cost_usd=self.flat_cost_usd,
            latency_ms=latency_ms,
            provider=self.name,
            extra={"model": self.model},
        )
