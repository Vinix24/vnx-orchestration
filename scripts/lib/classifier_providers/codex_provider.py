"""Codex CLI provider (subprocess only). Currently rate-limited until 2026-05-05."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_TIMEOUT = 120
_DEFAULT_FLAT_COST_USD = 0.002


class CodexProvider(ClassifierProvider):
    """Run classification via the local `codex` CLI (`codex exec --json`)."""

    name = "codex"

    def __init__(
        self,
        timeout_seconds: Optional[int] = None,
        flat_cost_usd: Optional[float] = None,
    ) -> None:
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_CODEX_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        try:
            self.flat_cost_usd = float(
                flat_cost_usd
                if flat_cost_usd is not None
                else os.environ.get("VNX_CODEX_FLAT_COST_USD", _DEFAULT_FLAT_COST_USD)
            )
        except (TypeError, ValueError):
            self.flat_cost_usd = _DEFAULT_FLAT_COST_USD

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def classify(self, prompt: str, max_tokens: int = 1500) -> ClassifierResult:
        cmd = ["codex", "exec", "--json"]
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
                error=f"codex CLI not found: {exc}",
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
        )
