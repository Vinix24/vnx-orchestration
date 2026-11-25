# headless-dispatch-routing

Selects execution targets by task class and registry eligibility. With headless routing enabled, research and documentation synthesis prefer headless targets and can fall back to interactive targets; `coding_interactive` remains restricted to interactive targets.

Background: [Headless run contract](../contracts/HEADLESS_RUN_CONTRACT.md).

## Entry points

[`scripts/lib/dispatch_router.py::DispatchRouter.route`](../../scripts/lib/dispatch_router.py).

## Tests

[tests/test_headless_system.py](../../tests/test_headless_system.py).

## Decisions

[ADR-012](../governance/decisions/ADR-012-hybrid-interactive-headless.md)

## Flag

`VNX_HEADLESS_ROUTING`
