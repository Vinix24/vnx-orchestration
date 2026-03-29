# Feature: VNX Execution Modes, Headless Routing, And Intelligence Quality

**Status**: Complete
**Priority**: P1
**Branch**: `feature/execution-modes-intelligence-routing`
**Baseline**: FP-A and FP-B merged on `main`; canonical runtime coordination, recovery supervision, tmux operator shell hardening, `vnx doctor`, and `vnx recover` are available
**Runtime policy**: T0 on Claude Opus; coding remains interactive-first in tmux; non-coding and structured synthesis may route to headless CLI targets; no Agent SDK dependency; execution remains CLI-agnostic across Claude, Codex, and future CLI targets

This feature expands VNX beyond a single interactive execution shape. FP-A made dispatching durable. FP-B made the runtime recoverable. FP-C now adds multiple execution modes, bounded headless delivery for non-coding work, inbound event/channel intake, and a stricter intelligence system that is short, task-specific, and measurable.

Primary objective:
Introduce task-class based execution routing so VNX can choose between interactive tmux workers, headless CLI workers, and inbound channel-driven dispatch creation without collapsing everything into the same terminal path.

Secondary objective:
Reduce intelligence noise by limiting injection to evidence-backed items at dispatch-create and resume time, then measure whether recommendations actually improve outcomes before FP-D expands autonomy.

Estimated effort: ~10-14 engineering days across PR-0 through PR-5.

## Design Principles
- Preserve the FP-A/FP-B control plane; execution targets are selected from canonical state, not from pane preference
- Keep coding interactive by default; do not force headless on workflows that need live terminal intervention
- Keep the system CLI-first and SDK-agnostic
- Treat headless execution as a durable worker class, not a shell shortcut
- Inject less intelligence, not more; every injected item must carry evidence metadata
- Measure recommendation usefulness before promoting stronger self-learning behavior

## Governance Rules

| # | Rule | Rationale |
|---|------|-----------|
| G-R1 | **Execution target selection must be explicit and reviewable** | Prevents hidden routing drift |
| G-R2 | **Coding stays interactive unless a policy explicitly says otherwise** | Avoids degrading operator control |
| G-R3 | **Headless execution must remain durable and receipt-producing** | Headless work cannot become an opaque side path |
| G-R4 | **Inbound channel events must become canonical dispatches before work starts** | Preserves governance and evidence flow |
| G-R5 | **Intelligence injection is bounded** — maximum 3 items, only at dispatch-create or resume | Keeps context short and relevant |
| G-R6 | **Every intelligence item must carry evidence metadata** — `confidence`, `evidence_count`, `last_seen`, and scope tags | Prevents advisory noise from looking authoritative |
| G-R7 | **Recommendation adoption must be measurable before it becomes policy** | Stops premature self-learning |
| G-R8 | **No execution-mode change may bypass T0 authority or receipts** | Preserves governance-first orchestration |

## Architecture Rules

| # | Rule | Description |
|---|------|-------------|
| A-R1 | **Execution targets are canonical runtime entities** — not inferred from pane IDs or CLI names |
| A-R2 | **Task classes drive routing** — `coding_interactive`, `research_structured`, `docs_synthesis`, `ops_watchdog`, `channel_response` |
| A-R3 | **Headless adapters are CLI-based** — no Agent SDK required for this feature |
| A-R4 | **Inbound events land in a durable inbox** before broker routing |
| A-R5 | **Intelligence selection runs only on create/resume paths** |
| A-R6 | **Recommendation usefulness metrics are first-class runtime data** |
| A-R7 | **Routing, injection, and recommendation decisions must emit events** |
| A-R8 | **Legacy all-through-tmux behavior remains available as fallback during cutover** |
| A-R9 | **Execution mode cutover must be reversible** |
| A-R10 | **No channel/event intake may directly mutate runtime state without broker registration** |

## Source Of Truth
- Task class registry: canonical runtime tables under `.vnx-data/state/`
- Execution target registry: canonical runtime state with target type, capability tags, and health
- Inbound channel/event inbox: durable file/queue plus runtime registration
- Headless execution attempts: dispatch attempts and receipts in canonical runtime state
- Intelligence catalog and selection metadata: canonical runtime state plus existing intelligence stores
- Recommendation usefulness metrics: canonical runtime state and exported analytics summaries
- tmux mappings: derived state only for interactive targets

## Known Failure Surface (Evidence / Problem Statement)
1. **All work still looks too terminal-shaped**: coding and non-coding tasks share the same interactive delivery assumptions
2. **Headless execution is under-modeled**: there is no canonical way to represent a CLI worker that is not a tmux pane
3. **Channel/event intake is not first-class**: inbound signals have no durable inbox-to-dispatch path
4. **Intelligence injection is still too noisy**: advisory content is not strongly tied to measurable benefit
5. **Recommendations lack causal evidence**: there is not enough before/after signal to trust self-learning decisions
6. **Future autonomy depends on cleaner routing and cleaner intelligence**: FP-D should not be built on ambiguous task classes or noisy injections

## What MUST NOT Be Done
1. Do NOT remove tmux in this feature
2. Do NOT require the Anthropic Agent SDK or any other SDK-only control surface
3. Do NOT route coding work headless by default
4. Do NOT inject continuous ambient intelligence into every worker turn
5. Do NOT let channel events bypass dispatch creation and receipt flow
6. Do NOT explode execution target types beyond what operators can understand
7. Do NOT let recommendation logic mutate runtime behavior silently

## Dependency Flow
```text
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-3
PR-1, PR-2 -> PR-4
PR-3, PR-4 -> PR-5
```

---

## PR-0: Task Classes, Execution Targets, And Intelligence Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 1-2 days
**Dependencies**: []

### Description
Define the FP-C contract surface first: task classes, execution target types, routing invariants, and the bounded intelligence contract. Later PRs should implement against one clear model instead of improvising execution shapes.

### Scope
- Define canonical task classes and their default execution target mappings
- Define canonical execution target types: `interactive_tmux_claude`, `interactive_tmux_codex`, `headless_claude_cli`, `headless_codex_cli`, `channel_adapter`
- Define routing invariants and fallback rules
- Define the intelligence item contract with evidence metadata and bounded count
- Define recommendation classes and acceptance semantics
- Create an FP-C certification matrix covering routing, inbox, intelligence, and measurement

### Success Criteria
- Task classes and execution target types are explicit and non-overlapping
- Coding versus non-coding routing is clearly bounded
- Intelligence items have one canonical schema and maximum payload shape
- FP-C has a certification matrix for later cutover and review

### Quality Gate
`gate_pr0_execution_contracts`:
- [ ] Canonical task classes and execution target types are documented and unambiguous
- [ ] Routing invariants define defaults and fallback behavior clearly
- [ ] Intelligence item contract includes confidence, evidence_count, last_seen, and scope tags
- [ ] Recommendation classes and acceptance semantics are explicit
- [ ] FP-C certification matrix covers routing, inbox, injection, and usefulness measurement

---

## PR-1: Headless CLI Target Registry And Dispatch Adapter
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Add a durable headless worker path that can execute structured non-coding dispatches via CLI adapters without pretending they are tmux panes.

### Scope
- Add canonical execution target registry entries for headless CLI workers
- Implement headless adapter interface for CLI-driven dispatch execution
- Support at least one bounded headless path for structured research/docs tasks
- Register headless attempts and receipts in the same runtime flow as interactive workers
- Add health and capability metadata for headless targets
- Preserve interactive tmux fallback for unsupported task classes

### Success Criteria
- Non-coding dispatches can target a headless CLI worker without using tmux prompt delivery
- Headless execution produces durable attempts and receipts
- Execution target health and capability are queryable from canonical state
- Unsupported work can fall back to the existing interactive path safely

### Quality Gate
`gate_pr1_headless_cli_targets`:
- [ ] Canonical execution target registry includes headless CLI worker types
- [ ] Headless adapter executes at least one structured non-coding task class end-to-end
- [ ] Headless attempts and receipts are durably recorded
- [ ] Interactive fallback remains available when headless routing is not allowed
- [ ] Tests cover target registration, adapter execution, and fallback behavior

---

## PR-2: Inbound Event Inbox And Channel-To-Dispatch Routing
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Create the durable inbox layer for inbound events and channel-originated tasks so external signals can become canonical dispatches before execution starts.

### Scope
- Add a durable inbound inbox for channel/event payloads
- Implement channel/session mapping and dedupe keys
- Translate inbox items into broker-registered dispatches
- Map task class and routing hints from inbound events
- Add bounded retry semantics for inbox processing
- Emit runtime events for accept, route, reject, and dead-letter decisions

### Success Criteria
- Inbound events can be durably stored before routing
- Channel-driven work enters the same dispatch lifecycle as internal tasks
- Dedupe and retry prevent duplicate dispatch storms
- Routing hints are visible and reviewable from canonical state

### Quality Gate
`gate_pr2_inbound_inbox_and_routing`:
- [ ] Inbound events are durably persisted before dispatch creation
- [ ] Channel/session mapping and dedupe keys prevent duplicate dispatch creation
- [ ] Inbox processing emits runtime events for route, reject, retry, and dead-letter outcomes
- [ ] Broker-registered dispatches preserve channel origin metadata
- [ ] Tests cover inbox persistence, dedupe, retry, and dispatch translation

---

## PR-3: Intelligence Selection Policy And Bounded Injection
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Replace broad, noisy intelligence injection with a strict selection policy that injects only evidence-backed items at dispatch-create and resume time.

### Scope
- Implement intelligence selection policy for create/resume paths only
- Enforce maximum injection set: one proven pattern, one failure-prevention rule, one recent comparable incident
- Attach evidence metadata and scope tags to each injected item
- Add routing-aware filtering so different task classes get different intelligence slices
- Add safe fallback when no intelligence meets the minimum evidence threshold
- Emit events for injection decisions and suppression decisions

### Success Criteria
- Intelligence payloads are shorter, more relevant, and evidence-backed
- Injection no longer behaves as generic context stuffing
- Different task classes can receive different intelligence slices safely
- Suppressed or missing intelligence is explicit, not silent

### Quality Gate
`gate_pr3_bounded_intelligence_injection`:
- [ ] Intelligence is injected only at dispatch-create and resume paths
- [ ] Injection payload is bounded to at most three evidence-backed items
- [ ] Each intelligence item includes confidence, evidence_count, last_seen, and scope tags
- [ ] Task-class-aware filtering changes the selected items when routing context changes
- [ ] Tests cover bounded payload enforcement, evidence thresholds, and suppression behavior

---

## PR-4: Recommendation Usefulness Metrics And Acceptance Loop
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-1, PR-2]

### Description
Add the measurement loop for recommendations so VNX can track whether a prompt patch, routing patch, or guardrail suggestion actually helped.

### Scope
- Add recommendation usefulness metrics and acceptance tracking
- Measure first-pass success, redispatch rate, open-item carry-over, ack timeout rate, repeated failure rate, and operator override rate
- Link recommendation acceptance to before/after outcome windows
- Export operator-readable usefulness summaries by recommendation class
- Keep the loop advisory-only; no automatic policy mutation
- Align metrics with headless and channel-originated dispatch paths

### Success Criteria
- Recommendation classes have measurable before/after outcomes
- Accepted versus ignored recommendations are distinguishable in runtime data
- Operators can inspect usefulness by recommendation class
- The learning loop remains advisory instead of self-mutating

### Quality Gate
`gate_pr4_recommendation_usefulness_metrics`:
- [ ] Recommendation acceptance and outcome windows are durably recorded
- [ ] Usefulness metrics cover the declared recommendation classes
- [ ] Before/after measurement can distinguish adopted from ignored recommendations
- [ ] Metrics work across headless and channel-originated dispatches
- [ ] Tests cover acceptance tracking, metric aggregation, and advisory-only behavior

---

## PR-5: Mixed Execution Routing Cutover And FP-C Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-3, PR-4]

### Description
Cut over from all-through-tmux assumptions to the certified mixed execution model only after headless routing, inbox registration, bounded intelligence, and usefulness measurement are all proven.

### Scope
- Enable mixed execution routing according to canonical task class rules
- Keep interactive coding as the default route while routing eligible non-coding work headless
- Wire bounded intelligence injection into the live dispatch path
- Add rollback controls for mixed execution cutover
- Certify FP-C against the PR-0 matrix and document residual risks
- Update operator documentation for execution target selection and intelligence review

### Success Criteria
- Mixed execution routing works without breaking interactive coding defaults
- Eligible non-coding work can flow headless with receipts and runtime events
- Bounded intelligence is visible and reviewable in live dispatches
- FP-C ends with a certified cutover path that leaves FP-D unblocked

### Quality Gate
`gate_pr5_mixed_execution_cutover`:
- [ ] Interactive coding remains the default route after cutover
- [ ] Eligible non-coding task classes can route headless end-to-end with receipts
- [ ] Live dispatches show bounded intelligence injection and routing decisions in evidence trails
- [ ] Cutover includes rollback controls and certification evidence
- [ ] Full FP-C verification passes before feature closure
