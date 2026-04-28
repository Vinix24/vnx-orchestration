"""Helper for emitting state_mutation receipts for state-file writes."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_state_mutation(
    file: str,
    *,
    trigger: str,
    section: str = "",
    size_bytes: int = 0,
    rebuild_seconds: float = 0.0,
) -> Optional[Any]:
    """Emit a state_mutation receipt for a state-file write. Best-effort."""
    receipt: Dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "event_type": "state_mutation",
        "terminal": "T0",
        "source": "vnx_state",
        "file": file,
        "trigger": trigger,
    }
    if section:
        receipt["section"] = section
    if size_bytes:
        receipt["size_bytes"] = size_bytes
    if rebuild_seconds:
        receipt["rebuild_seconds"] = round(rebuild_seconds, 3)

    try:
        if str(_SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS_DIR))
        from append_receipt import append_receipt_payload
        return append_receipt_payload(receipt, skip_enrichment=True)
    except Exception:
        return None
