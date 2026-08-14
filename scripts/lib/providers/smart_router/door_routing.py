"""door_routing.py — Smart-router integration for the single-entry dispatch door.

Called from dispatch_cli.run_dispatch() when the spec carries no explicit
provider (provider=AUTO). Resolves the dispatch to a concrete provider + model
via the cost-tier classifier and tier-routing engine, then hands back a
Provider enum + model string that compile_plan can consume.

Fail-open for the classifier, fail-loud for the registry (ADR-036 §2). A broken
classifier returns a declined result with a reason and the door falls through to
the existing behaviour — a routing *bug* must never block a dispatch. But an
unknown provider/model (a provider string the registry does not know) is drift
between the router and the registry, and raises RegistryLookupError naming what
was missing and where it was looked for. T0 is never routed (t0-opus-only is a
floor, not an advisory). The router is default-on since 2026-08-02.

Every decline path carries an explicit reason (OI-1187) so a no-route is
distinguishable in the dry-run output and receipt. Before this, a T0 skip, an
explicit provider, the kill-switch, the enum gap and a classifier crash all
returned a bare None and were indistinguishable in the audit trail.

Constraints:
- No imports from dispatch_cli or dispatch_plan (avoid circular deps).
- No I/O beyond what the smart_router submodules already do.
- Pure decision function — never mutates; never raises except for the
  registry-drift error of ADR-036 §2.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Optional, Tuple

from dispatch_spec import Provider, ValidatedSpec  # noqa: PLC0415 — sibling package, no cycle

logger = logging.getLogger(__name__)


# Decline reasons — one per distinct no-route cause (OI-1187). Each is a stable
# string surfaced in the dry-run output and receipt, so a decline is diagnosable
# instead of an anonymous None.
DECLINE_T0_NEVER_ROUTES = "t0-never-routes"
DECLINE_EXPLICIT_PROVIDER = "explicit-provider"
DECLINE_ROUTER_DISABLED = "router-disabled"
DECLINE_CLASSIFIER_ERROR = "classifier-error"


def _provider_enum(provider_str: str) -> Provider:
    """Map a route provider string to a Provider enum — fail-loud (ADR-036 §2).

    The route's provider string is the registry's ``dispatch_enum`` (read by the
    tier router from wave7_models.yaml). A string that is not a real dispatch
    Provider is drift between the tier map and the registry, and must surface
    with what+where — never a silent None.
    """
    try:
        return Provider(provider_str)
    except ValueError:
        from providers.provider_registry import RegistryLookupError  # noqa: PLC0415

        raise RegistryLookupError(
            f"provider {provider_str!r} is not a known dispatch Provider; the "
            f"tier map produced a provider string that the Provider enum "
            f"(dispatch_spec) and registry (wave7_models.yaml dispatch_enum) do "
            f"not recognize"
        ) from None


@dataclasses.dataclass(frozen=True)
class DoorRouteResult:
    """Outcome of resolve_door_route.

    Exactly one of ``route`` / ``decline_reason`` is populated. ``tier`` carries
    the classifier's tier whenever classification ran, so the door can make a
    tier-aware fallback when a route cannot be applied after a successful
    classification (OI-1187): a tier-high decline must never silently land on
    the sonnet default.
    """

    route: Optional[Tuple[Provider, str, str]] = None
    decline_reason: Optional[str] = None
    tier: Optional[str] = None


def resolve_door_route(
    spec_provider: Provider,
    spec_model: Optional[str],
    target_slot: str,
    instruction_text: str,
    file_paths: Optional[list[str]] = None,
    loc_estimate: int = 0,
    env: Optional[dict] = None,
) -> DoorRouteResult:
    """Resolve a concrete provider + model for a dispatch without an explicit one.

    Returns a DoorRouteResult: either ``route=(provider, model, route_reason)``
    when the router has a recommendation, or a ``decline_reason`` naming why
    routing was skipped (T0, explicit provider, router disabled, or a
    classifier error). ``tier`` is populated whenever the classifier ran so the
    door can fall back tier-aware instead of blindly.

    Raises RegistryLookupError when the classifier succeeded but produced a
    provider/model the registry does not know (ADR-036 §2 fail-loud) — the
    door must surface that drift, not fall back.

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
        return DoorRouteResult(decline_reason=DECLINE_T0_NEVER_ROUTES)

    # Gate 2: Only fill in when the spec is silent. An explicit provider wins
    # undiminished (worker-provider-free-choice, pin_semantics=default).
    if spec_provider != Provider.AUTO:
        return DoorRouteResult(decline_reason=DECLINE_EXPLICIT_PROVIDER)

    _env = env if env is not None else dict(os.environ)

    # Gate 3: VNX_SMART_ROUTER_DISABLE=1 disables the router entirely.
    if _env.get("VNX_SMART_ROUTER_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return DoorRouteResult(decline_reason=DECLINE_ROUTER_DISABLED)

    # Backward compat: VNX_AUTO_ROUTE=0/false/off also disables.
    auto_route = _env.get("VNX_AUTO_ROUTE", "").strip().lower()
    if auto_route in ("0", "false", "no", "off"):
        return DoorRouteResult(decline_reason=DECLINE_ROUTER_DISABLED)

    # Classifier — fail-open (ADR-036 §2). A routing bug (a stale loc_estimate,
    # an import error, a classifier crash) must never block a dispatch.
    try:
        from .cost_tier import classify_dispatch  # noqa: PLC0415

        task_spec = {"instruction": instruction_text}
        tier = classify_dispatch(task_spec, file_paths or [], loc_estimate)
    except Exception as exc:
        logger.warning(
            "smart-router door routing: classifier failed, dispatch falls "
            "through to default lane (fail-open). Error: %s",
            exc,
            exc_info=True,
        )
        return DoorRouteResult(decline_reason=DECLINE_CLASSIFIER_ERROR)

    # Tier routing + provider enum — fail-loud (ADR-036 §2). The classifier
    # worked; an unknown provider/model is registry drift and must surface, not
    # vanish as a silent None. RegistryLookupError propagates to the door.
    from .tier_routing import resolve_tier_route  # noqa: PLC0415

    route = resolve_tier_route(tier, _env)
    provider_enum = _provider_enum(route.provider)

    route_reason = f"smart-router:tier={tier},provider={route.provider},model={route.model},lane={route.lane}"
    if route.reason:
        route_reason += f";{route.reason}"
    return DoorRouteResult(
        route=(provider_enum, route.model, route_reason),
        tier=tier,
    )


def apply_door_route(
    vspec: ValidatedSpec,
    env: Optional[dict] = None,
) -> Tuple[ValidatedSpec, Optional[str]]:
    """Apply smart routing to a validated spec when provider is AUTO.

    Single call site for dispatch_cli.run_dispatch(). Returns (vspec, route_reason)
    where vspec is either the original (router skipped/disabled/failed) or a new
    ValidatedSpec with the resolved provider + model. route_reason is a
    human-readable string for the dry-run output and receipt: the router's
    recommendation when routed, or the decline reason (prefixed "smart-router:")
    when the router skipped — never a bare None for a decline (OI-1187).

    Fail-open for a broken classifier (returns the original vspec unchanged
    with a decline reason); fail-loud for registry drift (RegistryLookupError
    propagates to the caller, ADR-036 §2).
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
    if result.route is None:
        if result.decline_reason is None:
            return vspec, None
        return vspec, f"smart-router:no-route,reason={result.decline_reason}"

    new_provider, new_model, route_reason = result.route
    new_spec = dataclasses.replace(spec, provider=new_provider, model=new_model)
    new_vspec = dataclasses.replace(vspec, spec=new_spec)
    return new_vspec, route_reason
