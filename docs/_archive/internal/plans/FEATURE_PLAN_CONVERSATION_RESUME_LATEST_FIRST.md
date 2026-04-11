# Feature: Conversation Resume And Latest-First Timeline

## PR-0: Conversation Resume Contract And Source-Of-Truth Boundaries
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical contract for conversation resume inside the Operator OS so the operator can reopen the right Claude session quickly without confusing conversation data, tmux state, and runtime truth.

### Scope
- Define the source of truth for:
  - conversation index
  - worktree/session linkage
  - latest message ordering
  - rotation-summary continuity
- Define operator actions:
  - open recent session
  - flip sort order
  - filter by worktree or project
- Lock non-goals to prevent this from becoming a full chat client rewrite

### Success Criteria
- Conversation data ownership is explicit
- Latest-first becomes the canonical default
- Rotated-context sessions remain resumable and understandable
- The contract fits the current Operator OS direction cleanly

### Quality Gate
`gate_pr0_conversation_resume_contract`:
- [ ] Contract defines source of truth for conversation index, worktree/session linkage, and rotation continuity
- [ ] Contract states latest-first as the default operator view
- [ ] Contract distinguishes conversation resume from tmux attach and other runtime actions
- [ ] Contract blocks scope creep into a generic chat UI rewrite

---

## PR-1: Conversation Read Model And Worktree Linking
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Build the machine-readable read model that links conversations to worktrees, terminals, session ids, and rotation-summary continuity hints.

### Scope
- Expose recent conversation metadata with worktree-aware grouping
- Include stable fields for last message, session id, cwd/project path, and terminal identity when available
- Include rotation-summary or context-continuity hints when present
- Add tests for multiple projects and mixed session histories

### Success Criteria
- Operator can resolve which recent sessions belong to which worktree
- Read model does not guess from brittle UI state
- Latest message timestamps are available for latest-first sorting
- Rotated sessions retain continuity metadata

### Quality Gate
`gate_pr1_conversation_read_model`:
- [ ] All conversation read-model tests pass
- [ ] Read model links sessions to the correct worktree or project path
- [ ] Latest message timestamps support deterministic latest-first ordering
- [ ] Rotation-summary continuity metadata is exposed when present

---

## PR-2: Latest-First Timeline UI With Reversible Sort
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @frontend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0, PR-1]

### Description
Add the operator-facing timeline view that defaults to latest-first while allowing the operator to flip to oldest-first without losing context or selection.

### Scope
- Default message timeline and session list to latest-first ordering
- Add explicit sort toggle for oldest-first when needed
- Preserve selected session/worktree context when sorting changes
- Surface compacted-session continuity hints in the same view

### Success Criteria
- Latest-first is the default in the message timeline
- Operators can reverse sort without losing context
- Timeline remains readable for long rotated sessions
- Existing conversation-manager behavior does not regress unexpectedly

### Quality Gate
`gate_pr2_latest_first_timeline`:
- [ ] All conversation UI tests pass
- [ ] Latest-first is the default for the main operator timeline
- [ ] Sort toggle can switch between latest-first and oldest-first without dropping selected context
- [ ] Rotated-session continuity remains visible in the timeline

---

## PR-3: One-Click Resume From Correct Worktree Context
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1, PR-2]

### Description
Add deterministic resume actions so the selected conversation reopens in the correct worktree context rather than only showing history.

### Scope
- Implement stable resume action for selected conversation/session context
- Ensure cwd/worktree context is correct before resuming
- Block cross-worktree resume mistakes deterministically
- Return actionable errors when resume cannot be performed safely

### Success Criteria
- Operator can resume the intended conversation from the intended worktree
- Cross-worktree mistakes are blocked
- Resume action does not depend on fragile terminal injection
- Failure cases are explicit and operator-readable

### Quality Gate
`gate_pr3_one_click_resume`:
- [ ] All conversation resume tests pass
- [ ] Resume action opens the selected conversation with the correct worktree or cwd
- [ ] Cross-worktree resume mistakes are blocked by deterministic validation
- [ ] Resume path for interactive Claude sessions does not rely on terminal injection

---

## PR-4: Certification With Gemini Review And Codex Final Gate
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2, PR-3]

### Description
Certify that conversation resume and latest-first rendering materially reduce operator friction in realistic multi-worktree operation.

### Scope
- Run a realistic session-loss / reopen drill across at least two worktrees
- Capture evidence that the operator can find the right recent session faster than before
- Require Gemini review and Codex final gate on the certification PR and any risky resume-control PRs
- Produce residual risks for long-session continuity and missing session metadata

### Success Criteria
- Two-worktree session recovery flow is proven end-to-end
- Latest-first timeline makes recent work immediately visible
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists for risky resume-control logic

### Quality Gate
`gate_pr4_conversation_resume_certification`:
- [ ] All certification tests pass for two-worktree conversation recovery
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
- [ ] Residual risk report explains any remaining gaps in conversation continuity or missing metadata
