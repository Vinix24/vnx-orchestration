#!/usr/bin/env python3
"""Tests for conversation timeline API endpoint (PR-2).

Validates gate_pr2_latest_first_timeline:
  - Latest-first is the default for the main operator timeline
  - Sort toggle can switch between latest-first and oldest-first
    without dropping selected context
  - Rotation-chain continuity remains visible in the timeline
  - Terminal and worktree filters work correctly
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import sys

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

DASHBOARD_DIR = str(Path(__file__).resolve().parent.parent / "dashboard")
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)

from conversation_read_model import (
    ConversationReadModel,
    ConversationSession,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_index_db(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE conversations (
            session_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            slug TEXT,
            title TEXT,
            excerpt TEXT,
            first_message TEXT,
            last_message TEXT,
            message_count INTEGER DEFAULT 0,
            user_message_count INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cwd TEXT,
            version TEXT,
            file_path TEXT,
            file_mtime REAL,
            indexed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX idx_conversations_last_message ON conversations(last_message DESC)"
    )
    for row in rows:
        conn.execute(
            """INSERT INTO conversations
               (session_id, project_path, title, last_message,
                message_count, user_message_count, total_tokens, cwd, file_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["session_id"],
                row.get("project_path", "/test/project"),
                row.get("title", "Test session"),
                row.get("last_message"),
                row.get("message_count", 10),
                row.get("user_message_count", 5),
                row.get("total_tokens", 1000),
                row.get("cwd", ""),
                row.get("file_path", ""),
            ),
        )
    conn.commit()
    conn.close()


def _create_receipt_file(path: str, events: list[dict]) -> None:
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def timeline_sessions():
    """Sessions spanning multiple terminals and worktrees for timeline testing."""
    return [
        {
            "session_id": "sess-t0-dispatch",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Dispatch planning session",
            "last_message": "2026-03-31T18:00:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T0",
            "message_count": 20,
            "user_message_count": 10,
            "total_tokens": 5000,
        },
        {
            "session_id": "sess-t1-impl",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Feature implementation",
            "last_message": "2026-03-31T19:30:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T1",
            "message_count": 50,
            "user_message_count": 25,
            "total_tokens": 15000,
        },
        {
            "session_id": "sess-t2-test",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Test validation run",
            "last_message": "2026-03-31T17:00:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T2",
            "message_count": 15,
            "user_message_count": 8,
            "total_tokens": 3000,
        },
        {
            "session_id": "sess-t1-older",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Earlier T1 work",
            "last_message": "2026-03-31T14:00:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T1",
            "message_count": 30,
            "user_message_count": 12,
            "total_tokens": 8000,
        },
        {
            "session_id": "sess-other-wt",
            "project_path": "/Users/op/Dev/project-wt-feat",
            "title": "Feature branch session",
            "last_message": "2026-03-31T16:00:00Z",
            "cwd": "/Users/op/Dev/project-wt-feat/.claude/terminals/T1",
            "message_count": 10,
            "user_message_count": 5,
            "total_tokens": 2000,
        },
        {
            "session_id": "sess-null-ts",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Interrupted session",
            "last_message": None,
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T3",
            "message_count": 0,
            "user_message_count": 0,
            "total_tokens": 0,
        },
    ]


@pytest.fixture
def model_with_data(tmp_dir, timeline_sessions):
    """Create a read model ready for timeline testing."""
    db_path = os.path.join(tmp_dir, "conversation-index.db")
    _create_index_db(db_path, timeline_sessions)

    worktree_roots = [
        "/Users/op/Dev/project-wt",
        "/Users/op/Dev/project-wt-feat",
    ]

    receipt_path = os.path.join(tmp_dir, "t0_receipts.ndjson")
    _create_receipt_file(receipt_path, [
        {
            "event_type": "context_rotation_continuation",
            "terminal": "T1",
            "dispatch_id": "dispatch-timeline-001",
            "timestamp": "2026-03-31T15:00:00Z",
        },
        {
            "event_type": "context_rotation_continuation",
            "terminal": "T1",
            "dispatch_id": "dispatch-timeline-001",
            "timestamp": "2026-03-31T19:00:00Z",
        },
    ])

    model = ConversationReadModel(
        claude_index_db=db_path,
        worktree_roots=worktree_roots,
        receipt_path=receipt_path,
    )
    return model


# ---------------------------------------------------------------------------
# Gate: Latest-first is the default
# ---------------------------------------------------------------------------

class TestLatestFirstDefault:
    """ORDER-1: Latest-first is the canonical default view."""

    def test_default_sort_is_desc(self, model_with_data):
        sessions = model_with_data.list_sessions()
        timestamps = [s.last_message for s in sessions if s.last_message]
        assert timestamps == sorted(timestamps, reverse=True), (
            "Default sort must be latest-first (DESC)"
        )

    def test_most_recent_session_is_first(self, model_with_data):
        sessions = model_with_data.list_sessions()
        assert sessions[0].session_id == "sess-t1-impl", (
            "Most recent session (19:30) must appear first"
        )

    def test_null_timestamps_sort_last(self, model_with_data):
        sessions = model_with_data.list_sessions()
        non_null = [s for s in sessions if s.last_message is not None]
        null_sessions = [s for s in sessions if s.last_message is None]
        if null_sessions and non_null:
            last_non_null_idx = max(sessions.index(s) for s in non_null)
            first_null_idx = min(sessions.index(s) for s in null_sessions)
            assert first_null_idx > last_non_null_idx


# ---------------------------------------------------------------------------
# Gate: Sort toggle preserves context
# ---------------------------------------------------------------------------

class TestSortTogglePreservesContext:
    """ORDER-2: Sort toggle must not drop selected context."""

    def test_toggle_to_asc_reverses_order(self, model_with_data):
        desc_sessions = model_with_data.list_sessions(sort_order="DESC")
        asc_sessions = model_with_data.list_sessions(sort_order="ASC")

        desc_ts = [s.last_message for s in desc_sessions if s.last_message]
        asc_ts = [s.last_message for s in asc_sessions if s.last_message]

        assert desc_ts == sorted(desc_ts, reverse=True)
        assert asc_ts == sorted(asc_ts)

    def test_same_sessions_both_orders(self, model_with_data):
        desc_ids = {s.session_id for s in model_with_data.list_sessions(sort_order="DESC")}
        asc_ids = {s.session_id for s in model_with_data.list_sessions(sort_order="ASC")}
        assert desc_ids == asc_ids, (
            "Sort toggle must not change which sessions are visible"
        )

    def test_selected_session_survives_sort_toggle(self, model_with_data):
        """Simulates selecting a session then toggling sort order."""
        desc_sessions = model_with_data.list_sessions(sort_order="DESC")
        selected_id = desc_sessions[1].session_id  # Select the 2nd item

        asc_sessions = model_with_data.list_sessions(sort_order="ASC")
        asc_ids = [s.session_id for s in asc_sessions]
        assert selected_id in asc_ids, (
            "Selected session must remain in the list after sort toggle"
        )

    def test_worktree_context_preserved_across_sort(self, model_with_data):
        """Worktree linkage must remain stable regardless of sort order."""
        desc_sessions = model_with_data.list_sessions(sort_order="DESC")
        asc_sessions = model_with_data.list_sessions(sort_order="ASC")

        desc_roots = {s.session_id: s.worktree_root for s in desc_sessions}
        asc_roots = {s.session_id: s.worktree_root for s in asc_sessions}

        assert desc_roots == asc_roots, (
            "Worktree linkage must not change when sort order toggles"
        )


# ---------------------------------------------------------------------------
# Gate: Rotation chain continuity visible
# ---------------------------------------------------------------------------

class TestRotationChainVisibility:
    """Rotation chains must remain visible in the timeline."""

    def test_rotation_chain_discovered(self, model_with_data):
        sessions = model_with_data.list_sessions()
        chains = model_with_data.discover_rotation_chains(sessions)
        dispatch_ids = {c.dispatch_id for c in chains}
        assert "dispatch-timeline-001" in dispatch_ids

    def test_rotation_chain_contains_t1_sessions(self, model_with_data):
        sessions = model_with_data.list_sessions()
        chains = model_with_data.discover_rotation_chains(sessions)
        t1_chain = next(
            (c for c in chains if c.dispatch_id == "dispatch-timeline-001"), None
        )
        assert t1_chain is not None
        chain_terminals = {s.terminal for s in t1_chain.sessions}
        assert "T1" in chain_terminals

    def test_rotation_chain_sorted_latest_first(self, model_with_data):
        sessions = model_with_data.list_sessions()
        chains = model_with_data.discover_rotation_chains(sessions)
        for chain in chains:
            ts = [s.last_message for s in chain.sessions if s.last_message]
            assert ts == sorted(ts, reverse=True), (
                "Within a chain, sessions must be latest-first (ORDER-3)"
            )

    def test_rotation_chain_metadata_in_api_response(self, model_with_data):
        """Chains must be serializable for the API response."""
        sessions = model_with_data.list_sessions()
        chains = model_with_data.discover_rotation_chains(sessions)
        for chain in chains:
            serialized = {
                "dispatch_id": chain.dispatch_id,
                "chain_depth": chain.chain_depth,
                "latest_message": chain.latest_message,
                "session_ids": [s.session_id for s in chain.sessions],
            }
            assert isinstance(json.dumps(serialized), str)
            assert serialized["chain_depth"] == len(chain.sessions)


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------

class TestTerminalFiltering:
    def test_filter_by_single_terminal(self, model_with_data):
        sessions = model_with_data.list_sessions(terminal_filter="T1")
        assert all(s.terminal == "T1" for s in sessions)
        assert len(sessions) >= 2

    def test_filter_preserves_sort_order(self, model_with_data):
        sessions = model_with_data.list_sessions(
            terminal_filter="T1", sort_order="DESC"
        )
        timestamps = [s.last_message for s in sessions if s.last_message]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_filter_by_worktree(self, model_with_data):
        sessions = model_with_data.list_sessions(
            worktree_filter="/Users/op/Dev/project-wt-feat"
        )
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-other-wt"


class TestWorktreeGrouping:
    def test_group_by_worktree(self, model_with_data):
        sessions = model_with_data.list_sessions()
        groups = model_with_data.group_by_worktree(sessions)
        roots = {g.worktree_root for g in groups}
        assert "/Users/op/Dev/project-wt" in roots
        assert "/Users/op/Dev/project-wt-feat" in roots

    def test_groups_contain_all_sessions(self, model_with_data):
        sessions = model_with_data.list_sessions()
        groups = model_with_data.group_by_worktree(sessions)
        grouped_ids = set()
        for g in groups:
            for s in g.sessions:
                grouped_ids.add(s.session_id)
        original_ids = {s.session_id for s in sessions}
        assert grouped_ids == original_ids


# ---------------------------------------------------------------------------
# API response shape tests
# ---------------------------------------------------------------------------

class TestApiResponseShape:
    """Validates the shape of data the /api/conversations endpoint returns."""

    def test_response_includes_required_fields(self, model_with_data):
        sessions = model_with_data.list_sessions()
        groups = model_with_data.group_by_worktree(sessions)
        chains = model_with_data.discover_rotation_chains(sessions)

        response = {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "project_path": s.project_path,
                    "cwd": s.cwd,
                    "last_message": s.last_message,
                    "title": s.title,
                    "message_count": s.message_count,
                    "user_message_count": s.user_message_count,
                    "total_tokens": s.total_tokens,
                    "terminal": s.terminal,
                    "worktree_root": s.worktree_root,
                    "worktree_exists": s.worktree_exists,
                }
                for s in sessions
            ],
            "sort_order": "DESC",
            "total": len(sessions),
            "worktree_groups": [
                {
                    "worktree_root": g.worktree_root,
                    "worktree_exists": g.worktree_exists,
                    "session_ids": [s.session_id for s in g.sessions],
                }
                for g in groups
            ],
            "rotation_chains": [
                {
                    "dispatch_id": c.dispatch_id,
                    "chain_depth": c.chain_depth,
                    "latest_message": c.latest_message,
                    "session_ids": [s.session_id for s in c.sessions],
                }
                for c in chains
            ],
        }

        serialized = json.dumps(response)
        parsed = json.loads(serialized)

        assert "sessions" in parsed
        assert "sort_order" in parsed
        assert "total" in parsed
        assert parsed["sort_order"] == "DESC"
        assert parsed["total"] == len(sessions)

        for sess in parsed["sessions"]:
            assert "session_id" in sess
            assert "terminal" in sess
            assert "worktree_root" in sess
            assert "last_message" in sess

    def test_empty_db_returns_empty_response(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "empty.db")
        _create_index_db(db_path, [])
        model = ConversationReadModel(claude_index_db=db_path)
        sessions = model.list_sessions()
        assert sessions == []
