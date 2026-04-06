# Agent Stream Contract

**Feature**: F29 — Dashboard Agent Stream
**Version**: 1.0
**Date**: 2026-04-06

## Overview

Real-time agent output visibility for the operator dashboard via NDJSON event store and Server-Sent Events (SSE). Operators see live thinking, tool calls, and results from worker terminals without `tmux attach`.

## Data Flow

```
SubprocessAdapter.read_events()
    │ NDJSON events from subprocess stdout (claude -p --output-format stream-json)
    ▼
EventStore.append(terminal, event)
    │ writes to .vnx-data/events/{terminal}.ndjson
    ▼
SSE Endpoint: GET /api/agent-stream/{terminal}
    │ tails NDJSON file, sends events via SSE
    ▼
Browser: EventSource("/api/agent-stream/{terminal}")
    │ receives events in real-time
    ▼
AgentStream page: renders thinking/tool_use/result blocks
```

**Billing safety**: No Anthropic SDK imports. All data flows are local — reading from subprocess stdout pipes and writing to local NDJSON files.

---

## 1. NDJSON Event Store Format

### Storage Location

```
.vnx-data/events/{terminal}.ndjson
```

One file per terminal: `T1.ndjson`, `T2.ndjson`, `T3.ndjson`.

### File Format

Each file is append-only NDJSON (newline-delimited JSON). One JSON object per line, terminated by `\n`. Lines are written atomically (single `write()` call including trailing newline) to prevent partial reads under concurrent access.

### Write Semantics

- `EventStore.append(terminal, event)` — append one event as a single NDJSON line
- `EventStore.clear(terminal)` — truncate the file (called when a new dispatch starts on this terminal)
- `EventStore.tail(terminal, since)` — yield events with `timestamp > since`, used by SSE endpoint
- File-level locking via `fcntl.flock(LOCK_EX)` on write, `LOCK_SH` on read

### Directory Lifecycle

The `.vnx-data/events/` directory is created on first write. It is runtime state and must NOT be committed to git.

---

## 2. Event Schema

Every event in the NDJSON file conforms to this envelope:

```json
{
  "type": "<event_type>",
  "timestamp": "<ISO 8601 with milliseconds>",
  "dispatch_id": "<current dispatch ID>",
  "terminal": "<T1|T2|T3>",
  "sequence": <monotonic integer>,
  "data": { ... }
}
```

### Envelope Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Event type (see below) |
| `timestamp` | string | ISO 8601, e.g. `2026-04-06T16:20:34.123Z` |
| `dispatch_id` | string | The active dispatch ID for this terminal |
| `terminal` | string | Terminal identifier: `T1`, `T2`, `T3` |
| `sequence` | integer | Monotonically increasing per-terminal counter, starts at 1 per dispatch |
| `data` | object | Type-specific payload (see below) |

### Event Types

These map directly to the `type` field in Claude CLI `--output-format stream-json` output:

#### `init`

First event emitted when subprocess starts. Contains session metadata.

```json
{
  "type": "init",
  "timestamp": "2026-04-06T16:20:34.001Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 1,
  "data": {
    "session_id": "sess_abc123",
    "model": "sonnet",
    "tools": ["Read", "Write", "Bash", "Grep", "Glob", "Edit"]
  }
}
```

#### `thinking`

Agent reasoning/planning output (not shown to user in Claude UI, but visible to operators).

```json
{
  "type": "thinking",
  "timestamp": "2026-04-06T16:20:35.200Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 2,
  "data": {
    "thinking": "I need to read the file first to understand the structure..."
  }
}
```

#### `tool_use`

Agent invokes a tool. Contains tool name and input parameters.

```json
{
  "type": "tool_use",
  "timestamp": "2026-04-06T16:20:36.500Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 3,
  "data": {
    "tool_use_id": "toolu_abc123",
    "name": "Read",
    "input": {
      "file_path": "/path/to/file.py"
    }
  }
}
```

#### `tool_result`

Result returned from tool execution.

```json
{
  "type": "tool_result",
  "timestamp": "2026-04-06T16:20:37.100Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 4,
  "data": {
    "tool_use_id": "toolu_abc123",
    "content": "1\timport json\n2\timport os\n...",
    "is_error": false
  }
}
```

#### `text`

Streaming text output from the agent (assistant message content).

```json
{
  "type": "text",
  "timestamp": "2026-04-06T16:20:38.000Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 5,
  "data": {
    "text": "I've read the file. Now I'll make the following changes..."
  }
}
```

#### `result`

Final result event when the agent completes its turn. Contains the full response.

```json
{
  "type": "result",
  "timestamp": "2026-04-06T16:20:45.000Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 10,
  "data": {
    "cost_usd": 0.042,
    "duration_ms": 11000,
    "input_tokens": 8500,
    "output_tokens": 1200,
    "session_id": "sess_abc123"
  }
}
```

#### `error`

Error events from the subprocess (parse failures, crashes, permission errors).

```json
{
  "type": "error",
  "timestamp": "2026-04-06T16:20:40.000Z",
  "dispatch_id": "20260406-f29-pr1-T1",
  "terminal": "T1",
  "sequence": 6,
  "data": {
    "error": "Process exited with code 1",
    "details": "stderr output if available"
  }
}
```

### Unknown Event Types

If the Claude CLI emits an event type not listed above, the EventStore persists it as-is with `type` set to the raw value. The SSE endpoint forwards it. The dashboard renders it as a generic gray block with the raw JSON. This ensures forward compatibility when new event types are added to the CLI.

---

## 3. SSE Endpoint Contract

### `GET /api/agent-stream/{terminal}`

Stream real-time events for a terminal via Server-Sent Events.

**Path Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| `terminal` | string | Terminal ID: `T1`, `T2`, or `T3` |

**Query Parameters**:
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `since` | string (ISO 8601) | No | Resume from this timestamp. Only events with `timestamp > since` are sent. Used for reconnection. |

**Response**:
- Content-Type: `text/event-stream`
- Cache-Control: `no-cache`
- Connection: `keep-alive`

**SSE Message Format**:
```
event: agent_event
data: {"type":"thinking","timestamp":"2026-04-06T16:20:35.200Z","dispatch_id":"...","terminal":"T1","sequence":2,"data":{"thinking":"..."}}

event: agent_event
data: {"type":"tool_use","timestamp":"2026-04-06T16:20:36.500Z","dispatch_id":"...","terminal":"T1","sequence":3,"data":{"name":"Read","input":{...}}}

event: heartbeat
data: {"timestamp":"2026-04-06T16:20:40.000Z"}

```

Each SSE message uses `event: agent_event` for real events and `event: heartbeat` for keep-alive.

**Behavior**:
1. On connection, read all events from the NDJSON file matching the `since` filter (or all events if no `since`)
2. Send matching events as SSE messages
3. Enter tail mode: poll the NDJSON file every 500ms for new events
4. Send a `heartbeat` event every 15 seconds if no real events were sent (prevents proxy/browser timeout)
5. On `result` or `error` event, send the event then close the connection
6. On client disconnect, stop polling immediately (no resource leak)

**Error Responses**:
| Status | Condition |
|--------|-----------|
| 404 | No NDJSON file exists for the terminal (no active or recent stream) |
| 400 | Invalid terminal ID (not T1/T2/T3) |

### `GET /api/agent-stream/status`

List terminals with active streams.

**Response** (JSON):
```json
{
  "streams": {
    "T1": {
      "active": true,
      "dispatch_id": "20260406-f29-pr1-T1",
      "event_count": 42,
      "last_event_type": "tool_use",
      "last_event_time": "2026-04-06T16:20:36.500Z"
    },
    "T2": {
      "active": false,
      "dispatch_id": null,
      "event_count": 0,
      "last_event_type": null,
      "last_event_time": null
    },
    "T3": {
      "active": true,
      "dispatch_id": "20260406-f29-pr0-T3",
      "event_count": 15,
      "last_event_type": "thinking",
      "last_event_time": "2026-04-06T16:20:35.200Z"
    }
  }
}
```

A stream is `active: true` if the NDJSON file exists and the last event is not `result` or `error`.

---

## 4. Dashboard Rendering Specification

### Page: `/agent-stream`

#### Layout

```
┌─────────────────────────────────────────┐
│  Terminal: [T1 ▾]                       │
├─────────────────────────────────────────┤
│  Dispatch: 20260406-f29-pr1-T1          │
│  Status: ● Streaming (42 events)        │
├─────────────────────────────────────────┤
│                                         │
│  [thinking block - gray]                │
│  I need to read the file first...       │
│                                         │
│  [tool_use block - blue]                │
│  ⚙ Read /path/to/file.py               │
│                                         │
│  [tool_result block - green]            │
│  ✓ 42 lines read                        │
│                                         │
│  [text block - white]                   │
│  I've read the file. Now I'll...        │
│                                         │
│  [result block - purple]                │
│  ✓ Complete (0.042 USD, 11s)            │
│                                         │
│  [error block - red]                    │
│  ✗ Process exited with code 1           │
│                                         │
│                        [auto-scroll ▾]  │
└─────────────────────────────────────────┘
```

#### Event Type Rendering

| Event Type | Background | Icon | Content Displayed |
|------------|------------|------|-------------------|
| `init` | `slate-800` | `▶` | Session ID, model name, tool count |
| `thinking` | `gray-800` | `💭` | Thinking text (collapsible if >5 lines) |
| `tool_use` | `blue-900` | `⚙` | Tool name + summarized input (file paths, commands) |
| `tool_result` | `green-900` | `✓` / `✗` | Truncated output (first 10 lines), expandable. Red icon if `is_error: true` |
| `text` | `slate-700` | none | Full text content |
| `result` | `purple-900` | `✓` | Cost, duration, token counts |
| `error` | `red-900` | `✗` | Error message + details |
| unknown | `gray-900` | `?` | Raw JSON, collapsed |

#### Interaction Rules

1. **Terminal selector**: Dropdown with T1, T2, T3. Switching disconnects current EventSource and connects to the new terminal.
2. **Auto-scroll**: Enabled by default. Scrolls to bottom on each new event. Disabled if user scrolls up manually. Re-enabled when user scrolls to bottom or clicks the auto-scroll button.
3. **Collapsible blocks**: `thinking` blocks >5 lines and `tool_result` blocks >10 lines are collapsed by default with "[expand]" toggle.
4. **Timestamps**: Each event shows relative time ("2s ago", "1m ago") with full ISO timestamp on hover.
5. **Connection status indicator**:
   - `● Streaming` (green dot) — EventSource connected and receiving events
   - `● Idle` (yellow dot) — connected but no events in last 30 seconds
   - `● Disconnected` (red dot) — EventSource closed or errored, with "Reconnect" button

#### Empty/Loading/Error States

- **Loading**: Skeleton loader with pulsing gray blocks
- **Empty** (no events): "No active stream for {terminal}. Start a dispatch with `VNX_ADAPTER_{terminal}=subprocess` to see live output."
- **Error** (SSE connection failed): "Failed to connect to agent stream. Check that the dashboard API is running on port 4173." with retry button.

---

## 5. Event Retention Policy

### Current Dispatch Only

Events are retained for the **current dispatch only** per terminal. When `EventStore.clear(terminal)` is called (at the start of a new dispatch delivery), the NDJSON file is truncated to zero bytes.

### Retention Rules

| Condition | Action |
|-----------|--------|
| New dispatch delivered to terminal | `EventStore.clear(terminal)` — truncate file |
| Dispatch completes (`result` event) | Events remain until next dispatch |
| Dispatch errors (`error` event) | Events remain until next dispatch |
| No dispatch active | File may contain events from last dispatch (stale but harmless) |
| System restart | NDJSON files persist (they are in `.vnx-data/`) |

### Size Bounds

- Typical dispatch: 50-500 events, ~50KB-500KB per terminal
- Maximum observed: ~5,000 events for complex dispatches, ~5MB
- No time-based TTL — dispatch-scoped clearing is sufficient
- If a single dispatch exceeds 10MB, the EventStore logs a warning but continues appending (operator intervention expected for runaway processes)

### Cleanup

No background cleanup daemon is needed. The dispatch-scoped clearing pattern ensures files stay bounded. The `.vnx-data/events/` directory is excluded from git and can be safely deleted to free space.

---

## 6. Integration Points

### SubprocessAdapter (PR-1)

After each event from `read_events()`, the adapter calls:
```python
event_store.append(terminal_id, event)
```

The adapter must call `event_store.clear(terminal_id)` before writing the first event of a new dispatch (inside `deliver()`).

### Dashboard API (PR-2)

`api_operator.py` adds the SSE endpoint. It uses `event_store.tail()` to read events and streams them as SSE.

### Dashboard Frontend (PR-3)

`app/agent-stream/page.tsx` connects via `EventSource` and renders events per the rendering spec above.

---

## GATE ENFORCEMENT (MANDATORY)

For EVERY PR in this feature (F29):

1. Create GitHub PR with clear scope description
2. Request Gemini gate: `python scripts/review_gate_manager.py request --pr <N>`
3. Execute Gemini gate: `python scripts/review_gate_manager.py execute --gate gemini_review --pr <N>`
4. Verify `status=completed` with 0 blocking findings
5. ONLY THEN merge

Skipping this sequence is a T0 orchestration failure. No exceptions.
