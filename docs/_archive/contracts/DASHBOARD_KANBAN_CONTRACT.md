# Dashboard Kanban Contract

**Feature**: Feature 23 — Dashboard Data Pipeline Fix And Kanban Board
**Contract-ID**: dashboard-kanban-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines the kanban board surface (S6), health endpoint, dispatch-to-stage
mapping, and error/degraded state handling for the VNX operator dashboard.

---

## 2. Kanban Surface (S6)

### 2.1 Columns

The kanban board displays dispatches across 5 stage columns:

| Column | Stage | Description |
|--------|-------|-------------|
| **Staging** | `staging` | Dispatch created in staging area, not yet promoted |
| **Pending** | `pending` | Promoted and queued, waiting for terminal assignment |
| **Active** | `active` | Claimed by terminal, executing |
| **Review** | `review` | Execution complete, awaiting gate results or T0 review |
| **Done** | `done` | Terminal state reached (completed, expired, dead_letter) |

### 2.2 Dispatch Card Data Shape

Each card on the kanban board carries:

```json
{
  "dispatch_id": "20260403-181010-dashboard-kanban-contract-C",
  "pr_id": "PR-0",
  "track": "C",
  "terminal_id": "T3",
  "skill": "architect",
  "gate": "gate_pr0_dashboard_kanban_contract",
  "stage": "active",
  "dispatch_state": "running",
  "duration_seconds": 1234,
  "started_at": "ISO8601",
  "updated_at": "ISO8601",
  "status_label": "running",
  "attention": null,
  "error": null
}
```

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `dispatch_id` | str | dispatch broker | Unique dispatch identity |
| `pr_id` | str | dispatch bundle | PR reference (PR-0, PR-1, ...) |
| `track` | str | dispatch bundle | Track assignment (A, B, C) |
| `terminal_id` | str? | lease manager | Assigned terminal (null if pending) |
| `skill` | str? | dispatch bundle | Skill assigned to this dispatch |
| `gate` | str? | dispatch bundle | Quality gate identifier |
| `stage` | str | derived (Section 3) | Kanban stage (staging, pending, active, review, done) |
| `dispatch_state` | str | runtime_coordination.db | Raw dispatch state from state machine |
| `duration_seconds` | float? | computed | Time since dispatch creation or execution start |
| `started_at` | str? | dispatch record | When dispatch was created |
| `updated_at` | str? | dispatch record | Last state transition timestamp |
| `status_label` | str | derived | Human-readable status label |
| `attention` | str? | canonical_state_views | Attention flag (blocked, stale, review-needed) |
| `error` | str? | failure details | Error summary if in failure state |

---

## 3. Dispatch-To-Stage Mapping

### 3.1 Mapping Rules

Dispatch states map to kanban stages deterministically:

| Dispatch State | Kanban Stage | Status Label |
|---------------|-------------|--------------|
| (staging area) | `staging` | "staging" |
| `queued` | `pending` | "queued" |
| `claimed` | `active` | "claimed" |
| `delivering` | `active` | "delivering" |
| `accepted` | `active` | "accepted" |
| `running` | `active` | "running" |
| `timed_out` | `review` | "timed out" |
| `failed_delivery` | `review` | "delivery failed" |
| `recovered` | `review` | "recovered" |
| `completed` | `done` | "completed" |
| `expired` | `done` | "expired" |
| `dead_letter` | `done` | "dead letter" |

### 3.2 Staging Detection

Dispatches in the staging area (not yet promoted) are detected by:
- Present in `$VNX_DATA_DIR/dispatches/staging/` but not in `dispatches` table
- OR present in `dispatches` table with no `queued` transition yet

### 3.3 Mapping Invariants

- **KM-1**: Every dispatch maps to exactly one kanban stage. No dispatch may appear in two columns.
- **KM-2**: The mapping is pure — derived from dispatch state only, no side effects.
- **KM-3**: Unknown dispatch states map to `review` stage with `status_label: "unknown"` and `attention: "review-needed"`.
- **KM-4**: Terminal states (`completed`, `expired`, `dead_letter`) always map to `done`. No exceptions.

---

## 4. Health Endpoint

### 4.1 Endpoint Specification

```
GET /api/health
```

### 4.2 Response Format

```json
{
  "status": "healthy|degraded|unhealthy",
  "timestamp": "ISO8601",
  "checks": {
    "database": { "ok": true, "detail": "runtime_coordination.db accessible" },
    "queue_state": { "ok": true, "detail": "PR_QUEUE.md readable" },
    "receipt_pipeline": { "ok": true, "detail": "t0_receipts.ndjson writable" },
    "lease_manager": { "ok": true, "detail": "terminal_state.json fresh" }
  },
  "degraded_checks": [],
  "failed_checks": []
}
```

### 4.3 Status Derivation

| Condition | Status |
|-----------|--------|
| All checks `ok: true` | `healthy` |
| 1+ checks `ok: false` but database `ok: true` | `degraded` |
| Database `ok: false` | `unhealthy` |

### 4.4 Health Check Details

| Check | What It Verifies | Timeout |
|-------|-----------------|---------|
| `database` | `runtime_coordination.db` exists and readable via `get_connection()` | 2s |
| `queue_state` | `PR_QUEUE.md` exists and parseable | 1s |
| `receipt_pipeline` | `t0_receipts.ndjson` path writable | 1s |
| `lease_manager` | `terminal_state.json` exists and freshness < 5 minutes | 1s |

### 4.5 Frontend Usage

The frontend polls `/api/health` every 30 seconds. When status is:
- `healthy`: Normal rendering
- `degraded`: Yellow banner with degraded check names
- `unhealthy`: Red banner, all data surfaces show stale warning

---

## 5. Error And Degraded State Rendering

### 5.1 Rendering Modes

Every dashboard surface (S1-S6) must handle three rendering modes:

| Mode | Condition | Rendering |
|------|-----------|-----------|
| **Normal** | Health `healthy`, data fresh | Standard display |
| **Degraded** | Health `degraded` OR data stale (> 5 min) | Yellow banner, data shown with staleness indicator |
| **Error** | Health `unhealthy` OR data unavailable | Red banner, last-known data with "stale" label, or empty state with "no data" message |

### 5.2 Kanban-Specific Error States

| Error | Rendering |
|-------|-----------|
| Database unreachable | All columns show "Data unavailable" placeholder |
| Queue state unparseable | Staging column shows "Queue error" badge |
| Lease state stale | Active column cards show "state uncertain" indicator |
| No dispatches found | All columns empty with "No dispatches" message |

### 5.3 Card-Level Status Indicators

| Indicator | Condition | Visual |
|-----------|-----------|--------|
| Running normally | `stage=active`, no attention | Green dot |
| Needs review | `stage=review` | Orange dot |
| Attention required | `attention` field set | Pulsing indicator + attention reason |
| Error state | `error` field set | Red dot + error summary tooltip |
| Duration warning | `duration_seconds` > threshold (configurable, default 1 hour) | Clock icon |

---

## 6. Existing Surfaces (S1-S5)

For reference, the kanban board (S6) joins these existing dashboard surfaces:

| Surface | Description | Data Source |
|---------|-------------|-------------|
| S1 | Terminal status grid | terminal_state.json |
| S2 | Queue projection | PR_QUEUE.md parse |
| S3 | Open items list | open_items.json |
| S4 | Recent receipts | t0_receipts.ndjson |
| S5 | Context usage | context_window_*.json |
| **S6** | **Kanban board** | **runtime_coordination.db + staging dir + queue state** |

---

## 7. Testing Contract

### 7.1 Mapping Tests

1. Every dispatch state maps to exactly one kanban stage (KM-1)
2. Terminal states always map to `done` (KM-4)
3. Unknown states map to `review` with attention (KM-3)
4. Staging detection works for pre-promotion dispatches
5. Mapping is pure (no side effects, KM-2)

### 7.2 Health Endpoint Tests

1. All checks passing returns `healthy`
2. One non-database check failing returns `degraded`
3. Database check failing returns `unhealthy`
4. Response includes all 4 check names
5. Endpoint responds within 5 seconds total

### 7.3 Rendering Tests

1. Normal mode renders all cards correctly
2. Degraded mode shows yellow banner
3. Error mode shows red banner with stale label
4. Card-level indicators match status conditions
5. Duration warning triggers at threshold

---

## 8. Migration Path

### Phase 1: Contract Lock (This PR)
### Phase 2: Health Endpoint And Data Pipeline Fix (PR-1)
### Phase 3: Kanban Read Model And Stage Mapping (PR-2)
### Phase 4: Frontend Kanban Board Component (PR-3)
### Phase 5: Certification (PR-4)

---

## 9. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should the kanban show all-time or recent dispatches? | Recent: last 50 dispatches or last 7 days, whichever is smaller. Configurable. |
| Should staging dispatches come from the DB or filesystem? | Filesystem (`dispatches/staging/`) — staging dispatches are pre-DB. |
| Should the health endpoint be authenticated? | No. It's a liveness check. No sensitive data exposed. |
| Should done cards auto-hide after a period? | Yes. Done cards older than 24 hours fade to 50% opacity. Configurable. |
