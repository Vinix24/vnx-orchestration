"""DeepSeek classifier provider via the claude-CLI harness (key-auth).

Runs `claude --print` with ANTHROPIC_BASE_URL pointed at DeepSeek's
Anthropic-compatible endpoint and ANTHROPIC_API_KEY = the operator's own
DEEPSEEK_API_KEY. This is the sanctioned key-auth path per the provider
constraint `deepseek-harness-subscription-blocked`: own-key + hardening
(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, MCP off) routes all inference to
DeepSeek with zero calls to api.anthropic.com. It is BLOCKED to ride the
production OAuth subscription (no own key) — hence the explicit DEEPSEEK_API_KEY
requirement in is_available().

Cheap (DeepSeek-Flash ~$0.14/$0.28 per MTok) and model-agnostic: the model is
selectable via VNX_DEEPSEEK_CLASSIFIER_MODEL.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_TIMEOUT = 60
_DEFAULT_FLAT_COST_USD = 0.0005
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


class DeepSeekProvider(ClassifierProvider):
    """Run classification via the DeepSeek harness lane (own-key, hardened)."""

    name = "deepseek"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        flat_cost_usd: Optional[float] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_DEEPSEEK_CLASSIFIER_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_DEEPSEEK_CLASSIFIER_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        try:
            self.flat_cost_usd = float(
                flat_cost_usd
                if flat_cost_usd is not None
                else os.environ.get("VNX_DEEPSEEK_CLASSIFIER_FLAT_COST_USD", _DEFAULT_FLAT_COST_USD)
            )
        except (TypeError, ValueError):
            self.flat_cost_usd = _DEFAULT_FLAT_COST_USD

    def is_available(self) -> bool:
        # Requires the claude CLI AND the operator's own DeepSeek key (never the
        # production OAuth subscription — constraint deepseek-harness-subscription-blocked).
        return shutil.which("claude") is not None and bool(os.environ.get("DEEPSEEK_API_KEY"))

    def _harness_env(self) -> dict:
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = _DEEPSEEK_ANTHROPIC_BASE_URL
        env["ANTHROPIC_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
        # Hardening: kill non-essential traffic + telemetry so 0 calls reach
        # api.anthropic.com (the measured-safe configuration in the constraint doc).
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        cmd = [
            "claude", "--print", "--model", self.model,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=self._harness_env(),
            )
        except FileNotFoundError as exc:
            return ClassifierResult(
                raw_response="", parsed_json=None, cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name, error=f"claude CLI not found: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            return ClassifierResult(
                raw_response="", parsed_json=None, cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                provider=self.name, error=f"timeout after {self.timeout_seconds}s: {exc}",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode != 0:
            return ClassifierResult(
                raw_response=proc.stdout or "", parsed_json=None, cost_usd=0.0,
                latency_ms=latency_ms, provider=self.name,
                error=f"exit {proc.returncode}: {(proc.stderr or '').strip()[:500]}",
            )
        raw = proc.stdout or ""
        return ClassifierResult(
            raw_response=raw,
            parsed_json=parse_json_block(raw),
            cost_usd=self.flat_cost_usd,
            latency_ms=latency_ms,
            provider=self.name,
            extra={"model": self.model, "lane": "deepseek-harness"},
        )
