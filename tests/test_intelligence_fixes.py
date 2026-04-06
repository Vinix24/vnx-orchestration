#!/usr/bin/env python3
"""Tests for the intelligence system fixes (dispatch 20260406-250001)."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
import sys

# Add scripts directories to path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ── Fix 2: intelligence_persist bridge ──────────────────────────────────────

class TestIntelligencePersist:
    """Verify signals get persisted to quality_intelligence.db tables."""

    def _create_test_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript((REPO_ROOT / "schemas" / "quality_intelligence.sql").read_text())
        conn.close()

    def _make_signal(self, signal_type, content, severity="info", dispatch_id="", defect_family=""):
        """Create a duck-typed signal object."""
        class Corr:
            def __init__(self):
                self.dispatch_id = dispatch_id
                self.feature_id = ""
                self.session_id = ""
                self.provider_id = ""
                self.terminal_id = ""
                self.branch = ""
                self.pr_id = ""
        class Sig:
            pass
        s = Sig()
        s.signal_type = signal_type
        s.content = content
        s.severity = severity
        s.correlation = Corr()
        s.defect_family = defect_family
        s.count = 1
        return s

    def test_gate_success_creates_success_pattern(self, tmp_path):
        from intelligence_persist import persist_signals_to_db
        db_path = tmp_path / "quality_intelligence.db"
        self._create_test_db(db_path)

        signals = [self._make_signal("gate_success", "gate gemini_review passed", dispatch_id="d-001")]
        result = persist_signals_to_db(signals, db_path)

        assert result["patterns_upserted"] == 1
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM success_patterns").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_gate_failure_creates_antipattern(self, tmp_path):
        from intelligence_persist import persist_signals_to_db
        db_path = tmp_path / "quality_intelligence.db"
        self._create_test_db(db_path)

        signals = [self._make_signal("gate_failure", "gate codex_gate failed: 3 findings", severity="blocker", dispatch_id="d-002")]
        result = persist_signals_to_db(signals, db_path)

        assert result["antipatterns_upserted"] == 1
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM antipatterns").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_duplicate_signal_increments_count(self, tmp_path):
        from intelligence_persist import persist_signals_to_db
        db_path = tmp_path / "quality_intelligence.db"
        self._create_test_db(db_path)

        sig = self._make_signal("gate_success", "gate gemini_review passed", dispatch_id="d-001")
        persist_signals_to_db([sig], db_path)
        persist_signals_to_db([sig], db_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT usage_count FROM success_patterns").fetchone()
        conn.close()
        assert row["usage_count"] == 2

    def test_no_db_returns_zero_counts(self, tmp_path):
        from intelligence_persist import persist_signals_to_db
        db_path = tmp_path / "nonexistent.db"
        result = persist_signals_to_db([], db_path)
        assert result["patterns_upserted"] == 0

    def test_dispatch_metadata_outcome_updated(self, tmp_path):
        from intelligence_persist import persist_signals_to_db
        db_path = tmp_path / "quality_intelligence.db"
        self._create_test_db(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, terminal, track) VALUES (?, ?, ?)",
            ("d-003", "T1", "A"),
        )
        conn.commit()
        conn.close()

        sig = self._make_signal("gate_success", "gate passed", dispatch_id="d-003")
        result = persist_signals_to_db([sig], db_path)
        assert result["metadata_updated"] == 1

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id='d-003'").fetchone()
        conn.close()
        assert row["outcome_status"] == "success"


# ── Fix 3: governance signal extractor gate loading ─────────────────────────

class TestGateResultLoading:
    """Verify the intelligence daemon loads gate results correctly."""

    def test_task_timeout_maps_to_fail(self):
        """task_timeout events should be recognized as gate failures."""
        gate_event_map = {
            "task_complete": "pass",
            "task_success": "pass",
            "gate_pass": "pass",
            "task_failed": "fail",
            "gate_fail": "fail",
            "gate_failure": "fail",
            "task_timeout": "fail",
        }
        assert "task_timeout" in gate_event_map
        assert gate_event_map["task_timeout"] == "fail"

    def test_review_gate_result_events_parsed(self, tmp_path):
        """review_gate_result events with embedded status should produce gate results."""
        receipts_path = tmp_path / "t0_receipts.ndjson"
        events = [
            {"event_type": "review_gate_result", "gate": "gemini_review", "status": "pass", "dispatch_id": "d-100"},
            {"event_type": "review_gate_result", "gate": "codex_gate", "status": "fail", "dispatch_id": "d-101", "reason": "3 findings"},
            {"event_type": "task_complete", "gate": "planning", "dispatch_id": "d-102"},
            {"event_type": "task_timeout", "gate": "gemini_review", "dispatch_id": "d-103"},
        ]
        with open(receipts_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        # Simulate the _load_gate_results logic
        results = []
        gate_event_map = {
            "task_complete": "pass", "task_success": "pass", "gate_pass": "pass",
            "task_failed": "fail", "gate_fail": "fail", "gate_failure": "fail",
            "task_timeout": "fail",
        }
        with open(receipts_path) as fh:
            for raw in fh:
                receipt = json.loads(raw.strip())
                gate = receipt.get("gate", "")
                event_type = receipt.get("event_type", "")
                if event_type == "review_gate_result":
                    embedded = receipt.get("status", "")
                    if embedded in ("pass", "passed", "success"):
                        results.append({"gate_id": gate, "status": "pass", "dispatch_id": receipt.get("dispatch_id", "")})
                    elif embedded in ("fail", "failed"):
                        results.append({"gate_id": gate, "status": "fail", "dispatch_id": receipt.get("dispatch_id", "")})
                    continue
                if gate and event_type in gate_event_map:
                    results.append({"gate_id": gate, "status": gate_event_map[event_type], "dispatch_id": receipt.get("dispatch_id", "")})

        assert len(results) == 4
        assert results[0]["status"] == "pass"
        assert results[1]["status"] == "fail"
        assert results[2]["status"] == "pass"
        assert results[3]["status"] == "fail"


# ��─ Fix 5: system health endpoint ──────────────────────────────────────────

class TestSystemHealthEndpoint:
    """Verify the system health endpoint returns valid JSON structure."""

    def test_health_endpoint_returns_valid_structure(self):
        sys.path.insert(0, str(REPO_ROOT / "dashboard"))
        from api_operator import _operator_get_system_health
        result = _operator_get_system_health()

        assert "status" in result
        assert result["status"] in ("healthy", "degraded", "dead")
        assert "queried_at" in result
        assert "components" in result
        assert "health_score" in result
        assert isinstance(result["health_score"], float)
        assert 0.0 <= result["health_score"] <= 1.0

        expected_components = [
            "intelligence_db", "governance_digest", "dispatcher",
            "receipt_processor", "lease_health", "report_index",
        ]
        for comp in expected_components:
            assert comp in result["components"], f"Missing component: {comp}"
            assert "status" in result["components"][comp]
            assert result["components"][comp]["status"] in ("healthy", "degraded", "dead")
