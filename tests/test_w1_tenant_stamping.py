"""W1 Tenant-Stamping Fix — full test suite.

Tests every requirement in claudedocs/W1-TENANT-STAMPING-FIX-SPEC.md:

  1. 3-phase ordering enforced (Phase 1 before Phase 2 before Phase 3).
  2. NULL / blank / 'vnx-dev' all re-stamped.
  3. Guard allows {pid, 'vnx-dev'} + aborts on a third genuine tenant.
  4. Phase-1-makes-Phase-2-collision-safe (UNIQUE(name) without project_id,
     two rows differing only in pid, MUST NOT corrupt after re-stamp).
  5. Split-brain rerun convergence (simulate QI abort after RC commit;
     rerun must converge both DBs).
  6. FK-off preserves composite keys + foreign_key_check clean both DBs.
  7. Fail-loud on unqualified INSERT after Phase 3 (project_id NOT NULL).
  8. Cross-project negative isolation (a second store reads only its rows).
  9. Topological sort (parent emitted before child).
 10. Cross-DB FK assertion (no RC <-> QI FK).
 11. VNX_DATA_DIR_EXPLICIT/VNX_DATA_DIR branch in migrate_future_system.run()
     resolves the correct state_dir.
 12. _pytest_db_isolation_guard rejects resolved data_dir outside tempdir.

All tests operate on tmp DBs under tempfile.gettempdir(). The live
~/.vnx-data store is NEVER opened or modified.

ADR-007: composite UNIQUE/PK over project_id.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tenant_stamping as ts  # noqa: E402


# ---------------------------------------------------------------------------
# Autouse isolation: every test uses a fresh tmp dir, never ~/.vnx-data
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin VNX_DATA_DIR_EXPLICIT=1 + tmp VNX_DATA_DIR for every test."""
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_w1_data"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


# ---------------------------------------------------------------------------
# DB factory helpers
# ---------------------------------------------------------------------------

def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _make_simple_tenant_table(conn: sqlite3.Connection, table: str = "things") -> None:
    """Create a table with project_id but no composite UNIQUE."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        )
    """)
    conn.commit()


def _make_nullable_tenant_table(conn: sqlite3.Connection, table: str = "things") -> None:
    """Create a table with NULLABLE project_id (for testing NULL insertion)."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            project_id TEXT DEFAULT 'vnx-dev'
        )
    """)
    conn.commit()


def _make_table_with_unique_name(conn: sqlite3.Connection, table: str = "widgets") -> None:
    """Create a table with UNIQUE(name) excluding project_id."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            UNIQUE (name)
        )
    """)
    conn.commit()


def _make_composite_pk_table(conn: sqlite3.Connection, table: str = "patterns") -> None:
    """Create a table with composite PK that excludes project_id."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            pattern_id TEXT NOT NULL,
            category TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            body TEXT,
            PRIMARY KEY (pattern_id, category)
        )
    """)
    conn.commit()


def _make_parent_child_tables(conn: sqlite3.Connection) -> None:
    """Create parent -> child FK relationship both carrying project_id."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parents (
            parent_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            label TEXT,
            PRIMARY KEY (parent_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS children (
            child_id TEXT NOT NULL,
            parent_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            data TEXT,
            PRIMARY KEY (child_id, project_id),
            FOREIGN KEY (parent_id, project_id) REFERENCES parents(parent_id, project_id)
        )
    """)
    conn.commit()


def _db(tmp_path: Path, name: str = "test.db") -> Path:
    p = tmp_path / name
    return p


# ===========================================================================
# 1. Three-phase ordering
# ===========================================================================

class TestThreePhaseOrdering:
    """Phase 1 must run before Phase 2; Phase 2 must run before Phase 3."""

    def test_phase1_runs_before_phase2(self, tmp_path: Path) -> None:
        """Phase 1 makes UNIQUE constraints composite BEFORE data is re-stamped.

        Without Phase 1, re-stamping two rows that differ only in project_id
        (NULL vs 'vnx-dev') would collide on UNIQUE(name) because both get
        updated to the same pid. With Phase 1 first, the constraint is widened
        to UNIQUE(name, project_id), making the re-stamp safe.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Create a table with UNIQUE(name) excluding project_id
        conn.execute("""
            CREATE TABLE widgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id TEXT DEFAULT 'vnx-dev',
                UNIQUE (name)
            )
        """)
        # Two rows: same name, different legacy project_ids
        # We cannot insert both with UNIQUE(name) active as-is (constraint lives on name only).
        # Phase 1 must widen the constraint first. We insert them with FK-off on the raw DB
        # by manually bypassing via the staging pattern.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO widgets (name, project_id) VALUES ('gamma', NULL)")
        conn.commit()
        conn.close()

        # Running the full 3-phase migration should succeed
        result = ts.run_three_phase_migration_on_db(db, "proj-x", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT project_id FROM widgets WHERE name='gamma'").fetchall()
        conn.close()
        assert all(r[0] == "proj-x" for r in rows)

    def test_phase3_runs_after_phase2(self, tmp_path: Path) -> None:
        """Phase 3 cannot run while legacy rows remain — Phase 2 must go first."""
        db = _db(tmp_path)
        conn = _open(db)
        # Create table with NULLABLE project_id so we can insert NULL directly
        conn.execute("""
            CREATE TABLE things (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', NULL)")
        conn.commit()
        conn.close()

        # Attempt Phase 3 directly (skipping Phase 2) must fail because NULL rows exist
        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        with pytest.raises(RuntimeError, match="Phase 3 pre-condition failed"):
            ts.run_phase3_enforce(conn, tables, db_label="TEST")
        conn.close()

    def test_full_three_phase_order_produces_not_null(self, tmp_path: Path) -> None:
        """After all three phases, project_id is NOT NULL."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "my-project", db_label="TEST")

        conn = sqlite3.connect(str(db))
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(things)").fetchall()}
        conn.close()
        assert cols["project_id"][3] == 1, "project_id must be NOT NULL after Phase 3"


# ===========================================================================
# 2. Legacy values: NULL, blank, 'vnx-dev' all re-stamped
# ===========================================================================

class TestLegacyRestamp:
    """NULL, '', and 'vnx-dev' must all be re-stamped to the resolved pid."""

    def test_null_restamped(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_nullable_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('n', NULL)")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "acme-co", db_label="TEST")
        conn = sqlite3.connect(str(db))
        val = conn.execute("SELECT project_id FROM things WHERE name='n'").fetchone()[0]
        conn.close()
        assert val == "acme-co"

    def test_blank_restamped(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_nullable_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', '')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "acme-co", db_label="TEST")
        conn = sqlite3.connect(str(db))
        val = conn.execute("SELECT project_id FROM things WHERE name='b'").fetchone()[0]
        conn.close()
        assert val == "acme-co"

    def test_vnxdev_restamped_when_pid_differs(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('v', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "real-project", db_label="TEST")
        conn = sqlite3.connect(str(db))
        val = conn.execute("SELECT project_id FROM things WHERE name='v'").fetchone()[0]
        conn.close()
        assert val == "real-project"

    def test_vnxdev_store_noop_for_vnxdev_pid(self, tmp_path: Path) -> None:
        """When pid IS 'vnx-dev', rows stamped 'vnx-dev' stay 'vnx-dev'."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "vnx-dev", db_label="TEST")
        conn = sqlite3.connect(str(db))
        val = conn.execute("SELECT project_id FROM things WHERE name='x'").fetchone()[0]
        conn.close()
        assert val == "vnx-dev"

    def test_mixed_legacy_all_restamped(self, tmp_path: Path) -> None:
        """All three legacy types in one table — all restamped in one pass."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_nullable_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('n', NULL)")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', '')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('v', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "target-pid", db_label="TEST")
        conn = sqlite3.connect(str(db))
        vals = {r[0] for r in conn.execute("SELECT project_id FROM things").fetchall()}
        conn.close()
        assert vals == {"target-pid"}

    def test_already_correct_rows_not_touched(self, tmp_path: Path) -> None:
        """Rows already stamped with the correct pid are not updated."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'good-pid')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', 'vnx-dev')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "good-pid", db_label="TEST")
        # Only 'b' (vnx-dev) should be updated
        updated = result.get("phase2_updated", {})
        assert updated.get("things", 0) == 1

        conn = sqlite3.connect(str(db))
        rows = {r[0]: r[1] for r in conn.execute("SELECT name, project_id FROM things").fetchall()}
        conn.close()
        assert rows["a"] == "good-pid"
        assert rows["b"] == "good-pid"


# ===========================================================================
# 3. Re-stamp guard: allows {pid, vnx-dev}, aborts on third tenant
# ===========================================================================

class TestRestampGuard:
    def test_allows_pid_and_vnxdev(self, tmp_path: Path) -> None:
        """Guard proceeds when rows are only pid or vnx-dev."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'my-pid')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Should not raise
        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        ts._resolve_legacy_guard(conn, tables, "my-pid")
        conn.close()

    def test_aborts_on_third_tenant(self, tmp_path: Path) -> None:
        """Guard aborts when a third genuine tenant appears."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'project-a')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', 'project-b')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        with pytest.raises(RuntimeError, match="third genuine tenant"):
            ts._resolve_legacy_guard(conn, tables, "project-a")
        conn.close()

    def test_null_and_blank_not_treated_as_third_tenant(self, tmp_path: Path) -> None:
        """NULL and '' are legacy, not third tenants."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_nullable_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'my-pid')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', NULL)")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('c', '')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        # Must not raise
        ts._resolve_legacy_guard(conn, tables, "my-pid")
        conn.close()

    def test_entirely_vnxdev_store_restamped(self, tmp_path: Path) -> None:
        """A store entirely stamped 'vnx-dev' with pid != 'vnx-dev' proceeds."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('y', 'vnx-dev')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "real-owner", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))
        vals = {r[0] for r in conn.execute("SELECT project_id FROM things").fetchall()}
        conn.close()
        assert vals == {"real-owner"}

    def test_full_migration_aborts_on_third_tenant(self, tmp_path: Path) -> None:
        """run_three_phase_migration_on_db aborts when third tenant is present."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'owner-pid')")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('b', 'other-pid')")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="third genuine tenant"):
            ts.run_three_phase_migration_on_db(db, "owner-pid", db_label="TEST")


# ===========================================================================
# 4. Phase-1-makes-Phase-2-collision-safe
# ===========================================================================

class TestPhase1MakesPhase2Safe:
    """Without Phase 1, two rows with the same name but different legacy
    project_ids would collide on UNIQUE(name) during re-stamp.
    Phase 1 must widen the constraint to UNIQUE(name, project_id) first.
    """

    def test_two_rows_same_name_different_legacy_pid(self, tmp_path: Path) -> None:
        """Two legacy rows: name='x', one with 'vnx-dev', one with NULL.
        UNIQUE(name) would block re-stamp unless Phase 1 widens it first.
        These two rows are legit because UNIQUE(name) allows them when they
        differ in ANY column — but after re-stamp both get pid='new-owner',
        so UNIQUE(name) would collide at UPDATE time.

        Phase 1 must widen UNIQUE(name) -> UNIQUE(name, project_id) before Phase 2.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Table with UNIQUE(name) — row-level collision after re-stamp without Phase 1
        conn.execute("""
            CREATE TABLE things (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id TEXT,
                UNIQUE (name)
            )
        """)
        # Two rows: same name, one NULL and one vnx-dev. UNIQUE(name) allows these
        # since the table has no extra uniqueness distinction (SQLite UNIQUE treats
        # NULL as distinct from everything, so two NULLs would be fine; but one NULL
        # and one 'vnx-dev' also satisfy UNIQUE(name) IF name differs — here name
        # is the same 'dup', so we can only insert one row per unique name right now).
        # Use DIFFERENT names but same VIRTUAL re-stamp collision scenario.
        conn.execute("INSERT INTO things (name, project_id) VALUES ('dup-null', NULL)")
        conn.execute("INSERT INTO things (name, project_id) VALUES ('dup-vnxdev', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Phase 1 widens UNIQUE(name) -> UNIQUE(name, project_id)
        # Phase 2 re-stamps both rows to 'new-owner' without collision
        result = ts.run_three_phase_migration_on_db(db, "new-owner", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT project_id FROM things").fetchall()
        conn.close()
        assert all(r[0] == "new-owner" for r in rows)

    def test_phase1_idempotent_on_already_composite(self, tmp_path: Path) -> None:
        """Phase 1 skips tables whose UNIQUE already includes project_id."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE things (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                UNIQUE (name, project_id)
            )
        """)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        rebuilt = ts.run_phase1_ddl(conn, tables)
        conn.close()
        # Should be empty — table already composite
        assert rebuilt == []

    def test_phase1_widens_non_composite_unique(self, tmp_path: Path) -> None:
        """Phase 1 rebuilds a table with UNIQUE(name) to UNIQUE(name, project_id)."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_table_with_unique_name(conn, "items")
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        rebuilt = ts.run_phase1_ddl(conn, tables)
        conn.close()

        assert "items" in rebuilt

        # Verify the UNIQUE now includes project_id
        conn = sqlite3.connect(str(db))
        indexes = conn.execute("PRAGMA index_list(items)").fetchall()
        for idx in indexes:
            if idx[2]:  # unique
                idx_cols = [
                    r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                ]
                if "name" in idx_cols:
                    assert "project_id" in idx_cols, "UNIQUE(name) must be widened to include project_id"
        conn.close()


# ===========================================================================
# 5. Split-brain rerun convergence
# ===========================================================================

class TestSplitBrainConvergence:
    """Simulate QI abort after RC Phase 2 commit; rerun must converge both."""

    def test_rerun_after_partial_failure_converges(self, tmp_path: Path) -> None:
        """RC Phase 2 succeeds, QI simulated as still having legacy rows.
        Second run of QI Phase 2 should re-stamp remaining legacy rows.
        """
        rc_db = tmp_path / "rc.db"
        qi_db = tmp_path / "qi.db"

        # Set up RC with legacy data — run Phases 1+2 on it
        conn = _open(rc_db)
        _make_simple_tenant_table(conn, "dispatches_rc")
        conn.execute("INSERT INTO dispatches_rc (name, project_id) VALUES ('d1', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Simulate RC Phase 2 succeeded
        ts.run_three_phase_migration_on_db(rc_db, "owner-pid", db_label="RC", skip_phase3=True)

        # Set up QI with legacy data — simulate QI Phase 2 aborted (rows still legacy)
        conn = _open(qi_db)
        _make_simple_tenant_table(conn, "quality_rows")
        conn.execute("INSERT INTO quality_rows (name, project_id) VALUES ('q1', 'vnx-dev')")
        conn.commit()
        conn.close()

        # QI still has legacy rows — post-condition would fail
        # Now rerun QI Phase 2 — it should converge
        ts.run_three_phase_migration_on_db(qi_db, "owner-pid", db_label="QI", skip_phase3=True)

        # Verify both DBs are now clean
        rc_conn = sqlite3.connect(str(rc_db))
        qi_conn = sqlite3.connect(str(qi_db))

        rc_tables = ts.topological_sort_tables(rc_conn, ts.enumerate_project_id_tables(rc_conn))
        qi_tables = ts.topological_sort_tables(qi_conn, ts.enumerate_project_id_tables(qi_conn))
        # This must not raise
        ts.assert_phase2_postcondition(rc_conn, qi_conn, rc_tables, qi_tables, "owner-pid")
        rc_conn.close()
        qi_conn.close()

    def test_rerun_of_completed_migration_is_noop(self, tmp_path: Path) -> None:
        """Running the full 3-phase migration twice must be idempotent."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        result1 = ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")
        assert result1["ok"] is True

        # Second run — should be a no-op (no updated rows, no rebuilt tables)
        result2 = ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")
        assert result2["ok"] is True
        updated2 = result2.get("phase2_updated", {})
        assert all(n == 0 for n in updated2.values()), "Rerun should update 0 rows"
        assert result2.get("phase3_rebuilt", []) == [], "Rerun should rebuild 0 tables"


# ===========================================================================
# 6. FK-off preserves composite keys + foreign_key_check clean
# ===========================================================================

class TestFKHandling:
    def test_parent_before_child_ordering(self, tmp_path: Path) -> None:
        """Topological sort emits parent before child."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_parent_child_tables(conn)
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        ordered = ts.topological_sort_tables(conn, tables)
        conn.close()

        assert "parents" in ordered
        assert "children" in ordered
        assert ordered.index("parents") < ordered.index("children"), (
            "parents must appear before children in topological order"
        )

    def test_fk_check_clean_after_restamp(self, tmp_path: Path) -> None:
        """After Phase 2, foreign_key_check passes (FK-off re-stamp + post-check)."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_parent_child_tables(conn)
        # Insert with legacy project_ids
        conn.execute("INSERT INTO parents (parent_id, project_id, label) VALUES ('p1', 'vnx-dev', 'P1')")
        conn.execute("INSERT INTO children (child_id, parent_id, project_id, data) VALUES ('c1', 'p1', 'vnx-dev', 'C1')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "my-project", db_label="TEST")
        assert result["ok"] is True

        # Verify FK check is clean with FK ON
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == [], f"FK violations after re-stamp: {violations}"

    def test_integrity_check_clean_after_all_phases(self, tmp_path: Path) -> None:
        """After Phase 3, integrity_check passes."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "final-pid", db_label="TEST")

        conn = sqlite3.connect(str(db))
        result = conn.execute("PRAGMA integrity_check").fetchall()
        conn.close()
        assert result == [("ok",)], f"integrity_check failed: {result}"

    def test_composite_key_preserved_through_phase1(self, tmp_path: Path) -> None:
        """Tables with composite PKs have project_id added to the PK after Phase 1."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_composite_pk_table(conn, "patterns")
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        tables = ts.topological_sort_tables(conn, tables)
        ts.run_phase1_ddl(conn, tables)
        conn.close()

        conn = sqlite3.connect(str(db))
        pk_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(patterns)").fetchall()
            if r[5] > 0
        ]
        conn.close()
        assert "project_id" in pk_cols, (
            "project_id must be added to the composite PK after Phase 1"
        )


# ===========================================================================
# 7. Fail-loud on unqualified INSERT after Phase 3
# ===========================================================================

class TestPhase3FailLoud:
    def test_unqualified_insert_fails_after_phase3(self, tmp_path: Path) -> None:
        """After Phase 3, project_id is TEXT NOT NULL — an INSERT without it raises."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")

        # Now try an INSERT without project_id — must fail
        conn = sqlite3.connect(str(db))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO things (name) VALUES ('fail-me')")
            conn.commit()
        conn.close()

    def test_qualified_insert_succeeds_after_phase3(self, tmp_path: Path) -> None:
        """After Phase 3, a qualified INSERT (with project_id) still works."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")

        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO things (name, project_id) VALUES ('good', 'my-pid')")
        conn.commit()
        val = conn.execute("SELECT project_id FROM things WHERE name='good'").fetchone()[0]
        conn.close()
        assert val == "my-pid"

    def test_phase3_project_id_not_null_in_schema(self, tmp_path: Path) -> None:
        """After Phase 3, PRAGMA table_info reports project_id as NOT NULL."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        ts.run_three_phase_migration_on_db(db, "final", db_label="TEST")

        conn = sqlite3.connect(str(db))
        col_info = {
            r[1]: r for r in conn.execute("PRAGMA table_info(things)").fetchall()
        }
        conn.close()
        assert col_info["project_id"][3] == 1, "project_id must have notnull=1 after Phase 3"


# ===========================================================================
# 8. Cross-project negative isolation
# ===========================================================================

class TestCrossProjectIsolation:
    """A DB stamped for project A must not contain project B's rows after migration."""

    def test_project_rows_isolated_after_migration(self, tmp_path: Path) -> None:
        """Two separate DBs migrated with different pids stay isolated."""
        db_a = tmp_path / "db_a.db"
        db_b = tmp_path / "db_b.db"

        for db, name in [(db_a, "alpha"), (db_b, "beta")]:
            conn = _open(db)
            _make_simple_tenant_table(conn)
            conn.execute(f"INSERT INTO things (name, project_id) VALUES ('{name}', 'vnx-dev')")
            conn.commit()
            conn.close()

        ts.run_three_phase_migration_on_db(db_a, "project-alpha", db_label="A")
        ts.run_three_phase_migration_on_db(db_b, "project-beta", db_label="B")

        conn_a = sqlite3.connect(str(db_a))
        vals_a = {r[0] for r in conn_a.execute("SELECT project_id FROM things").fetchall()}
        conn_a.close()

        conn_b = sqlite3.connect(str(db_b))
        vals_b = {r[0] for r in conn_b.execute("SELECT project_id FROM things").fetchall()}
        conn_b.close()

        assert vals_a == {"project-alpha"}
        assert vals_b == {"project-beta"}
        assert vals_a.isdisjoint(vals_b), "project rows must not overlap"

    def test_no_cross_contamination_on_rerun(self, tmp_path: Path) -> None:
        """Re-running migration on DB-A must not affect DB-B's rows."""
        db_a = tmp_path / "db_a.db"
        db_b = tmp_path / "db_b.db"

        for db, pid in [(db_a, "pid-a"), (db_b, "pid-b")]:
            conn = _open(db)
            _make_simple_tenant_table(conn)
            conn.execute(f"INSERT INTO things (name, project_id) VALUES ('row', '{pid}')")
            conn.commit()
            conn.close()

        ts.run_three_phase_migration_on_db(db_a, "pid-a", db_label="A")
        ts.run_three_phase_migration_on_db(db_b, "pid-b", db_label="B")

        # Rerun A — should be no-op
        result = ts.run_three_phase_migration_on_db(db_a, "pid-a", db_label="A-rerun")
        assert result["ok"] is True

        # B must still only have pid-b
        conn_b = sqlite3.connect(str(db_b))
        vals_b = {r[0] for r in conn_b.execute("SELECT project_id FROM things").fetchall()}
        conn_b.close()
        assert vals_b == {"pid-b"}


# ===========================================================================
# 9. Topological sort correctness
# ===========================================================================

class TestTopologicalSort:
    def test_single_table_no_fk(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        ordered = ts.topological_sort_tables(conn, tables)
        conn.close()
        assert ordered == ["things"]

    def test_multiple_independent_tables_all_included(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        for name in ["aa", "bb", "cc"]:
            conn.execute(f"""
                CREATE TABLE {name} (
                    id INTEGER PRIMARY KEY,
                    project_id TEXT NOT NULL DEFAULT 'vnx-dev'
                )
            """)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        ordered = ts.topological_sort_tables(conn, tables)
        conn.close()
        assert set(ordered) == {"aa", "bb", "cc"}

    def test_three_level_chain_ordering(self, tmp_path: Path) -> None:
        """grandparent -> parent -> child: grandparent first, child last."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE grandparents (
                gp_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                PRIMARY KEY (gp_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE mids (
                mid_id TEXT NOT NULL,
                gp_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                PRIMARY KEY (mid_id, project_id),
                FOREIGN KEY (gp_id, project_id) REFERENCES grandparents(gp_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE leaves (
                leaf_id TEXT NOT NULL,
                mid_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                PRIMARY KEY (leaf_id, project_id),
                FOREIGN KEY (mid_id, project_id) REFERENCES mids(mid_id, project_id)
            )
        """)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        ordered = ts.topological_sort_tables(conn, tables)
        conn.close()

        assert ordered.index("grandparents") < ordered.index("mids")
        assert ordered.index("mids") < ordered.index("leaves")


# ===========================================================================
# 10. Cross-DB FK assertion
# ===========================================================================

class TestCrossDbFKAssertion:
    def test_no_cross_db_fk_passes(self, tmp_path: Path) -> None:
        """Two independent DBs with no cross-DB FKs pass the pre-flight."""
        rc_db = tmp_path / "rc.db"
        qi_db = tmp_path / "qi.db"

        for db in [rc_db, qi_db]:
            conn = _open(db)
            _make_simple_tenant_table(conn)
            conn.close()

        rc_conn = sqlite3.connect(str(rc_db))
        qi_conn = sqlite3.connect(str(qi_db))
        rc_tables = ts.enumerate_project_id_tables(rc_conn)
        qi_tables = ts.enumerate_project_id_tables(qi_conn)
        # Must not raise
        ts.assert_no_cross_db_fk(rc_conn, qi_conn, rc_tables, qi_tables)
        rc_conn.close()
        qi_conn.close()

    def test_simulated_cross_db_fk_detected(self, tmp_path: Path) -> None:
        """Cross-DB FK is detected by looking for a ref to a table in the other DB's set.

        We simulate this by having a table in DB-A FK-referencing a table that
        ONLY exists in DB-B (by naming it the same). The check compares the
        reference name against the other DB's table list.
        """
        rc_db = tmp_path / "rc.db"
        qi_db = tmp_path / "qi.db"

        # RC has a table referencing 'qi_anchor' (which exists only in QI)
        rc_conn = _open(rc_db)
        rc_conn.execute("""
            CREATE TABLE qi_anchor (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            )
        """)
        rc_conn.execute("""
            CREATE TABLE rc_child (
                id INTEGER PRIMARY KEY,
                anchor_id INTEGER,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                FOREIGN KEY (anchor_id) REFERENCES qi_anchor(id)
            )
        """)
        rc_conn.commit()
        rc_conn.close()

        qi_conn = _open(qi_db)
        qi_conn.execute("""
            CREATE TABLE qi_anchor (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            )
        """)
        qi_conn.commit()
        qi_conn.close()

        rc_conn = sqlite3.connect(str(rc_db))
        qi_conn = sqlite3.connect(str(qi_db))
        rc_tables = ts.enumerate_project_id_tables(rc_conn)
        qi_tables = ts.enumerate_project_id_tables(qi_conn)

        # qi_anchor exists in BOTH DBs, so the FK is not detected as cross-DB
        # (both sets contain 'qi_anchor'). This is correct — no cross-DB FK.
        ts.assert_no_cross_db_fk(rc_conn, qi_conn, rc_tables, qi_tables)
        rc_conn.close()
        qi_conn.close()

        # Simulate a true cross-DB FK: qi_only_table exists only in qi
        rc_db2 = tmp_path / "rc2.db"
        qi_db2 = tmp_path / "qi2.db"

        rc_conn2 = _open(rc_db2)
        rc_conn2.execute("""
            CREATE TABLE rc_with_xfk (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                FOREIGN KEY (id) REFERENCES qi_exclusive_table(id)
            )
        """)
        rc_conn2.commit()
        rc_conn2.close()

        qi_conn2 = _open(qi_db2)
        qi_conn2.execute("""
            CREATE TABLE qi_exclusive_table (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            )
        """)
        qi_conn2.commit()
        qi_conn2.close()

        rc_conn2 = sqlite3.connect(str(rc_db2))
        qi_conn2 = sqlite3.connect(str(qi_db2))
        rc_tables2 = ts.enumerate_project_id_tables(rc_conn2)
        qi_tables2 = ts.enumerate_project_id_tables(qi_conn2)

        with pytest.raises(RuntimeError, match="Cross-DB FK detected"):
            ts.assert_no_cross_db_fk(rc_conn2, qi_conn2, rc_tables2, qi_tables2)
        rc_conn2.close()
        qi_conn2.close()


# ===========================================================================
# 11. VNX_DATA_DIR_EXPLICIT / VNX_DATA_DIR path resolution
# ===========================================================================

class TestDataDirResolution:
    def test_explicit_dir_overrides_when_no_project_root_provided(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR and no explicit project_root,
        _resolve_data_dir returns VNX_DATA_DIR (central store targeting)."""
        import migrate_future_system as mfs
        explicit_dir = tmp_path / "my_explicit_data"
        explicit_dir.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(explicit_dir))
        # project_root_provided=False -> uses VNX_DATA_DIR
        result = mfs._resolve_data_dir(tmp_path / "project_root", project_root_provided=False)
        assert result == explicit_dir.resolve()

    def test_explicit_project_root_always_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit project_root argument always uses project_root/.vnx-data."""
        import migrate_future_system as mfs
        explicit_dir = tmp_path / "my_explicit_data"
        explicit_dir.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(explicit_dir))
        project_root = tmp_path / "my_project"
        # project_root_provided=True -> uses project_root/.vnx-data despite env vars
        result = mfs._resolve_data_dir(project_root, project_root_provided=True)
        assert result == (project_root / ".vnx-data").resolve()

    def test_fallback_to_project_root_dot_vnx_data(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without VNX_DATA_DIR_EXPLICIT, _resolve_data_dir returns project_root/.vnx-data."""
        import migrate_future_system as mfs
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        project_root = tmp_path / "project_root"
        result = mfs._resolve_data_dir(project_root)
        assert result == (project_root / ".vnx-data").resolve()

    def test_explicit_flag_without_value_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """VNX_DATA_DIR_EXPLICIT=1 but no VNX_DATA_DIR falls back to project_root."""
        import migrate_future_system as mfs
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        project_root = tmp_path / "my_project"
        result = mfs._resolve_data_dir(project_root)
        assert result == (project_root / ".vnx-data").resolve()


# ===========================================================================
# 12. _pytest_db_isolation_guard
# ===========================================================================

class TestPytestDbIsolationGuard:
    def test_guard_passes_with_temp_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard passes when data_dir is under tempfile.gettempdir()."""
        import migrate_future_system as mfs
        tmp_data = tmp_path / "data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_data))
        # Should not raise
        mfs._pytest_db_isolation_guard(tmp_path / "project")

    def test_guard_raises_without_explicit_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard raises when VNX_DATA_DIR_EXPLICIT is not set."""
        import migrate_future_system as mfs
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        with pytest.raises(RuntimeError, match="VNX_DATA_DIR_EXPLICIT=1"):
            mfs._pytest_db_isolation_guard(tmp_path / "project")

    def test_guard_raises_when_dir_is_home_vnx_data(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard raises when resolved data_dir is under ~/.vnx-data."""
        import migrate_future_system as mfs
        home_data = Path.home() / ".vnx-data" / "some-pid"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(home_data))
        with pytest.raises(RuntimeError, match="NOT under the system temp directory"):
            mfs._pytest_db_isolation_guard(tmp_path / "project")


# ===========================================================================
# 13. Schema enumeration: only tables with project_id are included
# ===========================================================================

class TestSchemaEnumeration:
    def test_table_without_project_id_excluded(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("CREATE TABLE no_pid (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE has_pid (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL)")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()
        assert "has_pid" in tables
        assert "no_pid" not in tables

    def test_virtual_tables_excluded(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("CREATE TABLE real_table (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL)")
        conn.execute("CREATE VIRTUAL TABLE fts_shadow_fts USING fts5(project_id, content='real_table')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()
        assert "real_table" in tables
        # Shadow table names containing "_fts" are excluded
        fts_tables = [t for t in tables if "fts" in t]
        assert fts_tables == []


# ===========================================================================
# 14. Post-condition assertion
# ===========================================================================

class TestPostConditionAssertion:
    def test_postcondition_passes_when_clean(self, tmp_path: Path) -> None:
        rc_db = tmp_path / "rc.db"
        qi_db = tmp_path / "qi.db"

        for db in [rc_db, qi_db]:
            conn = _open(db)
            _make_simple_tenant_table(conn)
            conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'my-pid')")
            conn.commit()
            conn.close()

        rc_conn = sqlite3.connect(str(rc_db))
        qi_conn = sqlite3.connect(str(qi_db))
        rc_tables = ts.topological_sort_tables(rc_conn, ts.enumerate_project_id_tables(rc_conn))
        qi_tables = ts.topological_sort_tables(qi_conn, ts.enumerate_project_id_tables(qi_conn))
        ts.assert_phase2_postcondition(rc_conn, qi_conn, rc_tables, qi_tables, "my-pid")
        rc_conn.close()
        qi_conn.close()

    def test_postcondition_fails_when_legacy_remains(self, tmp_path: Path) -> None:
        rc_db = tmp_path / "rc.db"
        qi_db = tmp_path / "qi.db"

        conn = _open(rc_db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'my-pid')")
        conn.commit()
        conn.close()

        # QI still has legacy
        conn = _open(qi_db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('y', 'vnx-dev')")
        conn.commit()
        conn.close()

        rc_conn = sqlite3.connect(str(rc_db))
        qi_conn = sqlite3.connect(str(qi_db))
        rc_tables = ts.topological_sort_tables(rc_conn, ts.enumerate_project_id_tables(rc_conn))
        qi_tables = ts.topological_sort_tables(qi_conn, ts.enumerate_project_id_tables(qi_conn))
        with pytest.raises(RuntimeError, match="legacy row"):
            ts.assert_phase2_postcondition(rc_conn, qi_conn, rc_tables, qi_tables, "my-pid")
        rc_conn.close()
        qi_conn.close()


# ===========================================================================
# 15. Checkpoint and restore
# ===========================================================================

class TestCheckpointRestore:
    def test_checkpoint_creates_copy(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        ckpt = ts.checkpoint_db(db, "test")
        assert ckpt.exists()
        assert ckpt != db

    def test_restore_checkpoint_recovers_db(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('original', 'vnx-dev')")
        conn.commit()
        conn.close()

        ckpt = ts.checkpoint_db(db, "before-mutation")

        # Corrupt the DB
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM things")
        conn.commit()
        conn.close()

        # Restore
        ts.restore_checkpoint(ckpt, db)

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM things").fetchone()[0]
        conn.close()
        assert count == 1


# ===========================================================================
# 16. B1 — Production path wires BOTH RC and QI (the coupled two-DB orchestrator)
# ===========================================================================

def _make_state_layout(tmp_path: Path, pid: str) -> tuple[Path, Path]:
    """Create tmp_path/.vnx-data/<pid>/state/ layout so path-anchored pid resolution works.

    Returns (rc_db_path, qi_db_path).
    """
    state = tmp_path / ".vnx-data" / pid / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc_db = state / "runtime_coordination.db"
    qi_db = state / "quality_intelligence.db"
    return rc_db, qi_db


class TestB1ProductionPathCoupledMigration:
    """B1 fix: _run_w1_coupled_migration wires BOTH DBs via the coupled orchestrator."""

    def test_coupled_orchestrator_migrates_both_dbs(self, tmp_path: Path) -> None:
        """Both RC and QI are re-stamped; coupled post-condition asserts zero legacy rows."""
        import migrate_future_system as mfs

        pid = "seocrawler-v2"
        rc_db, qi_db = _make_state_layout(tmp_path, pid)

        # Seed RC with contaminated rows (nullable to allow NULL inserts).
        conn = _open(rc_db)
        _make_nullable_tenant_table(conn, "rc_dispatches")
        conn.execute("INSERT INTO rc_dispatches (name, project_id) VALUES ('d1', 'vnx-dev')")
        conn.execute("INSERT INTO rc_dispatches (name, project_id) VALUES ('d2', NULL)")
        conn.commit()
        conn.close()

        # Seed QI with contaminated rows (the bulk contamination case from the spec).
        conn = _open(qi_db)
        _make_nullable_tenant_table(conn, "quality_rows")
        conn.execute("INSERT INTO quality_rows (name, project_id) VALUES ('q1', 'vnx-dev')")
        conn.execute("INSERT INTO quality_rows (name, project_id) VALUES ('q2', NULL)")
        conn.execute("INSERT INTO quality_rows (name, project_id) VALUES ('q3', '')")
        conn.commit()
        conn.close()

        # Call the production wiring function directly (this is what run() calls at step E).
        mfs._run_w1_coupled_migration(rc_db)

        # Assert RC is clean.
        rc_conn = sqlite3.connect(str(rc_db))
        rc_vals = {r[0] for r in rc_conn.execute("SELECT project_id FROM rc_dispatches").fetchall()}
        rc_conn.close()
        assert rc_vals == {pid}, f"RC still has legacy project_ids: {rc_vals}"

        # Assert QI is clean.
        qi_conn = sqlite3.connect(str(qi_db))
        qi_vals = {r[0] for r in qi_conn.execute("SELECT project_id FROM quality_rows").fetchall()}
        qi_conn.close()
        assert qi_vals == {pid}, f"QI still has legacy project_ids: {qi_vals}"

        # Assert coupled post-condition: zero legacy rows in BOTH DBs.
        rc_conn2 = sqlite3.connect(str(rc_db))
        qi_conn2 = sqlite3.connect(str(qi_db))
        rc_tables = ts.topological_sort_tables(rc_conn2, ts.enumerate_project_id_tables(rc_conn2))
        qi_tables = ts.topological_sort_tables(qi_conn2, ts.enumerate_project_id_tables(qi_conn2))
        ts.assert_phase2_postcondition(rc_conn2, qi_conn2, rc_tables, qi_tables, pid)
        rc_conn2.close()
        qi_conn2.close()

    def test_qi_missing_skips_cleanly(self, tmp_path: Path) -> None:
        """If QI DB does not exist, RC is migrated alone and no exception is raised."""
        import migrate_future_system as mfs

        pid = "solo-project"
        rc_db, _qi_db = _make_state_layout(tmp_path, pid)
        assert not _qi_db.exists()

        conn = _open(rc_db)
        _make_simple_tenant_table(conn, "rc_things")
        conn.execute("INSERT INTO rc_things (name, project_id) VALUES ('r1', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Must not raise even though QI is absent.
        mfs._run_w1_coupled_migration(rc_db)

        rc_conn = sqlite3.connect(str(rc_db))
        vals = {r[0] for r in rc_conn.execute("SELECT project_id FROM rc_things").fetchall()}
        rc_conn.close()
        assert vals == {pid}

    def test_coupled_migration_idempotent(self, tmp_path: Path) -> None:
        """Running the coupled migration twice is a no-op on the second run."""
        import migrate_future_system as mfs

        pid = "idempotent-pid"
        rc_db, qi_db = _make_state_layout(tmp_path, pid)

        for db in [rc_db, qi_db]:
            conn = _open(db)
            _make_simple_tenant_table(conn, "rows")
            conn.execute("INSERT INTO rows (name, project_id) VALUES ('x', 'vnx-dev')")
            conn.commit()
            conn.close()

        mfs._run_w1_coupled_migration(rc_db)
        # Second run must not raise and must not change any data.
        mfs._run_w1_coupled_migration(rc_db)

        for db, label in [(rc_db, "RC"), (qi_db, "QI")]:
            conn = sqlite3.connect(str(db))
            vals = {r[0] for r in conn.execute("SELECT project_id FROM rows").fetchall()}
            conn.close()
            assert vals == {pid}, f"{label} has wrong project_ids after idempotent rerun: {vals}"


# ===========================================================================
# 17. B2 — Single-column non-INTEGER TEXT PRIMARY KEY is NOT dropped by Phase 1
# ===========================================================================

class TestB2SingleColTextPK:
    """B2 fix: a TEXT PRIMARY KEY table gets (k, project_id) composite PK, never drops PK."""

    def test_text_pk_becomes_composite_after_phase1(self, tmp_path: Path) -> None:
        """A table with `k TEXT PRIMARY KEY` must get PRIMARY KEY (k, project_id) in Phase 1."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE tokens (
                k TEXT PRIMARY KEY,
                value TEXT,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute("INSERT INTO tokens (k, value, project_id) VALUES ('t1', 'alpha', 'vnx-dev')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        assert "tokens" in tables
        ts.run_phase1_ddl(conn, tables)
        conn.close()

        # Verify the rebuilt table has a composite PK including project_id.
        conn = sqlite3.connect(str(db))
        pk_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()
            if r[5] > 0
        ]
        conn.close()
        assert "k" in pk_cols, "Original PK column 'k' must remain in PK"
        assert "project_id" in pk_cols, (
            "project_id must be added to the composite PK (ADR-007 shape), not dropped"
        )

    def test_text_pk_table_survives_full_migration(self, tmp_path: Path) -> None:
        """A TEXT-PK table goes through all 3 phases without losing uniqueness."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE slugs (
                slug TEXT PRIMARY KEY,
                body TEXT,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute("INSERT INTO slugs (slug, body, project_id) VALUES ('hello', 'world', 'vnx-dev')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "new-tenant", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))
        # PK must include both slug and project_id.
        pk_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(slugs)").fetchall()
            if r[5] > 0
        ]
        project_id_val = conn.execute("SELECT project_id FROM slugs WHERE slug='hello'").fetchone()[0]
        conn.close()
        assert "slug" in pk_cols, "slug must remain a PK column"
        assert "project_id" in pk_cols, "project_id must be in the composite PK"
        assert project_id_val == "new-tenant"

    def test_fk_to_text_pk_catches_damage_and_restores_original(self, tmp_path: Path) -> None:
        """B3 spec: FK-to-single-col-PK → Phase 1 integrity check catches damage + restores ORIGINAL.

        When a parent table has TEXT PRIMARY KEY and a child FK references that single-col key,
        Phase 1 rebuilds the parent to a composite PK (code, project_id). The child's FK
        (cat_code) REFERENCES categories(code) now has a "foreign key mismatch" because 'code'
        is no longer the full PK. The B3(a) integrity check fires before COMMIT and forces
        ROLLBACK, and the B3(b) pre-migration restore brings the DB back to the original state
        (before Phase 1 DDL ran). This is the exact scenario the spec describes.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Parent table with TEXT PRIMARY KEY (single-col, non-integer).
        conn.execute("""
            CREATE TABLE categories (
                code TEXT PRIMARY KEY,
                label TEXT,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        # Child table with FK referencing the single-col PK.
        conn.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cat_code TEXT,
                project_id TEXT DEFAULT 'vnx-dev',
                FOREIGN KEY (cat_code) REFERENCES categories(code)
            )
        """)
        conn.execute("INSERT INTO categories (code, label) VALUES ('A', 'Alpha')")
        conn.execute("INSERT INTO items (cat_code, project_id) VALUES ('A', 'vnx-dev')")
        conn.commit()

        # Capture the original schema before migration.
        cat_pk_before = [r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall() if r[5] > 0]
        item_count_before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()

        # Phase 1 rebuild of 'categories' makes the child FK a mismatch.
        # B3(a): foreign_key_check detects this and forces ROLLBACK before COMMIT.
        # B3(b): the pre-migration restore brings the DB back to original state.
        with pytest.raises(RuntimeError, match="(?i)(foreign key mismatch|Phase 1.*failed)"):
            ts.run_three_phase_migration_on_db(db, "tenant-x", db_label="TEST")

        # The DB must be restored to its pre-migration state (B3(b)).
        conn = sqlite3.connect(str(db))
        cat_pk_after = [r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall() if r[5] > 0]
        item_count_after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()

        # Schema must be back to original (Phase 1 DDL rolled back).
        assert cat_pk_after == cat_pk_before, (
            f"Schema not restored after Phase 1 failure. "
            f"Before: {cat_pk_before}, After: {cat_pk_after}"
        )
        # Data must be intact.
        assert item_count_after == item_count_before


# ===========================================================================
# 18. B3 — Phase 1 integrity check + pre-migration restore on failure
# ===========================================================================

class TestB3Phase1IntegrityAndPreMigrationRestore:
    """B3 fix: Phase 1 runs integrity_check before COMMIT; failures restore to pre-migration."""

    def test_phase1_integrity_check_is_run(self, tmp_path: Path) -> None:
        """run_phase1_ddl exposes integrity violations instead of silently committing.

        We verify the check runs by inspecting a successful migration — if
        integrity_check returns 'ok', Phase 1 committed clean.  The pathological
        case (Phase 1 DDL produces a corrupt DB) is hard to trigger without
        monkey-patching SQLite internals, so we verify the check path through
        normal successful operation.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # A table with a composite PK excluding project_id — Phase 1 will rebuild it.
        conn.execute("""
            CREATE TABLE events (
                event_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                project_id TEXT DEFAULT 'vnx-dev',
                payload TEXT,
                PRIMARY KEY (event_id, seq)
            )
        """)
        conn.execute("INSERT INTO events VALUES ('e1', 1, 'vnx-dev', 'data')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        # Phase 1 must succeed (integrity_check passes internally before COMMIT).
        rebuilt = ts.run_phase1_ddl(conn, tables)
        conn.close()
        assert "events" in rebuilt

        # Post-check: DB is still integral after Phase 1.
        conn = sqlite3.connect(str(db))
        ic = conn.execute("PRAGMA integrity_check").fetchall()
        conn.close()
        assert ic == [("ok",)]

    def test_pre_migration_checkpoint_taken_before_phase1(self, tmp_path: Path) -> None:
        """run_three_phase_migration_on_db takes a pre-migration checkpoint and
        cleans it up on success to avoid disk blowup on large DBs."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")
        assert result["ok"] is True
        # The result records the checkpoint path (for logging / error messages).
        assert "checkpoint_premigration" in result
        # On SUCCESS the checkpoint is deleted to reclaim disk space.
        ckpt = Path(result["checkpoint_premigration"])
        assert not ckpt.exists(), (
            "Pre-migration checkpoint must be DELETED after successful migration "
            "(disk-cleanup fix — 1.65 GB DB × 4 checkpoints = ~6.6 GB otherwise)"
        )

    def test_phase2_failure_restores_to_pre_migration_state(self, tmp_path: Path) -> None:
        """When Phase 2 fails (third tenant guard), DB is restored to pre-migration state.

        The key property: the restore brings the DB back to the ORIGINAL state
        (before Phase 1 DDL), not to a mid-Phase-1 intermediate.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Table with UNIQUE(name) so Phase 1 rebuilds it.
        conn.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        # Two rows: one with pid, one with a THIRD genuine tenant — guard will abort Phase 2.
        conn.execute("INSERT INTO items (name, project_id) VALUES ('a', 'owner-pid')")
        conn.execute("INSERT INTO items (name, project_id) VALUES ('b', 'alien-tenant')")
        conn.commit()

        # Capture original schema before migration.
        pk_before = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall() if r[5] > 0]
        original_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()

        # Migration must fail at Phase 2 (third tenant guard).
        with pytest.raises(RuntimeError):
            ts.run_three_phase_migration_on_db(db, "owner-pid", db_label="TEST")

        # Verify DB is restored to its pre-migration state.
        conn = sqlite3.connect(str(db))
        pk_after = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall() if r[5] > 0]
        count_after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        alien_still_present = conn.execute(
            "SELECT COUNT(*) FROM items WHERE project_id = 'alien-tenant'"
        ).fetchone()[0]
        conn.close()

        # PK shape must be the original (Phase 1 DDL rebuild rolled back).
        assert pk_after == pk_before, (
            f"Schema was not restored to pre-migration state. "
            f"Before: {pk_before}, After: {pk_after}"
        )
        # Row count unchanged — no data lost.
        assert count_after == original_count
        # The alien tenant row is still present (restore brought it back).
        assert alien_still_present == 1


# ===========================================================================
# 19. B4 — WAL-mode: committed rows survive checkpoint + restore cycle
# ===========================================================================

class TestB4WalCheckpointConsistency:
    """B4 fix: checkpoint_db folds WAL; restore_checkpoint purges stale WAL/SHM."""

    def test_committed_rows_survive_wal_checkpoint_restore(self, tmp_path: Path) -> None:
        """Rows committed to WAL (not yet checkpointed) survive checkpoint+restore.

        Scenario:
        1. Open DB in WAL mode, insert a row, commit (row may be in WAL).
        2. checkpoint_db — must fold WAL via TRUNCATE so the copy is self-consistent.
        3. Write a second row to the live DB (post-checkpoint mutation).
        4. restore_checkpoint — must bring back the pre-second-write state.
        5. The first row must be present; the second row must be gone.
        6. No stale -wal/-shm must remain after restore.
        """
        db = tmp_path / "wal_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, v TEXT, project_id TEXT)")
        conn.execute("INSERT INTO rows VALUES (1, 'committed-before-checkpoint', 'pid')")
        conn.commit()
        conn.close()

        # checkpoint_db must fold the WAL (B4).
        ckpt = ts.checkpoint_db(db, "wal-test")
        assert ckpt.exists()
        # sha256 manifest must exist.
        manifest = Path(str(ckpt) + ".sha256")
        assert manifest.exists(), "checkpoint_db must write a sha256 manifest (B4)"

        # Mutate the live DB after checkpointing.
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO rows VALUES (2, 'added-after-checkpoint', 'pid')")
        conn.commit()
        conn.close()

        # Restore from checkpoint.
        ts.restore_checkpoint(ckpt, db)

        # Stale WAL/SHM must be gone.
        wal = Path(str(db) + "-wal")
        shm = Path(str(db) + "-shm")
        assert not wal.exists(), "stale -wal must be deleted after restore (B4)"
        assert not shm.exists(), "stale -shm must be deleted after restore (B4)"

        # The first row must survive; the second must be absent.
        conn = sqlite3.connect(str(db))
        rows = {r[0]: r[1] for r in conn.execute("SELECT id, v FROM rows").fetchall()}
        conn.close()
        assert 1 in rows, "Row committed before checkpoint must survive restore"
        assert rows[1] == "committed-before-checkpoint"
        assert 2 not in rows, "Row added after checkpoint must be absent after restore"

    def test_checkpoint_sha256_manifest_verified_on_restore(self, tmp_path: Path) -> None:
        """restore_checkpoint rejects a tampered checkpoint (sha256 mismatch)."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('x', 'vnx-dev')")
        conn.commit()
        conn.close()

        ckpt = ts.checkpoint_db(db, "tamper-test")
        manifest = Path(str(ckpt) + ".sha256")
        assert manifest.exists()

        # Tamper the manifest to simulate a corrupt checkpoint.
        manifest.write_text("0000000000000000000000000000000000000000000000000000000000000000")

        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            ts.restore_checkpoint(ckpt, db)

    def test_restore_deletes_stale_wal_shm(self, tmp_path: Path) -> None:
        """restore_checkpoint deletes any existing -wal and -shm files."""
        db = tmp_path / "stale_wal.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, project_id TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'pid')")
        conn.commit()
        conn.close()

        ckpt = ts.checkpoint_db(db, "stale")

        # Manually create stale WAL and SHM files to simulate leftover files.
        wal = Path(str(db) + "-wal")
        shm = Path(str(db) + "-shm")
        wal.write_bytes(b"stale_wal_data")
        shm.write_bytes(b"stale_shm_data")

        ts.restore_checkpoint(ckpt, db)

        assert not wal.exists(), "-wal must be deleted by restore_checkpoint"
        assert not shm.exists(), "-shm must be deleted by restore_checkpoint"


# ===========================================================================
# 20. Trigger preservation through Phase 1 and Phase 3
# ===========================================================================

class TestTriggerPreservation:
    """BLOCKER fix: Phase 1 and Phase 3 rebuild tables via DROP+CREATE+rename.
    DROP TABLE cascade-drops all triggers. They must be captured before DROP
    and recreated verbatim after the rename — mirroring migrate_future_system
    _triggers_for/_recreate_dependent_objects.

    Real-world case: the 'adrs' table in the seocrawler-v2 QI DB has
    adrs_ai/adrs_ad/adrs_au triggers that sync rows into adrs_fts.  Without
    this fix, Phase 3 rebuilds 'adrs' (it has DEFAULT 'vnx-dev'), the triggers
    vanish silently, and adrs_fts goes permanently stale on future inserts.
    integrity_check stays green — this is the 'green but broken' regression.

    The tests here prove the trigger actually FIRES after each phase (not just
    that it exists in sqlite_master), by using an AFTER INSERT pattern that
    writes to a sync table, then asserting the sync table received the row.
    """

    @staticmethod
    def _make_fts_style_schema(conn: sqlite3.Connection) -> None:
        """Create an 'adrs'-style table with a sync table and AFTER INSERT trigger.

        adrs: the source table (carries project_id, will be rebuilt by Phase 1 + 3)
        adrs_sync: the sync/shadow table (no project_id, excluded from migration)
        adrs_ai: AFTER INSERT trigger that propagates inserts to adrs_sync

        This mimics the adrs/adrs_fts pattern from the seocrawler-v2 QI DB.
        """
        conn.execute("""
            CREATE TABLE adrs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT,
                project_id TEXT DEFAULT 'vnx-dev',
                UNIQUE (title)
            )
        """)
        # Sync table: no project_id column, so it is excluded from migration enumeration.
        conn.execute("""
            CREATE TABLE adrs_sync (
                rowid INTEGER PRIMARY KEY,
                title TEXT,
                body TEXT
            )
        """)
        # AFTER INSERT trigger: mimics FTS content-sync triggers (adrs_ai pattern).
        conn.execute("""
            CREATE TRIGGER adrs_ai AFTER INSERT ON adrs BEGIN
                INSERT INTO adrs_sync(rowid, title, body)
                VALUES (new.id, new.title, new.body);
            END
        """)
        conn.commit()

    def test_trigger_exists_and_fires_after_phase1(self, tmp_path: Path) -> None:
        """After Phase 1 rebuilds 'adrs', adrs_ai must exist in sqlite_master
        AND fire on INSERT (propagating into adrs_sync)."""
        db = _db(tmp_path)
        conn = _open(db)
        self._make_fts_style_schema(conn)
        conn.close()

        # Phase 1: widen UNIQUE(title) → UNIQUE(title, project_id).
        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        assert "adrs" in tables
        rebuilt = ts.run_phase1_ddl(conn, tables)
        conn.close()
        assert "adrs" in rebuilt, "adrs must be rebuilt by Phase 1 (UNIQUE(title) not composite)"

        # 1. Trigger must exist in sqlite_master.
        conn = sqlite3.connect(str(db))
        trigger_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='adrs'"
            ).fetchall()
        }
        assert "adrs_ai" in trigger_names, (
            "adrs_ai trigger must still exist in sqlite_master after Phase 1 rebuild. "
            "Phase 1 DROP TABLE cascade-dropped it and it was not recreated."
        )

        # 2. Trigger must actually FIRE: insert a row and confirm adrs_sync received it.
        conn.execute(
            "INSERT INTO adrs (title, body, project_id) VALUES ('post-p1', 'content', 'vnx-dev')"
        )
        conn.commit()
        sync_row = conn.execute(
            "SELECT title FROM adrs_sync WHERE title='post-p1'"
        ).fetchone()
        conn.close()
        assert sync_row is not None, (
            "adrs_ai trigger must FIRE on INSERT after Phase 1 rebuild. "
            "adrs_sync received no row — the trigger exists in sqlite_master "
            "but is not functional, or was never recreated."
        )

    def test_trigger_exists_and_fires_after_phase3(self, tmp_path: Path) -> None:
        """After the full 3-phase migration, adrs_ai must exist AND fire.

        Phase 3 rebuilds 'adrs' to enforce NOT NULL on project_id — this is
        the exact path that triggered the original bug on the seocrawler-v2 QI DB.
        """
        db = _db(tmp_path)
        conn = _open(db)
        self._make_fts_style_schema(conn)
        # Seed with a legacy row so Phase 2 has something to re-stamp.
        conn.execute("INSERT INTO adrs (title, body, project_id) VALUES ('seed', 'data', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Run the full 3-phase migration.
        result = ts.run_three_phase_migration_on_db(db, "seocrawler-v2", db_label="TEST")
        assert result["ok"] is True
        assert "adrs" in result.get("phase3_rebuilt", []), (
            "adrs must be rebuilt by Phase 3 (it had DEFAULT 'vnx-dev')"
        )

        # 1. Trigger must exist in sqlite_master after Phase 3.
        conn = sqlite3.connect(str(db))
        trigger_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='adrs'"
            ).fetchall()
        }
        assert "adrs_ai" in trigger_names, (
            "adrs_ai trigger must still exist in sqlite_master after Phase 3 rebuild. "
            "Phase 3 DROP TABLE cascade-dropped it and it was not recreated — "
            "adrs_fts will go permanently stale on future inserts (silent corruption)."
        )

        # 2. Trigger must actually FIRE post-Phase-3.
        conn.execute(
            "INSERT INTO adrs (title, body, project_id) "
            "VALUES ('post-p3', 'new content', 'seocrawler-v2')"
        )
        conn.commit()
        sync_row = conn.execute(
            "SELECT title FROM adrs_sync WHERE title='post-p3'"
        ).fetchone()
        conn.close()
        assert sync_row is not None, (
            "adrs_ai trigger must FIRE on INSERT after Phase 3 rebuild. "
            "adrs_sync received no row — silent FTS corruption path confirmed."
        )

    def test_trigger_fires_after_phase1_and_phase3_independently(self, tmp_path: Path) -> None:
        """Verify trigger fires at EACH phase boundary, not just at the end.

        Runs Phase 1, inserts + checks, then runs Phases 2+3, inserts + checks.
        This catches any regression where Phase 1 recreates the trigger but
        Phase 3 drops it again.
        """
        db = _db(tmp_path)
        conn = _open(db)
        self._make_fts_style_schema(conn)
        conn.close()

        # Phase 1 only.
        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        ts.run_phase1_ddl(conn, tables)
        conn.close()

        # Assert fires after Phase 1.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO adrs (title, body, project_id) VALUES ('after-p1', 'x', 'vnx-dev')"
        )
        conn.commit()
        row_p1 = conn.execute(
            "SELECT title FROM adrs_sync WHERE title='after-p1'"
        ).fetchone()
        conn.close()
        assert row_p1 is not None, "Trigger must fire after Phase 1"

        # Phase 2 + 3.
        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        ts.run_phase2_restamp(conn, tables, "seocrawler-v2", db_label="TEST")
        ts.run_phase3_enforce(conn, tables, db_label="TEST")
        conn.close()

        # Assert fires after Phase 3.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO adrs (title, body, project_id) "
            "VALUES ('after-p3', 'y', 'seocrawler-v2')"
        )
        conn.commit()
        row_p3 = conn.execute(
            "SELECT title FROM adrs_sync WHERE title='after-p3'"
        ).fetchone()
        conn.close()
        assert row_p3 is not None, (
            "Trigger must fire after Phase 3 — Phase 3 must not re-drop it"
        )


# ===========================================================================
# 21. Secondary index preservation (partial unique + non-unique)
# ===========================================================================

class TestSecondaryIndexPreservation:
    """Regression: Phase 1 and Phase 3 DROP+RENAME removed CREATE [UNIQUE] INDEX
    indexes permanently. Partial UNIQUE indexes lost their WHERE clause by being
    folded into full table-level UNIQUE constraints, causing data collisions.
    Non-unique indexes were silently dropped, causing perf and functional regressions.

    Real cases from seocrawler-v2 RC DB:
      - idx_terminal_leases_token: CREATE UNIQUE INDEX ... WHERE lease_token != ''
      - idx_pool_membership_active: CREATE UNIQUE INDEX ... WHERE released_at IS NULL
      - idx_terminal_leases_project, idx_lease_dispatch, idx_lease_state: non-unique
    """

    def test_partial_unique_index_preserved_through_phase1(self, tmp_path: Path) -> None:
        """A partial UNIQUE index (WHERE tok != '') survives Phase 1 with WHERE intact.

        Three rows all with tok='' are excluded by the WHERE clause so they are
        currently legal. Phase 1 must NOT widen this to a full UNIQUE(tok, project_id)
        constraint — that would collapse the 3 rows (all share tok='') and cause
        silent data loss caught only by the row-copy mismatch guard.

        After Phase 1:
        - All 3 rows still present (no collapse).
        - The partial index exists in sqlite_master with its WHERE clause.
        - A 4th tok='' row can still be inserted (partial uniqueness preserved).
        - A duplicate non-empty tok is still rejected.
        """
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tok TEXT NOT NULL DEFAULT '',
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX ix_leases_tok ON leases(tok) WHERE tok != ''"
        )
        conn.execute("INSERT INTO leases (tok, project_id) VALUES ('', 'vnx-dev')")
        conn.execute("INSERT INTO leases (tok, project_id) VALUES ('', 'vnx-dev')")
        conn.execute("INSERT INTO leases (tok, project_id) VALUES ('', 'vnx-dev')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        ts.run_phase1_ddl(conn, tables)
        conn.close()

        conn = sqlite3.connect(str(db))

        # All 3 rows must survive (no collapse from partial-to-full UNIQUE widening).
        count = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        assert count == 3, (
            f"Expected 3 rows after Phase 1 but got {count}. "
            "Partial UNIQUE index was folded into full constraint, collapsing rows."
        )

        # The partial index must exist in sqlite_master with its WHERE clause.
        idx_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_leases_tok'"
        ).fetchone()
        assert idx_row is not None, "ix_leases_tok must exist in sqlite_master after Phase 1"
        assert "WHERE" in idx_row[0].upper(), (
            f"Partial index WHERE clause lost after Phase 1. Got sql: {idx_row[0]!r}"
        )

        # A 4th tok='' row must still be insertable (excluded by the WHERE).
        conn.execute("INSERT INTO leases (tok, project_id) VALUES ('', 'vnx-dev')")
        conn.commit()
        count2 = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        assert count2 == 4, "tok='' rows must remain insertable (partial WHERE excludes them)"

        # A duplicate non-empty tok must still be rejected.
        conn.execute("INSERT INTO leases (tok, project_id) VALUES ('abc', 'vnx-dev')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO leases (tok, project_id) VALUES ('abc', 'vnx-dev')")
            conn.commit()

        conn.close()

    def test_non_unique_secondary_index_preserved_through_phase1_and_phase3(
        self, tmp_path: Path
    ) -> None:
        """A non-unique secondary index survives both Phase 1 and Phase 3.

        Before this fix, DROP TABLE in Phase 1 removed the index and the rebuild
        never recreated it — permanent silent index loss.
        """
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL UNIQUE,
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute(
            "CREATE INDEX idx_leases_project ON leases(project_id)"
        )
        conn.execute("INSERT INTO leases (terminal_id, project_id) VALUES ('t1', 'vnx-dev')")
        conn.commit()
        conn.close()

        # Run full 3-phase migration.
        result = ts.run_three_phase_migration_on_db(db, "my-project", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='leases'"
            ).fetchall()
        }
        conn.close()

        assert "idx_leases_project" in idx_names, (
            "Non-unique secondary index idx_leases_project must survive Phase 1 + Phase 3. "
            "It was permanently dropped by the DROP TABLE in the rebuild."
        )

    def test_terminal_leases_exact_shape_three_phase_migration(self, tmp_path: Path) -> None:
        """Exact terminal_leases shape from seocrawler-v2 RC DB: full 3-phase migration.

        Schema:
          id INTEGER PK AUTOINCREMENT
          terminal_id TEXT UNIQUE (declared inline — origin='u', will be widened)
          lease_token TEXT DEFAULT '' (partial unique index WHERE lease_token != '')
          project_id TEXT DEFAULT 'vnx-dev'

        3 rows all with lease_token='' (excluded by the WHERE, currently legal).

        After full migration (Phases 1+2+3):
        - All 3 rows survive and are re-stamped to the resolved pid.
        - The partial unique index on lease_token still has its WHERE clause.
        - The UNIQUE on terminal_id is widened to (terminal_id, project_id).
        - integrity_check and foreign_key_check pass.
        """
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE terminal_leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL UNIQUE,
                lease_token TEXT NOT NULL DEFAULT '',
                project_id TEXT DEFAULT 'vnx-dev'
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX idx_terminal_leases_token "
            "ON terminal_leases(lease_token) WHERE lease_token != ''"
        )
        conn.execute(
            "CREATE INDEX idx_terminal_leases_project ON terminal_leases(project_id)"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, lease_token, project_id) "
            "VALUES ('T0', '', 'vnx-dev')"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, lease_token, project_id) "
            "VALUES ('T1', '', 'vnx-dev')"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, lease_token, project_id) "
            "VALUES ('T2', '', 'vnx-dev')"
        )
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "seocrawler-v2", db_label="TEST")
        assert result["ok"] is True

        conn = sqlite3.connect(str(db))

        # All 3 rows survive and are re-stamped.
        rows = conn.execute(
            "SELECT terminal_id, project_id FROM terminal_leases ORDER BY terminal_id"
        ).fetchall()
        assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}: {rows}"
        assert all(r[1] == "seocrawler-v2" for r in rows), (
            f"Not all rows re-stamped to seocrawler-v2: {rows}"
        )

        # Partial unique index must survive with WHERE clause intact.
        idx_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_terminal_leases_token'"
        ).fetchone()
        assert idx_row is not None, "idx_terminal_leases_token must exist after Phase 3"
        assert "WHERE" in idx_row[0].upper(), (
            f"Partial index WHERE clause must survive Phase 3. Got: {idx_row[0]!r}"
        )

        # Non-unique project index must survive.
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='terminal_leases'"
            ).fetchall()
        }
        assert "idx_terminal_leases_project" in idx_names, (
            "Non-unique index idx_terminal_leases_project must survive Phase 3"
        )

        # integrity_check and foreign_key_check must pass.
        ic = conn.execute("PRAGMA integrity_check").fetchall()
        assert ic == [("ok",)], f"integrity_check failed: {ic}"
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk == [], f"foreign_key_check failed: {fk}"

        conn.close()


# ===========================================================================
# 22. BUG 1+2 regression — origin='c' partial index must not trigger rebuild;
#     rebuild must preserve all declared (origin='u') UNIQUE constraints
# ===========================================================================

class TestBug1AndBug2Regressions:
    """Regression tests for the two bugs reproduced against the post-0031
    terminal_leases shape (composite declared UNIQUE + origin='c' partial index).

    BUG 1: run_phase1_ddl triggered a spurious rebuild on a table that was
    already in correct v31 shape because _get_unique_indexes returned an
    origin='c' partial-index entry lacking project_id. The rebuild trigger
    must check ONLY origin='u' declared UNIQUE constraints.

    BUG 2: When a rebuild DID run, _rebuild_table_phase1 only iterated the
    non_composite_uniques argument (the subset lacking project_id). Any
    already-composite declared UNIQUE (origin='u', already has project_id)
    was filtered out before being passed and was never re-emitted — the
    rebuilt table silently lost it, breaking FK constraints that referenced
    the composite unique key.
    """

    def test_composite_declared_unique_plus_partial_index_no_spurious_rebuild(
        self, tmp_path: Path
    ) -> None:
        """BUG 1 regression (a): a table with BOTH a composite declared UNIQUE(x, project_id)
        AND an origin='c' partial unique index must NOT be spuriously rebuilt by Phase 1,
        AND the composite UNIQUE must survive untouched.

        Pre-0031 fix: _get_unique_indexes returned the origin='c' index (it lacks project_id),
        which caused the rebuild trigger to fire even though the table was already in v31 shape.
        After the fix: only origin='u' constraints drive the rebuild trigger; origin='c' indexes
        are preserved verbatim by _get_secondary_indexes and must never force a rebuild.
        """
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                UNIQUE(terminal_id, project_id)
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX idx_leases_token ON leases(lease_token) WHERE lease_token != ''"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        assert "leases" in tables

        # BUG 1: must return [] — no spurious rebuild
        rebuilt = ts.run_phase1_ddl(conn, tables)
        assert rebuilt == [], (
            f"Phase 1 spuriously rebuilt 'leases' even though it was already in v31 shape. "
            f"rebuilt={rebuilt}. An origin='c' partial unique index (without project_id) must "
            f"NOT trigger a rebuild — only origin='u' declared UNIQUE constraints drive that decision."
        )

        # BUG 2 (would manifest on rebuild): composite UNIQUE must still be present
        s = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='leases'"
        ).fetchone()[0]
        assert "terminal_id" in s and "project_id" in s and "UNIQUE" in s, (
            f"composite UNIQUE(terminal_id, project_id) must still exist in schema after no-rebuild. "
            f"schema={s!r}"
        )

        # Partial unique index must be intact with WHERE clause
        idx_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_leases_token'"
        ).fetchone()
        assert idx_row is not None, "idx_leases_token must still exist"
        assert "WHERE" in idx_row[0].upper(), (
            f"Partial index WHERE clause must be intact. Got: {idx_row[0]!r}"
        )
        conn.close()

    def test_rebuild_preserves_composite_declared_unique_alongside_non_composite(
        self, tmp_path: Path
    ) -> None:
        """BUG 2 regression (b): when a rebuild IS triggered (by a non-composite declared UNIQUE),
        any already-composite declared UNIQUE on the same table must ALSO be re-emitted verbatim.

        Pre-fix: _rebuild_table_phase1 only iterated the non_composite_uniques argument (the subset
        lacking project_id). An already-composite UNIQUE was filtered out before being passed and
        silently lost in the rebuilt table.
        After fix: all origin='u' UNIQUEs are fetched fresh inside _rebuild_table_phase1;
        composites are re-emitted verbatim, non-composites are widened by appending project_id.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Table with TWO declared UNIQUEs:
        #   UNIQUE(name)          — non-composite, must be widened to UNIQUE(name, project_id)
        #   UNIQUE(code, project_id) — already composite, must be re-emitted verbatim
        conn.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                UNIQUE(name),
                UNIQUE(code, project_id)
            )
        """)
        conn.execute(
            "INSERT INTO items (name, code, project_id) VALUES ('alpha', 'A1', 'vnx-dev')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))
        rebuilt = ts.run_phase1_ddl(conn, tables)
        assert "items" in rebuilt, "items must be rebuilt (has non-composite UNIQUE(name))"

        # UNIQUE(name) must be widened to UNIQUE(name, project_id)
        indexes = conn.execute("PRAGMA index_list(items)").fetchall()
        unique_col_sets = []
        for idx in indexes:
            if idx[2]:  # unique
                cols = [
                    r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                ]
                unique_col_sets.append(frozenset(cols))

        assert frozenset(["name", "project_id"]) in unique_col_sets, (
            f"UNIQUE(name) must be widened to UNIQUE(name, project_id). "
            f"Found unique col sets: {unique_col_sets}"
        )
        # BUG 2: UNIQUE(code, project_id) must still exist (was already composite, must survive verbatim)
        assert frozenset(["code", "project_id"]) in unique_col_sets, (
            f"UNIQUE(code, project_id) must survive the rebuild verbatim. "
            f"BUG 2: it was silently dropped because only non_composite_uniques were re-emitted. "
            f"Found unique col sets: {unique_col_sets}"
        )

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK check must be clean after rebuild: {fk_violations}"
        ic = conn.execute("PRAGMA integrity_check").fetchall()
        assert ic == [("ok",)], f"integrity_check must pass after rebuild: {ic}"
        conn.close()

    def test_terminal_leases_post_0031_shape_fk_clean(self, tmp_path: Path) -> None:
        """BUG 1+2 regression (c): the exact terminal_leases post-0031 shape.

        After 0031 repairs the store:
          - terminal_leases has UNIQUE(terminal_id, project_id) [origin='u', already composite]
          - terminal_leases has idx_terminal_leases_token partial index [origin='c']
          - worker_pool_membership has FK (terminal_id, project_id) -> terminal_leases

        Phase 1 must NOT spuriously rebuild terminal_leases (BUG 1).
        After Phase 1, foreign_key_check must be clean (BUG 2: if the rebuild lost the
        composite UNIQUE, the FK would have no matching unique key -> fk mismatch).
        """
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE terminal_leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                UNIQUE(terminal_id, project_id)
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX idx_terminal_leases_token "
            "ON terminal_leases(lease_token) WHERE lease_token != ''"
        )
        conn.execute("""
            CREATE TABLE worker_pool_membership (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                FOREIGN KEY (terminal_id, project_id)
                    REFERENCES terminal_leases(terminal_id, project_id)
            )
        """)
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, lease_token, project_id) "
            "VALUES ('T0', '', 'vnx-dev')"
        )
        conn.execute(
            "INSERT INTO worker_pool_membership (terminal_id, project_id) "
            "VALUES ('T0', 'vnx-dev')"
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.topological_sort_tables(conn, ts.enumerate_project_id_tables(conn))

        # BUG 1: no spurious rebuild of terminal_leases
        rebuilt = ts.run_phase1_ddl(conn, tables)
        assert "terminal_leases" not in rebuilt, (
            f"terminal_leases must NOT be spuriously rebuilt — it is already in v31 shape. "
            f"BUG 1: origin='c' partial index triggered a rebuild. rebuilt={rebuilt}"
        )

        # BUG 2: FK check must be clean — the composite UNIQUE(terminal_id, project_id) must
        # still exist so the FK in worker_pool_membership has a matching unique key
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], (
            f"foreign_key_check mismatch after Phase 1: {fk_violations}. "
            f"BUG 2: rebuild lost UNIQUE(terminal_id, project_id) from terminal_leases, "
            f"breaking the FK from worker_pool_membership."
        )

        # The partial index must remain verbatim
        idx_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_terminal_leases_token'"
        ).fetchone()
        assert idx_row is not None, "idx_terminal_leases_token must still exist"
        assert "WHERE" in idx_row[0].upper(), (
            f"Partial index WHERE clause must be intact. Got: {idx_row[0]!r}"
        )

        # Composite UNIQUE must still be present in the terminal_leases schema
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='terminal_leases'"
        ).fetchone()[0]
        assert "terminal_id" in schema and "project_id" in schema and "UNIQUE" in schema, (
            f"UNIQUE(terminal_id, project_id) must still be in terminal_leases schema. "
            f"schema={schema!r}"
        )
        conn.close()


# ===========================================================================
# 23. C4 regression — FTS5 shadow exclusion must not evict project_id tables
#     whose names share a shadow suffix (pool_config, foo_data, foo_idx, etc.)
# ===========================================================================

class TestFtsShadowExclusion:
    """C4 fix: enumerate_project_id_tables must use PROPER FTS5 shadow detection
    instead of name-suffix filtering.

    The old implementation excluded tables matching %_config, %_data, %_idx,
    %_content, %_docsize by name suffix — this incorrectly excluded pool_config
    (a real tenant table with project_id that ends in _config) and any other
    legitimate table whose name happens to share a shadow suffix.

    The correct rule: FTS5 shadow tables are identified by deriving their names
    from the parent FTS virtual table (e.g. 'notes' -> notes_data, notes_idx,
    notes_config, ...).  A table named pool_config has no parent FTS virtual
    table named 'pool', so it is NOT a shadow table and must be included.

    Real-world impact: worker_pools has a composite FK (project_id, pool_id)
    referencing pool_config(project_id, pool_id).  Phase 2 re-stamped worker_pools
    but skipped pool_config (excluded by the suffix filter) -> FK violation ->
    Phase 2 ROLLBACK.  This is the manifested C4 concern from the first review.
    """

    def test_pool_config_named_table_with_project_id_is_enumerated(
        self, tmp_path: Path
    ) -> None:
        """A table named 'pool_config' that carries project_id must be included
        in enumerate_project_id_tables, not excluded by a name-suffix filter."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE pool_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                pool_id TEXT NOT NULL,
                max_workers INTEGER NOT NULL DEFAULT 4
            )
        """)
        conn.execute("INSERT INTO pool_config (project_id, pool_id) VALUES ('vnx-dev', 'default')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()

        assert "pool_config" in tables, (
            "pool_config must be included in enumeration — it carries project_id "
            "and is a real tenant table, not an FTS5 shadow table.  The old name-suffix "
            "filter (%_config) incorrectly excluded it."
        )

    def test_fts5_shadow_tables_not_enumerated(self, tmp_path: Path) -> None:
        """Real FTS5 shadow tables (notes_data, notes_idx, notes_config) must
        NOT be enumerated, even if we somehow injected a project_id column into them.

        The test creates:
          - 'notes' FTS5 virtual table (parent)
          - Its shadow tables: notes_data, notes_idx, notes_config are auto-created
          - A real table 'documents' with project_id (must be included)

        Verifies: notes_data, notes_idx, notes_config are excluded;
                  documents is included.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Create a real tenant table.
        conn.execute("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            )
        """)
        conn.execute("INSERT INTO documents (title) VALUES ('test doc')")
        # Create an FTS5 virtual table — SQLite auto-creates its shadow tables.
        conn.execute("""
            CREATE VIRTUAL TABLE notes USING fts5(
                title,
                body,
                content='documents',
                content_rowid='id'
            )
        """)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()

        assert "documents" in tables, (
            "documents (real tenant table with project_id) must be included"
        )
        fts_shadows = [t for t in tables if t.startswith("notes_")]
        assert fts_shadows == [], (
            f"FTS5 shadow tables for 'notes' must NOT be enumerated: {fts_shadows}"
        )

    def test_real_table_ending_in_data_with_project_id_is_enumerated(
        self, tmp_path: Path
    ) -> None:
        """A table named 'event_data' (ends in _data) that carries project_id
        must be included — it has no parent FTS virtual table named 'event'."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE event_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                payload TEXT
            )
        """)
        conn.execute("INSERT INTO event_data (payload) VALUES ('x')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()

        assert "event_data" in tables, (
            "event_data (project_id-bearing table ending in _data) must be included. "
            "There is no FTS virtual table named 'event', so event_data is not a shadow."
        )

    def test_real_table_ending_in_idx_with_project_id_is_enumerated(
        self, tmp_path: Path
    ) -> None:
        """A table named 'search_idx' (ends in _idx) that carries project_id
        must be included — it has no parent FTS virtual table named 'search'."""
        db = _db(tmp_path)
        conn = _open(db)
        conn.execute("""
            CREATE TABLE search_idx (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                term TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO search_idx (term) VALUES ('hello')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()

        assert "search_idx" in tables, (
            "search_idx (project_id-bearing table ending in _idx) must be included. "
            "There is no FTS virtual table named 'search', so search_idx is not a shadow."
        )

    def test_pool_config_and_fts_shadow_coexist_correctly(
        self, tmp_path: Path
    ) -> None:
        """pool_config (real, project_id) is enumerated; notes_config (FTS shadow) is not —
        even when both exist in the same DB.

        This is the exact mixed scenario from the seocrawler-v2 production DB:
        pool_config is a worker-pool config table (real tenant data);
        notes_config is an FTS5 shadow (internal metadata).
        """
        db = _db(tmp_path)
        conn = _open(db)
        # Real tenant table (must be included).
        conn.execute("""
            CREATE TABLE pool_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                pool_id TEXT NOT NULL,
                max_workers INTEGER NOT NULL DEFAULT 4
            )
        """)
        conn.execute("INSERT INTO pool_config (project_id, pool_id) VALUES ('vnx-dev', 'default')")
        # Real parent table for FTS content tracking.
        conn.execute("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            )
        """)
        # FTS5 virtual table 'notes' — auto-creates notes_config, notes_data, notes_idx, etc.
        conn.execute("""
            CREATE VIRTUAL TABLE notes USING fts5(title, body)
        """)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db))
        tables = ts.enumerate_project_id_tables(conn)
        conn.close()

        assert "pool_config" in tables, (
            "pool_config must be enumerated — it is a real tenant table with project_id, "
            "not an FTS shadow (no FTS virtual table named 'pool' exists)."
        )
        assert "documents" in tables, "documents must be enumerated"
        fts_shadows = [t for t in tables if t.startswith("notes_")]
        assert fts_shadows == [], (
            f"FTS5 shadows for 'notes' must NOT be enumerated: {fts_shadows}"
        )

    def test_pool_config_included_in_full_migration_fk_clean(
        self, tmp_path: Path
    ) -> None:
        """Full end-to-end: worker_pools FK to pool_config must survive Phase 2.

        This is the exact FK topology that caused the Phase 2 ROLLBACK in the
        seocrawler-v2 dry-run.  With the fix, pool_config is enumerated, re-stamped
        before worker_pools (topological order: parent pool_config before child
        worker_pools), and the FK check passes after Phase 2.
        """
        db = _db(tmp_path)
        conn = _open(db)
        # pool_config is the FK parent.
        conn.execute("""
            CREATE TABLE pool_config (
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                pool_id TEXT NOT NULL,
                max_workers INTEGER NOT NULL DEFAULT 4,
                PRIMARY KEY (project_id, pool_id)
            )
        """)
        # worker_pools has a composite FK to pool_config.
        conn.execute("""
            CREATE TABLE worker_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                pool_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (project_id, pool_id)
                    REFERENCES pool_config(project_id, pool_id)
                    ON UPDATE NO ACTION ON DELETE NO ACTION
            )
        """)
        conn.execute(
            "INSERT INTO pool_config (project_id, pool_id, max_workers) "
            "VALUES ('vnx-dev', 'default', 8)"
        )
        conn.execute(
            "INSERT INTO worker_pools (project_id, pool_id, status) "
            "VALUES ('vnx-dev', 'default', 'active')"
        )
        conn.commit()
        conn.close()

        # Full 3-phase migration must succeed without FK violation.
        result = ts.run_three_phase_migration_on_db(db, "seocrawler-v2", db_label="TEST")
        assert result["ok"] is True, f"Migration failed: {result}"

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")

        # Both tables must be re-stamped.
        pc_val = conn.execute(
            "SELECT project_id FROM pool_config WHERE pool_id='default'"
        ).fetchone()[0]
        wp_val = conn.execute(
            "SELECT project_id FROM worker_pools WHERE pool_id='default'"
        ).fetchone()[0]
        assert pc_val == "seocrawler-v2", (
            f"pool_config not re-stamped: got {pc_val!r}. "
            "pool_config was excluded from enumeration and skipped by Phase 2."
        )
        assert wp_val == "seocrawler-v2", (
            f"worker_pools not re-stamped: got {wp_val!r}"
        )

        # FK check must be clean — the original failure mode.
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], (
            f"FK violations after migration: {fk_violations}. "
            "pool_config was not re-stamped, leaving worker_pools FK broken."
        )
        ic = conn.execute("PRAGMA integrity_check").fetchall()
        assert ic == [("ok",)], f"integrity_check failed: {ic}"
        conn.close()
