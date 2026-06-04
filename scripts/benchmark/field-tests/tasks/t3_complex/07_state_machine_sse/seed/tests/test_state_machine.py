"""Contract tests for the reviewable-document state machine + persistence."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from state_machine import InvalidTransition, ReviewableDocument  # noqa: E402
from persistence import (  # noqa: E402
    get_audit_trail,
    load_document,
    save_document,
)


MIGRATION_SQL = (ROOT / "migrations" / "001_state_machine.sql").read_text()


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(MIGRATION_SQL)
    try:
        yield connection
    finally:
        connection.close()


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
    doc.transition("approve", "reviewer")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "reviewer", reason="needs sources")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "reviewer", reason="off-topic")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "reviewer")
    doc.transition("submit", "alice")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(1)
    with pytest.raises(InvalidTransition) as excinfo:
        doc.transition("approve", "reviewer")
    assert "DRAFT" in str(excinfo.value)
    assert "approve" in str(excinfo.value)
    assert doc.state == "DRAFT"


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "reviewer")
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "actor")
    assert doc.state == "APPROVED"


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "reviewer")
    for action in ("submit", "approve", "reject", "request_changes"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "actor")
    assert doc.state == "REJECTED"


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice", reason="ready")
    doc.transition("request_changes", "reviewer", reason="needs sources")
    doc.transition("submit", "alice", reason="addressed feedback")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 3
    assert [row["actor"] for row in trail] == ["alice", "reviewer", "alice"]
    assert [row["reason"] for row in trail] == [
        "ready", "needs sources", "addressed feedback"
    ]


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(7)
    doc.transition("submit", "alice")
    doc.transition("approve", "reviewer")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 7)
    assert (trail[0]["from_state"], trail[0]["to_state"]) == (
        "DRAFT", "PENDING_REVIEW",
    )
    assert (trail[1]["from_state"], trail[1]["to_state"]) == (
        "PENDING_REVIEW", "APPROVED",
    )


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(1, project_id="alpha")
    doc_b = ReviewableDocument(1, project_id="beta")

    doc_a.transition("submit", "ada")
    doc_b.transition("submit", "bea")
    doc_b.transition("reject", "reviewer", reason="duplicate")

    save_document(conn, doc_a)
    save_document(conn, doc_b)

    loaded_a = load_document(conn, 1, project_id="alpha")
    loaded_b = load_document(conn, 1, project_id="beta")
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "REJECTED"

    trail_a = get_audit_trail(conn, 1, project_id="alpha")
    trail_b = get_audit_trail(conn, 1, project_id="beta")
    assert len(trail_a) == 1
    assert len(trail_b) == 2
    assert {row["actor"] for row in trail_a} == {"ada"}
    assert {row["actor"] for row in trail_b} == {"bea", "reviewer"}
