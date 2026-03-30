#!/usr/bin/env python3

import sys
from pathlib import Path

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pr_queue_manager import PRQueueManager


@pytest.fixture
def queue_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    fake_vnx_home = project_root / ".claude" / "vnx-system"
    fake_vnx_home.mkdir(parents=True, exist_ok=True)
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    (dispatch_dir / "staging").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(fake_vnx_home))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.chdir(project_root)
    return project_root


def test_load_feature_plan_captures_feature_metadata(queue_env):
    feature_plan = queue_env / "FEATURE_PLAN.md"
    feature_plan.write_text(
        """# Feature: Governance Feature

**Status**: Draft
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Queue Metadata
**Track**: C
**Priority**: P0
**Complexity**: High
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: []
""",
        encoding="utf-8",
    )

    manager = PRQueueManager()
    success, count = manager.load_feature_plan(str(feature_plan))

    assert success is True
    assert count == 1
    assert manager.state["feature_metadata"]["risk_class"] == "high"
    assert manager.state["prs"][0]["risk_class"] == "high"
    assert manager.state["prs"][0]["review_stack"] == [
        "gemini_review",
        "codex_gate",
        "claude_github_optional",
    ]
    pr_queue = (queue_env / "PR_QUEUE.md").read_text(encoding="utf-8")
    assert "Risk-Class: high" in pr_queue
    assert "Review-Stack: gemini_review,codex_gate,claude_github_optional" in pr_queue


def test_init_feature_batch_writes_governance_metadata_to_dispatch(queue_env):
    feature_plan = queue_env / "FEATURE_PLAN.md"
    feature_plan.write_text(
        """# Feature: Dispatch Governance

**Status**: Draft
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Dispatch Metadata
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Skill**: @backend-developer
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 2h
**Dependencies**: []
""",
        encoding="utf-8",
    )

    manager = PRQueueManager()
    success, created = manager.init_feature_batch(str(feature_plan))

    assert success is True
    assert created == 1
    dispatch_files = list((queue_env / ".vnx-data" / "dispatches" / "staging").glob("*.md"))
    assert len(dispatch_files) == 1
    content = dispatch_files[0].read_text(encoding="utf-8")
    assert "Risk-Class: low" in content
    assert "Merge-Policy: conditional_auto" in content
    assert "Review-Stack: gemini_review,codex_gate" in content
