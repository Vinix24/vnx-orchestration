# Feature: Review Contracts, Acceptance Idempotency, And Auto-Next Trials

**Status**: Complete
**Priority**: P0
**Branch**: `feature/review-contract-gates-and-idempotency`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Turn the new roadmap/autopilot foundation into a deliverable-aware governance loop by fixing duplicate acceptance risk, generating deterministic review contracts per PR, and proving the next-feature workflow with controlled auto-next trials.

## Open Follow-Up Findings
- Duplicate acceptance / duplicate dispatch handling still lacks a hard idempotency guard in the dispatch lifecycle.
- Review gates exist, but reviewer prompts are not yet driven by structured deliverables and acceptance criteria.
- Closure verification is stronger, but reviewer evidence and deliverable contracts are not yet fused into one canonical gate contract.
- Auto-next exists at the roadmap layer, but has not yet been proven against real follow-up feature execution using review contracts.

## Dependency Flow
```text
PR-0 (no dependencies)
PR-1 (no dependencies)
PR-1 -> PR-2
PR-1 -> PR-3
PR-1 -> PR-4
PR-0, PR-2, PR-3, PR-4 -> PR-5
PR-5 -> PR-6
```

## PR-0: Dispatch Acceptance Idempotency Guard
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-5 hours
**Dependencies**: []

### Description
Add a canonical acceptance idempotency guard so duplicate acceptance events and already-terminal dispatches are rejected or treated as explicit no-ops instead of being silently reprocessed.

### Scope
- `scripts/lib/runtime_coordination.py`
- `scripts/dispatcher_v8_minimal.sh`
- `scripts/receipt_processor_v4.sh`
- focused lifecycle tests around duplicate acceptance and terminal-state repeats

### Success Criteria
- duplicate acceptance for an already accepted/running/terminal dispatch is deterministically blocked or no-op classified
- no new parallel lifecycle truth is introduced outside the canonical runtime coordination layer
- existing dispatch lifecycle behavior remains backward-compatible for valid forward transitions

### Quality Gate
`gate_pr0_acceptance_idempotency`:
- [ ] Duplicate acceptance for an already terminal dispatch is rejected or no-op classified with explicit evidence
- [ ] Forward-only valid dispatch transitions still pass without regression
- [ ] Existing dispatch lifecycle tests and new duplicate-acceptance tests pass

---

## PR-1: Review Contract Schema And Materializer
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-4 hours
**Dependencies**: []

### Description
Define the canonical review contract schema and build the materializer that derives it from FEATURE_PLAN, PR metadata, changed files, declared tests, quality gates, and deterministic verifier findings.

### Scope
- review contract schema and serializer
- mapping from PR queue + feature plan metadata into a review contract document
- stable fields for deliverables, non-goals, changed files, test evidence, and closure stage

### Success Criteria
- each PR can produce one structured review contract without handwritten prompt assembly
- contract fields cover deliverables, non-goals, tests, risk class, merge policy, and review stack
- contract generation is deterministic for the same inputs

### Quality Gate
`gate_pr1_review_contract_schema`:
- [ ] Review contract schema covers deliverables, non-goals, tests, risk class, merge policy, and review stack
- [ ] Contract generation is deterministic for identical inputs
- [ ] Schema serialization and parsing tests pass

---

## PR-2: Gemini Review Prompt Renderer And Receipt Contract
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-1]

### Description
Render deliverable-aware Gemini review prompts from the canonical review contract and emit richer review-gate receipts that show exactly what was checked.

### Scope
- Gemini prompt template(s) driven by review contract fields
- structured receipt payloads for advisory vs blocking findings
- tests for prompt rendering completeness and missing-field handling

### Success Criteria
- Gemini review prompts include deliverables, non-goals, declared tests, changed files, and deterministic findings
- emitted receipts clearly distinguish advisory and blocking findings
- missing contract fields fail explicitly instead of silently degrading the prompt

### Quality Gate
`gate_pr2_gemini_contract_review`:
- [ ] Gemini prompts include deliverables, non-goals, changed files, and declared tests
- [ ] Advisory vs blocking findings are emitted distinctly in review receipts
- [ ] Missing review-contract fields fail explicitly

---

## PR-3: Codex Final Gate Prompt Renderer And Headless Enforcement
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Use the review contract to drive Codex final-gate prompts and enforce that high-risk/runtime/governance PRs cannot bypass the Codex gate when the policy requires it.

### Scope
- deliverable-aware Codex final gate prompt renderer
- required/optional Codex gate enforcement based on change scope and policy
- structured residual-risk and rerun requirements in final-gate receipts

### Success Criteria
- high-risk/runtime/governance PRs are blocked without a valid Codex final gate result
- Codex prompts include deliverables, non-goals, tests, changed files, deterministic findings, and closure stage
- final-gate receipts capture pass/fail/blocked plus residual risk and rerun requirements

### Quality Gate
`gate_pr3_codex_final_gate_contract`:
- [ ] Runtime or governance PRs cannot clear without a Codex final gate when policy requires it
- [ ] Codex prompts include deliverables, non-goals, tests, changed files, and closure stage
- [ ] Final-gate receipts include findings, residual risk, and rerun requirements

---

## PR-4: Claude GitHub Review Bridge And Evidence Linkage
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1]

### Description
Turn the optional Claude GitHub review path into a first-class evidence source by linking GitHub review invocation/results into the same contract and receipt model.

### Scope
- GitHub review request payload integration with review contracts
- receipt linkage from optional Claude review requests/results
- explicit `not_configured` / `configured_dry_run` / `requested` semantics

### Success Criteria
- Claude GitHub review requests are linked to the same review contract as Gemini/Codex
- optional review states are explicit and auditable
- T0 can see whether GitHub review contributed evidence or was intentionally absent

### Quality Gate
`gate_pr4_claude_review_linkage`:
- [ ] Claude GitHub review request state is linked to the same review contract as Gemini and Codex
- [ ] Optional review states are explicit and auditable
- [ ] Review evidence linkage tests pass

---

## PR-5: Closure Verifier Contract Checks And Required Evidence Wiring
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0, PR-2, PR-3, PR-4]

### Description
Make closure verification consume review contracts and required review-gate evidence so T0 cannot claim closure-ready without the contractually required reviewers and deterministic deliverable evidence.

### Scope
- closure verifier checks for required review contract presence
- gate-result validation against review stack and risk policy
- explicit failures for missing evidence, missing required reviewer, or scope drift

### Success Criteria
- closure verifier fails when required review contracts or required gate results are missing
- deterministic and reviewer evidence are both visible in closure output
- T0 cannot claim closure-ready with partial or mismatched review evidence

### Quality Gate
`gate_pr5_closure_contract_enforcement`:
- [ ] Closure verifier fails when required review contracts or required gate results are missing
- [ ] Closure output shows both deterministic and reviewer evidence
- [ ] False-green closure claims are blocked in automated tests

---

## PR-6: Auto-Next Trial Harness And Controlled Certification
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-4 hours
**Dependencies**: [PR-5]

### Description
Prove the new loop with controlled trial sequences: one feature merges, required evidence clears, and the next feature loads only when drift/fix-up conditions are satisfied.

### Scope
- integration tests for feature A -> optional fix-up -> feature B advancement
- certification report for controlled rollout of the multi-feature loop
- explicit residual-risk summary for autonomous follow-up usage

### Success Criteria
- roadmap auto-next only advances after merged-to-main + green checks + closure verifier pass
- blocking drift inserts a fix-up feature before continuing
- certification evidence exists for at least one controlled multi-feature trial path

### Quality Gate
`gate_pr6_auto_next_trials`:
- [ ] Auto-next advances only after merged-to-main, green checks, and closure verifier pass
- [ ] Blocking drift inserts a fix-up feature before the next feature loads
- [ ] Controlled multi-feature trial certification evidence exists with residual risk documented
