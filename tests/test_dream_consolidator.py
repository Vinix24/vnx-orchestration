"""Tests for scripts/dream/consolidator.py (ADR-019 auto-dream memory consolidation).

Coverage:
- test_emit_dream_event_writes_ndjson: NDJSON-first emit (ADR-005)
- test_fetch_patterns_respects_project_id: tenant isolation (ADR-007)
- test_parse_kimi_response_strict_json: response JSON extraction
- test_dry_run_no_db_write: dry-run skips dream_cycles INSERT
- test_run_dream_cycle_happy_path: full cycle with mocked kimi
- GAP-7 receipt-completeness preflight: stale/empty → skip with warning event
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))

import consolidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_cycles (
    cycle_id          TEXT    NOT NULL,
    project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
    started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed','failed','reviewed','rejected')),
    provider          TEXT    NOT NULL DEFAULT 'kimi',
    insights_input    INTEGER NOT NULL DEFAULT 0,
    merged_count      INTEGER NOT NULL DEFAULT 0,
    dropped_count     INTEGER NOT NULL DEFAULT 0,
    archived_count    INTEGER NOT NULL DEFAULT 0,
    flagged_count     INTEGER NOT NULL DEFAULT 0,
    operator_reviewed INTEGER NOT NULL DEFAULT 0,
    report_path       TEXT,
    PRIMARY KEY (cycle_id, project_id)
);
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL DEFAULT 'approach',
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL DEFAULT 'approach',
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    why_problematic TEXT NOT NULL DEFAULT 'bad',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_FAKE_CONSOLIDATION = {
    "merged": [],
    "dropped": [{"id": 1, "table": "antipatterns", "reason": "stale_30d"}],
    "archived": [],
    "flagged": [{"id": 1, "table": "success_patterns", "reason": "novel"}],
    "summary": "Dropped 1 stale antipattern. Flagged 1 novel success pattern for review.",
}

_KIMI_STREAM_JSON = (
    '{"event_type":"TurnBegin"}\n'
    '{"event_type":"ContentPart","content":"Here is the JSON:\\n"}\n'
    '{"event_type":"ContentPart","content":"{\\"merged\\":[],\\"dropped\\":[],'
    '\\"archived\\":[],\\"flagged\\":[],\\"summary\\":\\"ok\\"}"}\n'
    '{"event_type":"complete"}\n'
)


def _make_in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DREAM_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmitDreamEvent:
    def test_emit_dream_event_writes_ndjson(self, tmp_path):
        """NDJSON event is written to events/dream/<date>.ndjson (ADR-005)."""
        event = {
            "event_type": "dream_cycle_started",
            "cycle_id": "dream-test-001",
            "project_id": "vnx-dev",
            "timestamp": "2026-05-29T00:00:00+00:00",
        }
        with patch("consolidator.resolve_project_root", return_value=tmp_path):
            consolidator._emit_dream_event(event)

        ndjson_files = list((tmp_path / ".vnx-data" / "events" / "dream").glob("*.ndjson"))
        assert len(ndjson_files) == 1

        lines = ndjson_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "dream_cycle_started"
        assert parsed["cycle_id"] == "dream-test-001"

    def test_emit_dream_event_appends(self, tmp_path):
        """Multiple emits append to the same NDJSON file, not overwrite."""
        with patch("consolidator.resolve_project_root", return_value=tmp_path):
            consolidator._emit_dream_event({"event_type": "a"})
            consolidator._emit_dream_event({"event_type": "b"})

        ndjson_files = list((tmp_path / ".vnx-data" / "events" / "dream").glob("*.ndjson"))
        lines = ndjson_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "a"
        assert json.loads(lines[1])["event_type"] == "b"


class TestFetchPatterns:
    def test_fetch_patterns_respects_project_id(self):
        """Only rows for the given project_id are returned (ADR-007 tenant isolation)."""
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
            ("vnx-dev", "pattern-A"),
        )
        conn.execute(
            "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
            ("other-project", "pattern-B"),
        )
        conn.commit()

        result = consolidator._fetch_patterns(conn, "vnx-dev")

        assert len(result["success_patterns"]) == 1
        assert result["success_patterns"][0]["title"] == "pattern-A"

    def test_fetch_patterns_missing_table_returns_empty(self):
        """Tables that don't exist (e.g. intelligence_injections absent) return []."""
        conn = _make_in_memory_db()
        result = consolidator._fetch_patterns(conn, "vnx-dev")
        assert result["intelligence_injections"] == []

    def test_fetch_patterns_returns_dicts(self):
        """Rows are returned as plain dicts (JSON-serializable)."""
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO antipatterns (project_id, title) VALUES (?, ?)",
            ("vnx-dev", "bad-pattern"),
        )
        conn.commit()
        result = consolidator._fetch_patterns(conn, "vnx-dev")
        row = result["antipatterns"][0]
        assert isinstance(row, dict)
        assert row["title"] == "bad-pattern"


class TestParseKimiResponse:
    def test_parse_kimi_response_strict_json(self):
        """Extracts first complete JSON object from text with preamble/postamble."""
        text = 'Sure! Here you go:\n{"merged":[],"dropped":[],"archived":[],"flagged":[],"summary":"ok"}\nDone.'
        result = consolidator._parse_kimi_response(text)
        assert result == {
            "merged": [],
            "dropped": [],
            "archived": [],
            "flagged": [],
            "summary": "ok",
        }

    def test_parse_kimi_response_no_json_raises(self):
        """Raises ValueError when no JSON object is found in the output."""
        with pytest.raises(ValueError, match="No JSON found"):
            consolidator._parse_kimi_response("no braces here at all")

    def test_parse_kimi_response_invalid_json_raises(self):
        """Raises ValueError on malformed JSON."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            consolidator._parse_kimi_response("{not: valid json}")


class TestExtractKimiText:
    def test_extracts_content_part_events(self):
        """ContentPart events are concatenated in order."""
        stdout = (
            '{"event_type":"TurnBegin"}\n'
            '{"event_type":"ContentPart","content":"Hello "}\n'
            '{"event_type":"ContentPart","content":"World"}\n'
            '{"event_type":"complete"}\n'
        )
        result = consolidator._extract_kimi_text(stdout)
        assert result == "Hello World"

    def test_empty_stdout_returns_empty_string(self):
        assert consolidator._extract_kimi_text("") == ""

    def test_no_content_parts_returns_empty(self):
        stdout = '{"event_type":"TurnBegin"}\n{"event_type":"complete"}\n'
        assert consolidator._extract_kimi_text(stdout) == ""


def _make_fresh_receipts(tmp_path: Path) -> None:
    """Create a fresh processed receipt so the GAP-7 preflight passes."""
    processed = tmp_path / ".vnx-data" / "receipts" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "receipt-fresh.ndjson").write_text("{}", encoding="utf-8")


class TestDryRun:
    def test_dry_run_no_db_write(self, tmp_path):
        """Dry-run emits NDJSON and writes pending-review.json but skips dream_cycles INSERT."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(tmp_path)

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch(
                "consolidator._dispatch_kimi_consolidation",
                return_value=_FAKE_CONSOLIDATION,
            ),
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=True)

        # dream_cycles table must be empty (no INSERT in dry-run)
        conn = sqlite3.connect(str(db_path))
        row_count = conn.execute("SELECT COUNT(*) FROM dream_cycles").fetchone()[0]
        conn.close()
        assert row_count == 0

        # pending-review.json must exist
        review_path = Path(result["review_path"])
        assert review_path.exists()
        review = json.loads(review_path.read_text())
        assert review["requires_operator_review"] is True

    def test_happy_path_db_write(self, tmp_path):
        """Non-dry-run inserts one row into dream_cycles after emitting NDJSON."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(tmp_path)

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch(
                "consolidator._dispatch_kimi_consolidation",
                return_value=_FAKE_CONSOLIDATION,
            ),
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT cycle_id, project_id, status, provider FROM dream_cycles"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        cycle_id, project_id, status, provider = rows[0]
        assert cycle_id == result["cycle_id"]
        assert project_id == "vnx-dev"
        assert status == "completed"
        assert provider == "kimi"

    def test_happy_path_ndjson_emitted_before_db(self, tmp_path):
        """NDJSON dream_cycle_completed event is written (ADR-005 emit-first)."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(tmp_path)

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch(
                "consolidator._dispatch_kimi_consolidation",
                return_value=_FAKE_CONSOLIDATION,
            ),
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=False)

        event_files = list((tmp_path / ".vnx-data" / "events" / "dream").glob("*.ndjson"))
        assert len(event_files) == 1

        events = [
            json.loads(line)
            for line in event_files[0].read_text().strip().splitlines()
        ]
        event_types = [e["event_type"] for e in events]
        assert "dream_cycle_started" in event_types
        assert "dream_cycle_completed" in event_types
        # started must appear before completed
        assert event_types.index("dream_cycle_started") < event_types.index(
            "dream_cycle_completed"
        )


# ---------------------------------------------------------------------------
# GAP-7 receipt-completeness preflight tests
# ---------------------------------------------------------------------------


class TestCheckReceiptCompleteness:
    def test_missing_processed_dir_returns_false(self, tmp_path):
        """No receipts/processed dir → incomplete."""
        data_root = tmp_path / ".vnx-data"
        ok, reason = consolidator._check_receipt_completeness(data_root)
        assert not ok
        assert "absent" in reason

    def test_empty_processed_dir_returns_false(self, tmp_path):
        """Empty receipts/processed dir → incomplete."""
        (tmp_path / ".vnx-data" / "receipts" / "processed").mkdir(parents=True)
        ok, reason = consolidator._check_receipt_completeness(tmp_path / ".vnx-data")
        assert not ok
        assert "empty" in reason

    def test_fresh_receipt_returns_true(self, tmp_path):
        """A recently modified receipt file → complete."""
        processed = tmp_path / ".vnx-data" / "receipts" / "processed"
        processed.mkdir(parents=True)
        (processed / "receipt-001.ndjson").write_text("{}", encoding="utf-8")
        ok, reason = consolidator._check_receipt_completeness(tmp_path / ".vnx-data")
        assert ok
        assert reason == "ok"

    def test_stale_receipts_return_false(self, tmp_path):
        """All receipts older than max_age_hours → incomplete."""
        import time
        processed = tmp_path / ".vnx-data" / "receipts" / "processed"
        processed.mkdir(parents=True)
        old_file = processed / "receipt-old.ndjson"
        old_file.write_text("{}", encoding="utf-8")
        # Set mtime to 200 hours ago
        old_ts = time.time() - (200 * 3600)
        import os
        os.utime(str(old_file), (old_ts, old_ts))

        ok, reason = consolidator._check_receipt_completeness(
            tmp_path / ".vnx-data", max_age_hours=48
        )
        assert not ok
        assert "stale" in reason


class TestRunDreamCycleReceiptPreflight:
    def test_skips_when_no_receipts(self, tmp_path):
        """run_dream_cycle returns status=skipped when receipts/processed absent."""
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        with patch("consolidator.resolve_project_root", return_value=tmp_path):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=False)

        assert result["status"] == "skipped"
        assert result["reason"] == "incomplete_data"

    def test_skip_emits_ndjson_warning_event(self, tmp_path):
        """Skipped cycle emits dream_cycle_skipped NDJSON event (ADR-005)."""
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        with patch("consolidator.resolve_project_root", return_value=tmp_path):
            consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=False)

        event_files = list((tmp_path / ".vnx-data" / "events" / "dream").glob("*.ndjson"))
        assert len(event_files) == 1
        lines = [json.loads(l) for l in event_files[0].read_text().strip().splitlines()]
        assert any(e["event_type"] == "dream_cycle_skipped" for e in lines)
        skipped = next(e for e in lines if e["event_type"] == "dream_cycle_skipped")
        assert skipped["reason"] == "incomplete_data"

    def test_skip_does_not_call_kimi(self, tmp_path):
        """Skipped cycle must not invoke kimi consolidation."""
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch("consolidator._dispatch_kimi_consolidation") as mock_kimi,
        ):
            consolidator.run_dream_cycle("vnx-dev", db_path)

        mock_kimi.assert_not_called()

    def test_proceeds_when_fresh_receipts_present(self, tmp_path):
        """run_dream_cycle proceeds normally when fresh receipts exist."""
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        # Create a fresh receipt file
        processed = tmp_path / ".vnx-data" / "receipts" / "processed"
        processed.mkdir(parents=True)
        (processed / "receipt-fresh.ndjson").write_text("{}", encoding="utf-8")

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch(
                "consolidator._dispatch_kimi_consolidation",
                return_value=_FAKE_CONSOLIDATION,
            ),
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=True)

        assert result.get("status") != "skipped"
        assert "cycle_id" in result
