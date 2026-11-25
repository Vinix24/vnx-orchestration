# deliverable-predecessor

Checks a fire's hard predecessors against project-scoped merge receipts, or T0 acceptance for doc/post outputs. Unreadable evidence remains unknown; a closed delivery split across PRs requires every listed part to have merged from its own branch family.
Background: [subsystem register](../core/SUBSYSTEMS.md).

## Entry points
[`scripts/lib/deliverable_predecessor_gate.py::assess`](../../scripts/lib/deliverable_predecessor_gate.py), [`scripts/lib/deliverable_predecessor_gate.py::judge`](../../scripts/lib/deliverable_predecessor_gate.py).

## Tests
[tests/test_deliverable_predecessor_gate.py](../../tests/test_deliverable_predecessor_gate.py).

## Decisions
[ADR-096](../governance/decisions/ADR-096-dependency-per-deliverable-and-blast-radius.md).

## Flag
`VNX_DELIVERABLE_PREDECESSOR_MODE`
