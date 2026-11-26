#!/usr/bin/env python3
"""GAP 3b: per-append hash-chain wiring tests (flag-gated VNX_HASH_CHAIN_REQUIRED).

Covers:
  - flag OFF: append behavior unchanged (no prev_hash field) — byte-for-byte.
  - flag ON: appends carry prev_hash; verify_chain passes on the result.
  - CONCURRENCY (the critical one): 8 concurrent processes each append a
    receipt under the lock with the flag ON; verify_chain passes (zero forks:
    every prev_hash matches the prior entry's hash, no duplicate prev_hash,
    no GENESIS after line 1).
  - replay/idempotency: re-running verify on the chained file is stable.

The concurrency test exercises the exact correctness invariant from the spec:
read-tail + stamp + write + cache-update must be serialized under the single
append lock or concurrent appends fork the chain.

``VNX_TEST_NO_APPEND_LOCK=1`` is the red run of that invariant: every child
appends without the lock and is held at the read-tail point until all have
read it, so the chain forks and the no-fork test fails. Unset (CI, and every
normal run) the children take the lock production takes.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    sys.path.insert(0, _p)

from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _compute_idempotency_key,
    _write_receipt_under_lock,
)
from ndjson_hash_chain import GENESIS_HASH, compute_entry_hash, verify_chain  # noqa: E402
from append_receipt_internals.common import AppendReceiptError
from chain_epoch_seal import seal_ledger


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
    monkeypatch.delenv("VNX_HASH_CHAIN_REQUIRED", raising=False)
    receipt_path = tmp_path / "t0_receipts.ndjson"

    for i in range(5):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    assert len(entries) == 5
    for entry in entries:
        assert "prev_hash" not in entry, "flag OFF must not stamp prev_hash"


def test_flag_off_explicit_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HASH_CHAIN_REQUIRED", "0")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": "d0", "n": 0})
    entries = _read_lines(receipt_path)
    assert "prev_hash" not in entries[0]


# --------------------------------------------------------------------------- #
# Flag ON: chained, verify_chain passes
# --------------------------------------------------------------------------- #
def test_flag_on_chains_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HASH_CHAIN_REQUIRED", "1")
    receipt_path = tmp_path / "t0_receipts.ndjson"

    for i in range(6):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    assert len(entries) == 6

    # First entry is genesis, every subsequent entry chains to the prior body.
    assert entries[0]["prev_hash"] == GENESIS_HASH
    for idx in range(1, len(entries)):
        assert entries[idx]["prev_hash"] == compute_entry_hash(entries[idx - 1])

    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid, f"chain should verify, violations: {violations}"
    assert status == "verified"


def test_flag_on_genesis_only_first_line(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HASH_CHAIN_REQUIRED", "true")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    for i in range(4):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    entries = _read_lines(receipt_path)
    genesis_count = sum(1 for e in entries if e.get("prev_hash") == GENESIS_HASH)
    assert genesis_count == 1, "GENESIS_HASH must appear only on line 1"


# --------------------------------------------------------------------------- #
# CONCURRENCY: the critical fork-test
# --------------------------------------------------------------------------- #
# The red run of the no-fork test: every child appends without the append lock.
_NO_APPEND_LOCK_ENV = "VNX_TEST_NO_APPEND_LOCK"


def _hold_every_writer_at_the_read_tail(idempotency, rendezvous: Path, worker_id: int, n_workers: int) -> None:
    """Hold each lock-less writer at the read-tail point until every writer has read it.

    Without the lock two writers can read the same tail before either writes; this makes
    that window as wide as the writer count instead of a race of luck, so the red run
    (``VNX_TEST_NO_APPEND_LOCK=1``) forks the chain every time.
    """
    read_the_tail = idempotency._last_hash_under_lock
    rendezvous.mkdir(parents=True, exist_ok=True)

    def read_then_wait_for_the_others(receipt_path: Path) -> str:
        tail = read_the_tail(receipt_path)
        (rendezvous / f"read-{worker_id}").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 30
        while len(list(rendezvous.iterdir())) < n_workers:
            if time.monotonic() >= deadline:
                raise RuntimeError("not every writer reached the read-tail point")
            time.sleep(0.01)
        return tail

    idempotency._last_hash_under_lock = read_then_wait_for_the_others


def _concurrent_worker(receipt_path_str: str, worker_id: int, n_workers: int) -> None:
    """Subprocess body: enable the flag, append one receipt under the lock production takes.

    With ``VNX_TEST_NO_APPEND_LOCK=1`` the lock is a no-op here: the red run.
    """
    os.environ["VNX_HASH_CHAIN_REQUIRED"] = "1"
    # Re-resolve imports inside the spawned process.
    for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
        sys.path.insert(0, _p)
    from append_receipt_internals import idempotency
    import config_runtime
    assert Path(config_runtime.__file__).resolve() == LIB_DIR / "config_runtime.py", config_runtime.__file__

    receipt_path = Path(receipt_path_str)
    if os.environ.get(_NO_APPEND_LOCK_ENV) == "1":
        idempotency._take_the_append_lock = lambda *_args, **_kwargs: None
        _hold_every_writer_at_the_read_tail(idempotency, receipt_path.parent / "read-tail", worker_id, n_workers)

    receipt = {
        "event_type": "test_event",
        "dispatch_id": f"worker-{worker_id}",
        "worker": worker_id,
        "pid": os.getpid(),
    }
    cache_path = idempotency._cache_file_for(receipt_path)
    key = idempotency._compute_idempotency_key(receipt, "test_event")
    idempotency._write_receipt_under_lock(receipt, receipt_path, cache_path, key, cache_window_seconds=300)


# Eight writers are enough: the race needs concurrency, not a number. Two writers that read
# the same tail already fork the chain, and eight spawned processes interleave far more than
# two; a sixteenth adds spawn time and no new interleaving the eight have not shown.
@pytest.mark.parametrize("n_workers", [8])
@pytest.mark.parametrize("foreign_lib", [False, True])
def test_concurrent_appends_no_fork(tmp_path, monkeypatch, n_workers, foreign_lib):
    receipt_path = tmp_path / f"t0_receipts_{n_workers}.ndjson"
    if foreign_lib:
        foreign = tmp_path / "foreign-lib"
        foreign.mkdir()
        (foreign / "config_runtime.py").write_text(
            'raise AssertionError("foreign config_runtime imported")\n')
        monkeypatch.syspath_prepend(str(foreign))

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_concurrent_worker, args=(str(receipt_path), wid, n_workers))
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
    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid, f"CHAIN FORKED under concurrency, violations: {violations}"
    assert status == "verified"

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
    monkeypatch.setenv("VNX_HASH_CHAIN_REQUIRED", "1")
    receipt_path = tmp_path / "t0_receipts.ndjson"
    for i in range(5):
        _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{i}", "n": i})

    first = verify_chain(receipt_path)
    second = verify_chain(receipt_path)
    third = verify_chain(receipt_path)
    assert first[0] is True
    assert first[2] == "verified"
    assert first == second == third, "verify must be deterministic across replays"



# ADR-083 (receipt-hashketen-aan H3): the ledger's own tail decides, the variable is unset.
_PINNED_LINE = (b'{"event_type":"test_event","dispatch_id":"d1","n":1,'
                b'"outcome_id":"bee1b5622c59434a0d6a892c856fdf4aa7c11e60ee2113b79f0a8871a2530732"}\n')


def _ledger(tmp_path: Path, monkeypatch, *, sealed: bool) -> tuple[Path, str]:
    monkeypatch.delenv("VNX_HASH_CHAIN_REQUIRED", raising=False)
    receipt_path = tmp_path / "t0_receipts.ndjson"
    receipt_path.write_bytes(b'{"a":1}\n')
    return receipt_path, (seal_ledger(receipt_path)["marker_hash"] if sealed else "")


def _append_n(receipt_path: Path, n: int) -> None:
    _append_one(receipt_path, {"event_type": "test_event", "dispatch_id": f"d{n}", "n": n})


def test_sealed_ledger_chains_without_the_variable(tmp_path, monkeypatch):
    receipt_path, marker_hash = _ledger(tmp_path, monkeypatch, sealed=True)
    _append_n(receipt_path, 1)
    assert _read_lines(receipt_path)[-1]["prev_hash"] == marker_hash
    assert verify_chain(receipt_path) == (True, [], "verified-segmented")


def test_unchained_ledger_without_the_variable_is_byte_for_byte_as_before(tmp_path, monkeypatch):
    receipt_path, _ = _ledger(tmp_path, monkeypatch, sealed=False)
    _append_n(receipt_path, 1)
    assert receipt_path.read_bytes() == b'{"a":1}\n' + _PINNED_LINE


def test_debris_after_a_chained_entry_links_to_that_entry(tmp_path, monkeypatch):
    receipt_path, _ = _ledger(tmp_path, monkeypatch, sealed=True)
    _append_n(receipt_path, 1)
    chained_hash = compute_entry_hash(_read_lines(receipt_path)[-1])
    with receipt_path.open("ab") as fh:
        fh.write(b'{"torn": "fr\t#vnx-torn-tail-closed\n\nnot json\n{"also": "torn')
    _append_n(receipt_path, 2)
    last = json.loads(receipt_path.read_bytes().splitlines()[-1])
    assert (last["dispatch_id"], last["prev_hash"]) == ("d2", chained_hash)


def test_ambiguous_tail_refuses_and_leaves_the_ledger_unchanged(tmp_path, monkeypatch):
    receipt_path, _ = _ledger(tmp_path, monkeypatch, sealed=True)
    with receipt_path.open("ab") as fh:
        fh.write(b"x" * 65536 + b"\n")  # 64 KiB + 1 of debris
    before = receipt_path.read_bytes()
    with pytest.raises(AppendReceiptError) as refused:
        _append_n(receipt_path, 1)
    assert refused.value.code == "receipt_tail_ambiguous" and str(receipt_path) in refused.value.message
    assert receipt_path.read_bytes() == before


def test_ten_parallel_appends_after_a_seal_do_not_fork(tmp_path, monkeypatch):
    receipt_path, _ = _ledger(tmp_path, monkeypatch, sealed=True)
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda n: _append_n(receipt_path, n), range(10)))
    entries = _read_lines(receipt_path)
    assert len(entries) == 12 and len({e["prev_hash"] for e in entries[2:]}) == 10
    assert verify_chain(receipt_path) == (True, [], "verified-segmented")
