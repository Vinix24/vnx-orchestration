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

The dispatch door follows [ADR-092](../governance/decisions/ADR-092-optionele-features-expliciet.md):
unknown or unreadable modes refuse by name. With a track and `required`, unreadable evidence
refuses with `plan_gate_evidence_unreadable` (including the error); unsupported evidence refuses
with `plan_gate_evidence_unsupported`. Retry after restoring the evidence. `VNX_OVERRIDE_PLAN_GATE=1`
waives those refusals or an unresolved plan, recording the refusal code in `waived_guarantees`.
Without a track, the existing track requirement still applies and a waiver names `track_unlinked`
or `no-track:<reason>`. Advisory and off keep their evidence behavior. The list includes the optional
feature snapshot, participates in the plan digest, and reaches the receipt under `route_decision.decision`.
