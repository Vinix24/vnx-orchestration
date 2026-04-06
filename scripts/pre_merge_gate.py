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
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# CQS threshold: dispatches below this score get a HOLD
CQS_THRESHOLD = 50.0

# Maximum diff size (lines added+removed) before triggering a size warning
PR_SIZE_WARN = 300
PR_SIZE_HOLD = 600

# Pytest timeout in seconds
PYTEST_TIMEOUT = 120


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
        return {
            "check": "open_items",
            "status": "GO",
            "detail": f"could not read open items: {exc}",
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
    except OSError:
        pass

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
            "status": "GO",
            "detail": "pytest not available — skipping",
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


def check_pr_size(project_root: Path) -> Dict[str, Any]:
    """Check PR diff size (lines added + removed)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {
                "check": "pr_size",
                "status": "GO",
                "detail": "could not compute diff stat",
                "lines_changed": None,
            }

        # Parse numstat for precise line counts
        numstat = subprocess.run(
            ["git", "diff", "--numstat", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        total_added = 0
        total_removed = 0
        for line in numstat.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total_added += int(parts[0])
                    total_removed += int(parts[1])
                except ValueError:
                    pass  # binary files show "-"

        total = total_added + total_removed

        if total > PR_SIZE_HOLD:
            status = "HOLD"
            detail = f"{total} lines changed (>{PR_SIZE_HOLD} hold threshold)"
        elif total > PR_SIZE_WARN:
            status = "GO"
            detail = f"{total} lines changed (>{PR_SIZE_WARN} — large but not blocking)"
        else:
            status = "GO"
            detail = f"{total} lines changed"

        return {
            "check": "pr_size",
            "status": status,
            "detail": detail,
            "lines_added": total_added,
            "lines_removed": total_removed,
            "lines_changed": total,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {
            "check": "pr_size",
            "status": "GO",
            "detail": f"diff stat failed: {exc}",
            "lines_changed": None,
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
    except OSError:
        return {
            "check": "artifact_verification",
            "status": "GO",
            "detail": "could not read dispatch",
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
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    status = "HOLD" if failures else "GO"
    return {
        "check": "shell_syntax",
        "status": status,
        "detail": f"{len(shell_files)} file(s) checked, {len(failures)} failure(s)",
        "files_checked": len(shell_files),
        "failures": failures,
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


def _is_artifact_path(path: str) -> bool:
    """Check if a path refers to a PDF or XLSX artifact."""
    lower = path.lower()
    return lower.endswith(".pdf") or lower.endswith(".xlsx") or lower.endswith(".xls")


# ---------------------------------------------------------------------------
# Gate orchestrator
# ---------------------------------------------------------------------------

def run_gate_checks(
    pr_id: str,
    project_root: Path,
    state_dir: Path,
    dispatch_dir: Path,
    skip_pytest: bool = False,
) -> Dict[str, Any]:
    """Run all gate checks and produce a merged verdict.

    Returns a structured result with per-check status and overall verdict.
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

    if not skip_pytest:
        checks.append(check_pytest(project_root))

    hold_checks = [c for c in checks if c.get("status") == "HOLD"]
    verdict = "HOLD" if hold_checks else "GO"

    return {
        "pr_id": pr_id,
        "verdict": verdict,
        "checked_at": _utc_now_iso(),
        "total_checks": len(checks),
        "go_count": len([c for c in checks if c.get("status") == "GO"]),
        "hold_count": len(hold_checks),
        "checks": checks,
        "hold_reasons": [
            {"check": c["check"], "detail": c.get("detail", "")}
            for c in hold_checks
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
    lines.append(f"   Checks: {result.get('go_count', 0)} GO, {result.get('hold_count', 0)} HOLD\n")

    for check in result.get("checks", []):
        icon = "✓" if check.get("status") == "GO" else "✗"
        lines.append(f"  [{icon}] {check.get('check', '?'):.<30s} {check.get('status', '?'):>4s}  {check.get('detail', '')}")

    if result.get("hold_reasons"):
        lines.append("\n  HOLD reasons:")
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
