# tmux-operational-scar

The tmux adapter resolves terminal panes and provides delivery, capture, health checks and pane lifecycle operations. Claude dispatch planning selects the headless subprocess lane; the retained adapter is not its default transport.

Background: [runtime state and terminal observations](../core/131_RUNTIME_STATE_MACHINE_CONTRACT.md).

## Entry points

[`scripts/lib/tmux_adapter.py::TmuxAdapter`](../../scripts/lib/tmux_adapter.py), [`scripts/lib/dispatch_plan.py::resolve_claude_lane`](../../scripts/lib/dispatch_plan.py).

## Tests

[tests/test_tmux_adapter_interface.py](../../tests/test_tmux_adapter_interface.py), [tests/test_dispatch_plan.py](../../tests/test_dispatch_plan.py).

## Decisions

[ADR-012](../governance/decisions/ADR-012-hybrid-interactive-headless.md), [ADR-024 and its amendment](../governance/decisions/ADR-024-single-entry-door-default-dispatch-lane.md).

## Flag

—
