# Residual Governance Bugfix Sweep Contract

**Status**: Canonical
**Feature**: Residual Governance Bugfix Sweep
**PR**: PR-0
**Gate**: `gate_pr0_residual_bugfix_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document clusters the remaining warn-level governance defects into explicit implementation buckets, defines what counts as closure for each, and calls out what is intentionally deferred beyond this sweep. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

After the hardening chain (Features 5-9) and the bridge lane features (B1-B3), the VNX governance system has resolved its chain-critical defects:

| Defect | Resolved By | Status |
|--------|------------|--------|
| rc_register non-fatal → FK violation | B1: Fail-Closed Bootstrap (Contract 170) | Fixed |
| Headless gate queued without runner | B2: Gate Execution Lifecycle (Contract 180) | Fixed |
| Operator scrollback regression (OI-163) | B3: Retry-Loop UX Protection | Fixed |
| Requeue-to-reject regression | Feature 8: RC-3 disposition fix (`7f4c4a7`) | Fixed |
| canonical_check_parse_error misclassification | Feature 8: RC-2 classification fix (`b091e90`) | Fixed |
| Empty/none role bypass to terminal ops | Feature 8: RC-4 empty role guard (`7f4c4a7`) | Fixed |
| Substep-level delivery failure logging | Feature 9: Substep annotation (`63718a0`) | Fixed |
| Gate evidence PR-scoping | Feature 10: GE-1 through GE-9 (`b9a49df`) | Fixed |

However, a set of smaller governance defects remain as warn-level items. These are not chain-critical but create friction during autonomous operation. This contract bundles them into a deliberate cleanup feature.

### 1.2 Current Open Item State

From `.vnx-data/state/open_items.json` (snapshot at contract creation):

| Category | Count | Description |
|----------|-------|-------------|
| Blocker (complexity) | 24 | File/function size threshold violations |
| Warn (governance) | 4 | Runtime behavior defects (OI-024, OI-048, OI-078, OI-163) |
| Warn (test complexity) | 5 | Test file/function size warnings (OI-220 through OI-224) |
| Info | 52 | Advisory observations |
| Done | 182 | Resolved in prior features |
| Deferred | 2 | Explicitly deferred |
| Wontfix | 1 | Intentional design tradeoff |

---

## 2. Residual Bug Clusters

### 2.1 Cluster A: Semantics And Classification Residuals

**Scope**: Fix remaining semantic mismatches between documented contracts and runtime behavior.

| ID | Defect | Current State | Fix Required |
|----|--------|---------------|-------------|
| RES-A1 | `_classify_blocked_dispatch` wildcard swallows new `delivery_failed:{code}` patterns | Fixed in PR-1 of this chain (Contract 160 Section 4.4) | Verify no regression |
| RES-A2 | `duplicate_delivery_prevented` audit event has no test coverage | Documented in Contract 140 RC-5 | Add deterministic test that exercises the classification |
| RES-A3 | Intelligence gathering blocking semantics documented inconsistently | Contract 140 RC-6 says "optional, non-fatal on parse" is misleading | Update docstring in dispatcher to match RC-6 |
| RES-A4 | `_classify_blocked_dispatch` missing `recovery_cooldown_deferred` case | Cooldown deferral falls through to `*` wildcard → `invalid false` | Add `recovery_cooldown_deferred` → `ambiguous true` case |

**Acceptance**: All 4 items either fixed with test or verified as already-fixed with regression test.

### 2.2 Cluster B: Mode Control And Input Safety Residuals

**Scope**: Address the gap between pre-lease mode control and post-lease input mode guard.

| ID | Defect | Current State | Fix Required |
|----|--------|---------------|-------------|
| RES-B1 | OI-024: `configure_terminal_mode` send-keys not guarded by input-mode probe | `configure_terminal_mode` (line ~1305) runs before lease; input-mode guard (line ~1588) runs after lease | Add best-effort pane mode check before `configure_terminal_mode` send-keys; non-blocking since no lease is held |
| RES-B2 | `configure_terminal_mode` failure reason not structured | Returns 1 with `log_structured_failure "mode_configuration_failed"` but no canonical failure code from Contract 160 | Map to `pre_mode_configuration` failure code |

**Acceptance**: OI-024 mitigated with best-effort pre-lease probe (not full recovery — that requires lease). RES-B2 code mapped.

### 2.3 Cluster C: CI And Evidence Path Residuals

**Scope**: Fix path configuration issues in CI verification and evidence lookup.

| ID | Defect | Current State | Fix Required |
|----|--------|---------------|-------------|
| RES-C1 | OI-078: Profile C CI looks for `PR_QUEUE.md` in `VNX_HOME` instead of `PROJECT_ROOT` | CI check fails silently on correct repos | Fix path reference in closure verification script |
| RES-C2 | Gate result `report_path` uses absolute paths that break across worktrees | Absolute paths in result JSON may point to wrong worktree | Normalize `report_path` to be relative to `VNX_DATA_DIR` or verify existence at read time |

**Acceptance**: Both path bugs fixed; CI Profile C passes on correct repos.

### 2.4 Cluster D: Non-Fatal Observability Gaps

**Scope**: Tighten observability for operations currently marked "non-fatal" that silently lose audit data.

| ID | Defect | Current State | Fix Required |
|----|--------|---------------|-------------|
| RES-D1 | `rc_delivery_start` failure silently loses `attempt_id` | When `rc_delivery_start` returns empty, subsequent `rc_delivery_failure` no-ops (guards on non-empty `attempt_id`) | Log structured failure when `attempt_id` is empty (per Contract 90 DFL-2) |
| RES-D2 | `rc_delivery_success` failure is fire-and-forget | Success recording failure means broker shows `delivering` instead of `accepted` | Log structured failure; add `delivery_success_record_failed` to audit stream |
| RES-D3 | Receipt footer generation failure is silent | `generate_receipt_footer` failure logged as warning, receipt continues without footer | Acceptable as-is (non-fatal, receipt is still valid without footer) — document as intentional |

**Acceptance**: RES-D1 and RES-D2 get structured failure logging. RES-D3 documented as intentional.

---

## 3. Acceptance Boundaries

### 3.1 What Counts As Closure

For each cluster, closure requires:

| Cluster | Closure Criteria |
|---------|-----------------|
| A (Semantics) | 4 items fixed or verified; test coverage for each; no classification regression |
| B (Mode Control) | OI-024 mitigated with pre-lease probe; `pre_mode_configuration` code mapped; test for probe-before-mode-control |
| C (CI/Evidence) | OI-078 fixed; Profile C CI passes; report_path handling documented |
| D (Observability) | RES-D1 and RES-D2 get structured failure events; RES-D3 documented as intentional in contract |

### 3.2 What Does NOT Count As Closure

- Complexity violations (OI-176 through OI-238) are NOT in scope. They require architectural refactoring, not bugfixes.
- OI-048 (headless gate reliability) is addressed by Contract 180 (stall detection + timeout). Further Gemini/Codex CLI reliability is a provider-level concern, not a governance defect.
- Test file complexity warnings (OI-220 through OI-224) are NOT in scope. Test files are allowed to be longer than production code.

### 3.3 Scope Creep Detection

A PR is out of scope for this sweep if it:
- Refactors file/function structure to reduce line counts (that's a separate refactoring feature)
- Adds new governance capabilities beyond fixing existing defects
- Modifies contracts beyond correcting documented inconsistencies
- Changes dispatch delivery mechanics (not a bugfix)
- Adds new audit event types (only fix existing audit gaps)

---

## 4. Deferred Residuals

These items are explicitly deferred beyond this sweep with rationale:

| ID | Defect | Deferred Rationale |
|----|--------|--------------------|
| DEF-1 | OI-176 through OI-219: Complexity violations (24 blockers) | Require architectural decomposition of dispatcher, runtime_coordination, and review_gate_manager. This is a multi-PR refactoring feature, not a bugfix sweep. |
| DEF-2 | Full input-mode guard before `configure_terminal_mode` | Requires moving the guard before lease acquisition or restructuring the delivery pipeline. Current mitigation (best-effort probe) is sufficient for this sweep. |
| DEF-3 | OI-048 root cause (Gemini stdout flush, Codex stall) | Provider CLI behavior is upstream of VNX. Contract 180 added stall detection and timeout as mitigation. Root cause fix requires Gemini/Codex CLI changes. |
| DEF-4 | Worker-side failure detection after accepted state | Contract 90 DFL-6 explicitly scopes this out. TTL + reconciler is the safety net. |
| DEF-5 | Test file complexity (OI-220 through OI-224) | Test files with comprehensive certification scenarios are expected to be longer. These warnings are informational, not actionable. |

---

## 5. Implementation Constraints For PR-1

### 5.1 Cluster A

1. **RES-A2**: Add a test that exercises `duplicate_delivery_prevented` event classification. The test must create a scenario where the canonical lease check returns `BLOCK:canonical_lease:leased:{same_dispatch_id}` and verify the event type is `duplicate_delivery_prevented`.
2. **RES-A3**: Update the intelligence gathering docstring/comment in `process_dispatches()` to clarify: command failure blocks, parse failure does not block.
3. **RES-A4**: Add `recovery_cooldown_deferred` case to `_classify_blocked_dispatch()`:
   ```bash
   recovery_cooldown_deferred)
       echo "ambiguous true" ;;
   ```

### 5.2 Cluster B

4. **RES-B1**: Add a best-effort `_input_mode_probe` call before `configure_terminal_mode` in `dispatch_with_skill_activation()`. If the probe returns `pane_in_mode=1`, log a structured warning and skip mode configuration (return 1 with no lease held). This is Phase 0 — no lease cleanup needed.
5. **RES-B2**: Change the `log_structured_failure` call for mode configuration failure to include the `pre_mode_configuration` failure code.

### 5.3 Cluster C

6. **RES-C1**: Fix `PR_QUEUE.md` lookup in the Profile C closure verification script to use `PROJECT_ROOT` instead of `VNX_HOME`.
7. **RES-C2**: Document in Contract 130 that `report_path` should be verified to exist at read time (already enforced by `record_result()` at write time).

### 5.4 Cluster D

8. **RES-D1**: Add structured failure event when `rc_delivery_start` returns empty `attempt_id`:
   ```bash
   if [[ -z "$_rc_attempt_id" ]]; then
       log_structured_failure "delivery_start_no_attempt" \
           "delivery_start returned empty attempt_id" \
           "dispatch=$dispatch_id terminal=$terminal_id"
   fi
   ```
9. **RES-D2**: Add structured failure event when `rc_delivery_success` fails.
10. **RES-D3**: Add comment in dispatcher documenting receipt footer generation as intentionally non-fatal.

### 5.5 Test Requirements

| Test | Validates |
|------|-----------|
| `duplicate_delivery_prevented` event classification | RES-A2 |
| `recovery_cooldown_deferred` classified as `ambiguous true` | RES-A4 |
| Pre-lease pane probe skips mode config on blocked pane | RES-B1 |
| `pre_mode_configuration` failure code in structured event | RES-B2 |
| Profile C CI path resolution | RES-C1 |
| Empty `attempt_id` produces structured failure | RES-D1 |

---

## 6. Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Complexity refactoring | DEF-1: separate multi-PR feature |
| NG-2 | New governance capabilities | Sweep scope is cleanup, not new features |
| NG-3 | Provider CLI reliability fixes | DEF-3: upstream concern |
| NG-4 | Worker→dispatcher rejection channel | DEF-4: Contract 90 DFL-6 |
| NG-5 | Test file complexity reduction | DEF-5: informational, not actionable |

---

## Appendix A: Residual Item Quick Reference

| ID | Cluster | Severity | Status | Summary |
|----|---------|----------|--------|---------|
| RES-A1 | A | warn | verify-only | Classification wildcard for delivery_failed patterns |
| RES-A2 | A | warn | needs-test | duplicate_delivery_prevented has no test coverage |
| RES-A3 | A | info | needs-docfix | Intelligence blocking semantics docstring misleading |
| RES-A4 | A | warn | needs-fix | recovery_cooldown_deferred falls to wildcard |
| RES-B1 | B | warn | needs-fix | OI-024: mode control not guarded (pre-lease probe) |
| RES-B2 | B | info | needs-fix | Mode config failure missing canonical code |
| RES-C1 | C | warn | needs-fix | OI-078: Profile C CI path |
| RES-C2 | C | info | needs-doc | report_path cross-worktree handling |
| RES-D1 | D | warn | needs-fix | Empty attempt_id silently loses failure record |
| RES-D2 | D | info | needs-fix | delivery_success failure is silent |
| RES-D3 | D | info | intentional | Receipt footer generation failure is acceptable |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Requeue And Classification (140) | RES-A1 verifies RC-3/RC-4. RES-A2 validates RC-5. RES-A3 clarifies RC-6. |
| Delivery Failure Lease (90) | RES-D1 implements DFL-2 (attempt_id fallback). RES-D2 adds success recording observability. |
| Delivery Failure Logging (160) | RES-B2 maps mode config failure to `pre_mode_configuration` code. |
| Input-Ready Terminal (110) | RES-B1 extends input-mode guard to pre-lease phase (best-effort). |
| PR-Scoped Gate Evidence (130) | RES-C2 documents report_path existence verification. |
| Fail-Closed Bootstrap (170) | OI-022 already resolved by BOOT-6/BOOT-7. No residual in this sweep. |
| Gate Execution Lifecycle (180) | OI-048 mitigated by GATE-6/GATE-7. Root cause deferred (DEF-3). |
