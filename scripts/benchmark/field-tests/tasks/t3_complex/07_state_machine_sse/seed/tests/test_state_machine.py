from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from state_machine import (  # noqa: E402
    APPROVED,
    CHANGES_REQUESTED,
    DRAFT,
    PENDING_REVIEW,
    REJECTED,
    InvalidTransition,
    ReviewableDocument,
)
from persistence import get_audit_trail, load_document, save_document  # noqa: E402

_MIGRATION_SQL = (
    Path(__file__).parent.parent / "migrations" / "001_state_machine.sql"
).read_text()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(_MIGRATION_SQL)
    yield c
    c.close()


def test_initial_state_is_draft():
    doc = ReviewableDocument(1)
    assert doc.state == DRAFT


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    assert doc.state == PENDING_REVIEW


def test_approve_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    assert doc.state == APPROVED


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob", "needs rework")
    assert doc.state == CHANGES_REQUESTED


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob", "not compliant")
    assert doc.state == REJECTED


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob")
    doc.transition("submit", "alice")
    assert doc.state == PENDING_REVIEW


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(1)
    with pytest.raises(InvalidTransition):
        doc.transition("approve", "bob")


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    assert doc.state == APPROVED
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "charlie")


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob")
    assert doc.state == REJECTED
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "charlie")


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    save_document(conn, doc)
    trail = get_audit_trail(conn, 1)
    assert len(trail) == 1
    assert trail[0]["from_state"] == DRAFT
    assert trail[0]["to_state"] == PENDING_REVIEW
    assert trail[0]["actor"] == "alice"


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    save_document(conn, doc)
    trail = get_audit_trail(conn, 1)
    assert len(trail) == 2
    assert trail[0]["from_state"] == DRAFT
    assert trail[0]["to_state"] == PENDING_REVIEW
    assert trail[1]["from_state"] == PENDING_REVIEW
    assert trail[1]["to_state"] == APPROVED


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(1, project_id="proj-a")
    doc_a.transition("submit", "alice")
    save_document(conn, doc_a)

    doc_b = ReviewableDocument(1, project_id="proj-b")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, 1, project_id="proj-a")
    loaded_b = load_document(conn, 1, project_id="proj-b")
    assert loaded_a.state == PENDING_REVIEW
    assert loaded_b.state == DRAFT

    trail_a = get_audit_trail(conn, 1, project_id="proj-a")
    trail_b = get_audit_trail(conn, 1, project_id="proj-b")
    assert len(trail_a) == 1
    assert len(trail_b) == 0
