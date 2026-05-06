# Strategic State (Layer 1)

This document describes Layer 1 of the strategic-state design, materialized
in this repo by Phase 2 wave **W-state-1**. Phase 2 of the master roadmap
delivers the full Layer-1 stack across W-state-1 through W-state-5; this
document is updated as each wave lands.

For the broader three-layer state design, see
`.vnx-data/state/PROJECT_STATE_DESIGN.md`.

## The three layers in brief

| Layer | Purpose                                                               | Lifetime               |
|------:|------------------------------------------------------------------------|------------------------|
| 1     | Strategic, durable, machine-readable plan + decisions                  | survives `/clear`      |
| 2     | Memory + cross-domain learning artifacts                               | weeks–months           |
| 3     | Runtime/transient state (receipts, events, dispatcher cooldown, etc.)  | minutes–hours          |

Layer 1 is the layer T0 needs at SessionStart so it does not have to
reconstruct context after a `/clear` boundary.

## What W-state-1 delivers

The `scripts/lib/strategy` Python package provides typed access to
`.vnx-data/strategy/roadmap.yaml`, the committed source of truth for the
multi-phase roadmap.

### Public API

```python
from scripts.lib.strategy.roadmap import (
    Roadmap, Phase, Wave, OperatorDecision,
    load_roadmap, write_roadmap, validate_roadmap,
    next_actionable_wave, dependents_of, phase_complete,
    RoadmapValidationError,
)
```

- `load_roadmap(path=None) -> Roadmap` — strict reader. Default path is
  `<repo-root>/.vnx-data/strategy/roadmap.yaml`. Raises
  `RoadmapValidationError` on schema violations. `schema_version` defaults
  to `1` if absent (backwards-compat shim).
- `write_roadmap(roadmap, path=None) -> None` — structured writer that
  serializes a `Roadmap` dataclass tree back to YAML.
- `validate_roadmap(roadmap) -> list[str]` — returns a list of error
  messages; an empty list means the roadmap is valid. Catches:
  - duplicate `wave_id` / `decision_id`
  - dangling `depends_on` to undefined waves
  - dangling `blocked_on` to undefined `od_<n>` / `td_<n>` decision IDs
  - status enum violations on waves and decisions
  - phases referencing undefined waves
  - decisions whose `blocking_waves` reference undefined waves
- `next_actionable_wave(roadmap) -> Wave | None` — returns the first wave
  in declaration order that is `planned`, has all `depends_on` waves
  `completed`, and all `blocked_on` decisions `closed`.
- `dependents_of(wave_id, roadmap) -> list[Wave]` — reverse lookup of
  `depends_on`.
- `phase_complete(phase_id, roadmap) -> bool` — true iff every wave in the
  phase has `status == 'completed'`.

### Where the YAML lives

`<repo-root>/.vnx-data/strategy/roadmap.yaml`

This file is intentionally **committed** to the repo (a `.gitignore`
exception): the roadmap is part of the project's durable knowledge.
Runtime artifacts under `.vnx-data/` (dispatches, receipts, events) remain
gitignored.

### Mutation discipline

Hand-edits to `roadmap.yaml` are still allowed for now (the file is
human-readable and reviewable). Programmatic mutation, however, must
always go through `write_roadmap()` so the schema is enforced and the
output stays stable. **Never** write the YAML by hand from Python via
raw `dict` -> `yaml.dump`; future tooling expects the dataclass round-trip.

### Comment preservation

The YAML round-trip helper (`scripts/lib/strategy/_yaml_io.py`) prefers
`ruamel.yaml` when it is installed (then comments and key ordering survive
a load → write cycle). When only PyYAML is available — the current state
of the project — comments and free-form ordering may be lost on rewrite.
A one-time stderr warning is emitted at first use to make the limitation
visible.

## What W-state-1 does *not* do

- It does not mutate `roadmap.yaml`. The committed file is read-only for
  this PR; only future wave helpers (mark-completed, append-history) will
  mutate it through `write_roadmap()`.
- It does not provide a CLI. `vnx status` (W-UX-3) is the operator-facing
  surface.
- It does not migrate `schema_version`. Only `1` is supported; future
  schema versions will need an explicit migration path.

## Forward look

- **W-state-2**: append-only `decisions.ndjson` writer, file-locked, with
  decision IDs `OD-YYYY-MM-DD-NNN` (operator) and `TD-YYYY-MM-DD-NNN`
  (T0).
- **W-state-3**: rewrite of `scripts/build_current_state.py` to consume
  the typed roadmap module and the decisions tail. Replaces the
  W-UX-2 quick projector with a schema-stable markdown renderer.
- **W-state-4**: `prd_index.json` and `adr_index.json` builders for the
  `docs/prds/` and `docs/adrs/` artifacts.
- **W-state-5**: extension of `scripts/build_t0_state.py` so the
  `strategic_state` block surfaces directly to T0 at SessionStart.
  Feature-end gate; high-blast-radius wave.

## Quick smoke test

```bash
python3 -c "
from scripts.lib.strategy.roadmap import load_roadmap, validate_roadmap, next_actionable_wave
r = load_roadmap()
errs = validate_roadmap(r)
assert errs == [], errs
print('next:', next_actionable_wave(r).wave_id)
"
```

If this prints the next-actionable wave id with no exception, Layer 1's
read path is wired up correctly.
