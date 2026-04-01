# Runtime State Machine Contract And Operator Truth Rules

**Status**: Canonical
**Feature**: Autonomous Runtime State Machine And Stall Supervision
**PR**: PR-0
**Gate**: `gate_pr0_runtime_state_machine_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for the canonical worker/session lifecycle, heartbeat semantics, stall classification, tie-break rules across state surfaces, and the escalation policy for anomalous runtime states. All downstream PRs (PR-1 through PR-3) implement against this contract.

Related contracts:
- [80_TERMINAL_EXCLUSIVITY_CONTRACT](80_TERMINAL_EXCLUSIVITY_CONTRACT.md) — dispatch safety and lease acquisition
- [90_DELIVERY_FAILURE_LEASE_CONTRACT](90_DELIVERY_FAILURE_LEASE_CONTRACT.md) — lease cleanup on failed delivery
- [120_PROJECTION_CONSISTENCY_CONTRACT](120_PROJECTION_CONSISTENCY_CONTRACT.md) — queue/runtime projection alignment

---

## 1. Why This Exists

### 1.1 The Problem

The current runtime layer tracks **dispatch states** (queued through completed) and **lease states** (idle through released) but has no canonical model for what a worker is actually doing between lease acquisition and lease release. This gap creates four classes of silent failure:

1. **Mystery idle**: A worker finishes or crashes but its terminal appears busy because the lease was not released. The reconciler eventually expires the lease via TTL, but during the 10-minute window the terminal is silently stranded.

2. **No-output hang**: A worker is technically alive (process exists, heartbeat renews) but has produced no output for an extended period. Nothing in the current system distinguishes "thinking for 3 minutes" from "hung on a broken MCP tool call for 20 minutes."

3. **Stale session persistence**: A headless or interactive session stops making progress but no governance object is created. T0 must manually inspect logs to discover the failure.

4. **Ambiguous runtime truth**: When lease state, dispatch state, terminal activity, and queue projection disagree, there is no deterministic rule for which surface to trust. Operators fall back to ad hoc inspection.

### 1.2 The Fix

Layer a **worker state model** on top of the existing lease/dispatch infrastructure that:

- Defines explicit, finite worker states with allowed transitions
- Uses heartbeat freshness and output recency to classify worker health
- Creates deterministic tie-break rules when surfaces disagree
- Escalates anomalous states to open items automatically, before T0 has to discover them

---

## 2. State Architecture

### 2.1 Three-Layer Model

The VNX runtime tracks state at three layers. Each layer has a distinct canonical source and update authority:

| Layer | Canonical Source | Update Authority | Scope |
|-------|-----------------|-----------------|-------|
| **Dispatch** | `dispatches` table in `runtime_coordination.db` | Dispatcher, broker, reconciler | Tracks the lifecycle of a unit of work from queue to completion |
| **Lease** | `terminal_leases` table in `runtime_coordination.db` | Lease manager, reconciler | Tracks terminal ownership — which dispatch holds which terminal |
| **Worker** | `worker_states` table in `runtime_coordination.db` (new) | Worker supervisor, heartbeat monitor | Tracks what the worker process is actually doing during execution |

The worker layer is new. It sits between lease acquisition and lease release, providing runtime observability that the lease layer cannot.

### 2.2 Relationship Between Layers

```
Dispatch Layer:  queued → claimed → delivering → accepted → running → completed
                                                     ↓           ↓
Lease Layer:                              idle → leased ──────→ released → idle
                                                   ↓
Worker Layer:                              initializing → working ⇄ idle_between_tasks
                                                            ↓
                                                  stalled / blocked / awaiting_input
                                                            ↓
                                                  exited_clean / exited_bad / resume_unsafe
```

Key principle: **the worker layer never contradicts the lease layer**. If the lease is `idle`, no worker state exists. Worker state is only valid while a lease is `leased`.

---

## 3. Canonical Worker States

### 3.1 State Definitions

| State | Description | Entry Condition | Heartbeat Required | Output Expected |
|-------|-------------|-----------------|-------------------|-----------------|
| `initializing` | Worker process is starting, loading context, reading dispatch | Lease acquired, process spawned | Yes (within startup grace) | No |
| `working` | Worker is actively producing output | First output detected after init | Yes | Yes (within output threshold) |
| `idle_between_tasks` | Worker completed a sub-task, awaiting next instruction within same dispatch | Worker signals sub-task boundary | Yes | No (grace period applies) |
| `stalled` | Worker is alive (heartbeat active) but has produced no output beyond the threshold | Output silence exceeds `stall_threshold` | Yes | No (threshold exceeded) |
| `blocked` | Worker has explicitly signaled it cannot proceed (tool failure, permission denied, resource unavailable) | Worker emits blocked signal with reason | Yes | No |
| `awaiting_input` | Worker requires operator input to continue (interactive mode only) | Worker emits input-request signal | Yes | No |
| `exited_clean` | Worker process terminated with exit code 0 and produced expected artifacts | Process exit with code 0 | No (terminal state) | N/A |
| `exited_bad` | Worker process terminated with non-zero exit code or without expected artifacts | Process exit with code != 0, or missing artifacts on exit 0 | No (terminal state) | N/A |
| `resume_unsafe` | Worker was in `stalled`, `blocked`, or `awaiting_input` when a timeout or operator intervention occurred, and the session state may be inconsistent | Forced termination or TTL expiry during non-clean state | No (terminal state) | N/A |

### 3.2 State Transition Matrix

Valid transitions — any transition not listed here is illegal and must be rejected:

| From → To | `initializing` | `working` | `idle_between_tasks` | `stalled` | `blocked` | `awaiting_input` | `exited_clean` | `exited_bad` | `resume_unsafe` |
|-----------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `initializing` | — | Y | — | Y | Y | — | Y | Y | Y |
| `working` | — | — | Y | Y | Y | Y | Y | Y | Y |
| `idle_between_tasks` | — | Y | — | Y | — | — | Y | Y | Y |
| `stalled` | — | Y | — | — | — | — | — | Y | Y |
| `blocked` | — | Y | — | — | — | — | — | Y | Y |
| `awaiting_input` | — | Y | — | — | — | — | — | Y | Y |
| `exited_clean` | — | — | — | — | — | — | — | — | — |
| `exited_bad` | — | — | — | — | — | — | — | — | — |
| `resume_unsafe` | — | — | — | — | — | — | — | — | — |

Key invariants:

- **W-T1**: Terminal states (`exited_clean`, `exited_bad`, `resume_unsafe`) have no outgoing transitions.
- **W-T2**: `stalled` can only recover to `working` (output resumes) or exit. It cannot transition to `idle_between_tasks` because silence after a stall requires active output to resolve.
- **W-T3**: `blocked` and `awaiting_input` can only recover to `working` (unblocked) or exit. They cannot silently become `idle_between_tasks`.
- **W-T4**: `initializing` cannot reach `idle_between_tasks` or `awaiting_input` — the worker must produce output (enter `working`) before it can signal sub-task boundaries or request input.

---

## 4. Heartbeat Semantics

### 4.1 Heartbeat Contract

The heartbeat is the worker's proof of life. It operates at the lease layer (existing) but the worker layer interprets it for health classification.

| Field | Location | Updated By | Frequency |
|-------|----------|-----------|-----------|
| `last_heartbeat_at` | `terminal_leases.last_heartbeat_at` | Worker heartbeat loop | Every `heartbeat_interval` seconds |
| `last_output_at` | `worker_states.last_output_at` (new) | Worker supervisor / output monitor | On every detected output event |
| `state_entered_at` | `worker_states.state_entered_at` (new) | State transition logic | On every state change |

### 4.2 Freshness Classification

Heartbeat freshness determines whether the worker process is alive. Output recency determines whether the worker is making progress.

| Classification | Condition | Meaning |
|---------------|-----------|---------|
| **fresh** | `now - last_heartbeat_at < heartbeat_stale_threshold` | Process is alive and renewing |
| **stale** | `heartbeat_stale_threshold ≤ now - last_heartbeat_at < heartbeat_dead_threshold` | Process may be hung or system under load — warning state |
| **dead** | `now - last_heartbeat_at ≥ heartbeat_dead_threshold` | Process is assumed dead — escalation required |

### 4.3 Threshold Defaults

| Parameter | Default | Configurable | Notes |
|-----------|---------|-------------|-------|
| `heartbeat_interval` | 30s | Yes | How often the worker sends a heartbeat |
| `heartbeat_stale_threshold` | 90s (3× interval) | Yes | Missed heartbeats before warning |
| `heartbeat_dead_threshold` | 300s (10× interval) | Yes | Missed heartbeats before assumed dead |
| `startup_grace_period` | 120s | Yes | Time allowed for `initializing` before stall classification |
| `stall_threshold` | 180s | Yes | Output silence in `working` state before `stalled` classification |
| `idle_between_tasks_grace` | 120s | Yes | Silence in `idle_between_tasks` before `stalled` classification |

### 4.4 Heartbeat Invariants

- **H-1**: A heartbeat with a stale generation is silently rejected (existing G-R3 behavior). No state transition occurs.
- **H-2**: A heartbeat does not reset `last_output_at`. Heartbeat proves liveness; output proves progress. These are independent signals.
- **H-3**: If `last_heartbeat_at` crosses the `dead` threshold, the worker transitions to `exited_bad` regardless of its current worker state (unless already in a terminal state).
- **H-4**: If `last_heartbeat_at` crosses the `stale` threshold while the worker is in `working`, this is a **warning** — logged but not an automatic state transition. The output monitor handles progress detection independently.

---

## 5. No-Output Detection

### 5.1 Output Events

An "output event" is any worker-produced artifact that proves progress:

| Output Type | Applicable To | Detection Method |
|------------|--------------|-----------------|
| stdout/stderr content | Headless workers | Supervisor monitors subprocess pipe |
| File write to report directory | Both headless and interactive | Filesystem watcher on `unified_reports/` and dispatch artifacts |
| Coordination event emitted | Both | `coordination_events` table insert with worker as actor |
| Git commit on working branch | Both | Git ref change detection |
| Explicit progress signal | Both | Worker writes to progress channel (structured heartbeat extension) |

### 5.2 No-Output Classification

| Worker State | Output Silence Duration | Classification | Action |
|-------------|------------------------|---------------|--------|
| `initializing` | < `startup_grace_period` | Normal | None |
| `initializing` | ≥ `startup_grace_period` | Startup stall | Transition to `stalled`, create open item |
| `working` | < `stall_threshold` | Normal | None |
| `working` | ≥ `stall_threshold` | Progress stall | Transition to `stalled`, create open item |
| `idle_between_tasks` | < `idle_between_tasks_grace` | Normal | None |
| `idle_between_tasks` | ≥ `idle_between_tasks_grace` | Inter-task stall | Transition to `stalled`, create open item |
| `stalled` | Any (heartbeat fresh) | Confirmed stall | Open item already exists; update staleness duration |
| `stalled` | Any (heartbeat dead) | Dead stall | Transition to `exited_bad`, escalate open item to blocking |
| `blocked` | Any | Expected | None (worker explicitly signaled block) |
| `awaiting_input` | Any | Expected | None (worker explicitly signaled input needed) |

### 5.3 Headless vs Interactive Differences

| Behavior | Headless | Interactive |
|----------|----------|-------------|
| `awaiting_input` state | Not applicable — headless workers never request input | Valid — interactive workers may prompt operator |
| stdout monitoring | Direct pipe from supervisor | Not available — tmux pane capture only |
| Stall threshold | Standard (`stall_threshold`) | Extended (`stall_threshold × 1.5`) — interactive workers may pause for operator reading |
| Output detection reliability | High (supervisor owns the pipe) | Medium (depends on tmux capture frequency) |
| Auto-termination on dead heartbeat | Yes — supervisor kills subprocess | No — open item created for operator action |

---

## 6. Operator Truth And Tie-Break Policy

### 6.1 State Surfaces

Three surfaces report information about terminal and worker state. They can disagree:

| Surface | Source | What It Reports | Freshness |
|---------|--------|----------------|-----------|
| **Runtime DB** | `runtime_coordination.db` (dispatches + terminal_leases + worker_states) | Canonical dispatch, lease, and worker state | Real-time (SQLite, WAL mode) |
| **Queue Projection** | `pr_queue_state.json` | Which dispatches are queued, in-progress, or completed | Derived — may lag by up to one projection cycle |
| **Terminal Activity** | tmux pane output, process table, log recency | Whether a terminal appears active | Observable but not authoritative |

### 6.2 Canonical Truth Hierarchy

When surfaces disagree, trust them in this order:

```
1. Runtime DB  (highest authority — canonical state machine)
2. Queue Projection  (derived but structured — use to detect DB inconsistency)
3. Terminal Activity  (observable but ambiguous — never overrides DB)
```

**Rule T-1**: The Runtime DB is always the canonical truth. No operator action, dashboard display, or automated decision may contradict it.

**Rule T-2**: Queue Projection is a derived surface. If it disagrees with the Runtime DB, the projection is stale — not the DB. The correct response is to re-project, not to "fix" the DB to match the projection.

**Rule T-3**: Terminal Activity (tmux output, process existence) is an observation, not a state. A terminal that looks active in tmux but has `lease.state = idle` in the DB is **idle**. A terminal that looks silent but has `lease.state = leased` is **leased**.

### 6.3 Deterministic Tie-Break Rules

| Scenario | Runtime DB Says | Queue Projection Says | Terminal Activity Says | Resolution |
|----------|----------------|----------------------|----------------------|------------|
| **Normal operation** | leased + working | dispatch in-progress | output flowing | No conflict. All surfaces agree. |
| **Projection lag** | leased + working | dispatch queued | output flowing | Queue projection is stale. Re-project from DB. No state change. |
| **Silent terminal** | leased + working | dispatch in-progress | no recent output | Worker may be stalled. Apply no-output detection (§5). Do not override DB. |
| **Zombie lease** | leased | dispatch completed/expired | terminal silent | Mismatch: lease should have been released. Create blocking open item. Reconciler releases lease. |
| **Ghost dispatch** | idle | dispatch in-progress (claimed/delivering/running) | terminal may or may not be active | Mismatch: work is claimed but no lease exists. Create blocking open item. Reconciler requeues or dead-letters. |
| **Phantom activity** | idle | no dispatch | terminal shows output | Terminal is running outside VNX governance. Create warning open item. Do not acquire lease. |
| **Stale heartbeat** | leased (expires_at passed) | dispatch in-progress | terminal silent | Lease TTL expired. Reconciler transitions lease to `expired`. Worker state → `exited_bad`. |
| **Output without heartbeat** | leased (heartbeat stale) | dispatch in-progress | output flowing | Heartbeat renewal is broken but worker is producing. Create warning open item. Do not kill — heartbeat bug, not worker bug. |

### 6.4 Decision Flowchart For T0

When T0 needs to determine terminal truth:

```
1. Read terminal_leases row from runtime_coordination.db
2. If lease.state = idle:
   a. Check dispatches table for any active dispatch targeting this terminal
   b. If found → ghost_dispatch anomaly (§7)
   c. If not found → terminal is genuinely idle
3. If lease.state = leased:
   a. Read worker_states row for this terminal
   b. If worker_state is a terminal state (exited_*) → zombie_lease anomaly (§7)
   c. If worker_state is stalled/blocked/awaiting_input → report as-is, check open items
   d. If worker_state is working/initializing → check heartbeat freshness
      - fresh → terminal is healthy
      - stale → warning, check output recency
      - dead → escalate to exited_bad
4. If lease.state = expired/recovering:
   a. Terminal is in recovery. Do not dispatch.
   b. Check if recovery has been pending beyond recovery_timeout → escalate
```

---

## 7. Anomaly Classification Matrix

### 7.1 Anomaly Types

Every anomalous runtime situation maps to exactly one anomaly type, one severity, and one escalation action:

| Anomaly | Description | Severity | Auto-Create Open Item | Escalation |
|---------|-------------|----------|----------------------|------------|
| `startup_stall` | Worker in `initializing` beyond `startup_grace_period` | warning | Yes | Log + open item. Worker supervisor may retry after grace+60s. |
| `progress_stall` | Worker in `working` with no output beyond `stall_threshold` | warning | Yes | Log + open item. If heartbeat still fresh, monitor. If stale, escalate to `dead_worker`. |
| `inter_task_stall` | Worker in `idle_between_tasks` beyond `idle_between_tasks_grace` | warning | Yes | Log + open item. Transition worker to `stalled`. |
| `dead_worker` | Heartbeat crossed `heartbeat_dead_threshold` | blocking | Yes | Transition worker to `exited_bad`. Lease eligible for expiry. Open item blocks new dispatch to terminal until resolved. |
| `zombie_lease` | Lease is `leased` but dispatch is in terminal state | blocking | Yes | Reconciler releases lease. Open item records the stranding duration and cause. |
| `ghost_dispatch` | Dispatch is active but no lease exists for target terminal | blocking | Yes | Reconciler requeues or dead-letters dispatch. Open item records the gap. |
| `phantom_activity` | Terminal shows activity but lease is `idle` and no dispatch is active | info | Yes | Warning-level open item. Does not block dispatch. Operator should investigate. |
| `heartbeat_without_output` | Heartbeat is fresh but no output for extended period (2× `stall_threshold`) | warning | Yes | Possible infinite loop or blocked I/O. Open item for operator review. |
| `output_without_heartbeat` | Output detected but heartbeat is stale | warning | Yes | Heartbeat mechanism is broken. Worker is likely alive. Do not kill. Open item for investigation. |
| `projection_drift` | Queue projection disagrees with runtime DB for > 60s | info | Yes | Re-project. If drift persists after re-projection, create warning open item. |
| `recovery_timeout` | Terminal in `expired` or `recovering` state beyond `recovery_timeout` (600s) | blocking | Yes | Terminal is stuck in recovery. Escalate to operator. Block all dispatch to terminal. |
| `bad_exit_no_artifacts` | Worker exited with code 0 but expected artifacts are missing | warning | Yes | Classify as `exited_bad` despite exit code. Open item records missing artifacts. |

### 7.2 Open Item Auto-Creation Contract

When an anomaly is detected, the following fields are written to `open_items.json`:

```json
{
  "id": "<generated-uuid>",
  "type": "runtime_anomaly",
  "anomaly": "<anomaly_type from §7.1>",
  "severity": "blocking|warning|info",
  "terminal_id": "<terminal>",
  "dispatch_id": "<dispatch or null>",
  "worker_state": "<current worker state or null>",
  "lease_state": "<current lease state>",
  "detected_at": "<ISO8601>",
  "evidence": {
    "last_heartbeat_at": "<ISO8601 or null>",
    "last_output_at": "<ISO8601 or null>",
    "heartbeat_age_seconds": "<number>",
    "output_silence_seconds": "<number>",
    "dispatch_state": "<current dispatch state or null>"
  },
  "auto_created": true,
  "resolution": null,
  "resolved_at": null
}
```

### 7.3 Open Item Invariants

- **OI-1**: Every anomaly in §7.1 with `Auto-Create Open Item = Yes` creates an open item without operator intervention.
- **OI-2**: A `blocking` open item prevents new dispatch to the affected terminal until resolved.
- **OI-3**: Open items created by the runtime anomaly detector include `"auto_created": true` so T0 can distinguish them from manually created items.
- **OI-4**: Resolution requires an explicit `resolved_at` timestamp and `resolution` reason. Anomaly auto-detection does not auto-resolve — only the reconciler (for structural fixes like zombie lease release) or the operator can resolve.
- **OI-5**: If an anomaly type already has an unresolved open item for the same terminal and dispatch, do not create a duplicate. Update the existing item's `evidence` and `detected_at` instead.

---

## 8. Worker State Persistence Schema

### 8.1 New Table: `worker_states`

This table is added to `runtime_coordination.db` alongside the existing `dispatches`, `terminal_leases`, and `coordination_events` tables:

```sql
CREATE TABLE IF NOT EXISTS worker_states (
    terminal_id     TEXT    NOT NULL,
    dispatch_id     TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'initializing',
    last_output_at  TEXT,
    state_entered_at TEXT   NOT NULL,
    stall_count     INTEGER NOT NULL DEFAULT 0,
    blocked_reason  TEXT,
    metadata_json   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (terminal_id),
    FOREIGN KEY (terminal_id) REFERENCES terminal_leases(terminal_id),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
);
```

Key design decisions:

- **Single row per terminal**: Only one worker state per terminal at a time (enforced by PK). When a new dispatch begins, the previous row is archived or overwritten.
- **`stall_count`**: Incremented each time the worker enters `stalled`. A worker that repeatedly enters and exits stall is qualitatively different from one that stalls once.
- **`blocked_reason`**: Free-text when state is `blocked` — e.g., "MCP tool timeout: brave_web_search", "Permission denied: git push".
- **`metadata_json`**: Extensible field for future worker telemetry.

### 8.2 Worker State Coordination Events

Every worker state transition appends to the existing `coordination_events` table:

| event_type | from_state | to_state | actor | reason |
|-----------|-----------|---------|-------|--------|
| `worker_state_changed` | Previous worker state | New worker state | `worker_supervisor` or `reconciler` | Human-readable transition cause |
| `worker_stall_detected` | `working` or `initializing` | `stalled` | `stall_detector` | Output silence details |
| `worker_output_detected` | `stalled` or `initializing` | `working` | `output_monitor` | Output event description |
| `worker_blocked` | Any active state | `blocked` | `worker` | Blocked reason |
| `worker_exited` | Any active state | `exited_clean` or `exited_bad` | `worker_supervisor` | Exit code and artifact status |

### 8.3 Lifecycle

1. **Creation**: When `lease_manager.acquire()` succeeds, insert a `worker_states` row with state `initializing`.
2. **Monitoring**: The worker supervisor updates `last_output_at` and transitions state based on output detection and heartbeat freshness.
3. **Termination**: When the worker process exits, transition to `exited_clean` or `exited_bad`.
4. **Cleanup**: When `lease_manager.release()` succeeds, the `worker_states` row for that terminal is archived (moved to `worker_states_history` or marked with terminal state) so the terminal can accept a new dispatch.

---

## 9. Implementation Guidance For Downstream PRs

### 9.1 PR-1: Session State Machine And Heartbeat Persistence

Must implement:
- The `worker_states` table (§8.1) as a schema migration
- State transition validation matching §3.2
- `last_output_at` tracking for at least one output type (stdout or file write)
- Coordination events for worker state changes (§8.2)
- `initializing → working` transition on first output detection
- `working → exited_clean` / `exited_bad` on process exit

Must NOT implement:
- Stall detection (that's PR-2)
- Open item auto-creation (that's PR-2)
- Tie-break reconciliation (that's a reconciler extension, also PR-2)

### 9.2 PR-2: Stall Detection, Exit Classification, And Open-Item Escalation

Must implement:
- No-output stall detection per §5.2
- All anomaly types from §7.1
- Open item auto-creation per §7.2 and §7.3
- `dead_worker` escalation path (heartbeat dead → `exited_bad` → open item)
- Tie-break detection for zombie_lease, ghost_dispatch, and phantom_activity

Must NOT implement:
- Dashboard UI
- Automatic remediation beyond lease release and open item creation

### 9.3 PR-3: Unattended Runtime Reliability Certification

Must verify:
- All states in §3.1 are reachable under test
- All transitions in §3.2 are exercised
- Stall detection fires correctly for each no-output scenario in §5.2
- Tie-break rules from §6.3 produce correct results
- Open items are auto-created for every anomaly in §7.1
- No silent runtime failure can persist beyond `heartbeat_dead_threshold + stall_threshold`

---

## 10. Residual Risks And Non-Goals

### 10.1 Not Addressed By This Contract

| Topic | Why Not Here | Where It Belongs |
|-------|-------------|-----------------|
| Dashboard visualization of worker state | UI concern, not runtime truth | Feature 13+ (dashboard) |
| Automatic remediation beyond lease release | Requires operator trust model | Future autonomy policy |
| Cross-worker coordination (task handoff) | Not needed for single-dispatch-per-terminal model | Future multi-dispatch work |
| Context window pressure as a stall signal | Requires Claude Code internals access | Future runtime adapter work |
| Learning from stall patterns | Requires intelligence loop | Future intelligence feature |

### 10.2 Known Risks In This Design

| Risk | Mitigation |
|------|-----------|
| Interactive worker stall thresholds may be too aggressive | Default is 1.5× headless threshold; configurable per terminal |
| Output detection via tmux capture is inherently lossy | PR-1 should implement at least one non-tmux output signal (file write, coordination event) |
| `stall_count` accumulation could mask intermittent infrastructure issues | PR-3 certification must verify that repeated stalls escalate open item severity |
| Open item flood during system-wide outage | OI-5 deduplication prevents duplicates per terminal+dispatch; system-wide outages should be a single open item |

---

## Appendix A: State Diagram (ASCII)

```
                    ┌─────────────────────────────────────────────┐
                    │         LEASE ACQUIRED (leased)             │
                    │                                             │
                    │  ┌──────────────┐                           │
                    │  │ initializing │──── startup_grace ────┐   │
                    │  └──────┬───────┘                       │   │
                    │         │ first output                  │   │
                    │         ▼                               ▼   │
                    │  ┌──────────┐  no output    ┌─────────┐    │
                    │  │ working  │──────────────→│ stalled │    │
                    │  └────┬─────┘  (threshold)  └────┬────┘    │
                    │       │ ▲                    output│ ▲      │
                    │       │ │ resume                   │ │      │
                    │       │ └─────────────────────────┘ │      │
                    │       │                              │      │
                    │       │ sub-task boundary            │      │
                    │       ▼                              │      │
                    │  ┌─────────────────────┐            │      │
                    │  │ idle_between_tasks  │─ grace ────┘      │
                    │  └─────────────────────┘                   │
                    │                                             │
                    │  ┌─────────┐  ┌─────────────────┐          │
                    │  │ blocked │  │ awaiting_input   │          │
                    │  └────┬────┘  └───────┬─────────┘          │
                    │       │ unblock       │ input received      │
                    │       └───────┐  ┌────┘                     │
                    │               ▼  ▼                          │
                    │           ┌──────────┐                      │
                    │           │ working  │                      │
                    │           └──────────┘                      │
                    └────────────────┬────────────────────────────┘
                                     │ process exit / forced termination
                    ┌────────────────┼────────────────────────────┐
                    │    ┌───────────┼──────────┐                 │
                    │    ▼           ▼          ▼                 │
                    │ exited_clean  exited_bad  resume_unsafe     │
                    │ (terminal)   (terminal)   (terminal)        │
                    └─────────────────────────────────────────────┘
                                     │
                              LEASE RELEASED
```

## Appendix B: Threshold Quick Reference

| Parameter | Default | Unit | Affects |
|-----------|---------|------|---------|
| `heartbeat_interval` | 30 | seconds | Heartbeat send frequency |
| `heartbeat_stale_threshold` | 90 | seconds | Warning: heartbeat may be late |
| `heartbeat_dead_threshold` | 300 | seconds | Escalation: assume process dead |
| `startup_grace_period` | 120 | seconds | Time before `initializing` → `stalled` |
| `stall_threshold` | 180 | seconds | Output silence before `working` → `stalled` |
| `idle_between_tasks_grace` | 120 | seconds | Silence before `idle_between_tasks` → `stalled` |
| `recovery_timeout` | 600 | seconds | Max time in `expired`/`recovering` before escalation |
| `interactive_stall_multiplier` | 1.5 | factor | Applied to `stall_threshold` for interactive workers |
