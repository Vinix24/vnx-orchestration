# Feature: Phase 2 — Strategic State Foundation (Layer 1)

**Status**: Draft
**Priority**: P0
**Branch**: feature/phase-02-strategic-state-foundation
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate

Primary objective:
Stand up Layer 1 of the strategic-state design (PROJECT_STATE_DESIGN.md): a typed roadmap module, an append-only decisions log, the formal current_state.md projector, derived doc indexes, and a build_t0_state extension that surfaces strategic state to T0 at SessionStart. After Phase 2, T0 has machine-readable, durable knowledge of plans, decisions, and current focus across `/clear` boundaries.

## Dependency Flow
```text
W-state-1 (deps: W-UX-1 from Phase 0)
W-state-1 -> W-state-2
W-state-1 -> W-state-4
W-state-1, W-state-2 -> W-state-3
W-state-1, W-state-2, W-state-3 -> W-state-5  (feature-end gate)
```

## W-state-1: roadmap.yaml schema + reader/writer Python module
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [W-UX-1]

### Description
Introduce `scripts/lib/strategy/roadmap.py`: typed dataclasses (`Phase`, `Wave`, `OperatorDecision`), a strict reader (`load_roadmap()`), a structured writer (`write_roadmap()` with PyYAML round-trip preserving comments where possible), and a validator (`validate_roadmap()`) enforcing the schema in `roadmap.yaml`. Foundational — everything downstream binds to this module's types.

### Scope
- In: dataclass schema, loader, writer, validator, derived helpers (`next_actionable_wave()`, `dependents_of()`, `phase_complete?()`).
- In: backwards-compat shim — if `schema_version` is absent, treat as 1.
- Out: hand-edits to roadmap.yaml itself; mutation flows are append/edit via well-defined helpers, never raw dict-write.

### Files to Create / Modify
- `scripts/lib/strategy/__init__.py` — package marker.
- `scripts/lib/strategy/roadmap.py` — schema + IO (~180 LOC).
- `scripts/lib/strategy/_yaml_io.py` — round-trip helper using ruamel.yaml only if already vendored; otherwise PyYAML with documented limitation that comments may not round-trip.
- `tests/test_strategy_roadmap.py` — schema parse, validation rejection, round-trip stability, derived helpers.
- `docs/architecture/STRATEGIC_STATE.md` — short reference doc (~100 LOC) explaining Layer 1.

### Success Criteria
- [ ] `load_roadmap()` parses the committed `.vnx-data/strategy/roadmap.yaml` without errors
- [ ] `validate_roadmap()` catches: missing `schema_version`, dangling `depends_on`, dangling `blocked_on` to undefined waves, status enum violations, duplicate wave_ids
- [ ] `next_actionable_wave()` returns deterministic result on a fixture roadmap
- [ ] Round-trip (`load → write → load`) yields equivalent dataclass tree

### Test Plan
**Unit tests:**
- `tests/test_strategy_roadmap.py` — covers: load valid, reject duplicate wave_id, reject dangling depends_on, derived `next_actionable_wave()` honors `depends_on` and `blocked_on`, round-trip stable, schema_version default.

**Integration tests:**
- `tests/test_strategy_roadmap_real_file.py` — load committed `.vnx-data/strategy/roadmap.yaml` and confirm zero validation errors.

**Smoke test:**
- `python3 -c "from scripts.lib.strategy.roadmap import load_roadmap; r = load_roadmap(); print(r.next_actionable_wave().wave_id)"`

**Coverage target:** 80%

### Quality Gate
`gate_phase02_w_state_1_roadmap_module`:
- [ ] All schema validations covered by tests
- [ ] Round-trip test passes (`load → write → load` equivalence)
- [ ] Real-file test passes against committed roadmap.yaml
- [ ] No regression on Phase 0 build_current_state.py (it can adopt the module optionally, no break)

## W-state-2: decisions.ndjson append-only log + writer
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 6 hours
**Dependencies**: [W-state-1]

### Description
Add `scripts/lib/strategy/decisions.py`: append-only NDJSON writer with file-locking (reuse `dispatch_register` pattern), strict schema validation, and a tail-reader. Decision IDs follow `OD-YYYY-MM-DD-NNN` (operator) and `TD-YYYY-MM-DD-NNN` (T0) per design.

### Scope
- In: writer (`record_decision`), tail-reader (`recent_decisions(n=10)`), schema validation, supersedes-chain helper.
- In: file-locking via `fcntl.flock` so concurrent writers (T0 + a background hook) cannot interleave.
- Out: cross-project query; centralized DB sync (Phase 12 territory).

### Files to Create / Modify
- `scripts/lib/strategy/decisions.py` — writer + reader (~140 LOC).
- `tests/test_strategy_decisions.py` — append, lock contention simulation, tail order, supersedes chain.
- `.vnx-data/strategy/decisions.ndjson` — created with empty file on first write (no schema header).

### Success Criteria
- [ ] `record_decision(...)` appends a valid line and returns the assigned decision_id
- [ ] Concurrent writes do not corrupt the file (lock test passes)
- [ ] `recent_decisions(n=10)` returns last N entries in chronological order
- [ ] Schema rejection on missing required fields (kind, actor, summary)

### Test Plan
**Unit tests:**
- `tests/test_strategy_decisions.py` — covers: append valid, reject missing fields, reject unknown `kind`, supersedes chain resolves, decision_id auto-numbers per day, tail reader honors `n`.

**Integration tests:**
- `tests/test_strategy_decisions_concurrency.py` — spawn 2 processes that each append 50 entries; verify 100 valid lines and zero corruption.

**Smoke test:**
- `python3 -c "from scripts.lib.strategy.decisions import record_decision; print(record_decision(kind='t0_decision', actor='T0', summary='smoke test', scope='project'))"`
- `tail -1 .vnx-data/strategy/decisions.ndjson | python3 -m json.tool`

**Coverage target:** 80%

### Quality Gate
`gate_phase02_w_state_2_decisions_log`:
- [ ] Concurrency test passes (no corruption under contention)
- [ ] Schema rejection covers all required fields
- [ ] Decision-id auto-numbering deterministic per day

## W-state-3: current_state.md auto-projector (formal version, replaces W-UX-2)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: [W-state-1, W-state-2]

### Description
Replace the W-UX-2 quick-projector with a typed projector that consumes `roadmap.py` dataclasses + `decisions.py` tail-reader + existing runtime sources (`t0_state.json`, `gh pr list`, `open_items_digest.json`). Schema-stable markdown sections matching PROJECT_STATE_DESIGN §4.2: Mission, Current focus, Roadmap snapshot, In flight, Last 3 decisions, Resume hints.

### Scope
- In: rewrite `scripts/build_current_state.py` to consume the typed module; preserve idempotency contract from W-UX-2.
- In: structured "Recommended next move" computed via `next_actionable_wave()`.
- In: keep the SessionEnd + post-merge hooks wired by W-UX-2 (no hook re-wire).
- Out: any UI / CLI change (those live in W-UX-3 / future waves).

### Files to Create / Modify
- `scripts/build_current_state.py` — rewrite to use typed module (~210 LOC total).
- `tests/test_build_current_state_v2.py` — section-by-section assertions, idempotency, deterministic ordering when 2 decisions share a timestamp.
- `docs/architecture/STRATEGIC_STATE.md` — append "current_state.md schema" section.

### Success Criteria
- [ ] Output schema matches PROJECT_STATE_DESIGN §4.2 (all six sections present)
- [ ] Idempotent (run twice → byte-identical)
- [ ] Runtime <2s on populated repo
- [ ] Backward compatible: a project with no `decisions.ndjson` still produces valid output (Last 3 decisions section says "none")
- [ ] "Recommended next move" derived from `next_actionable_wave()` matches manual inspection on fixture roadmap

### Test Plan
**Unit tests:**
- `tests/test_build_current_state_v2.py` — covers: every section appears, idempotent across two runs, missing decisions.ndjson handled, missing roadmap.yaml degrades gracefully, deterministic decision ordering on timestamp tie (use decision_id as secondary sort).

**Integration tests:**
- `tests/test_build_current_state_v2_integration.py` — invoke from SessionEnd hook simulation; assert file written under 2s.

**Smoke test:**
- `python3 scripts/build_current_state.py && wc -l .vnx-data/strategy/current_state.md` → ≤200 lines
- `diff <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md) <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md)` → empty

**Coverage target:** 80%

### Quality Gate
`gate_phase02_w_state_3_projector_v2`:
- [ ] All six sections present in output
- [ ] Idempotent (byte-identical on consecutive runs)
- [ ] Runtime <2s
- [ ] Graceful degradation when decisions.ndjson is empty

## W-state-4: prd_index.json + adr_index.json
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 4 hours
**Dependencies**: [W-state-1]

### Description
Add catalogs of PRD and ADR artifacts: `prd_index.json` and `adr_index.json` under `.vnx-data/strategy/`. A small builder script walks `docs/prds/` and `docs/adrs/` (after the Phase-1 → docs/ moves stipulated by PROJECT_STATE_DESIGN §5) and materializes a stable JSON index keyed by id with `{path, version, status, supersedes}`.

### Scope
- In: builder script `scripts/build_doc_indexes.py`, schema, validator, hook into the same SessionEnd path that already runs the projector.
- In: handle both `docs/prds/` and `docs/adrs/`; tolerate missing dirs.
- Out: actually moving PRDs out of `claudedocs/` (separate operational task; this wave only consumes the new `docs/` location once content is moved).

### Files to Create / Modify
- `scripts/build_doc_indexes.py` — index builder (~80 LOC).
- `scripts/lib/strategy/doc_indexes.py` — readers used by other tooling (~30 LOC).
- `tests/test_doc_indexes.py` — synthesize fixture docs, build index, assert schema.

### Success Criteria
- [ ] `prd_index.json` lists every file under `docs/prds/` with `id`, `path`, `version`, `status`
- [ ] Same for `adr_index.json` against `docs/adrs/`
- [ ] Missing source dir produces empty (not crashing) index
- [ ] Index builder hooked into SessionEnd alongside `build_current_state.py`

### Test Plan
**Unit tests:**
- `tests/test_doc_indexes.py` — covers: empty source dir, multiple PRDs with varying frontmatter, status enum (`draft|active|superseded`), supersedes chain.

**Smoke test:**
- `python3 scripts/build_doc_indexes.py && python3 -m json.tool .vnx-data/strategy/prd_index.json`

**Coverage target:** 75%

### Quality Gate
`gate_phase02_w_state_4_doc_indexes`:
- [ ] Both indexes parseable JSON
- [ ] Empty-source-dir does not crash
- [ ] Hook integration verified

## W-state-5: build_t0_state.py extension to load strategy/
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [W-state-1, W-state-2, W-state-3]

### Description
Extend `scripts/build_t0_state.py` with a `_build_strategic_state` builder that reads the strategy/ folder and surfaces it under `t0_state.json.strategic_state` plus a heavy-detail file `t0_detail/strategic_state.json`. This is the boot-path integration: T0 sees strategy as part of its session-start situational awareness without any new mechanism. Feature-end gate — high blast radius (touches the hot boot path), so all three reviewers run.

### Scope
- In: new builder in `build_t0_state.py`, addition to `_DETAIL_SECTION_MAP`, defensive fallbacks if strategy/ is absent.
- In: budget guard — strategic_state build must not add >200ms to total `build_t0_state` runtime.
- Out: T0's prompt-injection of strategic_state (T0's CLAUDE.md / orchestrator skill changes are out of scope — they read the existing t0_state.json with no contract change).

### Files to Create / Modify
- `scripts/build_t0_state.py` — add `_build_strategic_state()` + map entry (~80 LOC delta).
- `scripts/lib/strategy/loaders.py` — small `load_strategy_for_boot()` wrapper batching reads (~30 LOC).
- `tests/test_build_t0_state_strategy.py` — happy path, missing strategy/, malformed roadmap.yaml degrades gracefully, budget guard.
- `docs/architecture/STRATEGIC_STATE.md` — append "Boot loader integration" section.

### Success Criteria
- [ ] `t0_state.json` contains `strategic_state.available=true` after build on a project with strategy/
- [ ] `t0_state.json.strategic_state` includes: `current_focus`, `next_actionable_wave_id`, `recent_decisions[≤5]`, `available_indexes`
- [ ] `t0_detail/strategic_state.json` includes the heavy version (full roadmap, last 20 decisions)
- [ ] Missing strategy/ → `available=false`, no crash
- [ ] Total build_t0_state runtime regression <200ms (measured on fixture)

### Test Plan
**Unit tests:**
- `tests/test_build_t0_state_strategy.py` — covers: full happy path, missing strategy/ folder, malformed roadmap.yaml, oversized decisions log truncation, budget guard.

**Integration tests:**
- `tests/test_build_t0_state_strategy_integration.py` — full `build_t0_state` run against a fixture project, measure delta runtime, assert <200ms.

**Smoke test:**
- `python3 scripts/build_t0_state.py && python3 -c "import json; s=json.load(open('.vnx-data/state/t0_state.json'))['strategic_state']; print(s['next_actionable_wave_id'])"`

**Coverage target:** 80%

### Quality Gate
`gate_phase02_w_state_5_build_t0_state` (feature-end gate):
- [ ] `strategic_state` block present in `t0_state.json`
- [ ] Heavy-detail file emitted
- [ ] Runtime regression <200ms measured
- [ ] Graceful degradation on missing/malformed inputs
- [ ] Feature-end codex_gate: all blocking findings resolved
- [ ] Feature-end claude_github_optional: no unaddressed high-severity comments
