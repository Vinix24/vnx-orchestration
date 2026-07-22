"""Receipt-v2 finalize step — ADR-035 §9 PR-4.

Wires PR-1 (``receipt_verdict.compute_verdict``) and PR-2 (the
warning-destination engine, ``warning_destination.assign_destination``) into
the receipt dict immediately before the shared validator/append primitive
(§7.1) sees it. Both write paths call this SAME function —
``append_receipt_internals.payload.append_receipt_payload`` (Path 2) and
``governance_emit.emit_dispatch_receipt`` (Path 1) — so ``verdict{}`` and
``warnings[]`` land identically regardless of which writer produced the
receipt (Codex Defense Checklist: same fix to all handlers).

Additive only: no ``schema_version`` stamp, no field removed/renamed (PR-5).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from receipt_verdict import compute_verdict  # noqa: E402

from .warning_destination import (  # noqa: E402
    DEFAULT_RECURRENCE_THRESHOLD,
    assign_destination,
    derive_open_items_created,
)


def _finalize_warnings(
    receipt: Dict[str, Any],
    *,
    dispatch_id: str,
    report_path: str,
    pr_id: str,
    threshold: int,
    counter_path: Optional[Path],
    open_items_manager_module: Optional[Any],
) -> None:
    """ADR-035 §6.1/§6.2: run any ``warnings[]`` entry that has not already
    been assigned a ``destination`` through the destination-assignment
    engine, then derive ``open_items_created`` from the resolved
    ``warnings[]``.

    Only touches the receipt when it actually carries ``warnings[]`` — a
    receipt with no ``warnings[]`` key is left alone, including whatever
    pre-v2 mechanism (the ``quality_advisory`` dry-run count) already set
    ``open_items_created``; migrating that mechanism onto ``warnings[]`` is
    PR-4b/PR-5, out of scope here.
    """
    warnings_list = receipt.get("warnings")
    if not warnings_list:
        return

    processed = []
    for entry in warnings_list:
        if isinstance(entry, dict) and "destination" not in entry:
            kwargs: Dict[str, Any] = dict(
                dispatch_id=dispatch_id,
                report_path=report_path,
                pr_id=pr_id,
                threshold=threshold,
            )
            if counter_path is not None:
                kwargs["counter_path"] = counter_path
            if open_items_manager_module is not None:
                kwargs["open_items_manager_module"] = open_items_manager_module
            processed.append(assign_destination(entry, **kwargs))
        else:
            processed.append(entry)

    receipt["warnings"] = processed
    receipt["open_items_created"] = derive_open_items_created(processed)


def finalize_receipt_v2_fields(
    receipt: Dict[str, Any],
    *,
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    counter_path: Optional[Path] = None,
    open_items_manager_module: Optional[Any] = None,
) -> None:
    """Mutate ``receipt`` in place: process ``warnings[]`` (if present)
    through the destination-assignment engine, then compute ``verdict{}``
    from the receipt's (now finalized) shape.

    Called by both write paths, always the last step before
    ``_validate_receipt``/``_write_receipt_under_lock`` (§7.1) — ``verdict{}``
    must see the resolved ``warnings[]`` destinations and any
    ``verification{}`` the caller already stamped.

    ``threshold``/``counter_path``/``open_items_manager_module`` are
    test-injection seams (mirrors ``assign_destination``'s own signature);
    production callers use the defaults (the real on-disk recurrence
    counter and the real ``open_items_manager``).
    """
    dispatch_id = str(receipt.get("dispatch_id") or "")
    report_path = str(receipt.get("report_path") or "")
    pr_id = str(receipt.get("pr_id") or "")

    _finalize_warnings(
        receipt,
        dispatch_id=dispatch_id,
        report_path=report_path,
        pr_id=pr_id,
        threshold=threshold,
        counter_path=counter_path,
        open_items_manager_module=open_items_manager_module,
    )

    receipt["verdict"] = compute_verdict(receipt)
