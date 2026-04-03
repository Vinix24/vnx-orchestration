#!/usr/bin/env python3
"""PR-4 certification tests for Feature 17: Rich Headless Runtime Sessions.

Certifies that:
  1. Session/attempt lifecycle correctness under success, timeout, and failure
  2. Event-stream and artifact correlation integrity
  3. Provider-aware observability and explicit fallback semantics
  4. Contract-to-implementation alignment
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Set

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from adapter_protocol import RuntimeAdapter, REQUIRED_CAPABILITIES, validate_required_capabilities
from headless_event_stream import (
    CANONICAL_ORDER,
    TERMINAL_EVENT_TYPES,
    VALID_EVENT_TYPES,
    CorrelationKeys,
    HeadlessEvent,
    HeadlessEventStream,
)
from local_session_adapter import (
    LOCAL_SESSION_CAPABILITIES,
    LocalSessionAdapter,
    SessionAttempt,
)
from provider_observability import (
    PROVIDER_REGISTRY,
    UNKNOWN_PROVIDER_CAPABILITIES,
    ObservabilityQuality,
    ProviderCapabilities,
    get_provider_capabilities,
    is_provider_known,
)
from tmux_adapter import UnsupportedCapability


# ===================================================================
# Section 1: Session/Attempt Lifecycle Certification
# ===================================================================

class TestSessionLifecycleCertification:
    """Certify session lifecycle under success, timeout, and failure."""

    def test_success_lifecycle(self) -> None:
        """Spawn -> running -> stop -> completed."""
        adapter = LocalSessionAdapter()
        result = adapter.spawn("T1", {"command": "sleep 30"})
        assert result.success
        session = adapter.get_session("T1")
        assert session is not None
        assert session.current_attempt.state == "RUNNING"
        stop = adapter.stop("T1")
        assert stop.success

    def test_timeout_lifecycle(self) -> None:
        """Spawn -> running -> mark_timed_out -> TIMED_OUT."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.mark_timed_out("T1")
        session = adapter.get_session("T1")
        assert session.current_attempt.state == "TIMED_OUT"

    def test_failure_lifecycle(self) -> None:
        """Spawn -> running -> mark_failed -> FAILED."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.mark_failed("T1", reason="test failure")
        session = adapter.get_session("T1")
        assert session.current_attempt.state == "FAILED"
        assert session.current_attempt.failure_reason == "test failure"

    def test_retry_creates_new_attempt(self) -> None:
        """Retry increments attempt number and creates new attempt."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.mark_failed("T1", reason="first failure")
        first = adapter.get_session("T1")
        first_attempt_num = first.current_attempt.attempt_number

        adapter.spawn("T1", {"command": "sleep 30"})
        second = adapter.get_session("T1")
        assert second.current_attempt.attempt_number == first_attempt_num + 1
        assert second.total_attempts == 2
        adapter.stop("T1")

    def test_attempt_preserves_history(self) -> None:
        """All attempts are preserved in session history."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.mark_failed("T1", reason="fail 1")
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.mark_timed_out("T1")
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.stop("T1")

        session = adapter.get_session("T1")
        assert len(session.attempts) == 3
        assert session.attempts[0].state == "FAILED"
        assert session.attempts[1].state == "TIMED_OUT"

    def test_session_state_derives_from_attempt(self) -> None:
        """Session has no independent state machine — derived from attempts."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        obs = adapter.observe("T1")
        assert obs.exists is True
        assert obs.transport_state.get("process_alive") is True
        adapter.stop("T1")

    def test_stop_idempotent(self) -> None:
        """Stopping already-stopped session returns success."""
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        adapter.stop("T1")
        result = adapter.stop("T1")
        assert result.success is True
        assert result.was_running is False


# ===================================================================
# Section 2: Event Stream And Artifact Correlation
# ===================================================================

class TestEventStreamCertification:
    """Certify event stream integrity and artifact correlation."""

    def test_full_success_lifecycle_events(self) -> None:
        """Success lifecycle produces events in canonical order."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {"provider": "claude"})
        stream.emit("subprocess_launched", {"pid": 123})
        stream.emit("heartbeat", {"elapsed": 5.0})
        stream.record_artifact("log", "/tmp/log.txt")
        stream.emit("session_completed", {"exit_code": 0})

        assert stream.is_terminated
        timeline = stream.timeline()
        assert timeline[0]["event_type"] == "session_created"
        assert timeline[-1]["event_type"] == "session_completed"

    def test_failure_lifecycle_events(self) -> None:
        """Failure lifecycle produces correct terminal event."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("subprocess_launched", {"pid": 456})
        stream.emit("session_failed", {"exit_code": 1, "failure_class": "TOOL_FAIL"})

        assert stream.is_terminated
        timeline = stream.timeline()
        assert timeline[-1]["event_type"] == "session_failed"

    def test_timeout_lifecycle_events(self) -> None:
        """Timeout lifecycle produces correct terminal event."""
        corr = CorrelationKeys(session_id="T2", attempt_number=1, dispatch_id="d-2")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("subprocess_launched", {"pid": 789})
        stream.emit("session_timed_out", {"timeout": 300})

        assert stream.is_terminated
        timeline = stream.timeline()
        assert timeline[-1]["event_type"] == "session_timed_out"

    def test_artifact_correlation_in_events(self) -> None:
        """Artifacts recorded in stream are correlated to session."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("subprocess_launched", {})
        stream.record_artifact("log", "/tmp/log.txt")
        stream.record_artifact("output", "/tmp/output.txt")

        assert stream.artifacts.get("log") == "/tmp/log.txt"
        assert stream.artifacts.get("output") == "/tmp/output.txt"
        # artifact_materialized events in timeline
        art_events = [e for e in stream.timeline() if e["event_type"] == "artifact_materialized"]
        assert len(art_events) == 2

    def test_correlation_keys_consistent(self) -> None:
        """All events in stream share same correlation keys."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("subprocess_launched", {})
        stream.emit("session_completed", {})

        errors = stream.validate_correlation()
        assert errors == []

    def test_ndjson_serialization(self) -> None:
        """Event stream serializes as valid NDJSON."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("session_completed", {})

        ndjson = stream.to_ndjson()
        lines = ndjson.strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "event_type" in parsed
            assert "session_id" in parsed
            assert "dispatch_id" in parsed

    def test_canonical_order_validation(self) -> None:
        """Events must follow canonical order."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("subprocess_launched", {})
        stream.emit("heartbeat", {})
        stream.emit("session_completed", {})

        errors = stream.validate_order()
        assert errors == []

    def test_no_events_after_terminal(self) -> None:
        """No events can be emitted after terminal event."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        stream = HeadlessEventStream(corr.session_id, corr.dispatch_id, corr.attempt_number)
        stream.emit("session_created", {})
        stream.emit("session_completed", {})

        with pytest.raises(ValueError, match="terminated"):
            stream.emit("heartbeat", {})


# ===================================================================
# Section 3: Provider-Aware Observability
# ===================================================================

class TestProviderObservabilityCertification:
    """Certify provider capability declarations and fallback semantics."""

    def test_claude_code_is_rich(self) -> None:
        caps = get_provider_capabilities("claude_code")
        assert caps.tool_call_visibility is True
        assert caps.structured_progress_events is True
        assert caps.observability_quality() == ObservabilityQuality.RICH
        assert caps.progress_confidence() == "high"

    def test_gemini_is_structured(self) -> None:
        caps = get_provider_capabilities("gemini")
        assert caps.tool_call_visibility is False
        assert caps.structured_progress_events is True
        assert caps.observability_quality() == ObservabilityQuality.STRUCTURED
        assert caps.progress_confidence() == "medium"

    def test_codex_is_output_only(self) -> None:
        caps = get_provider_capabilities("codex_cli")
        assert caps.tool_call_visibility is False
        assert caps.structured_progress_events is False
        assert caps.output_only_fallback is True
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY
        assert caps.progress_confidence() == "low"

    def test_unknown_provider_degrades_to_output_only(self) -> None:
        caps = get_provider_capabilities("totally_unknown_provider")
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY
        assert caps.progress_confidence() == "low"
        assert is_provider_known("totally_unknown_provider") is False

    def test_all_registered_providers_have_capabilities(self) -> None:
        for provider_id, caps in PROVIDER_REGISTRY.items():
            assert isinstance(caps, ProviderCapabilities)
            assert caps.provider_id == provider_id
            quality = caps.observability_quality()
            assert isinstance(quality, ObservabilityQuality)
            confidence = caps.progress_confidence()
            assert confidence in ("high", "medium", "low")

    def test_attachability_explicit(self) -> None:
        """Only interactive providers support attachment."""
        assert get_provider_capabilities("claude_code").can_attach is True
        assert get_provider_capabilities("gemini").can_attach is False
        assert get_provider_capabilities("codex_cli").can_attach is False
        assert get_provider_capabilities("output_only").can_attach is False

    def test_visibility_levels_are_distinct(self) -> None:
        """Each quality level maps to different capability combinations."""
        qualities = set()
        for caps in PROVIDER_REGISTRY.values():
            qualities.add(caps.observability_quality())
        assert len(qualities) >= 2  # At least RICH and OUTPUT_ONLY differ


# ===================================================================
# Section 4: Contract-to-Implementation Alignment
# ===================================================================

class TestContractAlignment:
    """Certify implementation matches HEADLESS_SESSION_CONTRACT.md."""

    def test_local_session_adapter_conforms_to_protocol(self) -> None:
        adapter = LocalSessionAdapter()
        assert isinstance(adapter, RuntimeAdapter)

    def test_local_session_has_required_capabilities(self) -> None:
        adapter = LocalSessionAdapter()
        missing = validate_required_capabilities(adapter)
        assert missing == [], f"Missing required capabilities: {missing}"

    def test_unsupported_operations_raise_correctly(self) -> None:
        adapter = LocalSessionAdapter()
        with pytest.raises(UnsupportedCapability):
            adapter.attach("T1")
        with pytest.raises(UnsupportedCapability):
            adapter.reheal("T1")

    def test_event_types_cover_lifecycle(self) -> None:
        """Event types cover session lifecycle phases from contract."""
        required_phases = {"session_created", "subprocess_launched", "session_completed",
                          "session_failed", "session_timed_out"}
        assert required_phases <= VALID_EVENT_TYPES

    def test_terminal_events_are_terminal(self) -> None:
        """Terminal event types match contract requirement."""
        assert "session_completed" in TERMINAL_EVENT_TYPES
        assert "session_failed" in TERMINAL_EVENT_TYPES
        assert "session_timed_out" in TERMINAL_EVENT_TYPES

    def test_canonical_order_defined(self) -> None:
        """Canonical event order is defined for validation."""
        assert CANONICAL_ORDER[0] == "session_created"
        assert CANONICAL_ORDER[1] == "subprocess_launched"

    def test_correlation_keys_structure(self) -> None:
        """Correlation keys contain session_id, attempt, and dispatch linkage."""
        corr = CorrelationKeys(session_id="T1", attempt_number=1, dispatch_id="d-1")
        assert corr.session_id == "T1"
        assert corr.attempt_number == 1
        assert corr.dispatch_id == "d-1"
        assert corr.attempt_id == "T1-attempt-1"

    def test_provider_registry_covers_known_providers(self) -> None:
        """Registry covers all providers mentioned in contract."""
        assert "claude_code" in PROVIDER_REGISTRY
        assert "gemini" in PROVIDER_REGISTRY
        assert "codex_cli" in PROVIDER_REGISTRY

    def test_unknown_fallback_is_output_only(self) -> None:
        """Unknown providers default to output-only per contract."""
        caps = UNKNOWN_PROVIDER_CAPABILITIES
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY
        assert caps.tool_call_visibility is False


# ===================================================================
# Section 5: Adapter Capability Preservation
# ===================================================================

class TestAdapterCapabilities:
    """Certify LocalSessionAdapter capabilities are correct."""

    def test_supported_capabilities_count(self) -> None:
        assert len(LOCAL_SESSION_CAPABILITIES) == 7

    def test_required_capabilities_subset(self) -> None:
        assert REQUIRED_CAPABILITIES <= LOCAL_SESSION_CAPABILITIES

    def test_adapter_type(self) -> None:
        adapter = LocalSessionAdapter()
        assert adapter.adapter_type() == "local_session"

    def test_health_returns_result(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        result = adapter.health("T1")
        assert result.surface_exists is True
        assert result.process_alive is True
        adapter.stop("T1")

    def test_session_health_aggregation(self) -> None:
        adapter = LocalSessionAdapter()
        adapter.spawn("T1", {"command": "sleep 30"})
        result = adapter.session_health(["T1", "T2"])
        assert "T1" in result.terminals
        assert "T2" in result.terminals
        assert result.terminals["T1"].surface_exists is True
        assert result.terminals["T2"].surface_exists is False
        adapter.stop("T1")
