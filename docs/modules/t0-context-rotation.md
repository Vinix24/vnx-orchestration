# t0-context-rotation

Measures T0 context from transcript usage or the latest compaction boundary and applies warning, force and hard bands. At the force threshold (500,000 tokens by default), the hook denies recognized new-work starts and blocks Stop unless its latch or an active Stop hook suppresses another block; unmeasurable context leaves the hook silent.

Background: [T0 context rotation](../operations/T0_CONTEXT_ROTATION.md).

## Entry points

[`scripts/lib/t0_context_budget.py::measure`](../../scripts/lib/t0_context_budget.py); [`scripts/hooks/t0_context_guard.py::handle`](../../scripts/hooks/t0_context_guard.py).

## Tests

[tests/test_t0_context_guard.py](../../tests/test_t0_context_guard.py); [tests/test_env_reads_t0_rotation_flag.py](../../tests/test_env_reads_t0_rotation_flag.py).

## Decisions

[ADR-005](../governance/decisions/ADR-005-ndjson-audit-ledger-primary.md)

## Flag

`VNX_T0_ROTATE_FORCE_TOKENS`
