#!/usr/bin/env python3
"""Lightweight contract claim verification for VNX dispatches.

Runs fast, deterministic checks against a dispatch's contract block after
receipt processing. This is the Phase 2a lightweight verifier — it does NOT
execute test suites or heavy analysis.

Supported checks:
  - file_exists:   Checks Path.exists()
  - file_changed:  Checks git diff --name-only for the file
  - pattern_match: Checks regex against file content
  - no_pattern:    Checks regex does NOT match file content
  - bash_check:    Runs a shell command and checks exit code 0

Exit codes:
  0  - All claims verified (or no contract block present)
  1  - One or more claims failed verification
  10 - Invalid arguments or missing dispatch file
  20 - I/O error reading files
  40 - Unexpected internal error

Usage:
  python verify_claims.py --dispatch-file <path>
  python verify_claims.py --dispatch-id <id>
  python verify_claims.py --dispatch-file <path> --output-file <path>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from result_contract import Result, result_ok, result_error, result_exit_code, EXIT_OK, EXIT_VALIDATION, EXIT_IO, EXIT_INTERNAL
from contract_parser import (
    ContractBlock,
    Claim,
    parse_contract_from_file,
    find_dispatch_for_receipt,
)

# Timeout for bash_check commands (seconds) — keep lightweight
BASH_CHECK_TIMEOUT = 10

# Disallowed command patterns for bash_check (security)
_DISALLOWED_COMMANDS = re.compile(
    r"\b(rm\s+-rf|sudo|mkfs|dd\s+if=|shutdown|reboot|kill\s+-9|pytest|python\s+-m\s+pytest)\b",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_path(claim_path: str, project_root: Path) -> Path:
    """Resolve a claim path relative to project root."""
    p = Path(claim_path)
    if p.is_absolute():
        return p
    return project_root / p


def _check_file_exists(claim: Claim, project_root: Path) -> Dict[str, Any]:
    """Verify a file exists."""
    target = _resolve_path(claim.path, project_root)
    exists = target.exists()
    return {
        "claim_type": "file_exists",
        "path": str(claim.path),
        "resolved_path": str(target),
        "passed": exists,
        "detail": "file exists" if exists else "file not found",
    }


def _check_file_changed(claim: Claim, project_root: Path) -> Dict[str, Any]:
    """Verify a file was modified (appears in git diff)."""
    target = _resolve_path(claim.path, project_root)
    rel_path = claim.path

    try:
        # Check staged + unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        changed_files = result.stdout.strip().splitlines() if result.returncode == 0 else []

        # Also check staged-only
        result2 = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result2.returncode == 0:
            changed_files.extend(result2.stdout.strip().splitlines())

        # Also check untracked (new files)
        result3 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result3.returncode == 0:
            changed_files.extend(result3.stdout.strip().splitlines())

        # Normalize paths for comparison
        changed_set = {f.strip() for f in changed_files if f.strip()}
        found = rel_path in changed_set or str(target) in changed_set

        return {
            "claim_type": "file_changed",
            "path": str(claim.path),
            "passed": found,
            "detail": "file appears in git diff" if found else "file not found in git diff",
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {
            "claim_type": "file_changed",
            "path": str(claim.path),
            "passed": False,
            "detail": f"git check failed: {exc}",
        }


def _check_pattern_match(claim: Claim, project_root: Path) -> Dict[str, Any]:
    """Verify a regex pattern appears in a file."""
    target = _resolve_path(claim.path, project_root)

    if not target.exists():
        return {
            "claim_type": "pattern_match",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": False,
            "detail": f"target file not found: {target}",
        }

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        match = re.search(claim.pattern, content)
        found = match is not None
        return {
            "claim_type": "pattern_match",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": found,
            "detail": "pattern found" if found else "pattern not found in file",
        }
    except re.error as exc:
        return {
            "claim_type": "pattern_match",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": False,
            "detail": f"invalid regex: {exc}",
        }
    except OSError as exc:
        return {
            "claim_type": "pattern_match",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": False,
            "detail": f"read error: {exc}",
        }


def _check_no_pattern(claim: Claim, project_root: Path) -> Dict[str, Any]:
    """Verify a regex pattern does NOT appear in a file."""
    target = _resolve_path(claim.path, project_root)

    if not target.exists():
        # File not existing means pattern can't be there — pass
        return {
            "claim_type": "no_pattern",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": True,
            "detail": "file does not exist (pattern trivially absent)",
        }

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        match = re.search(claim.pattern, content)
        absent = match is None
        return {
            "claim_type": "no_pattern",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": absent,
            "detail": "pattern absent (good)" if absent else "pattern found (unexpected)",
        }
    except re.error as exc:
        return {
            "claim_type": "no_pattern",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": False,
            "detail": f"invalid regex: {exc}",
        }
    except OSError as exc:
        return {
            "claim_type": "no_pattern",
            "path": str(claim.path),
            "pattern": claim.pattern,
            "passed": False,
            "detail": f"read error: {exc}",
        }


def _check_bash(claim: Claim, project_root: Path) -> Dict[str, Any]:
    """Run a shell command and verify exit code 0."""
    command = claim.command or ""

    # Security: block dangerous commands
    if _DISALLOWED_COMMANDS.search(command):
        return {
            "claim_type": "bash_check",
            "command": command,
            "passed": False,
            "detail": "command blocked by security filter (disallowed pattern)",
        }

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=BASH_CHECK_TIMEOUT,
        )
        passed = result.returncode == 0
        return {
            "claim_type": "bash_check",
            "command": command,
            "passed": passed,
            "exit_code": result.returncode,
            "detail": "command exited 0" if passed else f"command exited {result.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {
            "claim_type": "bash_check",
            "command": command,
            "passed": False,
            "detail": f"command timed out after {BASH_CHECK_TIMEOUT}s",
        }
    except OSError as exc:
        return {
            "claim_type": "bash_check",
            "command": command,
            "passed": False,
            "detail": f"execution error: {exc}",
        }


_CHECK_DISPATCH = {
    "file_exists": _check_file_exists,
    "file_changed": _check_file_changed,
    "pattern_match": _check_pattern_match,
    "no_pattern": _check_no_pattern,
    "bash_check": _check_bash,
}


def verify_contract(
    contract: ContractBlock, project_root: Path
) -> Dict[str, Any]:
    """Run all claims in a contract block and return structured results.

    Returns a dict with:
      - dispatch_id: str
      - verified_at: ISO timestamp
      - verdict: "pass" | "fail" | "no_contract"
      - total_claims: int
      - passed: int
      - failed: int
      - results: list of per-claim result dicts
      - parse_errors: list of parse error strings
    """
    if not contract.has_claims:
        return {
            "dispatch_id": contract.dispatch_id,
            "verified_at": _utc_now_iso(),
            "verdict": "no_contract",
            "total_claims": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
            "parse_errors": contract.parse_errors,
        }

    results: List[Dict[str, Any]] = []
    passed_count = 0
    failed_count = 0

    for claim in contract.claims:
        checker = _CHECK_DISPATCH.get(claim.claim_type)
        if checker is None:
            results.append({
                "claim_type": claim.claim_type,
                "passed": False,
                "detail": f"unknown claim type: {claim.claim_type}",
                "raw_line": claim.raw_line,
            })
            failed_count += 1
            continue

        check_result = checker(claim, project_root)
        check_result["raw_line"] = claim.raw_line
        check_result["line_number"] = claim.line_number
        results.append(check_result)

        if check_result.get("passed"):
            passed_count += 1
        else:
            failed_count += 1

    verdict = "pass" if failed_count == 0 else "fail"

    return {
        "dispatch_id": contract.dispatch_id,
        "verified_at": _utc_now_iso(),
        "verdict": verdict,
        "total_claims": len(contract.claims),
        "passed": passed_count,
        "failed": failed_count,
        "results": results,
        "parse_errors": contract.parse_errors,
    }


def store_verification_results(
    verification: Dict[str, Any], state_dir: Path
) -> Path:
    """Write verification results to the state directory.

    Returns the path to the written file.
    """
    results_dir = state_dir / "verification_results"
    results_dir.mkdir(parents=True, exist_ok=True)

    dispatch_id = verification.get("dispatch_id", "unknown")
    timestamp = verification.get("verified_at", _utc_now_iso()).replace(":", "").replace("-", "")
    filename = f"{dispatch_id}_{timestamp}.json"
    output_path = results_dir / filename

    output_path.write_text(
        json.dumps(verification, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lightweight contract claim verification for VNX dispatches"
    )
    parser.add_argument(
        "--dispatch-file",
        type=Path,
        help="Path to dispatch markdown file",
    )
    parser.add_argument(
        "--dispatch-id",
        type=str,
        help="Dispatch ID (will search dispatch directories)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root for path resolution (default: auto-detect)",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Write results to this file (default: state dir + stdout)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON (default)",
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

    # Resolve dispatch file
    dispatch_file: Optional[Path] = args.dispatch_file

    if dispatch_file is None and args.dispatch_id:
        dispatch_file = find_dispatch_for_receipt(args.dispatch_id, dispatch_dir)
        if dispatch_file is None:
            error_result = {
                "dispatch_id": args.dispatch_id,
                "verified_at": _utc_now_iso(),
                "verdict": "error",
                "error": f"dispatch file not found for ID: {args.dispatch_id}",
            }
            print(json.dumps(error_result, indent=2))
            return EXIT_VALIDATION

    if dispatch_file is None:
        print(
            json.dumps({"error": "either --dispatch-file or --dispatch-id required"}, indent=2),
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    if not dispatch_file.exists():
        error_result = {
            "dispatch_id": "",
            "verified_at": _utc_now_iso(),
            "verdict": "error",
            "error": f"dispatch file not found: {dispatch_file}",
        }
        print(json.dumps(error_result, indent=2))
        return EXIT_VALIDATION

    # Parse contract
    try:
        contract = parse_contract_from_file(dispatch_file)
    except OSError as exc:
        print(json.dumps({"error": f"failed to read dispatch: {exc}"}, indent=2))
        return EXIT_IO

    # Phase 2a: No contract → exit cleanly with no_contract verdict
    if not contract.has_claims:
        no_contract = {
            "dispatch_id": contract.dispatch_id,
            "verified_at": _utc_now_iso(),
            "verdict": "no_contract",
            "total_claims": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
            "parse_errors": contract.parse_errors,
        }
        print(json.dumps(no_contract, indent=2))
        return EXIT_OK

    # Run verification
    verification = verify_contract(contract, project_root)

    # Store results
    if args.store:
        stored_path = store_verification_results(verification, state_dir)
        verification["stored_at"] = str(stored_path)

    # Write output
    output_json = json.dumps(verification, indent=2)

    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(output_json + "\n", encoding="utf-8")

    print(output_json)

    return EXIT_OK if verification["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
