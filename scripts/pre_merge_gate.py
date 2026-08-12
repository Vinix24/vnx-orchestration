#!/usr/bin/env python3
"""Pre-merge gate enforcement for VNX dispatches.

Runs heavy deterministic checks at gate time to produce a GO or HOLD verdict.
This is the pre-merge counterpart to the lightweight receipt-time verifier
(verify_claims.py). Heavy checks — pytest, AST analysis, artifact validation,
PR size — execute only in this flow, never after every receipt.

Exit codes:
  0  - Gate verdict: GO
  1  - Gate verdict: HOLD
  10 - Invalid arguments or missing data
  20 - I/O error
  40 - Unexpected internal error

Usage:
  python pre_merge_gate.py --pr PR-6
  python pre_merge_gate.py --pr PR-6 --json
  python pre_merge_gate.py --pr PR-6 --output-file gate_result.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from result_contract import EXIT_OK
from contract_parser import parse_contract_from_file
from verify_claims import verify_contract
from quality_advisory import (
    generate_quality_advisory,
    get_changed_files,
)
from cqs_calculator import calculate_cqs
from gate_findings_bridge import record_gate_finding, resolve_gate_finding

# CQS threshold: dispatches below this score get a HOLD
CQS_THRESHOLD = 50.0

# Maximum diff size (lines added+removed) before triggering a size warning
PR_SIZE_WARN = 300
PR_SIZE_HOLD = 600

# Maximum files completely deleted before triggering net-deletion warning/hold
DELETION_FILE_WARN = 5
DELETION_FILE_HOLD = 10

# Net line deletion thresholds (lines_removed - lines_added across all changed files).
NET_LINE_DELETION_WARN = 200
NET_LINE_DELETION_HOLD = 500

# Pytest timeout in seconds
PYTEST_TIMEOUT = 120

# CI workflow name queried via `gh run list --workflow`. Overridable per-repo
# via the VNX_CI_WORKFLOW_NAME env var or the --ci-workflow-name CLI flag —
# see _resolve_ci_workflow_name() for why this isn't auto-detected instead.
DEFAULT_CI_WORKFLOW_NAME = "VNX CI"
CI_WORKFLOW_NAME_ENV_VAR = "VNX_CI_WORKFLOW_NAME"

# Status for a check that could not establish an answer at all — distinct
# from GO (verified passing) and HOLD (verified failing/missing). A gate
# consumer must never treat this as permission to merge (OI-1140).
SKIPPED_UNVERIFIED = "SKIPPED_UNVERIFIED"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Individual gate checks
# ---------------------------------------------------------------------------

def check_open_items(pr_id: str, state_dir: Path) -> Dict[str, Any]:
    """Check open items for unresolved blockers targeting this PR."""
    oi_file = state_dir / "open_items.json"
    if not oi_file.exists():
        return {
            "check": "open_items",
            "status": "GO",
            "detail": "no open items file found",
            "blockers": 0,
            "warnings": 0,
        }

    try:
        data = json.loads(oi_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # The file exists but couldn't be read/parsed — we don't know whether
        # it holds a blocker. Unlike the no-file-at-all case above, this is
        # a failed verification attempt, not "nothing to check" (OI-1140).
        return {
            "check": "open_items",
            "status": SKIPPED_UNVERIFIED,
            "detail": f"open_items.json exists but could not be read: {exc}",
            "blockers": 0,
            "warnings": 0,
        }

    items = data.get("items", [])
    blockers = []
    warnings = []

    for item in items:
        if item.get("status") not in ("open",):
            continue
        item_pr = item.get("pr_id", "")
        if item_pr and item_pr != pr_id:
            continue
        severity = item.get("severity", "info")
        if severity == "blocker":
            blockers.append(item.get("title", item.get("id", "unknown")))
        elif severity == "warn":
            warnings.append(item.get("title", item.get("id", "unknown")))

    status = "HOLD" if blockers else "GO"
    return {
        "check": "open_items",
        "status": status,
        "detail": f"{len(blockers)} blocker(s), {len(warnings)} warning(s)",
        "blockers": len(blockers),
        "blocker_titles": blockers,
        "warnings": len(warnings),
        "warning_titles": warnings,
    }


def check_cqs(pr_id: str, state_dir: Path) -> Dict[str, Any]:
    """Check CQS from the latest receipt for this PR."""
    receipts_file = state_dir / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return {
            "check": "cqs_threshold",
            "status": "GO",
            "detail": "no receipts found — skipping CQS check",
            "cqs": None,
        }

    latest_receipt = None
    read_error = None
    try:
        for line in receipts_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue
            receipt_pr = receipt.get("pr_id") or receipt.get("pr") or ""
            if receipt_pr == pr_id:
                latest_receipt = receipt
    except OSError as exc:
        read_error = exc

    if read_error is not None:
        # The file exists but reading it failed outright — distinct from
        # "read fine, no receipt for this PR yet" below. We genuinely don't
        # know this PR's CQS, so this must not read as GO (OI-1140).
        return {
            "check": "cqs_threshold",
            "status": SKIPPED_UNVERIFIED,
            "detail": f"t0_receipts.ndjson exists but could not be read: {read_error}",
            "cqs": None,
        }

    if latest_receipt is None:
        return {
            "check": "cqs_threshold",
            "status": "GO",
            "detail": f"no receipts found for {pr_id}",
            "cqs": None,
        }

    cqs_result = calculate_cqs(latest_receipt, session=None)
    cqs_value = cqs_result.get("cqs")

    if cqs_value is None:
        return {
            "check": "cqs_threshold",
            "status": "GO",
            "detail": f"CQS excluded (status={cqs_result.get('normalized_status')})",
            "cqs": None,
        }

    status = "HOLD" if cqs_value < CQS_THRESHOLD else "GO"
    return {
        "check": "cqs_threshold",
        "status": status,
        "detail": f"CQS={cqs_value:.1f} (threshold={CQS_THRESHOLD})",
        "cqs": cqs_value,
        "threshold": CQS_THRESHOLD,
    }


def check_git_cleanliness(project_root: Path) -> Dict[str, Any]:
    """Check for uncommitted changes and merge conflicts."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        dirty_files = [
            line for line in result.stdout.strip().splitlines()
            if line.strip()
        ]

        conflict_result = subprocess.run(
            ["git", "diff", "--check"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        has_conflicts = conflict_result.returncode != 0 and "conflict" in conflict_result.stdout.lower()

    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {
            "check": "git_cleanliness",
            "status": "HOLD",
            "detail": f"git check failed: {exc}",
        }

    if has_conflicts:
        return {
            "check": "git_cleanliness",
            "status": "HOLD",
            "detail": "merge conflict markers detected",
            "dirty_files": len(dirty_files),
            "has_conflicts": True,
        }

    status = "GO"
    detail = "working tree clean"
    if dirty_files:
        detail = f"{len(dirty_files)} uncommitted file(s) — not blocking but noted"

    return {
        "check": "git_cleanliness",
        "status": status,
        "detail": detail,
        "dirty_files": len(dirty_files),
        "has_conflicts": False,
    }


def check_contract_verification(
    pr_id: str, dispatch_dir: Path, project_root: Path, state_dir: Path
) -> Dict[str, Any]:
    """Run contract verification for the latest dispatch of this PR."""
    dispatch_file = _find_dispatch_for_pr(pr_id, dispatch_dir)
    if dispatch_file is None:
        return {
            "check": "contract_verification",
            "status": "GO",
            "detail": f"no dispatch file found for {pr_id} — skipping contract check",
            "verdict": "no_dispatch",
        }

    try:
        contract = parse_contract_from_file(dispatch_file)
    except OSError as exc:
        return {
            "check": "contract_verification",
            "status": "HOLD",
            "detail": f"failed to read dispatch: {exc}",
            "verdict": "error",
        }

    if not contract.has_claims:
        return {
            "check": "contract_verification",
            "status": "GO",
            "detail": "no contract block in dispatch — Phase 2a skip",
            "verdict": "no_contract",
        }

    verification = verify_contract(contract, project_root)
    verdict = verification.get("verdict", "fail")
    status = "GO" if verdict == "pass" else "HOLD"

    return {
        "check": "contract_verification",
        "status": status,
        "detail": f"contract {verdict}: {verification.get('passed', 0)}/{verification.get('total_claims', 0)} claims passed",
        "verdict": verdict,
        "passed": verification.get("passed", 0),
        "failed": verification.get("failed", 0),
        "total_claims": verification.get("total_claims", 0),
        "results": verification.get("results", []),
    }


def check_pytest(project_root: Path) -> Dict[str, Any]:
    """Run pytest and report results."""
    tests_dir = project_root / "tests"
    if not tests_dir.is_dir():
        return {
            "check": "pytest",
            "status": "GO",
            "detail": "no tests/ directory found",
            "tests_found": False,
        }

    test_files = list(tests_dir.glob("test_*.py"))
    if not test_files:
        return {
            "check": "pytest",
            "status": "GO",
            "detail": "no test files found in tests/",
            "tests_found": False,
        }

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(tests_dir),
                "--tb=short",
                "-q",
                "--no-header",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT,
        )

        passed = result.returncode == 0
        output_lines = result.stdout.strip().splitlines()
        summary_line = output_lines[-1] if output_lines else ""

        return {
            "check": "pytest",
            "status": "GO" if passed else "HOLD",
            "detail": summary_line if summary_line else ("all tests passed" if passed else "tests failed"),
            "exit_code": result.returncode,
            "tests_found": True,
            "summary": summary_line,
        }
    except subprocess.TimeoutExpired:
        return {
            "check": "pytest",
            "status": "HOLD",
            "detail": f"pytest timed out after {PYTEST_TIMEOUT}s",
            "tests_found": True,
        }
    except FileNotFoundError:
        return {
            "check": "pytest",
            "status": SKIPPED_UNVERIFIED,
            "detail": "pytest not available — test results could not be verified",
            "tests_found": True,
        }


def check_quality_advisory(project_root: Path) -> Dict[str, Any]:
    """Run AST/quality checks on changed files."""
    changed_files = get_changed_files(project_root)

    if not changed_files:
        return {
            "check": "quality_advisory",
            "status": "GO",
            "detail": "no changed files to check",
            "blocking_count": 0,
            "warning_count": 0,
            "risk_score": 0,
        }

    advisory = generate_quality_advisory(changed_files, project_root)
    summary = advisory.summary
    blocking = summary.get("blocking_count", 0)
    warnings = summary.get("warning_count", 0)
    risk_score = summary.get("risk_score", 0)
    decision = advisory.t0_recommendation.get("decision", "approve")

    status = "HOLD" if decision == "hold" else "GO"

    return {
        "check": "quality_advisory",
        "status": status,
        "detail": f"risk_score={risk_score}, {blocking} blocking, {warnings} warning(s), decision={decision}",
        "blocking_count": blocking,
        "warning_count": warnings,
        "risk_score": risk_score,
        "decision": decision,
        "checks": advisory.checks,
    }


_PR_SIZE_BASE_CANDIDATES = ("origin/main", "origin/master")


def _resolve_merge_base(project_root: Path, base_ref: str, head_ref: str) -> Optional[str]:
    """Return the merge-base commit of base_ref and head_ref, or None if base_ref
    is not resolvable locally (not fetched, unknown ref, etc.)."""
    try:
        result = subprocess.run(
            ["git", "merge-base", base_ref, head_ref],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _diff_numstat_totals(project_root: Path, base_ref: str, head_ref: str) -> Optional[tuple]:
    """Return (lines_added, lines_removed) for base_ref..head_ref, or None on git failure."""
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", base_ref, head_ref],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    total_added = 0
    total_removed = 0
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                total_added += int(parts[0])
                total_removed += int(parts[1])
            except ValueError:
                pass  # binary files show "-"
    return total_added, total_removed


def check_pr_size(
    project_root: Path,
    *,
    base_ref: Optional[str] = None,
    head_ref: str = "HEAD",
) -> Dict[str, Any]:
    """Check PR diff size (lines added + removed) against the merge-base with the base branch.

    Measures merge_base(base_branch, head_ref)..head_ref — the range the PR actually
    represents — never the working tree and never an unrelated commit that landed on
    the base branch after this branch diverged (e.g. FEATURE_PLAN.md's background
    regen noise). ``base_ref`` defaults to trying ``origin/main`` then
    ``origin/master``; pass it explicitly to pin a specific base (e.g. in tests).

    Fails loudly (status=HOLD, no line counts) when the merge-base cannot be
    resolved, instead of silently falling back to a diff that produces a wrong
    number.
    """
    candidates = (base_ref,) if base_ref else _PR_SIZE_BASE_CANDIDATES

    resolved_base_ref = None
    merge_base = None
    for candidate in candidates:
        merge_base = _resolve_merge_base(project_root, candidate, head_ref)
        if merge_base is not None:
            resolved_base_ref = candidate
            break

    if merge_base is None:
        return {
            "check": "pr_size",
            "status": "HOLD",
            "detail": (
                f"could not resolve a merge-base for {head_ref!r}: none of "
                f"{list(candidates)!r} are available locally — fetch the base "
                "branch before running this gate"
            ),
            "lines_added": None,
            "lines_removed": None,
            "lines_changed": None,
        }

    totals = _diff_numstat_totals(project_root, merge_base, head_ref)
    if totals is None:
        return {
            "check": "pr_size",
            "status": "HOLD",
            "detail": f"git diff --numstat {merge_base[:12]}..{head_ref} failed",
            "lines_added": None,
            "lines_removed": None,
            "lines_changed": None,
        }

    total_added, total_removed = totals
    total = total_added + total_removed

    if total > PR_SIZE_HOLD:
        status = "HOLD"
        detail = f"{total} lines changed vs {resolved_base_ref} (>{PR_SIZE_HOLD} hold threshold)"
    elif total > PR_SIZE_WARN:
        status = "GO"
        detail = f"{total} lines changed vs {resolved_base_ref} (>{PR_SIZE_WARN} — large but not blocking)"
    else:
        status = "GO"
        detail = f"{total} lines changed vs {resolved_base_ref}"

    return {
        "check": "pr_size",
        "status": status,
        "detail": detail,
        "lines_added": total_added,
        "lines_removed": total_removed,
        "lines_changed": total,
    }


def check_artifacts(
    pr_id: str, dispatch_dir: Path, project_root: Path
) -> Dict[str, Any]:
    """Validate artifact claims (PDF/XLSX) from contract if present."""
    dispatch_file = _find_dispatch_for_pr(pr_id, dispatch_dir)
    if dispatch_file is None:
        return {
            "check": "artifact_verification",
            "status": "GO",
            "detail": "no dispatch file — skipping artifact check",
            "artifacts_checked": 0,
        }

    try:
        contract = parse_contract_from_file(dispatch_file)
    except OSError as exc:
        # The dispatch file was found by _find_dispatch_for_pr but couldn't
        # be read here — we don't know whether it declared artifact claims.
        # Same failure mode check_contract_verification treats as blocking;
        # this handler must not diverge into a silent GO (OI-1140).
        return {
            "check": "artifact_verification",
            "status": SKIPPED_UNVERIFIED,
            "detail": f"dispatch file found but could not be read: {exc}",
            "artifacts_checked": 0,
        }

    if not contract.has_claims:
        return {
            "check": "artifact_verification",
            "status": "GO",
            "detail": "no contract — no artifacts to verify",
            "artifacts_checked": 0,
        }

    artifact_claims = [
        c for c in contract.claims
        if c.claim_type == "file_exists" and c.path and _is_artifact_path(c.path)
    ]

    if not artifact_claims:
        return {
            "check": "artifact_verification",
            "status": "GO",
            "detail": "no artifact claims in contract",
            "artifacts_checked": 0,
        }

    results = []
    failed = 0
    for claim in artifact_claims:
        target = Path(claim.path)
        if not target.is_absolute():
            target = project_root / claim.path
        exists = target.exists()
        valid = exists and target.stat().st_size > 0 if exists else False
        if not valid:
            failed += 1
        results.append({
            "path": claim.path,
            "exists": exists,
            "valid": valid,
            "size": target.stat().st_size if exists else 0,
        })

    status = "HOLD" if failed > 0 else "GO"
    return {
        "check": "artifact_verification",
        "status": status,
        "detail": f"{len(artifact_claims)} artifact(s) checked, {failed} failed",
        "artifacts_checked": len(artifact_claims),
        "artifacts_failed": failed,
        "results": results,
    }


def check_shell_syntax(project_root: Path) -> Dict[str, Any]:
    """Run bash -n on changed shell files."""
    changed = get_changed_files(project_root)
    shell_files = [f for f in changed if f.suffix == ".sh" or f.name.endswith(".bash")]

    if not shell_files:
        return {
            "check": "shell_syntax",
            "status": "GO",
            "detail": "no shell files changed",
            "files_checked": 0,
        }

    failures = []
    unverified = []
    for sf in shell_files:
        try:
            result = subprocess.run(
                ["bash", "-n", str(sf)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                failures.append({
                    "file": str(sf),
                    "error": result.stderr.strip(),
                })
        except subprocess.TimeoutExpired:
            # bash -n never completed for this file — its syntax was never
            # actually checked. Folding this into "checked, 0 failures" would
            # silently pass a file we never looked at (OI-1140).
            unverified.append({"file": str(sf), "reason": "bash -n timed out"})
        except FileNotFoundError:
            unverified.append({"file": str(sf), "reason": "bash binary not available"})

    if failures:
        status = "HOLD"
    elif unverified:
        status = SKIPPED_UNVERIFIED
    else:
        status = "GO"

    detail = f"{len(shell_files)} file(s) checked, {len(failures)} failure(s)"
    if unverified:
        detail += f", {len(unverified)} could not be verified"

    return {
        "check": "shell_syntax",
        "status": status,
        "detail": detail,
        "files_checked": len(shell_files),
        "files_unverified": len(unverified),
        "failures": failures,
        "unverified": unverified,
    }


def check_net_deletion(project_root: Path) -> Dict[str, Any]:
    """Check for mass file deletion and large net line deletion in the PR diff.

    Two independent sub-checks run in parallel:
    - File deletion count (lines_removed > lines_added across deleted files)
    - Net line deletion (total_removed - total_added across all changed files)

    Either sub-check reaching its HOLD threshold produces a HOLD verdict.
    WARN-level findings are advisory and do not block merge. If a sub-check's
    underlying git query fails outright, that sub-check cannot be evaluated
    and is SKIPPED_UNVERIFIED — a merge gate that can't see the diff must
    not assume it's small (OI-1140). The two sub-checks are independent, so
    one failing does not skip evaluation of the other.
    """
    deleted = _get_deleted_files(project_root)
    net_line = _get_net_line_deletion(project_root)

    # Determine file-deletion verdict
    if deleted is None:
        file_status = SKIPPED_UNVERIFIED
        file_detail = "could not compute deleted files"
        deleted_count = None
    else:
        deleted_count = len(deleted)
        if deleted_count >= DELETION_FILE_HOLD:
            file_status = "HOLD"
            file_detail = f"{deleted_count} file(s) deleted (>={DELETION_FILE_HOLD} — mass deletion requires review)"
        elif deleted_count >= DELETION_FILE_WARN:
            file_status = "WARN"
            file_detail = f"{deleted_count} file(s) deleted (>={DELETION_FILE_WARN} — review deletions before merge)"
        else:
            file_status = "GO"
            file_detail = f"{deleted_count} file(s) deleted"

    # Determine net-line-deletion verdict
    if net_line is None:
        line_status = SKIPPED_UNVERIFIED
        line_detail = "net line deletion: could not be computed"
    elif net_line >= NET_LINE_DELETION_HOLD:
        line_status = "HOLD"
        line_detail = f"net line deletion: {net_line} lines removed (>={NET_LINE_DELETION_HOLD} — requires review)"
    elif net_line >= NET_LINE_DELETION_WARN:
        line_status = "WARN"
        line_detail = f"net line deletion: {net_line} lines removed (>={NET_LINE_DELETION_WARN} — review scope reduction)"
    else:
        line_status = "GO"
        line_detail = f"net line deletion: {net_line} lines removed"

    # Merge: HOLD wins over SKIPPED_UNVERIFIED wins over WARN wins over GO
    sub_statuses = {file_status, line_status}
    if "HOLD" in sub_statuses:
        status = "HOLD"
    elif SKIPPED_UNVERIFIED in sub_statuses:
        status = SKIPPED_UNVERIFIED
    else:
        status = "GO"

    details = [file_detail, line_detail]
    return {
        "check": "net_deletion",
        "status": status,
        "detail": "; ".join(details),
        "deleted_count": deleted_count,
        "deleted_files": deleted if deleted is not None else [],
        "net_line_deletion": net_line,
        "net_line_deletion_warn": line_status == "WARN",
        "file_deletion_warn": file_status == "WARN",
    }


def _resolve_ci_workflow_name(workflow_name: Optional[str]) -> str:
    """Resolve the workflow name to query via ``gh run list --workflow``.

    Resolution order: explicit ``workflow_name`` argument (CLI
    ``--ci-workflow-name`` or a programmatic caller) > ``VNX_CI_WORKFLOW_NAME``
    env var (per-repo operator override) > this fabric's own default,
    "VNX CI".

    Auto-detecting "the" CI workflow from ``.github/workflows/*.yml`` was
    considered and rejected: this repo alone ships seven workflow files
    (VNX CI, VNX Public CI, Burn-In Headless CI, Attestation Gate, Anchor
    Immutability Check, ADR-003 Enforcement, Subsystems Ledger Drift Check)
    and picking one via a naming heuristic would just trade one silent wrong
    answer (hardcoded name) for another (guessed name) — a confident
    misdetection is exactly as dangerous as the bug this fixes. An explicit,
    per-repo override is unambiguous and matches the operator's own claim.
    """
    if workflow_name:
        return workflow_name
    env_name = os.environ.get(CI_WORKFLOW_NAME_ENV_VAR)
    if env_name:
        return env_name
    return DEFAULT_CI_WORKFLOW_NAME


def check_ci_workflow(
    project_root: Path,
    *,
    branch: Optional[str] = None,
    head_sha: Optional[str] = None,
    workflow_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Check that the configured CI workflow ran successfully on this exact commit.

    Three *verified* states are distinguished sharply:
      - Never ran:   no CI run for this commit → HOLD, with explicit
                     "workflow not run" message independent of failure.
      - Ran + failed: run exists but conclusion is not "success" → HOLD.
      - Ran + passed: run exists with conclusion "success" → GO.

    A fourth state — unverifiable — covers every case where this check
    cannot establish which of the three above applies: the gh CLI is
    unavailable, ``gh run list`` fails (including "no such workflow" when
    the configured name doesn't match any workflow in the repo), its output
    is unparseable, or the branch/HEAD SHA cannot be resolved from git. That
    state is SKIPPED_UNVERIFIED, never GO (OI-1140) — a merge gate that
    cannot see CI must not grant permission to merge just because it failed
    to look. run_gate_checks treats SKIPPED_UNVERIFIED as blocking, same as
    HOLD, while keeping the status name distinct so a caller can always tell
    "verified green" apart from "could not verify".

    The workflow name is resolved via _resolve_ci_workflow_name() — never
    hardcoded past that point, so a consumer repo whose CI workflow has a
    different name gets a real answer instead of a silent pass.

    OI-931: ``gh pr checks`` can show all-green while the mandatory CI
    workflow never ran.  This check reads the workflow conclusion directly —
    never the check-names — so the three verified states are distinguishable.
    """
    resolved_workflow_name = _resolve_ci_workflow_name(workflow_name)

    # ── Resolve head SHA ──────────────────────────────────────────────────
    if head_sha is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(project_root),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {
                    "check": "ci_workflow",
                    "status": SKIPPED_UNVERIFIED,
                    "detail": "could not resolve HEAD SHA — CI workflow could not be verified",
                    "ci_conclusion": None,
                    "ci_ran_on_sha": False,
                }
            head_sha = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {
                "check": "ci_workflow",
                "status": SKIPPED_UNVERIFIED,
                "detail": "could not resolve HEAD SHA — CI workflow could not be verified",
                "ci_conclusion": None,
                "ci_ran_on_sha": False,
            }

    if branch is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(project_root),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {
                    "check": "ci_workflow",
                    "status": SKIPPED_UNVERIFIED,
                    "detail": "could not resolve branch name — CI workflow could not be verified",
                    "ci_conclusion": None,
                    "ci_ran_on_sha": False,
                }
            branch = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {
                "check": "ci_workflow",
                "status": SKIPPED_UNVERIFIED,
                "detail": "could not resolve branch name — CI workflow could not be verified",
                "ci_conclusion": None,
                "ci_ran_on_sha": False,
            }

    # ── Query gh for workflow runs ─────────────────────────────────────────
    try:
        result = subprocess.run(
            [
                "gh", "run", "list",
                "--branch", branch,
                "--workflow", resolved_workflow_name,
                "--limit", "10",
                "--json", "conclusion,headSha,status,databaseId",
            ],
            cwd=str(project_root),
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {
            "check": "ci_workflow",
            "status": SKIPPED_UNVERIFIED,
            "detail": "gh CLI not available — CI workflow could not be verified",
            "ci_conclusion": None,
            "ci_ran_on_sha": False,
        }

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return {
            "check": "ci_workflow",
            "status": SKIPPED_UNVERIFIED,
            "detail": (
                f"gh run list failed for workflow '{resolved_workflow_name}' — "
                f"CI workflow could not be verified: {stderr[:120]}"
            ),
            "ci_conclusion": None,
            "ci_ran_on_sha": False,
        }

    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "check": "ci_workflow",
            "status": SKIPPED_UNVERIFIED,
            "detail": "gh output unparseable — CI workflow could not be verified",
            "ci_conclusion": None,
            "ci_ran_on_sha": False,
        }

    # ── Match run to HEAD SHA ──────────────────────────────────────────────
    for run in runs:
        if run.get("headSha") == head_sha:
            conclusion = run.get("conclusion") or ""
            if conclusion == "success":
                return {
                    "check": "ci_workflow",
                    "status": "GO",
                    "detail": (
                        f"{resolved_workflow_name} succeeded on {head_sha[:12]} "
                        f"(run {run.get('databaseId')})"
                    ),
                    "ci_conclusion": conclusion,
                    "ci_ran_on_sha": True,
                    "ci_head_sha": head_sha,
                    "ci_run_id": run.get("databaseId"),
                }
            return {
                "check": "ci_workflow",
                "status": "HOLD",
                "detail": (
                    f"{resolved_workflow_name} conclusion is '{conclusion}' on {head_sha[:12]} "
                    f"(run {run.get('databaseId')}) — must be 'success'"
                ),
                "ci_conclusion": conclusion,
                "ci_ran_on_sha": True,
                "ci_head_sha": head_sha,
                "ci_run_id": run.get("databaseId"),
            }

    # No run matched the HEAD SHA — distinguish "ran on different SHA" from
    # "never ran at all" so the two failure modes get distinct messages.
    if runs:
        latest_run = runs[0]
        latest_sha = (latest_run.get("headSha") or "")[:12]
        latest_conclusion = latest_run.get("conclusion") or "unknown"
        return {
            "check": "ci_workflow",
            "status": "HOLD",
            "detail": (
                f"{resolved_workflow_name} has NOT run on HEAD ({head_sha[:12]}). "
                f"Latest run on branch '{branch}' was on {latest_sha} "
                f"(conclusion: {latest_conclusion}). "
                "The PR may show green checks from a prior run — re-run CI."
            ),
            "ci_conclusion": None,
            "ci_ran_on_sha": False,
            "ci_head_sha": head_sha,
            "ci_latest_run_sha": latest_sha,
        }
    return {
        "check": "ci_workflow",
        "status": "HOLD",
        "detail": (
            f"{resolved_workflow_name} has NEVER run on branch '{branch}'. "
            f"No runs found for HEAD {head_sha[:12]}. "
            "PR checks may show green from other workflows — "
            "this is the 'green-lie' that OI-931 guards against."
        ),
        "ci_conclusion": None,
        "ci_ran_on_sha": False,
        "ci_head_sha": head_sha,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_dispatch_for_pr(pr_id: str, dispatch_dir: Path) -> Optional[Path]:
    """Find the most recent dispatch file for a PR."""
    candidates = []
    for subdir in ("active", "completed", "staging", "pending"):
        d = dispatch_dir / subdir
        if not d.is_dir():
            continue
        for md_file in d.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                if f"PR: {pr_id}" in content or f"**PR**: {pr_id}" in content or f"PR-ID: {pr_id}" in content:
                    candidates.append(md_file)
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _resolve_dispatch_id_for_pr(pr_id: str, dispatch_dir: Path) -> Optional[str]:
    """Best-effort dispatch_id for a PR, via its dispatch file's 'Dispatch-ID:' footer.

    Reuses the same dispatch-file lookup as the contract/artifact checks. Returns None
    (never raises) when no dispatch file is found or it carries no Dispatch-ID footer —
    the fabric-linking bridge treats that as an unlinked gate (quiet no-op).
    """
    dispatch_file = _find_dispatch_for_pr(pr_id, dispatch_dir)
    if dispatch_file is None:
        return None
    try:
        dispatch_id = parse_contract_from_file(dispatch_file).dispatch_id
    except OSError:
        return None
    return dispatch_id or None


def _is_artifact_path(path: str) -> bool:
    """Check if a path refers to a PDF or XLSX artifact."""
    lower = path.lower()
    return lower.endswith(".pdf") or lower.endswith(".xlsx") or lower.endswith(".xls")


def _get_deleted_files(project_root: Path) -> Optional[List[str]]:
    """Return list of files deleted in current PR branch vs base. None on failure."""
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--diff-filter=D", "--name-only", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [f for f in result.stdout.strip().splitlines() if f.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--diff-filter=D", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def _parse_numstat_net(numstat_output: str) -> int:
    """Parse git diff --numstat output, return net line deletion (removed - added).

    Binary files report '-' for both columns and are skipped.
    """
    total_added = 0
    total_removed = 0
    for line in numstat_output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] != "-" and parts[1] != "-":
            try:
                total_added += int(parts[0])
                total_removed += int(parts[1])
            except ValueError:
                pass
    return total_removed - total_added


def _get_net_line_deletion(project_root: Path) -> Optional[int]:
    """Return net lines deleted (removed - added) for current PR vs origin/main.

    Returns None on git failure.
    """
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return _parse_numstat_net(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return _parse_numstat_net(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


# ---------------------------------------------------------------------------
# Gate orchestrator
# ---------------------------------------------------------------------------

def _sync_fabric_finding(
    pr_id: str,
    dispatch_dir: Path,
    state_dir: Path,
    verdict: str,
    hold_checks: List[Dict[str, Any]],
) -> None:
    """Best-effort: mirror the overall gate verdict onto the dispatch's track.

    HOLD records a single `blocks` open-item (gate_name="pre_merge_gate") carrying every
    HOLD check's detail — this also covers quality_advisory's severity="blocking" path,
    since a quality-advisory HOLD already surfaces as one of ``hold_checks``. GO resolves
    a previously-recorded finding (a fixed-and-repushed PR going green). Never raises and
    never changes ``verdict`` — a finding-emit failure is observability only, logged and
    swallowed by record_gate_finding/resolve_gate_finding themselves.
    """
    dispatch_id = _resolve_dispatch_id_for_pr(pr_id, dispatch_dir)
    if not dispatch_id:
        return
    try:
        if verdict == "HOLD":
            summary = "; ".join(f"{c['check']}: {c.get('detail', '')}" for c in hold_checks)
            record_gate_finding(
                state_dir, dispatch_id=dispatch_id, gate_name="pre_merge_gate",
                summary=summary, pr_ref=pr_id,
            )
        else:
            resolve_gate_finding(
                state_dir, dispatch_id=dispatch_id, gate_name="pre_merge_gate",
                reason="pre_merge_gate clean run",
            )
    except Exception as exc:  # noqa: BLE001 — defense in depth: the bridge is already
        # non-raising by design, but the gate's verdict must survive even a regression
        # in that contract.
        _LOG.warning(
            "pre_merge_gate: fabric finding sync failed dispatch=%s: %s", dispatch_id, exc,
        )


def run_gate_checks(
    pr_id: str,
    project_root: Path,
    state_dir: Path,
    dispatch_dir: Path,
    skip_pytest: bool = False,
    ci_workflow_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Run all gate checks and produce a merged verdict.

    Returns a structured result with per-check status and overall verdict.

    A check that returns SKIPPED_UNVERIFIED (couldn't establish an answer —
    see check_ci_workflow, check_pytest) blocks the verdict exactly like
    HOLD does: this gate is the last one before a merge, and "couldn't
    check" must never read as "checked and it's fine" (OI-1140). The two are
    kept distinguishable in the result via ``skipped_unverified_count`` and
    per-check ``status``, even though both drive the same HOLD verdict.
    """
    checks: List[Dict[str, Any]] = []

    checks.append(check_open_items(pr_id, state_dir))
    checks.append(check_cqs(pr_id, state_dir))
    checks.append(check_git_cleanliness(project_root))
    checks.append(check_contract_verification(pr_id, dispatch_dir, project_root, state_dir))
    checks.append(check_quality_advisory(project_root))
    checks.append(check_pr_size(project_root))
    checks.append(check_artifacts(pr_id, dispatch_dir, project_root))
    checks.append(check_shell_syntax(project_root))
    checks.append(check_net_deletion(project_root))
    checks.append(check_ci_workflow(project_root, workflow_name=ci_workflow_name))

    if not skip_pytest:
        checks.append(check_pytest(project_root))

    hold_checks = [c for c in checks if c.get("status") == "HOLD"]
    unverified_checks = [c for c in checks if c.get("status") == SKIPPED_UNVERIFIED]
    blocking_checks = hold_checks + unverified_checks
    verdict = "HOLD" if blocking_checks else "GO"

    _sync_fabric_finding(pr_id, dispatch_dir, state_dir, verdict, blocking_checks)

    return {
        "pr_id": pr_id,
        "verdict": verdict,
        "checked_at": _utc_now_iso(),
        "total_checks": len(checks),
        "go_count": len([c for c in checks if c.get("status") == "GO"]),
        "hold_count": len(blocking_checks),
        "skipped_unverified_count": len(unverified_checks),
        "checks": checks,
        "hold_reasons": [
            {"check": c["check"], "detail": c.get("detail", "")}
            for c in blocking_checks
        ],
    }


def store_gate_result(result: Dict[str, Any], state_dir: Path) -> Path:
    """Store gate check result to state directory."""
    gate_dir = state_dir / "gate_results"
    gate_dir.mkdir(parents=True, exist_ok=True)

    pr_id = result.get("pr_id", "unknown")
    timestamp = result.get("checked_at", _utc_now_iso()).replace(":", "").replace("-", "")
    filename = f"{pr_id}_{timestamp}.json"
    output_path = gate_dir / filename

    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def format_human_readable(result: Dict[str, Any]) -> str:
    """Format gate result for terminal display."""
    lines = []
    verdict = result.get("verdict", "UNKNOWN")
    pr_id = result.get("pr_id", "?")

    verdict_icon = "✅" if verdict == "GO" else "🚫"
    lines.append(f"\n{verdict_icon}  Gate verdict for {pr_id}: {verdict}")
    lines.append(f"   Checked at: {result.get('checked_at', '?')}")
    lines.append(
        f"   Checks: {result.get('go_count', 0)} GO, {result.get('hold_count', 0)} HOLD"
        f" ({result.get('skipped_unverified_count', 0)} unverified)\n"
    )

    for check in result.get("checks", []):
        status = check.get("status")
        icon = "✓" if status == "GO" else ("?" if status == SKIPPED_UNVERIFIED else "✗")
        lines.append(f"  [{icon}] {check.get('check', '?'):.<30s} {status or '?':>4s}  {check.get('detail', '')}")

    if result.get("hold_reasons"):
        lines.append("\n  HOLD reasons (includes unverified — see status column):")
        for hr in result["hold_reasons"]:
            lines.append(f"    - {hr['check']}: {hr['detail']}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-merge gate enforcement for VNX PRs"
    )
    parser.add_argument(
        "--pr",
        required=True,
        help="PR identifier (e.g. PR-6)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root (default: auto-detect)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output JSON only",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Write results to file",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        default=False,
        help="Skip pytest execution (useful for CI or fast checks)",
    )
    parser.add_argument(
        "--store",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Store results in state directory (default: true)",
    )
    parser.add_argument(
        "--ci-workflow-name",
        default=None,
        help=(
            "Name of the CI workflow to query via `gh run list --workflow`. "
            f"Default: {CI_WORKFLOW_NAME_ENV_VAR} env var, else "
            f"'{DEFAULT_CI_WORKFLOW_NAME}'. Set this for consumer repos whose "
            "CI workflow has a different name."
        ),
    )

    args = parser.parse_args()

    paths = ensure_env()
    project_root = args.project_root or Path(paths["PROJECT_ROOT"])
    state_dir = Path(paths["VNX_STATE_DIR"])
    dispatch_dir = Path(paths["VNX_DISPATCH_DIR"])

    result = run_gate_checks(
        pr_id=args.pr,
        project_root=project_root,
        state_dir=state_dir,
        dispatch_dir=dispatch_dir,
        skip_pytest=args.skip_pytest,
        ci_workflow_name=args.ci_workflow_name,
    )

    if args.store:
        stored_path = store_gate_result(result, state_dir)
        result["stored_at"] = str(stored_path)

    output_json = json.dumps(result, indent=2)

    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(output_json + "\n", encoding="utf-8")

    if args.json:
        print(output_json)
    else:
        print(format_human_readable(result))

    return EXIT_OK if result["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
