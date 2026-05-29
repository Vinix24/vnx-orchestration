#!/usr/bin/env python3

import json
import subprocess as _subprocess
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

    # RA-3: required gate evidence must be present for advance to proceed.
    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    (gate_results_dir / "pr0-gemini_review-contract.json").write_text(
        json.dumps({"pr_id": "PR-0", "gate": "gemini_review", "status": "pass"}),
        encoding="utf-8",
    )
    (gate_results_dir / "pr0-codex_gate-contract.json").write_text(
        json.dumps({"pr_id": "PR-0", "gate": "codex_gate", "status": "pass"}),
        encoding="utf-8",
    )

    result = manager.advance()
    state = manager.load_state()

    assert result["advanced"] is True
    assert result["reason"] == "loaded_next_feature"
    assert result["next_feature"] == "feature-b"
    assert state["current_active_feature"] == "feature-b"
    assert "feature-a" in state["merged_features"]


def test_project_id_mismatch_reinitializes_state(roadmap_env, monkeypatch):
    """ADR-007: load_state() must refuse/re-initialize when file's project_id mismatches."""
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    manager_a = rm.RoadmapManager()
    manager_a.init_roadmap(roadmap_env["roadmap_file"])
    manager_a.load_feature("feature-a")

    # Switch to project-b — load_state() must not return project-a's features.
    monkeypatch.setenv("VNX_PROJECT_ID", "project-b")
    manager_b = rm.RoadmapManager()
    state = manager_b.load_state()

    assert state["current_active_feature"] is None, "cross-tenant feature must not leak"
    assert state["features"] == [], "cross-tenant features must not leak"
    assert state["project_id"] == "project-b"


def test_unstamped_state_migrated_on_first_load(roadmap_env, monkeypatch):
    """ADR-007: existing state file without project_id is stamped on first load, feature progress preserved."""
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")

    # Simulate legacy unstamped file: remove project_id key.
    state_path = roadmap_env["state_dir"] / "roadmap_state.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    del raw["project_id"]
    state_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    # Re-load — should stamp project_id and preserve feature progress.
    state = manager.load_state()
    assert state["project_id"] == "project-a"
    assert state["current_active_feature"] == "feature-a", "feature progress must be preserved after migration"


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


# ---------------------------------------------------------------------------
# RA-2: git branch materialization
# ---------------------------------------------------------------------------

@pytest.fixture
def git_roadmap_env(roadmap_env):
    """roadmap_env with an initialized git repo for branch creation tests."""
    project_root = roadmap_env["project_root"]
    _subprocess.run(["git", "init", "-b", "main"], cwd=str(project_root), capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(project_root), capture_output=True)
    _subprocess.run(["git", "config", "user.name", "Test"], cwd=str(project_root), capture_output=True)
    (project_root / "README.md").write_text("init", encoding="utf-8")
    _subprocess.run(["git", "add", "README.md"], cwd=str(project_root), capture_output=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=str(project_root), capture_output=True)
    return roadmap_env


def test_load_feature_creates_branch(git_roadmap_env, monkeypatch):
    """load_feature creates the feature branch in git when absent."""
    project_root = git_roadmap_env["project_root"]
    manager = rm.RoadmapManager()
    manager.init_roadmap(git_roadmap_env["roadmap_file"])

    manager.load_feature("feature-a", no_worktree=True)

    r = _subprocess.run(
        ["git", "-C", str(project_root), "show-ref", "--verify", "refs/heads/feature/a"],
        capture_output=True,
    )
    assert r.returncode == 0, "feature/a branch must exist in git after load_feature"


def test_load_feature_branch_creation_idempotent(git_roadmap_env, monkeypatch):
    """Second load_feature call is a no-op: branch_created=False when branch already exists."""
    receipts = []
    monkeypatch.setattr(rm, "emit_governance_receipt", lambda *a, **kw: receipts.append(kw))
    manager = rm.RoadmapManager()
    manager.init_roadmap(git_roadmap_env["roadmap_file"])

    manager.load_feature("feature-a", no_worktree=True)
    receipts.clear()

    manager.load_feature("feature-a", no_worktree=True)

    load_receipt = next((r for r in receipts if r.get("action") == "load_feature"), None)
    assert load_receipt is not None
    assert load_receipt["branch_created"] is False, "second call must be no-op (branch already exists)"


def test_load_feature_receipt_carries_branch_info(git_roadmap_env, monkeypatch):
    """roadmap_transition receipt carries branch_created, worktree_path, and project_id (ADR-007)."""
    receipts = []
    monkeypatch.setattr(rm, "emit_governance_receipt", lambda *a, **kw: receipts.append(kw))
    manager = rm.RoadmapManager()
    manager.init_roadmap(git_roadmap_env["roadmap_file"])

    manager.load_feature("feature-a", no_worktree=True)

    load_receipt = next((r for r in receipts if r.get("action") == "load_feature"), None)
    assert load_receipt is not None, "load_feature receipt not emitted"
    assert "branch_created" in load_receipt, "receipt must carry branch_created"
    assert "worktree_path" in load_receipt, "receipt must carry worktree_path"
    assert load_receipt.get("project_id") is not None, "ADR-007: receipt must carry project_id"


def test_load_feature_no_worktree_flag_skips_provisioning(git_roadmap_env, monkeypatch):
    """--no-worktree prevents worktree_start from being called."""
    start_calls = []

    def _mock_worktree_start(*a, **kw):
        start_calls.append(kw)
        return type("R", (), {"success": True, "message": "/fake"})()

    monkeypatch.setattr(rm, "worktree_start", _mock_worktree_start)
    manager = rm.RoadmapManager()
    manager.init_roadmap(git_roadmap_env["roadmap_file"])

    manager.load_feature("feature-a", no_worktree=True)

    assert len(start_calls) == 0, "worktree_start must not be called when no_worktree=True"


# ---------------------------------------------------------------------------
# RA-3: gate evidence enforcement
# ---------------------------------------------------------------------------


def _write_gate_result(gate_results_dir: Path, pr_id: str, gate: str, status: str = "pass") -> None:
    pr_slug = pr_id.lower().replace("-", "")
    (gate_results_dir / f"{pr_slug}-{gate}-contract.json").write_text(
        json.dumps({"pr_id": pr_id, "gate": gate, "status": status}),
        encoding="utf-8",
    )


def test_reconcile_gates_incomplete_when_codex_missing(roadmap_env, monkeypatch):
    """Closure structurally passes but codex gate result absent → gates_incomplete."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review")
    # codex_gate intentionally absent

    result = manager.reconcile()

    assert result["verdict"] == "gates_incomplete"


def test_advance_blocked_when_gates_incomplete(roadmap_env, monkeypatch):
    """gates_incomplete verdict → advance does NOT progress feature."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    # gate_results_dir intentionally not created → gates_incomplete

    result = manager.advance()

    assert result["advanced"] is False
    assert result["reason"] == "gates_incomplete"
    state = manager.load_state()
    assert state["current_active_feature"] == "feature-a", "feature must not advance"


def test_reconcile_passes_when_gemini_and_codex_both_pass(roadmap_env, monkeypatch):
    """gemini PASS + codex PASS → reconcile passes → advance progresses to next feature."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate")

    result = manager.reconcile()
    assert result["verdict"] == "pass"


def test_optional_gate_missing_does_not_block(roadmap_env, monkeypatch):
    """claude_github_optional absent → does NOT produce gates_incomplete (advisory only)."""
    manager = rm.RoadmapManager()

    # Create a roadmap that includes claude_github_optional in review_stack.
    project_root = roadmap_env["project_root"]
    roadmap_with_optional = project_root / "ROADMAP_OPT.yaml"
    roadmap_with_optional.write_text(
        """features:
  - feature_id: feature-opt
    title: Feature Opt
    plan_path: roadmap/features/feature-a/FEATURE_PLAN.md
    branch_name: feature/opt
    risk_class: medium
    merge_policy: human
    review_stack: [gemini_review, codex_gate, claude_github_optional]
    depends_on: []
    status: planned
""",
        encoding="utf-8",
    )
    manager.init_roadmap(roadmap_with_optional)
    manager.load_feature("feature-opt")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate")
    # claude_github_optional intentionally absent

    result = manager.reconcile()

    assert result["verdict"] == "pass", "optional gate absence must not produce gates_incomplete"
