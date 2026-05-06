# Feature: Phase 14 / W14 — Folder-Based Agents Phase C Cutover (Remove Legacy Injection)

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w14-folder-agents-cutover`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Roadmap Wave**: w14
**Depends-On (roadmap)**: w13 (provider expansion proven over folder-agent path)

Primary objective:
Burn the bridge. Flip `VNX_FOLDER_AGENTS=1` to default, delete `_inject_skill_context()` and the rest of the legacy prompt-injection path, remove the `.claude/skills/` symlink, and run a deprecation period with structured warnings for any consumer still relying on the legacy entrypoint. After this lands, folder-loaded agents are the only dispatch path.

Per PRD §5 FR-4 / FR-12 and the W8 Phase C entry in §8.1: this is the cutover that justifies the entire folder-agent investment. It must be reversible up to the merge moment, and the legacy-removal PR carries `claude_github_optional` because deleting cross-cutting code with confidence requires a third reviewer.

## Dependency Flow
```text
PR-A (feature-flag-default-flip)              <- low-risk
PR-A -> PR-B (legacy-injection-removal)        <- the destructive step
PR-A -> PR-C (.claude-skills-symlink-removal)  <- coordinated with PR-B
PR-B -> PR-D (deprecation-period-warnings)
PR-A..PR-D -> PR-E (regression-tests)
```

## PR-A: Feature-Flag Default Flip
**Track**: A
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~30
**Dependencies**: []

### Description
Flip the default of `VNX_FOLDER_AGENTS` from `0` to `1`. Both code paths still exist; only the default changes. Any operator (or repo) that depends on the legacy path must opt out explicitly via `VNX_FOLDER_AGENTS=0` until PR-B removes the path entirely.

### Scope
- One-line change in the env-var resolver
- Update operator-facing doc snippet describing default
- Add a one-time `INFO` log when legacy path is invoked because of explicit opt-out

### Success Criteria
- `unset VNX_FOLDER_AGENTS` -> dispatcher takes folder-agent path
- `VNX_FOLDER_AGENTS=0` -> dispatcher takes legacy path (still works) AND emits the opt-out info log

### Test Plan
- **Pre-cutover assertion test**: scan receipts from the last 7 days; assert every dispatch already carries `agent_folder=<path>` (i.e., `VNX_FOLDER_AGENTS=1` was already winning de-facto). If this assertion fails, the flip is unsafe — block the PR
- **Default-resolution unit test**: with env unset, resolver returns `True`
- **Opt-out unit test**: with `VNX_FOLDER_AGENTS=0`, resolver returns `False` and emits the info log exactly once per process
- **Smoke dispatch test**: dispatch a no-op to T1; receipt carries `agent_folder` field populated

### Quality Gate
`gate_pra_default_flip`:
- [ ] Pre-cutover assertion test passes (last 7 days clean)
- [ ] Default unset = folder-agents
- [ ] Opt-out info log present and once-per-process
- [ ] Smoke dispatch produces a receipt with `agent_folder`

## PR-B: Legacy Injection Removal (`_inject_skill_context()` and Friends)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Estimated LOC**: ~120 (mostly deletes + verify-no-callers)
**Dependencies**: [PR-A]

### Description
Delete `_inject_skill_context()` from `subprocess_dispatch_internals/skill_injection.py` and its sibling helpers (`_legacy_claude_md_resolution`, any guarded `if not VNX_FOLDER_AGENTS:` branches). Verify the call graph: every caller must already route through the folder-agent loader. Remove the `VNX_FOLDER_AGENTS` env-var read; it becomes a no-op env var with a one-shot deprecation warning.

**Why Opus (deviation from default Sonnet for delete-heavy work):** confident deletion of cross-module code requires reasoning about the full call graph, governance receipt shape, and the `PromptAssembler` tri-file convention. Static `grep` is necessary but not sufficient — the architect-grade reviewer needs to confirm no dynamic dispatch (e.g. `getattr(module, name)` indirection) silently keeps the legacy path alive.

### Scope
- Delete `_inject_skill_context()`, `_legacy_claude_md_resolution()`, and guarded branches
- Remove `VNX_FOLDER_AGENTS` reads; add one deprecation warning if env var is set
- Update `PromptAssembler` to assume folder-agent input shape unconditionally
- Receipt-shape change: drop `legacy_inject_path` field where present
- Migration note in `docs/governance/decisions/` (ADR-style)

### Success Criteria
- `grep -rn "_inject_skill_context" .` returns zero hits (excluding ADR/CHANGELOG)
- `grep -rn "VNX_FOLDER_AGENTS" .` returns at most the deprecation warning site + tests
- All existing dispatches still route correctly
- No new receipt shape needed; existing folder-agent fields are sufficient

### Test Plan
- **Caller-graph test**: `grep -rn "_inject_skill_context" .` returns 0 hits in code, only allowed in `CHANGELOG.md` and ADR docs
- **Caller-graph test (dynamic)**: `grep -rn "getattr.*inject\|importlib.*skill_injection" .` returns 0 hits — guards against indirect references
- **Post-cutover backward-compat test**: a dispatch with explicit `legacy=true` flag (if such a test fixture existed) either still works (flag is documented as no-op for one release) OR fails with a clear `LEGACY_PATH_REMOVED` error pointing to the folder-agent migration doc — pick the second; document in ADR
- **Receipt-shape test**: receipts no longer contain `legacy_inject_path`; folder-agent receipt shape unchanged
- **End-to-end test**: full T1/T2/T3 dispatch round-trip green
- **Negative test**: setting `VNX_FOLDER_AGENTS=0` after this PR emits the deprecation warning and proceeds with folder-agent path anyway (env var is no-op)
- **Codex gate at PR level (in addition to wave-end)**: this PR runs codex inline because of high blast radius

### Quality Gate
`gate_prb_legacy_removal`:
- [ ] Static grep clean (no `_inject_skill_context` / no live `VNX_FOLDER_AGENTS` branches)
- [ ] Dynamic-dispatch grep clean
- [ ] Round-trip dispatches green for all three terminals
- [ ] ADR / migration note merged
- [ ] codex_gate green
- [ ] claude_github_optional executed; result recorded

## PR-C: `.claude/skills/` Symlink Removal
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~20 (mostly fs ops + docs)
**Dependencies**: [PR-A]

### Description
Remove the `.claude/skills/` -> `.vnx/skills/` symlink. Skills now live under `.claude/agents/<name>/skills/` per the W8 folder layout. A grace-period stub script lists the new locations if the old path is referenced.

### Scope
- Remove the symlink (committed as a directory removal)
- Update `vnx init` template so new projects do not recreate the symlink
- Add a `tools/migrate_legacy_skills.sh` shim that points at new locations
- Update operator-facing docs

### Success Criteria
- Fresh clone has no `.claude/skills/` directory
- `vnx init` on a fresh repo produces folder-agent layout only

### Test Plan
- **Repo-state test**: `test -L .claude/skills` returns false on fresh checkout post-merge
- **`vnx init` test**: bootstrap a tmpdir; assert no `.claude/skills` created
- **Migration-shim test**: `tools/migrate_legacy_skills.sh` prints the mapping table for the 8 known agent skills
- **Hooks-config test**: any `.claude/settings.json` reference to `.claude/skills/...` is updated or removed

### Quality Gate
`gate_prc_symlink_removal`:
- [ ] Symlink removed
- [ ] `vnx init` produces clean layout
- [ ] Migration shim present
- [ ] Hooks/settings updated

## PR-D: Deprecation Period Warnings
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~30
**Dependencies**: [PR-B]

### Description
For one release cycle, emit explicit deprecation warnings (stderr + receipt-side `deprecation_event`) when:
- `VNX_FOLDER_AGENTS` env var is set to anything (since it is a no-op now)
- `.claude/skills/` is detected at runtime (operator failed to delete after upgrade)
- An old-style dispatch payload that lacks `agent_folder` reaches the dispatcher

Each warning carries a doc link and a single-line remediation hint.

### Scope
- Three deprecation warning sites
- Receipt event shape: `{event:"deprecation", surface:"...", remediation:"..."}`
- Roll-up summary in operator digest

### Success Criteria
- Any of the three triggers produces exactly one warning per process + one receipt event
- Digest groups them into a single OI for operator follow-up

### Test Plan
- **Trigger test**: each of the three triggers fires the expected warning + receipt event
- **Idempotency test**: triggering the same surface twice in one process emits the warning once (rate-limited)
- **Digest test**: nightly digest aggregates `deprecation` events into a single open-item summary
- **No-noise test**: a clean folder-agent dispatch produces zero deprecation events

### Quality Gate
`gate_prd_deprecation_warnings`:
- [ ] All three triggers fire correctly
- [ ] Idempotent per process
- [ ] Digest aggregation works
- [ ] Clean dispatch produces no warnings

## PR-E: Regression Tests
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Estimated LOC**: ~80 (test-only)
**Dependencies**: [PR-A, PR-B, PR-C, PR-D]

### Description
The W14-final aggregator PR. Carries `claude_github_optional` because legacy-removal can break edge cases that only show up in cross-cutting integration scenarios (governance variants, cap-token attenuation, mission-level dispatch).

### Test Plan (cross-cutting)
- **Pre-cutover assertion (re-run)**: every dispatch in the last 7 days used `VNX_FOLDER_AGENTS=1`. If any dispatch used legacy, the cutover is unsafe and PR-E blocks the wave-end gate
- **Caller-graph test**: `grep -rn "_inject_skill_context" .` returns 0 hits across the entire repo
- **Dynamic-dispatch test**: no `getattr` / `importlib` / `__import__` references reach the deleted module
- **Backward-compat declaration test**: documented behavior of `VNX_FOLDER_AGENTS=0` matches actual behavior (no-op + deprecation warning)
- **Round-trip matrix test**: T0 dispatches -> T1 / T2 / T3, each via a different agent folder, each with a different provider chain (folds in W13 work)
- **Receipt-shape regression test**: pre-W14 receipts (frozen sample from a fixtures dir) and post-W14 receipts have identical core fields; only `legacy_inject_path` is gone
- **Mission-level test**: a multi-worker mission (Phase 11 W12 style if available, else simulated) completes with all workers using folder-agent path
- **Cap-token test**: an attenuated cap-token (Phase 9) still narrows scope correctly under folder-agents
- **Symlink-absence test**: `.claude/skills/` does not exist post-merge
- **Hook-integrity test**: SessionStart / SessionEnd hooks still fire correctly without the legacy path
- **Codex gate (feature-end)**: zero blocking findings on the legacy removal
- **Claude GitHub optional**: invoked because of legacy-removal blast radius

### Quality Gate
`gate_pre_w14_regression`:
- [ ] All cross-cutting tests green
- [ ] codex_gate green
- [ ] claude_github_optional executed; result recorded
- [ ] No regressions on receipt shape

## Model Assignment Justification

| PR | Model | Rationale |
|----|-------|-----------|
| PR-A flag flip | Sonnet | ~30 LOC; trivial default-toggle |
| PR-B legacy removal | **Opus** (deviation) | Cross-module deletion; static grep + dynamic-dispatch reasoning; high blast radius. The default for delete-only work is Sonnet, but "delete cross-cutting code with confidence" requires call-graph reasoning that benefits from Opus. |
| PR-C symlink removal | Sonnet | fs ops + doc updates |
| PR-D deprecation | Sonnet | Three warning sites + digest aggregation |
| PR-E regression | Sonnet | Test composition over the wave |

## Wave-End Quality Gate

`gate_w14_feature_end`:
- [ ] All 5 PR gates green
- [ ] codex_gate (feature-end) green
- [ ] claude_github_optional executed on PR-B and PR-E
- [ ] Pre-cutover assertion green (last 7 days)
- [ ] No `_inject_skill_context` references remain
- [ ] Symlink removed
- [ ] Deprecation warnings live and rate-limited
- [ ] Round-trip matrix green for T1/T2/T3 across providers landed in W13

## Notes / Risks

- **Reversibility**: PR-A is reversible (flip the default back). PR-B is **not** reversible without a revert commit — gate it carefully
- **Operator coordination**: between PR-A and PR-B (one release cycle), any external consumer who depends on the legacy path must migrate. The deprecation warning in PR-A's opt-out branch must be loud enough to surface in nightly digest
- **Worktree caveat**: T0 must not switch branches while a worker is mid-edit during this wave (per memory: shared worktree)
- **PRD §8.1 alignment**: this wave matches the W14 entry exactly; `_inject_skill_context()` removal is the load-bearing deletion
