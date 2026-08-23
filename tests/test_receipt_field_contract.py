"""OI-1408 — per-receipt_kind field contract (append_receipt_internals/validation.py).

Two things measured on the real ledger (see dispatch
20260823-alpha-a2-receipt-veldcontract):

1. ``model`` was the only receipt field with a fail-closed, gap-free content
   check. Every other field's presence check (dispatch_id/event_type) is
   structural — it fails on an absent KEY but is blind to a present-but-empty
   value or a sentinel literal that reads as filled but isn't. 3903 dispatch
   receipts measured: 551 carried the literal "unknown" as task_id, 3352
   never carried the key, ZERO carried a real value.

2. The OI-1382/1383 tmux-lane fallback (report_to_receipt_converter.py) could
   book a task_complete receipt with an empty status — a row with no
   readable outcome. See test_report_to_receipt_converter.py for the write-
   side fix (exit_code fallback + "no_signal" stamp); this file tests the
   append-time BACKSTOP in validation.py, which refuses an empty/absent
   status on a dispatch-kind receipt regardless of which writer produced it.

``task_id`` decision (OI-1408): retired from the contract, not enforced.
Given zero real values across the entire measured ledger, the field never
carried a task concept distinct from dispatch_id — see
report_to_receipt_converter.py / report_parser.py for the write-side fix
(omit the key instead of stamping "unknown").
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.payload import append_receipt_payload

# Importing append_receipt registers the append facade used by
# append_receipt_payload (mirrors test_model_ssot_and_chainlink.py's pattern).
import append_receipt  # noqa: E402, F401


def _append(tmp_path: Path, receipt: dict, name: str = "t0_receipts.ndjson") -> dict:
    rf = tmp_path / name
    append_receipt_payload(receipt, receipts_file=str(rf), skip_enrichment=True)
    return json.loads(rf.read_text().splitlines()[-1])


_BASE = {
    "timestamp": "2026-08-23T00:00:00Z",
    "dispatch_id": "d-oi1408-status",
    "terminal": "T1",
    "receipt_kind": "dispatch",
    "model": "sonnet",
}


class TestStatusFieldContract:
    """The append-time backstop: a dispatch-kind receipt claiming a terminal
    outcome (task_complete/task_failed/task_timeout/task_blocked/
    subprocess_completion/report_contract_invalid) must carry a real,
    non-empty status."""

    def test_task_complete_without_status_key_rejected(self, tmp_path):
        receipt = {**_BASE, "event_type": "task_complete"}
        with pytest.raises(AppendReceiptError) as exc_info:
            _append(tmp_path, receipt)
        assert exc_info.value.code == "missing_status"

    def test_task_complete_with_empty_status_rejected(self, tmp_path):
        receipt = {**_BASE, "event_type": "task_complete", "status": ""}
        with pytest.raises(AppendReceiptError) as exc_info:
            _append(tmp_path, receipt)
        assert exc_info.value.code == "missing_status"

    def test_task_failed_without_status_rejected(self, tmp_path):
        receipt = {**_BASE, "event_type": "task_failed"}
        with pytest.raises(AppendReceiptError) as exc_info:
            _append(tmp_path, receipt)
        assert exc_info.value.code == "missing_status"

    def test_task_complete_with_real_status_accepted(self, tmp_path):
        line = _append(tmp_path, {**_BASE, "event_type": "task_complete", "status": "success"})
        assert line["status"] == "success"

    def test_task_complete_with_no_signal_literal_accepted(self, tmp_path):
        # The write-side residual fallback (report_to_receipt_converter.py)
        # stamps this literal when neither status nor exit_code is usable —
        # it must pass the append-time gate, since it IS a readable outcome.
        line = _append(tmp_path, {**_BASE, "event_type": "task_complete", "status": "no_signal"})
        assert line["status"] == "no_signal"

    def test_status_unknown_is_not_treated_as_a_sentinel(self, tmp_path):
        # "unknown" is a recognized ignorable literal in the canonical status
        # vocabulary (event_outcome_semantics), unlike task_id/model, where
        # "unknown" IS a sentinel. Must not be rejected here.
        line = _append(tmp_path, {**_BASE, "event_type": "task_complete", "status": "unknown"})
        assert line["status"] == "unknown"

    def test_non_completion_event_type_exempt(self, tmp_path):
        # task_started precedes any outcome by construction — no status yet
        # is not a defect.
        line = _append(tmp_path, {**_BASE, "event_type": "task_started"})
        assert "status" not in line

    def test_model_exempt_source_exempt_from_status_too(self, tmp_path):
        # Corrective/system writers (_MODEL_EXEMPT_SOURCES) are exempt from
        # the status requirement for the same reason they're exempt from
        # model: their receipts override a worker's own claim, and refusing
        # them on a missing content field would lose the rejection signal.
        line = _append(tmp_path, {
            "timestamp": "2026-08-23T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-oi1408-exempt",
            "terminal": "T0",
            "source": "vnx_governance",
            "receipt_kind": "dispatch",
        })
        assert line["source"] == "vnx_governance"
        assert "status" not in line or line.get("status") in ("", None)

    def test_non_dispatch_receipt_kind_exempt(self, tmp_path):
        # The contract is scoped to receipt_kind="dispatch" only (OI-1408
        # begins there — the kind that carries the audit trail).
        line = _append(tmp_path, {
            "timestamp": "2026-08-23T00:00:00Z",
            "event_type": "state_mutation",
            "dispatch_id": "d-oi1408-state",
            "terminal": "T0",
            "receipt_kind": "state_mutation",
        })
        assert line["receipt_kind"] == "state_mutation"


class TestTaskIdNotInContract:
    """OI-1408 decision: task_id carries no fail-closed requirement — a
    dispatch-kind receipt is accepted whether task_id is absent, empty, or a
    sentinel. The field is retired, not silently required."""

    def test_dispatch_receipt_without_task_id_accepted(self, tmp_path):
        line = _append(tmp_path, {**_BASE, "event_type": "task_complete", "status": "success"})
        assert "task_id" not in line

    def test_dispatch_receipt_with_sentinel_task_id_still_accepted(self, tmp_path):
        # The contract does not reject a sentinel task_id (unlike model) —
        # the field is simply not checked. Write-side omission is enforced
        # in report_to_receipt_converter.py / report_parser.py instead, not
        # at this append-time gate.
        line = _append(tmp_path, {
            **_BASE, "event_type": "task_complete", "status": "success", "task_id": "unknown",
        })
        assert line["task_id"] == "unknown"
