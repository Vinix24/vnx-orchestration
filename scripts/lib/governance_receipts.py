#!/usr/bin/env python3
"""Helpers for governance/runtime receipts outside the report pipeline."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ``append_receipt`` is the facade at ``scripts/append_receipt.py``, one level
# above this package; the internals need its registration (the ``facade``
# proxy), so importing ``append_receipt_internals`` alone is not equivalent.
# An entry point that puts only ``scripts/lib`` on sys.path (scripts/analysis/*,
# a standalone test) would fail the bare import, so make the facade's own
# directory importable first — the same guard ``context_rotation`` uses.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from append_receipt import AppendReceiptError, append_receipt_payload
from dispatch_identity import validate_receipt_kind


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
