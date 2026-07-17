"""Regression test for _stamp_ingested_at (codex-gate fix-round #1184, Finding 2 HIGH).

append_receipt.py accepts worker-authored JSON directly, so a caller-supplied
`ingested_at` is exactly as forgeable as `timestamp`/`recorded_at`. The old
implementation returned early when `ingested_at` was already present on the
receipt, letting a worker pre-set an old value and defeat the staleness check
(`report_contract_scope.contract_invalid_effective_timestamp`) this field
exists to make forge-proof. `_stamp_ingested_at` must always overwrite it with
the processor-emit time, regardless of any pre-existing value.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import append_receipt_internals.payload as payload_mod  # noqa: E402


def test_t_adv7_preset_old_ingested_at_is_overwritten_with_fresh_time():
    """T-adv7: a receipt with a pre-set old ingested_at gets a fresh
    ingested_at after _stamp_ingested_at — a worker cannot forge it."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt = {"dispatch_id": "d-adv7-forged-ingested-at", "ingested_at": old_ts}

    before = datetime.now(timezone.utc)
    payload_mod._stamp_ingested_at(receipt)
    after = datetime.now(timezone.utc)

    assert receipt["ingested_at"] != old_ts
    stamped = datetime.strptime(receipt["ingested_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before - timedelta(seconds=5) <= stamped <= after + timedelta(seconds=5)


def test_missing_ingested_at_is_stamped():
    """Baseline: a receipt with no ingested_at at all still gets stamped."""
    receipt = {"dispatch_id": "d-adv7-no-ingested-at"}
    payload_mod._stamp_ingested_at(receipt)
    assert "ingested_at" in receipt
    datetime.strptime(receipt["ingested_at"], "%Y-%m-%dT%H:%M:%SZ")
