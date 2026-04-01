# Dashboard Read Model And Operator Surface Contract

**Status**: Canonical
**Feature**: Coding Operator Dashboard And Session Control (Feature 13)
**PR**: PR-0
**Gate**: `gate_pr0_dashboard_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for the dashboard's data contract, operator questions, safe actions, read-model boundaries, and degraded-state handling. All downstream PRs (PR-1 through PR-4) implement against this contract.

Related contracts:
- [130_RUNTIME_STATE_MACHINE_CONTRACT](130_RUNTIME_STATE_MACHINE_CONTRACT.md) — worker states, heartbeat, stall detection
- [120_PROJECTION_CONSISTENCY_CONTRACT](120_PROJECTION_CONSISTENCY_CONTRACT.md) — canonical vs projected surface truth
- [80_TERMINAL_EXCLUSIVITY_CONTRACT](80_TERMINAL_EXCLUSIVITY_CONTRACT.md) — dispatch safety and lease exclusivity

---

## 1. Why This Exists

### 1.1 The Problem

The coding operator currently relies on:
- Manual tmux navigation to discover which terminals are active
- Direct file inspection (`open_items.json`, `terminal_state.json`, logs) to understand session state
- Ad hoc script invocation to reconcile or inspect runtime health
- Mental aggregation of open items across projects with no unified view

A dashboard built directly on these raw surfaces would become a **cosmetic proxy** — rendering stale files with no freshness guarantees, coupling UI rendering to script output formats, and masking rather than surfacing degraded state.

### 1.2 The Fix

Interpose a **read-model layer** between the raw runtime surfaces and the dashboard UI. The read model:
- Composes from canonical sources with explicit freshness tracking
- Answers specific operator questions with structured responses
- Handles empty, stale, and degraded states as first-class rendering conditions
- Forbids the UI from reaching past the read model to parse raw files or invoke scripts directly

---

## 2. Dashboard Surfaces

### 2.1 First-Release Surface Inventory

The dashboard ships with exactly 5 surfaces. Each surface answers a defined set of operator questions and renders from a specific read-model view.

| # | Surface | Scope | Primary Operator Need |
|---|---------|-------|-----------------------|
| S1 | **Projects Overview** | Cross-project | Which projects exist, which have active sessions, which need attention |
| S2 | **Session Detail** | Per-project | What is the current session doing, which terminals are active, what is the feature/PR progress |
| S3 | **Terminal Status** | Per-terminal | What is this specific terminal doing, is it healthy, what dispatch is it running |
| S4 | **Open Items (Per-Project)** | Per-project | What blockers, warnings, and info items exist for this project |
| S5 | **Open Items (Aggregate)** | Cross-project | What are the most urgent items across all projects, ordered by severity |

### 2.2 Surface S1: Projects Overview

**Operator Questions This Surface Must Answer:**

| # | Question | Answer Source |
|---|----------|--------------|
| Q1.1 | Which projects are registered? | Project registry (new, §3.1) |
| Q1.2 | Which projects have an active VNX session? | Session profile existence + tmux session liveness |
| Q1.3 | Which projects need operator attention? | Attention model (open blocker count + stale terminal count) |
| Q1.4 | What is the active feature for each project? | `pr_queue_state.json` → `feature` field |
| Q1.5 | How many open items per project? | Open items projection (§3.3) |

**Rendering States:**

| State | Condition | Display |
|-------|-----------|---------|
| **Active** | Session exists, ≥1 terminal leased or working | Green indicator, terminal count, feature name |
| **Idle** | Session exists, all terminals idle | Neutral indicator, "idle" label |
| **No Session** | No `session_profile.json` or tmux session dead | Gray indicator, "start session" action |
| **Attention** | ≥1 open blocker or ≥1 terminal stalled/dead | Amber/red indicator, blocker count |

### 2.3 Surface S2: Session Detail

**Operator Questions:**

| # | Question | Answer Source |
|---|----------|--------------|
| Q2.1 | What feature is this session working on? | `pr_queue_state.json` → `feature` |
| Q2.2 | What is the PR progress? | `pr_queue_state.json` → `prs[]` with status |
| Q2.3 | Which terminals are active and what are they doing? | Terminal read model (§3.2) |
| Q2.4 | Which track is blocked or stalled? | `progress_state.yaml` + worker state |
| Q2.5 | What open items exist for this project? | Open items projection filtered by project |
| Q2.6 | When was this session last active? | Most recent `last_activity` across terminals |

**Rendering States:**

| State | Condition | Display |
|-------|-----------|---------|
| **Working** | ≥1 terminal in `working` state | Active indicator, terminal breakdown |
| **Blocked** | ≥1 terminal in `blocked` or `awaiting_input` | Blocked indicator with reason |
| **Stalled** | ≥1 terminal in `stalled` state | Warning indicator, stall duration |
| **Idle** | All terminals idle, no active dispatch | Neutral, "all terminals idle" |
| **Degraded** | Read model data is stale (§5) | Degraded badge, last-known state with age |

### 2.4 Surface S3: Terminal Status

**Operator Questions:**

| # | Question | Answer Source |
|---|----------|--------------|
| Q3.1 | What is this terminal's worker state? | `worker_states` table via read model |
| Q3.2 | What dispatch is it running? | `terminal_leases.dispatch_id` → dispatch detail |
| Q3.3 | Is the heartbeat fresh, stale, or dead? | Heartbeat classification from worker state manager |
| Q3.4 | How long since last output? | `worker_states.last_output_at` age |
| Q3.5 | What track and role does this terminal serve? | Session profile terminal metadata |
| Q3.6 | Is there context window pressure? | `context_window_T{n}.json` → `remaining_pct` |
| Q3.7 | Are there anomalies on this terminal? | Runtime supervisor anomaly check |

**Rendering States:**

| State | Condition | Display |
|-------|-----------|---------|
| **Working** | Worker state `working`, heartbeat fresh | Green, dispatch info, output recency |
| **Initializing** | Worker state `initializing` | Blue, "starting up", grace timer |
| **Idle** | Lease idle, no worker state | Gray, "available" |
| **Stalled** | Worker state `stalled` | Amber, stall duration, last output age |
| **Blocked** | Worker state `blocked` | Red, blocked reason |
| **Awaiting Input** | Worker state `awaiting_input` | Orange, "needs operator" |
| **Exited (Clean)** | Worker state `exited_clean` | Green check, completion time |
| **Exited (Bad)** | Worker state `exited_bad` | Red X, exit details |
| **Dead** | Heartbeat dead, lease still held | Red alert, "process dead" |
| **Context Pressure** | `remaining_pct < 25` | Warning badge overlay on any state |

### 2.5 Surface S4: Open Items (Per-Project)

**Operator Questions:**

| # | Question | Answer Source |
|---|----------|--------------|
| Q4.1 | How many open blockers? | `open_items.json` filtered by project, severity=blocker |
| Q4.2 | What are the blocker details? | Open item title, origin dispatch, age |
| Q4.3 | Which PR created each item? | `pr_id` field on open item |
| Q4.4 | Is an item auto-created or manual? | `auto_created` field (Feature 12 runtime anomalies) |
| Q4.5 | What items were recently resolved? | Items with `closed_at` within last 24h |

**Rendering States:**

| State | Condition | Display |
|-------|-----------|---------|
| **Clean** | 0 open blockers, 0 open warnings | Green "all clear" |
| **Warnings** | 0 blockers, ≥1 warnings | Amber warning count |
| **Blocked** | ≥1 open blockers | Red blocker count, blocker list |
| **Empty** | No open items at all (new project) | "No items yet" placeholder |

### 2.6 Surface S5: Open Items (Aggregate)

**Operator Questions:**

| # | Question | Answer Source |
|---|----------|--------------|
| Q5.1 | What is the total open blocker count across all projects? | Sum of per-project blocker counts |
| Q5.2 | Which project has the most blockers? | Sorted by blocker count descending |
| Q5.3 | Are any blockers from runtime anomalies? | Filter by `type=runtime_anomaly` |
| Q5.4 | What is the oldest unresolved blocker? | Sort by `created_at` ascending |
| Q5.5 | What is the cross-project resolution velocity? | Closed items per day over last 7 days |

**Rendering**: Same states as S4 but aggregated across projects.

---

## 3. Read-Model Architecture

### 3.1 Read-Model Views

The read model consists of structured views that the dashboard queries. Each view composes from canonical sources, tracks its own freshness, and never requires the dashboard to parse raw files.

| View | Composes From | Output | Freshness Source |
|------|-------------|--------|-----------------|
| `ProjectsView` | Project registry + session profiles + attention model | List of projects with status and attention | Registry scan + session profile mtime |
| `SessionView` | `pr_queue_state.json` + `progress_state.yaml` + terminal read model | Feature progress, track status, terminal summary | Youngest source mtime |
| `TerminalView` | `runtime_coordination.db` (leases + worker_states) + `context_window_*.json` | Per-terminal health with heartbeat and output recency | DB query timestamp |
| `OpenItemsView` | `open_items.json` | Filtered, sorted open items with age calculation | File mtime |
| `AggregateOpenItemsView` | All project `open_items.json` files | Cross-project blocker summary | Oldest source mtime |

### 3.2 Project Registry

The dashboard requires a project registry to know which projects exist. This is new — the current system discovers the project from `bin/vnx` location.

**Registry Format** (`~/.vnx/projects.json`):

```json
{
  "schema_version": 1,
  "projects": [
    {
      "name": "vnx-orchestration",
      "path": "/Users/operator/Development/vnx-roadmap-autopilot-wt",
      "vnx_data_dir": ".vnx-data",
      "registered_at": "2026-04-01T12:00:00Z",
      "active": true
    }
  ]
}
```

**Registration invariants:**

- **PR-1**: A project is registered when `vnx start` is first run in a directory (auto-registration).
- **PR-2**: Manual registration via `vnx project add <path>` for projects not yet started.
- **PR-3**: The projects overview (S1) reads only from this registry — it does not scan the filesystem.
- **PR-4**: A project with `active: false` is hidden from the dashboard but not deleted.

### 3.3 Canonical Source Mapping

Every read-model field traces back to exactly one canonical source. The dashboard never reads from multiple sources for the same fact.

| Fact | Canonical Source | Read-Model View | Forbidden Alternative |
|------|-----------------|-----------------|----------------------|
| Terminal lease state | `terminal_leases` table | TerminalView | ~~terminal_state.json~~ (projection, may lag) |
| Worker execution state | `worker_states` table | TerminalView | ~~tmux pane output~~ |
| Dispatch progress | `dispatches` table | SessionView | ~~dispatch filesystem scan~~ |
| PR queue status | `pr_queue_state.json` | SessionView | ~~FEATURE_PLAN.md parsing~~ |
| Track gate progress | `progress_state.yaml` | SessionView | ~~receipt log scanning~~ |
| Open items | `open_items.json` | OpenItemsView | ~~open_items.md~~ (rendered, not structured) |
| Session layout | `session_profile.json` | ProjectsView | ~~tmux list-panes~~ |
| Context pressure | `context_window_T{n}.json` | TerminalView | ~~conversation-index.db~~ |
| Heartbeat freshness | `terminal_leases.last_heartbeat_at` | TerminalView | ~~process table scan~~ |

### 3.4 Freshness Tracking

Every read-model response includes a freshness envelope:

```json
{
  "view": "TerminalView",
  "terminal_id": "T1",
  "queried_at": "2026-04-01T20:00:00Z",
  "source_freshness": {
    "runtime_coordination.db": "2026-04-01T19:59:58Z",
    "context_window_T1.json": "2026-04-01T19:58:30Z"
  },
  "staleness_seconds": 2,
  "degraded": false,
  "data": { ... }
}
```

**Freshness classification:**

| Classification | Condition | Dashboard Behavior |
|---------------|-----------|-------------------|
| **Fresh** | All sources < 60s old | Render normally |
| **Aging** | Any source 60–300s old | Render with age badge |
| **Stale** | Any source > 300s old | Render with degraded overlay, show last-known age |
| **Unavailable** | Source file missing or DB inaccessible | Show explicit "unavailable" state, not empty |

---

## 4. Safe Actions

### 4.1 Action Safety Model

The dashboard exposes a limited set of **safe actions** in the first release. An action is safe if:

1. It cannot corrupt runtime state
2. It has an explicit success/failure outcome
3. It does not bypass governance (dispatch system, lease exclusivity)
4. It is idempotent or clearly communicates non-idempotency

### 4.2 First-Release Safe Actions

| # | Action | Surface | Implementation | Safety Classification |
|---|--------|---------|---------------|----------------------|
| A1 | **Start Session** | S1 (Projects) | Invoke `vnx start` for the selected project | Safe — creates tmux session, initializes state files. Idempotent if session exists. |
| A2 | **Attach Terminal** | S3 (Terminal) | Open the operator's terminal emulator to the tmux pane for the selected terminal | Safe — read-only intent, no state change. Delegates to tmux attach. |
| A3 | **Refresh Projections** | S2 (Session) | Re-project `terminal_state.json` and `pr_queue_state.json` from canonical sources | Safe — read-only from DB, write to projection files. Idempotent. |
| A4 | **Run Reconciliation** | S2 (Session) | Invoke the runtime state reconciler to detect and report mismatches | Safe — read-only detection, writes audit records. Does not change state. |
| A5 | **Inspect Open Item** | S4, S5 (Open Items) | Navigate to open item detail with origin dispatch and evidence | Safe — pure read, no state change. |
| A6 | **Stop Session** | S1 (Projects) | Invoke `vnx stop` for the selected project | Safe with confirmation — kills tmux session, releases leases. Non-idempotent. |

### 4.3 Forbidden Actions (First Release)

These actions are explicitly out of scope for the first dashboard release:

| Action | Why Forbidden | When Allowed |
|--------|--------------|-------------|
| Create dispatch from dashboard | Dispatch creation requires T0 governance context | Future: when dashboard can feed T0 orchestrator |
| Kill individual worker process | Requires process management, risk of orphaned state | Future: with structured kill + lease cleanup |
| Resolve open item from dashboard | Resolution requires evidence and dispatch linkage | Future: when resolution workflow is defined |
| Edit project configuration | Config changes affect runtime behavior | Future: with validation and preview |
| Force-expire lease | Can orphan active workers | Never from UI — reconciler only |

### 4.4 Action Outcome Model

Every action returns a structured outcome:

```json
{
  "action": "start_session",
  "project": "vnx-orchestration",
  "status": "success" | "failed" | "already_active" | "degraded",
  "message": "Session started with 4 terminals",
  "details": { ... },
  "timestamp": "2026-04-01T20:00:00Z"
}
```

**Outcome invariants:**

- **AO-1**: Every action produces exactly one outcome. No silent failures.
- **AO-2**: `failed` outcomes include a human-readable `message` and a machine-readable `error_code`.
- **AO-3**: `already_active` is a valid success variant, not an error — actions must be graceful when the target state already exists.
- **AO-4**: `degraded` means the action partially succeeded but the result cannot be fully verified (e.g., session started but one terminal failed to initialize).

---

## 5. Degraded-State And Empty-State Policy

### 5.1 Degraded State

A surface is degraded when its read-model sources are stale, missing, or contradictory. The dashboard must never render degraded state as healthy.

| Degradation Type | Condition | Dashboard Response |
|-----------------|-----------|-------------------|
| **Source stale** | Read-model source mtime > 300s ago | Render last-known data with "stale since {age}" overlay |
| **Source missing** | Expected file does not exist | Render "unavailable" state, not empty. Offer "refresh" action. |
| **Source contradictory** | Projection reconciler detects mismatch | Render both values with "mismatch detected" indicator. Do not pick one silently. |
| **DB inaccessible** | `runtime_coordination.db` locked or corrupt | Render "database unavailable" for all DB-backed views. Fall back to JSON projections with stale badge. |
| **Partial data** | Some terminals have data, others do not | Render available terminals normally, missing ones as "no data". Never hide missing terminals. |

### 5.2 Degraded-State Invariants

- **DS-1**: The dashboard never silently drops a terminal, project, or open item because its data is unavailable. Missing data is rendered as explicitly missing.
- **DS-2**: Stale data is always labeled with its age. The label format is `"last updated {N}m ago"` for minutes, `"{N}h ago"` for hours.
- **DS-3**: When the read model cannot determine a terminal's state, it renders `"unknown"` — never `"idle"`. Unknown and idle are visually distinct.
- **DS-4**: A degraded surface still allows safe actions. The action outcome reports whether it operated on stale data.
- **DS-5**: The dashboard refreshes read-model views on a configurable interval (default: 10s for terminal state, 30s for open items, 60s for project list).

### 5.3 Empty State

A surface is empty when it has no data because the project is new, no session has run, or no items exist. Empty is not degraded — it is a valid initial state.

| Surface | Empty Condition | Display |
|---------|----------------|---------|
| S1 (Projects) | No projects registered | "No projects registered. Run `vnx start` in a project directory." |
| S2 (Session) | Project exists but no session has run | "No active session. Start one from the projects overview." |
| S3 (Terminal) | Terminal exists in profile but no worker state | "Terminal idle. No active dispatch." |
| S4 (Open Items) | No open items for this project | "No open items. All clear." |
| S5 (Aggregate) | No open items across any project | "No open items across any project." |

---

## 6. Forbidden Data Paths

### 6.1 The Rule

The dashboard UI layer must never:

1. **Parse raw files directly** — all data access goes through read-model views
2. **Invoke shell scripts for rendering data** — scripts are for actions, not reads
3. **Query tmux state** — terminal identity comes from session profile and runtime DB
4. **Read `.claude/` internal files** — conversation state is private to the AI session
5. **Scan the filesystem for project discovery** — projects come from the registry

### 6.2 Why This Matters

If the UI can reach past the read model:
- It will couple to file formats that change across VNX versions
- It will render stale files without knowing they are stale
- It will break when the canonical source moves (e.g., SQLite replaces JSON)
- It will mask degraded state by rendering whatever file it can find
- It cannot be tested without the full runtime environment

### 6.3 Enforcement

| Layer | Responsibility |
|-------|---------------|
| **Read-model API** | Expose structured views with freshness. This is the only data interface the UI may call. |
| **UI components** | Import from read-model API only. No `fs.readFile`, no `child_process.exec`, no SQLite direct access. |
| **Code review** | PR-3 (UI) review must verify that no UI component imports raw data paths. |
| **Test isolation** | UI tests mock the read-model API, never the underlying files. This proves the UI does not depend on file layout. |

---

## 7. Cross-Project Open-Item Visibility

### 7.1 Requirements

The aggregate open-item surface (S5) must answer questions across projects without the operator manually switching between project views.

**Visibility rules:**

- **V-1**: Every project in the registry contributes its open items to the aggregate view.
- **V-2**: Items are sorted by severity (blocker > warn > info), then by age (oldest first).
- **V-3**: Each item shows its project name, PR origin, and age.
- **V-4**: Runtime anomaly items (from Feature 12, `auto_created: true`) are visually distinct from manually created items.
- **V-5**: The aggregate view shows per-project subtotals alongside the full list.
- **V-6**: If a project's `open_items.json` is unavailable, the aggregate view shows that project as "data unavailable" rather than omitting it or showing zero.

### 7.2 Per-Project Filtering

Within the aggregate view, the operator can filter to a single project. This is equivalent to S4 but accessed from the aggregate surface without navigation.

### 7.3 Cross-Project Attention Model

The projects overview (S1) uses an attention model to highlight projects that need the operator:

| Attention Level | Condition | Visual |
|----------------|-----------|--------|
| **Critical** | ≥1 blocking open item OR ≥1 terminal with dead heartbeat | Red badge |
| **Warning** | ≥1 warning open item OR ≥1 terminal stalled | Amber badge |
| **Info** | Only info-level open items | Subtle indicator |
| **Clear** | Zero open items, all terminals healthy or idle | No badge |

---

## 8. Implementation Guidance For Downstream PRs

### 8.1 PR-1: Read-Model Projections

Must implement:
- `ProjectsView`, `SessionView`, `TerminalView`, `OpenItemsView`, `AggregateOpenItemsView` as Python classes
- Project registry (`~/.vnx/projects.json`) with auto-registration on `vnx start`
- Freshness envelope on every view response (§3.4)
- Degraded-state detection for stale, missing, and contradictory sources (§5.1)
- Tests for fresh, stale, missing, and contradictory source scenarios

Must NOT implement:
- UI rendering (that's PR-3)
- Action handlers (that's PR-2)

### 8.2 PR-2: Safe Operator Control Actions

Must implement:
- Actions A1 through A6 from §4.2
- Action outcome model (§4.4) with AO-1 through AO-4 invariants
- Degraded-action behavior (action on stale data reports degraded outcome)
- Tests for success, failure, already-active, and degraded action paths

Must NOT implement:
- Any forbidden action from §4.3
- UI rendering

### 8.3 PR-3: Dashboard UI

Must implement:
- All 5 surfaces (S1–S5) from §2
- All rendering states per surface
- Degraded and empty state rendering per §5
- Configurable refresh intervals per §5.2 DS-5

Must NOT:
- Import from any path other than the read-model API (§6.3)
- Query files, scripts, tmux, or SQLite directly

### 8.4 PR-4: Certification

Must verify:
- All operator questions from §2 are answerable from the dashboard
- Degraded-state rendering is explicit and never masquerades as healthy
- No UI component bypasses the read-model layer
- Cross-project open-item visibility works per §7
- Session start works end-to-end from the projects overview

---

## 9. Residual Risks And Non-Goals

### 9.1 Not Addressed By This Contract

| Topic | Why Not Here | Where It Belongs |
|-------|-------------|-----------------|
| Multi-user access control | Single-operator dashboard | Future hosted control plane |
| Real-time WebSocket updates | First release uses polling | Future: when latency matters |
| Dashboard persistence (bookmarks, preferences) | Scope creep for first release | Future UX iteration |
| Dispatch creation from dashboard | Requires T0 governance context | Future T0-dashboard integration |
| Historical analytics (trends, velocity charts) | First release is live state only | Future analytics feature |

### 9.2 Known Risks

| Risk | Mitigation |
|------|-----------|
| Project registry is a new file that can get out of sync | Auto-registration on `vnx start` + manual `vnx project add/remove` |
| 10s polling interval for terminal state may feel sluggish | Configurable; can be reduced. WebSocket is a future optimization. |
| Aggregate open-item view could be slow with many projects | Index by project in memory; lazy-load item details. First release targets <10 projects. |
| DB lock contention between dashboard reads and runtime writes | Dashboard uses read-only connections with short timeouts. WAL mode allows concurrent reads. |
| `context_window_*.json` files update irregularly | Show "no data" rather than stale percentage. Mark as degraded when > 300s old. |

---

## Appendix A: Read-Model View Signatures (Pseudocode)

```python
class ProjectsView:
    def list_projects() -> List[ProjectSummary]
    # Returns: name, path, active, session_active, attention_level,
    #          open_blocker_count, active_feature, terminal_summary

class SessionView:
    def get_session(project_path: str) -> SessionDetail
    # Returns: feature_name, pr_progress[], terminal_states[],
    #          track_status{A,B,C}, open_item_summary, last_activity

class TerminalView:
    def get_terminal(project_path: str, terminal_id: str) -> TerminalDetail
    # Returns: worker_state, heartbeat_class, dispatch_info,
    #          last_output_age, context_pressure, anomalies[]

class OpenItemsView:
    def get_items(project_path: str, **filters) -> OpenItemList
    # Returns: items[] with severity, age, pr_id, auto_created,
    #          summary{blocker_count, warn_count, info_count}

class AggregateOpenItemsView:
    def get_aggregate(**filters) -> AggregateOpenItemList
    # Returns: items[] across all projects, per_project_subtotals{},
    #          total_summary{blocker_count, warn_count, info_count}
```

## Appendix B: Refresh Interval Defaults

| View | Default Interval | Rationale |
|------|-----------------|-----------|
| TerminalView | 10s | Terminal state changes frequently during active work |
| SessionView | 30s | PR progress and track status change on dispatch boundaries |
| OpenItemsView | 30s | Open items change on receipt processing |
| AggregateOpenItemsView | 60s | Cross-project aggregation is expensive, changes slowly |
| ProjectsView | 60s | Project list rarely changes |
