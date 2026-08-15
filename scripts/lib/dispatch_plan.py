"""dispatch_plan.py — compile_plan: pure, total routing decision function.

Maps a ValidatedSpec + RuntimeSnapshot to exactly one ExecutionPlan or one Reject.
No I/O, no env reads, no filesystem access — every side-effectful input arrives
via the snapshot argument.

PR-2 of the single-entry dispatch gate. Nothing imports this module yet.
ADR-007 not triggered — pure in-process types only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from dispatch_spec import (
    DispatchPath,
    Isolation,
    Provider,
    Reject,
    ValidatedSpec,
)


# ---------------------------------------------------------------------------
# Snapshot types (caller computes via I/O, compile_plan only reads)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConstraintVerdict:
    code: str            # e.g. "kimi-via-cli-only"
    severity: str        # "blocking" | "warn"
    message: str
    override_applied: bool = False


@dataclass(frozen=True)
class ModelPin:
    """A model pin carrying enforcement semantics, not just a model name.

    floor:   coercive — spec.model is ignored, the pin always wins (today's
             behavior for both t0-opus-only and workers-kimi-pinned).
    default: advisory — spec.model wins when the caller set one; the pin is
             only used as a fallback when spec.model is absent.

    worker-provider-free-choice PR-1 added this type; PR-3 wired D4 to honour
    the semantics field.
    """
    model: str
    semantics: str  # "floor" | "default"


@dataclass(frozen=True)
class RuntimeSnapshot:
    constraint_verdicts: tuple[ConstraintVerdict, ...] = ()
    staging_promoted: bool = False
    target_health: Mapping[str, str] = field(default_factory=dict)    # target_id -> "healthy"|"unhealthy"|"offline"
    target_capable: Mapping[str, bool] = field(default_factory=dict)  # target_id -> capability match
    model_pins: Mapping[str, ModelPin] = field(default_factory=dict)  # target_slot -> pin (model + semantics)
    claude_serial_enabled: bool = True
    # OI-921: the set of role names that exist in the agents/ registry. None =
    # registry not provided (direct/test callers that pre-date the field) —
    # compile_plan skips the membership check so their behavior is unchanged. A
    # non-None frozenset (the door always provides one, possibly EMPTY) ENFORCES
    # membership: an empty set rejects every role, so an undiscoverable registry
    # fails closed instead of silently accepting anything.
    valid_roles: Optional[frozenset[str]] = None
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): the predecessor
    # dispatch this one continues, the tier escalation, and the smart_router
    # task class. Computed by the door (build_runtime_snapshot) and passed in so
    # compile_plan stays pure — it only copies them onto the plan.
    parent_dispatch: Optional[str] = None
    task_class: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None
    # OI-943: worker-claude-override gate outcome, threaded from build_runtime_snapshot
    # so the door can persist target_slot + override reason onto the dispatch row.
    worker_claude_override_reason: Optional[str] = None
    # OI-1156: auth-derived billing signal for the claude lane. compile_plan is
    # pure (no env reads), so the door computes this from the process env and
    # passes it in. True when ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL are present
    # (key-auth / redirected endpoint = metered), False for the subscription
    # session (keychain OAuth). The headless lane (claude -p) runs on the SAME
    # Max subscription unless this is True — measured 2026-08-11.
    claude_api_metered: bool = False


# ---------------------------------------------------------------------------
# ExecutionPlan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPlan:
    dispatch_id: str
    project_id: str
    provider: Provider
    model: str
    lane: str                           # "claude_tmux_subscription" | "provider"
    adapter: str                        # "tmux_claude" | "provider"
    target_id: str                      # "ephemeral" for the leaseless claude lane
    billing: str                        # "subscription" | "api_metered" | "provider_metered" | "local"
    serialization_class: Optional[str]  # "claude-tmux" | None
    isolation: Isolation                # always Isolation.WORKTREE
    require_worktree: bool              # always True
    seed_materialize: bool
    instruction_delivery: str           # always "file_ref"
    report_contract: str                # always "required"
    warmup: str                         # "verify_strict" (claude) | "n/a"
    deadline_seconds: int
    base_ref: str
    dispatch_paths: tuple[DispatchPath, ...]
    instruction_file: Path
    route_reason: str                   # comma-joined rule ids, e.g. "D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12"
    instruction_sha256: str = ""        # P0-3: sha256 of instruction content at validate() time
    warnings: tuple[str, ...] = ()
    role: Optional[str] = None          # carried from DispatchSpec for the phantom-guard review
                                        # exemption (codex P0.2 F2). NOT in digest() — advisory only,
                                        # must not perturb the permit fingerprint.
    pr_id: Optional[str] = None         # OI-982: carried from DispatchSpec so the fix-forward
                                        # diff fallback in _resolve_fix_forward_diff works.
                                        # NOT in digest() — advisory only.
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): advisory receipt
    # metadata, NOT in digest() — like ``role``, it must not perturb the permit
    # fingerprint. parent_dispatch / tier_from / tier_to / task_class.
    parent_dispatch: Optional[str] = None
    task_class: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None
    requires_mcp: bool = False          # OI-865: True keeps the worker's ambient MCP config instead
                                        # of the force-empty scoped posture. Default False is the
                                        # choice for a MISSING spec field: DispatchSpec.requires_mcp
                                        # defaults to False and the tmux lane's dispatch() defaults to
                                        # False, so a spec that omits the field lands on exactly the
                                        # value today's code already uses — never silently "no MCP"
                                        # for a dispatch that already gets MCP. It IS in digest():
                                        # MCP access changes worker behavior, so a permit for a
                                        # requires_mcp plan must not validate a force-empty plan.

    def canonical_dict(self) -> dict:
        """Return the canonical, order-independent decision dict.

        This is the same dict that digest() hashes — extracting it lets the door
        persist the full routing decision alongside the fingerprint (OI-849).
        Excludes advisory fields (warnings, role, pr_id, chain-link metadata).
        """
        return {
            "dispatch_id": self.dispatch_id,
            "project_id": self.project_id,
            "provider": self.provider.value,
            "model": self.model,
            "lane": self.lane,
            "adapter": self.adapter,
            "target_id": self.target_id,
            "billing": self.billing,
            "serialization_class": self.serialization_class,
            "isolation": self.isolation.value,
            "require_worktree": self.require_worktree,
            "seed_materialize": self.seed_materialize,
            "requires_mcp": self.requires_mcp,
            "instruction_delivery": self.instruction_delivery,
            "report_contract": self.report_contract,
            "warmup": self.warmup,
            "deadline_seconds": self.deadline_seconds,
            "base_ref": self.base_ref,
            "instruction_file": str(self.instruction_file),
            "instruction_sha256": self.instruction_sha256,
            "route_reason": self.route_reason,
            "dispatch_paths": [
                {
                    "path": str(dp.path),
                    "access": dp.access.value,
                    "materialize_at_cwd": dp.materialize_at_cwd,
                }
                for dp in self.dispatch_paths
            ],
        }

    def digest(self) -> str:
        """Stable sha256 over the canonical, order-independent field set.

        Excludes advisory warnings. Used by ExecutionPermit (satisfies PlanLike).
        """
        blob = json.dumps(self.canonical_dict(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# compile_plan — pure, total
# ---------------------------------------------------------------------------

def claude_auth_is_api_metered(env: Mapping[str, str]) -> bool:
    """Return True when the claude auth identity is metered (own key / redirect).

    OI-1156: billing follows AUTH IDENTITY, not lane. ``claude -p`` (headless)
    runs on the Max subscription by default — measured 2026-08-11 via auth
    state (no ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL, keychain
    subscriptionType=max). The metered identity is the presence of an own key
    or a base-URL redirect (ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL), which
    switches the claude CLI to key-auth / a redirected endpoint and off the
    subscription. Pure: the caller supplies the env mapping; compile_plan never
    reads the environment itself.
    """
    return bool((env.get("ANTHROPIC_API_KEY") or "").strip()) or bool(
        (env.get("ANTHROPIC_BASE_URL") or "").strip()
    )


def compile_plan(vspec: ValidatedSpec, snapshot: RuntimeSnapshot) -> ExecutionPlan | Reject:
    """Map ValidatedSpec + RuntimeSnapshot to exactly one ExecutionPlan or one Reject.

    Pure and total: no I/O, no env reads, no filesystem access. Every input
    arrives via arguments. First failing hard rule returns a Reject; no None,
    no fallthrough, no raise.
    """
    spec = vspec.spec
    warnings: list[str] = []
    fired: list[str] = []

    # D11 — staging gate (ADR-006)
    if not snapshot.staging_promoted:
        return Reject("ADR-006", "dispatch rejected: staging not promoted (ADR-006 gate)")
    fired.append("D11")

    # OI-921 — role-registry check: the validation dispatch_spec Rule 7 defers here.
    # dispatch_bridge no longer silently fills the backend-developer sentinel, so a
    # non-empty role must now name a role that actually exists in agents/. A
    # consciously chosen "backend-developer" IS valid (it is a real agents/ role);
    # only the SILENT default was the defect. snapshot.valid_roles=None (registry
    # not provided) skips — the door always passes a frozenset (possibly empty =
    # fail-closed, every role rejected).
    if snapshot.valid_roles is not None and spec.role not in snapshot.valid_roles:
        valid = ", ".join(sorted(snapshot.valid_roles))
        return Reject(
            "unknown-role",
            f"role {spec.role!r} is not a known agent role; "
            f"valid roles: {valid or '(none discovered — agents/ registry unavailable)'}",
        )

    # D3 — constraint verdicts; blocking → Reject immediately; warn → collect
    for v in snapshot.constraint_verdicts:
        if v.severity == "blocking":
            return Reject(v.code, v.message)
        elif v.severity == "warn":
            warnings.append(f"constraint-warn: {v.code}: {v.message}")
    fired.append("D3")

    # D1 — lane resolution from provider
    provider = spec.provider
    if provider == Provider.AUTO:
        return Reject(
            "unresolved-provider",
            "AUTO must be resolved by the capability seam before compile_plan",
        )
    is_claude_lane = provider == Provider.CLAUDE
    is_claude_headless = is_claude_lane and spec.allow_headless
    if is_claude_headless:
        lane = "claude_headless"
        adapter = "claude_subprocess"
        warnings.append(f"HEADLESS lane opted-in: {spec.headless_reason}")
    elif is_claude_lane:
        lane = "claude_tmux_subscription"
        adapter = "tmux_claude"
    else:
        lane = "provider"
        adapter = "provider"
    fired.append("D1")

    # D2 — billing. Provider lanes are metered by default, with two exceptions:
    # kimi runs on a flat CLI-OAuth subscription (kimi-via-cli-only), and
    # local-gemma runs on-device with no API cost. Labeling both as
    # provider_metered overstated cost and hid their real quota model.
    #
    # OI-1156: the CLAUDE lane's label is auth-derived, not lane-derived.
    # claude -p (headless) and the tmux lane both run on the Max subscription
    # unless an own ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL is present — the
    # snapshot's claude_api_metered flag carries that measurement in so
    # compile_plan stays pure. A headless dispatch with no key of its own is
    # therefore "subscription", not "api_metered".
    if is_claude_lane:
        billing = "api_metered" if snapshot.claude_api_metered else "subscription"
    elif provider == Provider.KIMI:
        billing = "subscription"
    elif provider == Provider.LOCAL_GEMMA:
        billing = "local"
    else:
        billing = "provider_metered"
    fired.append("D2")

    # D4 — model tier; warn-only pins are NOT a Reject
    #
    # worker-provider-free-choice PR-3: D4 honours ModelPin.semantics.
    #   floor:   the pin always wins (coercive — today's behaviour for all slots).
    #   default: spec.model wins when the caller set one; the pin is the fallback
    #            when spec.model is absent.
    #
    # worker-provider-kimi-flip (20260723): snapshot.model_pins now resolves T1/T2/T3
    # to "kimi-k3". This branch only runs when is_claude_lane is True (explicit
    # provider=claude). The "sonnet" fallback below deliberately stays a valid Claude
    # model name — it is the no-pin-found default for the claude lane specifically,
    # not a worker-role default. When a pin IS found for a claude-lane T1/T2/T3
    # (pinned="kimi-k3") with floor semantics, `model = pinned` intentionally yields
    # a non-Claude label; the D3 registry/constraint gate upstream rejects that
    # combination fail-loud rather than silently falling back to sonnet (kimi-only,
    # no fallback policy).
    target_slot = spec.target_slot
    if is_claude_lane:
        pin = snapshot.model_pins.get(target_slot)
        pinned = pin.model if pin is not None else None
        requested = spec.model
        if pinned:
            if pin.semantics == "default" and requested and requested != pinned:
                # Default semantics: the caller's explicit model choice wins.
                warnings.append(
                    f"model-tier: requested {requested} over pinned {pinned}"
                    f" for {target_slot} (default semantics — request honoured)"
                )
                model = requested
            elif requested and requested != pinned:
                # Floor semantics (or unrecognised — treat as floor): the pin wins.
                warnings.append(
                    f"model-tier: requested {requested}, pinned {pinned}"
                    f" for {target_slot} (floor semantics — pin honoured)"
                )
                model = pinned
            else:
                model = pinned
        else:
            model = requested or "sonnet"
    else:
        model = spec.model or "default"
    fired.append("D4")

    # D5 — serialization class; headless lane has no tmux serial lock
    serialization_class: Optional[str]
    if is_claude_lane and not is_claude_headless and snapshot.claude_serial_enabled:
        serialization_class = "claude-tmux"
    else:
        serialization_class = None
    fired.append("D5")

    # D6 — isolation; always worktree
    isolation = Isolation.WORKTREE
    require_worktree = True
    fired.append("D6")

    # D7 — seed materialize
    dispatch_paths = vspec.normalized_paths
    seed_materialize = bool(dispatch_paths) or any(dp.materialize_at_cwd for dp in dispatch_paths)
    fired.append("D7")

    # D8 — instruction delivery
    instruction_delivery = "file_ref"
    fired.append("D8")

    # D9 — report contract
    report_contract = "required"
    fired.append("D9")

    # D10 — warmup; headless lane has no tmux warmup
    warmup = "verify_strict" if (is_claude_lane and not is_claude_headless) else "n/a"
    fired.append("D10")

    # D12 — target resolution; claude lane is leaseless (ephemeral), skip health checks
    if is_claude_lane:
        target_id = "ephemeral"
    else:
        target_id = spec.target_id_override or spec.target_slot
        health = snapshot.target_health.get(target_id)
        if health != "healthy":
            return Reject("R-6", f"target {target_id!r} is not healthy (status={health!r})")
        if not snapshot.target_capable.get(target_id, True):
            return Reject("R-5", f"target {target_id!r} is not capable for this dispatch")
    fired.append("D12")

    return ExecutionPlan(
        dispatch_id=spec.dispatch_id,
        project_id=spec.project_id,
        provider=provider,
        model=model,
        lane=lane,
        adapter=adapter,
        target_id=target_id,
        billing=billing,
        serialization_class=serialization_class,
        isolation=isolation,
        require_worktree=require_worktree,
        seed_materialize=seed_materialize,
        requires_mcp=spec.requires_mcp,
        instruction_delivery=instruction_delivery,
        report_contract=report_contract,
        warmup=warmup,
        deadline_seconds=spec.deadline_seconds,
        base_ref=spec.base_ref,
        dispatch_paths=dispatch_paths,
        instruction_file=spec.instruction_file,
        route_reason=",".join(fired),
        role=spec.role,
        pr_id=spec.pr_id,
        # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): copied from the
        # door-computed snapshot so the receipt can say which dispatch this one
        # continues and on which tier.
        parent_dispatch=snapshot.parent_dispatch,
        task_class=snapshot.task_class,
        tier_from=snapshot.tier_from,
        tier_to=snapshot.tier_to,
        instruction_sha256=vspec.instruction_sha256,
        warnings=tuple(warnings),
    )
