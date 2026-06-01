#!/usr/bin/env python3
"""vnx_structural_doctor.py — diagnose and repair structural divergence in runtime_coordination.db.

Modes:
  DIAGNOSE (always, read-only): report user_version, track table presence,
    dispatches schema, and a divergence verdict.
  DRY-RUN  (DEFAULT, no --apply): copy the live DB to a temp file, run the
    repair against the COPY, report results. NEVER touches the live DB.
  --apply: write a timestamped backup, then repair the live DB inside a
    single transaction. Re-run integrity_check + rowcount assertion AFTER;
    rollback + exit non-zero if either fails.

The repair (idempotent, additive-only):
  a. Create the 4 missing track tables EXACTLY per 0024's final tenant-scoped
     definitions (composite PK/UNIQUE over project_id per ADR-007).
  b. Additively add output_ref TEXT and output_kind TEXT to dispatches
     (if absent). Backfill output_ref=pr_ref and output_kind='pr' WHERE
     pr_ref IS NOT NULL. Do NOT drop or rename pr_ref.
  c. Do NOT change user_version (already 26).
  d. No seed data — schema repair only.

Safety rules:
  - Default = dry-run on a copy. --apply backs up first, transactional + integrity-asserted.
  - Additive only. No DROP/DELETE on existing data.
  - Do not touch pr_ref or user_version.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root


# ---------------------------------------------------------------------------
# 0024 final tenant-scoped DDL — exact CREATE TABLE + CREATE INDEX statements
# ---------------------------------------------------------------------------

TRACKS_V24_DDL = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id                    TEXT    NOT NULL,
    project_id                  TEXT    NOT NULL DEFAULT 'vnx-dev',
    title                       TEXT    NOT NULL,
    goal_state                  TEXT,
    phase                       TEXT    NOT NULL DEFAULT 'queued'
                                        CHECK (phase IN ('queued','active','parked','done')),
    next_up                     INTEGER NOT NULL DEFAULT 0,
    sort_order                  INTEGER NOT NULL DEFAULT 0,
    priority                    TEXT    DEFAULT 'medium',
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template        TEXT,
    context_composer_rules      TEXT    DEFAULT '{}',
    pr_ref                      TEXT,
    trigger_condition           TEXT,
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    phase_changed_at            TEXT,
    completed_at                TEXT,
    metadata_json               TEXT    DEFAULT '{}',
    PRIMARY KEY (track_id, project_id)
)
"""

TRACK_PHASE_HISTORY_V24_DDL = """
CREATE TABLE IF NOT EXISTS track_phase_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    from_phase  TEXT,
    to_phase    TEXT    NOT NULL,
    actor       TEXT    NOT NULL CHECK (actor IN ('operator','T0','system')),
    reason      TEXT,
    approval_id TEXT,
    occurred_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (track_id, project_id) REFERENCES tracks(track_id, project_id),
    UNIQUE (track_id, project_id, occurred_at)
)
"""

TRACK_DEPENDENCIES_V24_DDL = """
CREATE TABLE IF NOT EXISTS track_dependencies (
    from_track_id       TEXT    NOT NULL,
    from_project_id     TEXT    NOT NULL DEFAULT 'vnx-dev',
    to_track_id         TEXT    NOT NULL,
    to_project_id       TEXT    NOT NULL DEFAULT 'vnx-dev',
    kind                TEXT    NOT NULL CHECK (kind IN ('hard','soft','overlap')),
    derivation_source   TEXT    NOT NULL
                                CHECK (derivation_source IN (
                                    'manual', 'git_ancestry', 'path_overlap', 'oi_ref', 'pr_ref'
                                )),
    confidence          REAL    NOT NULL DEFAULT 1.0,
    evidence_json       TEXT    DEFAULT '{}',
    derived_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id),
    FOREIGN KEY (from_track_id, from_project_id) REFERENCES tracks(track_id, project_id),
    FOREIGN KEY (to_track_id, to_project_id) REFERENCES tracks(track_id, project_id)
)
"""

TRACK_OPEN_ITEMS_V24_DDL = """
CREATE TABLE IF NOT EXISTS track_open_items (
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    oi_id       TEXT    NOT NULL,
    link_type   TEXT    NOT NULL CHECK (link_type IN ('blocks','warns','related')),
    link_source TEXT    NOT NULL CHECK (link_source IN ('file_path','mention','manual')),
    linked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (track_id, project_id, oi_id, link_type),
    FOREIGN KEY (track_id, project_id) REFERENCES tracks(track_id, project_id)
)
"""

TRACK_INDEXES_V24 = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_project_phase_nextup ON tracks(project_id, phase, next_up DESC, sort_order)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up_per_project ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued'",
    "CREATE INDEX IF NOT EXISTS idx_track_deps_from ON track_dependencies(from_track_id, from_project_id)",
    "CREATE INDEX IF NOT EXISTS idx_track_phase_history_track ON track_phase_history(track_id, project_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_track_open_items_oi ON track_open_items(oi_id)",
]

TRACK_TABLE_NAMES = [
    "tracks",
    "track_phase_history",
    "track_dependencies",
    "track_open_items",
]

TRACK_PRE_V24_TABLES = [
    "tracks_pre_v24",
    "track_phase_history_pre_v24",
    "track_dependencies_pre_v24",
    "track_open_items_pre_v24",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    """Return current UTC timestamp in compact form for backup filenames."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _get_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return any(row[1] == column_name for row in cols)


def _rowcount(conn: sqlite3.Connection, table_name: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def _integrity_check(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# DIAGNOSE (always, read-only)
# ---------------------------------------------------------------------------


def diagnose(conn: sqlite3.Connection, label: str = "live") -> dict:
    """Report structural state of the coordination DB. Read-only."""
    result: dict = {
        "label": label,
        "user_version": _get_user_version(conn),
        "dispatches_rowcount": _rowcount(conn, "dispatches"),
        "track_tables": {},
        "track_pre_v24_tables": {},
        "dispatches_columns": {},
        "divergence": None,
    }

    for t in TRACK_TABLE_NAMES:
        result["track_tables"][t] = _table_exists(conn, t)

    for t in TRACK_PRE_V24_TABLES:
        result["track_pre_v24_tables"][t] = _table_exists(conn, t)

    for col in ["track", "pr_ref", "output_ref", "output_kind"]:
        result["dispatches_columns"][col] = _column_exists(conn, "dispatches", col)

    missing_tracks = [t for t, exists in result["track_tables"].items() if not exists]
    missing_cols = [c for c, exists in result["dispatches_columns"].items() if not exists]
    pre_v24_remnants = [t for t, exists in result["track_pre_v24_tables"].items() if exists]

    if missing_tracks:
        result["divergence"] = {
            "verdict": "DIVERGENT",
            "detail": (
                f"user_version={result['user_version']} but {len(missing_tracks)} "
                f"track table(s) absent: {missing_tracks}. The standard migration "
                f"path skips at this version; manual repair required."
            ),
            "missing_track_tables": missing_tracks,
        }
    elif missing_cols and any(c in ("output_ref", "output_kind") for c in missing_cols):
        result["divergence"] = {
            "verdict": "DIVERGENT",
            "detail": (
                f"Track tables present but missing dispatches column(s): {missing_cols}."
            ),
            "missing_dispatches_columns": missing_cols,
        }
    elif pre_v24_remnants:
        result["divergence"] = {
            "verdict": "PARTIAL",
            "detail": (
                f"Pre-v24 table remnants found: {pre_v24_remnants}. "
                f"Migration 0024 likely did not complete cleanup."
            ),
            "pre_v24_remnants": pre_v24_remnants,
        }
    else:
        result["divergence"] = {
            "verdict": "CLEAN",
            "detail": "All track tables and dispatches columns present and consistent.",
        }

    return result


def print_diagnosis(result: dict, file=None) -> None:
    """Pretty-print a diagnosis result dict."""
    out = file or sys.stdout
    label = result["label"]

    print(f"\n{'='*60}", file=out)
    print(f"  STRUCTURAL DIAGNOSIS — {label.upper()}", file=out)
    print(f"{'='*60}", file=out)
    print(f"  user_version          : {result['user_version']}", file=out)
    print(f"  dispatches rowcount   : {result['dispatches_rowcount']}", file=out)
    print(file=out)
    print("  Track tables:", file=out)
    for t, exists in result["track_tables"].items():
        status = "PRESENT" if exists else "ABSENT"
        print(f"    {t:<30} {status}", file=out)
    print(file=out)
    print("  Pre-v24 remnants:", file=out)
    for t, exists in result["track_pre_v24_tables"].items():
        if exists:
            print(f"    {t:<30} PRESENT (remnant)", file=out)
    if not any(result["track_pre_v24_tables"].values()):
        print("    (none)", file=out)
    print(file=out)
    print("  dispatches columns:", file=out)
    for col, exists in result["dispatches_columns"].items():
        status = "PRESENT" if exists else "ABSENT"
        print(f"    {col:<20} {status}", file=out)
    print(file=out)
    d = result["divergence"]
    print(f"  VERDICT: {d['verdict']}", file=out)
    print(f"  {d['detail']}", file=out)
    print(f"{'='*60}\n", file=out)


# ---------------------------------------------------------------------------
# REPAIR (applied to a connection — caller decides live vs copy)
# ---------------------------------------------------------------------------


def apply_repair(conn: sqlite3.Connection) -> dict:
    """Apply additive-only repair to the given connection.

    Returns a dict with what was done.
    """
    report: dict = {
        "tables_created": [],
        "tables_already_exist": [],
        "columns_added": [],
        "columns_already_exist": [],
        "indexes_created": [],
        "output_ref_backfilled": 0,
        "errors": [],
    }

    # --- a. Create missing track tables ---
    table_ddls = {
        "tracks": TRACKS_V24_DDL,
        "track_phase_history": TRACK_PHASE_HISTORY_V24_DDL,
        "track_dependencies": TRACK_DEPENDENCIES_V24_DDL,
        "track_open_items": TRACK_OPEN_ITEMS_V24_DDL,
    }

    for table_name, ddl in table_ddls.items():
        if _table_exists(conn, table_name):
            report["tables_already_exist"].append(table_name)
        else:
            conn.executescript(ddl)
            report["tables_created"].append(table_name)

    # Create indexes (IF NOT EXISTS makes these idempotent)
    for idx_sql in TRACK_INDEXES_V24:
        try:
            conn.execute(idx_sql)
            report["indexes_created"].append(idx_sql)
        except sqlite3.OperationalError as e:
            # Index already exists with same name — not an error
            if "already exists" not in str(e).lower():
                report["errors"].append(f"Index error: {e} — {idx_sql[:80]}")

    # --- b. Add output_ref and output_kind columns to dispatches ---
    for col_name, col_type in [("output_ref", "TEXT"), ("output_kind", "TEXT")]:
        if _column_exists(conn, "dispatches", col_name):
            report["columns_already_exist"].append(col_name)
        else:
            conn.execute(f"ALTER TABLE dispatches ADD COLUMN {col_name} {col_type}")
            report["columns_added"].append(col_name)

    # Backfill output_ref = pr_ref, output_kind = 'pr' WHERE pr_ref IS NOT NULL
    backfill_count = conn.execute(
        "UPDATE dispatches SET output_ref = pr_ref, output_kind = 'pr' "
        "WHERE pr_ref IS NOT NULL AND output_ref IS NULL"
    ).rowcount
    report["output_ref_backfilled"] = backfill_count

    return report


def print_repair_report(report: dict, file=None) -> None:
    """Pretty-print a repair report dict."""
    out = file or sys.stdout
    print(f"\n{'='*60}", file=out)
    print(f"  REPAIR REPORT", file=out)
    print(f"{'='*60}", file=out)

    if report["tables_created"]:
        print(f"  Tables created  : {report['tables_created']}", file=out)
    if report["tables_already_exist"]:
        print(f"  Tables existing : {report['tables_already_exist']}", file=out)
    if report["columns_added"]:
        print(f"  Columns added   : {report['columns_added']}", file=out)
    if report["columns_already_exist"]:
        print(f"  Columns existing: {report['columns_already_exist']}", file=out)
    if report["indexes_created"]:
        print(f"  Indexes created : {len(report['indexes_created'])}", file=out)
    if report["output_ref_backfilled"]:
        print(f"  output_ref backfilled: {report['output_ref_backfilled']} row(s)", file=out)
    if report["errors"]:
        print(f"  ERRORS:", file=out)
        for e in report["errors"]:
            print(f"    - {e}", file=out)

    if not any([
        report["tables_created"], report["columns_added"],
        report["indexes_created"], report["errors"],
        report["output_ref_backfilled"],
    ]):
        print(f"  No changes needed — schema already consistent.", file=out)

    print(f"{'='*60}\n", file=out)


# ---------------------------------------------------------------------------
# DRY-RUN (default) — repair against a TEMP COPY, never touch the live DB
# ---------------------------------------------------------------------------


def dry_run(db_path: Path) -> None:
    """Copy the live DB to a temp file, repair the copy, report results."""
    print(f"\n  DRY-RUN MODE — operating on a temp copy of: {db_path}")

    # Diagnose the live DB first
    src_conn = sqlite3.connect(str(db_path), timeout=30.0)
    src_conn.execute("PRAGMA query_only = ON")
    try:
        live_diag = diagnose(src_conn, label="live")
    finally:
        src_conn.close()

    print_diagnosis(live_diag)

    if live_diag["divergence"]["verdict"] == "CLEAN":
        print("  No divergence detected. Dry-run is a no-op.")
        return

    # Copy to temp file
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="vnx_doctor_dryrun_")
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)

    try:
        shutil.copy2(str(db_path), str(tmp_path))

        # Repair the copy
        tmp_conn = sqlite3.connect(str(tmp_path), timeout=30.0)
        tmp_conn.execute("PRAGMA journal_mode = WAL")
        tmp_conn.execute("PRAGMA foreign_keys = ON")

        try:
            disp_before = _rowcount(tmp_conn, "dispatches")
            report = apply_repair(tmp_conn)
            tmp_conn.commit()
            disp_after = _rowcount(tmp_conn, "dispatches")

            # Integrity check on the copy
            integrity = _integrity_check(tmp_conn)
            integrity_ok = integrity == ["ok"]

            # Re-diagnose after repair
            post_diag = diagnose(tmp_conn, label="copy (after repair)")
        finally:
            tmp_conn.close()

        # --- Print results ---
        print_repair_report(report)

        print(f"  Rowcount assertion:")
        print(f"    dispatches before: {disp_before}")
        print(f"    dispatches after : {disp_after}")
        if disp_before == disp_after:
            print(f"    [ok] rowcount preserved ({disp_before})")
        else:
            print(f"    [!] ROWCOUNT MISMATCH — would not apply to live DB")

        print(f"\n  Integrity check (copy): {'[ok]' if integrity_ok else '[!] FAILED'}")
        if not integrity_ok:
            for line in integrity:
                print(f"    {line}")

        print_diagnosis(post_diag)

        if disp_before == disp_after and integrity_ok:
            print("  Dry-run successful. Run with --apply to repair the live DB.")
        else:
            print("  [!] Dry-run found issues. --apply blocked until resolved.")
            sys.exit(1)

    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# --apply — backup first, repair live DB in a single transaction
# ---------------------------------------------------------------------------


def apply_to_live(db_path: Path) -> None:
    """Backup, then repair the live DB transactionally with integrity assertion."""
    print(f"\n  --APPLY MODE — repairing live DB: {db_path}")

    # 1. Diagnose live
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA query_only = ON")
    try:
        live_diag = diagnose(conn, label="live (before)")
    finally:
        conn.close()

    print_diagnosis(live_diag)

    if live_diag["divergence"]["verdict"] == "CLEAN":
        print("  No divergence detected. --apply is a no-op.")
        return

    # 2. Write timestamped backup
    stamp = _utc_stamp()
    backup_path = db_path.with_suffix(f".db.bak-{stamp}")
    print(f"  Writing backup: {backup_path}")
    shutil.copy2(str(db_path), str(backup_path))
    print(f"  Backup written ({backup_path.stat().st_size} bytes)")

    # 3. Repair in a single transaction
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        disp_before = _rowcount(conn, "dispatches")
        report = apply_repair(conn)
        conn.commit()
        disp_after = _rowcount(conn, "dispatches")

        # Post-repair assertions
        integrity = _integrity_check(conn)
        integrity_ok = integrity == ["ok"]

        post_diag = diagnose(conn, label="live (after)")
    except Exception:
        conn.rollback()
        conn.close()
        print(f"\n  [ERROR] Repair failed — transaction rolled back.", file=sys.stderr)
        raise
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

    print_repair_report(report)

    # Rowcount assertion
    print(f"  Rowcount assertion:")
    print(f"    dispatches before: {disp_before}")
    print(f"    dispatches after : {disp_after}")

    # Integrity assertion
    print(f"\n  Integrity check (live): {'[ok]' if integrity_ok else '[!] FAILED'}")
    if not integrity_ok:
        for line in integrity:
            print(f"    {line}")

    print_diagnosis(post_diag)

    # Hard fail on assertion violation
    if disp_before != disp_after:
        print(
            f"\n  [FATAL] dispatches rowcount mismatch: {disp_before} → {disp_after}. "
            f"Transaction committed — manual investigation required.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not integrity_ok:
        print(
            f"\n  [FATAL] integrity_check failed after repair. "
            f"Backup available at: {backup_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n  Repair complete. Backup: {backup_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Structural doctor for runtime_coordination.db — repair v26/absent-tracks divergence.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply repair to the LIVE database (default: dry-run on a temp copy).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to runtime_coordination.db (default: autodetect from project root).",
    )
    args = parser.parse_args()

    if args.db:
        db_path = args.db.resolve()
    else:
        project_root = resolve_project_root(__file__)
        db_path = project_root / ".vnx-data" / "state" / "runtime_coordination.db"

    if not db_path.exists():
        print(f"  [ERROR] Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    if args.apply:
        apply_to_live(db_path)
    else:
        dry_run(db_path)


if __name__ == "__main__":
    main()
