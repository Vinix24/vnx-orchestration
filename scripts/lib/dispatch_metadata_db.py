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
  - Column-guarded: every optional column (provider, model, project_id, …) is
    checked in one ``PRAGMA table_info`` call before use so the code is safe on
    legacy DBs that predate the migration, and a lock-contended run only pays
    the schema-probe's lock wait once instead of once per column.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Receipt-quality PR-4: write-time role normalization (fake backend-developer
# default -> NULL). Imported defensively; degradation keeps prior behaviour.
try:
    from dispatch_identity import normalize_role as _normalize_role  # noqa: E402
except Exception:  # pragma: no cover - sibling module available in-tree
    _normalize_role = None  # type: ignore[assignment]


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


#: sqlite3.connect's own built-in default when no ``timeout=`` is passed —
#: named here so callers that need the historical (unbounded-for-practical-
#: purposes) wait can say so explicitly instead of relying on an unlabeled 5.0.
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0


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
    session_id: Optional[str] = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> bool:
    """Create-if-absent and provider/model-stamp a ``dispatch_metadata`` row.

    Returns ``True`` when a row was written/updated, ``False`` when the write was
    skipped (DB missing, the sqlite lock-wait timed out, or another sqlite error
    was swallowed).

    Args:
        model: The AI model string used (e.g. "claude-sonnet-4-6", "codex",
               "kimi"). Stamped when the ``model`` column exists (migration
               v23 / GAP-2). Optional — callers that don't know the model
               may omit it.
        session_id: Pre-assigned worker session UUID (F1.1). Stamped when the
               ``session_id`` column exists. Optional — ignored when absent/None.
        timeout: Total seconds to wait for sqlite locks before giving up on the
               whole write. Passed to ``sqlite3.connect`` as the initial busy
               timeout, then re-armed via ``PRAGMA busy_timeout`` before every
               later statement to the seconds *remaining* against a single
               deadline — sqlite3's own busy-timeout is per statement (see
               https://docs.python.org/3/library/sqlite3.html#sqlite3.connect),
               so without re-arming, a connection issuing several statements
               (schema probe, INSERT, UPDATE) could each independently wait up
               to the full ``timeout`` and the cumulative stall would be a
               multiple of it — exactly the stall this parameter exists to
               bound. Defaults to sqlite3's own driver default so existing
               callers keep the same single-statement wait behaviour they had
               before this parameter existed. A caller whose write must never
               delay its critical path (e.g. the tmux lane's best-effort
               stamp) should pass a short explicit value instead.

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

    # Receipt-quality PR-4: never persist the fake backend-developer default —
    # normalize it to NULL at write time so the emit-side resolver stamps
    # identity_unresolved instead of propagating a fabricated identity. The
    # UPDATE's COALESCE then also stops a fake/empty role from nulling an
    # existing real role.
    if _normalize_role is not None:
        role = _normalize_role(role)
    else:
        role = role or None

    db_path = Path(db_path)
    if not db_path.exists():
        logger.debug("upsert_dispatch_provider_row: DB not found at %s — skipping", db_path)
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    completed_at = now_iso if outcome_status else None

    # A single wall-clock deadline for the whole call. ``sqlite3``'s busy
    # timeout is per statement, not per connection lifetime, so without
    # re-arming it before every later statement to the seconds *remaining*,
    # a schema probe + BEGIN + INSERT + UPDATE + commit could each burn a
    # fresh ``timeout`` window under contention — turning a "give up after
    # timeout" contract into "give up after up to 5x timeout". commit() is
    # just as contendable as the writes before it (it's what upgrades the
    # connection's lock to EXCLUSIVE to flush them) and BEGIN is where the
    # write lock is first acquired, so both get their own deadline-checked
    # re-arm exactly like the schema probe/INSERT/UPDATE. See
    # DEFAULT_LOCK_TIMEOUT_SECONDS docs above and the timeout= docstring for
    # the measurement behind this.
    deadline = time.monotonic() + timeout

    def _rearm_busy_timeout(conn: sqlite3.Connection) -> bool:
        """Reset the connection's busy timeout to the seconds left on the
        deadline, clamped to zero. Always writes the PRAGMA — even down to
        0 — so a later contend point (the next statement, or an implicit
        rollback during teardown) never inherits a stale, larger value left
        over from an earlier arm. Returns False once the deadline has
        already passed — the caller must give up immediately."""
        remaining = deadline - time.monotonic()
        conn.execute(f"PRAGMA busy_timeout = {max(0, int(remaining * 1000))}")
        return remaining > 0

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=timeout)

        def _bail(stage: str) -> bool:
            """Give up because the deadline is exhausted before `stage`.
            Rolls back any transaction opened so far — best-effort: a
            rollback that can't get the lock in time (busy_timeout is
            already re-armed to the remaining, possibly zero, budget) is
            swallowed, matching the fail-open contract that a bail must
            never propagate to the dispatch. A no-op when no transaction is
            open yet (e.g. bailing before BEGIN)."""
            logger.debug(
                "upsert_dispatch_provider_row: deadline exhausted before %s "
                "for dispatch=%s — skipping", stage, dispatch_id,
            )
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.debug(
                    "upsert_dispatch_provider_row: rollback after deadline "
                    "exhaustion failed (non-fatal)", exc_info=True,
                )
            return False

        if not _rearm_busy_timeout(conn):
            return _bail("schema probe")
        table_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
        }
        has_provider = "provider" in table_cols
        has_model = "model" in table_cols
        has_project = "project_id" in table_cols
        has_report_path = "outcome_report_path" in table_cols
        has_outcome = "outcome_status" in table_cols
        has_completed = "completed_at" in table_cols
        has_session_id = "session_id" in table_cols

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
        if has_session_id and session_id:
            insert_cols.append("session_id")
            insert_vals.append(session_id)
        if has_project:
            insert_cols.append("project_id")
            insert_vals.append(resolved_project_id)
        placeholders = ", ".join("?" for _ in insert_cols)

        # Explicit BEGIN IMMEDIATE: makes acquiring the write lock its own
        # bounded contend point. Without this, sqlite3's default isolation
        # handling issues an *implicit* BEGIN right before the INSERT below,
        # inside that same conn.execute() call — a second, independent
        # SQLITE_BUSY retry (lock acquisition, then the write) sharing the
        # busy_timeout armed for the INSERT, which could burn up to 2x that
        # window instead of the intended 1x. Starting the transaction here,
        # under its own deadline check, means the INSERT's own re-arm below
        # only ever has to bound the INSERT itself.
        if not _rearm_busy_timeout(conn):
            return _bail("BEGIN")
        conn.execute("BEGIN IMMEDIATE")

        if not _rearm_busy_timeout(conn):
            return _bail("INSERT")
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
        if has_session_id and session_id:
            set_clauses.append("session_id = COALESCE(session_id, ?)")
            params.append(session_id)
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

        if not _rearm_busy_timeout(conn):
            return _bail("UPDATE")

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

        # commit() upgrades the connection's lock to EXCLUSIVE to flush the
        # transaction to disk — exactly as contendable as the INSERT/UPDATE
        # it follows, so it gets the same deadline-check + re-arm instead of
        # silently inheriting the UPDATE's busy_timeout window unchanged
        # (the codex BLOCK on the prior round: a commit that contends after
        # the earlier statements already spent most of the budget must not
        # get a fresh window layered on top of it).
        if not _rearm_busy_timeout(conn):
            return _bail("commit")
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
            try:
                conn.close()
            except sqlite3.Error:
                # Teardown must never propagate: if closing this connection
                # needs to roll back an open transaction and that rollback
                # itself hits a lock, fail-open the same as every other
                # contend point in this function rather than raising out of
                # a bail path.
                logger.debug(
                    "upsert_dispatch_provider_row: connection close failed "
                    "for dispatch=%s (non-fatal)", dispatch_id, exc_info=True,
                )
