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
class RuntimeSnapshot:
    constraint_verdicts: tuple[ConstraintVerdict, ...] = ()
    staging_promoted: bool = False
    target_health: Mapping[str, str] = field(default_factory=dict)    # target_id -> "healthy"|"unhealthy"|"offline"
    target_capable: Mapping[str, bool] = field(default_factory=dict)  # target_id -> capability match
    model_pins: Mapping[str, str] = field(default_factory=dict)       # target_slot -> pinned model
    claude_serial_enabled: bool = True


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
    warnings: tuple[str, ...] = ()

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
            "instruction_delivery": self.instruction_delivery,
            "report_contract": self.report_contract,
            "warmup": self.warmup,
            "deadline_seconds": self.deadline_seconds,
            "base_ref": self.base_ref,
            "instruction_file": str(self.instruction_file),
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
    if is_claude_lane:
        lane = "claude_tmux_subscription"
        adapter = "tmux_claude"
    else:
        lane = "provider"
        adapter = "provider"
    fired.append("D1")

    # D2 — billing
    billing = "subscription" if is_claude_lane else "provider_metered"
    fired.append("D2")

    # D4 — model tier; warn-only pins are NOT a Reject
    target_slot = spec.target_slot
    if is_claude_lane:
        pinned = snapshot.model_pins.get(target_slot)
        requested = spec.model
        if pinned:
            if requested and requested != pinned:
                warnings.append(
                    f"model-tier: requested {requested}, pinned {pinned}"
                    f" for {target_slot} (override-able)"
                )
            model = pinned
        else:
            model = requested or "sonnet"
    else:
        model = spec.model or "default"
    fired.append("D4")

    # D5 — serialization class
    serialization_class: Optional[str]
    if is_claude_lane and snapshot.claude_serial_enabled:
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

    # D10 — warmup
    warmup = "verify_strict" if is_claude_lane else "n/a"
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
        instruction_delivery=instruction_delivery,
        report_contract=report_contract,
        warmup=warmup,
        deadline_seconds=spec.deadline_seconds,
        base_ref=spec.base_ref,
        dispatch_paths=dispatch_paths,
        instruction_file=spec.instruction_file,
        route_reason=",".join(fired),
        warnings=tuple(warnings),
    )
