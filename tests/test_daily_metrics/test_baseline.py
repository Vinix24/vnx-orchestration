"""Baseline tests for the daily-metrics track (PR-9).

Fixed dates only; the pooled fixture reproduces the measured contrast
(1.008 h pooled against 0.402 h as the median of the day medians).
"""

from __future__ import annotations

import importlib.util
import statistics
from datetime import date, timedelta

import pytest

DAY = date(2026, 11, 8)


def test_the_baseline_module_exists():
    assert importlib.util.find_spec("daily_metrics.baseline") is not None
    from daily_metrics.baseline import KIND
    from daily_metrics.metric import REQUIRED

    assert set(KIND) == REQUIRED


def _scalar(value, samples=None):
    details = {} if samples is None else {"samples": samples}
    return {"kind": "scalar", "value": value, "source": "fixture", "sample_size": 0,
            "details": details}


def _day(status="final", metrics=None):
    return {"status": status, "metrics": metrics or {}}


def _build(values):
    return {
        DAY - timedelta(days=offset): _day(metrics={"merges": _scalar(value)})
        for offset, value in values.items()
    }


def _keyed_days(entries_by_offset):
    days = {}
    for offset, entries in entries_by_offset.items():
        record = {"kind": "keyed", "source": "gates", "entries": entries, "details": {}}
        days[DAY - timedelta(days=offset)] = _day(
            metrics={"gate_duration_seconds_by_seat": record})
    return days


def _stamp(offset):
    return (DAY - timedelta(days=offset)).isoformat()


def _pooled_fixture():
    days = {}
    for offset in range(1, 5):
        days[DAY - timedelta(days=offset)] = _day(metrics={
            "pr_open_to_merge_median_hours": _scalar(0.402, [0.2] * 15 + [0.604] * 15)})
    for offset in range(5, 8):
        days[DAY - timedelta(days=offset)] = _day(metrics={
            "pr_open_to_merge_median_hours": _scalar(1.008, [0.1] * 100 + [1.008] * 300)})
    return days


def test_pooled_quantile_differs_from_the_median_of_daily_medians():
    from daily_metrics.baseline import baseline, median_of_daily_medians

    days = _pooled_fixture()
    pooled = baseline("pr_open_to_merge_median_hours", DAY, days)
    assert pooled.value == pytest.approx(1.008, abs=0.001)
    assert pooled.sample_size == 1320
    assert pooled.source == "baseline:pr_open_to_merge_median_hours"
    assert pooled.details["samples_truncated"] is False
    assert pooled.details["samples_truncated_days"] == []
    assert median_of_daily_medians(
        "pr_open_to_merge_median_hours", DAY, days) == pytest.approx(0.402, abs=0.001)


def test_baseline_needs_four_final_days_and_ignores_incomplete_ones():
    from daily_metrics.baseline import baseline

    days = _build({offset: float(offset) for offset in range(1, 4)})
    for offset in range(4, 8):
        days[DAY - timedelta(days=offset)] = _day(
            status="incomplete", metrics={"merges": _scalar(None)})
    result = baseline("merges", DAY, days)
    assert result.value is None
    assert result.unknown_reason == "insufficient_baseline_days"
    assert result.sample_size == 3
    excluded = result.details["excluded_days"]
    assert set(excluded) == {_stamp(offset) for offset in range(4, 8)}
    assert set(excluded.values()) == {"not_final"}

    five = _build({offset: float(offset) for offset in range(1, 6)})
    five[DAY - timedelta(days=6)] = _day(
        status="incomplete", metrics={"merges": _scalar(100.0)})
    unchanged = baseline("merges", DAY, five)
    assert unchanged.value == 3.0
    assert unchanged.sample_size == 5
    assert unchanged.details["excluded_days"][_stamp(6)] == "not_final"


def test_the_day_itself_is_never_in_the_window():
    from daily_metrics.baseline import baseline

    days = _build({offset: 1.0 for offset in range(1, 8)})
    days[DAY] = _day(metrics={"merges": _scalar(1000.0)})
    result = baseline("merges", DAY, days)
    assert result.value == 1.0
    assert DAY.isoformat() not in result.details["used_days"]
    assert DAY.isoformat() not in result.details["excluded_days"]
    assert result.details["window"] == {"from": "2026-11-01", "to": "2026-11-07"}


def test_exclusions_name_every_reason():
    from daily_metrics.baseline import baseline

    days = _build({offset: 1.0 for offset in range(1, 5)})
    days[DAY - timedelta(days=5)] = _day(metrics={"unrelated": _scalar(1.0)})
    days[DAY - timedelta(days=6)] = _day(metrics={"rounds_per_pr": _scalar(1.0)})
    excluded = baseline("rounds_per_pr", DAY, days).details["excluded_days"]
    assert excluded[_stamp(7)] == "missing_day"
    assert excluded[_stamp(6)] == "no_samples"
    assert excluded[_stamp(5)] == "no_record"


def test_samples_above_400_are_truncated_or_flagged():
    from daily_metrics.baseline import baseline

    samples = [float(value) for value in range(401)]
    days = {DAY - timedelta(days=offset): _day(metrics={"rounds_per_pr": _scalar(1.0, samples)})
            for offset in range(1, 5)}
    result = baseline("rounds_per_pr", DAY, days)
    assert result.sample_size == 1600
    assert result.details["samples_truncated"] is True
    assert result.details["samples_truncated_days"] == [_stamp(4), _stamp(3), _stamp(2), _stamp(1)]

    flagged = _scalar(1.0, [1.0, 2.0])
    flagged["details"]["samples_truncated"] = True
    days = {DAY - timedelta(days=offset): _day(metrics={"rounds_per_pr": flagged})
            for offset in range(1, 5)}
    assert baseline("rounds_per_pr", DAY, days).details["samples_truncated"] is True


def test_keyed_baselines_are_per_key_and_per_seat_days():
    from daily_metrics.baseline import baseline, keyed_baselines

    entries_by_offset = {}
    for offset in range(1, 5):
        entries_by_offset[offset] = {
            "codex_gate": _scalar(1.0, [1.0] * 20),
            "deepseek_gate": _scalar(300.0, [300.0] * 20),
        }
        if offset <= 3:
            entries_by_offset[offset]["glm_gate"] = _scalar(20.0, [20.0] * 20)
    days = _keyed_days(entries_by_offset)
    codex = baseline("gate_duration_seconds_by_seat", DAY, days, key="codex_gate")
    deepseek = baseline("gate_duration_seconds_by_seat", DAY, days, key="deepseek_gate")
    glm = baseline("gate_duration_seconds_by_seat", DAY, days, key="glm_gate")
    assert codex.value == 1.0
    assert deepseek.value == 300.0
    assert codex.source == "baseline:gate_duration_seconds_by_seat:codex_gate"
    assert glm.value is None
    assert glm.unknown_reason == "insufficient_baseline_days"
    assert glm.details["excluded_days"][_stamp(4)] == "no_entry_for_key"
    baselines = keyed_baselines("gate_duration_seconds_by_seat", DAY, days)
    assert list(baselines) == ["codex_gate", "deepseek_gate", "glm_gate"]
    assert baselines["glm_gate"].value is None


def test_mean_over_seven_final_days_and_a_none_day():
    from daily_metrics.baseline import baseline

    days = _build({offset: float(offset) for offset in range(1, 8)})
    result = baseline("merges", DAY, days)
    assert result.value == 4.0
    assert result.sample_size == 7
    assert result.details["kind"] == "mean"
    assert len(result.details["used_days"]) == 7

    days[DAY - timedelta(days=7)] = _day(metrics={"merges": _scalar(None)})
    unknown_day = baseline("merges", DAY, days)
    assert unknown_day.value == 3.5
    assert unknown_day.sample_size == 6
    assert unknown_day.details["excluded_days"][_stamp(7)] == "value_unknown"


def test_quantile_matches_statistics_inclusive_and_rejects_bad_input():
    from daily_metrics.baseline import quantile

    assert quantile([1, 2, 3, 4], 0.75) == statistics.quantiles(
        [1, 2, 3, 4], n=4, method="inclusive")[2]
    assert quantile([1, 2, 3, 4], 0.5) == statistics.median([1, 2, 3, 4])
    assert quantile([5.0], 0.0) == 5.0
    assert quantile([5.0], 1.0) == 5.0
    with pytest.raises(ValueError):
        quantile([], 0.5)
    with pytest.raises(ValueError):
        quantile([1.0], 1.5)


def test_baseline_rejects_unknown_ids_and_mismatched_keys():
    from daily_metrics.baseline import baseline, keyed_baselines, median_of_daily_medians

    with pytest.raises(ValueError):
        baseline("unknown_metric", DAY, {})
    with pytest.raises(ValueError):
        baseline("gate_duration_seconds_by_seat", DAY, {})
    with pytest.raises(ValueError):
        baseline("merges", DAY, {}, key="codex_gate")
    with pytest.raises(ValueError):
        keyed_baselines("merges", DAY, {})
    with pytest.raises(ValueError):
        median_of_daily_medians("merges", DAY, {})
