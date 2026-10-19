"""provider_registry.py — Wave 7 model registry loader.

Reads wave7_models.yaml and exposes typed records for provider dispatch
and cost-routing. Used by provider_dispatch.py to resolve model names
without hardcoded strings.

BILLING SAFETY: read-only data loader; no Anthropic SDK, no API calls.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "wave7_models.yaml"


@dataclass(frozen=True)
class PriceVersion:
    """One dated price version for a model (ADR-040).

    A model's price can move over time; each movement is a distinct, dated
    version. ``date`` is the measurement/effective date (YYYY-MM-DD). Rates are
    per million tokens (Mtok). ``off_peak_*`` are only set for providers with a
    peak/off-peak split (DeepSeek); the split's window definition lives with the
    pricing logic, not the registry (it is a provider fact, not a per-version
    fact). ``input_per_mtok``/``output_per_mtok`` are the peak (upper-bound)
    rates where a split exists, matching the registry's "book the upper bound,
    never under-report" convention.
    """

    date: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: Optional[float] = None
    cache_write_per_mtok: Optional[float] = None
    source: str = ""
    off_peak_input_per_mtok: Optional[float] = None
    off_peak_output_per_mtok: Optional[float] = None


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
    # Price per Mtok of prompt tokens served from the provider's cache. None means
    # the registry carries no cache-read price: cache-read tokens are then not billed
    # by the cost calculation (unchanged behaviour for every model without it).
    cost_cache_read_per_mtok: Optional[float] = None
    # Price per Mtok of prompt tokens written to the provider's cache. None means
    # the registry carries no cache-write price (unchanged behaviour).
    cost_cache_write_per_mtok: Optional[float] = None
    # Dated price versions (ADR-040). This is the authoritative price record:
    # price_run() and alias resolution read it. The flat cost_* fields above are
    # the "current" projection of the latest version, kept for the pre-existing
    # read surface (provider_dispatch, cost_loader, the smart router). When the
    # registry carries price_versions, the flat fields are authored equal to the
    # latest version and guarded by a drift test.
    price_versions: List["PriceVersion"] = field(default_factory=list)


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


def _parse_price_version(data: dict, model_key: Optional[str] = None) -> PriceVersion:
    """Parse one dated price version entry (ADR-040).

    Fails loud (naming the model) on a malformed date or a missing/NaN rate —
    a price version without a date is unversioned, and a version without a rate
    is not a price.
    """
    date_raw = data.get("date")
    date_str = str(date_raw) if date_raw is not None else ""
    if not date_str:
        raise ValueError(
            f"model {model_key!r} has a price_version without a 'date' "
            f"(expected YYYY-MM-DD) — every price version must be dated"
        )
    _validate_price_checked_at(
        model_key or "<unknown model>",
        date_str,
    )
    try:
        input_per_mtok = float(data["input_per_mtok"])
        output_per_mtok = float(data["output_per_mtok"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"model {model_key!r} has a price_version with an invalid rate "
            f"({e}); expected numeric input_per_mtok/output_per_mtok"
        ) from e
    cache_read_raw = data.get("cache_read_per_mtok")
    cache_write_raw = data.get("cache_write_per_mtok")
    off_peak_in_raw = data.get("off_peak_input_per_mtok")
    off_peak_out_raw = data.get("off_peak_output_per_mtok")

    def _opt_float(raw, field_name: str) -> Optional[float]:
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"model {model_key!r} has an invalid {field_name} "
                f"{raw!r} in its price_version: {e}"
            ) from e

    return PriceVersion(
        date=date_str,
        input_per_mtok=input_per_mtok,
        output_per_mtok=output_per_mtok,
        cache_read_per_mtok=_opt_float(cache_read_raw, "cache_read_per_mtok"),
        cache_write_per_mtok=_opt_float(cache_write_raw, "cache_write_per_mtok"),
        source=str(data.get("source") or ""),
        off_peak_input_per_mtok=_opt_float(off_peak_in_raw, "off_peak_input_per_mtok"),
        off_peak_output_per_mtok=_opt_float(off_peak_out_raw, "off_peak_output_per_mtok"),
    )


def _parse_model(data: dict, model_key: Optional[str] = None) -> ProviderModel:
    context_window_raw = data.get("context_window")
    cli_model_arg_raw = data.get("cli_model_arg")
    dispatch_allowed_raw = data.get("dispatch_allowed")
    price_source_raw = data.get("price_source")
    price_checked_at_raw = data.get("price_checked_at")
    cache_read_raw = data.get("cost_cache_read_per_mtok")
    cache_write_raw = data.get("cost_cache_write_per_mtok")
    price_checked_at = str(price_checked_at_raw) if price_checked_at_raw is not None else ""
    if price_checked_at:
        _validate_price_checked_at(
            model_key or str(data.get("litellm_name") or "<unknown model>"),
            price_checked_at,
        )
    price_versions = sorted(
        (
            _parse_price_version(pv, model_key)
            for pv in (data.get("price_versions") or [])
        ),
        key=lambda pv: pv.date,
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
        cost_cache_read_per_mtok=float(cache_read_raw) if cache_read_raw is not None else None,
        cost_cache_write_per_mtok=float(cache_write_raw) if cache_write_raw is not None else None,
        price_versions=price_versions,
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


# Registry-section-specific model-name prefix a real provider model slug carries
# but the registry's own keys do not — e.g. Anthropic hands back "claude-opus-5"
# while wave7_models.yaml's anthropic section keys it "opus-5". Only anthropic
# needs this today (the same table provider_dispatch used before ADR-040 moved
# the price resolution here).
_REGISTRY_KEY_MODEL_PREFIX: Dict[str, str] = {
    "anthropic": "claude-",
}


def _resolve_model_key(
    cfg: ProviderConfig,
    registry_key: str,
    model: str,
) -> Optional[str]:
    """Deterministically resolve a model string to a registry model key.

    Mirrors the three-tier resolution provider_dispatch used for pricing
    (OI-1355): exact key on the slash-stripped name, then exact key after
    stripping the section's known name-prefix, then the single longest key that
    hyphen-bounds-prefixes the stripped name. Returns None on a total miss —
    a miss is a valid signal (the caller reports unavailable, never fabricates
    a price).
    """
    model_key = model.split("/")[-1] if "/" in model else model
    if model_key in cfg.models:
        return model_key

    prefix = _REGISTRY_KEY_MODEL_PREFIX.get(registry_key)
    stripped = model_key[len(prefix):] if prefix and model_key.startswith(prefix) else model_key
    if stripped != model_key and stripped in cfg.models:
        return stripped

    candidates = sorted(
        (k for k in cfg.models if stripped.startswith(k + "-")),
        key=len,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    return None


def resolve_price_versions(
    registry_key: str,
    model: str,
    registry_path: Optional[Path] = None,
) -> Optional[List["PriceVersion"]]:
    """Return the dated price versions for a model, or None when it has no price.

    ``registry_key`` is a registry section key (e.g. ``"anthropic"``,
    ``"deepseek_harness"``), ``model`` any model string the caller carries
    (bare key, provider-prefixed slug, slash-qualified litellm name). The model
    is resolved via :func:`_resolve_model_key` and its ``price_versions`` list
    returned. A model that exists but carries no price (and no price_versions)
    yields None.

    This is the single resolution point price_run() and alias-resolution tests
    share, so "every alias of a model resolves to the same price versions" is
    one place to reason about.
    """
    registry = load(registry_path)
    cfg = registry.get(registry_key)
    if cfg is None or not cfg.models:
        return None
    model_key = _resolve_model_key(cfg, registry_key, model)
    if model_key is None:
        return None
    entry = cfg.models[model_key]
    versions = entry.price_versions
    if versions:
        return versions
    # Backward-compat: a fixture/entry without price_versions still carries a
    # flat price — project it as a single version so price_run sees one path.
    if entry.cost_input_per_mtok is None:
        return None
    return [
        PriceVersion(
            date=entry.price_checked_at or "",
            input_per_mtok=entry.cost_input_per_mtok,
            output_per_mtok=entry.cost_output_per_mtok,
            cache_read_per_mtok=entry.cost_cache_read_per_mtok,
            cache_write_per_mtok=entry.cost_cache_write_per_mtok,
            source=entry.price_source,
        )
    ]


# Written where a seat or roster entry means "whatever openai.default_model says":
# a template names no codex model of its own, the builder swaps this for the
# registry value when the panel is built (OI-2062).
CODEX_REGISTRY_DEFAULT = "registry-default"


class CodexModelResolutionError(ValueError):
    """The codex model could not be resolved from the registry.

    Raised instead of ever substituting a hard-coded model name: a name written
    into code is exactly what codex refused on 14-10 (OI-2062).
    """


def resolve_codex_model(
    explicit: Optional[str] = None,
    env_names: tuple = (),
    registry_path: Optional[Path] = None,
) -> str:
    """Return the model every codex launch path passes to the codex CLI.

    Order: *explicit* model, then the first non-empty env var in *env_names*
    (each path keeps its own names and precedence), then ``openai.default_model``
    from wave7_models.yaml. Raises :class:`CodexModelResolutionError` when the
    registry is unreadable, holds a model row it cannot parse, has no ``openai``
    section, carries no ``default_model``, or names a default that is not one of
    its models.

    A row is parsed by :func:`_parse_model` for every provider section, so one
    malformed row anywhere stops the resolution: a missing field raises
    ``KeyError``, a null where a number belongs ``TypeError``, a bad number
    ``ValueError``, a row that is no mapping ``AttributeError``. Each of them is
    this error, never a raise past it: a gate run that met one stayed
    ``executing`` with no result (OI-2068).
    """
    requested = (explicit or "").strip()
    if requested:
        return requested
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    try:
        registry = load(path)
    except (OSError, yaml.YAMLError) as exc:
        raise CodexModelResolutionError(
            f"codex model: registry {path} is unreadable ({exc}); "
            "no model given and no hard-coded fallback exists"
        ) from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CodexModelResolutionError(
            f"codex model: registry {path} holds an entry it cannot parse "
            f"({type(exc).__name__}: {exc}); no model given and no hard-coded fallback exists"
        ) from exc
    cfg = registry.get("openai")
    default_key = (getattr(cfg, "default_model", None) or "").strip()
    if not default_key:
        raise CodexModelResolutionError(
            f"codex model: registry {path} carries no openai.default_model; "
            "no model given and no hard-coded fallback exists"
        )
    if default_key not in cfg.models:
        raise CodexModelResolutionError(
            f"codex model: openai.default_model {default_key!r} in {path} "
            "is not one of the registered openai models"
        )
    return default_key


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


def load_escalation_order(registry_path: Optional[Path] = None) -> List[str]:
    """Resolve the quality-escalation climb order (OI-1356) from the registry.

    ``routing.escalation_order`` is a separate, explicitly authored list from
    ``routing.tier_map`` (the cost ladder): tier_map stays purely cost-ordered
    (``load_tier_ladder``'s invariants are untouched by this function), while
    escalation_order is the list ``tier_routing.next_tier`` reads for climb
    destinations. A tier absent from escalation_order (e.g. kimi-k3, gpt-5.5)
    is still a valid tier_map rung — dispatchable, fallback-eligible — just not
    a climb destination.

    Fails loud (RegistryLookupError) when:
      * ``routing.escalation_order`` is missing entirely — silently falling
        back to the cost ladder would resurrect the exact coupling this key
        exists to break (a climb landing on a cheaper-but-different-lane
        rung instead of a genuinely more-capable one). A registry that has
        not been updated for OI-1356 must say so at load, not silently keep
        the old behavior.
      * a listed member is not a key in ``routing.tier_map`` — an escalation
        destination that is not even a valid tier is drift, not a typo to
        paper over.
    """
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise RegistryLookupError(f"registry file not found at {path}") from None
    except yaml.YAMLError as exc:
        raise RegistryLookupError(f"malformed registry yaml at {path}: {exc}") from exc

    routing = raw.get("routing") or {}
    if "escalation_order" not in routing:
        raise RegistryLookupError(
            f"routing.escalation_order is missing from {path}; the quality-"
            "escalation climb order must be authored explicitly, separate "
            "from routing.tier_map (OI-1356) — add the key to this registry"
        )
    escalation_order = routing.get("escalation_order") or []
    known_tiers = set((routing.get("tier_map") or {}).keys())
    for tier in escalation_order:
        if tier not in known_tiers:
            raise RegistryLookupError(
                f"routing.escalation_order member {tier!r} is not a key in "
                f"routing.tier_map in {path}"
            )
    return [str(tier) for tier in escalation_order]


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
