#!/usr/bin/env python3
"""Helpers for governance/runtime receipts outside the report pipeline."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from append_receipt import AppendReceiptError, append_receipt_payload


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_governance_receipt(
    event_type: str,
    *,
    status: str = "info",
    terminal: str = "T0",
    source: str = "vnx_governance",
    receipts_file: Optional[str] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Append a governance event into canonical receipts."""
    receipt: Dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "event_type": event_type,
        "status": status,
        "terminal": terminal,
        "source": source,
    }
    receipt.update(fields)
    result = append_receipt_payload(receipt, receipts_file=receipts_file)
    receipt["append_status"] = result.status
    receipt["idempotency_key"] = result.idempotency_key
    return receipt


__all__ = ["emit_governance_receipt", "utc_now_iso", "AppendReceiptError"]
