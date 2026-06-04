"""Contract tests for the reviewable-document state machine + persistence."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persistence import get_audit_trail, load_document, save_document  # noqa: E402
from state_machine import (  # noqa: E402
    APPROVED,
    CHANGES_REQUESTED,
    DRAFT,
    PENDING_REVIEW,
    REJECTED,
    InvalidTransition,
    ReviewableDocument,
)

MIGRATION = ROOT / "migrations" / "001_state_machine.sql"
ALL_ACTIONS = ["submit", "approve", "request_changes", "reject"]


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(MIGRATION.read_text())
    yield connection
    connection.close()


def test_initial_state_is_draft():
    doc = ReviewableDocument(1)
    assert doc.state == DRAFT


def test_submit_from_draft_goes_to_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author", "ready for review")
    assert doc.state == PENDING_REVIEW


def test_approve_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("approve", "reviewer", "looks good")
    assert doc.state == APPROVED


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer", "needs work")
    assert doc.state == CHANGES_REQUESTED


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("reject", "reviewer", "out of scope")
    assert doc.state == REJECTED


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer", "needs work")
    doc.transition("submit", "author", "addressed feedback")
    assert doc.state == PENDING_REVIEW


def test_invalid_transition_from_draft_approve_raises():
    doc = ReviewableDocument(1)
    with pytest.raises(InvalidTransition):
        doc.transition("approve", "reviewer", "skip the line")
    assert doc.state == DRAFT  # state unchanged on rejected action


def test_terminal_approved_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("approve", "reviewer")
    for action in ALL_ACTIONS:
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")
    assert doc.state == APPROVED


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("reject", "reviewer")
    for action in ALL_ACTIONS:
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")
    assert doc.state == REJECTED


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "author", "first pass")
    doc.transition("request_changes", "reviewer", "typo in intro")
    doc.transition("submit", "author", "fixed typo")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 3
    for row in trail:
        assert set(row) == {
            "from_state", "to_state", "actor", "reason", "occurred_at",
        }
    assert trail[1]["actor"] == "reviewer"
    assert trail[1]["reason"] == "typo in intro"

    # Saving again must not duplicate already-persisted audit rows.
    save_document(conn, doc)
    assert len(get_audit_trail(conn, 1)) == 3


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "author")
    doc.transition("request_changes", "reviewer")
    doc.transition("submit", "author")
    doc.transition("approve", "reviewer")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert trail[0]["from_state"] == DRAFT
    # Every row chains: from_state equals the previous row's to_state.
    for prev, cur in zip(trail, trail[1:]):
        assert cur["from_state"] == prev["to_state"]
    assert trail[-1]["to_state"] == APPROVED


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(1, project_id="project-a")
    doc_a.transition("submit", "author-a")
    doc_a.transition("approve", "reviewer-a")
    save_document(conn, doc_a)

    doc_b = ReviewableDocument(1, project_id="project-b")
    doc_b.transition("submit", "author-b")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, 1, project_id="project-a")
    loaded_b = load_document(conn, 1, project_id="project-b")
    assert loaded_a.state == APPROVED
    assert loaded_b.state == PENDING_REVIEW

    trail_a = get_audit_trail(conn, 1, project_id="project-a")
    trail_b = get_audit_trail(conn, 1, project_id="project-b")
    assert len(trail_a) == 2
    assert len(trail_b) == 1
    assert all(row["actor"].endswith("-a") for row in trail_a)
    assert all(row["actor"].endswith("-b") for row in trail_b)
