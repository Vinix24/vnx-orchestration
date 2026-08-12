#!/usr/bin/env python3
"""kimi_wrapper.py — Lightweight Kimi CLI wrapper with cost emission.

Wraps `kimi --print --output-format stream-json --yolo -p <prompt>` and emits
a provider cost event to .vnx-data/events/provider_costs.ndjson per ADR-005.

Authentication via `kimi login` (OAuth). No API key required.
Kimi is subscription-flat; cost_usd_estimate=None is emitted with billing_mode=subscription.

BILLING SAFETY: only subprocess.Popen(["kimi", ...]) is invoked.
No Anthropic SDK, no LiteLLM, no direct API calls.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

logger = logging.getLogger(__name__)

# Mirror of the registry default (kimi_cli.default_model in wave7_models.yaml).
# Retained ONLY for the 90-day backward-compat import surface
# (providers/provider_lanes/kimi.py re-exports it); the exec path never reads
# it — model resolution goes through provider_dispatch's kimi resolver, so a
# registry change cannot silently drift past this constant (OI-1077: the old
# hardcoded "kimi-k2.6" default outlived the model itself).
DEFAULT_KIMI_MODEL = "kimi-k3"
DEFAULT_TIMEOUT = 300.0


def _parse_kimi_token_usage(stdout: str) -> Optional[dict]:
    """Extract token counts from Kimi CLI stream-json output.

    Kimi emits `usage_complete` events with prompt_tokens / completion_tokens,
    and StatusUpdate events with token_count.
    """
    input_t = 0
    output_t = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("event_type") or event.get("type") or ""

        if event_type == "usage_complete":
            usage = event.get("usage") or {}
            input_t = int(usage.get("prompt_tokens") or 0)
            output_t = int(usage.get("completion_tokens") or 0)

        elif event_type == "StatusUpdate":
            tc = event.get("token_count") or event.get("usage") or {}
            in_val = int(tc.get("input_tokens") or tc.get("prompt_tokens") or 0)
            out_val = int(tc.get("output_tokens") or tc.get("completion_tokens") or 0)
            if in_val or out_val:
                input_t = in_val
                output_t = out_val

    if input_t == 0 and output_t == 0:
        return None
    return {"input_tokens": input_t, "output_tokens": output_t}


def kimi_exec(
    prompt: str,
    model: Optional[str] = None,
    dispatch_id: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Spawn `kimi --print --output-format stream-json --yolo -m <model> -p <prompt>`.

    Model resolution (OI-1077) goes through the SAME resolver chain the provider
    lane uses (provider_dispatch): ``_kimi_resolve_requested_key`` applies the
    explicit-model -> VNX_KIMI_MODEL env -> registry-default precedence, and
    ``_kimi_resolve_cli_model_arg`` maps the registry key to the CLI arg form
    (kimi-cli 1.46.0 wants slash-form managed ids like ``kimi-code/k3``; raw
    registry keys fail with rc=1 "LLM not set"). The resolved arg is ALWAYS
    passed via ``-m`` so what runs is pinned by the VNX registry, not by
    whatever ``~/.kimi/config.toml`` happens to default to.

    stdin=DEVNULL per cli-headless-subprocess-pattern (prevents interactive hang).
    Kimi is subscription-flat: cost_usd_estimate=None, billing_mode=subscription.

    Emits a provider cost event via emit_provider_cost() (labeled with the
    resolved registry key, same label the provider lane emits) and returns
    captured stdout.

    Raises KimiModelResolutionError (a ValueError) when the requested model is
    unknown or disabled in the registry — fail-loud, no silent substitution.
    Raises subprocess.TimeoutExpired on timeout.
    Raises RuntimeError on non-zero exit.
    """
    from provider_costs import emit_provider_cost  # noqa: PLC0415
    # Lazy import: keeps this wrapper light at import time and cycle-safe
    # (provider_lanes.kimi imports this module at module level).
    from provider_dispatch import (  # noqa: PLC0415
        _kimi_resolve_cli_model_arg,
        _kimi_resolve_requested_key,
    )

    model_key = _kimi_resolve_requested_key(model)
    cli_model_arg = _kimi_resolve_cli_model_arg(model_key)

    cmd = [
        "kimi", "--print", "--output-format", "stream-json", "--yolo",
        "-m", cli_model_arg, "-p", prompt,
    ]

    with open(os.devnull, "r") as devnull:
        proc = subprocess.Popen(
            cmd,
            stdin=devnull,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the entire process group so pipe-holding children also die.
            # start_new_session=True guarantees kimi is its own process group leader,
            # so os.killpg reaches all child processes that hold the pipe open.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            logger.error(
                "kimi_exec timed out after %.0fs (model=%s dispatch=%s)",
                timeout, model_key, dispatch_id,
            )
            raise

    stdout = stdout_data or ""
    token_usage = _parse_kimi_token_usage(stdout)
    input_tokens = token_usage.get("input_tokens") if token_usage else None
    output_tokens = token_usage.get("output_tokens") if token_usage else None

    # Kimi is subscription-flat: no per-token billing
    emit_provider_cost(
        provider="kimi",
        model=model_key,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_estimate=None,
        dispatch_id=dispatch_id,
        project_id=project_id,  # forward caller pid; emit resolves env fallback (best-effort)
        metadata={"billing_mode": "subscription"},
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"kimi_exec failed: returncode={proc.returncode} stderr={stderr_data[:500]!r}"
        )

    return stdout
