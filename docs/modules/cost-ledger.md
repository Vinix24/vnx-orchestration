# cost-ledger

Validates, prices and records provider runs in a cost ledger, using a spool when the ledger write fails. Totals use the last record per run and expose pending spool data as incomplete accounting.

Background: [cost tracking](../intelligence/COST_TRACKING_GUIDE.md).

## Entry points

[`scripts/lib/cost_ledger.py::record_run`](../../scripts/lib/cost_ledger.py), [`scripts/lib/cost_ledger.py::aggregate_runs`](../../scripts/lib/cost_ledger.py).

## Tests

[tests/test_cost_ledger.py](../../tests/test_cost_ledger.py), [tests/test_cost_ledger_one_lock.py](../../tests/test_cost_ledger_one_lock.py).

## Decisions

[ADR-005](../governance/decisions/ADR-005-ndjson-audit-ledger-primary.md), [ADR-043](../governance/decisions/ADR-043-het-kostengrootboek.md).

## Flag

—
