#!/usr/bin/env python3
"""provider_costs.py — Central provider-cost NDJSON emitter.

ADR-005: Writes append-only NDJSON events to .vnx-data/events/provider_costs.ndjson
BEFORE any downstream state mutation.

ADR-007: Every event includes project_id for multi-tenant traceability.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate table
# ---------------------------------------------------------------------------
# (provider, model) -> (input_per_mtok_usd, output_per_mtok_usd, is_subscription_flat)
# is_subscription_flat=True means the model is billed as a flat subscription;
# cost_usd_estimate=None is emitted in that case.

_PROVIDER_RATES: dict[tuple[str, str], tuple[float, float, bool]] = {
    # Anthropic Claude (metered via API / OAuth dispatch)
    ("claude", "claude-sonnet-4-6"):           (3.0,   15.0,  False),
    ("claude", "claude-sonnet-4-5"):           (3.0,   15.0,  False),
    ("claude", "claude-opus-4-8"):             (15.0,  75.0,  False),
    ("claude", "claude-opus-4-7"):             (15.0,  75.0,  False),
    ("claude", "claude-haiku-4-5"):            (0.8,   4.0,   False),
    ("claude", "claude-haiku-4-5-20251001"):   (0.8,   4.0,   False),
    # Short model aliases used internally
    ("claude", "sonnet"):                      (3.0,   15.0,  False),
    ("claude", "opus"):                        (15.0,  75.0,  False),
    ("claude", "haiku"):                       (0.8,   4.0,   False),
    # OpenAI via Codex CLI
    ("codex", "gpt-5.5"):                      (0.25,  1.00,  False),
    ("codex", "gpt-5.2-codex"):                (0.25,  1.00,  False),
    ("codex", "gpt-4o"):                       (2.5,   10.0,  False),
    ("codex", "gpt-4.1"):                      (2.0,   8.0,   False),
    # Google Gemini CLI
    ("gemini", "gemini-2.5-pro"):              (0.25,  0.75,  False),
    ("gemini", "gemini-2.5-flash"):            (0.075, 0.30,  False),
    ("gemini", "gemini-2.0-flash"):            (0.075, 0.30,  False),
    ("gemini", "gemini-1.5-pro"):              (1.25,  5.0,   False),
    # Kimi CLI — OAuth subscription, no per-token pricing
    ("kimi", "kimi-k2.6"):                     (0.0,   0.0,   True),
    ("kimi", "kimi-k2-0905-default"):          (0.0,   0.0,   True),
    ("kimi", "kimi-k2-0905-preview"):          (0.0,   0.0,   True),
    ("kimi", "kimi-default"):                  (0.0,   0.0,   True),
    # LiteLLM sub-providers
    ("litellm:deepseek", "deepseek-v4-pro"):   (0.14,  0.28,  False),
    ("litellm:moonshot", "kimi-k2-0905-default"): (0.0, 0.0,  True),
    ("litellm:moonshot", "kimi-k2-0905-preview"):  (0.0, 0.0,  True),
    ("litellm:zai", "glm-5.1-default"):        (0.07,  0.14,  False),
    ("litellm:ollama", "llama3"):              (0.0,   0.0,   True),
}


def _lookup_rate(provider: str, model: str) -> tuple[float, float, bool] | None:
    """Look up (provider, model) rate, with path-prefix normalization fallback."""
    key = (provider, model)
    if key in _PROVIDER_RATES:
        return _PROVIDER_RATES[key]
    # Normalize: 'anthropic/claude-sonnet-4-6' -> 'claude-sonnet-4-6'
    normalized = model.split("/")[-1] if "/" in model else model
    key2 = (provider, normalized)
    if key2 in _PROVIDER_RATES:
        return _PROVIDER_RATES[key2]
    return None


def _compute_cost_from_rates(
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> tuple[float | None, bool]:
    """Compute cost from rate table.

    Returns (cost_usd, is_subscription_flat):
    - (None, True)  for subscription-flat models
    - (cost, False) for metered models with tokens
    - (None, False) when provider+model not in rate table
    """
    rate = _lookup_rate(provider, model)
    if rate is None:
        return None, False
    input_per_mtok, output_per_mtok, is_flat = rate
    if is_flat:
        return None, True
    in_t = input_tokens or 0
    out_t = output_tokens or 0
    cost = (in_t / 1_000_000) * input_per_mtok + (out_t / 1_000_000) * output_per_mtok
    return round(cost, 8), False


def resolve_cost_usd(
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """Public cost resolver from the rate table.

    Returns the metered cost in USD, or ``None`` for subscription/OAuth flat
    lanes (legitimately $0) AND for provider+model pairs absent from the rate
    table. Used as the fallback when the wave7 provider-registry lookup misses,
    so cost_usd resolves to real dollars for API lanes (deepseek/openrouter/
    codex-API) instead of silently landing at 0.
    """
    cost, _is_flat = _compute_cost_from_rates(provider, model, input_tokens, output_tokens)
    return cost


def _make_record_id(
    dispatch_id: str | None,
    timestamp: str,
    project_id: str = "",
    event_type: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    """Stable idempotency key: sha256[:32] of all discriminating fields.

    Includes token counts so two emits in the same second remain distinct.
    Includes project_id per ADR-007.
    """
    raw = (
        f"{dispatch_id or ''}:{project_id}:{event_type}:{timestamp}"
        f":{input_tokens or 0}:{output_tokens or 0}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _resolve_costs_path() -> Path:
    """Resolve .vnx-data/events/provider_costs.ndjson via project_root.py."""
    from project_root import resolve_data_dir  # noqa: PLC0415
    data_dir = resolve_data_dir(__file__)
    return data_dir / "events" / "provider_costs.ndjson"


def emit_provider_cost(
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd_estimate: float | None,
    dispatch_id: str | None = None,
    project_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Append a provider cost NDJSON event to .vnx-data/events/provider_costs.ndjson.

    ADR-005: written before any downstream state mutation; raises on failure.
    ADR-007: project_id stamped on every event.

    Raises OSError/IOError on write failure — no silent except.

    Tenant resolution (best-effort, NOT fail-closed): the cost log is an
    append-only NDJSON receipt, not a central-DB table, so the ADR-007
    DEFAULT-ban does not bind it and it must NEVER skip an event (cost-audit =
    no data-loss). Callers that hold a store ``db_path`` (provider_dispatch,
    recovery) pass a store-derived pid explicitly; absent that, this falls back
    to the env (then ``vnx-dev``). The ``if not project_id`` trigger treats
    ``None`` and ``""`` identically.
    """
    effective_project_id = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record_id = _make_record_id(
        dispatch_id,
        timestamp,
        project_id=effective_project_id,
        event_type="provider_cost",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    # Determine billing mode from cost_usd_estimate and rate table
    if cost_usd_estimate is not None:
        billing_mode = "metered"
    else:
        rate = _lookup_rate(provider, model)
        billing_mode = "subscription" if (rate and rate[2]) else "unmetered"

    event: dict = {
        "record_id": record_id,
        "project_id": effective_project_id,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd_estimate": cost_usd_estimate,
        "billing_mode": billing_mode,
        "dispatch_id": dispatch_id,
        "timestamp": timestamp,
    }
    if metadata:
        event["metadata"] = metadata

    costs_path = _resolve_costs_path()
    costs_path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(event, separators=(",", ":")) + "\n"
    with costs_path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    logger.debug(
        "emit_provider_cost: provider=%s model=%s cost=%s dispatch=%s record=%s",
        provider, model, cost_usd_estimate, dispatch_id, record_id,
    )
