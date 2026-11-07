"""tests/test_cost_tracker_v3_tokens.py — OI-2595: tokens of a v3 attempt come from its governance record.

``dispatch_finished`` outranks ``task_complete`` as the witness of an attempt but
carries no usage. ``cost_tracker`` therefore reads the tokens from the
``task_complete``/``dispatch_governed`` record of the same ``(dispatch_id, attempt)``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))



def _load_cost_tracker():
    """Load scripts/cost_tracker.py by path: scripts/lib/cost_tracker.py shares the basename."""
    path = VNX_ROOT / "scripts" / "cost_tracker.py"
    spec = importlib.util.spec_from_file_location("v3_tokens_cost_tracker_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cost_tracker = _load_cost_tracker()

MODEL = "claude-sonnet-5-5"
USAGE_V2 = {"input": 12, "output": 3219, "cache_hit": 390544}


def _metrics(tmp_path: Path, lines: list) -> dict:
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    receipts, invalid = cost_tracker._load_receipts(ledger)
    assert invalid == 0
    return cost_tracker.build_metrics(receipts, invalid, tmp_path, ledger)


def _started(dispatch_id: str, attempt: int = 1) -> dict:
    return {"event_type": "dispatch_started", "dispatch_id": dispatch_id, "attempt": attempt}


def _finished(dispatch_id: str, attempt: int = 1, **extra) -> dict:
    return {"event_type": "dispatch_finished", "dispatch_id": dispatch_id, "attempt": attempt,
            "end_state": "succeeded", "terminal": "T1", **extra}


def _bucket(metrics: dict) -> dict:
    return metrics["by_model"][MODEL]


def test_task_complete_v2_tokens_book_once(tmp_path):
    metrics = _metrics(tmp_path, [
        _started("d-a"), _finished("d-a"),
        {"event_type": "task_complete", "dispatch_id": "d-a", "attempt": 1, "model": MODEL,
         "token_usage": USAGE_V2},
    ])
    totals = metrics["totals"]
    assert totals["events_analyzed"] == 1
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (12, 3219, 3231)
    assert totals["receipts_with_known_tokens"] == 1
    assert totals["receipts_with_unknown_tokens"] == 0
    assert _bucket(metrics)["total_tokens"] == 3231


def test_dispatch_governed_record_books_the_tokens(tmp_path):
    metrics = _metrics(tmp_path, [
        _started("d-b"), _finished("d-b"),
        {"event_type": "dispatch_governed", "dispatch_id": "d-b", "attempt": 1, "model": MODEL,
         "token_usage": USAGE_V2},
    ])
    totals = metrics["totals"]
    assert totals["events_analyzed"] == 1
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (12, 3219, 3231)
    assert _bucket(metrics)["receipts_with_known_tokens"] == 1


def test_legacy_task_complete_books_as_before(tmp_path):
    metrics = _metrics(tmp_path, [
        {"event_type": "task_complete", "dispatch_id": "d-c", "model": MODEL,
         "input_tokens": 100, "output_tokens": 50},
    ])
    totals = metrics["totals"]
    assert totals["events_analyzed"] == 1
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (100, 50, 150)
    assert _bucket(metrics)["total_tokens"] == 150


def test_v3_attempt_without_governance_record_books_unknown_not_zero(tmp_path):
    metrics = _metrics(tmp_path, [_started("d-d"), _finished("d-d", model=MODEL)])
    totals = metrics["totals"]
    assert totals["events_analyzed"] == 1
    assert totals["receipts_with_unknown_tokens"] == 1
    assert totals["receipts_with_known_tokens"] == 0
    assert totals["total_tokens"] == 0
    assert _bucket(metrics)["receipts_with_unknown_tokens"] == 1


def test_governance_record_without_tokens_is_not_used(tmp_path):
    metrics = _metrics(tmp_path, [
        _finished("d-g", model=MODEL),
        {"event_type": "dispatch_governed", "dispatch_id": "d-g", "attempt": 1, "token_usage": {}},
        {"event_type": "dispatch_governed", "dispatch_id": "d-g", "attempt": 1,
         "token_usage": {"input": "x", "output": None}},
    ])
    assert metrics["totals"]["receipts_with_unknown_tokens"] == 1
    assert metrics["totals"]["total_tokens"] == 0


def test_two_attempts_each_book_their_own_record(tmp_path):
    metrics = _metrics(tmp_path, [
        _started("d-e", 1), _finished("d-e", 1, end_state="failed"),
        {"event_type": "task_complete", "dispatch_id": "d-e", "attempt": 1, "model": MODEL,
         "token_usage": {"input": 10, "output": 20}},
        _started("d-e", 2), _finished("d-e", 2),
        {"event_type": "task_complete", "dispatch_id": "d-e", "attempt": 2, "model": MODEL,
         "token_usage": {"input": 100, "output": 200}},
    ])
    totals = metrics["totals"]
    assert totals["events_analyzed"] == 2
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (110, 220, 330)
    assert _bucket(metrics)["events"] == 2


def test_governance_record_of_another_attempt_is_not_borrowed(tmp_path):
    metrics = _metrics(tmp_path, [
        _finished("d-f", 2, model=MODEL),
        {"event_type": "task_complete", "dispatch_id": "d-f", "attempt": 1, "model": MODEL,
         "token_usage": {"input": 10, "output": 20}},
    ])
    totals = metrics["totals"]
    # attempt 2 has no record of its own; attempt 1's task_complete is its own attempt's witness.
    assert totals["events_analyzed"] == 2
    assert totals["receipts_with_unknown_tokens"] == 1
    assert totals["total_tokens"] == 30


def test_model_is_taken_from_governance_record_only_when_witness_has_none(tmp_path):
    metrics = _metrics(tmp_path, [
        _finished("d-h"),
        {"event_type": "dispatch_governed", "dispatch_id": "d-h", "model": MODEL,
         "token_usage": {"input": 1, "output": 2}},
        _finished("d-i", model="claude-opus-5-5"),
        {"event_type": "dispatch_governed", "dispatch_id": "d-i", "model": MODEL,
         "token_usage": {"input": 4, "output": 8}},
    ])
    assert MODEL in metrics["by_model"]
    assert metrics["by_model"][MODEL]["total_tokens"] == 3
    assert metrics["by_model"]["claude-opus-5-5"]["total_tokens"] == 12


def test_extract_tokens_reads_receipt_v2_keys_without_cache(tmp_path):
    assert cost_tracker._extract_tokens({"token_usage": USAGE_V2}) == (12, 3219, 3231)
    assert cost_tracker._extract_tokens({"token_usage": {"input": 1}}) == (1, None, None)
