#!/usr/bin/env python3
"""Tests for shadow_report.py CLI.

Uses subprocess + tmp ledger fixture to drive the CLI end-to-end.

Covers:
  - test_since_24h_filters_old_events
  - test_since_7d_includes_old_events
  - test_severity_filter_works
  - test_by_table_groups_correctly
  - test_by_metric_groups_correctly
  - test_json_output_is_valid
  - test_empty_ledger_produces_empty_report
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
REPORT_CLI = SCRIPTS_DIR / "shadow_report.py"

sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
import shadow_logger as sl
import shadow_verifier as sv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_offset(hours: float = 0.0, days: float = 0.0) -> str:
    """Return ISO timestamp offset from now by negative hours/days (i.e. in the past)."""
    delta = datetime.timedelta(hours=hours, days=days)
    dt = datetime.datetime.now(datetime.timezone.utc) - delta
    return dt.isoformat()


def _make_event(
    metric_id: int = 4,
    severity: str = sv.SEVERITY_SOFT,
    project_id: str = "proj_a",
    read_site: str = "test_site",
    table: str = "success_patterns",
    hours_ago: float = 0.0,
    days_ago: float = 0.0,
) -> sv.DivergenceEvent:
    return sv.DivergenceEvent(
        metric_id=metric_id,
        severity=severity,
        project_id=project_id,
        read_site=read_site,
        detail={"table": table, "drift_pct": 0.0001},
        legacy_count=100,
        central_count=100,
        timestamp_iso=_ts_offset(hours=hours_ago, days=days_ago),
    )


def _run_cli(ledger: Path, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(REPORT_CLI),
        "--ledger",
        str(ledger),
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _populate_ledger(ledger: Path, events: list[sv.DivergenceEvent]) -> None:
    for ev in events:
        sl.write_event(ev, ledger_path=ledger)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSinceFilter:
    def test_since_24h_filters_old_events(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(hours_ago=1),   # recent — should appear
            _make_event(hours_ago=2),   # recent — should appear
            _make_event(hours_ago=30),  # older than 24h — should NOT appear
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "24h", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total_events"] == 2

    def test_since_7d_includes_old_events(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(hours_ago=1),    # within 7d
            _make_event(days_ago=3),     # within 7d
            _make_event(days_ago=6),     # within 7d (barely)
            _make_event(days_ago=8),     # older than 7d — excluded
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "7d", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total_events"] == 3


class TestSeverityFilter:
    def test_severity_filter_works(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(severity=sv.SEVERITY_HARD, hours_ago=1),
            _make_event(severity=sv.SEVERITY_HARD, hours_ago=2),
            _make_event(severity=sv.SEVERITY_SOFT, hours_ago=1),
            _make_event(severity=sv.SEVERITY_AGGREGATE, hours_ago=1),
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "24h", "--severity", "hard", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total_events"] == 2

        result_soft = _run_cli(ledger, "--since", "24h", "--severity", "soft", "--json")
        assert result_soft.returncode == 0
        data_soft = json.loads(result_soft.stdout)
        assert data_soft["total_events"] == 1


class TestByTable:
    def test_by_table_groups_correctly(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(project_id="mc", table="success_patterns", hours_ago=1),
            _make_event(project_id="mc", table="success_patterns", hours_ago=2),
            _make_event(project_id="mc", table="antipatterns", hours_ago=1),
            _make_event(project_id="sales", table="success_patterns", hours_ago=1),
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "24h", "--by-table", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        # Keys are "project_id:table"
        assert data.get("mc:success_patterns") == 2
        assert data.get("mc:antipatterns") == 1
        assert data.get("sales:success_patterns") == 1


class TestByMetric:
    def test_by_metric_groups_correctly(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(metric_id=1, hours_ago=1),
            _make_event(metric_id=1, hours_ago=2),
            _make_event(metric_id=2, hours_ago=1),
            _make_event(metric_id=4, hours_ago=1),
            _make_event(metric_id=4, hours_ago=2),
            _make_event(metric_id=4, hours_ago=3),
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "24h", "--by-metric", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        assert data.get("1") == 2
        assert data.get("2") == 1
        assert data.get("3") == 0
        assert data.get("4") == 3
        assert data.get("5") == 0
        assert data.get("6") == 0


class TestJsonOutput:
    def test_json_output_is_valid(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.ndjson"
        events = [
            _make_event(severity=sv.SEVERITY_HARD, hours_ago=1),
            _make_event(severity=sv.SEVERITY_SOFT, hours_ago=2),
        ]
        _populate_ledger(ledger, events)

        result = _run_cli(ledger, "--since", "24h", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        assert "total_events" in data
        assert "by_severity" in data
        assert "by_metric" in data
        assert "by_project" in data
        assert data["total_events"] == 2
        assert data["by_severity"]["hard"] == 1
        assert data["by_severity"]["soft"] == 1


class TestSkippedLinesCount:
    def test_reports_skipped_lines_count(self, tmp_path: Path) -> None:
        """Malformed NDJSON lines are counted and reported; valid events still appear."""
        ledger = tmp_path / "ledger.ndjson"
        events = [_make_event(hours_ago=1) for _ in range(3)]
        _populate_ledger(ledger, events)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("NOT VALID JSON\n")
            fh.write("{broken: line without quotes}\n")

        # Human-readable report must mention skipped count
        result = _run_cli(ledger, "--since", "24h")
        assert result.returncode == 0, result.stderr
        assert "2" in result.stdout and "skipped" in result.stdout.lower()

        # JSON report must carry skipped_lines key
        result_json = _run_cli(ledger, "--since", "24h", "--json")
        assert result_json.returncode == 0, result_json.stderr
        data = json.loads(result_json.stdout)
        assert data["skipped_lines"] == 2
        assert data["total_events"] == 3

    def test_skipped_lines_in_by_metric_output(self, tmp_path: Path) -> None:
        """--by-metric output also surfaces skipped_lines count."""
        ledger = tmp_path / "ledger.ndjson"
        _populate_ledger(ledger, [_make_event(metric_id=1, hours_ago=1)])
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("this is garbage\n")

        result = _run_cli(ledger, "--since", "24h", "--by-metric", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["skipped_lines"] == 1


class TestEmptyLedger:
    def test_empty_ledger_produces_empty_report(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger_empty.ndjson"
        # ledger does not exist yet

        result = _run_cli(ledger, "--since", "24h")
        assert result.returncode == 0, result.stderr
        assert "Total events: 0" in result.stdout

    def test_empty_ledger_json_output(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger_empty.ndjson"

        result = _run_cli(ledger, "--since", "24h", "--json")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total_events"] == 0
