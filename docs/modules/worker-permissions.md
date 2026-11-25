# worker-permissions

Resolves role permission profiles and composes them with agent isolation settings into a permission policy. It also builds Claude worker arguments for tool restrictions and MCP scoping.

Background: [worker permissions](../operations/WORKER_PERMISSIONS.md), [policy enforcement rollout](../operations/PERMISSION_POLICY_FLIP.md).

## Entry points

[`scripts/lib/worker_permissions.py::resolve_worker_profile`](../../scripts/lib/worker_permissions.py), [`scripts/lib/worker_permissions.py::build_claude_scope_args`](../../scripts/lib/worker_permissions.py).

## Tests

[tests/test_worker_permissions.py](../../tests/test_worker_permissions.py), [tests/test_worker_permission_enforcement_flag.py](../../tests/test_worker_permission_enforcement_flag.py).

## Decisions

[ADR-012](../governance/decisions/ADR-012-hybrid-interactive-headless.md), [ADR-057](../governance/decisions/ADR-057-provider-agnostic-permission-policy.md).

## Flag

`VNX_ENFORCE_WORKER_PERMISSIONS`
