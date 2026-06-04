"""Contract tests for the reviewable-document state-machine."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


SEED_DIR = Path(__file__).resolve().parent.parent
if str(SEED_DIR) not in sys.path:
    sys.path.insert(0, str(SEED_DIR))

from state_machine import (  # noqa: E402
    APPROVED,
    CHANGES_REQUESTED,
    DRAFT,
    PENDING_REVIEW,
    REJECTED,
    InvalidTransition,
    ReviewableDocument,
)
from persistence import (  # noqa: E402
    get_audit_trail,
    load_document,
    save_document,
)


MIGRATION_PATH = SEED_DIR / "migrations" / "001_state_machine.sql"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(MIGRATION_PATH.read_text())
    yield connection
    connection.close()


def test_initial_state_is_draft():
    doc = ReviewableDocument(doc_id=1)
    assert doc.state == DRAFT


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    assert doc.state == PENDING_REVIEW


def test_approve_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("approve", actor="reviewer", reason="LGTM")
    assert doc.state == APPROVED


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("request_changes", actor="reviewer", reason="needs polish")
    assert doc.state == CHANGES_REQUESTED


def test_reject_from_pending_review():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("reject", actor="reviewer", reason="off-scope")
    assert doc.state == REJECTED


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("request_changes", actor="reviewer", reason="nits")
    doc.transition("submit", actor="author", reason="addressed")
    assert doc.state == PENDING_REVIEW


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(doc_id=1)
    with pytest.raises(InvalidTransition):
        doc.transition("approve", actor="reviewer")
    assert doc.state == DRAFT


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("approve", actor="reviewer")
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="anyone")
    assert doc.state == APPROVED


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(doc_id=1)
    doc.transition("submit", actor="author")
    doc.transition("reject", actor="reviewer")
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, actor="anyone")
    assert doc.state == REJECTED


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(doc_id=42)
    doc.transition("submit", actor="author", reason="initial")
    doc.transition("request_changes", actor="reviewer", reason="needs work")
    doc.transition("submit", actor="author", reason="fixed")
    doc.transition("approve", actor="reviewer", reason="LGTM")
    save_document(conn, doc)

    trail = get_audit_trail(conn, doc_id=42)
    assert len(trail) == 4
    assert [(r["from_state"], r["to_state"]) for r in trail] == [
        (DRAFT, PENDING_REVIEW),
        (PENDING_REVIEW, CHANGES_REQUESTED),
        (CHANGES_REQUESTED, PENDING_REVIEW),
        (PENDING_REVIEW, APPROVED),
    ]
    assert [r["actor"] for r in trail] == ["author", "reviewer", "author", "reviewer"]
    assert [r["reason"] for r in trail] == ["initial", "needs work", "fixed", "LGTM"]


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(doc_id=7)
    doc.transition("submit", actor="author")
    save_document(conn, doc)
    reloaded = load_document(conn, doc_id=7)
    reloaded.transition("approve", actor="reviewer")
    save_document(conn, reloaded)

    trail = get_audit_trail(conn, doc_id=7)
    assert len(trail) == 2
    # The second audit row's from_state must equal the persisted state
    # immediately before the transition (no state skipping).
    assert trail[0]["from_state"] == DRAFT
    assert trail[0]["to_state"] == PENDING_REVIEW
    assert trail[1]["from_state"] == PENDING_REVIEW
    assert trail[1]["to_state"] == APPROVED


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(doc_id=1, project_id="alpha")
    doc_a.transition("submit", actor="alice")
    save_document(conn, doc_a)

    doc_b = ReviewableDocument(doc_id=1, project_id="beta")
    doc_b.transition("submit", actor="bob")
    doc_b.transition("reject", actor="bob", reason="duplicate")
    save_document(conn, doc_b)

    reloaded_a = load_document(conn, doc_id=1, project_id="alpha")
    reloaded_b = load_document(conn, doc_id=1, project_id="beta")
    assert reloaded_a.state == PENDING_REVIEW
    assert reloaded_b.state == REJECTED

    trail_a = get_audit_trail(conn, doc_id=1, project_id="alpha")
    trail_b = get_audit_trail(conn, doc_id=1, project_id="beta")
    assert len(trail_a) == 1
    assert len(trail_b) == 2
    assert all(r["actor"] == "alice" for r in trail_a)
    assert all(r["actor"] == "bob" for r in trail_b)
