from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from persistence import get_audit_trail, load_document, save_document
from state_machine import InvalidTransition, ReviewableDocument

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_state_machine.sql"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the migration applied."""
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(MIGRATION.read_text())
    return db


# ── State-only tests (no persistence) ──────────────────────────────────


def test_initial_state_is_draft() -> None:
    doc = ReviewableDocument(doc_id=1)
    assert doc.state == "DRAFT"


def test_submit_from_draft_goes_to_pending_review() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    assert doc.state == "PENDING_REVIEW"


def test_approve_from_pending_review() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("approve", actor="bob")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("request_changes", actor="bob", reason="needs more detail")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("reject", actor="bob", reason="out of scope")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("request_changes", actor="bob")
    doc.transition("submit", actor="alice")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises() -> None:
    doc = ReviewableDocument(doc_id=1)
    with pytest.raises(InvalidTransition) as excinfo:
        doc.transition("approve", actor="alice")
    # Message names both the current state and the attempted action.
    message = str(excinfo.value)
    assert "DRAFT" in message
    assert "approve" in message


def test_terminal_approved_rejects_all_actions() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("approve", actor="bob")

    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="alice")
    # State is unchanged after every rejected action.
    assert doc.state == "APPROVED"


def test_terminal_rejected_rejects_all_actions() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("reject", actor="bob")

    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="alice")
    assert doc.state == "REJECTED"


# ── Persistence + audit tests ──────────────────────────────────────────


def test_audit_row_written_per_transition(conn: sqlite3.Connection) -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice", reason="ready for review")
    doc.transition("approve", actor="bob", reason="looks good")

    save_document(conn, doc)

    trail = get_audit_trail(conn, doc_id=1)
    assert len(trail) == 2
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    assert trail[0]["actor"] == "alice"
    assert trail[0]["reason"] == "ready for review"
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[1]["to_state"] == "APPROVED"
    assert trail[1]["actor"] == "bob"
    assert trail[1]["reason"] == "looks good"


def test_audit_from_state_matches_pre_transition(conn: sqlite3.Connection) -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    save_document(conn, doc)

    loaded = load_document(conn, doc_id=1)
    assert loaded is not None
    assert loaded.state == "PENDING_REVIEW"

    loaded.transition("request_changes", actor="bob")
    save_document(conn, loaded)

    trail = get_audit_trail(conn, doc_id=1)
    assert len(trail) == 2
    # The second entry's from_state is the state right before that transition,
    # proving no audit row was written for a state that was skipped.
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[1]["to_state"] == "CHANGES_REQUESTED"


def test_project_id_isolation_two_docs_same_id(conn: sqlite3.Connection) -> None:
    doc_a = ReviewableDocument(doc_id=1, project_id="project-a")
    doc_b = ReviewableDocument(doc_id=1, project_id="project-b")

    doc_a.transition("submit", actor="alice")
    save_document(conn, doc_a)

    doc_b.transition("submit", actor="bob")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, doc_id=1, project_id="project-a")
    loaded_b = load_document(conn, doc_id=1, project_id="project-b")

    assert loaded_a is not None
    assert loaded_b is not None
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "PENDING_REVIEW"

    trail_a = get_audit_trail(conn, doc_id=1, project_id="project-a")
    trail_b = get_audit_trail(conn, doc_id=1, project_id="project-b")

    assert len(trail_a) == 1
    assert len(trail_b) == 1
    assert trail_a[0]["actor"] == "alice"
    assert trail_b[0]["actor"] == "bob"
