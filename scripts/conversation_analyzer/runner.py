"""Orchestrator: full 4-phase pipeline + storage."""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import (
    SessionMetrics, SessionFlags, RunStats,
    ANALYZER_VERSION, VNX_BASE, Colors,
    log,
)
from .parser import SessionParser
from .detector import HeuristicDetector
from .deep_analyzer import DeepAnalyzer
from .generator import DigestGenerator
from . import intelligence_bridge


def _get_claude_projects_dir() -> Path:
    """Late-bind CLAUDE_PROJECTS_DIR to support test patching via the package namespace."""
    pkg = sys.modules.get(__package__)
    if pkg and hasattr(pkg, "CLAUDE_PROJECTS_DIR"):
        return pkg.CLAUDE_PROJECTS_DIR
    from .models import CLAUDE_PROJECTS_DIR
    return CLAUDE_PROJECTS_DIR


class ConversationAnalyzer:
    """Orchestrate the full 4-phase pipeline."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.parser = SessionParser()
        self.detector = HeuristicDetector()
        self.deep = DeepAnalyzer()
        self.digest_gen = DigestGenerator()
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    def find_unanalyzed_sessions(self, project_filter: Optional[str] = None,
                                  terminal_filter: Optional[str] = None,
                                  diagnostics: bool = False) -> List[Path]:
        claude_projects_dir = _get_claude_projects_dir()
        if not claude_projects_dir.exists():
            log("WARNING", f"Claude projects dir not found: {claude_projects_dir}")
            if diagnostics:
                log("INFO", "  Set CLAUDE_PROJECTS_DIR env var to override")
            return []

        if diagnostics:
            log("INFO", f"Session source: {claude_projects_dir}")

        known_ids = set()
        if self.conn:
            cur = self.conn.cursor()
            cur.execute("SELECT session_id FROM session_analytics")
            known_ids = {row[0] for row in cur.fetchall()}

        if diagnostics:
            log("INFO", f"Already imported: {len(known_ids)} sessions")

        candidates = []
        project_counts: Dict[str, int] = {}
        for project_dir in claude_projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            dir_name = project_dir.name
            if project_filter and project_filter not in dir_name:
                continue
            if terminal_filter:
                terminal = self.parser.detect_terminal(dir_name)
                if terminal != terminal_filter:
                    continue

            dir_candidates = [
                jsonl_file for jsonl_file in project_dir.glob("*.jsonl")
                if jsonl_file.stem not in known_ids
            ]
            if dir_candidates:
                project_counts[dir_name] = len(dir_candidates)
                candidates.extend(dir_candidates)

        if diagnostics and project_counts:
            log("INFO", "New sessions by project directory:")
            for dir_name, count in sorted(project_counts.items(), key=lambda x: -x[1])[:15]:
                terminal = self.parser.detect_terminal(dir_name)
                log("INFO", f"  [{terminal:>9}] {count:>4} sessions  {dir_name}")
            if len(project_counts) > 15:
                log("INFO", f"  ... and {len(project_counts) - 15} more project directories")

        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        return candidates

    def analyze_session(self, jsonl_path: Path,
                        deep_allowed: bool = True) -> Tuple[Optional[dict], List[dict]]:
        log("ANALYZE", f"Parsing: {jsonl_path.name} ({jsonl_path.stat().st_size // 1024}KB)")

        metrics, messages = self.parser.parse_file(jsonl_path)

        flags = self.detector.detect_patterns(metrics, messages)
        log("INFO", f"  Activity={flags.primary_activity} "
                     f"err={flags.has_error_recovery} ctx={flags.has_context_reset} "
                     f"refactor={flags.has_large_refactor} test={flags.has_test_cycle}")

        self.bridge_session_to_intelligence(metrics, flags)

        deep_result = None
        suggestions = []
        if deep_allowed and self.deep.should_deep_analyze(metrics, flags):
            log("ANALYZE", "  Deep analyzing (flagged)...")
            deep_result = self.deep.analyze_session(jsonl_path, metrics, flags)
            if deep_result and "suggestions" in deep_result:
                for sg in deep_result["suggestions"]:
                    sg["session_id"] = metrics.session_id
                suggestions = deep_result.get("suggestions", [])

        self._store_session(metrics, flags, deep_result)
        log("SUCCESS", f"  Stored: {metrics.session_id[:8]}... "
                        f"tokens={metrics.total_output_tokens:,}")

        row = {
            "terminal": metrics.terminal,
            "total_input_tokens": metrics.total_input_tokens,
            "total_output_tokens": metrics.total_output_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_creation_tokens": metrics.cache_creation_tokens,
        }
        return row, suggestions

    def bridge_session_to_intelligence(self, metrics: SessionMetrics,
                                       flags: SessionFlags):
        intelligence_bridge.bridge_session_to_intelligence(self.conn, metrics, flags)

    def _store_session(self, metrics: SessionMetrics, flags: SessionFlags,
                       deep_result: Optional[dict]):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO session_analytics (
                session_id, project_path, terminal, session_date,
                total_input_tokens, total_output_tokens,
                cache_creation_tokens, cache_read_tokens,
                tool_calls_total, tool_read_count, tool_edit_count,
                tool_bash_count, tool_grep_count, tool_write_count,
                tool_task_count, tool_other_count,
                message_count, user_message_count, assistant_message_count,
                duration_minutes,
                has_error_recovery, has_context_reset, context_reset_count,
                has_large_refactor, has_test_cycle, primary_activity,
                deep_analysis_json, deep_analysis_model, deep_analysis_at,
                file_size_bytes, analyzer_version, session_model, dispatch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metrics.session_id, metrics.project_path, metrics.terminal,
            metrics.session_date,
            metrics.total_input_tokens, metrics.total_output_tokens,
            metrics.cache_creation_tokens, metrics.cache_read_tokens,
            metrics.tool_calls_total, metrics.tool_read_count,
            metrics.tool_edit_count, metrics.tool_bash_count,
            metrics.tool_grep_count, metrics.tool_write_count,
            metrics.tool_task_count, metrics.tool_other_count,
            metrics.message_count, metrics.user_message_count,
            metrics.assistant_message_count, metrics.duration_minutes,
            flags.has_error_recovery, flags.has_context_reset,
            flags.context_reset_count,
            flags.has_large_refactor, flags.has_test_cycle,
            flags.primary_activity,
            json.dumps(deep_result) if deep_result else None,
            (deep_result.get("_model", "unknown") if deep_result else None),
            (datetime.now().isoformat() if deep_result else None),
            metrics.file_size_bytes, ANALYZER_VERSION,
            metrics.session_model or "unknown",
            metrics.dispatch_id or None,
        ))
        self.conn.commit()

    def _store_suggestions(self, suggestions: List[dict], digest_id: str):
        if not suggestions:
            return
        cur = self.conn.cursor()
        for sg in suggestions:
            cur.execute("""
                INSERT INTO improvement_suggestions (
                    session_id, category, component,
                    current_behavior, suggested_improvement,
                    evidence, priority, digest_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sg.get("session_id", ""),
                sg.get("category", "workflow"),
                sg.get("component", ""),
                sg.get("current_behavior", ""),
                sg.get("suggested_improvement", ""),
                sg.get("evidence", ""),
                sg.get("priority", "medium"),
                digest_id,
            ))
        self.conn.commit()

    def _store_digest(self, run_date: str, stats: RunStats,
                      markdown: str, digest_path: Path):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO nightly_digests (
                digest_date, sessions_analyzed, deep_analyzed,
                new_suggestions, total_tokens_used,
                digest_markdown, digest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_date, stats.sessions_analyzed, stats.sessions_deep,
            len(stats.suggestions), stats.total_tokens,
            markdown, str(digest_path),
        ))
        self.conn.commit()

    def run(self, max_sessions: int = 50, deep_budget: int = 20,
            dry_run: bool = False,
            project_filter: Optional[str] = None,
            terminal_filter: Optional[str] = None):
        log("INFO", "Starting conversation analysis pipeline...")
        run_date = datetime.now().strftime("%Y-%m-%d")
        stats = RunStats()

        sessions = self.find_unanalyzed_sessions(project_filter, terminal_filter,
                                                  diagnostics=dry_run)
        log("INFO", f"Found {len(sessions)} unanalyzed sessions")

        if not sessions:
            log("INFO", "Nothing to analyze")
            return stats

        sessions = sessions[:max_sessions]
        deep_remaining = deep_budget
        session_rows: List[dict] = []

        for i, jsonl_path in enumerate(sessions, 1):
            log("ANALYZE", f"[{i}/{len(sessions)}] {jsonl_path.parent.name}/{jsonl_path.name}")
            deep_remaining = self._process_one_session(
                jsonl_path, dry_run, deep_remaining, stats, session_rows)

        if not dry_run:
            self._finalize_run(stats, session_rows, run_date)

        self._print_summary(stats, dry_run, run_date)
        return stats

    def _process_one_session(self, jsonl_path: Path, dry_run: bool,
                              deep_remaining: int, stats: RunStats,
                              session_rows: List[dict]) -> int:
        if dry_run:
            metrics, _ = self.parser.parse_file(jsonl_path)
            log("INFO", f"  [DRY RUN] tokens={metrics.total_output_tokens:,} "
                        f"tools={metrics.tool_calls_total}")
            stats.sessions_analyzed += 1
            stats.total_tokens += metrics.total_output_tokens
            return deep_remaining

        try:
            row, suggestions = self.analyze_session(jsonl_path, deep_remaining > 0)
            stats.sessions_analyzed += 1
            stats.total_tokens += row.get("total_output_tokens", 0)
            session_rows.append(row)
            if suggestions:
                stats.sessions_deep += 1
                deep_remaining -= 1
                stats.suggestions.extend(suggestions)
        except Exception as e:
            log("ERROR", f"  Failed: {e}")
            stats.errors += 1

        return deep_remaining

    def _finalize_run(self, stats: RunStats, session_rows: List[dict],
                      run_date: str):
        digest_id = f"digest_{run_date}"
        self._store_suggestions(stats.suggestions, digest_id)
        markdown = self.digest_gen.generate(run_date, stats, session_rows, self.db_path)
        digest_path = self.digest_gen.write_digest(markdown, run_date)
        self._store_digest(run_date, stats, markdown, digest_path)
        log("SUCCESS", f"Digest written to: {digest_path}")

    def _print_summary(self, stats: RunStats, dry_run: bool, run_date: str):
        print(f"\n{Colors.GREEN}{'=' * 70}")
        print("Conversation Analysis Complete!")
        print(f"{'=' * 70}{Colors.RESET}\n")
        print(f"Sessions Analyzed: {stats.sessions_analyzed}")
        print(f"Deep Analyzed:     {stats.sessions_deep}")
        print(f"Suggestions:       {len(stats.suggestions)}")
        print(f"Total Tokens:      {stats.total_tokens:,}")
        print(f"Errors:            {stats.errors}")

        if not dry_run and stats.sessions_analyzed > 0:
            digest_path = VNX_BASE / "reports" / "nightly" / f"digest_{run_date}.md"
            print(f"\nDigest: {digest_path}")
