# Fail-Closed Bootstrap Contract

**Status**: Canonical
**Feature**: Runtime Bootstrap Hardening
**PR**: PR-0
**Gate**: `gate_pr0_bootstrap_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the contract for fail-closed runtime bootstrap: explicit startup preconditions, fail-closed dispatch registration before lease acquisition, deterministic lease cleanup at chain boundaries, and the rationale for preserving the FK constraint on `terminal_leases.dispatch_id`. All downstream PRs implement against this contract.

---

## 1. Why This Exists

### 1.1 The Three Bootstrap Defects

Three runtime bootstrap defects cause repeated manual operator intervention:

**Defect 1: /tmp State Fallback**

`runtime_core_cli.py` `_get_dirs()` (line 59-70) resolves state and dispatch directories with a `/tmp` fallback:

```python
state_dir = os.environ.get(
    "VNX_STATE_DIR",
    str(Path(vnx_data) / "state") if vnx_data else "/tmp/vnx-state",
)
```

When `VNX_DATA_DIR` is unset (e.g., a fresh shell, a tmux session without inherited env, or a worktree whose `.env_override` was not sourced), the runtime core silently operates against `/tmp/vnx-state` — a completely different database from the project's `.vnx-data/state/`. Dispatches registered in the `/tmp` database are invisible to the real dispatcher, and leases acquired against `/tmp` do not protect the actual terminals.

**Defect 2: Non-Fatal Registration Before Lease Acquire**

`rc_register()` in `dispatcher_v8_minimal.sh` (line 392-413) is explicitly non-fatal:

```bash
if ! _rc_python "${args[@]}" > /dev/null; then
    log "V8 RUNTIME_CORE: register non-fatal failure dispatch=$dispatch_id"
fi
```

When registration fails, the dispatch is never inserted into the `dispatches` table. The subsequent `rc_acquire_lease()` call then attempts to set `terminal_leases.dispatch_id` to a value that does not exist in `dispatches` — violating the FK constraint `terminal_leases.dispatch_id REFERENCES dispatches(dispatch_id)`. SQLite rejects the UPDATE with `FOREIGN KEY constraint failed`, and the lease acquisition fails.

Worse, the register→acquire ordering is inverted in the current code: lease acquisition (line 1562) happens **before** registration (line 1580). This means the FK violation occurs during acquire, not during register — the wrong function fails, producing a confusing error.

**Defect 3: No Chain-Boundary Lease Cleanup**

When a feature chain completes and the operator starts a new chain, any terminal leases from the old chain remain in the database. If a lease expired but was never explicitly released (e.g., the terminal timed out and the reconciler marked it `expired` but never `released`), the new chain's first dispatch to that terminal is blocked by a stale lease from the previous chain.

No existing script or procedure releases all leases at chain boundaries. The operator must manually query the database and release each lease — an error-prone process that defeats the purpose of automation.

### 1.2 The Fix

1. **Eliminate /tmp fallback**: `_get_dirs()` must raise an explicit error when VNX env vars are missing. No silent fallback to a different state store.
2. **Fail-closed registration**: Registration must succeed before lease acquisition. Registration failure blocks the dispatch with an actionable error.
3. **Chain-boundary cleanup**: A deterministic procedure releases all terminal leases during chain closeout.

---

## 2. Startup Preconditions

### 2.1 Required Environment Variables

The following environment variables MUST be set and valid before any runtime core operation:

| Variable | Required By | Validation |
|----------|------------|------------|
| `VNX_DATA_DIR` | `runtime_core_cli.py`, dispatcher | Must be a non-empty string pointing to an existing directory |
| `VNX_STATE_DIR` | `runtime_core_cli.py`, dispatcher | Must be a non-empty string; parent directory must exist |
| `VNX_DISPATCH_DIR` | `runtime_core_cli.py`, dispatcher | Must be a non-empty string; parent directory must exist |

### 2.2 Precondition Rules

**BOOT-1 (No /tmp Fallback)**: `_get_dirs()` MUST NOT fall back to `/tmp/vnx-state` or `/tmp/vnx-dispatches`. When `VNX_DATA_DIR` is not set and `VNX_STATE_DIR` is not set, the function MUST raise a `RuntimeError` with an actionable message:

```python
raise RuntimeError(
    "VNX_STATE_DIR is not set and VNX_DATA_DIR is not set. "
    "Source bin/vnx or set VNX_DATA_DIR before running runtime_core_cli.py."
)
```

**BOOT-2 (Directory Existence)**: `_get_dirs()` MUST verify that the resolved `state_dir` exists as a directory (or its parent exists and the directory can be created). If the directory does not exist and cannot be created, the function MUST raise a `RuntimeError` with the path that failed.

**BOOT-3 (Dispatcher Startup Check)**: The dispatcher (`dispatcher_v8_minimal.sh`) MUST verify at startup that `VNX_STATE_DIR` and `VNX_DATA_DIR` are non-empty and point to existing directories. If either check fails, the dispatcher MUST exit with a non-zero return code and an error message:

```bash
if [[ -z "${VNX_STATE_DIR:-}" ]] || [[ ! -d "$VNX_STATE_DIR" ]]; then
    echo "FATAL: VNX_STATE_DIR is unset or does not exist: '${VNX_STATE_DIR:-}'" >&2
    echo "Source bin/vnx or set VNX_DATA_DIR before starting the dispatcher." >&2
    exit 1
fi
```

This check MUST execute before the main dispatch loop, before any `rc_*` function calls, and before any file operations against `$STATE_DIR`.

**BOOT-4 (Database Initialization)**: When `VNX_RUNTIME_PRIMARY=1`, the dispatcher MUST verify that `runtime_coordination.db` exists in `$VNX_STATE_DIR` and is a valid SQLite database before entering the dispatch loop. If the database does not exist, the dispatcher MUST call `init_schema()` (via `runtime_core_cli.py init-db` or equivalent) to create it. If initialization fails, the dispatcher MUST exit.

### 2.3 Worktree Isolation

The worktree `.env_override` mechanism (`bin/vnx` lines 54-66) is the correct way to override paths for worktrees. This contract does not change that mechanism. However:

**BOOT-5 (Override Validation)**: When `.vnx-data/.env_override` is sourced, the overridden `VNX_DATA_DIR` MUST point to a directory within the worktree, not to `/tmp` or a path outside the project tree. The `bin/vnx` sourcing code SHOULD validate this after sourcing.

---

## 3. Fail-Closed Registration

### 3.1 The Registration-Before-Lease Invariant

**BOOT-6 (Registration Before Lease)**: A dispatch MUST be registered in the broker database (`dispatches` table) BEFORE the canonical lease is acquired (`terminal_leases` table). This is required by the FK constraint `terminal_leases.dispatch_id REFERENCES dispatches(dispatch_id)`.

Current code violates this:

```
Current order (WRONG):
  1. rc_acquire_lease()     — sets terminal_leases.dispatch_id  [line 1562]
  2. rc_register()          — inserts dispatches.dispatch_id    [line 1580]

Required order:
  1. rc_register()          — inserts dispatches.dispatch_id
  2. rc_acquire_lease()     — sets terminal_leases.dispatch_id (FK satisfied)
```

### 3.2 Fail-Closed Behavior

**BOOT-7 (Registration Fail-Closed)**: When `VNX_RUNTIME_PRIMARY=1`, `rc_register()` MUST be fail-closed: if registration fails, the dispatch MUST NOT proceed to lease acquisition. The function MUST return a non-zero exit code, and the caller MUST treat this as a blocking failure.

Required behavior:

```bash
rc_register() {
    # ... existing args ...
    _rc_enabled || return 0

    if ! result=$(_rc_python "${args[@]}"); then
        log_structured_failure "registration_failed" \
            "Dispatch registration failed — blocking delivery" \
            "dispatch=$dispatch_id terminal=$terminal_id"
        return 1  # FAIL-CLOSED: block dispatch
    fi
    log "V8 RUNTIME_CORE: registered dispatch=$dispatch_id terminal=$terminal_id"
    return 0
}
```

The caller in `dispatch_with_skill_activation()` MUST check the return code:

```bash
if ! rc_register "$dispatch_id" "$terminal_id" "$track" "$skill_name" "$gate" "$_rc_prompt_tmpfile"; then
    # Release claim (lease was not yet acquired)
    release_terminal_claim "$terminal_id" "$dispatch_id"
    return 1  # Stay in pending — retry when registration succeeds
fi
```

### 3.3 Registration Failure Is Requeueable

Registration failure is a transient condition (database contention, I/O error, schema migration in progress). The dispatch MUST stay in `pending/` — no `[REJECTED]` marker, no `[SKILL_INVALID]` marker. The dispatcher loop retries on the next 2-second cycle.

### 3.4 Ordering In dispatch_with_skill_activation()

The required execution order after this contract:

```
1. determine_executor              (Phase 0 — no lease, no claim)
2. configure_terminal_mode         (Phase 0)
3. map_role_to_skill, validate     (Phase 0)
4. extract instruction, build prompt (Phase 0)
5. rc_check_terminal               (Phase 0 — canonical lease check)
6. terminal_lock_allows_dispatch   (Phase 0 — legacy lock check)
7. acquire_terminal_claim          (Phase 0 — legacy claim)
8. rc_register                     (Phase 0.5 — NEW: register BEFORE lease)
9. rc_acquire_lease                (Phase 1 — lease acquired, FK satisfied)
10. rc_delivery_start              (Phase 1 — attempt created)
11. check_pane_input_ready         (Phase 1 — input mode guard)
12. tmux delivery                  (Phase 2 — transport)
13. rc_delivery_success            (Phase 3 — success)
```

Step 8 is new. It moves registration from after lease acquire (current line 1580) to before lease acquire. The prompt temp file must be written before step 8 (currently done at line 1577-1579, which stays in the same relative position).

---

## 4. FK Constraint Preservation

### 4.1 The Constraint

```sql
CREATE TABLE IF NOT EXISTS terminal_leases (
    ...
    dispatch_id TEXT REFERENCES dispatches (dispatch_id),
    ...
);
```

This FK ensures that every lease points to a dispatch that actually exists in the broker. Without it, a lease could reference a dispatch_id that was never registered — making the lease unrecoverable (no dispatch metadata to reason about) and the audit trail broken (events reference a phantom dispatch).

### 4.2 Why The FK Is Correct

The FK constraint caught a real bug: silent registration failure. Without the FK, the lease acquire would succeed silently against a phantom dispatch_id. The dispatcher would proceed with delivery, the broker would have no record of the dispatch, and the entire audit trail for that delivery would be missing. The FK turned a silent data corruption into a loud, diagnosable failure.

**BOOT-8 (FK Preservation)**: The FK constraint `terminal_leases.dispatch_id REFERENCES dispatches(dispatch_id)` MUST be preserved. The constraint is not the bug — the non-fatal registration path is the bug. Removing the FK would mask the registration failure instead of fixing it.

### 4.3 FK-Safe Operations

The following operations respect the FK:

| Operation | FK Safety | Rationale |
|-----------|-----------|-----------|
| `acquire_lease(terminal_id, dispatch_id)` | Safe IF dispatch_id exists in `dispatches` | BOOT-6 ensures registration precedes acquisition |
| `release_lease(terminal_id, generation)` | Safe — sets `dispatch_id = NULL` | NULL is allowed by the FK (no reference) |
| Chain closeout `release_all()` | Safe — sets all `dispatch_id = NULL` | Same as individual release |
| `DELETE FROM dispatches WHERE dispatch_id = X` | Unsafe if lease references X | Should not delete dispatches with active leases |

### 4.4 Future Consideration: ON DELETE SET NULL

The current FK has no `ON DELETE` action, so deleting a dispatch while a lease references it would fail with a FK violation. A future migration could add `ON DELETE SET NULL` to allow safe dispatch cleanup. This is out of scope for this contract.

---

## 5. Chain-Boundary Lease Cleanup

### 5.1 The Problem

When a feature chain completes:
1. All dispatches should be in terminal states (`completed`, `expired`, `dead_letter`).
2. All terminal leases should be `idle` (no active dispatch).
3. The coordination database should be consistent.

In practice, leases may be in `expired` or `recovering` state if:
- A terminal timed out and the reconciler ran but recovery was not completed.
- The operator closed the chain before all reconciliation completed.
- A dispatch was manually killed and its lease was never released.

### 5.2 Cleanup Rules

**BOOT-9 (Chain Closeout Lease Release)**: During chain closeout, ALL terminal leases (`T1`, `T2`, `T3`) MUST be released to `idle` state with `dispatch_id = NULL`. This applies regardless of the lease's current state (`leased`, `expired`, `recovering`).

**BOOT-10 (Closeout Procedure)**: Chain closeout MUST execute the following steps in order:

```
1. VERIFY: all dispatches in terminal states (completed/expired/dead_letter)
   - If any dispatch is in non-terminal state (queued/claimed/delivering/accepted/running):
     WARN operator, list non-terminal dispatches
     Require explicit --force flag to proceed

2. RELEASE: set all terminal_leases to idle
   UPDATE terminal_leases
   SET state = 'idle',
       dispatch_id = NULL,
       leased_at = NULL,
       expires_at = NULL,
       last_heartbeat_at = NULL,
       released_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
       generation = generation + 1
   WHERE state != 'idle';

3. AUDIT: emit coordination events for each released lease
   INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id,
       from_state, to_state, actor, reason)
   VALUES (uuid, 'lease_released', 'terminal', terminal_id,
       old_state, 'idle', 'chain_closeout', 'chain_boundary_cleanup');

4. VERIFY: confirm all leases are idle
   SELECT terminal_id, state FROM terminal_leases WHERE state != 'idle';
   - If any non-idle lease remains: ABORT with error
```

**BOOT-11 (Generation Increment)**: The generation MUST be incremented during closeout release. This ensures that any in-flight release-on-failure calls from the old chain (using the old generation) are rejected by the generation guard. Without the increment, a delayed cleanup from the old chain could accidentally release a lease acquired by the new chain.

### 5.3 Closeout Entry Point

The closeout procedure SHOULD be exposed as a `runtime_core_cli.py` subcommand:

```bash
python3 scripts/runtime_core_cli.py chain-closeout [--force]
```

And optionally integrated into `bin/vnx`:

```bash
vnx chain closeout [--force]
```

### 5.4 Closeout Is Not Automatic

**BOOT-12 (Explicit Closeout)**: Chain closeout MUST be an explicit operator action, not an automatic side-effect of chain completion. The operator decides when a chain is complete. This preserves the governance invariant that T0 reviews receipts and advances quality gates — automated cleanup must not bypass this.

---

## 6. Error Behavior

### 6.1 Error Message Requirements

Every fail-closed abort MUST produce an error message that includes:

1. **What failed**: The specific operation that could not complete.
2. **Why it failed**: The condition that triggered the abort.
3. **How to fix it**: An actionable recovery instruction.

### 6.2 Error Message Table

| Condition | Error Message |
|-----------|---------------|
| `VNX_STATE_DIR` unset | `FATAL: VNX_STATE_DIR is not set. Source bin/vnx or set VNX_DATA_DIR.` |
| `VNX_STATE_DIR` not a directory | `FATAL: VNX_STATE_DIR does not exist: '/path'. Run vnx init or check .env_override.` |
| `VNX_DATA_DIR` unset | `FATAL: VNX_DATA_DIR is not set. Source bin/vnx or set VNX_DATA_DIR.` |
| DB missing and init fails | `FATAL: Cannot initialize runtime_coordination.db in $VNX_STATE_DIR. Check permissions.` |
| Registration fails | `V8 ERROR: Dispatch registration failed — dispatch=$id. Check runtime_core_cli.py output.` |
| Registration fails (FK detail) | `V8 ERROR: Registration must succeed before lease acquire (FK: terminal_leases.dispatch_id → dispatches.dispatch_id).` |
| Closeout with active dispatches | `WARN: Non-terminal dispatches exist: [list]. Use --force to proceed.` |

### 6.3 No Silent Fallback

**BOOT-13 (No Silent Degradation)**: When a bootstrap precondition fails, the system MUST abort with an actionable error. It MUST NOT:
- Fall back to a different directory (`/tmp`).
- Skip the operation and continue (`non-fatal failure`).
- Suppress the error message (redirect to `/dev/null`).
- Substitute default values that mask the missing configuration.

The only acceptable fallback is `VNX_RUNTIME_PRIMARY=0`, which explicitly disables the runtime core and reverts to legacy-only mode. This is a deliberate operator decision, not a silent degradation.

---

## 7. Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Removing the FK constraint | BOOT-8 — the FK is correct, the register path is the bug |
| NG-2 | Automatic chain detection | BOOT-12 — closeout is an explicit operator action |
| NG-3 | Database migration tooling | Schema migrations via `init_schema()` are already incremental |
| NG-4 | Worktree env propagation | `.env_override` mechanism exists; this contract adds validation, not propagation |
| NG-5 | New governance surface | FEATURE_PLAN explicitly scopes this out |
| NG-6 | Headless gate execution changes | That is Bridge Lane B2 |

---

## 8. Implementation Constraints For PR-1

1. **BOOT-1**: Remove `/tmp` fallback from `_get_dirs()` in `runtime_core_cli.py`. Raise `RuntimeError` when vars are missing.
2. **BOOT-3**: Add startup validation in `dispatcher_v8_minimal.sh` before the main loop.
3. **BOOT-6 + BOOT-7**: Move `rc_register()` before `rc_acquire_lease()` in `dispatch_with_skill_activation()`. Make `rc_register()` fail-closed (return 1 on failure, caller blocks dispatch).
4. **BOOT-9 through BOOT-11**: Implement `chain-closeout` subcommand in `runtime_core_cli.py` that releases all leases with generation increment and audit events.
5. **BOOT-4**: Add DB existence check at dispatcher startup.
6. All changes must have test coverage for both success and failure paths.

### 8.1 Files To Modify

| File | Change | Contract Rule |
|------|--------|---------------|
| `scripts/runtime_core_cli.py` | Remove `/tmp` fallback in `_get_dirs()`, add `chain-closeout` subcommand | BOOT-1, BOOT-2, BOOT-9 |
| `scripts/dispatcher_v8_minimal.sh` | Add startup precondition checks, reorder register before acquire, make register fail-closed | BOOT-3, BOOT-4, BOOT-6, BOOT-7 |
| `scripts/lib/runtime_core.py` | Add `release_all_leases()` method to `LeaseManager` | BOOT-9 |
| `scripts/lib/runtime_coordination.py` | Add `release_all_leases()` with generation increment and audit | BOOT-9, BOOT-11 |

### 8.2 Test Requirements

| Test | Validates |
|------|-----------|
| `_get_dirs()` raises when VNX_DATA_DIR and VNX_STATE_DIR both unset | BOOT-1 |
| Dispatcher exits on startup when VNX_STATE_DIR is empty | BOOT-3 |
| `rc_register()` failure blocks dispatch (no lease acquire attempted) | BOOT-7 |
| `chain-closeout` releases all leases and increments generation | BOOT-9, BOOT-11 |
| `chain-closeout` warns on non-terminal dispatches without --force | BOOT-10 |
| Lease acquire succeeds when register precedes it (FK satisfied) | BOOT-6, BOOT-8 |
| Lease acquire fails when register is skipped (FK violation) | BOOT-8 (regression guard) |

---

## Appendix A: BOOT Rule Summary

| Rule | Obligation |
|------|-----------|
| BOOT-1 | No /tmp fallback in `_get_dirs()` — raise explicit error |
| BOOT-2 | Validate directory existence or creatability |
| BOOT-3 | Dispatcher startup validates VNX env vars and state dirs |
| BOOT-4 | Dispatcher verifies DB exists or initializes it at startup |
| BOOT-5 | `.env_override` paths must be within project tree |
| BOOT-6 | Registration BEFORE lease acquisition (FK invariant) |
| BOOT-7 | Registration is fail-closed when VNX_RUNTIME_PRIMARY=1 |
| BOOT-8 | FK constraint `terminal_leases.dispatch_id → dispatches.dispatch_id` preserved |
| BOOT-9 | Chain closeout releases ALL terminal leases to idle |
| BOOT-10 | Closeout procedure: verify → release → audit → confirm |
| BOOT-11 | Generation increment during closeout (stale-release guard) |
| BOOT-12 | Closeout is explicit operator action, not automatic |
| BOOT-13 | No silent degradation — abort with actionable error, not fallback |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Terminal Exclusivity (80) | Lease state model is unchanged. This contract defines cleanup at chain boundaries and registration ordering. |
| Delivery Failure Lease (90) | Lease cleanup sequence (DFL-1 through DFL-5) is unchanged. Registration ordering (BOOT-6) affects Phase 0 (before cleanup is relevant). |
| Queue Truth (70) | Dispatch file disposition is unchanged. Registration failure leaves dispatch in `pending/` (no marker). |
| Requeue And Classification (140) | Registration failure is a new requeueable condition — no marker, stays in pending, auto-retry. |
| Delivery Failure Logging (160) | Registration failure maps to a new failure code if Contract 160 is extended (out of scope here). |

## Appendix C: Current Code Gaps (For PR-1 Implementers)

| File | Line | Gap | Contract Rule |
|------|------|-----|---------------|
| `runtime_core_cli.py` | 59-70 | `_get_dirs()` falls back to `/tmp/vnx-state` | BOOT-1 |
| `dispatcher_v8_minimal.sh` | 392-413 | `rc_register()` is non-fatal (logs and continues) | BOOT-7 |
| `dispatcher_v8_minimal.sh` | 1562-1580 | Lease acquire (1562) before register (1580) — FK violation path | BOOT-6 |
| `dispatcher_v8_minimal.sh` | startup | No VNX_STATE_DIR/VNX_DATA_DIR validation at startup | BOOT-3 |
| N/A | N/A | No chain-closeout procedure exists | BOOT-9, BOOT-10 |
