#!/usr/bin/env python3
"""Tests for enriched governance signal extraction (Feature 18, PR-1).

Covers:
  1. Session event signal extraction (failure, timed_out, artifact)
  2. Gate result signal extraction (pass/fail)
  3. Queue anomaly signal extraction
  4. Open-item transition signal extraction
  5. Defect family normalization
  6. Correlation integrity across all signal types
  7. Full collection pipeline
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from governance_signal_extractor import (
    GOVERNANCE_SIGNAL_TYPES,
    GovernanceSignal,
    SignalCorrelation,
    collect_governance_signals,
    extract_from_gate_results,
    extract_from_open_item_transitions,
    extract_from_queue_anomalies,
    extract_from_session_events,
    normalize_defect_families,
)


# ---------------------------------------------------------------------------
# 1. Session event extraction
# ---------------------------------------------------------------------------

class TestSessionEventExtraction:

    def _make_failed_event(self, reason: str = "exit 1") -> dict:
        return {
            "event_type": "session_failed",
            "session_id": "T2",
            "dispatch_id": "d-001",
            "details": {"reason": reason, "exit_code": 1},
        }

    def _make_timed_out_event(self) -> dict:
        return {
            "event_type": "session_timed_out",
            "session_id": "T2",
            "dispatch_id": "d-001",
            "details": {},
        }

    def _make_artifact_event(self, name: str = "report", path: str = "/tmp/r.md") -> dict:
        return {
            "event_type": "artifact_materialized",
            "session_id": "T2",
            "dispatch_id": "d-001",
            "artifact_path": path,
            "details": {"artifact_name": name},
        }

    def test_failed_event_produces_session_failure_signal(self) -> None:
        sigs = extract_from_session_events([self._make_failed_event()])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "session_failure"

    def test_failed_event_severity_is_blocker(self) -> None:
        sigs = extract_from_session_events([self._make_failed_event()])
        assert sigs[0].severity == "blocker"

    def test_failed_event_content_includes_reason(self) -> None:
        sigs = extract_from_session_events([self._make_failed_event("assertion error")])
        assert "assertion error" in sigs[0].content

    def test_timed_out_produces_session_failure_warn(self) -> None:
        sigs = extract_from_session_events([self._make_timed_out_event()])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "session_failure"
        assert sigs[0].severity == "warn"

    def test_artifact_event_produces_session_artifact_signal(self) -> None:
        sigs = extract_from_session_events([self._make_artifact_event()])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "session_artifact"
        assert sigs[0].severity == "info"

    def test_artifact_content_includes_name_and_path(self) -> None:
        sigs = extract_from_session_events([self._make_artifact_event("output", "/out.txt")])
        assert "output" in sigs[0].content
        assert "/out.txt" in sigs[0].content

    def test_session_failure_has_defect_family_key(self) -> None:
        sigs = extract_from_session_events([self._make_failed_event()])
        assert sigs[0].defect_family is not None
        assert len(sigs[0].defect_family) == 12  # md5 hex prefix

    def test_unknown_event_types_ignored(self) -> None:
        sigs = extract_from_session_events([
            {"event_type": "heartbeat", "session_id": "T1"},
            {"event_type": "session_created", "session_id": "T1"},
        ])
        assert sigs == []

    def test_correlation_carried_from_event_fields(self) -> None:
        corr = SignalCorrelation(feature_id="F17", pr_id="PR-2", branch="main")
        sigs = extract_from_session_events([self._make_failed_event()], correlation=corr)
        assert sigs[0].correlation.feature_id == "F17"
        assert sigs[0].correlation.pr_id == "PR-2"
        assert sigs[0].correlation.session_id == "T2"   # from event
        assert sigs[0].correlation.dispatch_id == "d-001"  # from event

    def test_provider_id_from_correlation_when_not_in_event(self) -> None:
        corr = SignalCorrelation(provider_id="gemini")
        sigs = extract_from_session_events([self._make_failed_event()], correlation=corr)
        assert sigs[0].correlation.provider_id == "gemini"


# ---------------------------------------------------------------------------
# 2. Gate result extraction
# ---------------------------------------------------------------------------

class TestGateResultExtraction:

    def _make_gate_fail(self, gate_id: str = "gate_pr1_test", reason: str = "") -> dict:
        return {
            "gate_id": gate_id,
            "status": "fail",
            "feature_id": "F18",
            "pr_id": "PR-1",
            "reason": reason,
        }

    def _make_gate_pass(self, gate_id: str = "gate_pr1_test") -> dict:
        return {
            "gate_id": gate_id,
            "status": "pass",
            "feature_id": "F18",
            "pr_id": "PR-1",
        }

    def test_failed_gate_produces_gate_failure_signal(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail()])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "gate_failure"

    def test_failed_gate_severity_is_blocker(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail()])
        assert sigs[0].severity == "blocker"

    def test_failed_gate_content_includes_gate_id(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail("gate_pr2_tests")])
        assert "gate_pr2_tests" in sigs[0].content

    def test_failed_gate_content_includes_reason(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail(reason="3 blockers")])
        assert "3 blockers" in sigs[0].content

    def test_passed_gate_produces_gate_success_signal(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_pass()])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "gate_success"
        assert sigs[0].severity == "info"

    def test_gate_failure_has_defect_family(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail()])
        assert sigs[0].defect_family is not None

    def test_gate_feature_and_pr_correlation(self) -> None:
        sigs = extract_from_gate_results([self._make_gate_fail()])
        assert sigs[0].correlation.feature_id == "F18"
        assert sigs[0].correlation.pr_id == "PR-1"

    def test_unknown_status_produces_no_signal(self) -> None:
        sigs = extract_from_gate_results([{"gate_id": "g", "status": "pending"}])
        assert sigs == []

    def test_status_aliases(self) -> None:
        for status in ("failed", "fail"):
            sigs = extract_from_gate_results([{"gate_id": "g", "status": status}])
            assert sigs[0].signal_type == "gate_failure"
        for status in ("passed", "success"):
            sigs = extract_from_gate_results([{"gate_id": "g", "status": status}])
            assert sigs[0].signal_type == "gate_success"


# ---------------------------------------------------------------------------
# 3. Queue anomaly extraction
# ---------------------------------------------------------------------------

class TestQueueAnomalyExtraction:

    def _make_anomaly(self, etype: str = "delivery_failure", reason: str = "pane gone") -> dict:
        return {
            "event_type": etype,
            "dispatch_id": "d-99",
            "terminal_id": "T1",
            "reason": reason,
        }

    def test_delivery_failure_produces_queue_anomaly(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("delivery_failure")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == "queue_anomaly"

    def test_delivery_failure_severity_is_warn(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("delivery_failure")])
        assert sigs[0].severity == "warn"

    def test_dead_letter_severity_is_blocker(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("dead_letter", "budget exhausted")])
        assert sigs[0].severity == "blocker"

    def test_reconcile_error_severity_is_blocker(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("reconcile_error", "state mismatch")])
        assert sigs[0].severity == "blocker"

    def test_ack_timeout_produces_anomaly(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("ack_timeout", "no ack in 30s")])
        assert sigs[0].signal_type == "queue_anomaly"

    def test_unrelated_event_type_ignored(self) -> None:
        sigs = extract_from_queue_anomalies([{"event_type": "heartbeat", "terminal_id": "T1"}])
        assert sigs == []

    def test_anomaly_content_includes_type_and_reason(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly("delivery_failure", "pane gone")])
        assert "delivery_failure" in sigs[0].content
        assert "pane gone" in sigs[0].content

    def test_anomaly_has_defect_family(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly()])
        assert sigs[0].defect_family is not None

    def test_correlation_terminal_and_dispatch(self) -> None:
        sigs = extract_from_queue_anomalies([self._make_anomaly()])
        assert sigs[0].correlation.terminal_id == "T1"
        assert sigs[0].correlation.dispatch_id == "d-99"


# ---------------------------------------------------------------------------
# 4. Open-item transition extraction
# ---------------------------------------------------------------------------

class TestOpenItemTransitionExtraction:

    def _make_transition(
        self, title: str = "Tests fail on CI", severity: str = "blocker",
        from_s: str = "open", to_s: str = "open",
        feature_id: str = "F18", pr_id: str = "PR-1",
    ) -> dict:
        return {
            "item_id": "OI-001",
            "title": title,
            "severity": severity,
            "from_status": from_s,
            "to_status": to_s,
            "feature_id": feature_id,
            "pr_id": pr_id,
        }

    def test_new_blocker_produces_blocker_signal(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(severity="blocker", from_s="new", to_s="open")])
        assert len(sigs) == 1
        assert sigs[0].severity == "blocker"

    def test_resolution_produces_info_signal(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(severity="blocker", from_s="open", to_s="resolved")])
        assert sigs[0].severity == "info"

    def test_wontfix_produces_info_signal(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(severity="warn", from_s="open", to_s="wontfix")])
        assert sigs[0].severity == "info"

    def test_warn_non_terminal_produces_warn_signal(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(severity="warn", from_s="open", to_s="deferred")])
        assert sigs[0].severity == "warn"

    def test_content_includes_title_and_transition(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(title="Import fails", from_s="open", to_s="resolved")])
        assert "Import fails" in sigs[0].content
        assert "open->resolved" in sigs[0].content

    def test_short_title_ignored(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(title="Bug")])
        assert sigs == []

    def test_signal_type_is_open_item_transition(self) -> None:
        sigs = extract_from_open_item_transitions([self._make_transition()])
        assert sigs[0].signal_type == "open_item_transition"

    def test_feature_and_pr_correlation(self) -> None:
        sigs = extract_from_open_item_transitions([
            self._make_transition(feature_id="F17", pr_id="PR-3")])
        assert sigs[0].correlation.feature_id == "F17"
        assert sigs[0].correlation.pr_id == "PR-3"


# ---------------------------------------------------------------------------
# 5. Defect family normalization
# ---------------------------------------------------------------------------

class TestDefectFamilyNormalization:

    def _sig(self, content: str, family: str) -> GovernanceSignal:
        return GovernanceSignal(
            signal_type="session_failure",
            content=content,
            severity="blocker",
            defect_family=family,
        )

    def test_single_occurrence_passes_through_unchanged(self) -> None:
        sigs = [self._sig("session_failed: exit 1", "abc123")]
        result = normalize_defect_families(sigs)
        assert len(result) == 1
        assert result[0].signal_type == "session_failure"

    def test_repeated_family_collapsed_into_defect_family_signal(self) -> None:
        sigs = [
            self._sig("session_failed: exit 1", "abc123"),
            self._sig("session_failed: exit 1", "abc123"),
            self._sig("session_failed: exit 1", "abc123"),
        ]
        result = normalize_defect_families(sigs)
        assert len(result) == 1
        assert result[0].signal_type == "defect_family"
        assert result[0].count == 3

    def test_defect_family_content_includes_count(self) -> None:
        sigs = [self._sig("connection refused", "fam1")] * 4
        result = normalize_defect_families(sigs)
        assert "[x4]" in result[0].content

    def test_no_family_signals_pass_through(self) -> None:
        sigs = [
            GovernanceSignal("gate_success", "gate passed", "info"),
            GovernanceSignal("session_artifact", "artifact: report", "info"),
        ]
        result = normalize_defect_families(sigs)
        assert len(result) == 2
        assert all(s.signal_type != "defect_family" for s in result)

    def test_mixed_families_normalized_independently(self) -> None:
        sigs = [
            self._sig("error A", "family_a"),
            self._sig("error A", "family_a"),
            self._sig("error B", "family_b"),
        ]
        result = normalize_defect_families(sigs)
        assert len(result) == 2
        families = {s.defect_family for s in result}
        assert "family_a" in families
        assert "family_b" in families

    def test_worst_severity_selected_for_family(self) -> None:
        sigs = [
            GovernanceSignal("session_failure", "err", "warn", defect_family="fam"),
            GovernanceSignal("session_failure", "err", "blocker", defect_family="fam"),
        ]
        result = normalize_defect_families(sigs)
        assert result[0].severity == "blocker"

    def test_repeated_failures_queryable_by_family_key(self) -> None:
        """Recurring defect family can be queried by defect_family key."""
        # Same error content with instance-specific tokens should resolve to same family
        corr1 = SignalCorrelation(dispatch_id="20260101-000001-foo-B")
        corr2 = SignalCorrelation(dispatch_id="20260102-000002-foo-B")
        sigs_raw = extract_from_session_events([
            {"event_type": "session_failed", "session_id": "T1",
             "dispatch_id": "20260101-000001-foo-B", "details": {"reason": "exit 1"}},
        ], correlation=corr1) + extract_from_session_events([
            {"event_type": "session_failed", "session_id": "T2",
             "dispatch_id": "20260102-000002-foo-B", "details": {"reason": "exit 1"}},
        ], correlation=corr2)
        # Both should have same defect family (content normalizes dispatch IDs)
        result = normalize_defect_families(sigs_raw)
        defect_sigs = [s for s in result if s.signal_type == "defect_family"]
        assert len(defect_sigs) == 1
        assert defect_sigs[0].count == 2


# ---------------------------------------------------------------------------
# 6. Correlation integrity
# ---------------------------------------------------------------------------

class TestCorrelationIntegrity:

    def _full_corr(self) -> SignalCorrelation:
        return SignalCorrelation(
            feature_id="F18",
            pr_id="PR-1",
            session_id="T2",
            dispatch_id="d-dispatch",
            provider_id="codex_cli",
            terminal_id="T2",
            branch="feature/test",
        )

    def test_session_signal_preserves_all_correlation_keys(self) -> None:
        corr = self._full_corr()
        sigs = extract_from_session_events([
            {"event_type": "session_failed", "details": {"reason": "exit 1"}}
        ], correlation=corr)
        c = sigs[0].correlation
        assert c.feature_id == "F18"
        assert c.pr_id == "PR-1"
        assert c.provider_id == "codex_cli"
        assert c.terminal_id == "T2"
        assert c.branch == "feature/test"

    def test_gate_signal_preserves_feature_and_pr(self) -> None:
        corr = self._full_corr()
        sigs = extract_from_gate_results([
            {"gate_id": "g", "status": "fail"}
        ], correlation=corr)
        assert sigs[0].correlation.feature_id == "F18"
        assert sigs[0].correlation.pr_id == "PR-1"

    def test_queue_anomaly_preserves_terminal(self) -> None:
        corr = self._full_corr()
        sigs = extract_from_queue_anomalies([
            {"event_type": "ack_timeout", "reason": "no ack"}
        ], correlation=corr)
        assert sigs[0].correlation.terminal_id == "T2"

    def test_correlation_to_dict_omits_empty_fields(self) -> None:
        corr = SignalCorrelation(feature_id="F18")
        d = corr.to_dict()
        assert "feature_id" in d
        assert "pr_id" not in d
        assert "session_id" not in d

    def test_governance_signal_to_dict_includes_correlation(self) -> None:
        sig = GovernanceSignal(
            signal_type="gate_failure",
            content="gate g failed",
            severity="blocker",
            correlation=SignalCorrelation(feature_id="F18", pr_id="PR-1"),
        )
        d = sig.to_dict()
        assert d["correlation"]["feature_id"] == "F18"
        assert d["correlation"]["pr_id"] == "PR-1"

    def test_signal_types_all_valid(self) -> None:
        sigs = collect_governance_signals(
            session_timeline=[
                {"event_type": "session_failed", "details": {"reason": "exit 1"}},
                {"event_type": "artifact_materialized",
                 "artifact_path": "/tmp/r.md", "details": {"artifact_name": "r"}},
            ],
            gate_results=[{"gate_id": "g", "status": "fail"}],
            queue_anomalies=[{"event_type": "delivery_failure", "reason": "pane gone"}],
            open_item_transitions=[{
                "item_id": "OI-1", "title": "Tests fail in CI", "severity": "blocker",
                "from_status": "open", "to_status": "open",
            }],
        )
        for sig in sigs:
            assert sig.signal_type in GOVERNANCE_SIGNAL_TYPES


# ---------------------------------------------------------------------------
# 7. Full collection pipeline
# ---------------------------------------------------------------------------

class TestCollectionPipeline:

    def test_collect_from_all_sources(self) -> None:
        sigs = collect_governance_signals(
            session_timeline=[
                {"event_type": "session_failed", "details": {"reason": "exit 1"}},
            ],
            gate_results=[{"gate_id": "gate_pr1", "status": "pass"}],
            queue_anomalies=[{"event_type": "ack_timeout", "reason": "30s timeout"}],
            open_item_transitions=[{
                "item_id": "OI-5", "title": "Memory leak in adapter", "severity": "warn",
                "from_status": "open", "to_status": "resolved",
            }],
        )
        types = {s.signal_type for s in sigs}
        assert "session_failure" in types
        assert "gate_success" in types
        assert "queue_anomaly" in types
        assert "open_item_transition" in types

    def test_empty_inputs_return_empty(self) -> None:
        sigs = collect_governance_signals()
        assert sigs == []

    def test_max_signals_enforced(self) -> None:
        many = [
            {"event_type": "session_failed", "details": {"reason": f"error {i}"}}
            for i in range(100)
        ]
        sigs = collect_governance_signals(session_timeline=many, max_signals=10)
        assert len(sigs) <= 10

    def test_normalize_families_true_by_default(self) -> None:
        # Two identical failures should collapse to one defect_family signal
        two_fails = [
            {"event_type": "session_failed", "details": {"reason": "exit 1"}},
            {"event_type": "session_failed", "details": {"reason": "exit 1"}},
        ]
        sigs = collect_governance_signals(session_timeline=two_fails)
        defect = [s for s in sigs if s.signal_type == "defect_family"]
        assert len(defect) == 1
        assert defect[0].count == 2

    def test_normalize_families_false_preserves_raw(self) -> None:
        two_fails = [
            {"event_type": "session_failed", "details": {"reason": "exit 1"}},
            {"event_type": "session_failed", "details": {"reason": "exit 1"}},
        ]
        sigs = collect_governance_signals(
            session_timeline=two_fails, normalize_families=False)
        assert all(s.signal_type == "session_failure" for s in sigs)
        assert len(sigs) == 2

    def test_signals_richer_than_receipt_text(self) -> None:
        """Verify signals carry structured fields, not just raw text."""
        corr = SignalCorrelation(
            feature_id="F18", pr_id="PR-1",
            provider_id="gemini", branch="feature/test",
        )
        sigs = collect_governance_signals(
            session_timeline=[{
                "event_type": "session_failed",
                "session_id": "T2", "dispatch_id": "d-1",
                "details": {"reason": "timeout", "exit_code": 124},
            }],
            correlation=corr,
        )
        assert sigs[0].correlation.feature_id == "F18"
        assert sigs[0].correlation.provider_id == "gemini"
        assert sigs[0].signal_type in ("session_failure", "defect_family")
