# Delivery Failure Lease Ownership Contract

**Status**: Canonical
**Feature**: Failed Delivery Lease Cleanup And Runtime State Reconciliation
**PR**: PR-0
**Gate**: `gate_pr0_failed_delivery_lease_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for what must happen when a dispatch fails during terminal delivery. It defines lease cleanup obligations, distinguishes failed delivery from accepted execution, blocks silent terminal stranding, and specifies audit evidence requirements. All downstream PRs (PR-1 through PR-4) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

The Terminal Exclusivity Contract (80) defines when a terminal is safe to dispatch to. It does not define what happens when delivery fails **after** a lease has been acquired. When delivery fails and the lease is not released, the target terminal is silently stranded in `busy` state. No new dispatch can reach that terminal until the lease TTL expires (default 600s) and the reconciler runs.

Current code (dispatcher_v8_minimal.sh, lines 1486-1509) attempts cleanup on tmux delivery failure:
- Records `failed_delivery` in the broker.
- Releases the canonical lease.
- Releases the legacy terminal claim.

But this cleanup has gaps:
- Lease release is non-fatal — a failed release silently strands the terminal.
- Broker failure recording depends on a valid `attempt_id` — when delivery-start registration fails, the failure is not recorded.
- No process-exit trap protects the window between lease acquisition and delivery completion.
- The filesystem move to `rejected/` is not reconciled with the broker's DB state.
- Worker-side rejection after `accepted` state has no signal path back to the dispatcher.

### 1.2 The Fix

Make lease cleanup **deterministic and auditable** for every delivery-failure path:
- Every failure path that holds a lease must release it or escalate.
- Cleanup failure must be explicit, not silent.
- Audit evidence must distinguish why delivery failed and whether cleanup succeeded.
- Worker-side rejection after acceptance must be defined as a separate concern from delivery failure.

---

## 2. Delivery-Failure Taxonomy

A dispatch can fail at different points in the delivery sequence. Each point has different cleanup obligations depending on whether a lease has been acquired.

### 2.1 Failure Phases

The delivery sequence in `dispatch_with_skill_activation()` has three phases relative to lease ownership:

| Phase | Lines (approx.) | Lease Held | Claim Held | Cleanup Required |
|-------|-----------------|------------|------------|------------------|
| **Phase 0: Pre-lease** | 1119-1397 | No | No (until 1381) | None for lease; claim release needed if acquired |
| **Phase 1: Post-lease, pre-delivery** | 1399-1435 | Yes | Yes | Full lease + claim release |
| **Phase 2: During delivery** | 1436-1509 | Yes | Yes | Full lease + claim release + broker failure record |

### 2.2 Phase 0: Pre-Lease Failures

These failures occur before the canonical lease is acquired. No lease cleanup is required.

| Failure | Code Location | Lease Impact | Current Handling |
|---------|--------------|--------------|-----------------|
| `determine_executor` fails | 1134 | None | `return 1` — correct |
| `configure_terminal_mode` fails (clear-context, model switch, feedback modal) | 1142 | None | `return 1` — correct |
| Skill validation fails | 1169 | None | `return 1` + `[SKILL_INVALID]` marker — correct |
| Instruction extraction fails | 1181 | None | `return 1` — correct |
| Terminal ID resolution fails | 1293 | None | `return 1` — correct |
| Canonical lease check blocks | 1360 | None | `return 1` + blocked audit — correct |
| Legacy lock check blocks | 1377 | None | `return 1` — correct |
| Terminal claim acquisition fails | 1381 | None | `return 1` — correct |
| Canonical lease acquisition fails | 1389 | None (acquire failed) | Release claim, `return 1` — correct |

**Contract rule (DFL-0)**: Phase 0 failures require no lease cleanup. The existing handling is correct. No changes required.

**Note on configure_terminal_mode**: Clear-context failure, feedback modal loops, and model-switch failures all occur in Phase 0. The terminal may be left in an indeterminate UI state (not at a ready prompt), but no lease is held, so the failure does not strand the terminal in the coordination layer. The terminal may need operator intervention to return to a ready prompt, but this is a mode-control concern outside the scope of this contract.

### 2.3 Phase 1: Post-Lease, Pre-Delivery Failures

After lease acquisition (line 1389) and before tmux delivery begins (line 1436), the dispatcher performs:
- Writes prompt to temp file (line 1404).
- Registers dispatch bundle with broker (line 1407).
- Records delivery start with broker (line 1411).
- Resolves worktree path (line 1416).
- Optionally `cd`s the terminal to the worktree (line 1429).

**Current gap**: If the process is killed between lease acquisition (line 1389) and the delivery failure handler (line 1486), no cleanup occurs. There is no `trap` or `finally` equivalent protecting this window.

**Contract rules for Phase 1:**

**DFL-1 (Process-Exit Safety)**: The dispatcher must install a cleanup trap (or equivalent) immediately after acquiring the canonical lease that releases the lease and claim if the function exits abnormally. The trap must:
- Release the canonical lease using the acquired generation.
- Release the legacy terminal claim.
- Record a `failed_delivery` event in the broker if an `attempt_id` exists.
- Log the cleanup with explicit reason `"process_exit_during_delivery"`.

**DFL-2 (Broker Registration Failure)**: `rc_register` is currently non-fatal (shadow mode compatibility). This is acceptable — a failed registration does not affect lease ownership. However, if `rc_delivery_start` fails or returns an empty `attempt_id`, the subsequent `rc_delivery_failure` call silently no-ops (because it guards on non-empty `attempt_id`). This means a delivery failure after a failed delivery-start will not be recorded in the broker.

**Required behavior**: When `rc_delivery_start` returns empty or fails:
- The delivery failure must still be recorded. Either:
  - (a) Record the failure against the dispatch directly (not per-attempt), OR
  - (b) Treat the missing attempt-start as a cleanup failure and log it as structured failure evidence.
- The lease release and claim release must proceed regardless of broker state.

### 2.4 Phase 2: During Delivery (tmux Transport Failures)

These are the failures the current code handles most completely.

| Failure | Code Location | Current Handling | Gap |
|---------|--------------|-----------------|-----|
| `tmux_load_buffer_safe` fails (buffer load) | 1447/1472 | `_delivery_failed=true` | None — falls through to cleanup block |
| `tmux paste-buffer` fails (paste) | 1453/1479 | `_delivery_failed=true` | None — falls through to cleanup block |
| `tmux send-keys Enter` fails (submit) | 1500 | Explicit cleanup | None |
| `tmux send-keys` skill command fails | 1462 | `_delivery_failed=true` | None — falls through to cleanup block |

**Current cleanup block (lines 1486-1494):**
```bash
rc_delivery_failure "$dispatch_id" "$_rc_attempt_id" "tmux delivery failed"
rc_release_lease "$terminal_id" "$_rc_generation"
release_terminal_claim "$terminal_id" "$dispatch_id"
return 1
```

**Contract rules for Phase 2:**

**DFL-3 (Lease Release Must Be Checked)**: `rc_release_lease` is currently a fire-and-forget call (non-fatal, line 523-533 of the dispatcher). If it fails, the terminal is silently stranded.

**Required behavior**: After `rc_release_lease` fails:
- Emit a structured failure event with `event_type: lease_release_failed`.
- Include `terminal_id`, `generation`, and `dispatch_id` in the event.
- The failure must appear in `blocked_dispatch_audit.ndjson` or an equivalent durable audit stream so that the reconciler and operator tooling can detect and recover stranded leases.
- The dispatcher must NOT retry the release in an unbounded loop. One retry with a 1-second delay is acceptable. After failure, record and move on.

**DFL-4 (Claim-Lease Cleanup Pairing)**: The canonical lease and legacy terminal claim must always be released as a pair. The current code releases the lease first, then the claim. If the lease release succeeds but the claim release fails (or vice versa), the two ownership layers diverge.

**Required behavior**:
- Both release attempts must execute regardless of individual success or failure.
- Individual failure of either release must be logged as structured failure evidence.
- The overall delivery-failure cleanup is considered successful only when both release and the broker failure record succeed.
- When any cleanup step fails, the structured failure log must include which step(s) failed and which succeeded, to enable targeted reconciliation.

**DFL-5 (Delivery Failure Audit Evidence)**: Every delivery failure must produce a durable audit record with the following fields:

| Field | Source | Required |
|-------|--------|----------|
| `dispatch_id` | Dispatch being delivered | Yes |
| `terminal_id` | Target terminal | Yes |
| `attempt_id` | Broker attempt (if available) | Yes (or `"unavailable"`) |
| `failure_phase` | `pre_delivery` / `during_delivery` / `post_delivery` | Yes |
| `failure_reason` | Human-readable reason string | Yes |
| `lease_released` | `true` / `false` / `"release_failed"` | Yes |
| `claim_released` | `true` / `false` / `"release_failed"` | Yes |
| `broker_recorded` | `true` / `false` / `"record_failed"` | Yes |
| `timestamp` | ISO8601 UTC | Yes |

This record must be durable (written to NDJSON audit or coordination DB), not logs-only.

---

## 3. Failed Delivery vs. Accepted Execution vs. Worker Failure

These three outcomes have different cleanup obligations and different responsible actors. Conflating them produces incorrect recovery actions.

### 3.1 Definitions

| Outcome | DB State | Lease Held By | Responsible Actor | Terminal State After |
|---------|----------|--------------|-------------------|---------------------|
| **Failed Delivery** | `failed_delivery` | Nobody (released by dispatcher) | Dispatcher | idle |
| **Accepted Execution** | `accepted` or `running` | Worker (via dispatch) | Worker | busy until worker completes |
| **Worker Failure** | `timed_out` or `failed_delivery` (from `running`) | Worker (until TTL expiry) | Reconciler | busy until TTL, then expired |

### 3.2 Failed Delivery (Dispatcher Responsibility)

**Definition**: The dispatch never reached the worker. The tmux transport failed, the terminal was not ready, or the process exited before delivery completed.

**Invariants**:
- FD-1: The dispatcher owns cleanup. The worker has no knowledge of this dispatch.
- FD-2: The canonical lease must be released before `dispatch_with_skill_activation` returns.
- FD-3: The terminal must return to `idle` state. No subsequent availability check should find it blocked.
- FD-4: The dispatch must NOT be moved to `active/`. It stays in `pending/` (for retry) or is moved to `rejected/` (for terminal failure).
- FD-5: The broker must record `failed_delivery` state for audit. If the broker is unavailable, the failure must be logged to the structured audit stream.

### 3.3 Accepted Execution (Worker Responsibility)

**Definition**: The dispatch was successfully delivered to the terminal. The worker's CLI (Claude Code, Codex, Gemini) has received the prompt and is processing it.

**Invariants**:
- AE-1: The dispatcher's job is done. It does NOT release the lease after successful delivery.
- AE-2: The lease TTL is the safety net. If the worker does not produce a receipt within the TTL window, the lease expires and the reconciler handles recovery.
- AE-3: The worker signals completion by producing a receipt (report to `unified_reports/`). The receipt processor detects this, moves the dispatch to `completed/`, and the lease is released.
- AE-4: The dispatcher must NOT interpret worker silence as delivery failure. Slow execution is not failed delivery.

### 3.4 Worker Failure (Reconciler Responsibility)

**Definition**: The dispatch was accepted, but the worker failed to produce a receipt within the expected timeframe. The worker may have crashed, the CLI may have disconnected, or the task may have hit an unrecoverable error.

**Invariants**:
- WF-1: Worker failure is detected by TTL expiry, not by the dispatcher.
- WF-2: The reconciler (via `expire_stale()`) marks the lease as `expired`.
- WF-3: Recovery from `expired` requires explicit `recover()` — it is never automatic.
- WF-4: The dispatch transitions through `timed_out` → `recovered` → `queued` (for retry) or `timed_out` → `expired` → `dead_letter` (for permanent failure).
- WF-5: Worker failure handling is out of scope for this contract. This contract covers delivery failure only. The boundary is: once `rc_delivery_success` records `accepted`, delivery is complete and worker failure rules apply.

### 3.5 The Grey Zone: Worker-Side Rejection After Delivery

There is a scenario where delivery succeeds (tmux transport completes) but the worker rejects the dispatch:
- Claude Code hook rejects the prompt.
- Claude Code context is too large and the skill fails to load.
- The CLI displays an error and returns to the prompt without executing.

**Current state**: This scenario is not detectable by the dispatcher. The dispatch is in `accepted` state, the lease is held, and the dispatcher considers delivery complete.

**Contract rule (DFL-6)**: Worker-side rejection after successful delivery is NOT a delivery failure. It is a worker failure governed by the TTL and reconciliation path (WF-1 through WF-5). This contract does not add a signal path from worker to dispatcher for rejection.

**Rationale**: Adding a worker→dispatcher rejection channel would require:
- Bidirectional communication between terminals (currently unidirectional: dispatcher→worker).
- Worker-side instrumentation to detect and report rejection.
- Dispatcher-side listener for asynchronous rejection signals.

This is a runtime architecture change that exceeds the scope of lease cleanup. The TTL mechanism (600s default) is the existing safety net for this scenario. Future work may add worker health probes, but this contract does not require them.

---

## 4. Silent Stranding Prevention

The core safety property of this contract: **a failed delivery must never leave a terminal silently blocked**.

### 4.1 Definition of Silent Stranding

A terminal is silently stranded when:
1. The canonical lease state is `leased` (terminal is blocked for new dispatches).
2. No dispatch is actively executing on that terminal.
3. No operator-visible signal indicates the terminal is stuck.
4. The only recovery path is TTL expiry (waiting up to 600s) or manual lease surgery.

### 4.2 Stranding Prevention Rules

**SP-1 (Synchronous Release)**: The canonical lease must be released synchronously within the `dispatch_with_skill_activation` function before it returns on failure. Deferred or asynchronous release is not acceptable because the dispatcher's main loop may attempt to re-evaluate the same terminal on the next 2-second cycle and find it blocked.

**SP-2 (Release Failure Escalation)**: If synchronous lease release fails (DFL-3), the dispatcher must:
- Record the stranding risk in the audit stream.
- NOT retry indefinitely (one retry is acceptable).
- Allow the reconciler's `expire_stale()` to handle eventual recovery.
- The stranded-lease audit record must be distinguishable from a normal delivery failure so that operator tooling can surface it with higher severity.

**SP-3 (Filesystem-DB Consistency)**: When a dispatch is moved to `rejected/` after delivery failure, the broker DB state must be consistent:
- If the dispatch was registered with the broker: DB state must be `failed_delivery`.
- If the dispatch was never registered (Phase 0 failure): no DB state exists, which is consistent.
- The filesystem move to `rejected/` must NOT happen if the broker records `accepted` (this would mean delivery actually succeeded despite a transient error in post-delivery bookkeeping).

**SP-4 (Stale Pending Detection)**: A dispatch that remains in `pending/` after a delivery failure (because the caller chose to defer rather than reject) must not hold a lease. The lease must be released before the function returns, regardless of whether the caller will retry or reject.

### 4.3 Terminal State After Failed Delivery

After a successful delivery-failure cleanup, the following conditions must hold:

| State Layer | Expected Value | Verified By |
|-------------|---------------|-------------|
| Canonical lease (`terminal_leases`) | `idle`, `dispatch_id = NULL` | `LeaseManager.get(terminal_id)` |
| Legacy shadow (`terminal_state.json`) | No active claim for the terminal | `terminal_lock_allows_dispatch()` |
| Broker dispatch state | `failed_delivery` | `get_dispatch(dispatch_id)` |
| Filesystem | Dispatch in `pending/` or `rejected/` (never `active/`) | `ls` |

---

## 5. Cleanup Sequence

This section defines the exact cleanup sequence that must execute on delivery failure after a lease has been acquired (Phase 1 or Phase 2 failure).

```
on_delivery_failure(dispatch_id, terminal_id, generation, attempt_id, reason):

  1. RECORD BROKER FAILURE
     - If attempt_id is non-empty:
       rc_delivery_failure(dispatch_id, attempt_id, reason)
     - If attempt_id is empty:
       Log structured failure: "delivery_failure_without_attempt"
       Record failure via dispatch-level state transition if possible
     - Capture result: broker_recorded = true/false

  2. RELEASE CANONICAL LEASE
     - rc_release_lease(terminal_id, generation)
     - If release fails:
       Sleep 1s, retry once
       If retry fails:
         Log structured failure: "lease_release_failed"
         Emit stranding-risk audit record
     - Capture result: lease_released = true/false

  3. RELEASE LEGACY CLAIM
     - release_terminal_claim(terminal_id, dispatch_id)
     - If release fails:
       Log structured failure: "claim_release_failed"
     - Capture result: claim_released = true/false

  4. EMIT AUDIT RECORD (DFL-5)
     - Write delivery-failure audit record with all captured results
     - This record is durable (NDJSON or coordination DB)

  5. RETURN 1 (signal failure to caller)
```

### 5.1 Cleanup Trap (DFL-1)

The cleanup trap must be installed immediately after lease acquisition and removed after delivery success is recorded:

```
# Pseudocode — actual implementation in bash
_cleanup_trap_active=true
trap 'on_delivery_failure "$dispatch_id" "$terminal_id" "$_rc_generation" "$_rc_attempt_id" "process_exit"' EXIT

# ... delivery sequence ...

# On success:
_cleanup_trap_active=false
trap - EXIT
rc_delivery_success "$dispatch_id" "$_rc_attempt_id"
```

The trap must be scoped to the delivery function, not the entire dispatcher process.

---

## 6. Runtime Truth Reconciliation After Failure

After a delivery failure, the system must be able to verify that all state layers agree on the terminal's availability.

### 6.1 Reconciliation Check

The following query must return consistent results after delivery-failure cleanup:

```
canonical_lease = LeaseManager.get(terminal_id)
shadow_state = terminal_state.json[terminal_id]
dispatch_state = broker.get_dispatch(dispatch_id)

ASSERT canonical_lease.state == "idle" OR canonical_lease is None
ASSERT shadow_state has no active claim for terminal_id
ASSERT dispatch_state.state == "failed_delivery"
```

If any assertion fails, the state is divergent and must be surfaced as an operator-visible diagnostic.

### 6.2 Reconciliation Responsibility

| Actor | Responsibility |
|-------|---------------|
| **Dispatcher** | Synchronous cleanup (Section 5). Best-effort — may fail partially. |
| **Reconciler** (`expire_stale`) | Catches leases stranded by cleanup failure. Runs on configurable interval. |
| **Operator tooling** (`vnx status`, terminal state queries) | Surfaces divergent state. Does not auto-repair. |

### 6.3 Reconciliation Timing

- **Immediate**: Dispatcher cleanup (synchronous, <1s).
- **TTL-bounded**: Reconciler catches stranded leases within `lease_seconds` (default 600s).
- **Diagnostic**: Operator tooling can detect divergence at any time by running the reconciliation check (6.1).

The gap between immediate cleanup failure and reconciler recovery is the **stranding window**. This window is bounded by the lease TTL and is acceptable as long as:
1. The stranding is not silent (audit record exists per DFL-5).
2. The reconciler is running.
3. Operator tooling can detect and surface the stranding.

---

## 7. Non-Goals

This contract explicitly scopes out the following. Any PR work that drifts into these areas must be rejected or deferred.

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Worker→dispatcher rejection channel | Requires bidirectional terminal communication. The TTL mechanism is the safety net for worker-side rejection (see DFL-6). |
| NG-2 | Automatic dispatch retry after failure | The dispatcher loop already retries pending dispatches every 2s. Adding retry-with-backoff at the delivery level is a separate concern. |
| NG-3 | Dead-letter queue management | `failed_delivery` → `dead_letter` transitions are reconciler/operator decisions. This contract defines cleanup, not lifecycle management. |
| NG-4 | Lease TTL tuning | The 600s default is not changed by this contract. TTL tuning is an operational concern. |
| NG-5 | Modifying the lease state machine | The valid transitions in `LEASE_TRANSITIONS` are not changed. This contract defines cleanup behavior within the existing state machine. |
| NG-6 | Receipt processor changes | Receipt processing operates on `accepted`/`running`/`completed` dispatches. Failed deliveries never reach the receipt processor. |
| NG-7 | tmux delivery reliability improvements | Delivery retry (3 attempts with backoff) exists. This contract does not change transport-layer retry behavior. |
| NG-8 | Cross-feature dispatch coordination | This contract covers single-dispatch cleanup. Multi-dispatch terminal arbitration is a separate concern. |
| NG-9 | Replacing the legacy terminal claim path | The legacy claim stays as defense-in-depth per the Terminal Exclusivity Contract (80). |

### 7.1 Scope Creep Detection

A PR is out of scope for this contract if it:
- Adds worker-to-dispatcher communication channels.
- Modifies the lease state machine transitions.
- Changes receipt processing or report formatting.
- Adds dispatch retry logic beyond what exists (2s loop + 3-attempt tmux retry).
- Modifies the reconciler's expiry or recovery logic (except to consume the new audit records).
- Changes files under `.vnx/`.

---

## 8. Contract Verification

### 8.1 How To Verify This Contract Is Satisfied

| # | Check | Method |
|---|-------|--------|
| V-1 | Every delivery-failure path releases the canonical lease | Code inspection: every `return 1` after lease acquisition must pass through the cleanup sequence (Section 5) |
| V-2 | Cleanup failure is never silent | Code inspection: `rc_release_lease` failure produces a structured failure event, not just a log line |
| V-3 | Audit evidence exists for every delivery failure | Test: trigger each failure path and verify the DFL-5 audit record exists |
| V-4 | Failed delivery returns terminal to idle | Test: after delivery failure, `LeaseManager.get(terminal_id).state == "idle"` |
| V-5 | Failed delivery does not move dispatch to `active/` | Test: dispatch file remains in `pending/` or `rejected/` after delivery failure |
| V-6 | Process-exit cleanup trap works | Test: kill the delivery function mid-execution and verify lease is released |
| V-7 | Broker state and filesystem state are consistent | Test: after delivery failure, dispatch DB state is `failed_delivery` and file is not in `active/` |

### 8.2 Quality Gate Checklist

`gate_pr0_failed_delivery_lease_contract`:
- [x] Contract defines required lease and claim cleanup for every failed-delivery path (Section 2, Section 5)
- [x] Contract distinguishes failed delivery from accepted execution and worker failure (Section 3)
- [x] Contract blocks silent terminal stranding after dispatch rejection or delivery failure (Section 4)
- [x] Contract defines required audit evidence for cleanup and reconciliation (DFL-5 in Section 2.4, Section 6)

---

## 9. Implementation Priority For Downstream PRs

Based on the gaps identified in this contract, the following implementation priorities apply:

| Priority | Gap | Contract Rule | Target PR |
|----------|-----|---------------|-----------|
| P1 | Lease release failure is silent | DFL-3 (checked release) | PR-1 |
| P1 | No process-exit trap for lease cleanup | DFL-1 (cleanup trap) | PR-1 |
| P1 | Empty attempt_id silently skips broker failure record | DFL-2 (attempt-id fallback) | PR-1 |
| P1 | Claim-lease release not paired | DFL-4 (paired cleanup) | PR-1 |
| P2 | No structured audit record for delivery failure | DFL-5 (audit evidence) | PR-1 |
| P2 | Filesystem-DB state divergence after rejected move | SP-3 (filesystem-DB consistency) | PR-2 |
| P2 | No reconciliation check after cleanup | Section 6.1 | PR-2 |
| P3 | Worker-side rejection not detectable | DFL-6 (acknowledged non-goal) | Future work |

---

## Appendix A: Delivery-Failure Rule Summary

Quick reference for implementers.

| Rule | Obligation | Responsible Actor | Phase |
|------|-----------|-------------------|-------|
| DFL-0 | Pre-lease failures need no lease cleanup | N/A | 0 |
| DFL-1 | Process-exit trap must release lease | Dispatcher | 1-2 |
| DFL-2 | Empty attempt_id must not silently skip failure recording | Dispatcher | 1-2 |
| DFL-3 | Lease release failure must be explicit, not silent | Dispatcher | 1-2 |
| DFL-4 | Lease and claim release must be paired | Dispatcher | 1-2 |
| DFL-5 | Every delivery failure must produce durable audit evidence | Dispatcher | 1-2 |
| DFL-6 | Worker-side rejection after accepted is NOT delivery failure | N/A (TTL safety net) | Post-delivery |

## Appendix B: Stranding Prevention Rule Summary

| Rule | Property | Enforcement |
|------|----------|-------------|
| SP-1 | Synchronous release before function return | Code structure |
| SP-2 | Release failure escalation (one retry, then audit) | DFL-3 implementation |
| SP-3 | Filesystem-DB consistency after rejection | PR-2 reconciliation |
| SP-4 | Pending dispatch must not hold lease | Section 5 cleanup sequence |

## Appendix C: Current Code Gaps (For PR-1 Implementers)

| File | Line | Gap | Contract Rule |
|------|------|-----|---------------|
| `dispatcher_v8_minimal.sh` | 523-533 | `rc_release_lease` is fire-and-forget; failure is logged but not escalated | DFL-3 |
| `dispatcher_v8_minimal.sh` | 509-520 | `rc_delivery_failure` silently no-ops when `attempt_id` is empty | DFL-2 |
| `dispatcher_v8_minimal.sh` | 1486-1494 | No process-exit trap protecting the lease-to-delivery window | DFL-1 |
| `dispatcher_v8_minimal.sh` | 1486-1494 | `rc_release_lease` return code not checked | DFL-3 |
| `dispatcher_v8_minimal.sh` | 1486-1494 | No structured audit record emitted for delivery failure | DFL-5 |
| `dispatcher_v8_minimal.sh` | 1734-1737 | `rejected/` filesystem move not reconciled with broker DB state | SP-3 |

## Appendix D: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| 80_TERMINAL_EXCLUSIVITY_CONTRACT.md | Defines when a terminal is dispatchable. This contract defines what happens when delivery to a dispatchable terminal fails. |
| 70_QUEUE_TRUTH_CONTRACT.md | Defines canonical PR state sources. This contract defines canonical terminal state after delivery failure. |
| 30_FPC_EXECUTION_CONTRACTS.md | Defines routing (which terminal). This contract operates after routing. |
| 42_FPD_PROVENANCE_CONTRACT.md | Delivery-failure audit records follow the provenance model. |
| 60_CONVERSATION_RESUME_CONTRACT.md | Resume must respect lease state after delivery failure — a released lease means the terminal is available for new work. |

## Appendix E: Glossary

| Term | Definition |
|------|-----------|
| **Failed delivery** | A dispatch that never reached the worker. The tmux transport failed, or the process exited before delivery completed. The dispatcher is responsible for cleanup. |
| **Accepted execution** | A dispatch successfully delivered to the worker. The lease is held by the worker until completion or TTL expiry. |
| **Worker failure** | A dispatch was accepted but the worker did not produce a receipt within the TTL window. The reconciler handles recovery. |
| **Silent stranding** | A terminal blocked by a lease that no dispatch is using and no operator signal indicates. The worst outcome this contract prevents. |
| **Cleanup trap** | A shell `trap` that executes lease release on abnormal function exit. Prevents stranding when the delivery function is interrupted. |
| **Stranding window** | The time between cleanup failure and reconciler recovery. Bounded by lease TTL. Acceptable if the stranding is auditable. |
| **Generation guard** | The `generation` field on lease rows that prevents stale release operations from affecting a lease re-acquired by a different dispatch. |
| **Paired cleanup** | Releasing both the canonical lease and legacy terminal claim together, regardless of individual success or failure. |
