"""Tests for the self-learning proposals read API.

GET /api/intelligence/learning-proposals surfaces operator-gated proposals from:
  - state/pending_skill_refinements.json
  - state/pending_rules.json
  - state/pending_archival.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import api_intelligence


def _mock_sd(tmp_path: Path):
    """Return a mock serve_dashboard namespace with state paths under tmp_path."""
    import types

    sd = types.SimpleNamespace()
    sd.DB_PATH = tmp_path / "quality_intelligence.db"
    sd.REPORTS_DIR = tmp_path / "unified_reports"
    sd.RECEIPTS_PATH = tmp_path / "t0_receipts.ndjson"
    return sd


def _write_skill_refinements(tmp_path: Path, proposals: list[dict]) -> Path:
    path = tmp_path / "pending_skill_refinements.json"
    path.write_text(
        json.dumps(
            {"generated_at": "2026-07-09T10:00:00Z", "threshold": 0.3, "proposals": proposals},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_pending_rules(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "pending_rules.json"
    path.write_text(
        json.dumps({"pending_rules": rules}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_pending_archival(tmp_path: Path, candidates: list[dict]) -> Path:
    path = tmp_path / "pending_archival.json"
    path.write_text(
        json.dumps({"pending_archival": candidates}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class TestLearningProposalsReadApi:
    def test_empty_state_returns_empty_list(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert result == {"proposals": []}

    def test_returns_skill_refinement_proposals(self, tmp_path):
        _write_skill_refinements(tmp_path, [
            {
                "id": "skillref-debugger-20260709",
                "role": "debugger",
                "skill_path": ".claude/skills/debugger/SKILL.md",
                "rework_rate": 0.45,
                "reworked_count": 5,
                "total_dispatches": 11,
                "diff": "--- a/.claude/skills/debugger/SKILL.md\n+++ b/.claude/skills/debugger/SKILL.md\n@@ -1 +1 @@\n",
                "rationale": "Role 'debugger' has a rework rate of 45%.",
                "operator_test": "1. Review the diff.",
                "status": "pending",
                "generated_at": "2026-07-09T10:00:00Z",
            }
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert len(result["proposals"]) == 1
        prop = result["proposals"][0]
        assert prop["id"] == "skillref-debugger-20260709"
        assert prop["type"] == "skill_refinement"
        assert prop["target"] == ".claude/skills/debugger/SKILL.md"
        assert prop["summary"] == "Role 'debugger' has a rework rate of 45%."
        assert prop["confidence"] == 0.45
        assert prop["created_at"] == "2026-07-09T10:00:00Z"
        assert prop["meta"]["role"] == "debugger"

    def test_returns_pending_rules(self, tmp_path):
        _write_pending_rules(tmp_path, [
            {
                "id": "rule-abc123",
                "created_at": "2026-07-09T09:00:00Z",
                "source": "learning_loop",
                "rule_type": "failure_prevention",
                "pattern": "Error pattern: agent not found",
                "terminal_constraint": "T1",
                "prevention": "Validate agent exists before dispatch.",
                "confidence": 0.6,
                "occurrence_count": 3,
                "status": "pending",
            }
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert len(result["proposals"]) == 1
        prop = result["proposals"][0]
        assert prop["id"] == "rule-abc123"
        assert prop["type"] == "rule"
        assert prop["target"] == "T1"
        assert prop["summary"] == "Error pattern: agent not found"
        assert prop["rationale"] == "Validate agent exists before dispatch."
        assert prop["confidence"] == 0.6
        assert prop["created_at"] == "2026-07-09T09:00:00Z"

    def test_returns_archival_candidates(self, tmp_path):
        _write_pending_archival(tmp_path, [
            {
                "pattern_id": "pattern-1",
                "title": "Old auth helper",
                "last_used": "2026-06-01T00:00:00Z",
                "confidence": 0.15,
                "used_count": 0,
                "ignored_count": 5,
                "reason": "Unused for 30+ days with confidence < 0.3",
                "queued_at": "2026-07-09T08:00:00Z",
                "status": "pending",
            },
            {
                "pattern_id": "pattern-2",
                "title": "Stale success pattern",
                "confidence": 0.2,
                "source_table": "success_patterns",
                "action": "supersede",
                "reason": "confidence_score < 0.3, older than 30 days",
                "queued_at": "2026-07-09T08:30:00Z",
                "status": "pending",
            },
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert len(result["proposals"]) == 2
        # Newest first per created_at ordering
        supersede, archive = result["proposals"]
        assert archive["type"] == "archival"
        assert archive["target"] == "Old auth helper"
        assert archive["meta"]["action"] == "archive"
        assert supersede["type"] == "archival"
        assert supersede["target"] == "Stale success pattern"
        assert supersede["meta"]["action"] == "supersede"
        assert supersede["rationale"] == "supersede: confidence_score < 0.3, older than 30 days"

    def test_non_pending_status_is_filtered(self, tmp_path):
        _write_pending_rules(tmp_path, [
            {
                "id": "rule-approved",
                "created_at": "2026-07-09T09:00:00Z",
                "pattern": "approved pattern",
                "prevention": "prevention text",
                "confidence": 0.6,
                "status": "approved",
            },
            {
                "id": "rule-pending",
                "created_at": "2026-07-09T09:00:00Z",
                "pattern": "pending pattern",
                "prevention": "prevention text",
                "confidence": 0.6,
                "status": "pending",
            },
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert len(result["proposals"]) == 1
        assert result["proposals"][0]["id"] == "rule-pending"

    def test_missing_files_are_tolerant(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert result == {"proposals": []}

    def test_malformed_json_returns_empty(self, tmp_path):
        (tmp_path / "pending_rules.json").write_text("not json", encoding="utf-8")
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_learning_proposals({})

        assert result == {"proposals": []}
