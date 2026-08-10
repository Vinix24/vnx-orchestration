"""dispatch_envelope.py — Flag-gated unified dispatch envelope (PR-1 codex, PR-2 claude-subprocess).

Strangler-fig approach: legacy default OFF, each lane activated per VNX_UNIFIED_ENVELOPE_LANES.
Activate with VNX_UNIFIED_ENVELOPE=1 and VNX_UNIFIED_ENVELOPE_LANES containing "codex"
and/or "claude-subprocess".

Seams: PREPARE -> ROUTE -> EXECUTE -> GOVERN

GOVERN is fail-closed: a missing receipt_path raises EnvelopeGovernError — never
silently loses a receipt. Report is emitted before the receipt so the receipt carries
the linkage even when the report file is new (ADR-005).

Per-lane dual-receipt safety: GOVERN emits both report AND receipt. When the
receipt NDJSON already contains a line for this dispatch_id (e.g. written by
deliver_with_recovery's internal close-out), the GOVERN receipt write is skipped
(idempotent dedup). No double-emit.

Reuses:
  - spawn_codex from provider_spawns.codex_spawn (no reimplementation)
  - spawn_claude from provider_spawns.claude_spawn (no reimplementation)
  - emit_dispatch_receipt + emit_unified_report from governance_emit

No new hooks. Idempotent receipts (governance_emit uses fcntl.flock).
EventStore wiring and cost emission are open items for later PRs (PR-1 scope: flag-gate only).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

# ExecutionPlan / ExecutionPermit: the plan + permit types routed by the
# adapters and run_envelope_plan. Runtime import (not TYPE_CHECKING) so
# typing.get_type_hints() on these signatures resolves them — the F821
# unresolved-name finding (OI-288) was hiding a get_type_hints NameError.
# No import cycle: dispatch_plan -> dispatch_spec and dispatch_internal
# are stdlib-only leaf modules.
from dispatch_internal import ExecutionPermit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402

# Runtime import (not TYPE_CHECKING): typing.get_type_hints() on functions
# defined here (e.g. run_envelope_plan) resolves against THIS module's
# __globals__, so these names must be bound at runtime, not just for
# static type checkers.
from envelope_types import (  # noqa: E402
    EnvelopeGovernError,
    EnvelopeResult,
    EnvelopeSpec,
    _AdapterResult,
)
from envelope_prepare import (  # noqa: E402
    _prepare,
    _record_integrity,
    _verify_role_application,
)
from envelope_govern_support import (  # noqa: E402
    _archive_dispatch_events,
    _clear_dispatch_events,
    _receipt_exists_for_dispatch,
    _resolve_fix_forward_diff,
    _resolve_phantom_diff,
    _unknown_verification,
    _verification_from_report,
)
from envelope_govern import _govern  # noqa: E402
from envelope_adapters_claude import (  # noqa: E402
    ClaudeSubprocessAdapter,
    CodexAdapter,
)
from envelope_adapters_provider import ProviderAdapter  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rij-7 (lane-matrix): push+PR-verplichting op de envelope-lanes.
#
# Both envelope lanes (run_envelope_plan / run_envelope_headless_plan) create a
# dispatch worktree and run a worker in it. Until this hook, neither lane bound
# the push+PR obligation: a worker that committed but never pushed (or pushed
# but never opened a PR) completed with status="success" and work stranded.
#
# The per-state decision is NOT reimplemented here — it lives in the ONE binding
# site, pr_enforcement.enforce_pr_exists, which the tmux lane already calls. The
# envelope lanes reuse that same module and the shared classify_path() verdict,
# so two copies of the decision can never drift (the exact OI-1099 mistake).
#
# Runs BEFORE remove_dispatch_worktree (the worktree is the only handle to the
# local branch) and BEFORE _govern (so a push/PR failure is reflected in the
# governed report and the EnvelopeResult, not a silent "success").
# ---------------------------------------------------------------------------


def _is_dispatch_branch_ref(base_ref: str) -> bool:
    """Return True when *base_ref* names a dispatch branch on origin.

    A dispatch branch has the form ``origin/dispatch/<id>``.  When the
    dispatch's base_ref is a dispatch branch, the lane must NOT create a
    second auto-PR alongside the existing one (OI-1115).
    """
    return base_ref.startswith("origin/dispatch/")


def _enforce_push_pr(
    *,
    dispatch_id: str,
    branch: str,
    wt_path: Path,
    repo_root: Path,
    receipts_file: "str | Path",
    result: "_AdapterResult",
    base_ref: str = "origin/main",
    target_remote_head: "Optional[str]" = None,
    skip_pr: bool = False,
) -> "_AdapterResult":
    """Enforce the rij-7 push+PR obligation on an envelope-lane worktree.

    Classifies the worktree via the shared tmux_worktree.classify_path() and calls
    pr_enforcement.enforce_pr_exists() — the same module the tmux lane uses. When
    enforcement is applicable and fails (push or PR), the adapter result is
    rewritten to status="failure" so the governed EnvelopeResult is non-zero.
    pr_enforcement appends the corrective receipt itself.

    *target_remote_head* (OI-1113): the remote HEAD of *branch* captured BEFORE
    the worker started.  Passed through to enforce_pr_exists for containment
    verification after push.

    *skip_pr* (OI-1115): when True, the auto-PR creation is skipped.  Set when
    *base_ref* is a dispatch branch (the PR already exists).

    A non-applicable state (clean/dirty) leaves *result* unchanged. Never raises:
    an internal error fails open (the worktree is still torn down by the caller's
    finally block), matching the tmux lane's _enforce_pr_exists contract.

    ``base_ref`` is kept as a last-resort fallback only. The authoritative base
    SHA is read from the worktree claim ``create_dispatch_worktree`` wrote
    (``read_worktree_base_sha``) — the SAME source ``remove_dispatch_worktree``
    uses for L3 reap, so the allocator's recorded base is the single
    classification input across every lane. Re-deriving ``base_sha`` from
    ``plan.base_ref`` (the prior behavior) was OI-1106's root cause: the
    allocator bases the worktree on ``origin/main`` (or
    ``VNX_BENCH_WORKTREE_BASE_REF``) while ``plan.base_ref`` often names a local
    ``main`` ref; when the two disagree (a stale local ``main`` behind
    ``origin/main``, or a PR merge-commit checkout), a commit-less worktree is
    misclassified ``committed``/``pushed`` and the guard rejects a real success
    with ``status="failure"``. A guard that flips a different test each run is
    worse than none, so the base is never re-derived from a lane-local ref when
    the claim is available. When the claim is absent (test fixture that stubs
    the allocator), the explicit ``base_ref`` is resolved with a loud
    degradation log; when even that is unresolvable, ``classify_path`` degrades
    clean-safe and the degradation is surfaced, never guessed ``committed``.
    """
    try:
        from dispatch_worktree_isolation import (  # noqa: PLC0415
            read_worktree_base_sha,
        )
        base_sha, _claim_reason = read_worktree_base_sha(
            dispatch_id, project_root=repo_root
        )
        if base_sha is None:
            # Claim unavailable (stubbed allocator in tests, or pre-L3 claim).
            # Fall back to the explicit base_ref, LOUDLY — never silently guess.
            base_sha = _resolve_base_sha(repo_root=repo_root, base_ref=base_ref)
            logger.warning(
                "envelope: base_sha from claim unavailable for dispatch=%s (%s) — "
                "falling back to base_ref=%r resolved=%s; classify_path will degrade "
                "clean-safe if this too is unresolvable.",
                dispatch_id, _claim_reason, base_ref,
                base_sha[:12] if base_sha else None,
            )
        from tmux_worktree import classify_path  # noqa: PLC0415
        state = classify_path(
            wt=wt_path, branch=branch, dispatch_id=dispatch_id, base_sha=base_sha,
        )
        from pr_enforcement import enforce_pr_exists  # noqa: PLC0415
        pr_result = enforce_pr_exists(
            dispatch_id=dispatch_id,
            branch=branch,
            worktree_state=state,
            repo_root=repo_root,
            receipts_file=receipts_file,
            pr_title=f"dispatch({dispatch_id}): auto-created by VNX envelope lane",
            pr_body=(
                f"Auto-created by VNX envelope build-dispatch completion "
                f"(dispatch `{dispatch_id}`) — the worker left this branch "
                "without an open PR. Please review before merging."
            ),
            target_remote_head=target_remote_head,
            skip_pr=skip_pr,
        )
    except Exception as exc:  # noqa: BLE001 — never block a real completion on this guard
        logger.error(
            "envelope: PR-enforcement guard errored dispatch=%s: %s", dispatch_id, exc,
        )
        return result

    if not pr_result.applicable:
        return result
    if pr_result.ok:
        logger.info(
            "envelope: PR-enforcement OK dispatch=%s state=%s pr=%s created=%s",
            dispatch_id, state, pr_result.pr_number, pr_result.created,
        )
        return result

    logger.warning(
        "envelope: PR-enforcement REJECTED dispatch=%s state=%s — %s",
        dispatch_id, state, pr_result.reason,
    )
    return _AdapterResult(
        returncode=1,
        completion_text=result.completion_text,
        status="failure",
        token_usage=result.token_usage,
        error=f"dispatch_branch_no_pr (state={state}): {pr_result.reason}",
        session_id=result.session_id,
        model=result.model,
    )


def _resolve_base_sha(*, repo_root: Path, base_ref: str) -> "str | None":
    """Resolve the SHA of *base_ref* (``origin/main`` by default) in *repo_root*.

    Mirrors the tmux lane's ``WorktreeHandle.base_sha`` resolution: the commit a
    dispatch worktree was based on, resolved BEFORE ``git worktree add``. Returns
    None when the ref cannot be resolved (shallow clone without ``origin/main``,
    detached checkout, etc.) — ``classify_path`` then falls back to its own
    merge-base logic and, failing that, to a clean-safe default rather than a
    false ``committed``. Never raises: a resolve error is logged and treated as
    "base unknown", which degrades to the conservative clean path.
    """
    import subprocess  # noqa: PLC0415
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", base_ref],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — resolve failure degrades, never crashes
        logger.warning("envelope: base_sha resolve raised for %s: %s", base_ref, exc)
        return None
    if proc.returncode != 0:
        logger.info(
            "envelope: base_ref %s unresolvable in %s — classify_path will fall back",
            base_ref, repo_root,
        )
        return None
    return proc.stdout.strip() or None


def _dispatch_branch_name(dispatch_id: str) -> str:
    """The local branch name create_dispatch_worktree allocates for *dispatch_id*."""
    from dispatch_worktree_isolation import _sanitize_dispatch_id  # noqa: PLC0415
    return f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"


def _receipts_file_for(state_dir: Path) -> Path:
    """The NDJSON ledger the corrective receipt is appended to — the same path
    envelope_govern._govern writes the dispatch receipt to
    (``<state_dir>/t0_receipts.ndjson``), mirroring the tmux lane's
    self._receipts_file convention."""
    return Path(state_dir) / "t0_receipts.ndjson"


# ---------------------------------------------------------------------------
# Adapters -- CodexAdapter, ClaudeSubprocessAdapter moved to
# envelope_adapters_claude.py (dispatch-monolith-split, PR-5 of 6); imported
# above at bare name so every existing dispatch_envelope.CodexAdapter /
# dispatch_envelope.ClaudeSubprocessAdapter-shaped coupling keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lane router
# ---------------------------------------------------------------------------

_LANE_REGISTRY: Dict[str, type] = {
    "codex": CodexAdapter,
    "claude-subprocess": ClaudeSubprocessAdapter,
}


class LaneRouter:
    """Maps a lane name to an adapter instance."""

    def get(self, lane: str) -> object:
        cls = _LANE_REGISTRY.get(lane)
        if cls is None:
            raise ValueError(f"LaneRouter: no adapter registered for lane={lane!r}")
        return cls()


# ---------------------------------------------------------------------------
# PREPARE — _prepare, _record_integrity, _verify_role_application moved to
# envelope_prepare.py (dispatch-monolith-split, PR-2 of 6); imported above
# at bare name so every existing dispatch_envelope._prepare-shaped coupling
# keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GOVERN support -- _receipt_exists_for_dispatch, _resolve_fix_forward_diff,
# _resolve_phantom_diff, _unknown_verification, _verification_from_report,
# _archive_dispatch_events, _clear_dispatch_events moved to
# envelope_govern_support.py (dispatch-monolith-split, PR-3 of 6); imported
# above at bare name so every existing dispatch_envelope._name-shaped
# coupling keeps binding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GOVERN -- _govern moved to envelope_govern.py (dispatch-monolith-split,
# PR-4 of 6); imported above at bare name so every existing
# dispatch_envelope._govern-shaped coupling keeps binding. Every call site
# below (run_envelope, run_envelope_plan, run_envelope_headless_plan) stays
# in this facade, so patch("dispatch_envelope._govern") keeps binding
# against THIS module's globals unchanged by the move.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Envelope entry point
# ---------------------------------------------------------------------------


def run_envelope(spec: EnvelopeSpec, lane: str = "codex") -> EnvelopeResult:
    """Run PREPARE -> ROUTE -> EXECUTE -> GOVERN for the given lane.

    Returns EnvelopeResult on success / failure / timeout.
    Raises EnvelopeGovernError when GOVERN cannot confirm a receipt (fail-closed).
    """
    # ROUTE
    router = LaneRouter()
    adapter = router.get(lane)

    # PREPARE — enrich instruction (best-effort; failure falls back to original)
    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        # Chain-link — carried through PREPARE unchanged.
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — persist the enriched final prompt + verify raw+injections reconstruct it
    integrity = _record_integrity(enriched_spec, enriched_instruction, spec.instruction)

    # EXECUTE
    start_time = datetime.now(timezone.utc)
    adapter_result = adapter.run(enriched_spec)
    end_time = datetime.now(timezone.utc)

    # GOVERN (fail-closed on receipt)
    report_path, receipt_path = _govern(
        enriched_spec, adapter_result, start_time, end_time, integrity=integrity
    )

    returncode = 0 if adapter_result.status == "success" else 1
    return EnvelopeResult(
        status=adapter_result.status,
        returncode=returncode,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=adapter_result.completion_text,
        error=adapter_result.error,
    )


# ---------------------------------------------------------------------------
# PR-3: ProviderAdapter -- _fail_loud_on_empty_success, _map_generic_spawn_result,
# ProviderAdapter moved to envelope_adapters_provider.py (dispatch-monolith-split,
# PR-6 of 6); imported above at bare name so every existing
# dispatch_envelope.ProviderAdapter-shaped coupling keeps binding.
# ---------------------------------------------------------------------------


def run_envelope_plan(
    plan: "ExecutionPlan",
    permit: "ExecutionPermit",
    *,
    state_dir: Path,
    data_dir: Path,
) -> EnvelopeResult:
    """Execute a validated ExecutionPlan for the provider lane.

    Provider lane covers codex, kimi, gemini, litellm:*, deepseek-harness,
    local-gemma. The claude_tmux_subscription lane is wired separately in PR-4.

    require_permit is the first action — un-evadable and cannot be moved.

    Raises:
        PermissionError: permit was not issued by issue_permit for this plan.
        ValueError: plan.lane is not "provider".
        EnvelopeGovernError: GOVERN cannot confirm receipt (fail-closed).
    """
    from dispatch_internal import is_valid_instruction_hash, require_permit  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable backstop — FIRST action

    if plan.lane != "provider":
        raise ValueError(
            f"run_envelope_plan handles the provider lane only; got lane={plan.lane!r} "
            f"(claude_tmux_subscription is executed by the tmux lane, wired in PR-4)"
        )

    # P0-3 (PR-4c): REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    # The old `if plan.instruction_sha256:` guard fell OPEN on an empty hash, letting
    # an empty-hash plan + valid permit spawn mutated content. No hash → no spawn.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"plan.instruction_sha256 is not a valid 64-hex digest "
                f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
            ),
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"instruction file mutated after permit: sha256 mismatch "
                f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
            ),
        )

    spec = EnvelopeSpec(
        dispatch_id=plan.dispatch_id,
        terminal_id=plan.target_id,
        provider=plan.provider.value,
        model=plan.model,
        instruction=instruction,
        role=plan.role,  # F2 (codex): carry the role so the phantom-guard review-exemption applies
        pr_id=plan.pr_id,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
        # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): from the plan,
        # which the door computed from the spec.
        parent_dispatch=plan.parent_dispatch,
        task_class=plan.task_class,
        tier_from=plan.tier_from,
        tier_to=plan.tier_to,
    )

    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — the bundle dir is the pending/<id>/ that hosts instruction.md.
    integrity = _record_integrity(
        enriched_spec,
        enriched_instruction,
        instruction,
        bundle_dir=Path(plan.instruction_file).parent,
    )

    from dispatch_worktree_isolation import (  # noqa: PLC0415
        create_dispatch_worktree,
        remove_dispatch_worktree,
        resolve_consumer_project_root,
    )

    wt_path: Optional[Path] = None
    try:
        # Resolve the CONSUMER project root explicitly (VNX_PROJECT_ROOT / CWD-git,
        # never __file__) so a central-install consumer (SC/MC/SEO/...) gets its
        # worktree under ITS OWN project — not the shared ~/.vnx-system checkout
        # this lane code lives under in a central install (P0 provider-worktree-
        # root-fix). Resolved once and reused for both create and remove below so
        # a fluctuating ambient CWD can't split the two onto different roots.
        # Any resolution failure is handled by the same fail-loud path below as
        # a worktree-creation failure — never falls back to a shared checkout.
        _consumer_project_root = resolve_consumer_project_root()
        wt_path = create_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root)
    except Exception as _wt_exc:
        _isolation_error = (
            f"isolation required (require_worktree) but worktree creation failed "
            f"for {plan.dispatch_id}: {_wt_exc} — aborting; no shared-checkout fallback"
        )
        logger.error("run_envelope_plan: %s", _isolation_error)
        _fail_result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="failure",
            error=_isolation_error,
        )
        _fail_start = _fail_end = datetime.now(timezone.utc)
        report_path, receipt_path = _govern(
            enriched_spec, _fail_result, _fail_start, _fail_end, integrity=integrity
        )
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=report_path,
            receipt_path=receipt_path,
            completion_text="",
            error=_isolation_error,
        )

    # ── OI-1113 pre-measurement: capture remote HEAD of the target branch ──
    # BEFORE the worker runs, so we can verify after the push that the new HEAD
    # contains this one.  For a new branch (first dispatch), there is no remote
    # HEAD — target_remote_head stays None and containment is skipped.  The
    # capture runs BEFORE the worker so a force-push during the run is detectable.
    _target_branch = _dispatch_branch_name(plan.dispatch_id)
    _target_remote_head: Optional[str] = None
    try:
        from pr_enforcement import _get_remote_head  # noqa: PLC0415
        _target_remote_head = _get_remote_head(branch=_target_branch, repo_root=_consumer_project_root)
    except Exception:  # noqa: BLE001 — best-effort; None → containment skipped
        _target_remote_head = None

    # ── OI-1115: skip auto-PR when the dispatch works on an existing branch ──
    _base_ref = plan.base_ref or "origin/main"
    _skip_pr = _is_dispatch_branch_ref(_base_ref)

    _phantom_diff: Optional[str] = None
    try:
        start = datetime.now(timezone.utc)
        result = ProviderAdapter().run(plan, enriched_spec.instruction, cwd=wt_path)
        end = datetime.now(timezone.utc)
        # F1 (codex): capture the worker's diff BEFORE the teardown below —
        # remove_dispatch_worktree deletes both the worktree and the local dispatch/<id>
        # branch, so the phantom-guard inside _govern could not otherwise resolve the
        # provider lane's diff and would abstain, letting the exact kimi/glm/deepseek
        # phantom slip through.
        # Prefer the pushed branch (survives teardown → more durable evidence) over the
        # live worktree diff (ephemeral, torn down in the finally block). When the worker
        # pushed its branch, the branch diff is the more reliable evidence.
        try:
            _phantom_diff = _resolve_phantom_diff(
                plan.dispatch_id,
                base_ref=_base_ref,
                wt_path=wt_path,
                repo=_consumer_project_root,
            )
        except Exception:  # noqa: BLE001 — best-effort; None -> guard abstains, never false-rejects
            _phantom_diff = None
        # Rij-7: enforce push+PR BEFORE the teardown removes the worktree (the only
        # handle to the local branch). Only when the worker itself reported success —
        # a failed worker has nothing worth pushing. Reuses pr_enforcement.enforce_pr_exists
        # (the single per-state decision site) and the shared tmux_worktree.classify_path.
        if result.status == "success":
            result = _enforce_push_pr(
                dispatch_id=plan.dispatch_id,
                branch=_target_branch,
                wt_path=wt_path,
                repo_root=_consumer_project_root,
                receipts_file=_receipts_file_for(state_dir),
                result=result,
                base_ref=_base_ref,
                target_remote_head=_target_remote_head,
                skip_pr=_skip_pr,
            )
    finally:
        remove_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root, terminal_id=plan.target_id)

    report_path, receipt_path = _govern(
        enriched_spec, result, start, end, phantom_diff=_phantom_diff, integrity=integrity,
        base_ref=_base_ref,
    )

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )


def run_envelope_headless_plan(
    plan: "ExecutionPlan",
    permit: "ExecutionPermit",
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> EnvelopeResult:
    """Execute a validated ExecutionPlan for the claude_headless lane (api_metered billing).

    Headless lane routes to ClaudeSubprocessAdapter (spawn_claude, claude -p) with the
    same require_permit + instruction-sha256 TOCTOU verify + fail-closed GOVERN as the
    provider lane. ClaudeSubprocessAdapter is reused — not reimplemented.

    Raises:
        PermissionError: permit was not issued by issue_permit for this plan.
        ValueError: plan.lane is not "claude_headless".
        EnvelopeGovernError: GOVERN cannot confirm receipt (fail-closed).
    """
    from dispatch_internal import is_valid_instruction_hash, require_permit  # noqa: PLC0415

    require_permit(plan, permit)  # un-evadable backstop — FIRST action

    if plan.lane != "claude_headless":
        raise ValueError(
            f"run_envelope_headless_plan handles lane='claude_headless' only; "
            f"got lane={plan.lane!r}"
        )

    # P0-3: REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"plan.instruction_sha256 is not a valid 64-hex digest "
                f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
            ),
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=None,
            receipt_path=None,
            completion_text="",
            error=(
                f"instruction file mutated after permit: sha256 mismatch "
                f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
            ),
        )

    spec = EnvelopeSpec(
        dispatch_id=plan.dispatch_id,
        terminal_id=plan.target_id,
        provider="claude",
        model=plan.model,
        instruction=instruction,
        role=role,
        pr_id=plan.pr_id,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
        # Chain-link (OI-985): same four fields as run_envelope_plan —
        # without these, every claude_headless dispatch drops the chain.
        parent_dispatch=plan.parent_dispatch,
        task_class=plan.task_class,
        tier_from=plan.tier_from,
        tier_to=plan.tier_to,
    )

    enriched_instruction = _prepare(spec)
    enriched_spec = EnvelopeSpec(
        dispatch_id=spec.dispatch_id,
        terminal_id=spec.terminal_id,
        provider=spec.provider,
        model=spec.model,
        instruction=enriched_instruction,
        role=spec.role,
        pr_id=spec.pr_id,
        state_dir=spec.state_dir,
        data_dir=spec.data_dir,
        deadline_seconds=spec.deadline_seconds,
        parent_dispatch=spec.parent_dispatch,
        task_class=spec.task_class,
        tier_from=spec.tier_from,
        tier_to=spec.tier_to,
    )

    # INTEGRITY — persist the enriched final prompt + verify reconstruction
    integrity = _record_integrity(
        enriched_spec,
        enriched_instruction,
        instruction,
        bundle_dir=Path(plan.instruction_file).parent,
    )

    from dispatch_worktree_isolation import (  # noqa: PLC0415
        create_dispatch_worktree,
        remove_dispatch_worktree,
        resolve_consumer_project_root,
    )

    wt_path: Optional[Path] = None
    try:
        _consumer_project_root = resolve_consumer_project_root()
        wt_path = create_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root)
    except Exception as _wt_exc:
        _isolation_error = (
            f"isolation required but worktree creation failed "
            f"for {plan.dispatch_id}: {_wt_exc} — aborting; no shared-checkout fallback"
        )
        logger.error("run_envelope_headless_plan: %s", _isolation_error)
        _fail_result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="failure",
            error=_isolation_error,
        )
        _fail_start = _fail_end = datetime.now(timezone.utc)
        report_path, receipt_path = _govern(
            enriched_spec, _fail_result, _fail_start, _fail_end, integrity=integrity
        )
        return EnvelopeResult(
            status="failure",
            returncode=1,
            report_path=report_path,
            receipt_path=receipt_path,
            completion_text="",
            error=_isolation_error,
        )

    # ── OI-1113 pre-measurement: capture remote HEAD of the target branch ──
    # BEFORE the worker runs, so we can verify after the push that the new HEAD
    # contains this one.  For a new branch (first dispatch), there is no remote
    # HEAD — target_remote_head stays None and containment is skipped.
    _target_branch = _dispatch_branch_name(plan.dispatch_id)
    _target_remote_head: Optional[str] = None
    try:
        from pr_enforcement import _get_remote_head  # noqa: PLC0415
        _target_remote_head = _get_remote_head(branch=_target_branch, repo_root=_consumer_project_root)
    except Exception:  # noqa: BLE001 — best-effort; None → containment skipped
        _target_remote_head = None

    # ── OI-1115: skip auto-PR when the dispatch works on an existing branch ──
    _base_ref = plan.base_ref or "origin/main"
    _skip_pr = _is_dispatch_branch_ref(_base_ref)

    _phantom_diff: Optional[str] = None
    try:
        start = datetime.now(timezone.utc)
        result = ClaudeSubprocessAdapter().run(enriched_spec, cwd=wt_path)
        end = datetime.now(timezone.utc)
        try:
            _phantom_diff = _resolve_phantom_diff(
                plan.dispatch_id,
                base_ref=_base_ref,
                wt_path=wt_path,
                repo=_consumer_project_root,
            )
        except Exception:  # noqa: BLE001 — best-effort; None -> guard abstains, never false-rejects
            _phantom_diff = None
        # Rij-7: enforce push+PR BEFORE the teardown removes the worktree (the only
        # handle to the local branch). Only when the worker itself reported success.
        # Reuses pr_enforcement.enforce_pr_exists and the shared
        # tmux_worktree.classify_path — never a second copy of the per-state decision.
        if result.status == "success":
            result = _enforce_push_pr(
                dispatch_id=plan.dispatch_id,
                branch=_target_branch,
                wt_path=wt_path,
                repo_root=_consumer_project_root,
                receipts_file=_receipts_file_for(state_dir),
                result=result,
                base_ref=_base_ref,
                target_remote_head=_target_remote_head,
                skip_pr=_skip_pr,
            )
    finally:
        remove_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root, terminal_id=plan.target_id)

    report_path, receipt_path = _govern(
        enriched_spec, result, start, end, phantom_diff=_phantom_diff, integrity=integrity,
        base_ref=_base_ref,
    )

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )
