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
    billing: str                        # "subscription" | "provider_metered"
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
    requires_mcp: bool = False          # OI-865: True keeps the worker's ambient MCP config instead
                                        # of the force-empty scoped posture. Default False is the
                                        # choice for a MISSING spec field: DispatchSpec.requires_mcp
                                        # defaults to False and the tmux lane's dispatch() defaults to
                                        # False, so a spec that omits the field lands on exactly the
                                        # value today's code already uses — never silently "no MCP"
                                        # for a dispatch that already gets MCP. It IS in digest():
                                        # MCP access changes worker behavior, so a permit for a
                                        # requires_mcp plan must not validate a force-empty plan.

    def digest(self) -> str:
        """Stable sha256 over the canonical, order-independent field set.

        Excludes advisory warnings. Used by ExecutionPermit (satisfies PlanLike).
        """
        canonical = {
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
        blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# compile_plan — pure, total
# ---------------------------------------------------------------------------

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
        warnings.append(f"HEADLESS API-billing opted-in: {spec.headless_reason}")
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
    if is_claude_headless:
        billing = "api_metered"
    elif is_claude_lane:
        billing = "subscription"
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
        instruction_sha256=vspec.instruction_sha256,
        warnings=tuple(warnings),
    )
