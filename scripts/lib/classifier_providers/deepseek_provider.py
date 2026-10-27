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
import uuid
from datetime import datetime, timezone
from typing import Optional

import harness_run_cost
import provider_switch
from load_deepseek_key import resolve_deepseek_api_key

from provider_spawns.harness_config_dir import (
    HarnessConfigDirError,
    bare_harness_cli_args,
    harness_config_env,
    require_harness_credential,
)

from .base import ClassifierProvider, ClassifierResult, parse_json_block

_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_TIMEOUT = 60
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


class DeepSeekProvider(ClassifierProvider):
    """Run classification via the DeepSeek harness lane (own-key, hardened)."""

    name = "deepseek"
    reachability_key = "deepseek-harness"
    ledger_provider = "deepseek-harness"

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.model = model or os.environ.get("VNX_DEEPSEEK_CLASSIFIER_MODEL", _DEFAULT_MODEL)
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("VNX_DEEPSEEK_CLASSIFIER_TIMEOUT", _DEFAULT_TIMEOUT)
        )

    def is_present(self) -> bool:
        # Requires the claude CLI AND the operator's own DeepSeek key (never the
        # production OAuth subscription — constraint deepseek-harness-subscription-blocked).
        # OI-2182: the key may live in the provider env file, not this shell's env.
        return shutil.which("claude") is not None and bool(resolve_deepseek_api_key())

    def _harness_env(self) -> dict:
        env = dict(os.environ)
        # OI-2182: the only credential this path may carry is the freshly resolved
        # key (ANTHROPIC_API_KEY below). Scrub an inherited ANTHROPIC_AUTH_TOKEN
        # (e.g. a prior deepseek-harness shell) so require_harness_credential
        # refuses a key-less child instead of silently riding a stale token, and
        # scrub the OAuth token so the key-auth path can never fall back to OAuth.
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env["ANTHROPIC_BASE_URL"] = _DEEPSEEK_ANTHROPIC_BASE_URL
        env["ANTHROPIC_API_KEY"] = resolve_deepseek_api_key()
        # Hardening: kill non-essential traffic + telemetry so 0 calls reach
        # api.anthropic.com (the measured-safe configuration in the constraint doc).
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        # Own config dir, applied last: never inherit the operator's CLAUDE.md/skills/memory.
        env.update(harness_config_env("deepseek-harness"))
        # --bare reads only ANTHROPIC_API_KEY: refuse a child that would carry none.
        require_harness_credential(env)
        return env

    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        started = datetime.now(timezone.utc)
        session_id = str(uuid.uuid4())
        model = harness_run_cost.lane_model(self.ledger_provider, self.model)
        start = time.monotonic()
        try:
            provider_switch.require(self.name, consumer="classifier_providers.deepseek",
                                    project_id=self.switch_project_id)
        except provider_switch.ProviderDisabledError as exc:
            self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                            spawn_refused=f"provider_disabled: {exc}")
            raise
        cmd = [
            "claude", "--print", *bare_harness_cli_args(), "--session-id", session_id,
            "--model", model, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        try:
            child_env = self._harness_env()
        except HarnessConfigDirError as exc:
            return self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                                   spawn_refused=f"harness_config_dir: {exc}",
                                   error=f"harness config dir unsafe: {exc}")
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=self.timeout_seconds, env=child_env)
        except FileNotFoundError as exc:
            return self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                                   spawn_refused=f"claude_cli_missing: {exc}",
                                   error=f"claude CLI not found: {exc}")
        except subprocess.TimeoutExpired as exc:
            return self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                                   error=f"timeout after {self.timeout_seconds}s: {exc}")
        raw = proc.stdout or ""
        extra = {"model": model, "lane": self.ledger_provider}
        if proc.returncode != 0:
            return self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                                   raw_response=raw, extra=extra,
                                   error=f"exit {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
        return self.finish_run(session_id=session_id, model=model, started=started, monotonic_start=start,
                               raw_response=raw, parsed_json=parse_json_block(raw), extra=extra)
