# State Aggregator (Wave 5 PR-5.1)

Write-pad for multi-project state updates. Provides per-project facet files,
central view JSON, and a NDJSON audit trail per ADR-005. Pairs with the
Phase 6 read-only federation aggregator (`build_central_view.py`).

## Architecture

```
Project T0 (vnx-dev)  ─┐
Project T0 (seocrawler)─┤──► StateAggregator.submit() ──► central_state.json
Control Centre         ─┘                               ──► projects/{id}.json
                                                        ──► events/state_aggregator.ndjson
```

`build_central_view.py --read-write-pad <vnx-data-dir>` reads
`central_state.json` instead of scanning SQLite DBs.

## Usage from Control Centre or project T0

```python
from pathlib import Path
from scripts.aggregator.state_aggregator import StateAggregator, ProjectStateUpdate

agg = StateAggregator(vnx_data_dir=Path(".vnx-data"))
agg.submit(ProjectStateUpdate(
    project_id="vnx-dev",
    timestamp="2026-05-16T12:34:56Z",
    event_type="dispatch_created",
    payload={"dispatch_id": "20260516-foo", "track": "A"},
    source_t0="T0-vnx-dev",
))
```

Valid `event_type` values: `dispatch_created`, `dispatch_completed`,
`t0_heartbeat`, `incident`.

## Files written

- `.vnx-data/aggregator/central_state.json` — summary per project (event counts + timestamps)
- `.vnx-data/aggregator/projects/{project_id}.json` — full facet per project (ring buffer, last 100 events)
- `.vnx-data/events/state_aggregator.ndjson` — ADR-005 audit trail (append-only)

## Read-write-pad flag in build_central_view.py

```bash
python3 scripts/aggregator/build_central_view.py \
    --read-write-pad .vnx-data \
    --json
```

Returns `central_state.json` content as JSON. Bypasses SQLite scanning.
Backwards-compatible: omitting the flag restores default Phase 6 behavior.

## Thread safety

`submit()` acquires a process-level `threading.Lock`. All three writes
(event append, facet update, central view update) execute atomically within
the lock. Facet and central view use `os.replace()` (atomic rename) so
concurrent readers never see a partial write.

## References

- ADR-017: Wave 5 Control Centre architecture
- `claudedocs/wave5-control-centre-architecture.md`: design details
- ADR-005: NDJSON audit trail invariant
