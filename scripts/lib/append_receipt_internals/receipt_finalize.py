"""Receipt-v2 finalize step — ADR-035 §9 PR-4 (+ fix-r1 classify/commit split).

Wires PR-1 (``receipt_verdict.compute_verdict``) and PR-2 (the
warning-destination engine, ``warning_destination.assign_destination``) into
the receipt dict immediately before the shared validator/append primitive
(§7.1) sees it. Both write paths call the SAME functions —
``append_receipt_internals.payload.append_receipt_payload`` (Path 2) and
``governance_emit.emit_dispatch_receipt`` (Path 1) — so ``verdict{}`` and
``warnings[]`` land identically regardless of which writer produced the
receipt (Codex Defense Checklist: same fix to all handlers).

fix-r1: ``assign_destination``'s side effects (open-items promotion,
recurrence-counter increment) used to run inline, BEFORE the shared
validator and the idempotency-dedup check — so a receipt later rejected by
the validator or skipped as a duplicate had already created an open item /
bumped the counter for a line that never lands in the ledger. This module
now splits the work into two phases:

  - ``classify_receipt_v2_warnings`` (pure, no I/O beyond a read-only
    recurrence-counter peek): computes ``destination``/``requires_tracking``
    with an ``oi_id: None`` placeholder for anything requiring tracking —
    exactly what ``compute_verdict`` needs (blocker warning -> reject;
    unresolved ``oi_pending`` -> investigate). Called BEFORE
    ``_validate_receipt``.
  - ``commit_receipt_v2_fields`` (the deferred write): resolves the
    placeholder to its real outcome (calls ``add_item_programmatic``,
    increments the counter) and recomputes ``verdict{}`` from the
    now-final ``warnings[]``. Passed as the ``pre_write_hook`` into
    ``_write_receipt_under_lock`` (idempotency.py) — invoked ONLY once the
    shared append primitive has confirmed under the append lock that this
    receipt is not a duplicate and will actually be written. A deduped or
    validator-rejected receipt never reaches this function, so it never
    touches the open-items store or the counter.

``finalize_receipt_v2_fields`` remains as the all-in-one composition
(classify + commit back-to-back) for direct callers that don't need the
deferred-commit split (e.g. unit tests exercising the engine in isolation).

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
    _PENDING_COMMIT_REASON,
    DEFAULT_RECURRENCE_THRESHOLD,
    classify_destination,
    commit_destination,
    derive_open_items_created,
)


def classify_receipt_v2_warnings(
    receipt: Dict[str, Any],
    *,
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    counter_path: Optional[Path] = None,
) -> None:
    """ADR-035 §6.1 (fix-r1, phase 1 — pure): run any ``warnings[]`` entry
    that has not already been assigned a ``destination`` through the
    side-effect-free ``classify_destination``. Never calls
    ``add_item_programmatic`` and never increments the recurrence counter —
    safe to run before the receipt is validated or checked for a duplicate.

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
            kwargs: Dict[str, Any] = dict(threshold=threshold)
            if counter_path is not None:
                kwargs["counter_path"] = counter_path
            processed.append(classify_destination(entry, **kwargs))
        else:
            processed.append(entry)

    receipt["warnings"] = processed


def commit_receipt_v2_warnings(
    receipt: Dict[str, Any],
    *,
    counter_path: Optional[Path] = None,
    open_items_manager_module: Optional[Any] = None,
) -> None:
    """ADR-035 §6.1/§6.2 (fix-r1, phase 2 — the deferred write): commit the
    side effect for any ``warnings[]`` entry ``classify_receipt_v2_warnings``
    classified (recognised by its pending-commit placeholder reason), then
    derive ``open_items_created`` from the now-committed ``warnings[]``.

    An entry the caller already supplied a final ``destination`` for
    (present before classification ever ran) is left untouched here too —
    it was never classified, so it is never committed.

    MUST be called only once the shared append primitive has confirmed this
    receipt will actually be written (see module docstring).
    """
    warnings_list = receipt.get("warnings")
    if not warnings_list:
        return

    dispatch_id = str(receipt.get("dispatch_id") or "")
    report_path = str(receipt.get("report_path") or "")
    pr_id = str(receipt.get("pr_id") or "")

    committed = []
    for entry in warnings_list:
        if isinstance(entry, dict) and entry.get("reason") == _PENDING_COMMIT_REASON:
            kwargs: Dict[str, Any] = dict(
                dispatch_id=dispatch_id,
                report_path=report_path,
                pr_id=pr_id,
            )
            if counter_path is not None:
                kwargs["counter_path"] = counter_path
            if open_items_manager_module is not None:
                kwargs["open_items_manager_module"] = open_items_manager_module
            committed.append(commit_destination(entry, **kwargs))
        else:
            committed.append(entry)

    receipt["warnings"] = committed
    receipt["open_items_created"] = derive_open_items_created(committed)


def commit_receipt_v2_fields(
    receipt: Dict[str, Any],
    *,
    counter_path: Optional[Path] = None,
    open_items_manager_module: Optional[Any] = None,
) -> None:
    """fix-r1 phase 2, full: commit the deferred warning side effects, then
    (re)compute ``verdict{}`` from the now-final ``warnings[]`` state.

    This is the ``pre_write_hook`` both write paths pass into
    ``_write_receipt_under_lock`` — invoked only once the append primitive,
    under the append lock, has confirmed this receipt is not a duplicate and
    will actually be written.
    """
    commit_receipt_v2_warnings(
        receipt,
        counter_path=counter_path,
        open_items_manager_module=open_items_manager_module,
    )
    receipt["verdict"] = compute_verdict(receipt)


def finalize_receipt_v2_fields(
    receipt: Dict[str, Any],
    *,
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    counter_path: Optional[Path] = None,
    open_items_manager_module: Optional[Any] = None,
) -> None:
    """Mutate ``receipt`` in place: process ``warnings[]`` (if present)
    through the destination-assignment engine (classify THEN commit,
    back-to-back), then compute ``verdict{}`` from the receipt's (now
    finalized) shape.

    fix-r1: this all-in-one composition is for direct callers that want the
    pre-split, no-deferred-commit behavior (e.g. warning-engine unit tests).
    The write paths (``append_receipt_internals.payload``,
    ``governance_emit``) call ``classify_receipt_v2_warnings`` before
    validation and ``commit_receipt_v2_fields`` as the append primitive's
    ``pre_write_hook`` instead — see the module docstring.

    ``threshold``/``counter_path``/``open_items_manager_module`` are
    test-injection seams (mirrors ``assign_destination``'s own signature);
    production callers use the defaults (the real on-disk recurrence
    counter and the real ``open_items_manager``).
    """
    classify_receipt_v2_warnings(receipt, threshold=threshold, counter_path=counter_path)
    commit_receipt_v2_fields(
        receipt,
        counter_path=counter_path,
        open_items_manager_module=open_items_manager_module,
    )
