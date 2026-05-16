#!/usr/bin/env python3
"""Tests for timing_metrics.py — Wave 5 PR-5.x."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from timing_metrics import (
    DEFAULT_ACTIVE_THRESHOLD_SECONDS,
    TimingMetrics,
    _build_timing_block,
    analyze_dispatch,
    compute_effective_time,
    detect_parallel_dispatches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ndjson(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _ts(offset_seconds: float, base: datetime | None = None) -> str:
    """ISO-8601 timestamp at base + offset."""
    if base is None:
        base = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc)
    t = base.timestamp() + offset_seconds
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _events(*offsets: float) -> list:
    return [{"timestamp": _ts(o), "type": "tool_use"} for o in offsets]


# ---------------------------------------------------------------------------
# compute_effective_time
# ---------------------------------------------------------------------------

class TestComputeEffectiveTime:
    def test_caps_long_gaps(self, tmp_path: Path) -> None:
        """Events at 0, 30, 35, 65s → gaps of 30, 5, 30 → effective = 10+5+10 = 25."""
        ndjson = tmp_path / "dispatch.ndjson"
        _write_ndjson(ndjson, _events(0, 30, 35, 65))
        _, effective, count, started, ended = compute_effective_time(ndjson, threshold_seconds=10.0)
        assert count == 4
        assert effective == pytest.approx(25.0, abs=0.1)

    def test_walltime_includes_full_span(self, tmp_path: Path) -> None:
        """Walltime = last - first regardless of gap sizes."""
        ndjson = tmp_path / "dispatch.ndjson"
        _write_ndjson(ndjson, _events(0, 30, 35, 65))
        walltime, _, _, started, ended = compute_effective_time(ndjson, threshold_seconds=10.0)
        assert walltime == pytest.approx(65.0, abs=0.1)
        assert started != ""
        assert ended != ""

    def test_small_gaps_not_capped(self, tmp_path: Path) -> None:
        """Gaps smaller than threshold pass through unchanged."""
        ndjson = tmp_path / "dispatch.ndjson"
        _write_ndjson(ndjson, _events(0, 3, 7, 9))
        _, effective, _, _, _ = compute_effective_time(ndjson, threshold_seconds=10.0)
        assert effective == pytest.approx(9.0, abs=0.1)

    def test_single_event_returns_zeros(self, tmp_path: Path) -> None:
        ndjson = tmp_path / "single.ndjson"
        _write_ndjson(ndjson, _events(0))
        wt, eff, count, started, ended = compute_effective_time(ndjson)
        assert wt == 0.0
        assert eff == 0.0
        assert count == 1
        assert started == ""

    def test_empty_file_returns_zeros(self, tmp_path: Path) -> None:
        ndjson = tmp_path / "empty.ndjson"
        ndjson.write_text("", encoding="utf-8")
        wt, eff, count, started, ended = compute_effective_time(ndjson)
        assert wt == 0.0
        assert eff == 0.0
        assert count == 0

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        ndjson = tmp_path / "partial.ndjson"
        with ndjson.open("w") as f:
            f.write(json.dumps({"timestamp": _ts(0)}) + "\n")
            f.write("not valid json\n")
            f.write(json.dumps({"timestamp": _ts(10)}) + "\n")
        wt, _, count, _, _ = compute_effective_time(ndjson, threshold_seconds=10.0)
        assert count == 2
        assert wt == pytest.approx(10.0, abs=0.1)


# ---------------------------------------------------------------------------
# detect_parallel_dispatches
# ---------------------------------------------------------------------------

class TestDetectParallelDispatches:
    def _make_archive(self, tmp_path: Path, name: str, start_offset: float, end_offset: float) -> Path:
        p = tmp_path / f"{name}.ndjson"
        _write_ndjson(p, _events(start_offset, end_offset))
        return p

    def test_finds_overlapping_dispatches(self, tmp_path: Path) -> None:
        """Target: 0-100s. Dispatch A: 50-150s (overlaps). Dispatch B: 200-300s (no overlap)."""
        target_start = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        target_end = target_start + 100

        arch_a = self._make_archive(tmp_path, "dispatch-A", 50, 150)
        arch_b = self._make_archive(tmp_path, "dispatch-B", 200, 300)

        overlaps, _ = detect_parallel_dispatches(
            "target-dispatch",
            target_start,
            target_end,
            [arch_a, arch_b],
        )
        assert "dispatch-A" in overlaps
        assert "dispatch-B" not in overlaps

    def test_parallel_seconds_correct(self, tmp_path: Path) -> None:
        """Target 0-100s, overlap with A at 50-100 = 50 seconds parallel."""
        target_start = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        target_end = target_start + 100

        arch_a = self._make_archive(tmp_path, "dispatch-C", 50, 100)

        _, parallel_secs = detect_parallel_dispatches(
            "target-dispatch",
            target_start,
            target_end,
            [arch_a],
        )
        assert parallel_secs == pytest.approx(50.0, abs=0.5)

    def test_excludes_self(self, tmp_path: Path) -> None:
        """Target dispatch file is never counted as overlapping itself."""
        target_start = datetime(2026, 5, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        target_end = target_start + 100

        self_archive = tmp_path / "target-dispatch-T1.ndjson"
        _write_ndjson(self_archive, _events(0, 90))

        overlaps, _ = detect_parallel_dispatches(
            "target-dispatch",
            target_start,
            target_end,
            [self_archive],
        )
        assert overlaps == []


# ---------------------------------------------------------------------------
# analyze_dispatch
# ---------------------------------------------------------------------------

class TestAnalyzeDispatch:
    def test_missing_archive_returns_none(self, tmp_path: Path) -> None:
        result = analyze_dispatch("nonexistent-dispatch-id", tmp_path)
        assert result is None

    def test_returns_timing_metrics(self, tmp_path: Path) -> None:
        ndjson = tmp_path / "T1" / "my-dispatch-id.ndjson"
        _write_ndjson(ndjson, _events(0, 5, 10, 70))
        result = analyze_dispatch("my-dispatch-id", tmp_path)
        assert isinstance(result, TimingMetrics)
        assert result.dispatch_id == "my-dispatch-id"
        assert result.event_count == 4
        assert result.walltime_seconds == pytest.approx(70.0, abs=0.1)
        # 5+5+10(cap) = 20 effective with default 10s threshold
        assert result.effective_seconds == pytest.approx(20.0, abs=0.1)


# ---------------------------------------------------------------------------
# _build_timing_block
# ---------------------------------------------------------------------------

class TestBuildTimingBlock:
    def test_no_archive_dir_returns_none(self, tmp_path: Path) -> None:
        result = _build_timing_block("some-dispatch", tmp_path)
        assert result is None

    def test_missing_dispatch_in_archive_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "events" / "archive").mkdir(parents=True)
        result = _build_timing_block("missing-dispatch", tmp_path)
        assert result is None

    def test_returns_dict_with_dispatch_id(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "events" / "archive" / "T1"
        ndjson = archive_dir / "test-dispatch-id.ndjson"
        _write_ndjson(ndjson, _events(0, 5, 15))
        result = _build_timing_block("test-dispatch-id", tmp_path)
        assert isinstance(result, dict)
        assert result["dispatch_id"] == "test-dispatch-id"
        assert "walltime_seconds" in result
        assert "effective_seconds" in result
        assert "parallel_dispatch_ids" in result


# ---------------------------------------------------------------------------
# PR report aggregation
# ---------------------------------------------------------------------------

class TestPrReportAggregation:
    def test_aggregates_multiple_dispatches(self, tmp_path: Path) -> None:
        """PR with 3 dispatches → totals sum correctly."""
        archive_dir = tmp_path / "T1"
        dispatches = ["disp-alpha", "disp-beta", "disp-gamma"]
        expected_walltimes = [60.0, 120.0, 30.0]
        for name, duration in zip(dispatches, expected_walltimes):
            _write_ndjson(archive_dir / f"{name}.ndjson", _events(0, duration))

        metrics_list = [
            analyze_dispatch(did, tmp_path)
            for did in dispatches
        ]
        assert all(m is not None for m in metrics_list)

        total_walltime = sum(m.walltime_seconds for m in metrics_list)  # type: ignore[union-attr]
        assert total_walltime == pytest.approx(210.0, abs=1.0)

        total_effective = sum(m.effective_seconds for m in metrics_list)  # type: ignore[union-attr]
        # Each dispatch has a single gap capped at 10s → 10+10+10 = 30
        assert total_effective == pytest.approx(30.0, abs=1.0)
