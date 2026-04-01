# Feature: Fail-Closed Terminal Dispatch Guard

**Status**: Planned
**Priority**: P1
**Branch**: `feature/fail-closed-terminal-dispatch-guard`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Close the busy-terminal exclusivity breach by making dispatch safety fail closed under lease ambiguity, runtime uncertainty, invalid skill metadata, and Claude-specific delivery edge cases.

Execution context:
- second feature in the unattended 4-feature hardening chain
- T1 and T2 are Sonnet-pinned terminals
- T3 is a Claude terminal with known clear-context / modal sensitivity

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because dispatch-core behavior is merge-critical

## Problem Statement

The recent trial exposed a real runtime safety breach:
- a new dispatch was sent toward `T3`
- while `T3` was already busy or ambiguously busy
- later failures also showed:
  - invalid skills can reach pending delivery before being rejected
  - implicit `/clear` can trigger a Claude feedback modal and swallow real dispatch payload
  - smart-tap can reject a real manager block because of benign shell noise

This is not only observability drift. It is unsafe dispatch behavior.

## Design Goal

Move dispatch safety from:
- best-effort exclusivity with permissive fallback

to:
- fail-closed exclusivity with explicit blocked, requeue, and recovery semantics

That means:
- no second dispatch to a terminal that is busy, ambiguously busy, or runtime-uncertain
- no silent continuation after lease-acquire or availability-check failure
- no invalid skill metadata reaching pending delivery
- no implicit clear-context on Claude terminals unless explicitly requested and readiness is re-verified

## Non-Goals

- no full routing-engine rewrite
- no pseudo-parallelism on a single terminal
- no replacement of the broader queue system
- no speculative tmux delivery redesign beyond safety guards needed here

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
```

## PR-0: Terminal Exclusivity And Fail-Closed Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical dispatch-exclusivity contract so no second dispatch can be sent to a terminal that is already busy, ambiguously busy, or runtime-uncertain.

### Scope
- define terminal states relevant to dispatch safety
- define fail-closed behavior for:
  - canonical check failure
  - lease ambiguity
  - runtime-core unavailability
  - active worker ownership
- define retry, requeue, and escalation boundaries
- lock non-goals so this does not become a terminal scheduler rewrite

### Success Criteria
- terminal exclusivity rules are explicit
- ambiguous runtime state blocks rather than dispatches
- safe retry and requeue boundaries are clear
- T0 and dispatcher share the same safety expectations

### Quality Gate
`gate_pr0_terminal_exclusivity_contract`:
- [ ] Contract defines when a terminal is dispatchable, blocked, or ambiguous
- [ ] Contract requires fail-closed behavior on runtime or lease uncertainty
- [ ] Contract blocks silent second dispatch to an already occupied terminal
- [ ] Contract defines retry or requeue behavior for blocked dispatch attempts
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Fail-Closed Canonical Availability, Skill Validation, And Lease Acquire
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Harden the dispatcher so canonical availability checks and lease acquisition fail closed, and validate requested skill metadata before queue init, promote, and delivery.

### Scope
- remove fail-open behavior from canonical terminal checks
- make canonical lease acquisition enforceable for dispatch progression
- block dispatch when runtime availability cannot be proven
- validate requested skill or role before queue init, promote, and delivery so an invalid skill never becomes a silently hanging pending dispatch
- add tests for availability-check exceptions, lease-acquire failure, and invalid skill metadata

### Success Criteria
- dispatcher no longer continues after canonical availability ambiguity
- lease-acquire failure blocks dispatch deterministically
- invalid skill metadata is rejected before delivery rather than discovered only by worker-side failure
- runtime check errors are explicit and auditable
- existing safe dispatch paths continue to work

### Quality Gate
`gate_pr1_fail_closed_dispatch_guard`:
- [ ] All fail-closed dispatch guard tests pass
- [ ] Canonical availability check failure blocks dispatch explicitly
- [ ] Canonical lease acquisition failure blocks dispatch explicitly
- [ ] Invalid skill or role metadata blocks before pending delivery or worker pickup
- [ ] No dispatch continues after runtime uncertainty under test scenarios
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Requeue, Clear-Context Safety, Smart-Tap, And Operator Visibility
**Track**: C
**Priority**: P2
**Complexity**: Medium
**Risk**: High
**Skill**: @reviewer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1]

### Description
Improve blocked-dispatch handling so operator tooling and T0 can distinguish safe requeue from true ownership conflict, while preventing Claude `/clear` and smart-tap edge cases from swallowing valid dispatches.

### Scope
- add explicit blocked-dispatch audit reasons
- surface requeueable vs non-requeueable blocked state
- remove or constrain implicit `ClearContext: true` behavior so Claude terminals are not cleared by default
- verify Claude terminal ready state after explicit clear-context requests before delivering the actual dispatch
- harden smart-tap reject heuristics so benign tool noise such as `Shell cwd was reset` does not invalidate a real manager block
- verify no silent duplicate delivery attempts occur
- add tests for operator-readable blocked-dispatch evidence

### Success Criteria
- blocked dispatches have actionable reason text
- requeue behavior is deterministic and visible
- Claude delivery no longer loses the real dispatch behind feedback or modal state triggered by `/clear`
- valid manager blocks are not dropped by over-aggressive smart-tap reject heuristics
- operators can distinguish busy-terminal protection from broader runtime failure
- duplicate delivery attempts are auditable

### Quality Gate
`gate_pr2_blocked_dispatch_visibility`:
- [ ] All blocked-dispatch visibility tests pass
- [ ] Blocked dispatch reasons distinguish busy terminal from runtime failure
- [ ] Requeueable vs non-requeueable blocked state is explicit
- [ ] Claude clear-context behavior is explicit and does not silently swallow dispatch delivery
- [ ] Smart-tap preserves valid manager blocks even when surrounding pane output contains benign shell noise
- [ ] Audit trail preserves evidence of prevented duplicate dispatch
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Certification With Real Busy-Terminal Reproduction
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2]

### Description
Certify that the busy-terminal exclusivity breach seen in the double-feature run is closed under real dispatch conditions.

### Scope
- reproduce a terminal-already-busy scenario
- verify second dispatch is blocked before delivery
- verify operator-readable audit evidence explains the block
- verify invalid skill metadata is rejected pre-delivery
- verify Claude-targeted dispatches do not rely on implicit clear-context delivery

### Success Criteria
- second dispatch to an occupied terminal no longer lands
- certification evidence proves fail-closed behavior
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists for runtime-core changes
- no chain-created open items remain unresolved at feature closure

### Quality Gate
`gate_pr3_busy_terminal_certification`:
- [ ] All busy-terminal certification tests pass
- [ ] Reproduced second-dispatch attempt is blocked before delivery
- [ ] Certification evidence includes explicit blocked reason and no worker-side duplicate execution
- [ ] Invalid skill metadata cannot reach pending delivery in certification flow
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
- [ ] Feature closes with zero unresolved chain-created open items
