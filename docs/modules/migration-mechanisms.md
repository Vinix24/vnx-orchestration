# migration-mechanisms

Catalogues six migration surfaces: SQL files, Python appliers, schema manifests, migration helpers, project-ID stamping and the migrate CLI. The inventory classifies touched tables and checks filesystem/git completeness without applying or removing migrations.

Background: [schema-first migration decision](../governance/decisions/ADR-009-schema-first-migrations.md), [migration runbook](../operations/migration-rollback-runbook.md).

## Entry points

[`scripts/lib/migration_inventory.py::build_inventory`](../../scripts/lib/migration_inventory.py), [`scripts/lib/migration_inventory.py::verify_complete`](../../scripts/lib/migration_inventory.py).

## Tests

[tests/test_migration_inventory.py](../../tests/test_migration_inventory.py), [tests/test_migration_inventory_completeness.py](../../tests/test_migration_inventory_completeness.py).

## Decisions

[ADR-007](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [ADR-009](../governance/decisions/ADR-009-schema-first-migrations.md).

## Flag

`VNX_MIGRATION_SYSTEM` — read-only registry selector, default `manifest`; consolidation remains parked.
