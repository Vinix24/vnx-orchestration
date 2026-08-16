#!/usr/bin/env python3
"""Fail-closed CI-run check for ``vnx merge-preflight`` (OI-1216).

Determines whether the configured CI workflow has a *successful* run for the
exact HEAD SHA being merged. The check toetst op het BESTAAN van een geslaagde
run voor die head, niet op de afwezigheid van een gefaalde run:

  - zero runs for the head            -> NO-GO ("niet toetsbaar")
  - run with conclusion != "success"  -> NO-GO (in_progress is NO-GO too)
  - success but for a DIFFERENT sha   -> NO-GO (an older commit does not count)
  - success for the exact head        -> GO

Every unverifiable state (``gh`` missing, ``gh`` not authenticated, ``gh run
list`` failing or unparseable, HEAD/branch unresolvable) is a NO-GO with its
own message. A missing tool never passes as "no red found".

Escape hatch (OI-1216 merge-gate): an operator may override the gate by
supplying a non-empty reason via ``override_reason`` /
``--override-reason`` / ``VNX_MERGE_OVERRIDE_REASON``. The reason IS the
override: an empty/whitespace reason is a refusal, never a silent bypass, and
every overridden verdict carries ``overridden=True`` plus the reason so it is
visible in the output rather than hidden.

This is the merge-preflight counterpart to ``pre_merge_gate.check_ci_workflow``
(OI-931), which enforces the same invariant at gate time. The two share the
same ``gh run list`` invocation shape and the same workflow-name resolution
order (explicit arg > ``VNX_CI_WORKFLOW_NAME`` > "VNX CI"); they differ in
verdict vocabulary (GO/NO-GO here) and messaging (merge-preflight terms).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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

# How many recent runs to pull per branch. Matching is by exact head SHA, so a
# head older than the limit on a busy branch would read as "not run". The same
# limit is used by pre_merge_gate.check_ci_workflow.
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
    """Fail-closed check: a VNX CI run with conclusion=success must exist for head_sha.

    Returns {"verdict": "GO"|"NO-GO", "message": str, ...}. ``project_root`` is
    the directory whose HEAD is being merged (the worktree under preflight).

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
    if head_sha is None:
        head_sha = _git(project_root, ["rev-parse", "HEAD"])
        if not head_sha:
            return _no_go(
                "HEAD-SHA kon niet worden bepaald: deze merge is niet toetsbaar",
                workflow_name=resolved_workflow,
            )

    # ── Resolve branch ─────────────────────────────────────────────────────
    if branch is None:
        branch = _git(project_root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if not branch:
            return _no_go(
                "branch kon niet worden bepaald: deze merge is niet toetsbaar",
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

    # ── Query workflow runs ────────────────────────────────────────────────
    run_list, run_err = _capture(
        [
            gh_bin, "run", "list",
            "--branch", branch,
            "--workflow", resolved_workflow,
            "--limit", str(RUN_LIST_LIMIT),
            "--json", "conclusion,headSha,status,databaseId",
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

    # ── Match a run to the exact HEAD SHA ──────────────────────────────────
    for run in runs:
        if run.get("headSha") != head_sha:
            continue
        conclusion = run.get("conclusion") or ""
        status = run.get("status") or ""
        run_id = run.get("databaseId")
        if conclusion == "success":
            return {
                "verdict": "GO",
                "message": (
                    f"{resolved_workflow} geslaagd op {head_sha[:12]} "
                    f"(run {run_id})"
                ),
                "ci_conclusion": conclusion,
                "ran_on_sha": True,
                "head_sha": head_sha,
                "ci_run_id": run_id,
                "workflow_name": resolved_workflow,
                "overridden": False,
                "override_reason": None,
            }
        if status in ("in_progress", "queued"):
            return _no_go(
                f"{resolved_workflow} draait nog op {head_sha[:12]} (status: {status}): "
                "deze merge is niet toetsbaar tot de run op 'success' eindigt",
                ci_conclusion=None,
                ran_on_sha=True,
                head_sha=head_sha,
                ci_run_id=run_id,
                workflow_name=resolved_workflow,
            )
        return _no_go(
            f"{resolved_workflow} conclusion is '{conclusion or status}' op {head_sha[:12]}: "
            "deze merge is niet toetsbaar",
            ci_conclusion=conclusion or None,
            ran_on_sha=True,
            head_sha=head_sha,
            ci_run_id=run_id,
            workflow_name=resolved_workflow,
        )

    # No run matched the HEAD SHA — distinguish "ran on a different SHA" from
    # "never ran at all" so the two failure modes get distinct messages.
    if runs:
        latest_run = runs[0]
        latest_sha = (latest_run.get("headSha") or "")[:12]
        return _no_go(
            f"{resolved_workflow} heeft niet gedraaid op HEAD {head_sha[:12]}; "
            f"laatste run op '{branch}' was op {latest_sha}: "
            "deze merge is niet toetsbaar (een run op een oudere commit telt niet)",
            head_sha=head_sha,
            workflow_name=resolved_workflow,
        )
    return _no_go(
        f"Geen VNX CI-run gevonden voor {head_sha[:12]}: deze merge is niet toetsbaar",
        head_sha=head_sha,
        workflow_name=resolved_workflow,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_preflight_ci_check",
        description="Fail-closed check: does VNX CI have a successful run for the exact HEAD SHA?",
    )
    parser.add_argument("--project-root", required=True, help="Directory whose HEAD is being merged")
    parser.add_argument("--branch", help="Branch name (defaults to git rev-parse --abbrev-ref HEAD)")
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
