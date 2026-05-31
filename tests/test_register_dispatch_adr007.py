#!/usr/bin/env python3
"""ADR-007 regression: register_dispatch project_id stamping and dispatch_id global uniqueness.

Verifies:
- dispatch_id is GLOBALLY UNIQUE: registering same dispatch_id under a different
  project_id raises ValueError (cross-tenant reuse guard).
- Idempotency within a project: re-register same (dispatch_id, project_id) returns
  existing row unchanged.
- project_id is stamped on the row (ADR-007 column compliance).
- project_id is a required parameter — calling without it raises TypeError.
- transition_dispatch keys on dispatch_id alone after registration (state machine
  stays consistent because dispatch_id is globally unique).

Note: full composite-keying of the dispatch state machine is deferred to a
post-launch hardening item.
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
from runtime_state_machine import transition_dispatch

# Load _rebuild_dispatches from apply_0017 to ensure project_id column + composite UNIQUE.
_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)
_rebuild_dispatches_for_project_id = _mod_0017._rebuild_dispatches


class _ProjectIdDbTestCase(unittest.TestCase):
    """Base: init_schema + project_id column present on dispatches."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        with get_connection(self.state_dir) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatches)").fetchall()}
            if "project_id" not in cols:
                conn.execute(
                    "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
                )
            # Composite UNIQUE(dispatch_id, project_id) in DB; app guard enforces
            # global dispatch_id uniqueness above the DB constraint.
            _rebuild_dispatches_for_project_id(conn)
            conn.commit()

    def tearDown(self):
        self._tmpdir.cleanup()

    def conn(self):
        return get_connection(self.state_dir)


class TestRegisterDispatchAdr007(_ProjectIdDbTestCase):

    def test_cross_tenant_dispatch_id_raises_value_error(self):
        """Registering same dispatch_id under a different project raises ValueError.

        dispatch_id is globally unique (timestamped slug). Reusing one across
        projects is a bug — application guard must surface it clearly.
        """
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-shared", project_id="proj-alpha")
            conn.commit()

        with self.conn() as conn:
            with self.assertRaises(ValueError) as ctx:
                register_dispatch(conn, dispatch_id="d-shared", project_id="proj-beta")
        self.assertIn("proj-alpha", str(ctx.exception))
        self.assertIn("proj-beta", str(ctx.exception))

        # Only the original row must exist — no second row was created.
        with self.conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'd-shared'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_idempotency_within_project(self):
        """Re-registering same (dispatch_id, project_id) returns the existing row unchanged."""
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
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'd-idem'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_project_id_stamped_on_row(self):
        """project_id is stored on the dispatches row (ADR-007 column compliance)."""
        with self.conn() as conn:
            row = register_dispatch(conn, dispatch_id="d-stamp", project_id="proj-test")
            conn.commit()

        self.assertEqual(row["project_id"], "proj-test")

        with self.conn() as conn:
            stored = conn.execute(
                "SELECT project_id FROM dispatches WHERE dispatch_id = 'd-stamp'"
            ).fetchone()
        self.assertEqual(stored["project_id"], "proj-test")

    def test_project_id_required_keyword(self):
        """register_dispatch must raise TypeError when project_id is not passed."""
        with self.conn() as conn:
            with self.assertRaises(TypeError):
                register_dispatch(conn, dispatch_id="d-nopid")  # type: ignore[call-arg]

    def test_transition_dispatch_consistent_after_register(self):
        """transition_dispatch keys on dispatch_id alone — consistent after registration."""
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-trans", project_id="proj-alpha",
                              terminal_id="T1")
            conn.commit()

        with self.conn() as conn:
            result = transition_dispatch(conn, dispatch_id="d-trans", to_state="claimed",
                                         actor="test")
            conn.commit()
        self.assertEqual(result["state"], "claimed")
        self.assertEqual(result["dispatch_id"], "d-trans")


if __name__ == "__main__":
    unittest.main()
