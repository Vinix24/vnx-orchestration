# morning-pause

Pauses firing and merging during the configured morning window while decisions remain open or unreadable. With valid configuration, the deadline releases the pause; a missing daily snapshot means no round has started.
Background: [subsystem register](../core/SUBSYSTEMS.md).

## Entry points
[`scripts/lib/morning_pause_gate.py::morning_pause_verdict`](../../scripts/lib/morning_pause_gate.py), [`scripts/lib/morning_pause.py::is_paused`](../../scripts/lib/morning_pause.py).

## Tests
[tests/test_morning_pause.py](../../tests/test_morning_pause.py), [tests/test_morning_pause_seams.py](../../tests/test_morning_pause_seams.py).

## Decisions
[ADR-095](../governance/decisions/ADR-095-de-ochtendronde.md).

## Flag
`VNX_MORNING_PAUSE_ENABLED`
