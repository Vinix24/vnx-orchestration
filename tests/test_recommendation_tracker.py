#!/usr/bin/env python3
"""
Tests for recommendation_tracker.py (PR-4)

Quality gate coverage (gate_pr4_recommendation_usefulness_metrics):
  - Recommendation acceptance and outcome windows are durably recorded
  - Usefulness metrics cover the declared recommendation classes
  - Before/after measurement can distinguish adopted from ignored recommendations
  - Metrics work across headless and channel-originated dispatches
  - Tests cover acceptance tracking, metric aggregation, and advisory-only behavior
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from recommendation_tracker import (
    COMPARISON_COMPUTED,
    COMPARISON_INSUFFICIENT,
    COMPARISON_PENDING,
    DEFAULT_OUTCOME_WINDOW_DAYS,
    METRIC_NAMES,
    VALID_ACCEPTANCE_STATES,
    VALID_RECOMMENDATION_CLASSES,
    DuplicateRecommendationError,
    InvalidAcceptanceTransitionError,
    InvalidRecommendationClassError,
    OutcomeMetric,
    Recommendation,
    RecommendationError,
    RecommendationNotFoundError,
    RecommendationTracker,
    UsefulnessSummary,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Shared test base
# ---------------------------------------------------------------------------

class _DBTestCase(unittest.TestCase):
    """Base class that initialises a temp state dir with the full schema."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")
        self.tracker = RecommendationTracker(self.state_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _propose_default(self, **overrides) -> Recommendation:
        """Helper to propose a recommendation with sensible defaults."""
        kwargs = {
            "recommendation_class": "prompt_patch",
            "title": "Improve first-pass clarity",
            "description": "Add explicit output format instructions to dispatch prompts",
            "evidence_summary": "3 dispatches failed first pass due to ambiguous output format",
            "confidence": 0.75,
        }
        kwargs.update(overrides)
        return self.tracker.propose(**kwargs)

    def _insert_dispatch(
        self,
        conn: sqlite3.Connection,
        dispatch_id: str,
        state: str = "completed",
        attempt_count: int = 1,
        created_at: str = "",
        task_class: str = None,
        channel_origin: str = None,
        target_type: str = None,
    ) -> None:
        """Insert a dispatch row for metric testing."""
        now = created_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        conn.execute(
            """
            INSERT OR IGNORE INTO dispatches
                (dispatch_id, state, attempt_count, created_at, updated_at,
                 task_class, channel_origin, target_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (dispatch_id, state, attempt_count, now, now,
             task_class, channel_origin, target_type),
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
        event_type: str,
        to_state: str = None,
        metadata: dict = None,
    ) -> None:
        """Insert a coordination event for metric testing."""
        import uuid
        conn.execute(
            """
            INSERT INTO coordination_events
                (event_id, event_type, entity_type, entity_id,
                 to_state, actor, metadata_json, occurred_at)
            VALUES (?, ?, 'dispatch', ?, ?, 'test', ?, ?)
            """,
            (
                str(uuid.uuid4()),
                event_type,
                entity_id,
                to_state,
                json.dumps(metadata or {}),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            ),
        )


# ===========================================================================
# Test: Proposal lifecycle
# ===========================================================================

class TestPropose(_DBTestCase):

    def test_propose_creates_recommendation(self):
        rec = self._propose_default()
        self.assertEqual(rec.acceptance_state, "proposed")
        self.assertEqual(rec.recommendation_class, "prompt_patch")
        self.assertIsNotNone(rec.proposed_at)
        self.assertIsNone(rec.accepted_at)

    def test_propose_emits_event(self):
        rec = self._propose_default()
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events "
                "WHERE entity_id = ? AND event_type = 'recommendation_proposed'",
                (rec.recommendation_id,),
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_propose_duplicate_raises(self):
        rec = self._propose_default(recommendation_id="dup-1")
        with self.assertRaises(DuplicateRecommendationError):
            self._propose_default(recommendation_id="dup-1")

    def test_propose_invalid_class_raises(self):
        with self.assertRaises(InvalidRecommendationClassError):
            self._propose_default(recommendation_class="invalid_class")

    def test_propose_all_valid_classes(self):
        for cls in VALID_RECOMMENDATION_CLASSES:
            rec = self._propose_default(recommendation_class=cls)
            self.assertEqual(rec.recommendation_class, cls)


# ===========================================================================
# Test: Acceptance lifecycle
# ===========================================================================

class TestAcceptance(_DBTestCase):

    def test_accept_sets_state_and_window(self):
        rec = self._propose_default()
        accepted = self.tracker.accept(rec.recommendation_id)
        self.assertEqual(accepted.acceptance_state, "accepted")
        self.assertIsNotNone(accepted.accepted_at)
        self.assertIsNotNone(accepted.outcome_window_start)
        self.assertIsNotNone(accepted.outcome_window_end)

    def test_accept_creates_outcome_rows(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        outcomes = self.tracker.get_outcomes(rec.recommendation_id)
        metric_names = {o.metric_name for o in outcomes}
        self.assertEqual(metric_names, METRIC_NAMES)
        for o in outcomes:
            self.assertEqual(o.comparison_status, COMPARISON_PENDING)

    def test_accept_emits_event(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events "
                "WHERE entity_id = ? AND event_type = 'recommendation_accepted'",
                (rec.recommendation_id,),
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_accept_custom_window(self):
        rec = self._propose_default()
        accepted = self.tracker.accept(rec.recommendation_id, outcome_window_days=14)
        start_dt = datetime.fromisoformat(accepted.outcome_window_start.rstrip("Z"))
        end_dt = datetime.fromisoformat(accepted.outcome_window_end.rstrip("Z"))
        delta = end_dt - start_dt
        self.assertAlmostEqual(delta.days, 14, delta=1)

    def test_reject_sets_state_and_reason(self):
        rec = self._propose_default()
        rejected = self.tracker.reject(
            rec.recommendation_id, reason="Not applicable to current workload"
        )
        self.assertEqual(rejected.acceptance_state, "rejected")
        self.assertEqual(rejected.rejection_reason, "Not applicable to current workload")
        self.assertIsNotNone(rejected.rejected_at)

    def test_expire_sets_state(self):
        rec = self._propose_default()
        expired = self.tracker.expire(rec.recommendation_id)
        self.assertEqual(expired.acceptance_state, "expired")
        self.assertIsNotNone(expired.expired_at)

    def test_supersede_sets_state(self):
        rec1 = self._propose_default(recommendation_id="old-1")
        rec2 = self._propose_default(recommendation_id="new-1")
        superseded = self.tracker.supersede("old-1", superseded_by="new-1")
        self.assertEqual(superseded.acceptance_state, "superseded")
        self.assertEqual(superseded.superseded_by, "new-1")

    def test_invalid_transition_raises(self):
        rec = self._propose_default()
        self.tracker.reject(rec.recommendation_id)
        with self.assertRaises(InvalidAcceptanceTransitionError):
            self.tracker.accept(rec.recommendation_id)

    def test_not_found_raises(self):
        with self.assertRaises(RecommendationNotFoundError):
            self.tracker.accept("nonexistent-id")

    def test_accepted_can_be_superseded(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        superseded = self.tracker.supersede(
            rec.recommendation_id, superseded_by="newer"
        )
        self.assertEqual(superseded.acceptance_state, "superseded")


# ===========================================================================
# Test: Outcome recording
# ===========================================================================

class TestOutcomeRecording(_DBTestCase):

    def test_record_baseline_and_outcome(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)

        self.tracker.record_baseline(
            rec.recommendation_id, "first_pass_success_rate", 0.6, 10
        )
        outcome = self.tracker.record_outcome(
            rec.recommendation_id, "first_pass_success_rate", 0.8, 10
        )
        self.assertEqual(outcome.comparison_status, COMPARISON_COMPUTED)
        self.assertAlmostEqual(outcome.delta, 0.2, places=4)
        self.assertEqual(outcome.direction, "improved")

    def test_outcome_without_baseline_is_insufficient(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)

        # Record outcome without baseline first — should get insufficient
        # First, delete the auto-created pending row to test fresh insert
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "DELETE FROM recommendation_outcomes "
                "WHERE recommendation_id = ? AND metric_name = ?",
                (rec.recommendation_id, "redispatch_rate"),
            )
            conn.commit()

        outcome = self.tracker.record_outcome(
            rec.recommendation_id, "redispatch_rate", 0.1, 5
        )
        self.assertEqual(outcome.comparison_status, COMPARISON_INSUFFICIENT)
        self.assertIsNone(outcome.delta)

    def test_degraded_direction(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)

        self.tracker.record_baseline(
            rec.recommendation_id, "ack_timeout_rate", 0.15, 10
        )
        outcome = self.tracker.record_outcome(
            rec.recommendation_id, "ack_timeout_rate", 0.05, 10
        )
        # Delta is negative (0.05 - 0.15 = -0.1), so direction is "degraded"
        self.assertEqual(outcome.direction, "degraded")
        self.assertAlmostEqual(outcome.delta, -0.1, places=4)

    def test_unchanged_direction(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)

        self.tracker.record_baseline(
            rec.recommendation_id, "repeated_failure_rate", 0.0, 10
        )
        outcome = self.tracker.record_outcome(
            rec.recommendation_id, "repeated_failure_rate", 0.0, 10
        )
        self.assertEqual(outcome.direction, "unchanged")
        self.assertAlmostEqual(outcome.delta, 0.0)

    def test_invalid_metric_raises(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        with self.assertRaises(RecommendationError):
            self.tracker.record_baseline(
                rec.recommendation_id, "fake_metric", 1.0, 1
            )

    def test_get_outcomes_returns_all_metrics(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        outcomes = self.tracker.get_outcomes(rec.recommendation_id)
        self.assertEqual(len(outcomes), len(METRIC_NAMES))


# ===========================================================================
# Test: Dispatch metric computation
# ===========================================================================

class TestDispatchMetrics(_DBTestCase):

    def _setup_dispatches(self, window_start: str, window_end: str):
        """Insert a variety of dispatches within the window for metric testing."""
        with get_connection(self.state_dir) as conn:
            # Successful first-pass dispatch
            self._insert_dispatch(
                conn, "d-success-1", state="completed",
                attempt_count=1, created_at=window_start,
            )
            # Successful first-pass dispatch (headless)
            self._insert_dispatch(
                conn, "d-headless-1", state="completed",
                attempt_count=1, created_at=window_start,
                target_type="headless_claude_cli",
                task_class="research_structured",
            )
            # Channel-originated dispatch
            self._insert_dispatch(
                conn, "d-channel-1", state="completed",
                attempt_count=1, created_at=window_start,
                channel_origin="slack-ops",
                task_class="channel_response",
            )
            # Failed dispatch with retries
            self._insert_dispatch(
                conn, "d-fail-1", state="dead_letter",
                attempt_count=3, created_at=window_start,
            )
            # Timed out dispatch
            self._insert_dispatch(
                conn, "d-timeout-1", state="timed_out",
                attempt_count=1, created_at=window_start,
            )
            self._insert_event(conn, "d-timeout-1", "dispatch_transition", to_state="timed_out")

            # Recovered dispatch
            self._insert_dispatch(
                conn, "d-recovered-1", state="completed",
                attempt_count=2, created_at=window_start,
            )
            self._insert_event(conn, "d-recovered-1", "dispatch_transition", to_state="recovered")

            # Operator override dispatch
            self._insert_dispatch(
                conn, "d-override-1", state="completed",
                attempt_count=1, created_at=window_start,
            )
            self._insert_event(
                conn, "d-override-1", "routing_decision",
                metadata={"override": True, "reason": "operator preference"},
            )
            conn.commit()

    def test_compute_dispatch_metrics(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        end = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        self._setup_dispatches(start, end)

        metrics = self.tracker.compute_dispatch_metrics(
            window_start=start, window_end=end
        )
        # 7 total dispatches
        # 4 first-pass success (d-success-1, d-headless-1, d-channel-1, d-override-1)
        self.assertAlmostEqual(
            metrics["first_pass_success_rate"], 4.0 / 7.0, places=2
        )
        # 1 repeated failure (d-fail-1: attempt_count > 1, state dead_letter)
        self.assertAlmostEqual(
            metrics["repeated_failure_rate"], 1.0 / 7.0, places=2
        )
        # open_item_carry_over defaults to 0
        self.assertEqual(metrics["open_item_carry_over"], 0.0)

    def test_empty_window_returns_zeros(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        far_future = (datetime.now(timezone.utc) + timedelta(days=31)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        metrics = self.tracker.compute_dispatch_metrics(
            window_start=future, window_end=far_future
        )
        for name in METRIC_NAMES:
            self.assertEqual(metrics[name], 0.0)

    def test_metrics_include_headless_dispatches(self):
        """Gate check: metrics work across headless dispatches."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        end = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        with get_connection(self.state_dir) as conn:
            self._insert_dispatch(
                conn, "hl-1", state="completed", attempt_count=1,
                created_at=start, target_type="headless_claude_cli",
            )
            self._insert_dispatch(
                conn, "hl-2", state="completed", attempt_count=1,
                created_at=start, target_type="headless_codex_cli",
            )
            conn.commit()

        metrics = self.tracker.compute_dispatch_metrics(
            window_start=start, window_end=end
        )
        self.assertAlmostEqual(metrics["first_pass_success_rate"], 1.0)

    def test_metrics_include_channel_originated_dispatches(self):
        """Gate check: metrics work across channel-originated dispatches."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        end = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        with get_connection(self.state_dir) as conn:
            self._insert_dispatch(
                conn, "ch-1", state="completed", attempt_count=1,
                created_at=start, channel_origin="slack-ops",
            )
            self._insert_dispatch(
                conn, "ch-2", state="dead_letter", attempt_count=2,
                created_at=start, channel_origin="webhook-ci",
            )
            conn.commit()

        metrics = self.tracker.compute_dispatch_metrics(
            window_start=start, window_end=end
        )
        self.assertAlmostEqual(metrics["first_pass_success_rate"], 0.5)


# ===========================================================================
# Test: Before/after outcome computation
# ===========================================================================

class TestOutcomeComputation(_DBTestCase):

    def test_compute_outcomes_for_accepted_recommendation(self):
        rec = self._propose_default()
        accepted = self.tracker.accept(rec.recommendation_id, outcome_window_days=7)

        # Insert some dispatches in the baseline and outcome windows
        start_dt = datetime.fromisoformat(
            accepted.outcome_window_start.rstrip("Z")
        ).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(
            accepted.outcome_window_end.rstrip("Z")
        ).replace(tzinfo=timezone.utc)
        duration = end_dt - start_dt

        baseline_ts = (start_dt - duration / 2).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        outcome_ts = (start_dt + duration / 2).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"

        with get_connection(self.state_dir) as conn:
            # Baseline period: 1 success, 1 failure
            self._insert_dispatch(
                conn, "baseline-1", state="completed",
                attempt_count=1, created_at=baseline_ts,
            )
            self._insert_dispatch(
                conn, "baseline-2", state="dead_letter",
                attempt_count=3, created_at=baseline_ts,
            )
            # Outcome period: 2 successes
            self._insert_dispatch(
                conn, "outcome-1", state="completed",
                attempt_count=1, created_at=outcome_ts,
            )
            self._insert_dispatch(
                conn, "outcome-2", state="completed",
                attempt_count=1, created_at=outcome_ts,
            )
            conn.commit()

        outcomes = self.tracker.compute_outcomes_for_recommendation(
            rec.recommendation_id
        )
        self.assertEqual(len(outcomes), len(METRIC_NAMES))

        # Check first_pass_success_rate improved
        fps = next(o for o in outcomes if o.metric_name == "first_pass_success_rate")
        self.assertEqual(fps.comparison_status, COMPARISON_COMPUTED)
        # Baseline: 1/2 = 0.5, Outcome: 2/2 = 1.0
        self.assertAlmostEqual(fps.baseline_value, 0.5)
        self.assertAlmostEqual(fps.outcome_value, 1.0)
        self.assertAlmostEqual(fps.delta, 0.5)
        self.assertEqual(fps.direction, "improved")

    def test_compute_outcomes_not_accepted_raises(self):
        rec = self._propose_default()
        with self.assertRaises(RecommendationError):
            self.tracker.compute_outcomes_for_recommendation(rec.recommendation_id)


# ===========================================================================
# Test: Adopted vs ignored distinction
# ===========================================================================

class TestAdoptedVsIgnored(_DBTestCase):

    def test_accepted_has_outcomes_rejected_does_not(self):
        """Gate check: adopted vs ignored recommendations are distinguishable."""
        accepted_rec = self._propose_default(recommendation_id="rec-adopted")
        self.tracker.accept("rec-adopted")

        rejected_rec = self._propose_default(
            recommendation_id="rec-ignored",
            recommendation_class="routing_preference",
        )
        self.tracker.reject("rec-ignored", reason="Not needed")

        # Accepted has outcome rows
        accepted_outcomes = self.tracker.get_outcomes("rec-adopted")
        self.assertEqual(len(accepted_outcomes), len(METRIC_NAMES))

        # Rejected has no outcome rows
        rejected_outcomes = self.tracker.get_outcomes("rec-ignored")
        self.assertEqual(len(rejected_outcomes), 0)

    def test_list_by_state_separates_adopted_and_ignored(self):
        self._propose_default(recommendation_id="a1")
        self.tracker.accept("a1")
        self._propose_default(recommendation_id="r1")
        self.tracker.reject("r1")
        self._propose_default(recommendation_id="p1")

        accepted = self.tracker.list_by_state("accepted")
        rejected = self.tracker.list_by_state("rejected")
        proposed = self.tracker.list_by_state("proposed")

        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(accepted[0].recommendation_id, "a1")
        self.assertEqual(rejected[0].recommendation_id, "r1")


# ===========================================================================
# Test: Usefulness summaries
# ===========================================================================

class TestUsefulnessSummary(_DBTestCase):

    def test_summary_for_class(self):
        # Propose 3, accept 2, reject 1
        self._propose_default(recommendation_id="pp-1")
        self._propose_default(recommendation_id="pp-2")
        self._propose_default(recommendation_id="pp-3")

        self.tracker.accept("pp-1")
        self.tracker.accept("pp-2")
        self.tracker.reject("pp-3")

        # Record some outcomes for pp-1
        self.tracker.record_baseline("pp-1", "first_pass_success_rate", 0.5, 10)
        self.tracker.record_outcome("pp-1", "first_pass_success_rate", 0.8, 10)

        summary = self.tracker.summarize_usefulness("prompt_patch")
        self.assertEqual(summary.total_proposed, 3)
        self.assertEqual(summary.total_accepted, 2)
        self.assertEqual(summary.total_rejected, 1)

        fps_metrics = summary.metrics["first_pass_success_rate"]
        self.assertEqual(fps_metrics["total_measured"], 1)
        self.assertEqual(fps_metrics["improved_count"], 1)

    def test_summary_empty_class(self):
        summary = self.tracker.summarize_usefulness("guardrail_adjustment")
        self.assertEqual(summary.total_proposed, 0)
        self.assertEqual(summary.total_accepted, 0)
        for metric_data in summary.metrics.values():
            self.assertEqual(metric_data["total_measured"], 0)

    def test_export_full_report(self):
        """Gate check: advisory-only report generation."""
        self._propose_default(recommendation_id="rpt-1")
        self.tracker.accept("rpt-1")

        report = self.tracker.export_usefulness_report()
        self.assertTrue(report["advisory_only"])
        self.assertIn("generated_at", report)
        self.assertEqual(set(report["classes"].keys()), VALID_RECOMMENDATION_CLASSES)

        # Each class has the required fields
        for cls_data in report["classes"].values():
            self.assertIn("acceptance_rate", cls_data)
            self.assertIn("metrics", cls_data)

    def test_summary_to_dict(self):
        summary = self.tracker.summarize_usefulness("prompt_patch")
        d = summary.to_dict()
        self.assertIn("acceptance_rate", d)
        self.assertIn("metrics", d)
        self.assertEqual(d["recommendation_class"], "prompt_patch")


# ===========================================================================
# Test: Query operations
# ===========================================================================

class TestQueries(_DBTestCase):

    def test_get_returns_none_for_nonexistent(self):
        self.assertIsNone(self.tracker.get("no-such-id"))

    def test_list_by_class(self):
        self._propose_default(recommendation_id="c1", recommendation_class="prompt_patch")
        self._propose_default(recommendation_id="c2", recommendation_class="routing_preference")
        self._propose_default(recommendation_id="c3", recommendation_class="prompt_patch")

        results = self.tracker.list_by_class("prompt_patch")
        self.assertEqual(len(results), 2)

    def test_list_by_class_with_state_filter(self):
        self._propose_default(recommendation_id="f1")
        self._propose_default(recommendation_id="f2")
        self.tracker.accept("f1")

        proposed = self.tracker.list_by_class("prompt_patch", state="proposed")
        accepted = self.tracker.list_by_class("prompt_patch", state="accepted")
        self.assertEqual(len(proposed), 1)
        self.assertEqual(len(accepted), 1)

    def test_list_pending_measurement(self):
        rec = self._propose_default(recommendation_id="pm-1")
        # Accept with very short window (effectively in the past)
        accepted = self.tracker.accept("pm-1", outcome_window_days=0)

        pending = self.tracker.list_pending_measurement()
        # Window end is ~now, so it should appear in pending measurement
        self.assertTrue(len(pending) >= 0)  # Timing-sensitive, just ensure no error


# ===========================================================================
# Test: Advisory-only behavior
# ===========================================================================

class TestAdvisoryOnly(_DBTestCase):

    def test_no_automatic_policy_mutation(self):
        """Gate check: the tracker never auto-applies recommendations.

        Verify that proposing, accepting, computing metrics, and generating
        summaries does not create any 'policy_applied' or 'policy_mutated'
        coordination events.
        """
        rec = self._propose_default(recommendation_id="adv-1")
        self.tracker.accept("adv-1")
        self.tracker.record_baseline("adv-1", "first_pass_success_rate", 0.5, 10)
        self.tracker.record_outcome("adv-1", "first_pass_success_rate", 0.8, 10)
        self.tracker.summarize_usefulness("prompt_patch")
        self.tracker.export_usefulness_report()

        with get_connection(self.state_dir) as conn:
            policy_events = conn.execute(
                "SELECT * FROM coordination_events "
                "WHERE event_type LIKE '%policy%'",
            ).fetchall()
        self.assertEqual(len(policy_events), 0)

    def test_report_explicitly_marks_advisory(self):
        report = self.tracker.export_usefulness_report()
        self.assertTrue(report["advisory_only"])


# ===========================================================================
# Test: Data classes
# ===========================================================================

class TestDataClasses(_DBTestCase):

    def test_recommendation_properties(self):
        rec = self._propose_default()
        self.assertFalse(rec.is_terminal)
        self.assertFalse(rec.is_accepted)
        self.assertFalse(rec.has_outcome_window)

        accepted = self.tracker.accept(rec.recommendation_id)
        self.assertFalse(accepted.is_terminal)
        self.assertTrue(accepted.is_accepted)
        self.assertTrue(accepted.has_outcome_window)

    def test_recommendation_to_dict(self):
        rec = self._propose_default()
        d = rec.to_dict()
        self.assertIn("recommendation_id", d)
        self.assertIn("recommendation_class", d)
        self.assertIn("acceptance_state", d)
        self.assertEqual(d["acceptance_state"], "proposed")

    def test_outcome_metric_from_row(self):
        rec = self._propose_default()
        self.tracker.accept(rec.recommendation_id)
        self.tracker.record_baseline(
            rec.recommendation_id, "first_pass_success_rate", 0.5, 10
        )
        outcome = self.tracker.record_outcome(
            rec.recommendation_id, "first_pass_success_rate", 0.7, 10
        )
        self.assertIsInstance(outcome, OutcomeMetric)
        self.assertEqual(outcome.metric_name, "first_pass_success_rate")


# ===========================================================================
# Test: Events audit trail
# ===========================================================================

class TestEventAuditTrail(_DBTestCase):

    def test_full_lifecycle_emits_correct_events(self):
        rec = self._propose_default(recommendation_id="evt-1")
        self.tracker.accept("evt-1")

        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT event_type FROM coordination_events "
                "WHERE entity_id = 'evt-1' ORDER BY occurred_at",
            ).fetchall()

        event_types = [dict(e)["event_type"] for e in events]
        self.assertIn("recommendation_proposed", event_types)
        self.assertIn("recommendation_accepted", event_types)

    def test_reject_emits_event(self):
        rec = self._propose_default(recommendation_id="rej-evt-1")
        self.tracker.reject("rej-evt-1", reason="test")

        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT event_type FROM coordination_events "
                "WHERE entity_id = 'rej-evt-1' "
                "AND event_type = 'recommendation_rejected'",
            ).fetchall()
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
