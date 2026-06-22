"""Tests for D2.3 — the reusable branch-protection script (scripts/commands/protect_branch.sh).

Exercises the dry-run path (no network / no gh call) to assert the policy body
and target are correct: enforce_admins, PR-required with 0 approvals, no
force-pushes, no deletions, on the requested repo/branch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "commands" / "protect_branch.sh"


def _dry_run(*args: str):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "VNX_PROTECT_DRY_RUN": "1"}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env, capture_output=True, text=True, timeout=15,
    )


def test_script_exists_and_executable():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_dry_run_targets_repo_main():
    r = _dry_run("Vinix24/SEOCRAWLER_V2")
    assert r.returncode == 0
    assert "repos/Vinix24/SEOCRAWLER_V2/branches/main/protection" in r.stdout
    assert "-X PUT" in r.stdout


def test_dry_run_policy_body_is_correct():
    r = _dry_run("Vinix24/some-repo")
    out = r.stdout
    assert '"enforce_admins": true' in out
    assert '"required_approving_review_count": 0' in out
    assert '"allow_force_pushes": false' in out
    assert '"allow_deletions": false' in out


def test_dry_run_honors_explicit_branch():
    r = _dry_run("Vinix24/some-repo", "release")
    assert "branches/release/protection" in r.stdout


def test_missing_repo_arg_errors():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "VNX_PROTECT_DRY_RUN": "1"}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=15)
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()
