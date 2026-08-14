"""Tests for router_baseline — the repeatable pre-rollout nulmeting.

The key property under test: the measurement is a repeatable command, not a
one-off script — the same data yields the same output every time, so a
post-rollout run can be placed next to it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import router_baseline as rb  # noqa: E402


SINCE = "2026-08-03"


def _rec(timestamp, provider="claude", model="sonnet", status="success", lane=None,
         duration=None):
    r = {
        "timestamp": timestamp,
        "provider": provider,
        "model": model,
        "status": status,
    }
    if lane is not None:
        r["lane"] = lane
    if duration is not None:
        r["duration_seconds"] = duration
    return r


def test_baseline_distributions():
    records = [
        _rec("2026-08-04T00:00:00Z", "claude", "sonnet", "success"),
        _rec("2026-08-04T00:01:00Z", "claude", "sonnet", "failed"),
        _rec("2026-08-04T00:02:00Z", "deepseek-harness", "deepseek-v4-flash", "done"),
        _rec("2026-08-04T00:03:00Z", "kimi", "kimi-k3", "unknown"),
    ]
    report = rb.baseline(records, SINCE)
    assert report.total == 4
    assert report.providers == {"claude": 2, "deepseek-harness": 1, "kimi": 1}
    assert report.models == {"sonnet": 2, "deepseek-v4-flash": 1, "kimi-k3": 1}
    assert report.outcomes == {"success": 2, "failed": 1, "other": 1}


def test_outcome_buckets_cover_ledger_statuses():
    records = [
        _rec("2026-08-04T00:00:00Z", status="success"),
        _rec("2026-08-04T00:00:00Z", status="done"),
        _rec("2026-08-04T00:00:00Z", status="failed"),
        _rec("2026-08-04T00:00:00Z", status="failure"),
        _rec("2026-08-04T00:00:00Z", status="timeout"),
        _rec("2026-08-04T00:00:00Z", status="contract_invalid"),
        _rec("2026-08-04T00:00:00Z", status="not_executable"),
        _rec("2026-08-04T00:00:00Z", status="guard_error"),
        _rec("2026-08-04T00:00:00Z", status="requested"),
        _rec("2026-08-04T00:00:00Z", status=""),
    ]
    report = rb.baseline(records, SINCE)
    assert report.outcomes["success"] == 2
    assert report.outcomes["failed"] == 6
    assert report.outcomes["other"] == 2


def test_duration_grouping_falls_back_to_provider():
    records = [
        _rec("2026-08-04T00:00:00Z", provider="deepseek-harness", duration=10.0),
        _rec("2026-08-04T00:01:00Z", provider="deepseek-harness", duration=20.0),
    ]
    report = rb.baseline(records, SINCE)
    stats = report.durations_by_lane["deepseek-harness"]
    assert stats["n"] == 2
    assert stats["mean"] == 15.0
    assert stats["min"] == 10.0
    assert stats["max"] == 20.0


def test_baseline_is_deterministic_on_same_data():
    records = [
        _rec("2026-08-04T00:00:00Z", "claude", "sonnet", "success", duration=12.3),
        _rec("2026-08-04T00:01:00Z", "deepseek-harness", "deepseek-v4-flash", "failed"),
        _rec("2026-08-04T00:02:00Z", "codex", "gpt-5.5", "done", lane="tmux_interactive"),
    ]
    first = rb.render(rb.baseline(records, SINCE))
    second = rb.render(rb.baseline(records, SINCE))
    assert first == second, "the nulmeting must produce identical output on identical data"


def test_load_receipts_cutoff_and_epoch_timestamps(tmp_path):
    path = tmp_path / "receipts.ndjson"
    lines = [
        _rec("2026-08-02T23:59:59Z", provider="claude"),            # before window
        _rec("2026-08-03T00:00:00Z", provider="claude"),            # boundary (in)
        _rec(1785715200, provider="deepseek-harness"),              # epoch 2026-08-03T00:00:00Z (in)
    ]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    records = rb.load_receipts(path, SINCE)
    assert len(records) == 2


def test_main_json_and_human_outputs(tmp_path, capsys):
    path = tmp_path / "receipts.ndjson"
    path.write_text(
        json.dumps(_rec("2026-08-04T00:00:00Z", "claude", "sonnet", "success")) + "\n",
        encoding="utf-8",
    )
    rc = rb.main(["--receipts", str(path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["total"] == 1
    assert payload["providers"]["claude"] == 1

    rc = rb.main(["--receipts", str(path)])
    human = capsys.readouterr().out
    assert rc == 0
    assert "Provider distribution:" in human


def test_main_missing_file_returns_2(tmp_path, capsys):
    rc = rb.main(["--receipts", str(tmp_path / "nope.ndjson")])
    assert rc == 2
