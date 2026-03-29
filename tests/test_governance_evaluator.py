#!/usr/bin/env python3
"""Tests for governance evaluation engine — policy evaluation, escalation state, and overrides."""

import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure lib is importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPT_DIR))

from governance_evaluator import (
    DECISION_TYPE_REGISTRY,
    ESCALATION_LEVELS,
    ESCALATION_SEVERITY,
    POLICY_CLASSES,
    POLICY_VERSION,
    BUDGET_LIMITED_ACTIONS,
    ForbiddenActionError,
    GovernanceError,
    InvalidEscalationTransition,
    check_action,
    escalation_summary,
    evaluate_policy,
    get_escalation_level,
    get_escalation_state,
    get_overrides,
    get_unresolved_escalations,
    is_blocked,
    is_enforcement_enabled,
    record_override,
    transition_escalation,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory with initialized schema."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(str(sd))
    # Apply v5 migration for governance tables
    v5_path = Path(__file__).resolve().parent.parent / "schemas" / "runtime_coordination_v5.sql"
    with get_connection(str(sd)) as conn:
        conn.executescript(v5_path.read_text())
        conn.commit()
    return str(sd)


@pytest.fixture
def conn(state_dir):
    """Yield a database connection to the test state directory."""
    with get_connection(state_dir) as c:
        yield c


# ---------------------------------------------------------------------------
# Policy matrix invariants
# ---------------------------------------------------------------------------

class TestPolicyMatrixInvariants:
    def test_all_decision_types_map_to_valid_policy_class(self):
        for dt, (pc, ac) in DECISION_TYPE_REGISTRY.items():
            assert pc in POLICY_CLASSES, f"{dt} maps to unknown policy class {pc}"

    def test_all_decision_types_map_to_valid_action_class(self):
        for dt, (pc, ac) in DECISION_TYPE_REGISTRY.items():
            assert ac in {"automatic", "gated", "forbidden"}, (
                f"{dt} maps to unknown action class {ac}"
            )

    def test_policy_classes_are_non_overlapping(self):
        """Each decision type maps to exactly one policy class."""
        seen = set()
        for dt in DECISION_TYPE_REGISTRY:
            assert dt not in seen, f"Duplicate decision type: {dt}"
            seen.add(dt)

    def test_budget_limited_actions_are_automatic(self):
        for action in BUDGET_LIMITED_ACTIONS:
            _, ac = DECISION_TYPE_REGISTRY[action]
            assert ac == "automatic", f"Budget-limited {action} should be automatic, got {ac}"

    def test_forbidden_actions_exist(self):
        forbidden = [dt for dt, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "forbidden"]
        assert len(forbidden) >= 4, "Expected at least 4 forbidden actions"

    def test_gated_actions_exist(self):
        gated = [dt for dt, (_, ac) in DECISION_TYPE_REGISTRY.items() if ac == "gated"]
        assert len(gated) >= 5, "Expected at least 5 gated actions"


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

class TestPolicyEvaluation:
    def test_automatic_action_returns_automatic(self, conn):
        result = evaluate_policy(action="heartbeat_check", actor="runtime", conn=conn)
        assert result["outcome"] == "automatic"
        assert result["policy_class"] == "operational"
        assert result["action"] == "heartbeat_check"
        assert result["evidence"]["policy_version"] == POLICY_VERSION

    def test_gated_action_without_authority(self, conn):
        result = evaluate_policy(action="dispatch_complete", actor="runtime", conn=conn)
        assert result["outcome"] == "gated"
        assert result["gate_authority"] == "t0"

    def test_gated_action_with_t0_authority(self, conn):
        result = evaluate_policy(action="dispatch_complete", actor="t0", conn=conn)
        assert result["outcome"] == "gated"
        assert result["gate_authority"] == "t0"

    def test_gated_action_with_operator_authority(self, conn):
        result = evaluate_policy(action="pr_close", actor="operator", conn=conn)
        assert result["outcome"] == "gated"
        assert result["gate_authority"] == "operator"

    def test_forbidden_action_by_runtime(self, conn):
        result = evaluate_policy(action="branch_merge", actor="runtime", conn=conn)
        assert result["outcome"] == "forbidden"
        assert result["escalation_level"] == "escalate"

    def test_forbidden_action_by_t0_becomes_gated(self, conn):
        result = evaluate_policy(action="branch_merge", actor="t0", conn=conn)
        assert result["outcome"] == "gated"
        assert result["gate_authority"] == "t0"

    def test_forbidden_action_by_operator_becomes_gated(self, conn):
        result = evaluate_policy(action="gate_bypass", actor="operator", conn=conn)
        assert result["outcome"] == "gated"
        assert result["gate_authority"] == "operator"

    def test_budget_exhausted_promotes_to_gated(self, conn):
        result = evaluate_policy(
            action="delivery_retry",
            actor="runtime",
            context={"dispatch_id": "test-1", "budget_remaining": 0},
            conn=conn,
        )
        assert result["outcome"] == "gated"
        assert result["escalation_level"] == "hold"

    def test_budget_available_stays_automatic(self, conn):
        result = evaluate_policy(
            action="delivery_retry",
            actor="runtime",
            context={"dispatch_id": "test-1", "budget_remaining": 2},
            conn=conn,
        )
        assert result["outcome"] == "automatic"

    def test_unknown_action_raises(self, conn):
        with pytest.raises(GovernanceError, match="Unknown decision type"):
            evaluate_policy(action="nonexistent_action", conn=conn)

    def test_unknown_actor_raises(self, conn):
        with pytest.raises(GovernanceError, match="Unknown actor"):
            evaluate_policy(action="heartbeat_check", actor="unknown", conn=conn)

    def test_evaluation_emits_coordination_event(self, conn):
        evaluate_policy(
            action="dispatch_create",
            actor="runtime",
            context={"dispatch_id": "test-event"},
            conn=conn,
        )
        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'policy_evaluation'"
        ).fetchall()
        assert len(events) >= 1
        last = dict(events[-1])
        assert last["entity_id"] == "test-event"
        assert last["entity_type"] == "dispatch"

    def test_all_automatic_actions_evaluate(self, conn):
        for dt, (pc, ac) in DECISION_TYPE_REGISTRY.items():
            if ac == "automatic":
                result = evaluate_policy(action=dt, actor="runtime", conn=conn)
                assert result["outcome"] in ("automatic", "gated"), (
                    f"{dt} returned unexpected outcome {result['outcome']}"
                )


# ---------------------------------------------------------------------------
# Escalation state management
# ---------------------------------------------------------------------------

class TestEscalationState:
    def test_default_level_is_info(self, conn):
        level = get_escalation_level(conn, "dispatch", "no-record")
        assert level == "info"

    def test_transition_to_review_required(self, conn):
        result = transition_escalation(
            conn,
            entity_type="dispatch",
            entity_id="d-1",
            new_level="review_required",
            trigger_category="repeated_failure",
            trigger_description="Second delivery failure",
        )
        assert result["escalation_level"] == "review_required"
        assert result["trigger_category"] == "repeated_failure"

    def test_transition_to_hold(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-2",
            new_level="review_required",
        )
        result = transition_escalation(
            conn, entity_type="dispatch", entity_id="d-2",
            new_level="hold",
            trigger_category="budget_exhausted",
        )
        assert result["escalation_level"] == "hold"

    def test_transition_to_escalate(self, conn):
        result = transition_escalation(
            conn, entity_type="dispatch", entity_id="d-3",
            new_level="escalate",
            trigger_category="forbidden_action",
            trigger_description="Forbidden branch_merge attempted",
        )
        assert result["escalation_level"] == "escalate"

    def test_runtime_cannot_deescalate(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-4",
            new_level="hold",
        )
        with pytest.raises(InvalidEscalationTransition, match="cannot de-escalate"):
            transition_escalation(
                conn, entity_type="dispatch", entity_id="d-4",
                new_level="info",
                actor="runtime",
            )

    def test_operator_can_deescalate_hold(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-5",
            new_level="hold",
        )
        result = transition_escalation(
            conn, entity_type="dispatch", entity_id="d-5",
            new_level="info",
            actor="operator",
        )
        assert result["escalation_level"] == "info"

    def test_operator_cannot_deescalate_escalate(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-6",
            new_level="escalate",
        )
        with pytest.raises(InvalidEscalationTransition, match="lacks authority"):
            transition_escalation(
                conn, entity_type="dispatch", entity_id="d-6",
                new_level="info",
                actor="operator",
            )

    def test_t0_can_deescalate_escalate(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-7",
            new_level="escalate",
        )
        result = transition_escalation(
            conn, entity_type="dispatch", entity_id="d-7",
            new_level="info",
            actor="t0",
        )
        assert result["escalation_level"] == "info"
        assert result["resolved_at"] is not None
        assert result["resolved_by"] == "t0"

    def test_invalid_escalation_level_raises(self, conn):
        with pytest.raises(InvalidEscalationTransition, match="Unknown escalation level"):
            transition_escalation(
                conn, entity_type="dispatch", entity_id="d-8",
                new_level="invalid_level",
            )

    def test_escalation_emits_event(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="d-9",
            new_level="hold",
            trigger_category="budget_exhausted",
        )
        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'escalation_transition' AND entity_id = 'd-9'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert e["from_state"] == "info"
        assert e["to_state"] == "hold"

    def test_skip_severity_levels(self, conn):
        """info -> escalate (skipping review_required and hold) is valid."""
        result = transition_escalation(
            conn, entity_type="dispatch", entity_id="d-10",
            new_level="escalate",
            trigger_category="forbidden_action",
        )
        assert result["escalation_level"] == "escalate"


# ---------------------------------------------------------------------------
# Blocking checks
# ---------------------------------------------------------------------------

class TestBlocking:
    def test_info_not_blocked(self, conn):
        assert not is_blocked(conn, "dispatch", "unset")

    def test_review_required_not_blocked(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="b-1",
            new_level="review_required",
        )
        assert not is_blocked(conn, "dispatch", "b-1")

    def test_hold_is_blocked(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="b-2",
            new_level="hold",
        )
        assert is_blocked(conn, "dispatch", "b-2")

    def test_escalate_is_blocked(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="b-3",
            new_level="escalate",
        )
        assert is_blocked(conn, "dispatch", "b-3")


# ---------------------------------------------------------------------------
# Override recording
# ---------------------------------------------------------------------------

class TestOverrides:
    def test_record_granted_override(self, conn):
        override = record_override(
            conn,
            entity_type="dispatch",
            entity_id="o-1",
            actor="t0",
            override_type="hold_release",
            justification="Reviewed and resolved",
        )
        assert override["outcome"] == "granted"
        assert override["actor"] == "t0"
        assert override["override_type"] == "hold_release"

    def test_record_denied_override(self, conn):
        override = record_override(
            conn,
            entity_type="dispatch",
            entity_id="o-2",
            actor="operator",
            override_type="gate_bypass",
            justification="Requested bypass",
            outcome="denied",
        )
        assert override["outcome"] == "denied"

    def test_override_without_authority_raises(self, conn):
        with pytest.raises(GovernanceError, match="lacks override authority"):
            record_override(
                conn,
                entity_type="dispatch",
                entity_id="o-3",
                actor="runtime",
                override_type="hold_release",
                justification="Attempted by runtime",
            )

    def test_override_without_justification_raises(self, conn):
        with pytest.raises(GovernanceError, match="justification is required"):
            record_override(
                conn,
                entity_type="dispatch",
                entity_id="o-4",
                actor="t0",
                override_type="hold_release",
                justification="",
            )

    def test_override_with_unknown_type_raises(self, conn):
        with pytest.raises(GovernanceError, match="Unknown override type"):
            record_override(
                conn,
                entity_type="dispatch",
                entity_id="o-5",
                actor="t0",
                override_type="unknown_type",
                justification="test",
            )

    def test_override_emits_event(self, conn):
        record_override(
            conn,
            entity_type="dispatch",
            entity_id="o-6",
            actor="t0",
            override_type="escalation_resolve",
            justification="Resolved by T0 after review",
        )
        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'governance_override' AND entity_id = 'o-6'"
        ).fetchall()
        assert len(events) == 1

    def test_hold_release_deescalates(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="o-7",
            new_level="hold",
        )
        record_override(
            conn,
            entity_type="dispatch",
            entity_id="o-7",
            actor="operator",
            override_type="hold_release",
            justification="Hold cleared after review",
        )
        level = get_escalation_level(conn, "dispatch", "o-7")
        assert level == "info"

    def test_get_overrides(self, conn):
        record_override(
            conn, entity_type="dispatch", entity_id="o-8",
            actor="t0", override_type="gate_bypass",
            justification="Emergency bypass",
        )
        record_override(
            conn, entity_type="dispatch", entity_id="o-8",
            actor="t0", override_type="hold_release",
            justification="Second override",
        )
        overrides = get_overrides(conn, entity_id="o-8")
        assert len(overrides) == 2


# ---------------------------------------------------------------------------
# Operator summaries
# ---------------------------------------------------------------------------

class TestEscalationSummary:
    def test_empty_summary(self, conn):
        summary = escalation_summary(conn)
        assert summary["total_unresolved"] == 0
        assert summary["blocking_count"] == 0

    def test_summary_with_holds(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="s-1",
            new_level="hold",
            trigger_description="Budget exhausted",
        )
        transition_escalation(
            conn, entity_type="dispatch", entity_id="s-2",
            new_level="escalate",
            trigger_description="Forbidden action",
        )
        summary = escalation_summary(conn)
        assert summary["total_unresolved"] == 2
        assert summary["blocking_count"] == 2
        assert len(summary["holds"]) == 1
        assert len(summary["escalations"]) == 1

    def test_summary_enforcement_flag(self, conn):
        summary = escalation_summary(conn)
        assert summary["enforcement_active"] is False


# ---------------------------------------------------------------------------
# check_action (combined evaluation + blocking)
# ---------------------------------------------------------------------------

class TestCheckAction:
    def test_check_automatic_action(self, conn):
        result = check_action(
            conn, action="heartbeat_check", actor="runtime",
        )
        assert result["outcome"] == "automatic"
        assert result["blocked"] is False

    def test_check_blocked_entity(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="ca-1",
            new_level="hold",
        )
        result = check_action(
            conn, action="dispatch_run", actor="runtime",
            context={"dispatch_id": "ca-1"},
        )
        assert result["blocked"] is True

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1"})
    def test_enforcement_blocks_forbidden(self, conn):
        with pytest.raises(ForbiddenActionError, match="Forbidden action"):
            check_action(
                conn, action="branch_merge", actor="runtime",
                context={"dispatch_id": "ca-2"},
            )
        # Verify escalation was recorded
        level = get_escalation_level(conn, "dispatch", "ca-2")
        assert level == "escalate"

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0"})
    def test_shadow_mode_does_not_block_forbidden(self, conn):
        result = check_action(
            conn, action="branch_merge", actor="runtime",
            context={"dispatch_id": "ca-3"},
        )
        assert result["outcome"] == "forbidden"
        assert result["enforcement"] is False

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1"})
    def test_enforcement_allows_t0_forbidden(self, conn):
        result = check_action(
            conn, action="branch_merge", actor="t0",
            context={"dispatch_id": "ca-4"},
        )
        assert result["outcome"] == "gated"
        assert result["enforcement"] is True


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0"})
    def test_enforcement_disabled_by_default(self):
        assert not is_enforcement_enabled()

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1"})
    def test_enforcement_enabled(self):
        assert is_enforcement_enabled()


# ---------------------------------------------------------------------------
# Unresolved escalations query
# ---------------------------------------------------------------------------

class TestUnresolvedEscalations:
    def test_get_unresolved(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="u-1",
            new_level="hold",
        )
        transition_escalation(
            conn, entity_type="dispatch", entity_id="u-2",
            new_level="review_required",
        )
        transition_escalation(
            conn, entity_type="dispatch", entity_id="u-3",
            new_level="escalate",
        )
        results = get_unresolved_escalations(conn, min_level="hold")
        assert len(results) == 2
        levels = {r["escalation_level"] for r in results}
        assert levels == {"hold", "escalate"}

    def test_resolved_excluded(self, conn):
        transition_escalation(
            conn, entity_type="dispatch", entity_id="u-4",
            new_level="hold",
        )
        transition_escalation(
            conn, entity_type="dispatch", entity_id="u-4",
            new_level="info",
            actor="operator",
        )
        results = get_unresolved_escalations(conn, min_level="hold")
        entity_ids = {r["entity_id"] for r in results}
        assert "u-4" not in entity_ids
