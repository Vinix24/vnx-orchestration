from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence import get_audit_trail, load_document, save_document
from state_machine import InvalidTransition, ReviewableDocument

_MIGRATION = (Path(__file__).parent.parent / "migrations" / "001_state_machine.sql").read_text()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_MIGRATION)
    yield c
    c.close()


# --- pure state-machine tests (no DB) ---


def test_initial_state_is_draft():
    doc = ReviewableDocument(1)
    assert doc.state == "DRAFT"


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    assert doc.state == "PENDING_REVIEW"


def test_approve_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob", "needs more detail")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob", "does not meet criteria")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob")
    doc.transition("submit", "alice", "addressed feedback")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(1)
    with pytest.raises(InvalidTransition):
        doc.transition("approve", "bob")


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    assert doc.state == "APPROVED"
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "charlie")


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob")
    assert doc.state == "REJECTED"
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "charlie")


# --- persistence + audit tests ---


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    save_document(conn, doc)

    doc.transition("submit", "alice", "ready for review")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 1
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    assert trail[0]["actor"] == "alice"
    assert trail[0]["reason"] == "ready for review"


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(1)
    save_document(conn, doc)

    doc.transition("submit", "alice")
    save_document(conn, doc)

    doc.transition("request_changes", "bob", "revisions needed")
    save_document(conn, doc)

    doc.transition("submit", "alice", "fixed")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 3
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[2]["from_state"] == "CHANGES_REQUESTED"


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(42, "project-alpha")
    doc_b = ReviewableDocument(42, "project-beta")

    save_document(conn, doc_a)
    save_document(conn, doc_b)

    doc_a.transition("submit", "alice")
    save_document(conn, doc_a)

    loaded_a = load_document(conn, 42, "project-alpha")
    loaded_b = load_document(conn, 42, "project-beta")

    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "DRAFT"

    trail_a = get_audit_trail(conn, 42, "project-alpha")
    trail_b = get_audit_trail(conn, 42, "project-beta")
    assert len(trail_a) == 1
    assert len(trail_b) == 0
