#!/usr/bin/env python3
"""ADR-038 — outcome identity + durable cross-window idempotency.

Dispatch-ID: 20260906-f14-uitkomstidentiteit

Two booking routes (the lane itself, and the report_to_receipt_converter
processing the same report later) both call ``append_receipt_payload`` for
the SAME dispatch outcome. The existing idempotency key
(``IDEMPOTENCY_FIELDS`` in idempotency.py) includes ``source``/``report_path``
— fields that differ BETWEEN the two routes by construction — so the same
outcome gets two different keys and lands as two ledger lines. The existing
dedup cache is also only a 300s window (``cache_window_seconds``), so even a
matching key would not catch a re-booking after a longer gap (a
re-processing run, hours later).

This module tests the fix: ``compute_outcome_id`` hashes ONLY the fields that
identify the OUTCOME (dispatch_id, event_type, status, commit_sha, gate,
pr_number) — never the booking-route fields — and a durable on-disk index
(``receipt_outcome_index.json``, sibling to the ledger) makes the dedup check
span the whole ledger, not a 5-minute cache window. A later receipt for the
same dispatch/event/gate/pr/commit but a DIFFERENT status is a correction
(ADR-005: corrections are new lines referencing the corrected event), not a
duplicate — it is booked with a ``supersedes`` pointer to the prior
outcome_id, not silently dropped and not silently merged.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _compute_idempotency_key,
    _write_receipt_under_lock,
)
from append_receipt_internals.outcome_identity import (  # noqa: E402
    OUTCOME_INDEX_FILENAME,
    compute_outcome_id,
    rebuild_outcome_index,
)


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append(receipt_path: Path, receipt: dict, *, window: int = 300):
    cache_path = _cache_file_for(receipt_path)
    key = _compute_idempotency_key(receipt, receipt.get("event_type", "task_complete"))
    return _write_receipt_under_lock(receipt, receipt_path, cache_path, key, window)


def _outcome_receipt(*, dispatch_id: str, status: str, source: str, report_path: str) -> dict:
    """Same OUTCOME, deliberately different booking-route fields (source,
    report_path) — the shape the two real booking routes actually produce
    for one dispatch completion."""
    return {
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "status": status,
        "terminal": "T1",
        "source": source,
        "report_path": report_path,
    }


# ---------------------------------------------------------------------------
# 1. Same outcome via two routes -> second append is 'duplicate', one line.
# ---------------------------------------------------------------------------


def test_same_outcome_via_two_routes_is_duplicate(tmp_path):
    receipt_path = tmp_path / "t0_receipts.ndjson"

    r_lane = _outcome_receipt(
        dispatch_id="disp-two-routes-A", status="success",
        source="governance_emit", report_path="",
    )
    r_converter = _outcome_receipt(
        dispatch_id="disp-two-routes-A", status="success",
        source="report_to_receipt_converter", report_path="/reports/disp-two-routes-A.md",
    )

    # Pre-condition: the two routes really do produce different SHORT-WINDOW
    # idempotency keys today (source/report_path differ) -- if this fails the
    # rest of the test is not exercising the bug it claims to fix.
    k_lane = _compute_idempotency_key(r_lane, "task_complete")
    k_converter = _compute_idempotency_key(r_converter, "task_complete")
    assert k_lane != k_converter, "pre-condition: routes must differ on the short-window key"

    res1 = _append(receipt_path, r_lane)
    res2 = _append(receipt_path, r_converter)

    assert res1.status == "appended"
    assert res2.status == "duplicate", (
        f"second route's booking of the SAME outcome was {res2.status!r}, expected 'duplicate'"
    )

    lines = _read_lines(receipt_path)
    assert len(lines) == 1, f"expected exactly one ledger line for one outcome, got {len(lines)}"
    assert lines[0]["outcome_id"] == compute_outcome_id(r_lane)


# ---------------------------------------------------------------------------
# 2. Durable across a cache_window expiry (not just a 300s coincidence).
# ---------------------------------------------------------------------------


def test_duplicate_survives_cache_window_expiry(tmp_path):
    receipt_path = tmp_path / "t0_receipts.ndjson"
    r1 = _outcome_receipt(
        dispatch_id="disp-window-expiry", status="success",
        source="governance_emit", report_path="",
    )
    r2 = _outcome_receipt(
        dispatch_id="disp-window-expiry", status="success",
        source="report_to_receipt_converter", report_path="/reports/disp-window-expiry.md",
    )

    res1 = _append(receipt_path, r1, window=1)
    assert res1.status == "appended"

    time.sleep(2)  # the 1-second short-window cache has now expired

    res2 = _append(receipt_path, r2, window=1)
    assert res2.status == "duplicate", (
        "outcome-identity dedup must be durable — it must not depend on the "
        "short cache_window_seconds still covering the first booking"
    )
    assert len(_read_lines(receipt_path)) == 1


# ---------------------------------------------------------------------------
# 3. Different status for the same dispatch -> booked as a correction.
# ---------------------------------------------------------------------------


def test_different_status_is_booked_as_correction(tmp_path):
    receipt_path = tmp_path / "t0_receipts.ndjson"
    r_unavailable = _outcome_receipt(
        dispatch_id="disp-correction", status="unavailable",
        source="governance_emit", report_path="",
    )
    r_pass = _outcome_receipt(
        dispatch_id="disp-correction", status="success",
        source="report_to_receipt_converter", report_path="/reports/disp-correction.md",
    )

    res1 = _append(receipt_path, r_unavailable)
    res2 = _append(receipt_path, r_pass)

    assert res1.status == "appended"
    assert res2.status == "appended", (
        "a genuinely different outcome (different status) for the same "
        "dispatch must be booked, never dropped as a duplicate (this is the "
        "OI-1639/2026-09-04 regression: an unavailable booking must never "
        "block a later real verdict)"
    )

    lines = _read_lines(receipt_path)
    assert len(lines) == 2
    first_outcome_id = compute_outcome_id(r_unavailable)
    second_outcome_id = compute_outcome_id(r_pass)
    assert lines[0]["outcome_id"] == first_outcome_id
    assert lines[1]["outcome_id"] == second_outcome_id
    assert lines[1].get("supersedes") == first_outcome_id, (
        "the second (differently-statused) booking for the same dispatch must "
        "reference the outcome it corrects, per ADR-005 (corrections are new "
        "lines that reference the corrected event by ID, never in-place edits)"
    )
    assert "supersedes" not in lines[0]


# ---------------------------------------------------------------------------
# 4. Two different gates on the same PR/dispatch -> two distinct outcome_ids,
#    both booked (no cross-gate collision).
# ---------------------------------------------------------------------------


def test_two_gates_on_same_pr_both_booked(tmp_path):
    receipt_path = tmp_path / "t0_receipts.ndjson"
    r_codex = {
        "event_type": "review_gate_result",
        "dispatch_id": "disp-two-gates",
        "status": "pass",
        "gate": "codex_gate",
        "pr_number": 1900,
        "source": "gate_result_parser",
    }
    r_kimi = {
        "event_type": "review_gate_result",
        "dispatch_id": "disp-two-gates",
        "status": "pass",
        "gate": "kimi_gate",
        "pr_number": 1900,
        "source": "gate_result_parser",
    }

    res1 = _append(receipt_path, r_codex)
    res2 = _append(receipt_path, r_kimi)

    assert res1.status == "appended"
    assert res2.status == "appended"

    lines = _read_lines(receipt_path)
    assert len(lines) == 2
    ids = {l["outcome_id"] for l in lines}
    assert len(ids) == 2, "two different gates on the same PR must get distinct outcome_ids"


# ---------------------------------------------------------------------------
# 5. --rebuild equivalent: rebuilding the index from a 3-line ledger yields 3
#    booked outcome ids.
# ---------------------------------------------------------------------------


def test_rebuild_from_ledger_yields_three_ids(tmp_path):
    receipt_path = tmp_path / "t0_receipts.ndjson"
    receipts = [
        {"event_type": "task_complete", "dispatch_id": "disp-r1", "status": "success"},
        {"event_type": "task_complete", "dispatch_id": "disp-r2", "status": "success"},
        {"event_type": "task_complete", "dispatch_id": "disp-r3", "status": "failure"},
    ]
    with receipt_path.open("w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r) + "\n")

    stats = rebuild_outcome_index(receipt_path, write=True)

    assert stats.booked == 3
    assert stats.duplicates == 0
    assert stats.corrections == 0

    index_path = receipt_path.parent / OUTCOME_INDEX_FILENAME
    assert index_path.exists()
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(index_data["outcomes"]) == 3
    expected_ids = {compute_outcome_id(r) for r in receipts}
    assert set(index_data["outcomes"].keys()) == expected_ids


def test_rebuild_dry_run_does_not_write_index(tmp_path):
    """The measurement pass over the real ledger must not touch disk."""
    receipt_path = tmp_path / "t0_receipts.ndjson"
    with receipt_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "task_complete", "dispatch_id": "d1", "status": "success"}) + "\n")
        fh.write(json.dumps({"event_type": "task_complete", "dispatch_id": "d1", "status": "success"}) + "\n")

    stats = rebuild_outcome_index(receipt_path, write=False)
    assert stats.booked == 1
    assert stats.duplicates == 1

    index_path = receipt_path.parent / OUTCOME_INDEX_FILENAME
    assert not index_path.exists(), "dry-run rebuild must never write the index file"


# ---------------------------------------------------------------------------
# 6. VNX_CHAIN_RECEIPTS=0/unset -> existing chain tests stay green, and the
#    append path writes no chain fields (outcome_id is the one deliberate new
#    field, independent of the chain flag).
# ---------------------------------------------------------------------------


def test_flag_off_no_chain_fields_but_has_outcome_id(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
    receipt_path = tmp_path / "t0_receipts.ndjson"
    r = {"event_type": "task_complete", "dispatch_id": "disp-chain-off", "status": "success"}

    res = _append(receipt_path, r)
    assert res.status == "appended"

    lines = _read_lines(receipt_path)
    assert len(lines) == 1
    assert "prev_hash" not in lines[0], "VNX_CHAIN_RECEIPTS is off — no chain field must be stamped"
    assert lines[0]["outcome_id"] == compute_outcome_id(r)
