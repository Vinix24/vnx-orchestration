# plan-gate-panel

Composes a configurable plan-review panel and records its seat verdicts and overall decision.
The default composition starts with one seat and escalates on BLOCK; sensitive paths or the gate profile can require two seats from the start.
Background: [dispatch rules](../core/DISPATCH_RULES.md) and [enforcement chain](../core/ENFORCEMENT_CHAIN.md).

## Entry points

- [`scripts/lib/plan_gate_panel.py::compose_panel`](../../scripts/lib/plan_gate_panel.py)
- [`scripts/lib/plan_gate_panel.py::run_panel`](../../scripts/lib/plan_gate_panel.py)

## Tests

- [tests/test_plan_gate_one_seat.py](../../tests/test_plan_gate_one_seat.py)
- [tests/test_gate_profile_plan_gate.py](../../tests/test_gate_profile_plan_gate.py)

## Decisions

[ADR-030](../governance/decisions/ADR-030-plan-first-gate-enforcement.md)

## Flag

`VNX_PLAN_GATE_ENFORCE`
