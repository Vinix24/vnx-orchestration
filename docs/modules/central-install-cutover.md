# central-install-cutover

Checks and audits a central-install target's lag behind main before atomically replacing the `current` symlink. Measured lag over the configured threshold requires a non-empty operator reason, except during rollback; unmeasurable lag is reported as unknown and permits the flip.

Background: [Central installation runbook](../operations/install-central-runbook.md).

## Entry points

[`vnx_cli/commands/update.py::_cutover_behind_guard`](../../vnx_cli/commands/update.py); [`vnx_cli/commands/update.py::_atomic_symlink_flip`](../../vnx_cli/commands/update.py).

## Tests

[tests/test_oi1718_cutover_behind_guard.py](../../tests/test_oi1718_cutover_behind_guard.py).

## Decisions

[ADR-051](../governance/decisions/ADR-051-pin-op-runtime-en-releaseverificatie.md)

## Flag

`VNX_CUTOVER_MAX_BEHIND_COMMITS`
