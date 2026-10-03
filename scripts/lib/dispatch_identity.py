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

import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_LIB = Path(__file__).resolve().parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

logger = logging.getLogger(__name__)

# Single canonical sentinel for "no role resolved" (dispatch-20260804-190000).
# Every emit path reads from _IDENTITY_UNRESOLVED; there is no second copy and
# no bare literal anywhere.  _FAKE_DEFAULT_ROLE is an internal alias — the
# write-time sentinel that writers stamp when they never resolved a real role,
# and which the emit-side resolver filters out.  The two MUST agree: the value
# a writer stamps when it has no role IS the value the converter stamps when it
# can't resolve one — one concept, one sentinel, defined once.
#
# OI-981: a deliberately-chosen "backend-developer" is structurally
# distinguishable from a failed role resolution — the sentinel is
# "identity_unresolved", never the empty string and never a real role name.
_IDENTITY_UNRESOLVED = "identity_unresolved"
_FAKE_DEFAULT_ROLE = _IDENTITY_UNRESOLVED

# Receipt-quality PR-4: instruction-header source used by the write-time
# capture-gap backfill (mirrors subprocess_dispatch._ROLE_HEADER_RE).
_ROLE_HEADER_RE = re.compile(r"^Role:\s*(\S+)", re.MULTILINE)


def normalize_role(role: Optional[str]) -> Optional[str]:
    """Normalize a write-time role value.

    Returns the stripped role, or None when the value is empty/None or the
    canonical sentinel ``identity_unresolved``. Writers must persist NULL
    instead of the sentinel so the emit-side resolver stamps
    ``identity_unresolved`` rather than propagating a fabricated identity.
    """
    if not role:
        return None
    role = str(role).strip()
    if not role or role == _FAKE_DEFAULT_ROLE:
        return None
    return role


def extract_role_from_instruction(instruction: Optional[str]) -> Optional[str]:
    """Return the role from a ``Role: <name>`` header in the instruction, or None."""
    if not instruction:
        return None
    m = _ROLE_HEADER_RE.search(instruction)
    return m.group(1) if m else None

# Receipt-quality PR-3 (plan §3b): the authoritative closed set of
# ``receipt_kind`` values. Stamped per-emitter from the emitter's own
# knowledge — never derived from ``role``, never reusing ``task_class``.
RECEIPT_KINDS = frozenset({
    "build",
    "doc",
    "test",
    "review_gate",
    "panel_seat",
    "state_mutation",
    "sub_dispatch",
    "dispatch",
})


def validate_receipt_kind(receipt_kind: Optional[str]) -> str:
    """Emit-time lint (PR-3: warn -> raise). Every emitted receipt MUST carry
    a ``receipt_kind`` from the closed set; a missing or out-of-vocab value
    hard-fails the emit. Returns the validated kind. Raises ValueError.
    """
    if receipt_kind not in RECEIPT_KINDS:
        raise ValueError(
            f"Invalid receipt_kind {receipt_kind!r}. Must be one of "
            f"{sorted(RECEIPT_KINDS)} (receipt-quality §3b closed set; "
            "emit-time lint raises)."
        )
    return receipt_kind


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


def _candidate_state_dirs(state_dir: Optional[Path]) -> "list[Path]":
    """State dirs to search for the dispatch's own store, in resolution order.

    An explicit ``state_dir`` owns the lookup alone: its store is the only one
    consulted (ADR-007, a dispatch_id is only unique per project). Without one
    the order mirrors ``_resolve_db_path``: ``VNX_STATE_DIR`` env, then the
    canonical ``vnx_paths`` resolver.
    """
    if state_dir is not None:
        return [Path(state_dir)]
    candidates: "list[Path]" = []
    state_dir_env = os.environ.get("VNX_STATE_DIR")
    if state_dir_env:
        candidates.append(Path(state_dir_env))
    try:
        from vnx_paths import resolve_paths
        candidates.append(Path(resolve_paths()["VNX_STATE_DIR"]))
    except Exception:
        logger.debug(
            "dispatch_identity: vnx_paths canonical resolver unavailable",
            exc_info=True,
        )
    return candidates


def resolve_spec_role(
    dispatch_id: str,
    state_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the role from the dispatch's own ``dispatch-spec.json``, or None.

    Looks only in ``<data_dir>/dispatches/*/<dispatch_id>/dispatch-spec.json``
    of the store that owns ``state_dir`` (``data_dir = state_dir.parent``);
    never globs across stores. The value passes ``normalize_role``, so an empty
    role or the ``identity_unresolved`` sentinel never counts. A missing or
    unreadable spec, or one without a role, returns None. FAIL-OPEN: never raises.
    """
    try:
        did = (dispatch_id or "").strip()
        if not did or "/" in did or "\\" in did or did in (".", ".."):
            return None
        for candidate in _candidate_state_dirs(state_dir):
            dispatches_dir = candidate.parent / "dispatches"
            if not dispatches_dir.is_dir():
                continue
            for spec_path in sorted(dispatches_dir.glob(f"*/{did}/dispatch-spec.json")):
                try:
                    data = json.loads(spec_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                role = data.get("role")
                if not isinstance(role, str):
                    continue
                normalized = normalize_role(role)
                if normalized:
                    return normalized
        return None
    except Exception:
        logger.debug(
            "dispatch_identity: spec role resolution failed open dispatch=%s",
            dispatch_id, exc_info=True,
        )
        return None


def resolve_dispatch_role(
    dispatch_id: str,
    project_id: str,
    state_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the real role for a dispatch, or None when unresolved.

    Queries ``dispatch_metadata`` on the ADR-007 composite key, latest row
    wins. Returns None for missing rows, null/empty roles, and the canonical
    ``identity_unresolved`` sentinel. FAIL-OPEN: never raises.
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


def resolve_effective_role(
    role: Optional[str],
    dispatch_id: str,
    project_id: str,
    state_dir: Optional[Path] = None,
) -> str:
    """Resolve the dispatch's real role for receipt/report emission.

    The single canonical resolution shared by every emit path
    (``dispatch_govern._resolve_govern_role``, ``dispatch_envelope._govern``,
    ``provider_dispatch._emit_governance``, ``report_to_receipt_converter``).

    Order:
      1. A genuinely-set caller role (never the canonical ``identity_unresolved``
         sentinel, which writers stamp when they never resolved a real role).
      2. The role in the dispatch's own ``dispatch-spec.json``, in the store
         that owns ``state_dir`` only (``resolve_spec_role``).
      3. ``dispatch_metadata`` via the ADR-007 composite key
         (``dispatch_id``, ``project_id``) — the fallback for dispatches
         whose spec carries no role.
      4. ``_IDENTITY_UNRESOLVED`` — the single canonical sentinel, imported
         from this module by every emit path.

    FAIL-OPEN: never raises — receipt/report emission must not break on the
    identity join (mirrors ``resolve_dispatch_role``'s contract).
    """
    candidate = (role or "").strip()
    if candidate and candidate != _FAKE_DEFAULT_ROLE:
        return candidate
    spec_role = resolve_spec_role(dispatch_id, state_dir=state_dir)
    if spec_role:
        return spec_role
    resolved = resolve_dispatch_role(dispatch_id, project_id, state_dir=state_dir)
    return resolved or _IDENTITY_UNRESOLVED
