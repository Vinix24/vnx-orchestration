#!/usr/bin/env python3
"""Tests for f57_insights_reader.py (ARC-4)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_parameter_tracker import (
    DispatchOutcome,
    DispatchParameterTracker,
    DispatchParameters,
)
from f57_insights_reader import main, read_insights


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


def _make_params(**kwargs) -> DispatchParameters:
    defaults = dict(
        instruction_char_count=1800,
        context_item_count=2,
        repo_map_symbol_count=10,
        role="backend-developer",
        cognition="medium",
        model="sonnet",
        terminal="T1",
        file_count=4,
    )
    defaults.update(kwargs)
    return DispatchParameters(**defaults)


def _make_outcome(**kwargs) -> DispatchOutcome:
    defaults = dict(
        cqs=78.0,
        success=True,
        completion_minutes=6.0,
        test_count=3,
        committed=True,
        lines_changed=120,
    )
    defaults.update(kwargs)
    return DispatchOutcome(**defaults)


def _seed(state_dir: Path, n: int) -> None:
    tracker = DispatchParameterTracker(state_dir=state_dir)
    for i in range(n):
        # Alternate large/small to create contrast groups for analyze()
        chars = 2500 if i % 2 == 0 else 1200
        ctx = 2 if i % 3 == 0 else 0
        tracker.capture_parameters(
            f"exp-{i:04d}",
            _make_params(
                instruction_char_count=chars,
                context_item_count=ctx,
                cognition="high" if chars > 2000 else "medium",
            ),
        )
        tracker.capture_outcome(
            f"exp-{i:04d}",
            _make_outcome(cqs=80.0 + (i % 10) if chars > 2000 else 75.0 + (i % 8)),
        )


def _backdate_rows(state_dir: Path, prefix: str, days_ago: int) -> None:
    """Shift timestamps of matching rows back by days_ago days."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE dispatch_experiments SET timestamp = ? WHERE dispatch_id LIKE ?",
        (ts, f"{prefix}%"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Case A: F57 rows in DB → reader emits insights
# ---------------------------------------------------------------------------


def test_reader_emits_insights_with_data(state_dir):
    _seed(state_dir, 25)
    result = read_insights(days=7, state_dir=state_dir)

    assert isinstance(result, dict)
    assert "generated_at" in result
    assert "window_days" in result
    assert result["window_days"] == 7
    assert "all_time_insights" in result
    assert isinstance(result["all_time_insights"], list)
    assert len(result["all_time_insights"]) >= 1
    for item in result["all_time_insights"]:
        assert isinstance(item, str)
        assert len(item) > 5
    assert "stats" in result
    assert result["stats"]["total_experiments"] == 25


def test_reader_json_is_serializable(state_dir):
    _seed(state_dir, 25)
    result = read_insights(days=7, state_dir=state_dir)
    serialized = json.dumps(result)
    assert serialized


# ---------------------------------------------------------------------------
# Case B: empty DB → empty array, exit 0
# ---------------------------------------------------------------------------


def test_reader_empty_db_returns_empty_insights(state_dir):
    result = read_insights(days=7, state_dir=state_dir)

    assert result["all_time_insights"] == []
    assert result["window_insights"] == []
    assert result["window_experiment_count"] == 0
    assert result["stats"]["total_experiments"] == 0


def test_reader_empty_db_cli_exits_zero(state_dir, capsys, monkeypatch):
    monkeypatch.setattr(
        "f57_insights_reader._STATE_DIR", state_dir
    )
    main(["--days", "7"])
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["all_time_insights"] == []


# ---------------------------------------------------------------------------
# Case C: --days filter works
# ---------------------------------------------------------------------------


def test_days_filter_excludes_old_rows(state_dir):
    tracker = DispatchParameterTracker(state_dir=state_dir)
    # Insert 15 recent experiments (dispatch ids: new-NNNN)
    for i in range(15):
        tracker.capture_parameters(f"new-{i:04d}", _make_params())
        tracker.capture_outcome(f"new-{i:04d}", _make_outcome())

    # Insert 10 old experiments (dispatch ids: old-NNNN), back-date to 20 days ago
    for i in range(10):
        tracker.capture_parameters(f"old-{i:04d}", _make_params())
        tracker.capture_outcome(f"old-{i:04d}", _make_outcome())
    _backdate_rows(state_dir, "old-", days_ago=20)

    result_7d = read_insights(days=7, state_dir=state_dir)
    result_30d = read_insights(days=30, state_dir=state_dir)

    assert result_7d["window_experiment_count"] == 15
    assert result_30d["window_experiment_count"] == 25


def test_days_filter_default_is_7(state_dir):
    _seed(state_dir, 5)
    result = read_insights(state_dir=state_dir)
    assert result["window_days"] == 7
