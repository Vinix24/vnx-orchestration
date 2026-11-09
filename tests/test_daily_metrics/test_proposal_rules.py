"""Proposal-rule tests for the daily-metrics track (PR-11a).

Every day document is built here: seven ``final`` window days with
``details.samples`` so the pooled baseline is a known number, plus the day
under test. The rules are a pure function of those arguments.
"""

from __future__ import annotations

import dataclasses
import importlib.util
from datetime import date, timedelta

import pytest

DAY = date(2026, 11, 8)


def test_the_proposal_rules_module_exists():
    assert importlib.util.find_spec("daily_metrics.proposal_rules") is not None
    from daily_metrics.proposal_rules import RULE_VERSION

    assert RULE_VERSION


def _scalar(value, n=0, samples=None):
    details = {} if samples is None else {"samples": samples}
    return {"kind": "scalar", "value": value, "source": "fixture",
            "sample_size": n, "details": details}


def _keyed(entries):
    return {"kind": "keyed", "source": "gates", "entries": entries, "details": {}}


def _day(metrics, status="final"):
    return {"status": status, "metrics": metrics}


def _window(metrics_by_offset):
    return {DAY - timedelta(days=offset): _day(metrics)
            for offset, metrics in metrics_by_offset.items()}


def _pr_median_window(baseline=1.0, samples_per_day=8):
    record = _scalar(baseline, samples_per_day, [baseline] * samples_per_day)
    return _window({offset: {"pr_open_to_merge_median_hours": record}
                    for offset in range(1, 8)})


def test_pr_median_boundary_is_a_candidate_at_exactly_1_5x_and_plus_half():
    from daily_metrics.proposal_rules import evaluate

    days = _pr_median_window()
    days[DAY] = _day({"pr_open_to_merge_median_hours": _scalar(1.5, 8, [1.5] * 8)})
    evaluation = evaluate(DAY, days)
    candidate = evaluation.candidates[0]
    assert candidate.key == "pr_open_to_merge_median_hours"
    assert candidate.metric_id == "pr_open_to_merge_median_hours"
    assert candidate.seat is None
    assert candidate.value == pytest.approx(1.5)
    assert candidate.baseline == pytest.approx(1.0)
    assert candidate.baseline_n == 56
    assert candidate.day_n == 8
    assert candidate.exceedance == pytest.approx(0.5)
    assert "pr_open_to_merge_median_hours" not in evaluation.skipped

    days[DAY] = _day({"pr_open_to_merge_median_hours": _scalar(1.49, 8, [1.49] * 8)})
    below = evaluate(DAY, days)
    assert below.candidates == ()
    assert below.skipped["pr_open_to_merge_median_hours"] == "below_trigger"


def test_a_small_day_sample_is_sample_too_small():
    from daily_metrics.proposal_rules import evaluate

    days = _pr_median_window()
    days[DAY] = _day({"pr_open_to_merge_median_hours": _scalar(1.5, 7, [1.5] * 7)})
    evaluation = evaluate(DAY, days)
    assert evaluation.candidates == ()
    assert evaluation.skipped["pr_open_to_merge_median_hours"] == "sample_too_small"


def test_fewer_than_four_final_window_days_is_baseline_unknown():
    from daily_metrics.proposal_rules import evaluate

    record = _scalar(1.0, 8, [1.0] * 8)
    days = _window({offset: {"pr_open_to_merge_median_hours": record}
                    for offset in range(1, 4)})
    days[DAY] = _day({"pr_open_to_merge_median_hours": _scalar(1.5, 8, [1.5] * 8)})
    evaluation = evaluate(DAY, days)
    assert evaluation.candidates == ()
    assert evaluation.skipped["pr_open_to_merge_median_hours"] == "baseline_unknown"


def test_an_incomplete_day_skips_every_key_with_day_not_final():
    from daily_metrics.metric import REQUIRED
    from daily_metrics.proposal_rules import evaluate

    days = _pr_median_window()
    days[DAY] = _day({"pr_open_to_merge_median_hours": _scalar(1.5, 8, [1.5] * 8)},
                     status="incomplete")
    evaluation = evaluate(DAY, days)
    assert evaluation.candidates == ()
    assert set(evaluation.skipped.values()) == {"day_not_final"}
    assert set(REQUIRED) <= set(evaluation.skipped)


def test_merges_trigger_and_the_baseline_minimum():
    from daily_metrics.proposal_rules import evaluate

    days = _window({offset: {"merges": _scalar(12.0)} for offset in range(1, 8)})
    days[DAY] = _day({"merges": _scalar(6.0)})
    evaluation = evaluate(DAY, days)
    candidate = evaluation.candidates[0]
    assert candidate.key == "merges"
    assert candidate.value == pytest.approx(6.0)
    assert candidate.baseline == pytest.approx(12.0)
    assert candidate.exceedance == pytest.approx(0.5)

    small = _window({offset: {"merges": _scalar(9.0)} for offset in range(1, 8)})
    small[DAY] = _day({"merges": _scalar(6.0)})
    too_small = evaluate(DAY, small)
    assert too_small.candidates == ()
    assert too_small.skipped["merges"] == "sample_too_small"


def test_two_seats_one_candidate_and_one_small_sample():
    from daily_metrics.proposal_rules import evaluate

    entries = {
        "codex_gate": _scalar(100.0, 20, [100.0] * 20),
        "glm_gate": _scalar(100.0, 20, [100.0] * 20),
    }
    days = _window({offset: {"gate_duration_seconds_by_seat": _keyed(
        {seat: dict(record) for seat, record in entries.items()})}
        for offset in range(1, 8)})
    days[DAY] = _day({"gate_duration_seconds_by_seat": _keyed({
        "codex_gate": _scalar(160.0, 5, [160.0] * 5),
        "glm_gate": _scalar(160.0, 4, [160.0] * 4),
    })})
    evaluation = evaluate(DAY, days)
    assert [candidate.key for candidate in evaluation.candidates] == [
        "gate_duration_seconds_by_seat:codex_gate"]
    candidate = evaluation.candidates[0]
    assert candidate.metric_id == "gate_duration_seconds_by_seat"
    assert candidate.seat == "codex_gate"
    assert candidate.day_n == 5
    assert evaluation.skipped["gate_duration_seconds_by_seat:glm_gate"] == "sample_too_small"


def test_ranking_is_exceedance_descending_then_key_ascending():
    from daily_metrics.proposal_rules import evaluate

    window_metrics = {
        "pr_open_to_merge_median_hours": _scalar(1.0, 8, [1.0] * 8),
        "rounds_per_pr": _scalar(2.0, 8, [2.0] * 8),
        "merges": _scalar(10.0),
    }
    days = _window({offset: dict(window_metrics) for offset in range(1, 8)})
    days[DAY] = _day({
        "pr_open_to_merge_median_hours": _scalar(2.0, 8, [2.0] * 8),
        "rounds_per_pr": _scalar(3.0, 8, [3.0] * 8),
        "merges": _scalar(5.0),
    })
    evaluation = evaluate(DAY, days)
    assert [candidate.key for candidate in evaluation.candidates] == [
        "pr_open_to_merge_median_hours", "merges", "rounds_per_pr"]
    assert [round(candidate.exceedance, 6) for candidate in evaluation.candidates] == [
        1.0, 0.5, 0.5]


def test_a_metric_without_a_rule_is_no_rule_even_with_a_number():
    from daily_metrics.proposal_rules import evaluate

    days = {DAY: _day({"operator_wait_hours": _scalar(99.0, 8)})}
    evaluation = evaluate(DAY, days)
    assert evaluation.candidates == ()
    assert evaluation.skipped["operator_wait_hours"] == "no_rule"


def test_rule_version_is_twelve_hex_and_tracks_the_table(monkeypatch):
    from daily_metrics import proposal_rules as module

    assert len(module.RULE_VERSION) == 12
    assert all(char in "0123456789abcdef" for char in module.RULE_VERSION)
    changed = dict(module.RULES)
    changed["merges"] = dataclasses.replace(changed["merges"], ratio=0.6)
    monkeypatch.setattr(module, "RULES", changed)
    assert module.rule_version() != module.RULE_VERSION


def test_a_non_finite_day_value_is_value_unknown():
    from daily_metrics.proposal_rules import evaluate

    days = {DAY: _day({"pr_open_to_merge_median_hours": _scalar(float("nan"), 8)})}
    evaluation = evaluate(DAY, days)
    assert evaluation.candidates == ()
    assert evaluation.skipped["pr_open_to_merge_median_hours"] == "value_unknown"


def test_rule_rejects_an_unknown_direction():
    from daily_metrics.proposal_rules import Rule

    with pytest.raises(ValueError):
        Rule("sideways")
