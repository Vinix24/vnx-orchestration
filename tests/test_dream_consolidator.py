"""Tests for scripts/dream/consolidator.py (ADR-019 auto-dream memory consolidation).

Coverage:
- test_emit_dream_event_writes_ndjson: NDJSON-first emit (ADR-005)
- test_fetch_patterns_respects_project_id: tenant isolation (ADR-007)
- test_parse_kimi_response_strict_json: response JSON extraction
- test_dry_run_no_db_write: dry-run skips dream_cycles INSERT
- test_run_dream_cycle_happy_path: full cycle with mocked kimi
- GAP-7 receipt-completeness preflight: stale/empty → skip with warning event
- TestCentralDataRootRespected: file I/O uses data_root param, not source-repo .vnx-data
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
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
        data_root = tmp_path / ".vnx-data"
        event = {
            "event_type": "dream_cycle_started",
            "cycle_id": "dream-test-001",
            "project_id": "vnx-dev",
            "timestamp": "2026-05-29T00:00:00+00:00",
        }
        consolidator._emit_dream_event(event, data_root)

        ndjson_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert len(ndjson_files) == 1

        lines = ndjson_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "dream_cycle_started"
        assert parsed["cycle_id"] == "dream-test-001"

    def test_emit_dream_event_appends(self, tmp_path):
        """Multiple emits append to the same NDJSON file, not overwrite."""
        data_root = tmp_path / ".vnx-data"
        consolidator._emit_dream_event({"event_type": "a"}, data_root)
        consolidator._emit_dream_event({"event_type": "b"}, data_root)

        ndjson_files = list((data_root / "events" / "dream").glob("*.ndjson"))
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


def _make_fresh_receipts(data_root: Path) -> None:
    """Create a fresh processed receipt so the GAP-7 preflight passes."""
    processed = data_root / "receipts" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "receipt-fresh.ndjson").write_text("{}", encoding="utf-8")


def _seed_pattern(db_path: Path) -> None:
    """Insert one success_pattern so the insufficient-data guard passes."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
        ("vnx-dev", "seeded-pattern"),
    )
    conn.commit()
    conn.close()


class TestDryRun:
    def test_dry_run_no_db_write(self, tmp_path):
        """Dry-run emits NDJSON and writes pending-review.json but skips dream_cycles INSERT."""
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(data_root)
        _seed_pattern(db_path)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

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
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(data_root)
        _seed_pattern(db_path)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=False, data_root=data_root
            )

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
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _make_fresh_receipts(data_root)
        _seed_pattern(db_path)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=False, data_root=data_root
            )

        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
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
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        result = consolidator.run_dream_cycle(
            "vnx-dev", db_path, dry_run=False, data_root=data_root
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "incomplete_data"

    def test_skip_emits_ndjson_warning_event(self, tmp_path):
        """Skipped cycle emits dream_cycle_skipped NDJSON event (ADR-005)."""
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        consolidator.run_dream_cycle(
            "vnx-dev", db_path, dry_run=False, data_root=data_root
        )

        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert len(event_files) == 1
        lines = [json.loads(l) for l in event_files[0].read_text().strip().splitlines()]
        assert any(e["event_type"] == "dream_cycle_skipped" for e in lines)
        skipped = next(e for e in lines if e["event_type"] == "dream_cycle_skipped")
        assert skipped["reason"] == "incomplete_data"

    def test_skip_does_not_call_kimi(self, tmp_path):
        """Skipped cycle must not invoke kimi consolidation."""
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        with patch("consolidator._dispatch_kimi_consolidation") as mock_kimi:
            consolidator.run_dream_cycle("vnx-dev", db_path, data_root=data_root)

        mock_kimi.assert_not_called()

    def test_proceeds_when_fresh_receipts_present(self, tmp_path):
        """run_dream_cycle proceeds normally when fresh receipts exist."""
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)
        conn = __import__("sqlite3").connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()
        _seed_pattern(db_path)
        _make_fresh_receipts(data_root)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        assert result.get("status") != "skipped"
        assert "cycle_id" in result


# ---------------------------------------------------------------------------
# Insufficient-data guard (empty DB with fresh receipts)
# ---------------------------------------------------------------------------


def _make_db_with_receipts(tmp_path: Path, *, with_patterns: bool = False) -> tuple[Path, Path]:
    """Return (db_path, data_root) with receipts ready. Optionally seed one success pattern."""
    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DREAM_SCHEMA)
    if with_patterns:
        conn.execute(
            "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
            ("vnx-dev", "a-pattern"),
        )
    conn.commit()
    conn.close()

    data_root = tmp_path / ".vnx-data"
    processed = data_root / "receipts" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "receipt-fresh.ndjson").write_text("{}", encoding="utf-8")
    return db_path, data_root


class TestInsufficientDataGuard:
    """Fresh receipts present but 0 core patterns → skip without calling kimi."""

    def test_empty_db_returns_skipped(self, tmp_path):
        """run_dream_cycle skips quickly when success_patterns + antipatterns = 0."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=False)

        with patch("consolidator._dispatch_kimi_consolidation") as mock_kimi:
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, data_root=data_root
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_data"
        mock_kimi.assert_not_called()

    def test_empty_db_emits_skipped_ndjson_event(self, tmp_path):
        """Skipped-on-empty emits dream_cycle_skipped NDJSON event (ADR-005)."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=False)

        with patch("consolidator._dispatch_kimi_consolidation"):
            consolidator.run_dream_cycle("vnx-dev", db_path, data_root=data_root)

        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert event_files, "No NDJSON event file written"
        events = [
            json.loads(line)
            for line in event_files[0].read_text().strip().splitlines()
        ]
        skipped = [e for e in events if e.get("event_type") == "dream_cycle_skipped"]
        assert skipped, "dream_cycle_skipped event not emitted"
        assert skipped[0]["reason"] == "insufficient_data"

    def test_non_empty_db_proceeds_to_kimi(self, tmp_path):
        """run_dream_cycle calls kimi when at least one success pattern exists."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=True)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ) as mock_kimi:
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        mock_kimi.assert_called_once()
        assert result.get("status") != "skipped"
        assert "cycle_id" in result


# ---------------------------------------------------------------------------
# Kimi timeout guard
# ---------------------------------------------------------------------------


class TestKimiTimeout:
    """Slow/stuck kimi must not hang the cycle indefinitely."""

    def _make_timeout_error(self):
        return subprocess.TimeoutExpired(cmd=["kimi"], timeout=1)

    def test_timeout_returns_timeout_status(self, tmp_path):
        """run_dream_cycle returns status='timeout' when kimi times out."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=True)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            side_effect=self._make_timeout_error(),
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, data_root=data_root
            )

        assert result["status"] == "timeout"
        assert result["reason"] == "kimi_timeout"
        assert "cycle_id" in result

    def test_timeout_emits_timeout_ndjson_event(self, tmp_path):
        """Timeout emits dream_cycle_timeout NDJSON event (ADR-005)."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=True)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            side_effect=self._make_timeout_error(),
        ):
            consolidator.run_dream_cycle("vnx-dev", db_path, data_root=data_root)

        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert event_files, "No NDJSON event file written"
        events = [
            json.loads(line)
            for line in event_files[0].read_text().strip().splitlines()
        ]
        timeout_events = [e for e in events if e.get("event_type") == "dream_cycle_timeout"]
        assert timeout_events, "dream_cycle_timeout event not emitted"

    def test_timeout_env_override(self, tmp_path, monkeypatch):
        """VNX_DREAM_KIMI_TIMEOUT env var is passed to _dispatch_kimi_consolidation."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=True)
        monkeypatch.setenv("VNX_DREAM_KIMI_TIMEOUT", "42")
        captured = {}

        def _capture_timeout(patterns, project_id, timeout=180.0):
            captured["timeout"] = timeout
            return _FAKE_CONSOLIDATION

        with patch("consolidator._dispatch_kimi_consolidation", side_effect=_capture_timeout):
            consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        assert captured["timeout"] == 42.0


# ---------------------------------------------------------------------------
# Entry-print: no more zero-output hangs
# ---------------------------------------------------------------------------


class TestEntryPrint:
    """run_dream_cycle prints cycle_id+project at entry — before any blocking call."""

    def test_entry_print_emitted_on_skipped_cycle(self, tmp_path, capsys):
        """Entry print fires even when the cycle skips due to missing receipts."""
        data_root = tmp_path / ".vnx-data"
        db_path = tmp_path / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        conn.commit()
        conn.close()

        consolidator.run_dream_cycle("vnx-dev", db_path, data_root=data_root)

        out = capsys.readouterr().out
        assert "dream run: cycle=" in out
        assert "vnx-dev" in out

    def test_entry_print_emitted_before_kimi_call(self, tmp_path, capsys):
        """Entry print fires before any kimi invocation — ordering guarantee."""
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=True)
        call_order: list[str] = []

        def _record_print(*args, **kwargs):
            call_order.append("print")

        def _fake_kimi(patterns, project_id, timeout=180.0):
            call_order.append("kimi")
            return _FAKE_CONSOLIDATION

        with (
            patch("builtins.print", side_effect=_record_print),
            patch("consolidator._dispatch_kimi_consolidation", side_effect=_fake_kimi),
        ):
            consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        assert "print" in call_order
        assert "kimi" in call_order
        assert call_order.index("print") < call_order.index("kimi")

    def test_empty_db_returns_skipped_under_5s(self, tmp_path):
        """Empty DB with fresh receipts must return in <5s — no kimi spawn."""
        import time
        db_path, data_root = _make_db_with_receipts(tmp_path, with_patterns=False)
        called = []

        def _no_kimi(*a, **kw):
            called.append(True)
            raise AssertionError("kimi must not be called on empty DB")

        t0 = time.monotonic()
        with patch("consolidator._dispatch_kimi_consolidation", side_effect=_no_kimi):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, data_root=data_root)
        elapsed = time.monotonic() - t0

        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_data"
        assert not called, "kimi was invoked on empty DB"
        assert elapsed < 5.0, f"took {elapsed:.2f}s — exceeded 5s threshold"


# ---------------------------------------------------------------------------
# Central data root regression: file I/O must use data_root, not source repo
# ---------------------------------------------------------------------------


class TestCentralDataRootRespected:
    """File I/O (events, pending-review, receipt-preflight) must use the injected
    data_root, not resolve_project_root(__file__)/.vnx-data (the VNX source repo).
    This is the core regression guard for the central-install blocker fix.
    ADR-007: canonical paths are project_id-scoped.
    """

    def _make_db(self, path: Path, seed_pattern: bool = True) -> Path:
        db_path = path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_DREAM_SCHEMA)
        if seed_pattern:
            conn.execute(
                "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
                ("vnx-dev", "p"),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_events_written_to_data_root_not_source_repo(self, tmp_path):
        """Events and pending-review.json go to data_root, not the source repo .vnx-data."""
        data_root = tmp_path / "central-data"
        db_path = self._make_db(tmp_path)

        _make_fresh_receipts(data_root)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        # Events must be in data_root
        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert len(event_files) == 1, "Events not written to data_root"

        # Pending-review must be under data_root
        review_path = Path(result["review_path"])
        assert str(data_root) in str(review_path), (
            f"review_path {review_path} not under data_root {data_root}"
        )

    def test_receipt_preflight_reads_from_data_root(self, tmp_path):
        """GAP-7 preflight reads receipts from data_root, not source-repo .vnx-data."""
        data_root = tmp_path / "central-data"
        other_dir = tmp_path / "source-repo" / ".vnx-data"

        # Fresh receipts ONLY in data_root
        _make_fresh_receipts(data_root)
        # other_dir exists but has no receipts — if code reads from it, preflight fails
        other_dir.mkdir(parents=True)

        db_path = self._make_db(tmp_path)

        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        # Cycle must NOT be skipped (receipts found in data_root, not source repo)
        assert result.get("status") != "skipped", (
            "Preflight incorrectly read from source repo or elsewhere"
        )
        assert "cycle_id" in result

    def test_skipped_event_written_to_data_root(self, tmp_path):
        """Skip event (missing receipts) is written to data_root, not source repo."""
        data_root = tmp_path / "central-data"
        # No receipts anywhere — cycle will skip
        db_path = self._make_db(tmp_path)

        consolidator.run_dream_cycle(
            "vnx-dev", db_path, dry_run=False, data_root=data_root
        )

        event_files = list((data_root / "events" / "dream").glob("*.ndjson"))
        assert len(event_files) == 1, "Skip event not written to data_root"
        events = [
            json.loads(line)
            for line in event_files[0].read_text().strip().splitlines()
        ]
        assert any(e["event_type"] == "dream_cycle_skipped" for e in events)

    def test_resolve_data_root_uses_vnx_paths_when_no_override(self, tmp_path):
        """_resolve_data_root(None) delegates to vnx_paths, not resolve_project_root."""
        import vnx_paths as _vnx_paths_mod
        sentinel = tmp_path / "vnx-paths-resolved"
        paths_return = {"VNX_DATA_DIR": str(sentinel), "PROJECT_ROOT": str(tmp_path)}
        with patch.object(_vnx_paths_mod, "resolve_paths", return_value=paths_return):
            result = consolidator._resolve_data_root(None)
        assert result == sentinel

    def test_resolve_data_root_explicit_override_wins(self, tmp_path):
        """_resolve_data_root(explicit_path) returns that path unchanged."""
        explicit = tmp_path / "my-override"
        result = consolidator._resolve_data_root(explicit)
        assert result == explicit
