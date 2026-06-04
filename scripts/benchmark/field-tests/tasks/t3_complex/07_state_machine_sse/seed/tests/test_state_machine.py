"""Contract tests for the reviewable-document state machine."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persistence import (  # noqa: E402
    get_audit_trail,
    load_document,
    save_document,
)
from state_machine import (  # noqa: E402
    InvalidTransition,
    ReviewableDocument,
)

MIGRATION_SQL = (ROOT / "migrations" / "001_state_machine.sql").read_text()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(MIGRATION_SQL)
    try:
        yield c
    finally:
        c.close()


def test_initial_state_is_draft():
    doc = ReviewableDocument(1)
    assert doc.state == "DRAFT"


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    assert doc.state == "PENDING_REVIEW"


def test_approve_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("approve", "reviewer", "lgtm")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer", "needs tests")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("reject", "reviewer", "off-scope")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer")
    doc.transition("submit", "author", "addressed feedback")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(1)
    with pytest.raises(InvalidTransition) as exc_info:
        doc.transition("approve", "reviewer")
    msg = str(exc_info.value)
    assert "approve" in msg
    assert "DRAFT" in msg


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("approve", "reviewer")
    assert doc.state == "APPROVED"
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("reject", "reviewer", "off-scope")
    assert doc.state == "REJECTED"
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    save_document(conn, doc)
    doc.transition("approve", "reviewer", "lgtm")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 2
    assert trail[0]["actor"] == "author"
    assert trail[1]["actor"] == "reviewer"
    assert trail[1]["reason"] == "lgtm"


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer")
    doc.transition("submit", "author", "fixed")
    doc.transition("approve", "reviewer")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 4

    expected_chain = [
        ("DRAFT", "PENDING_REVIEW"),
        ("PENDING_REVIEW", "CHANGES_REQUESTED"),
        ("CHANGES_REQUESTED", "PENDING_REVIEW"),
        ("PENDING_REVIEW", "APPROVED"),
    ]
    for row, (expected_from, expected_to) in zip(trail, expected_chain):
        assert row["from_state"] == expected_from
        assert row["to_state"] == expected_to

    for row, prev in zip(trail[1:], trail[:-1]):
        assert row["from_state"] == prev["to_state"]


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(1, project_id="alpha")
    doc_a.transition("submit", "alice")
    save_document(conn, doc_a)

    doc_b = ReviewableDocument(1, project_id="beta")
    doc_b.transition("submit", "bob")
    doc_b.transition("reject", "bob", "off-topic")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, 1, "alpha")
    loaded_b = load_document(conn, 1, "beta")
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "REJECTED"

    trail_a = get_audit_trail(conn, 1, "alpha")
    trail_b = get_audit_trail(conn, 1, "beta")
    assert len(trail_a) == 1
    assert len(trail_b) == 2
    assert all(row["project_id"] == "alpha" for row in trail_a)
    assert all(row["project_id"] == "beta" for row in trail_b)
