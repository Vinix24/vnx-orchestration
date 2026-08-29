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
floor, not an advisory). Routing is gated by the staging layer (``.staging``)
since 2026-08-14: after the kill-switch, a per-tier enable flag and a
deterministic canary fraction decide whether the router hands back a route or
declines to the legacy path — the rollout starts from zero and ramps up via
operator-config, never in one leap.

Every decline path carries an explicit reason (OI-1187) so a no-route is
distinguishable in the dry-run output and receipt. Before this, a T0 skip, an
explicit provider, the kill-switch, the enum gap and a classifier crash all
returned a bare None and were indistinguishable in the audit trail.

Every decision — applied or declined — is appended to the router-decision ledger
(``.decision_log``, OI-1494). A declined dispatch still has its provider, model
and lane computed and written down; it simply is not acted on. Before this the
staging gate returned before ``resolve_tier_route`` was reached, so a switched-off
tier produced no observation at all and the rollout had no evidence to ramp on.
Recording happens at the single exit point of ``resolve_door_route`` so a decline
branch added later cannot silently skip the ledger.

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
from pathlib import Path
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

    ``would_route`` is the provider/model/lane the router settled on, present
    whenever it could be computed — including on a decline, where it is the
    route that was deliberately NOT taken (OI-1494). It lets the dry-run output
    show what the router would have done without the router doing it.
    """

    route: Optional[Tuple[Provider, str, str]] = None
    decline_reason: Optional[str] = None
    tier: Optional[str] = None
    would_route: Optional[dict] = None


def _compute_would_route(
    tier: str,
    env: dict,
    state_dir: Optional["Path"] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Compute the route the router WOULD take for a tier, without acting on it.

    Total and fail-open by contract, because this runs on the DECLINE path: the
    dispatch is already following the legacy lane, so a registry drift or a
    broken availability read here is an observation failure and is returned as
    one. Raising would turn a previously harmless decline into a hard dispatch
    failure — the observation layer would become an outage.

    On the APPLY path the same drift still raises through ``_provider_enum``,
    where fail-loud is correct (ADR-036 §2) because the route is actually used.
    """
    try:
        from .tier_routing import resolve_tier_route  # noqa: PLC0415

        route = resolve_tier_route(tier, env, state_dir=state_dir)
        return (
            {
                "provider": route.provider,
                "model": route.model,
                "lane": route.lane,
                "reason": route.reason,
            },
            None,
        )
    except Exception as exc:  # noqa: BLE001 — see the contract above
        return None, f"{type(exc).__name__}: {exc}"


def resolve_door_route(
    spec_provider: Provider,
    spec_model: Optional[str],
    target_slot: str,
    instruction_text: str,
    file_paths: Optional[list[str]] = None,
    loc_estimate: int = 0,
    env: Optional[dict] = None,
    dispatch_id: Optional[str] = None,
    state_dir: Optional["Path"] = None,
) -> DoorRouteResult:
    """Resolve a concrete provider + model for a dispatch without an explicit one.

    Returns a DoorRouteResult: either ``route=(provider, model, route_reason)``
    when the router has a recommendation, or a ``decline_reason`` naming why
    routing was skipped (T0, explicit provider, router disabled, or a
    classifier error). ``tier`` is populated whenever the classifier ran so the
    door can fall back tier-aware instead of blindly, and ``would_route`` carries
    the computed provider/model/lane even when the route is not applied.

    Every outcome is appended to the router-decision ledger (OI-1494) before it
    leaves this function — the applied ones, the declined ones, and the refusals
    that raise. The decision itself is made by ``_decide``; this wrapper is the
    single exit point, so a branch added to ``_decide`` later cannot skip the
    ledger. Recording is fail-open and never changes the returned result, nor the
    exception raised.

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
        dispatch_id: The dispatch's stable id. Seeds the deterministic canary
            bucket and identifies the decision in the ledger; without it a record
            cannot be tied back to the dispatch it describes.
        state_dir: Project state dir, used for the availability/cooldown read and
            for the ledger. Defaults to the resolved project state dir.
    """
    from .decision_log import record_router_decision  # noqa: PLC0415

    try:
        result, compute_error = _decide(
            spec_provider=spec_provider,
            spec_model=spec_model,
            target_slot=target_slot,
            instruction_text=instruction_text,
            file_paths=file_paths,
            loc_estimate=loc_estimate,
            env=env,
            dispatch_id=dispatch_id,
            state_dir=state_dir,
        )
    except Exception as exc:
        # Registry drift raises here and must keep raising (ADR-036 §2) — but a
        # refusal is the one outcome that actually stops a dispatch, so it is the
        # last thing the ledger may be blind to. Record, then re-raise unchanged.
        record_router_decision(
            tier=None,
            applied=False,
            decline_reason=None,
            would_route=None,
            target_slot=target_slot,
            dispatch_id=dispatch_id,
            compute_error=f"{type(exc).__name__}: {exc}",
            raised=True,
            state_dir=state_dir,
            env=env,
        )
        raise

    record_router_decision(
        tier=result.tier,
        applied=result.route is not None,
        decline_reason=result.decline_reason,
        would_route=result.would_route,
        target_slot=target_slot,
        dispatch_id=dispatch_id,
        compute_error=compute_error,
        state_dir=state_dir,
        env=env,
    )
    return result


def _decide(
    *,
    spec_provider: Provider,
    spec_model: Optional[str],
    target_slot: str,
    instruction_text: str,
    file_paths: Optional[list[str]],
    loc_estimate: int,
    env: Optional[dict],
    dispatch_id: Optional[str],
    state_dir: Optional["Path"],
) -> Tuple[DoorRouteResult, Optional[str]]:
    """The routing decision itself — pure, never writes.

    Returns the result plus an optional compute-error string describing why
    ``would_route`` could not be filled in. Kept separate from
    ``resolve_door_route`` so the ledger append has exactly one call site.
    """
    # Gate 1: T0 never routes. t0-opus-only is a floor, not an advisory.
    if target_slot == "T0":
        return DoorRouteResult(decline_reason=DECLINE_T0_NEVER_ROUTES), None

    # Gate 2: Only fill in when the spec is silent. An explicit provider wins
    # undiminished (worker-provider-free-choice, pin_semantics=default).
    if spec_provider != Provider.AUTO:
        return DoorRouteResult(decline_reason=DECLINE_EXPLICIT_PROVIDER), None

    _env = env if env is not None else dict(os.environ)

    # Gate 3: VNX_SMART_ROUTER_DISABLE=1 disables the router entirely.
    if _env.get("VNX_SMART_ROUTER_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return DoorRouteResult(decline_reason=DECLINE_ROUTER_DISABLED), None

    # Backward compat: VNX_AUTO_ROUTE=0/false/off also disables.
    auto_route = _env.get("VNX_AUTO_ROUTE", "").strip().lower()
    if auto_route in ("0", "false", "no", "off"):
        return DoorRouteResult(decline_reason=DECLINE_ROUTER_DISABLED), None

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
        return DoorRouteResult(decline_reason=DECLINE_CLASSIFIER_ERROR), None

    # Staging gate — UNDER the kill-switch (checked above), so a kill-switch
    # always wins. The rollout is per-tier + a deterministic canary fraction:
    # a tier that is not enabled, or a dispatch that falls in the control half
    # of the canary, declines here and follows the legacy path. The decline
    # reason carries the tier so the control group is recognisable in the trail.
    from .staging import dispatch_group_key, load_staging_config, staging_verdict  # noqa: PLC0415

    staging_config = load_staging_config()
    group_key = dispatch_group_key(dispatch_id, target_slot, instruction_text, file_paths)
    staging_decline = staging_verdict(tier, group_key, staging_config)
    if staging_decline is not None:
        # OI-1494: the dispatch follows the legacy lane, but the route it would
        # have taken is computed anyway so the declined half of the split is
        # observable. Computation is fail-open here — see _compute_would_route.
        would_route, compute_error = _compute_would_route(tier, _env, state_dir)
        return (
            DoorRouteResult(
                decline_reason=staging_decline, tier=tier, would_route=would_route,
            ),
            compute_error,
        )

    # Tier routing + provider enum — fail-loud (ADR-036 §2). The classifier
    # worked; an unknown provider/model is registry drift and must surface, not
    # vanish as a silent None. RegistryLookupError propagates to the door.
    from .tier_routing import resolve_tier_route  # noqa: PLC0415

    route = resolve_tier_route(tier, _env, state_dir=state_dir)
    provider_enum = _provider_enum(route.provider)

    route_reason = f"smart-router:tier={tier},provider={route.provider},model={route.model},lane={route.lane}"
    if route.reason:
        route_reason += f";{route.reason}"
    return (
        DoorRouteResult(
            route=(provider_enum, route.model, route_reason),
            tier=tier,
            would_route={
                "provider": route.provider,
                "model": route.model,
                "lane": route.lane,
                "reason": route.reason,
            },
        ),
        None,
    )


def apply_door_route(
    vspec: ValidatedSpec,
    env: Optional[dict] = None,
) -> Tuple[ValidatedSpec, Optional[str]]:
    """Apply smart routing to a validated spec when provider is AUTO.

    Returns (vspec, route_reason) where vspec is either the original (router
    skipped/disabled/failed) or a new ValidatedSpec with the resolved provider +
    model. route_reason is a human-readable string for the dry-run output and
    receipt: the router's recommendation when routed, or the decline reason
    (prefixed "smart-router:") when the router skipped — never a bare None for a
    decline (OI-1187).

    NOTE ON CALLERS: this function has no production caller. The door resolves
    the router BEFORE validate() (OI-962), so ``dispatch_cli._resolve_router_pre_validate``
    calls ``resolve_door_route`` on the raw DispatchSpec and applies the result
    itself; this ValidatedSpec-shaped wrapper is exercised only by tests. It used
    to claim to be "the single call site for dispatch_cli.run_dispatch()", which
    has not been true since the pre-validate move — a docstring naming a caller
    that does not exist sends the next reader to the wrong seam, which is exactly
    how an observability fix ends up somewhere it never runs (OI-1494).

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
        dispatch_id=spec.dispatch_id,
    )
    if result.route is None:
        if result.decline_reason is None:
            return vspec, None
        return vspec, f"smart-router:no-route,reason={result.decline_reason}"

    new_provider, new_model, route_reason = result.route
    new_spec = dataclasses.replace(spec, provider=new_provider, model=new_model)
    new_vspec = dataclasses.replace(vspec, spec=new_spec)
    return new_vspec, route_reason
