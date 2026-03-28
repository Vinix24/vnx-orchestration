#!/usr/bin/env python3
"""
CI tests for VNX Conversation Analyzer.
All tests use in-memory SQLite and synthetic data — no JSONL files or LLM needed.
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Setup path so we can import the analyzer
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

# Mock vnx_paths before importing conversation_analyzer
_mock_state_dir = tempfile.mkdtemp()
_mock_vnx_home = tempfile.mkdtemp()

_mock_project_root = tempfile.mkdtemp()

with patch.dict(os.environ, {
    "VNX_HOME": _mock_vnx_home,
    "VNX_STATE_DIR": _mock_state_dir,
    "PROJECT_ROOT": _mock_project_root,
}):
    from conversation_analyzer import (
        SessionParser, SessionMetrics, SessionFlags,
        HeuristicDetector, DeepAnalyzer, DigestGenerator,
        ConversationAnalyzer, RunStats, normalize_model,
    )
    from generate_t0_session_brief import (
        generate_brief, get_model_performance, get_model_routing_hints,
        get_active_concerns,
    )
    from generate_suggested_edits import (
        generate_memory_suggestions, generate_digest_section,
        _content_hash, _is_already_suggested_or_applied,
    )
    from apply_suggested_edits import (
        cmd_accept, cmd_reject, _parse_ids, _apply_memory_edit,
        _resolve_target_path, _load_pending, _save_pending,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_schema(conn: sqlite3.Connection):
    """Create session_analytics and related tables in memory."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL,
            terminal TEXT,
            session_date DATE NOT NULL,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            tool_calls_total INTEGER DEFAULT 0,
            tool_read_count INTEGER DEFAULT 0,
            tool_edit_count INTEGER DEFAULT 0,
            tool_bash_count INTEGER DEFAULT 0,
            tool_grep_count INTEGER DEFAULT 0,
            tool_write_count INTEGER DEFAULT 0,
            tool_task_count INTEGER DEFAULT 0,
            tool_other_count INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            user_message_count INTEGER DEFAULT 0,
            assistant_message_count INTEGER DEFAULT 0,
            duration_minutes REAL,
            has_error_recovery BOOLEAN DEFAULT FALSE,
            has_context_reset BOOLEAN DEFAULT FALSE,
            context_reset_count INTEGER DEFAULT 0,
            has_large_refactor BOOLEAN DEFAULT FALSE,
            has_test_cycle BOOLEAN DEFAULT FALSE,
            primary_activity TEXT,
            deep_analysis_json TEXT,
            deep_analysis_model TEXT,
            deep_analysis_at DATETIME,
            session_model TEXT DEFAULT 'unknown',
            file_size_bytes INTEGER,
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzer_version TEXT DEFAULT '1.0.0',
            dispatch_id TEXT
        );
        CREATE TABLE IF NOT EXISTS improvement_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            component TEXT,
            current_behavior TEXT NOT NULL,
            suggested_improvement TEXT NOT NULL,
            evidence TEXT,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'new',
            digest_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            acted_on_at DATETIME
        );
        CREATE TABLE IF NOT EXISTS nightly_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date DATE NOT NULL UNIQUE,
            sessions_analyzed INTEGER DEFAULT 0,
            deep_analyzed INTEGER DEFAULT 0,
            new_suggestions INTEGER DEFAULT 0,
            total_tokens_used INTEGER DEFAULT 0,
            digest_markdown TEXT NOT NULL,
            digest_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)


def _make_assistant_msg(tool_name: str = None,
                        input_tokens: int = 100,
                        output_tokens: int = 50,
                        cache_create: int = 0,
                        cache_read: int = 0,
                        timestamp: str = "2026-03-02T10:00:00Z",
                        model: str = "") -> dict:
    """Build a synthetic assistant message record."""
    content = []
    if tool_name:
        content.append({"type": "tool_use", "name": tool_name, "input": {}})
    else:
        content.append({"type": "text", "text": "Hello"})

    msg = {
        "role": "assistant",
        "content": content,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
        }
    }
    if model:
        msg["model"] = model

    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": msg,
    }


def _make_user_msg(text: str = "Fix the bug",
                   timestamp: str = "2026-03-02T10:01:00Z") -> dict:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": text}
    }


def _make_system_msg(subtype: str = "info",
                     timestamp: str = "2026-03-02T10:00:30Z") -> dict:
    return {
        "type": "system",
        "timestamp": timestamp,
        "subtype": subtype,
        "data": ""
    }


def _make_bash_tool_msg(command: str = "ls",
                        timestamp: str = "2026-03-02T10:02:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ],
            "usage": {"input_tokens": 50, "output_tokens": 20,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}
        }
    }


# ---------------------------------------------------------------------------
# Phase 1: Parsing tests
# ---------------------------------------------------------------------------

class TestSessionParser:

    def test_parse_assistant_message(self):
        """Token usage is correctly extracted from assistant messages."""
        parser = SessionParser()
        msg = _make_assistant_msg(
            input_tokens=500, output_tokens=200,
            cache_create=100, cache_read=1000)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False) as f:
            f.write(json.dumps(msg) + "\n")
            f.flush()
            metrics, _ = parser.parse_file(Path(f.name))

        assert metrics.total_input_tokens == 500
        assert metrics.total_output_tokens == 200
        assert metrics.cache_creation_tokens == 100
        assert metrics.cache_read_tokens == 1000
        os.unlink(f.name)

    def test_parse_tool_use_blocks(self):
        """Tool calls are counted per tool name."""
        parser = SessionParser()
        messages = [
            _make_assistant_msg(tool_name="Read"),
            _make_assistant_msg(tool_name="Read"),
            _make_assistant_msg(tool_name="Edit"),
            _make_assistant_msg(tool_name="Bash"),
            _make_assistant_msg(tool_name="Grep"),
            _make_assistant_msg(tool_name="Write"),
            _make_assistant_msg(tool_name="Task"),
            _make_assistant_msg(tool_name="WebFetch"),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False) as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")
            f.flush()
            metrics, _ = parser.parse_file(Path(f.name))

        assert metrics.tool_read_count == 2
        assert metrics.tool_edit_count == 1
        assert metrics.tool_bash_count == 1
        assert metrics.tool_grep_count == 1
        assert metrics.tool_write_count == 1
        assert metrics.tool_task_count == 1
        assert metrics.tool_other_count == 1
        assert metrics.tool_calls_total == 8
        os.unlink(f.name)

    def test_session_id_from_filename(self):
        """Session ID is extracted from JSONL filename (UUID stem)."""
        parser = SessionParser()
        path = Path("/some/dir/39743588-1c97-4059-b776-2bb1ce918a70.jsonl")
        assert parser.session_id_from_path(path) == "39743588-1c97-4059-b776-2bb1ce918a70"

    def test_terminal_detection(self):
        """Terminal is detected from project directory name."""
        parser = SessionParser()
        assert parser.detect_terminal(
            "-Users-user-Dev-project--claude-terminals-T-MANAGER") == "T-MANAGER"
        assert parser.detect_terminal(
            "-Users-user-Dev-project--claude-terminals-T1") == "T1"
        assert parser.detect_terminal(
            "-Users-user-Dev-project--claude-terminals-T2") == "T2"
        assert parser.detect_terminal(
            "-Users-user-Dev-project") == "unknown"


# ---------------------------------------------------------------------------
# Phase 2: Heuristic tests
# ---------------------------------------------------------------------------

class TestHeuristicDetector:

    def test_heuristic_error_recovery(self):
        """Detects error recovery when >=2 error indicators appear."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(tool_calls_total=10, tool_edit_count=5)

        # Messages with error indicators in user tool results
        messages = [
            {"type": "user", "message": {"content": "Error: module not found"}},
            _make_assistant_msg(tool_name="Edit"),
            {"type": "user", "message": {"content": "Traceback: import failed"}},
            _make_assistant_msg(tool_name="Edit"),
        ]

        flags = detector.detect_patterns(metrics, messages)
        assert flags.has_error_recovery is True

    def test_heuristic_primary_activity_research(self):
        """Classifies as research when Read+Grep dominate."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(
            tool_calls_total=20,
            tool_read_count=8,
            tool_grep_count=5,
            tool_edit_count=2,
            tool_bash_count=3,
            tool_write_count=1,
            tool_task_count=1,
        )
        flags = detector.detect_patterns(metrics, [])
        assert flags.primary_activity == "research"

    def test_heuristic_primary_activity_coding(self):
        """Classifies as coding when Edit+Write dominate."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(
            tool_calls_total=20,
            tool_read_count=3,
            tool_grep_count=1,
            tool_edit_count=7,
            tool_bash_count=2,
            tool_write_count=5,
            tool_task_count=2,
        )
        flags = detector.detect_patterns(metrics, [])
        assert flags.primary_activity == "coding"

    def test_heuristic_test_cycle(self):
        """Detects test cycle: Bash(test)→Edit→Bash(test) repeated >=2x."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(tool_calls_total=10, tool_bash_count=4,
                                  tool_edit_count=3)

        messages = [
            _make_bash_tool_msg("python3 -m pytest tests/ -q"),
            _make_assistant_msg(tool_name="Edit"),
            _make_bash_tool_msg("python3 -m pytest tests/ -q"),
            _make_assistant_msg(tool_name="Edit"),
            _make_bash_tool_msg("python3 -m pytest tests/ -q"),
        ]

        flags = detector.detect_patterns(metrics, messages)
        assert flags.has_test_cycle is True

    def test_heuristic_large_refactor(self):
        """Flags large refactor when >10 Edit calls."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(tool_calls_total=15, tool_edit_count=12)
        flags = detector.detect_patterns(metrics, [])
        assert flags.has_large_refactor is True

    def test_heuristic_context_reset(self):
        """Detects context reset from system compaction message."""
        detector = HeuristicDetector()
        metrics = SessionMetrics(tool_calls_total=5)
        messages = [
            _make_system_msg(subtype="compaction"),
        ]
        flags = detector.detect_patterns(metrics, messages)
        assert flags.has_context_reset is True


# ---------------------------------------------------------------------------
# Phase 3: Deep analysis criteria
# ---------------------------------------------------------------------------

class TestDeepAnalyzer:

    def test_deep_analysis_criteria_error_recovery(self):
        """Triggers deep analysis on error recovery sessions."""
        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(total_output_tokens=5000, tool_calls_total=20)
        flags = SessionFlags(has_error_recovery=True)
        assert analyzer.should_deep_analyze(metrics, flags) is True

    def test_deep_analysis_criteria_large_session(self):
        """Triggers deep analysis on sessions with >100K output tokens."""
        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(total_output_tokens=150_000, tool_calls_total=20)
        flags = SessionFlags()
        assert analyzer.should_deep_analyze(metrics, flags) is True

    def test_deep_analysis_criteria_normal_skip(self):
        """Skips deep analysis for normal small sessions."""
        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(total_output_tokens=5000, tool_calls_total=20)
        flags = SessionFlags()
        assert analyzer.should_deep_analyze(metrics, flags) is False


# ---------------------------------------------------------------------------
# Phase 4: Storage & idempotency
# ---------------------------------------------------------------------------

class TestStorage:

    def test_store_session_analytics(self):
        """Session metrics are stored and queryable."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.conn = conn

        metrics = SessionMetrics(
            session_id="test-session-001",
            project_path="/Users/test/project",
            terminal="T1",
            session_date="2026-03-02",
            total_input_tokens=5000,
            total_output_tokens=2000,
            tool_calls_total=15,
            tool_read_count=5,
            tool_edit_count=3,
            file_size_bytes=1024,
        )
        flags = SessionFlags(primary_activity="coding")

        analyzer._store_session(metrics, flags, None)

        cur = conn.cursor()
        cur.execute("SELECT * FROM session_analytics WHERE session_id = 'test-session-001'")
        row = cur.fetchone()

        assert row is not None
        assert row["terminal"] == "T1"
        assert row["total_input_tokens"] == 5000
        assert row["primary_activity"] == "coding"
        conn.close()

    def test_idempotent_skip(self):
        """Already analyzed sessions are skipped in find_unanalyzed_sessions."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        # Insert a known session
        conn.execute(
            "INSERT INTO session_analytics (session_id, project_path, session_date) "
            "VALUES ('known-id', '/test', '2026-03-02')")
        conn.commit()

        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.conn = conn
        analyzer.parser = SessionParser()

        # Mock CLAUDE_PROJECTS_DIR to a temp dir with one known and one new JSONL
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-Users-test-project"
            project_dir.mkdir()
            (project_dir / "known-id.jsonl").write_text('{"type":"user"}\n')
            (project_dir / "new-id.jsonl").write_text('{"type":"user"}\n')

            with patch("conversation_analyzer.CLAUDE_PROJECTS_DIR", Path(tmpdir)):
                sessions = analyzer.find_unanalyzed_sessions()

        assert len(sessions) == 1
        assert sessions[0].stem == "new-id"
        conn.close()

    def test_store_improvement_suggestion(self):
        """Suggestions are stored with correct category and priority."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.conn = conn

        suggestions = [{
            "session_id": "sess-001",
            "category": "prompt",
            "component": "dispatcher_v8",
            "current_behavior": "Missing schema context",
            "suggested_improvement": "Add @reference",
            "evidence": "4x schema Read calls after dispatch",
            "priority": "high",
        }]

        analyzer._store_suggestions(suggestions, "digest_2026-03-02")

        cur = conn.cursor()
        cur.execute("SELECT * FROM improvement_suggestions WHERE category = 'prompt'")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["priority"] == "high"
        assert rows[0]["component"] == "dispatcher_v8"
        conn.close()


# ---------------------------------------------------------------------------
# Digest tests
# ---------------------------------------------------------------------------

class TestDigestGenerator:

    def test_digest_markdown_structure(self):
        """Digest contains expected sections."""
        gen = DigestGenerator()
        stats = RunStats(
            sessions_analyzed=10, sessions_deep=2,
            total_tokens=50000,
            suggestions=[{
                "priority": "high", "category": "hook",
                "component": "pre-commit",
                "current_behavior": "Blocks too often",
                "suggested_improvement": "Add --fix",
                "evidence": "12 sessions",
            }]
        )
        session_rows = [
            {"terminal": "T1", "total_input_tokens": 1000,
             "total_output_tokens": 500, "cache_read_tokens": 800,
             "cache_creation_tokens": 200},
        ]

        # Use a temp DB with schema for trends query
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        conn.close()

        md = gen.generate("2026-03-02", stats, session_rows, db_path)

        assert "# VNX Nightly Digest" in md
        assert "## Samenvatting" in md
        assert "10" in md  # sessions_analyzed
        assert "## Token Overzicht" in md
        assert "T1" in md
        assert "## Verbeter Suggesties" in md
        assert "[HIGH]" in md
        os.unlink(db_path)

    def test_digest_includes_suggestions(self):
        """Suggestions appear with priority, category, and evidence."""
        gen = DigestGenerator()
        stats = RunStats(
            sessions_analyzed=5, sessions_deep=1,
            total_tokens=10000,
            suggestions=[
                {"priority": "critical", "category": "architecture",
                 "component": "database", "current_behavior": "N+1 queries",
                 "suggested_improvement": "Batch queries",
                 "evidence": "50 sequential SELECT calls"},
                {"priority": "low", "category": "workflow",
                 "component": "T1", "current_behavior": "Manual steps",
                 "suggested_improvement": "Automate",
                 "evidence": "repeated pattern"},
            ]
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        conn.close()

        md = gen.generate("2026-03-02", stats, [], db_path)

        # Critical should come before Low
        crit_pos = md.index("[CRITICAL]")
        low_pos = md.index("[LOW]")
        assert crit_pos < low_pos
        assert "N+1 queries" in md
        assert "Batch queries" in md
        os.unlink(db_path)

    def test_digest_stored_in_db(self):
        """Digest markdown is stored in nightly_digests table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.conn = conn

        stats = RunStats(sessions_analyzed=5, sessions_deep=1, total_tokens=10000)
        md = "# Test Digest"

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            digest_path = Path(f.name)

        analyzer._store_digest("2026-03-02", stats, md, digest_path)

        cur = conn.cursor()
        cur.execute("SELECT * FROM nightly_digests WHERE digest_date = '2026-03-02'")
        row = cur.fetchone()
        assert row is not None
        assert row["sessions_analyzed"] == 5
        assert row["digest_markdown"] == "# Test Digest"
        conn.close()
        os.unlink(digest_path)


# ---------------------------------------------------------------------------
# Model normalization tests
# ---------------------------------------------------------------------------

class TestNormalizeModel:

    def test_opus_model(self):
        assert normalize_model("claude-opus-4-1-20250805") == "claude-opus"

    def test_sonnet_model(self):
        assert normalize_model("claude-sonnet-4-5-20250514") == "claude-sonnet"

    def test_haiku_model(self):
        assert normalize_model("claude-haiku-4-5-20251001") == "claude-haiku"

    def test_codex_model(self):
        assert normalize_model("codex-mini-latest") == "codex"

    def test_gemini_model(self):
        assert normalize_model("gemini-2.0-flash") == "gemini"

    def test_unknown_model(self):
        assert normalize_model("some-random-model") == "unknown"

    def test_empty_model(self):
        assert normalize_model("") == "unknown"

    def test_case_insensitive(self):
        assert normalize_model("Claude-Opus-4-1") == "claude-opus"


class TestModelExtraction:

    def test_model_extracted_from_first_assistant(self):
        """Model is extracted from the first assistant message."""
        parser = SessionParser()
        messages = [
            _make_assistant_msg(model="claude-opus-4-1-20250805", output_tokens=100),
            _make_assistant_msg(model="claude-sonnet-4-5-20250514", output_tokens=50),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False) as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")
            f.flush()
            metrics, _ = parser.parse_file(Path(f.name))

        assert metrics.session_model == "claude-opus"
        os.unlink(f.name)

    def test_model_empty_when_not_in_jsonl(self):
        """Session model is empty string when no model field present."""
        parser = SessionParser()
        messages = [
            _make_assistant_msg(output_tokens=100),  # No model
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False) as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")
            f.flush()
            metrics, _ = parser.parse_file(Path(f.name))

        assert metrics.session_model == ""
        os.unlink(f.name)

    def test_store_session_with_model(self):
        """Session model is stored in database."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.conn = conn

        metrics = SessionMetrics(
            session_id="model-test-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-03",
            session_model="claude-opus",
        )
        flags = SessionFlags(primary_activity="coding")
        analyzer._store_session(metrics, flags, None)

        cur = conn.cursor()
        cur.execute("SELECT session_model FROM session_analytics WHERE session_id = 'model-test-001'")
        row = cur.fetchone()
        assert row["session_model"] == "claude-opus"
        conn.close()


# ---------------------------------------------------------------------------
# T0 Session Brief tests
# ---------------------------------------------------------------------------

class TestSessionBrief:

    def _setup_db_with_sessions(self) -> sqlite3.Connection:
        """Create in-memory DB with test session data."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        today = datetime.now().strftime("%Y-%m-%d")
        sessions = [
            ("s1", "/test", "T1", today, 5000, 2000, 100, 900, 10, "coding", 0, "claude-opus", 25.0),
            ("s2", "/test", "T1", today, 6000, 3000, 200, 1800, 15, "refactoring", 0, "claude-opus", 30.0),
            ("s3", "/test", "T2", today, 3000, 1500, 50, 500, 8, "research", 1, "claude-sonnet", 15.0),
            ("s4", "/test", "T1", today, 4000, 1800, 80, 700, 12, "coding", 0, "claude-opus", 20.0),
            ("s5", "/test", "T2", today, 2500, 1000, 40, 400, 6, "research", 0, "claude-sonnet", 10.0),
            ("s6", "/test", "T1", today, 7000, 4000, 150, 1200, 20, "coding", 1, "claude-opus", 35.0),
        ]

        for s in sessions:
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_path, terminal, session_date,
                    total_input_tokens, total_output_tokens,
                    cache_creation_tokens, cache_read_tokens,
                    tool_calls_total, primary_activity,
                    has_error_recovery, session_model, duration_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, s)
        conn.commit()
        return conn

    def test_model_performance_aggregation(self):
        """Model performance is correctly aggregated."""
        conn = self._setup_db_with_sessions()
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        perf = get_model_performance(conn, since)

        assert "claude-opus" in perf
        assert "claude-sonnet" in perf
        assert perf["claude-opus"]["sessions_7d"] == 4
        assert perf["claude-sonnet"]["sessions_7d"] == 2
        conn.close()

    def test_active_concerns_high_error_rate(self):
        """Concerns are raised for models with >30% error recovery."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        today = datetime.now().strftime("%Y-%m-%d")
        # 3 sessions for sonnet-storage, 2 with errors = 67% error rate
        for i, has_err in enumerate([1, 1, 0]):
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_path, terminal, session_date,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, '/test', 'T2', ?, 'coding', ?, 'claude-sonnet')
            """, (f"err-test-{i}", today, has_err))
        conn.commit()

        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        concerns = get_active_concerns(conn, since)
        assert len(concerns) >= 1
        assert concerns[0]["model"] == "claude-sonnet"
        conn.close()


# ---------------------------------------------------------------------------
# Suggested Edits tests
# ---------------------------------------------------------------------------

class TestSuggestedEdits:

    def test_content_hash_deterministic(self):
        """Same input produces same hash."""
        h1 = _content_hash("memory", "MEMORY.md", "test content")
        h2 = _content_hash("memory", "MEMORY.md", "test content")
        assert h1 == h2

    def test_content_hash_different(self):
        """Different input produces different hash."""
        h1 = _content_hash("memory", "MEMORY.md", "content A")
        h2 = _content_hash("memory", "MEMORY.md", "content B")
        assert h1 != h2

    def test_duplicate_detection(self):
        """Existing pending edits are detected as duplicates."""
        fp = _content_hash("memory", "MEMORY.md", "test")
        existing = [{"_fingerprint": fp, "status": "pending"}]
        assert _is_already_suggested_or_applied(fp, existing, []) is True

    def test_non_duplicate(self):
        """New fingerprints are not flagged as duplicates."""
        fp = _content_hash("memory", "MEMORY.md", "new content")
        existing = [{"_fingerprint": "other-fp", "status": "pending"}]
        assert _is_already_suggested_or_applied(fp, existing, []) is False

    def test_digest_section_generation(self):
        """Digest section is generated for pending edits."""
        edits = [
            {"id": 1, "category": "memory", "target": "MEMORY.md",
             "action": "append", "content": "Test content",
             "confidence": 0.85, "evidence": "10 sessions", "status": "pending"},
        ]
        section = generate_digest_section(edits)
        assert "Voorgestelde Wijzigingen" in section
        assert "#1" in section
        assert "MEMORY" in section

    def test_digest_section_empty_when_no_pending(self):
        """No digest section when all edits are applied."""
        edits = [
            {"id": 1, "category": "memory", "status": "applied"},
        ]
        section = generate_digest_section(edits)
        assert section == ""

    def test_memory_suggestions_from_db(self):
        """Memory suggestions are generated from model performance data."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)

        today = datetime.now().strftime("%Y-%m-%d")
        # 6 opus coding sessions (5 success, 1 error)
        for i in range(6):
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_path, terminal, session_date,
                    total_output_tokens, cache_read_tokens, cache_creation_tokens,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, '/test', 'T1', ?, 50000, 900, 100, 'coding', ?, 'claude-opus')
            """, (f"opus-{i}", today, 1 if i == 0 else 0))

        # 5 sonnet coding sessions (2 success, 3 error)
        for i in range(5):
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_path, terminal, session_date,
                    total_output_tokens, cache_read_tokens, cache_creation_tokens,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, '/test', 'T2', ?, 30000, 400, 100, 'coding', ?, 'claude-sonnet')
            """, (f"sonnet-{i}", today, 0 if i < 2 else 1))
        conn.commit()

        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        suggestions = generate_memory_suggestions(conn, since)

        # Should find at least one suggestion comparing opus vs sonnet for coding
        assert len(suggestions) >= 1
        conn.close()


# ---------------------------------------------------------------------------
# Apply Suggested Edits tests
# ---------------------------------------------------------------------------

class TestApplySuggestedEdits:

    def test_parse_ids(self):
        """Comma-separated IDs are parsed correctly."""
        assert _parse_ids("1,3,5") == [1, 3, 5]
        assert _parse_ids("1") == [1]
        assert _parse_ids("") == []
        assert _parse_ids("a,b") == []

    def test_accept_edits(self):
        """Accepted edits have status changed to accepted."""
        data = {
            "generated_at": "2026-03-03T00:00:00Z",
            "edits": [
                {"id": 1, "status": "pending", "category": "memory"},
                {"id": 2, "status": "pending", "category": "rule"},
            ]
        }
        with patch("apply_suggested_edits.PENDING_PATH", Path(_mock_state_dir) / "test_pending.json"):
            with patch("apply_suggested_edits.STATE_DIR", Path(_mock_state_dir)):
                from apply_suggested_edits import PENDING_PATH as PP
                PP.write_text(json.dumps(data), encoding="utf-8")
                cmd_accept("1")
                result = json.loads(PP.read_text(encoding="utf-8"))
                assert result["edits"][0]["status"] == "accepted"
                assert result["edits"][1]["status"] == "pending"

    def test_reject_edits_with_reason(self):
        """Rejected edits store reason."""
        data = {
            "generated_at": "2026-03-03T00:00:00Z",
            "edits": [
                {"id": 1, "status": "pending", "category": "memory"},
            ]
        }
        with patch("apply_suggested_edits.PENDING_PATH", Path(_mock_state_dir) / "test_pending2.json"):
            with patch("apply_suggested_edits.STATE_DIR", Path(_mock_state_dir)):
                from apply_suggested_edits import PENDING_PATH as PP
                PP.write_text(json.dumps(data), encoding="utf-8")
                cmd_reject("1", reason="te agressief")
                result = json.loads(PP.read_text(encoding="utf-8"))
                assert result["edits"][0]["status"] == "rejected"
                assert result["edits"][0]["reject_reason"] == "te agressief"

    def test_apply_memory_edit(self):
        """Memory edit appends content to MEMORY.md section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "MEMORY.md"
            memory_file.write_text(
                "# Memory\n\n## Geleerde Patronen\n\n- existing pattern\n",
                encoding="utf-8"
            )

            edit = {
                "target": str(memory_file),
                "section": "## Geleerde Patronen",
                "action": "append",
                "content": "- new pattern from analysis",
            }
            result = _apply_memory_edit(edit)
            assert result is True

            text = memory_file.read_text(encoding="utf-8")
            assert "- new pattern from analysis" in text
            assert "- existing pattern" in text

    def test_apply_memory_edit_creates_section(self):
        """Memory edit creates section when it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "MEMORY.md"
            memory_file.write_text("# Memory\n\nSome content\n", encoding="utf-8")

            edit = {
                "target": str(memory_file),
                "section": "## Geleerde Patronen",
                "action": "append",
                "content": "- first pattern",
            }
            result = _apply_memory_edit(edit)
            assert result is True

            text = memory_file.read_text(encoding="utf-8")
            assert "## Geleerde Patronen" in text
            assert "- first pattern" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
