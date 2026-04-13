"""Tests for intelligence API endpoints (F49 PR-1).

Covers: patterns, injections, classifications, dispatch-outcomes, transcript.
Each test verifies the response shape and graceful empty-DB behaviour.
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
        CREATE TABLE coordination_events (
            id INTEGER PRIMARY KEY,
            event_type TEXT,
            timestamp TEXT,
            dispatch_id TEXT,
            items_injected INTEGER,
            items_suppressed INTEGER
        );
        """
    )
    con.commit()
    con.close()
    return db_path


def _mock_sd(tmp_path: Path, db_path: Path | None = None, receipts_lines: list[str] | None = None):
    """Return a mock serve_dashboard module namespace."""
    import types

    sd = types.SimpleNamespace()
    sd.DB_PATH = db_path or (tmp_path / "missing.db")
    sd.REPORTS_DIR = tmp_path / "unified_reports"
    sd.RECEIPTS_PATH = tmp_path / "t0_receipts.ndjson"

    if receipts_lines is not None:
        sd.RECEIPTS_PATH.write_text("\n".join(receipts_lines), encoding="utf-8")

    return sd


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


class TestPatternsEndpoint:
    def test_empty_db_returns_empty_arrays(self, tmp_path):
        db_path = _make_db(tmp_path)
        sd = _mock_sd(tmp_path, db_path=db_path)

        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({})

        assert result == {"success_patterns": [], "antipatterns": []}

    def test_returns_success_patterns(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute(
            "INSERT INTO success_patterns VALUES (1, 'Test Pattern', 0.85, 'crawler', 3, '2026-01-01')"
        )
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({})

        assert len(result["success_patterns"]) == 1
        pat = result["success_patterns"][0]
        assert pat["title"] == "Test Pattern"
        assert pat["confidence"] == 0.85
        assert pat["used_count"] == 3

    def test_returns_antipatterns(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute(
            "INSERT INTO antipatterns VALUES (1, 'Bad Pattern', 'medium', 5, '2026-01-01')"
        )
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({})

        assert len(result["antipatterns"]) == 1
        ap = result["antipatterns"][0]
        assert ap["severity"] == "medium"
        assert ap["occurrence_count"] == 5

    def test_missing_db_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)  # db_path points to missing.db
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({})

        assert result["success_patterns"] == []
        assert result["antipatterns"] == []

    def test_limit_param(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        for i in range(10):
            con.execute(
                "INSERT INTO success_patterns VALUES (?, ?, 0.5, 'cat', 1, NULL)",
                (i + 1, f"Pattern {i}"),
            )
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({"limit": ["3"]})

        assert len(result["success_patterns"]) <= 3

    def test_invalid_limit_uses_default(self, tmp_path):
        db_path = _make_db(tmp_path)
        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_patterns({"limit": ["notanumber"]})

        assert "success_patterns" in result


# ---------------------------------------------------------------------------
# Injections
# ---------------------------------------------------------------------------


class TestInjectionsEndpoint:
    def test_empty_db_returns_empty_array(self, tmp_path):
        db_path = _make_db(tmp_path)
        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_injections({})

        assert result == {"injections": []}

    def test_returns_injection_events(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute(
            "INSERT INTO coordination_events VALUES (1, 'dispatch_injection', '2026-01-01T10:00:00Z', 'disp-001', 3, 0)"
        )
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_injections({})

        assert len(result["injections"]) == 1
        inj = result["injections"][0]
        assert inj["dispatch_id"] == "disp-001"
        assert inj["items_injected"] == 3

    def test_skips_non_injection_events(self, tmp_path):
        db_path = _make_db(tmp_path)
        con = sqlite3.connect(str(db_path))
        con.execute(
            "INSERT INTO coordination_events VALUES (1, 'gate_passed', '2026-01-01T10:00:00Z', 'disp-002', 0, 0)"
        )
        con.commit()
        con.close()

        sd = _mock_sd(tmp_path, db_path=db_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_injections({})

        assert result["injections"] == []

    def test_missing_db_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_injections({})

        assert result["injections"] == []


# ---------------------------------------------------------------------------
# Classifications
# ---------------------------------------------------------------------------


class TestClassificationsEndpoint:
    def test_empty_dir_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = tmp_path / "reports"
        sd.REPORTS_DIR.mkdir()

        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({})

        assert result == {"classifications": []}

    def test_missing_dir_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = tmp_path / "nonexistent"

        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({})

        assert result == {"classifications": []}

    def test_parses_yaml_frontmatter(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "report_001.md").write_text(
            "---\nquality_score: 0.92\ncontent_type: implementation\ncomplexity: medium\nsummary: Test report\n---\n\n# Body",
            encoding="utf-8",
        )

        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = reports
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({})

        assert len(result["classifications"]) == 1
        cls = result["classifications"][0]
        assert cls["quality_score"] == "0.92"
        assert cls["content_type"] == "implementation"
        assert cls["complexity"] == "medium"
        assert cls["summary"] == "Test report"

    def test_parses_bold_markdown_fields(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "report_002.md").write_text(
            "# Report\n\n**Quality Score**: 0.85\n**Content Type**: review\n",
            encoding="utf-8",
        )

        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = reports
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({})

        assert len(result["classifications"]) == 1
        cls = result["classifications"][0]
        assert cls["quality_score"] == "0.85"

    def test_report_without_fields_returns_empty_strings(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "plain.md").write_text("# Just a plain report\n\nNo metadata here.", encoding="utf-8")

        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = reports
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({})

        assert len(result["classifications"]) == 1
        cls = result["classifications"][0]
        assert cls["quality_score"] == ""
        assert cls["report_file"] == "plain.md"

    def test_limit_param(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        for i in range(5):
            (reports / f"rpt_{i:02d}.md").write_text(f"# Report {i}", encoding="utf-8")

        sd = _mock_sd(tmp_path)
        sd.REPORTS_DIR = reports
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_classifications({"limit": ["2"]})

        assert len(result["classifications"]) <= 2


# ---------------------------------------------------------------------------
# Dispatch Outcomes
# ---------------------------------------------------------------------------


class TestDispatchOutcomesEndpoint:
    def test_missing_receipts_returns_empty(self, tmp_path):
        sd = _mock_sd(tmp_path)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_dispatch_outcomes({})

        assert result == {"outcomes": []}

    def test_parses_receipts(self, tmp_path):
        lines = [
            json.dumps({
                "dispatch_id": "disp-001",
                "terminal": "T1",
                "track": "A",
                "status": "success",
                "timestamp": "2026-01-01T10:00:00Z",
            }),
            json.dumps({
                "dispatch_id": "disp-002",
                "terminal": "T2",
                "track": "B",
                "status": "failure",
                "timestamp": "2026-01-01T11:00:00Z",
            }),
        ]
        sd = _mock_sd(tmp_path, receipts_lines=lines)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_dispatch_outcomes({})

        assert len(result["outcomes"]) == 2
        ids = {o["dispatch_id"] for o in result["outcomes"]}
        assert "disp-001" in ids
        assert "disp-002" in ids

    def test_skips_malformed_lines(self, tmp_path):
        lines = [
            "not-json",
            json.dumps({"dispatch_id": "ok-001", "status": "success"}),
            "",
        ]
        sd = _mock_sd(tmp_path, receipts_lines=lines)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_dispatch_outcomes({})

        assert len(result["outcomes"]) == 1
        assert result["outcomes"][0]["dispatch_id"] == "ok-001"

    def test_limit_param(self, tmp_path):
        lines = [
            json.dumps({"dispatch_id": f"disp-{i:03d}", "status": "success"})
            for i in range(20)
        ]
        sd = _mock_sd(tmp_path, receipts_lines=lines)
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_dispatch_outcomes({"limit": ["5"]})

        assert len(result["outcomes"]) <= 5


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


class TestTranscriptEndpoint:
    def test_missing_db_returns_404(self, tmp_path):
        missing = tmp_path / "missing.db"
        with patch.object(api_intelligence, "_CONV_DB_PATH", missing):
            payload, status = api_intelligence._intelligence_get_transcript("some-session")

        assert status == 404
        assert "error" in payload

    def test_unknown_session_returns_404(self, tmp_path):
        db_path = tmp_path / "conversation-index.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE conversations (session_id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TEXT)")
        con.commit()
        con.close()

        with patch.object(api_intelligence, "_CONV_DB_PATH", db_path):
            payload, status = api_intelligence._intelligence_get_transcript("nonexistent")

        assert status == 404
        assert payload["session_id"] == "nonexistent"

    def test_returns_messages_for_valid_session(self, tmp_path):
        db_path = tmp_path / "conversation-index.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE conversations (session_id TEXT PRIMARY KEY)")
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TEXT)"
        )
        con.execute("INSERT INTO conversations VALUES ('sess-abc')")
        con.execute("INSERT INTO messages VALUES (1, 'sess-abc', 'user', 'Hello', '2026-01-01T10:00:00Z')")
        con.execute("INSERT INTO messages VALUES (2, 'sess-abc', 'assistant', 'Hi there', '2026-01-01T10:00:01Z')")
        con.commit()
        con.close()

        with patch.object(api_intelligence, "_CONV_DB_PATH", db_path):
            payload, status = api_intelligence._intelligence_get_transcript("sess-abc")

        assert status == 200
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][1]["role"] == "assistant"

    def test_invalid_session_id_returns_400(self, tmp_path):
        db_path = tmp_path / "conversation-index.db"
        db_path.write_bytes(b"")  # file exists but is empty — irrelevant, path check first

        with patch.object(api_intelligence, "_CONV_DB_PATH", db_path):
            payload, status = api_intelligence._intelligence_get_transcript("../../etc/passwd")

        assert status == 400

    def test_empty_messages_returns_empty_list(self, tmp_path):
        db_path = tmp_path / "conversation-index.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE conversations (session_id TEXT PRIMARY KEY)")
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TEXT)"
        )
        con.execute("INSERT INTO conversations VALUES ('sess-xyz')")
        con.commit()
        con.close()

        with patch.object(api_intelligence, "_CONV_DB_PATH", db_path):
            payload, status = api_intelligence._intelligence_get_transcript("sess-xyz")

        assert status == 200
        assert payload["messages"] == []
