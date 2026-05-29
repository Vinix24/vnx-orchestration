"""Regression test for Issue #687 — 0022 dispatches sqlite_sequence preservation."""
import sqlite3
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
import schema_migration


def _apply_v21(conn):
    """Bring DB to v21 (pre-22 schema) with all columns the 0022 INSERT...SELECT references."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dispatches (
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
        );
    """)
    conn.execute("PRAGMA user_version = 21")
    conn.commit()


def test_0022_preserves_dispatches_seq_high_water():
    """0022 must not regress sqlite_sequence when rows were deleted."""
    conn = sqlite3.connect(":memory:")
    _apply_v21(conn)

    # Seed: id=1 active, id=100 deleted → seq=100 (high-water)
    conn.execute("INSERT INTO dispatches (id, dispatch_id) VALUES (1, 'd-001')")
    conn.execute("INSERT INTO dispatches (id, dispatch_id) VALUES (100, 'd-100')")
    conn.commit()
    conn.execute("DELETE FROM dispatches WHERE id = 100")
    conn.commit()

    seq_before = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='dispatches'"
    ).fetchone()
    assert seq_before is not None
    assert seq_before[0] == 100, f"Expected pre-migration seq=100, got {seq_before[0]}"

    # Apply 0022
    sql = (_REPO_ROOT / "schemas" / "migrations" / "0022_track_layer.sql").read_text()
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()

    # Post-migration: seq should NOT have regressed
    seq_after = conn.execute(
        "SELECT MAX(seq) FROM sqlite_sequence WHERE name='dispatches'"
    ).fetchone()[0]
    assert seq_after is not None
    assert seq_after >= 100, (
        f"sqlite_sequence regressed: was 100, now {seq_after}. "
        "Fix #687 should preserve high-water mark via DELETE+INSERT pattern."
    )

    # Insert new dispatch — id must be 101+
    conn.execute("INSERT INTO dispatches (dispatch_id) VALUES ('d-new')")
    conn.commit()
    new_id = conn.execute(
        "SELECT id FROM dispatches WHERE dispatch_id='d-new'"
    ).fetchone()[0]
    assert new_id >= 101, f"New dispatch id={new_id} — seq not preserved"


def test_0022_no_duplicate_sqlite_sequence_rows():
    """Verify DELETE+INSERT pattern doesn't leave duplicate rows."""
    conn = sqlite3.connect(":memory:")
    _apply_v21(conn)

    conn.execute("INSERT INTO dispatches (dispatch_id) VALUES ('d-1')")
    conn.commit()

    sql = (_REPO_ROOT / "schemas" / "migrations" / "0022_track_layer.sql").read_text()
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()

    rows = list(conn.execute("SELECT * FROM sqlite_sequence WHERE name='dispatches'"))
    assert len(rows) == 1, f"Expected single sqlite_sequence row for dispatches, got {len(rows)}"
