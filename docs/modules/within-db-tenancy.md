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

The door carries the spec's project through claim, refire recovery, delivering,
accepted, running and the completed/failed_delivery outcome, including attempt
closure and every coordination event. Start refusal and slot timeout retain it
too. Legacy dispatch/attempt tables without a project column keep their ID-only
lookup while event tables with the column still receive the spec's project.
Provenance registration also
accepts `project_id`; commit reconciliation and receipt enrichment pass their
known project through to the registration event. These explicit identities
work even when the database path has no project source or the environment
names another project. Callers without an explicit identity still need a
resolvable store; tests of those callers provide a marker or environment source.
Commit reconciliation propagates `TenantUnresolved` for unresolved identity;
only git errors return an empty scan.

The dispatch broker, headless writer, PR queue, roadmap manager and dispatcher
shell also resolve against their destination store using the same contract.
Unresolved operations record ERROR `project_id_unresolved` with their component
name; missing sources name `vnx init` and `VNX_PROJECT_ID`. Existing daemon
error boundaries continue their loops; inbox processing returns a retry without
registering a dispatch. The shell refuses startup with a non-zero exit.

## Entry points

[`scripts/lib/tracks.py::get_track`](../../scripts/lib/tracks.py), [`scripts/lib/coordination_db.py::upsert_conflict_target`](../../scripts/lib/coordination_db.py).

## Tests

[tests/test_tracks_multitenant.py](../../tests/test_tracks_multitenant.py), [tests/test_migration_0034_project_id_completion.py](../../tests/test_migration_0034_project_id_completion.py).

## Decisions

[ADR-007](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md).

## Flag

—
