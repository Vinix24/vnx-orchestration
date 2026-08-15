"""tests/test_migration_prereq_guard.py — OI-1213 regression test.

The sweep mine: migrate_future_system registers a v30 preflight hook at IMPORT
time (``register_preflight(30, ...)``). A fixture that applied migration 0030
without first applying 0029 therefore passed solo (no hook registered in the
process) but derailed 26 later tests once tests/test_mc_v4_columns.py had
imported migrate_future_system into the same sweep. The dependency was silent
and import-order-dependent.

schema_migration now carries an UNCONDITIONAL ordered-migration prerequisite
guard (``_MIGRATION_PREREQUISITES``), independent of migrate_future_system's
hook registry. This module imports ONLY ``schema_migration`` — deliberately NOT
``migrate_future_system`` — and clears the hook registry in one test, so the
guard tests prove the check fires even when no preflight hook has ever been
registered. The positive controls build a real v28 store via the canonical
migration chain, so they pass whether or not migrate_future_system's hooks are
present in the sweep process.

Reproduce as the dispatch instructs:
  python3 -m pytest tests/test_migration_prereq_guard.py -q
  python3 -m pytest tests/test_mc_v4_columns.py tests/test_migration_prereq_guard.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns


_SQL_0029 = (_MIGRATIONS / "0029_track_type_discriminator.sql").read_text(encoding="utf-8")
_SQL_0030 = (_MIGRATIONS / "0030_track_oi_resolved_at.sql").read_text(encoding="utf-8")


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _minimal_v28_db(tmp_path: Path) -> sqlite3.Connection:
    """Minimal store missing the 0029 columns: ``tracks`` WITHOUT track_type /
    next_action_owner, ``track_open_items`` WITHOUT the 0030 columns.

    This is the shape of the mine fixture — a store a test would try to apply
    0030 onto directly. Used only by the guard tests, which must not depend on
    any other table/column being present."""
    conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE tracks (
            track_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            title TEXT NOT NULL,
            goal_state TEXT,
            PRIMARY KEY (track_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE track_open_items (
            track_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            oi_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            link_source TEXT NOT NULL,
            linked_at TEXT,
            PRIMARY KEY (track_id, project_id, oi_id, link_type)
        )
    """)
    conn.commit()
    return conn


def _base_v28_db(tmp_path: Path) -> sqlite3.Connection:
    """Real v28-equivalent store built via the canonical migration chain
    (0022+0024, then structural-doctor columns, then 0027+0028). Mirrors
    _base_v28_db in tests/test_migration_track_type.py.

    This satisfies migrate_future_system's v29 hook (tracks.derived_status is
    present) when it happens to be imported in the sweep, while still missing
    the 0029 columns (track_type / next_action_owner) that 0030 requires."""
    conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 22, (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 24, (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# The regression: 0030 without 0029 must fail loudly, unconditionally
# ---------------------------------------------------------------------------

def test_0030_without_0029_raises_clear_error(tmp_path):
    """Applying 0030 on a store missing 0029's columns raises the guard's own
    clear error — even though migrate_future_system was never imported here.

    On the OLD code this test failed in two ways, both intended:
    - solo: no hook registered, so 0030 applied silently (no RuntimeError), and
    - in a sweep after test_mc_v4_columns.py: the hook raised a DIFFERENT
      message ("missing 'track_type' column (prior migration not applied)").
    The match below is the guard's own message, so it pins the fix.
    """
    conn = _minimal_v28_db(tmp_path)
    with pytest.raises(RuntimeError, match="requires migration 0029"):
        schema_migration.apply_script_if_below(conn, 30, _SQL_0030)


def test_guard_fires_with_empty_hook_registry(tmp_path, monkeypatch):
    """The guard is independent of migrate_future_system's hook registry.

    Clearing _PREFLIGHT_HOOKS (so no v30 preflight can possibly fire) must NOT
    let 0030-through-without-0029 slip past: the guard still raises."""
    conn = _minimal_v28_db(tmp_path)
    monkeypatch.setattr(schema_migration, "_PREFLIGHT_HOOKS", {})
    with pytest.raises(RuntimeError, match="requires migration 0029"):
        schema_migration.apply_script_if_below(conn, 30, _SQL_0030)


def test_error_names_missing_columns(tmp_path):
    """The guard's message names the concrete missing columns, so an operator
    sees exactly what to fix (apply 0029), not a generic version mismatch.

    The guard raises BEFORE any SQL runs, so the same connection can be probed
    twice without mutation."""
    conn = _minimal_v28_db(tmp_path)
    with pytest.raises(RuntimeError, match="track_type"):
        schema_migration.apply_script_if_below(conn, 30, _SQL_0030)
    with pytest.raises(RuntimeError, match="next_action_owner"):
        schema_migration.apply_script_if_below(conn, 30, _SQL_0030)


# ---------------------------------------------------------------------------
# Positive control: 0029 then 0030 applies cleanly (on a real v28 store)
# ---------------------------------------------------------------------------

def test_0029_only_does_not_trigger_guard(tmp_path):
    """Applying the prerequisite itself (0029) is unaffected by the guard —
    there is no prerequisite for 0029, so it applies cleanly on a v28 store."""
    conn = _base_v28_db(tmp_path)
    assert schema_migration.apply_script_if_below(conn, 29, _SQL_0029) is True
    assert schema_migration.get_user_version(conn) == 29


def test_0029_then_0030_applies_cleanly(tmp_path):
    """The correct order (0029 first, then 0030) sails through the guard."""
    conn = _base_v28_db(tmp_path)
    assert schema_migration.apply_script_if_below(conn, 29, _SQL_0029) is True
    assert schema_migration.apply_script_if_below(conn, 30, _SQL_0030) is True
    assert schema_migration.get_user_version(conn) == 30
    assert "resolved_at" in _cols(conn, "track_open_items")
    assert "resolution_reason" in _cols(conn, "track_open_items")
    assert "track_type" in _cols(conn, "tracks")


def test_0030_idempotent_after_0029(tmp_path):
    """Second 0030 apply is a no-op once the prerequisite is satisfied."""
    conn = _base_v28_db(tmp_path)
    schema_migration.apply_script_if_below(conn, 29, _SQL_0029)
    assert schema_migration.apply_script_if_below(conn, 30, _SQL_0030) is True
    assert schema_migration.apply_script_if_below(conn, 30, _SQL_0030) is False
    assert schema_migration.get_user_version(conn) == 30
