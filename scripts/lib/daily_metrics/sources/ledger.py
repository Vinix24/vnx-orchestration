"""The merges-without-receipt source: deferred, and it opens nothing.

Decision 24: the receipt reader belongs to the ``deur-afgedwongen`` track
(D7), which joins the day's merge population with the door evidence. Until
that reader exists this module publishes ``merges_without_receipt_count`` as
``value: None`` with the reason ``ledger_reader_not_wired``. It opens no file
and never touches ``t0_receipts.ndjson``; a ``0`` is never the answer.

The reason changes when the reader lands: this module then calls it instead of
publishing the deferred record.
"""

from __future__ import annotations

from typing import Any

from daily_metrics import metric

SOURCE = "daily_metrics.ledger(deferred)"
UNKNOWN_REASON = "ledger_reader_not_wired"


def collect(ctx: Any) -> dict[str, metric.ScalarMetric]:
    """Publish ``merges_without_receipt_count`` as unknown: the reader is not wired."""
    return {
        "merges_without_receipt_count": metric.ScalarMetric(
            value=None,
            source=SOURCE,
            unknown_reason=UNKNOWN_REASON,
        )
    }
