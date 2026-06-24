#!/usr/bin/env python3
"""dispatch_metadata_db.py — Shared dispatch_metadata row writer (provider+model-aware).

Single source of truth for stamping a ``dispatch_metadata`` row with its
``provider`` and ``model``. Used by BOTH dispatch paths so the
self-learning/intelligence layer is never provider-blind:

  - ``log_dispatch_metadata.py`` (tmux / interactive claude path via dispatcher)
  - ``provider_dispatch._emit_governance`` (headless multi-provider path:
    codex / gemini / kimi / litellm and headless claude)

Before this module, only the tmux path created rows, so non-Claude dispatches
created zero intelligence rows and the receipt processor's
``UPDATE dispatch_metadata ... WHERE dispatch_id=?`` was a silent no-op.

ADR-007: every write stamps ``project_id`` when the column exists. ``provider``
and ``model`` are descriptive (non-key) columns; the composite
``(project_id, provider)`` index (migration v21/GAP-2) keeps them
tenant-scoped-queryable.

Design notes:
  - Best-effort: a missing DB or a transient sqlite error returns ``False``
    rather than raising — metadata logging is non-fatal to the dispatch, matching
    the dispatcher's ``|| log WARNING`` contract.
  - Idempotent: ``INSERT OR IGNORE`` creates the row only when absent so a richer
    row written by the dispatcher path is never clobbered. The follow-up UPDATE
    stamps provider/model authoritatively and fills outcome/report_path/role/gate/pr_id
    only when not already set (COALESCE), so concurrent writers converge.
  - Column-guarded: each optional column (provider, model, project_id, …) is checked
    via PRAGMA table_info before use so the code is safe on legacy DBs that predate
    the migration.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(row[1] == column for row in rows)


def _resolve_project_id(explicit: Optional[str], db_path: Optional[Path] = None) -> str:
    """Resolve the project_id to stamp on a ``dispatch_metadata`` row — fail-closed.

    Delegates to :func:`project_scope.resolve_stamp_project_id`, the single
    store-derived resolver: the OWNING store (derived from ``db_path``'s
    ``~/.vnx-data/<pid>/state/`` layout) is authoritative, so a write to a
    non-vnx-dev store (e.g. mission-control) stamps THAT tenant — never the bare
    ``vnx-dev`` literal.

    Raises :class:`project_scope.TenantUnresolved` when no tenant can be
    resolved (no source / source conflict / invalid id). This REVERSES the prior
    #907 fail-open semantics (degrade-to-env / keep-vnx-dev) on purpose: the
    QI-write-tier is fail-closed per the ADR-007 amendment (2026-06-24). The sole
    caller, :func:`upsert_dispatch_provider_row`, catches it and skips the write.
    """
    from project_scope import resolve_stamp_project_id  # noqa: PLC0415
    return resolve_stamp_project_id(explicit, db_path)


def _log_tenant_stamp_skip(
    db_path: Path, dispatch_id: str, terminal: str, exc: Exception
) -> None:
    """Record a fail-closed dispatch_metadata skip — observable, not silent.

    Logs at ERROR with the diagnostic fields and bumps a counter event in
    ``<db_dir>/skip_metrics.ndjson``. The metric path anchors on the DB file's
    own directory (NOT ``~/.vnx-data/<pid>/state``) precisely because the pid is
    what failed to resolve — the db dir always exists and needs no tenant.
    """
    logger.error(
        "tenant_stamp_skip: refused dispatch_metadata write (fail-closed) — "
        "db_path=%s dispatch_id=%s terminal=%s conflicting_sources=%s",
        db_path, dispatch_id, terminal or "?", exc,
    )
    try:
        from state_writer import append_locked  # noqa: PLC0415
        append_locked(
            Path(db_path).parent / "skip_metrics.ndjson",
            {
                "event_type": "tenant_stamp_skip",
                "table": "dispatch_metadata",
                "db_path": str(db_path),
                "dispatch_id": dispatch_id,
                "terminal": terminal or None,
                "reason": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:  # noqa: BLE001 — the metric is best-effort; the ERROR log is the contract
        logger.debug("skip_metrics append failed (non-fatal)", exc_info=True)


def upsert_dispatch_provider_row(
    db_path: Path | str,
    *,
    dispatch_id: str,
    terminal: str,
    provider: str,
    model: Optional[str] = None,
    track: str = "headless",
    role: Optional[str] = None,
    gate: Optional[str] = None,
    pr_id: Optional[str] = None,
    outcome_status: Optional[str] = None,
    report_path: Optional[str] = None,
    project_id: Optional[str] = None,
) -> bool:
    """Create-if-absent and provider/model-stamp a ``dispatch_metadata`` row.

    Returns ``True`` when a row was written/updated, ``False`` when the write was
    skipped (DB missing) or a sqlite error was swallowed.

    Args:
        model: The AI model string used (e.g. "claude-sonnet-4-6", "codex",
               "kimi"). Stamped when the ``model`` column exists (migration
               v23 / GAP-2). Optional — callers that don't know the model
               may omit it.

    Raises:
        ValueError: ``dispatch_id``, ``terminal``, or ``provider`` is empty —
            these are programmer-contract violations, not runtime conditions.
    """
    if not (dispatch_id or "").strip():
        raise ValueError("upsert_dispatch_provider_row: dispatch_id is required")
    if not (terminal or "").strip():
        raise ValueError("upsert_dispatch_provider_row: terminal is required")
    if not (provider or "").strip():
        raise ValueError("upsert_dispatch_provider_row: provider is required")

    db_path = Path(db_path)
    if not db_path.exists():
        logger.debug("upsert_dispatch_provider_row: DB not found at %s — skipping", db_path)
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    completed_at = now_iso if outcome_status else None

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        has_provider = _has_column(conn, "dispatch_metadata", "provider")
        has_model = _has_column(conn, "dispatch_metadata", "model")
        has_project = _has_column(conn, "dispatch_metadata", "project_id")
        has_report_path = _has_column(conn, "dispatch_metadata", "outcome_report_path")
        has_outcome = _has_column(conn, "dispatch_metadata", "outcome_status")
        has_completed = _has_column(conn, "dispatch_metadata", "completed_at")

        # Tenant-stamp only when the column exists (old column-less stores are
        # left untouched). Fail-closed: an unresolvable tenant logs + skips the
        # write rather than stamping a contaminating 'vnx-dev' default.
        resolved_project_id = None
        if has_project:
            from project_scope import TenantUnresolved  # noqa: PLC0415
            try:
                resolved_project_id = _resolve_project_id(project_id, db_path)
            except TenantUnresolved as exc:
                _log_tenant_stamp_skip(db_path, dispatch_id, terminal, exc)
                return False

        # --- create-if-absent (never clobber a richer dispatcher-written row) ---
        insert_cols = ["dispatch_id", "terminal", "track", "role", "gate", "pr_id", "dispatched_at"]
        insert_vals = [dispatch_id, terminal, track, role or None, gate or None, pr_id or None, now_iso]
        if has_provider:
            insert_cols.append("provider")
            insert_vals.append(provider)
        if has_model and model:
            insert_cols.append("model")
            insert_vals.append(model)
        if has_project:
            insert_cols.append("project_id")
            insert_vals.append(resolved_project_id)
        placeholders = ", ".join("?" for _ in insert_cols)
        conn.execute(
            f"INSERT OR IGNORE INTO dispatch_metadata ({', '.join(insert_cols)}) "
            f"VALUES ({placeholders})",
            insert_vals,
        )

        # --- authoritative provider/model stamp + non-clobbering field fills ---
        set_clauses = []
        params: list = []
        if has_provider:
            set_clauses.append("provider = ?")
            params.append(provider)
        if has_model and model:
            set_clauses.append("model = COALESCE(model, ?)")
            params.append(model)
        set_clauses.append("role = COALESCE(role, ?)")
        params.append(role or None)
        set_clauses.append("gate = COALESCE(gate, ?)")
        params.append(gate or None)
        set_clauses.append("pr_id = COALESCE(pr_id, ?)")
        params.append(pr_id or None)
        if outcome_status and has_outcome:
            set_clauses.append("outcome_status = COALESCE(outcome_status, ?)")
            params.append(outcome_status)
        if report_path and has_report_path:
            set_clauses.append("outcome_report_path = COALESCE(outcome_report_path, ?)")
            params.append(report_path)
        if completed_at and has_completed:
            set_clauses.append("completed_at = COALESCE(completed_at, ?)")
            params.append(completed_at)

        # ADR-007: scope UPDATE by (project_id, dispatch_id) to prevent cross-tenant overwrite.
        if has_project:
            params.append(resolved_project_id)
            params.append(dispatch_id)
            conn.execute(
                f"UPDATE dispatch_metadata SET {', '.join(set_clauses)} "
                f"WHERE project_id = ? AND dispatch_id = ?",
                params,
            )
        else:
            params.append(dispatch_id)
            conn.execute(
                f"UPDATE dispatch_metadata SET {', '.join(set_clauses)} WHERE dispatch_id = ?",
                params,
            )
        conn.commit()
        logger.debug(
            "upsert_dispatch_provider_row: stamped dispatch=%s provider=%s model=%s outcome=%s",
            dispatch_id, provider, model, outcome_status,
        )
        return True
    except sqlite3.Error as exc:
        logger.warning(
            "upsert_dispatch_provider_row: sqlite error for dispatch=%s (non-fatal): %s",
            dispatch_id, exc,
        )
        return False
    finally:
        if conn is not None:
            conn.close()
