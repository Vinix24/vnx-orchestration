#!/usr/bin/env python3
"""Tests for Wave 5 PR-5.x: timing_metrics module."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from timing_metrics import (
    TimingMetrics,
    _build_timing_block,
    analyze_dispatch,
    compute_effective_time,
    detect_parallel_dispatches,
)


def _write_ndjson(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _make_events(timestamps_iso: list) -> list:
    return [{"type": "assistant", "timestamp": ts} for ts in timestamps_iso]


class TestComputeEffectiveTime(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)

    def test_caps_long_gaps(self):
        """Events with 30s + 5s + 30s gaps → effective = 10+5+10 = 25s (cap 10)."""
        events = _make_events([
            "2026-05-16T10:00:00+00:00",
            "2026-05-16T10:00:30+00:00",
            "2026-05-16T10:00:35+00:00",
            "2026-05-16T10:01:05+00:00",
        ])
        f = self.tmp / "test.ndjson"
        _write_ndjson(f, events)
        _, eff, n, _, _ = compute_effective_time(f, threshold_seconds=10.0)
        self.assertAlmostEqual(eff, 25.0, places=1)
        self.assertEqual(n, 4)

    def test_walltime_includes_full_span(self):
        """Walltime = last - first regardless of internal gaps."""
        events = _make_events([
            "2026-05-16T10:00:00+00:00",
            "2026-05-16T10:00:30+00:00",
            "2026-05-16T10:30:00+00:00",
        ])
        f = self.tmp / "test2.ndjson"
        _write_ndjson(f, events)
        wt, eff, n, started, ended = compute_effective_time(f, threshold_seconds=10.0)
        self.assertAlmostEqual(wt, 1800.0, places=1)
        self.assertAlmostEqual(eff, 20.0, places=1)
        self.assertEqual(n, 3)
        self.assertIn("10:00:00", started)
        self.assertIn("10:30:00", ended)

    def test_single_event_returns_zeros(self):
        """Single event → walltime=0, effective=0."""
        events = _make_events(["2026-05-16T10:00:00+00:00"])
        f = self.tmp / "single.ndjson"
        _write_ndjson(f, events)
        wt, eff, n, started, ended = compute_effective_time(f)
        self.assertEqual(wt, 0.0)
        self.assertEqual(eff, 0.0)
        self.assertEqual(n, 1)
        self.assertEqual(started, ended)

    def test_skips_lines_with_no_timestamp(self):
        """Lines without timestamp field are silently ignored."""
        events = [
            {"type": "system"},
            {"type": "assistant", "timestamp": "2026-05-16T10:00:00+00:00"},
            {"type": "result", "timestamp": "2026-05-16T10:00:08+00:00"},
        ]
        f = self.tmp / "mixed.ndjson"
        _write_ndjson(f, events)
        wt, eff, n, _, _ = compute_effective_time(f, threshold_seconds=10.0)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(wt, 8.0, places=1)
        self.assertAlmostEqual(eff, 8.0, places=1)


class TestDetectParallelDispatches(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)

    def _create_archive(self, name: str, start: str, end: str) -> Path:
        """Create a minimal archive with two events at start and end."""
        f = self.tmp / f"{name}.ndjson"
        events = _make_events([start, end])
        _write_ndjson(f, events)
        return f

    def test_finds_overlapping(self):
        """3 dispatches, 2 overlap with target → list has 2 entries."""
        target_start = "2026-05-16T10:00:00+00:00"
        target_end = "2026-05-16T10:10:00+00:00"
        overlap1 = self._create_archive("overlap-1", "2026-05-16T10:05:00+00:00", "2026-05-16T10:15:00+00:00")
        overlap2 = self._create_archive("overlap-2", "2026-05-16T09:55:00+00:00", "2026-05-16T10:03:00+00:00")
        no_overlap = self._create_archive("no-overlap", "2026-05-16T11:00:00+00:00", "2026-05-16T11:30:00+00:00")

        from datetime import datetime
        t_start = datetime.fromisoformat(target_start.replace("Z", "+00:00")).timestamp()
        t_end = datetime.fromisoformat(target_end.replace("Z", "+00:00")).timestamp()

        ids, secs = detect_parallel_dispatches(
            "target-dispatch", t_start, t_end,
            [overlap1, overlap2, no_overlap]
        )
        self.assertEqual(len(ids), 2)
        self.assertIn("overlap-1", ids)
        self.assertIn("overlap-2", ids)
        self.assertNotIn("no-overlap", ids)

    def test_parallel_seconds_correct(self):
        """Assert sum-of-overlaps is correct."""
        target_start = "2026-05-16T10:00:00+00:00"
        target_end = "2026-05-16T10:10:00+00:00"
        # Overlaps from T+5m to T+10m → 300s
        arch = self._create_archive("overlap-a", "2026-05-16T10:05:00+00:00", "2026-05-16T10:20:00+00:00")

        from datetime import datetime
        t_start = datetime.fromisoformat(target_start.replace("Z", "+00:00")).timestamp()
        t_end = datetime.fromisoformat(target_end.replace("Z", "+00:00")).timestamp()

        _, secs = detect_parallel_dispatches("tgt", t_start, t_end, [arch])
        self.assertAlmostEqual(secs, 300.0, places=0)

    def test_excludes_target_itself(self):
        """Archive matching target dispatch_id is excluded from candidates."""
        target_start = "2026-05-16T10:00:00+00:00"
        target_end = "2026-05-16T10:10:00+00:00"
        self_arch = self._create_archive("my-dispatch-id", "2026-05-16T10:00:00+00:00", "2026-05-16T10:10:00+00:00")

        from datetime import datetime
        t_start = datetime.fromisoformat(target_start.replace("Z", "+00:00")).timestamp()
        t_end = datetime.fromisoformat(target_end.replace("Z", "+00:00")).timestamp()

        ids, _ = detect_parallel_dispatches("my-dispatch-id", t_start, t_end, [self_arch])
        self.assertEqual(ids, [])


class TestAnalyzeDispatch(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)

    def test_missing_archive_returns_none(self):
        """dispatch_id not in archive → returns None."""
        archive_root = self.tmp / "archive"
        archive_root.mkdir(parents=True)
        result = analyze_dispatch("nonexistent-dispatch-id", archive_root)
        self.assertIsNone(result)

    def test_returns_timing_metrics(self):
        """Valid archive returns a populated TimingMetrics."""
        archive_root = self.tmp / "T1"
        archive_root.mkdir(parents=True)
        f = archive_root / "test-dispatch-abc.ndjson"
        events = _make_events([
            "2026-05-16T10:00:00+00:00",
            "2026-05-16T10:00:05+00:00",
            "2026-05-16T10:00:09+00:00",
        ])
        _write_ndjson(f, events)
        result = analyze_dispatch("test-dispatch-abc", self.tmp, threshold_seconds=10.0)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.dispatch_id, "test-dispatch-abc")
        self.assertAlmostEqual(result.walltime_seconds, 9.0, places=1)
        self.assertAlmostEqual(result.effective_seconds, 9.0, places=1)
        self.assertEqual(result.event_count, 3)


class TestPRReportAggregation(unittest.TestCase):
    """Tests for the pr_timing_report CLI aggregation logic."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

    def _make_dispatch(self, name: str, events: list) -> Path:
        f = self.tmp / f"{name}.ndjson"
        _write_ndjson(f, events)
        return f

    def test_aggregates_multiple_dispatches(self):
        """PR with 3 dispatches → totals are sum of all three."""
        import importlib
        spec = importlib.util.spec_from_file_location(
            "pr_timing_report",
            Path(__file__).resolve().parent.parent / "scripts" / "pr_timing_report.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        archive_root = self.tmp / "archive"
        archive_root.mkdir(parents=True)

        dispatches = []
        for i, (start, end) in enumerate([
            ("2026-05-16T10:00:00+00:00", "2026-05-16T10:00:08+00:00"),
            ("2026-05-16T11:00:00+00:00", "2026-05-16T11:00:06+00:00"),
            ("2026-05-16T12:00:00+00:00", "2026-05-16T12:00:04+00:00"),
        ]):
            f = archive_root / f"wave5-pr522-dispatch-{i}.ndjson"
            _write_ndjson(f, _make_events([start, end]))
            m = analyze_dispatch(f.stem, archive_root)
            if m:
                dispatches.append(m)

        self.assertEqual(len(dispatches), 3)
        total_walltime = sum(m.walltime_seconds for m in dispatches)
        total_effective = sum(m.effective_seconds for m in dispatches)
        self.assertAlmostEqual(total_walltime, 18.0, places=0)
        self.assertAlmostEqual(total_effective, 18.0, places=0)


class TestBuildTimingBlock(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self._tmpdir)

    def test_returns_none_when_no_archive(self):
        """_build_timing_block returns None when archive dir missing."""
        result = _build_timing_block("any-dispatch", self.tmp / "nonexistent")
        self.assertIsNone(result)

    def test_returns_dict_when_archive_found(self):
        """_build_timing_block returns a dict with expected keys when archive exists."""
        # vnx_data_dir layout: <vnx_data>/events/archive/T1/<dispatch>.ndjson
        vnx_data = self.tmp / "vnx-data"
        archive = vnx_data / "events" / "archive" / "T1"
        archive.mkdir(parents=True)
        f = archive / "my-dispatch-xyz.ndjson"
        _write_ndjson(f, _make_events([
            "2026-05-16T10:00:00+00:00",
            "2026-05-16T10:00:05+00:00",
        ]))
        result = _build_timing_block("my-dispatch-xyz", vnx_data)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("dispatch_id", result)
        self.assertIn("walltime_seconds", result)
        self.assertIn("effective_seconds", result)
        self.assertAlmostEqual(result["walltime_seconds"], 5.0, places=1)
        self.assertEqual(result["dispatch_id"], "my-dispatch-xyz")


if __name__ == "__main__":
    unittest.main()
