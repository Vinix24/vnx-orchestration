#!/usr/bin/env python3
"""gate_obligation_retire_backlog.py — one-time honest booking for the
existing gate-obligation backlog (OI-1388).

Measured 2026-08-23 against the central store: 337 non-terminal obligations
(``pending``/``unresolvable``) — 58 already have a merged PR, 4 a closed
(never-merged) PR, and 212 never produced a PR at all with their head branch
already gone from origin. None of these can still be gated: the PR either
already closed the review window, or the dispatch died before ever opening
one. Yet nothing ever closed the obligation record — it just sits pending
forever, indistinguishable from a dispatch that is still running.

This script books the honest end-state for exactly those three shapes, using
:data:`gate_obligations.STATUS_RETIRED` — never ``fulfilled``, which would
falsely claim a review happened. Terminal, not re-run on a later pass
(idempotent): an obligation already in
``gate_obligations.TERMINAL_STATUSES`` is left untouched.

Discriminator: PR STATE, never age (operator decision 2026-08-23 — an age
threshold is not defensible in an audit: "why seven days and not five"). An
obligation with an OPEN PR is never touched, regardless of how old it is —
that PR can still gate it. Concretely, per dispatch_id/branch
``dispatch/<id>``:

  - open PR exists              -> left alone (still gateable)
  - PR merged                   -> retired, reason=pr_merged
  - PR closed without merge     -> retired, reason=pr_closed
  - no PR at all, branch gone   -> retired, reason=no_pr_branch_gone
  - no PR at all, branch exists -> left alone (class 3: cannot tell "still
                                    running" from "dead, branch never
                                    cleaned up" without an age threshold,
                                    which OI-1388 forbids using here — see
                                    the report this dispatch produced for the
                                    diagnostic count of that class)

No backdated gate results (OI-1259 hard boundary): ``resolved_at`` is stamped
at the moment THIS script books the record, never backdated to the historical
PR merge/close date — the ungated review window stays honestly ungated in the
record. The historical date is preserved in ``reason_detail`` instead.

Usage:
    python3 scripts/gate_obligation_retire_backlog.py            # dry run
    python3 scripts/gate_obligation_retire_backlog.py --write     # persist

Dry run is the default on purpose — this walks the whole backlog and the
operator reviews the counts before anything is written.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (SCRIPT_DIR / "lib", SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gate_obligations import (  # noqa: E402
    REASON_NO_PR_BRANCH_GONE,
    REASON_PR_CLOSED,
    REASON_PR_MERGED,
    STATUS_RETIRED,
    TERMINAL_STATUSES,
    iter_obligations,
    update_obligation,
)
from gate_obligation_runner import _gh_json, _resolve_github_owner_repo  # noqa: E402

# A growing repo needs headroom; 2026-08-23 measured 1537 merged + 113
# closed-without-merge + 3 open on this repo — 5000 leaves ample margin
# without needing pagination.
_GH_LIST_LIMIT = 5000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Live data fetchers (IO — kept separate from the pure classification logic
# below so tests exercise the logic against fixture data, never the real gh
# CLI or the real store).
# ---------------------------------------------------------------------------


def fetch_prs(owner_repo: str, state: str, fields: str, limit: int = _GH_LIST_LIMIT) -> List[Dict[str, Any]]:
    """Bulk ``gh pr list`` for one PR state; [] on any failure (never raises)."""
    data = _gh_json(
        ["pr", "list", "--state", state, "--json", fields, "--limit", str(limit)],
        owner_repo=owner_repo,
    )
    return data if isinstance(data, list) else []


def fetch_existing_branches(project_root: Path) -> Set[str]:
    """``dispatch/<id>`` branch names that currently exist on origin."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-remote", "--heads", "origin"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if proc.returncode != 0:
        return set()
    names: Set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            names.add(ref[len("refs/heads/"):])
    return names


def build_pr_index(
    merged_raw: List[Dict[str, Any]],
    closed_raw: List[Dict[str, Any]],
    open_raw: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Set[str]]:
    """Turn three raw ``gh pr list`` payloads into branch-keyed lookups.

    ``gh pr list --state closed`` returns EVERY non-open PR, merged included
    (measured 2026-08-23: 1650 closed-state PRs, 1537 of them with
    ``mergedAt`` set) — so ``closed_by_branch`` is filtered to the genuine
    closed-without-merge subset here, not left to the caller to get wrong.
    """
    merged_by_branch = {
        p["headRefName"]: p for p in merged_raw if p.get("headRefName")
    }
    closed_by_branch = {
        p["headRefName"]: p
        for p in closed_raw
        if p.get("headRefName") and not p.get("mergedAt")
    }
    open_branches = {p["headRefName"] for p in open_raw if p.get("headRefName")}
    return merged_by_branch, closed_by_branch, open_branches


# ---------------------------------------------------------------------------
# Pure classification — no IO, fully testable against fixture data.
# ---------------------------------------------------------------------------


def classify_obligation(
    dispatch_id: str,
    *,
    merged_by_branch: Dict[str, Dict[str, Any]],
    closed_by_branch: Dict[str, Dict[str, Any]],
    open_branches: Set[str],
    existing_branches: Set[str],
) -> Optional[Tuple[str, str, str]]:
    """Return ``(new_status, reason, reason_detail)``, or None to leave alone.

    An open PR always wins (never touched, regardless of any other signal):
    it can still gate the obligation. Class 3 (no PR at all, branch still on
    origin) also returns None — deliberately left for a human, per the
    module's ``REASON_NO_PR_BRANCH_EXISTS`` docstring.
    """
    branch = f"dispatch/{dispatch_id}"
    if branch in open_branches:
        return None
    if branch in merged_by_branch:
        pr = merged_by_branch[branch]
        number = pr.get("number")
        merged_at = pr.get("mergedAt") or "an unrecorded date"
        return (
            STATUS_RETIRED,
            REASON_PR_MERGED,
            f"PR #{number} merged on {merged_at} — the review window this "
            f"obligation declared is closed; nothing left to guard",
        )
    if branch in closed_by_branch:
        pr = closed_by_branch[branch]
        number = pr.get("number")
        closed_at = pr.get("closedAt") or "an unrecorded date"
        return (
            STATUS_RETIRED,
            REASON_PR_CLOSED,
            f"PR #{number} closed without merge on {closed_at} — nothing "
            f"left to guard",
        )
    if branch not in existing_branches:
        return (
            STATUS_RETIRED,
            REASON_NO_PR_BRANCH_GONE,
            f"dispatch {dispatch_id} never produced a PR and its head "
            f"branch {branch} no longer exists on origin — nothing left "
            f"to guard",
        )
    return None


def run_backlog_retirement(
    state_dir: Path,
    *,
    merged_by_branch: Dict[str, Dict[str, Any]],
    closed_by_branch: Dict[str, Dict[str, Any]],
    open_branches: Set[str],
    existing_branches: Set[str],
    write: bool = False,
) -> Dict[str, Any]:
    """Walk every obligation under ``state_dir`` and apply the classifier.

    Idempotent: an obligation already in ``TERMINAL_STATUSES`` (including an
    already-retired one from a prior run) is left untouched. Dry run
    (``write=False``, the default) never calls :func:`update_obligation` —
    it only reports what WOULD change.
    """
    obligations = list(iter_obligations(state_dir))

    before_by_status: Dict[str, int] = {}
    for _path, record in obligations:
        status = str(record.get("status") or "pending")
        before_by_status[status] = before_by_status.get(status, 0) + 1

    after_by_status: Dict[str, int] = dict(before_by_status)
    retired_by_reason: Dict[str, int] = {}
    class3_dispatch_ids: List[str] = []
    changes: List[Dict[str, Any]] = []

    for path, record in obligations:
        status = str(record.get("status") or "pending")
        dispatch_id = str(record.get("dispatch_id") or path.stem)

        if status in TERMINAL_STATUSES:
            if status == STATUS_RETIRED:
                reason = str(record.get("reason") or "")
                retired_by_reason[reason] = retired_by_reason.get(reason, 0) + 1
            continue

        outcome = classify_obligation(
            dispatch_id,
            merged_by_branch=merged_by_branch,
            closed_by_branch=closed_by_branch,
            open_branches=open_branches,
            existing_branches=existing_branches,
        )
        if outcome is None:
            branch = f"dispatch/{dispatch_id}"
            if branch in existing_branches:
                class3_dispatch_ids.append(dispatch_id)
            continue

        new_status, reason, reason_detail = outcome
        changes.append(
            {
                "dispatch_id": dispatch_id,
                "path": str(path),
                "from_status": status,
                "status": new_status,
                "reason": reason,
                "reason_detail": reason_detail,
            }
        )
        after_by_status[status] = after_by_status.get(status, 0) - 1
        after_by_status[new_status] = after_by_status.get(new_status, 0) + 1
        retired_by_reason[reason] = retired_by_reason.get(reason, 0) + 1

        if write:
            update_obligation(
                path,
                status=new_status,
                resolved_at=_utc_now_iso(),
                reason=reason,
                reason_detail=reason_detail,
            )

    return {
        "state_dir": str(state_dir),
        "write": write,
        "obligations_seen": len(obligations),
        "before_by_status": before_by_status,
        "after_by_status": after_by_status,
        "retired_by_reason": retired_by_reason,
        "class3_branch_exists_no_pr_count": len(class3_dispatch_ids),
        "class3_branch_exists_no_pr_dispatch_ids": class3_dispatch_ids,
        "changes": changes,
        "changed_count": len(changes),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="VNX state dir (default: resolved via vnx_paths ensure_env)",
    )
    parser.add_argument(
        "--project-root", type=Path, default=SCRIPT_DIR.parent,
        help="Checkout to run 'git ls-remote' against (default: this repo)",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Persist the retirements (default: dry run, changes nothing)",
    )
    parser.add_argument(
        "--limit", type=int, default=_GH_LIST_LIMIT,
        help=f"gh pr list --limit per state query (default: {_GH_LIST_LIMIT})",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    if args.state_dir is not None:
        state_dir = args.state_dir
    else:
        import vnx_paths  # noqa: PLC0415

        state_dir = Path(vnx_paths.ensure_env()["VNX_STATE_DIR"])
    if not state_dir.is_dir():
        print(f"ERROR: state dir not found: {state_dir}", file=sys.stderr)
        return 20

    owner_repo = _resolve_github_owner_repo(state_dir)
    if not owner_repo:
        print("ERROR: cannot resolve a GitHub owner/repo for this store", file=sys.stderr)
        return 20

    merged_raw = fetch_prs(owner_repo, "merged", "number,headRefName,mergedAt", args.limit)
    closed_raw = fetch_prs(owner_repo, "closed", "number,headRefName,mergedAt,closedAt", args.limit)
    open_raw = fetch_prs(owner_repo, "open", "number,headRefName", args.limit)
    merged_by_branch, closed_by_branch, open_branches = build_pr_index(merged_raw, closed_raw, open_raw)
    existing_branches = fetch_existing_branches(args.project_root)

    summary = run_backlog_retirement(
        state_dir,
        merged_by_branch=merged_by_branch,
        closed_by_branch=closed_by_branch,
        open_branches=open_branches,
        existing_branches=existing_branches,
        write=args.write,
    )
    summary["owner_repo"] = owner_repo

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"owner_repo={owner_repo} write={args.write} obligations_seen={summary['obligations_seen']}")
        print(f"before: {summary['before_by_status']}")
        print(f"after:  {summary['after_by_status']}")
        print(f"retired_by_reason (post-run total): {summary['retired_by_reason']}")
        print(
            "class 3 (no PR ever, branch still exists on origin — left "
            f"untouched, report only): {summary['class3_branch_exists_no_pr_count']}"
        )
        if args.write:
            print(f"{summary['changed_count']} obligation(s) retired this run")
        else:
            print(
                f"DRY RUN — {summary['changed_count']} obligation(s) would be "
                "retired; pass --write to persist"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
