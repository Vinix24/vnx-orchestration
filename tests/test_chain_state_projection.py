#!/usr/bin/env python3
"""Tests for chain state projection layer (PR-1, Feature 14).

Covers:
  - build_chain_projection: FEATURE_ACTIVE, advancement, blocked, recovery-needed states
  - compute_advancement_truth: requires merged PR AND gate certification
  - carry-forward summary and unresolved chain items surface
  - init_chain_state and record_state_transition lifecycle
  - audit trail append
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from chain_state_projection import (
    BLOCKED_STATES,
    RECOVERY_NEEDED_STATES,
    build_carry_forward_summary,
    build_chain_projection,
    compute_advancement_truth,
    init_chain_state,
    record_state_transition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Return a state directory pre-populated with a minimal pr_queue_state.json."""
    (tmp_path / "review_gates" / "results").mkdir(parents=True)
    return tmp_path


def _write_pr_queue(state_dir: Path, prs: list[dict]) -> None:
    record = {
        "feature": "Test Feature",
        "feature_metadata": {},
        "prs": prs,
    }
    (state_dir / "pr_queue_state.json").write_text(json.dumps(record))


def _write_open_items(state_dir: Path, items: list[dict]) -> None:
    (state_dir / "open_items.json").write_text(
        json.dumps({"schema_version": "1", "items": items, "next_id": len(items) + 1})
    )


def _write_gate_result(state_dir: Path, pr_num: int, gate: str, status: str,
                        blocking: int = 0, contract_hash: str = "abc123") -> None:
    result = {
        "gate": gate,
        "pr_number": pr_num,
        "pr_id": str(pr_num),
        "status": status,
        "blocking_count": blocking,
        "contract_hash": contract_hash,
        "report_path": f"/tmp/report-{pr_num}-{gate}.md",
        "recorded_at": "2026-04-02T10:00:00Z",
    }
    path = state_dir / "review_gates" / "results" / f"pr-{pr_num}-{gate}.json"
    path.write_text(json.dumps(result))


def _write_carry_forward(state_dir: Path, data: dict) -> None:
    (state_dir / "chain_carry_forward.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Tests: chain state derivation
# ---------------------------------------------------------------------------

class TestChainStateDerivation:
    def test_not_initialized_when_no_chain_state_file(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        projection = build_chain_projection(state_dir)
        # Without chain_state.json, state is derived from pr_queue
        assert projection["chain_state"] in {"FEATURE_ACTIVE", "NOT_INITIALIZED"}
        assert projection["active_feature"]["id"] == "PR-1"

    def test_feature_active_when_chain_state_set(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        init_chain_state(
            state_dir,
            chain_id="chain-001",
            feature_plan="FEATURE_PLAN.md",
            feature_sequence=["PR-0", "PR-1"],
            chain_origin_sha="deadbeef",
        )
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1", actor="T0", reason="PR-0 merged")
        projection = build_chain_projection(state_dir)

        assert projection["chain_state"] == "FEATURE_ACTIVE"
        assert projection["is_blocked"] is False
        assert projection["is_recovery_needed"] is False
        assert projection["active_feature"]["id"] == "PR-1"

    def test_advancement_blocked_state_is_blocked(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        init_chain_state(state_dir, chain_id="chain-002", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0", "PR-1"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="ADVANCEMENT_BLOCKED", feature_id="PR-1",
                                  actor="T0", reason="blocker open items")
        projection = build_chain_projection(state_dir)

        assert projection["chain_state"] == "ADVANCEMENT_BLOCKED"
        assert projection["is_blocked"] is True
        assert projection["is_recovery_needed"] is False

    def test_recovery_pending_is_recovery_needed(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        init_chain_state(state_dir, chain_id="chain-003", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0", "PR-1"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="RECOVERY_PENDING", feature_id="PR-1",
                                  actor="T0", reason="dispatch failed")
        projection = build_chain_projection(state_dir)

        assert projection["chain_state"] == "RECOVERY_PENDING"
        assert projection["is_blocked"] is True
        assert projection["is_recovery_needed"] is True

    def test_chain_halted_is_blocked_and_recovery_needed(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
        ])
        init_chain_state(state_dir, chain_id="chain-004", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="CHAIN_HALTED", feature_id="PR-0",
                                  actor="T0", reason="max retries exceeded")
        projection = build_chain_projection(state_dir)

        assert projection["chain_state"] == "CHAIN_HALTED"
        assert projection["is_blocked"] is True
        assert projection["is_recovery_needed"] is True

    def test_chain_complete_when_all_prs_done(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "completed", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        init_chain_state(state_dir, chain_id="chain-005", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0", "PR-1"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="CHAIN_COMPLETE", feature_id="PR-1", actor="T0", reason="all done")
        projection = build_chain_projection(state_dir)

        assert projection["chain_state"] == "CHAIN_COMPLETE"
        assert projection["is_blocked"] is False

    def test_all_states_are_distinguishable(self, state_dir: Path) -> None:
        """Verify BLOCKED_STATES and RECOVERY_NEEDED_STATES are proper subsets."""
        assert BLOCKED_STATES.issubset({"ADVANCEMENT_BLOCKED", "CHAIN_HALTED", "FEATURE_FAILED", "RECOVERY_PENDING"})
        assert RECOVERY_NEEDED_STATES.issubset({"RECOVERY_PENDING", "CHAIN_HALTED"})
        # Non-blocked states must not appear in BLOCKED_STATES
        for state in {"FEATURE_ACTIVE", "FEATURE_ADVANCING", "CHAIN_COMPLETE", "INITIALIZED"}:
            assert state not in BLOCKED_STATES


# ---------------------------------------------------------------------------
# Tests: advancement truth
# ---------------------------------------------------------------------------

class TestAdvancementTruth:
    def test_cannot_advance_when_pr_not_completed(self, state_dir: Path) -> None:
        pr_queue = {
            "prs": [
                {"id": "PR-0", "status": "completed", "dependencies": []},
                {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"]},
            ]
        }
        result = compute_advancement_truth(
            pr_queue=pr_queue,
            open_items=[],
            state_dir=state_dir,
            current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert any("not yet merged" in b for b in result["blockers"])

    def test_cannot_advance_when_gate_missing(self, state_dir: Path) -> None:
        """Advancement requires gate certification — not just PR completion."""
        pr_queue = {
            "prs": [
                {"id": "PR-1", "status": "completed", "dependencies": []},
            ]
        }
        # No gate result files written
        result = compute_advancement_truth(
            pr_queue=pr_queue,
            open_items=[],
            state_dir=state_dir,
            current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert result["certification_status"]["gemini_review"] == "missing"
        assert result["certification_status"]["codex_gate"] == "missing"
        assert any("gemini_review" in b for b in result["blockers"])
        assert any("codex_gate" in b for b in result["blockers"])

    def test_cannot_advance_when_gate_not_certified(self, state_dir: Path) -> None:
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        _write_gate_result(state_dir, 1, "gemini_review", status="reject", blocking=1)
        _write_gate_result(state_dir, 1, "codex_gate", status="approve", contract_hash="abc")
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is False
        assert "not_certified" in result["certification_status"]["gemini_review"]
        assert result["certification_status"]["codex_gate"] == "certified"

    def test_cannot_advance_with_blocker_open_item(self, state_dir: Path) -> None:
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        _write_gate_result(state_dir, 1, "gemini_review", status="approve")
        _write_gate_result(state_dir, 1, "codex_gate", status="approve")
        blocker = {"id": "OI-999", "severity": "blocker", "status": "open", "title": "Critical bug"}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[blocker], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is False
        assert any("blocker" in b for b in result["blockers"])

    def test_can_advance_when_all_conditions_met(self, state_dir: Path) -> None:
        """Advancement truth is true only when PR merged AND gates certified AND no blockers."""
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        _write_gate_result(state_dir, 1, "gemini_review", status="approve")
        _write_gate_result(state_dir, 1, "codex_gate", status="approve")
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is True
        assert result["blockers"] == []
        assert result["certification_status"]["gemini_review"] == "certified"
        assert result["certification_status"]["codex_gate"] == "certified"

    def test_advancement_does_not_rely_on_operator_memory(self, state_dir: Path) -> None:
        """Without any state files, advancement truth defaults to False with explicit blockers."""
        result = compute_advancement_truth(
            pr_queue={}, open_items=[], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is False
        assert len(result["blockers"]) > 0  # blockers are explicit, not implicit

    def test_no_current_feature_blocks_advancement(self, state_dir: Path) -> None:
        result = compute_advancement_truth(
            pr_queue={}, open_items=[], state_dir=state_dir, current_feature_id=None
        )
        assert result["can_advance"] is False
        assert any("no active feature" in b for b in result["blockers"])

    def test_done_open_item_does_not_block_advancement(self, state_dir: Path) -> None:
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        _write_gate_result(state_dir, 1, "gemini_review", status="approve")
        _write_gate_result(state_dir, 1, "codex_gate", status="approve")
        done_item = {"id": "OI-001", "severity": "blocker", "status": "done", "title": "Resolved"}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[done_item], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is True


# ---------------------------------------------------------------------------
# Tests: carry-forward and unresolved chain items
# ---------------------------------------------------------------------------

class TestCarryForwardSurface:
    def test_carry_forward_summary_counts_findings(self) -> None:
        cf = {
            "findings": [
                {"severity": "warn", "resolution_status": "open"},
                {"severity": "blocker", "resolution_status": "open"},
                {"severity": "info", "resolution_status": "resolved"},
            ],
            "open_items": [],
            "residual_risks": [{"risk": "perf"}],
            "feature_summaries": [{"feature_id": "PR-0"}],
        }
        summary = build_carry_forward_summary(cf, open_items=[])
        assert summary["total_findings"] == 3
        assert summary["open_findings"] == 2
        assert summary["blocker_findings"] == 1
        assert summary["residual_risks"] == 1
        assert summary["feature_summaries_count"] == 1

    def test_unresolved_chain_items_visible_in_projection(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
        ])
        _write_open_items(state_dir, [
            {"id": "OI-001", "severity": "warn", "status": "open", "title": "Perf concern", "pr_id": "PR-0"},
            {"id": "OI-002", "severity": "blocker", "status": "done", "title": "Resolved", "pr_id": "PR-0"},
        ])
        projection = build_chain_projection(state_dir)
        items = projection["unresolved_chain_items"]
        ids = [i["id"] for i in items]
        assert "OI-001" in ids       # open warn should appear
        assert "OI-002" not in ids   # done blocker should NOT appear

    def test_carry_forward_ledger_items_visible(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _write_carry_forward(state_dir, {
            "chain_id": "cid",
            "open_items": [
                {"id": "CF-001", "severity": "warn", "status": "open", "title": "Carry item", "origin_feature": "PR-0"},
            ],
            "findings": [],
            "deferred_items": [],
            "residual_risks": [],
            "feature_summaries": [],
        })
        projection = build_chain_projection(state_dir)
        items = projection["unresolved_chain_items"]
        sources = {i["source"] for i in items}
        ids = {i["id"] for i in items}
        assert "CF-001" in ids
        assert "carry_forward" in sources

    def test_empty_state_has_no_unresolved_items(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [])
        _write_open_items(state_dir, [])
        projection = build_chain_projection(state_dir)
        assert projection["unresolved_chain_items"] == []
        assert projection["carry_forward_summary"]["live_unresolved_items"] == 0


# ---------------------------------------------------------------------------
# Tests: next feature in sequence
# ---------------------------------------------------------------------------

class TestNextFeature:
    def test_next_feature_is_identified(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T0", "track": "C", "gate": "g0"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T1", "track": "B", "gate": "g1"},
            {"id": "PR-2", "status": "queued", "dependencies": ["PR-1"], "title": "T2", "track": "B", "gate": "g2"},
        ])
        init_chain_state(state_dir, chain_id="c1", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0", "PR-1", "PR-2"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1")
        projection = build_chain_projection(state_dir)
        assert projection["active_feature"]["id"] == "PR-1"
        assert projection["next_feature"]["id"] == "PR-2"

    def test_no_next_feature_at_chain_end(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        init_chain_state(state_dir, chain_id="c2", feature_plan="FEATURE_PLAN.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="CHAIN_COMPLETE", feature_id="PR-0")
        projection = build_chain_projection(state_dir)
        assert projection["next_feature"] is None


# ---------------------------------------------------------------------------
# Tests: state lifecycle and audit trail
# ---------------------------------------------------------------------------

class TestStateLifecycle:
    def test_init_creates_initialized_state(self, state_dir: Path) -> None:
        record = init_chain_state(
            state_dir,
            chain_id="test-chain",
            feature_plan="FEATURE_PLAN.md",
            feature_sequence=["PR-0", "PR-1"],
            chain_origin_sha="abcdef12",
        )
        assert record["current_state"] == "INITIALIZED"
        assert record["chain_id"] == "test-chain"
        assert (state_dir / "chain_state.json").exists()

    def test_transition_updates_state_file(self, state_dir: Path) -> None:
        init_chain_state(state_dir, chain_id="t1", feature_plan="FP.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        record = record_state_transition(
            state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0", actor="T0", reason="dispatch sent"
        )
        assert record["current_state"] == "FEATURE_ACTIVE"
        assert record["current_feature_id"] == "PR-0"

    def test_audit_trail_is_appended(self, state_dir: Path) -> None:
        init_chain_state(state_dir, chain_id="audit-test", feature_plan="FP.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0")
        record_state_transition(state_dir, to_state="FEATURE_ADVANCING", feature_id="PR-0")

        audit_path = state_dir / "chain_audit.jsonl"
        assert audit_path.exists()
        lines = [json.loads(l) for l in audit_path.read_text().strip().splitlines()]
        # init + 2 transitions = 3 records
        assert len(lines) == 3
        states = [l["to_state"] for l in lines]
        assert "INITIALIZED" in states
        assert "FEATURE_ACTIVE" in states
        assert "FEATURE_ADVANCING" in states

    def test_requeue_history_increments_on_recovery(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "queued", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        init_chain_state(state_dir, chain_id="requeue-test", feature_plan="FP.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0")
        record_state_transition(state_dir, to_state="FEATURE_FAILED", feature_id="PR-0", reason="ci failed")
        record_state_transition(state_dir, to_state="RECOVERY_PENDING", feature_id="PR-0")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0", reason="requeue attempt 1")

        projection = build_chain_projection(state_dir)
        assert projection["requeue_history"].get("PR-0", {}).get("total_attempts", 0) >= 1

    def test_invalid_state_raises_value_error(self, state_dir: Path) -> None:
        init_chain_state(state_dir, chain_id="err-test", feature_plan="FP.md",
                          feature_sequence=["PR-0"], chain_origin_sha="")
        with pytest.raises(ValueError, match="Invalid chain state"):
            record_state_transition(state_dir, to_state="INVALID_STATE")

    def test_projection_works_with_no_state_files(self, state_dir: Path) -> None:
        """Projection must not crash when all state files are absent."""
        projection = build_chain_projection(state_dir)
        assert "chain_state" in projection
        assert "advancement_truth" in projection
        assert "carry_forward_summary" in projection
        assert "unresolved_chain_items" in projection
        assert isinstance(projection["unresolved_chain_items"], list)
