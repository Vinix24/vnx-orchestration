# cheap-recon-scout

Runs an optional reconnaissance pass before the dispatch door claims work, writing a sidecar of candidate code references and a plan sketch. It reuses a sidecar for the same instruction hash and treats scout failures as non-blocking.

Background: [Scout pre-pass](../intelligence/SCOUT_PREPASS.md).

## Entry points

[`scripts/lib/scout_prepass.py::maybe_run_scout`](../../scripts/lib/scout_prepass.py).

## Tests

[tests/test_scout_prepass_producer.py](../../tests/test_scout_prepass_producer.py).

## Decisions

none

## Flag

`VNX_SCOUT_PREPASS`
