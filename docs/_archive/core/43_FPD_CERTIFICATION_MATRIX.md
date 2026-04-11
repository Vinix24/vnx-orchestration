# FP-D Certification Matrix

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-0
**Status**: Canonical
**Purpose**: Maps every in-scope scenario to its expected behavior, implementing PR, and verification evidence. FP-D is certified when every row passes.

---

## How To Use This Matrix

1. Each row is one scenario that FP-D must handle correctly.
2. The "Expected Outcome" column defines the correct behavior.
3. The "Implementing PR" column identifies which PR delivers the implementation.
4. The "Verification" column specifies what test or evidence proves correctness.
5. FP-D is certified when every row shows `pass` status.

---

## 1. Autonomy Policy Evaluation

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 1.1 | Automatic action evaluated (e.g., heartbeat_check) | Policy evaluator returns `outcome=automatic`; action proceeds; policy_evaluation event emitted | PR-1 | coordination_event with event_type=policy_evaluation, outcome=automatic |
| 1.2 | Gated action evaluated (e.g., dispatch_complete) | Policy evaluator returns `outcome=gated`; action blocked until gate authority confirms | PR-1 | coordination_event with outcome=gated; action does not proceed without T0/operator |
| 1.3 | Forbidden action attempted by runtime | Policy evaluator returns `outcome=forbidden`; action blocked; escalation_level=escalate | PR-1 | coordination_event with outcome=forbidden; escalation event at escalate level |
| 1.4 | Automatic action with exhausted budget | Outcome promoted from automatic to gated; escalation to hold | PR-1 | policy_evaluation event shows outcome=gated with budget_remaining=0 |
| 1.5 | Policy evaluation with VNX_AUTONOMY_EVALUATION=0 | Evaluation runs and emits events but outcomes are advisory-only; action follows pre-FP-D behavior | PR-1, PR-5 | Events emitted with enforcement_mode=shadow; action proceeds regardless |
| 1.6 | Unknown decision type evaluated | Evaluator returns forbidden (fail-closed); escalation to review_required | PR-1 | coordination_event with outcome=forbidden for unknown action |
| 1.7 | Policy class lookup for every defined decision type | Every decision type in the matrix maps to exactly one policy class | PR-0 | Contract document completeness check |

## 2. Escalation State Machine

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 2.1 | First delivery failure | Escalation level set to info; retry proceeds | PR-1 | escalation_state row with level=info; retry event exists |
| 2.2 | Second delivery failure (same dispatch) | Escalation promoted to review_required | PR-1 | escalation_state updated to review_required |
| 2.3 | Retry budget exhausted | Escalation promoted to hold; automatic actions blocked | PR-1 | escalation_state=hold; no further automatic retries |
| 2.4 | Forbidden action attempted | Escalation set directly to escalate | PR-1 | escalation_state=escalate; governance_override required to proceed |
| 2.5 | review_required unresolved for 30 minutes | Timeout promotion to hold | PR-1 | escalation_state transitions from review_required to hold after timeout |
| 2.6 | hold unresolved for 60 minutes | Timeout promotion to escalate | PR-1 | escalation_state transitions from hold to escalate after timeout |
| 2.7 | Operator releases hold | Escalation downgraded to info or review_required | PR-1 | governance_override event with outcome=granted; escalation_state decreased |
| 2.8 | T0 resolves escalation | Escalation downgraded; resolution_note recorded | PR-1 | governance_override event by t0; resolution_note non-empty |
| 2.9 | Runtime attempts de-escalation | Transition rejected; error logged | PR-1 | No state change; error coordination_event |
| 2.10 | Dead-letter accumulation (>3 in 1 hour) | Escalation set to escalate | PR-1 | escalation_state=escalate with trigger_category=dead_letter_accumulation |

## 3. Governance Overrides

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 3.1 | T0 overrides a hold state | governance_override event with outcome=granted; hold released | PR-1 | governance_overrides row with actor=t0, outcome=granted |
| 3.2 | Override without justification | Override rejected; justification required | PR-1 | governance_overrides row with outcome=denied, reason=missing_justification |
| 3.3 | Operator attempts to resolve escalate-level | Override rejected; only T0 can resolve escalate | PR-1 | governance_overrides row with outcome=denied, reason=insufficient_authority |
| 3.4 | Override does not modify policy matrix | Same action evaluated later still receives original classification | PR-1 | Subsequent policy_evaluation for same action returns same outcome |
| 3.5 | Override event queryable in audit view | Override appears in governance audit with full context | PR-4 | Audit view includes override with justification, actor, and scope |

## 4. Receipt Provenance Enrichment

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 4.1 | New receipt created with dispatch context | Receipt contains dispatch_id field matching originating dispatch | PR-2 | receipt.dispatch_id == dispatch.dispatch_id |
| 4.2 | Receipt created with git state | Receipt provenance contains git_ref, branch, is_dirty | PR-2 | provenance fields non-null and valid |
| 4.3 | Receipt without dispatch context | provenance_gap event emitted with gap_type=missing_dispatch_id | PR-2 | coordination_event with event_type=provenance_gap |
| 4.4 | Receipt backward compatibility | Existing receipt readers can still parse receipts with new fields | PR-2 | Old receipt parser handles new receipts without error |
| 4.5 | Receipt with cmd_id but no dispatch_id | cmd_id accepted as fallback; no provenance gap emitted | PR-2 | Receipt valid; no gap event |
| 4.6 | Mixed execution receipt (headless) | Headless dispatch receipt carries same provenance fields as interactive | PR-2 | Receipt from headless dispatch has dispatch_id, git_ref, trace_token |
| 4.7 | Channel-originated receipt | Receipt includes channel_origin from dispatch | PR-2 | receipt or dispatch metadata carries channel_origin |

## 5. Git Traceability Enforcement

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 5.1 | Commit with preferred trace token (`Dispatch-ID:`) | Validation passes; token extracted and resolved | PR-3 | commit-msg hook passes; CI check shows valid=true, format=preferred |
| 5.2 | Commit with legacy `PR-N` reference | Validation passes with format=legacy | PR-3 | CI check shows valid=true, format=legacy |
| 5.3 | Commit with legacy `FP-X` reference | Validation passes with format=legacy | PR-3 | CI check shows valid=true, format=legacy |
| 5.4 | Commit with no trace token (shadow mode) | Warning logged; commit allowed | PR-3 | commit-msg hook logs warning; provenance_gap event with severity=warning |
| 5.5 | Commit with no trace token (enforcement mode) | Commit blocked by hook; error message shown | PR-3 | commit-msg hook exits non-zero; error message references trace token |
| 5.6 | Commit with unresolvable dispatch ID | Warning logged; commit allowed (token format valid, ID unknown) | PR-3 | Validation shows format valid but dispatch unresolvable |
| 5.7 | prepare-commit-msg with VNX_CURRENT_DISPATCH_ID set | Dispatch-ID line auto-injected into commit template | PR-3 | Commit message template contains Dispatch-ID line |
| 5.8 | prepare-commit-msg without VNX_CURRENT_DISPATCH_ID | No injection; commit proceeds normally | PR-3 | Commit message template unchanged |
| 5.9 | Hook bypass via --no-verify | Commit proceeds; bypass logged as governance event | PR-3 | provenance_gap event with reason=hook_bypassed |
| 5.10 | CI trace token check on PR | All commits in PR scanned; report generated | PR-3 | CI output lists each commit with trace token status |
| 5.11 | CI provenance completeness check | Dispatch IDs matched against receipt log | PR-3, PR-4 | CI output shows which dispatch IDs have receipt evidence |
| 5.12 | Pre-FP-D commits in mixed branch | Pre-cutover commits exempt from enforcement | PR-3 | No errors for commits before enforcement boundary |

## 6. Provenance Verification And Audit

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 6.1 | Complete provenance chain | Verification returns chain_status=complete for dispatch with receipt, commit, and PR | PR-4 | provenance_registry row with chain_status=complete |
| 6.2 | Incomplete chain (missing receipt) | Verification returns chain_status=incomplete with gap_type=missing_receipt | PR-4 | provenance_registry shows incomplete; gaps_json lists missing_receipt |
| 6.3 | Broken chain (contradicting links) | Verification returns chain_status=broken with explanation | PR-4 | provenance_registry shows broken; gaps_json describes contradiction |
| 6.4 | Audit view shows policy outcomes | Governance audit surface lists policy evaluations with outcomes | PR-4 | Audit output includes automatic, gated, and forbidden outcomes |
| 6.5 | Audit view shows overrides | Override events visible with justification and scope | PR-4 | Audit output includes governance_overrides with full context |
| 6.6 | Audit view shows escalation history | Escalation state changes visible with timestamps and actors | PR-4 | Audit output shows escalation timeline per entity |
| 6.7 | Advisory guardrail: broken chain before merge | Warning surfaced when PR has incomplete provenance | PR-4 | Guardrail output warns before merge step |
| 6.8 | Advisory guardrail: unresolved escalation before completion | Warning surfaced when dispatch has hold or escalate level | PR-4 | Guardrail output warns about unresolved escalation |

## 7. Safe Autonomy Cutover (PR-5)

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 7.1 | VNX_AUTONOMY_EVALUATION=1 enabled | Policy evaluation outcomes are enforced; automatic actions proceed, gated actions block | PR-5 | Automatic actions complete; gated actions wait for authority |
| 7.2 | VNX_PROVENANCE_ENFORCEMENT=1 enabled | Commits without trace tokens are blocked by hook; CI check becomes blocking | PR-5 | Commits blocked without trace token; CI check fails on missing tokens |
| 7.3 | Rollback: both flags set to 0 | All behavior returns to pre-FP-D; events still emitted as advisory | PR-5 | Actions proceed regardless of policy evaluation; provenance warnings only |
| 7.4 | Automatic action within policy envelope | Action executes without operator intervention; audit trail complete | PR-5 | coordination_event chain shows automatic evaluation + successful execution |
| 7.5 | High-risk action remains gated after cutover | Completion, merge, and configuration actions still require T0/operator | PR-5 | Gated actions block; only proceed after explicit authority |
| 7.6 | End-to-end: dispatch -> evaluation -> execution -> receipt -> commit -> provenance verified | Full lifecycle with policy evaluation and provenance enforcement | PR-5 | All intermediate events exist; provenance chain is complete |
| 7.7 | FP-D does not grant autonomous merge authority | Branch merge and force push remain forbidden for runtime actors | PR-5 | branch_merge and force_push evaluated as forbidden; no autonomous merges |
| 7.8 | FP-D certification evidence complete | All matrix rows pass; residual risks documented | PR-5 | Certification report with all rows pass/fail |

---

## Certification Procedure

### Pre-Certification (Per-PR)

Each PR runs its quality gate tests covering the rows assigned to it. The gate must pass before the PR merges.

### Final Certification (PR-5)

After PR-5 merges, run full certification:

1. **Policy evaluation tests**: Every decision type evaluated with correct outcome (automatic/gated/forbidden).
2. **Escalation tests**: State machine transitions, timeout promotions, and de-escalation authority verified.
3. **Override tests**: Override flow, justification requirement, scope limitation, and audit trail verified.
4. **Receipt provenance tests**: Receipt enrichment, backward compatibility, gap detection verified.
5. **Git traceability tests**: Trace token validation, hook behavior, CI checks, and legacy acceptance verified.
6. **Provenance verification tests**: Chain reconstruction, gap detection, and audit views verified.
7. **Integration tests**: End-to-end flows with policy evaluation and provenance enforcement active.
8. **Rollback tests**: Both feature flags disabled returns to pre-FP-D behavior cleanly.

### Certification Evidence

The certification run produces a JSON report mapping each row number to:
- `status`: pass | fail | skip
- `evidence`: test name, event IDs, or log excerpts
- `notes`: any caveats or residual risks

FP-D is certified when all rows show `pass` status.

---

## Residual Risk Register

| Risk | Mitigation | Owner |
|------|-----------|-------|
| Policy classification may need refinement after real-world governance evaluation | Monitor policy evaluation distribution and escalation frequency; adjust matrix | T0 |
| Escalation timeout thresholds may be too aggressive or too lenient | Thresholds are configurable via VNX_REVIEW_TIMEOUT_MIN and VNX_HOLD_TIMEOUT_MIN | PR-1 |
| Legacy trace token acceptance may allow weak provenance during extended transition | Track legacy format usage; set sunset date for legacy acceptance | PR-3 |
| CI enforcement depends on repository CI configuration being maintained | Document CI setup; include CI config in governance audit checks | PR-3 |
| Provenance verification may produce false positives for rapidly-committed work | Verification includes timing tolerance; receipts may lag commits slightly | PR-4 |
| Override mechanism could be abused without social controls | Override frequency is visible in audit views; T0 reviews override patterns | PR-4 |
| Feature flag rollback may leave partial state (e.g., some receipts enriched, some not) | Rollback is behavioral only; enriched data remains valid and backward-compatible | PR-5 |
