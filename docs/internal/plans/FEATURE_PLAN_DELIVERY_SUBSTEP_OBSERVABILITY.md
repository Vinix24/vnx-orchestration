# Feature: Delivery Substep Observability

## PR-0: Delivery Substep Observability Contract
**Track**: C
**Priority**: P2
**Complexity**: Low
**Risk**: Low
**Skill**: @architect
**Estimated Time**: 1-2 hours
**Dependencies**: []

### Description
Define the canonical contract for fine-grained delivery rejection logging so operators can distinguish which substep of the delivery pipeline failed when a dispatch is rejected.

### Scope
- Define the delivery substep taxonomy:
  - `send_skill`: skill command injection into terminal
  - `load_buffer`: tmux paste buffer load
  - `paste_buffer`: tmux paste buffer paste
  - `enter`: tmux send-keys Enter
- Define required log fields per substep failure (timestamp, terminal, dispatch_id, substep, exit_code, stderr)
- Define how substep failure integrates with existing rejection annotation at `scripts/dispatcher_v8_minimal.sh:1897`
- Lock non-goals so this does not expand into full delivery pipeline instrumentation

### Success Criteria
- Every delivery substep has a named identifier
- Rejection annotations include the failing substep
- Operators can distinguish buffer failure from skill injection failure from enter failure
- Log format is parseable by existing audit tooling

### Quality Gate
`gate_pr0_delivery_substep_contract`:
- [ ] Contract defines named identifiers for every delivery substep
- [ ] Contract defines required log fields per substep failure
- [ ] Contract defines integration with existing rejection annotation format
- [ ] Contract preserves compatibility with existing audit parsing

---

## PR-1: Fine-Grained Delivery Substep Rejection Logging
**Track**: B
**Priority**: P2
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Add substep-level failure identification to the delivery pipeline so rejection annotations specify exactly which delivery substep failed.

### Scope
- Instrument each delivery substep in `scripts/dispatcher_v8_minimal.sh` with named failure tracking:
  - `send_skill`: skill command injection
  - `load_buffer`: tmux load-buffer
  - `paste_buffer`: tmux paste-buffer
  - `enter`: tmux send-keys Enter
- Replace generic `[REJECTED: Dispatch failed during execution]` annotation at line 1897 with substep-specific annotation
- Emit audit event with substep identifier on failure
- Add tests for:
  - Each substep failure producing correct annotation
  - Generic rejection replaced with substep-specific rejection

### Success Criteria
- Delivery rejection annotations include the failing substep name
- Each substep failure is independently identifiable in audit output
- Generic rejection annotation is replaced, not supplemented
- Existing successful delivery paths are not affected

### Quality Gate
`gate_pr1_delivery_substep_logging`:
- [ ] All delivery substep logging tests pass
- [ ] Each substep failure produces substep-specific rejection annotation under test
- [ ] Generic rejection annotation is no longer emitted for substep failures
- [ ] Successful delivery paths remain unaffected under test

---

## PR-2: Delivery Substep Observability Certification
**Track**: C
**Priority**: P2
**Complexity**: Low
**Risk**: Low
**Skill**: @quality-engineer
**Estimated Time**: 1-2 hours
**Dependencies**: [PR-1]

### Description
Certify the delivery substep observability by reproducing delivery failures at each substep and verifying the rejection annotations are correct and parseable.

### Scope
- Reproduce failure at each delivery substep and verify annotation
- Verify audit output is parseable by existing tooling
- Require Gemini review and Codex final gate on certification

### Success Criteria
- Each substep failure is correctly identified in certification evidence
- Audit output format is compatible with existing parsing
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists and passes

### Quality Gate
`gate_pr2_delivery_substep_certification`:
- [ ] All delivery substep certification tests pass
- [ ] Each substep failure annotation is verified in certification evidence
- [ ] Audit output format compatibility is verified in certification evidence
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
