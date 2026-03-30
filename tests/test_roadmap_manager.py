#!/usr/bin/env python3

import json
import sys
from pathlib import Path

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import roadmap_manager as rm


@pytest.fixture
def roadmap_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    fake_vnx_home = project_root / ".claude" / "vnx-system"
    fake_vnx_home.mkdir(parents=True, exist_ok=True)
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    state_dir.mkdir(parents=True, exist_ok=True)
    dispatch_dir.mkdir(parents=True, exist_ok=True)

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
    monkeypatch.setattr(rm, "emit_governance_receipt", lambda *args, **kwargs: None)

    roadmap_dir = project_root / "roadmap" / "features"
    (roadmap_dir / "feature-a").mkdir(parents=True, exist_ok=True)
    (roadmap_dir / "feature-b").mkdir(parents=True, exist_ok=True)
    feature_a = roadmap_dir / "feature-a" / "FEATURE_PLAN.md"
    feature_b = roadmap_dir / "feature-b" / "FEATURE_PLAN.md"
    for path, title, branch in [
        (feature_a, "Feature A", "feature/a"),
        (feature_b, "Feature B", "feature/b"),
    ]:
        path.write_text(
            f"""# Feature: {title}

**Status**: Draft
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: {title} PR
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Skill**: @architect
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: []
""",
            encoding="utf-8",
        )

    roadmap_file = project_root / "ROADMAP.yaml"
    roadmap_file.write_text(
        """features:
  - feature_id: feature-a
    title: Feature A
    plan_path: roadmap/features/feature-a/FEATURE_PLAN.md
    branch_name: feature/a
    risk_class: low
    merge_policy: conditional_auto
    review_stack: [gemini_review, codex_gate]
    depends_on: []
    status: planned
  - feature_id: feature-b
    title: Feature B
    plan_path: roadmap/features/feature-b/FEATURE_PLAN.md
    branch_name: feature/b
    risk_class: medium
    merge_policy: human
    review_stack: [gemini_review, codex_gate]
    depends_on: [feature-a]
    status: planned
""",
        encoding="utf-8",
    )

    return {"project_root": project_root, "roadmap_file": roadmap_file, "state_dir": state_dir}


def test_load_feature_materializes_active_plan_and_queue(roadmap_env):
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    state = manager.load_feature("feature-a")

    assert state["current_active_feature"] == "feature-a"
    feature_plan = roadmap_env["project_root"] / "FEATURE_PLAN.md"
    pr_queue = roadmap_env["project_root"] / "PR_QUEUE.md"
    assert feature_plan.exists()
    assert pr_queue.exists()
    assert "Feature A" in feature_plan.read_text(encoding="utf-8")
    assert "Risk-Class: low" in pr_queue.read_text(encoding="utf-8")


def test_advance_loads_next_feature_after_verified_merge(roadmap_env, monkeypatch):
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm,
        "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    result = manager.advance()
    state = manager.load_state()

    assert result["advanced"] is True
    assert result["reason"] == "loaded_next_feature"
    assert result["next_feature"] == "feature-b"
    assert state["current_active_feature"] == "feature-b"
    assert "feature-a" in state["merged_features"]


def test_advance_inserts_fixup_when_blocking_drift_detected(roadmap_env, monkeypatch):
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm,
        "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    (roadmap_env["state_dir"] / "post_feature_drift.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "oi-1",
                        "title": "Fix runtime regression",
                        "category": "path/runtime regression",
                        "blocking": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = manager.advance()
    state = manager.load_state()

    assert result["advanced"] is True
    assert result["reason"] == "blocking_fixup_inserted"
    assert state["current_active_feature"].startswith("fixup-")
    assert state["inserted_fixups"]
