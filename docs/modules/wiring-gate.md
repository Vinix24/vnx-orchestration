# wiring-gate

Checks new public definitions in a PR diff for callers and can scan a committed Python tree for unused definitions. Unwired definitions in diff mode are advisory unless the gate is required; tree mode also reports files it could not parse.

Background: [gate execution lifecycle](../core/180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md).

## Entry points

[`scripts/lib/wiring_gate.py::analyse_diff`](../../scripts/lib/wiring_gate.py), [`scripts/lib/wiring_gate.py::check_tree_wiring`](../../scripts/lib/wiring_gate.py).

## Tests

[tests/test_wiring_gate.py](../../tests/test_wiring_gate.py), [tests/test_wiring_gate_tree.py](../../tests/test_wiring_gate_tree.py).

## Decisions

[ADR-046](../governance/decisions/ADR-046-een-poortmotor.md).

## Flag

`VNX_WIRING_GATE_REQUIRED`
