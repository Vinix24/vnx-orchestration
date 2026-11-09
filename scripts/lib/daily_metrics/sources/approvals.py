"""The operator-wait source: unknown because the ledger has no request time.

Decision 9: the approval ledger records when the operator approved, never
when the request was raised, so there is no waiting time to measure. This
module opens no file at all; it publishes ``operator_wait_hours`` as
``value: None`` with the reason ``no_request_timestamp_in_approval_ledger``.

The reason changes when the override administration records a request
timestamp together with its reason and time; that source then owns the number.
"""

from __future__ import annotations

from typing import Any

from daily_metrics import metric

SOURCE = "daily_metrics.approvals"
UNKNOWN_REASON = "no_request_timestamp_in_approval_ledger"


def collect(ctx: Any) -> dict[str, metric.ScalarMetric]:
    """Publish ``operator_wait_hours`` as unknown: there is no request time."""
    return {
        "operator_wait_hours": metric.ScalarMetric(
            value=None,
            source=SOURCE,
            unknown_reason=UNKNOWN_REASON,
        )
    }
