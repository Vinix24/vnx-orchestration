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
from types import SimpleNamespace
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
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
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
            dispatch_id TEXT,
            file_size_bytes INTEGER,
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzer_version TEXT DEFAULT '1.0.0',
            UNIQUE (project_id, session_id)
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
            deep_attempts INTEGER DEFAULT 0,
            deep_failures INTEGER DEFAULT 0,
            deep_config_skips INTEGER DEFAULT 0,
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
# Phase 3a: _try_claude_max outcome classification (OI-1258)
# ---------------------------------------------------------------------------

class TestLLMOutcome:
    """``_try_claude_max`` must return a distinct outcome per failure mode.

    The old ``Optional[str]`` return collapsed four different situations —
    missing CLI, crashed CLI, successful-but-empty, and successful-with-result —
    into a single ``None``. Each test below asserts a different
    ``LLMOutcome.status`` so a caller can tell them apart without parsing logs.
    """

    @staticmethod
    def _run_outcome(side_effect=None, returncode=0, stdout="", stderr=""):
        import conversation_analyzer.deep_analyzer as da_module

        if side_effect is not None:
            patcher = patch.object(da_module.subprocess, "run", side_effect=side_effect)
        else:
            fake = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
            patcher = patch.object(da_module.subprocess, "run", return_value=fake)
        with patcher:
            return DeepAnalyzer._try_claude_max("test prompt")

    def test_missing_cli_returns_missing_cli_outcome(self):
        outcome = self._run_outcome(side_effect=FileNotFoundError())
        assert outcome.status == "missing_cli"
        assert outcome.text is None
        # A missing binary is a real invocation attempt (subprocess.run WAS
        # called) that happened to fail — distinct from config_skip, where no
        # call is ever made.
        assert outcome.attempted is True

    def test_nonzero_rc_returns_cli_failed_with_rc_and_stderr(self):
        outcome = self._run_outcome(returncode=2, stderr="boom: something broke")
        assert outcome.status == "cli_failed"
        assert outcome.returncode == 2
        assert "boom" in outcome.stderr

    def test_success_empty_output_returns_empty_outcome(self):
        outcome = self._run_outcome(returncode=0, stdout="")
        assert outcome.status == "empty"
        assert outcome.text == ""

    def test_success_with_result_returns_ok_outcome(self):
        outcome = self._run_outcome(returncode=0, stdout='{"result": "hello world"}')
        assert outcome.status == "ok"
        assert outcome.text == "hello world"

    def test_raw_stdout_without_json_returns_ok_outcome(self):
        outcome = self._run_outcome(returncode=0, stdout="plain text answer")
        assert outcome.status == "ok"
        assert outcome.text == "plain text answer"

    def test_timeout_returns_timeout_outcome(self):
        import conversation_analyzer.deep_analyzer as da_module
        outcome = self._run_outcome(
            side_effect=da_module.subprocess.TimeoutExpired(cmd=["claude"], timeout=90)
        )
        assert outcome.status == "timeout"

    def test_unexpected_exception_returns_error_outcome(self):
        outcome = self._run_outcome(side_effect=RuntimeError("disk full"))
        assert outcome.status == "error"
        assert "disk full" in outcome.stderr

    def test_config_skip_status_is_not_attempted(self):
        """``config_skip`` is the one status where no invocation ever fired."""
        from conversation_analyzer.deep_analyzer import LLMOutcome
        outcome = LLMOutcome("config_skip")
        assert outcome.attempted is False


class TestDeepAnalyzerCounter:
    """``deep_attempts``/``deep_failures``/``deep_config_skips`` track
    invoked-vs-failed-vs-never-invoked deep analysis (fix1585-r2)."""

    @staticmethod
    def _analyze_with_claude(outcome):
        import conversation_analyzer.deep_analyzer as da_module

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="counter-test", total_output_tokens=5000,
                                  tool_calls_total=20)
        flags = SessionFlags()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(DeepAnalyzer, '_try_claude_max', return_value=outcome), \
                 patch.object(da_module, 'LLM_STRATEGY', 'claude-only'):
                return analyzer, analyzer.analyze_session(jsonl_path, metrics, flags)
        finally:
            os.unlink(jsonl_path)

    def test_failed_attempt_counts_attempt_and_failure(self):
        from conversation_analyzer.deep_analyzer import LLMOutcome
        analyzer, result = self._analyze_with_claude(LLMOutcome("missing_cli"))
        assert result is None
        assert analyzer.deep_attempts == 1
        assert analyzer.deep_failures == 1
        assert analyzer.deep_config_skips == 0

    def test_successful_attempt_counts_attempt_no_failure(self):
        from conversation_analyzer.deep_analyzer import LLMOutcome
        analyzer, result = self._analyze_with_claude(
            LLMOutcome("ok", text='{"suggestions":[]}')
        )
        assert result is not None
        assert analyzer.deep_attempts == 1
        assert analyzer.deep_failures == 0
        assert analyzer.deep_config_skips == 0

    def test_parseable_but_empty_result_counts_failure(self):
        # LLM answered but returned no parseable JSON — an attempt that
        # produced nothing usable, distinct from a hard CLI failure.
        from conversation_analyzer.deep_analyzer import LLMOutcome
        analyzer, result = self._analyze_with_claude(
            LLMOutcome("ok", text="no json here")
        )
        assert result is None
        assert analyzer.deep_attempts == 1
        assert analyzer.deep_failures == 1
        assert analyzer.deep_config_skips == 0

    def test_json_without_suggestions_key_counts_as_failure_not_success(self):
        """A JSON reply that parses but lacks 'suggestions' is a silent-zero
        on the other side of the same bug (advisory point, fix1585-r2): it
        must count as a failed attempt, not slip through as a usable result."""
        from conversation_analyzer.deep_analyzer import LLMOutcome
        analyzer, result = self._analyze_with_claude(
            LLMOutcome("ok", text='{"patterns": ["foo"], "bottlenecks": []}')
        )
        assert result is None
        assert analyzer.deep_attempts == 1
        assert analyzer.deep_failures == 1
        assert analyzer.deep_config_skips == 0

    def test_deepseek_harness_missing_key_is_config_skip_not_attempt(self):
        """Live bug (fix1585-r2): deepseek-harness without DEEPSEEK_API_KEY
        must not count as an attempted-and-failed session. This drives the
        REAL ``_try_deepseek_harness`` production code (not a hand-built
        outcome) so the test fails if the production skip check regresses —
        it feeds the writing side, not a value invented on the checking side.
        """
        import conversation_analyzer.deep_analyzer as da_module

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="deepseek-nokey-test",
                                  total_output_tokens=5000, tool_calls_total=20)
        flags = SessionFlags()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(da_module, 'LLM_STRATEGY', 'deepseek-harness'), \
                 patch.dict(da_module.os.environ, {}, clear=True):
                result = analyzer.analyze_session(jsonl_path, metrics, flags)
        finally:
            os.unlink(jsonl_path)

        assert result is None
        assert analyzer.deep_attempts == 0, (
            "a missing API key never started a subprocess — it must not "
            "count as an attempt"
        )
        assert analyzer.deep_failures == 0, (
            "no attempt was made, so there is nothing to count as a failure"
        )
        assert analyzer.deep_config_skips == 1

    def test_ollama_probe_failure_is_config_skip_not_attempt(self):
        """Live bug (fix1585-r2): ollama-only with a missing/wrong model (or
        an unreachable server) must not count as an attempted-and-failed
        session. Drives the REAL ``_try_ollama`` production code; only the
        network-dependent probe is stubbed (no real Ollama server in CI) —
        the config_skip classification itself is the production code path.
        """
        import conversation_analyzer.deep_analyzer as da_module

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="ollama-noprobe-test",
                                  total_output_tokens=5000, tool_calls_total=20)
        flags = SessionFlags()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(da_module, 'LLM_STRATEGY', 'ollama-only'), \
                 patch.object(DeepAnalyzer, '_probe_ollama', return_value=False):
                result = analyzer.analyze_session(jsonl_path, metrics, flags)
        finally:
            os.unlink(jsonl_path)

        assert result is None
        assert analyzer.deep_attempts == 0, (
            "a failed probe never issued the generate call — it must not "
            "count as an attempt"
        )
        assert analyzer.deep_failures == 0
        assert analyzer.deep_config_skips == 1

    def test_config_skip_run_stays_green_on_fail_closed_exit_code(self):
        """The end-to-end point of fix1585-r2: a run where every flagged
        session hit a config gap must not fail-close the nightly job. Feeds
        fail_closed_exit_code with counters produced by the real
        analyze_session() call above, not hand-crafted RunStats."""
        from conversation_analyzer import fail_closed_exit_code, RunStats
        import conversation_analyzer.deep_analyzer as da_module

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="deepseek-nokey-rc-test",
                                  total_output_tokens=5000, tool_calls_total=20)
        flags = SessionFlags()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(da_module, 'LLM_STRATEGY', 'deepseek-harness'), \
                 patch.dict(da_module.os.environ, {}, clear=True):
                analyzer.analyze_session(jsonl_path, metrics, flags)
        finally:
            os.unlink(jsonl_path)

        stats = RunStats(sessions_analyzed=1, deep_attempts=analyzer.deep_attempts,
                         deep_failures=analyzer.deep_failures,
                         deep_config_skips=analyzer.deep_config_skips)
        assert fail_closed_exit_code(stats) == 0


# ---------------------------------------------------------------------------
# Phase 3b: LLM strategy selection (deepseek-harness)
# ---------------------------------------------------------------------------

class TestDeepAnalyzerStrategy:

    def test_deepseek_harness_strategy_skips_claude_and_ollama(self):
        """LLM_STRATEGY=deepseek-harness calls only the harness path, not claude or ollama."""
        import conversation_analyzer.deep_analyzer as da_module
        from conversation_analyzer.deep_analyzer import LLMOutcome

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="strat-test", total_output_tokens=5000,
                                  tool_calls_total=20)
        flags = SessionFlags()
        mock_result = '{"patterns":[],"bottlenecks":[],"suggestions":[]}'

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(DeepAnalyzer, '_try_deepseek_harness',
                              return_value=LLMOutcome("ok", text=mock_result)) as mock_harness, \
                 patch.object(DeepAnalyzer, '_try_claude_max') as mock_claude, \
                 patch.object(DeepAnalyzer, '_try_ollama') as mock_ollama, \
                 patch.object(da_module, 'LLM_STRATEGY', 'deepseek-harness'):

                result = analyzer.analyze_session(jsonl_path, metrics, flags)

                assert result is not None
                assert result.get("patterns") == []
                mock_harness.assert_called_once()
                mock_claude.assert_not_called()
                mock_ollama.assert_not_called()
                assert analyzer.deep_attempts == 1
                assert analyzer.deep_config_skips == 0
        finally:
            os.unlink(jsonl_path)

    def test_deepseek_harness_fail_closed_without_key(self):
        """_try_deepseek_harness returns a config_skip outcome (no subprocess
        started) when DEEPSEEK_API_KEY is unset."""
        # Consumer-namespace patch: the method reads os.environ at call time
        # inside deep_analyzer.py, so we patch os.environ in that module's
        # namespace to ensure the method sees the empty key.
        import conversation_analyzer.deep_analyzer as da_module

        with patch.dict(da_module.os.environ, {}, clear=True):
            result = DeepAnalyzer._try_deepseek_harness("test prompt")
            assert result.status == "config_skip"
            assert result.attempted is False

    def test_deepseek_harness_model_default(self):
        """VNX_ANALYZER_DEEPSEEK_MODEL defaults to deepseek-v4-flash."""
        from conversation_analyzer import DEEPSEEK_HARNESS_MODEL as DHM
        assert DHM == "deepseek-v4-flash"

    def test_ollama_only_strategy_still_works(self):
        """LLM_STRATEGY=ollama-only (default) still dispatches correctly (no regression)."""
        import conversation_analyzer.deep_analyzer as da_module
        from conversation_analyzer.deep_analyzer import LLMOutcome

        analyzer = DeepAnalyzer()
        metrics = SessionMetrics(session_id="ollama-test", total_output_tokens=5000,
                                  tool_calls_total=20)
        flags = SessionFlags()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"role":"user","content":"test"}}\n')
            f.flush()
            jsonl_path = Path(f.name)

        try:
            with patch.object(DeepAnalyzer, '_build_session_summary',
                              return_value='test summary'), \
                 patch.object(DeepAnalyzer, '_try_ollama',
                              return_value=LLMOutcome("ok", text='{"suggestions":[]}')) as mock_ollama, \
                 patch.object(DeepAnalyzer, '_try_claude_max') as mock_claude, \
                 patch.object(DeepAnalyzer, '_try_deepseek_harness') as mock_harness, \
                 patch.object(da_module, 'LLM_STRATEGY', 'ollama-only'):

                result = analyzer.analyze_session(jsonl_path, metrics, flags)

                assert result is not None
                mock_ollama.assert_called_once()
                mock_claude.assert_not_called()
                mock_harness.assert_not_called()
                assert analyzer.deep_attempts == 1
                assert analyzer.deep_config_skips == 0
        finally:
            os.unlink(jsonl_path)


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
        analyzer.db_path = Path(tempfile.mktemp(suffix=".db"))

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

        with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
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
            "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
            "VALUES ('known-id', 'vnx-dev', '/test', '2026-03-02')")
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

        stats = RunStats(sessions_analyzed=5, sessions_deep=1, deep_attempts=4,
                         deep_failures=3, deep_config_skips=2, total_tokens=10000)
        md = "# Test Digest"

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            digest_path = Path(f.name)

        analyzer._store_digest("2026-03-02", stats, md, digest_path)

        cur = conn.cursor()
        cur.execute("SELECT * FROM nightly_digests WHERE digest_date = '2026-03-02'")
        row = cur.fetchone()
        assert row is not None
        assert row["sessions_analyzed"] == 5
        assert row["deep_analyzed"] == 1
        assert row["deep_attempts"] == 4
        assert row["deep_failures"] == 3
        assert row["deep_config_skips"] == 2
        assert row["digest_markdown"] == "# Test Digest"
        conn.close()
        os.unlink(digest_path)


# ---------------------------------------------------------------------------
# ConversationAnalyzer.run() production wiring (advisory, fix1585-r2)
# ---------------------------------------------------------------------------

class TestRunCopiesDeepCountersIntoStats:
    """runner.py's ``run()`` copies ``self.deep.deep_attempts`` /
    ``deep_failures`` / ``deep_config_skips`` into ``stats`` after the
    session loop (runner.py ~321-323). Nothing else exercises that specific
    copy: ``DeepAnalyzer``'s counters are covered in isolation above, and
    ``RunStats``/``_store_digest`` are covered by constructing ``RunStats``
    directly. Deleting those three lines would leave every other test in
    this file green while the digest, DB row, and fail-closed exit code
    silently went back to reading 0/0/0 forever. This pins the wiring
    itself.
    """

    def test_run_copies_deep_counters_from_analyzer_to_stats(self):
        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.deep = DeepAnalyzer()
        # Simulate attempts/failures/config-skips having accumulated during
        # session processing, without needing a real LLM call.
        analyzer.deep.deep_attempts = 3
        analyzer.deep.deep_failures = 2
        analyzer.deep.deep_config_skips = 5

        def _fake_process_one_session(jsonl_path, dry_run, deep_remaining,
                                      stats, session_rows):
            stats.sessions_analyzed += 1
            return deep_remaining

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_session = Path(tmpdir) / "fake.jsonl"
            with patch.object(ConversationAnalyzer, 'find_unanalyzed_sessions',
                              return_value=[fake_session]), \
                 patch.object(ConversationAnalyzer, '_process_one_session',
                              side_effect=_fake_process_one_session), \
                 patch.object(ConversationAnalyzer, '_finalize_run'):
                stats = analyzer.run(max_sessions=1, deep_budget=1)

        assert stats.deep_attempts == 3
        assert stats.deep_failures == 2
        assert stats.deep_config_skips == 5

    def test_run_with_zero_deep_activity_leaves_stats_at_zero(self):
        """Control case: sessions were processed but none triggered deep
        analysis -> the copy still runs and leaves all three counters at 0
        (not unset/None). Uses the same processed-session path as the
        non-zero case above so the early ``if not sessions: return stats``
        short-circuit in run() (which never reaches the copy at all) isn't
        mistaken for coverage of the wiring."""
        analyzer = ConversationAnalyzer.__new__(ConversationAnalyzer)
        analyzer.deep = DeepAnalyzer()

        def _fake_process_one_session(jsonl_path, dry_run, deep_remaining,
                                      stats, session_rows):
            stats.sessions_analyzed += 1
            return deep_remaining

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_session = Path(tmpdir) / "fake.jsonl"
            with patch.object(ConversationAnalyzer, 'find_unanalyzed_sessions',
                              return_value=[fake_session]), \
                 patch.object(ConversationAnalyzer, '_process_one_session',
                              side_effect=_fake_process_one_session), \
                 patch.object(ConversationAnalyzer, '_finalize_run'):
                stats = analyzer.run(max_sessions=1, deep_budget=1)

        assert stats.deep_attempts == 0
        assert stats.deep_failures == 0
        assert stats.deep_config_skips == 0


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
        analyzer.db_path = Path(tempfile.mktemp(suffix=".db"))

        metrics = SessionMetrics(
            session_id="model-test-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-03",
            session_model="claude-opus",
        )
        flags = SessionFlags(primary_activity="coding")
        with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
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
            ("s1", "vnx-dev", "/test", "T1", today, 5000, 2000, 100, 900, 10, "coding", 0, "claude-opus", 25.0),
            ("s2", "vnx-dev", "/test", "T1", today, 6000, 3000, 200, 1800, 15, "refactoring", 0, "claude-opus", 30.0),
            ("s3", "vnx-dev", "/test", "T2", today, 3000, 1500, 50, 500, 8, "research", 1, "claude-sonnet", 15.0),
            ("s4", "vnx-dev", "/test", "T1", today, 4000, 1800, 80, 700, 12, "coding", 0, "claude-opus", 20.0),
            ("s5", "vnx-dev", "/test", "T2", today, 2500, 1000, 40, 400, 6, "research", 0, "claude-sonnet", 10.0),
            ("s6", "vnx-dev", "/test", "T1", today, 7000, 4000, 150, 1200, 20, "coding", 1, "claude-opus", 35.0),
        ]

        for s in sessions:
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_id, project_path, terminal, session_date,
                    total_input_tokens, total_output_tokens,
                    cache_creation_tokens, cache_read_tokens,
                    tool_calls_total, primary_activity,
                    has_error_recovery, session_model, duration_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    session_id, project_id, project_path, terminal, session_date,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, 'vnx-dev', '/test', 'T2', ?, 'coding', ?, 'claude-sonnet')
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
                    session_id, project_id, project_path, terminal, session_date,
                    total_output_tokens, cache_read_tokens, cache_creation_tokens,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, 'vnx-dev', '/test', 'T1', ?, 50000, 900, 100, 'coding', ?, 'claude-opus')
            """, (f"opus-{i}", today, 1 if i == 0 else 0))

        # 5 sonnet coding sessions (2 success, 3 error)
        for i in range(5):
            conn.execute("""
                INSERT INTO session_analytics (
                    session_id, project_id, project_path, terminal, session_date,
                    total_output_tokens, cache_read_tokens, cache_creation_tokens,
                    primary_activity, has_error_recovery, session_model
                ) VALUES (?, 'vnx-dev', '/test', 'T2', ?, 30000, 400, 100, 'coding', ?, 'claude-sonnet')
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


def _create_intelligence_schema(conn: sqlite3.Connection):
    """Add intelligence tables (success_patterns, antipatterns) to an in-memory DB."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL DEFAULT 'approach',
            category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen DATETIME,
            last_used DATETIME
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL DEFAULT 'approach',
            category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            description TEXT,
            pattern_data TEXT,
            why_problematic TEXT,
            severity TEXT DEFAULT 'medium',
            occurrence_count INTEGER DEFAULT 0,
            better_alternative TEXT,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen DATETIME,
            last_seen DATETIME
        );
    """)


def _make_analyzer_with_intel_db() -> ConversationAnalyzer:
    """Create a ConversationAnalyzer wired to an in-memory DB with all tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_schema(conn)
    _create_intelligence_schema(conn)

    import tempfile
    analyzer = ConversationAnalyzer(Path(tempfile.mktemp(suffix=".db")))
    analyzer.conn = conn
    return analyzer


# ---------------------------------------------------------------------------
# Bridge: intelligence DB integration
# ---------------------------------------------------------------------------

class TestBridgeSessionToIntelligence:
    """Tests for ConversationAnalyzer.bridge_session_to_intelligence()."""

    def test_test_cycle_writes_success_pattern(self):
        """has_test_cycle=True must insert a row into success_patterns."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics()
        flags = SessionFlags(has_test_cycle=True)

        analyzer.bridge_session_to_intelligence(metrics, flags)

        rows = analyzer.conn.execute(
            "SELECT title, pattern_data, category FROM success_patterns"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["title"] == "Test-driven workflow detected"
        assert "session_analysis" in row["pattern_data"]
        assert row["category"] == ""  # universal scope

    def test_test_cycle_upserts_on_repeat(self):
        """Repeated calls increment usage_count instead of inserting duplicates."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics()
        flags = SessionFlags(has_test_cycle=True)

        analyzer.bridge_session_to_intelligence(metrics, flags)
        analyzer.bridge_session_to_intelligence(metrics, flags)

        rows = analyzer.conn.execute(
            "SELECT usage_count FROM success_patterns "
            "WHERE title = 'Test-driven workflow detected'"
        ).fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["usage_count"] == 2

    def test_debugging_long_session_writes_antipattern(self):
        """primary_activity=debugging + duration>30 must insert an antipattern."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(duration_minutes=45.0)
        flags = SessionFlags(primary_activity="debugging")

        analyzer.bridge_session_to_intelligence(metrics, flags)

        rows = analyzer.conn.execute(
            "SELECT title, severity, category FROM antipatterns"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["title"] == "Extended debugging session"
        assert row["severity"] == "medium"
        assert row["category"] == ""

    def test_debugging_short_session_skipped(self):
        """Debugging under 30 min must not create an antipattern."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(duration_minutes=15.0)
        flags = SessionFlags(primary_activity="debugging")

        analyzer.bridge_session_to_intelligence(metrics, flags)

        count = analyzer.conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert count == 0

    def test_error_recovery_writes_low_severity_antipattern(self):
        """has_error_recovery=True must insert an antipattern with severity=low."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics()
        flags = SessionFlags(has_error_recovery=True)

        analyzer.bridge_session_to_intelligence(metrics, flags)

        rows = analyzer.conn.execute(
            "SELECT title, severity FROM antipatterns"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["title"] == "Error recovery required"
        assert row["severity"] == "low"

    def test_improvement_suggestion_bridged_as_antipattern(self):
        """High-priority improvement_suggestions with status='new' must become antipatterns."""
        analyzer = _make_analyzer_with_intel_db()
        # Seed a high-priority suggestion
        analyzer.conn.execute(
            "INSERT INTO improvement_suggestions "
            "(session_id, category, component, current_behavior, suggested_improvement, "
            " priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "workflow", "dispatcher_v8", "slow delivery",
             "batch dispatch creation", "high", "new"),
        )
        analyzer.conn.commit()

        metrics = SessionMetrics()
        flags = SessionFlags()
        analyzer.bridge_session_to_intelligence(metrics, flags)

        rows = analyzer.conn.execute(
            "SELECT title, severity FROM antipatterns "
            "WHERE pattern_data LIKE '%session_analysis%'"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert "HIGH" in row["title"]
        assert row["severity"] == "high"

    def test_medium_priority_suggestion_not_bridged(self):
        """Medium-priority suggestions must not be bridged (only critical/high)."""
        analyzer = _make_analyzer_with_intel_db()
        analyzer.conn.execute(
            "INSERT INTO improvement_suggestions "
            "(session_id, category, component, current_behavior, suggested_improvement, "
            " priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s2", "workflow", "comp", "current", "improvement", "medium", "new"),
        )
        analyzer.conn.commit()

        analyzer.bridge_session_to_intelligence(SessionMetrics(), SessionFlags())

        count = analyzer.conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert count == 0

    def test_no_flags_writes_nothing(self):
        """Default SessionFlags (all False) must produce no DB rows."""
        analyzer = _make_analyzer_with_intel_db()
        analyzer.bridge_session_to_intelligence(SessionMetrics(), SessionFlags())

        sp = analyzer.conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        ap = analyzer.conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert sp == 0
        assert ap == 0

    def test_pattern_data_source_tag(self):
        """All rows written by the bridge must have pattern_data containing 'session_analysis'."""
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(duration_minutes=60.0)
        flags = SessionFlags(
            has_test_cycle=True,
            has_error_recovery=True,
            primary_activity="debugging",
        )
        analyzer.bridge_session_to_intelligence(metrics, flags)

        for table in ("success_patterns", "antipatterns"):
            rows = analyzer.conn.execute(
                f"SELECT pattern_data FROM {table}"
            ).fetchall()
            for row in rows:
                assert "session_analysis" in (dict(row)["pattern_data"] or ""), \
                    f"{table} row missing session_analysis tag"

    def test_already_acted_on_suggestion_not_bridged(self):
        """Suggestions with status != 'new' must not be bridged."""
        analyzer = _make_analyzer_with_intel_db()
        analyzer.conn.execute(
            "INSERT INTO improvement_suggestions "
            "(session_id, category, component, current_behavior, suggested_improvement, "
            " priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s3", "workflow", "comp", "current", "improvement", "critical", "acted_on"),
        )
        analyzer.conn.commit()

        analyzer.bridge_session_to_intelligence(SessionMetrics(), SessionFlags())

        count = analyzer.conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Atomicity: session_analytics + intelligence bridge in one transaction
# ---------------------------------------------------------------------------

class TestAtomicWrites:
    """session_analytics and intelligence writes must be atomic."""

    def test_session_stored_before_bridge(self):
        """_store_session is called before bridge_session_to_intelligence.

        The INSERT into session_analytics runs first. When the bridge fails
        mid-write, the ABORT-level rollback undoes both writes — no orphan
        session_analytics row remains. The bridge catches its own exceptions
        internally and does not re-raise; the caller's commit() commits an
        empty transaction.
        """
        analyzer = _make_analyzer_with_intel_db()

        # Install a trigger that makes the first intelligence INSERT fail
        # after session_analytics has already been written. This simulates
        # the bridge failing mid-write.
        analyzer.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS _test_force_bridge_fail
            BEFORE INSERT ON success_patterns
            BEGIN
                SELECT RAISE(ABORT, 'simulated bridge failure');
            END
        """)

        metrics = SessionMetrics(
            session_id="atomic-test-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-04",
        )
        flags = SessionFlags(has_test_cycle=True)

        # Simulate the atomic write pattern from analyze_session.
        # The bridge catches its own exception; no exception propagates.
        # The ABORT inside the trigger rolls back the implicit transaction
        # that includes the _store_session INSERT.
        with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
            analyzer._store_session(metrics, flags, None)
        analyzer.bridge_session_to_intelligence(metrics, flags)
        analyzer.conn.commit()

        # Verify: neither write landed — the ABORT rolled back everything.
        sa_rows = analyzer.conn.execute(
            "SELECT COUNT(*) FROM session_analytics WHERE session_id = 'atomic-test-001'"
        ).fetchone()[0]
        sp_rows = analyzer.conn.execute(
            "SELECT COUNT(*) FROM success_patterns"
        ).fetchone()[0]

        assert sa_rows == 0, (
            f"session_analytics has {sa_rows} row(s) — "
            "INSERT survived the bridge-triggered ABORT"
        )
        assert sp_rows == 0, (
            f"success_patterns has {sp_rows} row(s) — "
            "bridge wrote despite trigger"
        )

    def test_no_orphan_intelligence_on_session_failure(self):
        """When session_analytics INSERT fails, no intelligence rows are written.

        The bridge is called AFTER _store_session. If _store_session raises,
        the bridge code is never reached at all — structural atomicity without
        needing a transaction rollback.
        """
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(
            session_id="orphan-test-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-04",
        )
        flags = SessionFlags(has_test_cycle=True)

        # Drop the session_analytics table to force _store_session to fail.
        analyzer.conn.execute("DROP TABLE IF EXISTS session_analytics")

        store_failed = False
        try:
            with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
                analyzer._store_session(metrics, flags, None)
            analyzer.bridge_session_to_intelligence(metrics, flags)
            analyzer.conn.commit()
        except Exception:
            store_failed = True
            try:
                analyzer.conn.rollback()
            except Exception:  # vnx-silent-except: in-memory sqlite3 rollback() cannot itself
                # raise here (no broken-connection scenario is under test); kept only for
                # structural parity with the production atomic-write pattern this test exercises.
                pass

        assert store_failed is True, "Expected _store_session to fail"

        # Recreate session_analytics to query intelligence tables (which share
        # the same in-memory DB but were never reached).
        _create_schema(analyzer.conn)
        _create_intelligence_schema(analyzer.conn)

        sp_rows = analyzer.conn.execute(
            "SELECT COUNT(*) FROM success_patterns"
        ).fetchone()[0]
        ap_rows = analyzer.conn.execute(
            "SELECT COUNT(*) FROM antipatterns"
        ).fetchone()[0]

        assert sp_rows == 0, (
            f"success_patterns has {sp_rows} orphan row(s) — "
            "bridge ran despite _store_session failure"
        )
        assert ap_rows == 0, (
            f"antipatterns has {ap_rows} orphan row(s) — "
            "bridge ran despite _store_session failure"
        )

    def test_project_id_populated_on_insert(self):
        """New rows carry the resolved project_id when a tenant is configured.

        The analyzer's db_path here is a bare tempfile (no .vnx-data/<pid>/state
        layout, no .vnx-project-id marker), so VNX_PROJECT_ID env is the only
        available tenant source.
        """
        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(
            session_id="pid-test-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-04",
        )
        flags = SessionFlags()

        with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
            analyzer._store_session(metrics, flags, None)
            analyzer.conn.commit()

        row = analyzer.conn.execute(
            "SELECT project_id FROM session_analytics WHERE session_id = 'pid-test-001'"
        ).fetchone()
        assert row is not None, "Row was not inserted"
        pid = row[0]
        assert pid is not None, "project_id is NULL"
        assert pid != "", "project_id is empty string"
        assert pid == "vnx-dev", f"Expected 'vnx-dev', got {pid!r}"

    def test_resolve_project_id_raises_when_unresolvable(self):
        """No default: an unresolvable tenant raises rather than stamping 'vnx-dev'.

        ADR-007 rejects a hardcoded default as "a sentinel for legitimate
        rows" — this analyzer runs against every VNX project (sales-copilot,
        seocrawler-v2, mission-control, ...), so a guessed identity here would
        let another tenant's sessions collide under the
        UNIQUE (project_id, session_id) constraint.
        """
        from project_scope import TenantUnresolved

        analyzer = _make_analyzer_with_intel_db()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_PROJECT_ID", None)
            with pytest.raises(TenantUnresolved):
                analyzer._resolve_project_id()

    def test_store_session_raises_and_writes_nothing_when_tenant_unresolved(self):
        """_store_session must not insert a row when project_id can't resolve."""
        from project_scope import TenantUnresolved

        analyzer = _make_analyzer_with_intel_db()
        metrics = SessionMetrics(
            session_id="unresolved-tenant-001",
            project_path="/test",
            terminal="T1",
            session_date="2026-03-04",
        )
        flags = SessionFlags()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_PROJECT_ID", None)
            with pytest.raises(TenantUnresolved):
                analyzer._store_session(metrics, flags, None)

        count = analyzer.conn.execute(
            "SELECT COUNT(*) FROM session_analytics "
            "WHERE session_id = 'unresolved-tenant-001'"
        ).fetchone()[0]
        assert count == 0, "row was inserted despite an unresolved project_id"

    def test_resolve_project_id_raises_when_project_scope_missing(self):
        """A project_scope import failure must not degrade to a guessed default.

        Simulates the module-level ``except ImportError`` path by clearing the
        cached reference on the runner module — the call-time raise is the
        deliberate choice (see _resolve_project_id docstring): other
        ConversationAnalyzer entry points that never call this method (dry-run,
        parsing-only) should not be broken by an unrelated import error.
        """
        import conversation_analyzer.runner as runner_module
        from project_scope import TenantUnresolved

        analyzer = _make_analyzer_with_intel_db()
        original = runner_module.resolve_stamp_project_id
        runner_module.resolve_stamp_project_id = None
        try:
            with pytest.raises(TenantUnresolved):
                analyzer._resolve_project_id()
        finally:
            runner_module.resolve_stamp_project_id = original

    def test_resolve_project_id_conflict_does_not_fall_back_to_env(self, tmp_path):
        """A path/env conflict must raise, never silently resolve to env.

        Regression test for the fix-forward on PR #1248: _resolve_project_id
        used to retry a bare, env-only resolve_stamp_project_id() whenever the
        db_path-anchored call raised TenantUnresolved — including when that
        raise came from a genuine SOURCE CONFLICT (db path says one tenant,
        VNX_PROJECT_ID says another), not just "no source at all". That retry
        silently returned the env value, papering over exactly the
        cross-tenant contamination this guard exists to catch.
        """
        from project_scope import TenantUnresolved

        db_dir = tmp_path / ".vnx-data" / "mission-control" / "state"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "quality_intelligence.db"

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _create_schema(conn)
        _create_intelligence_schema(conn)

        analyzer = ConversationAnalyzer(db_path)
        analyzer.conn = conn

        with patch.dict(os.environ, {"VNX_PROJECT_ID": "vnx-dev"}):
            with pytest.raises(TenantUnresolved):
                analyzer._resolve_project_id()


# ---------------------------------------------------------------------------
# Fail-closed exit code (OI-862)
# ---------------------------------------------------------------------------

class TestFailClosedExitCode:

    def test_fail_closed_pure_function(self):
        """The exit-code helper fails closed only on a fully-failed run."""
        from conversation_analyzer import fail_closed_exit_code, RunStats
        # All sessions failed -> non-zero (the OI-862 case).
        assert fail_closed_exit_code(RunStats(errors=2, sessions_analyzed=0)) == 1
        # Partial runs stay green — a single session hiccup must not alarm nightly.
        assert fail_closed_exit_code(RunStats(errors=1, sessions_analyzed=1)) == 0
        assert fail_closed_exit_code(RunStats(errors=2, sessions_analyzed=5)) == 0
        # Clean run -> zero.
        assert fail_closed_exit_code(RunStats(errors=0, sessions_analyzed=3)) == 0
        # Defensive: None stats (no run executed) -> zero.
        assert fail_closed_exit_code(None) == 0

    def test_fail_closed_deep_analysis_all_failed(self):
        """Every deep attempt failed -> non-zero (the OI-1258 case)."""
        from conversation_analyzer import fail_closed_exit_code, RunStats
        # 20 attempts, 20 failures -> the silent deep_analyzed=0 night.
        assert fail_closed_exit_code(RunStats(deep_attempts=20, deep_failures=20)) == 1
        # Partial deep failure stays green (some sessions did produce a result).
        assert fail_closed_exit_code(RunStats(deep_attempts=20, deep_failures=5)) == 0
        # No attempts -> zero (nothing was tried; not a failure).
        assert fail_closed_exit_code(RunStats(deep_attempts=0, deep_failures=0)) == 0
        # Deep failure does not need errors/sessions to be zero to fail closed.
        assert fail_closed_exit_code(
            RunStats(errors=0, sessions_analyzed=50, deep_attempts=20, deep_failures=20)
        ) == 1

    def test_fail_closed_config_skip_night_stays_green(self):
        """A run where every flagged session hit a configuration gap (no API
        key, no model, unreachable lane) must not fail-close (fix1585-r2).

        Before the fix, ``deep_attempts`` was incremented the moment a
        strategy branch was entered — including a pure config-gap
        short-circuit — so a 20-session config-gap night reported
        ``deep_attempts=20, deep_failures=20`` and fail-closed the nightly
        job over a problem no attempt was ever made to solve. With the fix,
        those 20 sessions land in ``deep_config_skips`` instead and
        ``deep_attempts`` stays 0.
        """
        from conversation_analyzer import fail_closed_exit_code, RunStats
        assert fail_closed_exit_code(
            RunStats(deep_attempts=0, deep_failures=0, deep_config_skips=20)
        ) == 0
        # Mixed run: some genuine attempts failed alongside config skips ->
        # still fails closed on the genuine failures.
        assert fail_closed_exit_code(
            RunStats(deep_attempts=5, deep_failures=5, deep_config_skips=15)
        ) == 1

    def test_main_returns_nonzero_when_all_sessions_fail(self):
        """``conversation_analyzer.py`` exits non-zero when every session failed."""
        import importlib.util
        from unittest.mock import Mock

        from conversation_analyzer import RunStats

        spec = importlib.util.spec_from_file_location(
            "conv_analyzer_main_oi862",
            SCRIPT_DIR / "conversation_analyzer.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fake = Mock()
        fake.run.return_value = RunStats(errors=3, sessions_analyzed=0)
        tmp_db = Path(tempfile.mkdtemp()) / "quality.db"
        tmp_db.write_text("")

        argv = ["conversation_analyzer.py", "--max-sessions", "5"]
        with patch.object(sys, "argv", argv), \
             patch.object(mod, "DB_PATH", tmp_db), \
             patch.object(mod, "ConversationAnalyzer", return_value=fake):
            rc = mod.main()

        assert rc == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
