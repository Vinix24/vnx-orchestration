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
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch="feature/a")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate", branch="feature/a")

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


def _write_gate_result(
    gate_results_dir: Path,
    pr_id: str,
    gate: str,
    status: str = "pass",
    branch: str = "feature/a",
    project_id: str = "vnx-dev",
) -> None:
    """Write a fully-valid gate result contract with report evidence on disk."""
    pr_slug = pr_id.lower().replace("-", "")
    report_file = gate_results_dir / f"{pr_slug}-{gate}-report.md"
    report_file.write_text(f"# {gate} report for {pr_id}\n", encoding="utf-8")
    data: dict = {
        "pr_id": pr_id,
        "gate": gate,
        "status": status,
        "project_id": project_id,
        "branch": branch,
        "report_path": str(report_file),
        "contract_hash": f"hash-{pr_slug}-{gate}",
    }
    (gate_results_dir / f"{pr_slug}-{gate}-contract.json").write_text(
        json.dumps(data),
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
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch="feature/a")
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
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch="feature/a")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate", branch="feature/a")

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
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch="feature/opt")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate", branch="feature/opt")
    # claude_github_optional intentionally absent

    result = manager.reconcile()

    assert result["verdict"] == "pass", "optional gate absence must not produce gates_incomplete"


# ---------------------------------------------------------------------------
# RA-3b: bypass matrix — governance holes closed
# ---------------------------------------------------------------------------


def test_gates_incomplete_status_only_result(roadmap_env, monkeypatch):
    """Status-only {status:pass} result (no report_path/contract_hash) → advance BLOCKED."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    # Write status-only results: missing report_path and contract_hash.
    pr_slug = "pr0"
    for gate in ("gemini_review", "codex_gate"):
        (gate_results_dir / f"{pr_slug}-{gate}-contract.json").write_text(
            json.dumps({"pr_id": "PR-0", "gate": gate, "status": "pass",
                        "project_id": "vnx-dev", "branch": "feature/a"}),
            encoding="utf-8",
        )

    result = manager.reconcile()
    assert result["verdict"] == "gates_incomplete", "status-only pass must not satisfy advance"


def test_gates_incomplete_mismatched_project_id(roadmap_env, monkeypatch):
    """Gate result with mismatched project_id → does NOT satisfy advance (ADR-007)."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    # Write results with a different project_id — should be rejected.
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review",
                       branch="feature/a", project_id="other-project")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate",
                       branch="feature/a", project_id="other-project")

    result = manager.reconcile()
    assert result["verdict"] == "gates_incomplete", "mismatched project_id must not satisfy advance"


def test_gates_incomplete_stale_branch_result(roadmap_env, monkeypatch):
    """Stale gate result from a different branch → rejected (ADR-005)."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    # Write results with a different branch — stale evidence from a prior feature.
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review",
                       branch="feature/other-old-feature")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate",
                       branch="feature/other-old-feature")

    result = manager.reconcile()
    assert result["verdict"] == "gates_incomplete", "stale branch result must not satisfy advance"


def test_gates_incomplete_branch_less_result(roadmap_env, monkeypatch):
    """Branch-less gate result (no branch field) → rejected as stale evidence."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    # Write results without branch field — must be rejected as stale.
    pr_slug = "pr0"
    report_file = gate_results_dir / "pr0-gemini_review-report.md"
    report_file.write_text("# report\n", encoding="utf-8")
    for gate in ("gemini_review", "codex_gate"):
        rep = gate_results_dir / f"pr0-{gate}-report.md"
        rep.write_text("# report\n", encoding="utf-8")
        (gate_results_dir / f"{pr_slug}-{gate}-contract.json").write_text(
            json.dumps({"pr_id": "PR-0", "gate": gate, "status": "pass",
                        "project_id": "vnx-dev",
                        "report_path": str(rep),
                        "contract_hash": f"hash-{gate}"}),
            encoding="utf-8",
        )

    result = manager.reconcile()
    assert result["verdict"] == "gates_incomplete", "branch-less result must be rejected as stale"


def test_advance_fully_valid_gate_result_proceeds(roadmap_env, monkeypatch):
    """Fully-valid result (status pass + report on disk + contract_hash + matching project_id + branch) → advance proceeds."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )

    gate_results_dir = roadmap_env["state_dir"] / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch="feature/a")
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate", branch="feature/a")

    result = manager.advance()

    assert result["advanced"] is True, "fully-valid gate evidence must allow advance"
    assert result["reason"] == "loaded_next_feature"
    assert result["next_feature"] == "feature-b"


# ---------------------------------------------------------------------------
# RA-4: human approval gate
# ---------------------------------------------------------------------------


def _make_high_risk_roadmap(project_root: Path) -> Path:
    """Write a single high-risk human-policy feature roadmap."""
    plan_dir = project_root / "roadmap" / "features" / "feature-hi"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "FEATURE_PLAN.md").write_text(
        """# Feature: High Risk Feature

**Status**: Draft
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: High Risk PR
**Track**: C
**Priority**: P0
**Complexity**: High
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: []
""",
        encoding="utf-8",
    )
    roadmap_file = project_root / "ROADMAP_HR.yaml"
    roadmap_file.write_text(
        """features:
  - feature_id: feature-hi
    title: High Risk Feature
    plan_path: roadmap/features/feature-hi/FEATURE_PLAN.md
    branch_name: feature/hi
    risk_class: high
    merge_policy: human
    review_stack: [gemini_review, codex_gate]
    depends_on: []
    status: planned
""",
        encoding="utf-8",
    )
    return roadmap_file


def _setup_passing_gates(state_dir: Path, branch: str = "feature/a", project_id: str = "vnx-dev") -> None:
    gate_results_dir = state_dir / "review_gates" / "results"
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    _write_gate_result(gate_results_dir, "PR-0", "gemini_review", branch=branch, project_id=project_id)
    _write_gate_result(gate_results_dir, "PR-0", "codex_gate", branch=branch, project_id=project_id)


def test_advance_blocked_awaiting_human_approval(roadmap_env, monkeypatch):
    """high-risk feature: closure+gates pass but no approval token → awaiting_human_approval."""
    project_root = roadmap_env["project_root"]
    roadmap_file = _make_high_risk_roadmap(project_root)
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"], branch="feature/hi")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_file)
    manager.load_feature("feature-hi")

    result = manager.advance()

    assert result["advanced"] is False
    assert result["reason"] == "awaiting_human_approval"
    assert result["feature_id"] == "feature-hi"
    state = manager.load_state()
    assert state["current_active_feature"] == "feature-hi", "feature must not progress without token"


def test_advance_proceeds_after_approve_and_token_consumed(roadmap_env, monkeypatch):
    """After approve, advance progresses and the token is consumed (single-use)."""
    project_root = roadmap_env["project_root"]
    roadmap_file = _make_high_risk_roadmap(project_root)
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"], branch="feature/hi")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_file)
    manager.load_feature("feature-hi")

    # First attempt: blocked
    first = manager.advance()
    assert first["advanced"] is False
    assert first["reason"] == "awaiting_human_approval"

    # Issue approval
    token = manager.approve("feature-hi", actor="vincent", justification="looks good")
    assert token["consumed"] is False
    assert token["feature_id"] == "feature-hi"

    # Second attempt: should proceed
    result = manager.advance()
    assert result["advanced"] is True or result["reason"] == "no_remaining_features"

    # Token must be consumed (single-use)
    token_path = manager._approval_token_path("feature-hi")
    stored = json.loads(token_path.read_text(encoding="utf-8"))
    assert stored["consumed"] is True, "token must be consumed after advance"
    assert stored["consumed_at"] is not None

    # Third attempt with no new token: blocked again on whatever comes next,
    # but the old consumed token must not re-authorize.
    # Reload state to check feature was actually advanced.
    state = manager.load_state()
    assert "feature-hi" in state["merged_features"], "feature-hi must be marked merged"


def test_conditional_auto_low_advances_without_token(roadmap_env, monkeypatch):
    """conditional_auto + low risk feature advances without any approval token."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a")
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"])

    result = manager.advance()

    # feature-a is conditional_auto + low → no token required → advances
    assert result["advanced"] is True
    assert result["reason"] == "loaded_next_feature"
    assert result["next_feature"] == "feature-b"


def test_approval_token_project_id_stamped_and_feature_pinned(roadmap_env, monkeypatch):
    """ADR-007: token carries project_id; a token issued for feature-a cannot approve feature-b."""
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-x")
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])

    token = manager.approve("feature-a", actor="vincent", justification="ok")

    # Token must be stamped with the correct project_id
    assert token["project_id"] == "proj-x", "ADR-007: token must carry project_id"
    assert token["feature_id"] == "feature-a"

    # Attempting to load the token as approval for feature-b must return None
    valid_for_b = manager._load_valid_approval_token("feature-b")
    assert valid_for_b is None, "feature-a token must not authorize feature-b"

    # The token IS valid for feature-a
    valid_for_a = manager._load_valid_approval_token("feature-a")
    assert valid_for_a is not None, "token must be valid for feature-a"
    assert valid_for_a["consumed"] is False


def test_consumed_token_does_not_reauthorize(roadmap_env, monkeypatch):
    """A consumed token must not grant a second advance."""
    project_root = roadmap_env["project_root"]
    roadmap_file = _make_high_risk_roadmap(project_root)
    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"], branch="feature/hi")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_file)
    manager.load_feature("feature-hi")
    manager.approve("feature-hi", actor="vincent", justification="ok")

    # First advance consumes the token
    r1 = manager.advance()
    assert r1["advanced"] is True or r1["reason"] == "no_remaining_features"

    # Manually re-activate feature-hi to simulate a second advance attempt
    state = manager.load_state()
    state["current_active_feature"] = "feature-hi"
    state["merged_features"] = [f for f in state.get("merged_features", []) if f != "feature-hi"]
    for f in state.get("features", []):
        if f["feature_id"] == "feature-hi":
            f["status"] = "active"
    manager._save_state(state)

    r2 = manager.advance()
    assert r2["advanced"] is False
    assert r2["reason"] == "awaiting_human_approval", "consumed token must not reauthorize"


# ---------------------------------------------------------------------------
# RA-5: per-PR dispatch driver (step)
# ---------------------------------------------------------------------------


def test_step_dispatches_first_ready_pr(roadmap_env, monkeypatch):
    """step creates+promotes a dispatch for the first dependency-ready PR, returns dispatch_id+pr_id."""
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    result = manager.run_feature_step()

    assert result["status"] == "dispatched", f"expected dispatched, got: {result}"
    assert result["pr_id"] == "PR-0"
    assert result["dispatch_id"] is not None
    assert result["feature_id"] == "feature-a"

    # Dispatch must exist in pending/ (auto-approved via VNX_QUEUE_POPUP_ENABLED=0)
    dispatch_dir = roadmap_env["project_root"] / ".vnx-data" / "dispatches"
    pending_files = list((dispatch_dir / "pending").glob("*.md"))
    assert len(pending_files) == 1, "exactly one dispatch must be in pending/"


def test_step_second_call_no_ready_pr(roadmap_env, monkeypatch):
    """second step returns no_ready_pr when only PR is already in_progress."""
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    first = manager.run_feature_step()
    assert first["status"] == "dispatched"

    second = manager.run_feature_step()
    assert second["status"] == "no_ready_pr"
    assert second["feature_id"] == "feature-a"


def test_step_receipt_project_id_stamped(roadmap_env, monkeypatch):
    """ADR-007: roadmap_dispatch_step receipt carries project_id."""
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-step-test")
    receipts = []
    monkeypatch.setattr(rm, "emit_governance_receipt", lambda *a, **kw: receipts.append(kw))

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    manager.run_feature_step()

    step_receipt = next(
        (r for r in receipts if r.get("project_id") == "proj-step-test" and "dispatch_id" in r),
        None,
    )
    assert step_receipt is not None, "roadmap_dispatch_step receipt not emitted"
    assert step_receipt["pr_id"] == "PR-0"
    assert step_receipt["feature_id"] == "feature-a"


def test_step_no_active_feature_returns_status(roadmap_env, monkeypatch):
    """step with no active feature returns no_active_feature without side effects."""
    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    # intentionally do NOT load any feature

    result = manager.run_feature_step()

    assert result["status"] == "no_active_feature"


# ---------------------------------------------------------------------------
# RA-6: autopilot tick
# ---------------------------------------------------------------------------


def test_autopilot_tick_disabled_when_flag_unset(roadmap_env, monkeypatch):
    """VNX_ROADMAP_AUTOPILOT unset → tick returns disabled, no progression."""
    monkeypatch.delenv("VNX_ROADMAP_AUTOPILOT", raising=False)

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    result = manager.autopilot_tick()

    assert result["status"] == "disabled"
    state = manager.load_state()
    assert state["current_active_feature"] == "feature-a", "flag off must not cause progression"


def test_autopilot_tick_full_loop_advances_a_to_b(roadmap_env, monkeypatch):
    """Full simulated loop: tick1 dispatches PR, tick2 (queue drained + gates pass) advances A→B."""
    monkeypatch.setenv("VNX_ROADMAP_AUTOPILOT", "1")
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"])

    # feature-a is conditional_auto + low risk → no approval token required
    tick1 = manager.autopilot_tick()
    assert tick1["status"] == "stepped", f"expected stepped, got: {tick1}"
    assert tick1["pr_id"] == "PR-0"

    # PR-0 is now in_progress; queue is drained → second tick advances
    tick2 = manager.autopilot_tick()
    assert tick2["status"] == "advanced", f"expected advanced, got: {tick2}"

    state = manager.load_state()
    assert state["current_active_feature"] == "feature-b"
    assert "feature-a" in state["merged_features"]


def test_autopilot_tick_blocked_when_gates_incomplete(roadmap_env, monkeypatch):
    """Queue drained but gates incomplete (RA-3) → tick blocked, no advancement."""
    monkeypatch.setenv("VNX_ROADMAP_AUTOPILOT", "1")
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    # No gate results written → gates_incomplete

    tick1 = manager.autopilot_tick()
    assert tick1["status"] == "stepped"

    tick2 = manager.autopilot_tick()
    assert tick2["status"] == "blocked"
    assert tick2["reason"] == "gates_incomplete"
    state = manager.load_state()
    assert state["current_active_feature"] == "feature-a", "must not advance with incomplete gates"


def test_autopilot_tick_blocked_awaiting_human_approval(roadmap_env, monkeypatch):
    """High-risk feature: queue drained + gates pass but no approval token → tick blocked (RA-4)."""
    project_root = roadmap_env["project_root"]
    roadmap_file = _make_high_risk_roadmap(project_root)

    monkeypatch.setenv("VNX_ROADMAP_AUTOPILOT", "1")
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")

    monkeypatch.setattr(
        rm, "verify_closure",
        lambda **kwargs: {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123"}}},
    )
    _setup_passing_gates(roadmap_env["state_dir"], branch="feature/hi")

    manager = rm.RoadmapManager()
    manager.init_roadmap(roadmap_file)
    manager.load_feature("feature-hi", no_worktree=True)

    tick1 = manager.autopilot_tick()
    assert tick1["status"] == "stepped"

    tick2 = manager.autopilot_tick()
    assert tick2["status"] == "blocked"
    assert tick2["reason"] == "awaiting_human_approval"
    state = manager.load_state()
    assert state["current_active_feature"] == "feature-hi", "must not advance without approval token"
