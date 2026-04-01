# Feature: Dispatch Requeue And Classification Accuracy

## PR-0: Requeue And Classification Accuracy Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical contract for dispatch requeue semantics, failure classification accuracy, role validation, and audit event reachability so dispatches that should be retried are not silently rejected and failure reasons are correctly classified.

### Scope
- Define requeue vs reject semantics for `dispatch_with_skill_activation` return codes in `scripts/dispatcher_v8_minimal.sh:1895-1899` (OI-060: requeueable blocks currently get rejected instead of deferred)
- Define classification rules for `canonical_check_parse_error` in `scripts/dispatcher_v8_minimal.sh:109-121` (OI-061: currently mislabeled as `invalid false` instead of `ambiguous`)
- Define empty/none role pre-validation behavior in `scripts/dispatcher_v8_minimal.sh:1771,1785` (OI-059: empty role bypasses skill pre-validation guard)
- Define reachability requirement for `duplicate_delivery_prevented` audit event in `scripts/dispatcher_v8_minimal.sh:1527-1530` (OI-062: unreachable because RuntimeCore allows same-dispatch reuse)
- Resolve contract inconsistency for intelligence gathering blocking semantics in `docs/core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md:241-242` (OI-058: says non-blocking then blocking)
- Lock non-goals so this does not become a full dispatcher rewrite

### Success Criteria
- Requeueable failures have a deterministic deferred path distinct from permanent rejection
- Classification of canonical_check_parse_error is correct and tested
- Empty/none role cannot silently bypass pre-validation
- duplicate_delivery_prevented is either reachable or explicitly removed
- Intelligence gathering blocking semantics are unambiguous in the contract

### Quality Gate
`gate_pr0_requeue_classification_contract`:
- [ ] Contract defines requeue vs reject return code semantics for every dispatch_with_skill_activation exit path
- [ ] Contract defines correct classification for canonical_check_parse_error
- [ ] Contract requires empty/none role to fail pre-validation rather than bypass it
- [ ] Contract resolves duplicate_delivery_prevented reachability (make reachable or remove)
- [ ] Contract resolves intelligence gathering blocking semantics ambiguity

---

## PR-1: Fix Requeue-To-Reject Regression And Empty Role Guard
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Scope
- Change `process_dispatches` to distinguish requeueable return codes from permanent failures in `scripts/dispatcher_v8_minimal.sh:1895-1899`
- Add a deferred path that moves requeueable dispatches back to pending instead of rejected
- Add explicit empty/none role rejection in pre-validation guard at `scripts/dispatcher_v8_minimal.sh:1771,1785`
- Fix contract text in `docs/core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md:241-242` to remove blocking ambiguity
- Add tests for:
  - Requeueable dispatch moved to pending instead of rejected
  - Empty role rejected at pre-validation
  - None role rejected at pre-validation

### Description
Fix the requeue-to-reject regression so requeueable dispatches are deferred rather than permanently rejected, and close the empty role bypass in skill pre-validation.

### Success Criteria
- Requeueable dispatches are moved back to pending, not rejected
- Empty and none role dispatches fail at pre-validation with explicit reason
- Contract text for intelligence gathering is internally consistent
- Existing permanent rejection paths are not affected

### Quality Gate
`gate_pr1_requeue_and_role_guard`:
- [ ] All requeue and role guard tests pass
- [ ] Requeueable dispatch defers to pending under test
- [ ] Empty/none role fails pre-validation under test with explicit reason
- [ ] Contract text for intelligence gathering blocking semantics is internally consistent

---

## PR-2: Classification Accuracy And Audit Event Reachability
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0, PR-1]

### Description
Fix canonical_check_parse_error classification and resolve the unreachable duplicate_delivery_prevented audit event so all classification paths are correct and all audit events are either reachable or explicitly removed.

### Scope
- Fix `_classify_blocked_dispatch` in `scripts/dispatcher_v8_minimal.sh:109-121` to map `canonical_check_parse_error` to `ambiguous` instead of falling through to `invalid false`
- Resolve `duplicate_delivery_prevented` unreachability in `scripts/dispatcher_v8_minimal.sh:1527-1530`:
  - Either make the code path reachable by checking before RuntimeCore allows same-dispatch reuse
  - Or remove the dead code path with explicit rationale
- Add tests for:
  - canonical_check_parse_error classified as ambiguous
  - duplicate_delivery_prevented either reachable or removed

### Success Criteria
- canonical_check_parse_error is classified as ambiguous, not invalid
- duplicate_delivery_prevented is either exercisable in tests or cleanly removed
- No other classification regressions introduced
- Audit trail accuracy is improved

### Quality Gate
`gate_pr2_classification_and_audit_accuracy`:
- [ ] All classification accuracy tests pass
- [ ] canonical_check_parse_error maps to ambiguous under test
- [ ] duplicate_delivery_prevented is either reachable under test or cleanly removed with rationale
- [ ] No classification regressions for other failure reasons

---

## PR-3: Dispatch Requeue And Classification Certification
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1, PR-2]

### Description
Certify the requeue, classification, and role guard fixes by exercising realistic dispatch flows that previously triggered incorrect rejection, misclassification, or silent bypass.

### Scope
- Reproduce requeueable dispatch scenario and verify deferral to pending
- Reproduce empty/none role dispatch and verify pre-validation rejection
- Reproduce canonical_check_parse_error and verify ambiguous classification
- Verify duplicate_delivery_prevented resolution
- Require Gemini review and Codex final gate on certification and dispatcher PRs

### Success Criteria
- Requeueable dispatches defer correctly under real dispatch flow
- Empty/none role dispatches are caught before delivery
- Classification accuracy is correct for all tested failure reasons
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists and passes

### Quality Gate
`gate_pr3_requeue_classification_certification`:
- [ ] All requeue and classification certification tests pass
- [ ] Requeueable dispatch deferral is verified in certification evidence
- [ ] Empty/none role rejection is verified in certification evidence
- [ ] Classification accuracy is verified for canonical_check_parse_error in certification evidence
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
