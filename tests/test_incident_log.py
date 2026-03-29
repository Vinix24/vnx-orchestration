#!/usr/bin/env python3
"""
Tests for VNX Incident Log — PR-1 durable incident substrate and retry budgets.

Covers:
  - Incident creation with dispatch/terminal/component context
  - Retry budget creation from contract defaults
  - Budget check: allowed, cooldown, exhausted, halted
  - Budget consumption and cooldown calculation
  - Budget reset after successful recovery
  - Repeated-failure-loop detection
  - Incident resolution and escalation state transitions
  - Incident summary generation (no shell log parsing)
  - Shadow mode isolation (VNX_INCIDENT_SHADOW=0 is a no-op)
  - Schema initialization is idempotent
"""

import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

# Add scripts/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from incident_log import (
    check_budget,
    consume_budget,
    create_incident,
    detect_repeated_failure_loop,
    escalate_incident,
    generate_incident_summary,
    get_active_incidents,
    get_budget,
    get_incident_history,
    is_in_cooldown,
    is_shadow_mode,
    resolve_incident,
    reset_budget,
)
from incident_taxonomy import (
    IncidentClass,
    REPEATED_FAILURE_THRESHOLD,
    Severity,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Provide a temporary state directory with initialized schema."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    return sd


# ---------------------------------------------------------------------------
# Schema idempotency
# ---------------------------------------------------------------------------

class TestSchemaInit:
    def test_schema_idempotent(self, state_dir):
        """init_schema can be called multiple times without error."""
        init_schema(state_dir)
        init_schema(state_dir)

    def test_incident_log_table_exists(self, state_dir):
        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_log'"
            ).fetchone()
        assert row is not None, "incident_log table must exist after schema init"

    def test_retry_budgets_table_exists(self, state_dir):
        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='retry_budgets'"
            ).fetchone()
        assert row is not None, "retry_budgets table must exist after schema init"

    def test_schema_version_2_recorded(self, state_dir):
        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT version FROM runtime_schema_version WHERE version = 2"
            ).fetchone()
        assert row is not None, "schema version 2 (PR-1) must be recorded"


# ---------------------------------------------------------------------------
# Incident creation
# ---------------------------------------------------------------------------

class TestCreateIncident:
    def test_creates_dispatch_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.DELIVERY_FAILURE,
            entity_type="dispatch",
            entity_id="dispatch-001",
            dispatch_id="dispatch-001",
            failure_detail="pane not found",
        )
        assert incident["incident_id"] is not None
        assert incident["incident_class"] == "delivery_failure"
        assert incident["entity_type"] == "dispatch"
        assert incident["entity_id"] == "dispatch-001"
        assert incident["state"] == "open"
        assert incident["attempt_count"] == 0
        assert incident["budget_exhausted"] == 0
        assert incident["failure_detail"] == "pane not found"

    def test_creates_terminal_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
            entity_type="terminal",
            entity_id="T2",
            terminal_id="T2",
        )
        assert incident["incident_class"] == "terminal_unresponsive"
        assert incident["terminal_id"] == "T2"
        assert incident["severity"] == Severity.ERROR.value

    def test_creates_component_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
            component_name="dispatcher",
        )
        assert incident["incident_class"] == "process_crash"
        assert incident["component_name"] == "dispatcher"
        assert incident["severity"] == Severity.WARNING.value

    def test_severity_override(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
            severity_override="critical",
        )
        assert incident["severity"] == "critical"

    def test_rejects_invalid_incident_class(self, state_dir):
        with pytest.raises(ValueError, match="Unknown incident class"):
            create_incident(
                state_dir,
                incident_class="not_a_class",
                entity_type="component",
                entity_id="dispatcher",
            )

    def test_incident_has_occurred_at(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.ACK_TIMEOUT,
            entity_type="dispatch",
            entity_id="d-abc",
        )
        assert incident["occurred_at"] is not None
        assert "T" in incident["occurred_at"]  # ISO format

    def test_all_incident_classes_can_be_created(self, state_dir):
        for ic in IncidentClass:
            incident = create_incident(
                state_dir,
                incident_class=ic,
                entity_type="component",
                entity_id=f"test-{ic.value}",
            )
            assert incident["incident_class"] == ic.value


# ---------------------------------------------------------------------------
# Retry budget management
# ---------------------------------------------------------------------------

class TestRetryBudgets:
    def test_get_budget_creates_from_contract(self, state_dir):
        budget = get_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        # process_crash contract: max_retries=3, cooldown=10, backoff=2.0
        assert budget["max_retries"] == 3
        assert budget["cooldown_seconds"] == 10
        assert budget["backoff_factor"] == 2.0
        assert budget["attempts_used"] == 0

    def test_get_budget_idempotent(self, state_dir):
        b1 = get_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        b2 = get_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert b1["budget_key"] == b2["budget_key"]
        assert b1["id"] == b2["id"]

    def test_budget_key_format(self, state_dir):
        budget = get_budget(
            state_dir,
            entity_type="terminal",
            entity_id="T2",
            incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
        )
        assert budget["budget_key"] == "terminal:T2:terminal_unresponsive"

    def test_different_entities_have_separate_budgets(self, state_dir):
        b1 = get_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        b2 = get_budget(
            state_dir,
            entity_type="component",
            entity_id="smart_tap",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert b1["budget_key"] != b2["budget_key"]


# ---------------------------------------------------------------------------
# Budget checks
# ---------------------------------------------------------------------------

class TestCheckBudget:
    def test_fresh_budget_is_allowed(self, state_dir):
        result = check_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result["allowed"] is True
        assert result["reason"] == "ok"
        assert result["in_cooldown"] is False

    def test_exhausted_budget_not_allowed(self, state_dir):
        # process_crash: max_retries=3 → consume 3 times
        for _ in range(3):
            consume_budget(
                state_dir,
                entity_type="component",
                entity_id="dispatcher",
                incident_class=IncidentClass.PROCESS_CRASH,
            )
        result = check_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result["allowed"] is False
        assert "budget_exhausted" in result["reason"]
        assert result["attempts_used"] == 3

    def test_halted_recovery_not_allowed(self, state_dir):
        # lease_conflict contract: halt_auto_recovery=True, escalate_after_retries=0
        # After first consume the halt should activate
        consume_budget(
            state_dir,
            entity_type="terminal",
            entity_id="T1",
            incident_class=IncidentClass.LEASE_CONFLICT,
        )
        result = check_budget(
            state_dir,
            entity_type="terminal",
            entity_id="T1",
            incident_class=IncidentClass.LEASE_CONFLICT,
        )
        assert result["allowed"] is False
        assert "auto_recovery_halted" in result["reason"]


# ---------------------------------------------------------------------------
# Budget consumption and cooldown
# ---------------------------------------------------------------------------

class TestConsumeBudget:
    def test_consume_increments_attempts(self, state_dir):
        b1 = consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert b1["attempts_used"] == 1

        b2 = consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert b2["attempts_used"] == 2

    def test_consume_sets_cooldown(self, state_dir):
        budget = consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        # process_crash: cooldown_seconds=10 — should set next_allowed_at
        assert budget["next_allowed_at"] is not None

    def test_consume_updates_linked_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        iid = incident["incident_id"]

        consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
            incident_id=iid,
        )

        with get_connection(state_dir) as conn:
            updated = dict(conn.execute(
                "SELECT * FROM incident_log WHERE incident_id = ?", (iid,)
            ).fetchone())
        assert updated["attempt_count"] == 1

    def test_budget_exhausted_flag_set_on_last_attempt(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        iid = incident["incident_id"]

        # Exhaust the budget (max_retries=3)
        for _ in range(3):
            consume_budget(
                state_dir,
                entity_type="component",
                entity_id="dispatcher",
                incident_class=IncidentClass.PROCESS_CRASH,
                incident_id=iid,
            )

        with get_connection(state_dir) as conn:
            updated = dict(conn.execute(
                "SELECT * FROM incident_log WHERE incident_id = ?", (iid,)
            ).fetchone())
        assert updated["budget_exhausted"] == 1

    def test_cooldown_gating_respects_next_allowed_at(self, state_dir):
        consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        result = check_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        # After first consume, cooldown window is active
        assert result["in_cooldown"] is True
        assert result["allowed"] is False
        assert "in_cooldown" in result["reason"]


# ---------------------------------------------------------------------------
# Budget reset
# ---------------------------------------------------------------------------

class TestResetBudget:
    def test_reset_clears_attempts(self, state_dir):
        for _ in range(2):
            consume_budget(
                state_dir,
                entity_type="component",
                entity_id="dispatcher",
                incident_class=IncidentClass.PROCESS_CRASH,
            )
        reset_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        budget = get_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert budget["attempts_used"] == 0
        assert budget["next_allowed_at"] is None
        assert budget["escalated_at"] is None
        assert budget["auto_recovery_halted"] == 0

    def test_check_budget_allowed_after_reset(self, state_dir):
        for _ in range(2):
            consume_budget(
                state_dir,
                entity_type="component",
                entity_id="dispatcher",
                incident_class=IncidentClass.PROCESS_CRASH,
            )
        reset_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        result = check_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# Repeated failure loop detection
# ---------------------------------------------------------------------------

class TestRepeatedFailureLoop:
    def test_not_detected_below_threshold(self, state_dir):
        for _ in range(REPEATED_FAILURE_THRESHOLD - 1):
            create_incident(
                state_dir,
                incident_class=IncidentClass.PROCESS_CRASH,
                entity_type="component",
                entity_id="dispatcher",
            )
        result = detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is False

    def test_detected_at_threshold(self, state_dir):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            create_incident(
                state_dir,
                incident_class=IncidentClass.PROCESS_CRASH,
                entity_type="component",
                entity_id="dispatcher",
            )
        result = detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is True

    def test_custom_threshold(self, state_dir):
        for _ in range(2):
            create_incident(
                state_dir,
                incident_class=IncidentClass.PROCESS_CRASH,
                entity_type="component",
                entity_id="dispatcher",
            )
        assert detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
            threshold=2,
        ) is True
        assert detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
            threshold=10,
        ) is False

    def test_different_class_does_not_count(self, state_dir):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            create_incident(
                state_dir,
                incident_class=IncidentClass.DELIVERY_FAILURE,
                entity_type="component",
                entity_id="dispatcher",
            )
        result = detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,  # different class
        )
        assert result is False

    def test_different_entity_does_not_count(self, state_dir):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            create_incident(
                state_dir,
                incident_class=IncidentClass.PROCESS_CRASH,
                entity_type="component",
                entity_id="smart_tap",  # different entity
            )
        result = detect_repeated_failure_loop(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is False


# ---------------------------------------------------------------------------
# Incident resolution and escalation
# ---------------------------------------------------------------------------

class TestIncidentStateTransitions:
    def test_resolve_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        resolved = resolve_incident(state_dir, incident["incident_id"])
        assert resolved["state"] == "resolved"
        assert resolved["resolved_at"] is not None

    def test_escalate_incident(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
            entity_type="terminal",
            entity_id="T1",
        )
        escalated = escalate_incident(state_dir, incident["incident_id"])
        assert escalated["state"] == "escalated"
        assert escalated["escalated"] == 1

    def test_resolve_unknown_incident_raises(self, state_dir):
        with pytest.raises(KeyError):
            resolve_incident(state_dir, "nonexistent-id")


# ---------------------------------------------------------------------------
# Active incident queries
# ---------------------------------------------------------------------------

class TestActiveIncidents:
    def test_get_active_returns_open_and_escalated(self, state_dir):
        i1 = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        i2 = create_incident(
            state_dir,
            incident_class=IncidentClass.DELIVERY_FAILURE,
            entity_type="dispatch",
            entity_id="d-001",
        )
        escalate_incident(state_dir, i2["incident_id"])

        active = get_active_incidents(state_dir)
        ids = {r["incident_id"] for r in active}
        assert i1["incident_id"] in ids
        assert i2["incident_id"] in ids

    def test_get_active_excludes_resolved(self, state_dir):
        incident = create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        resolve_incident(state_dir, incident["incident_id"])

        active = get_active_incidents(state_dir)
        ids = {r["incident_id"] for r in active}
        assert incident["incident_id"] not in ids

    def test_filter_by_entity(self, state_dir):
        create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="smart_tap",
        )
        active = get_active_incidents(state_dir, entity_id="dispatcher")
        assert all(r["entity_id"] == "dispatcher" for r in active)
        assert len(active) == 1

    def test_filter_by_dispatch_id(self, state_dir):
        create_incident(
            state_dir,
            incident_class=IncidentClass.DELIVERY_FAILURE,
            entity_type="dispatch",
            entity_id="d-001",
            dispatch_id="d-001",
        )
        create_incident(
            state_dir,
            incident_class=IncidentClass.DELIVERY_FAILURE,
            entity_type="dispatch",
            entity_id="d-002",
            dispatch_id="d-002",
        )
        active = get_active_incidents(state_dir, dispatch_id="d-001")
        assert all(r["dispatch_id"] == "d-001" for r in active)
        assert len(active) == 1


# ---------------------------------------------------------------------------
# Incident summary (no shell log parsing)
# ---------------------------------------------------------------------------

class TestIncidentSummary:
    def test_empty_summary(self, state_dir):
        summary = generate_incident_summary(state_dir)
        assert summary["total_open"] == 0
        assert summary["total_escalated"] == 0
        assert summary["critical_count"] == 0
        assert summary["budgets_exhausted"] == 0
        assert summary["auto_recovery_halted_count"] == 0
        assert "generated_at" in summary
        assert isinstance(summary["active_incidents"], list)

    def test_summary_counts_open_incidents(self, state_dir):
        for _ in range(3):
            create_incident(
                state_dir,
                incident_class=IncidentClass.PROCESS_CRASH,
                entity_type="component",
                entity_id="dispatcher",
            )
        summary = generate_incident_summary(state_dir)
        assert summary["total_open"] == 3

    def test_summary_counts_critical(self, state_dir):
        create_incident(
            state_dir,
            incident_class=IncidentClass.REPEATED_FAILURE_LOOP,
            entity_type="component",
            entity_id="dispatcher",
        )
        summary = generate_incident_summary(state_dir)
        assert summary["critical_count"] == 1

    def test_summary_shows_incidents_by_class(self, state_dir):
        create_incident(
            state_dir,
            incident_class=IncidentClass.PROCESS_CRASH,
            entity_type="component",
            entity_id="dispatcher",
        )
        create_incident(
            state_dir,
            incident_class=IncidentClass.DELIVERY_FAILURE,
            entity_type="dispatch",
            entity_id="d-001",
        )
        summary = generate_incident_summary(state_dir)
        by_class = summary["incidents_by_class"]
        assert "process_crash" in by_class
        assert "delivery_failure" in by_class
        assert by_class["process_crash"]["open"] == 1
        assert by_class["delivery_failure"]["open"] == 1

    def test_summary_reports_exhausted_budgets(self, state_dir):
        # Exhaust process_crash budget (max_retries=3)
        for _ in range(3):
            consume_budget(
                state_dir,
                entity_type="component",
                entity_id="dispatcher",
                incident_class=IncidentClass.PROCESS_CRASH,
            )
        summary = generate_incident_summary(state_dir)
        assert summary["budgets_exhausted"] == 1
        exhausted = summary["exhausted_budgets"]
        assert any(b["entity_id"] == "dispatcher" for b in exhausted)

    def test_summary_reports_halted_recoveries(self, state_dir):
        # lease_conflict halts on first consume
        consume_budget(
            state_dir,
            entity_type="terminal",
            entity_id="T1",
            incident_class=IncidentClass.LEASE_CONFLICT,
        )
        summary = generate_incident_summary(state_dir)
        assert summary["auto_recovery_halted_count"] == 1
        halted = summary["halted_recoveries"]
        assert any(b["entity_id"] == "T1" for b in halted)

    def test_summary_does_not_require_shell_logs(self, state_dir):
        """Summary is generated from SQLite state only — no file reads needed."""
        # Just verify it works in a pristine state dir with no log files
        summary = generate_incident_summary(state_dir)
        assert isinstance(summary, dict)
        assert "generated_at" in summary


# ---------------------------------------------------------------------------
# Shadow mode isolation
# ---------------------------------------------------------------------------

class TestShadowMode:
    def test_shadow_mode_on_by_default(self, monkeypatch):
        monkeypatch.delenv("VNX_INCIDENT_SHADOW", raising=False)
        assert is_shadow_mode() is True

    def test_shadow_mode_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("VNX_INCIDENT_SHADOW", "0")
        assert is_shadow_mode() is False

    def test_shadow_mode_on_when_set_to_1(self, monkeypatch):
        monkeypatch.setenv("VNX_INCIDENT_SHADOW", "1")
        assert is_shadow_mode() is True

    def test_supervisor_shadow_crash_noop_when_disabled(self, monkeypatch, state_dir):
        """When shadow mode is off, record_process_crash is a no-op."""
        monkeypatch.setenv("VNX_INCIDENT_SHADOW", "0")

        # Import here so monkeypatch takes effect
        from supervisor_shadow import record_process_crash
        result = record_process_crash(
            "dispatcher",
            pid=12345,
            state_dir=state_dir,
        )
        assert result is None

        # No incidents should have been written
        with get_connection(state_dir) as conn:
            count = conn.execute("SELECT COUNT(*) AS cnt FROM incident_log").fetchone()["cnt"]
        assert count == 0

    def test_supervisor_shadow_crash_records_when_enabled(self, monkeypatch, state_dir):
        """When shadow mode is on, record_process_crash writes an incident."""
        monkeypatch.setenv("VNX_INCIDENT_SHADOW", "1")

        from supervisor_shadow import record_process_crash
        iid = record_process_crash(
            "dispatcher",
            pid=12345,
            failure_detail="process not found",
            state_dir=state_dir,
        )
        assert iid is not None

        with get_connection(state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM incident_log WHERE incident_id = ?", (iid,)
            ).fetchone()
        assert row is not None
        assert row["incident_class"] == "process_crash"
        assert row["component_name"] == "dispatcher"

    def test_shadow_does_not_affect_existing_supervisor_state(self, monkeypatch, state_dir, tmp_path):
        """Shadow mode never touches restart_tracking files used by supervisor."""
        monkeypatch.setenv("VNX_INCIDENT_SHADOW", "1")
        restart_dir = tmp_path / "restart_tracking"
        restart_dir.mkdir()
        tracking_file = restart_dir / "dispatcher.txt"
        tracking_file.write_text("2|1711700000")  # simulate bash tracking state

        from supervisor_shadow import record_process_crash
        record_process_crash("dispatcher", state_dir=state_dir)

        # The bash tracking file must be untouched
        assert tracking_file.read_text() == "2|1711700000"


# ---------------------------------------------------------------------------
# Cooldown helper
# ---------------------------------------------------------------------------

class TestCooldownHelper:
    def test_not_in_cooldown_before_first_consume(self, state_dir):
        result = is_in_cooldown(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is False

    def test_in_cooldown_after_consume(self, state_dir):
        consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        result = is_in_cooldown(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is True

    def test_not_in_cooldown_after_reset(self, state_dir):
        consume_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        reset_budget(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        result = is_in_cooldown(
            state_dir,
            entity_type="component",
            entity_id="dispatcher",
            incident_class=IncidentClass.PROCESS_CRASH,
        )
        assert result is False
