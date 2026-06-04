from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from state_machine import InvalidTransition, ReviewableDocument
from persistence import apply_migration, get_audit_trail, load_document, save_document


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    apply_migration(c)
    yield c
    c.close()


def test_initial_state_is_draft():
    doc = ReviewableDocument(doc_id=1)
    assert doc.state == "DRAFT"


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    assert doc.state == "PENDING_REVIEW"


def test_approve_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob", "needs more detail")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob", "out of scope")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob")
    doc.transition("submit", "alice")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(doc_id=1)
    with pytest.raises(InvalidTransition):
        doc.transition("approve", "op")


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "carol")


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob")
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "carol")


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    save_document(conn, doc)
    trail = get_audit_trail(conn, doc_id=1)
    assert len(trail) == 1
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    assert trail[0]["actor"] == "alice"


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    save_document(conn, doc)
    trail = get_audit_trail(conn, doc_id=1)
    assert len(trail) == 2
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[1]["to_state"] == "APPROVED"


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(doc_id=1, project_id="proj_a")
    doc_b = ReviewableDocument(doc_id=1, project_id="proj_b")
    doc_a.transition("submit", "alice")
    save_document(conn, doc_a)
    save_document(conn, doc_b)
    loaded_a = load_document(conn, doc_id=1, project_id="proj_a")
    loaded_b = load_document(conn, doc_id=1, project_id="proj_b")
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "DRAFT"
