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
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

# ExecutionPlan / ExecutionPermit: the plan + permit types routed by the
# adapters and run_envelope_plan. Runtime import (not TYPE_CHECKING) so
# typing.get_type_hints() on these signatures resolves them — the F821
# unresolved-name finding (OI-288) was hiding a get_type_hints NameError.
# No import cycle: dispatch_plan -> dispatch_spec and dispatch_internal
# are stdlib-only leaf modules.
from dispatch_internal import ExecutionPermit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class EnvelopeSpec:
    """Normalized dispatch parameters passed through PREPARE -> ROUTE -> EXECUTE -> GOVERN."""

    dispatch_id: str
    terminal_id: str
    provider: str
    model: str
    instruction: str
    role: Optional[str]
    pr_id: Optional[str]
    state_dir: Path
    data_dir: Path
    deadline_seconds: int = 900


@dataclass
class EnvelopeResult:
    """Outcome from a complete envelope run."""

    status: str           # "success" | "failure" | "timeout"
    returncode: int
    report_path: Optional[Path]
    receipt_path: Optional[Path]
    completion_text: str = ""
    error: Optional[str] = None


class EnvelopeGovernError(RuntimeError):
    """Raised when GOVERN cannot emit or confirm a receipt (fail-closed contract)."""


# ---------------------------------------------------------------------------
# Internal adapter result
# ---------------------------------------------------------------------------


@dataclass
class _AdapterResult:
    returncode: int
    completion_text: str
    status: str           # "success" | "failure" | "timeout"
    token_usage: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    timed_out: bool = False
    event_writer_failures: int = 0
    # receipt-quality PR-B1: claude session_id (from the init event), threaded
    # through to emit_dispatch_receipt so it can backfill token_usage from the
    # local transcript when the spawn itself reported none. None for adapters
    # with no session concept (e.g. codex).
    session_id: Optional[str] = None
    # Actual model the spawn resolved and executed (e.g. deepseek-harness
    # resolves "default"/"sonnet" -> "deepseek-v4-pro"). Used for cost
    # computation in _govern so the receipt's cost_usd prices the model that
    # actually ran, not a placeholder from the dispatch spec. None when the
    # adapter did not resolve a distinct model (caller falls back to spec.model).
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Wraps spawn_codex — reuses existing spawn without reimplementation.

    event_writer is not wired in PR-1 (no EventStore setup here); the audit
    stream gap is documented in Open Items and closed in a later PR.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.codex_spawn import spawn_codex  # noqa: PLC0415

        try:
            result = spawn_codex(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"codex spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = getattr(result, "token_usage", None)
        if isinstance(raw_usage, dict):
            token_usage = {
                "input": int(
                    raw_usage.get("input_tokens", raw_usage.get("input", 0)) or 0
                ),
                "output": int(
                    raw_usage.get("output_tokens", raw_usage.get("output", 0)) or 0
                ),
                "cache_hit": int(
                    raw_usage.get(
                        "cache_read_tokens", raw_usage.get("cache_hit", 0)
                    ) or 0
                ),
            }

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
                event_writer_failures=result.event_writer_failures,
                model=spec.model,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                event_writer_failures=result.event_writer_failures,
                model=spec.model,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            event_writer_failures=result.event_writer_failures,
            model=spec.model,
        )


class ClaudeSubprocessAdapter:
    """Wraps spawn_claude — reuses existing spawn without reimplementation.

    Maps ClaudeSpawnResult fields to _AdapterResult for the envelope GOVERN
    phase.  Worker reports remain the primary audit artifact; completion_text
    is now also captured from spawn_claude for benchmark and utility callers
    that need the model's raw text output.

    event_writer is not wired in PR-2 (SubprocessAdapter handles EventStore
    internally); the audit stream gap is documented in Open Items.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.claude_spawn import spawn_claude  # noqa: PLC0415

        try:
            result = spawn_claude(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
                role=spec.role,
                total_deadline=float(spec.deadline_seconds),
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"claude spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = result.token_usage
        if isinstance(raw_usage, dict) and raw_usage:
            token_usage = {
                "input": int(raw_usage.get("input_tokens", 0) or 0),
                "output": int(raw_usage.get("output_tokens", 0) or 0),
                "cache_hit": int(
                    raw_usage.get("cache_read_input_tokens", 0) or 0
                ),
            }
        model_used = getattr(result, "model", None) or spec.model

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
                session_id=result.session_id,
                model=model_used,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                session_id=result.session_id,
                model=model_used,
            )
        if result.stopped_early:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="success",
                token_usage=token_usage,
                session_id=result.session_id,
                model=model_used,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            session_id=result.session_id,
            model=model_used,
        )


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
# PREPARE
# ---------------------------------------------------------------------------


def _prepare(spec: EnvelopeSpec) -> str:
    """Enrich instruction with intelligence context and repo map (best-effort).

    Mirrors _enrich_instruction in provider_dispatch.py but operates on EnvelopeSpec.
    Both layers fall back silently to the original instruction on any failure.
    """
    instruction = spec.instruction

    try:
        from intelligence_injection import build_intelligence_section  # noqa: PLC0415

        instruction = build_intelligence_section(
            instruction=instruction,
            dispatch_id=spec.dispatch_id,
            role=spec.role,
            state_dir=spec.state_dir,
            pr_id=spec.pr_id,
            dispatch_paths=None,
        )
    except ImportError:
        logger.debug(
            "envelope._prepare: intelligence_injection not available — skipping"
        )
    except Exception as exc:
        logger.warning(
            "envelope._prepare: intelligence injection failed (%s) — skipping", exc
        )

    try:
        from dispatch_enricher import apply_repo_map_layer  # noqa: PLC0415

        instruction = apply_repo_map_layer(instruction, {"role": spec.role})
    except Exception as exc:
        logger.warning(
            "envelope._prepare: repo map layer failed (%s) — skipping", exc
        )

    return instruction


# ---------------------------------------------------------------------------
# Final-prompt integrity (input-side audit closure)
# ---------------------------------------------------------------------------


def _record_integrity(
    spec: EnvelopeSpec,
    enriched_instruction: str,
    raw_instruction: str,
    *,
    bundle_dir: Optional[Path] = None,
):
    """Persist the enriched final prompt + verify raw+injections reconstruct it.

    Returns a FinalPromptIntegrity (stamped onto the receipt by _govern) or None
    when the integrity module is unavailable / persistence failed. The strict
    (fail-closed) reconstruction raise propagates; every other error is swallowed so
    the audit-closure step can never itself break a dispatch.
    """
    try:
        from final_prompt_integrity import record_final_prompt_integrity  # noqa: PLC0415
        from final_prompt_integrity import InjectionReconstructError  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return record_final_prompt_integrity(
            dispatch_id=spec.dispatch_id,
            final_prompt=enriched_instruction,
            raw_instruction=raw_instruction,
            data_dir=spec.data_dir,
            state_dir=spec.state_dir,
            bundle_dir=bundle_dir,
        )
    except InjectionReconstructError:
        raise
    except Exception as exc:  # noqa: BLE001 — audit closure must never break a dispatch
        logger.error(
            "envelope: final-prompt integrity failed (non-fatal) dispatch=%s: %s",
            spec.dispatch_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# GOVERN
# ---------------------------------------------------------------------------


def _receipt_exists_for_dispatch(receipt_path: Path, dispatch_id: str) -> bool:
    """Check whether the NDJSON receipt file already contains a line for dispatch_id.

    Used for idempotent dedup: when the legacy path (deliver_with_recovery) already
    wrote a receipt for this dispatch, the envelope GOVERN skips its own receipt
    write to avoid double-emit.
    """
    if not receipt_path.exists():
        return False
    target = f'"dispatch_id":"{dispatch_id}"'
    try:
        with open(receipt_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if target in line:
                    return True
    except OSError as exc:
        logger.warning(
            "envelope._receipt_exists_for_dispatch: cannot read receipt ledger %s: %s — "
            "treating as unreadable (fail-closed: will skip emit to avoid double-receipt)",
            receipt_path,
            exc,
        )
        return True
    return False


def _resolve_fix_forward_diff(
    spec: EnvelopeSpec,
    own_diff: Optional[str],
    *,
    base_ref: str = "origin/main",
    repo: Optional[Path] = None,
) -> Optional[str]:
    """Fall back to the PUSHED PR branch's diff when the dispatch's own worktree/branch diff
    reads empty and the dispatch targets an EXISTING PR (``spec.pr_id`` set) — a fix-forward
    dispatch.

    A fix-forward dispatch pushes its commit onto that PR's branch (per its instruction), not
    onto its own ``dispatch/<id>`` worktree branch — the own-worktree diff then reads empty even
    though real work landed and was pushed. T0's rule is "verify the pushed branch, not the
    report" (phantom_guard module docstring). ``spec.pr_id`` is the dispatch's existing-PR
    identifier — already carried on EnvelopeSpec, populated from ``--pr-id`` — resolved to its
    head branch via ``gh pr view``.

    ``repo`` is the git checkout to resolve/fetch/diff against — the orchestrator's own repo
    root (the ephemeral dispatch worktree is gone or going away by the time GOVERN runs), not
    the worker's torn-down worktree. Defaults to ``project_root.resolve_project_root``.

    No-op for a normal dispatch: a non-empty ``own_diff`` short-circuits before any gh/git call,
    so the own-worktree diff stays the sole source there (unchanged behavior). Best-effort: any
    resolution failure (no gh, bad pr_id, PR not pushed yet) falls back to ``own_diff``
    unchanged — a genuinely empty dispatch (no own diff, no resolvable/non-empty pushed branch)
    still reads empty here, so phantom_guard() still catches it.
    """
    if (own_diff or "").strip():
        return own_diff
    pr_id = (spec.pr_id or "").strip()
    if not pr_id:
        return own_diff
    try:
        from phantom_guard import compute_branch_diff, resolve_pr_head_branch  # noqa: PLC0415
        if repo is None:
            from project_root import resolve_project_root  # noqa: PLC0415
            repo = resolve_project_root(__file__)
        branch = resolve_pr_head_branch(pr_id, repo=repo)
        if not branch:
            return own_diff
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(repo), capture_output=True, text=True, timeout=30, check=False,
        )
        pushed_diff = compute_branch_diff(f"origin/{branch}", base_ref=base_ref, repo=repo)
    except Exception as exc:  # noqa: BLE001 — best-effort; never raise, never false-reject on a resolution error
        logger.warning(
            "envelope: fix-forward diff resolution failed dispatch=%s pr_id=%s: %s",
            spec.dispatch_id, pr_id, exc,
        )
        return own_diff
    return pushed_diff if pushed_diff.strip() else own_diff


def _resolve_phantom_diff(
    dispatch_id: str,
    *,
    base_ref: str = "origin/main",
    wt_path: Path,
    repo: Path,
) -> Optional[str]:
    """Resolve the phantom-guard diff for the provider lane.

    Prefers the pushed branch diff (durable, survives dispatch teardown) over
    the live worktree diff (ephemeral, torn down in the finally block). Falls
    back through each source; returns None when ALL sources are unresolvable so
    the guard abstains instead of false-rejecting.

    OI-870: reads the actual branch from the worktree (``git -C <wt_path>
    rev-parse --abbrev-ref HEAD``) instead of deriving it from dispatch_id.
    A fix-forward dispatch pushes onto a pre-existing PR branch, not its own
    ``dispatch/<id>`` branch — deriving the branch name from dispatch_id misses
    the real push target.

    OI-869: detects a self-referencing base_ref (where base_ref resolves to the
    same commit as the branch head, producing a zero-diff-by-definition) and
    falls back to ``origin/main`` as the effective comparison base.
    """
    from dispatch_worktree_isolation import _sanitize_dispatch_id  # noqa: PLC0415
    from phantom_guard import compute_branch_diff, compute_worktree_diff  # noqa: PLC0415

    # OI-870: read the actual branch from the worktree itself. A fix-forward
    # dispatch may have checked out a different branch (the PR's head branch)
    # and pushed onto that instead of its own dispatch/<id> branch.
    try:
        wt_branch = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if wt_branch.returncode == 0 and wt_branch.stdout.strip():
            branch = wt_branch.stdout.strip()
        else:
            branch = f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"
    except Exception as exc:
        logger.warning(
            "_resolve_phantom_diff: could not read branch from worktree dispatch=%s (%s) "
            "— falling back to dispatch-id-derived name",
            dispatch_id, exc,
        )
        branch = f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"

    # OI-869: detect a self-referencing base_ref. When base_ref resolves to the
    # same commit as the branch head (because the worker pushed onto the same
    # branch that base_ref names), the diff is zero by definition — "diff
    # against yourself" is not evidence of a phantom. Fall back to origin/main
    # as the effective comparison base.
    effective_base_ref = base_ref
    try:
        head_sha = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        base_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", base_ref],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if head_sha and base_sha and head_sha == base_sha:
            logger.warning(
                "_resolve_phantom_diff: base_ref %s resolves to the same commit "
                "as branch head %s (%s) — self-referencing base, falling back "
                "to origin/main dispatch=%s branch=%s",
                base_ref, head_sha[:8], head_sha, dispatch_id, branch,
            )
            effective_base_ref = "origin/main"
    except Exception as exc:
        logger.warning(
            "_resolve_phantom_diff: self-ref check failed for base_ref=%s "
            "dispatch=%s (%s) — using base_ref as-is",
            base_ref, dispatch_id, exc,
        )

    # Check whether the worker pushed its branch. ``git worktree add -b`` may
    # set upstream=origin/main implicitly, so ``@{upstream}`` alone is not a
    # reliable signal — ask the remote whether the branch actually exists.
    has_upstream = False
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            cwd=str(repo), capture_output=True, text=True, timeout=15,
        )
        # git ls-remote exits 0 even when the ref doesn't exist; non-empty
        # stdout means the branch IS on the remote.
        has_upstream = proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception as exc:
        logger.warning(
            "_resolve_phantom_diff: upstream check failed for branch=%s dispatch=%s (%s)",
            branch, dispatch_id, exc,
        )

    if has_upstream:
        try:
            return compute_branch_diff(f"origin/{branch}", base_ref=effective_base_ref, repo=repo)
        except Exception as exc:
            logger.warning(
                "_resolve_phantom_diff: branch diff failed for origin/%s dispatch=%s (%s)",
                branch, dispatch_id, exc,
            )

    # Fall back to worktree diff — last chance before the teardown deletes it
    try:
        return compute_worktree_diff(wt_path, base_ref=effective_base_ref)
    except Exception:
        return None


def _unknown_verification() -> Dict[str, Any]:
    """ADR-035 §3.1.1 fallback: an explicit 'we don't know' beats the
    current silent absence — used when no report is on disk to extract from."""
    return {
        "method": "unknown",
        "tests_run": None,
        "tests_passed": None,
        "tests_failed": None,
        "command": None,
        "pr_ref": None,
        "push_verified": None,
        "spec_deviation": None,
    }


def _verification_from_report(report_path: Optional[Path]) -> Dict[str, Any]:
    """ADR-035 §3.1.1 — envelope sub-path: `emit_unified_report` already ran,
    so the markdown report is on disk by the time the receipt is written.
    Threads the SAME `report_parser.py::extract_validation` regex extractor
    Path 2 already uses into this call — no new extraction mechanism, no new
    report format (§8 non-goal).

    Never raises: any extraction failure degrades to `method: "unknown"`
    rather than blocking the (fail-closed) receipt write.
    """
    if report_path is None or not report_path.exists():
        return _unknown_verification()

    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")

        _scripts_dir = str(Path(__file__).resolve().parent.parent)
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from report_parser import ReportParser  # noqa: PLC0415

        extracted = ReportParser().extract_validation(content)
        tests_passed = int(extracted.get("tests_passed") or 0)
        tests_failed = int(extracted.get("tests_failed") or 0)
        tests_run = tests_passed + tests_failed

        if tests_run > 0:
            method = "pytest"
        elif extracted.get("quality_gates"):
            method = "manual"
        else:
            method = "unknown"

        return {
            "method": method,
            "tests_run": tests_run if tests_run > 0 else None,
            "tests_passed": tests_passed if tests_run > 0 else None,
            "tests_failed": tests_failed if tests_run > 0 else None,
            "command": None,
            "pr_ref": None,
            "push_verified": None,
            "spec_deviation": None,
        }
    except Exception as exc:  # noqa: BLE001 — never block the receipt write
        logger.warning(
            "envelope._verification_from_report: extraction failed for %s: %s",
            report_path, exc,
        )
        return _unknown_verification()


def _archive_dispatch_events(terminal: Optional[str], dispatch_id: str) -> Optional[str]:
    """Archive the live event stream under ``dispatch_id`` (end-of-dispatch teardown).

    The envelope path previously never archived the per-dispatch ring buffer at
    END-of-dispatch (OI-878 / OI-902): ``run_envelope_plan`` wrote events via the
    SubprocessAdapter's internal EventStore but only the NEXT dispatch's write-side
    boundary guard (EventStore.append, #1276) rotated the previous stream, so the
    LAST dispatch in a series leaked its events into the live file.

    Returns the archive path (str) when archived, else None.  Guards against
    mislabeling: when the live file holds a DIFFERENT dispatch's events (this
    dispatch never wrote to the stream — e.g. a pre-spawn failure), the file is
    left untouched for the boundary guard of the next dispatch; archiving it here
    would mislabel the previous dispatch's events under our id.

    Best-effort — a failure never breaks the dispatch (mirrors the
    ``_emit_governance`` archive contract in provider_dispatch).
    """
    if not terminal or not dispatch_id:
        return None
    try:
        from event_store import EventStore  # noqa: PLC0415

        store = EventStore()
        last = store.last_event(terminal)
        last_dispatch = (last or {}).get("dispatch_id") or ""
        if last_dispatch and last_dispatch != dispatch_id:
            logger.debug(
                "envelope: end-dispatch archive skipped terminal=%s — live file "
                "holds %r, not %s (left for the next dispatch's boundary guard)",
                terminal, last_dispatch, dispatch_id,
            )
            return None
        archived = store.archive(terminal, dispatch_id)
        if archived is not None:
            logger.info("envelope: end-dispatch archived %s -> %s", terminal, archived)
        return str(archived) if archived is not None else None
    except Exception as exc:  # noqa: BLE001 — teardown must never break the dispatch
        logger.warning(
            "envelope: end-dispatch event archive failed terminal=%s dispatch=%s (non-fatal): %s",
            terminal, dispatch_id, exc,
        )
        return None


def _clear_dispatch_events(terminal: Optional[str], dispatch_id: str) -> None:
    """Truncate the live event stream after the receipt is written (end-of-dispatch).

    Runs in a finally after the receipt emit so the live ``T{n}.ndjson`` is
    truncated even when the fail-closed receipt write raises EnvelopeGovernError.
    Only clears when the live file still holds THIS dispatch's events (or is
    empty) — never wipes a different dispatch's stream. Best-effort; never raises.
    """
    if not terminal or not dispatch_id:
        return
    try:
        from event_store import EventStore  # noqa: PLC0415

        store = EventStore()
        last = store.last_event(terminal)
        last_dispatch = (last or {}).get("dispatch_id") or ""
        if last_dispatch and last_dispatch != dispatch_id:
            return
        store.clear(terminal)
    except Exception as exc:  # noqa: BLE001 — teardown must never break the dispatch
        logger.warning(
            "envelope: end-dispatch event clear failed terminal=%s dispatch=%s (non-fatal): %s",
            terminal, dispatch_id, exc,
        )


def _govern(
    spec: EnvelopeSpec,
    adapter_result: _AdapterResult,
    start_time: datetime,
    end_time: datetime,
    phantom_diff: Optional[str] = None,
    integrity: Optional[Any] = None,
    base_ref: str = "origin/main",
) -> tuple:
    """Emit unified_report then dispatch receipt. Returns (report_path, receipt_path).

    Fail-closed contract: raises EnvelopeGovernError when receipt_path is None or
    absent on disk after emit — never silently loses a receipt.
    Report is emitted first so the receipt can carry the linkage (ADR-005 ordering).

    Idempotent dedup: when the receipt NDJSON already contains a line for this
    dispatch_id (written by deliver_with_recovery's internal close-out as a safety
    net), the GOVERN receipt write is skipped.  This avoids double-emit during the
    migration period where both legacy and envelope paths may run.
    """
    from governance_emit import emit_dispatch_receipt, emit_unified_report  # noqa: PLC0415

    duration = (end_time - start_time).total_seconds()

    # OI-882: the envelope previously hardcoded cost_usd=None even though
    # adapter_result.token_usage carried real tokens and wave7_models.yaml has
    # the per-provider prices. Compute the estimate the same way
    # provider_dispatch._emit_governance does, using the actual model the spawn
    # resolved (falling back to the spec model). Non-fatal: an unresolvable
    # price leaves cost_usd None instead of failing the receipt.
    cost_usd: Optional[float] = None
    _cost_model = getattr(adapter_result, "model", None) or spec.model
    try:
        from provider_dispatch import _compute_cost  # noqa: PLC0415
        cost_usd = _compute_cost(spec.provider, _cost_model, adapter_result.token_usage)
    except Exception as _cost_exc:  # noqa: BLE001 — cost must never break receipt emission
        logger.debug(
            "envelope._govern: cost compute failed dispatch=%s provider=%s (non-fatal): %s",
            spec.dispatch_id, spec.provider, _cost_exc,
        )

    # OI-878/OI-902: end-of-dispatch event archive. Archive the live event stream
    # under THIS dispatch's id BEFORE the receipt so the receipt can carry
    # events_path (parity with provider_dispatch._emit_governance); the clear
    # runs in the finally below AFTER the receipt write. Without this the
    # envelope path never rotated the ring buffer at end-of-dispatch — only the
    # NEXT dispatch's write-side boundary guard did, so the LAST dispatch in a
    # series leaked its events into the live file.
    events_path = _archive_dispatch_events(spec.terminal_id, spec.dispatch_id)

    # REPORT first — idempotent: worker-written file is preserved, not overwritten.
    # OI-903: on failure/timeout, a killed worker's partial report is preserved
    # under a .partial.md sidecar so the canonical report stays contract-compliant
    # while the partial output remains retrievable.
    report_path: Optional[Path] = None
    try:
        report_path = emit_unified_report(
            dispatch_id=spec.dispatch_id,
            terminal_id=spec.terminal_id,
            provider=spec.provider,
            instruction=spec.instruction,
            response_text=adapter_result.completion_text,
            findings=[],
            duration_seconds=duration,
            data_dir=spec.data_dir,
            preserve_partial=adapter_result.status != "success",
        )
    except Exception as exc:
        logger.error(
            "envelope._govern: report emit failed dispatch=%s: %s — proceeding to receipt",
            spec.dispatch_id,
            exc,
        )

    # RECEIPT second — fail-closed, with idempotent dedup. The end-of-dispatch
    # event clear runs in finally so the live stream is truncated even when the
    # fail-closed receipt emit raises EnvelopeGovernError.
    receipt_path: Optional[Path] = None
    try:
        ndjson_path = spec.state_dir / "t0_receipts.ndjson"
        if _receipt_exists_for_dispatch(ndjson_path, spec.dispatch_id):
            logger.info(
                "envelope._govern: receipt already exists for dispatch=%s — skipping (idempotent dedup)",
                spec.dispatch_id,
            )
            receipt_path = ndjson_path
        else:
            try:
                # receipt-quality PR-1: resolve dispatch identity (role) from
                # dispatch_metadata just before the emit. FAIL-OPEN — a resolver
                # error must never break receipt emission.
                try:
                    from dispatch_identity import resolve_dispatch_role  # noqa: PLC0415
                    _project_id = getattr(spec, "project_id", None)
                    if not _project_id:
                        from dispatch_cli import _resolve_project_id  # noqa: PLC0415
                        _project_id = _resolve_project_id()
                    _role = resolve_dispatch_role(
                        spec.dispatch_id, _project_id, state_dir=spec.state_dir,
                    )
                except Exception:  # noqa: BLE001 — identity join is fail-open
                    logger.debug(
                        "envelope._govern: role resolution failed open dispatch=%s",
                        spec.dispatch_id,
                        exc_info=True,
                    )
                    _role = None
                _role = _role or "identity_unresolved"

                # receipt-quality PR-B2 fix-forward (Finding C): aggregate
                # PreToolUse-hook tool-call signals for this dispatch
                # (toolcall_signals.py), mirroring provider_dispatch._emit_
                # governance's wiring so the claude/subprocess-adapter lane also
                # populates these fields when VNX_TMUX_SIGNAL_DIR is set.
                # FAIL-OPEN — an aggregation error or absent signal log must
                # never break receipt emission; each field simply stays None
                # (omitted by ReceiptV2).
                _toolcall_signals: Dict[str, int] = {}
                try:
                    _signal_dir = os.environ.get("VNX_TMUX_SIGNAL_DIR")
                    if _signal_dir:
                        from toolcall_signals import aggregate_toolcall_signals  # noqa: PLC0415
                        _toolcall_signals = aggregate_toolcall_signals(_signal_dir) or {}
                except Exception:  # noqa: BLE001 — observability signal must never break receipt emission
                    logger.debug(
                        "envelope._govern: toolcall signal aggregation failed dispatch=%s (non-fatal)",
                        spec.dispatch_id,
                        exc_info=True,
                    )
                    _toolcall_signals = {}

                # ADR-005: emit cost event BEFORE receipt write. provider_dispatch
                # and recovery raise on failure (fail-loud); the envelope is the
                # third receipt path and matches them, but wraps in try/except so a
                # cost-log failure never breaks the fail-closed receipt contract.
                try:
                    from provider_costs import emit_provider_cost  # noqa: PLC0415
                    from project_scope import resolve_stamp_project_id, TenantUnresolved  # noqa: PLC0415
                    _cost_pid = ""
                    try:
                        _cost_pid = resolve_stamp_project_id(
                            db_path=str(spec.state_dir / "quality_intelligence.db")
                        )
                    except TenantUnresolved:
                        pass  # emit falls back to env; cost-audit must not lose the event
                    emit_provider_cost(
                        provider=spec.provider,
                        model=_cost_model,
                        input_tokens=(
                            adapter_result.token_usage.get("input")
                            if adapter_result.token_usage else None
                        ),
                        output_tokens=(
                            adapter_result.token_usage.get("output")
                            if adapter_result.token_usage else None
                        ),
                        cost_usd_estimate=cost_usd,
                        dispatch_id=spec.dispatch_id,
                        project_id=_cost_pid,
                    )
                except Exception as _cost_event_exc:  # noqa: BLE001 — cost event must not break receipt
                    logger.warning(
                        "envelope._govern: cost event emit failed dispatch=%s (non-fatal): %s",
                        spec.dispatch_id, _cost_event_exc,
                    )

                # OI-866: classify failure so the receipt carries a distinguishable
                # failure_reason + failure_class instead of a silent
                # "(no error captured)" log line.
                _classification: Dict[str, Optional[str]] = {"failure_class": None, "failure_reason": None}
                if adapter_result.status != "success":
                    try:
                        from failure_classification import classify_failure  # noqa: PLC0415
                        _classification = classify_failure(
                            status=adapter_result.status,
                            error=adapter_result.error,
                            completion_text=adapter_result.completion_text,
                            timed_out=adapter_result.timed_out,
                            provider=spec.provider,
                            duration_seconds=duration,
                            returncode=adapter_result.returncode,
                        )
                    except Exception:  # noqa: BLE001 — classification is best-effort
                        logger.debug(
                            "envelope._govern: failure classification failed dispatch=%s (non-fatal)",
                            spec.dispatch_id,
                            exc_info=True,
                        )

                receipt_path = emit_dispatch_receipt(
                    dispatch_id=spec.dispatch_id,
                    terminal_id=spec.terminal_id,
                    provider=spec.provider,
                    model=_cost_model,
                    pr_id=spec.pr_id,
                    status=adapter_result.status,
                    completion_pct=100 if adapter_result.status == "success" else 0,
                    risk=0.0,
                    findings=[],
                    duration_seconds=duration,
                    token_usage=adapter_result.token_usage,
                    cost_usd=cost_usd,
                    state_dir=spec.state_dir,
                    report_path=str(report_path) if report_path else None,
                    events_path=events_path,
                    final_prompt_path=getattr(integrity, "final_prompt_path", None),
                    final_prompt_sha256=getattr(integrity, "final_prompt_sha256", None),
                    injection_reconstructs=(
                        getattr(integrity, "injection_reconstructs", None)
                        if integrity is not None
                        else None
                    ),
                    # ADR-035 §3.1.1: envelope sub-path — the report is already
                    # on disk (emit_unified_report ran above), so extract
                    # verification{} from it via the shared regex extractor.
                    verification=_verification_from_report(report_path),
                    role=_role,
                    receipt_kind="dispatch",
                    session_id=adapter_result.session_id,
                    tool_call_count=_toolcall_signals.get("tool_call_count"),
                    tool_call_failures=_toolcall_signals.get("tool_call_failures"),
                    tool_call_retries=_toolcall_signals.get("tool_call_retries"),
                    deadline_seconds=spec.deadline_seconds,
                    failure_reason=_classification.get("failure_reason"),
                    failure_class=_classification.get("failure_class"),
                )
            except Exception as exc:
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt emit raised for dispatch={spec.dispatch_id}: {exc}"
                ) from exc

            if receipt_path is None:
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt_path is None after emit "
                    f"(fail-closed) dispatch={spec.dispatch_id}"
                )
            if not receipt_path.exists():
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt file absent on disk after emit "
                    f"path={receipt_path} dispatch={spec.dispatch_id} (fail-closed)"
                )
    finally:
        # OI-878/OI-902: truncate the live event stream now that the archive
        # (top of _govern) and the receipt write are complete. Best-effort.
        _clear_dispatch_events(spec.terminal_id, spec.dispatch_id)

    if adapter_result.status != "success":
        # Fail-loud: a failure/timeout/empty-completion receipt must never be silent.
        # dispatch_cli returns only an integer exit code for the provider lane, so this
        # log line is often the only place the raw error surfaces — print it in full.
        logger.error(
            "envelope._govern: dispatch=%s provider=%s status=%s report=%s receipt=%s "
            "completion_len=%d error=%s",
            spec.dispatch_id,
            spec.provider,
            adapter_result.status,
            report_path,
            receipt_path,
            len(adapter_result.completion_text or ""),
            adapter_result.error or "(no error captured)",
        )
    else:
        logger.info(
            "envelope._govern: dispatch=%s status=%s report=%s receipt=%s",
            spec.dispatch_id,
            adapter_result.status,
            report_path,
            receipt_path,
        )
    # P0.2: inline phantom-guard (provider lanes — the kimi/glm/deepseek text-only fabrication
    # vector). A delivery worker that reports success with no worktree/branch diff is rejected via
    # a corrective failed receipt. worktree_path is unavailable on EnvelopeSpec, so the guard derives
    # the dispatch/<id> branch (isolated dispatches) or abstains (never false-rejects). Non-fatal.
    try:
        from phantom_guard import record_phantom_if_any  # noqa: PLC0415
        _tok = adapter_result.token_usage or {}
        # Fix-forward: an empty own-worktree/dispatch-branch diff is falsely read as phantom when
        # the dispatch targets an existing PR (spec.pr_id) and pushed its commit onto THAT branch
        # instead — resolve the pushed branch and use its diff when the own diff is empty.
        _effective_diff = _resolve_fix_forward_diff(spec, phantom_diff, base_ref=base_ref)
        record_phantom_if_any(
            dispatch_id=spec.dispatch_id,
            role=spec.role,
            status=adapter_result.status,
            token_usage=(int(_tok.get("input", 0) or 0) + int(_tok.get("output", 0) or 0)) or None,
            worktree_path=None,
            base_sha=None,
            worktree_diff=_effective_diff,  # F1: pre-captured before the worktree teardown
            receipts_file=str(spec.state_dir / "t0_receipts.ndjson"),
            state_dir=spec.state_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "envelope._govern: phantom-guard check failed (non-fatal) dispatch=%s: %s",
            spec.dispatch_id, exc,
        )
        try:
            from phantom_guard import record_guard_error  # noqa: PLC0415
            record_guard_error(
                dispatch_id=spec.dispatch_id,
                receipts_file=str(spec.state_dir / "t0_receipts.ndjson"),
                error=exc,
            )
        except Exception:  # noqa: BLE001 — the guard-error audit signal must never make _govern fatal
            logger.error("envelope._govern: guard-error audit signal itself failed dispatch=%s", spec.dispatch_id)
    return report_path, receipt_path


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
# PR-3: ProviderAdapter + plan-based provider execution
# ---------------------------------------------------------------------------


def _fail_loud_on_empty_success(
    status: str,
    returncode: int,
    completion_text: str,
    provider_str: str,
    raw_result: Any,
) -> tuple:
    """Refuse to report a "success" with a blank completion — surface it instead.

    A provider spawn can return returncode=0 with an empty completion_text when
    its own CLI/output-format changed underneath it (the exact silent-empty-
    completion vector this guard exists to catch — see dispatch instruction
    20260710-211102-envelope-provider-lane-empty-completion). Not every spawn_*
    does its own empty-extraction check (kimi_spawn does; the harness/litellm/
    codex/gemini spawns do not), so this backstop lives once, centrally, here —
    the ONE place every provider-lane result passes through before GOVERN.

    Returns (status, returncode, error) — unchanged unless status=="success"
    and completion_text is blank, in which case status is downgraded to
    "failure", returncode is forced non-zero, and error carries the raw spawn
    result (repr'd) so the failure is diagnosable instead of a silent blank
    report + receipt.
    """
    if status == "success" and not (completion_text or "").strip():
        return (
            "failure",
            returncode or 1,
            (
                f"provider={provider_str} returned an empty completion with "
                f"returncode={returncode} — refusing to report a silent empty "
                f"success (raw spawn result: {raw_result!r})"
            ),
        )
    return status, returncode, None


def _map_generic_spawn_result(
    result: Any, provider_str: str, model: Optional[str] = None
) -> _AdapterResult:
    """Map codex/kimi/gemini/litellm:* spawn result to _AdapterResult.

    Normalises token fields via _extract_token_usage from provider_dispatch.
    Handles error, timeout, and success/failure by returncode. Does NOT handle
    the deepseek-harness stopped_early or local-gemma error-condition variant —
    those are handled inline in ProviderAdapter.run().
    """
    from provider_dispatch import _extract_token_usage  # noqa: PLC0415

    token_usage: Dict[str, int] = _extract_token_usage(result, provider_str)
    ewf = getattr(result, "event_writer_failures", 0)

    if result.error:
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status="failure",
            token_usage=token_usage,
            error=result.error,
            event_writer_failures=ewf,
            model=model,
        )
    if result.timed_out:
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status="timeout",
            token_usage=token_usage,
            timed_out=True,
            event_writer_failures=ewf,
            model=model,
        )
    completion_text = result.completion_text or ""
    status = "success" if result.returncode == 0 else "failure"
    status, returncode, guard_error = _fail_loud_on_empty_success(
        status, result.returncode, completion_text, provider_str, result
    )
    return _AdapterResult(
        returncode=returncode,
        completion_text=completion_text,
        status=status,
        token_usage=token_usage,
        error=guard_error,
        event_writer_failures=ewf,
        model=model,
    )


class ProviderAdapter:
    """Routes provider-lane ExecutionPlan to the correct spawn function.

    Mirrors CodexAdapter's shape but routes by plan.provider to the correct
    spawn function. Reuses existing resolution helpers and raw spawn functions
    from provider_dispatch and provider_spawns.*. Does NOT call the governing
    _dispatch_* wrappers in provider_dispatch — those govern; the envelope
    governs once via _govern.

    Supported plan.provider values: codex, kimi, gemini, litellm:deepseek,
    litellm:zai, litellm:moonshot, deepseek-harness, local-gemma.
    Provider.CLAUDE and Provider.AUTO are programming errors → ValueError.

    event_writer is not wired in PR-3 (EventStore setup is an open item for a
    later PR); the audit gap is documented in Open Items.
    """

    def run(
        self,
        plan: "ExecutionPlan",
        instruction: str,
        *,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from dispatch_spec import Provider  # noqa: PLC0415
        from provider_dispatch import (  # noqa: PLC0415
            _MLX_MODEL_MAP,
            _build_lane_key,
            _extract_token_usage,
            _resolve_codex_model,
            _resolve_deepseek_model,
            _resolve_moonshot_model,
            _resolve_zai_model,
        )

        pv: "Provider" = plan.provider  # type: ignore[assignment]

        # ---- codex ----
        if pv == Provider.CODEX:
            from provider_spawns.codex_spawn import spawn_codex  # noqa: PLC0415

            model = (
                plan.model if plan.model not in ("default", "") else _resolve_codex_model()
            )
            try:
                result = spawn_codex(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"codex spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- kimi ----
        if pv == Provider.KIMI:
            return self._run_kimi(plan, instruction, event_writer=event_writer, cwd=cwd)

        # ---- gemini ----
        if pv == Provider.GEMINI:
            from provider_spawns.gemini_spawn import spawn_gemini  # noqa: PLC0415

            model = (
                plan.model
                if plan.model not in ("default", "sonnet", "")
                else os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-pro")
            )
            try:
                result = spawn_gemini(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"gemini spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- litellm:deepseek | litellm:zai | litellm:moonshot ----
        if pv in (Provider.LITELLM_DEEPSEEK, Provider.LITELLM_ZAI, Provider.LITELLM_MOONSHOT):
            from provider_spawns.litellm_spawn import spawn_litellm  # noqa: PLC0415

            # Extract sub-provider from the enum value: "litellm:deepseek" -> "deepseek"
            base_sub = pv.value.split(":", 1)[1]
            if plan.model not in ("default", ""):
                model = plan.model
            elif pv == Provider.LITELLM_DEEPSEEK:
                model = _resolve_deepseek_model()
            elif pv == Provider.LITELLM_ZAI:
                model = _resolve_zai_model()
            else:
                model = _resolve_moonshot_model()
            lane_key = _build_lane_key(base_sub, None)
            try:
                result = spawn_litellm(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    sub_provider=base_sub,
                    lane=lane_key,
                    cwd=cwd,
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"litellm spawn BrokenPipeError: {exc}",
                )
            return _map_generic_spawn_result(result, pv.value, model=model)

        # ---- deepseek-harness ----
        if pv == Provider.DEEPSEEK_HARNESS:
            from provider_spawns.deepseek_harness_spawn import (  # noqa: PLC0415
                resolve_harness_model,
                spawn_deepseek_harness,
            )

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else None
            )
            model = resolve_harness_model(raw_model)
            try:
                result = spawn_deepseek_harness(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                    total_deadline=float(plan.deadline_seconds),
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"deepseek-harness spawn BrokenPipeError: {exc}",
                )
            token_usage: Dict[str, int] = _extract_token_usage(result, pv.value)
            if result.error:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=model,
                )
            if getattr(result, "stopped_early", False):
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="success",
                    token_usage=token_usage,
                    model=model,
                )
            _dh_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _dh_rc, _dh_err = _fail_loud_on_empty_success(
                status, result.returncode, _dh_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_dh_rc,
                completion_text=_dh_text,
                status=status,
                token_usage=token_usage,
                error=_dh_err,
                model=model,
            )

        # ---- glm-harness ---- (codex flip-PR F2: the door normalizes GLM -> glm-harness, so the
        # provider envelope MUST be able to execute it, not raise unsupported.)
        if pv == Provider.GLM_HARNESS:
            from provider_spawns.glm_harness_spawn import (  # noqa: PLC0415
                resolve_harness_model,
                spawn_glm_harness,
            )

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else None
            )
            model = resolve_harness_model(raw_model)
            try:
                result = spawn_glm_harness(
                    prompt=instruction,
                    model=model,
                    dispatch_id=plan.dispatch_id,
                    terminal_id=plan.target_id,
                    event_writer=event_writer,
                    cwd=cwd,
                    total_deadline=float(plan.deadline_seconds),
                )
            except BrokenPipeError as exc:
                return _AdapterResult(
                    returncode=1, completion_text="", status="failure",
                    error=f"glm-harness spawn BrokenPipeError: {exc}",
                )
            token_usage = _extract_token_usage(result, pv.value)
            if result.error:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=model,
                )
            if getattr(result, "stopped_early", False):
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="success",
                    token_usage=token_usage,
                    model=model,
                )
            _gh_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _gh_rc, _gh_err = _fail_loud_on_empty_success(
                status, result.returncode, _gh_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_gh_rc,
                completion_text=_gh_text,
                status=status,
                token_usage=token_usage,
                error=_gh_err,
                model=model,
            )

        # ---- local-gemma ----
        if pv == Provider.LOCAL_GEMMA:
            from provider_spawns.local_gemma_spawn import spawn_local_gemma  # noqa: PLC0415

            raw_model = (
                plan.model if plan.model not in ("default", "sonnet", "") else "gemma-4b-local"
            )
            canonical_model = _MLX_MODEL_MAP.get(raw_model, raw_model)
            result = spawn_local_gemma(
                instruction=instruction,
                model=canonical_model,
                role=None,
                deadline_seconds=300,
                dispatch_id=plan.dispatch_id,
                project_id="vnx-dev",
            )
            token_usage = _extract_token_usage(result, pv.value)
            if result.error and result.returncode != 0:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="failure",
                    token_usage=token_usage,
                    error=result.error,
                    model=canonical_model,
                )
            if result.timed_out:
                return _AdapterResult(
                    returncode=result.returncode,
                    completion_text=(result.completion_text or ""),
                    status="timeout",
                    token_usage=token_usage,
                    timed_out=True,
                    model=canonical_model,
                )
            _lg_text = result.completion_text or ""
            status = "success" if result.returncode == 0 else "failure"
            status, _lg_rc, _lg_err = _fail_loud_on_empty_success(
                status, result.returncode, _lg_text, pv.value, result
            )
            return _AdapterResult(
                returncode=_lg_rc,
                completion_text=_lg_text,
                status=status,
                token_usage=token_usage,
                error=_lg_err,
                model=canonical_model,
            )

        # Provider.CLAUDE, Provider.AUTO, or any unexpected value — programming error
        raise ValueError(
            f"ProviderAdapter: unsupported provider {pv!r} — "
            f"claude and auto do not route through the provider envelope "
            f"(claude_tmux_subscription is executed by the tmux lane, wired in PR-4)"
        )

    def _run_kimi(
        self,
        plan: "ExecutionPlan",
        instruction: str,
        *,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        """Kimi spawn branch, extracted from run() (OI-709 function-size gate).

        Pure extraction — behavior identical to the former inline kimi branch.
        """
        from dispatch_spec import Provider  # noqa: PLC0415
        from provider_spawns.kimi_spawn import spawn_kimi  # noqa: PLC0415
        from provider_dispatch import (  # noqa: PLC0415
            KimiModelResolutionError,
            _kimi_resolve_cli_model_arg,
            _kimi_resolve_requested_key,
        )

        # Shared resolver (20260721-kimi-lane-hardening): args.model/plan.model >
        # VNX_KIMI_MODEL > registry K3 default; bare provider/alias tokens ("kimi",
        # "kimi-default") normalize to the default; an unmapped/stale model_key RAISES
        # instead of ever being passed raw to `-m` or silently substituted with K3.
        try:
            model = _kimi_resolve_cli_model_arg(_kimi_resolve_requested_key(plan.model))
        except KimiModelResolutionError as exc:
            return _AdapterResult(
                returncode=1, completion_text="", status="failure",
                error=f"kimi model resolution failed: {exc}",
            )
        try:
            result = spawn_kimi(
                prompt=instruction,
                model=model,
                dispatch_id=plan.dispatch_id,
                terminal_id=plan.target_id,
                event_writer=event_writer,
                cwd=cwd,
                # worker-provider-kimi-flip (20260723): honor the spec's staged deadline
                # instead of spawn_kimi's own hardcoded 900s default — a caller staging a
                # longer deadline (e.g. stage_spec_bundle's 3600s default) was previously
                # silently capped short (memory: provider-lane-900s-deadline-kills-builds).
                total_deadline=float(plan.deadline_seconds),
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1, completion_text="", status="failure",
                error=f"kimi spawn BrokenPipeError: {exc}",
            )
        return _map_generic_spawn_result(result, Provider.KIMI.value, model=model)


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
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
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
                base_ref=plan.base_ref or "origin/main",
                wt_path=wt_path,
                repo=_consumer_project_root,
            )
        except Exception:  # noqa: BLE001 — best-effort; None -> guard abstains, never false-rejects
            _phantom_diff = None
    finally:
        remove_dispatch_worktree(plan.dispatch_id, project_root=_consumer_project_root)

    report_path, receipt_path = _govern(
        enriched_spec, result, start, end, phantom_diff=_phantom_diff, integrity=integrity,
        base_ref=plan.base_ref or "origin/main",
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
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
        deadline_seconds=plan.deadline_seconds,
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
    )

    # INTEGRITY — persist the enriched final prompt + verify reconstruction
    integrity = _record_integrity(
        enriched_spec,
        enriched_instruction,
        instruction,
        bundle_dir=Path(plan.instruction_file).parent,
    )

    start = datetime.now(timezone.utc)
    result = ClaudeSubprocessAdapter().run(enriched_spec)
    end = datetime.now(timezone.utc)

    report_path, receipt_path = _govern(enriched_spec, result, start, end, integrity=integrity)

    return EnvelopeResult(
        status=result.status,
        returncode=0 if result.status == "success" else 1,
        report_path=report_path,
        receipt_path=receipt_path,
        completion_text=result.completion_text,
        error=result.error,
    )
