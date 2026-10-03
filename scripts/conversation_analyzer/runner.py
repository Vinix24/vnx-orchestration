"""Orchestrator: full 4-phase pipeline + storage."""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import content_class
from .models import (
    format_failure_reasons,
    SessionMetrics, SessionFlags, RunStats,
    ANALYZER_VERSION, VNX_BASE, Colors,
    log,
)
from .parser import SessionParser
from .detector import HeuristicDetector
from .deep_analyzer import DeepAnalyzer
from .generator import DigestGenerator
from . import intelligence_bridge

# Deliberately NOT fatal at import: dry-run and parse-only entry points on
# ConversationAnalyzer never touch _resolve_project_id and should not break
# on an unrelated import failure. The sentinel below only matters to the one
# method (_resolve_project_id) that enforces the ADR-007 tenant guarantee —
# it raises there, at call time, instead of guessing a project_id.
try:
    from project_scope import resolve_stamp_project_id, TenantUnresolved
except ImportError:
    resolve_stamp_project_id = None  # type: ignore[assignment]
    TenantUnresolved = RuntimeError  # type: ignore[assignment,misc]


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

        origin = self._classify_origin(metrics)
        flags = self.detector.detect_patterns(metrics, messages)
        log("INFO", f"  Activity={flags.primary_activity} "
                     f"err={flags.has_error_recovery} ctx={flags.has_context_reset} "
                     f"refactor={flags.has_large_refactor} test={flags.has_test_cycle}")

        deep_result = None
        suggestions = []
        deferred_reason = None
        if deep_allowed and self.deep.should_deep_analyze(metrics, flags):
            log("ANALYZE", "  Deep analyzing (flagged)...")
            deep_result = self.deep.analyze_session(jsonl_path, metrics, flags,
                                                    origin=origin)
            suggestions = self._tag_suggestions(deep_result, metrics, origin)
            if deep_result is None and self.deep.last_status == "restricted_deferred":
                deferred_reason = self.deep.last_defer_reason

        # Single transaction over both writes (ADR-007 atomicity):
        # _store_session first so a failing INSERT does not leave orphan
        # intelligence rows from bridge_session_to_intelligence.
        try:
            self._store_session(metrics, flags, deep_result, origin, deferred_reason)
            self.bridge_session_to_intelligence(metrics, flags)
            self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception as rollback_exc:
                log("ERROR", f"  Rollback failed for {metrics.session_id[:8]}...: "
                             f"{rollback_exc} — connection may be left in a broken "
                             f"state for the next session's write")
            log("ERROR", f"  Atomic write failed for {metrics.session_id[:8]}..., "
                         f"rolling back both session_analytics and intelligence rows")
            raise

        log("SUCCESS", f"  Stored: {metrics.session_id[:8]}... "
                        f"tokens={metrics.total_output_tokens:,}")

        row = {
            "terminal": metrics.terminal,
            "total_input_tokens": metrics.total_input_tokens,
            "total_output_tokens": metrics.total_output_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_creation_tokens": metrics.cache_creation_tokens,
            "origin_class": origin.cls,
        }
        return row, suggestions

    def _classify_origin(self, metrics: SessionMetrics) -> content_class.Origin:
        """Class of the place the session worked in, from the transcript cwd.

        The decoded dir name is the fallback for a transcript with no cwd. A missing boundary
        file gives ``unknown``: with no roots to compare against, no path can be called safe.
        """
        boundary = content_class.load_boundary()
        origin = content_class.classify_path(metrics.project_path or None, boundary)
        if not boundary.configured:
            return content_class.Origin(content_class.UNKNOWN, origin.project_id, origin.source)
        return origin

    @staticmethod
    def _origin_source(metrics: SessionMetrics) -> str:
        return "cwd" if metrics.cwd else "decoded_dirname"

    @staticmethod
    def _tag_suggestions(deep_result: Optional[dict], metrics: SessionMetrics,
                         origin: content_class.Origin) -> List[dict]:
        if not deep_result or "suggestions" not in deep_result:
            return []
        for sg in deep_result["suggestions"]:
            sg["session_id"] = metrics.session_id
            sg["origin_class"] = origin.cls
        return deep_result.get("suggestions", [])

    def bridge_session_to_intelligence(self, metrics: SessionMetrics,
                                       flags: SessionFlags):
        intelligence_bridge.bridge_session_to_intelligence(
            self.conn, metrics, flags, project_id=self._resolve_project_id()
        )

    def _store_session(self, metrics: SessionMetrics, flags: SessionFlags,
                       deep_result: Optional[dict],
                       origin: Optional[content_class.Origin] = None,
                       deferred_reason: Optional[str] = None):
        origin = origin or self._classify_origin(metrics)
        project_id = self._resolve_project_id()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO session_analytics (
                session_id, project_id, project_path, terminal, session_date,
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
                file_size_bytes, analyzer_version, session_model, dispatch_id,
                origin_class, origin_project_id, origin_source, deep_deferred_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metrics.session_id, project_id, metrics.project_path, metrics.terminal,
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
            origin.cls, origin.project_id, self._origin_source(metrics),
            None if deep_result else deferred_reason,
        ))

    def _resolve_project_id(self) -> str:
        """Resolve the project_id for tenant-scoped writes, fail-closed (ADR-007).

        Delegates to ``resolve_stamp_project_id(db_path=...)``, which already
        weighs {DB-path layout, ``.vnx-project-id`` marker, ``VNX_PROJECT_ID``
        env} as co-sources that must agree. There is no ``'vnx-dev'`` default:
        ADR-007 explicitly rejects a stamped default as "a sentinel for
        legitimate rows", and this analyzer runs against every VNX project,
        not just this one — a guessed identity here would let another
        tenant's sessions land in the wrong project's key space under the
        ``UNIQUE (project_id, session_id)`` constraint.

        Deliberately does NOT retry with a bare ``resolve_stamp_project_id()``
        (env-only) on ``TenantUnresolved``: when the db_path-anchored call
        raises because its sources conflict (path/marker disagree with env),
        a retry that checks env alone would silently pick the env value and
        paper over that conflict — the exact contamination this guard exists
        to catch. Any ``TenantUnresolved`` propagates so the caller
        (``analyze_session``) aborts and rolls back that one session's write;
        the run continues with the next session.

        A missing ``project_scope`` module (import failure) is treated the
        same way — raised here at call time rather than made fatal at module
        import. Other ``ConversationAnalyzer`` entry points (dry-run, parsing
        only) never reach this method and should not be broken by an
        unrelated import error; only the write path that actually needs the
        tenant guarantee pays for enforcing it.
        """
        if resolve_stamp_project_id is None:
            raise TenantUnresolved(
                "project_scope module is unavailable (import failed); "
                "refusing to stamp a guessed project_id (ADR-007)"
            )
        return resolve_stamp_project_id(db_path=str(self.db_path))

    def _store_suggestions(self, suggestions: List[dict], digest_id: str):
        if not suggestions:
            return
        cur = self.conn.cursor()
        for sg in suggestions:
            cur.execute("""
                INSERT INTO improvement_suggestions (
                    session_id, category, component,
                    current_behavior, suggested_improvement,
                    evidence, priority, digest_id, origin_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sg.get("session_id", ""),
                sg.get("category", "workflow"),
                sg.get("component", ""),
                sg.get("current_behavior", ""),
                sg.get("suggested_improvement", ""),
                sg.get("evidence", ""),
                sg.get("priority", "medium"),
                digest_id,
                sg.get("origin_class"),
            ))
        self.conn.commit()

    def _store_digest(self, run_date: str, stats: RunStats,
                      markdown: str, digest_path: Path):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO nightly_digests (
                digest_date, sessions_analyzed, deep_analyzed,
                deep_attempts, deep_failures, deep_config_skips,
                new_suggestions, total_tokens_used,
                digest_markdown, digest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_date, stats.sessions_analyzed, stats.sessions_deep,
            stats.deep_attempts, stats.deep_failures, stats.deep_config_skips,
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
        self.deep.reset_restricted_run()

        boundary = content_class.load_boundary()
        if not boundary.configured:
            log("ERROR", f"Content boundary unconfigured ({boundary.reason}): every session is "
                         f"treated as restricted and goes to Claude only, never to another "
                         f"provider. Write ~/.vnx/content_boundary.json to classify sessions.")

        sessions = self.find_unanalyzed_sessions(project_filter, terminal_filter,
                                                  diagnostics=dry_run)
        log("INFO", f"Found {len(sessions)} unanalyzed sessions")

        # Inform the deep analyzer of the backlog size for the billing guard.
        self.deep.set_session_backlog(len(sessions))

        deep_remaining = deep_budget
        session_rows: List[dict] = []

        # Restricted sessions that were deferred on an earlier night get the Claude budget
        # first. Their rows are already stored, so known_ids never lets them back in. They draw
        # from the same deep budget as the sessions below.
        if not dry_run:
            deep_remaining = self._process_restricted_backlog(stats, deep_remaining)

        if not sessions:
            log("INFO", "Nothing to analyze")
            self._copy_deep_counters(stats)
            if not dry_run and stats.suggestions:
                self._finalize_run(stats, session_rows, run_date)
            return stats

        sessions = sessions[:max_sessions]

        for i, jsonl_path in enumerate(sessions, 1):
            log("ANALYZE", f"[{i}/{len(sessions)}] {jsonl_path.parent.name}/{jsonl_path.name}")
            deep_remaining = self._process_one_session(
                jsonl_path, dry_run, deep_remaining, stats, session_rows)

        self._copy_deep_counters(stats)

        if not dry_run:
            self._finalize_run(stats, session_rows, run_date)

        self._print_summary(stats, dry_run, run_date)
        return stats

    def _copy_deep_counters(self, stats: RunStats) -> None:
        # OI-1258: copy the analyzer's per-run attempt accounting into the run
        # stats so the digest, DB row, and fail-closed exit code all see the
        # attempts/failures alongside the success-only ``sessions_deep``.
        stats.deep_attempts = self.deep.deep_attempts
        stats.deep_failures = self.deep.deep_failures
        stats.deep_failure_reasons = dict(self.deep.deep_failure_reasons)
        stats.deep_config_skips = self.deep.deep_config_skips
        stats.deep_restricted_claude = self.deep.deep_restricted_claude
        stats.deep_restricted_deferred = self.deep.deep_restricted_deferred

    def _process_restricted_backlog(self, stats: RunStats, deep_remaining: int) -> int:
        """Spend the restricted Claude budget on stored sessions that still lack a deep result.

        Candidates are rows of this store (ADR-007: filtered on project_id) carrying a
        deferral marker and no deep result, newest first, whose transcript still exists and
        is flagged for deep analysis. The marker is independent of the origin class: a fabric
        session routed to Claude by its transcript or summary text is replayed too. A legacy row has no
        marker, so no night silently backfills history.

        Every Claude call made here decrements ``deep_remaining`` (the run's ``--deep-budget``);
        the restricted Claude cap stays a separate, additional limit. Returns the budget left.
        """
        if getattr(self, "conn", None) is None or not self.deep.restricted_budget_left():
            return deep_remaining
        projects_dir = _get_claude_projects_dir()
        if not projects_dir.exists():
            return deep_remaining
        rows = self.conn.execute(
            "SELECT session_id FROM session_analytics "
            "WHERE project_id = ? AND deep_deferred_reason IS NOT NULL "
            "AND deep_analysis_json IS NULL "
            "ORDER BY session_date DESC, id DESC",
            (self._resolve_project_id(),),
        ).fetchall()

        for row in rows:
            if deep_remaining <= 0 or not self.deep.restricted_budget_left():
                break
            jsonl_path = self._stored_transcript(projects_dir, row["session_id"])
            if jsonl_path is None:
                continue
            try:
                metrics, messages = self.parser.parse_file(jsonl_path)
                flags = self.detector.detect_patterns(metrics, messages)
                if not self.deep.should_deep_analyze(metrics, flags):
                    continue
                origin = self._classify_origin(metrics)
                log("ANALYZE", f"Deferred restricted session {metrics.session_id[:8]}...: "
                               f"deep analysing on Claude")
                calls_before = self.deep.restricted_claude_calls
                deep_result = self.deep.analyze_session(jsonl_path, metrics, flags,
                                                        origin=origin)
                deep_remaining -= self.deep.restricted_claude_calls - calls_before
                if not deep_result:
                    if self.deep.last_status == "restricted_deferred":
                        self._store_deferral(metrics.session_id, self.deep.last_defer_reason)
                    continue
                suggestions = self._tag_suggestions(deep_result, metrics, origin)
                self._store_deep_result(metrics.session_id, deep_result)
                stats.sessions_deep += 1
                stats.suggestions.extend(suggestions)
            except Exception as e:
                log("ERROR", f"  Deferred session {row['session_id'][:8]}... failed: {e}")
                stats.errors += 1
                try:
                    self.conn.rollback()
                except sqlite3.Error as rb_exc:
                    log("ERROR", f"  Rollback failed: {rb_exc}")
        return deep_remaining

    @staticmethod
    def _stored_transcript(projects_dir: Path, session_id) -> Optional[Path]:
        """Transcript of a stored session id, or None when the id is malformed or has no file.

        A session id is a transcript file stem. One holding a separator, a glob metacharacter,
        NUL or a dot-only name is skipped with a warning (first 8 characters only), and a match
        is used only when its realpath lies below the realpath of the projects dir.
        """
        sid = session_id if isinstance(session_id, str) else ""
        if (not sid or sid in (".", "..") or "\0" in sid
                or any(ch in sid for ch in "/\\*?[]")):
            log("WARNING", f"Deferred session {sid[:8]!r}: stored session id is not a plain "
                           f"file stem, replay skipped")
            return None
        root = Path(os.path.realpath(projects_dir))
        matches = sorted(projects_dir.glob(f"*/{sid}.jsonl"))
        for match in matches:
            if root in Path(os.path.realpath(match)).parents:
                return match
        if matches:
            log("WARNING", f"Deferred session {sid[:8]!r}: transcript resolves outside the "
                           f"projects dir, replay skipped")
        return None

    def _store_deep_result(self, session_id: str, deep_result: dict) -> None:
        self.conn.execute(
            "UPDATE session_analytics SET deep_analysis_json = ?, deep_analysis_model = ?, "
            "deep_analysis_at = ?, deep_deferred_reason = NULL WHERE project_id = ? AND session_id = ?",
            (json.dumps(deep_result), deep_result.get("_model", "unknown"),
             datetime.now().isoformat(), self._resolve_project_id(), session_id),
        )
        self.conn.commit()

    def _store_deferral(self, session_id: str, reason: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE session_analytics SET deep_deferred_reason = ? "
            "WHERE project_id = ? AND session_id = ?",
            (reason, self._resolve_project_id(), session_id),
        )
        self.conn.commit()

    def _process_one_session(self, jsonl_path: Path, dry_run: bool,
                              deep_remaining: int, stats: RunStats,
                              session_rows: List[dict]) -> int:
        if dry_run:
            metrics, _ = self.parser.parse_file(jsonl_path)
            log("INFO", f"  [DRY RUN] tokens={metrics.total_output_tokens:,} "
                        f"tools={metrics.tool_calls_total}")
            origin_cls = self._classify_origin(metrics).cls
            stats.sessions_by_origin[origin_cls] = stats.sessions_by_origin.get(origin_cls, 0) + 1
            stats.sessions_analyzed += 1
            stats.total_tokens += metrics.total_output_tokens
            return deep_remaining

        try:
            row, suggestions = self.analyze_session(jsonl_path, deep_remaining > 0)
            stats.sessions_analyzed += 1
            stats.total_tokens += row.get("total_output_tokens", 0)
            origin_cls = row["origin_class"]
            stats.sessions_by_origin[origin_cls] = stats.sessions_by_origin.get(origin_cls, 0) + 1
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
        print(f"Deep Attempts:     {stats.deep_attempts}")
        print(f"Deep Failures:     {stats.deep_failures}")
        if stats.deep_failures:
            print(f"DEGRADED:          deep analysis failed ({format_failure_reasons(stats.deep_failure_reasons)})")
        print(f"Deep Config Skips: {stats.deep_config_skips}")
        print(f"Deep Restricted:   {stats.deep_restricted_claude} on Claude, "
              f"{stats.deep_restricted_deferred} deferred")
        if stats.sessions_by_origin:
            by_origin = ", ".join(f"{cls} x{n}" for cls, n in sorted(stats.sessions_by_origin.items()))
            print(f"Sessions by origin: {by_origin}")
        print(f"Suggestions:       {len(stats.suggestions)}")
        print(f"Total Tokens:      {stats.total_tokens:,}")
        print(f"Errors:            {stats.errors}")

        if not dry_run and stats.sessions_analyzed > 0:
            digest_path = VNX_BASE / "reports" / "nightly" / f"digest_{run_date}.md"
            print(f"\nDigest: {digest_path}")
