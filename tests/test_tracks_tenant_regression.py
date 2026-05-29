"""tests/test_tracks_tenant_regression.py — ADR-007 tenant-isolation regression safety.

ADR-007 binding: all track child tables must carry composite UNIQUE/PK over project_id
and composite FK to tracks(track_id, project_id).

Tests:
1. Structural invariants hold on current schema for track_phase_history,
   track_dependencies, and track_open_items.  These tests FAIL if a future
   migration drops or weakens any composite key — catching regressions before merge.
2. Regression-detection proof: invariant checker finds violations in known-bad schemas.
3. track_phase_history dedupe helper makes v22 duplicate timestamps distinct before
   the v24 UNIQUE(track_id, project_id, occurred_at) constraint is applied.
4. DB-level UNIQUE(track_id, project_id, occurred_at) rejects direct duplicate inserts,
   proving the constraint is enforced at the database layer, not only in Python.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SCRIPTS = _PROJECT_ROOT / "scripts"
_MIGRATIONS = _PROJECT_ROOT / "schemas" / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
from migrate_future_system import _dedupe_v22_phase_history_timestamps


# ---------------------------------------------------------------------------
# DB builders
# ---------------------------------------------------------------------------

def _base_conn() -> sqlite3.Connection:
    """Minimal in-memory coordination DB: dispatches + coordination_events only."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT,
            entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()
    return conn


def _v24_conn() -> sqlite3.Connection:
    """In-memory DB with migrations 0022 and 0024 applied."""
    conn = _base_conn()
    for ver, fname in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")]:
        sql = (_MIGRATIONS / fname).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, ver, sql)
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# ADR-007 invariant checker
# ---------------------------------------------------------------------------

def _pk_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names that form the PRIMARY KEY, in PK order."""
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    pairs = [(r[5], r[1]) for r in rows if r[5] > 0]
    pairs.sort()
    return [name for _, name in pairs]


def _unique_col_sets(conn: sqlite3.Connection, table: str) -> list[frozenset]:
    """Return one frozenset of column names per UNIQUE index on *table*."""
    indexes = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
    result = []
    for idx in indexes:
        if idx[2] == 1:  # unique flag
            cols = frozenset(
                r[2] for r in conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()
            )
            result.append(cols)
    return result


def _fk_groups(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return one dict per FK constraint: {to_table, from_cols (set), to_cols (set)}."""
    rows = conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
    groups: dict[int, dict] = {}
    for row in rows:
        fk_id = row[0]
        if fk_id not in groups:
            groups[fk_id] = {"to_table": row[2], "from_cols": set(), "to_cols": set()}
        groups[fk_id]["from_cols"].add(row[3])
        groups[fk_id]["to_cols"].add(row[4])
    return list(groups.values())


def _collect_adr007_violations(conn: sqlite3.Connection) -> list[str]:
    """Return ADR-007 violation strings for all track child tables.

    Returns an empty list when all invariants hold.  Each violation names
    the table and the missing constraint so regressions are immediately
    actionable.

    A future migration that drops project_id from a composite key will cause
    structural tests that call this helper to FAIL, catching the regression
    before merge.
    """
    violations: list[str] = []

    # ---- track_phase_history ----
    # ADR-007: UNIQUE(track_id, project_id, occurred_at) — composite tenant constraint
    tph_unique = _unique_col_sets(conn, "track_phase_history")
    required_tph_unique = frozenset({"track_id", "project_id", "occurred_at"})
    if not any(required_tph_unique.issubset(s) for s in tph_unique):
        violations.append(
            "track_phase_history: missing UNIQUE constraint covering "
            "(track_id, project_id, occurred_at) [ADR-007 composite tenant constraint]"
        )

    # ADR-007: composite FK (track_id, project_id) → tracks(track_id, project_id)
    tph_fks = _fk_groups(conn, "track_phase_history")
    if not any(
        g["to_table"] == "tracks"
        and {"track_id", "project_id"}.issubset(g["from_cols"])
        for g in tph_fks
    ):
        violations.append(
            "track_phase_history: missing composite FK "
            "(track_id, project_id) → tracks [ADR-007 tenant FK]"
        )

    # ---- track_dependencies ----
    # ADR-007: composite PK (from_track_id, from_project_id, to_track_id, to_project_id)
    td_pk = frozenset(_pk_cols(conn, "track_dependencies"))
    required_td_pk = frozenset(
        {"from_track_id", "from_project_id", "to_track_id", "to_project_id"}
    )
    missing_td = required_td_pk - td_pk
    if missing_td:
        violations.append(
            f"track_dependencies: composite PK missing {missing_td} "
            "[ADR-007 requires (from_track_id, from_project_id, to_track_id, to_project_id)]"
        )

    # ADR-007: FK (from_track_id, from_project_id) → tracks
    td_fks = _fk_groups(conn, "track_dependencies")
    if not any(
        g["to_table"] == "tracks"
        and {"from_track_id", "from_project_id"}.issubset(g["from_cols"])
        for g in td_fks
    ):
        violations.append(
            "track_dependencies: missing composite FK "
            "(from_track_id, from_project_id) → tracks [ADR-007]"
        )

    # ADR-007: FK (to_track_id, to_project_id) → tracks
    if not any(
        g["to_table"] == "tracks"
        and {"to_track_id", "to_project_id"}.issubset(g["from_cols"])
        for g in td_fks
    ):
        violations.append(
            "track_dependencies: missing composite FK "
            "(to_track_id, to_project_id) → tracks [ADR-007]"
        )

    # ---- track_open_items ----
    # ADR-007: composite PK (track_id, project_id, oi_id, link_type)
    toi_pk = frozenset(_pk_cols(conn, "track_open_items"))
    required_toi_pk = frozenset({"track_id", "project_id", "oi_id", "link_type"})
    missing_toi = required_toi_pk - toi_pk
    if missing_toi:
        violations.append(
            f"track_open_items: composite PK missing {missing_toi} "
            "[ADR-007 requires (track_id, project_id, oi_id, link_type)]"
        )

    # ADR-007: composite FK (track_id, project_id) → tracks
    toi_fks = _fk_groups(conn, "track_open_items")
    if not any(
        g["to_table"] == "tracks"
        and {"track_id", "project_id"}.issubset(g["from_cols"])
        for g in toi_fks
    ):
        violations.append(
            "track_open_items: missing composite FK "
            "(track_id, project_id) → tracks [ADR-007]"
        )

    return violations


# ---------------------------------------------------------------------------
# Test 1: ADR-007 structural invariants hold on current schema
# ---------------------------------------------------------------------------

class TestADR007StructuralInvariants:
    """Encodes ADR-007 as executable assertions against the live schema.

    Each test will FAIL if a future migration removes or weakens a composite
    key on any track child table, catching tenant-isolation regressions before
    merge.  Cite ADR-007-multitenant-project-id-stamping.md on failure.
    """

    def test_no_violations_on_current_schema(self):
        """Omnibus: zero ADR-007 violations across all three child tables."""
        conn = _v24_conn()
        violations = _collect_adr007_violations(conn)
        assert violations == [], (
            "ADR-007 tenant-isolation violations found.  A migration may have "
            "weakened a composite constraint:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    # track_phase_history
    def test_track_phase_history_unique_covers_project_id(self):
        conn = _v24_conn()
        unique_sets = _unique_col_sets(conn, "track_phase_history")
        required = frozenset({"track_id", "project_id", "occurred_at"})
        assert any(required.issubset(s) for s in unique_sets), (
            "ADR-007: track_phase_history must have UNIQUE(track_id, project_id, "
            f"occurred_at).  Actual unique index column sets: {unique_sets}"
        )

    def test_track_phase_history_fk_is_composite(self):
        conn = _v24_conn()
        fks = _fk_groups(conn, "track_phase_history")
        assert any(
            g["to_table"] == "tracks"
            and {"track_id", "project_id"}.issubset(g["from_cols"])
            for g in fks
        ), (
            "ADR-007: track_phase_history must have composite FK "
            f"(track_id, project_id) → tracks.  Actual FKs: {fks}"
        )

    # track_dependencies
    def test_track_dependencies_pk_covers_project_ids(self):
        conn = _v24_conn()
        pk = frozenset(_pk_cols(conn, "track_dependencies"))
        required = frozenset(
            {"from_track_id", "from_project_id", "to_track_id", "to_project_id"}
        )
        assert required.issubset(pk), (
            f"ADR-007: track_dependencies PK must include {required}.  Actual: {pk}"
        )

    def test_track_dependencies_from_fk_is_composite(self):
        conn = _v24_conn()
        fks = _fk_groups(conn, "track_dependencies")
        assert any(
            g["to_table"] == "tracks"
            and {"from_track_id", "from_project_id"}.issubset(g["from_cols"])
            for g in fks
        ), (
            "ADR-007: track_dependencies must have composite FK "
            f"(from_track_id, from_project_id) → tracks.  Actual FKs: {fks}"
        )

    def test_track_dependencies_to_fk_is_composite(self):
        conn = _v24_conn()
        fks = _fk_groups(conn, "track_dependencies")
        assert any(
            g["to_table"] == "tracks"
            and {"to_track_id", "to_project_id"}.issubset(g["from_cols"])
            for g in fks
        ), (
            "ADR-007: track_dependencies must have composite FK "
            f"(to_track_id, to_project_id) → tracks.  Actual FKs: {fks}"
        )

    # track_open_items
    def test_track_open_items_pk_covers_project_id(self):
        conn = _v24_conn()
        pk = frozenset(_pk_cols(conn, "track_open_items"))
        required = frozenset({"track_id", "project_id", "oi_id", "link_type"})
        assert required.issubset(pk), (
            f"ADR-007: track_open_items PK must include {required}.  Actual: {pk}"
        )

    def test_track_open_items_fk_is_composite(self):
        conn = _v24_conn()
        fks = _fk_groups(conn, "track_open_items")
        assert any(
            g["to_table"] == "tracks"
            and {"track_id", "project_id"}.issubset(g["from_cols"])
            for g in fks
        ), (
            "ADR-007: track_open_items must have composite FK "
            f"(track_id, project_id) → tracks.  Actual FKs: {fks}"
        )


# ---------------------------------------------------------------------------
# Test 2: regression-detection proof
# ---------------------------------------------------------------------------

def _tracks_table_ddl() -> str:
    return """
        CREATE TABLE tracks (
            track_id   TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            title      TEXT NOT NULL,
            goal_state TEXT,
            phase      TEXT NOT NULL DEFAULT 'queued',
            PRIMARY KEY (track_id, project_id)
        )
    """


def _td_full_ddl() -> str:
    return """
        CREATE TABLE track_dependencies (
            from_track_id   TEXT NOT NULL,
            from_project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            to_track_id     TEXT NOT NULL,
            to_project_id   TEXT NOT NULL DEFAULT 'vnx-dev',
            kind            TEXT NOT NULL,
            derivation_source TEXT NOT NULL,
            confidence      REAL NOT NULL DEFAULT 1.0,
            evidence_json   TEXT,
            derived_at      TEXT NOT NULL,
            PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id),
            FOREIGN KEY (from_track_id, from_project_id)
                REFERENCES tracks(track_id, project_id),
            FOREIGN KEY (to_track_id, to_project_id)
                REFERENCES tracks(track_id, project_id)
        )
    """


def _toi_full_ddl() -> str:
    return """
        CREATE TABLE track_open_items (
            track_id    TEXT NOT NULL,
            project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
            oi_id       TEXT NOT NULL,
            link_type   TEXT NOT NULL,
            link_source TEXT NOT NULL,
            linked_at   TEXT NOT NULL,
            PRIMARY KEY (track_id, project_id, oi_id, link_type),
            FOREIGN KEY (track_id, project_id)
                REFERENCES tracks(track_id, project_id)
        )
    """


class TestADR007RegressionDetection:
    """Proves _collect_adr007_violations catches weakened schemas.

    Each test builds a deliberately regressed schema and asserts the checker
    returns at least one violation for the regressed table.  If the checker
    itself were wrong, these tests would fail — so they double-verify both the
    checker logic and the invariant definition.
    """

    def test_checker_detects_missing_project_id_in_tph_unique(self):
        """track_phase_history with UNIQUE(track_id, occurred_at) → violation found."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(_tracks_table_ddl())
        # Regressed: project_id absent from UNIQUE
        conn.execute("""
            CREATE TABLE track_phase_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id    TEXT NOT NULL,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
                from_phase  TEXT,
                to_phase    TEXT NOT NULL,
                actor       TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (track_id, project_id)
                    REFERENCES tracks(track_id, project_id),
                UNIQUE (track_id, occurred_at)
            )
        """)
        conn.execute(_td_full_ddl())
        conn.execute(_toi_full_ddl())
        conn.commit()

        violations = _collect_adr007_violations(conn)
        tph_violations = [v for v in violations if "track_phase_history" in v and "UNIQUE" in v]
        assert tph_violations, (
            "Checker must report a UNIQUE violation for track_phase_history missing "
            f"project_id.  All violations: {violations}"
        )

    def test_checker_detects_missing_project_id_in_tph_fk(self):
        """track_phase_history with FK only on track_id (not project_id) → violation found."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(_tracks_table_ddl())
        # Regressed: FK only on single column, no project_id
        conn.execute("""
            CREATE TABLE track_phase_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id    TEXT NOT NULL,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
                from_phase  TEXT,
                to_phase    TEXT NOT NULL,
                actor       TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (track_id)
                    REFERENCES tracks(track_id),
                UNIQUE (track_id, project_id, occurred_at)
            )
        """)
        conn.execute(_td_full_ddl())
        conn.execute(_toi_full_ddl())
        conn.commit()

        violations = _collect_adr007_violations(conn)
        tph_fk_violations = [
            v for v in violations if "track_phase_history" in v and "FK" in v
        ]
        assert tph_fk_violations, (
            "Checker must report an FK violation for track_phase_history with "
            f"single-column FK only.  All violations: {violations}"
        )

    def test_checker_detects_missing_project_id_in_toi_pk(self):
        """track_open_items with PK(track_id, oi_id, link_type) — missing project_id → violation."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(_tracks_table_ddl())
        conn.execute("""
            CREATE TABLE track_phase_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id    TEXT NOT NULL,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
                from_phase  TEXT,
                to_phase    TEXT NOT NULL,
                actor       TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (track_id, project_id)
                    REFERENCES tracks(track_id, project_id),
                UNIQUE (track_id, project_id, occurred_at)
            )
        """)
        conn.execute(_td_full_ddl())
        # Regressed: project_id absent from PK
        conn.execute("""
            CREATE TABLE track_open_items (
                track_id    TEXT NOT NULL,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
                oi_id       TEXT NOT NULL,
                link_type   TEXT NOT NULL,
                link_source TEXT NOT NULL,
                linked_at   TEXT NOT NULL,
                PRIMARY KEY (track_id, oi_id, link_type),
                FOREIGN KEY (track_id)
                    REFERENCES tracks(track_id)
            )
        """)
        conn.commit()

        violations = _collect_adr007_violations(conn)
        toi_violations = [v for v in violations if "track_open_items" in v and "PK" in v]
        assert toi_violations, (
            "Checker must report a PK violation for track_open_items missing "
            f"project_id.  All violations: {violations}"
        )

    def test_checker_detects_missing_project_id_in_td_pk(self):
        """track_dependencies with PK(from_track_id, to_track_id) — no project_ids → violation."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(_tracks_table_ddl())
        conn.execute("""
            CREATE TABLE track_phase_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id    TEXT NOT NULL,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
                from_phase  TEXT, to_phase TEXT NOT NULL, actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (track_id, project_id)
                    REFERENCES tracks(track_id, project_id),
                UNIQUE (track_id, project_id, occurred_at)
            )
        """)
        # Regressed: project_ids absent from PK
        conn.execute("""
            CREATE TABLE track_dependencies (
                from_track_id   TEXT NOT NULL,
                to_track_id     TEXT NOT NULL,
                kind            TEXT NOT NULL,
                derivation_source TEXT NOT NULL,
                confidence      REAL NOT NULL DEFAULT 1.0,
                evidence_json   TEXT,
                derived_at      TEXT NOT NULL,
                PRIMARY KEY (from_track_id, to_track_id),
                FOREIGN KEY (from_track_id) REFERENCES tracks(track_id),
                FOREIGN KEY (to_track_id) REFERENCES tracks(track_id)
            )
        """)
        conn.execute(_toi_full_ddl())
        conn.commit()

        violations = _collect_adr007_violations(conn)
        td_violations = [v for v in violations if "track_dependencies" in v]
        assert td_violations, (
            "Checker must report violations for track_dependencies missing "
            f"project_id columns in PK/FK.  All violations: {violations}"
        )


# ---------------------------------------------------------------------------
# Test 3 + 4: dedupe helper and DB-level UNIQUE constraint
# ---------------------------------------------------------------------------

class TestDedupeAndUniqueConstraint:
    """track_phase_history dedupe + UNIQUE constraint prove the current shape is correct.

    The dedupe helper (migrate_future_system._dedupe_v22_phase_history_timestamps)
    makes v22 timestamps unique before the v24 UNIQUE(track_id, project_id, occurred_at)
    is applied.  The DB-level UNIQUE then enforces the invariant on all future inserts.
    """

    def _v22_conn_with_dup_timestamps(self) -> sqlite3.Connection:
        conn = _base_conn()
        sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, 22, sql)
        conn.commit()

        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state)"
            " VALUES (?, ?, ?, ?)",
            ("track-01", "vnx-dev", "T", "G"),
        )
        ts = "2026-01-01T00:00:00.000Z"
        conn.execute(
            "INSERT INTO track_phase_history"
            " (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("track-01", "queued", "active", "operator", ts),
        )
        conn.execute(
            "INSERT INTO track_phase_history"
            " (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("track-01", "active", "parked", "operator", ts),
        )
        conn.commit()
        return conn

    def test_dedupe_makes_timestamps_distinct(self):
        """Dedupe helper appends a microsecond suffix so no two rows share occurred_at."""
        conn = self._v22_conn_with_dup_timestamps()
        _dedupe_v22_phase_history_timestamps(conn)

        rows = conn.execute(
            "SELECT occurred_at FROM track_phase_history ORDER BY id"
        ).fetchall()
        assert len(rows) == 2, f"Expected 2 history rows, got {len(rows)}"
        ts0, ts1 = rows[0][0], rows[1][0]
        assert ts0 != ts1, (
            f"Dedupe did not make timestamps distinct: both are {ts0!r}"
        )

    def test_dedupe_then_v24_migration_succeeds(self):
        """After dedupe, v24 UNIQUE(track_id, project_id, occurred_at) accepts both rows."""
        conn = self._v22_conn_with_dup_timestamps()
        _dedupe_v22_phase_history_timestamps(conn)
        conn.commit()

        sql = (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, 24, sql)
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0]
        assert count == 2, (
            f"Expected 2 history rows after dedupe + v24 migration, got {count}"
        )

    def test_v24_unique_rejects_duplicate_occurred_at_same_project(self):
        """DB-level UNIQUE(track_id, project_id, occurred_at) raises on a direct duplicate."""
        conn = _v24_conn()
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state)"
            " VALUES (?, ?, ?, ?)",
            ("track-01", "vnx-dev", "T", "G"),
        )
        ts = "2026-01-01T00:00:00.000Z"
        conn.execute(
            "INSERT INTO track_phase_history"
            " (track_id, project_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("track-01", "vnx-dev", "queued", "active", "operator", ts),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_phase_history"
                " (track_id, project_id, from_phase, to_phase, actor, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("track-01", "vnx-dev", "active", "parked", "operator", ts),
            )
            conn.commit()

    def test_v24_unique_allows_same_timestamp_different_project(self):
        """Same (track_id, occurred_at) from different projects must not conflict."""
        conn = _v24_conn()
        ts = "2026-01-01T00:00:00.000Z"
        for pid in ("vnx-dev", "seocrawler-v2"):
            conn.execute(
                "INSERT INTO tracks (track_id, project_id, title, goal_state)"
                " VALUES (?, ?, ?, ?)",
                ("track-01", pid, "T", "G"),
            )
        conn.commit()

        for pid in ("vnx-dev", "seocrawler-v2"):
            conn.execute(
                "INSERT INTO track_phase_history"
                " (track_id, project_id, from_phase, to_phase, actor, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("track-01", pid, "queued", "active", "operator", ts),
            )
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0]
        assert count == 2, (
            "Same occurred_at from two different projects must both be stored. "
            f"Got {count} rows."
        )
