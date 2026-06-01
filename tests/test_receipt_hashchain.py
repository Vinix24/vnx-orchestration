#!/usr/bin/env python3
"""GAP 3b: per-append hash-chain wiring tests (flag-gated VNX_CHAIN_RECEIPTS).

Covers:
  - flag OFF: append behavior unchanged (no prev_hash field) — byte-for-byte.
  - flag ON: appends carry prev_hash; verify_chain passes on the result.
  - CONCURRENCY (the critical one): 12 concurrent processes each append a
    receipt under the lock with the flag ON; verify_chain passes (zero forks:
    every prev_hash matches the prior entry's hash, no duplicate prev_hash,
    no GENESIS after line 1).
  - replay/idempotency: re-running verify on the chained file is stable.

The concurrency test exercises the exact correctness invariant from the spec:
read-tail + stamp + write + cache-update must be serialized under the single
append lock or concurrent appends fork the chain.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _compute_idempotency_key,
    _write_receipt_under_lock,
)
from ndjson_hash_chain import GENESIS_HASH, compute_entry_hash, verify_chain  # noqa: E402


def _append_one(receipt_path: Path, receipt: dict) -> None:
    """Append a single receipt through the canonical lock-scoped writer."""
    cache_path = _cache_file_for(receipt_path)
    key = _compute_idempotency_key(receipt, receipt.get("event_type", "test_event"))
    _write_receipt_under_lock(
        receipt,
        receipt_path,
        cache_path,
        key,
        cache_window_seconds=300,
    )


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------- #
# Flag OFF: byte-for-byte unchanged, no prev_hash field
# --------------------------------------------------------------------------- #
def test_flag_off_no_prev_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
    receipt_path = tmp_path / "t0_receipts.ndjson"

    for i in range(5):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    assert len(entries) == 5
    for entry in entries:
        assert "prev_hash" not in entry, "flag OFF must not stamp prev_hash"


def test_flag_off_explicit_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "0")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": "d0", "n": 0})
    entries = _read_lines(receipt_path)
    assert "prev_hash" not in entries[0]


# --------------------------------------------------------------------------- #
# Flag ON: chained, verify_chain passes
# --------------------------------------------------------------------------- #
def test_flag_on_chains_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
    receipt_path = tmp_path / "t0_receipts.ndjson"

    for i in range(6):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    assert len(entries) == 6

    # First entry is genesis, every subsequent entry chains to the prior body.
    assert entries[0]["prev_hash"] == GENESIS_HASH
    for idx in range(1, len(entries)):
        assert entries[idx]["prev_hash"] == compute_entry_hash(entries[idx - 1])

    is_valid, violations = verify_chain(receipt_path)
    assert is_valid, f"chain should verify, violations: {violations}"


def test_flag_on_genesis_only_first_line(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "true")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    for i in range(4):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    genesis_count = sum(1 for e in entries if e.get("prev_hash") == GENESIS_HASH)
    assert genesis_count == 1, "GENESIS_HASH must appear only on line 1"


# --------------------------------------------------------------------------- #
# CONCURRENCY: the critical fork-test
# --------------------------------------------------------------------------- #
def _concurrent_worker(receipt_path_str: str, worker_id: int) -> None:
    """Subprocess body: enable flag, append one receipt under the lock."""
    os.environ["VNX_CHAIN_RECEIPTS"] = "1"
    # Re-resolve imports inside the spawned process.
    for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from append_receipt_internals.idempotency import (
        _cache_file_for as cf,
        _compute_idempotency_key as ck,
        _write_receipt_under_lock as wr,
    )

    receipt_path = Path(receipt_path_str)
    receipt = {
        "event_type": "test_event",
        "dispatch_id": f"worker-{worker_id}",
        "worker": worker_id,
        "pid": os.getpid(),
    }
    cache_path = cf(receipt_path)
    key = ck(receipt, "test_event")
    wr(receipt, receipt_path, cache_path, key, cache_window_seconds=300)


@pytest.mark.parametrize("n_workers", [8, 16])
def test_concurrent_appends_no_fork(tmp_path, n_workers):
    receipt_path = tmp_path / f"t0_receipts_{n_workers}.ndjson"

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_concurrent_worker, args=(str(receipt_path), wid))
        for wid in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited non-zero: {p.exitcode}"

    entries = _read_lines(receipt_path)
    assert len(entries) == n_workers, "every worker must have appended exactly once"

    # 1. verify_chain passes — the single source of truth for "no fork".
    is_valid, violations = verify_chain(receipt_path)
    assert is_valid, f"CHAIN FORKED under concurrency, violations: {violations}"

    # 2. Exactly one GENESIS, on line 1.
    assert entries[0]["prev_hash"] == GENESIS_HASH
    genesis_count = sum(1 for e in entries if e.get("prev_hash") == GENESIS_HASH)
    assert genesis_count == 1, "fork: GENESIS_HASH appears more than once"

    # 3. No duplicate prev_hash (a fork manifests as two entries sharing a parent).
    prev_hashes = [e["prev_hash"] for e in entries]
    assert len(prev_hashes) == len(set(prev_hashes)), "fork: duplicate prev_hash"

    # 4. Each prev_hash matches the prior entry's body hash (explicit, beyond verify).
    for idx in range(1, len(entries)):
        assert entries[idx]["prev_hash"] == compute_entry_hash(entries[idx - 1])


# --------------------------------------------------------------------------- #
# Replay / idempotency: verify is stable across repeated runs
# --------------------------------------------------------------------------- #
def test_verify_is_stable_on_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    for i in range(5):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    first = verify_chain(receipt_path)
    second = verify_chain(receipt_path)
    third = verify_chain(receipt_path)
    assert first[0] is True
    assert first == second == third, "verify must be deterministic across replays"
