# Feature: Dashboard Agent Stream

**Feature-ID**: Feature 29
**Status**: Planned
**Priority**: P1
**Branch**: `feature/dashboard-agent-stream`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Add real-time agent output visibility to the operator dashboard via Server-Sent Events (SSE). Operators see live thinking, tool calls, and results from worker terminals without needing `tmux attach`.

Execution context:
- Follows F28 (SubprocessAdapter) — requires subprocess stdout pipe for event capture
- SubprocessAdapter reads `--output-format stream-json` events from subprocess stdout
- This feature connects that stream to the dashboard via NDJSON event store + SSE
- Dashboard runs on Next.js (port 3100) with Python API backend (port 4173)
- This is the final feature in the F27→F28→F29 critical path

Execution preconditions:
- F28 must be merged on main (SubprocessAdapter with stream-json support)
- Dashboard must be running (serve_dashboard.py + Next.js frontend)
- At least one terminal must be configurable as `VNX_ADAPTER_T{n}=subprocess`

Review gate policy:
- Gemini headless review required on every PR
- Every PR must be opened as a GitHub PR before merge consideration

## BILLING SAFETY CONSTRAINT

This feature adds no Anthropic SDK imports or API calls. It reads from SubprocessAdapter's stdout pipe (which comes from the `claude` CLI binary) and writes to local NDJSON files. The SSE endpoint serves these events to the browser. All data flows are local.

## Problem Statement

Operators currently have two options for observing worker output:
1. `tmux attach` to a terminal pane — requires SSH/terminal access, single pane at a time
2. Wait for the report to be written — no real-time visibility during execution

With SubprocessAdapter (F28), worker output is available as structured NDJSON events on stdout. But this data is consumed by the adapter and not visible to operators until the dispatch completes.

## Design Goal

Pipe SubprocessAdapter's stream-json output to a persistent NDJSON event store. Serve events to the dashboard via SSE. The dashboard renders a live view of agent thinking, tool calls, and results per terminal.

## Data Flow

```
SubprocessAdapter.read_output()
    │ NDJSON events from subprocess stdout
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

## Non-Goals

- No input sending to workers from dashboard (that's F30+ scope)
- No session management from dashboard (start/stop/restart)
- No historical replay of past dispatch streams (live only for now)
- No multi-terminal combined view (one stream per page)

## Delivery Discipline

- Each PR must have a GitHub PR with clear scope before merge
- Dependent PRs must branch from post-merge main
- Dashboard pages must be testable with mock event data

## Dependency Flow

```text
PR-0 (no dependencies, but F28 must be merged)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
```

---

## PR-0: Agent Stream Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @architect
**Requires-Model**: opus
**Dependencies**: [F28 merged]

### Description
Define the event store format, SSE endpoint contract, and dashboard page data requirements for the agent stream feature.

### Scope
- Define NDJSON event store format: one file per terminal at `.vnx-data/events/{terminal}.ndjson`
- Define event schema: `{"type": "thinking|tool_use|tool_result|text|result|error", "timestamp": "ISO8601", "data": {...}}`
- Define SSE endpoint contract: `GET /api/agent-stream/{terminal}?since={timestamp}`
  - Returns `text/event-stream` with NDJSON events
  - Supports `since` parameter for reconnection (resume from last received event)
  - Returns 404 if terminal has no active stream
- Define dashboard page data requirements: event types to render, color coding, layout
- Define event retention policy: events from current dispatch only, cleared on new dispatch

### Deliverables
- Agent stream contract document
- Event schema specification
- SSE endpoint specification
- Dashboard rendering specification
- GitHub PR with contract

### Success Criteria
- Event schema covers all stream-json event types
- SSE reconnection via `since` parameter is specified
- Dashboard rendering rules are deterministic
- Retention policy prevents unbounded growth

### Quality Gate
`gate_pr0_stream_contract`:
- [ ] Event store format specified (NDJSON, per-terminal files)
- [ ] Event schema covers all stream-json types
- [ ] SSE endpoint contract with reconnection support
- [ ] Dashboard rendering spec
- [ ] Retention policy defined
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-1: Event Store and NDJSON Persistence
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
Implement `EventStore` that persists stream-json events to NDJSON files and supports tailing for SSE consumption.

### Scope
- Create `scripts/lib/event_store.py`:
  - `EventStore.append(terminal: str, event: StreamEvent)` — append event to `.vnx-data/events/{terminal}.ndjson`
  - `EventStore.tail(terminal: str, since: Optional[str]) -> Iterator[StreamEvent]` — yield events since timestamp
  - `EventStore.clear(terminal: str)` — clear events for terminal (called on new dispatch)
  - File locking for concurrent write safety
- Integrate with SubprocessAdapter: after reading each event from stdout, call `event_store.append()`
- Add unit tests with NDJSON fixtures
- Add integration test: subprocess writes events → event store persists → tail reads them

### Deliverables
- `scripts/lib/event_store.py`
- SubprocessAdapter integration (append call)
- Unit and integration tests
- GitHub PR with event store implementation

### Success Criteria
- Events are persisted to NDJSON files atomically
- `tail()` returns events in order with correct `since` filtering
- File locking prevents corruption under concurrent writes
- `clear()` removes all events for a terminal
- Tests pass with real NDJSON data

### Quality Gate
`gate_pr1_event_store`:
- [ ] EventStore.append() writes atomic NDJSON lines
- [ ] EventStore.tail() returns events in order with since filtering
- [ ] File locking works under concurrent access
- [ ] SubprocessAdapter calls append() for each event
- [ ] Tests pass
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-2: SSE Endpoint
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-1]

### Description
Add SSE endpoint to the dashboard API that streams events from the event store to browser clients.

### Scope
- Add `GET /api/agent-stream/{terminal}` to `dashboard/api_operator.py`
  - Content-Type: `text/event-stream`
  - Reads from `EventStore.tail(terminal, since=request.args.get("since"))`
  - Yields events as SSE `data:` lines
  - Keeps connection open, polling event store every 500ms for new events
  - Returns 404 if no events exist for terminal
- Add `GET /api/agent-stream/status` endpoint listing which terminals have active streams
- Handle client disconnection gracefully (stop polling)
- Add tests for SSE response format and reconnection

### Deliverables
- SSE endpoint in `dashboard/api_operator.py`
- Stream status endpoint
- Tests for SSE format and reconnection
- GitHub PR with endpoint implementation

### Success Criteria
- `curl localhost:4173/api/agent-stream/T1` returns `text/event-stream`
- Events arrive in real-time as subprocess produces them
- `since` parameter enables reconnection without duplicate events
- Client disconnection is handled (no resource leak)
- Status endpoint lists active streams

### Quality Gate
`gate_pr2_sse_endpoint`:
- [ ] SSE endpoint returns text/event-stream
- [ ] Events stream in real-time from event store
- [ ] `since` parameter works for reconnection
- [ ] Client disconnection handled
- [ ] Status endpoint works
- [ ] Tests pass
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-3: Dashboard Agent Stream Page + Certification
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-2]

### Description
Build the agent stream page in the Next.js dashboard. Add sidebar navigation link. Certify end-to-end streaming works.

### Scope
- Create `dashboard/token-dashboard/app/agent-stream/page.tsx`:
  - Terminal selector dropdown (T1, T2, T3)
  - Real-time event display using `EventSource` API
  - Render thinking blocks (gray), tool_use blocks (blue), tool_result blocks (green), text (white), errors (red)
  - Auto-scroll to latest event
  - Reconnection on disconnect (using `since` from last received event timestamp)
- Add sidebar link under Operator section: "Agent Stream"
- Add loading/empty/error states per contract
- Certify end-to-end: dispatch → subprocess → event store → SSE → browser render
- Update CHANGELOG.md with F29 closeout
- Update PROJECT_STATUS.md

### Deliverables
- Agent stream page component
- Event renderer components (per event type)
- Sidebar navigation update
- End-to-end certification evidence
- Updated CHANGELOG.md and PROJECT_STATUS.md
- GitHub PR with screenshots and certification

### Success Criteria
- Agent stream page renders real-time events from active worker
- Terminal selector switches between streams
- Events are color-coded by type
- Auto-scroll follows latest output
- Reconnection works after network interruption
- Sidebar shows "Agent Stream" link

### Quality Gate
`gate_pr3_stream_certification`:
- [ ] Agent stream page renders real-time events
- [ ] Terminal selector works
- [ ] Events color-coded by type
- [ ] Auto-scroll works
- [ ] Reconnection works
- [ ] Sidebar link present
- [ ] End-to-end certification: dispatch → subprocess → SSE → browser
- [ ] CHANGELOG.md updated with F29 closeout
- [ ] PROJECT_STATUS.md updated
- [ ] GitHub PR exists with screenshots
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes (no Anthropic SDK imports in any new code)
