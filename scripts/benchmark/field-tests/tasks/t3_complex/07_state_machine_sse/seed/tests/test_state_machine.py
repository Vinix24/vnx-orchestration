"""Contract tests for the reviewable-document state-machine.

The 12 test names below match the contract in instruction.md exactly. Tests
that need persistence use the ``conn`` fixture; pure state-machine tests
don't touch the DB.

The migration is resolved relative to ``__file__`` so the suite works both
when run from the seed directory and when verify.py copies files into a
temporary directory and runs pytest from there.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from persistence import get_audit_trail, load_document, save_document
from state_machine import InvalidTransition, ReviewableDocument

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "001_state_machine.sql"
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the migration applied + FK on."""
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(MIGRATION_PATH.read_text())
    return db


# ── Pure state-machine tests (no persistence) ─────────────────────────────


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
    doc.transition("submit", actor="alice", reason="addressed feedback")
    assert doc.state == "PENDING_REVIEW"


def test_invalid_transition_from_draft_approve_raises() -> None:
    doc = ReviewableDocument(doc_id=1)
    with pytest.raises(InvalidTransition) as excinfo:
        doc.transition("approve", actor="alice")
    # Message must name both the current state and the attempted action so
    # operators can debug rejected requests without re-deriving context.
    message = str(excinfo.value)
    assert "DRAFT" in message
    assert "approve" in message
    # The rejected transition must NOT mutate the document.
    assert doc.state == "DRAFT"


def test_terminal_approved_rejects_all_actions() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("approve", actor="bob")

    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="alice")
    assert doc.state == "APPROVED"


def test_terminal_rejected_rejects_all_actions() -> None:
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="alice")
    doc.transition("reject", actor="bob")

    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="alice")
    assert doc.state == "REJECTED"


# ── Persistence + audit tests ─────────────────────────────────────────────


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
    # A document round-tripped through the DB (save → load → transition →
    # save) must still record from_state as the state immediately before
    # each transition, with no skipping.
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
    # First transition's from_state is the initial DRAFT.
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    # Second transition's from_state matches the state right before — no skip.
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[1]["to_state"] == "CHANGES_REQUESTED"


def test_project_id_isolation_two_docs_same_id(conn: sqlite3.Connection) -> None:
    # Two documents share doc_id=1 but live in different projects — they
    # must be fully independent rows, audit trails, and load lookups.
    doc_a = ReviewableDocument(doc_id=1, project_id="project-a")
    doc_b = ReviewableDocument(doc_id=1, project_id="project-b")

    doc_a.transition("submit", actor="alice")
    save_document(conn, doc_a)

    doc_b.transition("submit", actor="bob")
    doc_b.transition("approve", actor="bob")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, doc_id=1, project_id="project-a")
    loaded_b = load_document(conn, doc_id=1, project_id="project-b")

    assert loaded_a is not None
    assert loaded_b is not None
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "APPROVED"
    assert loaded_a.project_id == "project-a"
    assert loaded_b.project_id == "project-b"

    trail_a = get_audit_trail(conn, doc_id=1, project_id="project-a")
    trail_b = get_audit_trail(conn, doc_id=1, project_id="project-b")

    assert len(trail_a) == 1
    assert len(trail_b) == 2
    assert trail_a[0]["actor"] == "alice"
    assert trail_b[0]["actor"] == "bob"
    assert trail_b[1]["actor"] == "bob"
    assert trail_b[1]["to_state"] == "APPROVED"
