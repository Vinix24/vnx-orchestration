#!/usr/bin/env python3
"""Helpers for governance/runtime receipts outside the report pipeline."""

from __future__ import annotations

import datetime as dt
import importlib
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dispatch_identity import validate_receipt_kind

logger = logging.getLogger(__name__)


def _load_append_receipt():
    """Import the ``scripts/append_receipt.py`` facade, making its dir importable.

    ``append_receipt_payload`` reads the ``common.facade`` proxy, which is only
    populated when the facade module registers, so importing
    ``append_receipt_internals`` alone is not equivalent. A lib-only entry point
    (``scripts/analysis/*``, a standalone test) has ``scripts/lib`` on sys.path
    but not ``scripts/``, where the facade lives, so put it there first — the
    same self-bootstrap ``context_rotation`` does.
    """
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("append_receipt")


_append_receipt = _load_append_receipt()
AppendReceiptError = _append_receipt.AppendReceiptError
append_receipt_payload = _append_receipt.append_receipt_payload


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_governance_receipt(
    event_type: str,
    *,
    receipt_kind: str,
    status: str = "info",
    terminal: str = "T0",
    source: str = "vnx_governance",
    receipts_file: Optional[str] = None,
    lock_timeout: Optional[float] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Append a governance event into canonical receipts.

    ``receipt_kind`` is REQUIRED (receipt-quality PR-3, plan §3b): a member
    of the ``dispatch_identity.RECEIPT_KINDS`` closed set, stamped from the
    emitter's own knowledge. A missing kwarg hard-fails with TypeError; an
    out-of-vocab value raises ValueError before anything is written.

    ``lock_timeout`` (seconds) bounds the wait on the append lock, for a
    caller that holds a lock of its own: still taken then, it raises
    ``AppendReceiptError`` with code ``lock_timeout`` and nothing is written
    (``append_receipt_payload``). The default waits as long as it takes.
    """
    validate_receipt_kind(receipt_kind)
    receipt: Dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "event_type": event_type,
        "receipt_kind": receipt_kind,
        "status": status,
        "terminal": terminal,
        "source": source,
    }
    receipt.update(fields)
    result = append_receipt_payload(receipt, receipts_file=receipts_file, lock_timeout=lock_timeout)
    receipt["append_status"] = result.status
    receipt["idempotency_key"] = result.idempotency_key
    return receipt


#: Value of an identity key whose source could not be resolved (ADR-056): never a default project.
UNKNOWN_IDENTITY = "unknown"

# Same shape ``schema_manifest.is_round_of`` accepts: ``-r`` plus a positive integer without a leading zero.
_ROUND_SUFFIX_RE = re.compile(r"^(?P<base>.+)-r[1-9][0-9]*$", re.ASCII)


def base_delivery_id(dispatch_id: Any) -> str:
    """The base delivery id of a dispatch: ``dlv-abc`` for ``dlv-abc`` and for its round ``dlv-abc-r3``.

    One round suffix is stripped. A missing, non-string or blank id gives ``unknown``.
    """
    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        return UNKNOWN_IDENTITY
    dispatch_id = dispatch_id.strip()
    match = _ROUND_SUFFIX_RE.match(dispatch_id)
    return match.group("base") if match else dispatch_id


def _resolve_receipt_project_id(receipts_file: Optional[str]) -> str:
    """``project_id`` of the store a receipt goes to; ``unknown`` when the resolver fails (ADR-056)."""
    try:
        from project_scope import resolve_stamp_project_id

        target = _append_receipt._resolve_receipts_file(receipts_file)
        project_id = resolve_stamp_project_id(db_path=target)
    except Exception as exc:
        logger.warning("governance_receipts: project_id unresolved, writing %r: %s: %s",
                       UNKNOWN_IDENTITY, type(exc).__name__, exc)
        return UNKNOWN_IDENTITY
    return project_id if isinstance(project_id, str) and project_id.strip() else UNKNOWN_IDENTITY


def dispatch_identity_fields(dispatch_id: Any, receipts_file: Optional[str] = None) -> Dict[str, str]:
    """The delivery identity ``(project_id, delivery_id)`` of ADR-056, plus ``dispatch_id`` when one is known.

    Both identity keys are always present: a source that cannot resolve one writes ``unknown``
    and is never booked silently as ``vnx-dev``.
    """
    fields: Dict[str, str] = {
        "project_id": _resolve_receipt_project_id(receipts_file),
        "delivery_id": base_delivery_id(dispatch_id),
    }
    if isinstance(dispatch_id, str) and dispatch_id:
        fields["dispatch_id"] = dispatch_id
    return fields


__all__ = [
    "emit_governance_receipt",
    "utc_now_iso",
    "AppendReceiptError",
    "UNKNOWN_IDENTITY",
    "base_delivery_id",
    "dispatch_identity_fields",
]
