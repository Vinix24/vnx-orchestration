#!/usr/bin/env python3
"""Tests for dispatch_parameter_tracker.py — F57-PR1."""

import sqlite3
import sys
from pathlib import Path
import tempfile

import pytest

# Ensure lib is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_parameter_tracker import (
    DispatchParameters,
    DispatchOutcome,
    DispatchParameterTracker,
    Insight,
    extract_parameters,
    init_schema,
    _count_context_items,
    _count_file_mentions,
    _count_repo_map_symbols,
    _infer_cognition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path):
    """Isolated state directory per test."""
    return tmp_path


@pytest.fixture
def tracker(state_dir):
    return DispatchParameterTracker(state_dir=state_dir)


def _make_params(**kwargs) -> DispatchParameters:
    defaults = dict(
        instruction_char_count=1800,
        context_item_count=2,
        repo_map_symbol_count=15,
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
        cqs=78.5,
        success=True,
        completion_minutes=8.3,
        test_count=5,
        committed=True,
        lines_changed=140,
    )
    defaults.update(kwargs)
    return DispatchOutcome(**defaults)


# ---------------------------------------------------------------------------
# test_capture_parameters
# ---------------------------------------------------------------------------


def test_capture_parameters(tracker, state_dir):
    """Parameters are written to dispatch_experiments table."""
    params = _make_params(role="architect", instruction_char_count=2500)
    tracker.capture_parameters("dispatch-001", params)

    db_path = state_dir / "quality_intelligence.db"  # unified DB (was dispatch_tracker.db)
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT * FROM dispatch_experiments WHERE dispatch_id = ?",
        ("dispatch-001",),
    ).fetchone()
    conn.close()

    assert row is not None
    # Row columns: id, dispatch_id, timestamp, instruction_chars, context_items,
    #              repo_map_symbols, role, cognition, model, terminal, file_count,
    #              success, cqs, completion_minutes, test_count, committed, lines_changed
    col_names = [
        "id", "dispatch_id", "timestamp", "instruction_chars", "context_items",
        "repo_map_symbols", "role", "cognition", "model", "terminal", "file_count",
        "success", "cqs", "completion_minutes", "test_count", "committed", "lines_changed",
    ]
    row_dict = dict(zip(col_names, row))
    assert row_dict["role"] == "architect"
    assert row_dict["instruction_chars"] == 2500
    assert row_dict["success"] is None  # outcome not yet captured


def test_capture_parameters_upsert(tracker, state_dir):
    """Re-capturing parameters for same dispatch_id updates the record."""
    tracker.capture_parameters("dispatch-002", _make_params(role="backend-developer"))
    tracker.capture_parameters("dispatch-002", _make_params(role="test-engineer"))

    db_path = state_dir / "quality_intelligence.db"  # unified DB (was dispatch_tracker.db)
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM dispatch_experiments WHERE dispatch_id = ?",
        ("dispatch-002",),
    ).fetchone()[0]
    role = conn.execute(
        "SELECT role FROM dispatch_experiments WHERE dispatch_id = ?",
        ("dispatch-002",),
    ).fetchone()[0]
    conn.close()

    assert count == 1
    assert role == "test-engineer"


# ---------------------------------------------------------------------------
# test_capture_outcome
# ---------------------------------------------------------------------------


def test_capture_outcome(tracker, state_dir):
    """Outcome columns are populated after capture_parameters + capture_outcome."""
    tracker.capture_parameters("dispatch-003", _make_params())
    outcome = _make_outcome(cqs=82.0, success=True, committed=True, lines_changed=200)
    tracker.capture_outcome("dispatch-003", outcome)

    db_path = state_dir / "quality_intelligence.db"  # unified DB (was dispatch_tracker.db)
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT success, cqs, committed, lines_changed FROM dispatch_experiments WHERE dispatch_id = ?",
        ("dispatch-003",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 1       # success
    assert abs(row[1] - 82.0) < 0.01  # cqs
    assert row[2] == 1       # committed
    assert row[3] == 200     # lines_changed


# ---------------------------------------------------------------------------
# test_analyze_with_enough_data
# ---------------------------------------------------------------------------


def _seed_experiments(tracker, n: int, role: str = "backend-developer") -> None:
    """Insert n completed experiments with varied parameters."""
    for i in range(n):
        dispatch_id = f"seed-{i:04d}"
        # Alternate between large and small instructions
        chars = 2500 if i % 2 == 0 else 1200
        ctx = 3 if i % 3 == 0 else 0
        cqs = 80.0 + (i % 10) if chars > 2000 else 75.0 + (i % 8)
        tracker.capture_parameters(
            dispatch_id,
            _make_params(
                instruction_char_count=chars,
                context_item_count=ctx,
                role=role,
                cognition="high" if chars > 2000 else "medium",
            ),
        )
        tracker.capture_outcome(
            dispatch_id,
            _make_outcome(
                cqs=cqs,
                success=True,
                completion_minutes=5.0 + (i % 5),
            ),
        )


def test_analyze_with_enough_data(tracker):
    """analyze() returns Insight objects when >= 20 experiments exist."""
    _seed_experiments(tracker, 30)
    insights = tracker.analyze(min_experiments=20)
    assert isinstance(insights, list)
    # At least one insight expected with 30 experiments split across groups
    assert len(insights) >= 1
    for ins in insights:
        assert isinstance(ins, Insight)
        assert ins.dimension
        assert ins.group_a
        assert ins.group_b
        assert ins.metric
        assert isinstance(ins.value_a, float)
        assert isinstance(ins.value_b, float)
        assert ins.sample_a >= 1
        assert ins.sample_b >= 1


# ---------------------------------------------------------------------------
# test_analyze_insufficient_data_returns_empty
# ---------------------------------------------------------------------------


def test_analyze_insufficient_data_returns_empty(tracker):
    """analyze() returns empty list when fewer than min_experiments exist."""
    _seed_experiments(tracker, 5)
    insights = tracker.analyze(min_experiments=20)
    assert insights == []


def test_analyze_zero_experiments_returns_empty(tracker):
    insights = tracker.analyze(min_experiments=20)
    assert insights == []


# ---------------------------------------------------------------------------
# test_recommend_parameters
# ---------------------------------------------------------------------------


def test_recommend_parameters_defaults_when_insufficient(tracker):
    """get_recommended_parameters returns conservative defaults with < 20 experiments."""
    _seed_experiments(tracker, 10)
    rec = tracker.get_recommended_parameters(role="backend-developer")
    assert "instruction_chars" in rec
    assert "context_items" in rec
    assert "cognition" in rec
    assert rec.get("note") == "defaults (insufficient data)"


def test_recommend_parameters_data_driven(tracker):
    """get_recommended_parameters returns data-driven values with >= 20 experiments."""
    _seed_experiments(tracker, 40)
    rec = tracker.get_recommended_parameters(role="backend-developer")
    assert "instruction_chars" in rec
    assert "context_items" in rec
    assert "cognition" in rec
    assert rec.get("note") == "data-driven"
    # Values should be range strings or single values
    assert rec["instruction_chars"]
    assert rec["context_items"]


def test_recommend_parameters_unknown_role_falls_back(tracker):
    """Role with too few experiments falls back to overall population."""
    _seed_experiments(tracker, 25, role="backend-developer")
    rec = tracker.get_recommended_parameters(role="nonexistent-role")
    # Should still return something (falls back to full population)
    assert "instruction_chars" in rec


# ---------------------------------------------------------------------------
# test_insight_format
# ---------------------------------------------------------------------------


def test_insight_format():
    """Insight.summary() produces a readable string."""
    ins = Insight(
        dimension="instruction_chars",
        group_a="> 2000 chars",
        group_b="<= 2000 chars",
        metric="avg_cqs",
        value_a=81.3,
        value_b=72.1,
        sample_a=15,
        sample_b=12,
    )
    summary = ins.summary()
    assert "instruction_chars" in summary
    assert "81.3" in summary
    assert "72.1" in summary
    assert "n=15" in summary
    assert "n=12" in summary
    assert "higher" in summary


def test_insight_format_lower():
    """Insight.summary() shows 'lower' when group_a is worse."""
    ins = Insight(
        dimension="role",
        group_a="test-engineer",
        group_b="backend-developer",
        metric="avg_completion_min",
        value_a=4.2,
        value_b=7.1,
        sample_a=10,
        sample_b=12,
    )
    summary = ins.summary()
    assert "lower" in summary


# ---------------------------------------------------------------------------
# extract_parameters helpers
# ---------------------------------------------------------------------------


def test_count_context_items_empty():
    assert _count_context_items("No context here") == 0


def test_count_context_items_detects_markers():
    text = "## Context\nsome stuff\n### intelligence items\nmore"
    assert _count_context_items(text) >= 1


def test_count_file_mentions():
    text = "Modify scripts/lib/foo.py and tests/test_bar.py as per dashboard/index.ts"
    count = _count_file_mentions(text)
    assert count == 3


def test_count_repo_map_symbols_empty():
    assert _count_repo_map_symbols(None) == 0
    assert _count_repo_map_symbols("") == 0


def test_count_repo_map_symbols_counts_lines():
    repo_map = "  MyClass\n    my_method\n    other_method\n"
    count = _count_repo_map_symbols(repo_map)
    assert count == 3


def test_infer_cognition_high():
    long_text = "architecture " + "x" * 3000
    assert _infer_cognition(long_text, None) == "high"


def test_infer_cognition_low():
    assert _infer_cognition("short simple fix", None) == "low"


def test_extract_parameters_full():
    instruction = "Fix scripts/lib/foo.py and tests/test_foo.py\n## Context\nsome context"
    params = extract_parameters(
        instruction=instruction,
        terminal_id="T1",
        model="sonnet",
        role="backend-developer",
        repo_map="  MyClass\n    my_method\n",
    )
    assert params.terminal == "T1"
    assert params.model == "sonnet"
    assert params.role == "backend-developer"
    assert params.instruction_char_count == len(instruction)
    assert params.file_count >= 2
    assert params.repo_map_symbol_count >= 1


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_empty(tracker):
    s = tracker.stats()
    assert s["total_experiments"] == 0
    assert s["completed"] == 0
    assert s["insights_available"] is False


def test_stats_after_seeding(tracker):
    _seed_experiments(tracker, 25)
    s = tracker.stats()
    assert s["total_experiments"] == 25
    assert s["completed"] == 25
    assert s["success_count"] == 25
    assert s["success_rate"] == 100.0
    assert s["avg_cqs"] is not None
    assert s["insights_available"] is True


# ---------------------------------------------------------------------------
# top_insights_for_t0
# ---------------------------------------------------------------------------


def test_top_insights_for_t0_empty_when_insufficient(tracker):
    result = tracker.top_insights_for_t0()
    assert result == []


def test_top_insights_for_t0_returns_strings(tracker):
    _seed_experiments(tracker, 30)
    result = tracker.top_insights_for_t0(n=3)
    assert isinstance(result, list)
    assert len(result) <= 3
    for item in result:
        assert isinstance(item, str)
        assert len(item) > 10
