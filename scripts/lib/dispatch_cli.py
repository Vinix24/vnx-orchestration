"""dispatch_cli.py — Single-entry dispatch gate (PR-4).

spec -> validate -> snapshot -> compile_plan -> permit -> execute

Feature-gated by VNX_SINGLE_ENTRY_DISPATCH=1 in dispatch.sh. When the flag is
unset the bash layer uses the legacy path; this module's logic is unchanged.

BILLING SAFETY: no anthropic SDK import. Claude lane executes via interactive
tmux (subscription). Provider lane executes via run_envelope_plan (provider_metered).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

logger = logging.getLogger(__name__)

from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    Isolation,
    PathAccess,
    Provider,
    Reject,
    ValidatedSpec,
    validate,
)
from dispatch_plan import (  # noqa: E402
    ConstraintVerdict,
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
)
from dispatch_internal import ExecutionPermit, issue_permit, require_permit  # noqa: E402
from dispatch_envelope import run_envelope_plan  # noqa: E402


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_data_dir() -> Path:
    """Resolve VNX data directory. Mirrors provider_dispatch._resolve_data_dir."""
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR", "")
    if explicit_flag and explicit_val:
        return Path(explicit_val).resolve()
    project_id = os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    return Path.home() / ".vnx-data" / project_id


def _resolve_project_id() -> str:
    return os.environ.get("VNX_PROJECT_ID", "vnx-dev")


def _resolve_repo_root() -> Path:
    """scripts/lib/dispatch_cli.py -> repo root (parents[2])."""
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Spec loading from JSON
# ---------------------------------------------------------------------------

def load_spec(spec_file: Path) -> DispatchSpec:
    """Parse a DispatchSpec from a JSON dispatch-spec.json file."""
    raw = json.loads(spec_file.read_text(encoding="utf-8"))

    raw_paths = raw.get("dispatch_paths") or []
    dispatch_paths = tuple(
        DispatchPath(
            path=PurePosixPath(str(p["path"])),
            access=PathAccess(p.get("access", "read_write")),
            materialize_at_cwd=bool(p.get("materialize_at_cwd", False)),
        )
        for p in raw_paths
    )

    return DispatchSpec(
        schema_version=int(raw["schema_version"]),
        project_id=str(raw["project_id"]),
        dispatch_id=str(raw["dispatch_id"]),
        staging_id=str(raw["staging_id"]),
        instruction_file=Path(raw["instruction_file"]),
        role=str(raw["role"]),
        target_slot=str(raw["target_slot"]),
        gate=str(raw.get("gate", "")),
        dispatch_paths=dispatch_paths,
        provider=Provider(raw.get("provider", "auto")),
        model=(raw.get("model") or None),
        skill=(raw.get("skill") or None),
        task_class=(raw.get("task_class") or None),
        pr_id=(raw.get("pr_id") or None),
        deadline_seconds=int(raw.get("deadline_seconds", 3600)),
        base_ref=str(raw.get("base_ref", "origin/main")),
        isolation=Isolation(raw.get("isolation", "worktree")),
        requires_mcp=bool(raw.get("requires_mcp", False)),
        target_id_override=(raw.get("target_id_override") or None),
        tags=tuple(str(t) for t in (raw.get("tags") or [])),
    )


# ---------------------------------------------------------------------------
# Permit fingerprint helper
# ---------------------------------------------------------------------------

def fingerprint(permit: ExecutionPermit) -> str:
    """Short stable display string: plan_digest[:12]-dispatch_id.

    Unforgeable (anchored to plan digest) yet human-readable for log lines.
    """
    return f"{permit.plan_digest[:12]}-{permit.dispatch_id}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit_reject(r: Reject) -> None:
    print(f"[dispatch_cli] REJECT [{r.code}]: {r.reason}", file=sys.stderr)


def _print_plan(plan: ExecutionPlan, fp: str) -> None:
    print(f"[dispatch_cli] DRY RUN — fingerprint: {fp}")
    print(f"  dispatch_id:  {plan.dispatch_id}")
    print(f"  provider:     {plan.provider.value}")
    print(f"  model:        {plan.model}")
    print(f"  lane:         {plan.lane}")
    print(f"  target_id:    {plan.target_id}")
    print(f"  billing:      {plan.billing}")
    print(f"  route_reason: {plan.route_reason}")
    for w in plan.warnings:
        print(f"  [WARN] {w}")


# ---------------------------------------------------------------------------
# build_runtime_snapshot — all I/O lives here
# ---------------------------------------------------------------------------

def _sub_provider_for(provider_value: str) -> Optional[str]:
    if provider_value.startswith("litellm:"):
        return provider_value.split(":", 1)[1].split(":", 1)[0] or None
    if provider_value == "deepseek-harness":
        return "deepseek"
    return None


def _via_for_provider(provider_value: str, sub_provider: Optional[str]) -> Optional[str]:
    via_per_sub = {
        "deepseek": "litellm",
        "moonshot": "moonshot",
        "openrouter": "openrouter",
        "zai": "openrouter",
    }
    if provider_value.startswith("litellm:") or provider_value == "litellm":
        return via_per_sub.get(sub_provider or "", "litellm")
    if provider_value == "deepseek-harness":
        return "claude_harness_keyed"
    if provider_value in ("claude", "codex", "gemini", "kimi"):
        return "cli"
    if provider_value == "local-gemma":
        return "local"
    return None


def build_runtime_snapshot(
    vspec: ValidatedSpec,
    *,
    data_dir: Path,
) -> RuntimeSnapshot:
    """Perform all I/O required by compile_plan.

    Constraint enforcement runs for ALL providers including claude — this is the
    door's per-provider correctness gate. compile_plan's D3 then rejects on any
    blocking verdict before a single subprocess is spawned.

    Staging check reuses staging_validator._exists_in_dir logic (including path
    containment defense-in-depth).

    model_pins are derived from provider_constraints.yaml tiers:
      T0 → opus, T1/T2/T3 → sonnet

    Target health defaults to healthy (best-effort; the tmux lane is leaseless).
    """
    from providers.constraint_enforcer import check_constraints as _constraint_check  # noqa: PLC0415
    from staging_validator import _exists_in_dir as _staging_exists  # noqa: PLC0415

    spec = vspec.spec
    provider_value = spec.provider.value
    sub_provider = _sub_provider_for(provider_value)
    via = _via_for_provider(provider_value, sub_provider)

    model = spec.model or "sonnet"

    raw_violations = _constraint_check(
        provider=provider_value,
        sub_provider=sub_provider,
        model=model,
        terminal_id=spec.target_slot,
        role=spec.role,
        via=via,
        env=os.environ,
        check_registry=False,
    )

    constraint_verdicts = tuple(
        ConstraintVerdict(
            code=v.code,
            severity=v.severity,
            message=v.message,
            override_applied=v.override_applied,
        )
        for v in raw_violations
    )

    dispatches_dir = data_dir / "dispatches"
    staging_promoted = _staging_exists(dispatches_dir / "pending", spec.staging_id)

    model_pins: dict[str, str] = {
        "T0": "opus",
        "T1": "sonnet",
        "T2": "sonnet",
        "T3": "sonnet",
    }

    is_claude_lane = spec.provider == Provider.CLAUDE
    if is_claude_lane:
        target_health: dict[str, str] = {"ephemeral": "healthy"}
        target_capable: dict[str, bool] = {"ephemeral": True}
    else:
        target_id = spec.target_id_override or spec.target_slot
        target_health = {target_id: "healthy"}
        target_capable = {target_id: True}

    return RuntimeSnapshot(
        constraint_verdicts=constraint_verdicts,
        staging_promoted=staging_promoted,
        target_health=target_health,
        target_capable=target_capable,
        model_pins=model_pins,
    )


# ---------------------------------------------------------------------------
# Lane executors
# ---------------------------------------------------------------------------

def _execute_claude(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> int:
    """Execute a validated claude_tmux_subscription plan via TmuxInteractiveDispatch.

    require_permit is the first action — un-evadable. The tmux lane self-governs
    (emits its own receipt + report); the door's job is validate + permit +
    plan-faithful invocation. The claude serial lock and warmup are owned by the
    tmux lane itself.
    """
    from tmux_interactive_dispatch import TmuxInteractiveDispatch  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable gate — FIRST action, cannot be moved

    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")

    lane = TmuxInteractiveDispatch(state_dir)
    result = lane.dispatch(
        instruction,
        plan.dispatch_id,
        role=role,
        model=plan.model,
        dispatch_paths=[str(dp.path) for dp in plan.dispatch_paths],
        deadline_seconds=plan.deadline_seconds,
        base_ref=plan.base_ref,
        isolated_worktree=True,
    )
    return 0 if result.success else 1


# ---------------------------------------------------------------------------
# run_dispatch — the single door
# ---------------------------------------------------------------------------

def run_dispatch(spec_file: Path, *, dry_run: bool = False) -> int:
    """Turn a spec file into a governed dispatch for BOTH lanes.

    Returns 0 on success, 1 on any reject or execution failure.
    When dry_run=True, prints plan + permit fingerprint and spawns nothing.
    """
    project_id = _resolve_project_id()
    repo_root = _resolve_repo_root()
    data_dir = _resolve_data_dir()
    state_dir = data_dir / "state"

    try:
        spec = load_spec(spec_file)
    except Exception as exc:
        print(f"[dispatch_cli] REJECT [spec-parse-error]: {exc}", file=sys.stderr)
        return 1

    vspec = validate(spec, project_id=project_id, repo_root=repo_root)
    if isinstance(vspec, Reject):
        _emit_reject(vspec)
        return 1

    snapshot = build_runtime_snapshot(vspec, data_dir=data_dir)

    plan = compile_plan(vspec, snapshot)
    if isinstance(plan, Reject):
        _emit_reject(plan)
        return 1

    permit = issue_permit(plan)
    fp = fingerprint(permit)
    logger.info("[dispatch_cli] permit fingerprint: %s", fp)

    if dry_run:
        _print_plan(plan, fp)
        return 0

    if plan.lane == "provider":
        result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)
        return result.returncode
    elif plan.lane == "claude_tmux_subscription":
        return _execute_claude(
            plan,
            permit,
            state_dir=state_dir,
            data_dir=data_dir,
            role=vspec.spec.role,
        )
    else:
        raise ValueError(
            f"[dispatch_cli] closed set violated — unknown lane: {plan.lane!r}"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX single-entry dispatch gate (PR-4)"
    )
    parser.add_argument(
        "--spec-file", required=True, type=Path, dest="spec_file",
        help="Absolute path to dispatch-spec.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print plan + fingerprint; spawn nothing",
    )
    args = parser.parse_args(argv)
    return run_dispatch(args.spec_file, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
