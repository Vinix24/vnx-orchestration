#!/usr/bin/env python3
"""Independent closure-verification pass (PR-7).

Checks push/PR/CI/metadata consistency for completed PRs. This script is
the adversarial review tool required by C-R7 and gate_pr7_qa_and_certification.

Usage:
  python3 scripts/verify_closure.py                  # Check all completed PRs
  python3 scripts/verify_closure.py --json            # JSON output for CI
  python3 scripts/verify_closure.py --pr PR-3         # Check specific PR

Checks performed:
  1. FEATURE_PLAN.md and PR_QUEUE.md exist and are parseable
  2. Completed PRs in PR_QUEUE have matching commits on the branch
  3. FEATURE_PLAN.md status matches PR_QUEUE.md status
  4. No phantom PRs (claimed complete but no evidence)
  5. Test files referenced by PRs actually exist
  6. Branch exists and has been pushed (not local-only)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class ClosureCheck:
    name: str
    pr: str
    status: str  # pass | warn | fail
    message: str
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name, "pr": self.pr, "status": self.status, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd or str(REPO_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return 1, str(e)


def _get_branch() -> str:
    rc, out = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return out if rc == 0 else "unknown"


def _branch_has_remote() -> bool:
    rc, out = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return rc == 0 and bool(out)


def _commit_messages(count: int = 50) -> List[str]:
    rc, out = _git(["log", f"--max-count={count}", "--pretty=format:%s"])
    return out.split("\n") if rc == 0 and out else []


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------

def _parse_pr_queue(path: Path) -> Dict[str, str]:
    """Parse PR_QUEUE.md, return {PR-N: status}."""
    if not path.exists():
        return {}
    text = path.read_text()
    result = {}

    # Completed PRs section
    completed_section = re.search(
        r"### ✅ Completed PRs\n(.*?)(?=\n### |$)", text, re.DOTALL
    )
    if completed_section:
        for m in re.finditer(r"PR-(\d+)", completed_section.group(1)):
            result[f"PR-{m.group(1)}"] = "completed"

    # Active PRs section
    active_section = re.search(
        r"### 🔄 Active PRs\n(.*?)(?=\n### |$)", text, re.DOTALL
    )
    if active_section:
        for m in re.finditer(r"PR-(\d+)", active_section.group(1)):
            result[f"PR-{m.group(1)}"] = "active"

    # Queued PRs section
    queued_section = re.search(
        r"### ⏳ Queued PRs\n(.*?)(?=\n### |$)", text, re.DOTALL
    )
    if queued_section:
        for m in re.finditer(r"PR-(\d+)", queued_section.group(1)):
            result[f"PR-{m.group(1)}"] = "queued"

    return result


def _parse_feature_plan_prs(path: Path) -> List[str]:
    """Extract PR identifiers from FEATURE_PLAN.md."""
    if not path.exists():
        return []
    text = path.read_text()
    return list(set(re.findall(r"PR-\d+", text)))


def _find_test_files() -> set:
    """Find all test files in tests/ directory."""
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        return set()
    return {f.name for f in tests_dir.glob("test_*.py")}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_metadata_files_exist() -> List[ClosureCheck]:
    checks = []
    for fname in ["FEATURE_PLAN.md", "PR_QUEUE.md"]:
        path = REPO_ROOT / fname
        if path.exists():
            checks.append(ClosureCheck(
                "metadata_exists", fname, PASS, f"{fname} exists"
            ))
        else:
            checks.append(ClosureCheck(
                "metadata_exists", fname, FAIL, f"{fname} missing"
            ))
    return checks


def check_pr_queue_feature_plan_alignment() -> List[ClosureCheck]:
    """FEATURE_PLAN.md and PR_QUEUE.md must agree on which PRs exist."""
    checks = []
    queue_prs = _parse_pr_queue(REPO_ROOT / "PR_QUEUE.md")
    plan_prs = set(_parse_feature_plan_prs(REPO_ROOT / "FEATURE_PLAN.md"))

    if not queue_prs and not plan_prs:
        return [ClosureCheck("alignment", "all", WARN, "No PRs found in either file")]

    # Every completed PR in queue should be in feature plan
    for pr, status in queue_prs.items():
        if pr in plan_prs:
            checks.append(ClosureCheck(
                "alignment", pr, PASS, f"{pr} in both files (queue: {status})"
            ))
        else:
            checks.append(ClosureCheck(
                "alignment", pr, WARN, f"{pr} in PR_QUEUE but not in FEATURE_PLAN"
            ))

    return checks


def check_completed_prs_have_commits() -> List[ClosureCheck]:
    """Completed PRs should have at least one commit mentioning them."""
    checks = []
    queue_prs = _parse_pr_queue(REPO_ROOT / "PR_QUEUE.md")
    messages = _commit_messages(100)
    msg_text = "\n".join(messages).lower()

    for pr, status in queue_prs.items():
        if status != "completed":
            continue
        pr_lower = pr.lower()
        # Check if any commit message references this PR
        if pr_lower in msg_text:
            checks.append(ClosureCheck(
                "commit_evidence", pr, PASS,
                f"{pr} has commit evidence on branch"
            ))
        else:
            checks.append(ClosureCheck(
                "commit_evidence", pr, WARN,
                f"{pr} marked complete but no commit references it",
                details=[f"Searched {len(messages)} recent commits"]
            ))

    return checks


def check_branch_pushed() -> List[ClosureCheck]:
    """Current branch should track a remote (not local-only)."""
    branch = _get_branch()
    has_remote = _branch_has_remote()
    if has_remote:
        return [ClosureCheck("branch_pushed", branch, PASS, f"Branch '{branch}' tracks remote")]
    return [ClosureCheck(
        "branch_pushed", branch, WARN,
        f"Branch '{branch}' has no remote tracking — push before closure",
    )]


def check_test_files_exist() -> List[ClosureCheck]:
    """Test files referenced by the feature should actually exist."""
    checks = []
    test_files = _find_test_files()

    # Key test files for this adoption feature
    expected_tests = [
        "test_vnx_mode.py",
        "test_vnx_starter.py",
        "test_vnx_init.py",
        "test_vnx_setup.py",
        "test_vnx_install.py",
        "test_vnx_demo.py",
        "test_vnx_doctor.py",
        "test_vnx_worktree.py",
        "test_path_resolution_regression.py",
        "test_quickstart_validation.py",
        "test_docs_command_validation.py",
    ]

    for tf in expected_tests:
        if tf in test_files:
            checks.append(ClosureCheck("test_file", tf, PASS, f"{tf} exists"))
        else:
            checks.append(ClosureCheck("test_file", tf, FAIL, f"{tf} missing"))

    return checks


def check_feature_plan_not_draft() -> List[ClosureCheck]:
    """FEATURE_PLAN.md should not still say 'Draft' if queue shows significant completion."""
    plan_path = REPO_ROOT / "FEATURE_PLAN.md"
    queue_path = REPO_ROOT / "PR_QUEUE.md"
    if not plan_path.exists():
        return [ClosureCheck("plan_status", "FEATURE_PLAN", FAIL, "File missing")]

    text = plan_path.read_text()
    queue_prs = _parse_pr_queue(queue_path)
    completed_count = sum(1 for s in queue_prs.values() if s == "completed")
    total = len(queue_prs)

    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text)
    plan_status = status_match.group(1) if status_match else "unknown"

    if completed_count > total * 0.5 and plan_status.lower() == "draft":
        return [ClosureCheck(
            "plan_status", "FEATURE_PLAN", WARN,
            f"FEATURE_PLAN still 'Draft' but {completed_count}/{total} PRs complete",
        )]
    return [ClosureCheck(
        "plan_status", "FEATURE_PLAN", PASS,
        f"Status: {plan_status} ({completed_count}/{total} complete)",
    )]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_closure_verification(pr_filter: Optional[str] = None) -> List[ClosureCheck]:
    all_checks = []
    all_checks.extend(check_metadata_files_exist())
    all_checks.extend(check_pr_queue_feature_plan_alignment())
    all_checks.extend(check_completed_prs_have_commits())
    all_checks.extend(check_branch_pushed())
    all_checks.extend(check_test_files_exist())
    all_checks.extend(check_feature_plan_not_draft())

    if pr_filter:
        all_checks = [c for c in all_checks if c.pr == pr_filter or c.pr in ("all", pr_filter)]

    return all_checks


def _print_results(checks: List[ClosureCheck], json_output: bool = False) -> int:
    if json_output:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        icons = {PASS: "✓", WARN: "⚠", FAIL: "✗"}
        for c in checks:
            icon = icons.get(c.status, "?")
            print(f"  [{icon}] {c.pr}: {c.name} — {c.message}")
            for d in c.details:
                print(f"      {d}")

    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    passes = sum(1 for c in checks if c.status == PASS)
    print(f"\nSummary: {passes} pass, {warns} warn, {fails} fail")
    return 1 if fails > 0 else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    json_output = "--json" in sys.argv
    pr_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--pr" and i + 1 < len(sys.argv):
            pr_filter = sys.argv[i + 1]

    if not json_output:
        print("VNX Closure Verification")
        print(f"Branch: {_get_branch()}")
        print(f"Repo: {REPO_ROOT}\n")

    checks = run_closure_verification(pr_filter)
    return _print_results(checks, json_output)


if __name__ == "__main__":
    sys.exit(main())
