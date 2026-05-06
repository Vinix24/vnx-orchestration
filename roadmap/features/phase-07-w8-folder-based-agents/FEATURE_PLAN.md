# Feature: W8 Folder-Based Agents + Agent Registry (FR-4 + FR-12)

**Status**: Draft
**Priority**: P0
**Branch**: `feature/phase-07-w8-folder-based-agents`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate

Primary objective:
Replace today's prompt-injected skill model (`subprocess_dispatch_internals/skill_injection.py:228-247`) with a folder-based agent layout that mirrors Claude Code's native subagent pattern. Each agent (orchestrator or worker) becomes a self-contained folder under `.claude/agents/{orchestrators,workers}/<name>/` with `BEHAVIOR.md` (single source) plus `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` symlinks plus `permissions.yaml` plus governance/runtime/workers config plus per-agent `skills/` for invocable task templates. Layered on top: a frontmatter-driven agent registry, dispatch-time validation, and orchestrator-prompt library renderer (FR-12). The cutover is gated behind `VNX_FOLDER_AGENTS=1`; legacy injection (`VNX_FOLDER_AGENTS=0`) keeps working in parallel until W14.

Reference specs:
- `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` §5 FR-4 (folder-based agents) and §5 FR-12 (agent registry + dispatch routing)
- `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` §7.3 (folder-based loading)
- `claudedocs/2026-05-01-universal-harness-research.md` §3 (folder-based skills migration path) and §6 (workers=N hardcoded surface inventory — informs what we DO NOT touch in this feature)

Scope boundary: this feature does NOT do workers=N (W11), does NOT touch the WorkerProvider Protocol refactor (W9), does NOT remove `T0..T3` aliases. Those land in later phases. Here: folder-based loading + registry + validator + library renderer only.

## Dependency Flow
```text
w8-a (skeleton + skill migration; no dependencies on w7-a/w-state-2 because those land before Phase 7 per roadmap.yaml)
w8-a -> w8-b (governance/permissions config builds on the skeleton)
w8-b -> w8-fr12 (registry + validator + library renderer reads the schemas defined in w8-b)
```

Note: the strategy `roadmap.yaml` lists `w8-a depends_on: [w7-a, w-state-2]`. Those Phase 3/Phase 4 dependencies are upstream; within Phase 7 itself, the chain is `w8-a -> w8-b -> w8-fr12`.

## Cross-cutting backward-compat requirement (applies to all 3 sub-PRs)

Per PRD FR-4.7 and the universal-harness research §4.6, the legacy `_inject_skill_context()` path in `subprocess_dispatch_internals/skill_injection.py:228-247` MUST keep working until **W14** (a future cutover wave). Concretely:

- `VNX_FOLDER_AGENTS=0` (default during this feature) -> dispatcher uses the legacy injection path; existing skill-name -> `SKILL.md` resolution unchanged
- `VNX_FOLDER_AGENTS=1` (opt-in) -> dispatcher uses the folder-based path with registry + validator
- Both code paths exercised in CI; identical receipt shape across both paths
- The `.claude/skills/` symlink stays intact and points at `.vnx/skills/` until W14

This is the **non-negotiable backward-compat contract** for Phase 7. Every test plan below includes a "VNX_FOLDER_AGENTS=0 still works" assertion.

## w8-a: `.claude/agents/` Skeleton + Migrate 8 Existing Skills to BEHAVIOR.md + Symlinks
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 week (~400 LOC: skeleton + 8 BEHAVIOR migrations + symlink helper + idempotent migrator)
**Dependencies**: [w7-a, w-state-2]

**Model justification (Sonnet):** Mechanical migration. The 8 existing `.claude/skills/<role>/SKILL.md` files become `.claude/agents/workers/<role>/BEHAVIOR.md` with `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` symlinks. The migration logic is well-specified by PRD FR-4.7. No design decisions remain. Sonnet is sufficient.

### Description
Create the `.claude/agents/{orchestrators,workers}/` skeleton. Migrate the 8 existing `.claude/skills/<role>/SKILL.md` files to `.claude/agents/workers/<role>/BEHAVIOR.md` (per PRD FR-4.7: today's `.claude/skills/<role>/SKILL.md` is a naming collision — it contains *behavior* not *invocable skill*). Create `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` provider-named symlinks pointing at `BEHAVIOR.md` for each agent. Implement idempotent boot helper `scripts/lib/agent_folder_loader.py` that ensures the symlink trio exists for every agent folder containing a `BEHAVIOR.md` (per PRD FR-4.6). Legacy `.claude/skills/` symlink stays as backward-compat fallback.

### Scope
- Folder skeleton `.claude/agents/orchestrators/` and `.claude/agents/workers/`
- Migration script `scripts/migrate_w8a_skills_to_agents.py` — copies each existing `SKILL.md` -> `BEHAVIOR.md` and creates the 3 symlinks
- Boot helper `scripts/lib/agent_folder_loader.py` — idempotent symlink creation
- Renamed agents (target list, all under `.claude/agents/workers/`):
  - `backend-developer/`
  - `frontend-developer/`
  - `test-engineer/`
  - `quality-engineer/`
  - `reviewer/`
  - `architect/`
  - `debugger/`
  - `data-analyst/`
- Each gets `BEHAVIOR.md` with provider-name symlinks; `permissions.yaml` deferred to w8-b
- `t0-orchestrator` skill migrated to `.claude/agents/orchestrators/t0/BEHAVIOR.md`
- Backward-compat: `.claude/skills/` symlink retained; legacy injection path unchanged

### Files to Create/Modify
- **Create:** `.claude/agents/orchestrators/t0/BEHAVIOR.md` (migrated from `t0-orchestrator` skill)
- **Create:** `.claude/agents/orchestrators/t0/{CLAUDE,AGENTS,GEMINI}.md` (symlinks -> BEHAVIOR.md)
- **Create (×8 workers):** `.claude/agents/workers/<role>/BEHAVIOR.md` + `{CLAUDE,AGENTS,GEMINI}.md` symlinks
- **Create:** `scripts/migrate_w8a_skills_to_agents.py` (~150 LOC; idempotent migrator)
- **Create:** `scripts/lib/agent_folder_loader.py` (~80 LOC; boot-time symlink ensurer + idempotent repair)
- **Modify:** `subprocess_dispatch_internals/skill_injection.py` (+30 LOC; gated branch — if `VNX_FOLDER_AGENTS=1`, prefer `BEHAVIOR.md`; else legacy path unchanged)
- **No modification:** `.claude/skills/` symlink retained (legacy fallback)

### Success Criteria
- 9 agent folders created (8 workers + 1 orchestrator stub) each with `BEHAVIOR.md` + 3 symlinks
- `scripts/lib/agent_folder_loader.py` is idempotent — running it 3 times in a row makes zero filesystem changes after the first run
- Migration script is idempotent — running it twice does NOT duplicate or corrupt files
- Legacy injection path still works: with `VNX_FOLDER_AGENTS=0`, every existing test passes unchanged
- Folder-based path works: with `VNX_FOLDER_AGENTS=1`, dispatcher reads `BEHAVIOR.md` from agent folder; receipt content matches injection-path output for the same dispatch

### Test Plan
- **Unit:**
  - `test_agent_folder_loader_create.py` — given empty folder with only `BEHAVIOR.md`, helper creates 3 symlinks
  - `test_agent_folder_loader_idempotent.py` — re-running on already-symlinked folder is no-op
  - `test_agent_folder_loader_repair.py` — broken/stale symlink (points elsewhere) is repaired to `BEHAVIOR.md`
  - `test_migrate_idempotency.py` — run migrator twice; 2nd run reports "0 changes"
- **Integration:**
  - `VNX_FOLDER_AGENTS=1` dispatch on `backend-developer` — worker prompt assembled from `.claude/agents/workers/backend-developer/BEHAVIOR.md` (verify by inspecting captured prompt in event archive)
  - Receipt shape identical between `VNX_FOLDER_AGENTS=0` and `VNX_FOLDER_AGENTS=1` for the same dispatch (ignoring the new `agent_folder` receipt field added in w8-b)
- **Smoke:**
  - `ls -la .claude/agents/workers/backend-developer/` shows BEHAVIOR.md + 3 symlinks
  - `readlink .claude/agents/workers/backend-developer/CLAUDE.md` returns `BEHAVIOR.md`
- **Migration safety test (CRITICAL — w8-a-specific):** run `scripts/migrate_w8a_skills_to_agents.py` on a clean checkout. Assert: 9 BEHAVIOR.md files created. Run it again. Assert: 0 files modified, 0 symlinks recreated, exit 0. Run a 3rd time after manually breaking one symlink. Assert: only the broken symlink is repaired.
- **Backward-compat test (CRITICAL):** `VNX_FOLDER_AGENTS=0` (legacy path):
  - All existing tests in `tests/dispatch/`, `tests/governance/`, `tests/skills/` pass unchanged
  - `subprocess_dispatch_internals/skill_injection.py:_inject_skill_context()` still resolves via `.claude/skills/<role>/CLAUDE.md`
  - Receipt content unchanged
- **Smoke test for legacy alongside folder:** flip `VNX_FOLDER_AGENTS=1` in CI; rerun the same test suite; assert green. Both paths exercised.

### Quality Gate
`gate_w8a_skeleton`:
- [ ] 9 agent folders created with correct structure (BEHAVIOR.md + 3 symlinks)
- [ ] `scripts/lib/agent_folder_loader.py` is idempotent (verified via 3-run test)
- [ ] Migration script is idempotent (verified via 2nd-run zero-change test)
- [ ] `VNX_FOLDER_AGENTS=0` path: all existing tests pass unchanged
- [ ] `VNX_FOLDER_AGENTS=1` path: end-to-end dispatch succeeds, receipt valid
- [ ] Legacy `.claude/skills/` symlink intact
- [ ] Symlink repair test green (broken symlink restored)

## w8-b: Governance + Permissions + Runtime Config Per Agent
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Medium-High (security-relevant: governance schema gates what agents can do)
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1.5 weeks (~500 LOC: 4 schemas × 9 agents + dispatcher reads + gate-stack resolver)
**Dependencies**: [w8-a]

**Model justification (Opus):** Governance and permissions schema is security-relevant. `permissions.yaml` declares allowed/denied tools per agent — getting the schema wrong (or letting a worker silently widen its permissions) is a security regression. `governance.yaml` variant declarations gate review-stack composition. Opus is required for the careful multi-schema design + the dispatcher's reading logic + the gate-stack resolver.

### Description
For every agent folder under `.claude/agents/{orchestrators,workers}/<name>/`, add four config files:
- `permissions.yaml` — allowed_tools / denied_tools (per PRD FR-4.2)
- `governance.yaml` — variant declaration (`coding-strict` OR `business-light` per FR-6) controlling required gates, auto_merge, max_risk_class, PR-size limit, blocking-findings policy
- `guardrails.yaml` — model whitelist, max risk class, gate stack
- `runtime.yaml` — provider chain (primary + fallbacks per FR-11)
- For orchestrators only: `workers.yaml` — which worker pool this orch owns
- For agents with multi-task patterns: `skills/` subfolder with `<task>.md` invocable templates (per FR-4.5)

Dispatcher reads these schemas at dispatch time. New helper `scripts/lib/gate_stack_resolver.py` (per FR-6) reads `governance.yaml` variant per dispatch and resolves the gate stack accordingly. Receipt envelope gains `agent_folder` field (per PRD §7.5).

### Scope
- 4 YAML schemas per agent folder × 9 agents = 36 config files
- 1 `workers.yaml` for the t0 orchestrator
- Dispatcher reads `permissions.yaml` (replaces today's `_inject_permission_profile`)
- `scripts/lib/gate_stack_resolver.py` — variant -> gate stack resolution (~150 LOC)
- Receipt envelope: `agent_folder` field added (additive per NFR-9)
- Schema validators: `scripts/lib/agent_config_schema.py` validates each YAML against a JSON-schema before dispatcher reads it
- Backward-compat: `VNX_FOLDER_AGENTS=0` path keeps using today's `_inject_permission_profile`; only `=1` reads the new YAMLs

### Files to Create/Modify
- **Create (×9 agents):** `.claude/agents/.../<name>/permissions.yaml` (allowed/denied tools)
- **Create (×9 agents):** `.claude/agents/.../<name>/governance.yaml` (variant + gate config)
- **Create (×9 agents):** `.claude/agents/.../<name>/guardrails.yaml` (model whitelist, risk class, gate stack)
- **Create (×9 agents):** `.claude/agents/.../<name>/runtime.yaml` (provider chain)
- **Create (orchestrators only):** `.claude/agents/orchestrators/t0/workers.yaml`
- **Create:** `scripts/lib/gate_stack_resolver.py` (~150 LOC)
- **Create:** `scripts/lib/agent_config_schema.py` (~80 LOC; JSON-schema validation for each YAML)
- **Create:** `schemas/agent-config/permissions.schema.json` (~30 LOC)
- **Create:** `schemas/agent-config/governance.schema.json` (~30 LOC)
- **Create:** `schemas/agent-config/guardrails.schema.json` (~25 LOC)
- **Create:** `schemas/agent-config/runtime.schema.json` (~25 LOC)
- **Modify:** `subprocess_dispatch_internals/skill_injection.py` (+50 LOC; gated branch reads YAMLs when `VNX_FOLDER_AGENTS=1`)
- **Modify:** `scripts/append_receipt.py` (+15 LOC; add `agent_folder` to receipt envelope)
- **Modify:** `scripts/lib/governance_helpers.py` (+30 LOC; gate_stack_resolver wired in)

### Success Criteria
- All 9 agent folders have 4 valid YAML configs (5 for orchestrators)
- Each YAML validates against its JSON-schema
- `gate_stack_resolver.py` returns the correct gate stack for each variant (`coding-strict` -> `[codex, gemini, ci_green]`; `business-light` -> `[content_review]`)
- Dispatcher (with `VNX_FOLDER_AGENTS=1`) reads `permissions.yaml` and applies allowed/denied tools to the worker spawn
- Receipt has `agent_folder` field populated
- Legacy path with `VNX_FOLDER_AGENTS=0` unchanged

### Test Plan
- **Unit:**
  - `test_gate_stack_resolver_coding_strict.py` — variant `coding-strict` -> gate stack matches PRD §5 FR-6
  - `test_gate_stack_resolver_business_light.py` — variant `business-light` -> gate stack matches FR-6
  - `test_agent_config_schema_valid.py` — every committed YAML validates against its JSON-schema
  - `test_agent_config_schema_invalid.py` — mutated YAML (extra field, wrong type) fails validation
  - `test_permissions_yaml_applies.py` — given `permissions.yaml` with `denied_tools: [Bash]`, dispatcher sets `--disallowedTools Bash` (or equivalent) on worker spawn
- **Integration:**
  - End-to-end dispatch with `VNX_FOLDER_AGENTS=1` and `governance.yaml: variant: coding-strict` — receipt shows correct gate stack
  - `permissions.yaml denied_tools` honored — worker attempts denied tool -> rejected
  - `agent_folder` field populated in receipt with the correct path
- **Smoke:**
  - `python3 -c "from scripts.lib.gate_stack_resolver import resolve; print(resolve('coding-strict'))"` returns expected list
  - `find .claude/agents -name '*.yaml' | xargs -I {} python3 scripts/lib/agent_config_schema.py {}` exits 0 for all
- **Dispatch validation test (preview of w8-fr12):** invalid `governance.yaml` (variant: unknown) -> dispatcher refuses to dispatch with structured error.
- **Backward-compat test (CRITICAL):** `VNX_FOLDER_AGENTS=0` path:
  - `_inject_permission_profile` still resolves to `<role>/permissions.yaml` via legacy path
  - All existing governance tests pass unchanged
  - Receipt's `agent_folder` field is `None` (legacy mode never populates)
- **Migration safety test:** running w8-a's migrator AFTER w8-b YAMLs are committed does NOT overwrite the YAMLs.

### Quality Gate
`gate_w8b_governance_config`:
- [ ] 36 (or 37 with `workers.yaml`) YAML configs committed and schema-valid
- [ ] `gate_stack_resolver.py` resolves variants correctly (both directions tested)
- [ ] `permissions.yaml` honored by dispatcher (denied tool rejected at runtime)
- [ ] Receipt envelope's `agent_folder` field populated when `VNX_FOLDER_AGENTS=1`
- [ ] `VNX_FOLDER_AGENTS=0` legacy path unchanged; all existing governance tests pass
- [ ] JSON-schema validation runs in CI
- [ ] No agent's `permissions.yaml` accidentally widens scope vs today's `_inject_permission_profile` baseline (regression scan)

## w8-fr12: Agent Registry + Dispatch Validator + Library Renderer
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Medium-High (security-critical: validator rejects unauthorized dispatches)
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 week (~370 LOC: registry 150 + validator 120 + library renderer 80 + receipt schema 20)
**Dependencies**: [w8-b]

**Model justification (Opus):** The dispatch validator is **security-critical** — its job is to reject unauthorized dispatches (worker not in pool, unknown agent, missing required input, provider unsupported by worker). A bug in the validator either lets bad dispatches through (security hole) or rejects valid ones (operator pain). Per PRD FR-12.5, five rejection conditions are mandatory; each needs a unit test. Opus is required.

### Description
Implement FR-12 of PRD-VNX-UH-001:
- **Registry** (`scripts/lib/agent_registry.py`): walks `.claude/agents/{orchestrators,workers}/*/BEHAVIOR.md` at startup, parses YAML frontmatter (per FR-12.1), builds in-memory + JSON-serialized registry at `.vnx-data/state/agent_registry.json` (per FR-12.2)
- **Validator** (`scripts/lib/dispatch_validator.py`): before VNX accepts a dispatch in `pending/`, validates against registry per FR-12.5 — agent resolves, in pool, inputs present, skill_ref valid, provider supported
- **Library renderer** (`scripts/lib/agent_library_renderer.py`): renders the orchestrator's worker-pool view (per FR-12.3) — frontmatter-derived summary, NOT full BEHAVIOR.md
- **Frontmatter** added to all 9 BEHAVIOR.md files (per FR-12.1) — name, description, inputs_expected, outputs, supported_providers, preferred_model, risk_class, typical_duration_minutes
- **Receipt** gains `agent_registry_version` field (per FR-12.6)

### Scope
- Registry build + read (`agent_registry.py`)
- Dispatch validator with 5 rejection conditions (`dispatch_validator.py`)
- Library renderer (`agent_library_renderer.py`)
- Frontmatter for all 9 BEHAVIOR.md files
- Receipt schema field `agent_registry_version`
- `vnx registry rebuild` operator command (force-rebuild)
- Filesystem watcher (optional; documented as deferred to W14)
- Codex gate at end of feature (FR-12 is the security-critical one)

### Files to Create/Modify
- **Create:** `scripts/lib/agent_registry.py` (~150 LOC; registry build + read + force-rebuild)
- **Create:** `scripts/lib/dispatch_validator.py` (~120 LOC; 5 rejection conditions + structured error reporting)
- **Create:** `scripts/lib/agent_library_renderer.py` (~80 LOC; frontmatter-derived summary)
- **Modify (×9 agents):** `BEHAVIOR.md` — add YAML frontmatter per FR-12.1
- **Modify:** `scripts/append_receipt.py` (+15 LOC; `agent_registry_version` field)
- **Modify:** `subprocess_dispatch_internals/skill_injection.py` (+30 LOC; gated branch — when `VNX_FOLDER_AGENTS=1`, validator runs before dispatch composition)
- **Create:** `scripts/vnx_registry_cli.py` (~25 LOC; `vnx registry rebuild` and `vnx registry list` commands)

### FR-12.5 Validation Rejection Conditions (each needs a unit test)
1. **`unknown_agent`**: `dispatch.agent` value does not resolve to a folder in the registry
2. **`worker_not_in_pool`**: orch is sub-orchestrator, but `dispatch.agent` is not in orch's `workers.yaml` pool
3. **`missing_input`**: a key from worker frontmatter's `inputs_expected` is absent in `dispatch.inputs`
4. **`unknown_skill`**: `dispatch.skill_ref` is set but does not match a file in `<agent_folder>/skills/`
5. **`provider_unsupported_by_worker`**: orch's chosen provider for the worker is not in worker's `supported_providers`

### Codex gate placement
Per the gate-placement strategy: this PR carries the **end-of-feature codex_gate** for the entire Phase 7 (W8) feature. Codex reviews the cumulative folder-based agents work — registry correctness, validator security, library renderer correctness, frontmatter consistency across 9 agents, backward-compat against legacy injection path.

### Success Criteria
- Registry built at boot from frontmatter; persisted to `.vnx-data/state/agent_registry.json`
- All 5 validator rejection conditions implemented and tested
- Library renderer produces orchestrator-prompt section per FR-12.3
- All 9 BEHAVIOR.md files have valid YAML frontmatter
- Receipt has `agent_registry_version` field
- `vnx registry rebuild` works
- Legacy `VNX_FOLDER_AGENTS=0` path unchanged
- Codex gate green at end of feature

### Test Plan
- **Unit:**
  - `test_registry_build.py` — given fixture `.claude/agents/` with 3 agents, registry builds with correct frontmatter
  - `test_registry_persist.py` — `agent_registry.json` written and re-readable
  - `test_registry_rebuild.py` — `vnx registry rebuild` regenerates the JSON; old in-memory registry replaced
  - **Dispatch validation test (CRITICAL — w8-fr12-specific, per FR-12.5; each rejection condition has its own unit test):**
    - `test_validator_unknown_agent.py` — `agent: workers/nonexistent` -> rejection with `unknown_agent`
    - `test_validator_worker_not_in_pool.py` — sub-orch dispatches to worker not in `workers.yaml` -> rejection with `worker_not_in_pool`
    - `test_validator_missing_input.py` — frontmatter requires `branch_name`; dispatch omits it -> rejection with `missing_input: branch_name`
    - `test_validator_unknown_skill.py` — `skill_ref: nonexistent` -> rejection with `unknown_skill`
    - `test_validator_provider_unsupported.py` — orch picks `gemini` but worker's `supported_providers: [claude]` -> rejection with `provider_unsupported_by_worker`
  - `test_library_renderer.py` — given registry with 3 workers, renderer produces markdown matching FR-12.3 example shape
- **Integration:**
  - End-to-end `VNX_FOLDER_AGENTS=1` dispatch — validator runs, registry consulted, library injected into orchestrator prompt, worker spawned
  - Receipt has `agent_registry_version` matching the registry's hash at dispatch time
  - Frontmatter-driven dispatch composition (per FR-12.4) — `BEHAVIOR.md` body + `## Inputs` + `## Task` (+ optional `## Active skill` between Inputs and Task)
- **Smoke:**
  - `vnx registry rebuild` exits 0 and produces `.vnx-data/state/agent_registry.json`
  - `vnx registry list` prints 9 agent rows
  - `python3 -c "from scripts.lib.agent_registry import load; print(load()['workers'].keys())"` lists 8 worker roles
- **Backward-compat test (CRITICAL):** `VNX_FOLDER_AGENTS=0` path:
  - Validator NOT invoked
  - Registry NOT loaded
  - Library renderer NOT used
  - Existing `_inject_skill_context()` resolves as before
  - All existing tests in `tests/dispatch/` pass unchanged
- **Migration safety test:** running w8-a's migrator after w8-fr12 frontmatter is added does NOT strip the frontmatter from BEHAVIOR.md files.
- **Schema/format consistency test:** every committed BEHAVIOR.md frontmatter validates against `schemas/agent-frontmatter.schema.json`. Required fields: name, description, supported_providers, preferred_model, risk_class.

### Quality Gate
`gate_w8fr12_agent_registry`:
- [ ] Registry builds from frontmatter; persisted; `vnx registry rebuild` works
- [ ] All 5 validator rejection conditions tested (one unit test per condition; FR-12.5)
- [ ] Library renderer produces correct markdown shape (FR-12.3)
- [ ] All 9 BEHAVIOR.md files have valid frontmatter; schema-validated
- [ ] Receipt `agent_registry_version` field populated when `VNX_FOLDER_AGENTS=1`
- [ ] `VNX_FOLDER_AGENTS=0` legacy path unchanged; all existing tests pass
- [ ] **Feature-end codex_gate**: codex reviews entire Phase 7 (w8-a + w8-b + w8-fr12); assesses registry correctness, validator security, library renderer correctness, frontmatter consistency, backward-compat hygiene
- [ ] Total LOC delta across Phase 7: ~1,270 (within plan envelope of 400+500+370 = 1,270)
- [ ] Schema/security regression scan: no agent's effective permissions widen vs today's `_inject_permission_profile` baseline

---

## Feature-End Quality Gate (Phase 7 cohesion)

`gate_phase07_folder_agents_complete`:
- [ ] All 3 sub-PRs merged in dependency order (w8-a -> w8-b -> w8-fr12)
- [ ] `VNX_FOLDER_AGENTS=0` (legacy injection) and `VNX_FOLDER_AGENTS=1` (folder-loaded) BOTH exercised in CI on every PR
- [ ] Migration script idempotent end-to-end (running w8-a migrator after all 3 PRs land doesn't damage anything)
- [ ] All 9 agent folders have: BEHAVIOR.md (with frontmatter), 3 provider symlinks, permissions.yaml, governance.yaml, guardrails.yaml, runtime.yaml; orchestrators additionally have workers.yaml
- [ ] Registry, validator, library renderer all wired into the dispatcher (when `VNX_FOLDER_AGENTS=1`)
- [ ] Codex final-pass gate green (run on w8-fr12 PR)
- [ ] Out of scope but documented: `.claude/skills/` symlink retention until W14; W14 cutover removes legacy injection path
