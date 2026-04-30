#!/usr/bin/env python3
"""CFX-4: fcntl-locked RMW for events/worker_health.json.

Covers ``scripts/lib/file_locking.py:file_locked_rmw`` directly:
  - Case A: single-writer correctness (no race)
  - Case B: concurrent writers — 5 threads each updating different keys land all updates
  - Case C: lock released on exception (subsequent writer can proceed)
  - Case D: idempotent — re-running on the same file produces the same result
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from file_locking import file_locked_rmw  # noqa: E402


class TestSingleWriter(unittest.TestCase):
    """Case A: single-writer RMW round-trips correctly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "worker_health.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_file_on_first_write(self) -> None:
        self.assertFalse(self.path.exists())
        with file_locked_rmw(self.path) as data:
            data["T1"] = {"status": "active", "events": 1}
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, {"T1": {"status": "active", "events": 1}})

    def test_round_trip_preserves_existing_keys(self) -> None:
        self.path.write_text(json.dumps({"T1": {"status": "active"}}))
        with file_locked_rmw(self.path) as data:
            data["T2"] = {"status": "slow"}
        loaded = json.loads(self.path.read_text())
        self.assertEqual(
            loaded, {"T1": {"status": "active"}, "T2": {"status": "slow"}}
        )

    def test_empty_or_corrupt_file_yields_empty_dict(self) -> None:
        self.path.write_text("not-json{{")
        with file_locked_rmw(self.path) as data:
            self.assertEqual(data, {})
            data["T1"] = {"status": "active"}
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, {"T1": {"status": "active"}})


class TestConcurrentWriters(unittest.TestCase):
    """Case B: 5 threads each updating different keys → all updates land."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "worker_health.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_five_threads_distinct_keys_all_persisted(self) -> None:
        barrier = threading.Barrier(5)
        errors: list[BaseException] = []

        def writer(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                # Each thread does N RMW cycles to amplify race likelihood.
                for i in range(20):
                    with file_locked_rmw(self.path) as data:
                        data[f"T{idx}"] = {"events": i + 1, "status": "active"}
            except BaseException as exc:  # pragma: no cover — recorded for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,), daemon=True) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        loaded = json.loads(self.path.read_text())
        self.assertEqual(set(loaded.keys()), {f"T{i}" for i in range(5)})
        for i in range(5):
            self.assertEqual(loaded[f"T{i}"], {"events": 20, "status": "active"})

    def test_concurrent_increment_no_lost_updates(self) -> None:
        # Many threads incrementing the same counter under lock — without
        # locking, classic lost-update race; with LOCK_EX, count must equal
        # total number of RMW operations.
        thread_count = 8
        per_thread = 25
        barrier = threading.Barrier(thread_count)
        errors: list[BaseException] = []

        def incrementer() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(per_thread):
                    with file_locked_rmw(self.path) as data:
                        data["counter"] = int(data.get("counter", 0)) + 1
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=incrementer, daemon=True)
            for _ in range(thread_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded["counter"], thread_count * per_thread)


class TestLockReleasedOnException(unittest.TestCase):
    """Case C: lock released on exception so subsequent writers can proceed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "worker_health.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exception_in_block_does_not_corrupt_file(self) -> None:
        self.path.write_text(json.dumps({"T1": {"status": "active"}}))

        class Boom(RuntimeError):
            pass

        with self.assertRaises(Boom):
            with file_locked_rmw(self.path) as data:
                data["T2"] = {"status": "slow"}
                raise Boom()

        # File untouched on exception — still equals pre-exception state.
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, {"T1": {"status": "active"}})

    def test_subsequent_writer_can_acquire_lock_after_exception(self) -> None:
        try:
            with file_locked_rmw(self.path):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        # If the lock was leaked, this acquire would block forever; the test
        # harness will time out. Successful acquisition proves release.
        with file_locked_rmw(self.path) as data:
            data["T1"] = {"status": "active"}

        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, {"T1": {"status": "active"}})


class TestIdempotent(unittest.TestCase):
    """Case D: re-running the same RMW on the same file produces the same result."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "worker_health.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_repeated_identical_rmw_is_stable(self) -> None:
        payload = {"status": "active", "events": 5, "elapsed": "0m05s"}
        for _ in range(4):
            with file_locked_rmw(self.path) as data:
                data["T1"] = dict(payload)
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, {"T1": payload})

    def test_no_op_block_preserves_content(self) -> None:
        seed = {"T1": {"status": "active"}, "T2": {"status": "slow"}}
        self.path.write_text(json.dumps(seed, indent=2))
        with file_locked_rmw(self.path):
            pass  # no mutation
        loaded = json.loads(self.path.read_text())
        self.assertEqual(loaded, seed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
