#!/usr/bin/env python3
"""
Tests for PR-2: Inbound Event Inbox And Channel-To-Dispatch Routing.

Covers:
  - Durable inbox persistence before dispatch creation
  - Channel/session mapping and dedupe key enforcement
  - Inbox lifecycle: receive -> process -> dispatched/rejected/dead_letter
  - Bounded retry semantics
  - Runtime event emission for all outcomes
  - Broker-registered dispatches preserve channel origin metadata
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from runtime_coordination import init_schema, get_connection
from inbound_inbox import (
    InboundInbox,
    InboxEvent,
    DuplicateEventError,
    InboxEventNotFoundError,
    InvalidInboxTransitionError,
    generate_dedupe_key,
    INBOX_STATES,
    INBOX_TRANSITIONS,
)


class _DBTestCase(unittest.TestCase):
    """Base class that sets up a temp DB with all schema migrations."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ============================================================================
# RECEIVE / PERSISTENCE TESTS
# ============================================================================

class TestInboxReceive(_DBTestCase):

    def test_receive_persists_event(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive(
            "slack-ops",
            {"type": "message", "text": "deploy request"},
        )
        self.assertFalse(result.already_existed)
        self.assertEqual(result.event.channel_id, "slack-ops")
        self.assertEqual(result.event.state, "received")
        self.assertEqual(result.event.payload["type"], "message")

    def test_receive_sets_default_state(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("webhook-ci", {"action": "build_complete"})
        self.assertEqual(result.event.state, "received")
        self.assertFalse(result.event.is_terminal)

    def test_receive_stores_routing_hints(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive(
            "slack-ops",
            {"text": "run security scan"},
            routing_hints={"task_class": "research_structured", "priority": "P1"},
        )
        self.assertEqual(result.event.routing_hints["task_class"], "research_structured")
        self.assertEqual(result.event.routing_hints["priority"], "P1")

    def test_receive_stores_metadata(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive(
            "webhook-ci",
            {"action": "test"},
            metadata={"source_ip": "10.0.0.1"},
        )
        self.assertEqual(result.event.metadata["source_ip"], "10.0.0.1")

    def test_receive_custom_max_retries(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"x": 1}, max_retries=5)
        self.assertEqual(result.event.max_retries, 5)

    def test_receive_queryable_after_persist(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"key": "value"})
        fetched = inbox.get(result.event.event_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.channel_id, "ch1")
        self.assertEqual(fetched.payload["key"], "value")


# ============================================================================
# DEDUPE TESTS
# ============================================================================

class TestInboxDedupe(_DBTestCase):

    def test_same_payload_deduped(self):
        inbox = InboundInbox(self.state_dir)
        r1 = inbox.receive("ch1", {"action": "deploy", "version": "1.0"})
        r2 = inbox.receive("ch1", {"action": "deploy", "version": "1.0"})
        self.assertFalse(r1.already_existed)
        self.assertTrue(r2.already_existed)
        self.assertEqual(r1.event.event_id, r2.event.event_id)

    def test_different_payload_not_deduped(self):
        inbox = InboundInbox(self.state_dir)
        r1 = inbox.receive("ch1", {"action": "deploy", "version": "1.0"})
        r2 = inbox.receive("ch1", {"action": "deploy", "version": "2.0"})
        self.assertFalse(r1.already_existed)
        self.assertFalse(r2.already_existed)
        self.assertNotEqual(r1.event.event_id, r2.event.event_id)

    def test_different_channel_not_deduped(self):
        inbox = InboundInbox(self.state_dir)
        r1 = inbox.receive("ch1", {"action": "test"})
        r2 = inbox.receive("ch2", {"action": "test"})
        self.assertFalse(r1.already_existed)
        self.assertFalse(r2.already_existed)

    def test_explicit_dedupe_key(self):
        inbox = InboundInbox(self.state_dir)
        r1 = inbox.receive("ch1", {"a": 1}, dedupe_key="webhook-id-123")
        r2 = inbox.receive("ch1", {"b": 2}, dedupe_key="webhook-id-123")
        self.assertTrue(r2.already_existed)
        self.assertEqual(r1.event.event_id, r2.event.event_id)

    def test_different_explicit_dedupe_keys(self):
        inbox = InboundInbox(self.state_dir)
        r1 = inbox.receive("ch1", {"a": 1}, dedupe_key="key-1")
        r2 = inbox.receive("ch1", {"a": 1}, dedupe_key="key-2")
        self.assertFalse(r2.already_existed)
        self.assertNotEqual(r1.event.event_id, r2.event.event_id)

    def test_dedupe_key_generation_deterministic(self):
        key1 = generate_dedupe_key("ch1", {"a": 1, "b": 2})
        key2 = generate_dedupe_key("ch1", {"b": 2, "a": 1})
        self.assertEqual(key1, key2)  # Sort-stable JSON


# ============================================================================
# PROCESS / DISPATCH TRANSLATION TESTS
# ============================================================================

class TestInboxProcess(_DBTestCase):

    def test_process_creates_dispatch(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("slack-ops", {"text": "review PR-5"})
        proc = inbox.process(result.event.event_id)
        self.assertEqual(proc.outcome, "dispatched")
        self.assertIsNotNone(proc.dispatch_id)

    def test_process_transitions_to_dispatched(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"action": "run"})
        inbox.process(result.event.event_id)
        event = inbox.get(result.event.event_id)
        self.assertEqual(event.state, "dispatched")
        self.assertIsNotNone(event.processed_at)

    def test_process_links_dispatch_id(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"action": "run"})
        proc = inbox.process(result.event.event_id)
        event = inbox.get(result.event.event_id)
        self.assertEqual(event.dispatch_id, proc.dispatch_id)

    def test_dispatch_has_channel_origin(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("slack-ops", {"text": "check status"})
        proc = inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?",
                (proc.dispatch_id,),
            ).fetchone()
        self.assertEqual(dispatch["channel_origin"], "slack-ops")

    def test_dispatch_has_task_class_from_hints(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive(
            "ch1", {"text": "analyze"},
            routing_hints={"task_class": "research_structured"},
        )
        proc = inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT task_class FROM dispatches WHERE dispatch_id = ?",
                (proc.dispatch_id,),
            ).fetchone()
        self.assertEqual(dispatch["task_class"], "research_structured")

    def test_dispatch_defaults_to_channel_response(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "hello"})
        proc = inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT task_class FROM dispatches WHERE dispatch_id = ?",
                (proc.dispatch_id,),
            ).fetchone()
        self.assertEqual(dispatch["task_class"], "channel_response")

    def test_dispatch_preserves_priority_from_hints(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive(
            "ch1", {"text": "urgent"},
            routing_hints={"priority": "P1"},
        )
        proc = inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT priority FROM dispatches WHERE dispatch_id = ?",
                (proc.dispatch_id,),
            ).fetchone()
        self.assertEqual(dispatch["priority"], "P1")

    def test_dispatch_metadata_has_inbox_event_id(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        proc = inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT metadata_json FROM dispatches WHERE dispatch_id = ?",
                (proc.dispatch_id,),
            ).fetchone()
        meta = json.loads(dispatch["metadata_json"])
        self.assertEqual(meta["inbox_event_id"], result.event.event_id)
        self.assertEqual(meta["channel_origin"], "ch1")

    def test_process_terminal_event_noop(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        inbox.process(result.event.event_id)  # -> dispatched
        proc2 = inbox.process(result.event.event_id)  # already dispatched
        self.assertEqual(proc2.outcome, "dispatched")

    def test_custom_dispatch_id_generator(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        proc = inbox.process(
            result.event.event_id,
            dispatch_id_generator=lambda: "custom-dispatch-123",
        )
        self.assertEqual(proc.dispatch_id, "custom-dispatch-123")

    def test_process_not_found_raises(self):
        inbox = InboundInbox(self.state_dir)
        with self.assertRaises(InboxEventNotFoundError):
            inbox.process("nonexistent-id")


# ============================================================================
# REJECTION TESTS
# ============================================================================

class TestInboxReject(_DBTestCase):

    def test_reject_event(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        proc = inbox.reject(result.event.event_id, "Policy violation")
        self.assertEqual(proc.outcome, "rejected")
        self.assertEqual(proc.failure_reason, "Policy violation")
        event = inbox.get(result.event.event_id)
        self.assertEqual(event.state, "rejected")
        self.assertTrue(event.is_terminal)

    def test_reject_empty_payload(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {}, dedupe_key="empty-test")
        proc = inbox.process(result.event.event_id)
        self.assertEqual(proc.outcome, "rejected")
        self.assertIn("Empty payload", proc.failure_reason)

    def test_reject_not_found_raises(self):
        inbox = InboundInbox(self.state_dir)
        with self.assertRaises(InboxEventNotFoundError):
            inbox.reject("nonexistent", "reason")


# ============================================================================
# RETRY / DEAD-LETTER TESTS
# ============================================================================

class TestInboxRetry(_DBTestCase):

    def test_retry_resets_to_received(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"}, max_retries=3)
        # Manually move to processing state
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE inbound_inbox SET state = 'processing', attempt_count = 1 WHERE event_id = ?",
                (result.event.event_id,),
            )
            conn.commit()
        proc = inbox.retry(result.event.event_id)
        self.assertEqual(proc.outcome, "retry")
        event = inbox.get(result.event.event_id)
        self.assertEqual(event.state, "received")

    def test_retry_exhausted_dead_letters(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"}, max_retries=2)
        # Exhaust retries
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE inbound_inbox SET state = 'processing', attempt_count = 3 WHERE event_id = ?",
                (result.event.event_id,),
            )
            conn.commit()
        proc = inbox.retry(result.event.event_id)
        self.assertEqual(proc.outcome, "dead_letter")
        event = inbox.get(result.event.event_id)
        self.assertEqual(event.state, "dead_letter")
        self.assertTrue(event.is_terminal)

    def test_retry_terminal_state_noop(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        inbox.process(result.event.event_id)  # -> dispatched
        proc = inbox.retry(result.event.event_id)
        self.assertEqual(proc.outcome, "dispatched")

    def test_retry_not_found_raises(self):
        inbox = InboundInbox(self.state_dir)
        with self.assertRaises(InboxEventNotFoundError):
            inbox.retry("nonexistent")

    def test_process_increments_attempt_count(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        inbox.process(result.event.event_id)
        event = inbox.get(result.event.event_id)
        self.assertGreaterEqual(event.attempt_count, 1)


# ============================================================================
# QUERY TESTS
# ============================================================================

class TestInboxQueries(_DBTestCase):

    def _populate(self):
        inbox = InboundInbox(self.state_dir)
        inbox.receive("ch1", {"a": 1}, dedupe_key="k1")
        inbox.receive("ch1", {"a": 2}, dedupe_key="k2")
        inbox.receive("ch2", {"a": 3}, dedupe_key="k3")
        r4 = inbox.receive("ch2", {"a": 4}, dedupe_key="k4")
        inbox.process(r4.event.event_id)
        return inbox

    def test_list_pending(self):
        inbox = self._populate()
        pending = inbox.list_pending()
        self.assertEqual(len(pending), 3)  # 4 received, 1 dispatched
        for e in pending:
            self.assertEqual(e.state, "received")

    def test_list_by_channel(self):
        inbox = self._populate()
        ch1 = inbox.list_by_channel("ch1")
        self.assertEqual(len(ch1), 2)
        for e in ch1:
            self.assertEqual(e.channel_id, "ch1")

    def test_list_dead_letters_empty(self):
        inbox = self._populate()
        dead = inbox.list_dead_letters()
        self.assertEqual(len(dead), 0)

    def test_count_by_state(self):
        inbox = self._populate()
        counts = inbox.count_by_state()
        self.assertEqual(counts.get("received", 0), 3)
        self.assertEqual(counts.get("dispatched", 0), 1)

    def test_get_nonexistent(self):
        inbox = InboundInbox(self.state_dir)
        self.assertIsNone(inbox.get("nonexistent"))


# ============================================================================
# COORDINATION EVENT TESTS
# ============================================================================

class TestInboxEvents(_DBTestCase):

    def test_receive_emits_event(self):
        inbox = InboundInbox(self.state_dir)
        inbox.receive("ch1", {"text": "test"})
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'inbox_event_received'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entity_type"], "inbox_event")

    def test_dedupe_emits_event(self):
        inbox = InboundInbox(self.state_dir)
        inbox.receive("ch1", {"text": "test"})
        inbox.receive("ch1", {"text": "test"})
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'inbox_dedupe_hit'"
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_dispatch_emits_event(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "run analysis"})
        inbox.process(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'inbox_event_dispatched'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        meta = json.loads(events[0]["metadata_json"])
        self.assertIn("dispatch_id", meta)
        self.assertEqual(meta["channel_id"], "ch1")

    def test_reject_emits_event(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"})
        inbox.reject(result.event.event_id, "policy")
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'inbox_event_rejected'"
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_dead_letter_emits_event(self):
        inbox = InboundInbox(self.state_dir)
        result = inbox.receive("ch1", {"text": "test"}, max_retries=0)
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE inbound_inbox SET state = 'processing', attempt_count = 1 WHERE event_id = ?",
                (result.event.event_id,),
            )
            conn.commit()
        inbox.retry(result.event.event_id)
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'inbox_event_dead_letter'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        meta = json.loads(events[0]["metadata_json"])
        self.assertEqual(meta["escalation"], "T0")


# ============================================================================
# STATE TRANSITION VALIDATION TESTS
# ============================================================================

class TestInboxStateTransitions(unittest.TestCase):

    def test_valid_transitions(self):
        from inbound_inbox import _validate_inbox_transition
        _validate_inbox_transition("received", "processing")
        _validate_inbox_transition("received", "rejected")
        _validate_inbox_transition("processing", "dispatched")
        _validate_inbox_transition("processing", "received")  # retry
        _validate_inbox_transition("processing", "dead_letter")

    def test_invalid_transitions(self):
        from inbound_inbox import _validate_inbox_transition
        with self.assertRaises(InvalidInboxTransitionError):
            _validate_inbox_transition("received", "dispatched")
        with self.assertRaises(InvalidInboxTransitionError):
            _validate_inbox_transition("dispatched", "received")
        with self.assertRaises(InvalidInboxTransitionError):
            _validate_inbox_transition("dead_letter", "received")
        with self.assertRaises(InvalidInboxTransitionError):
            _validate_inbox_transition("rejected", "processing")

    def test_terminal_states(self):
        self.assertTrue(InboxEvent.from_row({
            "event_id": "x", "channel_id": "c", "dedupe_key": "d",
            "state": "dispatched", "payload_json": "{}",
        }).is_terminal)
        self.assertTrue(InboxEvent.from_row({
            "event_id": "x", "channel_id": "c", "dedupe_key": "d",
            "state": "rejected", "payload_json": "{}",
        }).is_terminal)
        self.assertTrue(InboxEvent.from_row({
            "event_id": "x", "channel_id": "c", "dedupe_key": "d",
            "state": "dead_letter", "payload_json": "{}",
        }).is_terminal)
        self.assertFalse(InboxEvent.from_row({
            "event_id": "x", "channel_id": "c", "dedupe_key": "d",
            "state": "received", "payload_json": "{}",
        }).is_terminal)


if __name__ == "__main__":
    unittest.main()
