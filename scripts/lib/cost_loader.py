"""cost_loader.py — Derive per-call cost estimates from wave7_models.yaml.

Provides enrich_candidates() to fill in null cost_usd_per_call fields in
routing_recommendations.yaml candidates. Single source of truth stays in
wave7_models.yaml; this module reads it at call time so costs stay in sync.

Model names are normalized via ``providers.model_normalizer`` before the cost
lookup so that ledger-stored variants (e.g. ``openrouter/z-ai/glm-5.2``,
``glm-5.2``) resolve to the same cost as the canonical registry key.
Deprecated model variants (e.g. ``glm-4.5``, ``glm-5.1``) resolve to their
successor's cost via the ``deprecated_models`` lists in wave7_models.yaml.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from providers.model_normalizer import normalize_model_name

_log = logging.getLogger(__name__)

_WAVE7_PATH = Path(__file__).parent / "providers" / "wave7_models.yaml"

# Assumed average tokens per dispatch call for cost estimation.
_AVG_INPUT_TOKENS = 5_000
_AVG_OUTPUT_TOKENS = 2_000

# Maps model_id → (provider_key, model_key) in wave7_models.yaml.
#
# Keys are the canonical registry model key (e.g. ``glm-5.2``, ``deepseek-v4-pro``).
# Legacy dash-form keys (``glm-5-1``, ``glm-5-2``) and routing_recommendations
# model_ids are kept ADDITIVELY for backward compat — callers that haven't been
# normalized to canonical form still resolve through these entries.
#
# 2026-07-22 model-registry-refresh: routing_recommendations.yaml model_ids were bumped to
# current (claude-sonnet-4-6 -> claude-sonnet-5, glm-5-1 -> glm-5-2; claude-opus-4-6 already
# had a claude-opus-4-8 entry). The retired keys below are kept ADDITIVELY (not renamed) —
# tests/test_smart_router_cost_aware.py calls compute_cost_per_call() with these exact retired
# strings directly against the real wave7_models.yaml and must keep resolving. New keys for the
# current model_ids are added alongside so real enrichment doesn't silently break post-rename.
#
# 2026-08-02 model-ssot-en-ketenlink: the 5-series registry keys (sonnet-5 / opus-5 /
# fable-5) are now the canonical spellings; the current model_ids resolve to them, and the
# missing claude-opus-5 / claude-fable-5 cost lookups are added so a smart_router decision
# on the 5-series is not cost-blind.
#
# 2026-08-04 OI-977: canonical dot-form GLM keys added alongside legacy dash-form keys
# so that ledger-stored names (glm-5.2) resolve. normalize_model_name() is called before
# the lookup; deprecated variants (glm-4.5, glm-5.1) resolve via _deprecated_map.
#
# 2026-08-11 OI-1143: routing_recommendations carries kimi-k2-7-code (the kimi-cli
# "kimi-for-coding" model = registry key kimi_cli/kimi-k2-7) and codex-gpt-5-4 /
# codex-gpt-5-5 (the codex CLI lane running openai gpt-5.4 / gpt-5.5 — API-metered at
# $1.25/$10 per Mtok, measured 2026-06-06, see routing_policy.yaml cost reference).
# None of these had a map entry, so enrich_candidates() warned "unknown candidate model"
# and the router weighed them cost-blind (cost_usd_per_call stayed None).
_ROUTING_MODEL_MAP: dict[str, tuple[str, str]] = {
    "claude-sonnet-4-6": ("anthropic", "sonnet"),
    "claude-sonnet-5": ("anthropic", "sonnet-5"),
    "claude-opus-4-6": ("anthropic", "opus"),
    "claude-opus-4-7": ("anthropic", "opus"),
    "claude-opus-4-8": ("anthropic", "opus-4-8"),
    "claude-opus-5": ("anthropic", "opus-5"),
    "claude-fable-5": ("anthropic", "fable-5"),
    "claude-haiku-4-5": ("anthropic", "haiku"),
    "deepseek-v4-flash": ("deepseek", "deepseek-v4-flash"),
    "deepseek-v4-pro": ("deepseek", "deepseek-v4-pro"),
    # GLM — canonical dot-form keys (OI-977: what the ledger stores)
    "glm-5.2": ("zai", "glm-5.2"),
    # GLM — legacy dash-form keys (routing_recommendations / test backward compat)
    "glm-5-1": ("zai", "glm-5.2"),
    "glm-5-2": ("zai", "glm-5.2"),
    # Kimi
    "kimi-k2-0905": ("kimi_cli", "kimi-default"),
    "kimi-k2-6": ("kimi_cli", "kimi-k2-6"),
    # kimi-cli "kimi-for-coding" (registry key kimi-k2-7); kimi-k2-7-code is the
    # routing_recommendations model_id for the same model (OI-1143)
    "kimi-k2-7": ("kimi_cli", "kimi-k2-7"),
    "kimi-k2-7-code": ("kimi_cli", "kimi-k2-7"),
    # Codex CLI lane — runs openai gpt-5.4 / gpt-5.5, API-metered (OI-1143)
    "codex-gpt-5-4": ("openai", "gpt-5.4"),
    "codex-gpt-5-5": ("openai", "gpt-5.5"),
}

# Cache: deprecated model name → current canonical model key.
# Built from the deprecated_models lists in wave7_models.yaml.
_deprecated_map_cache: Optional[dict[str, str]] = None


def _load_deprecated_map(
    path: Optional[Path] = None,
) -> dict[str, str]:
    """Return {deprecated_model_name: canonical_model_key} from wave7_models.yaml.

    Reads every model's ``deprecated_models`` list and maps each entry to the
    canonical model key of its parent.  E.g. ``glm-4.5`` → ``glm-5.2``.
    """
    global _deprecated_map_cache  # noqa: PLW0603
    if _deprecated_map_cache is not None:
        return _deprecated_map_cache
    yaml_path = path or _WAVE7_PATH
    result: dict[str, str] = {}
    if not yaml_path.exists():
        _deprecated_map_cache = result
        return result
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for _provider_key, pdata in (raw.get("providers") or {}).items():
        for model_key, mdata in (pdata.get("models") or {}).items():
            for deprecated in mdata.get("deprecated_models") or []:
                dep_norm = str(deprecated).strip().lower()
                if dep_norm:
                    result.setdefault(dep_norm, model_key)
    _deprecated_map_cache = result
    return result


def _resolve_model_for_cost(
    model_id: str,
    *,
    wave7_path: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a model identifier to a canonical key for cost lookup.

    Pipeline:
      1. ``normalize_model_name()`` (handles provider-prefixed, litellm_name aliases).
      2. If the result still contains ``/``, strip the provider prefix and retry.
      3. Check the deprecated-model map from wave7_models.yaml.
      4. Return the resolved canonical key, or None if unresolvable.
    """
    # Step 1: normalize_model_name
    resolved = normalize_model_name(model_id)

    # Step 2: if the result still has a provider prefix, strip and retry
    if "/" in resolved:
        stripped = resolved.rsplit("/", 1)[-1]
        stripped_normalized = normalize_model_name(stripped)
        if stripped_normalized != stripped:
            resolved = stripped_normalized
        else:
            resolved = stripped

    # Step 3: check deprecated model map
    deprecated_map = _load_deprecated_map(wave7_path)
    if resolved.lower() in deprecated_map:
        resolved = deprecated_map[resolved.lower()]

    return resolved if resolved else None


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

    Model names are normalized via ``normalize_model_name()`` before the lookup
    so that ledger-stored variants (e.g. ``openrouter/z-ai/glm-5.2``) resolve.

    Returns None when model_id is unknown or wave7_models.yaml is absent.
    Assumes avg_input_tokens=5000 / avg_output_tokens=2000 as typical dispatch size.
    """
    wave7_costs = _load_wave7_costs(wave7_path)
    if not wave7_costs:
        return None

    # Normalize the model name to canonical form (OI-977)
    resolved = _resolve_model_for_cost(model_id, wave7_path=wave7_path)

    # Try resolved canonical name first, then original input for backward compat
    mapping = (
        _ROUTING_MODEL_MAP.get(resolved or "")
        or _ROUTING_MODEL_MAP.get(model_id)
    )
    if mapping is None:
        _log.warning(
            "cost_loader: unknown model %r (normalized: %r) — no cost assigned",
            model_id,
            resolved,
        )
        return None
    rates = wave7_costs.get(mapping)
    if rates is None:
        _log.warning(
            "cost_loader: model %r resolved to %r but no wave7 cost entry for %r",
            model_id,
            resolved,
            mapping,
        )
        return None
    inp_rate, out_rate = rates
    return (avg_input_tokens * inp_rate + avg_output_tokens * out_rate) / 1_000_000


def enrich_candidates(candidates: list, wave7_path: Optional[Path] = None) -> None:
    """Fill in cost_usd_per_call for candidates where it is None.

    Mutates the list in-place. Safe to call when wave7_models.yaml is absent —
    costs remain None and the router falls back to score-based sort.

    Model names are normalized before the cost lookup (OI-977).
    """
    wave7_costs = _load_wave7_costs(wave7_path)
    if not wave7_costs:
        return
    for candidate in candidates:
        if candidate.cost_usd_per_call is None:
            resolved = _resolve_model_for_cost(
                candidate.model_id, wave7_path=wave7_path,
            )
            mapping = (
                _ROUTING_MODEL_MAP.get(resolved or "")
                or _ROUTING_MODEL_MAP.get(candidate.model_id)
            )
            if mapping is None:
                _log.warning(
                    "cost_loader: unknown candidate model %r (normalized: %r)",
                    candidate.model_id,
                    resolved,
                )
                continue
            rates = wave7_costs.get(mapping)
            if rates is None:
                continue
            inp_rate, out_rate = rates
            candidate.cost_usd_per_call = (
                _AVG_INPUT_TOKENS * inp_rate + _AVG_OUTPUT_TOKENS * out_rate
            ) / 1_000_000
