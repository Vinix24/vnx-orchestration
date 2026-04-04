# Terminal Startup And Session Control Contract

**Feature**: Feature 26 — Rich Headless Runtime Sessions And Structured Observability
**Contract-ID**: terminal-startup-session-control-v1
**Status**: Canonical
**Last Updated**: 2026-04-04
**PR**: PR-0
**Gate**: `gate_pr0_terminal_startup_contract`
**Author**: T3 (Track C Architecture)

This document defines startup profiles, session naming, session lifecycle API shapes,
safe-action A8 (terminal launch), and the session state model for operator-controlled
terminal startup and session management.

Related contracts:
- [140_DASHBOARD_READ_MODEL_CONTRACT](core/140_DASHBOARD_READ_MODEL_CONTRACT.md) — safe actions A1-A6, read-model surfaces
- [OPEN_ITEMS_GATE_TOGGLE_CONTRACT](OPEN_ITEMS_GATE_TOGGLE_CONTRACT.md) — safe action A7
- [130_RUNTIME_STATE_MACHINE_CONTRACT](core/130_RUNTIME_STATE_MACHINE_CONTRACT.md) — worker states, heartbeat, stall detection
- [80_TERMINAL_EXCLUSIVITY_CONTRACT](core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md) — dispatch safety and lease exclusivity

---

## 1. Purpose

### 1.1 The Problem

Terminal startup currently requires the operator to:
- Know the correct `vnx start` invocation and preset flags
- Understand tmux layout conventions implicitly
- Have no programmatic way to start a session from the dashboard
- Lack a "business mode" for single-terminal operation without the full 2x2 grid
- Have no dry-run capability to preview what a session launch would do before committing

### 1.2 The Fix

Define two startup profiles (dev and business), formalize session naming, expose
session lifecycle through typed API endpoints, and register safe-action A8 for
dashboard-driven terminal launch with dry-run support.

---

## 2. Startup Profiles

### 2.1 Profile Definitions

VNX supports exactly two startup profiles. Each profile determines the tmux layout,
terminal count, and default provider assignment.

| Field | Dev Profile | Business Profile |
|-------|-------------|------------------|
| **Profile ID** | `dev` | `business` |
| **Description** | Full multi-agent orchestration | Single-terminal operator mode |
| **tmux Layout** | 2x2 grid (4 panes) | Single pane (1 pane) |
| **Terminals** | T0, T1, T2, T3 | T0 only |
| **Default Window** | `home` | `home` |
| **T0 Role** | Orchestrator | Orchestrator + worker |
| **T1 Role** | Worker (Track A) | Not launched |
| **T2 Role** | Worker (Track B) | Not launched |
| **T3 Role** | Worker (Track C) | Not launched |
| **Dispatch Model** | Multi-track (A/B/C) | Single-track (T0 self-executes) |
| **Use Case** | Feature development, chain execution | Quick tasks, business ops, review |

### 2.2 Dev Profile: tmux Layout

```
+------------------+------------------+
|                  |                  |
|   T0 (top-left)  |  T1 (top-right)  |
|   Orchestrator   |  Track A         |
|                  |                  |
+------------------+------------------+
|                  |                  |
|  T2 (bottom-left)|  T3 (bottom-right)|
|   Track B        |  Track C         |
|                  |                  |
+------------------+------------------+
```

Window name: `home`
Layout command: `tiled` (tmux even-split)

Provider assignment follows the active preset (full-auto, review-mode, etc.).
T0 and T3 always use `claude_code`. T1 and T2 are configurable per preset.

### 2.3 Business Profile: tmux Layout

```
+-------------------------------------+
|                                     |
|           T0 (single pane)           |
|           Orchestrator               |
|                                     |
+-------------------------------------+
```

Window name: `home`
Layout: single pane, no splits.

T0 operates as both orchestrator and worker. Dispatches targeting tracks A/B/C
are self-executed by T0 sequentially. No worker terminals are spawned.

### 2.4 Profile Storage

Profiles are declared in `$VNX_DATA_DIR/startup_presets/` as `.env` files.
Each preset includes a `VNX_PROFILE` variable:

```bash
# In preset file
VNX_PROFILE=dev        # or "business"
```

When `VNX_PROFILE` is absent, the default is `dev`.

### 2.5 Profile Invariants

| ID | Invariant |
|----|-----------|
| SP-1 | Exactly two profiles exist: `dev` and `business`. No custom profiles. |
| SP-2 | Profile selection is immutable for the lifetime of a session. Changing profile requires stop + start. |
| SP-3 | Business profile must not spawn worker terminals (T1/T2/T3). |
| SP-4 | Dev profile must spawn all four terminals (T0/T1/T2/T3). |
| SP-5 | Both profiles produce a valid `session_profile.json` with the correct pane count. |

---

## 3. Session Naming Convention

### 3.1 Format

```
vnx-<project-name>
```

Where `<project-name>` is `$(basename "$PROJECT_ROOT")`.

Examples:
- Project at `/Users/alice/dev/my-app` -> session `vnx-my-app`
- Worktree at `/Users/alice/dev/my-app-wt` -> session `vnx-my-app-wt`

### 3.2 Naming Invariants

| ID | Invariant |
|----|-----------|
| SN-1 | Session name is deterministic from `PROJECT_ROOT`. No random suffixes. |
| SN-2 | One tmux session per project root. Duplicate names are a startup error. |
| SN-3 | Worktrees produce distinct session names because `basename` differs. |
| SN-4 | Session name is stored in `session_profile.json` as `session_name`. |

---

## 4. Session State Model

### 4.1 States

A VNX session exists in exactly one of these states at any time:

| State | Meaning | Condition |
|-------|---------|-----------|
| **running** | Session is active and all expected terminals are healthy | tmux session exists, all terminals in profile respond to health check |
| **degraded** | Session is active but one or more terminals are unhealthy | tmux session exists, at least one terminal is dead/stalled/exited-bad |
| **stopped** | No active session | No tmux session, no session profile, or session was explicitly stopped |

### 4.2 State Transitions

```
                    start (success)
  [stopped] ─────────────────────────> [running]
      ^                                    |
      |                                    | terminal failure
      |            stop                    v
      +──────────────────────────── [degraded]
      |                                    |
      |            stop                    |
      +────────────────────────────────────+
                                           |
                    reheal (success)        |
  [running] <──────────────────────────────+
```

### 4.3 State Derivation

Session state is derived, not stored. It is computed on each query from:

1. tmux session existence (`tmux has-session -t <name>`)
2. Per-terminal health from `SessionHealthResult`
3. Session profile completeness

```python
def derive_session_state(
    session_name: str,
    profile: SessionProfile,
    health: SessionHealthResult,
) -> str:
    if not health.session_exists:
        return "stopped"
    if health.degraded_terminals:
        return "degraded"
    return "running"
```

### 4.4 State Invariants

| ID | Invariant |
|----|-----------|
| SS-1 | Session state is always derived, never persisted as a static field. |
| SS-2 | A session cannot be `running` if any expected terminal is missing from the health check. |
| SS-3 | A session cannot be `degraded` without an active tmux session. |
| SS-4 | `stopped` is the default state when no evidence of a session exists. |

---

## 5. Session Lifecycle API

All session endpoints follow the existing dashboard API conventions:
- JSON request/response
- `FreshnessEnvelope` wrapping where applicable
- `ActionOutcome` for mutation operations
- `project` parameter identifies the target project by absolute path

### 5.1 POST /api/operator/session/start

Starts a new VNX session for a project. Wraps `vnx start` with profile selection
and dry-run support.

**Request**:
```json
{
  "project": "/absolute/path/to/project",
  "profile": "dev",
  "preset": "full-auto",
  "dry_run": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | yes | — | Absolute path to the project root |
| `profile` | string | no | `"dev"` | Startup profile: `"dev"` or `"business"` |
| `preset` | string | no | `"last-used"` | Preset name from `startup_presets/` |
| `dry_run` | boolean | no | `false` | If true, return what would happen without executing |

**Response (200) — execution mode** (`dry_run: false`):

Returns `ActionOutcome`:
```json
{
  "action": "start_session",
  "project": "/absolute/path/to/project",
  "status": "success",
  "message": "VNX session vnx-project started with dev profile (full-auto preset)",
  "details": {
    "session_name": "vnx-project",
    "profile": "dev",
    "preset": "full-auto",
    "terminals_launched": ["T0", "T1", "T2", "T3"],
    "session_state": "running"
  },
  "timestamp": "2026-04-04T10:00:00Z"
}
```

**Response (200) — dry-run mode** (`dry_run: true`):

Returns `ActionOutcome` with status `"success"` and execution plan in details:
```json
{
  "action": "start_session",
  "project": "/absolute/path/to/project",
  "status": "success",
  "message": "Dry run: would start vnx-project with dev profile (full-auto preset)",
  "details": {
    "dry_run": true,
    "session_name": "vnx-project",
    "profile": "dev",
    "preset": "full-auto",
    "terminals_planned": [
      {
        "terminal_id": "T0",
        "provider": "claude_code",
        "model": "default",
        "role": "orchestrator",
        "track": null,
        "work_dir": "/absolute/path/to/project/.claude/terminals/T0"
      },
      {
        "terminal_id": "T1",
        "provider": "claude_code",
        "model": "sonnet",
        "role": "worker",
        "track": "A",
        "work_dir": "/absolute/path/to/project/.claude/terminals/T1"
      },
      {
        "terminal_id": "T2",
        "provider": "claude_code",
        "model": "sonnet",
        "role": "worker",
        "track": "B",
        "work_dir": "/absolute/path/to/project/.claude/terminals/T2"
      },
      {
        "terminal_id": "T3",
        "provider": "claude_code",
        "model": "default",
        "role": "worker",
        "track": "C",
        "work_dir": "/absolute/path/to/project/.claude/terminals/T3"
      }
    ],
    "tmux_layout": "tiled",
    "environment_variables": [
      "PROJECT_ROOT", "VNX_HOME", "VNX_DATA_DIR", "VNX_STATE_DIR",
      "VNX_DISPATCH_DIR", "VNX_LOGS_DIR", "VNX_SKILLS_DIR",
      "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_REPORTS_DIR", "VNX_DB_DIR"
    ],
    "conflicts": []
  },
  "timestamp": "2026-04-04T10:00:00Z"
}
```

**Error responses**:

| Status | error_code | Condition |
|--------|------------|-----------|
| `already_active` | `SESSION_EXISTS` | A session already exists for this project |
| `failed` | `INVALID_PROFILE` | Profile is not `dev` or `business` |
| `failed` | `INVALID_PRESET` | Preset file not found in `startup_presets/` |
| `failed` | `PROJECT_NOT_FOUND` | Project path does not exist or is not a VNX project |
| `failed` | `TMUX_NOT_AVAILABLE` | tmux binary not found or not running |
| `degraded` | `PARTIAL_LAUNCH` | Some terminals failed to start (dev profile) |

### 5.2 POST /api/operator/session/stop

Stops an active VNX session. Wraps `vnx stop`.

**Request**:
```json
{
  "project": "/absolute/path/to/project"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project` | string | yes | Absolute path to the project root |

**Response (200)**:
```json
{
  "action": "stop_session",
  "project": "/absolute/path/to/project",
  "status": "success",
  "message": "VNX session vnx-project stopped",
  "details": {
    "session_name": "vnx-project",
    "terminals_stopped": ["T0", "T1", "T2", "T3"],
    "intelligence_exported": true
  },
  "timestamp": "2026-04-04T10:05:00Z"
}
```

**Error responses**:

| Status | error_code | Condition |
|--------|------------|-----------|
| `failed` | `NO_SESSION` | No active session for this project |
| `degraded` | `STOP_TIMEOUT` | Session stop timed out (120s) |

### 5.3 POST /api/operator/terminal/attach

Resolves a terminal pane and returns the tmux attach command for the operator.

**Request**:
```json
{
  "project": "/absolute/path/to/project",
  "terminal_id": "T1"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project` | string | yes | Absolute path to the project root |
| `terminal_id` | string | yes | Terminal to attach: `"T0"`, `"T1"`, `"T2"`, or `"T3"` |

**Response (200)**:
```json
{
  "action": "attach_terminal",
  "project": "/absolute/path/to/project",
  "status": "success",
  "message": "Attach command ready for T1",
  "details": {
    "session_name": "vnx-project",
    "terminal_id": "T1",
    "pane_id": "%1",
    "attach_command": "tmux select-pane -t vnx-project:%1"
  },
  "timestamp": "2026-04-04T10:10:00Z"
}
```

**Error responses**:

| Status | error_code | Condition |
|--------|------------|-----------|
| `failed` | `NO_SESSION` | No active session for this project |
| `failed` | `TERMINAL_NOT_FOUND` | Terminal ID not in session profile |
| `failed` | `PANE_DEAD` | Pane exists in profile but process is dead |

---

## 6. Safe Action A8: Terminal Launch

### 6.1 Registration

Per the dashboard safe-action model (140_DASHBOARD_READ_MODEL_CONTRACT.md §4),
terminal launch is registered as action A8:

| Field | Value |
|-------|-------|
| **Action ID** | `A8` |
| **Name** | `launch_terminal` |
| **Verb** | Launch |
| **Scope** | Per-project |
| **Mutating** | Yes |
| **Supports dry-run** | Yes |
| **Outcome statuses** | `success`, `already_active`, `degraded`, `failed` |

### 6.2 Behavior

A8 is the dashboard-facing action that delegates to `POST /api/operator/session/start`.
It differs from the existing A1 (start_session) in the following ways:

| Aspect | A1 (existing) | A8 (new) |
|--------|---------------|----------|
| **Profile selection** | Implicit (uses last-used preset) | Explicit profile + preset params |
| **Dry-run** | Not supported | Supported |
| **Terminal plan** | Not returned | Returned in dry-run details |
| **Business mode** | Not supported | Supported via `profile: "business"` |
| **Conflict detection** | Basic (session exists check) | Extended (shows conflicts in dry-run) |

### 6.3 Dry-Run Semantics

When `dry_run: true`:

1. Resolve project path and validate it is a VNX project
2. Check for existing session (report as conflict, do not fail)
3. Load preset configuration
4. Build terminal plan (which terminals, providers, models, work dirs)
5. Validate tmux availability
6. Return the full plan without executing any tmux commands

Dry-run must never:
- Create tmux sessions or panes
- Write state files (`session_profile.json`, `panes.json`)
- Start any processes
- Modify any environment

### 6.4 A8 Invariants

| ID | Invariant |
|----|-----------|
| A8-1 | A8 always returns an `ActionOutcome`. No silent failures. |
| A8-2 | Dry-run produces the same plan that execution would follow. |
| A8-3 | A8 respects SP-1 through SP-5 (profile invariants). |
| A8-4 | A8 with `profile: "business"` must not spawn T1/T2/T3. |
| A8-5 | A8 does not bypass existing session detection — `already_active` is returned if a session exists. |
| A8-6 | A8 dry-run returns conflicts but does not resolve them. |

---

## 7. A1 Deprecation Path

With A8 providing a superset of A1 functionality:

| Phase | Behavior |
|-------|----------|
| **Phase 1 (this PR)** | A8 is defined. A1 continues to work unchanged. |
| **Phase 2 (future)** | A1 delegates internally to A8 with `profile: "dev"`, `preset: "last-used"`, `dry_run: false`. |
| **Phase 3 (future)** | A1 is deprecated in the dashboard UI. API preserved for backward compatibility. |

No breaking changes in this contract. A1 remains functional.

---

## 8. Session Profile Extension

The existing `SessionProfile` dataclass is extended with profile metadata:

```python
@dataclass
class SessionProfile:
    session_name: str
    home_window: WindowProfile
    dynamic_windows: List[WindowProfile] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""
    # New fields
    profile: str = "dev"           # "dev" | "business"
    preset: str = "last-used"      # Preset name used at launch
```

### 8.1 Profile Extension Invariants

| ID | Invariant |
|----|-----------|
| PE-1 | `profile` defaults to `"dev"` for backward compatibility with existing session profiles. |
| PE-2 | `preset` is informational — it records which preset was active at launch time. |
| PE-3 | Existing session profiles without `profile`/`preset` fields are valid and default to `dev`/`last-used`. |

---

## 9. Contract Boundary

This contract defines:
- Startup profile shapes and invariants
- Session naming convention
- Session state model (derived, not stored)
- API request/response shapes for session start, stop, and terminal attach
- Safe-action A8 with dry-run support
- A1 deprecation path

This contract does NOT define:
- Preset file format (existing, unchanged)
- Internal tmux command sequences (implementation detail)
- Provider selection logic (existing, per `vnx_start_runtime.py`)
- Dashboard UI layout or component design
- Headless session lifecycle (separate contract: `HEADLESS_SESSION_CONTRACT.md`)
- Intelligence export behavior on stop (existing, per `stop.sh`)

---

## 10. Quality Gate Checklist

`gate_pr0_terminal_startup_contract`:
- [ ] Contract defines dev and business startup profiles with explicit terminal counts
- [ ] Contract defines session naming convention `vnx-<project-name>`
- [ ] Contract defines `POST /api/operator/session/start` request/response shape
- [ ] Contract defines `POST /api/operator/session/stop` request/response shape
- [ ] Contract defines `POST /api/operator/terminal/attach` request/response shape
- [ ] Contract defines safe-action A8 with dry-run support
- [ ] Contract defines session state model (running/stopped/degraded)
- [ ] All invariants are explicitly numbered and testable
- [ ] GitHub PR exists with contract summary
- [ ] Required GitHub Actions checks are green before merge
