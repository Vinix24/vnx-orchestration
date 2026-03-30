#!/usr/bin/env python3
"""Governance closure verification for VNX feature branches."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_feature_plan(path: Path) -> Dict[str, Any]:
    content = _read_text(path)
    title_match = re.search(r"^#\s*Feature:\s*(.+)$", content, re.MULTILINE)
    status_match = re.search(r"^\*\*Status\*\*:\s*(.+)$", content, re.MULTILINE)
    deps_match = re.search(r"^## Dependency Flow\s*```text\s*(.+?)```", content, re.MULTILINE | re.DOTALL)
    pr_ids = re.findall(r"^##\s+(PR-\d+):", content, re.MULTILINE)
    return {
        "title": title_match.group(1).strip() if title_match else "",
        "status": status_match.group(1).strip() if status_match else "",
        "dependency_flow": deps_match.group(1).strip() if deps_match else "",
        "pr_ids": pr_ids,
    }


def _parse_pr_queue(path: Path) -> Dict[str, Any]:
    content = _read_text(path)
    title_match = re.search(r"^# PR Queue(?:\s*-\s*Feature:|\s*—\s*FP\d+:\s*)(.+)$", content, re.MULTILINE)
    overview_match = re.search(
        r"Total:\s*(\d+)\s+PRs\s*\|\s*Complete:\s*(\d+)\s*\|\s*Active:\s*(\d+)\s*\|\s*Queued:\s*(\d+)\s*\|\s*Blocked:\s*(\d+)",
        content,
    )
    deps_match = re.search(r"## Dependency Flow(?: \(executed\))?\s*```\s*(.+?)```", content, re.DOTALL)
    return {
        "title": title_match.group(1).strip() if title_match else "",
        "overview": tuple(int(x) for x in overview_match.groups()) if overview_match else None,
        "dependency_flow": deps_match.group(1).strip() if deps_match else "",
    }


def _find_branch_pr(branch: str) -> Optional[Dict[str, Any]]:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,mergeStateStatus,mergeCommit,statusCheckRollup,headRefName,baseRefName",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return payload[0] if payload else None


def _remote_branch_exists(branch: str, project_root: Path) -> bool:
    result = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=project_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def _merge_commit_on_main(oid: str, project_root: Path) -> bool:
    _run(["git", "fetch", "origin", "main", "--quiet"], cwd=project_root)
    result = _run(["git", "merge-base", "--is-ancestor", oid, "origin/main"], cwd=project_root)
    return result.returncode == 0


def _load_claim_file(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError:
        return {}


def _validate_test_claims(claims: Dict[str, Any], project_root: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    test_files = claims.get("test_files") or []
    test_command = (claims.get("test_command") or "").strip()
    trusted_result = claims.get("trusted_test_result")

    if not test_files:
        results.append(CheckResult("test_files", "FAIL", "no claimed test files provided"))
    else:
        missing = []
        for rel in test_files:
            path = Path(rel)
            if not path.is_absolute():
                path = project_root / rel
            if not path.exists():
                missing.append(str(rel))
        if missing:
            results.append(CheckResult("test_files", "FAIL", f"missing claimed test files: {', '.join(missing)}"))
        else:
            results.append(CheckResult("test_files", "PASS", f"{len(test_files)} claimed test file(s) exist"))

    if test_command:
        results.append(CheckResult("test_command", "PASS", "claimed test command present"))
    elif trusted_result:
        results.append(CheckResult("test_command", "PASS", "trusted test result recorded"))
    else:
        results.append(CheckResult("test_command", "FAIL", "no test command or trusted test result provided"))

    parallel_assignments = claims.get("parallel_assignments") or []
    if parallel_assignments:
        terminals = [str(entry.get("terminal")).strip() for entry in parallel_assignments if entry.get("terminal")]
        duplicates = sorted({terminal for terminal in terminals if terminals.count(terminal) > 1})
        if duplicates:
            results.append(
                CheckResult(
                    "parallelism",
                    "FAIL",
                    f"same terminal reported for parallel work: {', '.join(duplicates)}",
                )
            )
        else:
            results.append(CheckResult("parallelism", "PASS", "parallel assignments use distinct terminals"))

    commit_map = claims.get("commit_pr_map") or {}
    if commit_map:
        bad = []
        for sha in commit_map.keys():
            result = _run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=project_root)
            if result.returncode != 0:
                bad.append(sha)
        if bad:
            results.append(CheckResult("commit_mapping", "FAIL", f"unknown commit(s): {', '.join(bad)}"))
        else:
            results.append(CheckResult("commit_mapping", "PASS", "commit-to-PR mapping references known commits"))

    return results


def _check_stale_staging(paths: Dict[str, str], active_pr_ids: Iterable[str]) -> CheckResult:
    staging_dir = Path(paths["VNX_DISPATCH_DIR"]) / "staging"
    if not staging_dir.exists():
        return CheckResult("stale_staging", "PASS", "no staging directory")

    active_set = set(active_pr_ids)
    stale: List[str] = []
    for dispatch in staging_dir.glob("*.md"):
        content = _read_text(dispatch)
        match = re.search(r"^PR-ID:\s*(.+)$", content, re.MULTILINE)
        pr_id = match.group(1).strip() if match else ""
        if pr_id and pr_id not in active_set:
            stale.append(dispatch.name)

    if stale:
        return CheckResult("stale_staging", "FAIL", f"stale staging dispatches present: {', '.join(sorted(stale))}")
    return CheckResult("stale_staging", "PASS", "no stale staging dispatches")


def verify_closure(
    *,
    project_root: Path,
    feature_plan: Path,
    pr_queue: Path,
    branch: str,
    mode: str,
    claim_file: Optional[Path] = None,
) -> Dict[str, Any]:
    feature = _parse_feature_plan(feature_plan)
    queue = _parse_pr_queue(pr_queue)
    claims = _load_claim_file(claim_file)
    paths = ensure_env()

    checks: List[CheckResult] = []

    checks.append(
        CheckResult(
            "feature_plan_status",
            "PASS" if feature["status"].lower() == "complete" else "FAIL",
            f"FEATURE_PLAN status is {feature['status'] or 'missing'}",
        )
    )

    queue_match = queue["overview"] is not None and queue["overview"][0] == queue["overview"][1]
    checks.append(
        CheckResult(
            "pr_queue_complete",
            "PASS" if queue_match else "FAIL",
            "PR queue is fully complete" if queue_match else "PR queue totals are not fully complete",
        )
    )

    checks.append(
        CheckResult(
            "metadata_sync",
            "PASS" if feature["title"] and feature["title"] == queue["title"] and feature["dependency_flow"] == queue["dependency_flow"] else "FAIL",
            "FEATURE_PLAN and PR_QUEUE titles/dependency flow match"
            if feature["title"] and feature["title"] == queue["title"] and feature["dependency_flow"] == queue["dependency_flow"]
            else "FEATURE_PLAN and PR_QUEUE drift detected",
        )
    )

    branch_exists = _remote_branch_exists(branch, project_root)
    checks.append(
        CheckResult(
            "branch_pushed",
            "PASS" if branch_exists else "FAIL",
            f"remote branch {'found' if branch_exists else 'missing'}: {branch}",
        )
    )

    pr = _find_branch_pr(branch)
    checks.append(
        CheckResult(
            "pr_exists",
            "PASS" if pr else "FAIL",
            f"PR {'found' if pr else 'missing'} for branch {branch}",
        )
    )

    if pr:
        if mode == "pre_merge":
            clean = str(pr.get("mergeStateStatus") or "").upper() == "CLEAN" and str(pr.get("state") or "").upper() == "OPEN"
            checks.append(
                CheckResult(
                    "merge_state",
                    "PASS" if clean else "FAIL",
                    f"PR state={pr.get('state')} mergeStateStatus={pr.get('mergeStateStatus')}",
                )
            )
            rollup = pr.get("statusCheckRollup") or []
            all_green = bool(rollup) and all(
                item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
                for item in rollup
                if item.get("__typename") == "CheckRun"
            )
            checks.append(
                CheckResult(
                    "github_checks",
                    "PASS" if all_green else "FAIL",
                    "all required GitHub checks green" if all_green else "GitHub checks incomplete or failing",
                )
            )
        else:
            merged = str(pr.get("state") or "").upper() == "MERGED"
            checks.append(
                CheckResult(
                    "pr_merged",
                    "PASS" if merged else "FAIL",
                    f"PR state={pr.get('state')}",
                )
            )
            merge_commit = ((pr.get("mergeCommit") or {}) if isinstance(pr.get("mergeCommit"), dict) else {}) or {}
            oid = merge_commit.get("oid")
            on_main = bool(oid) and _merge_commit_on_main(oid, project_root)
            checks.append(
                CheckResult(
                    "merge_commit_on_main",
                    "PASS" if on_main else "FAIL",
                    f"merge commit {'present on origin/main' if on_main else 'missing from origin/main'}",
                )
            )

    checks.extend(_validate_test_claims(claims, project_root))
    checks.append(_check_stale_staging(paths, feature["pr_ids"]))

    verdict = "pass" if all(check.status == "PASS" for check in checks) else "fail"
    payload = {
        "verdict": verdict,
        "mode": mode,
        "branch": branch,
        "feature_title": feature["title"],
        "checks": [check.__dict__ for check in checks],
        "pr": pr,
        "claim_file": str(claim_file) if claim_file else None,
    }
    return payload


def _default_claim_file(paths: Dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "closure_claim.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX closure verifier")
    parser.add_argument("--feature-plan", default="FEATURE_PLAN.md")
    parser.add_argument("--pr-queue", default="PR_QUEUE.md")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--mode", choices=("pre_merge", "post_merge"), default="pre_merge")
    parser.add_argument("--claim-file", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-receipt", action="store_true")
    args = parser.parse_args(argv)

    paths = ensure_env()
    project_root = Path(paths["PROJECT_ROOT"]).resolve()
    branch = args.branch or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()
    claim_file = Path(args.claim_file) if args.claim_file else _default_claim_file(paths)

    result = verify_closure(
        project_root=project_root,
        feature_plan=(project_root / args.feature_plan if not Path(args.feature_plan).is_absolute() else Path(args.feature_plan)),
        pr_queue=(project_root / args.pr_queue if not Path(args.pr_queue).is_absolute() else Path(args.pr_queue)),
        branch=branch,
        mode=args.mode,
        claim_file=claim_file if claim_file.exists() else None,
    )

    if args.emit_receipt:
        emit_governance_receipt(
            "closure_verification_result",
            status="success" if result["verdict"] == "pass" else "blocked",
            branch=branch,
            verification_mode=args.mode,
            feature_title=result.get("feature_title"),
            verifier="closure_verifier.py",
            checks=result["checks"],
        )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Closure verifier: {result['verdict'].upper()}")
        for check in result["checks"]:
            print(f"- [{check['status']}] {check['name']}: {check['detail']}")

    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
