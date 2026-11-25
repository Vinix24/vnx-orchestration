# within-db-tenancy

Keeps project identity in database keys and queries: tracks can share a track ID across projects, and reads select both IDs. Runtime upserts prefer the store's composite project/natural-key constraint when it exists.

Background: [tenant stamping](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [per-project stores](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md).

Coordination event writes, lease/event reads and terminal projections use
`resolve_stamp_project_id`: a valid explicit `project_id` wins; otherwise the
opened database's layout, marker and environment sources must agree. Without
a source they raise `TenantUnresolved`, never select a default project. Legacy
tables without `project_id` retain their existing behavior. At the schema-init
and live-work boundaries, `build_t0_state_hook` continues without the refused
operation, logging ERROR with the hook name and recording `project_id_unresolved`
under context `coordination_db` in the store's `skip_metrics.ndjson`.

## Entry points

[`scripts/lib/tracks.py::get_track`](../../scripts/lib/tracks.py), [`scripts/lib/coordination_db.py::upsert_conflict_target`](../../scripts/lib/coordination_db.py).

## Tests

[tests/test_tracks_multitenant.py](../../tests/test_tracks_multitenant.py), [tests/test_migration_0034_project_id_completion.py](../../tests/test_migration_0034_project_id_completion.py).

## Decisions

[ADR-007](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md).

## Flag

—
