#!/usr/bin/env python3
"""dispatch_identity.py — resolve the dispatch's real role from dispatch_metadata.

Receipt-quality track PR-1: receipts in the canonical ledger carry no `role`,
yet ``dispatch_metadata`` (quality_intelligence.db) already holds a real role
for most dispatches, keyed on ``(dispatch_id, project_id)`` per ADR-007. This
module propagates that identity into the v2 receipt emit.

FAIL-OPEN contract: any DB-missing / table-missing / query error returns None
and never raises — receipt emission must never break on the identity join.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_LIB = Path(__file__).resolve().parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

logger = logging.getLogger(__name__)

# The fake default stamped by writers that never resolved a real role. The
# trail must stop propagating it — treated as "no real role".
_FAKE_DEFAULT_ROLE = "backend-developer"


def _resolve_db_path(state_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate quality_intelligence.db — same resolution as intelligence_backfill.

    Order: explicit state_dir → VNX_STATE_DIR env → canonical vnx_paths
    resolver. If none of those resolve to an existing DB, return None
    (fail-open — no repo-local guess). Never raises.
    """
    try:
        if state_dir is not None:
            candidate = Path(state_dir) / "quality_intelligence.db"
            if candidate.exists():
                return candidate
        state_dir_env = os.environ.get("VNX_STATE_DIR")
        if state_dir_env:
            candidate = Path(state_dir_env) / "quality_intelligence.db"
            if candidate.exists():
                return candidate
        try:
            from vnx_paths import resolve_paths
            candidate = Path(resolve_paths()["VNX_STATE_DIR"]) / "quality_intelligence.db"
            if candidate.exists():
                return candidate
        except Exception:
            logger.debug(
                "dispatch_identity: vnx_paths canonical resolver unavailable",
                exc_info=True,
            )
    except Exception:
        logger.debug("dispatch_identity: db path resolution failed", exc_info=True)
    return None


def resolve_dispatch_role(
    dispatch_id: str,
    project_id: str,
    state_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the real role for a dispatch, or None when unresolved.

    Queries ``dispatch_metadata`` on the ADR-007 composite key, latest row
    wins. Returns None for missing rows, null/empty roles, and the literal
    ``"backend-developer"`` fake default. FAIL-OPEN: never raises.
    """
    try:
        if not dispatch_id or not project_id:
            return None
        db_path = _resolve_db_path(state_dir)
        if db_path is None:
            logger.debug(
                "dispatch_identity: no quality_intelligence.db found "
                "(dispatch=%s project=%s)", dispatch_id, project_id,
            )
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT role FROM dispatch_metadata "
                "WHERE dispatch_id=? AND project_id=? ORDER BY id DESC LIMIT 1",
                (dispatch_id, project_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        role = row[0]
        if not role or not str(role).strip():
            return None
        role = str(role).strip()
        if role == _FAKE_DEFAULT_ROLE:
            return None
        return role
    except Exception:
        logger.debug(
            "dispatch_identity: role resolution failed open "
            "(dispatch=%s project=%s)", dispatch_id, project_id,
            exc_info=True,
        )
        return None
