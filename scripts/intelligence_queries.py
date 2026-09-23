#!/usr/bin/env python3
"""
VNX Intelligence Query System
==============================
Provides T0 and agent access to intelligence system through simple command-line queries.

Author: T-MANAGER
Date: 2026-01-19
Version: 1.0.0
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Add scripts directory to path
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(script_dir / "lib"))

from gather_intelligence import T0IntelligenceGatherer
from cli_output import emit_json, emit_human, parse_human_flag
from result_contract import (
    EXIT_DEPENDENCY,
    EXIT_OK,
    Result,
    result_error,
    result_exit_code,
    result_ok,
)
from vnx_paths import ensure_env, refuse_real_central_store_write_under_test_runner


class IntelligenceQueryAPI:
    """Command-line API for intelligence queries."""

    def __init__(self):
        """Initialize query API with gatherer."""
        self.gatherer = T0IntelligenceGatherer()
        state_dir = Path(ensure_env()["VNX_STATE_DIR"])
        self.usage_log = state_dir / "intelligence_usage.ndjson"
        refuse_real_central_store_write_under_test_runner(self.usage_log)
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _query_error(code: str, message: str, context: Optional[Dict[str, Any]] = None) -> Result:
        if context:
            return result_error(code, message, {"context": context})
        return result_error(code, message)

    def _log_usage(self, command: str, agent: Optional[str] = None) -> None:
        """Best-effort usage logging (non-critical telemetry)."""
        usage = {
            "timestamp": datetime.now().isoformat(),
            "command": command,
            "agent": agent or "T0",
            "user": "system",
        }

        try:
            refuse_real_central_store_write_under_test_runner(self.usage_log)
            with open(self.usage_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(usage, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"[NON_CRITICAL] usage_log_write_failed: {exc}", file=sys.stderr)

    def show_patterns(
        self,
        query: str,
        min_quality: int = 85,
        limit: int = 5,
        agent: Optional[str] = None,
    ) -> Result:
        self._log_usage(f"show_patterns:{query}", agent)

        if not query:
            return self._query_error("missing_argument", "show_patterns requires <query>")
        if limit <= 0:
            return self._query_error("invalid_argument", "limit must be > 0")

        try:
            patterns = self.gatherer.query_relevant_patterns(query, limit=limit * 2)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query patterns: {exc}",
                {"query": query},
            )

        filtered = [p for p in patterns if p.get("quality_score", 0) >= min_quality]
        return result_ok(filtered[:limit])

    def pattern_usage(self, pattern_id: str, agent: Optional[str] = None) -> Result:
        self._log_usage(f"pattern_usage:{pattern_id}", agent)

        if not pattern_id:
            return self._query_error("missing_argument", "pattern_usage requires <pattern_id>")
        if not self.gatherer.quality_db:
            return self._query_error("quality_db_unavailable", "Quality DB is unavailable")

        try:
            cursor = self.gatherer.quality_db.execute(
                """
                SELECT
                    title,
                    usage_count,
                    quality_score,
                    last_used,
                    tags
                FROM code_snippets
                WHERE title LIKE ?
                LIMIT 1
                """,
                (f"%{pattern_id}%",),
            )
            row = cursor.fetchone()
            return result_ok(dict(row) if row else None)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query pattern usage: {exc}",
                {"pattern_id": pattern_id},
            )

    def find_tag_patterns(self, tags: List[str], agent: Optional[str] = None) -> Result:
        self._log_usage(f"find_tag_patterns:{','.join(tags)}", agent)

        if not tags:
            return self._query_error("missing_argument", "find_tag_patterns requires at least one tag")
        if not self.gatherer.tag_engine:
            return self._query_error("tag_engine_unavailable", "Tag engine is unavailable")

        try:
            analysis = self.gatherer.analyze_tags_for_task(tags)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query tag patterns: {exc}",
                {"tags": tags},
            )
        return result_ok(analysis.get("similar_combinations", []))

    def get_tag_frequency(self, tag: str, last_days: int = 30, agent: Optional[str] = None) -> Result:
        self._log_usage(f"get_tag_frequency:{tag}", agent)

        if not tag:
            return self._query_error("missing_argument", "get_tag_frequency requires <tag>")
        if last_days <= 0:
            return self._query_error("invalid_argument", "days must be > 0")
        if not self.gatherer.tag_engine:
            return self._query_error("tag_engine_unavailable", "Tag engine is unavailable")

        try:
            stats = self.gatherer.tag_engine.get_tag_statistics(tag, last_days)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to get tag frequency: {exc}",
                {"tag": tag, "days": last_days},
            )
        frequency = int(stats.get("total_occurrences", 0) or 0)
        return result_ok({"tag": tag, "frequency": frequency, "days": last_days})

    def show_recurring_issues(self, threshold: int = 3, agent: Optional[str] = None) -> Result:
        self._log_usage(f"show_recurring_issues:{threshold}", agent)

        if threshold <= 0:
            return self._query_error("invalid_argument", "threshold must be > 0")
        if not self.gatherer.quality_db:
            return self._query_error("quality_db_unavailable", "Quality DB is unavailable")

        try:
            cursor = self.gatherer.quality_db.execute(
                """
                SELECT
                    tag_combination,
                    occurrence_count,
                    last_seen,
                    phase,
                    terminal
                FROM tag_combinations
                WHERE occurrence_count >= ?
                ORDER BY occurrence_count DESC
                LIMIT 10
                """,
                (threshold,),
            )
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query recurring issues: {exc}",
                {"threshold": threshold},
            )

        results: List[Dict[str, Any]] = []
        for row in cursor:
            try:
                tags = json.loads(row["tag_combination"])
            except (TypeError, json.JSONDecodeError):
                tags = []
            results.append(
                {
                    "tags": tags,
                    "count": row["occurrence_count"],
                    "last_seen": row["last_seen"],
                    "phase": row["phase"],
                    "terminal": row["terminal"],
                }
            )
        return result_ok(results)

    def find_similar_reports(self, query: str, limit: int = 5, agent: Optional[str] = None) -> Result:
        self._log_usage(f"find_similar_reports:{query}", agent)

        if not query:
            return self._query_error("missing_argument", "find_similar_reports requires <query>")
        if limit <= 0:
            return self._query_error("invalid_argument", "limit must be > 0")

        try:
            reports = self.gatherer.find_similar_reports(query)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query similar reports: {exc}",
                {"query": query},
            )
        return result_ok(reports[:limit])

    def get_report_findings(self, last_days: int = 7, agent: Optional[str] = None) -> Result:
        self._log_usage(f"get_report_findings:{last_days}", agent)

        if last_days <= 0:
            return self._query_error("invalid_argument", "days must be > 0")
        if not self.gatherer.quality_db:
            return self._query_error("quality_db_unavailable", "Quality DB is unavailable")

        cutoff = (datetime.now() - timedelta(days=last_days)).isoformat()

        try:
            cursor = self.gatherer.quality_db.execute(
                """
                SELECT
                    report_path,
                    task_type,
                    summary,
                    tags_found,
                    patterns_found,
                    antipatterns_found,
                    report_date,
                    terminal
                FROM report_findings
                WHERE extracted_at >= ?
                ORDER BY extracted_at DESC
                LIMIT 20
                """,
                (cutoff,),
            )
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query report findings: {exc}",
                {"days": last_days},
            )

        findings: List[Dict[str, Any]] = []
        for row in cursor:
            tags: List[str]
            try:
                tags = json.loads(row["tags_found"]) if row["tags_found"] else []
            except (TypeError, json.JSONDecodeError):
                tags = []
            findings.append(
                {
                    "report": row["report_path"],
                    "type": row["task_type"],
                    "summary": row["summary"],
                    "tags": tags,
                    "patterns": row["patterns_found"],
                    "antipatterns": row["antipatterns_found"],
                    "date": row["report_date"],
                    "terminal": row["terminal"],
                }
            )
        return result_ok(findings)

    def extract_prevention_rules(self, component: str, agent: Optional[str] = None) -> Result:
        self._log_usage(f"extract_prevention_rules:{component}", agent)

        if not component:
            return self._query_error("missing_argument", "extract_prevention_rules requires <component>")

        try:
            tags = self.gatherer.extract_tags_from_description(component)
            return result_ok(self.gatherer.query_prevention_rules(component, tags))
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to extract prevention rules: {exc}",
                {"component": component},
            )

    def debug_query(self, issue: str, agent: Optional[str] = None) -> Result:
        self._log_usage(f"debug_query:{issue}", agent)

        if not issue:
            return self._query_error("missing_argument", "debug_query requires <issue>")

        patterns_result = self.show_patterns(issue, limit=3, agent=agent)
        if not patterns_result.ok:
            return patterns_result

        similar_result = self.find_similar_reports(issue, limit=3, agent=agent)
        if not similar_result.ok:
            return similar_result

        prevention_result = self.extract_prevention_rules(issue, agent=agent)
        if not prevention_result.ok:
            return prevention_result

        try:
            antipatterns = self.gatherer.query_antipatterns(issue, limit=3)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to query antipatterns: {exc}",
                {"issue": issue},
            )

        return result_ok(
            {
                "issue": issue,
                "patterns": patterns_result.data,
                "similar_reports": similar_result.data,
                "antipatterns": antipatterns,
                "prevention_rules": prevention_result.data,
            }
        )

    def root_cause_analysis(self, symptom: str, last_days: int = 30, agent: Optional[str] = None) -> Result:
        self._log_usage(f"root_cause_analysis:{symptom}", agent)

        if not symptom:
            return self._query_error("missing_argument", "root_cause_analysis requires <symptom>")
        if last_days <= 0:
            return self._query_error("invalid_argument", "days must be > 0")

        recurring_result = self.show_recurring_issues(threshold=2, agent=agent)
        if not recurring_result.ok:
            return recurring_result

        findings_result = self.get_report_findings(last_days, agent=agent)
        if not findings_result.ok:
            return findings_result

        try:
            tags = self.gatherer.extract_tags_from_description(symptom)
            tag_analysis = self.gatherer.analyze_tags_for_task(tags)
            prevention_rules = self.gatherer.query_prevention_rules(symptom, tags)
            antipatterns = self.gatherer.query_antipatterns(symptom, limit=5)
        except Exception as exc:
            return self._query_error(
                "query_execution_failed",
                f"Failed to execute root cause analysis: {exc}",
                {"symptom": symptom, "days": last_days},
            )

        return result_ok(
            {
                "symptom": symptom,
                "extracted_tags": tags,
                "tag_analysis": tag_analysis,
                "recurring_patterns": recurring_result.data,
                "historical_reports": findings_result.data,
                "prevention_rules": prevention_rules,
                "antipatterns": antipatterns,
            }
        )


def _parse_int(value: str, field_name: str) -> Result:
    try:
        return result_ok(int(value))
    except ValueError:
        return result_error(
            "invalid_argument",
            f"{field_name} must be an integer",
            {"context": {"field": field_name, "value": value}},
        )


def _emit_result(result: Result, human: bool) -> None:
    if human:
        if result.ok:
            emit_human(json.dumps(result.data, indent=2, default=str))
        else:
            emit_human(f"ERROR [{result.error_code}]: {result.error_msg}")
    else:
        emit_json(result.to_dict())


def _run_command(api: IntelligenceQueryAPI, argv_without_human: List[str]) -> Result:
    command = argv_without_human[0]

    if command == "show_patterns":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "show_patterns requires <query>")
        query = argv_without_human[1]
        min_quality = 85
        limit = 5
        if len(argv_without_human) > 2:
            parsed = _parse_int(argv_without_human[2], "min_quality")
            if not parsed.ok:
                return parsed
            min_quality = parsed.data
        if len(argv_without_human) > 3:
            parsed = _parse_int(argv_without_human[3], "limit")
            if not parsed.ok:
                return parsed
            limit = parsed.data
        agent = argv_without_human[4] if len(argv_without_human) > 4 else None
        return api.show_patterns(query, min_quality, limit, agent)

    if command == "pattern_usage":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "pattern_usage requires <pattern_id>")
        pattern_id = argv_without_human[1]
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.pattern_usage(pattern_id, agent)

    if command == "find_tag_patterns":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "find_tag_patterns requires <tag1,tag2,...>")
        tags = [tag for tag in argv_without_human[1].split(",") if tag]
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.find_tag_patterns(tags, agent)

    if command == "get_tag_frequency":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "get_tag_frequency requires <tag>")
        tag = argv_without_human[1]
        days = 30
        if len(argv_without_human) > 2:
            parsed = _parse_int(argv_without_human[2], "days")
            if not parsed.ok:
                return parsed
            days = parsed.data
        agent = argv_without_human[3] if len(argv_without_human) > 3 else None
        return api.get_tag_frequency(tag, days, agent)

    if command == "show_recurring_issues":
        threshold = 3
        if len(argv_without_human) > 1:
            parsed = _parse_int(argv_without_human[1], "threshold")
            if not parsed.ok:
                return parsed
            threshold = parsed.data
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.show_recurring_issues(threshold, agent)

    if command == "find_similar_reports":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "find_similar_reports requires <query>")
        query = argv_without_human[1]
        limit = 5
        if len(argv_without_human) > 2:
            parsed = _parse_int(argv_without_human[2], "limit")
            if not parsed.ok:
                return parsed
            limit = parsed.data
        agent = argv_without_human[3] if len(argv_without_human) > 3 else None
        return api.find_similar_reports(query, limit, agent)

    if command == "get_report_findings":
        days = 7
        if len(argv_without_human) > 1:
            parsed = _parse_int(argv_without_human[1], "days")
            if not parsed.ok:
                return parsed
            days = parsed.data
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.get_report_findings(days, agent)

    if command == "extract_prevention_rules":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "extract_prevention_rules requires <component>")
        component = argv_without_human[1]
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.extract_prevention_rules(component, agent)

    if command == "debug_query":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "debug_query requires <issue>")
        issue = argv_without_human[1]
        agent = argv_without_human[2] if len(argv_without_human) > 2 else None
        return api.debug_query(issue, agent)

    if command == "root_cause_analysis":
        if len(argv_without_human) < 2:
            return result_error("missing_argument", "root_cause_analysis requires <symptom>")
        symptom = argv_without_human[1]
        days = 30
        if len(argv_without_human) > 2:
            parsed = _parse_int(argv_without_human[2], "days")
            if not parsed.ok:
                return parsed
            days = parsed.data
        agent = argv_without_human[3] if len(argv_without_human) > 3 else None
        return api.root_cause_analysis(symptom, days, agent)

    return result_error("unknown_command", f"Unknown command: {command}", {"context": {"command": command}})


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    human, argv_without_human = parse_human_flag(args)

    commands = [
        "show_patterns <query> [min_quality] [limit] [agent]",
        "pattern_usage <pattern_id> [agent]",
        "find_tag_patterns <tag1,tag2,...> [agent]",
        "get_tag_frequency <tag> [days] [agent]",
        "show_recurring_issues [threshold] [agent]",
        "find_similar_reports <query> [limit] [agent]",
        "get_report_findings [days] [agent]",
        "extract_prevention_rules <component> [agent]",
        "debug_query <issue> [agent]",
        "root_cause_analysis <symptom> [days] [agent]",
    ]

    if len(argv_without_human) < 1:
        if human:
            emit_human("VNX Intelligence Query System v1.0.0")
            emit_human("\nCommands:")
            for command in commands:
                emit_human(f"  {command}")
        else:
            _emit_result(result_ok({"version": "1.0.0", "commands": commands}), human=False)
        return EXIT_OK

    try:
        api = IntelligenceQueryAPI()
    except Exception as exc:
        result = result_error("initialization_failed", f"Failed to initialize intelligence query API: {exc}")
        _emit_result(result, human)
        return result_exit_code(result, default_error_exit_code=EXIT_DEPENDENCY)

    try:
        result = _run_command(api, argv_without_human)
    except Exception as exc:  # pragma: no cover - defensive wrapper
        result = result_error("unexpected_error", str(exc))

    _emit_result(result, human)
    return result_exit_code(result, default_error_exit_code=EXIT_DEPENDENCY)


if __name__ == "__main__":
    raise SystemExit(main())
