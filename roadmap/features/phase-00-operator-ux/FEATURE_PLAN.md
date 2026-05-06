# Feature: Phase 0 — Operator UX Quick Wins

**Status**: Draft
**Priority**: P0
**Branch**: feature/phase-00-operator-ux
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate

Primary objective:
Make T0 instantly aware of project state after `/clear` by introducing a per-project `strategy/` folder, an auto-projected `current_state.md`, an operator-facing `vnx status` CLI, retention hygiene for runtime state, and a `vnx init` extension that bootstraps the same setup for new projects.

## Dependency Flow
```text
W-UX-1 (no deps)
W-UX-1 -> W-UX-2
W-UX-1 -> W-UX-4
W-UX-2 -> W-UX-3
W-UX-1, W-UX-2 -> W-UX-5  (feature-end gate)
```

## W-UX-1: Bootstrap strategy/ folder + roadmap.yaml + .gitignore exception
**Track**: A
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 2 hours
**Dependencies**: []

### Description
Create the `.vnx-data/strategy/` folder skeleton, seed an authoritative `roadmap.yaml`, and add a `.gitignore` exception so strategic state is git-tracked while runtime state stays ignored. This is the foundation every later wave depends on.

### Scope
- In: create `.vnx-data/strategy/` directory, seed `roadmap.yaml`, add `.gitignore` exception for `.vnx-data/strategy/**`, add `README.md` describing the folder semantics.
- In: minimal placeholder `current_state.md` (one line: "auto-projected by W-UX-2").
- Out: any projector logic, CLI, hooks, build_t0_state changes (covered by later waves).

### Files to Create / Modify
- `.vnx-data/strategy/roadmap.yaml` — authoritative copy (already exists; this wave validates and locks it).
- `.vnx-data/strategy/README.md` — explains strategy/ vs state/ boundary.
- `.vnx-data/strategy/current_state.md` — placeholder.
- `.gitignore` — add `!.vnx-data/strategy/` and `!.vnx-data/strategy/**` after the existing `.vnx-data/` ignore.
- `tests/test_strategy_bootstrap.py` — verify folder exists, gitignore exception works, roadmap.yaml is parseable.

### Success Criteria
- [ ] `.vnx-data/strategy/` is git-tracked while `.vnx-data/state/` remains ignored
- [ ] `roadmap.yaml` parses with PyYAML and matches schema_version 1
- [ ] `git status` shows strategy/ files; runtime state stays hidden
- [ ] Worktree clones include strategy/ on next `git pull`

### Test Plan
**Unit tests:**
- `tests/test_strategy_bootstrap.py` — covers: directory exists, roadmap.yaml parses, gitignore allows strategy paths, gitignore still blocks state paths.

**Smoke test:**
- `python3 -c "import yaml; yaml.safe_load(open('.vnx-data/strategy/roadmap.yaml'))"` exits 0
- `git check-ignore -v .vnx-data/state/t0_state.json` returns ignored
- `git check-ignore -v .vnx-data/strategy/roadmap.yaml` returns NOT ignored

**Coverage target:** 80%

### Quality Gate
`gate_phase00_w_ux_1_bootstrap`:
- [ ] strategy/ folder exists and is git-tracked
- [ ] roadmap.yaml parses and matches schema_version 1
- [ ] .gitignore exception verified by `git check-ignore`
- [ ] No regression on existing `.vnx-data/state/` ignore behavior

## W-UX-2: current_state.md auto-projector + retire vestigial state files
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 4 hours
**Dependencies**: [W-UX-1]

### Description
Implement `scripts/build_current_state.py` that reads roadmap.yaml + open PRs (`gh pr list`) + last-N receipts + open_items_digest + (if exists) decisions.ndjson and emits an idempotent ≤200-line `current_state.md`. Wire to SessionEnd + post-merge hooks. Archive 4 vestigial state files.

### Scope
- In: projector script, SessionEnd hook wiring, post-merge hook wiring, archival of `STATE.md`, `PROJECT_STATUS.md`, `HANDOVER_2026-04-28.md`, `HANDOVER_2026-04-28-evening.md`.
- In: idempotency guarantee (no live timestamps in body; one "Last updated" line only).
- Out: schema-formal projector with full Layer 1 fields (W-state-3 supersedes this).
- Out: cross-project federation.

### Files to Create / Modify
- `scripts/build_current_state.py` — projector (~120 LOC).
- `.claude/settings.json` — add SessionEnd hook entry.
- `scripts/lib/receipt_processor/rp_dispatch.sh` — append projector call after lease release.
- `.vnx-data/state/_archive/README.md` — deprecation notice for archived files.
- `tests/test_build_current_state.py` — empty roadmap, all-completed roadmap, in-progress + blocked OD, idempotence.

### Success Criteria
- [ ] Running projector twice produces byte-identical output
- [ ] End-to-end runtime <2s on a populated repo
- [ ] Markdown ≤200 lines and human-scannable in <30s
- [ ] SessionEnd hook fires without breaking unrelated paths
- [ ] 4 vestigial files moved to `_archive/`; nothing in tracked code references them (verified via grep)

### Test Plan
**Unit tests:**
- `tests/test_build_current_state.py` — covers: projector handles empty roadmap, all-complete roadmap, in-progress wave with blocking OD; idempotence (run twice → diff empty); failed gh CLI degrades gracefully.

**Integration tests:**
- `tests/test_build_current_state_integration.py` — SessionEnd hook invokes projector successfully under a temp `.claude/settings.json`.

**Smoke test:**
- `python3 scripts/build_current_state.py && wc -l .vnx-data/strategy/current_state.md` → ≤200 lines
- `diff <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md) <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md)` → empty

**Coverage target:** 80%

### Quality Gate
`gate_phase00_w_ux_2_projector`:
- [ ] Projector idempotent across two consecutive runs
- [ ] Runtime <2s on operator laptop
- [ ] SessionEnd + post-merge hooks fire without side effects
- [ ] Archived files have no remaining tracked references

## W-UX-3: vnx status CLI dashboard
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 3 hours
**Dependencies**: [W-UX-2]

### Description
Add a `vnx status` subcommand that prints a colorized 1-screen dashboard composed from `current_state.md` + live `t0_state.json` (queue counts, terminal idle/busy, pending dispatches). Read-only; no state mutation.

### Scope
- In: `bin/vnx status` subcommand wired into existing CLI dispatcher; uses existing libraries (no new deps).
- In: optional `--json` flag for scripting.
- Out: a new TUI; `--json` is the only programmatic surface.

### Files to Create / Modify
- `bin/vnx` — add `status` subcommand entry.
- `scripts/cli/vnx_status.py` — implementation (~80 LOC).
- `tests/test_vnx_status.py` — output sanity, `--json` schema.

### Success Criteria
- [ ] `vnx status` prints in <1s
- [ ] Output contains: current focus line, top 3 active waves, top 3 open PRs, terminal status, last 3 decisions
- [ ] `vnx status --json` emits parseable JSON
- [ ] No write side effects (verified by file mtime check pre/post)

### Test Plan
**Unit tests:**
- `tests/test_vnx_status.py` — covers: missing strategy/ falls back gracefully; missing t0_state.json falls back gracefully; `--json` schema stable.

**Smoke test:**
- `vnx status` exits 0
- `vnx status --json | python3 -m json.tool` exits 0

**Coverage target:** 70%

### Quality Gate
`gate_phase00_w_ux_3_status_cli`:
- [ ] `vnx status` exits 0 on a fresh checkout
- [ ] Read-only (no mutated files post-execution)
- [ ] `--json` output schema documented in CLI help

## W-UX-4: GC retention policy in build_t0_state.py
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 2 hours
**Dependencies**: [W-UX-1]

### Description
Add a retention policy to `scripts/build_t0_state.py` that prunes oversized `t0_detail/*.json` files older than N days (default 14) at SessionStart. Bounded GC keeps the boot-path snapshot lean.

### Scope
- In: retention sweep helper invoked at the end of `build_t0_state` after the snapshot writes.
- In: env-var override `VNX_T0_DETAIL_RETENTION_DAYS` (default 14).
- Out: retention for receipts/dispatch register (those have separate ring-buffer policies).

### Files to Create / Modify
- `scripts/build_t0_state.py` — append `_gc_t0_detail()` helper + call at end of main path.
- `tests/test_t0_state_gc.py` — verify cutoff respected, dry-run mode safe.

### Success Criteria
- [ ] Files older than retention window are removed
- [ ] Files inside retention window are untouched
- [ ] `VNX_T0_DETAIL_RETENTION_DAYS=0` disables GC entirely (escape hatch)
- [ ] No regression on existing `t0_state.json` build path

### Test Plan
**Unit tests:**
- `tests/test_t0_state_gc.py` — covers: old file removed, fresh file retained, env-var disables GC, GC is idempotent.

**Smoke test:**
- `python3 scripts/build_t0_state.py && ls .vnx-data/state/t0_detail/ | wc -l` → reasonable count
- `find .vnx-data/state/t0_detail/ -mtime +14 -name '*.json'` → empty after run

**Coverage target:** 80%

### Quality Gate
`gate_phase00_w_ux_4_gc_retention`:
- [ ] Old files removed; fresh files retained
- [ ] Env-var escape hatch works
- [ ] No regression on existing build_t0_state suite

## W-UX-5: vnx init strategy + agent folders bootstrap
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
**Dependencies**: [W-UX-1, W-UX-2]

### Description
Extend `vnx init <project>` to also create `.vnx-data/strategy/` (roadmap.yaml + decisions.ndjson + current_state.md skeleton), seed an orchestrator BEHAVIOR.md template, and prompt the operator for project type (`code|content|sales|mixed|custom`) to seed the right governance variant. This is the feature-end gate wave; touches existing init flow across multiple domains.

### Scope
- In: `vnx init` modifications, project-type prompt, governance-variant template selection, scaffolded `.claude/agents/orchestrators/main/BEHAVIOR.md`.
- In: idempotent re-run (existing strategy/ files are not overwritten).
- Out: full agent registry / dispatch validator (Phase 7 W8 territory).

### Files to Create / Modify
- `scripts/cli/vnx_init.py` — extend bootstrap flow.
- `templates/strategy/roadmap.yaml.tmpl` — minimal seed roadmap.
- `templates/strategy/current_state.md.tmpl` — skeleton.
- `templates/agents/orchestrators/main/BEHAVIOR.md.tmpl` — base persona.
- `templates/governance/<variant>/governance.yaml.tmpl` — code, content, sales, mixed.
- `tests/test_vnx_init_strategy_bootstrap.py` — fresh-repo bootstrap, idempotency, project-type branching.

### Success Criteria
- [ ] `vnx init <new-project>` creates strategy/ + agents/ scaffolding
- [ ] Re-running `vnx init` on existing project does not overwrite operator edits
- [ ] Each project type produces its declared governance variant
- [ ] No regression on the pre-existing init flow (smoke test passes)

### Test Plan
**Unit tests:**
- `tests/test_vnx_init_strategy_bootstrap.py` — covers: each project-type variant, idempotent re-run, partial-bootstrap recovery (strategy/ exists but agents/ missing).

**Integration tests:**
- `tests/test_vnx_init_e2e.py` — fresh tempdir → run init → verify all expected files + parseable yaml + gitignore exception applied.

**Smoke test:**
- `cd $(mktemp -d) && vnx init my-test-project --type=code && ls .vnx-data/strategy/` → roadmap.yaml + current_state.md present

**Coverage target:** 80%

### Quality Gate
`gate_phase00_w_ux_5_init_bootstrap` (feature-end gate):
- [ ] All four project-type variants seed the correct governance template
- [ ] Idempotent re-run safe (no clobber of operator edits)
- [ ] Existing init flow regressions = 0
- [ ] Feature-end codex_gate: all blocking findings resolved
