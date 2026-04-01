# Terminal Exclusivity And Fail-Closed Dispatch Contract

**Status**: Canonical
**Feature**: Fail-Closed Terminal Dispatch Guard
**PR**: PR-0
**Gate**: `gate_pr0_terminal_exclusivity_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for terminal dispatch exclusivity. All downstream PRs (PR-1 through PR-3) implement against this contract. Any component that checks terminal availability, acquires a lease, or delivers a dispatch must conform to the rules defined here.

---

## 1. Why This Exists

### 1.1 The Problem

The double-feature trial exposed a real dispatch safety breach:

- A dispatch was sent toward T3 while T3 was already busy or ambiguously busy.
- The canonical lease check (`rc_check_terminal`) defaults to `ALLOW` on any failure — JSON parse error, missing state file, Python crash, or runtime-core unavailability.
- The legacy lock check (`terminal_lock_allows_dispatch`) returns `0` (allow) when `terminal_state.json` does not exist.
- No check in the chain treats ambiguity as a blocking signal. Every failure path falls through to delivery.

This is not an observability problem. It is a **dispatch safety failure**. When two dispatches land on the same terminal:

- The second dispatch overwrites the first worker's context.
- The first worker's report may never arrive.
- Receipts link to the wrong dispatch.
- Lease ownership becomes incoherent.
- Operator trust in the system breaks.

### 1.2 The Fix

Move dispatch safety from:
- **Best-effort exclusivity with permissive fallback** (current)

To:
- **Fail-closed exclusivity with explicit blocked, requeue, and recovery semantics**

That means: if we cannot prove a terminal is available, we do not dispatch to it. Ambiguity is not permission.

---

## 2. Terminal States

A terminal is in exactly one of four dispatch-safety states at any moment. These states are derived from the canonical lease table (`terminal_leases` in SQLite), not from `terminal_state.json` (which is a projection — see A-R4 in the Queue Truth Contract).

### 2.1 State Definitions

| State | Definition | Dispatchable |
|-------|-----------|-------------|
| **idle** | No active lease. No claimed dispatch. Terminal is available. | **Yes** |
| **busy** | Active lease held by a dispatch. Lease TTL has not elapsed. Worker is executing. | **No** |
| **ambiguous** | State cannot be determined with certainty. Canonical source is unreadable, unreachable, corrupt, or returns contradictory signals. | **No** |
| **expired** | Lease TTL has elapsed but the lease has not been formally released or recovered. | **No** |

### 2.2 State Derivation

```
Given terminal_id T:

1. Attempt to read canonical lease from terminal_leases (SQLite).

2. IF read fails (connection error, corrupt DB, missing table):
     state = ambiguous
     STOP

3. IF no lease row exists for T:
     state = idle
     STOP

4. IF lease.state == "idle" AND lease.dispatch_id IS NULL:
     state = idle
     STOP

5. IF lease.state == "leased" AND lease.expires_at > now:
     state = busy
     owner = lease.dispatch_id
     STOP

6. IF lease.state == "leased" AND lease.expires_at <= now:
     state = expired
     STOP

7. IF lease.state IN {"expired", "recovering"}:
     state = expired
     STOP

8. OTHERWISE:
     state = ambiguous
     STOP
```

### 2.3 Legacy Shadow State

`terminal_state.json` provides a secondary signal. It is a projection (A-R4) and must never override the canonical lease. However, when the canonical source is unavailable (step 2 above yields `ambiguous`), the legacy shadow is NOT treated as a fallback that permits dispatch. Ambiguous means blocked.

The legacy `terminal_lock_allows_dispatch()` function remains as a defense-in-depth layer. Both the canonical check AND the legacy check must pass for dispatch to proceed. Either one blocking is sufficient to block.

---

## 3. Fail-Closed Rules

These rules define the contract that PR-1 must enforce in code. Each rule is labeled for traceability.

### 3.1 Canonical Check Failure (FC-1)

**Rule**: If the canonical lease check fails for any reason — Python crash, SQLite error, missing database file, timeout, unexpected output format — the terminal is classified as **ambiguous** and dispatch is **blocked**.

**Current violation**: `rc_check_terminal()` returns `ALLOW` on any failure (line 342-343 of `dispatcher_v8_minimal.sh`). The JSON parse fallback also defaults to `"yes"` (available) on parse failure (line 347).

**Required behavior**: On canonical check failure, emit `BLOCK:canonical_check_failed:<error_class>` and return non-zero. The dispatcher must not fall through to delivery.

### 3.2 Lease Ambiguity (FC-2)

**Rule**: If the canonical lease row exists but its state cannot be mapped to idle/busy/expired (unexpected value, null fields where non-null is required, version mismatch), the terminal is classified as **ambiguous** and dispatch is **blocked**.

**Required behavior**: The availability check must validate lease row field integrity before classifying the terminal as idle. Missing `dispatch_id` on a `leased` row, missing `expires_at`, or unrecognized state values all yield `ambiguous`.

### 3.3 Runtime-Core Unavailability (FC-3)

**Rule**: If `VNX_RUNTIME_PRIMARY=1` (runtime core is the active lease authority) and the runtime core Python module cannot be loaded or executed, dispatch is **blocked** for all terminals.

**Current violation**: `_rc_enabled` returning false causes the canonical check to be skipped entirely, falling through to the legacy-only path. When the runtime core is the designated authority, its absence is a safety failure, not a graceful degradation.

**Required behavior**: When `VNX_RUNTIME_PRIMARY=1`, canonical check failure is a hard block. The legacy path runs as defense-in-depth but cannot override a canonical block or substitute for a missing canonical check.

**Exception**: When `VNX_RUNTIME_PRIMARY=0` (explicit legacy mode), the canonical check is not required and the legacy path alone governs dispatch safety. This is the rollback path.

### 3.4 Active Worker Ownership (FC-4)

**Rule**: A terminal with an active lease held by dispatch D1 must block any dispatch D2 where D2 != D1. This applies regardless of:
- Whether D2 has higher priority than D1.
- Whether D1's worker has produced recent output.
- Whether the operator believes D1 is stuck.

**Required behavior**: Priority-based preemption does not exist. Lease ownership is absolute until the lease is released, expired, or recovered through the formal state machine. There is no "steal" transition.

**Rationale**: Preemption would require coordinating worker termination, context cleanup, and receipt linkage — complexity that is out of scope for this feature and creates new failure modes worse than waiting.

### 3.5 Missing State File (FC-5)

**Rule**: If `terminal_state.json` does not exist and the canonical lease table has no row for the terminal, the terminal is classified as **idle** (not ambiguous). This is the cold-start case.

**Rationale**: A system that has never dispatched has no lease state. Blocking on absence of state would prevent the first-ever dispatch. The canonical source's absence of a row is a definitive signal (no lease has ever been acquired), distinct from an unreadable or corrupt source.

### 3.6 Dual-Check Conjunction (FC-6)

**Rule**: Both the canonical lease check and the legacy lock check must independently allow dispatch. Either one blocking is sufficient to block dispatch. This is a logical AND, not OR.

```
dispatch_allowed = canonical_check_passes AND legacy_check_passes
```

**Rationale**: Defense-in-depth. The canonical check is the authority; the legacy check catches edge cases during the transition period (e.g., a lease that was acquired before runtime-core cutover and only exists in the shadow state).

---

## 4. Retry, Requeue, And Escalation Boundaries

### 4.1 Blocked Dispatch Outcomes

When a dispatch is blocked by terminal exclusivity, one of three outcomes applies:

| Outcome | Condition | Dispatcher Behavior |
|---------|-----------|-------------------|
| **defer** | Terminal is busy with a known dispatch. The blocking lease is healthy. | Dispatch remains in `pending/`. Dispatcher retries on next loop iteration (2-second cycle). No state change. |
| **requeue** | Terminal is expired or ambiguous. The blocking condition may resolve with operator intervention. | Dispatch remains in `pending/`. Dispatcher logs a structured warning with the block reason. T0 escalation emitted after configurable timeout (default: 15 minutes). |
| **reject** | Dispatch metadata is invalid (no track, invalid skill, T0 target). | Dispatch moved to `rejected/`. Not retried. |

### 4.2 Defer Behavior

Deferred dispatches are **not** moved out of `pending/`. The dispatcher's main loop (`process_dispatches`) re-evaluates them every 2 seconds. No retry counter is maintained — the dispatch remains eligible indefinitely until:

- The blocking lease is released (worker completes).
- The blocking lease expires and is recovered.
- The dispatch is manually removed by the operator.

**Rationale**: Adding a retry limit would create a new failure mode (dispatch silently drops after N retries). The operator can always remove a dispatch from `pending/` to cancel it. The lease TTL mechanism (default 600s) ensures that truly stuck terminals eventually become recoverable.

### 4.3 Requeue Behavior

Requeue is semantically identical to defer in the current architecture — the dispatch stays in `pending/` and is retried. The distinction exists for audit purposes: the log and coordination event differentiate "terminal is healthily busy" (defer) from "terminal state is uncertain" (requeue).

Future PRs may introduce a formal requeue with backoff, but this contract does not require it. The current 2-second loop with log differentiation is sufficient.

### 4.4 Escalation

When a dispatch has been blocked for longer than `VNX_DISPATCH_BLOCK_TIMEOUT` (default: 900 seconds / 15 minutes), the dispatcher emits a structured escalation:

```json
{
  "event_type": "dispatch_blocked_escalation",
  "entity_type": "dispatch",
  "entity_id": "<dispatch_id>",
  "terminal_id": "<terminal_id>",
  "block_reason": "<reason from availability check>",
  "blocked_since": "<ISO8601 timestamp>",
  "blocked_seconds": 900,
  "actor": "dispatcher"
}
```

The escalation is advisory. It does not change dispatch state. The operator or T0 decides whether to:
- Extend the blocking lease (if the worker is still active but slow).
- Expire and recover the lease (if the worker is truly stuck).
- Reassign the dispatch to a different track/terminal (manual operation).

### 4.5 No Automatic Lease Theft

The dispatcher never automatically expires a lease to unblock a pending dispatch. Lease expiry is driven by the reconciler (`expire_stale()`) or explicit operator action. The dispatcher only reads lease state; it does not write it (except for acquiring a new lease on an idle terminal).

---

## 5. Dispatch Safety Sequence

This section defines the exact sequence of checks that must pass before a dispatch is delivered to a terminal. PR-1 implements this sequence.

```
process_dispatches() loop:
  for each dispatch in pending/:

    1. VALIDATE METADATA
       - Extract track, skill, gate, dispatch_id.
       - Reject if track is missing, invalid, or T0.
       - Block if skill is marked invalid ([SKILL_INVALID]).
       → On reject: move to rejected/. NEXT dispatch.
       → On block: skip. NEXT dispatch.

    2. VALIDATE SKILL
       - Run gather_intelligence validate.
       - Block if skill is not recognized.
       → On block: mark [SKILL_INVALID], skip. NEXT dispatch.

    3. PRE-FILTER AVAILABILITY (fast path)
       - Call terminal_lock_allows_dispatch(terminal, dispatch_id).
       - This is the legacy check, kept as an early-exit optimization.
       → On block: log defer, skip. NEXT dispatch.

    4. GATHER INTELLIGENCE (non-blocking on failure since PR-1)
       - Failures block dispatch to prevent silent intelligence loss.

    5. ENTER dispatch_with_skill_activation():

       5a. CANONICAL LEASE CHECK (FC-1, FC-2, FC-3)
           - rc_check_terminal(terminal, dispatch_id)
           - MUST return BLOCK on any failure or ambiguity.
           → On BLOCK: return 1 (dispatch not delivered).

       5b. LEGACY LOCK CHECK (FC-6, defense-in-depth)
           - terminal_lock_allows_dispatch(terminal, dispatch_id)
           → On block: return 1.

       5c. ACQUIRE TERMINAL CLAIM (legacy shadow)
           - acquire_terminal_claim(terminal, dispatch_id)
           → On failure: return 1.

       5d. ACQUIRE CANONICAL LEASE
           - rc_acquire_lease(terminal, dispatch_id)
           → On failure: release terminal claim, return 1.

       5e. REGISTER WITH BROKER
           - rc_register(dispatch_id, terminal, ...)
           - Non-fatal (shadow mode compatibility).

       5f. RECORD DELIVERY START
           - rc_delivery_start(dispatch_id, terminal)

       5g. DELIVER VIA TMUX
           - Skill activation + paste-buffer.
           → On failure: rc_delivery_failure, release lease, release claim, return 1.

       5h. RECORD DELIVERY SUCCESS
           - rc_delivery_success(dispatch_id, attempt_id)

       5i. MOVE TO ACTIVE
           - mv dispatch to active/.
```

### 5.1 Invariant: No Delivery Without Proven Availability

At no point in the sequence above does a check failure result in continued delivery. Every check either passes (proceed) or fails (return 1 / skip). There is no `|| true` fallback, no `echo "ALLOW"` on error, no `return 0` on missing state.

### 5.2 Invariant: Lease Before Delivery

The canonical lease is acquired (step 5d) before tmux delivery begins (step 5g). If lease acquisition fails — because another dispatch claimed the terminal between the check and the acquire — delivery does not proceed. This closes the TOCTOU window between check and delivery.

### 5.3 Invariant: Cleanup On Delivery Failure

If tmux delivery fails (step 5g), the lease is released, the terminal claim is released, and the broker records a `failed_delivery` event. The terminal returns to idle. The dispatch is moved to `rejected/` by the caller in `process_dispatches()`.

---

## 6. Interaction With Existing Components

### 6.1 Queue Truth Contract (70)

This contract is complementary to the Queue Truth Contract. Queue truth answers "what state is this PR in?" Terminal exclusivity answers "is this terminal safe to dispatch to?" They share the principle that canonical sources override projections, but operate on different entities (PRs vs terminals).

### 6.2 FPC Execution Contracts (30)

The execution contracts define routing invariants (R-1 through R-8) that determine which terminal a dispatch targets. Terminal exclusivity operates after routing: once a target terminal is selected, exclusivity determines whether dispatch to that terminal is safe. Routing and exclusivity are separate concerns.

### 6.3 Lease Manager

The `LeaseManager` class is the Python API for canonical lease operations. This contract requires that the dispatcher use `LeaseManager.acquire()` (or its shell wrapper `rc_acquire_lease`) as the authority for lease acquisition, and that `LeaseManager.get()` (or `rc_check_terminal`) is the authority for availability checks.

### 6.4 terminal_state_shadow.py

The terminal state shadow remains as a defense-in-depth layer and as the write target for the legacy `acquire_terminal_claim()` path. This contract does not remove it. It constrains it: the shadow cannot override a canonical block, and the shadow cannot substitute for a missing canonical check when the runtime core is active.

### 6.5 Dispatcher V8

PR-1 modifies the dispatcher to enforce this contract. The modifications are:
1. `rc_check_terminal()` returns `BLOCK` on failure instead of `ALLOW`.
2. `terminal_lock_allows_dispatch()` returns `1` (block) when state file is unreadable (not just missing — the cold-start exception in FC-5 still applies).
3. The conjunction rule (FC-6) is enforced: both checks must pass.

---

## 7. Non-Goals

This contract explicitly scopes out the following. Any PR work that drifts into these areas must be rejected or deferred.

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Terminal scheduling or routing engine | Exclusivity operates after routing. It does not decide which terminal to target, only whether the target is safe. |
| NG-2 | Priority-based preemption | Preemption requires worker coordination, context cleanup, and receipt relinking. Out of scope and higher risk than the problem it solves. |
| NG-3 | Automatic lease recovery | The dispatcher reads lease state but does not write it (except acquire on idle). Lease expiry and recovery are reconciler responsibilities. |
| NG-4 | Multi-dispatch terminal sharing | A terminal executes one dispatch at a time. Pseudo-parallelism on a single terminal is explicitly rejected. |
| NG-5 | Cross-feature terminal arbitration | This contract covers one feature at a time. Cross-feature dispatch coordination is a separate concern. |
| NG-6 | Replacing the legacy lock path | The legacy path stays as defense-in-depth. Removing it is a future simplification, not a safety improvement. |
| NG-7 | Modifying VNX core infrastructure (.vnx/) | This feature exercises and validates infrastructure; it does not change it. |
| NG-8 | Tmux delivery reliability improvements | Delivery retry (3 attempts with backoff) already exists. Improving tmux transport is orthogonal to exclusivity. |
| NG-9 | Clear-context and smart-tap safety | These are PR-2 concerns. This contract defines exclusivity only. |

### 7.1 Scope Creep Detection

A PR is out of scope for terminal exclusivity if it:
- Changes routing logic (which terminal to target).
- Adds preemption or priority-based lease stealing.
- Modifies lease state machine transitions in `runtime_coordination.py`.
- Adds new execution target types.
- Changes the receipt processor or receipt format.
- Modifies files under `.vnx/`.

---

## 8. Contract Verification

### 8.1 How To Verify This Contract Is Satisfied

| # | Check | Method |
|---|-------|--------|
| V-1 | Terminal states are defined | Section 2.1 defines idle, busy, ambiguous, expired |
| V-2 | State derivation is deterministic | Section 2.2 provides a step-by-step algorithm |
| V-3 | Fail-closed rules are explicit | Section 3 defines FC-1 through FC-6 with current violations and required behavior |
| V-4 | Retry/requeue/escalation boundaries are defined | Section 4 defines defer, requeue, reject, and escalation with timeouts |
| V-5 | Dispatch safety sequence is complete | Section 5 defines the exact check order with no fallthrough paths |
| V-6 | No delivery without proven availability | Section 5.1 states the invariant explicitly |
| V-7 | Non-goals prevent scope creep | Section 7 lists nine explicit non-goals |

### 8.2 Quality Gate Checklist

`gate_pr0_terminal_exclusivity_contract`:
- [ ] Contract defines when a terminal is dispatchable, blocked, or ambiguous (Section 2)
- [ ] Contract requires fail-closed behavior on runtime or lease uncertainty (Section 3: FC-1, FC-2, FC-3)
- [ ] Contract blocks silent second dispatch to an already occupied terminal (Section 3: FC-4, Section 5.1)
- [ ] Contract defines retry or requeue behavior for blocked dispatch attempts (Section 4)
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## Appendix A: Fail-Closed Rule Summary

Quick reference for implementers.

| Rule | Signal | Classification | Dispatch |
|------|--------|---------------|----------|
| FC-1 | Canonical check crashes or returns unparseable output | ambiguous | **BLOCKED** |
| FC-2 | Lease row has unexpected state value or missing required fields | ambiguous | **BLOCKED** |
| FC-3 | Runtime core is designated authority but unavailable | ambiguous (all terminals) | **BLOCKED** |
| FC-4 | Active lease held by different dispatch | busy | **BLOCKED** |
| FC-5 | No state file and no lease row (cold start) | idle | **ALLOWED** |
| FC-6 | Either canonical or legacy check blocks | busy or ambiguous | **BLOCKED** |

## Appendix B: Current Fail-Open Violations

These are the specific code locations that PR-1 must fix.

| File | Line | Violation | Required Fix |
|------|------|-----------|-------------|
| `dispatcher_v8_minimal.sh` | 180-181 | `terminal_lock_allows_dispatch()` returns 0 (allow) when state file missing | Return 0 only when no lease row exists in canonical source (FC-5). Block on unreadable file. |
| `dispatcher_v8_minimal.sh` | 338 | `rc_check_terminal()` returns `ALLOW` when `_rc_enabled` is false | When `VNX_RUNTIME_PRIMARY=1`, absence of runtime core is a hard block (FC-3). |
| `dispatcher_v8_minimal.sh` | 342-343 | `rc_check_terminal()` returns `ALLOW` on Python execution failure | Return `BLOCK:canonical_check_failed` (FC-1). |
| `dispatcher_v8_minimal.sh` | 347 | JSON parse of canonical check result defaults to `"yes"` on error | Default to `"no"` (blocked) on parse error (FC-1). |
| `dispatcher_v8_minimal.sh` | 210 | `terminal_lock_allows_dispatch()` returns allow when no record exists for terminal | Acceptable only after canonical check has passed (FC-5 + FC-6 conjunction). |

## Appendix C: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| 70_QUEUE_TRUTH_CONTRACT.md | Shares canonical-over-projection principle. Queue truth: PR state. This contract: terminal state. |
| 30_FPC_EXECUTION_CONTRACTS.md | Defines routing (which terminal). This contract: exclusivity (is that terminal safe). |
| 50_DOUBLE_FEATURE_TRIAL_CONTRACT.md | Trial exposed the busy-terminal breach this contract addresses. |
| 42_FPD_PROVENANCE_CONTRACT.md | Blocked-dispatch events follow the provenance model for audit trail. |
| 60_CONVERSATION_RESUME_CONTRACT.md | Resume must respect terminal exclusivity — resuming a dispatch on a busy terminal is the same safety violation. |

## Appendix D: Glossary

| Term | Definition |
|------|-----------|
| **Canonical lease** | The `terminal_leases` row in the SQLite coordination database. Authoritative for terminal ownership. |
| **Legacy shadow** | `terminal_state.json` — a JSON projection of terminal state, maintained by `terminal_state_shadow.py`. Defense-in-depth, not authoritative. |
| **Fail-closed** | Default to blocking dispatch when state is uncertain. The opposite of fail-open (default to allowing). |
| **Fail-open** | Default to allowing dispatch when state is uncertain. The current (broken) behavior this contract replaces. |
| **Defer** | Blocked dispatch stays in `pending/` and is retried on the next dispatcher loop. Terminal is healthily busy. |
| **Requeue** | Blocked dispatch stays in `pending/` and is retried, but the block reason is ambiguity or expiry, not healthy work. |
| **TOCTOU** | Time-of-check to time-of-use. The window between checking availability and acquiring the lease, during which another dispatch could claim the terminal. Closed by acquiring the lease before delivery (Section 5.2). |
| **Cold start** | First-ever dispatch on a system with no prior lease history. Handled by FC-5. |
| **Generation guard** | The `generation` field on lease rows that prevents stale heartbeat or release operations from affecting a lease that has been re-acquired by a different dispatch. |
