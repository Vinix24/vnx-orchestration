#!/usr/bin/env python3
"""Fail-closed CI-run check for ``vnx merge-preflight`` (OI-1216).

Determines whether the configured CI workflow has a *successful* run for the
exact HEAD SHA being merged. The check toetst op het BESTAAN van een geslaagde
run voor die head, niet op de afwezigheid van een gefaalde run:

  - zero runs for the head sha              -> NO-GO ("niet toetsbaar")
  - any run for the head sha is running     -> NO-GO (in_progress/queued blocks)
  - exactly one completed run for the head  -> its conclusion decides
  - multiple completed runs for the head    -> the LATEST one decides (OI-1613)
  - order among completed runs can't be established -> NO-GO, own reason

Every unverifiable state (``gh`` missing, ``gh`` not authenticated, ``gh run
list`` failing or unparseable, HEAD unresolvable) is a NO-GO with its own
message. A missing tool never passes as "no red found".

OI-1387: the query is scoped to the exact commit (``gh run list --commit``),
NOT the branch. The tmux-spawn dispatch lane pushes a fix-forward commit to
both the target branch and its own per-dispatch auto-branch, so one sha can
have VNX CI runs on two branches at once. A branch-scoped query only sees the
run on the branch it was given and can call GO while a run for the exact same
sha is still in progress on the other branch — which is precisely the
disagreement OI-1387 measured between this gate (was branch-scoped) and the
review-gate's ``gh pr checks`` (always commit-scoped). Scoping both to the
commit makes them agree. ``branch`` remains an accepted argument/CLI flag for
callers that still resolve and pass it (e.g. ``pr_merge.py`` alongside
head_sha), but it no longer participates in the CI query — there is no
fallback to a branch-scoped query when head_sha is unresolvable, since that
fallback would silently reintroduce the exact defect being fixed here.

OI-1613: OI-1387's "every run on this commit must be conclusion=success" was
itself too strict — a run-history is immutable, so a sha that failed once and
was later re-verified GO (the tmux-spawn lane re-triggers CI on an UNCHANGED
head sha by closing/reopening the PR, precisely so bound review-gate evidence
for that sha stays valid) stayed NO-GO forever, with no way back except
changing the head sha and invalidating that evidence. Measured on four PRs
2026-09-02 (#1744, #1733, #1743, #1741): each carried an old failing run next
to a newer successful one on the same head, and the old "every run" rule
refused all four. The fix: among completed (non-running) runs on the commit,
the LATEST one by ``createdAt`` is the current truth; older runs on the same
sha are history, not a standing veto. The still-running guard (in_progress/
queued anywhere on the commit blocks, unconditionally) is unchanged — it is
not a "some runs disagree" case, it is "the truth for this commit isn't known
yet". Order is established from ``createdAt``, which this module now requests
explicitly and sorts itself; ``gh run list``'s own return order is not
documented as time-ordered and is never relied on. When order cannot be
established — ``createdAt`` missing or unparseable on any completed run, or
two completed runs sharing the exact same timestamp — that is a distinct
NO-GO ("kan de volgorde ... niet vaststellen"), never a silent fall-through to
"first in the list" or back to the old all-must-succeed rule.

Escape hatch (OI-1216 merge-gate): an operator may override the gate by
supplying a non-empty reason via ``override_reason`` /
``--override-reason`` / ``VNX_MERGE_OVERRIDE_REASON``. The reason IS the
override: an empty/whitespace reason is a refusal, never a silent bypass, and
every overridden verdict carries ``overridden=True`` plus the reason so it is
visible in the output rather than hidden.

This is the merge-preflight counterpart to ``pre_merge_gate.check_ci_workflow``
(OI-931), which enforces the same invariant at gate time. The two share the
same workflow-name resolution order (explicit arg > ``VNX_CI_WORKFLOW_NAME``
> "VNX CI"); they differ in verdict vocabulary (GO/NO-GO here) and messaging
(merge-preflight terms).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# CI workflow name queried via `gh run list --workflow`. Overridable per repo
# via VNX_CI_WORKFLOW_NAME — same resolution order as pre_merge_gate.
DEFAULT_CI_WORKFLOW_NAME = "VNX CI"
CI_WORKFLOW_NAME_ENV_VAR = "VNX_CI_WORKFLOW_NAME"

# Escape-hatch env var. The value is the required reason: a non-empty value
# overrides the gate (visibly), an empty value is a refusal, an unset variable
# means no override is attempted.
OVERRIDE_ENV_VAR = "VNX_MERGE_OVERRIDE_REASON"

# How many recent runs to pull for the commit. Matching is by exact head SHA,
# so a head older than the limit would read as "not run". The same limit is
# used by pre_merge_gate.check_ci_workflow.
RUN_LIST_LIMIT = 10

# Subprocess timeouts (seconds). Fail closed on expiry.
GH_AUTH_TIMEOUT = 10
GH_RUN_LIST_TIMEOUT = 15
GIT_TIMEOUT = 10


def _resolve_workflow_name(workflow_name: Optional[str]) -> str:
    """Resolve the workflow name: explicit arg > env var > fabric default.

    Mirrors pre_merge_gate._resolve_ci_workflow_name() — keep the two in sync.
    """
    if workflow_name:
        return workflow_name
    env_name = os.environ.get(CI_WORKFLOW_NAME_ENV_VAR)
    if env_name:
        return env_name
    return DEFAULT_CI_WORKFLOW_NAME


def _resolve_override_reason(override_reason: Optional[str]) -> Optional[str]:
    """Resolve the escape-hatch reason: explicit arg > env var > None.

    Returns the stripped reason string, or one of two sentinels that the caller
    must distinguish:
      - ``None``  -> no override attempted; run the normal fail-closed check.
      - ``""``    -> override attempted WITHOUT a reason; refuse (fail closed).
      - non-empty -> override granted, with this reason (visible in the verdict).
    """
    if override_reason is not None:
        return override_reason.strip()
    env_reason = os.environ.get(OVERRIDE_ENV_VAR)
    if env_reason is not None:
        return env_reason.strip()
    return None


def _capture(argv: List[str], *, timeout: int, cwd: Optional[str] = None) -> Tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Run a command and return (result, error_tag).

    error_tag is one of "missing" (binary not found), "timeout", or None. A
    non-zero exit is *not* an error_tag: the caller inspects ``returncode`` so
    it can distinguish "authenticated" from "command failed".
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return result, None
    except FileNotFoundError:
        return None, "missing"
    except subprocess.TimeoutExpired:
        return None, "timeout"


def _parse_created_at(value: Any) -> Optional[datetime]:
    """Parse a gh ``createdAt`` timestamp (e.g. ``2026-09-02T18:24:11Z``).

    Returns ``None`` when the value is missing, not a string, or fails to
    parse — the caller treats that as "order unknown", never as "sorts
    first" or "sorts last" (OI-1613).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _git(project_root: Path, args: List[str]) -> Optional[str]:
    """Resolve a git value inside project_root; None when unresolvable."""
    result, err = _capture(["git", *args], timeout=GIT_TIMEOUT, cwd=str(project_root))
    if err is not None or result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _no_go(message: str, **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "verdict": "NO-GO",
        "message": message,
        "ci_conclusion": None,
        "ran_on_sha": False,
        "head_sha": None,
        "ci_run_id": None,
        "overridden": False,
        "override_reason": None,
    }
    base.update(extra)
    return base


def _overridden_go(reason: str, head_sha: Optional[str], workflow_name: str) -> Dict[str, Any]:
    """GO verdict for an explicit operator override. Always visible, never silent."""
    return {
        "verdict": "GO",
        "message": f"OVERRIDE: VNX CI-check overgeslagen voor merge ({reason})",
        "ci_conclusion": None,
        "ran_on_sha": False,
        "head_sha": head_sha,
        "ci_run_id": None,
        "workflow_name": workflow_name,
        "overridden": True,
        "override_reason": reason,
    }


def check_ci_run_for_head(
    project_root: Path,
    *,
    branch: Optional[str] = None,
    head_sha: Optional[str] = None,
    gh_bin: str = "gh",
    workflow_name: Optional[str] = None,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Fail-closed check: every VNX CI run for the exact head_sha must be conclusion=success.

    Returns {"verdict": "GO"|"NO-GO", "message": str, ...}. ``project_root`` is
    the directory whose HEAD is being merged (the worktree under preflight).

    ``branch`` is accepted for callers that resolve and pass it alongside
    ``head_sha`` (e.g. ``pr_merge.py``), but per OI-1387 it is NOT used to scope
    the CI query — see the module docstring for why a branch-scoped query can
    disagree with the review-gate's commit-scoped one on the same commit.

    ``override_reason`` is the escape hatch: a non-empty value skips the check
    with a visible GO (``overridden=True``), an empty/whitespace value is a
    refusal, and None (or an unset env var) runs the normal check.
    """
    resolved_workflow = _resolve_workflow_name(workflow_name)

    # ── Escape hatch ──────────────────────────────────────────────────────
    reason = _resolve_override_reason(override_reason)
    if reason is not None:
        if not reason:
            return _no_go(
                "override zonder reden geweigerd: een override vereist een niet-lege reden "
                "(geen stille bypass)",
                workflow_name=resolved_workflow,
                overridden=True,
                override_reason=reason,
            )
        return _overridden_go(reason, head_sha, resolved_workflow)

    # ── gh availability ────────────────────────────────────────────────────
    if shutil.which(gh_bin) is None:
        return _no_go(
            "gh CLI niet beschikbaar: deze merge is niet toetsbaar",
            workflow_name=resolved_workflow,
        )

    # ── Resolve HEAD SHA ───────────────────────────────────────────────────
    # No fallback to a branch-scoped query when this fails: that fallback is
    # exactly the defect OI-1387 fixes (see module docstring).
    if head_sha is None:
        head_sha = _git(project_root, ["rev-parse", "HEAD"])
        if not head_sha:
            return _no_go(
                "HEAD-SHA kon niet worden bepaald: deze merge is niet toetsbaar",
                workflow_name=resolved_workflow,
            )

    # `gh run list --commit` silently returns zero rows (exit 0, no error) for
    # an abbreviated sha — measured 2026-08-23 on PR #1672, where that read as
    # "no CI ran" instead of "wrong query". A short head_sha here would recreate
    # that exact false-NO-GO, so it is refused explicitly rather than queried.
    if len(head_sha) < 40:
        return _no_go(
            f"head_sha '{head_sha}' is afgekort ({len(head_sha)} tekens): "
            "gh run list --commit vereist de volle 40-teken sha, anders komt er stil nul "
            "rijen terug: deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    # ── gh authentication ──────────────────────────────────────────────────
    auth, auth_err = _capture([gh_bin, "auth", "status"], timeout=GH_AUTH_TIMEOUT)
    if auth_err == "missing":
        return _no_go(
            "gh CLI niet beschikbaar: deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )
    if auth_err == "timeout":
        return _no_go(
            "gh auth status liep vast: deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )
    if auth is None or auth.returncode != 0:
        return _no_go(
            "gh is niet geauthenticeerd (gh auth status faalde): deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    # ── Query workflow runs, scoped to the exact commit (OI-1387) ──────────
    run_list, run_err = _capture(
        [
            gh_bin, "run", "list",
            "--commit", head_sha,
            "--workflow", resolved_workflow,
            "--limit", str(RUN_LIST_LIMIT),
            "--json", "conclusion,headSha,status,databaseId,createdAt",
        ],
        timeout=GH_RUN_LIST_TIMEOUT,
        cwd=str(project_root),
    )
    if run_err == "missing":
        return _no_go(
            "gh CLI niet beschikbaar: deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )
    if run_err == "timeout":
        return _no_go(
            f"gh run list liep vast voor workflow '{resolved_workflow}': deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )
    if run_list is None or run_list.returncode != 0:
        stderr = (run_list.stderr if run_list else "").strip()
        return _no_go(
            f"gh run list faalde voor workflow '{resolved_workflow}': deze merge is niet toetsbaar"
            + (f" ({stderr[:120]})" if stderr else ""),
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    try:
        runs = json.loads(run_list.stdout)
    except json.JSONDecodeError:
        return _no_go(
            f"gh-uitvoer niet te parsen voor workflow '{resolved_workflow}': deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    # ── Match runs to the exact HEAD SHA ────────────────────────────────────
    # ``--commit`` already scopes gh's response to head_sha; this filter is a
    # defensive no-op against a gh quirk returning extra entries.
    matching = [run for run in runs if run.get("headSha") == head_sha]
    if not matching:
        return _no_go(
            f"Geen VNX CI-run gevonden voor {head_sha[:12]}: deze merge is niet toetsbaar",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    # ── Still-running guard (OI-1387, unchanged) ────────────────────────────
    # Any in_progress/queued run on this commit blocks outright, regardless of
    # what any completed run on the same commit concluded: this is "the truth
    # for this commit isn't known yet", not "runs on this commit disagree" —
    # the latter is what the ordering logic below resolves.
    running = [run for run in matching if (run.get("status") or "") in ("in_progress", "queued")]
    if running:
        run_id = running[0].get("databaseId")
        return _no_go(
            f"{resolved_workflow} draait nog op {head_sha[:12]} (status: {running[0].get('status')}): "
            "deze merge is niet toetsbaar tot alle runs op deze commit op 'success' eindigen",
            ci_conclusion=None,
            ran_on_sha=True,
            head_sha=head_sha,
            ci_run_id=run_id,
            workflow_name=resolved_workflow,
        )

    # From here every run in `matching` is completed (in_progress/queued was
    # filtered above).
    completed = matching

    def _go(run_id: Any, detail: str) -> Dict[str, Any]:
        return {
            "verdict": "GO",
            "message": f"{resolved_workflow} geslaagd op {head_sha[:12]} ({detail})",
            "ci_conclusion": "success",
            "ran_on_sha": True,
            "head_sha": head_sha,
            "ci_run_id": run_id,
            "workflow_name": resolved_workflow,
            "overridden": False,
            "override_reason": None,
        }

    def _fail(run_id: Any, conclusion: str, detail: str) -> Dict[str, Any]:
        return _no_go(
            f"{resolved_workflow} conclusion is '{conclusion}' op {head_sha[:12]} ({detail}): "
            "deze merge is niet toetsbaar",
            ci_conclusion=conclusion or None,
            ran_on_sha=True,
            head_sha=head_sha,
            ci_run_id=run_id,
            workflow_name=resolved_workflow,
        )

    # ── Exactly one completed run: its conclusion decides (unchanged) ──────
    if len(completed) == 1:
        run = completed[0]
        run_id = run.get("databaseId")
        conclusion = run.get("conclusion") or ""
        if conclusion != "success":
            return _fail(run_id, conclusion, f"run {run_id}")
        return _go(run_id, f"run {run_id}")

    # ── Multiple completed runs on the same commit: the LATEST decides ─────
    # (OI-1613). A stale failing run next to a newer successful one on the
    # same head (the tmux-spawn lane's reopen-to-retrigger pattern) must not
    # veto forever — but if order genuinely cannot be established, that is
    # its own NO-GO, never a silent fall-through to "all must succeed" or
    # "first in the list".
    dated = [(_parse_created_at(run.get("createdAt")), run) for run in completed]
    if any(ts is None for ts, _ in dated):
        return _no_go(
            f"kan de volgorde van {len(completed)} klare VNX CI-runs op {head_sha[:12]} niet "
            "vaststellen (createdAt ontbreekt of is onparsebaar bij minstens een run): "
            "deze merge is niet toetsbaar",
            ran_on_sha=True,
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    dated.sort(key=lambda pair: pair[0], reverse=True)
    latest_ts, latest_run = dated[0]
    second_ts, _second_run = dated[1]
    if latest_ts == second_ts:
        return _no_go(
            f"kan de volgorde van {len(completed)} klare VNX CI-runs op {head_sha[:12]} niet "
            "vaststellen (twee runs delen exact dezelfde createdAt): deze merge is niet toetsbaar",
            ran_on_sha=True,
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )

    run_id = latest_run.get("databaseId")
    conclusion = latest_run.get("conclusion") or ""
    detail = f"laatste van {len(completed)} runs: {run_id}, createdAt {latest_run.get('createdAt')}"
    if conclusion != "success":
        return _fail(run_id, conclusion, detail)
    return _go(run_id, detail)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_preflight_ci_check",
        description="Fail-closed check: does VNX CI have a successful run for the exact HEAD SHA?",
    )
    parser.add_argument("--project-root", required=True, help="Directory whose HEAD is being merged")
    parser.add_argument(
        "--branch",
        help="Accepted for backward compat; NOT used to scope the CI query (OI-1387 — see module docstring)",
    )
    parser.add_argument("--head-sha", help="Exact HEAD SHA (defaults to git rev-parse HEAD)")
    parser.add_argument("--workflow", help="CI workflow name (default: VNX_CI_WORKFLOW_NAME or 'VNX CI')")
    parser.add_argument("--gh-bin", default="gh", help="Path/name of the gh binary")
    parser.add_argument(
        "--override-reason",
        default=None,
        help="Escape hatch: skip the check with this required reason (empty is refused). "
             "Also read from VNX_MERGE_OVERRIDE_REASON.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    args = parser.parse_args(argv)

    result = check_ci_run_for_head(
        Path(args.project_root),
        branch=args.branch,
        head_sha=args.head_sha,
        gh_bin=args.gh_bin,
        workflow_name=args.workflow,
        override_reason=args.override_reason,
    )

    if args.json:
        print(json.dumps(result))
    else:
        print(result["message"])
    return 0 if result["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
