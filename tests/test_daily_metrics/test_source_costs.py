"""Source tests for the three deferred daily metrics (doorloop PR-7).

The three sources publish ``unknown`` with a precise, permanent reason and
never a number: cost per merged PR waits for its owning join, operator wait
has no request timestamp in the approval ledger, and the merges-without-receipt
reader is not wired yet. Every clock is fixed; the cost ledger is always a
fixture under ``tmp_path`` reached through a monkeypatched
``provider_costs._resolve_costs_path``, never the real store.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import pathlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import provider_costs

DAY = date(2026, 11, 6)
COST_REASON = "owner_track_kosten-per-gemergede-pr_not_landed"
APPROVAL_REASON = "no_request_timestamp_in_approval_ledger"
LEDGER_REASON = "ledger_reader_not_wired"


def _context():
    from daily_metrics.collect import CollectContext

    return CollectContext("p", DAY, datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc))


def _write_costs(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _ledger_records() -> list[dict]:
    """52 records on the day, 2 without a numeric ``cost_usd``, plus other days."""
    records = [
        {"recorded_at": f"2026-11-06T{index % 24:02d}:{index % 60:02d}:00Z", "cost_usd": 0.5}
        for index in range(50)
    ]
    records.append({"recorded_at": "2026-11-06T05:00:00Z"})  # no cost_usd at all
    records.append({"recorded_at": "2026-11-06T06:00:00Z", "cost_usd": "n/a"})  # not a number
    records.append({"recorded_at": "2026-11-05T23:59:59Z", "cost_usd": 1.0})  # other day
    records.append({"recorded_at": "2026-11-07T00:00:00Z", "cost_usd": 1.0})  # other day
    return records


def test_three_source_modules_are_importable():
    """Red run: the first assert fails on the merge base, before the modules land."""
    assert importlib.util.find_spec("daily_metrics.sources.costs") is not None
    assert importlib.util.find_spec("daily_metrics.sources.approvals") is not None
    assert importlib.util.find_spec("daily_metrics.sources.ledger") is not None


def test_costs_publishes_unknown_with_numeric_coverage(tmp_path, monkeypatch):
    from daily_metrics.sources import costs

    path = _write_costs(tmp_path / "provider_costs.ndjson", _ledger_records())
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: path)

    record = costs.collect(_context())["cost_per_merged_pr_usd"]

    assert record.value is None
    assert record.source == "daily_metrics.costs"
    assert record.unknown_reason == COST_REASON
    assert record.details["records"] == 52
    assert record.details["without_cost_usd"] == 2
    assert record.details["numeric_coverage"] == pytest.approx(0.962, abs=0.001)
    assert "read_error" not in record.details


def test_costs_counts_only_records_on_the_context_day(tmp_path, monkeypatch):
    from daily_metrics.sources import costs

    path = _write_costs(
        tmp_path / "provider_costs.ndjson",
        [
            {"recorded_at": "2026-11-05T23:59:59Z", "cost_usd": 1.0},
            {"recorded_at": "2026-11-07T00:00:00Z", "cost_usd": 1.0},
        ],
    )
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: path)

    record = costs.collect(_context())["cost_per_merged_pr_usd"]

    assert record.value is None
    assert record.details["records"] == 0
    assert record.details["numeric_coverage"] is None


def test_costs_missing_ledger_is_unknown_with_no_coverage(tmp_path, monkeypatch):
    from daily_metrics.sources import costs

    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: tmp_path / "absent.ndjson")

    record = costs.collect(_context())["cost_per_merged_pr_usd"]

    assert record.value is None
    assert record.unknown_reason == COST_REASON
    assert record.details["records"] == 0
    assert record.details["numeric_coverage"] is None


def test_costs_broken_line_sets_read_error_and_still_returns_the_record(tmp_path, monkeypatch):
    from daily_metrics.sources import costs

    path = tmp_path / "provider_costs.ndjson"
    path.write_text(
        '{"recorded_at": "2026-11-06T01:00:00Z", "cost_usd": 0.5}\n{not json\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: path)

    record = costs.collect(_context())["cost_per_merged_pr_usd"]

    assert record.value is None
    assert record.unknown_reason == COST_REASON
    assert record.details["read_error"] == "JSONDecodeError"
    assert record.details["records"] == 1


def test_costs_unreadable_ledger_sets_read_error_and_still_returns_the_record(tmp_path, monkeypatch):
    from daily_metrics.sources import costs

    path = tmp_path / "provider_costs.ndjson"
    path.mkdir()
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: path)

    record = costs.collect(_context())["cost_per_merged_pr_usd"]

    assert record.value is None
    assert record.unknown_reason == COST_REASON
    assert record.details["read_error"] == "IsADirectoryError"
    assert record.details["numeric_coverage"] is None


def test_approvals_publishes_unknown_without_a_request_timestamp():
    from daily_metrics.sources import approvals

    record = approvals.collect(_context())["operator_wait_hours"]

    assert record.value is None
    assert record.source == "daily_metrics.approvals"
    assert record.unknown_reason == APPROVAL_REASON


def test_ledger_publishes_unknown_and_never_opens_the_receipts_file(monkeypatch):
    from daily_metrics.sources import ledger

    real_builtin_open = builtins.open
    real_path_open = pathlib.Path.open

    def guarded_builtin_open(file, *args, **kwargs):
        if str(file).endswith("t0_receipts.ndjson"):
            raise AssertionError("the ledger source must not open t0_receipts.ndjson")
        return real_builtin_open(file, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        if str(self).endswith("t0_receipts.ndjson"):
            raise AssertionError("the ledger source must not open t0_receipts.ndjson")
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(pathlib.Path, "open", guarded_path_open)

    record = ledger.collect(_context())["merges_without_receipt_count"]

    assert record.value is None
    assert record.source == "daily_metrics.ledger(deferred)"
    assert record.unknown_reason == LEDGER_REASON


def test_discover_sources_finds_the_three_without_failures():
    from daily_metrics import collect as collect_mod

    modules, failures = collect_mod.discover_sources(collect_mod.sources_dir())
    names = {module.__name__.rsplit(".", 1)[-1] for module in modules}

    assert {"costs", "approvals", "ledger"} <= names
    assert failures == []


def test_collect_day_carries_the_three_reasons(tmp_path, monkeypatch):
    from daily_metrics import collect as collect_mod

    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: tmp_path / "absent.ndjson")

    document = collect_mod.collect_day(_context())
    metrics = document["metrics"]

    assert metrics["cost_per_merged_pr_usd"]["unknown_reason"] == COST_REASON
    assert metrics["cost_per_merged_pr_usd"]["source"] == "daily_metrics.costs"
    assert metrics["operator_wait_hours"]["unknown_reason"] == APPROVAL_REASON
    assert metrics["operator_wait_hours"]["source"] == "daily_metrics.approvals"
    assert metrics["merges_without_receipt_count"]["unknown_reason"] == LEDGER_REASON
    assert metrics["merges_without_receipt_count"]["source"] == "daily_metrics.ledger(deferred)"


def test_the_three_reasons_are_permanent():
    from daily_metrics import metric

    assert LEDGER_REASON in metric.PERMANENT_REASONS
    assert "ledger-is-falsifiable not merged" in metric.PERMANENT_REASONS
    for reason in (COST_REASON, APPROVAL_REASON, LEDGER_REASON):
        assert metric.is_transient_reason(reason) is False
