#!/usr/bin/env python3
"""Tests for event_analyzer — deterministic behavioral extraction from dispatch NDJSON archives."""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from event_analyzer import (
    DispatchBehavior,
    analyze_dispatch,
    analyze_all,
    get_summary,
    _append_phase,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_ARCHIVE_T1 = (
    Path(__file__).resolve().parent.parent
    / ".vnx-data/events/archive/T1/20260414-090200-f58-pr3-layered-prompt-A.ndjson"
)


def _make_event(type: str, **kwargs) -> dict:
    base = {"type": type, "dispatch_id": "test-dispatch", "terminal": "T1", "sequence": 0}
    base.update(kwargs)
    return base


def _make_tool_use(name: str, input: dict, tool_id: str, ts: str = "2026-04-14T10:00:00+00:00") -> dict:
    return {
        "type": "tool_use",
        "timestamp": ts,
        "dispatch_id": "test-dispatch",
        "terminal": "T1",
        "sequence": 0,
        "data": {"name": name, "input": input, "id": tool_id},
    }


def _make_tool_result(tool_id: str, content: str, ts: str = "2026-04-14T10:00:01+00:00") -> dict:
    return {
        "type": "tool_result",
        "timestamp": ts,
        "dispatch_id": "test-dispatch",
        "terminal": "T1",
        "sequence": 0,
        "data": {"tool_use_id": tool_id, "content": content},
    }


def _write_ndjson(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAnalyzeRealDispatch:
    def test_analyze_real_dispatch(self):
        """Integration: analyze real archive, verify basic structure is correct."""
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip(f"Real archive not found: {REAL_ARCHIVE_T1}")

        b = analyze_dispatch(REAL_ARCHIVE_T1)

        assert b.dispatch_id == "20260414-090200-f58-pr3-layered-prompt-A"
        assert b.terminal == "T1"
        assert b.total_events > 0
        assert b.reads > 0
        assert b.duration_seconds > 0
        assert isinstance(b.phase_sequence, list)
        assert len(b.phase_sequence) > 0

    def test_real_dispatch_has_real_tool_counts(self):
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip(f"Real archive not found: {REAL_ARCHIVE_T1}")
        b = analyze_dispatch(REAL_ARCHIVE_T1)
        # This dispatch had 13 Reads, 8 Writes, 6 Edits, 12 Bash, 4 Glob
        assert b.reads == 13
        assert b.writes == 8
        assert b.edits == 6
        assert b.bash_calls == 12
        assert b.glob_calls == 4

    def test_real_dispatch_committed_and_pushed(self):
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip(f"Real archive not found: {REAL_ARCHIVE_T1}")
        b = analyze_dispatch(REAL_ARCHIVE_T1)
        assert b.committed is True
        assert b.pushed is True

    def test_real_dispatch_wrote_report(self):
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip(f"Real archive not found: {REAL_ARCHIVE_T1}")
        b = analyze_dispatch(REAL_ARCHIVE_T1)
        assert b.wrote_report is True


class TestReadsBeforeFirstWrite:
    def test_reads_before_first_write_counted(self, tmp_path):
        """3 reads before first write → reads_before_first_write == 3."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Read", {"file_path": "/a.py"}, "r1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result("r1", "content", "2026-04-14T10:00:02+00:00"),
            _make_tool_use("Read", {"file_path": "/b.py"}, "r2", "2026-04-14T10:00:03+00:00"),
            _make_tool_result("r2", "content", "2026-04-14T10:00:04+00:00"),
            _make_tool_use("Read", {"file_path": "/c.py"}, "r3", "2026-04-14T10:00:05+00:00"),
            _make_tool_result("r3", "content", "2026-04-14T10:00:06+00:00"),
            _make_tool_use("Write", {"file_path": "/out.py"}, "w1", "2026-04-14T10:00:07+00:00"),
            _make_tool_result("w1", "ok", "2026-04-14T10:00:08+00:00"),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.reads_before_first_write == 3

    def test_reads_before_first_write_zero_when_write_first(self, tmp_path):
        """Write before any reads → reads_before_first_write == 0."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Write", {"file_path": "/out.py"}, "w1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result("w1", "ok", "2026-04-14T10:00:02+00:00"),
            _make_tool_use("Read", {"file_path": "/a.py"}, "r1", "2026-04-14T10:00:03+00:00"),
            _make_tool_result("r1", "content", "2026-04-14T10:00:04+00:00"),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.reads_before_first_write == 0

    def test_real_dispatch_exploration_depth(self):
        """Real dispatch should have > 0 reads before first write."""
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip()
        b = analyze_dispatch(REAL_ARCHIVE_T1)
        assert b.reads_before_first_write == 8  # known value from real archive


class TestDetectRework:
    def test_edit_same_file_twice_counts_as_one_rework_cycle(self, tmp_path):
        """File edited 3 times → 2 rework cycles (N-1)."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
        ]
        for i in range(3):
            tid = f"e{i}"
            ts = f"2026-04-14T10:00:0{i+1}+00:00"
            events.append(_make_tool_use(
                "Edit",
                {"file_path": "/foo.py", "old_string": "a", "new_string": "b"},
                tid, ts,
            ))
            events.append(_make_tool_result(tid, "ok", ts))
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.edit_cycles_same_file == 2

    def test_test_fail_edit_cycle_detected(self, tmp_path):
        """pytest FAILED → Edit → another pytest detected as test_fail_edit_cycle."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            # First pytest run — 1 failure
            _make_tool_use(
                "Bash", {"command": "pytest tests/", "description": "run tests"}, "b1",
                "2026-04-14T10:00:01+00:00",
            ),
            _make_tool_result(
                "b1",
                "FAILED tests/test_x.py::test_foo - AssertionError\n1 failed, 5 passed",
                "2026-04-14T10:00:02+00:00",
            ),
            # Edit to fix
            _make_tool_use(
                "Edit",
                {"file_path": "/foo.py", "old_string": "x", "new_string": "y"},
                "e1", "2026-04-14T10:00:03+00:00",
            ),
            _make_tool_result("e1", "ok", "2026-04-14T10:00:04+00:00"),
            # Second pytest run — all pass
            _make_tool_use(
                "Bash", {"command": "pytest tests/", "description": "run again"}, "b2",
                "2026-04-14T10:00:05+00:00",
            ),
            _make_tool_result(
                "b2", "6 passed, 0 failed", "2026-04-14T10:00:06+00:00",
            ),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.test_fail_edit_cycles >= 1


class TestExtractBashErrors:
    def test_extract_error_lines_from_bash_result(self, tmp_path):
        """Error/Exception lines from Bash tool_result are captured."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Bash", {"command": "python3 script.py"}, "b1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result(
                "b1",
                "Traceback (most recent call last):\n  File 'x.py', line 3\nImportError: No module named 'foo'\n",
                "2026-04-14T10:00:02+00:00",
            ),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert len(b.bash_errors) >= 2
        joined = " ".join(b.bash_errors)
        assert "Traceback" in joined or "ImportError" in joined

    def test_real_archive_has_errors(self):
        if not REAL_ARCHIVE_T1.exists():
            pytest.skip()
        b = analyze_dispatch(REAL_ARCHIVE_T1)
        assert len(b.bash_errors) > 0


class TestExtractTestResults:
    def test_parse_passed_and_failed_counts(self, tmp_path):
        """Regex parses '15 passed, 2 failed' from pytest output."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use(
                "Bash", {"command": "pytest tests/ -q"}, "b1", "2026-04-14T10:00:01+00:00",
            ),
            _make_tool_result(
                "b1",
                "FAILED tests/test_x.py::test_bar\n15 passed, 2 failed in 3.14s",
                "2026-04-14T10:00:02+00:00",
            ),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.test_results.get("passed") == 15
        assert b.test_results.get("failed") == 2

    def test_parse_all_passed(self, tmp_path):
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Bash", {"command": "pytest"}, "b1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result("b1", "45 passed in 1.23s", "2026-04-14T10:00:02+00:00"),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.test_results.get("passed") == 45
        assert b.test_results.get("failed", 0) == 0


class TestClassifyPhases:
    def test_read_write_bash_phases(self, tmp_path):
        """Read→Write→Bash(pytest)→Bash(git commit) maps to explore→implement→test→commit."""
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "test", "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Read", {"file_path": "/a.py"}, "r1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result("r1", "content", "2026-04-14T10:00:01+00:00"),
            _make_tool_use("Write", {"file_path": "/b.py"}, "w1", "2026-04-14T10:00:02+00:00"),
            _make_tool_result("w1", "ok", "2026-04-14T10:00:02+00:00"),
            _make_tool_use("Bash", {"command": "pytest tests/"}, "b1", "2026-04-14T10:00:03+00:00"),
            _make_tool_result("b1", "5 passed", "2026-04-14T10:00:03+00:00"),
            _make_tool_use(
                "Bash", {"command": "git commit -m 'feat: x'"}, "b2", "2026-04-14T10:00:04+00:00",
            ),
            _make_tool_result("b2", "[main abc] feat: x", "2026-04-14T10:00:04+00:00"),
        ]
        p = tmp_path / "test.ndjson"
        _write_ndjson(p, events)
        b = analyze_dispatch(p)
        assert b.phase_sequence == ["explore", "implement", "test", "commit"]

    def test_append_phase_deduplicates_consecutive(self):
        phases: list[str] = []
        _append_phase(phases, "explore")
        _append_phase(phases, "explore")  # duplicate — should not append
        _append_phase(phases, "implement")
        assert phases == ["explore", "implement"]

    def test_append_phase_allows_recurrence(self):
        phases: list[str] = []
        _append_phase(phases, "explore")
        _append_phase(phases, "implement")
        _append_phase(phases, "explore")  # back to explore — should append
        assert phases == ["explore", "implement", "explore"]


class TestSummaryAggregation:
    def test_summary_aggregation(self, tmp_path):
        """Two behaviors produce correct averages."""
        b1 = DispatchBehavior(
            dispatch_id="d1", terminal="T1", role="backend-developer",
            duration_seconds=100.0, total_events=50,
            reads=5, writes=2, edits=1, bash_calls=3, grep_calls=0, glob_calls=1,
            reads_before_first_write=5,
            edit_cycles_same_file=2, test_fail_edit_cycles=1,
            unique_files_read=4, unique_files_written=2,
            files_read=["/a.py", "/b.py"], files_written=["/c.py"],
            committed=True, pushed=True, wrote_report=True,
        )
        b2 = DispatchBehavior(
            dispatch_id="d2", terminal="T2", role="backend-developer",
            duration_seconds=200.0, total_events=80,
            reads=10, writes=4, edits=2, bash_calls=6, grep_calls=1, glob_calls=2,
            reads_before_first_write=3,
            edit_cycles_same_file=0, test_fail_edit_cycles=0,
            unique_files_read=8, unique_files_written=3,
            files_read=["/a.py", "/d.py"], files_written=["/e.py"],
            committed=False, pushed=False, wrote_report=False,
        )
        summary = get_summary([b1, b2])
        assert summary["total_dispatches"] == 2
        role_stats = summary["role_stats"]["backend-developer"]
        assert role_stats["count"] == 2
        assert role_stats["duration_avg_s"] == 150.0
        assert role_stats["duration_min_s"] == 100.0
        assert role_stats["duration_max_s"] == 200.0
        assert role_stats["reads_before_write_avg"] == 4.0  # (5+3)/2
        assert summary["total_rework_events"] == 3  # (2+1) + (0+0)
        assert summary["commits_pct"] == 50.0
        assert summary["push_pct"] == 50.0
        assert summary["report_pct"] == 50.0
        # /a.py read in both
        top_reads = {r["file"]: r["count"] for r in summary["top_files_read"]}
        assert top_reads.get("/a.py") == 2

    def test_summary_empty_behaviors(self):
        summary = get_summary([])
        assert summary["total_dispatches"] == 0

    def test_summary_real_data(self):
        """Run on real archives — verify non-empty output with real stats."""
        archive_dir = (
            Path(__file__).resolve().parent.parent / ".vnx-data" / "events" / "archive"
        )
        if not archive_dir.exists():
            pytest.skip("Archive dir not found")
        behaviors = analyze_all(archive_dir)
        if not behaviors:
            pytest.skip("No archives found")
        summary = get_summary(behaviors)
        assert summary["total_dispatches"] > 0
        assert len(summary["top_files_read"]) > 0


class TestAnalyzeAll:
    def test_analyze_all_returns_list(self, tmp_path):
        """analyze_all with an empty dir returns empty list."""
        empty = tmp_path / "archive"
        empty.mkdir()
        result = analyze_all(empty)
        assert result == []

    def test_analyze_all_finds_nested_ndjson(self, tmp_path):
        """Files nested in T1/ subdir are discovered."""
        t1 = tmp_path / "T1"
        t1.mkdir()
        events = [
            {"type": "init", "timestamp": "2026-04-14T10:00:00+00:00",
             "dispatch_id": "nested-test", "terminal": "T1",
             "data": {"model": "x", "session_id": "s"}},
            _make_tool_use("Read", {"file_path": "/x.py"}, "r1", "2026-04-14T10:00:01+00:00"),
            _make_tool_result("r1", "code", "2026-04-14T10:00:02+00:00"),
        ]
        _write_ndjson(t1 / "nested-test.ndjson", events)
        result = analyze_all(tmp_path)
        assert len(result) == 1
        assert result[0].dispatch_id == "nested-test"

    def test_analyze_all_sorted_by_timestamp(self, tmp_path):
        """analyze_all returns behaviors sorted by first_timestamp."""
        t1 = tmp_path / "T1"
        t1.mkdir()

        for i, ts in enumerate(["2026-04-14T12:00:00+00:00", "2026-04-14T10:00:00+00:00"]):
            events = [
                {"type": "init", "timestamp": ts, "dispatch_id": f"d{i}",
                 "terminal": "T1", "data": {"model": "x", "session_id": "s"}},
            ]
            _write_ndjson(t1 / f"d{i}.ndjson", events)

        result = analyze_all(tmp_path)
        assert len(result) == 2
        assert result[0].first_timestamp < result[1].first_timestamp
