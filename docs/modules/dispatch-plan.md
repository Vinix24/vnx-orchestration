# dispatch-plan

Compiles a validated dispatch specification and runtime snapshot into an execution plan or an explicit rejection. It checks promotion, role, task class, skill and constraints, then fixes lane, billing, model, target, worktree isolation and report requirements.

Background: [dispatch architecture](../core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md), [enforcement chain](../core/ENFORCEMENT_CHAIN.md).

## Entry points

[`scripts/lib/dispatch_plan.py::compile_plan`](../../scripts/lib/dispatch_plan.py), [`scripts/lib/dispatch_plan.py::ExecutionPlan`](../../scripts/lib/dispatch_plan.py).

## Tests

[tests/test_dispatch_plan.py](../../tests/test_dispatch_plan.py).

## Decisions

[ADR-006](../governance/decisions/ADR-006-staging-promote-human-gate.md), [ADR-024](../governance/decisions/ADR-024-single-entry-door-default-dispatch-lane.md), [ADR-025](../governance/decisions/ADR-025-raw-file-dispatch-deprecation.md).

## Flag

—
