"""Tests for proposals API endpoints (F50 PR-1).

Covers: GET proposals, accept, reject, confidence-trends, weekly-digest read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import api_intelligence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Create a minimal quality_intelligence.db with all expected tables."""
    db_path = tmp_path / "quality_intelligence.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            confidence_score REAL,
            category TEXT,
            usage_count INTEGER,
            last_used TEXT
        );
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            severity TEXT,
            occurrence_count INTEGER,
            last_seen TEXT
        );
        """
    )
    con.commit()
    con.close()
    return db_path


def _mock_sd(tmp_path: Path, db_path: Path | None = None):
    """Return a mock serve_dashboard namespace with state paths under tmp_path."""
    import types
    sd = types.SimpleNamespace()
    sd.DB_PATH = db_path or (tmp_path / "quality_intelligence.db")
    sd.REPORTS_DIR = tmp_path / "unified_reports"
    sd.RECEIPTS_PATH = tmp_path / "t0_receipts.ndjson"
    return sd


def _write_pending(tmp_path: Path, edits: list[dict]) -> Path:
    path = tmp_path / "pending_edits.json"
    path.write_text(
        json.dumps({"generated_at": "2026-04-13T00:00:00Z", "edits": edits}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# GET /api/intelligence/proposals
# ---------------------------------------------------------------------------


class TestGetProposals:
    def test_empty_pending_returns_empty_list(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_proposals({})

        assert result == {"proposals": []}

    def test_missing_pending_edits_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)
        # pending_edits.json does not exist
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_proposals({})

        assert "proposals" in result
        assert result["proposals"] == []

    def test_returns_proposal_fields(self, tmp_path):
        _write_pending(tmp_path, [
            {
                "id": 1,
                "category": "memory",
                "content": "Add pattern X to MEMORY.md",
                "evidence": "5 sessions observed",
                "confidence": 0.85,
                "status": "pending",
                "suggested_at": "2026-04-13T00:00:00Z",
            }
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_proposals({})

        assert len(result["proposals"]) == 1
        prop = result["proposals"][0]
        assert prop["id"] == 1
        assert prop["category"] == "memory"
        assert prop["proposed_change"] == "Add pattern X to MEMORY.md"
        assert prop["evidence"] == "5 sessions observed"
        assert prop["confidence"] == 0.85
        assert prop["status"] == "pending"

    def test_multiple_proposals_all_returned(self, tmp_path):
        edits = [
            {"id": i, "category": "memory", "content": f"edit {i}",
             "evidence": "e", "confidence": 0.9, "status": "pending"}
            for i in range(1, 6)
        ]
        _write_pending(tmp_path, edits)
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_proposals({})

        assert len(result["proposals"]) == 5


# ---------------------------------------------------------------------------
# POST /api/intelligence/proposals/<id>/accept
# ---------------------------------------------------------------------------


class TestAcceptProposal:
    def test_accept_pending_proposal(self, tmp_path):
        _write_pending(tmp_path, [
            {"id": 1, "category": "memory", "content": "x",
             "evidence": "e", "confidence": 0.9, "status": "pending"}
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_accept_proposal("1")

        assert status == 200
        assert result["ok"] is True
        assert result["status"] == "accepted"

        # Verify file updated
        data = json.loads((tmp_path / "pending_edits.json").read_text())
        assert data["edits"][0]["status"] == "accepted"
        assert "accepted_at" in data["edits"][0]

    def test_accept_nonexistent_proposal_returns_404(self, tmp_path):
        _write_pending(tmp_path, [])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_accept_proposal("99")

        assert status == 404
        assert "error" in result

    def test_accept_already_accepted_returns_404(self, tmp_path):
        _write_pending(tmp_path, [
            {"id": 1, "category": "memory", "content": "x",
             "evidence": "e", "confidence": 0.9, "status": "accepted"}
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_accept_proposal("1")

        assert status == 404

    def test_accept_invalid_id_returns_400(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_accept_proposal("notanumber")

        assert status == 400


# ---------------------------------------------------------------------------
# POST /api/intelligence/proposals/<id>/reject
# ---------------------------------------------------------------------------


class TestRejectProposal:
    def test_reject_pending_proposal(self, tmp_path):
        _write_pending(tmp_path, [
            {"id": 2, "category": "rule", "content": "y",
             "evidence": "e", "confidence": 0.8, "status": "pending"}
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_reject_proposal("2", {"reason": "not applicable"})

        assert status == 200
        assert result["ok"] is True
        assert result["status"] == "rejected"

        data = json.loads((tmp_path / "pending_edits.json").read_text())
        edit = data["edits"][0]
        assert edit["status"] == "rejected"
        assert edit["reject_reason"] == "not applicable"
        assert "rejected_at" in edit

    def test_reject_without_reason(self, tmp_path):
        _write_pending(tmp_path, [
            {"id": 3, "category": "memory", "content": "z",
             "evidence": "e", "confidence": 0.75, "status": "pending"}
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_reject_proposal("3", {})

        assert status == 200
        data = json.loads((tmp_path / "pending_edits.json").read_text())
        assert data["edits"][0]["status"] == "rejected"
        assert "reject_reason" not in data["edits"][0]

    def test_reject_accepted_proposal_is_allowed(self, tmp_path):
        _write_pending(tmp_path, [
            {"id": 4, "category": "memory", "content": "w",
             "evidence": "e", "confidence": 0.9, "status": "accepted"}
        ])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_reject_proposal("4", {})

        assert status == 200

    def test_reject_nonexistent_returns_404(self, tmp_path):
        _write_pending(tmp_path, [])
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_reject_proposal("99", {})

        assert status == 404

    def test_reject_invalid_id_returns_400(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_reject_proposal("bad", {})

        assert status == 400


# ---------------------------------------------------------------------------
# GET /api/intelligence/confidence-trends
# ---------------------------------------------------------------------------


class TestConfidenceTrends:
    def test_empty_db_returns_empty_trends(self, tmp_path):
        db_path = _make_db(tmp_path)
        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_confidence_trends({})

        assert result == {"trends": []}

    def test_missing_db_returns_empty_trends(self, tmp_path):
        sd = _mock_sd(tmp_path)  # db does not exist
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_confidence_trends({})

        assert result == {"trends": []}

    def test_success_patterns_aggregated_by_date(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute("INSERT INTO success_patterns VALUES (1, 'P1', 0.8, 'cat', 1, '2026-04-10')")
        con.execute("INSERT INTO success_patterns VALUES (2, 'P2', 0.6, 'cat', 1, '2026-04-10')")
        con.execute("INSERT INTO success_patterns VALUES (3, 'P3', 0.9, 'cat', 1, '2026-04-11')")
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_confidence_trends({})

        assert len(result["trends"]) == 2
        by_date = {t["date"]: t for t in result["trends"]}
        assert "2026-04-10" in by_date
        assert by_date["2026-04-10"]["avg_success_confidence"] == pytest.approx(0.7, abs=0.001)
        assert by_date["2026-04-10"]["pattern_count"] == 2
        assert "2026-04-11" in by_date
        assert by_date["2026-04-11"]["avg_success_confidence"] == pytest.approx(0.9, abs=0.001)

    def test_antipatterns_included_in_trends(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute("INSERT INTO antipatterns VALUES (1, 'AP1', 'high', 3, '2026-04-12')")
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_confidence_trends({})

        assert len(result["trends"]) == 1
        t = result["trends"][0]
        assert t["date"] == "2026-04-12"
        assert t["avg_antipattern_severity"] == pytest.approx(0.75, abs=0.001)
        assert t["avg_success_confidence"] is None

    def test_trend_entry_has_required_fields(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute("INSERT INTO success_patterns VALUES (1, 'P1', 0.7, 'cat', 1, '2026-04-13')")
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_confidence_trends({})

        entry = result["trends"][0]
        assert "date" in entry
        assert "avg_success_confidence" in entry
        assert "avg_antipattern_severity" in entry
        assert "pattern_count" in entry


# ---------------------------------------------------------------------------
# GET /api/intelligence/weekly-digest
# ---------------------------------------------------------------------------


class TestWeeklyDigest:
    def test_missing_digest_returns_404(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_get_weekly_digest()

        assert status == 404
        assert "error" in result

    def test_returns_digest_contents(self, tmp_path):
        digest = {
            "generated_at": "2026-04-13T00:00:00Z",
            "period": {"start": "2026-04-06", "end": "2026-04-13", "days": 7},
            "metrics": {"patterns_learned": 3},
            "narrative": "3 patterns learned this week.",
        }
        (tmp_path / "weekly_digest.json").write_text(
            json.dumps(digest), encoding="utf-8"
        )
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_get_weekly_digest()

        assert status == 200
        assert result["narrative"] == "3 patterns learned this week."
        assert result["period"]["days"] == 7

    def test_malformed_digest_returns_500(self, tmp_path):
        (tmp_path / "weekly_digest.json").write_text("not-json", encoding="utf-8")
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result, status = api_intelligence._intelligence_get_weekly_digest()

        assert status == 500
        assert "error" in result


# ---------------------------------------------------------------------------
# weekly_digest.py — unit tests for metrics collection + narrative
# ---------------------------------------------------------------------------


class TestWeeklyDigestScript:
    """Test the weekly_digest.py script functions directly."""

    def test_collect_metrics_empty_state(self, tmp_path, monkeypatch):
        """collect_metrics returns baseline structure when DB/files are absent."""
        import scripts.weekly_digest as wd

        monkeypatch.setattr(wd, "DB_PATH", tmp_path / "missing.db")
        monkeypatch.setattr(wd, "RECEIPTS_PATH", tmp_path / "missing.ndjson")
        monkeypatch.setattr(wd, "PENDING_PATH", tmp_path / "missing.json")

        metrics = wd.collect_metrics(days=7)
        assert "patterns_learned" in metrics
        assert "dispatch_outcomes" in metrics
        assert metrics["patterns_learned"] == 0
        assert metrics["dispatch_outcomes"]["total"] == 0

    def test_template_narrative_structure(self):
        """_template_narrative returns a non-empty string with key metrics."""
        import scripts.weekly_digest as wd

        metrics = {
            "patterns_learned": 4,
            "avg_success_confidence": 0.82,
            "dispatch_outcomes": {"total": 10, "success": 8, "failure": 1, "unknown": 1},
            "pending_suggestions": 2,
            "antipatterns_active": 1,
        }
        narrative = wd._template_narrative(metrics, days=7)
        assert isinstance(narrative, str)
        assert len(narrative) > 0
        assert "4" in narrative  # patterns_learned

    def test_generate_narrative_dry_run_returns_template(self, monkeypatch):
        """generate_narrative with dry_run=True always uses template."""
        import scripts.weekly_digest as wd

        cli_called = []

        def _fake_cli(metrics, days, timeout=20):
            cli_called.append(True)
            return "LLM result"

        monkeypatch.setattr(wd, "_cli_narrative", _fake_cli)

        metrics = {
            "patterns_learned": 2,
            "avg_success_confidence": 0.9,
            "dispatch_outcomes": {"total": 5, "success": 4, "failure": 0, "unknown": 1},
            "pending_suggestions": 0,
            "antipatterns_active": 0,
        }
        narrative = wd.generate_narrative(metrics, days=7, dry_run=True)
        assert cli_called == []  # LLM not invoked in dry_run
        assert isinstance(narrative, str)
        assert len(narrative) > 0

    def test_build_digest_structure(self):
        """build_digest returns dict with required top-level keys."""
        import scripts.weekly_digest as wd

        metrics = {"patterns_learned": 1, "dispatch_outcomes": {}}
        digest = wd.build_digest(metrics, "Test narrative", days=7)

        assert "generated_at" in digest
        assert "period" in digest
        assert digest["period"]["days"] == 7
        assert "start" in digest["period"]
        assert "end" in digest["period"]
        assert digest["metrics"] is metrics
        assert digest["narrative"] == "Test narrative"

    def test_write_digest_creates_file(self, tmp_path, monkeypatch):
        """write_digest writes valid JSON to DIGEST_PATH."""
        import scripts.weekly_digest as wd

        digest_path = tmp_path / "weekly_digest.json"
        monkeypatch.setattr(wd, "STATE_DIR", tmp_path)
        monkeypatch.setattr(wd, "DIGEST_PATH", digest_path)

        digest = {"generated_at": "2026-04-13T00:00:00Z", "narrative": "ok"}
        wd.write_digest(digest)

        assert digest_path.exists()
        loaded = json.loads(digest_path.read_text())
        assert loaded["narrative"] == "ok"
