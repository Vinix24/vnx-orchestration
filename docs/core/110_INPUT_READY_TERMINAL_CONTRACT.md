# Input-Ready Terminal Contract

**Status**: Canonical
**Feature**: Terminal Input-Ready Mode Guard
**PR**: PR-0
**Gate**: `gate_pr0_input_ready_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for what makes a tmux pane dispatch-safe with respect to tmux input mode. All downstream PRs (PR-1 and PR-2) implement against this contract. Any component that delivers a dispatch via `tmux send-keys` must conform to the rules defined here.

---

## 1. Why This Exists

### 1.1 The Problem

Dispatches often begin with a slash-prefixed skill invocation (e.g., `/architect`, `/backend-developer`). The V8 hybrid dispatch mechanism delivers this skill command via `tmux send-keys -l` (dispatcher_v8_minimal.sh, line 1625).

This is safe only when the target pane is in **normal input mode** (`pane_in_mode = 0`). When a pane is in tmux copy-mode or search-mode (`pane_in_mode = 1`):

- The `/` character is interpreted by tmux as "search down" (in copy-mode with vi bindings) or "search forward" (in emacs bindings).
- The remainder of the skill command becomes tmux search input, not CLI input.
- The instruction payload pasted via `paste-buffer` lands in the search prompt or is silently discarded.
- The final `Enter` executes the search, not the dispatch submission.
- The dispatcher reports delivery success (tmux accepted the keys) while the worker never received the prompt.

This creates a **silent dispatch-corruption path** — the most dangerous class of delivery failure because it is invisible to the coordination layer.

### 1.2 The Current Gap

The dispatcher currently checks prompt-readiness (line 896) only after clear-context operations, looking for visual prompt characters (`>`, `$`, `%`, `>`). This check:

- Runs only when `clear_context=true` — most dispatches skip it.
- Does not query tmux mode state at all — it reads pane content, not pane metadata.
- Cannot detect copy-mode or search-mode, which display the same pane content as normal mode.
- Cannot distinguish between "prompt visible" and "prompt visible but pane is in copy-mode overlay".

The Terminal Exclusivity Contract (80) defines when a terminal is safe from a **coordination perspective** (lease, claim, busy/idle). It does not address whether the tmux pane itself can receive keystrokes correctly. The Delivery Failure Lease Contract (90) defines cleanup after delivery failure but cannot prevent the silent corruption described above, because tmux reports the keys as successfully sent.

### 1.3 The Fix

Add a new pre-delivery invariant: **input-readiness**. Before any `tmux send-keys` delivery, the dispatcher must prove that the target pane is in normal input mode. If the pane is in a blocking mode, the dispatcher must attempt deterministic recovery. If recovery fails, the dispatcher must block delivery and emit explicit audit evidence.

This contract sits between lease acquisition (Phase 1 in the Delivery Failure Lease Contract) and tmux key delivery (Phase 2). It introduces no new lease semantics — it adds a mode-state gate within the existing delivery sequence.

---

## 2. Definitions

### 2.1 Input-Ready

A tmux pane is **input-ready** when all of the following are true:

| Property | Required Value | Query Method |
|----------|---------------|--------------|
| `pane_in_mode` | `0` | `tmux display-message -p -t {pane} '#{pane_in_mode}'` |
| Pane exists | Yes | `tmux has-session` / `tmux list-panes` succeeds for target |
| Pane is alive | Yes | `pane_dead` = `0` via `tmux display-message -p -t {pane} '#{pane_dead}'` |

A pane that satisfies all three properties is **input-ready**. A pane that fails any property is **input-blocked**.

### 2.2 Input-Blocked

A pane is **input-blocked** when `pane_in_mode = 1`. This is a boolean — tmux does not distinguish between copy-mode and search-mode at the `pane_in_mode` level. Both set `pane_in_mode = 1`.

Additional tmux format variables available for audit enrichment (not required for the blocking decision):

| Variable | Purpose |
|----------|---------|
| `pane_mode` | Name of the active mode (e.g., `copy-mode`, `view-mode`) |
| `copy_cursor_x`, `copy_cursor_y` | Cursor position within copy-mode (indicates search activity) |
| `pane_searching` | `1` if an active search is in progress within copy-mode |

These variables are informational. The blocking decision is made solely on `pane_in_mode`.

### 2.3 Dispatch-Safe

A pane is **dispatch-safe** when it is:

1. **Input-ready** (this contract, Section 2.1), AND
2. **Lease-available** (Terminal Exclusivity Contract, Section 2.1), AND
3. **Pane-resolved** (pane discovery has mapped the terminal ID to a valid tmux pane target)

All three conditions must be true before delivery. This contract governs condition (1).

---

## 3. Blocking Mode States

### 3.1 Mode State Classification

tmux panes enter non-normal modes through user interaction or programmatic commands. The following modes set `pane_in_mode = 1` and **block dispatch delivery**:

| Mode | How Entered | Effect on `/` Keystroke | Frequency in VNX |
|------|-------------|------------------------|-------------------|
| **copy-mode** | Mouse scroll, `Prefix + [`, `copy-mode` command | `/` = search forward (vi) or incremental search (emacs) | Common (mouse-enabled tmux) |
| **copy-mode-vi** | Same as copy-mode when `mode-keys vi` is set | `/` = search down | Common (default vi bindings) |
| **view-mode** | Pane output exceeds history, `Prefix + Page-Up` | `/` = search within view | Rare |

All modes that set `pane_in_mode = 1` are treated identically by this contract: they block dispatch delivery. There is no allow-list of "safe" non-normal modes.

### 3.2 The Rule

**IMR-1 (Input-Mode Rule 1)**: The dispatcher MUST NOT deliver keystrokes via `tmux send-keys` to any pane where `pane_in_mode != 0`.

This is a hard constraint. There are no exceptions, overrides, or force-delivery flags.

---

## 4. Input-Readiness Probe

### 4.1 Probe Specification

The input-readiness probe is a single tmux query that returns the mode state of the target pane:

```bash
tmux display-message -p -t "$target_pane" '#{pane_in_mode}:#{pane_dead}:#{pane_mode}'
```

This returns a colon-separated triple, e.g.:
- `0:0:` — normal mode, alive, no mode name (input-ready)
- `1:0:copy-mode` — copy-mode active, alive (input-blocked)
- `1:0:copy-mode-vi` — vi copy-mode active, alive (input-blocked)

### 4.2 Probe Interpretation

| `pane_in_mode` | `pane_dead` | Interpretation | Action |
|---------------|-------------|----------------|--------|
| `0` | `0` | Input-ready | Proceed to delivery |
| `1` | `0` | Input-blocked (mode active) | Enter recovery (Section 5) |
| Any | `1` | Pane is dead | Abort delivery, emit `pane_dead` failure |
| Query fails | N/A | Pane unreachable or session lost | Abort delivery, emit `probe_failed` failure |

### 4.3 Probe Placement

The probe MUST execute:
- **After** lease acquisition (the dispatcher already holds the lease and claim).
- **Before** any `tmux send-keys` call in the delivery sequence.
- **Before** mode control operations (`force_normal`, `clear_context`, model switch) that themselves use `send-keys`.

This means the probe runs at the beginning of `configure_terminal_mode()` or at the start of `dispatch_with_skill_activation()` after the pane target is resolved, whichever provides the earliest gate.

### 4.4 Probe Timing

The probe must be executed as close to the delivery moment as practical. A stale probe (e.g., checked 30 seconds before delivery) provides weak guarantees because the user can enter copy-mode between the probe and delivery. The maximum acceptable staleness is **2 seconds** — if more than 2 seconds elapse between the probe and the first `send-keys` call, the probe must be re-executed.

---

## 5. Recovery

### 5.1 Recovery Philosophy

When a pane is input-blocked, the dispatcher SHOULD attempt to restore normal input mode before failing the dispatch. This is a pragmatic concession: mouse-enabled tmux environments frequently leave panes in copy-mode after accidental scroll events, and failing every such dispatch would create excessive operator friction.

However, recovery must be:
- **Bounded**: A finite number of attempts with a wall-clock timeout.
- **Deterministic**: The same recovery action is applied to every blocked mode.
- **Verifiable**: The probe is re-executed after recovery to confirm success.
- **Auditable**: Every recovery attempt is logged with mode state before and after.

### 5.2 Allowed Recovery Actions

The following actions are permitted as recovery attempts:

| Action | tmux Command | Purpose |
|--------|-------------|---------|
| **Cancel mode** | `tmux send-keys -t {pane} q` | Exits copy-mode (vi bindings: `q` quits copy-mode) |
| **Escape mode** | `tmux send-keys -t {pane} Escape` | Exits copy-mode (emacs bindings and universal fallback) |
| **Programmatic cancel** | `tmux copy-mode -q -t {pane}` | Directly cancels copy-mode via tmux command (not keystrokes) |

The preferred recovery action is **programmatic cancel** (`tmux copy-mode -q`) because it does not depend on key bindings and cannot be misinterpreted by the pane process.

### 5.3 Recovery Sequence

The recovery sequence is:

```
1. Probe returns pane_in_mode = 1
2. Log: recovery_started, mode={pane_mode}, terminal={terminal_id}, dispatch={dispatch_id}
3. Execute: tmux copy-mode -q -t {pane}
4. Wait: 200ms (allow tmux to process mode exit)
5. Re-probe: tmux display-message -p -t {pane} '#{pane_in_mode}:#{pane_dead}:#{pane_mode}'
6. IF pane_in_mode = 0:
     Log: recovery_succeeded, mode_before={mode}, terminal={terminal_id}
     Proceed to delivery
7. IF pane_in_mode = 1:
     Attempt fallback: tmux send-keys -t {pane} Escape
     Wait: 200ms
     Re-probe
8. IF pane_in_mode = 0:
     Log: recovery_succeeded_fallback, mode_before={mode}, terminal={terminal_id}
     Proceed to delivery
9. IF pane_in_mode = 1:
     Log: recovery_failed, mode={pane_mode}, terminal={terminal_id}
     BLOCK delivery (Section 6)
```

### 5.4 Recovery Limits

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max recovery attempts | 2 (programmatic cancel + escape fallback) | More attempts risk side effects; if two methods fail, the mode is non-standard |
| Recovery timeout (wall clock) | 1 second total | Recovery must not materially delay the dispatch pipeline |
| Post-recovery re-probe required | Yes, always | Recovery without verification is indistinguishable from no recovery |

### 5.5 Recovery Prohibitions

The following actions are **NOT** permitted during recovery:

| Prohibited Action | Reason |
|-------------------|--------|
| `tmux send-keys C-c` | May kill the CLI process running in the pane (documented risk in dispatcher line 864) |
| `tmux send-keys C-z` | Suspends the foreground process |
| `tmux respawn-pane` | Destroys the pane process; equivalent to killing the worker |
| `tmux kill-pane` + recreate | Terminal identity is canonical (doc 21); pane destruction is a reheal operation, not a recovery action |
| Arbitrary key sequences | Unpredictable interaction with the CLI process under copy-mode |
| Silent retry loops | Retrying recovery beyond the defined limit without audit evidence |

---

## 6. Fail-Closed Behavior

### 6.1 The Rule

**IMR-2 (Input-Mode Rule 2)**: If the dispatcher cannot prove that a pane is input-ready after exhausting allowed recovery actions, the dispatch MUST be blocked. The dispatcher MUST NOT deliver keystrokes to the pane.

Fail-closed means:
- No delivery attempt is made.
- The dispatch is **not silently dropped** — it enters the requeue or failure path.
- The canonical lease is released (per Delivery Failure Lease Contract, Phase 1 cleanup).
- The terminal claim is released.
- An explicit audit event explains why delivery was blocked.

### 6.2 Blocked Dispatch Disposition

When delivery is blocked due to unrecoverable input-mode state:

| Action | Responsibility | Detail |
|--------|---------------|--------|
| Release canonical lease | Dispatcher | Same as any Phase 1 failure (DFL contract) |
| Release terminal claim | Dispatcher | Same as any Phase 1 failure |
| Record broker failure | Dispatcher | `mark_delivery_failed(reason="input_mode_blocked")` |
| Move dispatch file | Dispatcher | Move to `rejected/` with `input_mode_blocked` classification |
| Emit coordination event | Dispatcher | `input_mode_delivery_blocked` event (Section 7) |
| Requeue decision | T0 / Operator | Blocked dispatch may be retried on the same terminal after mode is resolved, or rerouted to another terminal |

### 6.3 Terminal State After Block

After a blocked delivery, the terminal is:
- **Lease-idle**: The lease has been released.
- **Input-blocked**: The pane remains in its non-normal mode (recovery failed).
- **Not auto-recovered**: The dispatcher does not attempt further mode recovery after the block decision.

The pane will remain input-blocked until:
- The operator manually exits copy-mode, OR
- A subsequent dispatch triggers a successful recovery cycle, OR
- The tmux session is restarted/rehealed.

---

## 7. Audit Evidence

### 7.1 Required Audit Events

Every input-readiness interaction must emit a structured coordination event. The following events are required:

| Event | Emitted When | Required Fields |
|-------|-------------|-----------------|
| `input_mode_probed` | Every probe execution | `terminal_id`, `pane_target`, `pane_in_mode`, `pane_dead`, `pane_mode`, `dispatch_id`, `timestamp` |
| `input_mode_recovery_started` | Recovery begins | `terminal_id`, `pane_target`, `mode_before`, `recovery_action`, `dispatch_id`, `timestamp` |
| `input_mode_recovery_succeeded` | Recovery restores normal mode | `terminal_id`, `pane_target`, `mode_before`, `recovery_action`, `dispatch_id`, `timestamp` |
| `input_mode_recovery_failed` | All recovery attempts exhausted | `terminal_id`, `pane_target`, `mode_before`, `attempts`, `dispatch_id`, `timestamp` |
| `input_mode_delivery_blocked` | Delivery blocked due to unrecoverable mode | `terminal_id`, `pane_target`, `mode_before`, `dispatch_id`, `reason`, `timestamp` |

### 7.2 Audit Trail Location

Events are written to the VNX structured audit log via the existing `log_structured_event()` function in the dispatcher. They follow the same NDJSON format as existing coordination events.

### 7.3 Evidence Linking

The `dispatch_id` field in every event links input-readiness audit evidence to the dispatch lifecycle. This allows:
- T0 to understand why a dispatch was blocked.
- The receipt processor to include mode-recovery events in the dispatch receipt.
- Post-mortem analysis to correlate copy-mode incidents with mouse/keyboard activity.

---

## 8. Integration Points

### 8.1 Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Terminal Exclusivity (80) | Orthogonal. Exclusivity governs lease/claim availability. This contract governs pane input-mode. Both must pass before delivery. |
| Delivery Failure Lease (90) | This contract adds a new failure reason (`input_mode_blocked`) to Phase 1. Lease cleanup follows the same rules as any Phase 1 failure. |
| FPC Execution Contracts (30) | Input-readiness applies to `interactive_tmux_*` execution targets only. Headless targets (`headless_*_cli`) do not use tmux `send-keys` and are not subject to this contract. |
| Tmux Identity Invariants (21) | Pane identity is derived (Layer 2). If a pane is remapped, the input-readiness probe must use the current pane target, not a cached one. |

### 8.2 Dispatcher Integration Point

The input-readiness probe and recovery sequence insert into the existing `dispatch_with_skill_activation()` function at the following point in the delivery sequence:

```
[Existing] Lease acquired (line ~1389)
[Existing] Prompt file written (line ~1404)
[Existing] Broker registration (line ~1407)
[Existing] Delivery-start recorded (line ~1435)
            ↓
[NEW]    Input-readiness probe
[NEW]    Recovery if blocked
[NEW]    Block if recovery fails (release lease, abort)
            ↓
[Existing] configure_terminal_mode() — force_normal, clear_context, model switch
[Existing] Skill activation via send-keys (line ~1625)
[Existing] Instruction via paste-buffer (line ~1635)
[Existing] Enter submission (line ~1662)
```

### 8.3 Headless Exemption

Headless execution targets (`headless_claude_cli`, `headless_codex_cli`) invoke the CLI as a subprocess without tmux. They are exempt from this contract. The input-readiness check MUST be skipped for headless targets to avoid false probe failures.

---

## 9. Implementation Constraints For PR-1

PR-1 (Dispatcher Input-Mode Detection And Recovery) implements against this contract. The following constraints are binding:

1. **The probe query format** (Section 4.1) is canonical. PR-1 must use `tmux display-message -p` with the specified format variables.
2. **The recovery sequence** (Section 5.3) is canonical. PR-1 must follow the exact order: programmatic cancel first, escape fallback second.
3. **The recovery limits** (Section 5.4) are canonical. PR-1 must not exceed 2 recovery attempts or 1 second wall-clock timeout.
4. **The recovery prohibitions** (Section 5.5) are canonical. PR-1 must not use any prohibited action.
5. **The fail-closed rule** (IMR-2, Section 6.1) is canonical. PR-1 must block delivery when recovery fails.
6. **The audit events** (Section 7.1) are canonical. PR-1 must emit all specified events with all required fields.
7. **The integration point** (Section 8.2) is canonical. The probe must run after lease acquisition and before any `send-keys` call.

---

## 10. Verification Criteria For PR-2

PR-2 (Real Reproduction Certification) certifies this contract by reproducing the `search down` failure mode. The following must be demonstrated:

1. A pane in copy-mode receives a slash-prefixed dispatch attempt.
2. The dispatcher detects `pane_in_mode = 1` and enters recovery.
3. Recovery either succeeds (pane returns to normal mode, dispatch delivered) or fails (dispatch blocked).
4. In the failure case, no partial keystrokes reach the CLI process.
5. Audit evidence distinguishes recovered delivery from blocked delivery.
6. The original `search down` corruption path is no longer reachable through the dispatcher.

---

## Appendix A: tmux Mode Reference

For implementors and reviewers, the following tmux format variables are relevant to this contract:

| Variable | Type | Description |
|----------|------|-------------|
| `pane_in_mode` | Boolean (0/1) | Whether the pane is in any special mode |
| `pane_mode` | String | Name of the current mode (empty when normal) |
| `pane_dead` | Boolean (0/1) | Whether the pane process has exited |
| `pane_searching` | Boolean (0/1) | Whether a search is active in copy-mode |
| `copy_cursor_x` | Integer | X position of cursor in copy-mode |
| `copy_cursor_y` | Integer | Y position of cursor in copy-mode |
| `pane_id` | String | Unique pane identifier (e.g., `%42`) |
| `pane_pid` | Integer | PID of the process running in the pane |

All variables are queried via `tmux display-message -p -t {pane} '#{variable}'`.

## Appendix B: Real Incident Trace

The motivating incident for this contract:

```
1. Operator scrolled mouse wheel in T3 pane (accidental)
2. tmux entered copy-mode-vi (pane_in_mode = 1)
3. Operator did not notice and switched focus away
4. T0 created dispatch for T3 with skill /architect
5. Dispatcher acquired lease, resolved pane, called send-keys -l "/architect"
6. tmux interpreted "/" as "search down" in copy-mode-vi
7. "architect" became the search term
8. paste-buffer instruction landed in the search prompt
9. Enter executed the search, not the dispatch
10. Dispatcher logged delivery_success (tmux accepted the keys)
11. Worker (Claude Code) never received the prompt
12. T0 waited for receipt that never arrived
13. Lease expired after 600s, terminal recovered by reconciler
14. Dispatch was lost — no audit trail explained the silent failure
```

This contract ensures step 5 is preceded by a `pane_in_mode` check that would have caught the copy-mode state at step 2 and either recovered it or blocked the dispatch with explicit audit evidence.
