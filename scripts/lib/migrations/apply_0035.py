"""apply_0035.py — review_cap_verdicts: the tiebreaker's verdicts at the B3 review cap (R2a, ADR-041).

Track review-stopregel-en-lichte-stoelen, plan section 2. One row per verdict a
registry model gives on a PR at the B3 cap, keyed per ADR-007 by
``(project_id, pr_number, verdict_id)``. ``scripts/lib/review_cap_verdicts.py``
reads and writes it; the round ledger (``review_gates/rounds.ndjson``) says
which codex-seat verdict used the extra round a ``verkleinen`` granted.

Target shape (schema manifest v35, ``_review_cap_verdicts``):
  - the table, ``project_id TEXT NOT NULL DEFAULT '<store id>'``, a composite
    PRIMARY KEY over (project_id, pr_number, verdict_id), CHECKs on the rule and
    the outcome, and a ``verkleinen`` that names its intervention and dispatch;
  - ``ux_review_cap_verdicts_one_verkleinen``: a partial UNIQUE over
    (project_id, pr_number) WHERE outcome = 'verkleinen'. Two ``verkleinen`` on
    one PR are refused by the store itself, not only by the writer.

Built the way apply_0034 is, and for the same reasons:
  - the default is the store's id, resolved fail-closed from the DB path
    (``resolve_init_project_id(strict=True)``). An unresolvable store, or one
    whose sources disagree, raises before anything is written;
  - ``CREATE ... IF NOT EXISTS`` is a silent no-op when an object of that name
    already exists in another shape, so the post-condition (shape_violations)
    checks the v35 manifest's own invariants for the table and a clean
    ``composite_index_audit`` before the version stamp;
  - any failure raises inside apply_if_below's SAVEPOINT, inside an outer
    ``BEGIN IMMEDIATE`` that is rolled back: user_version stays below 35 and the
    file stays byte-equal.

No down script: the table is additive and nothing before it reads or writes it.
The rollback path is ``DROP TABLE review_cap_verdicts`` plus ``PRAGMA
user_version = 34`` on a store whose table is empty, else the pre-0035 backup.

Idempotent: skipped entirely when user_version >= 35; on a store that already
has the table in its v35 shape (a reconcile that dropped the version) the
statements are no-ops and only the stamp moves.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import List

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_id_migration
import schema_manifest
import schema_migration
from coordination_db import composite_index_audit

log = logging.getLogger(__name__)

_TARGET_VERSION = 35
TABLE = "review_cap_verdicts"
ONE_VERKLEINEN_INDEX = "ux_review_cap_verdicts_one_verkleinen"


class Migration0035Error(RuntimeError):
    """The store did not reach the 0035 shape; the migration rolled back."""


def table_sql(project_id: str) -> str:
    """The CREATE TABLE of review_cap_verdicts with the store's id as the project_id default."""
    if not project_id or "'" in project_id or "\\" in project_id:
        raise ValueError(f"Unsafe default_project_id: {project_id!r}")
    return f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            project_id          TEXT    NOT NULL DEFAULT '{project_id}',
            pr_number           INTEGER NOT NULL,
            verdict_id          TEXT    NOT NULL,
            head_sha            TEXT    NOT NULL,
            cap_rule            TEXT    NOT NULL CHECK (cap_rule IN ('B3.1', 'B3.2')),
            outcome             TEXT    NOT NULL CHECK (outcome IN (
                'verkleinen', 'terug-naar-plan', 'mergen-met-OI', 'outcome_unavailable_in_r2a')),
            chosen_outcome      TEXT    NOT NULL,
            intervention        TEXT    NOT NULL DEFAULT '',
            oi_ids              TEXT    NOT NULL DEFAULT '[]',
            granted_dispatch_id TEXT    NOT NULL DEFAULT '',
            tiebreaker_model    TEXT    NOT NULL DEFAULT '',
            rationale           TEXT    NOT NULL DEFAULT '',
            created_at          TEXT    NOT NULL,
            PRIMARY KEY (project_id, pr_number, verdict_id),
            CHECK (outcome <> 'verkleinen' OR (intervention <> '' AND granted_dispatch_id <> ''))
        )
    """


def create_table(conn: sqlite3.Connection, project_id: str) -> None:
    """The table and its one-verkleinen index; no-ops where they already exist.

    A table of that name in another shape makes the index fail on a missing
    column: that is the same refusal as a failed post-condition.
    """
    conn.execute(table_sql(project_id))
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {ONE_VERKLEINEN_INDEX} ON {TABLE} (project_id, pr_number) "
            "WHERE outcome = 'verkleinen'"
        )
    except sqlite3.OperationalError as exc:
        raise Migration0035Error(f"apply_0035: {TABLE} did not reach the 0035 shape, rolled back: {exc}") from exc


def shape_violations(conn: sqlite3.Connection) -> List[str]:
    """What keeps *conn*'s store from the 0035 shape; empty when it holds."""
    out: List[str] = [
        f"{m['table']}: index {m['index']} is not attached to the table"
        for m in composite_index_audit(conn)["missing"]
    ]
    out.extend(schema_manifest.validate_table(conn, schema_manifest.table_at(_TARGET_VERSION, TABLE)))
    return out


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at user_version >= 35).

    *migration_sql_path* is accepted for the auto_apply runner contract; the
    .sql file documents the shape, this runner produces it.

    Raises project_scope.TenantUnresolved (no id) or RuntimeError (sources
    disagree, invalid id), sqlite3.Error and Migration0035Error (post-condition);
    in every case the store is unchanged.
    """
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        if schema_migration.get_user_version(conn) >= _TARGET_VERSION:
            log.debug("apply_0035: already at user_version >= %d; skipped", _TARGET_VERSION)
            return False

        def _migrate(c: sqlite3.Connection) -> None:
            pid = project_id_migration.resolve_init_project_id(Path(db_path), strict=True)
            create_table(c, pid)
            violations = shape_violations(c)
            if violations:
                raise Migration0035Error(
                    f"apply_0035: {Path(db_path).name} did not reach the 0035 shape, rolled back: {violations}"
                )
            log.info("apply_0035: %s ensured with project_id default %r", TABLE, pid)

        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = schema_migration.apply_if_below(conn, _TARGET_VERSION, _migrate)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()

    if applied:
        log.info("apply_0035: review_cap_verdicts applied (user_version → 35)")
    return applied
