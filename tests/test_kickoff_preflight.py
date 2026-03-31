#!/usr/bin/env python3
"""Tests for kickoff preflight reconciliation (PR-2).

Covers:
- Kickoff preflight runs reconciliation before promotion
- Stale queue state is surfaced explicitly
- Blocking drift halts promotion
- Per-PR status check during preflight
- Repair mode auto-fixes projections
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "lib"))

from kickoff_preflight import run_preflight


FEATURE_PLAN = """\
# Feature: Test Kickoff Preflight

**Status**: Active
**Risk-Class**: high

## PR-0: Foundation
**Track**: C
**Priority**: P1
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: []

`gate_pr0_foundation`

---

## PR-1: Core
**Track**: B
**Priority**: P1
**Skill**: @backend-developer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: [PR-0]

`gate_pr1_core`

---

## PR-2: Integration
**Track**: C
**Priority**: P2
**Skill**: @quality-engineer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: [PR-1]

`gate_pr2_integration`

---
"""


def _write_dispatch(path: Path, pr_id: str, dispatch_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[[TARGET:B]]\nManager Block\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n"
    )


def _write_receipt(receipts_file: Path, dispatch_id: str) -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"dispatch_id": dispatch_id, "event_type": "task_complete", "status": "success"}
    with receipts_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _write_projection(state_dir: Path, pr_states: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    prs = []
    completed, active, blocked = [], [], []
    status_map = {"completed": "completed", "active": "in_progress", "pending": "queued", "blocked": "blocked"}
    for pr_id, state in pr_states.items():
        prs.append({"id": pr_id, "title": pr_id, "status": status_map.get(state, state)})
        if state == "completed":
            completed.append(pr_id)
        elif state == "active":
            active.append(pr_id)
        elif state == "blocked":
            blocked.append(pr_id)
    payload = {"prs": prs, "completed": completed, "active": active, "blocked": blocked}
    (state_dir / "pr_queue_state.json").write_text(json.dumps(payload))


@pytest.fixture
def env(tmp_path):
    fp = tmp_path / "FEATURE_PLAN.md"
    fp.write_text(FEATURE_PLAN)
    dispatch_dir = tmp_path / "dispatches"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts = tmp_path / "state" / "t0_receipts.ndjson"
    return {
        "project_root": tmp_path,
        "feature_plan": fp,
        "dispatch_dir": dispatch_dir,
        "state_dir": state_dir,
        "receipts_file": receipts,
    }


class TestPreflightSafeWhenFresh:
    def test_no_dispatches_is_safe(self, env):
        result = run_preflight(**env)
        assert result["safe_to_promote"] is True
        assert result["error"] is None
        assert result["blocking_drift"] == []

    def test_completed_dispatch_matches_projection(self, env):
        _write_dispatch(
            env["dispatch_dir"] / "completed" / "d1.md", "PR-0", "d1"
        )
        _write_projection(env["state_dir"], {"PR-0": "completed", "PR-1": "blocked", "PR-2": "blocked"})
        result = run_preflight(**env)
        assert result["safe_to_promote"] is True


class TestPreflightBlocksOnStaleness:
    def test_active_but_projected_pending_blocks(self, env):
        _write_dispatch(
            env["dispatch_dir"] / "active" / "d1.md", "PR-0", "d1"
        )
        _write_projection(env["state_dir"], {"PR-0": "pending", "PR-1": "blocked", "PR-2": "blocked"})
        result = run_preflight(**env)
        assert result["safe_to_promote"] is False
        assert len(result["blocking_drift"]) >= 1
        assert result["blocking_drift"][0]["pr_id"] == "PR-0"

    def test_completed_but_projected_active_blocks(self, env):
        _write_dispatch(
            env["dispatch_dir"] / "completed" / "d1.md", "PR-0", "d1"
        )
        _write_projection(env["state_dir"], {"PR-0": "active", "PR-1": "blocked", "PR-2": "blocked"})
        result = run_preflight(**env)
        assert result["safe_to_promote"] is False
        blocking = [w for w in result["blocking_drift"] if w["pr_id"] == "PR-0"]
        assert len(blocking) == 1

    def test_missing_feature_plan_returns_error(self, env):
        env["feature_plan"] = env["project_root"] / "NONEXISTENT.md"
        result = run_preflight(**env)
        assert result["safe_to_promote"] is False
        assert "not found" in result["error"]


class TestPreflightPRSpecific:
    def test_pr_status_returned_when_requested(self, env):
        _write_dispatch(
            env["dispatch_dir"] / "active" / "d1.md", "PR-0", "d1"
        )
        result = run_preflight(**env, pr_id="PR-0")
        assert result["pr_status"] is not None
        assert result["pr_status"]["pr_id"] == "PR-0"
        assert result["pr_status"]["state"] == "active"

    def test_unknown_pr_id_returns_error(self, env):
        result = run_preflight(**env, pr_id="PR-99")
        assert result["safe_to_promote"] is False
        assert len(result["blocking_drift"]) >= 1
        assert "PR-99" in result["blocking_drift"][0]["message"]


class TestPreflightRepairMode:
    def test_repair_fixes_stale_projection(self, env):
        _write_dispatch(
            env["dispatch_dir"] / "completed" / "d1.md", "PR-0", "d1"
        )
        _write_projection(env["state_dir"], {"PR-0": "active", "PR-1": "blocked", "PR-2": "blocked"})

        result = run_preflight(**env, repair=True)
        # Still reports blocking drift (pre-repair state)
        assert result["safe_to_promote"] is False

        # But projection is now repaired
        repaired = json.loads((env["state_dir"] / "pr_queue_state.json").read_text())
        assert "PR-0" in repaired.get("completed", [])


class TestPreflightMultiFeature:
    def test_stale_projection_during_active_dispatch(self, env):
        """Multi-feature scenario: active dispatch exists but projection is stale from previous run."""
        _write_dispatch(
            env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_dispatch(
            env["dispatch_dir"] / "active" / "d1.md", "PR-1", "d1"
        )
        # Stale projection: shows PR-1 as blocked (hasn't been updated since PR-0 completed)
        _write_projection(env["state_dir"], {"PR-0": "completed", "PR-1": "blocked", "PR-2": "blocked"})

        result = run_preflight(**env)
        assert result["safe_to_promote"] is False
        # PR-1 drift: derived=active, projected=blocked -> blocking
        blocking = [w for w in result["blocking_drift"] if w["pr_id"] == "PR-1"]
        assert len(blocking) >= 1
