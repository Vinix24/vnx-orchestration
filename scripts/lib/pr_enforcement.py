"""pr_enforcement.py — enforce that a build worker's pushed dispatch branch has an
open PR, in the tmux-spawn build-dispatch completion path.

Problem: a build worker commits and pushes its `dispatch/<id>` branch to origin but
frequently never runs `gh pr create`. The dispatch then "completes" with a branch
sitting on origin and no PR, and T0 has to salvage it by hand every time
(`gh pr create --head dispatch/<id>`).

This module is the enforcement chokepoint the tmux lane calls right before it
governs the dispatch (see tmux_interactive_dispatch.TmuxInteractiveDispatch's
teardown flow). It reuses gh_pr_ensure (the one shared gh-pr-create implementation
— auto_gate_trigger.py uses the same module) so there is exactly one `gh pr create`
invocation in the codebase, never two independently-drifting ones.

Enforcement, not best-effort: when the branch was pushed and PR creation fails, this
is recorded as a receipt-visible failure — a corrective 'failed' completion receipt
is appended to the ndjson ledger, mirroring phantom_guard's tier-0 override pattern
(dispatch_govern.dedup_completion_receipts honors ``autopr_rejected`` the same way
it honors ``phantom_rejected``) — so a dispatch that pushed real work but never got
a PR does NOT silently resolve as 'done'.

Out of scope: a branch that was never pushed (worktree_state != "pushed") has
nothing to enforce here — an empty/uncommitted worktree is phantom_guard's problem,
not this module's.

BILLING SAFETY: No Anthropic SDK. CLI-only (gh/git via subprocess, through
gh_pr_ensure).
"""

from __future__ import annotations

import logging
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

    applicable=False: the branch was never pushed — nothing to enforce, ok=True.
    applicable=True, ok=True: a PR exists (found or just created).
    applicable=True, ok=False: the branch was pushed but no PR could be found or
        created — a corrective receipt has already been appended.
    """
    applicable: bool
    ok: bool
    pr_number: Optional[int] = None
    created: bool = False
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
    """Ensure *branch* has an open PR when it was pushed to origin.

    ``worktree_state`` is the tmux_worktree.classify() verdict
    ("clean"/"committed"/"pushed"/"dirty"). Only "pushed" is in scope.

    Never raises: a gh_pr_ensure exception is treated as a creation failure (still
    enforced — reported via PrEnforcementResult.ok=False + corrective receipt), not
    propagated, so a transient GitHub/network error never crashes the dispatch lane.
    """
    if worktree_state != "pushed":
        return PrEnforcementResult(
            applicable=False, ok=True,
            reason=f"worktree_state={worktree_state!r} — nothing pushed to open a PR for",
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
            applicable=True, ok=True,
            pr_number=pr_number, created=bool(result.get("created")),
        )

    reason = result.get("reason") or f"gh pr create failed for branch {branch!r}"
    logger.warning(
        "pr_enforcement: REJECTED dispatch=%s branch=%s — %s", dispatch_id, branch, reason,
    )
    _record_corrective_receipt(
        dispatch_id=dispatch_id, branch=branch, reason=reason, receipts_file=receipts_file,
    )
    return PrEnforcementResult(applicable=True, ok=False, reason=reason)


def _record_corrective_receipt(
    *, dispatch_id: str, branch: str, reason: str, receipts_file: "str | Path",
) -> None:
    """Append a corrective 'failed' completion receipt — the loud, receipt-visible
    signal that a pushed-but-PR-less dispatch is incomplete. Never raises."""
    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        from append_receipt import append_receipt_payload  # noqa: PLC0415
        append_receipt_payload(
            {
                "event_type": "subprocess_completion",
                "dispatch_id": dispatch_id,
                "status": "failed",
                "autopr_rejected": True,
                "autopr_reason": reason,
                "branch": branch,
                "source": "pr_enforcement",
                "synthesized": False,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            receipts_file=str(receipts_file),
            cache_window_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 — a corrective-append failure must never break the lane
        logger.warning(
            "pr_enforcement: corrective receipt append failed dispatch=%s: %s", dispatch_id, exc,
        )
