"""cost_loader.py — Derive per-call cost estimates from wave7_models.yaml.

Provides enrich_candidates() to fill in null cost_usd_per_call fields in
routing_recommendations.yaml candidates. Single source of truth stays in
wave7_models.yaml; this module reads it at call time so costs stay in sync.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

_WAVE7_PATH = Path(__file__).parent / "providers" / "wave7_models.yaml"

# Assumed average tokens per dispatch call for cost estimation.
_AVG_INPUT_TOKENS = 5_000
_AVG_OUTPUT_TOKENS = 2_000

# Maps routing_recommendations model_id → (provider_key, model_key) in wave7_models.yaml.
#
# 2026-07-22 model-registry-refresh: routing_recommendations.yaml model_ids were bumped to
# current (claude-sonnet-4-6 -> claude-sonnet-5, glm-5-1 -> glm-5-2; claude-opus-4-6 already
# had a claude-opus-4-8 entry). The retired keys below are kept ADDITIVELY (not renamed) —
# tests/test_smart_router_cost_aware.py calls compute_cost_per_call() with these exact retired
# strings directly against the real wave7_models.yaml and must keep resolving. New keys for the
# current model_ids are added alongside so real enrichment doesn't silently break post-rename.
_ROUTING_MODEL_MAP: dict[str, tuple[str, str]] = {
    "claude-sonnet-4-6": ("anthropic", "sonnet"),
    "claude-sonnet-5": ("anthropic", "sonnet"),
    "claude-opus-4-6": ("anthropic", "opus"),
    "claude-opus-4-7": ("anthropic", "opus"),
    "claude-opus-4-8": ("anthropic", "opus"),
    "claude-haiku-4-5": ("anthropic", "haiku"),
    "deepseek-v4-flash": ("deepseek", "deepseek-v4-flash"),
    "deepseek-v4-pro": ("deepseek", "deepseek-v4-pro"),
    "glm-5-1": ("zai", "glm-5.1-default"),
    "glm-5-2": ("zai", "glm-5.2"),
    "kimi-k2-0905": ("kimi_cli", "kimi-default"),
    "kimi-k2-6": ("kimi_cli", "kimi-k2-6"),
}


def _load_wave7_costs(
    path: Optional[Path] = None,
) -> dict[tuple[str, str], tuple[float, float]]:
    """Return {(provider, model_key): (input_per_mtok, output_per_mtok)} from wave7_models.yaml.

    Returns an empty dict when the file is absent (safe — callers treat missing cost as None).
    """
    yaml_path = path or _WAVE7_PATH
    if not yaml_path.exists():
        return {}
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], tuple[float, float]] = {}
    for provider_key, pdata in (raw.get("providers") or {}).items():
        for model_key, mdata in (pdata.get("models") or {}).items():
            inp = mdata.get("cost_input_per_mtok")
            out = mdata.get("cost_output_per_mtok")
            if inp is not None and out is not None:
                result[(provider_key, model_key)] = (float(inp), float(out))
    return result


def compute_cost_per_call(
    model_id: str,
    *,
    avg_input_tokens: int = _AVG_INPUT_TOKENS,
    avg_output_tokens: int = _AVG_OUTPUT_TOKENS,
    wave7_path: Optional[Path] = None,
) -> Optional[float]:
    """Return estimated USD cost per dispatch call for model_id using wave7 rates.

    Returns None when model_id is unknown or wave7_models.yaml is absent.
    Assumes avg_input_tokens=5000 / avg_output_tokens=2000 as typical dispatch size.
    """
    wave7_costs = _load_wave7_costs(wave7_path)
    if not wave7_costs:
        return None
    mapping = _ROUTING_MODEL_MAP.get(model_id)
    if mapping is None:
        return None
    rates = wave7_costs.get(mapping)
    if rates is None:
        return None
    inp_rate, out_rate = rates
    return (avg_input_tokens * inp_rate + avg_output_tokens * out_rate) / 1_000_000


def enrich_candidates(candidates: list, wave7_path: Optional[Path] = None) -> None:
    """Fill in cost_usd_per_call for candidates where it is None.

    Mutates the list in-place. Safe to call when wave7_models.yaml is absent —
    costs remain None and the router falls back to score-based sort.
    """
    wave7_costs = _load_wave7_costs(wave7_path)
    if not wave7_costs:
        return
    for candidate in candidates:
        if candidate.cost_usd_per_call is None:
            mapping = _ROUTING_MODEL_MAP.get(candidate.model_id)
            if mapping is None:
                continue
            rates = wave7_costs.get(mapping)
            if rates is None:
                continue
            inp_rate, out_rate = rates
            candidate.cost_usd_per_call = (
                _AVG_INPUT_TOKENS * inp_rate + _AVG_OUTPUT_TOKENS * out_rate
            ) / 1_000_000
