# Feature: VNX Safe Autonomy, Governance Envelopes, And End-To-End Provenance

**Status**: Complete
**Priority**: P1
**Branch**: `feature/safe-autonomy-governance`
**Baseline**: FP-A, FP-B, and FP-C merged on `main`; canonical runtime coordination, bounded recovery, mixed execution routing, headless CLI targets, bounded intelligence, and recommendation usefulness measurement are available
**Runtime policy**: T0 on Claude Opus; autonomy remains governance-first; coding and non-coding execution modes from FP-C stay intact; provenance enforcement must remain CLI-agnostic across Claude CLI, Codex CLI, and future CLI targets

This feature is the policy and control layer that sits on top of the hardened runtime. FP-A made runtime truth explicit. FP-B made recovery and operator control reliable. FP-C added multiple execution modes and bounded intelligence. FP-D now defines what VNX may do automatically, what must always be gated, how escalation states are encoded, and how dispatch, receipt, commit, and PR/featureplan become bidirectionally traceable.

Primary objective:
Introduce explicit autonomy envelopes and governance evaluation so VNX can act automatically within policy while preserving T0 authority, escalation points, and reviewable evidence.

Secondary objective:
Close the remaining provenance gap by making CLI-agnostic traceability enforceable across Git metadata and NDJSON receipts, with optional CI/server-side backstops.

Estimated effort: ~10-14 engineering days across PR-0 through PR-5.

## Design Principles
- Preserve T0 as the decision center for completion, merges, and governance exceptions
- Keep autonomy bounded by explicit policy, not by optimistic runtime behavior
- Separate low-risk automatic actions from high-risk gated actions
- Treat provenance as a first-class control surface, not documentation after the fact
- Keep Git enforcement CLI-agnostic and receipt-aware
- Prefer additive guardrails and visibility over hidden automation

## Governance Rules

| # | Rule | Rationale |
|---|------|-----------|
| G-R1 | **Every automatic action must map to a policy class** | No implicit autonomy |
| G-R2 | **High-risk actions are always gated** | Prevents silent governance bypass |
| G-R3 | **Repeated failure loops escalate to hold or escalate states** | Stops endless retry autonomy |
| G-R4 | **Completion and merge authority remain with T0 or an explicit human gate** | Preserves governance-first model |
| G-R5 | **Georchestreerd werk must carry a trace token** — prefer `dispatch:<id>` while accepting approved legacy refs | Makes Git-native traceability enforceable |
| G-R6 | **Receipts remain the primary evidence layer** | Commit metadata is a pointer, not the whole truth |
| G-R7 | **Dispatch, receipt, commit, and PR/featureplan must be bidirectionally traceable** | End-to-end provenance must survive tool changes |
| G-R8 | **No CLI-specific hook system may be the primary enforcement layer** | Enforcement must survive tool changes |

## Architecture Rules

| # | Rule | Description |
|---|------|-------------|
| A-R1 | **Policy matrix is canonical data** — not buried in prose only |
| A-R2 | **Escalation states are explicit** — `info`, `review_required`, `hold`, `escalate` |
| A-R3 | **Autonomy evaluation emits runtime events** |
| A-R4 | **Git provenance enforcement is implemented at Git/CI level** |
| A-R5 | **Receipt schema carries commit linkage fields where needed** |
| A-R6 | **prepare-commit-msg / commit-msg may assist locally, but CI/server checks remain the durable backstop** |
| A-R7 | **Policy overrides are durable governance events** |
| A-R8 | **Provenance checks must tolerate approved legacy references during transition** |
| A-R9 | **No silent policy mutation from recommendation logic** |
| A-R10 | **Autonomy rollout must be reversible by feature flag or policy switch** |

## Source Of Truth
- Autonomy policy matrix: canonical runtime/config state
- Escalation and override events: canonical runtime events plus receipts
- Provenance registry: canonical mapping between dispatch, receipt, commit, and PR/featureplan
- Git enforcement: local hooks plus optional CI/server-side validation
- Receipt evidence: `t0_receipts.ndjson` and related runtime receipt outputs
- PR/featureplan linkage: queue and feature plan metadata plus PR context

## Known Failure Surface (Evidence / Problem Statement)
1. **Autonomy boundaries are not yet encoded sharply enough**: too much depends on operator discipline instead of policy evaluation
2. **Retries and recovery can still appear autonomous without clear governance framing**: FP-B bounded behavior needs policy meaning
3. **Git-native traceability is still weaker than runtime traceability**: receipts and dispatches are richer than commits
4. **Local-only enforcement is bypassable**: CLI-specific or local-only hooks are not enough
5. **Legacy workflows still allow partial provenance gaps**: commit -> dispatch -> receipt and the reverse path are not universally enforced
6. **FP-D must not accidentally become full self-governing automation**: policy needs to bound autonomy, not erase oversight

## What MUST NOT Be Done
1. Do NOT allow autonomous merge or PR completion in this feature
2. Do NOT remove T0 authority from completion, merge, or exception handling
3. Do NOT rely on Claude-specific hooks as the primary provenance enforcement path
4. Do NOT force a single provider- or CLI-specific trace format beyond the approved token contract
5. Do NOT allow receipts without enough linkage to reconstruct the provenance chain
6. Do NOT let recommendation logic rewrite policy automatically
7. Do NOT collapse `hold` and `escalate` into generic error noise

## Dependency Flow
```text
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-3
PR-1, PR-2 -> PR-4
PR-3, PR-4 -> PR-5
```

---

## PR-0: Autonomy Policy Matrix, Escalation Model, And Provenance Contract
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 1-2 days
**Dependencies**: []

### Description
Lock the FP-D contract before implementation starts: what VNX may do automatically, what is always gated, how escalation states work, and what the end-to-end provenance contract requires from dispatches, receipts, commits, and PRs.

### Scope
- Define canonical policy classes and decision types
- Define automatic, gated, and forbidden action classes
- Define escalation states and transition semantics
- Define CLI-agnostic trace token rules, including accepted legacy refs
- Define bidirectional provenance contract across dispatch, receipt, commit, and PR/featureplan
- Create FP-D certification matrix for autonomy and provenance

### Success Criteria
- Policy classes and action classes are explicit and non-overlapping
- Escalation states and transitions are unambiguous
- Provenance contract is explicit enough for hooks, receipts, and CI to implement
- FP-D certification matrix covers autonomy envelopes and provenance checks

### Quality Gate
`gate_pr0_policy_and_provenance_contract`:
- [ ] Canonical policy matrix distinguishes automatic, gated, and forbidden actions
- [ ] Escalation states and transitions are documented and non-overlapping
- [ ] Trace token contract defines preferred and accepted legacy formats
- [ ] Provenance contract covers dispatch, receipt, commit, and PR/featureplan in both directions
- [ ] FP-D certification matrix covers autonomy and provenance behavior

---

## PR-1: Governance Evaluation Engine And Escalation State Tracking
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Implement the runtime policy evaluation layer so recovery, retries, routing, and overrides can be classified and escalated through one governance-aware engine.

### Scope
- Add governance policy evaluation module
- Persist escalation state and override events
- Classify actions into automatic, gated, or forbidden outcomes
- Integrate with runtime events from FP-A/FP-B/FP-C
- Add operator-readable summaries of holds and escalations
- Keep feature-flagged rollout for policy enforcement

### Success Criteria
- Runtime actions can be evaluated against a canonical policy matrix
- Escalation states are durable and reviewable
- Forbidden actions are blocked before execution
- Holds and escalations become visible without log archaeology

### Quality Gate
`gate_pr1_governance_evaluator`:
- [ ] Policy evaluation returns automatic, gated, or forbidden outcomes deterministically
- [ ] Escalation state transitions are durably recorded
- [ ] Override events are explicit and reviewable
- [ ] Forbidden actions are blocked before execution
- [ ] Tests cover policy evaluation, hold/escalate transitions, and override recording

---

## PR-2: Receipt Provenance Enrichment And Bidirectional Linkage
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Strengthen the receipt layer so it can point cleanly to commits, PRs, and featureplan context, and so provenance can be reconstructed from receipts without manual digging.

### Scope
- Enrich receipt schema where needed with commit/PR provenance fields
- Add mapping helpers between dispatches, receipts, and commit identities
- Ensure provenance survives mixed execution and headless paths
- Add receipt-side validation helpers for missing or broken provenance links
- Export operator-readable provenance summaries
- Preserve backward compatibility with existing receipt readers where possible

### Success Criteria
- Receipts can participate in the full provenance chain in both directions
- Mixed execution paths no longer create provenance blind spots
- Missing provenance linkage is detectable, not silent
- Existing evidence-first model remains intact

### Quality Gate
`gate_pr2_receipt_provenance`:
- [ ] Receipts carry enough linkage to connect dispatch, commit, and PR context
- [ ] Bidirectional provenance reconstruction works from the receipt layer
- [ ] Missing or broken receipt provenance is detectable through validation
- [ ] Mixed execution paths preserve receipt linkage
- [ ] Tests cover receipt enrichment, linkage reconstruction, and backward-compatible reads

---

## PR-3: CLI-Agnostic Git Traceability Enforcement
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Implement Git-native provenance enforcement that works regardless of whether work is performed through Claude CLI, Codex CLI, or another future CLI.

### Scope
- Add `prepare-commit-msg` and/or `commit-msg` support for trace token assistance and validation
- Support preferred `dispatch:<id>` token plus approved legacy refs
- Add CI/server-side validation path for trace token enforcement
- Document operator and worker expectations for traceability
- Ensure enforcement remains tool-agnostic and does not depend on one AI CLI
- Add bypass/override handling as explicit governance events rather than silent skips

### Success Criteria
- Git commits can be validated for traceability independent of the CLI used
- Local hooks assist but are not the only enforcement path
- Approved legacy refs remain accepted during transition
- Traceability failures are explicit and actionable

### Quality Gate
`gate_pr3_git_traceability`:
- [ ] Git traceability enforcement works without depending on a specific AI CLI
- [ ] Preferred dispatch token and approved legacy refs are both handled correctly
- [ ] CI or server-side validation can detect missing trace tokens
- [ ] Local hook behavior and override handling are documented and testable
- [ ] Tests cover valid, invalid, and legacy trace token cases

---

## PR-4: Provenance Verification, Audit Views, And Advisory Guardrails
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-1, PR-2]

### Description
Add the verification and audit surfaces that let operators and T0 check whether the provenance chain is intact and whether autonomy decisions stayed within policy.

### Scope
- Build provenance verification routines across dispatch, receipt, commit, and PR metadata
- Add operator/T0 audit views or reports for policy outcomes and provenance completeness
- Surface missing links, broken chains, and override events clearly
- Add advisory guardrails that recommend intervention before governance drift becomes hidden
- Keep the output reviewable and evidence-oriented
- Ensure compatibility with existing receipts and queue state

### Success Criteria
- Operators can verify provenance completeness without manual log archaeology
- Broken provenance chains are surfaced before merge/closure steps
- Governance audit views show where autonomy acted and where it was gated
- Advisory guardrails strengthen oversight without mutating policy

### Quality Gate
`gate_pr4_provenance_verification`:
- [ ] Provenance verification can detect broken links across dispatch, receipt, commit, and PR data
- [ ] Audit views surface policy outcomes, overrides, and broken chains clearly
- [ ] Advisory guardrails are evidence-backed and non-mutating
- [ ] Verification remains compatible with existing queue and receipt flows
- [ ] Tests cover complete, partial, and broken provenance chains

---

## PR-5: Safe Autonomy Cutover, Provenance Enforcement Rollout, And FP-D Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-3, PR-4]

### Description
Cut over to the explicit autonomy envelope and provenance-enforced governance path only after the policy engine, receipt linkage, Git traceability, and audit views are all proven.

### Scope
- Enable safe autonomy envelopes through policy-backed evaluation
- Roll out provenance enforcement with documented fallback/transition behavior
- Integrate audit and verification outputs into operator/T0 review flow
- Add rollback controls for autonomy and provenance enforcement changes
- Certify FP-D against the PR-0 matrix and document residual risks
- Confirm that FP-D does not grant autonomous merge or completion authority

### Success Criteria
- Automatic actions occur only within explicit policy envelopes
- High-risk actions remain gated and visible
- Provenance enforcement is active without binding the system to one CLI
- FP-D ends with certified autonomy and provenance controls on top of FP-A through FP-C

### Quality Gate
`gate_pr5_safe_autonomy_cutover`:
- [ ] Policy-backed autonomy is limited to approved automatic action classes
- [ ] High-risk actions remain gated after cutover
- [ ] Provenance enforcement is active and reviewable across Git and receipt layers
- [ ] Rollback controls and transition guidance are documented
- [ ] Full FP-D verification passes before feature closure
