# claude-tmux-serialization

Limits Claude dispatch concurrency with account-level exclusive file-lock slots shared by the Claude lanes. The historical `claude-tmux` serialization class holds its slot across execution and receipt handling, releases it on exit, and defaults to ten slots.

Background: [Provider lanes](../core/PROVIDER_LANES.md).

## Entry points

[`scripts/lib/dispatch_serialization.py::serialize_lane`](../../scripts/lib/dispatch_serialization.py).

## Tests

[tests/test_dispatch_serialization.py](../../tests/test_dispatch_serialization.py).

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md)

## Flag

`VNX_TMUX_MAX_CONCURRENT`
