"""OI-1370 — concurrency tests for migrate_phase3_envelope sentinel lock.

Verifies that the directory-level sentinel (.state.lock) prevents event loss
when _restamp_ndjson_inplace races concurrent dispatch_register writers.

Race condition being tested:
  BEFORE fix: writer opens NDJSON (inode X) → migrator locks inode X, renames
  to new inode Y → migrator releases inode-X lock → writer appends to
  unlinked inode X → event LOST from canonical log.

  AFTER fix: both migrator and writer acquire the same sentinel BEFORE opening
  the NDJSON file, so the writer always opens the current inode (Y or later).
"""
from __future__ import annotations

import fcntl
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# Make scripts/ and scripts/lib importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import migrate_phase3_envelope
from dispatch_register import append_dispatch_event
from migrate_phase3_envelope import _restamp_ndjson_inplace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_ENVELOPE: dict = {
    "operator_id": None,
    "project_id": "test-project",
    "orchestrator_id": None,
    "agent_id": None,
}


def _count_seq_lines(path: Path) -> int:
    """Return number of lines containing '"seq":' in path."""
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text().splitlines() if '"seq":' in ln)


# ---------------------------------------------------------------------------
# Test 1 — no event loss under concurrent writer + migrator
# ---------------------------------------------------------------------------


class TestMigrateEnvelopeConcurrency:
    """OI-1370 — verify writes during migration are not lost to unlinked inode."""

    def test_concurrent_writer_does_not_lose_events(self, tmp_path: Path) -> None:
        """Race 100 appends against a re-stamper run. All 100 must land in final file."""
        envelope = tmp_path / "dispatch_register.ndjson"
        # Seed one record so the migrator has something to restamp
        envelope.write_text('{"initial":true}\n', encoding="utf-8")

        events = [f'{{"dispatch_id":"test-d{i}","seq":{i}}}' for i in range(100)]
        results: list[str] = []
        results_lock = threading.Lock()

        def writer(event_line: str) -> None:
            try:
                append_dispatch_event(envelope, event_line)
                with results_lock:
                    results.append("ok")
            except Exception as exc:
                with results_lock:
                    results.append(f"err:{type(exc).__name__}")

        def migrator() -> None:
            time.sleep(0.01)  # let some writes happen first
            _restamp_ndjson_inplace(envelope, _EMPTY_ENVELOPE)

        with ThreadPoolExecutor(max_workers=20) as ex:
            write_futures = [ex.submit(writer, e) for e in events]
            mig_future = ex.submit(migrator)
            for f in write_futures:
                f.result(timeout=10)
            mig_future.result(timeout=10)

        seq_count = _count_seq_lines(envelope)
        # All 100 events must be present — none lost to an unlinked inode
        assert seq_count == 100, (
            f"lost events: got {seq_count}/100 in final file\n"
            f"writer errors: {[r for r in results if r != 'ok']}"
        )

    def test_directory_lock_serializes_with_migration(self, tmp_path: Path) -> None:
        """Directory-level sentinel blocks new appends while migration holds the lock."""
        envelope = tmp_path / "envelope.ndjson"
        envelope.write_text("init\n", encoding="utf-8")

        migration_held = threading.Event()
        write_done = threading.Event()
        write_start_time: list[float] = []
        write_end_time: list[float] = []

        # Sentinel path that both _write_event_locked and _restamp_ndjson_inplace use
        sentinel_path = envelope.parent / ".state.lock"

        def slow_migration_lock_holder() -> None:
            """Hold the sentinel lock for 0.5 s to simulate a slow migration."""
            with sentinel_path.open("a+", encoding="utf-8") as fp:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                migration_held.set()
                time.sleep(0.5)
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)

        t = threading.Thread(target=slow_migration_lock_holder)
        t.start()
        assert migration_held.wait(timeout=2), "migration never acquired the lock"

        # Writer should block until the sentinel is released.
        # Use a thread with a timeout so the test fails fast if locking regresses
        # rather than hanging until CI timeout.
        import concurrent.futures as _cf
        write_start_time.append(time.time())
        with ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(
                append_dispatch_event,
                envelope,
                '{"dispatch_id":"test-lock","event":"dispatch_promoted"}',
            )
            _done, _ = _cf.wait([_fut], timeout=5)
            if not _done:
                pytest.fail(
                    "append_dispatch_event blocked for >5s — sentinel locking may have regressed"
                )
            for f in _done:
                f.result()  # raises if append raised (e.g. after sentinel release with regressed locking)
        write_end_time.append(time.time())

        t.join(timeout=5)

        duration = write_end_time[0] - write_start_time[0]
        assert duration > 0.3, (
            f"write did not wait for migration lock (took {duration:.3f}s; expected >0.3s)"
        )

    def test_restamp_reads_all_pre_lock_events(self, tmp_path: Path) -> None:
        """Re-stamper must include all events written before it acquired the sentinel."""
        envelope = tmp_path / "dispatch_register.ndjson"
        # Write 20 events before any migration
        for i in range(20):
            append_dispatch_event(envelope, f'{{"dispatch_id":"test-d{i}","seq":{i},"phase":"pre"}}')

        _restamp_ndjson_inplace(envelope, _EMPTY_ENVELOPE)

        seq_count = _count_seq_lines(envelope)
        assert seq_count == 20, f"expected 20 pre-migration events, got {seq_count}"
        # Every line should now carry the project_id stamp
        for ln in envelope.read_text().splitlines():
            if '"seq":' not in ln:
                continue
            rec = json.loads(ln)
            assert rec.get("project_id") == "test-project", (
                f"missing project_id stamp on re-stamped line: {ln}"
            )

    def test_restamp_idempotent_under_double_run(self, tmp_path: Path) -> None:
        """Running _restamp_ndjson_inplace twice must not duplicate or lose events."""
        envelope = tmp_path / "dispatch_register.ndjson"
        for i in range(10):
            append_dispatch_event(envelope, f'{{"dispatch_id":"test-d{i}","seq":{i}}}')

        _restamp_ndjson_inplace(envelope, _EMPTY_ENVELOPE)
        _restamp_ndjson_inplace(envelope, _EMPTY_ENVELOPE)

        seq_count = _count_seq_lines(envelope)
        assert seq_count == 10, f"idempotency violation: expected 10, got {seq_count}"

    def test_no_events_lost_across_multiple_migrations(self, tmp_path: Path) -> None:
        """Multiple sequential migrations interleaved with writes must preserve all events."""
        envelope = tmp_path / "dispatch_register.ndjson"
        envelope.write_text("", encoding="utf-8")

        total = 0
        for batch in range(5):
            for i in range(10):
                append_dispatch_event(envelope, f'{{"dispatch_id":"test-b{batch}-s{i}","batch":{batch},"seq":{i}}}')
                total += 1
            _restamp_ndjson_inplace(envelope, _EMPTY_ENVELOPE)

        # Count lines with "batch":
        batch_lines = sum(
            1 for ln in envelope.read_text().splitlines() if '"batch":' in ln
        )
        assert batch_lines == total, (
            f"event loss across migrations: expected {total}, got {batch_lines}"
        )
