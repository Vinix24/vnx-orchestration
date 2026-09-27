# Requeue And Classification Accuracy Contract

**Status**: Canonical
**Feature**: Dispatch Requeue And Classification Accuracy
**PR**: PR-0
**Gate**: `gate_pr0_requeue_classification_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the canonical rules for how dispatch failures are classified, which failures allow requeue, how return codes drive file movement, and how audit events must be reachable. All downstream PRs (PR-1 through PR-3) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

The dispatcher classifies blocked dispatches into three categories (`busy`, `ambiguous`, `invalid`) and uses this classification to decide whether a dispatch should be retried or permanently rejected. Five accuracy gaps remain:

**Requeue-to-reject regression**: When `dispatch_with_skill_activation()` returns 1, `process_dispatches()` checks for the `[SKILL_INVALID]` marker. If the marker is absent (e.g., the dispatch was blocked by a requeueable condition like `blocked_input_mode` or `canonical_lease_expired`), the dispatch is moved to `rejected/` — permanently lost. Requeueable failures must defer to `pending/`, not reject.

**canonical_check_parse_error misclassification**: When the canonical lease check's JSON parse fails, the fallback reason is `canonical_check_parse_error`. This currently falls through to the `*` wildcard in `_classify_blocked_dispatch()`, classifying it as `invalid false` (permanent reject). It should be `ambiguous true` (transient — requeue).

**Empty role bypass**: When `agent_role` is empty or `"none"`, the pre-validation guard (`if [ -n "$agent_role" ] && [ "$agent_role" != "none" ]`) skips validation entirely. The dispatch proceeds to `dispatch_with_skill_activation()` where `map_role_to_skill("")` returns empty, triggering `[SKILL_INVALID]` deep in the delivery path — after terminal operations may have already started. Empty role should be caught at pre-validation.

**duplicate_delivery_prevented reachability**: The `duplicate_delivery_prevented` event is emitted when a canonical lease is held by the same dispatch_id being dispatched. This path IS reachable (confirmed via code inspection at lines 1532-1541) when a dispatch is retried after a prior delivery attempt timed out but the lease was not yet expired. However, it has no test coverage.

**Intelligence gathering blocking ambiguity**: Both `validate` and `gather` operations in `gather_intelligence.py` block dispatch on failure (rc != 0) with `[DEPENDENCY_ERROR]`. But the contract text in the feature plan says intelligence gathering is "optional, non-fatal on parse" — this is only true for result parsing, not for the command execution itself. The blocking behavior is correct but the documentation is misleading.

### 1.2 The Fix

1. Distinguish requeueable return codes from permanent failures in the dispatch result path.
2. Add `canonical_check_parse_error` to the `ambiguous true` classification.
3. Catch empty/none role at pre-validation before any terminal operations.
4. Ensure `duplicate_delivery_prevented` is either tested or removed.
5. Make intelligence gathering blocking semantics unambiguous.

---

## 2. Failure Classification

### 2.1 Classification Categories

Every dispatch failure is classified into exactly one of three categories:

| Category | Meaning | Requeueable | File Disposition |
|----------|---------|-------------|-----------------|
| **busy** | Terminal is legitimately occupied by another dispatch | Yes | Remain in `pending/` — retry on next loop |
| **ambiguous** | Terminal state is indeterminate or pane is non-interactive | Yes | Remain in `pending/` — retry after condition resolves |
| **invalid** | Dispatch metadata, skill, or dependency is permanently broken | No | Move to `rejected/` — requires manual intervention |

### 2.2 Classification Table

**RC-1 (Requeue Classification Rule 1)**: Every block reason MUST map to exactly one category. The classification function `_classify_blocked_dispatch()` is the single authority.

| Block Reason | Category | Requeueable | Rationale |
|-------------|----------|-------------|-----------|
| `active_claim:*` | busy | Yes | Another dispatch holds terminal claim |
| `status_claimed:*` | busy | Yes | Terminal status shows active claim |
| `canonical_lease:leased:*` | busy | Yes | Canonical lease held (healthy) |
| `canonical_lease:lease_expired*` | ambiguous | Yes | Lease expired — reconciler may recover |
| `canonical_lease:lease_expired_recovering` | ambiguous | Yes | Recovery in progress |
| `canonical_check_error:*` | ambiguous | Yes | Canonical lease check failed (transient) |
| `canonical_check_parse_error` | **ambiguous** | **Yes** | JSON parse of lease check failed — transient, not a metadata defect |
| `canonical_lease_acquire_failed` | ambiguous | Yes | Lease acquisition failed (contention) |
| `terminal_state_unreadable` | ambiguous | Yes | State file unreadable |
| `recent_*` | ambiguous | Yes | Recent activity detected |
| `blocked_input_mode` | ambiguous | Yes | Pane in copy/search mode |
| `recovery_failed` | ambiguous | Yes | Input mode recovery exhausted |
| `pane_dead` | ambiguous | Yes | Pane process exited |
| `probe_failed` | ambiguous | Yes | Input mode probe unreachable |
| `input_mode_blocked` | ambiguous | Yes | Input mode guard blocked delivery |
| Everything else (`*`) | invalid | No | Unrecognized reason — permanent reject |

### 2.3 The canonical_check_parse_error Fix

**RC-2 (Requeue Classification Rule 2)**: `canonical_check_parse_error` MUST be classified as `ambiguous true`, not `invalid false`. This reason occurs when the Python JSON parser fails to read the canonical lease check result — a transient failure (e.g., concurrent write, I/O error), not a permanent metadata defect.

The fix in `_classify_blocked_dispatch()`:
```bash
# CURRENT (wrong): falls through to * → "invalid false"
# CORRECT: explicit case
canonical_check_parse_error)
    echo "ambiguous true" ;;
```

---

## 3. Return Code Semantics

### 3.1 dispatch_with_skill_activation() Return Codes

| Return Code | Meaning | Lease Held | File State |
|-------------|---------|------------|-----------|
| **0** | Delivery succeeded | Yes (leased) | Moved to `active/` |
| **1** | Delivery failed | No (released or never acquired) | Remains in `pending/` |

Return code 1 does NOT indicate whether the failure is requeueable or permanent. The classification is determined by the block reason, not the return code.

### 3.2 process_dispatches() Disposition Logic

**RC-3 (Requeue Classification Rule 3)**: When `dispatch_with_skill_activation()` returns 1, the disposition MUST distinguish requeueable failures from permanent rejections.

Current behavior (wrong):
```
return 1 → check [SKILL_INVALID] marker
  ├─ marker present → stay in pending (requeue)
  └─ marker absent → move to rejected (WRONG for requeueable failures)
```

Required behavior:
```
return 1 → check [SKILL_INVALID] or [DEPENDENCY_ERROR] or [REJECTED] marker
  ├─ [REJECTED] marker → move to rejected (permanent)
  ├─ [SKILL_INVALID] marker → stay in pending (wait for edit)
  ├─ [DEPENDENCY_ERROR] marker → stay in pending (wait for dependency)
  └─ no marker → stay in pending (requeueable — blocked by terminal state)
```

The key change: **no marker means requeueable**, not permanent reject. The only path to `rejected/` is an explicit `[REJECTED]` marker written by the failure path that determined the failure is permanent.

### 3.3 Marker Semantics

| Marker | Meaning | File Disposition | Recovery |
|--------|---------|-----------------|----------|
| `[SKILL_INVALID]` | Role/skill not found in registry | Stay in `pending/` | Edit dispatch to fix role |
| `[DEPENDENCY_ERROR]` | Runtime dependency unavailable | Stay in `pending/` | Resolve dependency, retry |
| `[REJECTED: ...]` | Permanent failure — dispatch is invalid | Move to `rejected/` | Manual rework required |
| No marker | Requeueable transient failure | Stay in `pending/` | Automatic retry on next loop |

---

## 4. Empty Role Pre-Validation

### 4.1 The Problem

When `agent_role` is empty, `""`, `"none"`, or `"None"`, the pre-validation guard skips validation:

```bash
if [ -n "$agent_role" ] && [ "$agent_role" != "none" ] && [ "$agent_role" != "None" ]; then
    # validation happens here
fi
# empty role silently passes
```

The dispatch then proceeds to `dispatch_with_skill_activation()` where `map_role_to_skill("")` returns empty, triggering `[SKILL_INVALID]` at line 1331 — deep in the delivery path, after terminal resolution.

### 4.2 The Rule

**RC-4 (Requeue Classification Rule 4)**: Empty, null, or `"none"` agent_role MUST be caught at pre-validation with an explicit rejection. The dispatch MUST NOT proceed to terminal resolution.

Required behavior:
```bash
# Explicit empty/none role guard
if [ -z "$agent_role" ] || [ "$agent_role" = "none" ] || [ "$agent_role" = "None" ]; then
    log "V8 ERROR: Empty or 'none' role — dispatch blocked at pre-validation"
    if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
        echo -e "\n\n[SKILL_INVALID] Role is empty or 'none'. Set a valid Role and remove this marker to retry.\n" >> "$dispatch"
    fi
    continue  # Stay in pending
fi
```

This must execute BEFORE the existing validation guard, not inside it.

---

## 5. duplicate_delivery_prevented Reachability

### 5.1 Current State

The `duplicate_delivery_prevented` event is emitted at dispatcher line 1535 when:
1. The canonical lease check returns `BLOCK:canonical_lease:leased:{dispatch_id}`.
2. The `{dispatch_id}` in the lease matches the dispatch being delivered.

This indicates a retry: the same dispatch is being sent again while its prior delivery attempt still holds the lease.

### 5.2 Reachability Analysis

The path IS reachable in production when:
1. A dispatch is delivered to a terminal (lease acquired).
2. The delivery times out or the process loop restarts.
3. The lease has not yet expired (TTL 600s).
4. The same dispatch file is picked up again from `pending/` (it was never moved to `active/` because delivery failed after lease acquisition but before file move).

This is an edge case but a real one — it prevents duplicate delivery to the same terminal.

### 5.3 The Rule

**RC-5 (Requeue Classification Rule 5)**: The `duplicate_delivery_prevented` event MUST be exercisable under test. If the code path cannot be exercised with a deterministic test, the event emission must be clearly documented as a defensive guard and the test must cover the classification logic that would handle it.

### 5.4 Resolution

The `duplicate_delivery_prevented` event MUST remain in the codebase. It serves a defensive purpose. PR-2 must add test coverage that exercises the classification of a `canonical_lease:leased:{same_dispatch_id}` block reason and verifies the event type is `duplicate_delivery_prevented` (not `dispatch_blocked`).

---

## 6. Intelligence Gathering Blocking Semantics

### 6.1 The Ambiguity

The feature plan describes intelligence gathering as "optional, non-fatal on parse." The code has two intelligence operations:

| Operation | Command Failure (rc != 0) | Result Parse Failure | Blocking? |
|-----------|--------------------------|---------------------|-----------|
| `validate` | Blocks — `[DEPENDENCY_ERROR]` marker, requeue | N/A (pass/fail from rc) | **Yes** on command failure |
| `gather` | Blocks — `[DEPENDENCY_ERROR]` marker, requeue | Non-fatal — fallback to empty | **Yes** on command failure, **No** on parse failure |

### 6.2 The Rule

**RC-6 (Requeue Classification Rule 6)**: Intelligence gathering blocking semantics are:

1. **Command execution failure** (rc != 0): BLOCKS dispatch. Marks `[DEPENDENCY_ERROR]`. File stays in `pending/`. This is correct — a failed command means the intelligence system is unavailable, not that intelligence is empty.

2. **Result parse failure**: Does NOT block dispatch. Falls back to empty intelligence. This is correct — missing intelligence is a quality degradation, not a dispatch integrity failure.

3. **Validation failure** (valid=false): BLOCKS dispatch. Marks `[SKILL_INVALID]`. File stays in `pending/`. This is correct — an invalid role should not be dispatched.

The contract text "optional, non-fatal on parse" applies ONLY to result parsing (case 2), NOT to command execution (case 1) or validation (case 3). PR-1 must update any misleading documentation.

---

## 7. Dispatch Exit Path Table

Every exit path from `dispatch_with_skill_activation()` and `process_dispatches()` is listed below with its classification and file disposition.

### 7.1 Pre-Dispatch Exits (in process_dispatches)

| Exit | Reason | Marker | File Disposition | Classification |
|------|--------|--------|-----------------|----------------|
| Skip: `[SKILL_INVALID]` present | Prior skill failure | `[SKILL_INVALID]` | Stay in `pending/` | Waiting for edit |
| Skip: empty/none role | Missing role | `[SKILL_INVALID]` (new) | Stay in `pending/` | Invalid role |
| Skip: skill registry check fails | Unknown skill | `[SKILL_INVALID]` | Stay in `pending/` | Waiting for edit |
| Skip: validate command fails | Runtime dependency | `[DEPENDENCY_ERROR]` | Stay in `pending/` | Waiting for dependency |
| Skip: validate result invalid | Wrong role | `[SKILL_INVALID]` | Stay in `pending/` | Waiting for edit |
| Reject: no track | Missing metadata | `[REJECTED]` | Move to `rejected/` | Invalid metadata |
| Reject: T0 track | Invalid target | `[REJECTED]` | Move to `rejected/` | Invalid target |
| Reject: invalid track | No terminal mapping | `[REJECTED]` | Move to `rejected/` | Invalid target |
| Skip: terminal lock busy | Terminal occupied | None | Stay in `pending/` | Busy (requeueable) |
| Skip: gather command fails | Runtime dependency | `[DEPENDENCY_ERROR]` | Stay in `pending/` | Waiting for dependency |

### 7.2 In-Dispatch Exits (in dispatch_with_skill_activation)

| Exit | Reason | Return Code | Marker | Classification |
|------|--------|-------------|--------|----------------|
| determine_executor fails | No available terminal | 1 | None | Requeueable |
| configure_terminal_mode fails | Mode config error | 1 | None | Requeueable |
| Empty skill_name | Role maps to nothing | 1 | `[SKILL_INVALID]` | Waiting for edit |
| Skill not in registry | Unknown skill | 1 | `[SKILL_INVALID]` | Waiting for edit |
| Instruction extraction fails | Bad dispatch content | 1 | None | Requeueable |
| Canonical lease blocked | Terminal busy/ambiguous | 1 | None | Requeueable |
| Legacy lock blocked | Terminal claimed | 1 | None | Requeueable |
| Claim acquisition fails | Shadow state error | 1 | None | Requeueable |
| Canonical lease acquire fails | Lease contention | 1 | None | Requeueable |
| Input mode guard fails | Pane in copy-mode | 1 | None | Requeueable |
| tmux send-keys fails | Delivery transport error | 1 | None | Requeueable |
| tmux Enter fails | Submission transport error | 1 | None | Requeueable |
| Success | Delivered | 0 | None | Active |

---

## 8. Implementation Constraints

### 8.1 For PR-1

1. **RC-3 disposition fix**: `process_dispatches()` must not move unmarked failed dispatches to `rejected/`. No-marker return-1 means requeueable.
2. **RC-4 empty role guard**: Add explicit empty/none role check before existing validation guard.
3. **RC-6 documentation**: Update any misleading "optional" intelligence gathering description.
4. All fixes must have test coverage for both the success and failure path.

### 8.2 For PR-2

1. **RC-2 classification fix**: Add `canonical_check_parse_error` to `_classify_blocked_dispatch()` as `ambiguous true`.
2. **RC-5 test coverage**: Add test exercising `duplicate_delivery_prevented` event for same-dispatch-id lease conflict.
3. All classification cases must be tested — no regression in existing classifications.

### 8.3 For PR-3

1. Certify all RC-* rules with realistic dispatch flow scenarios.
2. Reproduce the requeue-to-reject regression and verify it is fixed.
3. Verify `canonical_check_parse_error` is classified as `ambiguous` (not `invalid`).
4. Verify empty role is caught at pre-validation.

---

## Appendix A: Classification Rule Quick Reference

| Rule | Description |
|------|-------------|
| RC-1 | Every block reason maps to exactly one category via `_classify_blocked_dispatch()` |
| RC-2 | `canonical_check_parse_error` is `ambiguous true` (transient, not metadata defect) |
| RC-3 | No-marker return-1 means requeueable — do not move to `rejected/` |
| RC-4 | Empty/none role fails at pre-validation before terminal operations |
| RC-5 | `duplicate_delivery_prevented` must be testable or documented as defensive |
| RC-6 | Intelligence command failure blocks; result parse failure does not block |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Terminal Exclusivity (80) | Classification of `canonical_lease:*` reasons follows the lease state model in doc 80 |
| Delivery Failure Lease (90) | Lease cleanup after delivery failure follows doc 90 phases; this contract classifies the failure reason |
| Input-Ready Terminal (110) | `blocked_input_mode` and related reasons from doc 110 are classified as `ambiguous true` |
| Projection Consistency (120) | Requeue decisions may interact with projection drift — a requeued dispatch must still reconcile with P-3 |
| Queue Truth (70) | Requeued dispatches remain in `pending/`; rejected dispatches move to `rejected/` — both per doc 70 P-2 |
