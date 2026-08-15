"""envelope_govern_support.py — GOVERN-seam support functions for the
dispatch_envelope module family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-3 of the dispatch-monolith-split
(dispatch-monolith-split, PR-3 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.

``_govern`` (the caller of four of these seven functions) moved to
envelope_govern.py in PR-4 of the dispatch-monolith-split — every
``patch("dispatch_envelope._archive_dispatch_events")``-style coupling that
used to bind against the facade's globals through ``_govern`` was re-targeted
to ``envelope_govern.<name>`` in that same PR, since a name-string coupling
resolves against the CALLER's globals, not this module's. The couplings that
bind against these functions' OWN globals (their
``logging.getLogger(__name__)`` logger name) were unaffected by that move.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from envelope_types import EnvelopeSpec

logger = logging.getLogger(__name__)


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
    """Fall back to the PUSHED branch's diff when the dispatch's own worktree/branch diff reads
    empty and the dispatch declares a work target — a fix-forward dispatch.

    A fix-forward dispatch pushes its commit onto an EXISTING branch (per its instruction), not
    onto its own ``dispatch/<id>`` worktree branch — the own-worktree diff then reads empty even
    though real work landed and was pushed. T0's rule is "verify the pushed branch, not the
    report" (phantom_guard module docstring). The work target is declared by precedence:
    ``spec.work_ref`` (an explicit branch name), ``spec.pr_id`` (an existing PR resolved to its
    head branch via ``gh pr view``), or ``spec.parent_dispatch`` (derived to
    ``dispatch/<parent>``). The resolution + fetch + diff live in
    ``phantom_guard.resolve_pushed_work_diff`` so the tmux and envelope lanes share one
    implementation.

    ``repo`` is the git checkout to resolve/fetch/diff against — the orchestrator's own repo
    root (the ephemeral dispatch worktree is gone or going away by the time GOVERN runs), not
    the worker's torn-down worktree. Defaults to ``project_root.resolve_project_root``.

    No-op for a normal dispatch: a non-empty ``own_diff`` short-circuits before any gh/git call,
    and a dispatch with no work_ref/pr_id/parent_dispatch returns ``own_diff`` untouched — so
    the own-worktree diff stays the sole source there (unchanged behavior). Best-effort: any
    resolution failure (no gh, bad pr_id, branch not pushed yet) falls back to ``own_diff``
    unchanged — a genuinely empty dispatch (no own diff, no resolvable/non-empty pushed branch)
    still reads empty here, so phantom_guard() still catches it.
    """
    if (own_diff or "").strip():
        return own_diff
    work_ref = (getattr(spec, "work_ref", None) or "").strip()
    pr_id = (spec.pr_id or "").strip()
    parent_dispatch = (getattr(spec, "parent_dispatch", None) or "").strip()
    if not work_ref and not pr_id and not parent_dispatch:
        return own_diff
    try:
        from phantom_guard import resolve_pushed_work_diff  # noqa: PLC0415
        if repo is None:
            from project_root import resolve_project_root  # noqa: PLC0415
            repo = resolve_project_root(__file__)
        pushed_diff = resolve_pushed_work_diff(
            work_ref=work_ref, pr_id=pr_id, parent_dispatch=parent_dispatch,
            base_ref=base_ref, repo=repo,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never raise, never false-reject on a resolution error
        logger.warning(
            "envelope: fix-forward diff resolution failed dispatch=%s: %s",
            spec.dispatch_id, exc,
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


def _archive_dispatch_events(
    terminal: Optional[str], dispatch_id: str
) -> "tuple[Optional[str], bool]":
    """Archive the live event stream under ``dispatch_id`` (end-of-dispatch teardown).

    The envelope path previously never archived the per-dispatch ring buffer at
    END-of-dispatch (OI-878 / OI-902): ``run_envelope_plan`` wrote events via the
    SubprocessAdapter's internal EventStore but only the NEXT dispatch's write-side
    boundary guard (EventStore.append, #1276) rotated the previous stream, so the
    LAST dispatch in a series leaked its events into the live file.

    Returns ``(events_path, clear_ok)``:
    - ``events_path`` is the archive path (str) when archived, else None.
    - ``clear_ok`` tells the caller whether the live stream may be truncated
      afterwards. It is False ONLY when the archive step itself raised and the
      live file still holds this dispatch's events — clearing then would destroy
      exactly the events that failed to archive (OI-918). It is True when the
      archive succeeded, when there was nothing to archive (empty/missing file),
      or when the live file holds a DIFFERENT dispatch's events (the caller's own
      clear guard already refuses to wipe a foreign stream).

    Guards against mislabeling: when the live file holds a DIFFERENT dispatch's
    events (this dispatch never wrote to the stream — e.g. a pre-spawn failure),
    the file is left untouched for the boundary guard of the next dispatch;
    archiving it here would mislabel the previous dispatch's events under our id.

    Best-effort — a failure never breaks the dispatch (mirrors the
    ``_emit_governance`` archive contract in provider_dispatch).
    """
    if not terminal or not dispatch_id:
        return None, True
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
            return None, True
        archived = store.archive(terminal, dispatch_id)
        if archived is not None:
            logger.info("envelope: end-dispatch archived %s -> %s", terminal, archived)
        return (str(archived) if archived is not None else None), True
    except Exception as exc:  # noqa: BLE001 — teardown must never break the dispatch
        logger.warning(
            "envelope: end-dispatch event archive failed terminal=%s dispatch=%s "
            "(non-fatal, live file left for the next dispatch's boundary guard): %s",
            terminal, dispatch_id, exc,
        )
        return None, False


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
