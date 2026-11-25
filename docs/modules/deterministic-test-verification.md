# deterministic-test-verification

Builds receipt verification counts from the worker's JUnit sidecar when enabled, falling back to report text when the sidecar is missing or rejected and recording a rejection reason when present.
Failures reported in either source are retained, and disagreements are marked.
Background: [receipt format](../core/11_RECEIPT_FORMAT.md).

## Entry points

- [`scripts/lib/verification_runs.py::verification_record_for_report`](../../scripts/lib/verification_runs.py)

## Tests

- [tests/test_verification_junit_counts.py](../../tests/test_verification_junit_counts.py)

## Decisions

[ADR-035](../governance/decisions/ADR-035-receipt-v2.md)

## Flag

`VNX_JUNIT_COUNTS`
