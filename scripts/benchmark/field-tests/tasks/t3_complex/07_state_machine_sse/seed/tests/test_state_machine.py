"""Contract tests for the review state-machine.

12 tests covering: initial state, every legal transition, terminal rejection,
audit-trail persistence, and project_id isolation.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# verify.py copies the seed layout into a tmp dir and runs pytest from cwd=tmp.
# Prepending the project root makes `state_machine` / `persistence` resolvable
# whether or not pytest's import-mode adds it for us.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from state_machine import InvalidTransition, ReviewableDocument  # noqa: E402
from persistence import (  # noqa: E402
    get_audit_trail,
    load_document,
    save_document,
)

MIGRATION_SQL = (_ROOT / "migrations" / "001_state_machine.sql").read_text()


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "state.db"
    c = sqlite3.connect(db_path)
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
    doc.transition("submit", "alice")
    assert doc.state == "PENDING_REVIEW"


def test_approve_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("approve", "bob", reason="lgtm")
    assert doc.state == "APPROVED"


def test_request_changes_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob", reason="needs more tests")
    assert doc.state == "CHANGES_REQUESTED"


def test_reject_from_pending_review():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob", reason="duplicate")
    assert doc.state == "REJECTED"


def test_resubmit_from_changes_requested():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob")
    doc.transition("submit", "alice")
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
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")


def test_terminal_rejected_rejects_all_actions():
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("reject", "bob")
    assert doc.state == "REJECTED"
    for action in ("submit", "approve", "request_changes", "reject"):
        with pytest.raises(InvalidTransition):
            doc.transition(action, "anyone")


def test_audit_row_written_per_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice", reason="initial submit")
    doc.transition("approve", "bob", reason="ship it")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    assert len(trail) == 2
    assert trail[0]["from_state"] == "DRAFT"
    assert trail[0]["to_state"] == "PENDING_REVIEW"
    assert trail[0]["actor"] == "alice"
    assert trail[0]["reason"] == "initial submit"
    assert trail[1]["from_state"] == "PENDING_REVIEW"
    assert trail[1]["to_state"] == "APPROVED"
    assert trail[1]["actor"] == "bob"
    assert trail[1]["reason"] == "ship it"


def test_audit_from_state_matches_pre_transition(conn):
    doc = ReviewableDocument(1)
    doc.transition("submit", "alice")
    doc.transition("request_changes", "bob")
    doc.transition("submit", "alice")
    doc.transition("approve", "bob")
    save_document(conn, doc)

    trail = get_audit_trail(conn, 1)
    pairs = [(row["from_state"], row["to_state"]) for row in trail]
    assert pairs == [
        ("DRAFT", "PENDING_REVIEW"),
        ("PENDING_REVIEW", "CHANGES_REQUESTED"),
        ("CHANGES_REQUESTED", "PENDING_REVIEW"),
        ("PENDING_REVIEW", "APPROVED"),
    ]
    # And every from_state matches the to_state of the previous row.
    for prev, curr in zip(trail, trail[1:]):
        assert curr["from_state"] == prev["to_state"]


def test_project_id_isolation_two_docs_same_id(conn):
    doc_a = ReviewableDocument(1, project_id="proj_a")
    doc_a.transition("submit", "alice")
    save_document(conn, doc_a)

    doc_b = ReviewableDocument(1, project_id="proj_b")
    doc_b.transition("submit", "bob")
    doc_b.transition("approve", "carol")
    save_document(conn, doc_b)

    loaded_a = load_document(conn, 1, "proj_a")
    loaded_b = load_document(conn, 1, "proj_b")
    assert loaded_a.state == "PENDING_REVIEW"
    assert loaded_b.state == "APPROVED"

    trail_a = get_audit_trail(conn, 1, "proj_a")
    trail_b = get_audit_trail(conn, 1, "proj_b")
    assert len(trail_a) == 1
    assert len(trail_b) == 2
    # Trails are scoped: no cross-project bleed.
    assert all(row["actor"] in {"alice"} for row in trail_a)
    assert {row["actor"] for row in trail_b} == {"bob", "carol"}
