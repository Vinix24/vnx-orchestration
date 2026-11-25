# dream-consolidation

Consolidates a project's intelligence patterns into a pending review document; applying archive and source-invalidation actions requires the separate approval path. Scheduled and manual cycles share the arm-switch and probe checks, with degraded probe health permitting proposal-only consolidation.

Background: [Dreaming](../intelligence/DREAMING.md).

## Entry points

[`scripts/dream/consolidator.py::run_dream_cycle`](../../scripts/dream/consolidator.py); [`scripts/dream/review_gate.py::approve_cycle`](../../scripts/dream/review_gate.py).

## Tests

[tests/test_dream_consolidator.py](../../tests/test_dream_consolidator.py); [tests/test_dream_approve_invalidates_and_skips.py](../../tests/test_dream_approve_invalidates_and_skips.py).

## Decisions

[ADR-019](../governance/decisions/ADR-019-auto-dream-memory-consolidation.md), [ADR-066](../governance/decisions/ADR-066-dream-liveness-watch.md)

## Flag

`VNX_DREAM_SCHEDULER_ENABLED`
