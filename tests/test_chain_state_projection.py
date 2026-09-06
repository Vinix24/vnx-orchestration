#!/usr/bin/env python3
"""Tests for chain state projection layer (PR-1, Feature 14).

Covers:
  - build_chain_projection: FEATURE_ACTIVE, advancement, blocked, recovery-needed states
  - compute_advancement_truth: requires merged PR AND gate certification
  - carry-forward summary and unresolved chain items surface
  - init_chain_state and record_state_transition lifecycle
  - audit trail append
  - F2-4 (06-09, dispatch 20260906-f24-poortset-configureerbaar): the old
    hardcoded ``REQUIRED_GATES = ("gemini_review", "codex_gate")`` ALL-of-two
    rule is replaced by ``required_signer_gates()`` (sourced from the SAME
    operator-configured review-gate takeover chain
    ``gate_request_handler`` uses) plus an AT-LEAST-ONE-of-N certification
    rule -- see ``TestAtLeastOneSigner`` below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
# Both dirs, matching test_beta3_e1_review_gate_chain.py's own convention:
# chain_state_projection now imports gate_request_handler (F2-4), whose own
# transitive import of gate_recorder needs scripts/ on sys.path (append_receipt.py
# lives there, not under scripts/lib/) on top of chain_state_projection's own
# home under scripts/lib/.
for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chain_state_projection import (
    BLOCKED_STATES,
    RECOVERY_NEEDED_STATES,
    build_carry_forward_summary,
    build_chain_projection,
    compute_advancement_truth,
    init_chain_state,
    record_state_transition,
    required_signer_gates,
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
                        blocking: int = 0, contract_hash: str = "abc123",
                        commit_sha: str = "", write_report: bool = True) -> None:
    """Write a gate result record. ``write_report=True`` (default) creates a
    REAL file at ``report_path`` -- F2-4's certification check requires the
    report to exist on disk, not merely be named. Pass ``write_report=False``
    to exercise the "report_path bestaat niet" guard-break scenario.
    """
    if write_report:
        report_file = state_dir / "reports" / f"report-{pr_num}-{gate}.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(f"# {gate} report for PR {pr_num}\n")
        report_path = str(report_file)
    else:
        report_path = str(state_dir / "reports" / f"never-written-{pr_num}-{gate}.md")
    result = {
        "gate": gate,
        "pr_number": pr_num,
        "pr_id": str(pr_num),
        "status": status,
        "blocking_count": blocking,
        "contract_hash": contract_hash,
        "report_path": report_path,
        "commit_sha": commit_sha,
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
        """Advancement requires gate certification — not just PR completion.

        F2-4: with zero result records, every configured candidate reads
        "absent" (neither a missing-and-blocking gate nor a vote) and the
        ZERO-certified-signers rule is what actually blocks — not a
        per-gate "missing" blocker (that hardcoded pair no longer exists).
        """
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
        assert result["certification_status"]["codex_gate"] == "absent"
        assert result["certification_status"]["gemini_review"] == "absent"
        assert any("no valid signer" in b for b in result["blockers"])

    def test_cannot_advance_when_gate_not_certified(self, state_dir: Path) -> None:
        """A decided-but-failing gate blocks even when another configured
        gate certifies clean (F2-4 requirement 3: a dissenting vote never
        loses to a passing one elsewhere)."""
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        _write_gate_result(state_dir, 1, "codex_gate", status="reject", blocking=1)
        _write_gate_result(state_dir, 1, "glm_gate", status="approve", contract_hash="abc")
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1"
        )
        assert result["can_advance"] is False
        assert "not_certified" in result["certification_status"]["codex_gate"]
        assert result["certification_status"]["glm_gate"] == "certified"

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
# Tests: F2-4 -- at least one certified signer from the configured chain
# ---------------------------------------------------------------------------

class TestAtLeastOneSigner:
    """F2-4 (06-09, dispatch 20260906-f24-poortset-configureerbaar).

    ``test_real_glm_pass_plus_codex_not_executable_pr1777`` uses the ACTUAL
    values recorded on disk 2026-09-05/06 for PR #1777
    (``~/.vnx-data/vnx-dev/state/review_gates/results/pr-1777-glm_gate.json``
    and ``pr-1777-codex_gate.json``) — the live case this dispatch exists to
    fix. Before this change: ``REQUIRED_GATES = ("gemini_review",
    "codex_gate")`` read codex_gate's not_executable record as a missing
    mandatory gate and gemini_review had no record at all, so BOTH required
    gates were uncertified — NO-GO — even though glm_gate's own record was a
    complete, evidenced PASS. This test proves the fix: GO.
    """

    def test_real_glm_pass_plus_codex_not_executable_pr1777(self, state_dir: Path) -> None:
        report_file = state_dir / "reports" / "glm-gate-pr1777-report.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("# glm gate: pass (0 blocking finding(s))\n")
        # Verbatim shape of the real pr-1777-glm_gate.json record (report_path
        # repointed to a file that actually exists in THIS test's tmp_path).
        glm_result = {
            "gate": "glm_gate",
            "pr_id": "1777",
            "pr_number": 1777,
            "test_run": False,
            "status": "pass",
            "reason": "verdict",
            "summary": "glm gate: pass (0 blocking finding(s))",
            "contract_hash": "549c0288ef98004e",
            "report_path": str(report_file),
            "provider": "glm-harness",
            "model": "glm-5.2",
            "dispatch_id": "glm-gate-pr1777-1788639375",
            "blocking_findings": [],
            "recorded_at": "2026-09-05T20:17:03Z",
            "evidence_source": "live",
            "branch": "dispatch/20260905-golf1b-report-store-split",
            "commit_sha": "01f54411d975e39f41cb2b0d005fd8dcd5aad04a",
        }
        # Verbatim shape of the real pr-1777-codex_gate.json record.
        codex_result = {
            "gate": "codex_gate",
            "pr_id": "1777",
            "pr_number": 1777,
            "status": "not_executable",
            "reason": "provider_not_installed",
            "reason_detail": "codex binary not found in PATH",
            "failure_reason": "codex binary not found in PATH",
            "summary": "codex_gate not executable: codex binary not found in PATH",
            "contract_hash": "",
            "report_path": "",
            "blocking_findings": [],
            "recorded_at": "2026-09-05T20:20:41Z",
            "dispatch_id": "20260905-golf1b-report-store-split",
        }
        (state_dir / "review_gates" / "results" / "pr-1777-glm_gate.json").write_text(json.dumps(glm_result))
        (state_dir / "review_gates" / "results" / "pr-1777-codex_gate.json").write_text(json.dumps(codex_result))

        pr_queue = {"prs": [{"id": "1777", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="1777",
        )
        assert result["certification_status"]["codex_gate"] == "absent"
        assert result["certification_status"]["glm_gate"] == "certified"
        assert result["can_advance"] is True
        assert result["blockers"] == []

    def test_zero_valid_signers_is_still_no_go(self, state_dir: Path) -> None:
        """The 'at least one' rule never degrades to 'zero is fine'."""
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert any("no valid signer" in b for b in result["blockers"])

    def test_guard_rejects_pass_record_with_wrong_commit_sha(self, state_dir: Path) -> None:
        """Break the guard #1: a PASS whose commit_sha is not the PR head
        must not certify, even though every other field looks clean."""
        _write_gate_result(state_dir, 1, "codex_gate", status="pass", commit_sha="deadbeef")
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
            pr_head_sha="cafef00d",
        )
        assert result["can_advance"] is False
        assert "not_certified" in result["certification_status"]["codex_gate"]
        assert "commit_sha" in result["certification_status"]["codex_gate"]

    def test_guard_rejects_pass_record_without_contract_hash(self, state_dir: Path) -> None:
        """Break the guard #2a: a PASS with an empty contract_hash must not
        certify."""
        _write_gate_result(state_dir, 1, "codex_gate", status="pass", contract_hash="")
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert "contract_hash" in result["certification_status"]["codex_gate"]

    def test_guard_rejects_pass_record_with_missing_report_file(self, state_dir: Path) -> None:
        """Break the guard #2b: a PASS whose report_path does not exist on
        disk must not certify."""
        _write_gate_result(state_dir, 1, "codex_gate", status="pass", write_report=False)
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["can_advance"] is False
        assert "report_path" in result["certification_status"]["codex_gate"]

    def test_guard_a_pass_never_overrides_a_blocking_dissent(self, state_dir: Path) -> None:
        """Break the guard #3: one gate's PASS sitting next to another
        gate's blocking findings must still be NO-GO overall."""
        _write_gate_result(state_dir, 1, "glm_gate", status="pass")
        _write_gate_result(state_dir, 1, "codex_gate", status="fail", blocking=1)
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["certification_status"]["glm_gate"] == "certified"
        assert "not_certified" in result["certification_status"]["codex_gate"]
        assert result["can_advance"] is False

    def test_unavailable_and_not_executable_are_absent_not_a_dissent(self, state_dir: Path) -> None:
        """OI-1624 decision for this surface: an unavailable/not_executable
        record is absent evidence -- neither a certifying vote nor a
        blocking one -- so it must not stop a genuinely certified signer
        elsewhere from reaching GO, and must never appear in blockers."""
        _write_gate_result(state_dir, 1, "glm_gate", status="pass")
        result_path = state_dir / "review_gates" / "results" / "pr-1-kimi_gate.json"
        result_path.write_text(json.dumps({
            "gate": "kimi_gate", "pr_number": 1, "status": "unavailable",
            "reason": "dispatch_error", "contract_hash": "", "report_path": "",
        }))
        pr_queue = {"prs": [{"id": "PR-1", "status": "completed", "dependencies": []}]}
        result = compute_advancement_truth(
            pr_queue=pr_queue, open_items=[], state_dir=state_dir, current_feature_id="PR-1",
        )
        assert result["certification_status"]["kimi_gate"] == "absent"
        assert result["can_advance"] is True
        assert not any("kimi_gate" in b for b in result["blockers"])

    def test_required_signer_gates_reads_the_configured_takeover_chain(self) -> None:
        """No second, independently-drifting gate list: the candidate set
        is sourced from gate_request_handler's own configured chain, plus
        the always-eligible gemini_review carve-out (05-09 operator
        decision)."""
        gates = required_signer_gates()
        assert "gemini_review" in gates
        assert set(gates) >= {"codex_gate", "kimi_gate", "glm_gate", "deepseek_gate"}


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
