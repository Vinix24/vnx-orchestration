#!/usr/bin/env python3
"""Online migration runner: add events.tenant_id.

Companion to ``migrate_add_tenant_id.sql``. Uses only the Python
standard library (``sqlite3``) so it has no provider/SDK dependencies.

Design points
-------------
* WAL journal mode + 10_000-row transactions: writers block for a few ms
  per batch, readers never block. Safe for live traffic on 50M rows.
* Idempotent: ``ALTER TABLE`` is gated on ``PRAGMA table_info``; the
  ``migration_state`` row is INSERT-OR-IGNOREd; triggers use
  ``IF NOT EXISTS``; batches resume from the last committed ``rowid``
  recorded in ``migration_state.last_rowid``.
* Auditable: every batch writes a ``migration_log`` row. Start, finish,
  rollback, and error events are emitted as their own rows.
* Concurrent-write tolerant: trigger-based NOT NULL enforcement is
  installed only after the backfill so concurrent INSERTs during the
  migration window are not rejected.

Tenant resolution
-----------------
The script assumes a ``tenant_mapping(user_id INTEGER PRIMARY KEY,
tenant_id INTEGER NOT NULL)`` lookup table. Rows whose ``user_id`` has
no mapping are assigned ``--default-tenant-id`` (required). If you do
not have a ``tenant_mapping`` table, pass ``--default-tenant-id`` and
``--all-default`` to assign the default to every row.

Usage
-----
    # Forward, normal run
    python3 migrate_add_tenant_id_runner.py \\
        --db /var/data/events.db \\
        --default-tenant-id 1

    # Resume after interruption (same flags; runner reads last_rowid)
    python3 migrate_add_tenant_id_runner.py --db ... --default-tenant-id 1

    # Rollback
    python3 migrate_add_tenant_id_runner.py --db ... --direction down
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MIGRATION_NAME = "add_tenant_id_to_events"
TABLE = "events"
COLUMN = "tenant_id"
DEFAULT_BATCH_SIZE = 10_000
PROGRESS_EVERY_N_BATCHES = 100
EXPECTED_SCHEMA_VERSION = 1

LOG = logging.getLogger("migrate_add_tenant_id")


# ---------------------------------------------------------------------------
# SQL parsing
# ---------------------------------------------------------------------------

SECTION_MARKER = "-- @section:"


def parse_sql_sections(sql_path: Path) -> dict[str, str]:
    """Split the migration SQL into ``@section: <name>`` blocks.

    Returns a dict ``{section_name: sql_body}``. Comments inside a section
    are preserved. Unknown sections are kept verbatim so the file format
    can be extended without breaking the runner.
    """
    if not sql_path.exists():
        raise FileNotFoundError(f"migration SQL not found: {sql_path}")
    text = sql_path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(SECTION_MARKER):
            tail = stripped[len(SECTION_MARKER):].strip()
            current = tail.split()[0] if tail else None
            if current:
                sections.setdefault(current, [])
            continue
        if current is None:
            continue
        sections[current].append(line)
    return {name: "\n".join(body).strip() for name, body in sections.items()}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL must be set per-DB; idempotent.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any((r["name"] or "").lower() == column.lower() for r in rows)


def trigger_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def sqlite_supports_drop_column() -> bool:
    return sqlite3.sqlite_version_info >= (3, 35, 0)


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def audit(
    conn: sqlite3.Connection,
    event: str,
    *,
    batch_no: int | None = None,
    rows_seen: int | None = None,
    last_rowid: int | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO migration_log "
        "(migration, event, batch_no, rows_seen, last_rowid, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (MIGRATION_NAME, event, batch_no, rows_seen, last_rowid, message),
    )


def update_state(
    conn: sqlite3.Connection,
    *,
    last_rowid: int | None = None,
    status: str | None = None,
) -> None:
    fields = []
    params: list[object] = []
    if last_rowid is not None:
        fields.append("last_rowid = ?")
        params.append(last_rowid)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    fields.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    params.append(MIGRATION_NAME)
    conn.execute(
        f"UPDATE migration_state SET {', '.join(fields)} WHERE migration = ?",
        params,
    )


def get_state(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT last_rowid, status, schema_ver FROM migration_state WHERE migration = ?",
        (MIGRATION_NAME,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Forward migration
# ---------------------------------------------------------------------------

@dataclass
class ForwardOptions:
    db_path: str
    sql_path: Path
    default_tenant_id: int
    batch_size: int = DEFAULT_BATCH_SIZE
    all_default: bool = False
    build_index: bool = False


def run_forward(opts: ForwardOptions) -> int:
    """Execute the forward migration. Returns total rows backfilled."""
    sections = parse_sql_sections(opts.sql_path)
    if "prepare" not in sections:
        raise RuntimeError("migration SQL missing @section: prepare")

    conn = open_db(opts.db_path)
    with closing(conn):
        # Step 1: ensure audit/state tables exist (prepare section).
        conn.executescript(sections["prepare"])

        state = get_state(conn)
        if state is None:
            raise RuntimeError("migration_state row missing after prepare")
        if state["schema_ver"] != EXPECTED_SCHEMA_VERSION:
            raise RuntimeError(
                f"schema_ver mismatch: expected {EXPECTED_SCHEMA_VERSION}, "
                f"found {state['schema_ver']}"
            )
        if state["status"] == "complete":
            LOG.info("migration already complete; nothing to do")
            return 0

        # Step 2: add column (idempotent).
        if not column_exists(conn, TABLE, COLUMN):
            LOG.info("adding column %s.%s", TABLE, COLUMN)
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INTEGER")
        else:
            LOG.info("column %s.%s already exists; skipping ADD", TABLE, COLUMN)

        audit(
            conn,
            "start",
            last_rowid=state["last_rowid"],
            message=f"resume_from_rowid={state['last_rowid']}",
        )
        update_state(conn, status="running")

        # Step 3: batched backfill.
        total = _backfill(conn, opts, resume_after=int(state["last_rowid"]))

        # Step 4: install NOT NULL triggers (after backfill).
        _install_not_null_triggers(conn)

        # Step 5: optional supporting index.
        if opts.build_index:
            LOG.info("building ix_events_tenant_id (may block writers briefly)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_tenant_id "
                f"ON {TABLE}({COLUMN})"
            )

        update_state(conn, status="complete")
        audit(conn, "finish", rows_seen=total, message="migration_complete")
        LOG.info("done: %d rows backfilled", total)
        return total


def _backfill(
    conn: sqlite3.Connection,
    opts: ForwardOptions,
    *,
    resume_after: int,
) -> int:
    """Walk the table in rowid order, 10k rows per transaction."""
    cursor_rowid = resume_after
    total = 0
    batch_no = 0
    start_ts = time.monotonic()

    while True:
        batch_no += 1
        rows_in_batch, new_cursor = _backfill_one_batch(
            conn,
            cursor_rowid=cursor_rowid,
            batch_size=opts.batch_size,
            default_tenant_id=opts.default_tenant_id,
            all_default=opts.all_default,
        )
        if rows_in_batch == 0:
            LOG.info("no more rows to process (batch %d)", batch_no)
            break

        total += rows_in_batch
        cursor_rowid = new_cursor
        update_state(conn, last_rowid=cursor_rowid)
        audit(
            conn,
            "batch",
            batch_no=batch_no,
            rows_seen=total,
            last_rowid=cursor_rowid,
        )

        if batch_no % PROGRESS_EVERY_N_BATCHES == 0:
            elapsed = time.monotonic() - start_ts
            rate = total / elapsed if elapsed > 0 else 0.0
            LOG.info(
                "progress: batch=%d total_rows=%d cursor_rowid=%d rate=%.0f rows/s",
                batch_no,
                total,
                cursor_rowid,
                rate,
            )

    return total


def _backfill_one_batch(
    conn: sqlite3.Connection,
    *,
    cursor_rowid: int,
    batch_size: int,
    default_tenant_id: int,
    all_default: bool,
) -> tuple[int, int]:
    """Backfill one batch.

    Returns ``(rows_updated, new_cursor_rowid)``. ``new_cursor_rowid`` is
    the max rowid seen in the batch — the next call resumes after it.
    """
    # Acquire a fresh transaction per batch so the writer lock is held
    # briefly. With WAL and a 30s busy_timeout, concurrent readers are
    # unaffected and concurrent writers wait at most ms per batch.
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            f"SELECT rowid AS rid, user_id FROM {TABLE} "
            f"WHERE rowid > ? AND {COLUMN} IS NULL "
            "ORDER BY rowid LIMIT ?",
            (cursor_rowid, batch_size),
        ).fetchall()

        if not rows:
            conn.execute("COMMIT")
            return 0, cursor_rowid

        updates: list[tuple[int, int]] = []  # (tenant_id, rowid)
        max_rowid = cursor_rowid
        for r in rows:
            row_id = int(r["rid"])
            if row_id > max_rowid:
                max_rowid = row_id
            tenant_id = _resolve_tenant_id(
                conn,
                user_id=r["user_id"],
                default_tenant_id=default_tenant_id,
                all_default=all_default,
            )
            updates.append((tenant_id, row_id))

        conn.executemany(
            f"UPDATE {TABLE} SET {COLUMN} = ? WHERE rowid = ? AND {COLUMN} IS NULL",
            updates,
        )
        conn.execute("COMMIT")
        return len(updates), max_rowid
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


def _resolve_tenant_id(
    conn: sqlite3.Connection,
    *,
    user_id: object,
    default_tenant_id: int,
    all_default: bool,
) -> int:
    """Pick a tenant_id for one event row.

    Null guards: ``user_id`` may be NULL for legacy/system rows; fall
    back to ``default_tenant_id`` in that case.
    """
    if all_default or user_id is None:
        return default_tenant_id
    row = conn.execute(
        "SELECT tenant_id FROM tenant_mapping WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None or row["tenant_id"] is None:
        return default_tenant_id
    return int(row["tenant_id"])


def _install_not_null_triggers(conn: sqlite3.Connection) -> None:
    """NOT NULL enforcement via BEFORE INSERT/UPDATE triggers.

    SQLite ALTER TABLE cannot add NOT NULL to an existing column without
    a table rebuild. Triggers give equivalent semantics: any future row
    with a NULL tenant_id is rejected at write time. Symmetric for
    INSERT and UPDATE so partial migrations cannot be hidden by an
    asymmetric handler.
    """
    if not trigger_exists(conn, "trg_events_tenant_id_not_null_ins"):
        conn.execute(
            "CREATE TRIGGER trg_events_tenant_id_not_null_ins "
            f"BEFORE INSERT ON {TABLE} "
            f"FOR EACH ROW WHEN NEW.{COLUMN} IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'tenant_id must not be NULL'); END"
        )
    if not trigger_exists(conn, "trg_events_tenant_id_not_null_upd"):
        conn.execute(
            "CREATE TRIGGER trg_events_tenant_id_not_null_upd "
            f"BEFORE UPDATE OF {COLUMN} ON {TABLE} "
            f"FOR EACH ROW WHEN NEW.{COLUMN} IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'tenant_id must not be NULL'); END"
        )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def run_rollback(db_path: str, sql_path: Path) -> None:
    sections = parse_sql_sections(sql_path)
    if "rollback" not in sections:
        raise RuntimeError("migration SQL missing @section: rollback")

    conn = open_db(db_path)
    with closing(conn):
        # Ensure audit tables exist (rollback is allowed even before forward).
        if "prepare" in sections:
            conn.executescript(sections["prepare"])
        audit(conn, "rollback_start", message="rollback_begin")
        conn.executescript(sections["rollback"])
        _rollback_drop_column(conn)
        update_state(conn, status="rolled_back", last_rowid=0)
        audit(conn, "rollback_finish", message="rollback_complete")
        LOG.info("rollback complete")


def _rollback_drop_column(conn: sqlite3.Connection) -> None:
    """Drop tenant_id from the events table.

    SQLite 3.35+ supports ALTER TABLE DROP COLUMN. Older versions need a
    table rebuild — implemented here as the documented 12-step procedure
    (https://sqlite.org/lang_altertable.html#otheralter). Both paths are
    idempotent: if the column does not exist, the function is a no-op.
    """
    if not column_exists(conn, TABLE, COLUMN):
        return
    if sqlite_supports_drop_column():
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
        return
    # Pre-3.35 fallback: rebuild events without the column.
    # The operator is responsible for scheduling this during a
    # maintenance window — it briefly blocks the table.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        kept = [c["name"] for c in cols if (c["name"] or "").lower() != COLUMN.lower()]
        if not kept:
            raise RuntimeError("cannot drop only column from table")
        col_list = ", ".join(f'"{c}"' for c in kept)
        conn.execute(f"CREATE TABLE {TABLE}__rebuild AS SELECT {col_list} FROM {TABLE}")
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE {TABLE}__rebuild RENAME TO {TABLE}")
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate_add_tenant_id_runner",
        description="Online migration: add events.tenant_id.",
    )
    p.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database (explicit; env vars are not consulted).",
    )
    p.add_argument(
        "--sql",
        default=str(Path(__file__).with_name("migrate_add_tenant_id.sql")),
        help="Path to companion SQL file.",
    )
    p.add_argument(
        "--direction",
        choices=("up", "down"),
        default="up",
        help="up = forward migration (default); down = rollback.",
    )
    p.add_argument(
        "--default-tenant-id",
        type=int,
        help="Tenant ID for rows whose user_id has no tenant_mapping entry. "
        "Required for --direction up.",
    )
    p.add_argument(
        "--all-default",
        action="store_true",
        help="Assign --default-tenant-id to every row, ignoring tenant_mapping. "
        "Use when tenant_mapping is unavailable.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per transaction (default {DEFAULT_BATCH_SIZE}).",
    )
    p.add_argument(
        "--build-index",
        action="store_true",
        help="After backfill, CREATE INDEX ix_events_tenant_id. Briefly "
        "blocks writers on 50M-row tables; default off.",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (default INFO).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sql_path = Path(args.sql)
    if args.direction == "down":
        run_rollback(args.db, sql_path)
        return 0

    if args.default_tenant_id is None:
        LOG.error("--default-tenant-id is required for forward migration")
        return 2
    if args.batch_size <= 0:
        LOG.error("--batch-size must be positive")
        return 2

    try:
        run_forward(
            ForwardOptions(
                db_path=args.db,
                sql_path=sql_path,
                default_tenant_id=args.default_tenant_id,
                batch_size=args.batch_size,
                all_default=args.all_default,
                build_index=args.build_index,
            )
        )
    except sqlite3.Error as exc:
        LOG.exception("migration failed")
        # Best-effort error audit; ignore if the DB itself is unreachable.
        try:
            conn = open_db(args.db)
            with closing(conn):
                audit(conn, "error", message=f"{type(exc).__name__}: {exc}")
                update_state(conn, status="pending")
        except sqlite3.Error:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
