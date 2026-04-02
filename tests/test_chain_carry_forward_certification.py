#!/usr/bin/env python3
"""Chain carry-forward certification tests (PR-3, Feature 14).

Certifies that chain-level findings, residual risks, and open items survive
across feature boundaries and remain visible until properly closed or
deliberately carried forward.

Covers:
  - Findings persist across feature boundaries (F-1 through F-5)
  - Blocker findings halt chain advancement (F-2, stop condition 4)
  - Open items remain cumulative and visible (O-1 through O-5)
  - Residual risks carry forward with provenance (RR-1 through RR-4)
  - Chain stop conditions are explicit when blockers persist
  - End-to-end multi-feature chain carry-forward lifecycle
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from chain_state_projection import (
    build_carry_forward_summary,
    build_chain_projection,
    compute_advancement_truth,
    init_chain_state,
    record_state_transition,
)
from chain_recovery import (
    build_next_feature_context,
    snapshot_feature_boundary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """State directory with gate results subdirectory."""
    (tmp_path / "review_gates" / "results").mkdir(parents=True)
    return tmp_path


def _write_pr_queue(state_dir: Path, prs: List[Dict[str, Any]]) -> None:
    record = {"feature": "Certification Test", "feature_metadata": {}, "prs": prs}
    (state_dir / "pr_queue_state.json").write_text(json.dumps(record))


def _write_open_items(state_dir: Path, items: List[Dict[str, Any]]) -> None:
    (state_dir / "open_items.json").write_text(
        json.dumps({"schema_version": "1", "items": items, "next_id": len(items) + 1})
    )


def _write_gate_result(state_dir: Path, pr_num: int, gate: str, status: str = "approve",
                        blocking: int = 0, contract_hash: str = "abc123") -> None:
    result = {
        "gate": gate, "pr_number": pr_num, "pr_id": str(pr_num), "status": status,
        "blocking_count": blocking, "contract_hash": contract_hash,
        "report_path": f"/tmp/report-{pr_num}-{gate}.md", "recorded_at": "2026-04-02T10:00:00Z",
    }
    path = state_dir / "review_gates" / "results" / f"pr-{pr_num}-{gate}.json"
    path.write_text(json.dumps(result))


def _init_three_feature_chain(state_dir: Path) -> None:
    """Initialize a 3-feature chain: PR-0 -> PR-1 -> PR-2."""
    _write_pr_queue(state_dir, [
        {"id": "PR-0", "status": "completed", "dependencies": [], "title": "Contract", "track": "C", "gate": "g0"},
        {"id": "PR-1", "status": "completed", "dependencies": ["PR-0"], "title": "Projection", "track": "B", "gate": "g1"},
        {"id": "PR-2", "status": "queued", "dependencies": ["PR-1"], "title": "Recovery", "track": "B", "gate": "g2"},
    ])
    init_chain_state(
        state_dir, chain_id="cert-chain-001", feature_plan="FEATURE_PLAN.md",
        feature_sequence=["PR-0", "PR-1", "PR-2"], chain_origin_sha="aaa111",
    )


# ---------------------------------------------------------------------------
# Tests: findings persist across feature boundaries (F-1 through F-5)
# ---------------------------------------------------------------------------

class TestFindingsPersistAcrossFeatures:
    """Contract Section 6.2: findings from earlier features remain visible in later features."""

    def test_findings_from_feature_0_visible_after_feature_1_snapshot(self, state_dir: Path) -> None:
        """F-1: every finding recorded in carry-forward with source feature."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="Contract",
            status="completed", prs_merged=["PR-0"],
            findings=[
                {"id": "F-001", "severity": "warn", "resolution_status": "open", "description": "perf concern"},
                {"id": "F-002", "severity": "info", "resolution_status": "resolved", "description": "doc typo"},
            ],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="Projection",
            status="completed", prs_merged=["PR-1"],
            findings=[
                {"id": "F-003", "severity": "blocker", "resolution_status": "open", "description": "missing guard"},
            ],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        finding_ids = {f["id"] for f in ledger["findings"]}
        assert finding_ids == {"F-001", "F-002", "F-003"}, "all findings from both features must persist"

        # Verify source feature provenance (F-1)
        sources = {f["id"]: f["source_feature"] for f in ledger["findings"]}
        assert sources["F-001"] == "PR-0"
        assert sources["F-003"] == "PR-1"

    def test_resolved_findings_remain_as_closed_records(self, state_dir: Path) -> None:
        """F-5: resolved findings stay in ledger as closed records."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            findings=[{"id": "F-010", "severity": "warn", "resolution_status": "resolved"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        resolved = [f for f in ledger["findings"] if f["id"] == "F-010"]
        assert len(resolved) == 1
        assert resolved[0]["resolution_status"] == "resolved"

    def test_warn_findings_carried_forward_without_blocking(self, state_dir: Path) -> None:
        """F-3: warn findings carry forward and are visible but do not block."""
        _init_three_feature_chain(state_dir)
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="Projection",
            status="completed", prs_merged=["PR-1"],
            findings=[{"id": "F-020", "severity": "warn", "resolution_status": "open"}],
        )
        _write_gate_result(state_dir, 1, "gemini_review")
        _write_gate_result(state_dir, 1, "codex_gate")

        # Warn findings should not block advancement
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is True

        # But the finding must be visible in next feature context
        ctx = build_next_feature_context(state_dir)
        assert ctx["open_finding_count"] >= 1

    def test_info_findings_carried_for_audit(self, state_dir: Path) -> None:
        """F-4: info findings carry forward for audit completeness."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            findings=[{"id": "F-030", "severity": "info", "resolution_status": "open"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert any(f["id"] == "F-030" for f in ledger["findings"])


# ---------------------------------------------------------------------------
# Tests: blocker findings halt chain (F-2 + stop condition 4)
# ---------------------------------------------------------------------------

class TestBlockerFindingsHaltChain:
    """Contract Section 2.4 stop condition 4 + F-2: blocker findings block advancement."""

    def test_blocker_open_item_prevents_advancement(self, state_dir: Path) -> None:
        """Unresolved blocker open items block chain advancement."""
        _init_three_feature_chain(state_dir)
        _write_gate_result(state_dir, 1, "gemini_review")
        _write_gate_result(state_dir, 1, "codex_gate")

        blocker = {"id": "OI-BLOCK-1", "severity": "blocker", "status": "open", "title": "Critical defect"}
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[blocker], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert any("blocker" in b for b in result["blockers"])

    def test_blocker_finding_in_carry_forward_visible_at_boundary(self, state_dir: Path) -> None:
        """Blocker findings in the ledger are surfaced in carry-forward summary."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            findings=[{"id": "F-BLK-1", "severity": "blocker", "resolution_status": "open"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        summary = build_carry_forward_summary(ledger, open_items=[])
        assert summary["blocker_findings"] >= 1

    def test_resolved_blocker_does_not_block_advancement(self, state_dir: Path) -> None:
        """A blocker that's been resolved should not prevent advancement."""
        _init_three_feature_chain(state_dir)
        _write_gate_result(state_dir, 1, "gemini_review")
        _write_gate_result(state_dir, 1, "codex_gate")

        resolved_blocker = {"id": "OI-BLK-2", "severity": "blocker", "status": "done", "title": "Fixed"}
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[resolved_blocker], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is True

    def test_chain_enters_blocked_state_with_blocker_items(self, state_dir: Path) -> None:
        """Chain projection shows ADVANCEMENT_BLOCKED when live blockers exist."""
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "queued", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _write_open_items(state_dir, [
            {"id": "OI-BLK-3", "severity": "blocker", "status": "open", "title": "Critical"},
        ])
        projection = build_chain_projection(state_dir)
        assert projection["carry_forward_summary"]["live_blocker_items"] >= 1
        assert projection["chain_state"] == "ADVANCEMENT_BLOCKED"
        assert projection["is_blocked"] is True


# ---------------------------------------------------------------------------
# Tests: open items remain cumulative and visible (O-1 through O-5)
# ---------------------------------------------------------------------------

class TestOpenItemsCumulative:
    """Contract Section 6.3: open items never silently dropped between features."""

    def test_items_accumulate_across_three_features(self, state_dir: Path) -> None:
        """O-5: items are cumulative — never silently dropped."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            open_items=[{"id": "OI-A", "severity": "warn", "status": "open", "title": "Item A"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="F1",
            status="completed", prs_merged=["PR-1"],
            open_items=[{"id": "OI-B", "severity": "info", "status": "open", "title": "Item B"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-2", feature_name="F2",
            status="completed", prs_merged=["PR-2"],
            open_items=[{"id": "OI-C", "severity": "warn", "status": "open", "title": "Item C"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        item_ids = {i["id"] for i in ledger["open_items"]}
        assert item_ids == {"OI-A", "OI-B", "OI-C"}, "all three items must persist across three features"

    def test_item_resolved_in_later_feature_updates_status(self, state_dir: Path) -> None:
        """O-1: snapshot at boundary updates status of previously carried items."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            open_items=[{"id": "OI-X", "severity": "warn", "status": "open", "title": "T"}],
        )
        # Resolve OI-X in feature PR-1
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="F1",
            status="completed", prs_merged=["PR-1"],
            open_items=[{"id": "OI-X", "severity": "warn", "status": "done", "title": "T"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        oi = next(i for i in ledger["open_items"] if i["id"] == "OI-X")
        assert oi["status"] == "done", "resolution must update carry-forward ledger"
        assert oi["origin_feature"] == "PR-0", "original feature provenance preserved"

    def test_deferred_items_only_allowed_for_non_blocker(self, state_dir: Path) -> None:
        """O-3: items may be deferred only if severity < blocker."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            open_items=[
                {"id": "OI-DEF-W", "severity": "warn", "status": "deferred", "title": "Warn deferred"},
                {"id": "OI-DEF-I", "severity": "info", "status": "deferred", "title": "Info deferred"},
            ],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        deferred = [i for i in ledger["open_items"] if i["status"] == "deferred"]
        assert len(deferred) == 2
        for item in deferred:
            assert item["severity"] in ("warn", "info"), "only non-blocker items should be deferrable"

    def test_next_feature_dispatch_includes_carried_items(self, state_dir: Path) -> None:
        """O-4: next feature dispatch context includes carried-forward open item summary."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            open_items=[
                {"id": "OI-P1", "severity": "warn", "status": "open", "title": "Perf concern"},
                {"id": "OI-P2", "severity": "blocker", "status": "open", "title": "Security gap"},
            ],
        )
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 2
        assert ctx["blocker_item_count"] == 1
        assert ctx["warn_item_count"] == 1
        assert len(ctx["blocker_items"]) == 1
        assert ctx["blocker_items"][0]["id"] == "OI-P2"

    def test_unresolved_items_visible_in_chain_projection(self, state_dir: Path) -> None:
        """Projection surface shows unresolved items from both carry-forward and live state."""
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
            {"id": "PR-1", "status": "queued", "dependencies": ["PR-0"], "title": "T", "track": "B", "gate": "g"},
        ])
        # Carry-forward item from previous feature
        (state_dir / "chain_carry_forward.json").write_text(json.dumps({
            "chain_id": "cert", "findings": [], "deferred_items": [],
            "residual_risks": [], "feature_summaries": [],
            "open_items": [{"id": "CF-01", "severity": "warn", "status": "open", "title": "Old", "origin_feature": "PR-0"}],
        }))
        # Live item from current feature
        _write_open_items(state_dir, [
            {"id": "OI-LIVE", "severity": "info", "status": "open", "title": "Current", "pr_id": "PR-1"},
        ])
        projection = build_chain_projection(state_dir)
        ids = {i["id"] for i in projection["unresolved_chain_items"]}
        sources = {i["id"]: i["source"] for i in projection["unresolved_chain_items"]}
        assert "CF-01" in ids, "carry-forward items must be visible"
        assert "OI-LIVE" in ids, "live items must be visible"
        assert sources["CF-01"] == "carry_forward"
        assert sources["OI-LIVE"] == "open_items.json"


# ---------------------------------------------------------------------------
# Tests: residual risk carry-forward (RR-1 through RR-4)
# ---------------------------------------------------------------------------

class TestResidualRiskCarryForward:
    """Contract Section 6.4: residual risks persist with provenance."""

    def test_residual_risks_accumulate_across_features(self, state_dir: Path) -> None:
        """RR-1/RR-2: risks recorded with accepting feature and rationale."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            residual_risks=[{"risk": "perf under load", "acceptance_rationale": "deferred to PR-4"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="F1",
            status="completed", prs_merged=["PR-1"],
            residual_risks=[{"risk": "memory growth", "acceptance_rationale": "acceptable for MVP"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert len(ledger["residual_risks"]) == 2
        assert ledger["residual_risks"][0]["accepting_feature"] == "PR-0"
        assert ledger["residual_risks"][1]["accepting_feature"] == "PR-1"

    def test_residual_risks_visible_in_next_feature_context(self, state_dir: Path) -> None:
        """RR-3: risks visible for final certification."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            residual_risks=[{"risk": "r1"}, {"risk": "r2"}, {"risk": "r3"}],
        )
        ctx = build_next_feature_context(state_dir)
        assert ctx["residual_risk_count"] == 3

    def test_residual_risks_in_carry_forward_summary(self, state_dir: Path) -> None:
        """Summary surface includes residual risk count."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            residual_risks=[{"risk": "latency spike"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        summary = build_carry_forward_summary(ledger, open_items=[])
        assert summary["residual_risks"] == 1


# ---------------------------------------------------------------------------
# Tests: chain stop conditions explicit and auditable
# ---------------------------------------------------------------------------

class TestChainStopConditionsExplicit:
    """Chain stop conditions (Section 2.4) remain operator-readable and auditable."""

    def test_chain_halted_state_preserves_audit_trail(self, state_dir: Path) -> None:
        """Audit trail records the halt with reason and evidence."""
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "queued", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        init_chain_state(
            state_dir, chain_id="halt-test", feature_plan="FP.md",
            feature_sequence=["PR-0"], chain_origin_sha="",
        )
        record_state_transition(
            state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0", actor="T0", reason="dispatched",
        )
        record_state_transition(
            state_dir, to_state="CHAIN_HALTED", feature_id="PR-0", actor="T0",
            reason="max retries exceeded for PR-0",
            evidence={"total_attempts": 3, "failure_class": "recoverable_transient"},
        )
        audit_lines = [
            json.loads(l) for l in (state_dir / "chain_audit.jsonl").read_text().strip().splitlines()
        ]
        halt_record = next(r for r in audit_lines if r["to_state"] == "CHAIN_HALTED")
        assert halt_record["reason"] == "max retries exceeded for PR-0"
        assert halt_record["evidence"]["total_attempts"] == 3
        assert halt_record["feature_id"] == "PR-0"

    def test_advancement_blocked_has_explicit_blockers(self, state_dir: Path) -> None:
        """Advancement truth always provides explicit blocker reasons."""
        _write_pr_queue(state_dir, [
            {"id": "PR-1", "status": "queued", "dependencies": [], "title": "T", "track": "B", "gate": "g"},
        ])
        blocker = {"id": "OI-E1", "severity": "blocker", "status": "open", "title": "Critical"}
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[blocker], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert len(result["blockers"]) >= 1
        # Blockers must be human-readable strings, not empty
        for b in result["blockers"]:
            assert isinstance(b, str) and len(b) > 5

    def test_missing_gates_produce_explicit_blocker_messages(self, state_dir: Path) -> None:
        """Gate certification failures produce clear, actionable blocker messages."""
        _write_pr_queue(state_dir, [
            {"id": "PR-1", "status": "completed", "dependencies": [], "title": "T", "track": "B", "gate": "g"},
        ])
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert any("gemini_review" in b and "not certified" in b for b in result["blockers"])
        assert any("codex_gate" in b and "not certified" in b for b in result["blockers"])


# ---------------------------------------------------------------------------
# Tests: end-to-end multi-feature carry-forward lifecycle
# ---------------------------------------------------------------------------

class TestEndToEndChainLifecycle:
    """Full chain lifecycle: 3 features with findings, items, risks accumulating."""

    def test_three_feature_chain_carry_forward_complete(self, state_dir: Path) -> None:
        """Simulate a 3-feature chain with progressive findings and verify final state."""
        _init_three_feature_chain(state_dir)

        # Feature PR-0: produces 1 finding and 1 open item
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0")
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="Contract",
            status="completed", prs_merged=["PR-0"], merge_shas=["sha0"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
            findings=[{"id": "F-E2E-1", "severity": "warn", "resolution_status": "open"}],
            open_items=[{"id": "OI-E2E-1", "severity": "warn", "status": "open", "title": "Perf concern"}],
        )
        record_state_transition(state_dir, to_state="FEATURE_ADVANCING", feature_id="PR-0")

        # Feature PR-1: produces 1 new finding, resolves the old open item, adds residual risk
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1")
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="Projection",
            status="completed", prs_merged=["PR-1"], merge_shas=["sha1"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
            findings=[{"id": "F-E2E-2", "severity": "info", "resolution_status": "open"}],
            open_items=[
                {"id": "OI-E2E-1", "severity": "warn", "status": "done", "title": "Perf concern"},
                {"id": "OI-E2E-2", "severity": "info", "status": "open", "title": "Cleanup debt"},
            ],
            residual_risks=[{"risk": "memory under sustained load", "acceptance_rationale": "monitor in prod"}],
        )
        record_state_transition(state_dir, to_state="FEATURE_ADVANCING", feature_id="PR-1")

        # Feature PR-2: adds 1 more finding, keeps items unchanged
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-2")
        snapshot_feature_boundary(
            state_dir, feature_id="PR-2", feature_name="Recovery",
            status="completed", prs_merged=["PR-2"], merge_shas=["sha2"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
            findings=[{"id": "F-E2E-3", "severity": "warn", "resolution_status": "resolved"}],
            open_items=[
                {"id": "OI-E2E-2", "severity": "info", "status": "done", "title": "Cleanup debt"},
            ],
        )

        # Verify final ledger state
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())

        # All 3 findings from all 3 features
        finding_ids = {f["id"] for f in ledger["findings"]}
        assert finding_ids == {"F-E2E-1", "F-E2E-2", "F-E2E-3"}

        # Open items: OI-E2E-1 resolved, OI-E2E-2 resolved
        item_ids = {i["id"] for i in ledger["open_items"]}
        assert "OI-E2E-1" in item_ids
        assert "OI-E2E-2" in item_ids
        oi1 = next(i for i in ledger["open_items"] if i["id"] == "OI-E2E-1")
        oi2 = next(i for i in ledger["open_items"] if i["id"] == "OI-E2E-2")
        assert oi1["status"] == "done"
        assert oi2["status"] == "done"

        # 1 residual risk from PR-1
        assert len(ledger["residual_risks"]) == 1
        assert ledger["residual_risks"][0]["accepting_feature"] == "PR-1"

        # 3 feature summaries
        assert len(ledger["feature_summaries"]) == 3
        summary_ids = [s["feature_id"] for s in ledger["feature_summaries"]]
        assert summary_ids == ["PR-0", "PR-1", "PR-2"]

    def test_final_context_explains_carry_forward(self, state_dir: Path) -> None:
        """Final certification can explain what was carried forward and why."""
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            findings=[{"id": "F-FC-1", "severity": "warn", "resolution_status": "open"}],
            open_items=[{"id": "OI-FC-1", "severity": "warn", "status": "open", "title": "T"}],
            residual_risks=[{"risk": "latency", "acceptance_rationale": "acceptable for now"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="F1",
            status="completed", prs_merged=["PR-1"],
            findings=[{"id": "F-FC-2", "severity": "info", "resolution_status": "resolved"}],
            open_items=[{"id": "OI-FC-1", "severity": "warn", "status": "done", "title": "T"}],
        )
        ctx = build_next_feature_context(state_dir)
        # After PR-1: OI-FC-1 resolved, F-FC-1 still open, F-FC-2 resolved
        assert ctx["features_completed"] == 2
        assert ctx["residual_risk_count"] == 1
        assert ctx["last_feature_summary"]["feature_id"] == "PR-1"
        # Unresolved items should be 0 (OI-FC-1 is done)
        assert ctx["unresolved_item_count"] == 0

    def test_audit_trail_covers_full_lifecycle(self, state_dir: Path) -> None:
        """Chain audit trail records every state transition for the full lifecycle."""
        _init_three_feature_chain(state_dir)
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-0")
        record_state_transition(state_dir, to_state="FEATURE_ADVANCING", feature_id="PR-0")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1")
        record_state_transition(state_dir, to_state="FEATURE_ADVANCING", feature_id="PR-1")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-2")
        record_state_transition(state_dir, to_state="CHAIN_COMPLETE", feature_id="PR-2")

        audit_lines = [
            json.loads(l) for l in (state_dir / "chain_audit.jsonl").read_text().strip().splitlines()
        ]
        # init + 6 transitions = 7 records
        assert len(audit_lines) == 7
        states = [r["to_state"] for r in audit_lines]
        assert states[0] == "INITIALIZED"
        assert states[-1] == "CHAIN_COMPLETE"
        # Each record has required fields
        for record in audit_lines:
            assert "chain_id" in record
            assert "timestamp" in record
            assert "to_state" in record
            assert "actor" in record
