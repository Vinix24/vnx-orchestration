#!/usr/bin/env python3
"""
Tests for PR-3: Dispatch Failure Classification And Operator Visibility.

Quality gate: gate_pr3_failure_classification_visibility

Coverage:
  - Failure classification for all 6 failure classes:
    invalid_skill, stale_lease, runtime_state_divergence,
    worker_handoff_failure, hook_feedback_interruption, tmux_transport_failure
  - Retryable vs non-retryable distinction is deterministic
  - Operator summary is present and meaningful for every class
  - Rejected dispatches preserve actionable root-cause markers
  - Cleanup outcome is visible in release_on_delivery_failure result
  - check_terminal surfaces classification for zombie lease
  - T0 can distinguish retryable from non-retryable deterministically
  - Unknown reasons default to retryable (safe default)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from failure_classifier import (
    HOOK_FEEDBACK_INTERRUPTION,
    INVALID_SKILL,
    RUNTIME_STATE_DIVERGENCE,
    STALE_LEASE,
    TMUX_TRANSPORT_FAILURE,
    WORKER_HANDOFF_FAILURE,
    FailureClassification,
    classify_failure,
    is_retryable,
)
from runtime_coordination import (
    get_connection,
    get_dispatch,
    init_schema,
    transition_dispatch,
)
from dispatch_broker import DispatchBroker
from lease_manager import LeaseManager
from runtime_core import RuntimeCore


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def _setup(tmp: tempfile.TemporaryDirectory):
    base = Path(tmp.name)
    state_dir = base / "state"
    dispatch_dir = base / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    init_schema(state_dir)
    broker = DispatchBroker(str(state_dir), str(dispatch_dir), shadow_mode=False)
    lease_mgr = LeaseManager(state_dir, auto_init=False)
    core = RuntimeCore(broker=broker, lease_mgr=lease_mgr)
    return str(state_dir), str(dispatch_dir), broker, lease_mgr, core


def _full_delivery_setup(core, broker, lease_mgr, state_dir,
                         dispatch_id, terminal_id="T2"):
    broker.register(dispatch_id, f"Work for {dispatch_id}", terminal_id=terminal_id)
    lease_result = lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
    generation = lease_result.generation
    delivery = core.delivery_start(dispatch_id, terminal_id)
    attempt_id = delivery.attempt_id or ""
    return attempt_id, generation


# ---------------------------------------------------------------------------
# TestClassifyFailure — unit tests for the classifier
# ---------------------------------------------------------------------------

class TestClassifyFailure(unittest.TestCase):
    """Pure classifier tests — no DB or runtime dependency."""

    def test_invalid_skill_from_skill_invalid_marker(self):
        c = classify_failure("SKILL_INVALID: skill '@backend-developer' not found")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_invalid_skill_from_not_found_in_registry(self):
        c = classify_failure("Skill '@reviewer' not found in registry")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_invalid_skill_from_skill_not_found(self):
        c = classify_failure("skill_not_found for role backend-developer")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_stale_lease_from_generation_mismatch(self):
        c = classify_failure("generation mismatch: expected 5, got 3")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_stale_lease_from_lease_expired(self):
        c = classify_failure("lease_expired for T2")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_stale_lease_from_stale_lease_keyword(self):
        c = classify_failure("stale_lease: generation guard rejected")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_runtime_state_divergence(self):
        c = classify_failure("runtime_state_divergence:zombie_lease:completed")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_runtime_state_divergence_from_zombie(self):
        c = classify_failure("zombie_lease detected for T2")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_runtime_state_divergence_from_ghost(self):
        c = classify_failure("ghost_dispatch: dispatch active but no lease")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_worker_handoff_failure(self):
        c = classify_failure("rejected_execution_handoff")
        self.assertEqual(c.failure_class, WORKER_HANDOFF_FAILURE)
        self.assertTrue(c.retryable)

    def test_worker_handoff_from_worker_rejected(self):
        c = classify_failure("worker_rejected during handoff")
        self.assertEqual(c.failure_class, WORKER_HANDOFF_FAILURE)
        self.assertTrue(c.retryable)

    def test_hook_feedback_interruption(self):
        c = classify_failure("prompt_loop_interrupted_after_clear_context")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_hook_interruption_from_hook_failure(self):
        c = classify_failure("hook failure during feedback loop")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_hook_interruption_from_context_reset(self):
        c = classify_failure("context reset interrupted delivery")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_tmux_transport_failure(self):
        c = classify_failure("tmux delivery failed")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_tmux_enter_failed(self):
        c = classify_failure("tmux Enter failed")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_tmux_paste_buffer(self):
        c = classify_failure("paste-buffer failed for T2")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_unknown_defaults_to_tmux_transport(self):
        c = classify_failure("some completely unknown failure reason")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_classification_preserves_original_reason(self):
        reason = "rejected_execution_handoff: worker busy"
        c = classify_failure(reason)
        self.assertEqual(c.reason, reason)

    def test_operator_summary_always_present(self):
        reasons = [
            "skill_invalid", "stale_lease", "runtime_state_divergence",
            "rejected_execution_handoff", "prompt_loop_interrupted",
            "tmux delivery failed", "unknown reason",
        ]
        for reason in reasons:
            c = classify_failure(reason)
            self.assertIsInstance(c.operator_summary, str, f"Missing summary for {reason}")
            self.assertGreater(len(c.operator_summary), 10, f"Summary too short for {reason}")

    def test_to_dict_contains_all_fields(self):
        c = classify_failure("tmux delivery failed")
        d = c.to_dict()
        self.assertIn("failure_class", d)
        self.assertIn("retryable", d)
        self.assertIn("operator_summary", d)
        self.assertIn("reason", d)

    def test_is_retryable_helper(self):
        self.assertTrue(is_retryable(TMUX_TRANSPORT_FAILURE))
        self.assertTrue(is_retryable(STALE_LEASE))
        self.assertTrue(is_retryable(WORKER_HANDOFF_FAILURE))
        self.assertTrue(is_retryable(HOOK_FEEDBACK_INTERRUPTION))
        self.assertFalse(is_retryable(INVALID_SKILL))
        self.assertFalse(is_retryable(RUNTIME_STATE_DIVERGENCE))


# ---------------------------------------------------------------------------
# TestRetryableVsNonRetryable — deterministic distinction
# ---------------------------------------------------------------------------

class TestRetryableVsNonRetryable(unittest.TestCase):
    """T0 can distinguish retryable from non-retryable deterministically."""

    def test_non_retryable_classes(self):
        non_retryable_reasons = [
            ("SKILL_INVALID: not found", INVALID_SKILL),
            ("runtime_state_divergence:zombie", RUNTIME_STATE_DIVERGENCE),
        ]
        for reason, expected_class in non_retryable_reasons:
            c = classify_failure(reason)
            self.assertEqual(c.failure_class, expected_class)
            self.assertFalse(c.retryable, f"{reason} should be non-retryable")

    def test_retryable_classes(self):
        retryable_reasons = [
            ("stale_lease: gen mismatch", STALE_LEASE),
            ("rejected_execution_handoff", WORKER_HANDOFF_FAILURE),
            ("prompt_loop_interrupted_after_clear_context", HOOK_FEEDBACK_INTERRUPTION),
            ("tmux delivery failed", TMUX_TRANSPORT_FAILURE),
        ]
        for reason, expected_class in retryable_reasons:
            c = classify_failure(reason)
            self.assertEqual(c.failure_class, expected_class)
            self.assertTrue(c.retryable, f"{reason} should be retryable")


# ---------------------------------------------------------------------------
# TestReleaseOnFailureClassification — integration with RuntimeCore
# ---------------------------------------------------------------------------

class TestReleaseOnFailureClassification(unittest.TestCase):
    """release_on_delivery_failure result includes classification fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _release_with_reason(self, dispatch_id, reason, terminal_id="T2"):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            dispatch_id, terminal_id,
        )
        return self.core.release_on_delivery_failure(
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            terminal_id=terminal_id,
            generation=generation,
            reason=reason,
        )

    def test_tmux_failure_classified(self):
        result = self._release_with_reason("fc-tmux-001", "tmux delivery failed")
        self.assertEqual(result["failure_class"], TMUX_TRANSPORT_FAILURE)
        self.assertTrue(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_invalid_skill_classified(self):
        result = self._release_with_reason("fc-skill-001", "SKILL_INVALID: not found")
        self.assertEqual(result["failure_class"], INVALID_SKILL)
        self.assertFalse(result["retryable"])

    def test_stale_lease_classified(self):
        result = self._release_with_reason("fc-stale-001", "stale_lease: generation mismatch")
        self.assertEqual(result["failure_class"], STALE_LEASE)
        self.assertTrue(result["retryable"])

    def test_worker_handoff_classified(self):
        result = self._release_with_reason("fc-handoff-001", "rejected_execution_handoff")
        self.assertEqual(result["failure_class"], WORKER_HANDOFF_FAILURE)
        self.assertTrue(result["retryable"])

    def test_hook_interruption_classified(self):
        result = self._release_with_reason(
            "fc-hook-001", "prompt_loop_interrupted_after_clear_context"
        )
        self.assertEqual(result["failure_class"], HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(result["retryable"])

    def test_runtime_divergence_classified(self):
        result = self._release_with_reason(
            "fc-div-001", "runtime_state_divergence:zombie_lease:completed"
        )
        self.assertEqual(result["failure_class"], RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(result["retryable"])

    def test_cleanup_outcome_visible_alongside_classification(self):
        result = self._release_with_reason("fc-vis-001", "tmux delivery failed")
        self.assertIn("lease_released", result)
        self.assertIn("failure_recorded", result)
        self.assertIn("cleanup_complete", result)
        self.assertIn("failure_class", result)
        self.assertIn("retryable", result)
        self.assertIn("operator_summary", result)
        self.assertTrue(result["cleanup_complete"])

    def test_classification_present_even_when_cleanup_fails(self):
        """Classification is always present regardless of cleanup outcome."""
        self.broker.register("fc-partial-001", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="fc-partial-001")
        generation = lease_result.generation
        delivery = self.core.delivery_start("fc-partial-001", "T2")
        attempt_id = delivery.attempt_id or ""

        # Use stale generation so lease release fails
        result = self.core.release_on_delivery_failure(
            "fc-partial-001", attempt_id, "T2",
            generation - 1, "stale_lease: generation mismatch",
        )
        self.assertFalse(result["lease_released"])
        self.assertEqual(result["failure_class"], STALE_LEASE)
        self.assertTrue(result["retryable"])
        self.assertIsNotNone(result["operator_summary"])


# ---------------------------------------------------------------------------
# TestRejectedDispatchReasonPreservation
# ---------------------------------------------------------------------------

class TestRejectedDispatchReasonPreservation(unittest.TestCase):
    """Rejected dispatches preserve actionable root-cause markers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failure_reason_preserved_in_attempt_row(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-001", "T2",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-001", attempt_id, "T2", generation,
            reason="SKILL_INVALID: @reviewer not found in registry",
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("reject-reason-001",),
            ).fetchone()
        self.assertIsNotNone(attempt_row)
        self.assertIn("SKILL_INVALID", attempt_row["failure_reason"])
        self.assertIn("@reviewer", attempt_row["failure_reason"])

    def test_failure_reason_preserved_for_handoff_rejection(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-002", "T1",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-002", attempt_id, "T1", generation,
            reason="rejected_execution_handoff: worker context overflow",
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("reject-reason-002",),
            ).fetchone()
        self.assertIn("rejected_execution_handoff", attempt_row["failure_reason"])
        self.assertIn("worker context overflow", attempt_row["failure_reason"])

    def test_dispatch_state_is_failed_delivery_with_reason(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-003", "T2",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-003", attempt_id, "T2", generation,
            reason="hook_feedback_interruption after terminal reset",
        )
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "reject-reason-003")
        self.assertEqual(row["state"], "failed_delivery")


# ---------------------------------------------------------------------------
# TestCheckTerminalClassification
# ---------------------------------------------------------------------------

class TestCheckTerminalClassification(unittest.TestCase):
    """check_terminal surfaces failure classification for zombie lease."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state_dir = base / "state"
        self.dispatch_dir = base / "dispatches"
        self.state_dir.mkdir(parents=True)
        self.dispatch_dir.mkdir(parents=True)
        init_schema(self.state_dir)
        self.broker = DispatchBroker(
            str(self.state_dir), str(self.dispatch_dir), shadow_mode=False
        )
        self.lease_mgr = LeaseManager(self.state_dir, auto_init=False)
        self.core = RuntimeCore(broker=self.broker, lease_mgr=self.lease_mgr)

    def tearDown(self):
        self._tmp.cleanup()

    def _create_zombie(self, dispatch_id, terminal_id, end_state):
        from runtime_coordination import register_dispatch
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id, project_id="vnx-dev")
            conn.commit()
        self.lease_mgr.acquire(terminal_id, dispatch_id)
        # Transition to end_state to create zombie (lease not released)
        with get_connection(self.state_dir) as conn:
            states = {
                "completed": ["claimed", "delivering", "accepted", "running", "completed"],
                "failed_delivery": ["claimed", "delivering", "failed_delivery"],
                "expired": ["claimed", "expired"],
            }
            for state in states.get(end_state, [end_state]):
                try:
                    transition_dispatch(conn, dispatch_id=dispatch_id,
                                        to_state=state, actor="test")
                except Exception:
                    pass
            conn.commit()

    def test_zombie_lease_includes_classification(self):
        self._create_zombie("zombie-class-001", "T2", "failed_delivery")
        result = self.core.check_terminal("T2", "d-new")
        self.assertFalse(result["available"])
        self.assertEqual(result["failure_class"], RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_zombie_lease_operator_summary_is_readable(self):
        self._create_zombie("zombie-class-002", "T2", "completed")
        result = self.core.check_terminal("T2", "d-new")
        self.assertIsInstance(result["operator_summary"], str)
        self.assertGreater(len(result["operator_summary"]), 20)

    def test_normal_block_has_no_classification(self):
        """Active lease with live dispatch does not include failure classification."""
        from runtime_coordination import register_dispatch
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="active-001", terminal_id="T2", project_id="vnx-dev")
            conn.commit()
        self.lease_mgr.acquire("T2", "active-001")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="active-001",
                                to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="active-001",
                                to_state="delivering", actor="test")
            conn.commit()

        result = self.core.check_terminal("T2", "d-other")
        self.assertFalse(result["available"])
        self.assertNotIn("failure_class", result)


# ---------------------------------------------------------------------------
# TestDeliveryFailureClassification — delivery_failure method
# ---------------------------------------------------------------------------

class TestDeliveryFailureClassification(unittest.TestCase):
    """delivery_failure method includes classification in result."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delivery_failure_includes_classification(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "df-class-001", "T2",
        )
        result = self.core.delivery_failure("df-class-001", attempt_id,
                                             reason="tmux delivery failed")
        self.assertEqual(result["failure_class"], TMUX_TRANSPORT_FAILURE)
        self.assertTrue(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_delivery_failure_non_retryable(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "df-class-002", "T2",
        )
        result = self.core.delivery_failure(
            "df-class-002", attempt_id,
            reason="SKILL_INVALID: @missing-skill not found",
        )
        self.assertEqual(result["failure_class"], INVALID_SKILL)
        self.assertFalse(result["retryable"])


# ---------------------------------------------------------------------------
# TestNearEmptyCompletionMisclassifiedAsUnknown — OI-1333
#
# scripts/lib/failure_classification.py:132 only treats a FULLY empty
# completion as `empty_completion` (`if not completion and error is None`).
# Measured case, dispatch 20260817-d1592r3-pro: a deepseek-v4-pro review
# dispatch ran 733s, spent 57,505 input / 1,937 output tokens, and returned a
# 17-character completion. That completion cannot possibly be a report — the
# envelope rejected it on the report contract (all four mandatory headings
# absent) — yet classify_failure still calls it `unknown` because the
# completion is merely non-empty rather than blank.
#
# The consequence sits one layer up: _ESCALATION_TABLE in
# providers/smart_router/tier_routing.py maps `empty_completion` to
# `retry_same_tier` but `unknown` to `no_climb`. The exact failure mode that
# deserves a retry gets none. This class does not touch
# failure_classification.py or tier_routing.py — it only proves the current
# behavior is wrong and pins the two adjacent behaviors that must not
# regress once the fix lands.
# ---------------------------------------------------------------------------

class TestNearEmptyCompletionMisclassifiedAsUnknown(unittest.TestCase):
    """OI-1333: a vrijwel-lege completion (non-empty, but not a report) must
    classify as `empty_completion`, not fall through to `unknown`."""

    # The measured deepseek-v4-pro completion length (dispatch
    # 20260817-d1592r3-pro) — demonstrably not a report: no headings, no
    # structure, a single abrupt sentence fragment.
    _NEAR_EMPTY_COMPLETION = "I cannot proceed."

    _FULL_REPORT_COMPLETION = (
        "## Summary\n"
        "Fixed the empty completion misclassification bug so near-empty text "
        "no longer falls through to the unknown bucket and misses a retry.\n\n"
        "## Changes\n"
        "Updated failure_classification.py to recognize a report-shaped "
        "completion boundary.\n\n"
        "## Verification\n"
        "Ran the targeted pytest module; all cases green.\n\n"
        "## Open Items\n"
        "None\n"
    )

    def test_near_empty_completion_classifies_as_empty_completion(self):
        """A short, demonstrably-not-a-report completion with no error must
        classify as empty_completion, not unknown."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=None,
            completion_text=self._NEAR_EMPTY_COMPLETION,
            timed_out=False,
            provider="litellm:deepseek",
            returncode=1,
        )
        self.assertEqual(result["failure_class"], "empty_completion")

    def test_full_report_completion_is_not_classified_as_empty(self):
        """A completion carrying all four mandatory report headings must NOT
        be swept into empty_completion — this guards against a fix that
        overshoots and starts treating legitimate output as empty. This test
        passes today and must keep passing after the fix lands."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=None,
            completion_text=self._FULL_REPORT_COMPLETION,
            timed_out=False,
            provider="litellm:deepseek",
            returncode=1,
        )
        self.assertNotEqual(result["failure_class"], "empty_completion")

    def test_fully_empty_completion_still_classifies_as_empty_completion(self):
        """The existing fully-blank branch is untouched by this bug — it
        already passes today and must keep passing after the fix lands."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=None,
            completion_text="",
            timed_out=False,
            provider="litellm:deepseek",
            returncode=0,
        )
        self.assertEqual(result["failure_class"], "empty_completion")

    def test_near_empty_completion_escalates_as_retry_not_no_climb(self):
        """The consequence, not just the label: feeding the near-empty
        completion's failure_class through _ESCALATION_TABLE must yield
        retry_same_tier — the same action a fully-empty completion gets.
        Today it yields no_climb, the same as auth_rejected/unknown, so a
        dispatch that deserves a retry gets none (measured: zero climbs
        across 27,848 receipts)."""
        from failure_classification import classify_failure
        from providers.smart_router.tier_routing import escalate_tier

        classification = classify_failure(
            status="failure",
            error=None,
            completion_text=self._NEAR_EMPTY_COMPLETION,
            timed_out=False,
            provider="litellm:deepseek",
            returncode=1,
        )
        escalation = escalate_tier(
            "tier-mid",
            "parent-dispatch-oi-1333",
            failure_class=classification["failure_class"],
        )
        self.assertEqual(escalation.action, "retry_same_tier")


# ---------------------------------------------------------------------------
# TestP3FailureClassSplit — dispatch 20260821-q3-failure-class-split
#
# Three real, distinguishable failure modes previously collapsed into
# `unknown` together (measured on the ledger, 19-08 through 21-08: 15
# `unknown` receipts, 3 separable causes, each deserving its own escalation
# action instead of the shared `unknown -> no_climb`):
#   - completion_without_execution (kimi fabrication guard, 8x observed)
#   - no_verdict (codex gate-runner stdin/verdict flake, 6x observed)
#   - tool_missing (kimi CLI resolved relative to the repo, not PATH, 1x)
#
# This class does not touch scripts/lib/failure_classifier.py (the unrelated,
# runtime-coordination taxonomy the rest of this file tests) — it targets
# scripts/lib/failure_classification.py and
# scripts/lib/providers/smart_router/tier_routing.py, following the same
# precedent as TestNearEmptyCompletionMisclassifiedAsUnknown above.
# ---------------------------------------------------------------------------

class TestP3FailureClassSplit(unittest.TestCase):
    """P3: completion_without_execution / no_verdict / tool_missing each get
    their own failure_class and their own escalation action."""

    # Literal failure_reason texts as actually emitted by the source
    # (kimi_spawn.py / codex_spawn.py), per the P3 dispatch instruction.
    _COMPLETION_WITHOUT_EXECUTION_REASON = (
        "kimi emitted tool_calls but the dispatch worktree shows no git "
        "changes — completion without execution (fabrication guard). "
        "worktree=/tmp/wt-example events=3"
    )
    _NO_VERDICT_REASON = "codex stderr tail: Reading prompt from stdin..."
    _TOOL_MISSING_REASON = (
        "kimi CLI not found: [Errno 2] No such file or directory: "
        "'/Users/vincentvandeth/Development/vnx-orchestration/.venv/bin/kimi'"
    )

    def test_completion_without_execution_recognized(self):
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=self._COMPLETION_WITHOUT_EXECUTION_REASON,
            completion_text=None,
            provider="kimi",
            returncode=1,
        )
        self.assertEqual(result["failure_class"], "completion_without_execution")

    def test_no_verdict_recognized(self):
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=self._NO_VERDICT_REASON,
            completion_text=None,
            provider="codex",
            returncode=1,
        )
        self.assertEqual(result["failure_class"], "no_verdict")

    def test_tool_missing_recognized(self):
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=self._TOOL_MISSING_REASON,
            completion_text=None,
            provider="kimi",
            returncode=1,
        )
        self.assertEqual(result["failure_class"], "tool_missing")

    def test_unknown_reason_still_falls_back_to_unknown(self):
        """A reason matching none of the three new narrow patterns (nor any
        existing keyword bag) must still land on `unknown` with
        unknown_class=True — the P3 patterns must not widen the net."""
        from failure_classification import classify_failure
        from providers.smart_router.tier_routing import escalate_tier

        result = classify_failure(
            status="failure",
            error=(
                "a completely novel failure text never seen before, no "
                "keyword in any existing or new pattern matches this"
            ),
            completion_text=None,
            provider="codex",
            returncode=1,
        )
        self.assertEqual(result["failure_class"], "unknown")

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-unknown",
            failure_class=result["failure_class"],
        )
        self.assertEqual(escalation.action, "no_climb")
        self.assertTrue(escalation.unknown_class)

    def test_completion_without_execution_escalates_climb(self):
        from providers.smart_router.tier_routing import escalate_tier

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-cwe",
            failure_class="completion_without_execution",
        )
        self.assertEqual(escalation.action, "climb")
        self.assertEqual(escalation.tier_to, "tier-high")

    def test_no_verdict_escalates_retry_same_tier(self):
        from providers.smart_router.tier_routing import escalate_tier

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-nv", failure_class="no_verdict",
        )
        self.assertEqual(escalation.action, "retry_same_tier")
        self.assertEqual(escalation.tier_to, "tier-mid")

    def test_no_verdict_climbs_once_already_retried(self):
        """A second rejection of a same-tier retry must climb, exactly like
        timeout/empty_completion do (retried=True)."""
        from providers.smart_router.tier_routing import escalate_tier

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-nv-retry",
            failure_class="no_verdict", retried=True,
        )
        self.assertEqual(escalation.action, "climb")
        self.assertEqual(escalation.tier_to, "tier-high")

    def test_tool_missing_escalates_no_climb(self):
        from providers.smart_router.tier_routing import escalate_tier

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-tm", failure_class="tool_missing",
        )
        self.assertEqual(escalation.action, "no_climb")
        self.assertIsNone(escalation.tier_to)
        self.assertFalse(escalation.unknown_class)


# ---------------------------------------------------------------------------
# TestP3VocabularySeparation — dispatch 20260821-q3-failure-class-split
#
# The dispatch measured a real two-vocabulary hazard:
# escalate_tier(failure_class="UNKNOWN") raises ValueError, but
# escalate_tier(failure_class="unknown") does not. Traced call chain (see
# failure_classification.py's module docstring): exit_classifier.py's
# UPPERCASE FC_* taxonomy is consumed ONLY by headless_adapter.py, which
# stamps it onto its own result/receipt fields and never calls escalate_tier
# or dispatch_bridge.stage_escalation_bundle. The sole production caller of
# stage_escalation_bundle is dispatch_cli._maybe_stage_escalation, which
# derives failure_class exclusively from failure_classification's lowercase
# classify_failure_safe. The two vocabularies therefore never meet at
# escalate_tier today. These tests pin that fact instead of "fixing" it with
# a `.lower()` that would silently paper over a real coupling if one is ever
# introduced.
# ---------------------------------------------------------------------------

class TestP3VocabularySeparation(unittest.TestCase):
    """P3: exit_classifier's UPPERCASE FC_* taxonomy must never reach
    escalate_tier / _ESCALATION_TABLE."""

    def test_exit_classifier_uppercase_class_rejected_by_escalate_tier(self):
        import exit_classifier
        from providers.smart_router.tier_routing import escalate_tier

        with self.assertRaises(ValueError):
            escalate_tier(
                "tier-mid", "parent-dispatch-p3-vocab",
                failure_class=exit_classifier.FC_UNKNOWN,
            )

    def test_failure_classification_lowercase_unknown_is_accepted(self):
        """Sanity companion to the rejection above: THIS module's own
        spelling of "unknown" (lowercase) is a valid table key and must not
        raise — the two constants read the same in English but are different
        vocabularies, and only the lowercase one is ever produced on the
        production escalation call path."""
        from providers.smart_router.tier_routing import escalate_tier

        escalation = escalate_tier(
            "tier-mid", "parent-dispatch-p3-vocab-lower", failure_class="unknown",
        )
        self.assertEqual(escalation.action, "no_climb")

    def test_exit_classifier_constants_disjoint_from_failure_classes(self):
        """Structural pin: none of exit_classifier's FC_* constants collide
        with failure_classification.FAILURE_CLASSES (the _ESCALATION_TABLE
        key set) — the two taxonomies do not even share a spelling."""
        import exit_classifier
        from failure_classification import FAILURE_CLASSES

        exit_classifier_constants = {
            exit_classifier.FC_SUCCESS,
            exit_classifier.FC_TIMEOUT,
            exit_classifier.FC_TOOL_FAIL,
            exit_classifier.FC_INFRA_FAIL,
            exit_classifier.FC_NO_OUTPUT,
            exit_classifier.FC_INTERRUPTED,
            exit_classifier.FC_PROMPT_ERR,
            exit_classifier.FC_UNKNOWN,
        }
        self.assertEqual(exit_classifier_constants & FAILURE_CLASSES, set())


if __name__ == "__main__":
    unittest.main()
