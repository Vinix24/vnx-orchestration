"""tests/test_create_attempt_project_id.py — OI-1625: create_attempt() never
stamped project_id, so every INSERT into dispatch_attempts raised
sqlite3.IntegrityError against the live (post-migration) schema shape and
dispatch_attempts stayed permanently empty while dispatches accumulated
hundreds of rows (measured 2026-09-05: 0 vs 801 on
~/.vnx-data/vnx-dev/state/runtime_coordination.db, PRAGMA user_version 33).

``dispatch_attempts.project_id`` on that live store is ``TEXT NOT NULL`` with
NO DEFAULT — unlike the static ``schemas/migrations/0031_runtime_tenant_fk_repair.sql``,
which carries ``DEFAULT 'vnx-dev'``. The bare test schema
(``schemas/runtime_coordination.sql`` via plain ``init_schema()``, what every
other create_attempt test in this repo uses) predates migration 0010+ and has
no ``project_id`` column on ``dispatch_attempts``/``dispatches`` at all, so
none of those tests ever exercised this failure mode — hence it shipped
unnoticed. ``_build_live_shape_db`` below reproduces the exact measured live
DDL (``sqlite3 ... ".schema dispatch_attempts"`` against the real store) so
this test fails for the SAME reason the live store fails, not an approximation
of it.

Two levels:
  1. Unit: create_attempt() itself must stamp project_id on the INSERT.
  2. Behavioral (what OI-1625 actually asks for): after a door-fired dispatch
     (``dispatch_cli._persist_dispatch_row``, the single entry point), a row
     must exist in dispatch_attempts with the correct project_id.
"""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import dispatch_cli  # noqa: E402
from runtime_state_machine import create_attempt  # noqa: E402


def _build_live_shape_db(db_path: Path) -> None:
    """Reproduce the measured live vnx-dev shape: project_id TEXT NOT NULL,
    NO DEFAULT, on dispatches/dispatch_attempts/coordination_events, at
    PRAGMA user_version 33 — so a subsequent _rc_init_schema() call (which
    only ever schedules versions up to 11) treats the store as already fully
    migrated, exactly like it does against the real production DB.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'proposed',
            terminal_id TEXT,
            track TEXT,
            priority TEXT DEFAULT 'P2',
            pr_ref TEXT,
            gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT,
            metadata_json TEXT DEFAULT '{}',
            target_slot TEXT,
            worker_claude_override_reason TEXT,
            UNIQUE (dispatch_id, project_id)
        );

        CREATE TABLE dispatch_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            terminal_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ended_at TEXT,
            failure_reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            UNIQUE (attempt_id, project_id),
            FOREIGN KEY (dispatch_id, project_id) REFERENCES dispatches (dispatch_id, project_id)
        );

        CREATE TABLE coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            project_id TEXT NOT NULL,
            UNIQUE (event_id, project_id)
        );
        """
    )
    conn.execute("PRAGMA user_version = 33")
    conn.commit()
    conn.close()


class TestCreateAttemptStampsProjectId:
    """Unit-level: create_attempt() itself, against the live NOT-NULL-no-default shape."""

    def test_create_attempt_inserts_with_project_id(self, tmp_path):
        db_path = tmp_path / "runtime_coordination.db"
        _build_live_shape_db(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES (?, ?)",
            ("d-oi1625", "vnx-dev"),
        )
        conn.commit()

        attempt = create_attempt(
            conn, dispatch_id="d-oi1625", project_id="vnx-dev",
            terminal_id="T1", attempt_number=1,
        )
        conn.commit()

        assert attempt["project_id"] == "vnx-dev"
        row = conn.execute(
            "SELECT project_id FROM dispatch_attempts WHERE dispatch_id = ?", ("d-oi1625",)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["project_id"] == "vnx-dev"


class TestDoorDispatchPersistsAttemptRow:
    """Behavioral, door-level: OI-1625's actual symptom. After a door-fired
    dispatch (dispatch_cli._persist_dispatch_row, the single entry point for
    every dispatch), a row must exist in dispatch_attempts with the correct
    project_id — against the exact schema shape measured on the live vnx-dev
    store.
    """

    def test_door_dispatch_creates_attempt_row_with_project_id(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"
        _build_live_shape_db(db_path)

        spec = types.SimpleNamespace(
            dispatch_id="20260905-oi1625-door-attempt",
            project_id="vnx-dev",
            track_id=None,
            gate=None,
            target_slot="T1",
        )

        attempt_id = dispatch_cli._persist_dispatch_row(spec, state_dir=state_dir)

        assert attempt_id is not None, (
            "the door swallowed a bookkeeping failure and returned None — "
            "see state_dir/dispatch_register.ndjson for the door_bookkeeping_failed fact"
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
            (spec.dispatch_id,),
        ).fetchone()
        conn.close()

        assert row is not None, "no dispatch_attempts row was created for the door dispatch"
        assert row["project_id"] == "vnx-dev"
        assert row["attempt_id"] == attempt_id


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
