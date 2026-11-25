# git-grounded-reconcile

Derives and persists track status from dispatches, dependencies, blockers and merged-PR evidence. Merge evidence combines local event and receipt ledgers with a cached GitHub lookup rooted in the project repository.

Background: [state fabric](../core/STATE_FABRIC.md), [horizon lifecycle](../core/HORIZON_LIFECYCLE.md).

## Entry points

[`scripts/lib/track_reconciler.py::reconcile_track`](../../scripts/lib/track_reconciler.py), [`scripts/lib/track_reconciler.py::_load_merged_pr_numbers`](../../scripts/lib/track_reconciler.py).

## Tests

[tests/test_track_reconciler.py](../../tests/test_track_reconciler.py), [tests/test_objective_reconcile.py](../../tests/test_objective_reconcile.py).

## Decisions

[ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md), [ADR-047](../governance/decisions/ADR-047-tracks-db-enige-planbron.md).

## Flag

—
