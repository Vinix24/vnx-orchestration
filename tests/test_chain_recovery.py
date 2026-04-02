#!/usr/bin/env python3
"""Tests for chain recovery, requeue enforcement, and branch transition guard (PR-2, Feature 14).

Covers:
  - Failure classification: recoverable_transient / recoverable_fixable / non_recoverable
  - Recovery decision: requeue vs escalate with retry-limit enforcement (R-2/R-3)
  - Resume safety: resume-safe vs resume-unsafe chain states
  - Branch baseline guard: valid and stale branch detection
  - Carry-forward snapshot: feature boundary persistence
  - Next feature context: carry-forward summary for dispatch injection
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Any, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from chain_recovery import (
    FAILURE_CLASS_FIXABLE,
    FAILURE_CLASS_NON_RECOVERABLE,
    FAILURE_CLASS_TRANSIENT,
    MAX_ATTEMPTS_PER_CLASS,
    MAX_TOTAL_ATTEMPTS,
    RECOVERY_ACTION_ESCALATE,
    RECOVERY_ACTION_REQUEUE,
    RESUME_SAFE_STATES,
    RESUME_UNSAFE_STATES,
    BaselineCheckResult,
    FailureClassification,
    RecoveryDecision,
    build_next_feature_context,
    check_branch_baseline,
    classify_failure,
    evaluate_recovery,
    is_resume_safe,
    record_failure_attempt,
    snapshot_feature_boundary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


def _make_history(feature_id: str, total: int, by_class: Dict[str, int]) -> Dict[str, Any]:
    return {feature_id: {"total_attempts": total, "failure_classes": by_class}}


# ---------------------------------------------------------------------------
# Tests: failure classification
# ---------------------------------------------------------------------------

class TestFailureClassification:
    def test_timeout_is_transient(self) -> None:
        result = classify_failure("provider timeout after 30s")
        assert result.failure_class == FAILURE_CLASS_TRANSIENT
        assert result.is_recoverable is True

    def test_rate_limit_is_transient(self) -> None:
        result = classify_failure("rate_limit from Gemini API")
        assert result.failure_class == FAILURE_CLASS_TRANSIENT
        assert result.is_recoverable is True

    def test_network_outage_is_transient(self) -> None:
        result = classify_failure("network connection refused")
        assert result.failure_class == FAILURE_CLASS_TRANSIENT

    def test_lint_error_is_fixable(self) -> None:
        result = classify_failure("lint error in scripts/lib/foo.py")
        assert result.failure_class == FAILURE_CLASS_FIXABLE
        assert result.is_recoverable is True

    def test_test_failure_is_fixable(self) -> None:
        result = classify_failure("test failure: assertion error in test_foo.py")
        assert result.failure_class == FAILURE_CLASS_FIXABLE

    def test_review_finding_is_fixable(self) -> None:
        result = classify_failure("gate_finding: blocking issue in PR review")
        assert result.failure_class == FAILURE_CLASS_FIXABLE

    def test_unknown_reason_is_non_recoverable(self) -> None:
        result = classify_failure("architectural incompatibility with chain dependencies")
        assert result.failure_class == FAILURE_CLASS_NON_RECOVERABLE
        assert result.is_recoverable is False

    def test_explicit_hint_overrides_keyword_match(self) -> None:
        # Even though "timeout" would classify as transient, explicit hint wins
        result = classify_failure("timeout", hint=FAILURE_CLASS_NON_RECOVERABLE)
        assert result.failure_class == FAILURE_CLASS_NON_RECOVERABLE

    def test_explicit_transient_hint(self) -> None:
        result = classify_failure("some vague error", hint=FAILURE_CLASS_TRANSIENT)
        assert result.failure_class == FAILURE_CLASS_TRANSIENT
        assert result.is_recoverable is True

    def test_invalid_hint_falls_through_to_keyword(self) -> None:
        result = classify_failure("timeout occurred", hint="invalid_class")
        assert result.failure_class == FAILURE_CLASS_TRANSIENT


# ---------------------------------------------------------------------------
# Tests: recovery decision tree
# ---------------------------------------------------------------------------

class TestRecoveryDecision:
    def test_transient_failure_requeues_on_first_attempt(self) -> None:
        decision = evaluate_recovery("PR-1", "rate_limit hit", {})
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.failure_class == FAILURE_CLASS_TRANSIENT
        assert decision.must_start_from_main is True

    def test_fixable_failure_requeues_on_first_attempt(self) -> None:
        decision = evaluate_recovery("PR-1", "lint error in module.py", {})
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.failure_class == FAILURE_CLASS_FIXABLE

    def test_non_recoverable_always_escalates(self) -> None:
        decision = evaluate_recovery("PR-1", "architectural conflict", {})
        assert decision.action == RECOVERY_ACTION_ESCALATE

    def test_escalates_when_per_class_limit_reached(self) -> None:
        """R-2: max 2 retries per failure class."""
        history = _make_history("PR-1", total=2, by_class={FAILURE_CLASS_TRANSIENT: MAX_ATTEMPTS_PER_CLASS})
        decision = evaluate_recovery("PR-1", "timeout again", history)
        assert decision.action == RECOVERY_ACTION_ESCALATE
        assert "per-class" in decision.reason

    def test_escalates_when_total_limit_reached(self) -> None:
        """R-3: max 3 total retries regardless of class."""
        history = _make_history("PR-1", total=MAX_TOTAL_ATTEMPTS, by_class={})
        decision = evaluate_recovery("PR-1", "lint error", history)
        assert decision.action == RECOVERY_ACTION_ESCALATE
        assert "total attempts" in decision.reason

    def test_requeue_still_allowed_when_other_class_exhausted(self) -> None:
        """A different failure class can still requeue if its own count is under limit."""
        history = _make_history("PR-1", total=1, by_class={FAILURE_CLASS_TRANSIENT: MAX_ATTEMPTS_PER_CLASS})
        decision = evaluate_recovery("PR-1", "lint error in module", history)
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.failure_class == FAILURE_CLASS_FIXABLE

    def test_requeue_carries_must_start_from_main(self) -> None:
        """R-4: requeue must start from current main."""
        decision = evaluate_recovery("PR-2", "ci flake", {})
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.must_start_from_main is True

    def test_unknown_feature_gets_empty_history(self) -> None:
        """Feature with no history should requeue recoverable failures."""
        decision = evaluate_recovery("PR-99", "test failure", {})
        assert decision.action == RECOVERY_ACTION_REQUEUE


# ---------------------------------------------------------------------------
# Tests: failure attempt recording
# ---------------------------------------------------------------------------

class TestFailureAttemptRecording:
    def test_records_first_attempt(self) -> None:
        history: Dict[str, Any] = {}
        record_failure_attempt(history, "PR-1", FAILURE_CLASS_TRANSIENT)
        assert history["PR-1"]["total_attempts"] == 1
        assert history["PR-1"]["failure_classes"][FAILURE_CLASS_TRANSIENT] == 1

    def test_increments_on_repeated_calls(self) -> None:
        history: Dict[str, Any] = {}
        record_failure_attempt(history, "PR-1", FAILURE_CLASS_TRANSIENT)
        record_failure_attempt(history, "PR-1", FAILURE_CLASS_TRANSIENT)
        record_failure_attempt(history, "PR-1", FAILURE_CLASS_FIXABLE)
        assert history["PR-1"]["total_attempts"] == 3
        assert history["PR-1"]["failure_classes"][FAILURE_CLASS_TRANSIENT] == 2
        assert history["PR-1"]["failure_classes"][FAILURE_CLASS_FIXABLE] == 1

    def test_separate_features_are_independent(self) -> None:
        history: Dict[str, Any] = {}
        record_failure_attempt(history, "PR-1", FAILURE_CLASS_TRANSIENT)
        record_failure_attempt(history, "PR-2", FAILURE_CLASS_FIXABLE)
        assert history["PR-1"]["total_attempts"] == 1
        assert history["PR-2"]["total_attempts"] == 1


# ---------------------------------------------------------------------------
# Tests: resume safety
# ---------------------------------------------------------------------------

class TestResumeSafety:
    def test_feature_active_is_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "FEATURE_ACTIVE"}) is True

    def test_initialized_is_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "INITIALIZED"}) is True

    def test_feature_advancing_is_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "FEATURE_ADVANCING"}) is True

    def test_chain_complete_is_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "CHAIN_COMPLETE"}) is True

    def test_feature_failed_is_not_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "FEATURE_FAILED"}) is False

    def test_recovery_pending_is_not_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "RECOVERY_PENDING"}) is False

    def test_chain_halted_is_not_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "CHAIN_HALTED"}) is False

    def test_advancement_blocked_is_not_resume_safe(self) -> None:
        assert is_resume_safe({"current_state": "ADVANCEMENT_BLOCKED"}) is False

    def test_none_chain_state_is_not_resume_safe(self) -> None:
        assert is_resume_safe(None) is False

    def test_all_safe_states_covered(self) -> None:
        for state in RESUME_SAFE_STATES:
            assert is_resume_safe({"current_state": state}) is True

    def test_all_unsafe_states_covered(self) -> None:
        for state in RESUME_UNSAFE_STATES:
            assert is_resume_safe({"current_state": state}) is False


# ---------------------------------------------------------------------------
# Tests: branch baseline guard
# ---------------------------------------------------------------------------

class TestBranchBaselineGuard:
    def _make_runner(self, merge_base_sha: str, ancestor_valid: bool = False):
        """Return a fake git runner for merge-base and --is-ancestor checks."""
        def runner(args: list[str]) -> str:
            if "--is-ancestor" in args:
                if ancestor_valid:
                    return ""
                raise RuntimeError("not ancestor")
            if "merge-base" in args:
                # Verify we check against main, not HEAD
                assert "main" in args, f"branch guard must compare against 'main', got: {args}"
                return merge_base_sha
            raise RuntimeError(f"unexpected git command: {args}")
        return runner

    def test_valid_when_merge_base_matches_expected(self) -> None:
        sha = "abc1234567890000000000000000000000000000"
        result = check_branch_baseline(
            "feature/my-feature", sha, git_runner=self._make_runner(sha)
        )
        assert result.is_valid is True
        assert "matches" in result.reason

    def test_invalid_when_merge_base_differs(self) -> None:
        expected = "abc1234567890000000000000000000000000000"
        actual = "def9876543210000000000000000000000000000"
        result = check_branch_baseline(
            "feature/my-feature", expected, git_runner=self._make_runner(actual)
        )
        assert result.is_valid is False
        assert "stale branch" in result.reason
        assert result.actual_merge_base == actual

    def test_valid_when_no_expected_sha(self) -> None:
        """Bootstrap case: empty expected SHA accepts any merge-base."""
        result = check_branch_baseline(
            "feature/my-feature", "", git_runner=self._make_runner("anything")
        )
        assert result.is_valid is True

    def test_prefix_match_accepted(self) -> None:
        """Short SHA prefix must match full SHA."""
        full_sha = "abc1234567890abcdef1234567890abcdef12345"
        short = "abc1234"
        result = check_branch_baseline(
            "feature/x", short, git_runner=self._make_runner(full_sha)
        )
        assert result.is_valid is True

    def test_git_failure_returns_invalid(self) -> None:
        def failing_runner(args: list[str]) -> str:
            raise RuntimeError("not a git repository")
        result = check_branch_baseline(
            "feature/x", "abc123", git_runner=failing_runner
        )
        assert result.is_valid is False
        assert "git merge-base failed" in result.reason

    def test_stale_branch_blocks_dispatch(self) -> None:
        """Contract S-3: stale branch must be rejected."""
        expected = "aaaa000000000000000000000000000000000000"
        stale = "bbbb111111111111111111111111111111111111"
        result = check_branch_baseline(
            "feature/stale", expected, git_runner=self._make_runner(stale)
        )
        assert result.is_valid is False
        assert "recreate worktree" in result.reason


# ---------------------------------------------------------------------------
# Tests: carry-forward snapshot
# ---------------------------------------------------------------------------

class TestCarryForwardSnapshot:
    def test_snapshot_creates_carry_forward_file(self, state_dir: Path) -> None:
        snapshot_feature_boundary(
            state_dir,
            feature_id="PR-0",
            feature_name="Chain Contract",
            status="completed",
            prs_merged=["PR-0"],
            merge_shas=["abc123"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
        )
        cf_path = state_dir / "chain_carry_forward.json"
        assert cf_path.exists()
        ledger = json.loads(cf_path.read_text())
        assert len(ledger["feature_summaries"]) == 1
        assert ledger["feature_summaries"][0]["feature_id"] == "PR-0"

    def test_findings_accumulate_across_features(self, state_dir: Path) -> None:
        findings_f0 = [{"id": "F-001", "severity": "warn", "resolution_status": "open"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], findings=findings_f0)
        findings_f1 = [{"id": "F-002", "severity": "info", "resolution_status": "open"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-1", feature_name="F1",
                                   status="completed", prs_merged=["PR-1"], findings=findings_f1)
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        finding_ids = {f["id"] for f in ledger["findings"]}
        assert "F-001" in finding_ids
        assert "F-002" in finding_ids

    def test_open_items_never_silently_dropped(self, state_dir: Path) -> None:
        """Contract O-5: open items are cumulative across features."""
        items_f0 = [{"id": "OI-001", "severity": "warn", "status": "open", "title": "Perf concern"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], open_items=items_f0)
        items_f1 = [{"id": "OI-002", "severity": "info", "status": "open", "title": "Cleanup"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-1", feature_name="F1",
                                   status="completed", prs_merged=["PR-1"], open_items=items_f1)
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        item_ids = {i["id"] for i in ledger["open_items"]}
        assert "OI-001" in item_ids
        assert "OI-002" in item_ids

    def test_open_item_status_updates_on_re_snapshot(self, state_dir: Path) -> None:
        """If an item is resolved in a later feature, its status updates in the ledger."""
        items = [{"id": "OI-001", "severity": "warn", "status": "open", "title": "T"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], open_items=items)
        resolved = [{"id": "OI-001", "severity": "warn", "status": "done", "title": "T"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-1", feature_name="F1",
                                   status="completed", prs_merged=["PR-1"], open_items=resolved)
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        oi = next(i for i in ledger["open_items"] if i["id"] == "OI-001")
        assert oi["status"] == "done"

    def test_residual_risks_accumulate(self, state_dir: Path) -> None:
        risks = [{"risk": "perf degradation under load", "acceptance_rationale": "deferred to PR-4"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], residual_risks=risks)
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert len(ledger["residual_risks"]) == 1
        assert ledger["residual_risks"][0]["accepting_feature"] == "PR-0"

    def test_feature_summary_structure(self, state_dir: Path) -> None:
        snapshot_feature_boundary(
            state_dir,
            feature_id="PR-0",
            feature_name="Chain Contract",
            status="completed",
            prs_merged=["PR-0"],
            merge_shas=["abc123"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
            requeue_count=1,
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        summary = ledger["feature_summaries"][0]
        assert summary["feature_id"] == "PR-0"
        assert summary["status"] == "completed"
        assert summary["prs_merged"] == ["PR-0"]
        assert summary["requeue_count"] == 1
        assert summary["gate_results"]["gemini_review"] == "passed"


# ---------------------------------------------------------------------------
# Tests: next feature context
# ---------------------------------------------------------------------------

class TestNextFeatureContext:
    def test_empty_state_returns_zero_counts(self, state_dir: Path) -> None:
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 0
        assert ctx["blocker_item_count"] == 0
        assert ctx["open_finding_count"] == 0
        assert ctx["features_completed"] == 0

    def test_carry_forward_items_appear_in_context(self, state_dir: Path) -> None:
        items = [
            {"id": "OI-001", "severity": "blocker", "status": "open", "title": "Critical"},
            {"id": "OI-002", "severity": "warn", "status": "open", "title": "Warn"},
            {"id": "OI-003", "severity": "info", "status": "done", "title": "Resolved"},
        ]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], open_items=items)
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 2   # OI-001 + OI-002 (OI-003 resolved)
        assert ctx["blocker_item_count"] == 1
        assert ctx["warn_item_count"] == 1
        assert len(ctx["blocker_items"]) == 1

    def test_last_feature_summary_present(self, state_dir: Path) -> None:
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"])
        snapshot_feature_boundary(state_dir, feature_id="PR-1", feature_name="F1",
                                   status="completed", prs_merged=["PR-1"])
        ctx = build_next_feature_context(state_dir)
        assert ctx["features_completed"] == 2
        assert ctx["last_feature_summary"]["feature_id"] == "PR-1"

    def test_context_includes_residual_risk_count(self, state_dir: Path) -> None:
        risks = [{"risk": "latency spike"}, {"risk": "memory growth"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], residual_risks=risks)
        ctx = build_next_feature_context(state_dir)
        assert ctx["residual_risk_count"] == 2

    def test_wontfix_items_excluded_from_unresolved(self, state_dir: Path) -> None:
        """wontfix is a terminal status — must not count as unresolved."""
        items = [
            {"id": "OI-001", "severity": "warn", "status": "wontfix", "title": "Accepted"},
            {"id": "OI-002", "severity": "warn", "status": "open", "title": "Still open"},
        ]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], open_items=items)
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 1  # only OI-002


# ---------------------------------------------------------------------------
# Tests: Codex review fixes — branch guard main ref and descendant
# ---------------------------------------------------------------------------

class TestBranchBaselineMainRef:
    def test_merge_base_checks_against_main_not_head(self) -> None:
        """Contract S-2: branch guard must compare against main, not HEAD."""
        calls: list[list[str]] = []
        def tracking_runner(args: list[str]) -> str:
            calls.append(args)
            if "--is-ancestor" in args:
                raise RuntimeError("not ancestor")
            if "merge-base" in args:
                return "abc123"
            raise RuntimeError(f"unexpected: {args}")
        check_branch_baseline("feature/x", "abc123", git_runner=tracking_runner)
        merge_base_call = [c for c in calls if "merge-base" in c and "--is-ancestor" not in c]
        assert len(merge_base_call) == 1
        assert "main" in merge_base_call[0]
        assert "HEAD" not in merge_base_call[0]

    def test_descendant_of_expected_sha_is_valid(self) -> None:
        """S-2: merge-base equal to OR descendant of expected SHA must be accepted."""
        expected = "aaaa000000000000000000000000000000000000"
        newer = "bbbb111111111111111111111111111111111111"
        def runner(args: list[str]) -> str:
            if "--is-ancestor" in args:
                return ""  # success = is ancestor
            if "merge-base" in args:
                return newer
            raise RuntimeError(f"unexpected: {args}")
        result = check_branch_baseline("feature/x", expected, git_runner=runner)
        assert result.is_valid is True
        assert "descendant" in result.reason


# ---------------------------------------------------------------------------
# Tests: Codex review fixes — wontfix terminal status
# ---------------------------------------------------------------------------

class TestWontfixTerminalStatus:
    def test_wontfix_open_item_not_counted_as_unresolved(self, state_dir: Path) -> None:
        items = [{"id": "OI-001", "severity": "blocker", "status": "wontfix", "title": "Dismissed"}]
        snapshot_feature_boundary(state_dir, feature_id="PR-0", feature_name="F0",
                                   status="completed", prs_merged=["PR-0"], open_items=items)
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 0
        assert ctx["blocker_item_count"] == 0


# ---------------------------------------------------------------------------
# Tests: Codex review fixes — deferred items persistence
# ---------------------------------------------------------------------------

class TestDeferredItemsPersistence:
    def test_deferred_items_written_to_carry_forward(self, state_dir: Path) -> None:
        deferred = [
            {"id": "D-001", "severity": "warn", "title": "Deferred cleanup", "reason": "out of scope"},
        ]
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"], deferred_items=deferred,
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert len(ledger["deferred_items"]) == 1
        assert ledger["deferred_items"][0]["id"] == "D-001"
        assert ledger["deferred_items"][0]["origin_feature"] == "PR-0"

    def test_deferred_items_accumulate_across_features(self, state_dir: Path) -> None:
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            deferred_items=[{"id": "D-001", "severity": "warn", "title": "A"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-1", feature_name="F1",
            status="completed", prs_merged=["PR-1"],
            deferred_items=[{"id": "D-002", "severity": "info", "title": "B"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        ids = {d["id"] for d in ledger["deferred_items"]}
        assert ids == {"D-001", "D-002"}
