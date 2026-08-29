"""Two concurrent writers of one gate-result slot must not corrupt each other (OI-1486).

``gate_recorder`` persists every ``review_gates/results/pr-<N>-<gate>.json``
record through a read-check-write pair: ``_check_overwrite_guard`` reads the
existing record and decides, then ``_write_result_atomic`` writes. Two
defects made that pair unsafe for the concurrent writers the guard itself was
built for (OI-1469/OI-1470 names three of them landing in one slot with no
ordering guarantee):

1. ``_write_result_atomic`` derived its scratch file as
   ``result_path.with_suffix(".json.tmp")`` — a name fixed by the destination,
   so every concurrent writer of the same slot used the SAME scratch file.
   Writer A's content is overwritten by B before A renames, so A renames B's
   bytes into place and reports its own payload as landed; B then renames a
   path A already consumed and dies on FileNotFoundError. The writer that
   reports success is not the writer whose bytes are on disk.

2. Nothing held a lock across read-check-write, so two writers could both
   read the same "absent" state, both pass the guard, and both write. The
   guard's rule — a decided, evidenced verdict may only be replaced by
   another decided, evidenced verdict — is a statement about serialized
   writes; without a lock it does not hold.

Both tests below fail on the pre-fix code and pass after.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_recorder
from gate_recorder import write_result_guarded

# Wide enough that the write of one thread reliably straddles the write of the
# other. The defect is a shared scratch path, not a timing curiosity, but a
# one-line payload can slip through the window often enough to look green.
_FILLER = "x" * 200_000
_ROUNDS = 12


def _running_payload(marker: str) -> dict:
    """A non-terminal record. The overwrite guard returns early on these, so
    any exception or mixed content is the write path itself, never a refusal."""
    return {
        "gate": "codex_gate",
        "pr_id": "1486",
        "status": "running",
        "marker": marker,
        "filler": _FILLER,
    }


def test_concurrent_writers_never_report_a_write_that_did_not_land(tmp_path):
    """No writer may raise, and disk must hold exactly one writer's payload."""
    result_path = tmp_path / "results" / "pr-1486-codex_gate.json"
    errors: list[BaseException] = []
    landed: list[str] = []
    lock = threading.Lock()

    def writer(marker: str, barrier: threading.Barrier) -> None:
        for _ in range(_ROUNDS):
            payload = _running_payload(marker)
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
            try:
                _, written = write_result_guarded(
                    result_path, payload, gate="codex_gate", pr_ref="1486"
                )
                if written:
                    with lock:
                        landed.append(marker)
            except BaseException as exc:  # noqa: BLE001 - the defect IS the exception
                with lock:
                    errors.append(exc)
                return

    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=writer, args=(m, barrier), daemon=True)
        for m in ("aaaaaa", "bbbbbb")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), (
        "a writer thread was still running after the join timeout — the lock "
        "this test exercises can deadlock, and a join that only times out "
        "reports that as success"
    )

    assert not errors, (
        "a concurrent writer raised instead of writing: "
        f"{[repr(e) for e in errors]} — a scratch path shared between writers "
        "lets one rename away the file the other is about to rename"
    )
    assert landed, "no writer reported a landed write"

    on_disk = json.loads(result_path.read_text(encoding="utf-8"))
    assert on_disk["marker"] in {"aaaaaa", "bbbbbb"}
    assert on_disk["filler"] == _FILLER, (
        "the record on disk is truncated or mixed — two writers shared one "
        "scratch file"
    )


def test_overwrite_guard_holds_under_two_concurrent_writers(tmp_path):
    """A decided, evidenced verdict survives a concurrent evidence-less write.

    Both threads are forced to read the same absent state before either
    writes, which is exactly what an unlocked read-check-write pair allows.
    The guard's rule must still hold: whatever the interleaving, the evidenced
    ``pass`` is what remains on disk — it may overwrite the evidence-less
    ``not_executable`` (that record is terminal but not decided), and it may
    never be overwritten BY it.
    """
    result_path = tmp_path / "results" / "pr-1486-glm_gate.json"

    evidenced_pass = {
        "gate": "glm_gate",
        "pr_id": "1486",
        "status": "pass",
        "contract_hash": "088a30754169bb91",
        "report_path": "/tmp/glm-report.md",
        "dispatch_id": "glm-gate-pr1486-1787999000",
    }
    evidence_less = {
        "gate": "glm_gate",
        "pr_id": "1486",
        "status": "not_executable",
        "reason": "provider_unavailable",
    }

    # Force both threads to complete their read of the slot before either
    # writes. Under a lock held across read-check-write the second thread
    # cannot reach this point until the first has finished, so the barrier
    # times out instead of syncing — that is the passing shape, not a failure.
    stale_read_barrier = threading.Barrier(2)
    synced: set[int] = set()
    sync_lock = threading.Lock()
    real_read = gate_recorder._read_existing_result

    def read_then_sync_once(path: Path):
        value = real_read(path)
        tid = threading.get_ident()
        with sync_lock:
            first_time = tid not in synced
            synced.add(tid)
        if first_time:
            try:
                stale_read_barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return value

    gate_recorder._read_existing_result = read_then_sync_once
    errors: list[BaseException] = []
    try:
        def writer(payload: dict) -> None:
            try:
                write_result_guarded(
                    result_path, payload, gate="glm_gate", pr_ref="1486"
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(p,), daemon=True)
            for p in (evidenced_pass, evidence_less)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        stuck = [t for t in threads if t.is_alive()]
    finally:
        gate_recorder._read_existing_result = real_read

    assert not stuck, (
        "a writer thread was still running after the join timeout — two "
        "writers contending for one slot lock is exactly the shape that "
        "deadlocks, and a bare join would let that pass"
    )

    assert not errors, f"a writer raised: {[repr(e) for e in errors]}"

    on_disk = json.loads(result_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "pass", (
        "an evidence-less not_executable erased a decided, evidenced verdict — "
        "the overwrite guard read the slot outside the lock that protects the "
        "write, so both writers passed it against the same stale state"
    )
    assert on_disk.get("contract_hash") == "088a30754169bb91"
