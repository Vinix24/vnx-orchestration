"""pr_enforcement.py — enforce that a build worker's dispatch branch is pushed to
origin AND has an open PR, in every lane's completion path.

Problem: a build worker commits its `dispatch/<id>` branch but frequently never
pushes it, or pushes it but never runs `gh pr create`. Either way the dispatch
"completes" with work stranded locally (lost on the next reap) or with a branch
sitting on origin and no PR — and T0 has to salvage it by hand every time
(`gh pr create --head dispatch/<id>`).

This module is the single enforcement chokepoint every lane calls right before it
governs the dispatch (see tmux_interactive_dispatch.TmuxInteractiveDispatch's
teardown flow; run_envelope_plan / run_envelope_headless_plan in the envelope
family). It reuses gh_pr_ensure (the one shared gh-pr-create implementation
— auto_gate_trigger.py uses the same module) so there is exactly one `gh pr create`
invocation in the codebase, never two independently-drifting ones.

Per-state decision (one binding site, never duplicated across lanes):
- ``pushed``   → PR afdwingen (bestaand gedrag).
- ``committed`` → pushen, dan PR afdwingen. Dit is rij-7 van de lane-matrix: een
  worktree met commits die niet op origin staan is de gevaarlijkste staat die er
  is, want bij een reap is het werk weg. Een mislukte push is een luide,
  receipt-zichtbare uitkomst (ok=False + corrective receipt), geen ``exit 0``.
- ``clean``    → niet van toepassing; er is niets om te pushen of een PR voor te
  openen. Blijft ok=True, applicable=False.
- ``dirty``    → gesplitst (OI-1119). ``git status --porcelain`` on a dirty tree
  mixes two very different things: a worker's own scratch/editor droppings
  (untracked, ``??``) and real tracked source/test edits it simply never
  committed. The former stays what it always was — phantom_guard's domain,
  ok=True, applicable=False. The latter is a delivery failure exactly like an
  unpushed ``committed`` branch (the reaper destroys it the same way): loud,
  receipt-visible (ok=False, corrective receipt), AND — when ``wt_path`` is
  supplied — salvaged: the tracked changes, plus any untracked non-gitignored
  files sitting next to them (OI-1128: a worker's genuinely new file that
  never saw ``git add``), are committed onto *branch* under an
  unmistakable ``[SALVAGED, UNREVIEWED]`` marker, pushed, and (unless
  ``skip_pr``) opened as a **draft** PR so it can never be mistaken for a
  worker-vouched, ready-for-review delivery. See ``_classify_dirty_worktree``
  and ``_handle_dirty_substantive``. Callers that do not pass ``wt_path`` keep
  the pre-OI-1119 behaviour unchanged (ok=True, applicable=False) — this is an
  opt-in upgrade, not a silent behavior change for every caller.

Containment (OI-1113): when *target_remote_head* is provided (the remote HEAD of
the target branch captured BEFORE the worker started), this module verifies after
the push that the new remote HEAD contains the old one via
``git merge-base --is-ancestor``.  A non-fast-forward replacement (force-push that
dropped commits) is refused with a receipt-visible ``containment_failed`` corrective
receipt — the same loud failure pattern as ``push_failed`` and ``pr_failed``.

Enforcement, not best-effort: when the branch was committed (or pushed) and the
push or PR creation fails, this is recorded as a receipt-visible failure — a
corrective 'failed' completion receipt is appended to the ndjson ledger, mirroring
phantom_guard's tier-0 override pattern (dispatch_govern.dedup_completion_receipts
honors ``autopr_rejected`` the same way it honors ``phantom_rejected``) — so a
dispatch that committed real work but never got it to a PR does NOT silently
resolve as 'done'.

BILLING SAFETY: No Anthropic SDK. CLI-only (gh/git via subprocess, through
gh_pr_ensure).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# append_receipt.py lives in scripts/, not scripts/lib — mirrors
# dispatch_govern.ensure_receipt / phantom_guard.record_phantom_if_any.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)


@dataclass(frozen=True)
class PrEnforcementResult:
    """Outcome of enforce_pr_exists().

    applicable=False: the state had nothing to push (clean/dirty) — ok=True.
    applicable=True, ok=True: the branch is on origin AND a PR exists (found or
        just created). When the state was ``committed``, the branch was pushed
        first (pushed=True records that the push step ran).
    applicable=True, ok=False: the branch was committed (or pushed) but the push,
        PR creation, or containment check failed — a corrective receipt has
        already been appended.
    """
    applicable: bool
    ok: bool
    pr_number: Optional[int] = None
    created: bool = False
    pushed: bool = False
    reason: Optional[str] = None


def _get_remote_head(*, branch: str, repo_root: Path) -> "Optional[str]":
    """Return the remote HEAD SHA of *branch* on origin, or None.

    Uses ``git ls-remote origin refs/heads/<branch>``.  Returns None when the
    branch does not exist on origin or when the lookup fails (network error,
    timeout).  Never raises: a lookup failure is a degraded skip, not a crash.
    """
    remote_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "origin", remote_ref],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, Exception):
        return None
    if proc.returncode != 0:
        return None
    output = proc.stdout.strip()
    if not output:
        return None
    return output.split()[0]


def _check_containment(*, branch: str, old_head: str, repo_root: Path) -> "tuple[bool, Optional[str]]":
    """Verify *old_head* is an ancestor of the current remote HEAD of *branch*.

    Returns ``(True, None)`` when containment holds (fast-forward or merge).
    Returns ``(False, reason)`` when the check fails or the new HEAD cannot be
    resolved.  Never raises.
    """
    new_head = _get_remote_head(branch=branch, repo_root=repo_root)
    if new_head is None:
        return False, f"cannot resolve remote HEAD of {branch!r} for containment check"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", old_head, new_head],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        return False, f"containment check raised for {branch!r}: {exc}"
    if proc.returncode == 0:
        return True, None
    return False, (
        f"containment violated: remote HEAD of {branch!r} ({new_head[:12]}) "
        f"does not contain the pre-worker HEAD ({old_head[:12]}) — "
        f"the branch history was replaced (force-push detected)"
    )


@dataclass(frozen=True)
class _DirtyClassification:
    """Verdict of _classify_dirty_worktree(): whether a ``dirty`` tree carries
    real (tracked) uncommitted work, or only untracked scratch.

    ``untracked_paths`` (OI-1128): the ``??`` paths ``git status --porcelain``
    listed. git status respects .gitignore natively, so ignored junk
    (.DS_Store, build artefacts covered by .gitignore) never appears here.
    When the tree is substantive, these ride along in the salvage — on
    2026-08-10 two of the three hand-rescued files were ``??`` (a worker
    that creates a NEW file and never runs ``git add`` leaves it exactly
    here), so excluding them re-creates the OI-1119 loss in the
    new-file variant."""
    substantive: bool
    evidence: str
    tracked_paths: "tuple[str, ...]" = ()
    untracked_paths: "tuple[str, ...]" = ()


def _classify_dirty_worktree(*, wt_path: "Path | str") -> _DirtyClassification:
    """Split the ``dirty`` verdict (OI-1119) into the two cases it conflates.

    Rule, evidence-based: a change is substantive when git already knows the
    path — modified, staged, deleted, renamed, copied, or conflicted (any
    ``git status --porcelain`` code other than ``??``). A tree with ONLY
    untracked paths stays non-substantive: their mere presence is not evidence
    a worker left real work behind, and going loud on every stray scratch file
    would turn ordinary dispatches into false failures. This re-derives
    file-level detail from the same git flags tmux_worktree.classify_path uses
    for its yes/no verdict, without duplicating that verdict itself —
    classify_path still owns clean/committed/pushed/dirty; this only refines
    what "dirty" means.

    OI-1128 refinement: once a tree IS substantive, the untracked (``??``)
    paths are collected too and salvaged alongside the tracked ones. The
    distinguishing signal between "new source file the worker forgot to add"
    and "editor droppings" is .gitignore — ``git status`` already applies it,
    so anything the repo declares junk never shows up. ``-uall`` lists files
    inside new directories individually (a new ``tests/foo/`` otherwise
    collapses to one directory entry), so the receipt names the actual files.

    Never raises: a git failure degrades to substantive=False (the existing
    safe not-applicable behaviour) — a broken git invocation must never turn
    into a false-positive loud failure or a hung/crashed dispatch.
    """
    try:
        proc = subprocess.run(
            [
                "git", "-c", "core.fileMode=false", "-c", "core.autocrlf=input",
                "-c", "core.quotePath=false",
                "-C", str(wt_path), "status", "--porcelain", "-uall",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the lane
        return _DirtyClassification(
            substantive=False, evidence=f"dirty-classification git status raised: {exc}",
        )
    if proc.returncode != 0:
        return _DirtyClassification(
            substantive=False,
            evidence=(
                f"dirty-classification git status failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            ),
        )

    tracked_paths: "list[str]" = []
    untracked_paths: "list[str]" = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path_part = line[:2], line[3:]
        if code == "??":
            path = path_part.strip()
            if path:
                untracked_paths.append(path)
            continue
        path = path_part.split(" -> ")[-1].strip()
        if path:
            tracked_paths.append(path)

    if not tracked_paths:
        return _DirtyClassification(
            substantive=False,
            evidence=(
                f"dirty worktree has only untracked paths ({len(untracked_paths)} file(s)) — "
                "no tracked source/test changes; treated as scratch/non-substantive"
            ),
            untracked_paths=tuple(untracked_paths),
        )

    shown = tracked_paths[:10]
    more = len(tracked_paths) - len(shown)
    listing = ", ".join(shown) + (f", +{more} more" if more else "")
    evidence = (
        f"dirty worktree has {len(tracked_paths)} tracked file(s) with uncommitted "
        f"changes the worker never committed: {listing}"
    )
    if untracked_paths:
        u_shown = untracked_paths[:10]
        u_more = len(untracked_paths) - len(u_shown)
        u_listing = ", ".join(u_shown) + (f", +{u_more} more" if u_more else "")
        evidence += (
            f"; plus {len(untracked_paths)} untracked non-gitignored file(s) "
            f"salvaged alongside (OI-1128): {u_listing}"
        )
    return _DirtyClassification(
        substantive=True,
        evidence=evidence,
        tracked_paths=tuple(tracked_paths),
        untracked_paths=tuple(untracked_paths),
    )


def enforce_pr_exists(
    *,
    dispatch_id: str,
    branch: str,
    worktree_state: str,
    repo_root: Path,
    receipts_file: "str | Path",
    pr_title: str,
    pr_body: str,
    target_remote_head: "Optional[str]" = None,
    skip_pr: bool = False,
    wt_path: "Optional[Path | str]" = None,
) -> PrEnforcementResult:
    """Ensure *branch* is pushed to origin AND has an open PR.

    ``worktree_state`` is the tmux_worktree.classify() verdict
    ("clean"/"committed"/"pushed"/"dirty"). This is the ONE binding site for the
    per-state push+PR decision — every lane calls here, never re-implements it.

    Per state (rij-7, lane-matrix):
    - ``pushed``   → PR afdwingen (bestaand gedrag).
    - ``committed`` → pushen, dan PR afdwingen.
    - ``clean``    → niets om te pushen → applicable=False, ok=True.
    - ``dirty``    → niet hier; phantom_guard's domein → applicable=False, ok=True.

    *target_remote_head* (OI-1113): the remote HEAD SHA of *branch* captured
    BEFORE the worker started.  When set, containment is verified after the
    push: the new remote HEAD must contain *target_remote_head* (fast-forward or
    merge).  A non-fast-forward replacement is refused with a receipt-visible
    ``containment_failed`` corrective receipt.  When ``None`` (new branch, no
    prior remote HEAD), the containment check is skipped — there is nothing to
    contain.

    *skip_pr* (OI-1115): when ``True``, the PR creation step is skipped.  The
    push still runs for the ``committed`` state.  This is used when the dispatch
    operates on an existing dispatch branch (``base_ref`` starts with
    ``origin/dispatch/``) — the PR already exists, and creating a second one is
    a duplicate destination.

    *wt_path* (OI-1119): the worktree's filesystem path.  Required to split a
    ``dirty`` verdict into substantive (real tracked edits, never committed)
    vs non-substantive (scratch/untracked only) — see
    ``_classify_dirty_worktree``.  When ``None`` (a caller that hasn't wired
    this through yet), a ``dirty`` verdict keeps the pre-OI-1119 behaviour:
    applicable=False, ok=True.

    Never raises: a push or gh_pr_ensure exception is treated as a failure (still
    enforced — reported via PrEnforcementResult.ok=False + corrective receipt), not
    propagated, so a transient git/GitHub/network error never crashes the dispatch
    lane.
    """
    if worktree_state == "clean":
        return PrEnforcementResult(
            applicable=False, ok=True,
            reason=f"worktree_state={worktree_state!r} — no changes to push or open a PR for",
        )
    if worktree_state == "dirty":
        if wt_path is None:
            # Back-compat: a caller that has not wired the worktree path through
            # yet gets the original degraded-safe behaviour, not a silent upgrade
            # to a check it never opted into.
            return PrEnforcementResult(
                applicable=False, ok=True,
                reason=(
                    f"worktree_state={worktree_state!r} — uncommitted changes left in the "
                    f"worktree; wt_path not provided, cannot classify substantive vs "
                    "scratch (phantom_guard's domain)"
                ),
            )
        classification = _classify_dirty_worktree(wt_path=wt_path)
        if not classification.substantive:
            return PrEnforcementResult(applicable=False, ok=True, reason=classification.evidence)
        return _handle_dirty_substantive(
            dispatch_id=dispatch_id, branch=branch, wt_path=Path(wt_path),
            repo_root=repo_root, receipts_file=receipts_file,
            tracked_paths=classification.tracked_paths,
            untracked_paths=classification.untracked_paths,
            evidence=classification.evidence,
            pr_title=pr_title, pr_body=pr_body, skip_pr=skip_pr,
        )

    # committed or pushed are the two states with work that must reach a PR.
    pushed = worktree_state == "pushed"
    if worktree_state == "committed":
        push_outcome = _push_branch(branch=branch, repo_root=repo_root)
        if not push_outcome.ok:
            reason = push_outcome.reason
            logger.warning(
                "pr_enforcement: push FAILED dispatch=%s branch=%s — %s",
                dispatch_id, branch, reason,
            )
            _record_corrective_receipt(
                dispatch_id=dispatch_id, branch=branch, reason=reason,
                receipts_file=receipts_file, kind="push_failed",
            )
            return PrEnforcementResult(applicable=True, ok=False, pushed=False, reason=reason)
        pushed = True

    # ── OI-1113: containment check ──────────────────────────────────────────
    # After the branch is on origin (our push or the worker's own), verify the
    # new remote HEAD contains the pre-worker HEAD.  A non-fast-forward
    # replacement means the worker (or something else) force-pushed and dropped
    # commits — refuse with a receipt-visible failure.
    if target_remote_head is not None:
        contained, containment_reason = _check_containment(
            branch=branch, old_head=target_remote_head, repo_root=repo_root,
        )
        if not contained:
            logger.warning(
                "pr_enforcement: containment FAILED dispatch=%s branch=%s — %s",
                dispatch_id, branch, containment_reason,
            )
            _record_corrective_receipt(
                dispatch_id=dispatch_id, branch=branch, reason=containment_reason,
                receipts_file=receipts_file, kind="containment_failed",
            )
            return PrEnforcementResult(
                applicable=True, ok=False, pushed=pushed, reason=containment_reason,
            )

    # ── OI-1115: skip auto-PR when the dispatch works on an existing branch ──
    if skip_pr:
        logger.info(
            "pr_enforcement: PR skipped for dispatch=%s branch=%s (skip_pr=True) — "
            "branch is on origin, no auto-PR created",
            dispatch_id, branch,
        )
        return PrEnforcementResult(
            applicable=True, ok=True, pushed=pushed, pr_number=None, created=False,
            reason="skip_pr=True — branch pushed, auto-PR suppressed (existing dispatch branch)",
        )

    try:
        from gh_pr_ensure import ensure_pr  # noqa: PLC0415
        result = ensure_pr(branch, repo_root, title=pr_title, body=pr_body, draft=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pr_enforcement: ensure_pr raised for %s: %s", branch, exc)
        result = {"pr_number": None, "created": False, "reason": f"ensure_pr exception: {exc}"}

    pr_number = result.get("pr_number")
    if pr_number is not None:
        return PrEnforcementResult(
            applicable=True, ok=True, pushed=pushed,
            pr_number=pr_number, created=bool(result.get("created")),
        )

    reason = result.get("reason") or f"gh pr create failed for branch {branch!r}"
    logger.warning(
        "pr_enforcement: REJECTED dispatch=%s branch=%s — %s", dispatch_id, branch, reason,
    )
    _record_corrective_receipt(
        dispatch_id=dispatch_id, branch=branch, reason=reason, receipts_file=receipts_file,
        kind="pr_failed",
    )
    return PrEnforcementResult(applicable=True, ok=False, pushed=pushed, reason=reason)


def _handle_dirty_substantive(
    *,
    dispatch_id: str,
    branch: str,
    wt_path: Path,
    repo_root: Path,
    receipts_file: "str | Path",
    tracked_paths: "tuple[str, ...]",
    evidence: str,
    pr_title: str,
    pr_body: str,
    skip_pr: bool,
    untracked_paths: "tuple[str, ...]" = (),
) -> PrEnforcementResult:
    """OI-1119: a ``dirty`` tree with substantive uncommitted work — loud AND salvaged.

    Decision (see the dispatch report for the full defence): reporting loudly
    alone leaves the work itself to be destroyed by the reaper — it only makes
    the loss visible after the fact. Salvaging alone would silently promote a
    diff no worker ever reviewed into normal history. Doing both keeps the
    review boundary (the dispatch still resolves ok=False, exactly like a
    failed push) while preventing the data loss T0 was hand-salvaging.

    Salvage commits ONLY the explicit paths this call already classified
    (never ``git add -A``): the tracked paths that made the tree substantive,
    plus — OI-1128 — the untracked non-gitignored paths ``git status`` listed
    next to them. A worker's genuinely NEW file (created, never ``git add``ed)
    is ``??`` by definition and was previously left to the reaper; .gitignore
    is the junk filter, applied by git itself before the paths ever reach this
    function. The commit is unmistakably marked ``[SALVAGED, UNREVIEWED]``
    (mirrors the existing fleet convention, e.g. PR #1444), pushed, and —
    unless ``skip_pr`` (an existing PR already covers this branch) — opened as
    a **draft** PR with a title/body banner saying the same thing, so it can
    never be mistaken for a normal, ready-for-review delivery.

    Never raises: every git/gh step is wrapped; a failure at any stage still
    reaches the corrective receipt below (kind="dirty_substantive_unsalvaged")
    instead of crashing or hanging the dispatch teardown.
    """
    reason = evidence
    committed = False
    pushed = False
    pr_number: "Optional[int]" = None

    try:
        add_proc = subprocess.run(
            ["git", "-C", str(wt_path), "add", "--", *tracked_paths, *untracked_paths],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to unsalvaged, still loud
        add_proc = None
        reason = f"{evidence}; salvage git-add raised: {exc}"

    if add_proc is not None and add_proc.returncode != 0:
        reason = f"{evidence}; salvage git-add failed: {(add_proc.stderr or '').strip()}"
        add_proc = None

    if add_proc is not None:
        commit_message = (
            f"chore(salvage): unvouched auto-salvage of uncommitted work [SALVAGED, UNREVIEWED]\n\n"
            f"Auto-committed by pr_enforcement (OI-1119) because dispatch {dispatch_id} left "
            "substantive tracked changes uncommitted when the worktree was about to be reaped. "
            "No worker reviewed or vouched for this diff — treat with elevated scrutiny before "
            "merging.\n\n"
            f"Dispatch-ID: {dispatch_id}"
        )
        try:
            commit_proc = subprocess.run(
                ["git", "-C", str(wt_path), "commit", "-m", commit_message],
                capture_output=True, text=True, timeout=30,
            )
            committed = commit_proc.returncode == 0
            if not committed:
                reason = f"{evidence}; salvage git-commit failed: {(commit_proc.stderr or '').strip()}"
        except Exception as exc:  # noqa: BLE001
            reason = f"{evidence}; salvage git-commit raised: {exc}"

    if committed:
        push_outcome = _push_branch(branch=branch, repo_root=repo_root)
        pushed = push_outcome.ok
        if not pushed:
            reason = f"{evidence}; salvage committed locally but push failed: {push_outcome.reason}"

    if pushed and not skip_pr:
        try:
            from gh_pr_ensure import ensure_pr  # noqa: PLC0415
            salvage_title = f"[SALVAGED-UNVOUCHED] {pr_title}"
            salvage_body = (
                "**Auto-generated by push-enforcement salvage (OI-1119). No worker committed "
                "or reviewed this diff** — it was recovered from a dirty worktree about to be "
                "reaped. Do not merge without review; verify the diff matches the dispatch "
                "instruction before treating this as a normal delivery.\n\n---\n\n" + pr_body
            )
            pr_result = ensure_pr(branch, repo_root, title=salvage_title, body=salvage_body, draft=True)
            pr_number = pr_result.get("pr_number")
            if pr_number is None:
                reason = f"{evidence}; salvage pushed but PR creation failed: {pr_result.get('reason')}"
        except Exception as exc:  # noqa: BLE001
            reason = f"{evidence}; salvage pushed but PR creation raised: {exc}"

    if pushed:
        kind = "dirty_substantive_salvaged"
        reason = (
            f"{evidence}; SALVAGED as unvouched [SALVAGED, UNREVIEWED] commit on {branch!r}"
            + (f", draft PR #{pr_number}" if pr_number else "")
        )
    else:
        kind = "dirty_substantive_unsalvaged"

    logger.warning(
        "pr_enforcement: DIRTY-SUBSTANTIVE dispatch=%s branch=%s salvaged=%s — %s",
        dispatch_id, branch, pushed, reason,
    )
    _record_corrective_receipt(
        dispatch_id=dispatch_id, branch=branch, reason=reason, receipts_file=receipts_file,
        kind=kind,
        extra_fields={
            "dirty_substantive": True,
            "dirty_files": list(tracked_paths[:25]),
            "dirty_file_count": len(tracked_paths),
            # OI-1128: the ?? (untracked, non-gitignored) paths salvaged alongside.
            "salvaged_untracked_files": list(untracked_paths[:25]),
            "salvaged_untracked_count": len(untracked_paths),
            "salvaged": pushed,
            "salvage_pr_number": pr_number,
        },
    )
    return PrEnforcementResult(
        applicable=True, ok=False, pushed=pushed, pr_number=pr_number, reason=reason,
    )


@dataclass(frozen=True)
class _PushOutcome:
    ok: bool
    reason: "Optional[str]" = None


def _push_branch(*, branch: str, repo_root: Path) -> "_PushOutcome":
    """Push *branch* (``dispatch/<id>``) to origin. Never raises.

    Runs ``git push -u origin <branch>`` from *repo_root*. The branch already
    exists locally (the worker committed to it); this is the step the worker
    skipped. A failed push returns ok=False with the git stderr as the reason —
    the caller records a corrective receipt, so a committed-but-not-pushed
    dispatch never silently resolves as 'done'.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "push", "-u", "origin", branch],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — a push error must never crash the lane
        return _PushOutcome(ok=False, reason=f"git push raised: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return _PushOutcome(
            ok=False,
            reason=f"git push origin {branch} failed (rc={proc.returncode}): {stderr}",
        )
    return _PushOutcome(ok=True)


def _record_corrective_receipt(
    *, dispatch_id: str, branch: str, reason: str, receipts_file: "str | Path",
    kind: str = "pr_failed",
    extra_fields: "Optional[dict]" = None,
) -> None:
    """Append a corrective 'failed' completion receipt — the loud, receipt-visible
    signal that a committed-but-not-PR'd dispatch is incomplete. Never raises.

    *extra_fields* (OI-1119): merged into the payload after the base fields
    below, so a caller (e.g. the dirty-substantive path) can attach
    kind-specific evidence — file lists, salvage outcome — without every
    other corrective-receipt caller needing to know about it.
    """
    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        from append_receipt import append_receipt_payload  # noqa: PLC0415
        payload = {
            "event_type": "subprocess_completion",
            "receipt_kind": "dispatch",
            "dispatch_id": dispatch_id,
            "status": "failed",
            "autopr_rejected": True,
            "autopr_reason": reason,
            "autopr_kind": kind,
            "branch": branch,
            "source": "pr_enforcement",
            "synthesized": False,
            # dispatch-20260802-model-ssot-en-ketenlink: carry the dispatch
            # model when the door exported it (best-effort; exempt source).
            "model": os.environ.get("VNX_CURRENT_MODEL") or None,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if extra_fields:
            payload.update(extra_fields)
        append_receipt_payload(
            payload,
            receipts_file=str(receipts_file),
            cache_window_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 — a corrective-append failure must never break the lane
        logger.warning(
            "pr_enforcement: corrective receipt append failed dispatch=%s: %s", dispatch_id, exc,
        )
