"""Claude Haiku provider via the local `claude` CLI (subprocess only).

Pricing constants are pinned to claude-haiku-4-5 list pricing. We use a
flat-rate estimate per call because the CLI does not consistently report
per-call token usage on stdout. Operators can override via env vars.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_TIMEOUT = 60
_DEFAULT_FLAT_COST_USD = 0.001


class HaikuProvider(ClassifierProvider):
    """Run classification via `claude --print --model claude-haiku-4-5`."""

    name = "haiku"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        flat_cost_usd: Optional[float] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_HAIKU_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_HAIKU_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        try:
            self.flat_cost_usd = float(
                flat_cost_usd
                if flat_cost_usd is not None
                else os.environ.get("VNX_HAIKU_FLAT_COST_USD", _DEFAULT_FLAT_COST_USD)
            )
        except (TypeError, ValueError):
            self.flat_cost_usd = _DEFAULT_FLAT_COST_USD

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        cmd = ["claude", "--print", "--model", self.model]
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
                error=f"claude CLI not found: {exc}",
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
        parsed = parse_json_block(raw)
        return ClassifierResult(
            raw_response=raw,
            parsed_json=parsed,
            cost_usd=self.flat_cost_usd,
            latency_ms=latency_ms,
            provider=self.name,
            extra={"model": self.model},
        )
