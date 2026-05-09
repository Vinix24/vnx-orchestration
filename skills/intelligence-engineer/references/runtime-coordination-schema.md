# runtime_coordination.db Schema Reference

Authoritative source: `schemas/runtime_coordination.sql` (v1 base) + delta migrations `runtime_coordination_v2.sql` through `runtime_coordination_v9.sql`. Apply v1 → v2 → … → v9 in order. Use `scripts/lib/coordination_db.init_schema()` to bootstrap; do not handcraft.

## Core tables

### dispatches

The primary record of every dispatch ever created.

```sql
CREATE TABLE dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT NOT NULL,        -- canonical id, slug-style YYYYMMDD-…
    state           TEXT NOT NULL DEFAULT 'queued',
    terminal_id     TEXT,                  -- T0/T1/T2/T3 when leased
    track           TEXT,                  -- A/B/C
    priority        TEXT DEFAULT 'P2',     -- P0/P1/P2/P3
    pr_ref          TEXT,                  -- PR number when closed
    parent_dispatch TEXT,                  -- for retries / fix-forwards
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT,
    closed_at       TEXT,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev',  -- ADDED migration 0010
    -- ... more columns ...
    UNIQUE (project_id, dispatch_id)        -- composite, added round-5 of P4
);
```

**State machine:**
```
queued → dispatched → executing → received → archived
                ↓                       ↓
              failed                 escalated
```

### dispatch_attempts

Retry attempts. One row per attempt, including the first.

```sql
CREATE TABLE dispatch_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    outcome         TEXT,                  -- 'success', 'fail', 'timeout'
    error_summary   TEXT,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

### terminal_leases

Which terminal currently owns which dispatch.

```sql
CREATE TABLE terminal_leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id     TEXT NOT NULL,         -- T0/T1/T2/T3
    state           TEXT NOT NULL DEFAULT 'idle',
    dispatch_id     TEXT,                  -- NULL when idle
    generation      INTEGER NOT NULL DEFAULT 1,
    leased_at       TEXT,
    expires_at      TEXT,                  -- lease TTL
    last_heartbeat_at TEXT,
    released_at     TEXT,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev',
    UNIQUE (project_id, terminal_id)        -- composite, P4 round-5 fix
);
```

Lease semantics: terminal acquires lease before executing; releases on receipt. Stale leases (`expires_at < now()`) get cleaned up by janitor process.

### coordination_events

Append-only event log. Every state transition creates a row.

```sql
CREATE TABLE coordination_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,         -- 'dispatch_created', 'lease_acquired', 'receipt_received', etc.
    entity_type     TEXT NOT NULL,         -- 'dispatch', 'pattern', 'terminal'
    entity_id       TEXT NOT NULL,
    payload         TEXT,                  -- JSON blob with event details
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev'
);
CREATE INDEX idx_coord_events_entity ON coordination_events(project_id, entity_type, entity_id);
```

Used for: audit trail, post-mortem analysis, replay logic.

### incident_log

Flagged incidents (cooldowns, errors, escalations).

```sql
CREATE TABLE incident_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    severity        TEXT NOT NULL,         -- 'blocking', 'warn', 'info'
    incident_type   TEXT NOT NULL,
    description     TEXT,
    related_dispatch_id TEXT,
    flagged_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT,                  -- NULL if still open
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

Used for: T0 visibility into open incidents, escalation triggers.

### intelligence_injections

Record of which intelligence was injected into which session.

```sql
CREATE TABLE intelligence_injections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    pattern_id      TEXT,                  -- → quality_intelligence.success_patterns.pattern_id
    dispatch_id     TEXT,                  -- which dispatch triggered the injection
    scope_match     TEXT,                  -- JSON: how the scope matched
    injected_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

Lets you analyze: "which patterns get injected most? which never do? what's the recall vs precision of injection?"

## Retry / escalation tables

### retry_budgets

Per-dispatch retry budget (e.g. "max 3 retries on chunk_timeout").

```sql
CREATE TABLE retry_budgets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT NOT NULL,
    failure_class   TEXT NOT NULL,         -- 'chunk_timeout', 'tool_use_error', etc.
    budget_remaining INTEGER NOT NULL,
    last_consumed_at TEXT,
    project_id      TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

### retry_state

Current retry state per dispatch.

### escalation_log

Records when a dispatch was escalated (operator wake, manual intervention, etc).

## Schema versions (v1 → v9)

- **v1** (`runtime_coordination.sql`): base schema with dispatches, dispatch_attempts, terminal_leases, coordination_events, incident_log
- **v2**: added intelligence_injections, retry_budgets
- **v3**: added retry_state, escalation_log
- **v4**: added execution_targets, inbound_inbox, recommendations, recommendation_outcomes
- **v5**: minor — added indexes for performance
- **v6**: added worker_states (collateral cleanup in P4 round-5)
- **v7**: added headless_runs (Wave-2 headless review work)
- **v8**: minor — corrections to v7 indexing
- **v9**: latest delta — exact contents in `schemas/runtime_coordination_v9.sql`

For fresh init: `coordination_db.init_schema(con)` applies v1 base + v2-v9 deltas in order. **Don't run only the v1 SQL** — you'll miss the rest.

## Migration 0010 / 0015 layered on top

`schemas/migrations/0010_add_project_id.sql` adds `project_id TEXT NOT NULL DEFAULT 'vnx-dev'` to: dispatches, dispatch_attempts, terminal_leases, coordination_events, incident_log, intelligence_injections.

`schemas/migrations/0015_complete_project_id.sql` extends to: retry_budgets, retry_state, escalation_log, execution_targets, inbound_inbox, recommendations, recommendation_outcomes (and many QI tables).

**P4 round-5 also added** composite UNIQUE constraints replacing single-column ones for: terminal_leases, execution_targets, dispatches.

## Common gotchas

- **`v8` is a delta, not a full schema** — running only `runtime_coordination_v8.sql` produces an incomplete DB. Use `coordination_db.init_schema()` always.
- **`dispatch_id` is unique within a project, NOT globally.** The composite UNIQUE on `(project_id, dispatch_id)` was added in P4 round-5; older code that assumes globally-unique dispatch_id will break for cross-project central queries.
- **`coordination_events.entity_id`** can reference dispatch_id, pattern_id, etc. For cross-project queries, use prefix-rewrite (`<project_id>:<entity_id>`) when entity_type matches a project-scoped table.
- **`dispatches.parent_dispatch`** is also project-scoped. Walking the chain across projects requires prefix-rewriting.
