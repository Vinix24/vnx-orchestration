# Dispatch Lifecycle

How a dispatch flows through VNX from creation to intelligence injection back into T0.

## The seven stages

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ 1.CREATE │ → │ 2.PROMOTE│ → │ 3.DELIVER│ → │ 4.EXECUTE│
└──────────┘   └──────────┘   └──────────┘   └─────┬────┘
                                                    │
                                                    ▼
   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │ 7.INJECT │ ← │ 6.ARCHIVE│ ← │ 5.RECEIPT│
   └──────────┘   └──────────┘   └──────────┘
```

## Stage details

### 1. CREATE (T0 writes dispatch file)

T0 writes a JSON dispatch to `.vnx-data/dispatches/pending/<dispatch-id>/dispatch.json`. Contains: target terminal, role, priority, instructions, deps.

**Persists to:**
- `.vnx-data/dispatches/pending/<id>/dispatch.json` (filesystem)
- Nothing in RC yet

**Code:** T0 manager-block dispatch via `subprocess_dispatch.py` or staging promote.

### 2. PROMOTE (operator approval gate)

Operator (or autonomous T0 in approved mode) moves the dispatch from `pending/` to `active/`. Before this, the dispatch is just a draft.

**Persists to:**
- File moved to `.vnx-data/dispatches/active/<id>/`
- Row inserted into `rc.dispatches` (state='queued')
- `coordination_event` row: `event_type='dispatch_created'`

**Code:** `pr_queue_manager.py promote <id>` or auto-promote via dispatch_register.

### 3. DELIVER (dispatcher → terminal pane)

Dispatcher daemon picks up active dispatches, finds an idle terminal lease, delivers via subprocess (T1/T2/T3) or tmux send-keys (legacy).

**Persists to:**
- `terminal_leases` row updated: `state='leased', dispatch_id=<id>, leased_at=<now>`
- `dispatch_attempts` row inserted: `attempt_number=1, started_at=<now>`
- `rc.dispatches.state` → 'dispatched'
- `coordination_event`: `event_type='lease_acquired'`

**Code:** `scripts/lib/dispatcher.py`, `scripts/lib/subprocess_adapter.py`.

### 4. EXECUTE (worker runs)

Worker (Claude in subprocess pane or interactive tmux) executes the dispatch instructions. Streams events to `.vnx-data/events/T<n>.ndjson` (live ring buffer).

**Persists to:**
- `events/T<n>.ndjson` (live; truncated post-dispatch, archived to `events/archive/<terminal>/<dispatch_id>.ndjson`)
- `rc.dispatches.state` → 'executing'

**Code:** worker process (Claude); `subprocess_dispatch.py` shepherds.

### 5. RECEIPT (worker writes outcome)

Worker writes a structured receipt indicating: status (success/failure), what was done, evidence, follow-ups.

**Persists to:**
- `.vnx-data/state/t0_receipts.ndjson` (atomic append via `fcntl.flock`)
- `coordination_event`: `event_type='receipt_received'`

Receipt format spec: `docs/core/40_RECEIPT_CONTRACT.md`.

**Code:** `scripts/append_receipt.py`, called by worker.

### 6. ARCHIVE (T0 reviews receipt, releases lease)

T0 reads receipt, verifies, decides next action (close/retry/escalate). Lease released, dispatch moved to archive/.

**Persists to:**
- File moved to `.vnx-data/dispatches/archive/<id>/`
- `terminal_leases` row updated: `state='idle', dispatch_id=NULL, released_at=<now>`
- `rc.dispatches.state` → 'archived' (or 'failed', 'escalated')
- `dispatch_attempts.ended_at = <now>, outcome=...`
- `coordination_event`: `event_type='dispatch_closed'`

**Code:** T0 orchestration (this skill), `pr_queue_manager.py complete <id>`.

### 7. INJECT (intelligence flows back)

On next SessionStart, `build_t0_state.py` reads central QI + RC, filters by scope match, writes `t0_state.json` with intelligence block.

**Reads:**
- `qi.success_patterns` filtered by `project_id`, `valid_until IS NULL`, `confidence >= 0.7`
- `qi.antipatterns`, `qi.prevention_rules` similarly
- `rc.dispatches` recent + `rc.coordination_events` recent

**Persists to:**
- `.vnx-data/state/t0_state.json` (atomic write)
- `rc.intelligence_injections` row recording what was injected

T0 then reads `t0_state.json` automatically as the SessionStart context.

**Code:** `scripts/build_t0_state.py`, `scripts/lib/intelligence_injection.py`.

## Retention rules per stage

| Artifact | Retention |
|---|---|
| `dispatches/pending/` | until promoted or operator-pruned |
| `dispatches/active/` | until completed |
| `dispatches/archive/` | 90 days, then operator decision |
| `rc.dispatches` rows | indefinite (operator may prune for disk) |
| `events/T<n>.ndjson` | per-dispatch ring buffer (truncated post-dispatch) |
| `events/archive/<terminal>/...` | 30 days |
| `t0_receipts.ndjson` | append-only, indefinite |
| `qi.*` learned patterns | until `valid_until` set |
| `qi.code_snippets` | indefinite (operator prunes) |

## Common failure modes & where to look

| Symptom | Likely cause | Look at |
|---|---|---|
| "T0 doesn't have intelligence" | Injection wrote nothing | `rc.intelligence_injections` (empty?) → check scope match in `intelligence_injection.py` |
| "Receipt not archived" | T0 didn't process | `pr_queue_manager.py status`, check incident_log |
| "Dispatch stuck in active" | Lease held by dead worker | `terminal_leases` for stale lease (`expires_at < now`) → janitor process |
| "Events file empty" | Dispatch ended; check archive | `events/archive/<terminal>/<dispatch_id>.ndjson` |
| "Cross-project query returns nothing" | project_id filter mismatch | check `~/.vnx/projects.json` slug vs central rows' project_id |

## Project_id at each stage

The `project_id` is established at CREATE (read from registry by T0) and propagated through every subsequent stage. Stages 1-6 each write rows with `project_id` stamped from the registry-derived value. Stage 7 INJECT filters reads by `project_id` for scope match.

When migration consolidates per-project DBs into central (P4), stages 1-6 already happened locally with project_id stamping; the migration just relocates the rows. Source rows' project_id should be overwritten with the registry's canonical slug at import time (the round-5 fix made this robust against pre-existing values).

## Dispatch chain (parent → child)

For retries / fix-forwards: each new dispatch references its parent via `parent_dispatch`. Walk the chain:

```sql
WITH RECURSIVE chain AS (
    SELECT dispatch_id, parent_dispatch, 0 AS depth
    FROM dispatches WHERE dispatch_id = ?
    UNION ALL
    SELECT d.dispatch_id, d.parent_dispatch, c.depth + 1
    FROM dispatches d
    JOIN chain c ON d.parent_dispatch = c.dispatch_id
)
SELECT * FROM chain ORDER BY depth;
```

For cross-project chains: prefix-rewrite `parent_dispatch` to `<project_id>:<id>` so chains can span projects unambiguously.
