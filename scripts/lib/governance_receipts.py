#!/usr/bin/env python3
"""Helpers for governance/runtime receipts outside the report pipeline."""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dispatch_identity import validate_receipt_kind


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


__all__ = ["emit_governance_receipt", "utc_now_iso", "AppendReceiptError"]
