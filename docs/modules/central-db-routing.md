# central-db-routing

Switches wired read sites between local per-project data, the project's central store, and shadow comparison that keeps local data authoritative. Dispatch-register reads fall back to local data when central mode lacks a project identity.

Background: [State fabric](../core/STATE_FABRIC.md).

## Entry points

[`scripts/lib/config_runtime.py::central_db_flag`](../../scripts/lib/config_runtime.py); [`scripts/lib/dispatch_register.py::_read_register_locked`](../../scripts/lib/dispatch_register.py).

## Tests

[tests/test_dispatch_register.py](../../tests/test_dispatch_register.py).

## Decisions

[ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md)

## Flag

`VNX_USE_CENTRAL_DB`
