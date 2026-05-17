# FP-B Certification Matrix

**Feature**: FP-B — Runtime Recovery, tmux Hardening, And Operability
**PR**: PR-0
**Status**: Canonical
**Purpose**: Maps every in-scope incident class to its expected recovery outcome, the PR that implements it, and the test evidence required for certification.

---

## How To Use This Matrix

1. Each row is one incident scenario that FP-B must handle correctly.
2. The "Expected Outcome" column defines the correct recovery behavior.
3. The "Implementing PR" column identifies which PR delivers the implementation.
4. The "Verification" column specifies what test or evidence proves correctness.
5. FP-B is certified when every row passes verification.

---

## Certification Rows

### 1. Process Crash Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 1.1 | Worker process crashes once | `process_crash` | Process restarted after 10s cooldown; incident logged; dispatch state unchanged | PR-1, PR-2 | Incident record exists with class=process_crash; process restarted within budget |
| 1.2 | Worker process crashes 2x | `process_crash` | Restarted; T0 escalation emitted after 2nd crash | PR-1, PR-2 | Escalation event exists; incident count=2 |
| 1.3 | Worker process crashes 3x (budget exhausted) | `process_crash` | 3rd restart attempted; if 4th crash occurs, dispatch evaluated for dead-letter via separate incident | PR-1, PR-2 | Budget counter=3; no further auto-restart |

### 2. Terminal Unresponsive Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 2.1 | Terminal misses heartbeat | `terminal_unresponsive` | Lease expired; pane remap attempted; incident logged | PR-1, PR-3 | Lease state=expired; pane remap event exists |
| 2.2 | Terminal unresponsive after remap | `terminal_unresponsive` | T0 escalation; 2nd recovery attempt with 60s cooldown | PR-1, PR-2 | Escalation event after 1st retry; cooldown respected |
| 2.3 | Terminal unresponsive, budget exhausted | `terminal_unresponsive` | Dispatch enters dead-letter; terminal halted | PR-1, PR-2 | Dead-letter transition exists; terminal state=halted or equivalent |

### 3. Delivery Failure Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 3.1 | tmux send-keys fails once | `delivery_failure` | Re-delivery after 5s cooldown; incident logged | PR-1, PR-2 | Incident record with class=delivery_failure; re-delivery attempt exists |
| 3.2 | Pane not found during delivery | `delivery_failure` | Pane remap attempted; re-delivery after remap | PR-1, PR-3 | Remap event + re-delivery event exist |
| 3.3 | Delivery fails 3x | `delivery_failure` | T0 escalation after 2nd; dead-letter after 3rd | PR-1, PR-2 | Escalation at count=2; dead-letter at count=3 |

### 4. ACK Timeout Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 4.1 | Dispatch stuck in `delivering` past threshold | `ack_timeout` | Dispatch timed out; re-delivery after 30s; terminal health checked first | PR-1, PR-2, PR-4 | Timeout event; terminal health check event; re-delivery attempt |
| 4.2 | ACK timeout on 2nd attempt | `ack_timeout` | Dead-letter; T0 escalation | PR-1, PR-2 | Dead-letter transition; escalation event |

### 5. Lease Conflict Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 5.1 | Generation mismatch on lease renew | `lease_conflict` | Immediate T0 escalation; auto-recovery halted; one reconciliation attempt | PR-1, PR-2 | Escalation event at count=0; halt flag set |
| 5.2 | Concurrent lease claim attempt | `lease_conflict` | Second claim rejected; incident logged; no state corruption | PR-1 | InvalidTransitionError logged as incident; lease state unchanged |

### 6. Resume Failed Recovery

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 6.1 | Recovered dispatch fails on re-delivery | `resume_failed` | T0 escalation; one more retry after 60s | PR-1, PR-2 | Escalation event; retry cooldown=60s |
| 6.2 | Resume fails twice | `resume_failed` | Dead-letter; operator review required | PR-1, PR-2 | Dead-letter transition; incident trail complete |

### 7. Repeated Failure Loop Detection

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 7.1 | Same class fails 3x on one dispatch | `repeated_failure_loop` | All auto-recovery halted; dead-letter; terminal may be halted; T0 escalation | PR-1, PR-2 | Circuit-breaker triggered; dead-letter; halt flag |
| 7.2 | Different classes fail on one dispatch | N/A (no loop) | Each class uses its own budget independently | PR-1, PR-2 | No repeated_failure_loop incident; individual budgets correct |

### 8. tmux Identity Invariants

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 8.1 | Pane destroyed and recreated | N/A | `panes.json` updated; terminal identity unchanged; lease state unchanged | PR-3 | panes.json updated; terminal_leases row unchanged |
| 8.2 | tmux session crashed and rebuilt | N/A | Session rebuilt from profile; pane mappings refreshed; leases reconciled | PR-3, PR-5 | Session matches profile; panes.json refreshed; leases verified |
| 8.3 | Operator rearranges panes manually | N/A | Remap updates adapter state; no runtime state mutation | PR-3 | Remap event logged; terminal_leases unchanged |

### 9. Operator Command Integrity

| # | Scenario | Incident Class | Expected Outcome | Implementing PR | Verification |
|---|----------|---------------|-----------------|-----------------|--------------|
| 9.1 | `vnx doctor` on healthy runtime | N/A | All checks pass; no mutations | PR-4 | Exit code 0; no state changes; pass/warn/fail output |
| 9.2 | `vnx doctor` detects stale lease | N/A | Warn for stale lease; recovery preflight output | PR-4 | Warn output with lease details; preflight recommendation |
| 9.3 | `vnx doctor` detects incident pressure | N/A | Fail for high incident count; recovery blocked | PR-4 | Fail output with incident summary |
| 9.4 | `vnx recover` on degraded runtime | N/A | Leases reconciled; incidents summarized; tmux rebound; resume safe | PR-5 | Recovery log shows reconciliation + remap + incident summary |
| 9.5 | `vnx recover` idempotent (run twice) | N/A | Second run is no-op; no compound incidents | PR-5 | Second run clean; no new incidents |
| 9.6 | `vnx recover` with unresolvable blocker | N/A | Recovery blocked; operator told why; no partial mutation | PR-5 | Blocker reported; state unchanged from before recover |

---

## Certification Procedure

### Pre-Certification (Per-PR)

Each PR runs its own quality gate tests covering the rows assigned to it. The gate must pass before the PR merges.

### Final Certification (PR-5)

After PR-5 merges, run the full certification:

1. **Unit tests**: Every incident class has tests for creation, retry budget, cooldown, escalation, and dead-letter transitions.
2. **Integration tests**: Simulated failure scenarios covering rows 1.1 through 9.6.
3. **Idempotency tests**: All recovery commands run twice; second run produces no state changes.
4. **tmux identity tests**: Pane remap/reheal scenarios verified against canonical terminal state.
5. **Operator flow tests**: `vnx doctor` and `vnx recover` produce correct output for healthy, degraded, and blocked scenarios.

### Certification Evidence

The certification run produces a JSON report mapping each row number to:
- `status`: pass | fail | skip
- `evidence`: test name, event IDs, or log excerpts
- `notes`: any caveats or residual risks

FP-B is certified when all rows show `pass` status.

---

## Residual Risk Register

| Risk | Mitigation | Owner |
|------|-----------|-------|
| Incident class boundaries may need refinement after real-world operation | Monitor incident distribution in first week; adjust thresholds via config | T0 |
| Retry budgets may be too aggressive or too conservative for some workloads | Budgets are configurable in `ReconcilerConfig`; adjust based on evidence | T0 |
| tmux session crash during active recovery could compound incidents | Recovery commands are idempotent; re-run is safe | PR-5 |
| Dead-letter dispatches accumulate without operator attention | `vnx doctor` surfaces dead-letter count; dashboard shows dead-letter queue | PR-4, Dashboard |
