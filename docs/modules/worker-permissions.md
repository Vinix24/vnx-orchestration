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

`VNX_ENFORCE_WORKER_PERMISSIONS` resolves override > project row > global config > env > default (off). Stored rows beat exported values; use `VNX_OVERRIDE_ENFORCE_WORKER_PERMISSIONS` for one process. The Python hook binds the supplied `VNX_STATE_DIR`, otherwise `VNX_DATA_DIR/state` (or local `.vnx-data/state`), without git discovery. Audit and report-directory checks reuse those supplied paths. Unreadable wired config retains enforcement; `VNX_WORKER_ENFORCEMENT_SKIP` remains the per-dispatch opt-out.
