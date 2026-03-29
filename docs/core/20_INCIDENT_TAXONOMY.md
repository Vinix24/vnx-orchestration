# VNX Incident Taxonomy And Recovery Contracts

**Feature**: FP-B — Runtime Recovery, tmux Hardening, And Operability
**PR**: PR-0
**Status**: Canonical
**Source of truth**: `scripts/lib/incident_taxonomy.py`

This document defines the canonical incident language for FP-B. All supervision, reconciliation, recovery, and operator commands use these definitions. Later PRs (PR-1 through PR-5) implement against this taxonomy — they do not invent local variants.

---

## Design Principles

1. **Non-overlapping classes**: Every runtime failure maps to exactly one incident class.
2. **Bounded retries** (G-R2): No incident class permits unbounded retry loops.
3. **Incident trail** (G-R3): Every recovery action emits a durable incident record.
4. **Explicit dead-letter** (G-R5): Dispatches that cannot safely resume stop in a reviewable state.
5. **Governance-aware escalation** (G-R8): Auto-retry acts within policy; escalation is explicit.
6. **Failure visibility** (G-R1): No automatic recovery may hide a failure class.

---

## Severity Levels

| Level | Meaning | Automatic Recovery? | Operator Notification |
|-------|---------|--------------------|-----------------------|
| `info` | Observable event, no recovery needed | N/A | Logged only |
| `warning` | Recoverable within budget | Yes | On escalation |
| `error` | Recovery attempted, attention likely needed | Yes (limited) | Immediate |
| `critical` | Immediate intervention required | No | Immediate + halt |

---

## Incident Classes

### Process-Level Incidents

#### `process_crash`
- **Severity**: warning
- **Detection**: PID check, exit-code observation, process monitor
- **Scope**: Single process on a single terminal
- **Recovery**: Restart process up to 3 times, exponential backoff (10s, 20s, 40s)
- **Escalation**: After 2nd failure, notify T0
- **Dead-letter**: No (process crash alone does not dead-letter the dispatch)

#### `terminal_unresponsive`
- **Severity**: error
- **Detection**: Heartbeat timeout, tmux pane query failure
- **Scope**: Single terminal
- **Recovery**: Expire lease, attempt pane remap (2 attempts, 30s cooldown)
- **Escalation**: After 1st retry, notify T0
- **Dead-letter**: Yes (associated dispatches become eligible after budget exhaustion)

### Delivery-Level Incidents

#### `delivery_failure`
- **Severity**: warning
- **Detection**: tmux send-keys error, adapter pane-not-found, transport timeout
- **Scope**: Single dispatch attempt
- **Recovery**: Re-deliver up to 3 times with backoff (5s, 10s, 20s), attempt pane remap if pane-not-found
- **Escalation**: After 2nd failure, notify T0
- **Dead-letter**: Yes (after budget exhaustion)

#### `ack_timeout`
- **Severity**: warning
- **Detection**: Dispatch stuck in `delivering` or `accepted` past threshold
- **Scope**: Single dispatch attempt
- **Recovery**: Re-deliver once after 30s cooldown (must verify terminal responsive first)
- **Escalation**: After 1st timeout, notify T0
- **Dead-letter**: Yes (after budget exhaustion)

### Ownership-Level Incidents

#### `lease_conflict`
- **Severity**: error
- **Detection**: Generation mismatch, InvalidTransitionError on lease operations
- **Scope**: Single terminal lease
- **Recovery**: One reconciliation attempt to resolve stale lease (15s cooldown)
- **Escalation**: Immediate (before any retry), automatic recovery halted
- **Dead-letter**: No (ownership resolved first, dispatch evaluated separately)

### Workflow-Level Incidents

#### `resume_failed`
- **Severity**: error
- **Detection**: Second+ failure on a previously-recovered dispatch
- **Scope**: Single dispatch across multiple attempts
- **Recovery**: One more retry permitted (60s cooldown)
- **Escalation**: Immediate, notify T0
- **Dead-letter**: Yes (if final retry fails)

#### `repeated_failure_loop`
- **Severity**: critical
- **Detection**: Same failure class hits threshold (3) within dispatch lifetime
- **Scope**: Single dispatch or terminal across retry history
- **Recovery**: None (all automatic recovery halted)
- **Escalation**: Immediate, halt everything
- **Dead-letter**: Yes (dispatch enters dead-letter, terminal may be halted)
- **Note**: This is the circuit-breaker for the runtime

---

## Recovery Contract Summary

| Incident Class | Severity | Max Retries | Cooldown | Backoff | Escalate After | Dead-Letter | Halt Auto |
|---------------|----------|-------------|----------|---------|----------------|-------------|-----------|
| `process_crash` | warning | 3 | 10s | 2.0x | 2 retries | No | No |
| `terminal_unresponsive` | error | 2 | 30s | 2.0x | 1 retry | Yes | No |
| `delivery_failure` | warning | 3 | 5s | 2.0x | 2 retries | Yes | No |
| `ack_timeout` | warning | 2 | 30s | 1.5x | 1 retry | Yes | No |
| `lease_conflict` | error | 1 | 15s | 1.0x | 0 retries | No | Yes |
| `resume_failed` | error | 1 | 60s | 1.0x | 0 retries | Yes | No |
| `repeated_failure_loop` | critical | 0 | N/A | N/A | 0 retries | Yes | Yes |

---

## Dead-Letter Entry Rules

A dispatch enters dead-letter when ALL of:

1. Its current state is one of: `timed_out`, `failed_delivery`, `recovered`
2. The triggering incident class is dead-letter eligible (see table above)
3. The retry budget for that incident class is exhausted
4. The escalation rule permits dead-letter entry

Dead-letter is a terminal state for **automatic** recovery. It is NOT terminal for operator intervention — T0 can review the incident trail and manually re-queue if the root cause is resolved.

### Dead-letter eligible classes

| Class | Condition |
|-------|-----------|
| `terminal_unresponsive` | 2 attempts exhausted, terminal still unresponsive |
| `delivery_failure` | 3 delivery attempts exhausted |
| `ack_timeout` | 2 timeout cycles exhausted |
| `resume_failed` | Final retry after prior recovery fails |
| `repeated_failure_loop` | Immediate (circuit-breaker threshold reached) |

---

## Escalation Triggers

| Trigger | Action |
|---------|--------|
| Retry count reaches `escalate_after_retries` | Notify T0, include incident summary |
| Budget exhausted with `halt_auto_recovery=True` | Halt all automatic recovery for this entity |
| Budget exhausted with `dead_letter_eligible=True` | Transition dispatch to dead-letter |
| `repeated_failure_loop` detected | Immediate halt + dead-letter + T0 notification |
| Unknown incident class encountered | Always escalate (safe default) |

---

## Interaction With Existing State Machine

The incident taxonomy layers on top of the FP-A dispatch and lease state machines. It does not replace them.

| FP-A State Transition | Incident Class Triggered |
|----------------------|--------------------------|
| `delivering` -> `failed_delivery` | `delivery_failure` |
| `delivering`/`accepted` -> `timed_out` | `ack_timeout` |
| `leased` -> `expired` (TTL elapsed) | `terminal_unresponsive` |
| Lease operation raises `ValueError` (generation) | `lease_conflict` |
| `recovered` -> `failed_delivery` (again) | `resume_failed` |
| `attempt_count` >= threshold, same class | `repeated_failure_loop` |
| Process exit observed by supervisor | `process_crash` |
