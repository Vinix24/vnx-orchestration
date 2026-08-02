"""model_normalizer.py — canonical model-name SSOT resolver.

One place that maps every incoming model variant to the canonical registry key
from ``wave7_models.yaml`` (the single source of truth for model identity).
Receipt writers and routing code call ``normalize_model_name()`` instead of
carrying their own spelling, so the same model can no longer land in the
ledger under three names.

Canonical name = the registry key (e.g. ``opus-5``, ``sonnet-5``,
``deepseek-v4-pro``, ``kimi-k3``). Variants are matched against registry keys,
``litellm_name``, ``cli_model_arg``, and their provider-prefix-stripped forms:

  deepseek/deepseek-v4-pro      -> deepseek-v4-pro
  moonshot/kimi-k2-0905-preview -> kimi-k2-0905-default
  kimi-code/k3                  -> kimi-k3
  claude-opus-4-8               -> opus-4-8
  claude-opus-5                 -> opus-5
  claude-sonnet-5               -> sonnet-5
  claude-fable-5                -> fable-5

Unknown/unmapped strings pass through unchanged — the caller decides how to
treat them (receipt validation fails closed on them; routing rejects them
with model-not-in-current-registry).

``tier_for_model()`` is the deterministic reverse map of
``tier_routing.resolve_tier_route``: model -> cost tier. It is the
escalation signal that stamps ``tier_to`` on a receipt when the spec did not
carry one.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

_LIB_DIR_RE = __file__  # pragma: no cover — kept for symmetry with sibling modules

_UNKNOWN_SENTINELS = frozenset({"", "unknown", "null", "none", "n/a", "na", "unset", "-"})

# Registry keys that carry a version suffix ("opus-4-8", "sonnet-5") are the
# canonical spelling over their unversioned alias ("opus", "sonnet"): the
# versioned key names exactly one model generation, the unversioned one names
# a moving default.
_VERSIONED_KEY_RE = re.compile(r"-\d")

# Explicit aliases the registry cannot derive from litellm_name/cli_model_arg
# (common shorthand the operator uses, or dated-suffix variants):
#   - "fable" is the operator's shorthand for claude-fable-5 (only "fable-5"
#     exists as a key; a bare "fable" is not otherwise resolvable).
#   - "claude-haiku-4-5" is the undated alias of
#     anthropic/claude-haiku-4-5-20251001 (the dated full id is the only
#     litellm_name in the registry).
_EXPLICIT_ALIASES: Dict[str, str] = {
    "fable": "fable-5",
    "claude-haiku-4-5": "haiku",
    "claude-fable-5": "fable-5",
}

_registry_cache: Optional[Dict] = None
_alias_cache: Optional[Dict[str, str]] = None
_keys_cache: Optional[Dict[str, str]] = None


def _load_registry() -> Dict:
    """Lazy-load the wave7 registry (provider_registry.load)."""
    global _registry_cache  # noqa: PLW0603
    if _registry_cache is None:
        from providers import provider_registry  # noqa: PLC0415
        _registry_cache = provider_registry.load()
    return _registry_cache


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _best_canonical_key(alias: str, candidates: list) -> str:
    """Pick the canonical key for an alias from a list of candidate keys.

    Deterministic preference:
      1. the alias itself when it is already a key,
      2. a versioned key over an unversioned one (``opus-4-8`` beats ``opus``),
      3. lexicographically smallest (stable tiebreak).
    """
    if alias in candidates:
        return alias
    versioned = [c for c in candidates if _VERSIONED_KEY_RE.search(c)]
    pool = versioned if versioned else candidates
    return min(pool)


def _build_alias_map() -> Dict[str, str]:
    """Build alias -> canonical registry key from the loaded registry.

    For every provider/model entry, these aliases resolve to the model key:
      - the registry key itself,
      - the full ``litellm_name`` and ``cli_model_arg`` values,
      - the part of ``litellm_name`` after the last ``/`` (so
        ``deepseek/deepseek-v4-pro`` and ``anthropic/claude-opus-4-8`` resolve
        without knowing the provider prefix).
    """
    alias_sets: Dict[str, list] = {}
    for cfg in _load_registry().values():
        for key, entry in (getattr(cfg, "models", None) or {}).items():
            key_norm = _norm(key)
            if not key_norm:
                continue
            aliases = {key_norm}
            litellm = _norm(getattr(entry, "litellm_name", "") or "")
            if litellm:
                aliases.add(litellm)
                if "/" in litellm:
                    aliases.add(litellm.rsplit("/", 1)[-1])
            cli_arg = _norm(getattr(entry, "cli_model_arg", "") or "")
            if cli_arg:
                aliases.add(cli_arg)
            for alias in aliases:
                alias_sets.setdefault(alias, []).append(key_norm)

    result: Dict[str, str] = {}
    for alias, candidates in alias_sets.items():
        result[alias] = _best_canonical_key(alias, candidates)
    result.update(_EXPLICIT_ALIASES)
    return result


def _alias_map() -> Dict[str, str]:
    global _alias_cache  # noqa: PLW0603
    if _alias_cache is None:
        _alias_cache = _build_alias_map()
    return _alias_cache


def _key_map() -> Dict[str, str]:
    """Registry key -> key lookup (case-insensitive)."""
    global _keys_cache  # noqa: PLW0603
    if _keys_cache is None:
        keys: Dict[str, str] = {}
        for cfg in _load_registry().values():
            for key in (getattr(cfg, "models", None) or {}).keys():
                keys.setdefault(_norm(key), key)
        _keys_cache = keys
    return _keys_cache


def canonical_model_names() -> set:
    """The set of canonical registry keys (case-insensitive)."""
    return set(_key_map().values()) | set(_alias_map().values())


def normalize_model_name(model: Optional[str]) -> str:
    """Map a model variant to its canonical registry key.

    Returns the input unchanged (trimmed) when it cannot be resolved — an
    unknown model string is the caller's problem to reject, never silently
    rewritten here.
    """
    if not model:
        return ""
    raw = str(model).strip()
    norm = _norm(raw)
    if not norm or norm in _UNKNOWN_SENTINELS:
        return raw

    keys = _key_map()
    if norm in keys:
        return keys[norm]

    aliases = _alias_map()
    if norm in aliases:
        return aliases[norm]

    # Provider-prefixed form the registry does not carry verbatim: retry on the
    # part after the last "/" (mirrors constraint_enforcer._model_aliases).
    if "/" in norm:
        stripped = norm.rsplit("/", 1)[-1]
        if stripped in keys:
            return keys[stripped]
        if stripped in aliases:
            return aliases[stripped]

    return raw


def is_unknown_model(model: Optional[str]) -> bool:
    """True when the model is absent or an explicit unknown sentinel.

    Receipt validation uses this to fail closed on a worker receipt that does
    not name a real model (``model: unknown`` is the current silent gap).
    """
    if not model:
        return True
    return _norm(str(model)) in _UNKNOWN_SENTINELS


def tier_for_model(model: Optional[str]) -> Optional[str]:
    """Reverse-map a model to its cost tier (deterministic).

    Mirrors ``tier_routing.resolve_tier_route``: tier-high <- fable/opus,
    tier-mid <- sonnet, tier-low <- deepseek/kimi, tier-zero <- local gemma.
    Returns None for an unresolvable model string.
    """
    norm = _norm(model)
    if not norm or norm in _UNKNOWN_SENTINELS:
        return None
    if any(token in norm for token in ("fable", "opus")):
        return "tier-high"
    if "sonnet" in norm:
        return "tier-mid"
    if any(token in norm for token in ("deepseek", "kimi")):
        return "tier-low"
    if any(token in norm for token in ("gemma", "ollama")):
        return "tier-zero"
    return None


__all__ = [
    "canonical_model_names",
    "is_unknown_model",
    "normalize_model_name",
    "tier_for_model",
]
