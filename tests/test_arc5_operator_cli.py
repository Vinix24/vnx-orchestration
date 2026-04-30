"""Tests for ARC-5: operator advisory CLI integration.

Cases:
  A — vnx suggest review --weekly aggregates by target file correctly
  B — vnx insights surfaces F57 dispatch parameter data
  C — /api/operator/recommendations dashboard endpoint returns JSON
  D — empty t0_recommendations.json → graceful empty response
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/ and scripts/lib/ importable
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
_DASHBOARD_DIR = _REPO_ROOT / "dashboard"

for _p in (_SCRIPTS_DIR, _LIB_DIR, _DASHBOARD_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_edit(eid: int, target: str, status: str = "pending", confidence: float = 0.8,
               content: str = "add example") -> dict:
    return {
        "id": eid,
        "category": "memory",
        "status": status,
        "target": target,
        "action": "append",
        "content": content,
        "confidence": confidence,
        "evidence": "test evidence",
    }


def _make_history_entry(eid: int, target: str, applied_at: str, content: str = "applied line") -> dict:
    return {
        "id": eid,
        "category": "memory",
        "status": "applied",
        "target": target,
        "action": "append",
        "content": content,
        "confidence": 0.75,
        "evidence": "test evidence",
        "applied_at": applied_at,
    }


# ---------------------------------------------------------------------------
# Case A: --weekly aggregates correctly
# ---------------------------------------------------------------------------

class TestWeeklyAggregation:
    """vnx suggest review --weekly groups by target file and shows counts."""

    def test_weekly_groups_by_target(self, tmp_path, monkeypatch, capsys):
        import apply_suggested_edits as mod

        pending_path = tmp_path / "pending_edits.json"
        history_path = tmp_path / "edit_history.json"

        # Two pending edits targeting the same file
        pending_data = {
            "generated_at": datetime.now(_UTC).isoformat(),
            "edits": [
                _make_edit(1, "memory/MEMORY.md", content="pattern A"),
                _make_edit(2, "memory/MEMORY.md", content="pattern B"),
            ],
        }
        pending_path.write_text(json.dumps(pending_data), encoding="utf-8")

        # One history entry from yesterday targeting a different file
        yesterday = (datetime.now(_UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        history_data = [
            _make_history_entry(10, ".claude/CLAUDE.md", applied_at=yesterday, content="rule X"),
        ]
        history_path.write_text(json.dumps(history_data), encoding="utf-8")

        monkeypatch.setattr(mod, "PENDING_PATH", pending_path)
        monkeypatch.setattr(mod, "HISTORY_PATH", history_path)

        rc = mod.cmd_review_weekly()
        captured = capsys.readouterr()

        assert rc == 0
        assert "memory/MEMORY.md" in captured.out
        assert ".claude/CLAUDE.md" in captured.out
        # Multi-occurrence pattern label for the file with 2 entries
        assert "Pattern detected (2 occurrences, last 7d)" in captured.out

    def test_weekly_excludes_old_history(self, tmp_path, monkeypatch, capsys):
        import apply_suggested_edits as mod

        pending_path = tmp_path / "pending_edits.json"
        history_path = tmp_path / "edit_history.json"

        pending_path.write_text(json.dumps({"generated_at": "", "edits": []}), encoding="utf-8")

        # History entry from 10 days ago — should be excluded
        old_ts = (datetime.now(_UTC) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        history_data = [
            _make_history_entry(99, "scripts/foo.py", applied_at=old_ts),
        ]
        history_path.write_text(json.dumps(history_data), encoding="utf-8")

        monkeypatch.setattr(mod, "PENDING_PATH", pending_path)
        monkeypatch.setattr(mod, "HISTORY_PATH", history_path)

        rc = mod.cmd_review_weekly()
        captured = capsys.readouterr()

        assert rc == 0
        assert "scripts/foo.py" not in captured.out
        assert "No suggestions" in captured.out

    def test_weekly_shows_batch_accept_hint(self, tmp_path, monkeypatch, capsys):
        import apply_suggested_edits as mod

        pending_path = tmp_path / "pending_edits.json"
        history_path = tmp_path / "edit_history.json"

        pending_data = {
            "generated_at": "",
            "edits": [
                _make_edit(3, "skills/backend.md"),
                _make_edit(4, "skills/backend.md"),
            ],
        }
        pending_path.write_text(json.dumps(pending_data), encoding="utf-8")
        history_path.write_text("[]", encoding="utf-8")

        monkeypatch.setattr(mod, "PENDING_PATH", pending_path)
        monkeypatch.setattr(mod, "HISTORY_PATH", history_path)

        rc = mod.cmd_review_weekly()
        captured = capsys.readouterr()

        assert rc == 0
        assert "vnx suggest accept" in captured.out
        assert "3,4" in captured.out or "4,3" in captured.out


# ---------------------------------------------------------------------------
# Case B: vnx insights shows F57 data
# ---------------------------------------------------------------------------

class TestVnxInsights:
    """vnx insights surfaces F57 dispatch parameter and behavioral signals."""

    def test_insights_low_data_returns_message(self, tmp_path, monkeypatch, capsys):
        import vnx_insights_cli as mod

        monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(mod, "DB_PATH", tmp_path / "quality_intelligence.db")

        rc = mod.main.__wrapped__() if hasattr(mod.main, "__wrapped__") else None

        # Run via _collect_all_insights instead of main() to avoid argparse
        data = mod._collect_all_insights()

        assert "parameter_insights" in data
        assert isinstance(data["parameter_insights"], list)
        assert len(data["parameter_insights"]) > 0
        # Low-data message when DB absent
        assert any("Insufficient" in s or "unavailable" in s or "error" in s
                   for s in data["parameter_insights"])

    def test_insights_context_signals_with_db(self, tmp_path, monkeypatch):
        import sqlite3
        import vnx_insights_cli as mod

        db_path = tmp_path / "quality_intelligence.db"
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(mod, "DB_PATH", db_path)

        # Build a minimal dispatch_experiments table with enough signal
        con = sqlite3.connect(str(db_path))
        con.execute("""
            CREATE TABLE dispatch_experiments (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT,
                terminal TEXT,
                role TEXT,
                context_items INTEGER,
                success BOOLEAN,
                cqs REAL
            )
        """)
        # T2 with test-engineer role has high context (avg=10 vs baseline~4)
        for i in range(5):
            con.execute(
                "INSERT INTO dispatch_experiments (dispatch_id, terminal, role, context_items) VALUES (?,?,?,?)",
                (f"t2-{i}", "T2", "test-engineer", 10),
            )
        for i in range(5):
            con.execute(
                "INSERT INTO dispatch_experiments (dispatch_id, terminal, role, context_items) VALUES (?,?,?,?)",
                (f"t1-{i}", "T1", "backend-developer", 4),
            )
        con.commit()
        con.close()

        signals = mod._get_context_load_signals()

        # T2/test-engineer should be flagged as having extra context vs T1/backend-developer
        assert any("T2" in s and "extra" in s for s in signals)

    def test_insights_json_output(self, tmp_path, monkeypatch, capsys):
        import vnx_insights_cli as mod

        monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(mod, "DB_PATH", tmp_path / "missing.db")

        data = mod._collect_all_insights()
        output = json.dumps(data, indent=2)
        parsed = json.loads(output)

        assert "parameter_insights" in parsed
        assert "context_load_signals" in parsed
        assert "behavioral" in parsed


# ---------------------------------------------------------------------------
# Case C: dashboard endpoint returns JSON
# ---------------------------------------------------------------------------

class TestDashboardRecommendationsEndpoint:
    """GET /api/operator/recommendations returns structured JSON."""

    def test_returns_recommendations_with_counts(self, tmp_path, monkeypatch):
        import api_recommendations as mod

        recs_file = tmp_path / "t0_recommendations.json"
        recs_file.write_text(
            json.dumps({
                "timestamp": "2026-04-30T10:00:00",
                "engine_version": "1.1.0",
                "recommendations": [
                    {"trigger": "task_success", "action": "create_dispatch", "priority": "P1"},
                    {"trigger": "task_failure", "action": "create_dispatch", "priority": "P0"},
                    {"trigger": "pr_ready", "action": "start_next_pr", "priority": "P1"},
                ],
                "active_conflicts": {},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "RECOMMENDATIONS_FILE", recs_file)

        result = mod.get_operator_recommendations()

        assert result["total"] == 3
        assert result["total_p0"] == 1
        assert result["total_p1"] == 2
        assert result["total_p2"] == 0
        assert result["engine_version"] == "1.1.0"
        assert isinstance(result["recommendations"], list)
        assert len(result["recommendations"]) == 3

    def test_returns_counts_by_priority(self, tmp_path, monkeypatch):
        import api_recommendations as mod

        recs_file = tmp_path / "t0_recommendations.json"
        recs_file.write_text(
            json.dumps({
                "recommendations": [
                    {"priority": "P0"},
                    {"priority": "P0"},
                    {"priority": "P2"},
                ],
                "active_conflicts": {"config.yaml": ["d1"]},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "RECOMMENDATIONS_FILE", recs_file)

        result = mod.get_operator_recommendations()

        assert result["total_p0"] == 2
        assert result["total_p2"] == 1
        assert result["active_conflicts"] == {"config.yaml": ["d1"]}


# ---------------------------------------------------------------------------
# Case D: empty / missing t0_recommendations → graceful empty response
# ---------------------------------------------------------------------------

class TestEmptyRecommendationsGraceful:
    """Absent or broken recommendations file returns a safe empty response."""

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import api_recommendations as mod

        monkeypatch.setattr(mod, "RECOMMENDATIONS_FILE", tmp_path / "nonexistent.json")

        result = mod.get_operator_recommendations()

        assert result["total"] == 0
        assert result["recommendations"] == []
        assert result["active_conflicts"] == {}
        assert result["timestamp"] is None
        assert "error" not in result

    def test_malformed_json_returns_error_key(self, tmp_path, monkeypatch):
        import api_recommendations as mod

        recs_file = tmp_path / "t0_recommendations.json"
        recs_file.write_text("{broken json", encoding="utf-8")
        monkeypatch.setattr(mod, "RECOMMENDATIONS_FILE", recs_file)

        result = mod.get_operator_recommendations()

        assert result["total"] == 0
        assert result["recommendations"] == []
        assert result.get("error") == "parse_error"

    def test_empty_recommendations_list(self, tmp_path, monkeypatch):
        import api_recommendations as mod

        recs_file = tmp_path / "t0_recommendations.json"
        recs_file.write_text(
            json.dumps({"recommendations": [], "active_conflicts": {}, "timestamp": "2026-04-30"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "RECOMMENDATIONS_FILE", recs_file)

        result = mod.get_operator_recommendations()

        assert result["total"] == 0
        assert result["total_p0"] == 0
        assert result["recommendations"] == []
        assert "error" not in result
