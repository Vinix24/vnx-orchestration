#!/usr/bin/env python3
"""gemini_wrapper.py — Lightweight Gemini CLI wrapper with cost emission.

Wraps `gemini --model <model> --output-format stream-json` (prompt via stdin)
and emits a provider cost event to .vnx-data/events/provider_costs.ndjson per ADR-005.

BILLING SAFETY: only subprocess.Popen(["gemini", ...]) is invoked.
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

DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
DEFAULT_TIMEOUT = 300.0


def _parse_gemini_token_usage(stdout: str) -> Optional[dict]:
    """Extract token counts from Gemini CLI stream-json output.

    Gemini emits usageMetadata with promptTokenCount / candidatesTokenCount.
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

        # usageMetadata at top level or nested
        usage_meta = event.get("usageMetadata") or {}
        if not isinstance(usage_meta, dict):
            usage_meta = {}

        prompt_t = int(usage_meta.get("promptTokenCount") or 0)
        candidates_t = int(usage_meta.get("candidatesTokenCount") or 0)
        if prompt_t or candidates_t:
            input_t = prompt_t
            output_t = candidates_t
            continue

        # Fallback: promptTokenCount at top level
        top_prompt = int(event.get("promptTokenCount") or 0)
        top_candidates = int(event.get("candidatesTokenCount") or 0)
        if top_prompt or top_candidates:
            input_t = top_prompt
            output_t = top_candidates

    if input_t == 0 and output_t == 0:
        return None
    return {"input_tokens": input_t, "output_tokens": output_t}


def gemini_exec(
    prompt: str,
    model: str = DEFAULT_GEMINI_MODEL,
    dispatch_id: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Spawn `gemini --model <model> --output-format stream-json` with prompt via stdin.

    Emits a provider cost event via emit_provider_cost() and returns captured stdout.

    Raises subprocess.TimeoutExpired on timeout.
    Raises RuntimeError on non-zero exit.
    """
    from provider_costs import emit_provider_cost, _compute_cost_from_rates  # noqa: PLC0415

    effective_project_id = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    cmd = ["gemini", "--model", model, "--output-format", "stream-json"]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "gemini_exec timed out after %.0fs (model=%s dispatch=%s)",
            timeout, model, dispatch_id,
        )
        raise

    stdout = result.stdout or ""
    token_usage = _parse_gemini_token_usage(stdout)
    input_tokens = token_usage.get("input_tokens") if token_usage else None
    output_tokens = token_usage.get("output_tokens") if token_usage else None

    cost_usd, is_flat = _compute_cost_from_rates("gemini", model, input_tokens, output_tokens)
    if is_flat:
        cost_usd = None

    emit_provider_cost(
        provider="gemini",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_estimate=cost_usd,
        dispatch_id=dispatch_id,
        project_id=effective_project_id,
        metadata={"billing_mode": "subscription"} if is_flat else None,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"gemini_exec failed: returncode={result.returncode} stderr={result.stderr[:500]!r}"
        )

    return stdout
