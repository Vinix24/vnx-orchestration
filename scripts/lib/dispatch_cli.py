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
from dispatch_envelope import run_envelope_plan, run_envelope_headless_plan  # noqa: E402
from dispatch_serialization import force_release, serialize_lane  # noqa: E402


class _InvariantViolation(Exception):
    """A door closed-set or permit invariant was breached — a should-never-happen,
    security-relevant event, categorically distinct from a transient runtime error.
    Surfaced with its own reject code so the audit signal is not masked (audit finding A7)."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_data_dir(project_id: "str | None" = None) -> Path:
    """Resolve VNX data directory. Mirrors provider_dispatch._resolve_data_dir.

    ``project_id`` (when given) is the authoritative tenant — used to derive the
    central store ``~/.vnx-data/<project_id>`` so a caller that already knows the
    target project (e.g. the staged-bundle authority in ``run_dispatch``) does not
    fall back to the ambient ``VNX_PROJECT_ID``/``vnx-dev`` default.


    PR-4d trust boundary: the resolved data root is OPERATOR config, not attacker
    input. The threat model is our own agents, not an external adversary — and an
    operator who wants an external-drive layout would point VNX_DATA_DIR straight
    at it. A symlinked data root is therefore legitimate and is NOT rejected (that
    would break external-drive setups). What IS untrusted is anything PLANTED
    INSIDE this root by a dispatch (e.g. a symlinked dispatches/pending escaping
    it); that is closed by _check_pending_root_anchor_verdict below.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR", "")
    if explicit_flag and explicit_val:
        return Path(explicit_val).resolve()
    pid = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    return Path.home() / ".vnx-data" / pid


def _resolve_project_id() -> str:
    """Authoritative project_id for the door's ADR-007 guard.

    Delegates to the canonical resolver: VNX_PROJECT_ID env, then the nearest
    ``.vnx-project-id`` marker walking up from CWD. The old ``return
    os.environ.get("VNX_PROJECT_ID", "vnx-dev")`` HARDCODED ``vnx-dev`` as the
    fallback, so EVERY consumer dispatch that did not export VNX_PROJECT_ID
    resolved to ``vnx-dev`` — the guard then either mis-routed the entire
    governance state (receipt, report, spec, events, log) into the vnx-dev store
    or rejected the correct project_id as a "cross-project redirect". This hit
    every consumer (sales-copilot, mission-control, seocrawler). Resolving from
    the marker fixes the fleet; an unresolvable project fails closed (raises)
    rather than silently landing in vnx-dev.
    """
    from project_root import resolve_project_id  # noqa: PLC0415
    return resolve_project_id()


def _resolve_repo_root() -> Path:
    """scripts/lib/dispatch_cli.py -> repo root (parents[2])."""
    return Path(__file__).resolve().parents[2]


def _authority_from_spec_path(spec_file: Path) -> "tuple[str | None, Path | None]":
    """Derive (project_id, data_dir) from a staged bundle's PHYSICAL location.

    Bundle layout (stage_spec_bundle): ``<data_dir>/dispatches/pending/<id>/dispatch-spec.json``.
    The store the bundle physically lives in IS the dispatch's tenant authority — NOT the
    ambient CWD. In a central install the door's CWD is the shared engine tree, whose stray
    ``.vnx-project-id`` would mis-resolve every consumer to ``vnx-dev`` (misroute pre-#1091,
    hard-reject post-#1091). Deriving from the bundle location fixes the whole class and keeps
    the ADR-007 anti-redirect guard meaningful: a spec that declares a project_id different from
    the store it was staged into still fails validation.

    Returns ``(None, None)`` when ``spec_file`` is not under that layout (ad-hoc/test specs), so
    the caller falls back to ambient resolution.
    """
    try:
        p = Path(spec_file).resolve()
        if p.name != "dispatch-spec.json":
            return None, None
        if p.parents[1].name != "pending" or p.parents[2].name != "dispatches":
            return None, None
        data_dir = p.parents[3]
        from vnx_paths import project_id_from_state_dir  # noqa: PLC0415
        pid = project_id_from_state_dir(data_dir / "state")
        if not pid:
            return None, None
        return pid, data_dir
    except Exception:  # noqa: BLE001 — resolution is best-effort; fall back to ambient
        return None, None


# ---------------------------------------------------------------------------
# Spec loading from JSON
# ---------------------------------------------------------------------------

def _sanitize_headless_reason(raw: object) -> "str | None":
    """Strip newlines/control chars from headless_reason so multi-line values can't break log formatting."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _re.sub(r"[\x00-\x1f\x7f]+", " ", raw).strip()
    return cleaned or None


def load_spec(spec_file: Path) -> DispatchSpec:
    """Parse a DispatchSpec from a JSON dispatch-spec.json file."""
    raw = json.loads(spec_file.read_text(encoding="utf-8"))

    raw_paths = raw.get("dispatch_paths") or []
    dispatch_paths = tuple(
        DispatchPath(
            path=PurePosixPath(str(p["path"])),
            access=PathAccess(p.get("access", "read_write")),
            materialize_at_cwd=p.get("materialize_at_cwd") is True,
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
        track_id=(raw.get("track_id") or None),
        deadline_seconds=int(raw.get("deadline_seconds", 3600)),
        base_ref=str(raw.get("base_ref", "origin/main")),
        isolation=Isolation(raw.get("isolation", "worktree")),
        requires_mcp=raw.get("requires_mcp") is True,
        target_id_override=(raw.get("target_id_override") or None),
        tags=tuple(str(t) for t in (raw.get("tags") or [])),
        instruction_sha256=(raw.get("instruction_sha256") or None),
        allow_headless=raw.get("allow_headless") is True,
        headless_reason=_sanitize_headless_reason(raw.get("headless_reason")),
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

    PR-4d trust boundary: this anchor protects against symlinks PLANTED INSIDE the
    trusted data root (a dispatch-controlled `dispatches`/`dispatches/pending`
    that escapes it). The data root ITSELF is trusted operator config (see
    _resolve_data_dir) and is intentionally not rejected for being a symlink.
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
# TL-D1 — track_id door validation + persistence
#
# Structural link dispatch -> track, validated at the door (fail-closed on an
# invalid/nonexistent/done track_id) and staged advisory->required on absence
# via VNX_REQUIRE_DISPATCH_TRACK (mirrors the wiring_gate.py VNX_WIRING_GATE_REQUIRED
# shadow/blocking staging pattern). Tracks live in the same runtime_coordination.db
# as the dispatches table (schemas/migrations/0022_track_layer.sql).
# ---------------------------------------------------------------------------

_TRACKS_DB_FILENAME = "runtime_coordination.db"

_NO_TRACK_ESCAPE_RE = _re.compile(r"^no-track:.+$")


def _tracks_db_path(state_dir: Path) -> Path:
    return state_dir / _TRACKS_DB_FILENAME


def _has_col(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _has_no_track_escape(tags: "tuple[str, ...]") -> bool:
    return any(_NO_TRACK_ESCAPE_RE.match(t) for t in tags)


def _lookup_track_phase(db_path: Path, track_id: str, project_id: str) -> Optional[str]:
    """Return the track's phase for (track_id, project_id), or None if no such track.

    Read-only URI connection: a missing DB file raises immediately rather than
    silently creating an empty one. Caller degrades any exception to a WARN
    verdict (fail-open on tracks-DB unavailability; never crash the door).
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _check_track_link_verdict(spec: DispatchSpec, *, state_dir: Path) -> Optional[ConstraintVerdict]:
    """Validate spec.track_id at the door.

    - track_id present, references a nonexistent or already-done track -> blocking Reject
      (the tag-vs-link mistake becomes impossible).
    - track_id present, references a live track -> None (passes clean).
    - tracks DB unavailable while checking a present track_id -> WARN, never crash.
    - track_id absent -> staged advisory (VNX_REQUIRE_DISPATCH_TRACK OFF, default) WARN,
      or required (ON) blocking Reject unless tags carries a 'no-track:<reason>' escape.
    """
    track_id = (spec.track_id or "").strip()
    db_path = _tracks_db_path(state_dir)

    if track_id:
        try:
            phase = _lookup_track_phase(db_path, track_id, spec.project_id)
        except Exception as exc:
            return ConstraintVerdict(
                code="tracks-db-unavailable",
                severity="warn",
                message=(
                    f"tracks DB unavailable ({exc}); cannot verify track_id={track_id!r}, "
                    "degrading to warn"
                ),
            )
        if phase is None:
            return ConstraintVerdict(
                code="bad-track-link",
                severity="blocking",
                message=(
                    f"track_id={track_id!r} does not reference an existing track "
                    f"for project_id={spec.project_id!r}"
                ),
            )
        if phase == "done":
            return ConstraintVerdict(
                code="bad-track-link",
                severity="blocking",
                message=f"track_id={track_id!r} references a track already in phase='done'",
            )

        # Plan-first-gate enforcement (advisory-first). A live track whose OI-PLAN
        # plan-first gate is unresolved must not be dispatched: building before
        # planning is exactly what the gate exists to prevent, yet the gate used to
        # bind only closure bookkeeping. Shared read-only check lives in
        # plan_gate_enforcement so the merge gate applies the same rule.
        import plan_gate_enforcement as _pge  # noqa: PLC0415
        mode = _pge.enforce_mode()
        if mode != "off":
            try:
                pg_state = _pge.plan_gate_state(db_path, track_id, spec.project_id)
            except Exception:
                # DB race between the phase read and here; already fail-open above.
                pg_state = _pge.UNSUPPORTED
            if pg_state == _pge.UNRESOLVED:
                run_cmd = f"vnx horizon plan-gate run {track_id} --doc <plan-doc>"
                if mode == "required" and not _pge.override_active():
                    return ConstraintVerdict(
                        code="plan-gate-unresolved",
                        severity="blocking",
                        message=(
                            f"track_id={track_id!r} has not passed its plan-first gate "
                            f"(OI-PLAN-{track_id} unresolved). Plan before work: run "
                            f"`{run_cmd}` (or `vnx horizon plan-gate attest {track_id}`), or "
                            f"operator-override with VNX_OVERRIDE_PLAN_GATE=1."
                        ),
                    )
                overridden = mode == "required" and _pge.override_active()
                return ConstraintVerdict(
                    code="plan-gate-unresolved",
                    severity="warn",
                    message=(
                        f"track_id={track_id!r} plan-first gate unresolved "
                        f"(VNX_PLAN_GATE_ENFORCE={mode}"
                        + (", operator override applied" if overridden else "")
                        + f"); advisory. Run `{run_cmd}` before dispatching."
                    ),
                    override_applied=overridden,
                )
        return None

    import config_runtime
    required = config_runtime.get_bool("VNX_REQUIRE_DISPATCH_TRACK")
    if not required:
        return ConstraintVerdict(
            code="track_unlinked",
            severity="warn",
            message="dispatch has no track_id (VNX_REQUIRE_DISPATCH_TRACK is OFF; advisory-only)",
        )
    if _has_no_track_escape(spec.tags):
        logger.info(
            "[dispatch_cli] dispatch=%s: no-track escape applied (tags=%r)",
            spec.dispatch_id, spec.tags,
        )
        return None
    return ConstraintVerdict(
        code="track-required",
        severity="blocking",
        message=(
            "VNX_REQUIRE_DISPATCH_TRACK=1 requires a track_id; add a tags entry "
            "'no-track:<reason>' to opt out for a genuinely exploratory dispatch"
        ),
    )


def _persist_track_id(spec: DispatchSpec, *, state_dir: Path) -> None:
    """Best-effort: attach spec.track_id to an EXISTING dispatches row (UPDATE-only).

    Never INSERTs. The dispatches table (runtime_coordination.db) is read by the
    worker-pool claim query, the runtime reconciler, and the runtime supervisor's
    stuck/ghost-dispatch sweeps, all keyed off `state`. Fabricating a row for the
    leaseless claude-tmux lane (which has no pre-existing tracker row) risks
    tripping those sweeps. D2 treats an absent/None track_id as a no-op, so a
    dispatch with no pre-existing row is a safe, anticipated case here, not a
    partial failure. Adds the track_id column additively (_has_col-guarded) when
    missing. Never raises.
    """
    track_id = (spec.track_id or "").strip()
    if not track_id:
        return
    db_path = _tracks_db_path(state_dir)
    if not db_path.exists():
        return
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_col(conn, "dispatches", "track_id"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN track_id TEXT")
                conn.commit()
            conn.execute(
                "UPDATE dispatches SET track_id = ? WHERE dispatch_id = ? AND project_id = ?",
                (track_id, spec.dispatch_id, spec.project_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[dispatch_cli] track_id persist skipped: %s", exc)


# ---------------------------------------------------------------------------
# build_runtime_snapshot — all I/O lives here
# ---------------------------------------------------------------------------

def _sub_provider_for(provider_value: str) -> Optional[str]:
    if provider_value.startswith("litellm:"):
        return provider_value.split(":", 1)[1].split(":", 1)[0] or None
    if provider_value == "deepseek-harness":
        return "deepseek"
    if provider_value == "glm-harness":
        # sub=zai lets a future glm-specific constraint match forbidden_route.provider=zai;
        # the distinct harness via (below) is what clears zai-via-openrouter-only AND
        # glm-via-harness-only (mirrors the deepseek-harness sub=deepseek/keyed-via pattern).
        return "zai"
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
    if provider_value == "glm-harness":
        # Distinct harness via (NOT plain "openrouter"): the claude CLI pointed at the local
        # :4141 litellm proxy → OpenRouter. Clears zai-via-openrouter-only (via != direct) AND
        # glm-via-harness-only (via not in [openrouter, litellm]); plain litellm:zai (via=openrouter)
        # stays blocked. Must match provider_dispatch._constraint_via_for_provider for glm-harness.
        return "claude_harness_openrouter"
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

    P0-1: instruction_text + check_registry=True (FAIL-CLOSED); effective model; SDK scan (warn via constraint engine).
    P0-2: staging binding verified via spec_file containment check.
    P1-#3: model_pins from provider_constraints.yaml SSOT.
    """
    from providers.constraint_enforcer import check_constraints as _constraint_check  # noqa: PLC0415
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

    # claude-headless enforcement: allow_headless=True sets via to 'headless', which
    # triggers the claude-headless forbid_route constraint. Normal tmux lane keeps via='cli'.
    if is_claude_lane and spec.allow_headless:
        via = "headless"

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

    # TL-D1: track_id door validation (fail-closed on invalid/nonexistent/done;
    # staged advisory->required WARN/Reject when absent).
    track_verdict = _check_track_link_verdict(spec, state_dir=data_dir / "state")
    if track_verdict is not None:
        constraint_verdicts = constraint_verdicts + (track_verdict,)

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
    from tmux_interactive_dispatch import (  # noqa: PLC0415
        TmuxInteractiveDispatch,
        _resolve_invocation_project_root,
    )

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

    # Thread the PROJECT repo root from the invocation context (VNX_PROJECT_ROOT /
    # cwd-git), NOT the lane code's __file__: in central-install mode the code lives
    # under the shared keystone, so the constructor's __file__ fallback would spawn
    # the worker in the keystone instead of the operator's project.
    lane = TmuxInteractiveDispatch(
        state_dir, project_root=_resolve_invocation_project_root()
    )
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


def _execute_claude_headless(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> int:
    """Execute a validated claude_headless plan via ClaudeSubprocessAdapter (headless api_metered).

    Delegates all permit verification, TOCTOU check, and GOVERN to
    run_envelope_headless_plan — same security contract as the provider lane.

    Fail-closed last gate before spawn: consults lane_safety.headless_block from
    routing_policy.yaml (OI-223) instead of a hardcoded env check, so the yaml stays
    the single source of truth for the block and its override var name.
    """
    from routing_policy import is_claude_headless_blocked, load_lane_safety  # noqa: PLC0415
    lane_safety = load_lane_safety()
    if is_claude_headless_blocked(lane_safety):
        override_env = (lane_safety.get("headless_block") or {}).get(
            "override_env", "VNX_OVERRIDE_CLAUDE_HEADLESS"
        )
        raise PermissionError(
            f"claude_headless lane blocked by default; set {override_env}=1 to opt in"
        )
    result = run_envelope_headless_plan(plan, permit, state_dir=state_dir, data_dir=data_dir, role=role)
    return result.returncode


# ---------------------------------------------------------------------------
# run_dispatch — the single door
# ---------------------------------------------------------------------------

def run_dispatch(spec_file: Path, *, dry_run: bool = False) -> int:
    """Turn a spec file into a governed dispatch for BOTH lanes.

    Returns 0 on success, 1 on any reject or execution failure.
    When dry_run=True, prints plan + permit fingerprint and spawns nothing.
    """
    # Authority = where the bundle is PHYSICALLY staged, not ambient CWD/env. In a
    # central install the door's CWD is the shared engine tree (its stray
    # .vnx-project-id would mis-resolve every consumer to vnx-dev). Fall back to
    # ambient resolution only when the spec isn't under the staged-bundle layout
    # (ad-hoc/test specs).
    derived_pid, derived_data_dir = _authority_from_spec_path(spec_file)

    # ADR-007 independent-authority cross-check (codex gate PR #1093): when the OPERATOR
    # explicitly pins the tenant / data root (VNX_PROJECT_ID / VNX_DATA_DIR_EXPLICIT), the
    # staged-bundle authority MUST agree. This restores the anti-redirect guard whenever an
    # independent authority exists — a bundle physically staged under a different project's
    # store than the pinned one is a cross-project redirect and is rejected. Without an
    # explicit pin (the common central-install case) the store's filesystem write-access is
    # the trust boundary, per the PR-4d model ("resolved data root is OPERATOR config; the
    # threat model is our own agents, not an external adversary").
    if derived_pid:
        env_pid = (os.environ.get("VNX_PROJECT_ID") or "").strip()
        if env_pid and env_pid != derived_pid:
            _emit_reject(Reject(
                "project-mismatch",
                f"staged-bundle project_id={derived_pid!r} != pinned VNX_PROJECT_ID={env_pid!r}; "
                "caller cannot redirect state to another project",
            ))
            return 1
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
            explicit = (os.environ.get("VNX_DATA_DIR") or "").strip()
            if (
                explicit
                and derived_data_dir is not None
                and Path(explicit).resolve() != derived_data_dir.resolve()
            ):
                _emit_reject(Reject(
                    "project-mismatch",
                    f"staged-bundle data_dir={derived_data_dir} != pinned VNX_DATA_DIR={explicit}; "
                    "refusing to write state outside the operator-pinned data root",
                ))
                return 1

    project_id = derived_pid or _resolve_project_id()
    repo_root = _resolve_repo_root()
    data_dir = derived_data_dir or _resolve_data_dir(derived_pid)
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

        # Scout pre-pass (opt-in VNX_SCOUT_PREPASS, fail-open): a cheap key-auth
        # model ranks the deterministic anchors into a sidecar BEFORE the permit
        # is issued. It reads vspec.instruction_text in-memory and writes a
        # SEPARATE sidecar file — it never touches the instruction, so the
        # permit / instruction_sha256 TOCTOU below is untouched. Never blocks the
        # door (best-effort, never raises).
        if not dry_run:
            try:
                from scout_prepass import maybe_run_scout
                maybe_run_scout(
                    dispatch_id=plan.dispatch_id,
                    instruction_text=vspec.instruction_text,
                    dispatch_paths=[dp.path for dp in vspec.spec.dispatch_paths],
                    state_dir=state_dir,
                    task_class=getattr(vspec.spec, "task_class", None),
                    lane=plan.lane,
                )
            except Exception as exc:
                logger.debug("[dispatch_cli] scout pre-pass skipped: %s", exc)

            # TL-D1: export the resolved track_id alongside VNX_CURRENT_DISPATCH_ID and
            # persist it onto the dispatch tracker row so D2 can propagate it to
            # track.pr_ref on merge. Best-effort — never blocks the door.
            if vspec.spec.track_id:
                os.environ["VNX_CURRENT_TRACK_ID"] = vspec.spec.track_id
                _persist_track_id(vspec.spec, state_dir=state_dir)

        permit = issue_permit(plan)
        try:
            require_permit(plan, permit)  # P1-#6: door backstop for BOTH lanes
        except PermissionError as exc:
            raise _InvariantViolation(f"permit invariant breached: {exc}") from exc
        fp = fingerprint(permit)
        logger.info("[dispatch_cli] permit fingerprint: %s", fp)

        if dry_run:
            _print_plan(plan, fp)
            return 0

        with serialize_lane(plan.serialization_class, dispatch_id=vspec.spec.dispatch_id):
            if plan.lane == "provider":
                result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)
                if result.status != "success":
                    # Fail-loud: the door must never swallow a provider-lane failure into a
                    # bare exit code — the caller (bin/vnx dispatch) prints nothing else.
                    print(
                        f"[dispatch_cli] provider lane {result.status}: "
                        f"{result.error or '(no error captured)'}",
                        file=sys.stderr,
                    )
                return result.returncode
            elif plan.lane == "claude_tmux_subscription":
                return _execute_claude(
                    plan,
                    permit,
                    state_dir=state_dir,
                    data_dir=data_dir,
                    role=vspec.spec.role,
                )
            elif plan.lane == "claude_headless":
                return _execute_claude_headless(
                    plan,
                    permit,
                    state_dir=state_dir,
                    data_dir=data_dir,
                    role=vspec.spec.role,
                )
            else:
                raise _InvariantViolation(
                    f"closed set violated — unknown lane: {plan.lane!r}"
                )

    except _InvariantViolation as exc:
        logger.error(
            "[dispatch_cli] INVARIANT VIOLATION dispatch=%s: %s",
            getattr(getattr(vspec, "spec", None), "dispatch_id", "?"), exc,
        )
        print(f"[dispatch_cli] REJECT [invariant-violation]: {exc}", file=sys.stderr)
        return 1
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
        "--spec-file", type=Path, dest="spec_file",
        help="Absolute path to dispatch-spec.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print plan + fingerprint; spawn nothing",
    )
    parser.add_argument(
        "--force-release-lock", dest="force_release_class",
        metavar="CLASS", nargs="?", const="claude-tmux", default=None,
        help="Release stale lock for CLASS (default: claude-tmux); "
             "prints prior holder and removes lock file",
    )
    args = parser.parse_args(argv)

    if args.force_release_class is not None:
        force_release(args.force_release_class)
        return 0

    if args.spec_file is None:
        parser.error("--spec-file is required")

    return run_dispatch(args.spec_file, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
