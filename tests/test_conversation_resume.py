#!/usr/bin/env python3
"""Tests for VNX Conversation Resume (PR-3).

Validates:
  - Resume from matching worktree succeeds (RESUME-1, RESUME-4)
  - Cross-worktree resume is blocked deterministically (RESUME-3)
  - Missing worktree produces actionable error (RESUME-2)
  - Session without cwd produces clear error
  - Force flag overrides cross-worktree check
  - Command uses `claude --resume <session_id>` (RESUME-4)
  - API handler validates input and returns correct HTTP status
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

from conversation_read_model import (
    ConversationReadModel,
    ConversationSession,
)
from conversation_resume import (
    ResumeError,
    ResumeResult,
    resume_conversation,
    validate_resume_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_index_db(db_path: str, rows: list[dict]) -> None:
    """Create a conversation-index.db with test data."""
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


def _make_session(
    session_id: str = "sess-001",
    cwd: str = "/Users/op/Dev/project-wt/.claude/terminals/T1",
    worktree_root: str = "/Users/op/Dev/project-wt",
    **kwargs,
) -> ConversationSession:
    """Create a ConversationSession for testing."""
    return ConversationSession(
        session_id=session_id,
        project_path=kwargs.get("project_path", "/Users/op/Dev/project-wt"),
        cwd=cwd,
        last_message=kwargs.get("last_message", "2026-03-31T19:00:00Z"),
        title=kwargs.get("title", "Test session"),
        message_count=kwargs.get("message_count", 10),
        user_message_count=kwargs.get("user_message_count", 5),
        total_tokens=kwargs.get("total_tokens", 1000),
        file_path=kwargs.get("file_path", ""),
        terminal=kwargs.get("terminal", "T1"),
        worktree_root=worktree_root,
    )


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def worktree_roots(tmp_dir):
    """Create real worktree directories on disk for path existence checks."""
    wt1 = os.path.join(tmp_dir, "project-wt")
    wt2 = os.path.join(tmp_dir, "project-wt-feat")
    wt1_terminal = os.path.join(wt1, ".claude", "terminals", "T1")
    wt2_terminal = os.path.join(wt2, ".claude", "terminals", "T1")
    os.makedirs(wt1_terminal, exist_ok=True)
    os.makedirs(wt2_terminal, exist_ok=True)
    return [wt1, wt2]


# ---------------------------------------------------------------------------
# Unit tests: validate_resume_context
# ---------------------------------------------------------------------------

class TestValidateResumeContext:
    """Direct tests for the core validation function."""

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_resume_success_same_worktree(self, _mock_cli, worktree_roots):
        """RESUME-1/4: Resume succeeds when operator is in the same worktree."""
        session = _make_session(
            cwd=os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            worktree_root=worktree_roots[0],
        )
        result = validate_resume_context(session, worktree_roots[0], worktree_roots)

        assert result.ok is True
        assert result.command == f"claude --resume {session.session_id}"
        assert result.cwd == session.cwd
        assert result.worktree_root == worktree_roots[0]
        assert result.error is None

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_cross_worktree_blocked(self, _mock_cli, worktree_roots):
        """RESUME-3: Cross-worktree resume is deterministically blocked."""
        session = _make_session(
            cwd=os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            worktree_root=worktree_roots[0],
        )
        # Operator is in worktree_roots[1], session is in worktree_roots[0]
        result = validate_resume_context(session, worktree_roots[1], worktree_roots)

        assert result.ok is False
        assert result.error == ResumeError.WORKTREE_MISMATCH
        assert "Cross-worktree resume blocked" in result.message
        assert worktree_roots[1] in result.message
        assert worktree_roots[0] in result.message

    def test_session_no_cwd(self, worktree_roots):
        """Session without cwd cannot be resumed."""
        session = _make_session(cwd="", worktree_root=None)
        result = validate_resume_context(session, worktree_roots[0], worktree_roots)

        assert result.ok is False
        assert result.error == ResumeError.SESSION_NO_CWD
        assert "no working directory" in result.message

    def test_worktree_missing_on_disk(self, tmp_dir):
        """RESUME-2: Stale worktree returns actionable error."""
        gone_root = os.path.join(tmp_dir, "gone-worktree")
        session = _make_session(
            cwd=os.path.join(gone_root, ".claude", "terminals", "T1"),
            worktree_root=gone_root,
        )
        result = validate_resume_context(session, None, [gone_root])

        assert result.ok is False
        assert result.error == ResumeError.WORKTREE_MISSING
        assert "no longer exists" in result.message

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_terminal_dir_gone_falls_back_to_worktree(self, _mock_cli, tmp_dir):
        """When terminal dir is deleted but worktree root exists, use root as cwd."""
        wt_root = os.path.join(tmp_dir, "project-wt")
        os.makedirs(wt_root, exist_ok=True)
        # Terminal dir does NOT exist
        terminal_cwd = os.path.join(wt_root, ".claude", "terminals", "T1")
        session = _make_session(cwd=terminal_cwd, worktree_root=wt_root)

        result = validate_resume_context(session, wt_root, [wt_root])

        assert result.ok is True
        assert result.cwd == wt_root  # Falls back to worktree root

    @patch("conversation_resume._find_claude_cli", return_value=None)
    def test_claude_cli_not_found(self, _mock_cli, worktree_roots):
        """Missing CLI binary returns clear error."""
        session = _make_session(
            cwd=os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            worktree_root=worktree_roots[0],
        )
        result = validate_resume_context(session, worktree_roots[0], worktree_roots)

        assert result.ok is False
        assert result.error == ResumeError.CLAUDE_CLI_NOT_FOUND

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_operator_outside_worktree_can_resume(self, _mock_cli, worktree_roots):
        """Operator not in any worktree (None) can resume any session."""
        session = _make_session(
            cwd=os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            worktree_root=worktree_roots[0],
        )
        result = validate_resume_context(session, None, worktree_roots)

        assert result.ok is True

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_session_outside_worktree_can_resume(self, _mock_cli, tmp_dir):
        """Session without a worktree root can be resumed from anywhere."""
        standalone_dir = os.path.join(tmp_dir, "standalone")
        os.makedirs(standalone_dir, exist_ok=True)
        session = _make_session(cwd=standalone_dir, worktree_root=None)

        result = validate_resume_context(session, None, [])

        assert result.ok is True
        assert result.cwd == standalone_dir


# ---------------------------------------------------------------------------
# Integration tests: resume_conversation (full pipeline)
# ---------------------------------------------------------------------------

class TestResumeConversation:
    """End-to-end tests through the read model."""

    @pytest.fixture
    def model_setup(self, tmp_dir, worktree_roots):
        """Create a read model with test sessions."""
        db_path = os.path.join(tmp_dir, "conversation-index.db")
        _create_index_db(db_path, [
            {
                "session_id": "sess-wt1-001",
                "project_path": worktree_roots[0],
                "title": "Feature work",
                "last_message": "2026-03-31T19:00:00Z",
                "cwd": os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            },
            {
                "session_id": "sess-wt2-002",
                "project_path": worktree_roots[1],
                "title": "Bug fix",
                "last_message": "2026-03-31T18:00:00Z",
                "cwd": os.path.join(worktree_roots[1], ".claude", "terminals", "T1"),
            },
            {
                "session_id": "sess-no-cwd-003",
                "project_path": worktree_roots[0],
                "title": "Broken session",
                "last_message": "2026-03-31T17:00:00Z",
                "cwd": "",
            },
        ])
        model = ConversationReadModel(
            claude_index_db=db_path,
            worktree_roots=worktree_roots,
        )
        return model, worktree_roots

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_resume_existing_session(self, _mock_cli, model_setup):
        """Full pipeline: look up session, validate, return command."""
        model, roots = model_setup
        result = resume_conversation(
            session_id="sess-wt1-001",
            model=model,
            operator_cwd=os.path.join(roots[0], ".claude", "terminals", "T0"),
            worktree_roots=roots,
        )
        assert result.ok is True
        assert "claude --resume sess-wt1-001" in result.command

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_cross_worktree_blocked_e2e(self, _mock_cli, model_setup):
        """Full pipeline: cross-worktree resume is blocked."""
        model, roots = model_setup
        result = resume_conversation(
            session_id="sess-wt1-001",
            model=model,
            operator_cwd=os.path.join(roots[1], ".claude", "terminals", "T0"),
            worktree_roots=roots,
        )
        assert result.ok is False
        assert result.error == ResumeError.WORKTREE_MISMATCH

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_force_overrides_cross_worktree(self, _mock_cli, model_setup):
        """Force flag allows cross-worktree resume."""
        model, roots = model_setup
        result = resume_conversation(
            session_id="sess-wt1-001",
            model=model,
            operator_cwd=os.path.join(roots[1], ".claude", "terminals", "T0"),
            worktree_roots=roots,
            force=True,
        )
        assert result.ok is True
        assert "claude --resume sess-wt1-001" in result.command

    def test_session_not_found(self, model_setup):
        """Non-existent session ID returns clear error."""
        model, roots = model_setup
        result = resume_conversation(
            session_id="nonexistent-session",
            model=model,
            operator_cwd=roots[0],
            worktree_roots=roots,
        )
        assert result.ok is False
        assert result.error == ResumeError.SESSION_NOT_FOUND
        assert "not found" in result.message

    def test_session_without_cwd(self, model_setup):
        """Session with empty cwd returns actionable error."""
        model, roots = model_setup
        result = resume_conversation(
            session_id="sess-no-cwd-003",
            model=model,
            operator_cwd=roots[0],
            worktree_roots=roots,
        )
        assert result.ok is False
        assert result.error == ResumeError.SESSION_NO_CWD


# ---------------------------------------------------------------------------
# ResumeResult serialization
# ---------------------------------------------------------------------------

class TestResumeResultSerialization:
    def test_success_to_dict(self):
        result = ResumeResult(
            ok=True,
            session_id="sess-001",
            command="claude --resume sess-001",
            cwd="/project",
            worktree_root="/project",
            message="Ready",
        )
        d = result.to_dict()
        assert d["ok"] is True
        assert d["command"] == "claude --resume sess-001"
        assert d["cwd"] == "/project"
        assert "error" not in d

    def test_error_to_dict(self):
        result = ResumeResult(
            ok=False,
            session_id="sess-001",
            error=ResumeError.WORKTREE_MISMATCH,
            message="Cross-worktree blocked",
        )
        d = result.to_dict()
        assert d["ok"] is False
        assert d["error"] == "worktree_mismatch"
        assert "command" not in d

    def test_to_dict_is_json_serializable(self):
        result = ResumeResult(
            ok=True, session_id="s1", command="cmd", cwd="/c",
            worktree_root="/w", message="ok",
        )
        serialized = json.dumps(result.to_dict())
        assert '"ok": true' in serialized


# ---------------------------------------------------------------------------
# API handler tests (unit-level, no HTTP server)
# ---------------------------------------------------------------------------

class TestResumeApiHandler:
    """Test the _resume_conversation handler function directly."""

    @pytest.fixture
    def api_setup(self, tmp_dir, worktree_roots):
        """Patch dashboard module globals for API handler testing."""
        db_path = os.path.join(tmp_dir, "conversation-index.db")
        _create_index_db(db_path, [
            {
                "session_id": "api-sess-001",
                "project_path": worktree_roots[0],
                "title": "API test session",
                "last_message": "2026-03-31T19:00:00Z",
                "cwd": os.path.join(worktree_roots[0], ".claude", "terminals", "T1"),
            },
        ])

        # We need to import the handler — add dashboard to path
        dashboard_dir = str(Path(__file__).resolve().parent.parent / "dashboard")
        if dashboard_dir not in sys.path:
            sys.path.insert(0, dashboard_dir)

        return db_path, worktree_roots

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_api_missing_session_id(self, _mock_cli, api_setup):
        """API returns error for missing session_id."""
        db_path, roots = api_setup

        with patch("serve_dashboard.CLAUDE_INDEX_DB", Path(db_path)), \
             patch("serve_dashboard.PROJECT_ROOT", Path(roots[0])), \
             patch("serve_dashboard.RECEIPTS_PATH", Path("/nonexistent")):
            from serve_dashboard import _resume_conversation
            result = _resume_conversation({})

        assert result["ok"] is False
        assert result["error"] == "missing_session_id"

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_api_successful_resume(self, _mock_cli, api_setup):
        """API returns command for valid session."""
        db_path, roots = api_setup

        with patch("serve_dashboard.CLAUDE_INDEX_DB", Path(db_path)), \
             patch("serve_dashboard.PROJECT_ROOT", Path(roots[0])), \
             patch("serve_dashboard.RECEIPTS_PATH", Path("/nonexistent")):
            from serve_dashboard import _resume_conversation
            result = _resume_conversation({
                "session_id": "api-sess-001",
                "cwd": os.path.join(roots[0], ".claude", "terminals", "T0"),
            })

        assert result["ok"] is True
        assert "claude --resume api-sess-001" in result["command"]

    @patch("conversation_resume._find_claude_cli", return_value="/usr/bin/claude")
    def test_api_cross_worktree_returns_conflict(self, _mock_cli, api_setup):
        """API returns error for cross-worktree attempt."""
        db_path, roots = api_setup

        with patch("serve_dashboard.CLAUDE_INDEX_DB", Path(db_path)), \
             patch("serve_dashboard.PROJECT_ROOT", Path(roots[0])), \
             patch("serve_dashboard.RECEIPTS_PATH", Path("/nonexistent")):
            from serve_dashboard import _resume_conversation
            result = _resume_conversation({
                "session_id": "api-sess-001",
                "cwd": os.path.join(roots[1], ".claude", "terminals", "T0"),
            })

        assert result["ok"] is False
        assert result["error"] == "worktree_mismatch"

    def test_api_missing_index_db(self, tmp_dir, worktree_roots):
        """API handles missing conversation index gracefully."""
        dashboard_dir = str(Path(__file__).resolve().parent.parent / "dashboard")
        if dashboard_dir not in sys.path:
            sys.path.insert(0, dashboard_dir)

        with patch("serve_dashboard.CLAUDE_INDEX_DB", Path(os.path.join(tmp_dir, "nonexistent.db"))), \
             patch("serve_dashboard.PROJECT_ROOT", Path(worktree_roots[0])), \
             patch("serve_dashboard.RECEIPTS_PATH", Path("/nonexistent")):
            from serve_dashboard import _resume_conversation
            result = _resume_conversation({"session_id": "any-id"})

        assert result["ok"] is False
        assert result["error"] == "session_not_found"
