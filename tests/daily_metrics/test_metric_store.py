"""Contract and day-store tests for the daily-metrics track (PR-1)."""

from __future__ import annotations

import hashlib
import importlib.util

import pytest


def test_package_is_importable():
    # Through ``scripts.lib``: pytest imports this test package as a top-level
    # ``daily_metrics``, which would otherwise shadow the library package.
    assert importlib.util.find_spec("scripts.lib.daily_metrics") is not None
    assert importlib.util.find_spec("scripts.lib.daily_metrics.metric") is not None
    assert importlib.util.find_spec("scripts.lib.daily_metrics.store") is not None


@pytest.fixture()
def metric():
    import scripts.lib.daily_metrics.metric as module

    return module


@pytest.fixture()
def store_mod():
    import scripts.lib.daily_metrics.store as module

    return module


def _day_doc(day: str = "2026-11-06", status: str = "final") -> dict:
    record = {"kind": "scalar", "value": None, "source": "daily_metrics.ledger(deferred)",
              "baseline_7d": None, "unknown_reason": "ledger-is-falsifiable not merged",
              "sample_size": 0, "details": {}}
    return {"project_id": "proj", "day": day, "status": status, "revision": 1,
            "collected_at": "2026-11-08T03:10:00Z", "as_of": "2026-11-07T23:59:59Z",
            "reason": None, "metrics": {"merges_without_receipt_count": record}}


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


def test_write_final_is_immutable(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    doc = _day_doc()
    path = store.write_final("2026-11-06", doc)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(store_mod.AlreadyFinal):
        store.write_final("2026-11-06", {**doc, "metrics": {}})
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_revision_requires_reason_and_wins(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    doc = _day_doc()
    store.write_final("2026-11-06", doc)
    with pytest.raises(ValueError):
        store.write_revision("2026-11-06", doc, reason="   ")
    assert store.write_revision("2026-11-06", doc, reason="recollect").name == "2026-11-06.r2.json"
    assert store.load("2026-11-06")["revision"] == 2
    assert (tmp_path / "2026-11-06.json").exists()


def test_none_value_reads_back(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    record = store.load("2026-11-06")["metrics"]["merges_without_receipt_count"]
    assert record["value"] is None
    assert record["unknown_reason"] == "ledger-is-falsifiable not merged"


def test_write_incomplete_replaces_until_final(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    assert store.is_final("2026-11-06") is False
    store.write_final("2026-11-06", _day_doc())
    assert store.is_final("2026-11-06") is True
    with pytest.raises(store_mod.AlreadyFinal):
        store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))


def test_list_days_and_missing_load(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    assert store.load("2026-11-06") is None
    assert store.list_days() == []
    store.write_final("2026-11-07", _day_doc(day="2026-11-07"))
    store.write_final("2026-11-06", _day_doc())
    assert store.list_days() == ["2026-11-06", "2026-11-07"]


def test_malformed_day_is_refused(store_mod, tmp_path):
    with pytest.raises(ValueError):
        store_mod.DayStore(tmp_path, "proj").write_final("../escape", _day_doc())
