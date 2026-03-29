#!/usr/bin/env python3
"""
Tests for dispatch_broker.py (PR-1)

Quality gate coverage (gate_pr1_durable_dispatch_bundles):
  - Every dispatch creates a durable registry row before any terminal delivery
  - Every dispatch writes a bundle with payload, metadata, and expected outputs
  - Delivery failures create failed_delivery attempt records (never logs-only)
  - Shadow mode can be enabled and disabled without breaking the current dispatcher
  - All tests pass for broker registration and bundle writing logic
  - Re-running the same dispatch registration path does not corrupt state
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    init_schema,
)
from dispatch_broker import (
    BrokerError,
    ClaimResult,
    DispatchBroker,
    RegisterResult,
    broker_config_from_env,
    load_broker,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _make_broker(
    state_dir: str,
    dispatch_dir: str,
    *,
    shadow_mode: bool = True,
) -> DispatchBroker:
    return DispatchBroker(state_dir, dispatch_dir, shadow_mode=shadow_mode)


def _setup_dirs(tmp: tempfile.TemporaryDirectory) -> tuple[str, str]:
    """Return (state_dir, dispatch_dir) with schema initialized."""
    base = Path(tmp.name)
    state_dir = str(base / "state")
    dispatch_dir = str(base / "dispatches")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    Path(dispatch_dir).mkdir(parents=True, exist_ok=True)
    init_schema(state_dir)
    return state_dir, dispatch_dir


def _register_dispatch(
    broker: DispatchBroker,
    dispatch_id: str = "test-dispatch-001",
    prompt: str = "Do some work.",
    **kwargs,
) -> RegisterResult:
    return broker.register(dispatch_id, prompt, **kwargs)


# ---------------------------------------------------------------------------
# TestBrokerRegistration
# ---------------------------------------------------------------------------

class TestBrokerRegistration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_returns_register_result(self) -> None:
        result = _register_dispatch(self.broker)
        self.assertIsInstance(result, RegisterResult)

    def test_register_creates_db_row(self) -> None:
        _register_dispatch(self.broker, "d-001")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-001")
        self.assertIsNotNone(row)
        self.assertEqual(row["dispatch_id"], "d-001")

    def test_register_initial_state_is_queued(self) -> None:
        result = _register_dispatch(self.broker, "d-002")
        self.assertEqual(result.dispatch_row["state"], "queued")

    def test_register_creates_bundle_directory(self) -> None:
        result = _register_dispatch(self.broker, "d-003")
        self.assertTrue(result.bundle_path.is_dir())

    def test_register_creates_bundle_json(self) -> None:
        result = _register_dispatch(self.broker, "d-004")
        bundle_json = result.bundle_path / "bundle.json"
        self.assertTrue(bundle_json.exists())

    def test_register_creates_prompt_txt(self) -> None:
        result = _register_dispatch(self.broker, "d-005", prompt="Hello worker.")
        prompt_file = result.bundle_path / "prompt.txt"
        self.assertTrue(prompt_file.exists())
        self.assertEqual(prompt_file.read_text(encoding="utf-8"), "Hello worker.")

    def test_register_already_existed_false_on_new(self) -> None:
        result = _register_dispatch(self.broker, "d-006")
        self.assertFalse(result.already_existed)

    def test_register_stores_priority(self) -> None:
        _register_dispatch(self.broker, "d-007", priority="P1")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-007")
        self.assertEqual(row["priority"], "P1")

    def test_register_stores_terminal_id(self) -> None:
        _register_dispatch(self.broker, "d-008", terminal_id="T2")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-008")
        self.assertEqual(row["terminal_id"], "T2")

    def test_register_stores_track(self) -> None:
        _register_dispatch(self.broker, "d-009", track="B")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-009")
        self.assertEqual(row["track"], "B")

    def test_register_stores_pr_ref(self) -> None:
        _register_dispatch(self.broker, "d-010", pr_ref="PR-1")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-010")
        self.assertEqual(row["pr_ref"], "PR-1")

    def test_register_stores_gate(self) -> None:
        _register_dispatch(self.broker, "d-011", gate="gate_pr1_durable_dispatch_bundles")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-011")
        self.assertEqual(row["gate"], "gate_pr1_durable_dispatch_bundles")

    def test_register_stores_bundle_path_in_db(self) -> None:
        result = _register_dispatch(self.broker, "d-012")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-012")
        self.assertIsNotNone(row["bundle_path"])
        self.assertIn("d-012", row["bundle_path"])

    def test_register_appends_queued_event(self) -> None:
        _register_dispatch(self.broker, "d-013")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="d-013")
        event_types = [e["event_type"] for e in events]
        self.assertIn("dispatch_queued", event_types)


# ---------------------------------------------------------------------------
# TestBrokerBundleContent
# ---------------------------------------------------------------------------

class TestBrokerBundleContent(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bundle_contains_dispatch_id(self) -> None:
        result = _register_dispatch(self.broker, "b-001")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["dispatch_id"], "b-001")

    def test_bundle_contains_bundle_version(self) -> None:
        result = _register_dispatch(self.broker, "b-002")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["bundle_version"], 1)

    def test_bundle_contains_created_at(self) -> None:
        result = _register_dispatch(self.broker, "b-003")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertIn("created_at", bundle)
        self.assertTrue(bundle["created_at"].endswith("Z"))

    def test_bundle_contains_expected_outputs(self) -> None:
        result = _register_dispatch(
            self.broker, "b-004",
            expected_outputs=["report.md", "receipt.json"],
        )
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["expected_outputs"], ["report.md", "receipt.json"])

    def test_bundle_expected_outputs_defaults_to_empty_list(self) -> None:
        result = _register_dispatch(self.broker, "b-005")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["expected_outputs"], [])

    def test_bundle_contains_intelligence_refs(self) -> None:
        result = _register_dispatch(
            self.broker, "b-006",
            intelligence_refs=["ref-A", "ref-B"],
        )
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["intelligence_refs"], ["ref-A", "ref-B"])

    def test_bundle_contains_target_profile(self) -> None:
        profile = {"model": "claude-opus", "max_tokens": 8192}
        result = _register_dispatch(self.broker, "b-007", target_profile=profile)
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["target_profile"], profile)

    def test_bundle_contains_metadata(self) -> None:
        meta = {"source": "test", "sprint": 42}
        result = _register_dispatch(self.broker, "b-008", metadata=meta)
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["metadata"], meta)

    def test_bundle_metadata_defaults_to_empty_dict(self) -> None:
        result = _register_dispatch(self.broker, "b-009")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["metadata"], {})

    def test_bundle_stores_terminal_id(self) -> None:
        result = _register_dispatch(self.broker, "b-010", terminal_id="T3")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["terminal_id"], "T3")

    def test_bundle_stores_track(self) -> None:
        result = _register_dispatch(self.broker, "b-011", track="C")
        bundle = json.loads((result.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["track"], "C")

    def test_prompt_txt_contains_full_prompt(self) -> None:
        long_prompt = "Do this:\n- Step 1\n- Step 2\n" * 100
        result = _register_dispatch(self.broker, "b-012", prompt=long_prompt)
        stored = (result.bundle_path / "prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(stored, long_prompt)

    def test_bundle_written_atomically_no_tmp_left(self) -> None:
        result = _register_dispatch(self.broker, "b-013")
        tmp_files = list(result.bundle_path.glob("*.tmp"))
        self.assertEqual(tmp_files, [], "Atomic write left .tmp files behind")

    def test_get_bundle_returns_parsed_dict(self) -> None:
        _register_dispatch(self.broker, "b-014")
        bundle = self.broker.get_bundle("b-014")
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["dispatch_id"], "b-014")

    def test_get_bundle_returns_none_for_missing(self) -> None:
        result = self.broker.get_bundle("nonexistent-dispatch-xyz")
        self.assertIsNone(result)

    def test_get_bundle_path_returns_correct_path(self) -> None:
        path = self.broker.get_bundle_path("b-015")
        self.assertEqual(path, Path(self.dispatch_dir) / "b-015")


# ---------------------------------------------------------------------------
# TestBrokerAttemptLifecycle
# ---------------------------------------------------------------------------

class TestBrokerAttemptLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register_and_claim(self, dispatch_id: str, terminal_id: str = "T1") -> ClaimResult:
        _register_dispatch(self.broker, dispatch_id)
        return self.broker.claim(dispatch_id, terminal_id)

    def test_claim_transitions_to_claimed(self) -> None:
        result = self._register_and_claim("c-001")
        self.assertEqual(result.dispatch_row["state"], "claimed")

    def test_claim_returns_attempt_id(self) -> None:
        result = self._register_and_claim("c-002")
        self.assertIsNotNone(result.attempt_id)
        self.assertTrue(len(result.attempt_id) > 0)

    def test_claim_returns_attempt_number(self) -> None:
        result = self._register_and_claim("c-003")
        self.assertEqual(result.attempt_number, 1)

    def test_claim_increments_attempt_count_in_db(self) -> None:
        self._register_and_claim("c-004")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "c-004")
        self.assertEqual(row["attempt_count"], 1)

    def test_deliver_start_transitions_to_delivering(self) -> None:
        claim = self._register_and_claim("c-005")
        self.broker.deliver_start("c-005", claim.attempt_id)
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "c-005")
        self.assertEqual(row["state"], "delivering")

    def test_deliver_success_transitions_to_accepted(self) -> None:
        claim = self._register_and_claim("c-006")
        self.broker.deliver_start("c-006", claim.attempt_id)
        self.broker.deliver_success("c-006", claim.attempt_id)
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "c-006")
        self.assertEqual(row["state"], "accepted")

    def test_deliver_failure_transitions_to_failed_delivery(self) -> None:
        claim = self._register_and_claim("c-007")
        self.broker.deliver_start("c-007", claim.attempt_id)
        self.broker.deliver_failure("c-007", claim.attempt_id, "tmux pane gone")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "c-007")
        self.assertEqual(row["state"], "failed_delivery")

    def test_full_happy_path_events_recorded(self) -> None:
        claim = self._register_and_claim("c-008")
        self.broker.deliver_start("c-008", claim.attempt_id)
        self.broker.deliver_success("c-008", claim.attempt_id)
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="c-008")
        event_types = {e["event_type"] for e in events}
        self.assertIn("dispatch_queued", event_types)
        self.assertIn("dispatch_claimed", event_types)
        self.assertIn("dispatch_delivering", event_types)
        self.assertIn("dispatch_accepted", event_types)

    def test_claim_raises_on_nonexistent_dispatch(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.claim("nonexistent-xyz", "T1")

    def test_deliver_start_raises_on_nonexistent_dispatch(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.deliver_start("nonexistent-xyz", "fake-attempt-id")

    def test_deliver_success_raises_on_nonexistent_dispatch(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.deliver_success("nonexistent-xyz", "fake-attempt-id")

    def test_deliver_failure_raises_on_nonexistent_dispatch(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.deliver_failure("nonexistent-xyz", "fake-attempt-id", "reason")

    def test_claim_raises_when_not_in_queued_state(self) -> None:
        # Claim once to move out of queued
        claim = self._register_and_claim("c-009")
        # Trying to claim again (now in claimed state) should raise
        with self.assertRaises(BrokerError):
            self.broker.claim("c-009", "T2")

    def test_custom_attempt_number_stored(self) -> None:
        _register_dispatch(self.broker, "c-010")
        result = self.broker.claim("c-010", "T2", attempt_number=3)
        self.assertEqual(result.attempt_number, 3)


# ---------------------------------------------------------------------------
# TestBrokerFailureRecording
# ---------------------------------------------------------------------------

class TestBrokerFailureRecording(unittest.TestCase):
    """Verify that delivery failures always create durable records.

    This covers the quality gate requirement:
    'Delivery failures create failed_delivery attempt records
    instead of disappearing into logs only.'
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _setup_delivering(self, dispatch_id: str) -> str:
        """Register, claim, and start delivery. Returns attempt_id."""
        _register_dispatch(self.broker, dispatch_id)
        claim = self.broker.claim(dispatch_id, "T2")
        self.broker.deliver_start(dispatch_id, claim.attempt_id)
        return claim.attempt_id

    def test_failure_creates_failed_delivery_state(self) -> None:
        attempt_id = self._setup_delivering("f-001")
        self.broker.deliver_failure("f-001", attempt_id, "tmux: no such pane")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "f-001")
        self.assertEqual(row["state"], "failed_delivery")

    def test_failure_reason_stored_in_attempt(self) -> None:
        attempt_id = self._setup_delivering("f-002")
        reason = "send-keys returned exit code 1"
        self.broker.deliver_failure("f-002", attempt_id, reason)
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertIsNotNone(attempt_row)
        self.assertEqual(attempt_row["failure_reason"], reason)
        self.assertEqual(attempt_row["state"], "failed")

    def test_failure_appends_coordination_event(self) -> None:
        attempt_id = self._setup_delivering("f-003")
        self.broker.deliver_failure("f-003", attempt_id, "connection timeout")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="f-003")
        event_types = [e["event_type"] for e in events]
        self.assertIn("dispatch_failed_delivery", event_types)

    def test_failure_event_contains_reason(self) -> None:
        attempt_id = self._setup_delivering("f-004")
        reason = "pane session terminated unexpectedly"
        self.broker.deliver_failure("f-004", attempt_id, reason)
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="f-004")
        failed_events = [e for e in events if e["event_type"] == "dispatch_failed_delivery"]
        self.assertTrue(len(failed_events) >= 1)
        self.assertEqual(failed_events[0]["reason"], reason)

    def test_failure_attempt_has_ended_at_timestamp(self) -> None:
        attempt_id = self._setup_delivering("f-005")
        self.broker.deliver_failure("f-005", attempt_id, "timeout")
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_multiple_attempts_on_same_dispatch(self) -> None:
        """After a failure, dispatch can be re-registered and retried."""
        _register_dispatch(self.broker, "f-006")
        claim1 = self.broker.claim("f-006", "T1")
        self.broker.deliver_start("f-006", claim1.attempt_id)
        self.broker.deliver_failure("f-006", claim1.attempt_id, "first attempt failed")

        # Reconcile back to queued via recovered
        with get_connection(self.state_dir) as conn:
            from runtime_coordination import transition_dispatch as td
            td(conn, dispatch_id="f-006", to_state="recovered", actor="test", reason="manual recovery")
            td(conn, dispatch_id="f-006", to_state="queued", actor="test", reason="re-queue after recovery")
            conn.commit()

        claim2 = self.broker.claim("f-006", "T1", attempt_number=2)
        self.assertEqual(claim2.attempt_number, 2)
        with get_connection(self.state_dir) as conn:
            attempts = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("f-006",),
            ).fetchall()
        self.assertEqual(len(attempts), 2)


# ---------------------------------------------------------------------------
# TestBrokerShadowMode
# ---------------------------------------------------------------------------

class TestBrokerShadowMode(unittest.TestCase):
    """Verify shadow mode flag behavior and load_broker() env-based creation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_shadow_mode_is_true(self) -> None:
        broker = DispatchBroker(self.state_dir, self.dispatch_dir)
        self.assertTrue(broker.shadow_mode)

    def test_shadow_mode_false_when_set(self) -> None:
        broker = DispatchBroker(self.state_dir, self.dispatch_dir, shadow_mode=False)
        self.assertFalse(broker.shadow_mode)

    def test_enabled_property_always_true_when_instantiated(self) -> None:
        broker = DispatchBroker(self.state_dir, self.dispatch_dir)
        self.assertTrue(broker.enabled)

    def test_load_broker_returns_instance_when_enabled(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "1", "VNX_BROKER_SHADOW": "1"}):
            broker = load_broker(self.state_dir, self.dispatch_dir)
        self.assertIsNotNone(broker)
        self.assertIsInstance(broker, DispatchBroker)

    def test_load_broker_returns_none_when_disabled(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "0"}):
            broker = load_broker(self.state_dir, self.dispatch_dir)
        self.assertIsNone(broker)

    def test_load_broker_shadow_mode_from_env_on(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "1", "VNX_BROKER_SHADOW": "1"}):
            broker = load_broker(self.state_dir, self.dispatch_dir)
        self.assertTrue(broker.shadow_mode)

    def test_load_broker_shadow_mode_from_env_off(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "1", "VNX_BROKER_SHADOW": "0"}):
            broker = load_broker(self.state_dir, self.dispatch_dir)
        self.assertFalse(broker.shadow_mode)

    def test_broker_config_from_env_enabled_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            # Remove keys if present to test defaults
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VNX_BROKER_ENABLED", "VNX_BROKER_SHADOW")}
            with patch.dict(os.environ, env, clear=True):
                config = broker_config_from_env()
        self.assertTrue(config["enabled"])
        self.assertTrue(config["shadow_mode"])

    def test_broker_config_from_env_disabled(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "0", "VNX_BROKER_SHADOW": "1"}):
            config = broker_config_from_env()
        self.assertFalse(config["enabled"])

    def test_broker_config_from_env_shadow_off(self) -> None:
        with patch.dict(os.environ, {"VNX_BROKER_ENABLED": "1", "VNX_BROKER_SHADOW": "0"}):
            config = broker_config_from_env()
        self.assertFalse(config["shadow_mode"])

    def test_shadow_mode_broker_still_registers_durably(self) -> None:
        """In shadow mode, registration must still be durable in the DB."""
        broker = DispatchBroker(self.state_dir, self.dispatch_dir, shadow_mode=True)
        _register_dispatch(broker, "s-001")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "s-001")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "queued")

    def test_shadow_mode_broker_still_writes_bundle(self) -> None:
        broker = DispatchBroker(self.state_dir, self.dispatch_dir, shadow_mode=True)
        result = _register_dispatch(broker, "s-002")
        self.assertTrue((result.bundle_path / "bundle.json").exists())
        self.assertTrue((result.bundle_path / "prompt.txt").exists())


# ---------------------------------------------------------------------------
# TestBrokerIdempotency
# ---------------------------------------------------------------------------

class TestBrokerIdempotency(unittest.TestCase):
    """Verify re-running the same dispatch_id does not corrupt state.

    Quality gate requirement:
    'Re-running the same dispatch registration path does not create
    inconsistent duplicate state.'
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_second_register_returns_already_existed_true(self) -> None:
        _register_dispatch(self.broker, "i-001")
        result2 = _register_dispatch(self.broker, "i-001")
        self.assertTrue(result2.already_existed)

    def test_second_register_does_not_change_db_state(self) -> None:
        _register_dispatch(self.broker, "i-002")
        # Move out of queued state
        with get_connection(self.state_dir) as conn:
            from runtime_coordination import transition_dispatch as td
            td(conn, dispatch_id="i-002", to_state="claimed", actor="test")
            conn.commit()
        # Re-register should not reset the state back to queued
        _register_dispatch(self.broker, "i-002")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "i-002")
        self.assertEqual(row["state"], "claimed")

    def test_second_register_does_not_overwrite_bundle_json(self) -> None:
        result1 = _register_dispatch(self.broker, "i-003", prompt="original prompt")
        original_created_at = json.loads(
            (result1.bundle_path / "bundle.json").read_text()
        )["created_at"]

        result2 = _register_dispatch(self.broker, "i-003", prompt="new prompt")
        # Bundle must be unchanged (immutability G-R6)
        bundle = json.loads((result2.bundle_path / "bundle.json").read_text())
        self.assertEqual(bundle["created_at"], original_created_at)

    def test_second_register_does_not_overwrite_prompt_txt(self) -> None:
        _register_dispatch(self.broker, "i-004", prompt="first prompt content")
        result2 = _register_dispatch(self.broker, "i-004", prompt="second prompt content")
        stored = (result2.bundle_path / "prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(stored, "first prompt content")

    def test_repeated_register_produces_single_db_row(self) -> None:
        for _ in range(5):
            _register_dispatch(self.broker, "i-005")
        with get_connection(self.state_dir) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = ?", ("i-005",)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_repeated_register_bundle_path_consistent(self) -> None:
        result1 = _register_dispatch(self.broker, "i-006")
        result2 = _register_dispatch(self.broker, "i-006")
        self.assertEqual(result1.bundle_path, result2.bundle_path)

    def test_concurrent_same_id_no_duplicate_state(self) -> None:
        """Both calls succeed; second returns existing data without error."""
        _register_dispatch(self.broker, "i-007")
        # This must not raise even though bundle exists
        try:
            _register_dispatch(self.broker, "i-007")
        except Exception as exc:
            self.fail(f"Second register raised unexpectedly: {exc}")

    def test_register_different_ids_independent(self) -> None:
        result_a = _register_dispatch(self.broker, "i-008-a")
        result_b = _register_dispatch(self.broker, "i-008-b")
        self.assertFalse(result_a.already_existed)
        self.assertFalse(result_b.already_existed)
        with get_connection(self.state_dir) as conn:
            row_a = get_dispatch(conn, "i-008-a")
            row_b = get_dispatch(conn, "i-008-b")
        self.assertIsNotNone(row_a)
        self.assertIsNotNone(row_b)
        self.assertNotEqual(row_a["dispatch_id"], row_b["dispatch_id"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
