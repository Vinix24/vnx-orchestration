"""door_routing.py — Smart-router integration for the single-entry dispatch door.

Called from dispatch_cli.run_dispatch() when the spec carries no explicit
provider (provider=AUTO). Resolves the dispatch to a concrete provider + model
via the cost-tier classifier and tier-routing engine, then hands back a
Provider enum + model string that compile_plan can consume.

Fail-open by design: a broken router returns None and the door falls through
to the existing behaviour. T0 is never routed (t0-opus-only is a floor, not
an advisory). The router is default-on since 2026-08-02.

Constraints:
- No imports from dispatch_cli or dispatch_plan (avoid circular deps).
- No I/O beyond what the smart_router submodules already do.
- Pure decision function — never mutates, never raises.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Optional, Tuple, Union

from dispatch_spec import Provider, ValidatedSpec  # noqa: PLC0415 — sibling package, no cycle

logger = logging.getLogger(__name__)


# TierRoute.provider string -> Provider enum.
# The tier-routing engine uses short provider strings; the door's compile_plan
# needs a Provider enum. This mapping is the canonical translation layer.
_TIER_PROVIDER_TO_ENUM: dict[str, Provider] = {
    "deepseek": Provider.DEEPSEEK_HARNESS,
    "codex": Provider.CODEX,
    "kimi": Provider.KIMI,
    "claude": Provider.CLAUDE,
    "local-gemma": Provider.LOCAL_GEMMA,
}


def resolve_door_route(
    spec_provider: Provider,
    spec_model: Optional[str],
    target_slot: str,
    instruction_text: str,
    file_paths: Optional[list[str]] = None,
    loc_estimate: int = 0,
    env: Optional[dict] = None,
) -> Optional[Tuple[Provider, str, str]]:
    """Resolve a concrete provider + model for a dispatch without an explicit one.

    Returns (provider, model, route_reason) when the router has a recommendation,
    or None when routing should be skipped (T0, explicit provider, router
    disabled, or router error).

    Args:
        spec_provider: The spec's provider enum value (check Provider.AUTO before calling).
        spec_model: The spec's model field (None when provider is AUTO).
        target_slot: "T0" through "T3".
        instruction_text: The full instruction text for classification.
        file_paths: List of dispatch file paths (for LOC-aware classification).
        loc_estimate: Estimated LOC of the change.
        env: Process environment dict (defaults to os.environ).
    """
    # Gate 1: T0 never routes. t0-opus-only is a floor, not an advisory.
    if target_slot == "T0":
        return None

    # Gate 2: Only fill in when the spec is silent. An explicit provider wins
    # undiminished (worker-provider-free-choice, pin_semantics=default).
    if spec_provider != Provider.AUTO:
        return None

    _env = env if env is not None else dict(os.environ)

    # Gate 3: VNX_SMART_ROUTER_DISABLE=1 disables the router entirely.
    if _env.get("VNX_SMART_ROUTER_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    # Backward compat: VNX_AUTO_ROUTE=0/false/off also disables.
    auto_route = _env.get("VNX_AUTO_ROUTE", "").strip().lower()
    if auto_route in ("0", "false", "no", "off"):
        return None

    try:
        from .cost_tier import classify_dispatch  # noqa: PLC0415
        from .tier_routing import resolve_tier_route  # noqa: PLC0415

        task_spec = {"instruction": instruction_text}
        tier = classify_dispatch(task_spec, file_paths or [], loc_estimate)
        route = resolve_tier_route(tier, _env)

        provider_enum = _TIER_PROVIDER_TO_ENUM.get(route.provider)
        if provider_enum is None:
            # A decline is still possible (an unmapped provider string), but it
            # must be visible, not silent: the door falls through to its own
            # lane resolution, and the reason is logged so a drift in the tier
            # map is diagnosable.
            logger.warning(
                "smart-router door routing: provider %r for tier=%s is not in "
                "_TIER_PROVIDER_TO_ENUM; declining route (fail-open to default lane)",
                route.provider,
                tier,
            )
            return None

        route_reason = f"smart-router:tier={tier},provider={route.provider},model={route.model},lane={route.lane}"
        if route.reason:
            route_reason += f";{route.reason}"
        return provider_enum, route.model, route_reason
    except Exception:
        # Fail-open: a broken classifier or resolver must never block a dispatch.
        # The door falls through to its existing behaviour.
        # A failing router that is silent is indistinguishable from a correctly
        # operating router that declined to route — always log at WARNING so
        # drift (e.g. a TypeError on stale loc_estimate=None) is visible.
        logger.warning(
            "smart-router door routing: classifier/resolver failed, dispatch "
            "falls through to default lane (fail-open). Error: %s",
            exc_info=True,
        )
        return None


def apply_door_route(
    vspec: ValidatedSpec,
    env: Optional[dict] = None,
) -> Tuple[ValidatedSpec, Optional[str]]:
    """Apply smart routing to a validated spec when provider is AUTO.

    Single call site for dispatch_cli.run_dispatch(). Returns (vspec, route_reason)
    where vspec is either the original (router skipped/disabled/failed) or a new
    ValidatedSpec with the resolved provider + model. route_reason is a
    human-readable string for the dry-run output and receipt, or None when the
    router did not apply.

    Fail-open: never raises. A broken router returns the original vspec unchanged
    with route_reason=None.
    """
    spec = vspec.spec
    if spec.provider != Provider.AUTO:
        return vspec, None

    _env = env if env is not None else dict(os.environ)

    # Extract file paths from dispatch_paths as strings for the classifier.
    file_paths = [str(dp.path) for dp in spec.dispatch_paths]

    result = resolve_door_route(
        spec_provider=spec.provider,
        spec_model=spec.model,
        target_slot=spec.target_slot,
        instruction_text=vspec.instruction_text,
        file_paths=file_paths,
        env=_env,
    )
    if result is None:
        return vspec, None

    new_provider, new_model, route_reason = result
    new_spec = dataclasses.replace(spec, provider=new_provider, model=new_model)
    new_vspec = dataclasses.replace(vspec, spec=new_spec)
    return new_vspec, route_reason

