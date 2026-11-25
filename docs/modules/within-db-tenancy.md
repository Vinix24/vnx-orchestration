# within-db-tenancy

Keeps project identity in database keys and queries: tracks can share a track ID across projects, and reads select both IDs. Runtime upserts prefer the store's composite project/natural-key constraint when it exists.

Background: [tenant stamping](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [per-project stores](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md).

## Entry points

[`scripts/lib/tracks.py::get_track`](../../scripts/lib/tracks.py), [`scripts/lib/coordination_db.py::upsert_conflict_target`](../../scripts/lib/coordination_db.py).

## Tests

[tests/test_tracks_multitenant.py](../../tests/test_tracks_multitenant.py), [tests/test_migration_0034_project_id_completion.py](../../tests/test_migration_0034_project_id_completion.py).

## Decisions

[ADR-007](../governance/decisions/ADR-007-multitenant-project-id-stamping.md), [ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md).

## Flag

—
