# smart-router-staging

Gates AUTO routing by enabled cost tier and a deterministic SHA-256 canary bucket. Disabled tiers and control-group dispatches decline to the existing routing path; defaults enable no tiers and route no canary traffic.

Background: [Smart router design](../core/110_SMART_ROUTER_DESIGN.md).

## Entry points

[`scripts/lib/providers/smart_router/staging.py::load_staging_config`](../../scripts/lib/providers/smart_router/staging.py); [`scripts/lib/providers/smart_router/staging.py::staging_verdict`](../../scripts/lib/providers/smart_router/staging.py).

## Tests

[tests/smart_router/test_staging.py](../../tests/smart_router/test_staging.py).

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md)

## Flag

`VNX_SMART_ROUTER_CANARY_PCT`
