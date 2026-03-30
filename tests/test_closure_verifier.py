#!/usr/bin/env python3

import json
import subprocess
import sys
from pathlib import Path

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import closure_verifier as cv


@pytest.fixture
def verifier_env(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_root, check=True, capture_output=True)
    (project_root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_root, check=True, capture_output=True)

    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    (dispatch_dir / "staging").mkdir(parents=True, exist_ok=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))

    feature_plan = project_root / "FEATURE_PLAN.md"
    feature_plan.write_text(
        """# Feature: Demo Feature

**Status**: Complete

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Demo PR
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Skill**: @architect
**Dependencies**: []
""",
        encoding="utf-8",
    )
    pr_queue = project_root / "PR_QUEUE.md"
    pr_queue.write_text(
        """# PR Queue - Feature: Demo Feature

## Progress Overview
Total: 1 PRs | Complete: 1 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Status

## Dependency Flow
```
PR-0 (no dependencies)
```
""",
        encoding="utf-8",
    )
    claim_file = state_dir / "closure_claim.json"
    claim_file.write_text(
        json.dumps(
            {
                "test_files": ["FEATURE_PLAN.md"],
                "test_command": "python3 -m pytest tests/test_demo.py",
                "parallel_assignments": [{"terminal": "T1"}, {"terminal": "T2"}],
            }
        ),
        encoding="utf-8",
    )

    return {
        "project_root": project_root,
        "feature_plan": feature_plan,
        "pr_queue": pr_queue,
        "claim_file": claim_file,
        "dispatch_dir": dispatch_dir,
    }


def _good_pr_payload(state="OPEN", merge_state="CLEAN"):
    return {
        "number": 45,
        "url": "https://example.test/pr/45",
        "state": state,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "mergeCommit": {"oid": "abc123"},
    }


def test_verify_closure_fails_when_pr_missing(verifier_env, monkeypatch):
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: None)

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    assert result["verdict"] == "fail"
    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "pr_exists" in failed


def test_verify_closure_fails_on_metadata_drift(verifier_env, monkeypatch):
    verifier_env["pr_queue"].write_text(
        """# PR Queue - Feature: Wrong Feature

## Progress Overview
Total: 1 PRs | Complete: 1 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Status

## Dependency Flow
```
PR-0 -> PR-1
```
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload())

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "metadata_sync" in failed


def test_verify_closure_fails_when_stale_staging_dispatches_present(verifier_env, monkeypatch):
    stale_dispatch = verifier_env["dispatch_dir"] / "staging" / "stale.md"
    stale_dispatch.write_text("PR-ID: PR-999\n", encoding="utf-8")
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload())

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "stale_staging" in failed


def test_verify_closure_passes_for_valid_post_merge_state(verifier_env, monkeypatch):
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload(state="MERGED"))
    monkeypatch.setattr(cv, "_merge_commit_on_main", lambda oid, project_root: True)

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="post_merge",
        claim_file=verifier_env["claim_file"],
    )

    assert result["verdict"] == "pass"
