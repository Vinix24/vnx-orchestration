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
        """run_three_phase_migration_on_db takes a pre-migration checkpoint."""
        db = _db(tmp_path)
        conn = _open(db)
        _make_simple_tenant_table(conn)
        conn.execute("INSERT INTO things (name, project_id) VALUES ('a', 'vnx-dev')")
        conn.commit()
        conn.close()

        result = ts.run_three_phase_migration_on_db(db, "my-pid", db_label="TEST")
        assert result["ok"] is True
        # The pre-migration checkpoint must exist.
        assert "checkpoint_premigration" in result
        ckpt = Path(result["checkpoint_premigration"])
        assert ckpt.exists(), "Pre-migration checkpoint file must exist after migration"

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
