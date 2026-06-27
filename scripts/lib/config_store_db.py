"""config_store_db — persistence for the operator config control-plane (P0, PR 2).

The two tables that back the UI-set, per-project, audited config:
  - ``project_config``       : the live values (one row per (project_id, config_key)).
  - ``project_config_audit`` : append-only history of every change (who/when/old→new + event link).

SCHEMA HOME — these tables are created via an idempotent ``ensure_config_tables`` (CREATE TABLE
IF NOT EXISTS), NOT via the numbered track-layer migration walk. Reasoning (and a deliberate
deviation from the "put it in 0032" review suggestion):

  * The track-layer migration system (migrate_future_system.py + schema_manifest.py) governs the
    track-layer schema (tracks/dispatches/…): its reconciler validates the FULL expected shape for
    a claimed ``user_version`` and DOWNGRADES on mismatch. Bolting orthogonal config tables onto
    that machinery means extending the full-shape manifest for an unrelated feature (high blast
    radius on every project's runtime_coordination.db).
  * The original review flagged a real bug — a ``runtime_coordination_v11.sql`` migration would be
    SKIPPED on a DB already at user_version 31. An idempotent ``CREATE TABLE IF NOT EXISTS`` is not
    user_version-gated at all, so it addresses that concern MORE robustly (it can never be skipped),
    while staying out of the manifest's full-shape contract.

ADR-007 (tenant stamping): both tables carry ``project_id`` and a composite key over it —
``project_config`` PK ``(project_id, config_key)``; ``project_config_audit`` UNIQUE
``(project_id, event_id)``.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_config (
    project_id   TEXT NOT NULL,
    config_key   TEXT NOT NULL,
    config_value TEXT NOT NULL,
    config_type  TEXT NOT NULL DEFAULT 'string',
    updated_by   TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    approval_id  TEXT,
    PRIMARY KEY (project_id, config_key)
);

CREATE TABLE IF NOT EXISTS project_config_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    config_key  TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    changed_by  TEXT NOT NULL,
    changed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    approval_id TEXT,
    event_id    TEXT NOT NULL,
    UNIQUE (project_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_pca_project_key ON project_config_audit(project_id, config_key);
"""


def ensure_config_tables(conn: sqlite3.Connection) -> None:
    """Create the config-store tables if absent. Idempotent; safe to call on every open."""
    conn.executescript(_SCHEMA)


def has_config_tables(conn: sqlite3.Connection) -> bool:
    """True when project_config exists (so callers can fail-open before ensure)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_config'"
    ).fetchone()
    return row is not None


def read_config(conn: sqlite3.Connection, project_id: str, key: str) -> Optional[str]:
    """Return the stored config_value for (project_id, key), or None when unset / table absent.

    This is the DB layer (step 2) of config_registry's precedence chain. Fail-open: any sqlite
    error (including a missing table) yields None so the runtime falls through to the env/default.
    """
    try:
        if not has_config_tables(conn):
            return None
        row = conn.execute(
            "SELECT config_value FROM project_config WHERE project_id = ? AND config_key = ?",
            (project_id, key),
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None
