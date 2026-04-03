#!/usr/bin/env python3
"""LocalSessionAdapter lifecycle and attempt tracking tests.

Covers:
  1. Session creation and lifecycle states
  2. Attempt tracking and rollover
  3. Timeout handling
  4. Completion and abnormal exit
  5. Session/attempt correlation
  6. RuntimeAdapter conformance
  7. Unsupported operations
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from adapter_protocol import RuntimeAdapter, validate_required_capabilities
from local_session_adapter import LocalSessionAdapter, SessionAttempt
from tmux_adapter import UnsupportedCapability


# ---------------------------------------------------------------------------
# 1. Session creation and lifecycle states
# ---------------------------------------------------------------------------

class TestSessionCreation:

    def test_spawn_creates_session(self) -> None:
        adapter = LocalSessionAdapter()
        result = adapter.spawn("T0", {"command": "sleep 30"})
        assert result.success is True
        session = adapter.get_session("T0")
        assert session is not None
        assert session.current_attempt.state == "RUNNING"
        adapter.stop("T0")

    def test_spawn_idempotent_while_running(self) -> None:
        adapter = LocalSessionAdapter()
        r1 = adapter.spawn("T0", {"command": "sleep 30"})
        r2 = adapter.spawn("T0", {"command": "sleep 30"})
        assert r1.transport_ref == r2.transport_ref
        adapter.stop("T0")

    def test_spawn_missing_command_fails(self) -> None:
        adapter = LocalSessionAdapter()
        result = adapter.spawn("T0", {})
        assert result.success is False
        assert "command" in result.error

    def test_stop_transitions_to_completed(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.stop("T0")
        attempt = adapter.get_attempt("T0")
        assert attempt.state == "COMPLETED"
        assert attempt.ended_at is not None

    def test_stop_nonexistent_succeeds(self) -> None:
        adapter = LocalSessionAdapter()
        result = adapter.stop("T9")
        assert result.success is True
        assert result.was_running is False


# ---------------------------------------------------------------------------
# 2. Attempt tracking and rollover
# ---------------------------------------------------------------------------

class TestAttemptTracking:

    def test_first_attempt_is_number_1(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        assert adapter.get_attempt("T0").attempt_number == 1
        adapter.stop("T0")

    def test_respawn_increments_attempt(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.stop("T0")
        adapter.spawn("T0", {"command": "sleep 30"})
        assert adapter.get_attempt("T0").attempt_number == 2
        session = adapter.get_session("T0")
        assert len(session.attempts) == 2
        adapter.stop("T0")

    def test_attempt_carries_dispatch_id(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30", "dispatch_id": "20260403-120000-test"})
        assert adapter.get_attempt("T0").dispatch_id == "20260403-120000-test"
        adapter.stop("T0")

    def test_deliver_updates_dispatch_id(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.deliver("T0", "20260403-130000-new")
        assert adapter.get_attempt("T0").dispatch_id == "20260403-130000-new"
        adapter.stop("T0")

    def test_attempt_has_timestamps(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        attempt = adapter.get_attempt("T0")
        assert attempt.started_at is not None
        assert attempt.ended_at is None  # still running
        adapter.stop("T0")
        assert adapter.get_attempt("T0").ended_at is not None


# ---------------------------------------------------------------------------
# 3. Timeout handling
# ---------------------------------------------------------------------------

class TestTimeoutHandling:

    def test_mark_timed_out(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.mark_timed_out("T0")
        attempt = adapter.get_attempt("T0")
        assert attempt.state == "TIMED_OUT"
        assert attempt.failure_reason == "Execution timed out"
        assert attempt.ended_at is not None

    def test_timed_out_kills_process(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.mark_timed_out("T0")
        health = adapter.health("T0")
        assert health.process_alive is False


# ---------------------------------------------------------------------------
# 4. Completion and abnormal exit
# ---------------------------------------------------------------------------

class TestCompletionAndAbnormalExit:

    def test_mark_failed_with_reason(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.mark_failed("T0", reason="Import error")
        attempt = adapter.get_attempt("T0")
        assert attempt.state == "FAILED"
        assert attempt.failure_reason == "Import error"

    def test_natural_exit_detected_on_observe(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "true"})
        time.sleep(0.2)  # let process exit
        obs = adapter.observe("T0")
        attempt = adapter.get_attempt("T0")
        assert attempt.state == "COMPLETED"

    def test_abnormal_exit_detected_on_health(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "false"})
        time.sleep(0.2)
        adapter.health("T0")
        attempt = adapter.get_attempt("T0")
        assert attempt.state == "FAILED"
        assert attempt.exit_code != 0

    def test_deliver_to_dead_session_fails(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "true"})
        time.sleep(0.2)
        result = adapter.deliver("T0", "dispatch-1")
        assert result.success is False


# ---------------------------------------------------------------------------
# 5. Session/attempt correlation
# ---------------------------------------------------------------------------

class TestCorrelation:

    def test_multiple_attempts_preserved(self) -> None:
        adapter = LocalSessionAdapter()
        for i in range(3):
            adapter.spawn("T0", {"command": "sleep 30", "dispatch_id": f"d-{i}"})
            adapter.stop("T0")
        session = adapter.get_session("T0")
        assert session.total_attempts == 3
        assert len(session.attempts) == 3
        assert session.attempts[0].dispatch_id == "d-0"
        assert session.attempts[2].dispatch_id == "d-2"

    def test_observe_includes_lifecycle_state(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        obs = adapter.observe("T0")
        assert obs.transport_state["lifecycle_state"] == "RUNNING"
        assert obs.transport_state["attempt_number"] == 1
        adapter.stop("T0")

    def test_inspect_includes_attempt_metadata(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30", "dispatch_id": "d-test"})
        insp = adapter.inspect("T0")
        assert insp.transport_details["dispatch_id"] == "d-test"
        assert insp.transport_details["attempt_number"] == 1
        adapter.stop("T0")


# ---------------------------------------------------------------------------
# 6. RuntimeAdapter conformance
# ---------------------------------------------------------------------------

class TestConformance:

    def test_is_runtime_adapter(self) -> None:
        assert isinstance(LocalSessionAdapter(), RuntimeAdapter)

    def test_has_required_capabilities(self) -> None:
        missing = validate_required_capabilities(LocalSessionAdapter())
        assert missing == []

    def test_adapter_type(self) -> None:
        assert LocalSessionAdapter().adapter_type() == "local_session"

    def test_shutdown_stops_all(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T0", {"command": "sleep 30"})
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.shutdown()
        assert adapter.health("T0").process_alive is False
        assert adapter.health("T1").process_alive is False


# ---------------------------------------------------------------------------
# 7. Unsupported operations
# ---------------------------------------------------------------------------

class TestUnsupported:

    def test_attach_raises(self) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            LocalSessionAdapter().attach("T0")
        assert exc_info.value.operation == "ATTACH"

    def test_reheal_raises(self) -> None:
        with pytest.raises(UnsupportedCapability) as exc_info:
            LocalSessionAdapter().reheal("T0")
        assert exc_info.value.operation == "REHEAL"
