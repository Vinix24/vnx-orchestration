# horizon-planning

The opt-in roadmap autopilot synchronizes track state, dispatches a ready feature step, or advances after the queue drains. A failed track synchronization stops progression; advancing still checks completion gates and any required human approval.

Background: [Horizon planning](../core/HORIZON_PLANNING.md).

## Entry points

[`scripts/roadmap_manager.py::RoadmapManager.autopilot_tick`](../../scripts/roadmap_manager.py); [`scripts/roadmap_manager.py::RoadmapManager.advance`](../../scripts/roadmap_manager.py).

## Tests

[tests/test_roadmap_manager.py](../../tests/test_roadmap_manager.py).

## Decisions

[ADR-047](../governance/decisions/ADR-047-tracks-db-enige-planbron.md), [ADR-006](../governance/decisions/ADR-006-staging-promote-human-gate.md)

## Flag

`VNX_ROADMAP_AUTOPILOT`
