#!/usr/bin/env python3
"""Tests for claim_next_queued_dispatch — N-1 queue claim primitive.

Covers:
  - Basic claim from non-empty queue
  - Empty queue returns None
  - Audit event emitted per claim
  - claimed_by / claimed_at provenance columns set
  - terminal_id updated on the dispatch row
  - Cross-project isolation: project-A claimer never sees project-B rows (ADR-007)
  - Concurrency: 10 threads × 5 queued dispatches → exactly 5 distinct claims,
    5 None returns, zero double-claims
  - Migration 0026 idempotency: run twice, no error, existing rows preserved
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR / "lib" / "migrations"))

from coordination_db import DB_FILENAME, db_path_from_state_dir
from runtime_coordination import (
    claim_next_queued_dispatch,
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
)

# Import migration runner directly
import importlib.util as _ilu

_RUNNER_PATH = SCRIPT_DIR / "lib" / "migrations" / "apply_0026.py"
_spec = _ilu.spec_from_file_location("apply_0026", _RUNNER_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
apply_migration_0026 = _mod.apply_migration

_MIGRATION_SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0026_dispatch_claim.sql"


def _apply_0026(state_dir: str) -> bool:
    db_path = db_path_from_state_dir(state_dir)
    return apply_migration_0026(db_path, _MIGRATION_SQL)


class _DbTestCase(unittest.TestCase):
    """Base: temp dir + schema init + migration 0026 applied."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        _apply_0026(self.state_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def conn(self):
        return get_connection(self.state_dir)

    def _insert_queued(self, dispatch_id: str, project_id: str = "test-proj", priority: str = "P2") -> None:
        """Insert a queued dispatch directly (bypasses register_dispatch default project_id)."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state, priority) VALUES (?, ?, 'queued', ?)",
                (dispatch_id, project_id, priority),
            )
            c.commit()


class TestClaimBasic(_DbTestCase):

    def test_claim_returns_dispatch_id(self):
        self._insert_queued("d-001")
        with self.conn() as c:
            result = claim_next_queued_dispatch(c, "T1", "test-proj")
        self.assertEqual(result, "d-001")

    def test_claim_empty_queue_returns_none(self):
        with self.conn() as c:
            result = claim_next_queued_dispatch(c, "T1", "test-proj")
        self.assertIsNone(result)

    def test_claim_transitions_state_to_claimed(self):
        self._insert_queued("d-002")
        with self.conn() as c:
            claim_next_queued_dispatch(c, "T1", "test-proj")
        with self.conn() as c:
            row = c.execute(
                "SELECT state FROM dispatches WHERE dispatch_id = ?", ("d-002",)
            ).fetchone()
        self.assertEqual(row["state"], "claimed")

    def test_claim_sets_terminal_id(self):
        self._insert_queued("d-003")
        with self.conn() as c:
            claim_next_queued_dispatch(c, "T2", "test-proj")
        with self.conn() as c:
            row = c.execute(
                "SELECT terminal_id FROM dispatches WHERE dispatch_id = ?", ("d-003",)
            ).fetchone()
        self.assertEqual(row["terminal_id"], "T2")

    def test_claim_sets_claimed_by_and_claimed_at(self):
        self._insert_queued("d-004")
        with self.conn() as c:
            claim_next_queued_dispatch(c, "T3", "test-proj")
        with self.conn() as c:
            row = c.execute(
                "SELECT claimed_by, claimed_at FROM dispatches WHERE dispatch_id = ?", ("d-004",)
            ).fetchone()
        self.assertEqual(row["claimed_by"], "T3")
        self.assertIsNotNone(row["claimed_at"])

    def test_claim_appends_dispatch_claimed_event(self):
        self._insert_queued("d-005")
        with self.conn() as c:
            claim_next_queued_dispatch(c, "T1", "test-proj")
        with self.conn() as c:
            events = get_events(c, entity_id="d-005")
        event_types = [e["event_type"] for e in events]
        self.assertIn("dispatch_claimed", event_types)

    def test_second_claim_on_same_dispatch_fails(self):
        """Once claimed, the dispatch is no longer in 'queued' state — next caller gets None."""
        self._insert_queued("d-006")
        with self.conn() as c:
            r1 = claim_next_queued_dispatch(c, "T1", "test-proj")
        with self.conn() as c:
            r2 = claim_next_queued_dispatch(c, "T2", "test-proj")
        self.assertEqual(r1, "d-006")
        self.assertIsNone(r2)

    def test_priority_ordering_p1_before_p2(self):
        """P1 dispatches are claimed before P2 (alphabetical ASC ordering)."""
        self._insert_queued("d-p2", priority="P2")
        self._insert_queued("d-p1", priority="P1")
        with self.conn() as c:
            result = claim_next_queued_dispatch(c, "T1", "test-proj")
        self.assertEqual(result, "d-p1")


class TestClaimCrossProjectIsolation(_DbTestCase):
    """ADR-007: project-A claimer must never see project-B queued rows."""

    def test_claimer_does_not_see_other_project_rows(self):
        self._insert_queued("proj-b-001", project_id="project-b")

        with self.conn() as c:
            result = claim_next_queued_dispatch(c, "T1", "project-a")

        self.assertIsNone(result, "project-a claimer must not claim project-b dispatch")

    def test_claimer_only_claims_own_project(self):
        self._insert_queued("proj-a-001", project_id="project-a")
        self._insert_queued("proj-b-001", project_id="project-b")

        with self.conn() as c:
            result_a = claim_next_queued_dispatch(c, "T1", "project-a")
        with self.conn() as c:
            result_b = claim_next_queued_dispatch(c, "T1", "project-b")

        self.assertEqual(result_a, "proj-a-001")
        self.assertEqual(result_b, "proj-b-001")

        # project-a dispatch must NOT be in project-b and vice versa
        with self.conn() as c:
            row_a = c.execute(
                "SELECT project_id FROM dispatches WHERE dispatch_id = ?", ("proj-a-001",)
            ).fetchone()
            row_b = c.execute(
                "SELECT project_id FROM dispatches WHERE dispatch_id = ?", ("proj-b-001",)
            ).fetchone()
        self.assertEqual(row_a["project_id"], "project-a")
        self.assertEqual(row_b["project_id"], "project-b")


class TestClaimConcurrency(_DbTestCase):
    """10 threads × 5 queued dispatches → 5 distinct claims, 5 None, zero double-claims."""

    def setUp(self):
        super().setUp()
        # Insert 5 queued dispatches
        for i in range(5):
            self._insert_queued(f"d-conc-{i:03d}", project_id="concurrent-proj")

    def test_10_threads_5_dispatches_no_double_claim(self):
        results: list = []
        lock = threading.Lock()
        errors: list = []

        def worker(n: int) -> None:
            try:
                with get_connection(self.state_dir) as c:
                    res = claim_next_queued_dispatch(c, f"T{n % 4 + 1}", "concurrent-proj")
                with lock:
                    results.append(res)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"worker errors: {errors}")
        self.assertEqual(len(results), 10, f"expected 10 results, got {len(results)}")

        claimed = [r for r in results if r is not None]
        none_returns = [r for r in results if r is None]

        self.assertEqual(
            len(claimed), 5,
            f"expected exactly 5 claims, got {len(claimed)}: {claimed}",
        )
        self.assertEqual(
            len(none_returns), 5,
            f"expected exactly 5 None returns, got {len(none_returns)}",
        )
        self.assertEqual(
            len(set(claimed)), 5,
            f"duplicate claims detected: {claimed}",
        )

        # Verify each claimed dispatch has state='claimed' in DB
        with self.conn() as c:
            for dispatch_id in claimed:
                row = c.execute(
                    "SELECT state FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                self.assertEqual(
                    row["state"], "claimed",
                    f"dispatch {dispatch_id} state should be 'claimed', got {row['state']}",
                )

    def test_one_audit_event_per_claim(self):
        """Each successful claim produces exactly one dispatch_claimed event."""
        with self.conn() as c:
            for i in range(5):
                claim_next_queued_dispatch(c, "T1", "concurrent-proj")

        with self.conn() as c:
            rows = c.execute(
                "SELECT entity_id FROM coordination_events WHERE event_type = 'dispatch_claimed'"
            ).fetchall()
        claimed_in_events = [r["entity_id"] for r in rows]
        # 5 claims → 5 events (one per dispatch)
        self.assertEqual(len(claimed_in_events), 5)
        self.assertEqual(len(set(claimed_in_events)), 5)


class TestMigration0026Idempotency(unittest.TestCase):
    """Migration 0026 idempotency: run twice, no error, existing rows preserved."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_first_apply_returns_true(self):
        applied = _apply_0026(self.state_dir)
        self.assertTrue(applied)

    def test_second_apply_returns_false(self):
        _apply_0026(self.state_dir)
        applied = _apply_0026(self.state_dir)
        self.assertFalse(applied)

    def test_second_apply_no_error(self):
        _apply_0026(self.state_dir)
        try:
            _apply_0026(self.state_dir)
        except Exception as e:
            self.fail(f"second apply raised: {e}")

    def test_existing_dispatches_preserved_after_migration(self):
        # Insert dispatch before migration
        with get_connection(self.state_dir) as c:
            c.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES ('pre-migration', 'queued')"
            )
            c.commit()

        _apply_0026(self.state_dir)

        # Row must still exist after migration
        with get_connection(self.state_dir) as c:
            row = c.execute(
                "SELECT dispatch_id, state FROM dispatches WHERE dispatch_id = ?",
                ("pre-migration",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["dispatch_id"], "pre-migration")
        self.assertEqual(row["state"], "queued")

    def test_columns_exist_after_migration(self):
        _apply_0026(self.state_dir)
        db_path = db_path_from_state_dir(self.state_dir)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(dispatches)").fetchall()}
        finally:
            conn.close()
        self.assertIn("claimed_by", cols)
        self.assertIn("claimed_at", cols)

    def test_index_exists_after_migration(self):
        _apply_0026(self.state_dir)
        db_path = db_path_from_state_dir(self.state_dir)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            idx_names = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn("idx_dispatch_project_state_claim", idx_names)

    def test_version_stamped_correctly(self):
        _apply_0026(self.state_dir)
        with get_connection(self.state_dir) as c:
            row = c.execute(
                "SELECT version FROM runtime_schema_version WHERE version = 15"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["version"], 15)


if __name__ == "__main__":
    unittest.main()
