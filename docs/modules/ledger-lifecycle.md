# ledger-lifecycle

Records dispatch attempts with a start event and a deduplicated finish event keyed by project, dispatch and attempt.
For lifecycle-enabled dispatches, report ingestion becomes a separate `report_ingested` event rather than another outcome.
Background: [receipt format](../core/11_RECEIPT_FORMAT.md).

## Entry points

- [`scripts/lib/dispatch_start.py::begin`](../../scripts/lib/dispatch_start.py)
- [`scripts/lib/report_lifecycle_mode.py::report_line`](../../scripts/lib/report_lifecycle_mode.py)

## Tests

- [tests/test_k1_lifecycle_vocabulary.py](../../tests/test_k1_lifecycle_vocabulary.py)
- [tests/test_k8a_report_path_decision.py](../../tests/test_k8a_report_path_decision.py)

## Decisions

[ADR-045](../governance/decisions/ADR-045-audit-spoor-groep-1.md)

## Flag

`VNX_LEDGER_LIFECYCLE`
