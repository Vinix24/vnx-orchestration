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
import subprocess
import sys
from pathlib import Path
from typing import Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

logger = logging.getLogger(__name__)

DEFAULT_KIMI_MODEL = "kimi-k2.6"
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
    model: str = DEFAULT_KIMI_MODEL,
    dispatch_id: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Spawn `kimi --print --output-format stream-json --yolo -p <prompt>`.

    stdin=DEVNULL per cli-headless-subprocess-pattern (prevents interactive hang).
    Kimi is subscription-flat: cost_usd_estimate=None, billing_mode=subscription.

    Emits a provider cost event via emit_provider_cost() and returns captured stdout.

    Raises subprocess.TimeoutExpired on timeout.
    Raises RuntimeError on non-zero exit.
    """
    from provider_costs import emit_provider_cost  # noqa: PLC0415

    effective_project_id = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    cmd = ["kimi", "--print", "--output-format", "stream-json", "--yolo", "-p", prompt]
    if model and model != DEFAULT_KIMI_MODEL:
        cmd.extend(["-m", model])

    with open(os.devnull, "r") as devnull:
        try:
            result = subprocess.run(
                cmd,
                stdin=devnull,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                text=True,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "kimi_exec timed out after %.0fs (model=%s dispatch=%s)",
                timeout, model, dispatch_id,
            )
            raise

    stdout = result.stdout or ""
    token_usage = _parse_kimi_token_usage(stdout)
    input_tokens = token_usage.get("input_tokens") if token_usage else None
    output_tokens = token_usage.get("output_tokens") if token_usage else None

    # Kimi is subscription-flat: no per-token billing
    emit_provider_cost(
        provider="kimi",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_estimate=None,
        dispatch_id=dispatch_id,
        project_id=effective_project_id,
        metadata={"billing_mode": "subscription"},
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"kimi_exec failed: returncode={result.returncode} stderr={result.stderr[:500]!r}"
        )

    return stdout
