#!/usr/bin/env python3
"""Tests for shadow_logger — NDJSON divergence event writer.

Covers:
  - test_write_event_creates_ledger_if_missing
  - test_write_event_appends_not_overwrites
  - test_write_event_uses_lock (concurrent writes produce no malformed lines)
  - test_write_comparison_result_writes_all_divergences
  - test_write_event_serializes_dataclass_correctly (round-trip JSON)
  - test_no_lock_required_when_only_reading (reader does not block writer >1ms)
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import shadow_logger as sl  # noqa: E402
import shadow_verifier as sv  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_ledger(tmp_path: Path) -> Path:
    return tmp_path / "shadow_divergence.ndjson"


def _make_event(
    metric_id: int = 4,
    severity: str = sv.SEVERITY_SOFT,
    project_id: str = "proj_a",
    read_site: str = "test_site",
) -> sv.DivergenceEvent:
    return sv.DivergenceEvent(
        metric_id=metric_id,
        severity=severity,
        project_id=project_id,
        read_site=read_site,
        detail={"table": "success_patterns", "drift_pct": 0.0001},
        legacy_count=100,
        central_count=100,
        timestamp_iso=sv._now_iso(),
    )


def _make_comparison_result(count: int = 3) -> sv.ComparisonResult:
    events = [_make_event(metric_id=i + 1) for i in range(count)]
    result = sv.ComparisonResult(
        divergences=events,
        legacy_latency_ms=10.0,
        central_latency_ms=12.0,
        sql_template_hash="abc123",
    )
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteEvent:
    def test_write_event_creates_ledger_if_missing(self, tmp_ledger: Path) -> None:
        assert not tmp_ledger.exists()
        event = _make_event()
        sl.write_event(event, ledger_path=tmp_ledger)
        assert tmp_ledger.exists()
        lines = tmp_ledger.read_text().splitlines()
        assert len(lines) == 1

    def test_write_event_appends_not_overwrites(self, tmp_ledger: Path) -> None:
        event = _make_event()
        sl.write_event(event, ledger_path=tmp_ledger)
        sl.write_event(event, ledger_path=tmp_ledger)
        sl.write_event(event, ledger_path=tmp_ledger)
        lines = tmp_ledger.read_text().splitlines()
        assert len(lines) == 3

    def test_write_event_uses_lock(self, tmp_ledger: Path) -> None:
        """Concurrent writes from multiple threads produce no malformed lines."""
        n_threads = 20
        n_writes_per_thread = 10

        def worker() -> None:
            for _ in range(n_writes_per_thread):
                sl.write_event(_make_event(), ledger_path=tmp_ledger)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = tmp_ledger.read_text().splitlines()
        assert len(lines) == n_threads * n_writes_per_thread

        for line in lines:
            record = json.loads(line)  # raises on malformed JSON
            assert "event" in record
            assert "metric_id" in record

    def test_write_event_serializes_dataclass_correctly(self, tmp_ledger: Path) -> None:
        """Round-trip: written JSON matches original DivergenceEvent fields."""
        event = _make_event(metric_id=2, severity=sv.SEVERITY_HARD, project_id="mc")
        sl.write_event(event, ledger_path=tmp_ledger)

        line = tmp_ledger.read_text().strip()
        record = json.loads(line)

        assert record["event"] == "shadow_divergence"
        assert record["metric_id"] == event.metric_id
        assert record["severity"] == event.severity
        assert record["project_id"] == event.project_id
        assert record["read_site"] == event.read_site
        assert record["legacy_count"] == event.legacy_count
        assert record["central_count"] == event.central_count
        assert record["timestamp_iso"] == event.timestamp_iso
        assert isinstance(record["detail"], dict)

    def test_write_event_terminates_line_with_newline(self, tmp_ledger: Path) -> None:
        sl.write_event(_make_event(), ledger_path=tmp_ledger)
        raw = tmp_ledger.read_bytes()
        assert raw.endswith(b"\n"), "NDJSON line must end with newline"


class TestWriteComparisonResult:
    def test_write_comparison_result_writes_all_divergences(self, tmp_ledger: Path) -> None:
        result = _make_comparison_result(count=4)
        written = sl.write_comparison_result(
            result, project_id="sales", read_site="build_t0_state", ledger_path=tmp_ledger
        )
        assert written == 4
        lines = tmp_ledger.read_text().splitlines()
        assert len(lines) == 4

    def test_write_comparison_result_empty_produces_no_writes(self, tmp_ledger: Path) -> None:
        result = sv.ComparisonResult()
        written = sl.write_comparison_result(
            result, project_id="mc", read_site="test_site", ledger_path=tmp_ledger
        )
        assert written == 0
        assert not tmp_ledger.exists()

    def test_write_comparison_result_includes_sql_template_hash(self, tmp_ledger: Path) -> None:
        result = _make_comparison_result(count=1)
        result.sql_template_hash = "deadbeef"
        sl.write_comparison_result(
            result, project_id="mc", read_site="site_a", ledger_path=tmp_ledger
        )
        record = json.loads(tmp_ledger.read_text().strip())
        assert record["sql_template_hash"] == "deadbeef"

    def test_write_comparison_result_count_matches(self, tmp_ledger: Path) -> None:
        for count in [1, 5, 10]:
            ledger = tmp_ledger.parent / f"ledger_{count}.ndjson"
            result = _make_comparison_result(count=count)
            n = sl.write_comparison_result(
                result, project_id="proj", read_site="site", ledger_path=ledger
            )
            assert n == count
            lines = ledger.read_text().splitlines()
            assert len(lines) == count


class TestNoLockRequiredForReaders:
    def test_no_lock_required_when_only_reading(self, tmp_ledger: Path) -> None:
        """Plain read of the ledger does not block a concurrent writer by >1ms."""
        for _ in range(10):
            sl.write_event(_make_event(), ledger_path=tmp_ledger)

        t_start = time.monotonic()
        _lines = tmp_ledger.read_text().splitlines()
        read_elapsed_ms = (time.monotonic() - t_start) * 1000

        write_start = time.monotonic()
        sl.write_event(_make_event(), ledger_path=tmp_ledger)
        write_elapsed_ms = (time.monotonic() - write_start) * 1000

        assert read_elapsed_ms < 100, f"read took {read_elapsed_ms:.1f}ms, unexpectedly slow"
        assert write_elapsed_ms < 200, f"write took {write_elapsed_ms:.1f}ms, unexpectedly slow"
