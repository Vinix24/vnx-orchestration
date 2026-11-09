"""Metric-contract tests for the daily-metrics track (PR-1).

The test package ``test_daily_metrics`` never shadows the library ``daily_metrics``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def test_library_package_owns_the_daily_metrics_name():
    """`import daily_metrics` must reach the library, not a shadowing test package."""
    import daily_metrics

    library = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "daily_metrics"
    imported = Path(daily_metrics.__file__).resolve().parent
    assert imported == library, f"daily_metrics resolved to {imported}, not {library}"


def test_package_is_importable():
    assert importlib.util.find_spec("daily_metrics") is not None
    assert importlib.util.find_spec("daily_metrics.metric") is not None


@pytest.fixture()
def metric():
    from daily_metrics import metric as module

    return module


def test_required_has_exactly_ten_ids(metric):
    assert len(metric.REQUIRED) == 10
    assert set(metric.REQUIRED) == {
        "merges", "pr_open_to_merge_median_hours", "pr_open_to_merge_p75_hours",
        "rounds_per_pr", "ci_duration_seconds_median", "gate_duration_seconds_by_seat",
        "silence_windows_over_30m", "operator_wait_hours", "cost_per_merged_pr_usd",
        "merges_without_receipt_count",
    }
    assert metric.KEYED == frozenset({"gate_duration_seconds_by_seat"})


def test_scalar_rejects_invalid_records(metric):
    with pytest.raises(ValueError):
        metric.ScalarMetric(value=None, unknown_reason=None)
    with pytest.raises(ValueError):
        metric.ScalarMetric(value=1.0)
    with pytest.raises(ValueError):
        metric.ScalarMetric(value=1.0, source="   ")
    with pytest.raises(ValueError):
        metric.ScalarMetric(value=1.0, source="s", unknown_reason="x")


def test_keyed_rejects_empty_entries(metric):
    with pytest.raises(ValueError):
        metric.KeyedMetric(entries={})


def test_records_round_trip(metric):
    record = metric.ScalarMetric(
        value=None, source="s", unknown_reason="ledger-is-falsifiable not merged"
    )
    restored = metric.ScalarMetric.from_dict(record.to_dict())
    assert restored.value is None
    assert restored.unknown_reason == "ledger-is-falsifiable not merged"
    keyed = metric.KeyedMetric(source="gate", entries={
        "codex_gate": metric.ScalarMetric(value=1.5, source="gate", sample_size=3)})
    payload = keyed.to_dict()
    assert payload["kind"] == "keyed"
    assert metric.KeyedMetric.from_dict(payload).entries["codex_gate"].value == 1.5


def test_day_status_follows_transient_reasons(metric):
    transient = [
        metric.ScalarMetric(value=None, source="s", unknown_reason="source_import_failed: boom"),
        metric.ScalarMetric(value=None, source="s", unknown_reason="ci_run_unreadable_for_2_prs"),
        metric.KeyedMetric(source="gate", entries={
            "codex_gate": metric.ScalarMetric(value=None, source="g", unknown_reason="gh_failed")}),
    ]
    for record in transient:
        assert metric.day_status({"merges": record}) == "incomplete"
    permanent = metric.KeyedMetric(source="gate", entries={
        "codex_gate": metric.ScalarMetric(
            value=None, source="gate", unknown_reason="no_results_for_seat_on_day")})
    assert metric.day_status({"gate_duration_seconds_by_seat": permanent}) == "final"
