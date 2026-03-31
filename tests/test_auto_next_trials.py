#!/usr/bin/env python3
"""Integration tests for auto-next trial harness and controlled certification.

PR-6 / gate_pr6_auto_next_trials

Proves the roadmap auto-next loop with controlled trial sequences:
- Feature A merges -> closure verifier passes -> next feature loads
- Blocking drift inserts a fix-up feature before continuing
- Full multi-feature trial path: A -> optional fix-up -> B
- Advance blocked when closure verification fails
- Advance blocked when review evidence is incomplete
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import roadmap_manager as rm
from review_contract import (
    Deliverable,
    DeterministicFinding,
    QualityGate,
    ReviewContract,
    TestEvidence,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _write_feature_plan(path: Path, title: str, branch: str, risk_class: str = "low") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# Feature: {title}

**Status**: Draft
**Risk-Class**: {risk_class}
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: {title} PR
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Skill**: @architect
**Risk-Class**: {risk_class}
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: []
""",
        encoding="utf-8",
    )


def _write_roadmap(
    roadmap_file: Path,
    features: List[Dict[str, Any]],
) -> None:
    """Write a ROADMAP.yaml with the given features."""
    roadmap_file.write_text(
        "features:\n"
        + "\n".join(
            f"""  - feature_id: {f['id']}
    title: {f['title']}
    plan_path: {f['plan_path']}
    branch_name: {f['branch']}
    risk_class: {f.get('risk_class', 'low')}
    merge_policy: human
    review_stack: [gemini_review, codex_gate, claude_github_optional]
    depends_on: {json.dumps(f.get('depends_on', []))}
    status: planned"""
            for f in features
        ),
        encoding="utf-8",
    )


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    """Comprehensive trial environment with three features (A depends on nothing, B depends on A, C depends on B)."""
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
    for fid, title, branch in [
        ("feature-a", "Feature Alpha", "feature/alpha"),
        ("feature-b", "Feature Bravo", "feature/bravo"),
        ("feature-c", "Feature Charlie", "feature/charlie"),
    ]:
        _write_feature_plan(roadmap_dir / fid / "FEATURE_PLAN.md", title, branch)

    roadmap_file = project_root / "ROADMAP.yaml"
    _write_roadmap(
        roadmap_file,
        [
            {"id": "feature-a", "title": "Feature Alpha", "plan_path": "roadmap/features/feature-a/FEATURE_PLAN.md", "branch": "feature/alpha", "depends_on": []},
            {"id": "feature-b", "title": "Feature Bravo", "plan_path": "roadmap/features/feature-b/FEATURE_PLAN.md", "branch": "feature/bravo", "depends_on": ["feature-a"]},
            {"id": "feature-c", "title": "Feature Charlie", "plan_path": "roadmap/features/feature-c/FEATURE_PLAN.md", "branch": "feature/charlie", "depends_on": ["feature-b"]},
        ],
    )

    return {
        "project_root": project_root,
        "roadmap_file": roadmap_file,
        "state_dir": state_dir,
        "data_dir": data_dir,
    }


def _closure_pass(**kwargs):
    """Simulate a passing closure verification with merge evidence."""
    return {"verdict": "pass", "pr": {"mergeCommit": {"oid": "abc123def456"}}}


def _closure_fail(**kwargs):
    """Simulate a failing closure verification (missing evidence)."""
    return {
        "verdict": "fail",
        "pr": None,
        "checks": [{"name": "review_contract", "status": "FAIL", "detail": "missing"}],
    }


# ---------------------------------------------------------------------------
# Gate criterion 1: Auto-next advances ONLY after merged-to-main + green
#                   checks + closure verifier pass
# ---------------------------------------------------------------------------


class TestAutoNextAdvancementGating:
    """Verify that auto-next respects all three gating conditions."""

    def test_advance_blocked_when_closure_verification_fails(self, trial_env, monkeypatch):
        """advance() must NOT load next feature when closure verifier returns 'fail'."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")

        monkeypatch.setattr(rm, "verify_closure", _closure_fail)

        result = manager.advance()
        state = manager.load_state()

        assert result["advanced"] is False
        assert result["reason"] == "closure_verification_failed"
        assert state["current_active_feature"] == "feature-a"
        assert "feature-a" not in state.get("merged_features", [])

    def test_advance_succeeds_when_closure_passes(self, trial_env, monkeypatch):
        """advance() loads next feature when closure verifier returns 'pass'."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")

        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        result = manager.advance()
        state = manager.load_state()

        assert result["advanced"] is True
        assert result["reason"] == "loaded_next_feature"
        assert result["next_feature"] == "feature-b"
        assert state["current_active_feature"] == "feature-b"
        assert "feature-a" in state["merged_features"]

    def test_advance_records_merge_commit_on_pass(self, trial_env, monkeypatch):
        """Successful reconciliation records the merge commit OID in state."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")

        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        manager.advance()
        state = manager.load_state()

        assert state["last_verified_merge_commit"] == "abc123def456"

    def test_advance_does_not_skip_dependency_chain(self, trial_env, monkeypatch):
        """Feature C cannot load before Feature B is merged (dependency enforcement)."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        result_ab = manager.advance()
        assert result_ab["next_feature"] == "feature-b"

        result_bc = manager.advance()
        assert result_bc["next_feature"] == "feature-c"

        state = manager.load_state()
        assert state["current_active_feature"] == "feature-c"
        assert set(state["merged_features"]) == {"feature-a", "feature-b"}

    def test_advance_returns_no_remaining_after_all_merged(self, trial_env, monkeypatch):
        """After all features are merged, advance() returns no_remaining_features."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        manager.advance()  # A -> B
        manager.advance()  # B -> C
        result = manager.advance()  # C -> done

        assert result["advanced"] is False
        assert result["reason"] == "no_remaining_features"
        state = manager.load_state()
        assert state["current_active_feature"] is None
        assert set(state["merged_features"]) == {"feature-a", "feature-b", "feature-c"}

    def test_repeated_advance_on_failed_closure_stays_blocked(self, trial_env, monkeypatch):
        """Calling advance() multiple times while closure fails does not mutate state."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_fail)

        for _ in range(3):
            result = manager.advance()
            assert result["advanced"] is False

        state = manager.load_state()
        assert state["current_active_feature"] == "feature-a"
        assert state["merged_features"] == []


# ---------------------------------------------------------------------------
# Gate criterion 2: Blocking drift inserts fix-up feature before continuing
# ---------------------------------------------------------------------------


class TestBlockingDriftFixup:
    """Verify that blocking drift inserts a fix-up feature and loads it."""

    def _inject_drift(self, state_dir: Path, items: List[Dict[str, Any]]) -> None:
        (state_dir / "post_feature_drift.json").write_text(
            json.dumps({"items": items}),
            encoding="utf-8",
        )

    def test_blocking_drift_inserts_fixup_before_next_feature(self, trial_env, monkeypatch):
        """When blocking drift is detected after closure pass, a fixup is inserted and loaded."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        self._inject_drift(trial_env["state_dir"], [
            {"id": "oi-1", "title": "Fix path regression", "category": "path/runtime regression", "blocking": True},
        ])

        result = manager.advance()
        state = manager.load_state()

        assert result["advanced"] is True
        assert result["reason"] == "blocking_fixup_inserted"
        assert state["current_active_feature"].startswith("fixup-")
        assert len(state["inserted_fixups"]) == 1
        assert state["blocked_reason"] is None  # fixup is now active, not blocked

    def test_fixup_feature_plan_exists_on_disk(self, trial_env, monkeypatch):
        """Inserted fixup generates an actual FEATURE_PLAN.md on disk."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        self._inject_drift(trial_env["state_dir"], [
            {"id": "oi-2", "title": "Fix governance gap", "category": "governance_gap", "blocking": True},
        ])

        manager.advance()
        state = manager.load_state()

        fixup_id = state["inserted_fixups"][0]["feature_id"]
        plan_path = Path(state["inserted_fixups"][0]["plan_path"])
        assert plan_path.exists()
        content = plan_path.read_text(encoding="utf-8")
        assert fixup_id in content
        assert "Fix governance gap" in content

    def test_non_blocking_drift_does_not_insert_fixup(self, trial_env, monkeypatch):
        """Non-blocking drift items are ignored — advance proceeds normally."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            json.dumps({"items": [
                {"id": "oi-3", "title": "Minor cleanup", "category": "bugfix", "blocking": False},
            ]}),
            encoding="utf-8",
        )

        result = manager.advance()
        assert result["reason"] == "loaded_next_feature"
        assert result["next_feature"] == "feature-b"

    def test_invalid_drift_category_ignored(self, trial_env, monkeypatch):
        """Drift items with categories outside the allowed set are ignored."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        self._inject_drift(trial_env["state_dir"], [
            {"id": "oi-4", "title": "Unrecognized category", "category": "unknown_category", "blocking": True},
        ])

        result = manager.advance()
        assert result["reason"] == "loaded_next_feature"

    def test_open_items_blocker_triggers_fixup(self, trial_env, monkeypatch):
        """Blocking drift from open_items.json (blocker severity) also triggers fixup."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "open_items.json").write_text(
            json.dumps({"items": [
                {"id": "oi-5", "title": "Runtime hang", "category": "bugfix", "status": "open", "severity": "blocker"},
            ]}),
            encoding="utf-8",
        )

        result = manager.advance()
        assert result["advanced"] is True
        assert result["reason"] == "blocking_fixup_inserted"


# ---------------------------------------------------------------------------
# Gate criterion 3: Controlled multi-feature trial path
# ---------------------------------------------------------------------------


class TestMultiFeatureTrialPath:
    """End-to-end trial: Feature A -> optional fix-up -> Feature B advancement."""

    def test_full_trial_a_fixup_b(self, trial_env, monkeypatch):
        """Complete controlled trial: A closure passes -> drift detected -> fixup inserted ->
        fixup merges -> A re-verified (safety re-check) -> A merges -> B loads -> B merges -> C loads.

        Key safety property: when drift is detected after A's closure passes,
        A is NOT marked as merged. After the fixup completes, A is re-loaded
        for re-verification because the environment changed during drift.
        """
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        # Step 1: Advance from A — drift detected, fixup inserted (A not merged)
        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            json.dumps({"items": [
                {"id": "drift-1", "title": "Fix runtime regression", "category": "path/runtime regression", "blocking": True},
            ]}),
            encoding="utf-8",
        )

        result_a = manager.advance()
        assert result_a["reason"] == "blocking_fixup_inserted"
        fixup_id = result_a["next_feature"]
        state = manager.load_state()
        assert state["current_active_feature"] == fixup_id
        assert "feature-a" not in state.get("merged_features", [])  # A NOT merged

        # Step 2: Clear drift and advance from fixup -> A re-loaded (not B!)
        (trial_env["state_dir"] / "post_feature_drift.json").unlink()

        result_fixup = manager.advance()
        assert result_fixup["advanced"] is True
        assert result_fixup["reason"] == "loaded_next_feature"
        assert result_fixup["next_feature"] == "feature-a"  # safety re-check
        state = manager.load_state()
        assert fixup_id in state["merged_features"]
        assert state["current_active_feature"] == "feature-a"

        # Step 3: A re-verified and merges -> B loads
        result_a2 = manager.advance()
        assert result_a2["advanced"] is True
        assert result_a2["next_feature"] == "feature-b"
        assert "feature-a" in manager.load_state()["merged_features"]

        # Step 4: B merges -> C loads
        result_b = manager.advance()
        assert result_b["advanced"] is True
        assert result_b["next_feature"] == "feature-c"

    def test_full_trial_a_b_c_no_drift(self, trial_env, monkeypatch):
        """Clean trial path: A -> B -> C -> done with no drift."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        results = []
        for _ in range(3):
            results.append(manager.advance())

        assert results[0]["next_feature"] == "feature-b"
        assert results[1]["next_feature"] == "feature-c"
        assert results[2]["reason"] == "no_remaining_features"

        state = manager.load_state()
        assert set(state["merged_features"]) == {"feature-a", "feature-b", "feature-c"}
        assert state["current_active_feature"] is None

    def test_trial_blocked_then_unblocked(self, trial_env, monkeypatch):
        """Advance fails (blocked), then succeeds after conditions are met."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")

        # Phase 1: closure fails
        monkeypatch.setattr(rm, "verify_closure", _closure_fail)
        result_blocked = manager.advance()
        assert result_blocked["advanced"] is False
        assert manager.load_state()["current_active_feature"] == "feature-a"

        # Phase 2: closure passes
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)
        result_ok = manager.advance()
        assert result_ok["advanced"] is True
        assert result_ok["next_feature"] == "feature-b"

    def test_multiple_drift_items_consolidated_in_single_fixup(self, trial_env, monkeypatch):
        """Multiple blocking drift items produce one fixup feature, not many."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            json.dumps({"items": [
                {"id": "d1", "title": "Regression A", "category": "bugfix", "blocking": True},
                {"id": "d2", "title": "Regression B", "category": "post_cleanup", "blocking": True},
            ]}),
            encoding="utf-8",
        )

        result = manager.advance()
        state = manager.load_state()
        assert result["reason"] == "blocking_fixup_inserted"
        assert len(state["inserted_fixups"]) == 1
        plan_content = Path(state["inserted_fixups"][0]["plan_path"]).read_text(encoding="utf-8")
        assert "Regression A" in plan_content
        assert "Regression B" in plan_content


# ---------------------------------------------------------------------------
# Edge cases and robustness
# ---------------------------------------------------------------------------


class TestAutoNextEdgeCases:
    """Edge case coverage for robustness."""

    def test_advance_with_no_active_feature_loads_first_eligible(self, trial_env, monkeypatch):
        """advance() on idle roadmap loads the first eligible feature."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        result = manager.advance()
        assert result["advanced"] is True
        assert result["next_feature"] == "feature-a"

    def test_reconcile_returns_idle_when_no_active_feature(self, trial_env):
        """reconcile() with no active feature returns idle verdict and persists state."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])

        result = manager.reconcile()
        assert result["verdict"] == "idle"
        assert result["reason"] == "no_active_feature"

        state = manager.load_state()
        assert state["last_closure_verification_result"]["verdict"] == "idle"

    def test_feature_status_transitions_are_correct(self, trial_env, monkeypatch):
        """Verify feature status transitions: planned -> active -> merged."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        manager.load_feature("feature-a")
        state = manager.load_state()
        feature_a = next(f for f in state["features"] if f["feature_id"] == "feature-a")
        assert feature_a["status"] == "active"

        manager.advance()  # A -> B
        state = manager.load_state()
        feature_a = next(f for f in state["features"] if f["feature_id"] == "feature-a")
        feature_b = next(f for f in state["features"] if f["feature_id"] == "feature-b")
        assert feature_a["status"] == "merged"
        assert feature_b["status"] == "active"

    def test_malformed_drift_json_is_ignored(self, trial_env, monkeypatch):
        """Malformed post_feature_drift.json does not crash — treated as no drift."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            "not valid json {{{",
            encoding="utf-8",
        )

        result = manager.advance()
        assert result["reason"] == "loaded_next_feature"

    def test_fixup_feature_is_marked_as_inserted(self, trial_env, monkeypatch):
        """Fixup features have inserted=True in the features list."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            json.dumps({"items": [
                {"id": "d1", "title": "Fix it", "category": "bugfix", "blocking": True},
            ]}),
            encoding="utf-8",
        )

        manager.advance()
        state = manager.load_state()
        fixup = next(f for f in state["features"] if f.get("inserted"))
        assert fixup["inserted"] is True
        assert fixup["feature_id"].startswith("fixup-")

    def test_duplicate_drift_items_are_deduplicated(self, trial_env, monkeypatch):
        """Duplicate drift items (same id/title/category) are consolidated."""
        manager = rm.RoadmapManager()
        manager.init_roadmap(trial_env["roadmap_file"])
        manager.load_feature("feature-a")
        monkeypatch.setattr(rm, "verify_closure", _closure_pass)

        (trial_env["state_dir"] / "post_feature_drift.json").write_text(
            json.dumps({"items": [
                {"id": "d1", "title": "Same issue", "category": "bugfix", "blocking": True},
                {"id": "d1", "title": "Same issue", "category": "bugfix", "blocking": True},
            ]}),
            encoding="utf-8",
        )

        manager.advance()
        state = manager.load_state()
        fixup_items = state["inserted_fixups"][0]["items"]
        assert len(fixup_items) == 1
