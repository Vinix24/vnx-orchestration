# runtime_coordination.db Schema Reference

Authoritative source: `schemas/runtime_coordination.sql` (v1 base) + delta migrations `schemas/runtime_coordination_v2.sql` through `schemas/runtime_coordination_v10.sql` (list the directory: `ls schemas/runtime_coordination_v*.sql`; a later file is picked up by the same loop). Apply v1 → v2 → … in order. Bootstrap with `scripts/lib/coordination_db.init_schema(state_dir)`; do not handcraft. The argument is the state directory that holds `runtime_coordination.db` (a `str` or `Path`), not a connection.

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

**State machine** (source: `DISPATCH_STATES` / `DISPATCH_TRANSITIONS` in `scripts/lib/coordination_db.py`):
```
proposed -> ready -> claimed -> delivering -> accepted -> running -> completed
queued   -> claimed                                  (queued: registered, not yet assigned)
delivering | running -> timed_out | failed_delivery
timed_out | failed_delivery -> recovered | expired | dead_letter
recovered -> queued | claimed | expired | dead_letter
```
`proposed` is the un-approved planning state, `ready` the operator-approved one. `queued` is registered and not yet assigned. `completed`, `expired` and `dead_letter` are terminal. A transition outside `DISPATCH_TRANSITIONS` raises `InvalidTransitionError`. There is no `dispatched`, `executing`, `received`, `archived`, `failed` or `escalated` dispatch state.

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

Lease states (`LEASE_STATES` in `scripts/lib/coordination_db.py`): `idle`, `leased`, `expired`, `recovering`, `released`. Transitions: `idle → leased`, `leased → released | expired`, `expired → recovering | idle`, `recovering → idle | leased`, `released → idle`. A terminal acquires its lease before executing and releases on completion. A lease whose `expires_at` has passed becomes `expired` and the reconciler recovers it.

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

## Schema versions (v1 → v10, then v11 in code)

Files in `schemas/`, applied in order by `init_schema` (the loop stops at the first missing `runtime_coordination_v<N>.sql`):

- **v1** (`runtime_coordination.sql`): `runtime_schema_version`, `dispatches`, `dispatch_attempts`, `terminal_leases`, `coordination_events`
- **v2**: `incident_log`, `retry_budgets`
- **v3**: `retry_state`, `escalation_log`
- **v4**: `execution_targets`, `intelligence_injections`, `inbound_inbox`, `recommendations`, `recommendation_outcomes`, plus routing columns on `dispatches` (`task_class`, `target_type`, `target_id`, `channel_origin`, `intelligence_payload`)
- **v5**: `escalation_state`, `governance_overrides`
- **v6**: `provenance_registry` (receipt, commit and PR linkage per dispatch)
- **v7**: `provenance_verifications`
- **v8**: `headless_runs` (headless run identity, heartbeat, lifecycle state)
- **v9**: `worker_states` (one row per terminal)
- **v10**: baseline for multi-tenant lease isolation: composite UNIQUE on `terminal_leases` and `dispatches`, `project_id` on `worker_states`. Its `project_id` indexes are created by migrations 0010 and 0017, which run after `init_schema`
- **v11** (no SQL file): `_migrate_v11_composite_keys` in `coordination_db.py` creates the ADR-007 `ux_<table>_pid` unique indexes over `project_id`, only when `project_id` is already present. Check which are missing with `composite_index_audit()`

`init_schema` records progress in `PRAGMA user_version` (v1 = 1, each file +1). Call it with the state directory: `coordination_db.init_schema(state_dir)`. **Don't run only a single delta file**: v2 to v10 assume the earlier tables exist, and you will miss the rest.

## Migration 0010 / 0015 layered on top

`schemas/migrations/0010_add_project_id.sql` adds `project_id TEXT NOT NULL DEFAULT 'vnx-dev'` to: dispatches, dispatch_attempts, terminal_leases, coordination_events, incident_log, intelligence_injections.

`schemas/migrations/0015_complete_project_id.sql` extends to: retry_budgets, retry_state, escalation_log, execution_targets, inbound_inbox, recommendations, recommendation_outcomes (and many QI tables).

**Composite keys:** v10 (`terminal_leases`, `dispatches`) and the v11 `ux_<table>_pid` indexes put `project_id` into the uniqueness of the tables in `_RC_COMPOSITE_KEYS`. The base v1 file still declares `dispatch_id` and `terminal_id` as single-column UNIQUE.

## Common gotchas

- **Every `runtime_coordination_v<N>.sql` is a delta, not a full schema**: running one alone produces an incomplete DB. Use `coordination_db.init_schema(state_dir)` always.
- **`dispatch_id` is unique within a project, NOT globally.** The composite UNIQUE on `(project_id, dispatch_id)` was added in P4 round-5; older code that assumes globally-unique dispatch_id will break for cross-project central queries.
- **`coordination_events.entity_id`** can reference dispatch_id, pattern_id, etc. For cross-project queries, use prefix-rewrite (`<project_id>:<entity_id>`) when entity_type matches a project-scoped table.
- **`dispatches.parent_dispatch`** is also project-scoped. Walking the chain across projects requires prefix-rewriting.
