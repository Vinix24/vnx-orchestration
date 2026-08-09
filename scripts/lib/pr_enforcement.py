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
- ``dirty``    → niet van toepassing hier. De worktree heeft uncommitted wijzigingen
  die de worker zelf had moeten committen; een push hiervan kan niet deterministisch
  (er is niets om te pushen). Dit is phantom_guard's domein (lege extractie /
  niet-gecommit werk), niet rij-7. Blijft ok=True, applicable=False, met een reden
  die de caller laat zien dat er nog dirty werk is.

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
    applicable=True, ok=False: the branch was committed (or pushed) but the push
        or PR creation failed — a corrective receipt has already been appended.
    """
    applicable: bool
    ok: bool
    pr_number: Optional[int] = None
    created: bool = False
    pushed: bool = False
    reason: Optional[str] = None


def enforce_pr_exists(
    *,
    dispatch_id: str,
    branch: str,
    worktree_state: str,
    repo_root: Path,
    receipts_file: "str | Path",
    pr_title: str,
    pr_body: str,
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
        # Uncommitted changes are the worker's own unhandled working tree — there
        # is nothing deterministically pushable here, and a "push" of a dirty tree
        # cannot land. phantom_guard owns the not-committed-work failure mode; rij-7
        # binds only on committed-or-pushed work.
        return PrEnforcementResult(
            applicable=False, ok=True,
            reason=(
                f"worktree_state={worktree_state!r} — uncommitted changes left in the "
                f"worktree; nothing deterministically pushable (phantom_guard's domain)"
            ),
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
) -> None:
    """Append a corrective 'failed' completion receipt — the loud, receipt-visible
    signal that a committed-but-not-PR'd dispatch is incomplete. Never raises."""
    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        from append_receipt import append_receipt_payload  # noqa: PLC0415
        append_receipt_payload(
            {
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
            },
            receipts_file=str(receipts_file),
            cache_window_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 — a corrective-append failure must never break the lane
        logger.warning(
            "pr_enforcement: corrective receipt append failed dispatch=%s: %s", dispatch_id, exc,
        )
