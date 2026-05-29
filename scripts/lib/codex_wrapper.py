#!/usr/bin/env python3
"""codex_wrapper.py — Lightweight Codex CLI wrapper with cost emission.

Wraps `codex exec --json --model <model>` (prompt via stdin) and emits a
provider cost event to .vnx-data/events/provider_costs.ndjson per ADR-005.

BILLING SAFETY: only subprocess.Popen(["codex", "exec", "--json"]) is invoked.
No Anthropic SDK, no LiteLLM, no direct API calls.
"""

from __future__ import annotations

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

DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_TIMEOUT = 300.0


def _parse_codex_token_usage(stdout: str) -> Optional[dict]:
    """Extract token counts from Codex NDJSON stream output."""
    import json as _json
    from provider_spawns.codex_spawn import _extract_token_count_payload, _normalize_token_count  # noqa: PLC0415

    input_t = 0
    output_t = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        payload = _extract_token_count_payload(event)
        if payload:
            normalized = _normalize_token_count(payload)
            if normalized:
                input_t = normalized.get("input_tokens", 0) or 0
                output_t = normalized.get("output_tokens", 0) or 0

    if input_t == 0 and output_t == 0:
        return None
    return {"input_tokens": input_t, "output_tokens": output_t}


def codex_exec(
    prompt: str,
    model: str = DEFAULT_CODEX_MODEL,
    dispatch_id: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Spawn `codex exec --json --model <model>` with prompt via stdin.

    Emits a provider cost event via emit_provider_cost() and returns captured stdout.

    Raises subprocess.TimeoutExpired on timeout.
    Raises RuntimeError on non-zero exit.
    """
    from provider_costs import emit_provider_cost, _compute_cost_from_rates  # noqa: PLC0415

    effective_project_id = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    cmd = ["codex", "exec", "--json", "--model", model]

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
            "codex_exec timed out after %.0fs (model=%s dispatch=%s)",
            timeout, model, dispatch_id,
        )
        raise

    stdout = result.stdout or ""
    token_usage = _parse_codex_token_usage(stdout)
    input_tokens = token_usage.get("input_tokens") if token_usage else None
    output_tokens = token_usage.get("output_tokens") if token_usage else None

    cost_usd, is_flat = _compute_cost_from_rates("codex", model, input_tokens, output_tokens)
    if is_flat:
        cost_usd = None

    emit_provider_cost(
        provider="codex",
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
            f"codex_exec failed: returncode={result.returncode} stderr={result.stderr[:500]!r}"
        )

    return stdout
