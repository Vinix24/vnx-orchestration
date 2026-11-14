"""Characterisation of ``cost_tracker.build_metrics`` (golf schoon R03b).

Pins the report ``build_metrics`` returns for a mixed fixture, so the
extract-method split of that function cannot move any number.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cost_tracker  # noqa: E402


def _usage(status, tokens=None, cost=None):
    i, o, t = tokens or (None, None, None)
    return {"input_tokens": i, "output_tokens": o, "total_tokens": t,
            "cost_usd": cost, "resolution_status": status}


def _receipt(dispatch_id, terminal="T1", **extra):
    base = {"event_type": "task_complete", "dispatch_id": dispatch_id, "terminal": terminal}
    base.update(extra)
    return base


def _run(receipts, usage_by_dispatch, tmp_path, invalid=2):
    def fake(receipt):
        return usage_by_dispatch.get(receipt.get("dispatch_id"), _usage("no_session_id"))

    with mock.patch.object(cost_tracker, "resolve_usage_for_receipt", side_effect=fake):
        return cost_tracker.build_metrics(receipts, invalid, tmp_path, tmp_path / "r.ndjson")


@pytest.fixture()
def metrics(tmp_path):
    receipts = [
        _receipt("d1", "t1", provider="Claude", model="sonnet", input_tokens=1000, output_tokens=500, total_tokens=1500),
        _receipt("d2", "T2", model="opus"),
        _receipt("d3", "T2", model="opus"),
        _receipt("d4", "T3", model="sonnet"),
        _receipt("d5", "", model="sonnet", input_tokens=10, output_tokens=5),
        _receipt("d6", "T1", model="sonnet"),
        {"event_type": "receipt_x", "dispatch_id": "dz"},
    ]
    usage = {
        "d2": _usage("resolved", (200, 100, 300), 0.5),
        "d3": _usage("resolved", (50, 25, 75), None),
        "d4": _usage("transcript_not_found"),
        "d5": _usage("error: boom"),
        "d6": _usage("no_usage_data"),
    }
    return _run(receipts, usage, tmp_path)


def test_totals(metrics):
    t = metrics["totals"]
    assert t["receipts_total"] == 7
    assert t["invalid_receipt_lines"] == 2
    assert t["events_analyzed"] == 6
    assert t["receipts_with_known_tokens"] == 4  # d5 derives its total from input + output
    assert t["receipts_with_unknown_tokens"] == 2
    assert t["input_tokens"] == 1260
    assert t["output_tokens"] == 630
    assert t["total_tokens"] == 1890
    assert t["receipts_with_estimated_cost"] + t["receipts_with_unknown_cost"] == 6
    assert t["receipts_with_estimated_cost"] == 3
    # d2 resolved cost 0.5 + d1 and d5 estimated from the receipt tokens at sonnet rates
    assert t["estimated_cost_usd"] == pytest.approx(0.510605)
    assert t["unknown_cost_usd"] == "unknown"


def test_resolution_stats(metrics):
    assert metrics["totals"]["resolution_stats"] == {
        "resolved": 2, "no_session_id": 1, "transcript_not_found": 1,
        "no_usage_data": 1, "error": 1, "fallback_to_receipt": 2,
    }


def test_event_counts_census_counts_raw_lines(metrics):
    assert metrics["event_counts"] == {"receipt_x": 1, "task_complete": 6}


def test_buckets_and_key_order(metrics):
    assert list(metrics["by_terminal"]) == ["T1", "T2", "T3", "UNKNOWN"]
    assert list(metrics["by_model"]) == sorted(metrics["by_model"])
    assert metrics["by_terminal"]["T1"]["events"] == 2
    assert metrics["by_terminal"]["T2"]["input_tokens"] == 250
    assert metrics["by_terminal"]["T3"]["receipts_with_unknown_tokens"] == 1
    assert metrics["by_model"]["claude-opus-4.6"]["events"] == 2
    assert metrics["by_model"]["claude-opus-4.6"]["receipts_with_unknown_cost"] == 1
    assert metrics["by_model"]["claude-sonnet-4.6"]["events"] == 4
    assert "T1|claude" in metrics["by_worker"]
    assert metrics["by_worker"]["T2|claude_code"]["events"] == 2
    assert metrics["by_worker"]["UNKNOWN|unknown"]["events"] == 1
    assert list(metrics["by_worker"]) == sorted(metrics["by_worker"])


def test_envelope(metrics, tmp_path):
    assert metrics["version"] == "1.0"
    assert metrics["generated_at_utc"].endswith("+00:00")
    assert metrics["source"] == {
        "receipts_path": str(tmp_path / "r.ndjson"),
        "state_dir": str(tmp_path),
        "pricing_basis": "AGENT_TEAMS/VNX_MULTIMODEL_ARCHITECTURE.MD",
    }
    assert len(metrics["notes"]) == 3
    assert isinstance(metrics["totals"]["estimated_cost_usd"], float)


def test_empty_input(tmp_path):
    m = _run([], {}, tmp_path, invalid=0)
    assert m["totals"]["events_analyzed"] == 0
    assert m["totals"]["estimated_cost_usd"] == 0.0
    assert m["event_counts"] == {} and m["by_model"] == {} and m["by_worker"] == {}
