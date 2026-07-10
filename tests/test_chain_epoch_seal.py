#!/usr/bin/env python3
"""Tests for the ADR-029 chain-epoch seal migration (scripts/chain_epoch_seal.py)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from ndjson_hash_chain import append_chained_entry, verify_chain  # noqa: E402

# Load scripts/chain_epoch_seal.py (not importable as a package name).
_spec = importlib.util.spec_from_file_location(
    "chain_epoch_seal", REPO / "scripts" / "chain_epoch_seal.py"
)
seal_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seal_mod)  # type: ignore[union-attr]
seal_ledger = seal_mod.seal_ledger


def _write_unchained(path: Path, n: int) -> None:
    for i in range(n):
        with path.open("a") as f:
            f.write(json.dumps({"seq": i, "old": True}) + "\n")


def test_seal_unchained_ledger_opens_epoch_1_and_verifies_segmented(tmp_path):
    p = tmp_path / "t0_receipts.ndjson"
    _write_unchained(p, 3)
    res = seal_ledger(p)
    assert res["action"] == "sealed" and res["epoch"] == 1
    assert res["status"] == "verified-segmented"
    # And a subsequent chained append still verifies as segmented.
    append_chained_entry(p, {"seq": 3})
    ok, _v, status = verify_chain(p)
    assert ok is True and status == "verified-segmented"


def test_seal_is_idempotent(tmp_path):
    p = tmp_path / "t0_receipts.ndjson"
    _write_unchained(p, 2)
    first = seal_ledger(p)
    assert first["action"] == "sealed"
    lines_after_first = p.read_text().splitlines()
    # Second run must be a no-op (ledger is now chaining).
    second = seal_ledger(p)
    assert second["action"] == "noop"
    assert p.read_text().splitlines() == lines_after_first  # nothing appended


def test_seal_is_append_only(tmp_path):
    """The pre-adoption history lines are byte-identical after sealing (ADR-005)."""
    p = tmp_path / "t0_receipts.ndjson"
    _write_unchained(p, 3)
    original = p.read_text().splitlines()
    seal_ledger(p)
    after = p.read_text().splitlines()
    assert after[: len(original)] == original          # history untouched
    assert len(after) == len(original) + 1             # exactly one marker appended
    assert json.loads(after[-1])["type"] == "chain_epoch_start"


def test_seal_empty_or_absent_is_noop(tmp_path):
    p = tmp_path / "absent.ndjson"
    assert seal_ledger(p)["action"] == "noop"
    p.touch()
    assert seal_ledger(p)["action"] == "noop"


def test_seal_next_epoch_numbering(tmp_path):
    """A ledger that already has epoch 1 but ends on an unchained entry seals epoch 2."""
    p = tmp_path / "t0_receipts.ndjson"
    _write_unchained(p, 1)
    seal_ledger(p)                       # opens epoch 1
    append_chained_entry(p, {"seq": 1})
    # Simulate a later unchained tail (rotation) to force a re-seal.
    with p.open("a") as f:
        f.write(json.dumps({"seq": 2, "tail": True}) + "\n")
    res = seal_ledger(p)
    assert res["action"] == "sealed" and res["epoch"] == 2
