# Feature: Phase 11 — W12 Sub-Orchestrator Pools, Missions, And Assistant Orchestrator

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w12-sub-orchestrators`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Add a multi-tier orchestration hierarchy: operator → main-orchestrator → sub-orchestrators (tech-lead, marketing-lead, etc.) → workers. Introduce mission lifecycle (planning → active → review → done), parent_dispatch_id linkage in receipts, and an `agent_kind` discriminator that distinguishes orchestrators from workers from gates. Drives PRD-VNX-UH-001 §FR-7 (multi-tier hierarchy) and §FR-8 (session continuity). Designed against claudedocs/2026-05-01-multi-orchestrator-research.md.

## Dependency Flow
```text
PR-0 (depends on W10 cap-tokens, W11 workers=N)
PR-0 -> PR-1
PR-0 -> PR-2
PR-1, PR-2 -> PR-3
PR-3 -> PR-4
PR-4 -> PR-5
PR-5 -> PR-6
```

## PR-0: Orchestrator Adapter (WorkerProvider Subtype For Orchestration)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.25 day
**Dependencies**: []

### Description
Create `OrchestratorProvider`, a specialization of `WorkerProvider` (W9) for agents that mint child cap tokens and dispatch to subordinates. Orchestrators differ from workers in three ways: they hold cap-token signing capability (attenuated from operator root), they may spawn child dispatches, and they participate in mission lifecycle. OD-5 default is virtual subprocess; opt-in tmux pane.

### Scope
- `scripts/lib/orchestrator_provider.py` — extends WorkerProvider
- New methods: `mint_child_token(scope, caveats)`, `spawn_child_dispatch(spec)`, `enter_mission(mission_id)`, `summarize_for_continuity()`
- Capability flags: `CAN_DISPATCH`, `CAN_MINT_CHILD_TOKEN`, `CAN_HOLD_MISSION`
- Default execution mode: subprocess virtual (no tmux pane); opt-in `VNX_ORCHESTRATOR_PANE=true` for visible mode

### Success Criteria
- OrchestratorProvider isinstance of WorkerProvider
- Capability flags expose orchestration powers
- Subprocess virtual mode does not allocate tmux pane
- Opt-in tmux pane mode allocates pane and persists worker_id mapping
- Cap-token attenuation enforced at mint (cannot exceed parent scope)

### Quality Gate
`gate_pr0_orchestrator_adapter`:
- [ ] OrchestratorProvider passes WorkerProvider Protocol check
- [ ] Subprocess virtual mode runs without pane allocation
- [ ] Opt-in pane mode allocates and registers correctly
- [ ] Mint-child-token enforces attenuation (negative test)
- [ ] Capability flags accurate

## PR-1: Mission Manager (Lifecycle State Machine)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-0]

### Description
Mission is the unit of multi-dispatch coordination owned by an orchestrator. State machine: `planning → active → review → done`, with `failed` as terminal and `paused` allowed from active. Persist in `runtime_coordination.db` `missions` table. Receipts associate dispatches with mission_id. Concurrency-sensitive: a mission can have multiple in-flight child dispatches.

### Scope
- SQL migration: `missions` table (id, owner_worker_id, parent_mission_id, state, created_at, updated_at, summary_blob)
- `scripts/lib/mission_manager.py` — state-machine transitions, lock-protected
- Transition guards: only owner orchestrator can advance state; review→done requires evidence linkage
- Persistence: every state change emits a receipt event
- Pause/resume support for context-rotation (W12 FR-8)

### Success Criteria
- State machine rejects invalid transitions
- Concurrent state-change attempts produce one winner (lock-correct)
- Missions persisted across process restart
- Pause/resume preserves all child-dispatch state

### Quality Gate
`gate_pr1_mission_manager`:
- [ ] State machine matrix (every from-state × every event) tested
- [ ] Concurrent transition test: 100 racers, exactly one succeeds
- [ ] Restart resilience: state survives process kill
- [ ] Pause/resume preserves child-dispatch tracking
- [ ] Receipts emitted for every transition

## PR-2: Agent_Kind Discriminator
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
**Dependencies**: [PR-0]

### Description
Add `agent_kind` field to worker_registry, dispatches, receipts, and capability tokens. Values: `operator`, `main_orchestrator`, `sub_orchestrator`, `worker`, `gate`. Dispatcher consults agent_kind to enforce who can dispatch to whom. Cap-token scope is keyed partially on agent_kind. Receipt processor surfaces kind in audit trail. This discriminator is the routing primitive that makes the multi-tier hierarchy enforceable.

### Scope
- Schema additions to worker_registry, dispatches table, receipt NDJSON
- `AgentKind` enum and validation
- Routing rules: who can dispatch to whom (matrix in code, documented in YAML)
- Receipt processor extension to surface kind in audit ledger

### Success Criteria
- Schema migration adds field with default value (no breakage of existing rows)
- Routing rules enforce hierarchy: worker cannot dispatch, sub_orchestrator can dispatch only to its workers, main can dispatch to sub_orchestrator and workers
- Cap tokens encode agent_kind in scope
- Receipts include kind for every event

### Quality Gate
`gate_pr2_agent_kind`:
- [ ] Schema migration idempotent
- [ ] Routing rule matrix complete (operator/main/sub/worker/gate × dispatch target)
- [ ] Worker→worker dispatch rejected
- [ ] Sub_orchestrator→cross-domain worker rejected (cap-token scope mismatch)
- [ ] Receipts include agent_kind for every emitted event

## PR-3: Parent_Dispatch_Id Linkage And Multi-Tier Receipt Audit
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [PR-1, PR-2]

### Description
Add `parent_dispatch_id` to every dispatch and receipt. Builds a tree: operator dispatch → main orchestrator → sub-orchestrator → worker. Receipt processor materializes the tree for operator inspection. Sonnet appropriate — mostly mechanical schema and projection work; the architecturally novel pieces are upstream in PR-1/PR-2.

### Scope
- Schema: `parent_dispatch_id` field on dispatches and receipts
- Receipt processor: build tree projection for operator state file
- t0_state.json renders dispatch tree per active mission
- Cap-token verifier consults dispatch chain via parent_dispatch_id (defense-in-depth)

### Success Criteria
- Every dispatch records its parent (root operator dispatch has parent=None)
- Receipt tree renders correctly to operator state
- Verifier parent-dispatch-id check catches injected dispatches whose token chain looks valid but whose parent does not match (defense in depth)

### Quality Gate
`gate_pr3_parent_linkage`:
- [ ] Every dispatch records parent_dispatch_id correctly
- [ ] Tree projection renders 3-level chain
- [ ] Verifier rejects dispatch whose cap-token parent ≠ dispatch parent
- [ ] Operator state file shows tree per mission

## PR-4: Dispatch Tree Rendering (Operator UX)
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
**Dependencies**: [PR-3]

### Description
Operator-facing tree visualization in t0_state.json and a CLI command `python3 scripts/show_mission_tree.py --mission <id>`. Tree shows mission state, agent kinds, in-flight dispatches, completed receipts, gate evidence. Critical for operator situational awareness in multi-tier orchestration.

### Scope
- Tree renderer in t0_state builder
- CLI viewer with depth limit and live-refresh option
- Color-coded states (planning yellow, active green, review blue, done gray, failed red)
- Compact mode for ≥5-deep chains

### Success Criteria
- Tree renders correctly for 1-deep, 3-deep, 5-deep missions
- CLI viewer refreshes live
- Compact mode applied at depth ≥5
- Operator can identify which sub-orchestrator owns which worker visually

### Quality Gate
`gate_pr4_tree_rendering`:
- [ ] 1-deep, 3-deep, 5-deep render correctly
- [ ] Live refresh works without stale-state flicker
- [ ] Compact mode triggers at depth threshold
- [ ] Mission state colored correctly

## PR-5: Integration With Folder-Based Agents
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [PR-4]

### Description
Wire sub-orchestrators into the folder-based agent system (`.claude/agents/orchestrators/<name>/`). Each folder declares its governance variant, capability scope, allowed workers. Dispatcher consults the folder when an orchestrator dispatch is requested. Sonnet appropriate — primarily integration glue; the substantive design is in folder-agents Phase 14.

### Scope
- Folder loader extension: read `governance.yaml`, `workers.yaml`, `capabilities.yaml`
- Sub-orchestrator spawn: instantiate from folder definition
- Cap-token scope derived from folder capabilities
- Worker pool derived from `workers.yaml`

### Success Criteria
- `tech-lead` orchestrator folder loads and spawns
- `marketing-lead` orchestrator folder loads and spawns
- Cap-token scope automatically attenuates per folder definition
- Worker pool restricted per folder definition

### Quality Gate
`gate_pr5_folder_integration`:
- [ ] tech-lead folder loads and spawns sub-orchestrator
- [ ] marketing-lead folder loads and spawns sub-orchestrator
- [ ] Cap-token scope matches folder declaration
- [ ] Worker pool restricted per folder

## PR-6: End-To-End Mission Test (Multi-Tier, Session Resume)
**Track**: B
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-5]

### Description
Comprehensive end-to-end test exercising the full multi-tier hierarchy: operator → main → tech-lead → backend-developer (worker) → return chain. Includes session-rotate test (tech-lead context rotates mid-mission, fresh session resumes from summary checkpoint). Concurrency-sensitive — tests run with multiple sub-orchestrators in flight.

### Scope
- 3-level dispatch chain test
- Cross-domain handoff test (marketing-lead vs tech-lead pool isolation)
- Session-resume test (context rotation, summary checkpoint)
- Multi-mission concurrent test (2 simultaneous missions, no cross-talk)
- Mission lifecycle full-cycle test

### Success Criteria
- 3-level chain completes with full audit trail
- Cross-domain handoff blocked by cap-token scope
- Session resume continues mission from checkpoint
- Concurrent missions do not cross-contaminate
- Lifecycle completes for happy-path mission

### Quality Gate
`gate_pr6_mission_e2e`:
- [ ] **Mission lifecycle test**: planning → active → review → done state transitions all execute correctly with proper guards
- [ ] **Cross-domain handoff test**: marketing-lead cannot dispatch to backend-developer (cap-token scope enforces); main can dispatch to both
- [ ] **Multi-tier audit trail test**: 3-level dispatch (operator → main → tech-lead → worker) shows complete chain in receipts
- [ ] **Session resume test**: tech-lead context-rotates mid-mission, fresh session resumes from summary checkpoint, dispatches continue
- [ ] Two concurrent missions: no cross-contamination of receipts or leases
- [ ] CODEX GATE on this PR is mandatory feature-end gate
- [ ] CLAUDE_GITHUB_OPTIONAL on this PR is mandatory triple-gate (concurrency-critical, multi-tier hierarchy is the highest-blast-radius feature in the roadmap)

## Test Plan (Phase-Level — Concurrency Critical)

### State Machine Tests
- Every from-state × every event matrix exercised
- Invalid transitions rejected with structured error
- Concurrent transitions race-tested (100 attempts, one winner)

### Hierarchy Routing Tests
- operator → main: allowed
- main → sub_orchestrator: allowed
- main → worker: allowed
- sub_orchestrator → worker (own pool): allowed
- sub_orchestrator → worker (other pool): rejected (cap-token scope)
- worker → anything: rejected (worker has no CAN_DISPATCH)
- gate → anything: rejected (gate is GATE_ONLY)

### Mission Lifecycle E2E
- Plan a 3-step mission, advance through every state
- Pause mid-active, verify state preserved
- Resume from pause, complete remaining steps
- Failed mission: verify failed state terminal, no further transitions accepted

### Cross-Domain Handoff
- marketing-lead orchestrator with content-only governance variant
- Dispatches to blog-writer worker → accept
- Attempts to dispatch to backend-developer worker → cap-token scope check fails, rejected with structured error
- main-orchestrator (strict variant) can dispatch to either domain

### Multi-Tier Audit Trail
- 3-level chain: operator → main → tech-lead → worker
- Every receipt records parent_dispatch_id
- Tree projection shows full chain
- Cap-token chain verifies end-to-end

### Session Resume
- Mid-mission context rotation simulated (orchestrator process killed, restarted)
- Summary checkpoint written before kill
- Resumed orchestrator reads checkpoint, continues mission
- Child dispatches in flight at kill-time are reconciled (running ones picked up via lease, completed ones recorded)

### Concurrent Missions
- Two missions running simultaneously (mission_A, mission_B)
- Each owns separate sub-orchestrator
- Receipts isolated by mission_id
- Lease pools isolated
- No cross-talk in worker assignments

### Folder Agent Integration
- tech-lead folder spawns sub-orchestrator with strict variant
- marketing-lead folder spawns sub-orchestrator with content-only variant
- Each enforces its scope correctly

### Tree Rendering
- Render mission tree at depth 1, 3, 5
- Compact mode triggers at threshold
- Live refresh in CLI viewer
