# Feature: Runtime Adapter Formalization And Headless Transport Abstraction

**Feature-ID**: Feature 16
**Status**: Planned
**Priority**: P1
**Branch**: `feature/runtime-adapter-formalization-and-headless-transport-abstraction`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Make runtime execution and observation flow through an explicit adapter boundary so tmux remains supported but is no longer the hidden architecture for worker/session control.

Execution context:
- intended follow-on after Feature 14 and Feature 15
- maps primarily to Roadmap M5a: Runtime Adapter Interface And `TmuxAdapter` Formalization
- assumes Feature 12 runtime truth and Feature 13 dashboard visibility already exist
- assumes chain and context quality work are strong enough that transport abstraction can land without masking runtime ambiguity

Execution preconditions:
- both Gemini and Codex headless gates must be proven end-to-end on current `main` before this feature starts
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- Feature 14 and Feature 15 must already be merged, except for narrowly scoped interface-only overlap explicitly documented by T0

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because runtime-boundary mistakes can silently damage future autonomy
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

VNX runtime behavior is now more observable and governable, but execution still leans too heavily on implicit tmux assumptions:
- launch, attach, stop, inspect, and health semantics are still too coupled to tmux-specific behavior
- new runtime improvements risk deepening transport coupling if they continue to talk directly in tmux terms
- headless execution exists, but not yet behind a clear session/runtime boundary that can coexist with tmux safely
- future worker/session evolution will stay fragile until runtime capabilities are modeled explicitly instead of being inferred from terminal mechanics

## Design Goal

Introduce an explicit runtime adapter boundary with a production `TmuxAdapter` and a constrained early headless transport abstraction, so future worker/session work can evolve without a transport rewrite or hidden tmux dependency.

## Non-Goals

- no full tmux removal
- no complete transport rewrite
- no remote-control channel architecture
- no broad Business OS rollout
- no forced production cutover to a richer headless runtime in this feature

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- new runtime work must land through adapter-backed interfaces, not fresh direct tmux coupling
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Runtime Adapter Contract And Capability Matrix
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical runtime adapter interface, capability model, adapter responsibilities, and state mapping rules before code extraction starts.

### Scope
- define `RuntimeAdapter` interface for spawn, stop, attach, observe, inspect, and health/status queries
- define adapter capability matrix and explicit unsupported-operation semantics
- define mapping rules between adapter state and canonical runtime truth
- define compatibility requirements for `TmuxAdapter`
- define boundary rules for future headless/local-session adapters

### Deliverables
- runtime adapter contract
- adapter capability matrix
- canonical state-mapping rules
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- runtime/session responsibilities are explicit before implementation
- adapter capability gaps are surfaced as governed states rather than hidden behavior
- tmux compatibility is preserved without making tmux the architecture
- future headless adapter work has a locked contract to build against

### Quality Gate
`gate_pr0_runtime_adapter_contract`:
- [ ] Contract defines `RuntimeAdapter` responsibilities for spawn, stop, attach, observe, inspect, and health/status
- [ ] Contract defines adapter capability matrix and unsupported-operation semantics
- [ ] Contract defines mapping between adapter-visible state and canonical runtime truth
- [ ] Contract defines `TmuxAdapter` compatibility requirements and future headless-adapter boundary rules
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: TmuxAdapter Extraction And Direct-Coupling Freeze
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Extract current tmux runtime behavior behind an explicit `TmuxAdapter` and freeze new direct tmux coupling in the validated path.

### Scope
- implement `TmuxAdapter` against the new runtime adapter contract
- route validated launch/attach/stop/inspect calls through `TmuxAdapter`
- add guardrails/tests that block new direct tmux wiring in the protected path
- preserve existing operator-visible behavior while shifting the boundary

### Deliverables
- `TmuxAdapter` implementation
- direct-coupling freeze guard
- adapter-backed runtime tests
- GitHub PR with adapter extraction evidence summary

### Success Criteria
- validated runtime actions no longer need to call tmux primitives directly outside the adapter
- `TmuxAdapter` preserves existing behavior for current operator and worker flows
- new direct tmux coupling is blocked in the protected path
- adapter extraction does not regress runtime truth visibility

### Quality Gate
`gate_pr1_tmux_adapter_extraction`:
- [ ] All adapter extraction and compatibility tests pass
- [ ] Validated launch/attach/stop/inspect flows use `TmuxAdapter` under test
- [ ] New direct tmux coupling is blocked or flagged in the protected path under test
- [ ] Existing operator-visible behavior remains compatible under test
- [ ] GitHub PR exists with adapter extraction evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Runtime Launch And Observation Facade
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Introduce an adapter-backed runtime facade so orchestration and dashboard surfaces ask for runtime behavior through one explicit boundary.

### Scope
- add runtime facade/service that uses `RuntimeAdapter` rather than transport-specific helpers
- route dashboard/operator actions through the facade where applicable
- route validated runtime observation/read-model paths through the facade
- add tests for launch, observation, and failure propagation

### Deliverables
- runtime facade/service
- adapter-backed operator/runtime integration
- facade behavior tests
- GitHub PR with facade evidence summary

### Success Criteria
- orchestration and dashboard code consume one runtime boundary instead of transport-specific helpers
- transport-specific failures surface as explicit runtime outcomes
- runtime facade preserves current truth/read-model compatibility
- future adapter work has a stable integration seam

### Quality Gate
`gate_pr2_runtime_facade`:
- [ ] All runtime facade tests pass
- [ ] Validated orchestration and dashboard paths use the runtime facade under test
- [ ] Transport-specific failures surface as explicit runtime outcomes under test
- [ ] Runtime facade preserves current runtime truth and read-model compatibility under test
- [ ] GitHub PR exists with facade evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Early Headless Transport Abstraction And Capability Gating
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-2]

### Description
Add a constrained early headless transport abstraction so future non-tmux worker/session execution can be reasoned about without forcing a production cutover.

### Scope
- define and implement a minimal headless/local-session adapter skeleton behind the runtime adapter contract
- add capability gating so unsupported operations remain explicit
- keep tmux as the active production adapter while validating headless capability semantics
- add tests for adapter registration, capability gating, and safe fallback behavior

### Deliverables
- early headless/local-session adapter skeleton
- adapter capability-gating logic
- safe fallback behavior tests
- GitHub PR with headless-adapter evidence summary

### Success Criteria
- VNX can represent a non-tmux runtime path without pretending it is production-ready
- unsupported headless operations fail explicitly and governably
- tmux remains the default active adapter for current production flows
- future richer headless runtime work no longer needs to invent its boundary from scratch

### Quality Gate
`gate_pr3_headless_transport_abstraction`:
- [ ] All headless-adapter and capability-gating tests pass
- [ ] Early headless adapter is registered behind the runtime adapter contract under test
- [ ] Unsupported headless operations fail explicitly under test
- [ ] Tmux remains the default active adapter for current production flows under test
- [ ] GitHub PR exists with headless-adapter evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Runtime Adapter Certification And Transition Guardrails
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify that runtime behavior now flows through stable adapter boundaries, tmux remains safely supported, and the early headless abstraction is explicit rather than accidental.

### Scope
- certify adapter-backed parity for validated launch/attach/stop/inspect paths
- certify canonical runtime truth remains aligned with adapter-backed state reporting
- verify no new protected-path direct tmux coupling was introduced during the feature
- update `docs/internal/plans/CHANGELOG.md` with feature-closeout summary and next-step recommendation
- update `docs/internal/plans/PROJECT_STATUS.md` with the new runtime-adapter baseline and remaining next-order steps
- require Gemini review and Codex final gate on the certification PR

### Deliverables
- runtime adapter certification report
- parity and guardrail evidence
- updated internal planning changelog
- updated internal planning project status

### Success Criteria
- validated runtime behavior is adapter-backed rather than transport-hard-coded
- tmux remains safely supported as one adapter
- early headless transport abstraction is explicit and bounded
- both Gemini and Codex gates execute successfully on the certification PR
- this feature closes with zero unresolved chain-created open items

### Quality Gate
`gate_pr4_runtime_adapter_certification`:
- [ ] All runtime adapter certification tests pass
- [ ] Adapter-backed parity is proven for validated launch, attach, stop, and inspect flows under test
- [ ] Canonical runtime truth remains aligned with adapter-backed state reporting under test
- [ ] No new protected-path direct tmux coupling is introduced under test
- [ ] Gemini and Codex both execute to terminal success on the certification path with request, result, and report artifacts present
- [ ] `docs/internal/plans/CHANGELOG.md` is updated with feature-closeout progress and next recommended order
- [ ] `docs/internal/plans/PROJECT_STATUS.md` is updated with the new runtime-adapter baseline
- [ ] Feature closes with zero unresolved chain-created open items
- [ ] GitHub PR exists with certification evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

### Post-Merge Continuation
If this certification PR merges cleanly and no feature-level blockers remain, T0 must
auto-continue into the next feature in the chain rather than waiting for a new human
kickoff prompt.

Required continuation sequence:
1. Close Feature 16 in the queue and confirm no unresolved blocker open items remain.
2. Perform chain-boundary runtime cleanup / stale-lease reconciliation.
3. Materialize Feature 17 into root `FEATURE_PLAN.md`.
4. Reinitialize `PR_QUEUE.md` from the new plan.
5. Run kickoff preflight for `PR-0`.
6. Promote exactly one kickoff dispatch for Feature 17.
7. Continue normal orchestration from the new queue state.

Next feature to start automatically:
- Feature 17: Rich Headless Runtime Sessions And Structured Observability
