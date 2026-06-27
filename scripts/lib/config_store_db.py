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
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import config_registry

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


def _coerce(type_: str, value: Any) -> str:
    """Validate + normalise a value to its stored string form. Raises ValueError on a bad value."""
    if type_ == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return "1"
        if s in ("0", "false", "no", "off", ""):
            return "0"
        raise ValueError(f"invalid bool value: {value!r}")
    return str(value)


def _emit_config_event_best_effort(
    conn: sqlite3.Connection, project_id: str, key: str, old: Optional[str], new: str,
    actor: str, event_id: str,
) -> None:
    """Append a `config_changed` coordination event with the audit's event_id. Best-effort: the
    project_config_audit row (written atomically by set_config) is the hard governance record; the
    broader event stream must never block or roll back a config write."""
    try:
        import coordination_db
        coordination_db._append_event(  # noqa: SLF001 — canonical event helper
            conn, event_type="config_changed", entity_type="project_config", entity_id=key,
            from_state=old, to_state=new, actor=actor,
            reason="operator config change", metadata={"event_id": event_id},
            project_id=project_id,
        )
        conn.commit()
    except Exception:
        # A partial _append_event may leave an open implicit txn — roll it back so the connection
        # is handed back clean to the caller. The audited write already committed above.
        try:
            conn.rollback()
        except Exception:  # vnx-silent-except: rollback of a failed best-effort emit; audited write already committed
            pass


def set_config(
    conn: sqlite3.Connection, project_id: str, key: str, value: Any, *,
    actor: str, approval_id: Optional[str] = None,
) -> dict:
    """Persist an operator config change, atomically writing the value + a mandatory audit row.

    Validates against the registry (unknown / not-writable / approval-required all raise), coerces
    the value, then writes `project_config` (upsert) + `project_config_audit` in ONE transaction —
    there is no write without an audit row. A `config_changed` coordination event is emitted
    best-effort afterwards. Returns {key, old_value, new_value, event_id}.
    """
    entry = config_registry.CONFIG_REGISTRY.get(key)
    if entry is None:
        raise ValueError(f"unknown config key: {key}")
    if not entry.writable_from_ui:
        raise PermissionError(f"{key} is not writable from the UI")
    if entry.requires_approval and not approval_id:
        raise PermissionError(f"{key} requires an approval_id")

    coerced = _coerce(entry.type, value)
    ensure_config_tables(conn)  # DDL, idempotent — commits any pending txn, so BEGIN below is clean
    event_id = str(uuid.uuid4())

    # BEGIN IMMEDIATE takes the write lock up front so the old-value read and the value+audit write
    # share ONE snapshot: no concurrent writer can slip between them and stale the audit's old_value.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Audit C2: read old_value DIRECTLY (not via the fail-open read_config). read_config swallows
        # sqlite errors -> None, which would write a FALSIFIED audit old_value=NULL on a real read
        # failure. Here a read error must propagate and roll the txn back (the write fails loudly).
        _old_row = conn.execute(
            "SELECT config_value FROM project_config WHERE project_id = ? AND config_key = ?",
            (project_id, key),
        ).fetchone()
        old = _old_row[0] if _old_row else None
        conn.execute(
            """
            INSERT INTO project_config (project_id, config_key, config_value, config_type, updated_by, approval_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, config_key) DO UPDATE SET
                config_value = excluded.config_value,
                config_type  = excluded.config_type,
                updated_by   = excluded.updated_by,
                updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                approval_id  = excluded.approval_id
            """,
            (project_id, key, coerced, entry.type, actor, approval_id),
        )
        conn.execute(
            """
            INSERT INTO project_config_audit
                (project_id, config_key, old_value, new_value, changed_by, approval_id, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, key, old, coerced, actor, approval_id, event_id),
        )
        conn.commit()  # the value + its audit row commit together, or neither does
    except BaseException:
        conn.rollback()
        raise

    _emit_config_event_best_effort(conn, project_id, key, old, coerced, actor, event_id)
    return {"key": key, "old_value": old, "new_value": coerced, "event_id": event_id}


def make_db_resolver(
    state_dir_for_project: Callable[[str], "Optional[Path | str]"],
) -> config_registry.DbResolver:
    """Build a config_registry DB-resolver (step 2 of the precedence chain) backed by read_config.

    ``state_dir_for_project(project_id)`` returns the project's state dir (or None). The resolver
    opens the per-project runtime_coordination.db READ-ONLY; any miss / error → None (fail-open)."""
    def _resolver(project_id: Optional[str], key: str) -> Optional[str]:
        if not project_id:
            return None
        sdir = state_dir_for_project(project_id)
        if not sdir:
            return None
        db = Path(sdir) / "runtime_coordination.db"
        if not db.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            return read_config(conn, project_id, key)
        finally:
            conn.close()
    return _resolver
