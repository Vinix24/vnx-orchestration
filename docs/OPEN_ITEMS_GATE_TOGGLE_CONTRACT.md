# Open Items And Gate Toggle Contract

**Feature**: Feature 24 — Open Items Page And Gate Toggle
**Contract-ID**: open-items-gate-toggle-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines the project-switcher UX for the open items page, the gate
toggle API, gate state shape, and safe-action A7 (toggle gate) for the operator
dashboard.

---

## 2. Project Switcher UX

### 2.1 Behavior

The open items page shows items scoped to a single project at a time. A project
switcher lets the operator select which project's open items to view.

| Element | Detail |
|---------|--------|
| **Location** | Top of open items page, above the items list |
| **Default** | Auto-select the project matching the current VNX session (from `$VNX_PROJECT_ROOT`) |
| **Options** | All registered projects from `~/.vnx/projects.json` |
| **Selection** | Dropdown. Selecting a project reloads the open items list via `GET /api/operator/open-items?project=<path>` |
| **Persistence** | Selection stored in URL query param (`?project=...`) for shareability |
| **Empty state** | "No projects registered" message with link to `vnx init` instructions |

### 2.2 Open Items Endpoint (Existing, Extended)

```
GET /api/operator/open-items?project=<path>
```

Response: `FreshnessEnvelope` wrapping `OpenItemsView` (existing S3 surface). The `project` param filters to that project's `open_items.json`.

---

## 3. Gate Toggle API

### 3.1 GET /api/operator/gate/config

Returns the current gate configuration for the active project.

**Request**:
```
GET /api/operator/gate/config?project=<path>
```

**Response** (200):
```json
{
  "project": "/path/to/project",
  "gates": {
    "gemini_review": {
      "enabled": true,
      "env_var": "VNX_GEMINI_GATE_ENABLED",
      "description": "Gemini headless code review"
    },
    "codex_gate": {
      "enabled": true,
      "env_var": "VNX_CODEX_GATE_ENABLED",
      "description": "Codex final quality gate"
    },
    "claude_github": {
      "enabled": false,
      "env_var": "VNX_CLAUDE_GITHUB_GATE_ENABLED",
      "description": "Claude GitHub PR review (optional)"
    }
  },
  "queried_at": "ISO8601"
}
```

### 3.2 POST /api/operator/gate/toggle

Toggle a specific gate on or off for the active project.

**Request**:
```
POST /api/operator/gate/toggle
Content-Type: application/json

{
  "project": "/path/to/project",
  "gate": "gemini_review",
  "enabled": false
}
```

**Response** (200):
```json
{
  "success": true,
  "gate": "gemini_review",
  "enabled": false,
  "previous": true,
  "toggled_at": "ISO8601",
  "toggled_by": "operator"
}
```

**Error** (400):
```json
{
  "success": false,
  "error": "Unknown gate: invalid_gate"
}
```

### 3.3 Gate State Shape

Gate state is per-project, per-gate:

```json
{
  "<project_path>": {
    "gemini_review": { "enabled": true },
    "codex_gate": { "enabled": true },
    "claude_github": { "enabled": false }
  }
}
```

**Storage**: `$VNX_STATE_DIR/gate_config.json` (per project). Created with defaults on first query.

**Defaults**: All gates enabled except `claude_github` (matches current behavior — Gemini and Codex default-enabled, Claude GitHub optional).

### 3.4 Gate Toggle Invariants

- **GT-1**: Toggle is idempotent. Toggling to the current state returns success with `previous == enabled`.
- **GT-2**: Toggle records an audit event in coordination_events (`gate_config_changed`).
- **GT-3**: Unknown gate names are rejected with 400.
- **GT-4**: Gate state persists across server restarts (file-backed).
- **GT-5**: Toggle does not affect in-flight dispatches — only future dispatch gate selection.

---

## 4. Safe Action A7: Toggle Gate

### 4.1 Action Definition

Per the dashboard safe-action model (Feature 13), gate toggle is registered as action A7:

| Field | Value |
|-------|-------|
| **Action ID** | `A7` |
| **Name** | `toggle_gate` |
| **Description** | Enable or disable a review gate for the active project |
| **Input** | `{ gate: string, enabled: boolean }` |
| **Output** | `ActionOutcome` with success/error status |
| **Side effects** | Writes to gate_config.json, emits coordination event |
| **Reversible** | Yes — toggle back to previous state |
| **Requires confirmation** | No (lightweight toggle) |

### 4.2 ActionOutcome

```python
ActionOutcome(
    action="toggle_gate",
    status="success",
    message="gemini_review disabled",
    data={"gate": "gemini_review", "enabled": False, "previous": True},
)
```

---

## 5. Testing Contract

### 5.1 Project Switcher Tests

1. Default project matches current session
2. Project selection updates URL query param
3. Empty projects shows "no projects" message
4. Open items reload on project change

### 5.2 Gate Config Tests

1. GET returns all 3 gates with correct defaults
2. Unknown project returns defaults (not error)
3. Response is valid JSON with gates dict

### 5.3 Gate Toggle Tests

1. Toggle changes enabled state and persists
2. Toggle is idempotent (GT-1)
3. Unknown gate returns 400 (GT-3)
4. Audit event recorded (GT-2)
5. File persists across reads (GT-4)

### 5.4 Safe Action Tests

1. A7 registered in action registry
2. A7 returns valid ActionOutcome
3. A7 does not affect in-flight dispatches (GT-5)

---

## 6. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should gate toggle require confirmation? | No. It's a lightweight toggle with immediate feedback. Reversal is trivial. |
| Should gate state be stored per-project or globally? | Per-project. Different projects may have different gate needs. |
| Per-PR gate override? | Not in this feature. Per-project is sufficient. Per-PR deferred. |
