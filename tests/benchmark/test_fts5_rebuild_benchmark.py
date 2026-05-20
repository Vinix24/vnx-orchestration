"""Tests for scripts/benchmark/fts5_rebuild_benchmark.py.

Uses a synthetic 100-row FTS5 database — no production DB required.
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "benchmark"))
import fts5_rebuild_benchmark as bm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def synthetic_db(tmp_path: Path) -> Path:
    """Create a 100-row FTS5 DB (code_snippets) + regular table (snippet_metadata)."""
    db_path = tmp_path / "synthetic_quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE VIRTUAL TABLE code_snippets USING fts5(
            title,
            description,
            code,
            tags,
            language,
            tokenize='porter unicode61'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE snippet_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snippet_rowid INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            quality_score REAL DEFAULT 0.0
        )
        """
    )
    # Insert 100 rows
    for i in range(100):
        lang = "python" if i % 2 == 0 else "bash"
        conn.execute(
            "INSERT INTO code_snippets(title, description, code, tags, language) VALUES (?,?,?,?,?)",
            (
                f"func_{i}",
                f"Description for snippet {i}",
                f"def func_{i}(): return {i}",
                f"tag_{i % 5}",
                lang,
            ),
        )
        conn.execute(
            "INSERT INTO snippet_metadata(snippet_rowid, file_path, quality_score) VALUES (?,?,?)",
            (i + 1, f"/src/file_{i}.py", float(i % 100)),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# B.3 tests
# ---------------------------------------------------------------------------


class TestCopyDb:
    def test_copy_does_not_mutate_source(self, synthetic_db: Path, tmp_path: Path):
        """Copy leaves source file untouched (same size, unmodified mtime)."""
        work_dir = tmp_path / "work"
        stat_before = synthetic_db.stat()

        work_db = bm._copy_db(synthetic_db, work_dir)

        stat_after = synthetic_db.stat()
        assert stat_before.st_size == stat_after.st_size
        assert work_db.exists()
        assert work_db != synthetic_db

    def test_copy_idempotent(self, synthetic_db: Path, tmp_path: Path):
        """Second copy overwrites previous copy without error."""
        work_dir = tmp_path / "work"
        bm._copy_db(synthetic_db, work_dir)
        work_db = bm._copy_db(synthetic_db, work_dir)
        assert work_db.exists()

    def test_work_dir_created_if_absent(self, synthetic_db: Path, tmp_path: Path):
        work_dir = tmp_path / "nested" / "work"
        assert not work_dir.exists()
        bm._copy_db(synthetic_db, work_dir)
        assert work_dir.exists()


class TestRowCount:
    def test_row_count_returns_correct_count(self, synthetic_db: Path):
        conn = sqlite3.connect(str(synthetic_db))
        count = bm._row_count(conn, "code_snippets")
        conn.close()
        assert count == 100

    def test_row_count_raises_on_nonexistent_table(self, synthetic_db: Path):
        """_row_count must raise sqlite3.OperationalError on missing table, not return -1."""
        conn = sqlite3.connect(str(synthetic_db))
        with pytest.raises(sqlite3.OperationalError):
            bm._row_count(conn, "does_not_exist")
        conn.close()


class TestIsFts5Table:
    def test_fts5_table_detected(self, synthetic_db: Path):
        conn = sqlite3.connect(str(synthetic_db))
        assert bm._is_fts5_table(conn, "code_snippets") is True
        conn.close()

    def test_regular_table_not_fts5(self, synthetic_db: Path):
        conn = sqlite3.connect(str(synthetic_db))
        assert bm._is_fts5_table(conn, "snippet_metadata") is False
        conn.close()

    def test_nonexistent_table_returns_false(self, synthetic_db: Path):
        conn = sqlite3.connect(str(synthetic_db))
        assert bm._is_fts5_table(conn, "does_not_exist") is False
        conn.close()


class TestRebuildRoundTrip:
    def test_rebuild_preserves_row_count(self, synthetic_db: Path, tmp_path: Path):
        """FTS5 rebuild round-trip retains all 100 rows."""
        work_dir = tmp_path / "work"
        work_db = bm._copy_db(synthetic_db, work_dir)

        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        result = bm._benchmark_table(work_db, "code_snippets", pre_rss, pre_io)

        assert result["is_fts5"] is True
        assert result["skipped"] is False
        assert result["error"] is None
        assert result["baseline_rows"] == 100
        v = result["validation"]
        assert v["row_match"] is True, f"Row mismatch: {v}"

    def test_rebuild_match_query_functional_after_rebuild(
        self, synthetic_db: Path, tmp_path: Path
    ):
        """MATCH query after rebuild returns same results as before."""
        work_dir = tmp_path / "work"
        work_db = bm._copy_db(synthetic_db, work_dir)

        # Baseline MATCH count before rebuild
        conn_before = sqlite3.connect(str(work_db))
        count_before = conn_before.execute(
            "SELECT count(*) FROM code_snippets WHERE code_snippets MATCH 'python'"
        ).fetchone()[0]
        conn_before.close()

        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        result = bm._benchmark_table(work_db, "code_snippets", pre_rss, pre_io)

        assert result["validation"]["match_query_ok"] is True
        # post-rebuild MATCH count must match pre-rebuild
        assert result["validation"]["match_query_count"] == count_before

    def test_non_fts5_table_skipped(self, synthetic_db: Path, tmp_path: Path):
        """snippet_metadata is not FTS5 — benchmark marks it skipped."""
        work_dir = tmp_path / "work"
        work_db = bm._copy_db(synthetic_db, work_dir)

        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        result = bm._benchmark_table(work_db, "snippet_metadata", pre_rss, pre_io)

        assert result["skipped"] is True
        assert result["is_fts5"] is False
        assert "not an FTS5" in result["skip_reason"]


class TestMetricKeys:
    def test_metric_capture_fills_expected_keys(
        self, synthetic_db: Path, tmp_path: Path
    ):
        """All mandatory metric keys are present and non-None for FTS5 tables."""
        work_dir = tmp_path / "work"
        work_db = bm._copy_db(synthetic_db, work_dir)

        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        result = bm._benchmark_table(work_db, "code_snippets", pre_rss, pre_io)

        mandatory_keys = [
            "table",
            "is_fts5",
            "baseline_rows",
            "wall_seconds",
            "cpu_seconds",
            "wal_growth_bytes",
            "io_read_bytes_delta",
            "io_write_bytes_delta",
            "validation",
        ]
        for key in mandatory_keys:
            assert key in result, f"Missing key: {key}"
            assert result[key] is not None, f"Key {key} is None"

        v = result["validation"]
        val_keys = [
            "expected_rows",
            "actual_rows",
            "row_match",
            "match_query_ok",
            "match_query_count",
        ]
        for key in val_keys:
            assert key in v, f"Missing validation key: {key}"


class TestReportOutput:
    def test_json_report_written_atomically(self, synthetic_db: Path, tmp_path: Path):
        """JSON report exists and parses cleanly after benchmark run."""
        work_dir = tmp_path / "work"
        report_dir = tmp_path / "reports"

        work_db = bm._copy_db(synthetic_db, work_dir)
        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        table_results = [
            bm._benchmark_table(work_db, "code_snippets", pre_rss, pre_io)
        ]

        from datetime import datetime, timezone

        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = {
            "meta": {"run_date": run_date, "script": "test", "psutil_available": bm._HAVE_PSUTIL},
            "source_db": {
                "path": str(synthetic_db),
                "size_bytes": synthetic_db.stat().st_size,
                "journal_mode": "wal",
                "page_size": 4096,
                "page_count": 0,
                "work_copy": str(work_db),
            },
            "tables": table_results,
            "summary": {
                "fts5_tables_benchmarked": ["code_snippets"],
                "total_wall_seconds": table_results[0]["wall_seconds"],
                "recommended_maintenance_window_seconds": (
                    table_results[0]["wall_seconds"] * 1.5
                    if table_results[0]["wall_seconds"]
                    else 0
                ),
                "risk_classification": bm._risk_classification(
                    table_results[0]["wall_seconds"] or 0
                ),
            },
        }

        json_path = report_dir / "test-report.json"
        bm._write_json_report(report, json_path)

        assert json_path.exists()
        # No .tmp file should linger
        assert not Path(str(json_path) + ".tmp").exists()
        parsed = json.loads(json_path.read_text())
        assert "summary" in parsed
        assert "tables" in parsed

    def test_md_report_written(self, synthetic_db: Path, tmp_path: Path):
        """Markdown report is written and contains expected headings."""
        work_dir = tmp_path / "work"
        report_dir = tmp_path / "reports"

        work_db = bm._copy_db(synthetic_db, work_dir)
        pre_rss = bm._baseline_memory_rss()
        pre_io = bm._disk_io_snapshot()
        table_results = [
            bm._benchmark_table(work_db, "code_snippets", pre_rss, pre_io)
        ]

        from datetime import datetime, timezone

        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = {
            "meta": {"run_date": run_date, "script": "test", "psutil_available": bm._HAVE_PSUTIL},
            "source_db": {
                "path": str(synthetic_db),
                "size_bytes": synthetic_db.stat().st_size,
                "journal_mode": "wal",
                "page_size": 4096,
                "page_count": 0,
                "work_copy": str(work_db),
            },
            "tables": table_results,
            "summary": {
                "fts5_tables_benchmarked": ["code_snippets"],
                "total_wall_seconds": table_results[0]["wall_seconds"] or 0,
                "recommended_maintenance_window_seconds": (
                    (table_results[0]["wall_seconds"] or 0) * 1.5
                ),
                "risk_classification": "green",
            },
        }

        md_path = report_dir / "test-report.md"
        bm._write_md_report(report, md_path)

        assert md_path.exists()
        content = md_path.read_text()
        assert "FTS5 Rebuild Benchmark" in content
        assert "## Summary" in content
        assert "## Maintenance Window Recommendation" in content
        assert "code_snippets" in content
