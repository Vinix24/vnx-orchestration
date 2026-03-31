#!/usr/bin/env python3
"""Tests for VNX Conversation Read Model (PR-1).

Validates:
  - Worktree linkage via cwd path containment (LINK-1, LINK-3)
  - Terminal detection from cwd segments (LINK-2)
  - Latest-first sorting with NULL handling (ORDER-1, ORDER-2, ORDER-3)
  - Multi-project and mixed session grouping
  - Rotation chain discovery from receipt events (ROTATE-1)
  - Stale worktree handling (§2.3)
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Ensure scripts/lib is importable
import sys
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from conversation_read_model import (
    ConversationReadModel,
    ConversationSession,
    RotationChain,
    WorktreeGroup,
    derive_terminal,
    derive_worktree_root,
    load_rotation_events,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_index_db(db_path: str, rows: list[dict]) -> None:
    """Create an in-memory-style conversation-index.db with test data."""
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
    """Write NDJSON receipt events to a file."""
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sample_sessions():
    """Session data spanning two worktrees and one non-worktree project."""
    return [
        {
            "session_id": "sess-wt1-t0-001",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Dispatch planning",
            "last_message": "2026-03-31T18:00:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T0",
            "message_count": 20,
            "user_message_count": 10,
            "total_tokens": 5000,
        },
        {
            "session_id": "sess-wt1-t1-002",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Feature implementation",
            "last_message": "2026-03-31T19:30:00Z",
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T1",
            "message_count": 50,
            "user_message_count": 25,
            "total_tokens": 15000,
        },
        {
            "session_id": "sess-wt2-t1-003",
            "project_path": "/Users/op/Dev/project-wt-feat",
            "title": "Bug fix in feature branch",
            "last_message": "2026-03-31T17:00:00Z",
            "cwd": "/Users/op/Dev/project-wt-feat/.claude/terminals/T1",
            "message_count": 15,
            "user_message_count": 8,
            "total_tokens": 3000,
        },
        {
            "session_id": "sess-main-t2-004",
            "project_path": "/Users/op/Dev/project",
            "title": "Test validation",
            "last_message": "2026-03-31T16:00:00Z",
            "cwd": "/Users/op/Dev/project/.claude/terminals/T2",
            "message_count": 30,
            "user_message_count": 12,
            "total_tokens": 8000,
        },
        {
            "session_id": "sess-no-terminal-005",
            "project_path": "/Users/op/Dev/standalone",
            "title": "Ad-hoc session",
            "last_message": "2026-03-31T15:00:00Z",
            "cwd": "/Users/op/Dev/standalone",
            "message_count": 5,
            "user_message_count": 3,
            "total_tokens": 500,
        },
        {
            "session_id": "sess-null-ts-006",
            "project_path": "/Users/op/Dev/project-wt",
            "title": "Interrupted session",
            "last_message": None,
            "cwd": "/Users/op/Dev/project-wt/.claude/terminals/T3",
            "message_count": 0,
            "user_message_count": 0,
            "total_tokens": 0,
        },
    ]


# ---------------------------------------------------------------------------
# Unit tests: derive_terminal
# ---------------------------------------------------------------------------

class TestDeriveTerminal:
    def test_standard_terminal_paths(self):
        assert derive_terminal("/project/.claude/terminals/T0") == "T0"
        assert derive_terminal("/project/.claude/terminals/T1") == "T1"
        assert derive_terminal("/project/.claude/terminals/T2") == "T2"
        assert derive_terminal("/project/.claude/terminals/T3") == "T3"

    def test_nested_worktree_path(self):
        cwd = "/Users/op/Dev/project-wt/.claude/terminals/T1"
        assert derive_terminal(cwd) == "T1"

    def test_no_terminal_pattern(self):
        assert derive_terminal("/Users/op/Dev/project") is None
        assert derive_terminal("/Users/op/Dev/project/.claude") is None
        assert derive_terminal("") is None

    def test_empty_and_none(self):
        assert derive_terminal("") is None
        assert derive_terminal(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Unit tests: derive_worktree_root
# ---------------------------------------------------------------------------

class TestDeriveWorktreeRoot:
    def test_exact_match(self):
        roots = ["/Users/op/Dev/project-wt"]
        assert derive_worktree_root("/Users/op/Dev/project-wt", roots) == "/Users/op/Dev/project-wt"

    def test_subpath_match(self):
        roots = ["/Users/op/Dev/project-wt"]
        cwd = "/Users/op/Dev/project-wt/.claude/terminals/T1"
        assert derive_worktree_root(cwd, roots) == "/Users/op/Dev/project-wt"

    def test_longest_match_wins(self):
        """LINK-3: main repo and worktree of same repo are separate scopes."""
        roots = ["/Users/op/Dev/project", "/Users/op/Dev/project-wt"]
        cwd = "/Users/op/Dev/project-wt/.claude/terminals/T1"
        assert derive_worktree_root(cwd, roots) == "/Users/op/Dev/project-wt"

    def test_no_match(self):
        roots = ["/Users/op/Dev/project-wt"]
        assert derive_worktree_root("/Users/op/Dev/other", roots) is None

    def test_empty_cwd(self):
        assert derive_worktree_root("", ["/a"]) is None

    def test_empty_roots(self):
        assert derive_worktree_root("/some/path", []) is None

    def test_trailing_slash_normalization(self):
        roots = ["/Users/op/Dev/project-wt/"]
        cwd = "/Users/op/Dev/project-wt/.claude/terminals/T0"
        assert derive_worktree_root(cwd, roots) == "/Users/op/Dev/project-wt"

    def test_partial_name_no_false_match(self):
        """'/project-wt-feat' must NOT match root '/project-wt'."""
        roots = ["/Users/op/Dev/project-wt"]
        cwd = "/Users/op/Dev/project-wt-feat/.claude/terminals/T1"
        assert derive_worktree_root(cwd, roots) is None


# ---------------------------------------------------------------------------
# Unit tests: load_rotation_events
# ---------------------------------------------------------------------------

class TestLoadRotationEvents:
    def test_basic_loading(self, tmp_dir):
        events = [
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "dispatch-001",
                "timestamp": "2026-03-31T18:00:00Z",
            },
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "dispatch-001",
                "timestamp": "2026-03-31T19:00:00Z",
            },
        ]
        receipt_path = os.path.join(tmp_dir, "receipts.ndjson")
        _create_receipt_file(receipt_path, events)

        chains = load_rotation_events(receipt_path)
        assert "dispatch-001" in chains
        assert len(chains["dispatch-001"]) == 2
        assert chains["dispatch-001"][0]["timestamp"] < chains["dispatch-001"][1]["timestamp"]

    def test_filters_non_rotation_events(self, tmp_dir):
        events = [
            {"event_type": "dispatch_completed", "dispatch_id": "d-1"},
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "d-2",
                "timestamp": "2026-03-31T18:00:00Z",
            },
        ]
        receipt_path = os.path.join(tmp_dir, "receipts.ndjson")
        _create_receipt_file(receipt_path, events)

        chains = load_rotation_events(receipt_path)
        assert "d-1" not in chains
        assert "d-2" in chains

    def test_skips_unknown_dispatch_id(self, tmp_dir):
        events = [
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "unknown",
                "timestamp": "2026-03-31T18:00:00Z",
            },
        ]
        receipt_path = os.path.join(tmp_dir, "receipts.ndjson")
        _create_receipt_file(receipt_path, events)

        chains = load_rotation_events(receipt_path)
        assert len(chains) == 0

    def test_missing_file_returns_empty(self):
        chains = load_rotation_events("/nonexistent/file.ndjson")
        assert chains == {}

    def test_malformed_json_lines_skipped(self, tmp_dir):
        receipt_path = os.path.join(tmp_dir, "receipts.ndjson")
        with open(receipt_path, "w") as f:
            f.write("not-json\n")
            f.write(json.dumps({
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "d-ok",
                "timestamp": "2026-03-31T18:00:00Z",
            }) + "\n")

        chains = load_rotation_events(receipt_path)
        assert "d-ok" in chains

    def test_multiple_dispatch_ids(self, tmp_dir):
        events = [
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "d-alpha",
                "timestamp": "2026-03-31T18:00:00Z",
            },
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T2",
                "dispatch_id": "d-beta",
                "timestamp": "2026-03-31T19:00:00Z",
            },
        ]
        receipt_path = os.path.join(tmp_dir, "receipts.ndjson")
        _create_receipt_file(receipt_path, events)

        chains = load_rotation_events(receipt_path)
        assert len(chains) == 2
        assert "d-alpha" in chains
        assert "d-beta" in chains


# ---------------------------------------------------------------------------
# Integration tests: ConversationReadModel
# ---------------------------------------------------------------------------

class TestConversationReadModel:
    @pytest.fixture
    def model_setup(self, tmp_dir, sample_sessions):
        """Create a read model with a test database."""
        db_path = os.path.join(tmp_dir, "conversation-index.db")
        _create_index_db(db_path, sample_sessions)

        worktree_roots = [
            "/Users/op/Dev/project-wt",
            "/Users/op/Dev/project-wt-feat",
            "/Users/op/Dev/project",
        ]

        receipt_path = os.path.join(tmp_dir, "t0_receipts.ndjson")
        _create_receipt_file(receipt_path, [
            {
                "event_type": "context_rotation_continuation",
                "terminal": "T1",
                "dispatch_id": "dispatch-rotation-001",
                "timestamp": "2026-03-31T19:00:00Z",
            },
        ])

        model = ConversationReadModel(
            claude_index_db=db_path,
            worktree_roots=worktree_roots,
            receipt_path=receipt_path,
        )
        return model

    # --- Sorting ---

    def test_latest_first_default(self, model_setup):
        """ORDER-1: Default sort is last_message DESC."""
        sessions = model_setup.list_sessions()
        timestamps = [s.last_message for s in sessions if s.last_message]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_oldest_first_toggle(self, model_setup):
        """ORDER-2: ASC sort works without changing other state."""
        sessions = model_setup.list_sessions(sort_order="ASC")
        timestamps = [s.last_message for s in sessions if s.last_message]
        assert timestamps == sorted(timestamps)

    def test_null_timestamp_sorts_last(self, model_setup):
        """Sessions with NULL last_message sort to the bottom."""
        sessions = model_setup.list_sessions()
        null_sessions = [s for s in sessions if s.last_message is None]
        non_null = [s for s in sessions if s.last_message is not None]
        # All null sessions should appear after all non-null sessions
        null_indices = [sessions.index(s) for s in null_sessions]
        non_null_indices = [sessions.index(s) for s in non_null]
        if null_sessions and non_null:
            assert min(null_indices) > max(non_null_indices)

    # --- Worktree Linkage ---

    def test_worktree_root_linked(self, model_setup):
        """LINK-1: Sessions get linked to the correct worktree root."""
        sessions = model_setup.list_sessions()
        by_id = {s.session_id: s for s in sessions}

        assert by_id["sess-wt1-t0-001"].worktree_root == "/Users/op/Dev/project-wt"
        assert by_id["sess-wt2-t1-003"].worktree_root == "/Users/op/Dev/project-wt-feat"
        assert by_id["sess-main-t2-004"].worktree_root == "/Users/op/Dev/project"

    def test_separate_worktree_scopes(self, model_setup):
        """LINK-3: Main repo and worktree of same repo are separate scopes."""
        sessions = model_setup.list_sessions()
        by_id = {s.session_id: s for s in sessions}

        wt1 = by_id["sess-wt1-t1-002"].worktree_root
        wt2 = by_id["sess-wt2-t1-003"].worktree_root
        assert wt1 != wt2

    def test_no_worktree_for_standalone(self, model_setup):
        """Sessions in non-worktree dirs get None worktree_root."""
        sessions = model_setup.list_sessions()
        by_id = {s.session_id: s for s in sessions}
        assert by_id["sess-no-terminal-005"].worktree_root is None

    # --- Terminal Detection ---

    def test_terminal_detected(self, model_setup):
        """LINK-2: Terminal identity derived from cwd path."""
        sessions = model_setup.list_sessions()
        by_id = {s.session_id: s for s in sessions}

        assert by_id["sess-wt1-t0-001"].terminal == "T0"
        assert by_id["sess-wt1-t1-002"].terminal == "T1"
        assert by_id["sess-wt2-t1-003"].terminal == "T1"
        assert by_id["sess-main-t2-004"].terminal == "T2"
        assert by_id["sess-null-ts-006"].terminal == "T3"

    def test_no_terminal_for_standalone(self, model_setup):
        """cwd without terminal pattern gets None."""
        sessions = model_setup.list_sessions()
        by_id = {s.session_id: s for s in sessions}
        assert by_id["sess-no-terminal-005"].terminal is None

    # --- Filtering ---

    def test_filter_by_project(self, model_setup):
        sessions = model_setup.list_sessions(
            project_filter="/Users/op/Dev/project-wt"
        )
        assert all(
            s.project_path == "/Users/op/Dev/project-wt" for s in sessions
        )
        assert len(sessions) == 3  # wt1-t0, wt1-t1, null-ts

    def test_filter_by_worktree(self, model_setup):
        sessions = model_setup.list_sessions(
            worktree_filter="/Users/op/Dev/project-wt-feat"
        )
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-wt2-t1-003"

    def test_filter_by_terminal(self, model_setup):
        sessions = model_setup.list_sessions(terminal_filter="T1")
        assert all(s.terminal == "T1" for s in sessions)
        assert len(sessions) == 2  # wt1-t1 and wt2-t1

    # --- Grouping ---

    def test_group_by_worktree(self, model_setup):
        sessions = model_setup.list_sessions()
        groups = model_setup.group_by_worktree(sessions)

        roots = {g.worktree_root for g in groups}
        assert "/Users/op/Dev/project-wt" in roots
        assert "/Users/op/Dev/project-wt-feat" in roots
        assert "/Users/op/Dev/project" in roots
        assert "" in roots  # standalone session

    def test_group_session_counts(self, model_setup):
        sessions = model_setup.list_sessions()
        groups = model_setup.group_by_worktree(sessions)
        by_root = {g.worktree_root: g for g in groups}

        assert len(by_root["/Users/op/Dev/project-wt"].sessions) == 3
        assert len(by_root["/Users/op/Dev/project-wt-feat"].sessions) == 1
        assert len(by_root["/Users/op/Dev/project"].sessions) == 1
        assert len(by_root[""].sessions) == 1

    # --- Stale Worktree Handling ---

    def test_stale_worktree_marked(self, model_setup):
        """§2.3: Sessions from non-existent worktrees are marked absent."""
        sessions = model_setup.list_sessions()
        # All our test worktree roots don't actually exist on disk
        for s in sessions:
            if s.worktree_root:
                assert s.worktree_exists is False

    # --- Rotation Chains ---

    def test_rotation_metadata_exposed(self, model_setup):
        """ROTATE-1: Rotation events are discoverable by dispatch_id."""
        meta = model_setup.get_rotation_metadata()
        assert "dispatch-rotation-001" in meta
        assert len(meta["dispatch-rotation-001"]) == 1

    def test_rotation_chain_discovery(self, model_setup):
        """Rotation chains link sessions matching terminal from events."""
        sessions = model_setup.list_sessions()
        chains = model_setup.discover_rotation_chains(sessions)
        # At least one chain should exist for dispatch-rotation-001 (T1 sessions)
        dispatch_ids = {c.dispatch_id for c in chains}
        assert "dispatch-rotation-001" in dispatch_ids

    def test_rotation_chain_sorted_latest_first(self, model_setup):
        """ORDER-3: Within a chain, sessions are latest-first."""
        sessions = model_setup.list_sessions()
        chains = model_setup.discover_rotation_chains(sessions)
        for chain in chains:
            ts = [s.last_message for s in chain.sessions if s.last_message]
            assert ts == sorted(ts, reverse=True)

    # --- Limit ---

    def test_limit_respected(self, model_setup):
        sessions = model_setup.list_sessions(limit=2)
        assert len(sessions) == 2


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_database(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "empty.db")
        _create_index_db(db_path, [])
        model = ConversationReadModel(claude_index_db=db_path)
        assert model.list_sessions() == []

    def test_session_with_empty_cwd(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test.db")
        _create_index_db(db_path, [
            {
                "session_id": "s1",
                "project_path": "/p",
                "cwd": "",
                "last_message": "2026-03-31T18:00:00Z",
            }
        ])
        model = ConversationReadModel(claude_index_db=db_path)
        sessions = model.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].terminal is None
        assert sessions[0].worktree_root is None

    def test_all_null_timestamps(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test.db")
        _create_index_db(db_path, [
            {"session_id": "s1", "project_path": "/p", "last_message": None},
            {"session_id": "s2", "project_path": "/p", "last_message": None},
        ])
        model = ConversationReadModel(claude_index_db=db_path)
        sessions = model.list_sessions()
        assert len(sessions) == 2

    def test_no_receipt_file(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test.db")
        _create_index_db(db_path, [
            {"session_id": "s1", "project_path": "/p", "last_message": "2026-03-31T18:00:00Z"},
        ])
        model = ConversationReadModel(
            claude_index_db=db_path,
            receipt_path="/nonexistent.ndjson",
        )
        sessions = model.list_sessions()
        chains = model.discover_rotation_chains(sessions)
        assert chains == []

    def test_invalid_sort_order_defaults_to_desc(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test.db")
        _create_index_db(db_path, [
            {"session_id": "s1", "project_path": "/p", "last_message": "2026-03-31T18:00:00Z"},
            {"session_id": "s2", "project_path": "/p", "last_message": "2026-03-31T19:00:00Z"},
        ])
        model = ConversationReadModel(claude_index_db=db_path)
        sessions = model.list_sessions(sort_order="INVALID")
        # Should default to DESC
        assert sessions[0].last_message > sessions[1].last_message

    def test_group_by_worktree_no_root(self, tmp_dir):
        """Sessions without worktree roots group under empty string."""
        db_path = os.path.join(tmp_dir, "test.db")
        _create_index_db(db_path, [
            {"session_id": "s1", "project_path": "/p", "cwd": "/standalone", "last_message": "2026-03-31T18:00:00Z"},
        ])
        model = ConversationReadModel(claude_index_db=db_path)
        sessions = model.list_sessions()
        groups = model.group_by_worktree(sessions)
        assert len(groups) == 1
        assert groups[0].worktree_root == ""

    def test_rotation_chain_depth(self, tmp_dir):
        """RotationChain.chain_depth reflects number of sessions."""
        chain = RotationChain(dispatch_id="d-1")
        assert chain.chain_depth == 0

        chain.sessions.append(ConversationSession(
            session_id="s1", project_path="/p", cwd="", last_message="2026-03-31T18:00:00Z",
            title="", message_count=0, user_message_count=0, total_tokens=0, file_path="",
        ))
        assert chain.chain_depth == 1

    def test_rotation_chain_latest_message(self):
        chain = RotationChain(dispatch_id="d-1", sessions=[
            ConversationSession(
                session_id="s1", project_path="/p", cwd="", last_message="2026-03-31T17:00:00Z",
                title="", message_count=0, user_message_count=0, total_tokens=0, file_path="",
            ),
            ConversationSession(
                session_id="s2", project_path="/p", cwd="", last_message="2026-03-31T19:00:00Z",
                title="", message_count=0, user_message_count=0, total_tokens=0, file_path="",
            ),
        ])
        assert chain.latest_message == "2026-03-31T19:00:00Z"

    def test_rotation_chain_latest_message_all_none(self):
        chain = RotationChain(dispatch_id="d-1", sessions=[
            ConversationSession(
                session_id="s1", project_path="/p", cwd="", last_message=None,
                title="", message_count=0, user_message_count=0, total_tokens=0, file_path="",
            ),
        ])
        assert chain.latest_message is None
