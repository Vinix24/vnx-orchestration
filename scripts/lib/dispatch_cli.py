"""dispatch_cli.py — Single-entry dispatch gate (PR-4).

spec -> validate -> snapshot -> compile_plan -> permit -> execute

Feature-gated by VNX_SINGLE_ENTRY_DISPATCH=1 in dispatch.sh. When the flag is
unset the bash layer uses the legacy path; this module's logic is unchanged.

BILLING SAFETY: no anthropic SDK import. Claude lane executes via interactive
tmux (subscription). Provider lane executes via run_envelope_plan (provider_metered).
"""

from __future__ import annotations

import hashlib
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
from dispatch_internal import (  # noqa: E402
    ExecutionPermit,
    is_valid_instruction_hash,
    issue_permit,
    require_permit,
)
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
        instruction_sha256=(raw.get("instruction_sha256") or None),
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
# P1-#3: model pins from SSOT
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_PINS: dict[str, str] = {
    "T0": "opus",
    "T1": "sonnet",
    "T2": "sonnet",
    "T3": "sonnet",
}


def _load_model_pins_from_yaml() -> dict[str, str]:
    """Load T0/T1/T2/T3 model pins from provider_constraints.yaml SSOT.

    Falls back to DEFAULT_MODEL_PINS on any read/parse error (never raises).
    """
    yaml_path = _LIB_DIR / "providers" / "provider_constraints.yaml"
    try:
        import yaml  # noqa: PLC0415
        with yaml_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict) or data.get("version") != 1:
            return dict(_DEFAULT_MODEL_PINS)
        pins: dict[str, str] = {}
        for constraint in (data.get("constraints") or []):
            cid = str(constraint.get("id", ""))
            required = constraint.get("required_route") or {}
            model = required.get("model")
            if not model:
                continue
            if cid == "t0-opus-only":
                pins["T0"] = str(model)
            elif cid == "workers-sonnet-pinned":
                for slot in ("T1", "T2", "T3"):
                    pins[slot] = str(model)
        return {**_DEFAULT_MODEL_PINS, **pins}
    except Exception:
        return dict(_DEFAULT_MODEL_PINS)


# ---------------------------------------------------------------------------
# P0-2: staging binding check helpers
# ---------------------------------------------------------------------------

# Mirrors _DISPATCH_ID_RE from staging_validator.py
import re as _re
_PENDING_ID_RE = _re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$')


def _check_pending_root_anchor_verdict(data_dir: Path) -> Optional[ConstraintVerdict]:
    """Return BLOCKING ConstraintVerdict if dispatches/pending escapes the data root.

    P0-2: the binding/existence checks resolve bundle paths *relative to* the
    pending root. If `dispatches` or `pending` is a symlink that hops outside the
    trusted data_dir, an external bundle resolves "inside" the (escaped) pending
    root and every downstream containment check passes — fail-OPEN. Anchor the
    pending root first: its fully-resolved path must stay under the resolved
    data_dir, else refuse to promote. Returns None on pass.
    """
    try:
        data_root = data_dir.resolve()
        pending_root = (data_dir / "dispatches" / "pending").resolve()
    except (ValueError, OSError) as exc:
        return ConstraintVerdict(
            code="ADR-006-untrusted-root",
            severity="blocking",
            message=f"pending root resolution failed: {exc}",
        )
    if not pending_root.is_relative_to(data_root):
        return ConstraintVerdict(
            code="ADR-006-untrusted-root",
            severity="blocking",
            message=(
                f"dispatches/pending escapes the trusted data root: resolved "
                f"{pending_root} is not under {data_root} — refusing to promote"
            ),
        )
    return None


def _check_staging_binding_verdict(
    spec_file: Path,
    instruction_file: Path,
    *,
    data_dir: Path,
    staging_id: str,
) -> Optional[ConstraintVerdict]:
    """Return BLOCKING ConstraintVerdict if spec_file or instruction_file escape the bundle.

    Follows symlinks via resolve() so symlink escapes are caught. Returns None on pass.

    P0-2: the bundle dir is anchored under the resolved data root before the
    per-file containment checks, so a symlinked `staging_id` (or any symlink in
    the pending path) that resolves outside the data root is rejected rather than
    silently trusted.
    """
    try:
        data_root = data_dir.resolve()
        bundle_dir = (data_dir / "dispatches" / "pending" / staging_id).resolve()
        if not bundle_dir.is_relative_to(data_root):
            return ConstraintVerdict(
                code="ADR-006-untrusted-root",
                severity="blocking",
                message=(
                    f"bundle pending/{staging_id}/ escapes the trusted data root: "
                    f"resolved {bundle_dir} is not under {data_root}"
                ),
            )
        sf_resolved = spec_file.resolve()
        if not sf_resolved.is_relative_to(bundle_dir):
            return ConstraintVerdict(
                code="ADR-006-binding",
                severity="blocking",
                message=(
                    f"spec_file is not inside bundle pending/{staging_id}/: "
                    f"got {sf_resolved}"
                ),
            )
        if_resolved = instruction_file.resolve()
        if not if_resolved.is_relative_to(bundle_dir):
            return ConstraintVerdict(
                code="ADR-006-binding",
                severity="blocking",
                message=(
                    f"instruction_file is not inside bundle pending/{staging_id}/: "
                    f"got {if_resolved}"
                ),
            )
    except (ValueError, OSError) as exc:
        return ConstraintVerdict(
            code="ADR-006-binding",
            severity="blocking",
            message=f"staging binding path resolution failed: {exc}",
        )
    return None


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
    spec_file: Path,
) -> RuntimeSnapshot:
    """Perform all I/O required by compile_plan.

    P0-1: instruction_text + check_registry=True (FAIL-CLOSED); effective model; SDK scan (blocking).
    P0-2: staging binding verified via spec_file containment check.
    P1-#3: model_pins from provider_constraints.yaml SSOT.
    """
    from providers.constraint_enforcer import (  # noqa: PLC0415
        check_constraints as _constraint_check,
        scan_anthropic_sdk_text as _scan_sdk,
    )
    from staging_validator import _exists_in_dir as _staging_exists  # noqa: PLC0415

    spec = vspec.spec
    provider_value = spec.provider.value
    sub_provider = _sub_provider_for(provider_value)
    via = _via_for_provider(provider_value, sub_provider)

    # P1-#3: model_pins from SSOT
    model_pins = _load_model_pins_from_yaml()

    # P0-1: effective model — same computation compile_plan uses in D4
    is_claude_lane = spec.provider == Provider.CLAUDE
    if is_claude_lane:
        effective_model = model_pins.get(spec.target_slot) or spec.model or "sonnet"
    else:
        effective_model = spec.model or "default"

    # P0-1: constraint check with instruction_text + check_registry=True; FAIL-CLOSED on error
    constraint_verdicts: tuple[ConstraintVerdict, ...] = ()
    try:
        raw_violations = _constraint_check(
            provider=provider_value,
            sub_provider=sub_provider,
            model=effective_model,
            terminal_id=spec.target_slot,
            role=spec.role,
            via=via,
            env=os.environ,
            check_registry=True,
            instruction_text=vspec.instruction_text,
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
    except Exception as exc:
        constraint_verdicts = (ConstraintVerdict(
            code="registry-unavailable",
            severity="blocking",
            message=f"Constraint registry unavailable — fail-closed: {exc}",
        ),)

    # P0-1: direct blocking SDK import scan (dispatch-time gate, belt-and-suspenders
    # vs CI grep). Whitespace-aware so `import\tanthropic` / `import   anthropic`
    # cannot slip past as they did with literal-substring matching.
    if _scan_sdk(vspec.instruction_text):
        constraint_verdicts = constraint_verdicts + (ConstraintVerdict(
            code="no-anthropic-sdk",
            severity="blocking",
            message="Instruction references forbidden Anthropic SDK (dispatch-time gate)",
        ),)

    # P0-2: anchor the pending root BEFORE trusting any bundle path. A symlinked
    # dispatches/pending that escapes the data root cannot host a promoted bundle
    # (fail-closed) — checked unconditionally so it holds even if existence is faked.
    root_verdict = _check_pending_root_anchor_verdict(data_dir)
    if root_verdict is not None:
        constraint_verdicts = constraint_verdicts + (root_verdict,)

    # Staging existence check (belt-and-suspenders; binding check below is the specific gate)
    dispatches_dir = data_dir / "dispatches"
    staging_promoted = _staging_exists(dispatches_dir / "pending", spec.staging_id)

    # P0-2: staging binding — spec_file and instruction_file must be inside the bundle dir
    if staging_promoted:
        binding_verdict = _check_staging_binding_verdict(
            spec_file,
            spec.instruction_file,
            data_dir=data_dir,
            staging_id=spec.staging_id,
        )
        if binding_verdict is not None:
            constraint_verdicts = constraint_verdicts + (binding_verdict,)

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

    require_permit is the first action — un-evadable. P0-3: sha256 of the instruction
    file is re-verified immediately before delivery to detect TOCTOU swaps.
    """
    from tmux_interactive_dispatch import TmuxInteractiveDispatch  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable gate — FIRST action, cannot be moved

    # P0-3 (PR-4c): REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    # The old `if plan.instruction_sha256:` guard fell OPEN on an empty hash, letting
    # an empty-hash plan + valid permit spawn mutated content. No hash → no spawn.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        raise PermissionError(
            f"plan.instruction_sha256 is not a valid 64-hex digest "
            f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        raise PermissionError(
            f"instruction file mutated after permit: sha256 mismatch "
            f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
        )

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

    # P1-#1: wrap everything after validate in try/except — door never panics
    try:
        snapshot = build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)

        plan = compile_plan(vspec, snapshot)
        if isinstance(plan, Reject):
            _emit_reject(plan)
            return 1

        permit = issue_permit(plan)
        require_permit(plan, permit)  # P1-#6: door backstop for BOTH lanes
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

    except Exception as exc:
        print(f"[dispatch_cli] REJECT [runtime-error]: {exc}", file=sys.stderr)
        return 1


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
