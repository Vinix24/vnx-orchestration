#!/usr/bin/env python3
"""ADR-007 regression: register_dispatch cross-tenant isolation and project_id stamping.

Verifies:
- Same dispatch_id in two projects creates DISTINCT rows (cross-tenant isolation).
- Idempotency within a project: re-register same (dispatch_id, project_id) returns
  existing row unchanged.
- Each row has its correct project_id stamped (not the other tenant's id).
- project_id is a required parameter — calling without it raises TypeError.
"""

from __future__ import annotations

import importlib.util as _ilu
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import get_connection, init_schema, register_dispatch
from coordination_db import DB_FILENAME

# Load _rebuild_dispatches from apply_0017 to set up composite UNIQUE constraint.
_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)
_rebuild_dispatches_composite = _mod_0017._rebuild_dispatches


class _CompositeDbTestCase(unittest.TestCase):
    """Base: init_schema + project_id column + composite UNIQUE(dispatch_id, project_id)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        with get_connection(self.state_dir) as conn:
            # Add project_id column if not present (schema pre-dates migration 0010).
            cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatches)").fetchall()}
            if "project_id" not in cols:
                conn.execute(
                    "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
                )
            # Rebuild dispatches with composite UNIQUE(dispatch_id, project_id).
            _rebuild_dispatches_composite(conn)
            conn.commit()

    def tearDown(self):
        self._tmpdir.cleanup()

    def conn(self):
        return get_connection(self.state_dir)


class TestCrossTenantIsolation(_CompositeDbTestCase):

    def test_same_dispatch_id_distinct_projects_creates_two_rows(self):
        """ADR-007: same dispatch_id under proj-A and proj-B → two distinct rows."""
        with self.conn() as conn:
            r_a = register_dispatch(conn, dispatch_id="d-shared", project_id="proj-alpha")
            r_b = register_dispatch(conn, dispatch_id="d-shared", project_id="proj-beta")
            conn.commit()

        self.assertEqual(r_a["dispatch_id"], "d-shared")
        self.assertEqual(r_a["project_id"], "proj-alpha")
        self.assertEqual(r_b["dispatch_id"], "d-shared")
        self.assertEqual(r_b["project_id"], "proj-beta")

        with self.conn() as conn:
            rows = conn.execute(
                "SELECT project_id FROM dispatches WHERE dispatch_id = 'd-shared' ORDER BY project_id"
            ).fetchall()
        projects = [r[0] for r in rows]
        self.assertEqual(projects, ["proj-alpha", "proj-beta"])

    def test_idempotency_within_project(self):
        """Re-registering same (dispatch_id, project_id) returns the existing row."""
        with self.conn() as conn:
            r1 = register_dispatch(conn, dispatch_id="d-idem", project_id="proj-alpha",
                                   terminal_id="T1")
            conn.commit()
            r2 = register_dispatch(conn, dispatch_id="d-idem", project_id="proj-alpha",
                                   terminal_id="T2")
            conn.commit()

        # Second call must not mutate the row (idempotent — returns existing).
        self.assertEqual(r1["terminal_id"], r2["terminal_id"])
        self.assertEqual(r2["project_id"], "proj-alpha")

        with self.conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'd-idem' AND project_id = 'proj-alpha'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_no_cross_project_collision(self):
        """proj-A idempotency check must NOT return proj-B's row with same dispatch_id."""
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-iso", project_id="proj-beta",
                              terminal_id="T3")
            conn.commit()
            # Now register same dispatch_id under proj-alpha — must create a NEW row.
            r_a = register_dispatch(conn, dispatch_id="d-iso", project_id="proj-alpha",
                                    terminal_id="T1")
            conn.commit()

        self.assertEqual(r_a["project_id"], "proj-alpha")
        self.assertEqual(r_a["terminal_id"], "T1")

        with self.conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'd-iso'"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_project_id_required_keyword(self):
        """register_dispatch must raise TypeError when project_id is not passed."""
        with self.conn() as conn:
            with self.assertRaises(TypeError):
                register_dispatch(conn, dispatch_id="d-nopid")  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
