# receipt-pipeline

Converts worker reports into governed receipts and scans report directories with per-outcome accounting. Receipt payloads pass through a shared append path that validates, enriches and deduplicates them under a lock.

Background: [receipt format](../core/11_RECEIPT_FORMAT.md), [receipt pipeline](../operations/RECEIPT_PIPELINE.md).

## Entry points

[`scripts/lib/report_to_receipt_converter.py::scan_and_convert`](../../scripts/lib/report_to_receipt_converter.py), [`scripts/lib/append_receipt_internals/payload.py::append_receipt_payload`](../../scripts/lib/append_receipt_internals/payload.py).

## Tests

[tests/test_report_to_receipt_converter.py](../../tests/test_report_to_receipt_converter.py), [tests/test_receipt_v2_pr4_wiring.py](../../tests/test_receipt_v2_pr4_wiring.py).

## Decisions

[ADR-005](../governance/decisions/ADR-005-ndjson-audit-ledger-primary.md), [ADR-035](../governance/decisions/ADR-035-receipt-v2.md), [ADR-055](../governance/decisions/ADR-055-processor-liveness-heartbeat.md).

## Flag

—
