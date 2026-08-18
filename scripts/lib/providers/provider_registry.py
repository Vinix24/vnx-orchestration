"""provider_registry.py — Wave 7 model registry loader.

Reads wave7_models.yaml and exposes typed records for provider dispatch
and cost-routing. Used by provider_dispatch.py to resolve model names
without hardcoded strings.

BILLING SAFETY: read-only data loader; no Anthropic SDK, no API calls.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "wave7_models.yaml"


@dataclass
class ProviderModel:
    litellm_name: str
    cost_input_per_mtok: float
    cost_output_per_mtok: float
    max_tokens: int
    supports_streaming: bool
    supports_tool_calls: bool
    price_source: str = ""
    price_checked_at: str = ""
    context_window: Optional[int] = None
    task_classes: List[str] = field(default_factory=list)
    cli_model_arg: Optional[str] = None
    dispatch_allowed: bool = True


@dataclass
class ProviderConfig:
    enabled: bool
    api_key_env: str
    models: Dict[str, ProviderModel] = field(default_factory=dict)
    default_model: Optional[str] = None
    # dispatch_enum: the dispatch Provider enum value (dispatch_spec.Provider)
    # this registry section routes to. Sourced from YAML (ADR-036); absent for
    # sections the static router never emits (deepseek litellm-proxy, moonshot,
    # zai, google).
    dispatch_enum: Optional[str] = None


class RegistryLookupError(LookupError):
    """A provider or model key was not found in wave7_models.yaml.

    Raised on the fail-loud routing path (ADR-036 §2): the message names what
    was missing and where it was looked for (the registry path). It is
    intentionally NOT a silent None — a provider/model the registry does not
    know is drift between the router and the registry, and must surface.
    """


@dataclass(frozen=True)
class TierRouteStep:
    """One resolved step in a tier's route chain (primary or fallback).

    ``provider`` is the dispatch enum value (dispatch_spec.Provider), derived
    from the registry section's ``dispatch_enum`` — NOT the registry section
    key. ``model`` is the registry model key (validated to exist). ``lane`` is
    the dispatch lane name.
    """

    provider: str
    model: str
    lane: str


@dataclass(frozen=True)
class TierRouteSpec:
    """Resolved route chain for one cost tier (primary + ordered fallbacks)."""

    tier: str
    provider: str
    model: str
    lane: str
    fallback: tuple = ()  # tuple[TierRouteStep, ...]


@dataclass(frozen=True)
class TierLadderRung:
    """One rung of the cost-ordered escalation ladder.

    Carries the resolved primary (provider/model/lane) plus the primary model's
    output cost read from the registry, so the ladder's ordering is derived from
    the registry's own prices — never a Python literal (ADR-036).
    """

    tier: str
    provider: str
    model: str
    lane: str
    output_cost_per_mtok: float
    fallback: tuple = ()  # tuple[TierRouteStep, ...]


def _validate_price_checked_at(model_key: str, price_checked_at: str) -> None:
    """Fail loud, naming the model and the field, on a malformed price_checked_at.

    A bare ``date.fromisoformat`` ValueError names neither — the raised
    message here does, so a broken date is diagnosable from the traceback
    alone (OI-1334).
    """
    try:
        date.fromisoformat(price_checked_at)
    except ValueError as e:
        raise ValueError(
            f"model {model_key!r} has an invalid price_checked_at "
            f"{price_checked_at!r} (expected YYYY-MM-DD): {e}"
        ) from e


def _parse_model(data: dict, model_key: Optional[str] = None) -> ProviderModel:
    context_window_raw = data.get("context_window")
    cli_model_arg_raw = data.get("cli_model_arg")
    dispatch_allowed_raw = data.get("dispatch_allowed")
    price_source_raw = data.get("price_source")
    price_checked_at_raw = data.get("price_checked_at")
    price_checked_at = str(price_checked_at_raw) if price_checked_at_raw is not None else ""
    if price_checked_at:
        _validate_price_checked_at(
            model_key or str(data.get("litellm_name") or "<unknown model>"),
            price_checked_at,
        )
    return ProviderModel(
        litellm_name=str(data["litellm_name"]),
        cost_input_per_mtok=float(data["cost_input_per_mtok"]),
        cost_output_per_mtok=float(data["cost_output_per_mtok"]),
        max_tokens=int(data["max_tokens"]),
        supports_streaming=bool(data["supports_streaming"]),
        supports_tool_calls=bool(data["supports_tool_calls"]),
        price_source=str(price_source_raw) if price_source_raw is not None else "",
        price_checked_at=price_checked_at,
        context_window=int(context_window_raw) if context_window_raw is not None else None,
        task_classes=list(data.get("task_classes") or []),
        cli_model_arg=str(cli_model_arg_raw) if cli_model_arg_raw is not None else None,
        dispatch_allowed=bool(dispatch_allowed_raw) if dispatch_allowed_raw is not None else True,
    )


def _parse_provider(data: dict) -> ProviderConfig:
    models: Dict[str, ProviderModel] = {}
    for model_key, model_data in (data.get("models") or {}).items():
        models[model_key] = _parse_model(model_data, model_key)
    default_model_raw = data.get("default_model")
    dispatch_enum_raw = data.get("dispatch_enum")
    return ProviderConfig(
        enabled=bool(data.get("enabled", False)),
        api_key_env=str(data.get("api_key_env") or ""),
        models=models,
        default_model=str(default_model_raw) if default_model_raw is not None else None,
        dispatch_enum=str(dispatch_enum_raw) if dispatch_enum_raw is not None else None,
    )


def load(registry_path: Optional[Path] = None) -> Dict[str, ProviderConfig]:
    """Parse wave7_models.yaml and return a dict of provider configs.

    Raises FileNotFoundError when registry_path does not exist.
    Raises yaml.YAMLError on malformed YAML.
    """
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    result: Dict[str, ProviderConfig] = {}
    for provider_key, provider_data in (raw or {}).get("providers", {}).items():
        result[str(provider_key)] = _parse_provider(provider_data or {})
    return result


def get_default_model(
    sub_provider: str,
    registry_path: Optional[Path] = None,
) -> Optional[ProviderModel]:
    """Return the first model entry for *sub_provider*, or None if not found/disabled."""
    try:
        registry = load(registry_path)
    except FileNotFoundError as e:
        log.error("provider_registry: file missing at %s: %s", registry_path or _REGISTRY_PATH, e)
        raise
    except yaml.YAMLError as e:
        log.error("provider_registry: malformed yaml at %s: %s", registry_path or _REGISTRY_PATH, e)
        raise ValueError(f"malformed wave7_models.yaml: {e}") from e
    cfg = registry.get(sub_provider)
    if cfg is None or not cfg.enabled or not cfg.models:
        return None
    return next(iter(cfg.models.values()))


def _resolve_step(
    entry: dict,
    providers: Dict[str, ProviderConfig],
    path: Path,
    where: str,
) -> TierRouteStep:
    """Resolve one tier-map step (primary or fallback) against the registry.

    Validates the provider key exists and is enabled, carries a dispatch_enum,
    and the model key exists and is dispatch_allowed. Any gap raises
    RegistryLookupError naming what+where (ADR-036 §2).
    """
    provider_key = entry.get("provider")
    model_key = entry.get("model")
    lane = entry.get("lane")
    cfg = providers.get(provider_key)
    if cfg is None:
        raise RegistryLookupError(
            f"provider {provider_key!r} referenced by {where} is not in the registry "
            f"(checked {path})"
        )
    if not cfg.enabled:
        raise RegistryLookupError(
            f"provider {provider_key!r} referenced by {where} is disabled in {path}"
        )
    if not cfg.dispatch_enum:
        raise RegistryLookupError(
            f"provider {provider_key!r} referenced by {where} has no dispatch_enum "
            f"in {path}; add a dispatch_enum to the {provider_key} section"
        )
    model = cfg.models.get(model_key)
    if model is None:
        raise RegistryLookupError(
            f"model {model_key!r} referenced by {where} is not under provider "
            f"{provider_key!r} in {path}"
        )
    if not model.dispatch_allowed:
        raise RegistryLookupError(
            f"model {model_key!r} under {provider_key!r} (referenced by {where}) is "
            f"dispatch_allowed=false in {path}"
        )
    return TierRouteStep(provider=cfg.dispatch_enum, model=model_key, lane=lane)


def load_tier_map(registry_path: Optional[Path] = None) -> Dict[str, TierRouteSpec]:
    """Resolve the static router's tier map from the registry (ADR-036).

    Reads the top-level ``routing.tier_map`` block and resolves every referenced
    provider/model against the ``providers`` sections. Raises RegistryLookupError
    (naming what+where) on any unknown provider key, missing dispatch_enum, or
    unknown/disabled model key — never a silent None. This is the fail-loud half
    of ADR-036: a malformed registry fails at load, before any dispatch reaches
    the lookup.
    """
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise RegistryLookupError(f"registry file not found at {path}") from None
    except yaml.YAMLError as exc:
        raise RegistryLookupError(f"malformed registry yaml at {path}: {exc}") from exc

    providers = load(path)
    tier_map = ((raw.get("routing") or {}).get("tier_map")) or {}
    result: Dict[str, TierRouteSpec] = {}
    for tier, entry in tier_map.items():
        if not isinstance(entry, dict):
            raise RegistryLookupError(
                f"routing.tier_map[{tier!r}] is not a mapping in {path}"
            )
        where = f"routing.tier_map[{tier!r}]"
        primary = _resolve_step(entry, providers, path, where)
        fallback: tuple = ()
        raw_fallbacks = entry.get("fallback") or []
        steps = []
        for idx, fb in enumerate(raw_fallbacks):
            steps.append(
                _resolve_step(fb, providers, path, f"{where}.fallback[{idx}]")
            )
        fallback = tuple(steps)
        result[str(tier)] = TierRouteSpec(
            tier=str(tier),
            provider=primary.provider,
            model=primary.model,
            lane=primary.lane,
            fallback=fallback,
        )
    return result


def _output_cost_for(
    spec: TierRouteSpec,
    providers: Dict[str, ProviderConfig],
    path: Path,
) -> float:
    """Resolve a tier's primary model output cost ($/Mtok) from the registry.

    ``spec.provider`` is the dispatch enum (e.g. "claude"), not the registry
    section key (e.g. "anthropic"); walk the sections by their dispatch_enum to
    find the model and read its registered price. Fails loud when the model is
    not found — the ladder must never invent a price.
    """
    for cfg in providers.values():
        if cfg.dispatch_enum != spec.provider:
            continue
        model = cfg.models.get(spec.model)
        if model is not None:
            return model.cost_output_per_mtok
    raise RegistryLookupError(
        f"cannot resolve output cost for {spec.provider!r}/{spec.model!r} "
        f"(tier {spec.tier!r}) in {path}"
    )


def load_tier_ladder(registry_path: Optional[Path] = None) -> List[TierLadderRung]:
    """Return the escalation ladder in the tier_map's authored order, validated.

    The ladder is DERIVED from the registry (ADR-036): each tier's primary model
    cost is read from wave7_models.yaml — never a Python literal. The ``routing.
    tier_map`` block is itself the ladder: its authored order IS the climb order
    (tier-zero first, tier-high last), and two invariants are enforced fail-loud
    so a drift that would make escalation a no-op surfaces at load, not silently:

      1. no two tiers may resolve to the same primary (provider, model) — a
         duplicate rung means tier_from + 1 changes nothing;
      2. primary cost must be strictly increasing along the authored order — a
         higher rung must actually be more expensive, otherwise "escalation" is
         a price cut in disguise.
    """
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    tier_map = load_tier_map(path)
    providers = load(path)
    rungs: List[TierLadderRung] = []
    seen = set()
    for spec in tier_map.values():
        key = (spec.provider, spec.model)
        if key in seen:
            raise RegistryLookupError(
                f"routing.tier_map has duplicate rungs: {spec.provider!r}/"
                f"{spec.model!r} appears in more than one tier ({path}); merge "
                "them or give them a real difference"
            )
        seen.add(key)
        rungs.append(
            TierLadderRung(
                tier=spec.tier,
                provider=spec.provider,
                model=spec.model,
                lane=spec.lane,
                output_cost_per_mtok=_output_cost_for(spec, providers, path),
                fallback=spec.fallback,
            )
        )
    for lo, hi in zip(rungs, rungs[1:]):
        if not (lo.output_cost_per_mtok < hi.output_cost_per_mtok):
            raise RegistryLookupError(
                f"routing.tier_map is not a strict cost ladder: {hi.tier!r} "
                f"({hi.model} @ {hi.output_cost_per_mtok}) is not more expensive "
                f"than {lo.tier!r} ({lo.model} @ {lo.output_cost_per_mtok}) in {path}"
            )
    return rungs
