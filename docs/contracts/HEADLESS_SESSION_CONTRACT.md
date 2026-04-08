# Headless Runtime Session Contract And Structured Observability Schema

**Feature**: Feature 17 — Rich Headless Runtime Sessions And Structured Observability
**Contract-ID**: headless-session-v1
**Status**: Canonical
**Last Updated**: 2026-04-03
**Extends**: [HEADLESS_RUN_CONTRACT.md](HEADLESS_RUN_CONTRACT.md) (run identity, lifecycle, failure taxonomy)

---

## 1. Purpose

This contract extends the headless run contract to model **sessions** as first-class
entities, define a **structured event schema** richer than raw coordination events,
and make **provider capability limits** explicit so observability claims are honest
about what each CLI actually exposes.

The contract exists so that:

- Headless execution can be reasoned about as governed sessions, not orphan subprocesses
- Structured observability is locked before runtime changes land
- Provider limitations around tool-call visibility are surfaced rather than hidden
- Future learning-loop work has a stable session/attempt evidence model to consume

**Relationship to existing contracts**:
- `HEADLESS_RUN_CONTRACT.md` defines run identity (Section 1), lifecycle states (Section 2), failure taxonomy (Section 4), and basic observability (Section 5). This contract does not replace those — it layers session identity, richer events, evidence classes, and provider awareness on top.
- `RUNTIME_ADAPTER_CONTRACT.md` in this directory defines the adapter interface. This contract defines the observability model that session-aware adapters must emit, not the adapter interface itself.

---

## 2. Session, Attempt, And Run Identity Model

### 2.1 Definitions

| Entity | Scope | Identity | Lifecycle |
|--------|-------|----------|-----------|
| **Session** | A logical headless execution context for one terminal performing one dispatch. Groups all attempts and runs for that terminal/dispatch pair. | `session_id` (UUID4) | Created at dispatch acceptance, closed when dispatch reaches terminal state. |
| **Attempt** | One delivery attempt within a session. A retry creates a new attempt under the same session. | `attempt_id` (existing, from `dispatch_attempts`) | Created per delivery attempt. Terminal on success or classified failure. |
| **Run** | One subprocess execution within an attempt. Maps 1:1 to a `headless_runs` record. | `run_id` (existing, from `headless_run_registry`) | Created per subprocess spawn. Terminal on exit. |

### 2.2 Entity Relationships

```
Session (1) --contains--> (1..N) Attempt --contains--> (1) Run
   |
   +-- session_id (new, UUID4)
   +-- dispatch_id (FK to dispatches)
   +-- terminal_id (canonical T1/T2/T3)
   +-- provider_type (e.g., "headless_claude_cli")
   +-- session_state (lifecycle state)
   +-- created_at, closed_at
```

**Key invariants**:

- **S-1**: A session is scoped to one delivery cycle of a (terminal_id, dispatch_id) pair. Each delivery cycle gets exactly one session. If the dispatch is retried after recovery, a new delivery cycle begins and a new session (with a new `session_id`) is created — the previous session remains CLOSED. The effective identity key is `(terminal_id, dispatch_id, attempt_generation)` where `attempt_generation` is the monotonic attempt counter from the dispatch system.
- **S-2**: A session always has at least one attempt. An attempt always has exactly one run.
- **S-3**: Session state is derived from its attempts — it does not have independent state transitions. When the last attempt reaches a terminal state, the session closes.
- **S-4**: `session_id` appears in all structured events, evidence artifacts, and correlation metadata produced during the session.
- **S-5**: Session closure does not imply dispatch completion. Dispatch completion is T0's authority via the receipt pipeline.

### 2.3 Session State Machine

Session state is a projection of attempt states, not an independent state machine:

```
CREATED -----> ACTIVE -----> CLOSING -----> CLOSED
                 |                            |
                 +--- (attempt terminal) ---->+
```

| State | Meaning | Transition Rule |
|-------|---------|----------------|
| `CREATED` | Session record exists, no attempt started yet | Initial state on session creation |
| `ACTIVE` | At least one attempt is in progress (run state = `init` or `running`) | First attempt transitions to `init` |
| `CLOSING` | Current attempt reached terminal state, evidence being finalized | Run reaches `completing` or `failing` |
| `CLOSED` | All evidence finalized, session complete | Evidence artifacts persisted and linked |

**Invariants**:
- Sessions never reopen after `CLOSED`. A new dispatch attempt creates a new session.
- `CLOSING` -> `CLOSED` requires evidence finalization (Section 6 evidence completeness).
- Session state is read-only for adapters — derived by the session manager from attempt/run state.

---

## 3. Structured Event Schema

### 3.1 Design Principles

The existing `coordination_events` table captures state transitions as append-only
records. This contract defines a richer **structured event stream** that adds
progress signals, observability metadata, and artifact correlation — without
replacing or modifying coordination_events.

**Key rules**:
- Structured events are a superset of coordination events (every transition still emits a coordination_event)
- Structured events add non-transition signals (progress heartbeats, output fragments, artifact links)
- Structured events are append-only and immutable after write
- Structured events carry `session_id` as the primary correlation key

### 3.2 Event Envelope

Every structured event has this envelope:

```json
{
  "event_id": "evt-<uuid4>",
  "event_type": "<type from Section 3.3>",
  "session_id": "<session_id>",
  "run_id": "<run_id or null>",
  "dispatch_id": "<dispatch_id>",
  "terminal_id": "<T1/T2/T3>",
  "timestamp": "<ISO8601 UTC>",
  "sequence": 42,
  "payload": { }
}
```

| Field | Purpose |
|-------|---------|
| `event_id` | Unique event identity. Never reused. |
| `event_type` | One of the types in Section 3.3. |
| `session_id` | Primary correlation key. All events in one session share this. |
| `run_id` | Set when event is specific to a subprocess run. Null for session-level events. |
| `dispatch_id` | Parent dispatch. Always set. |
| `terminal_id` | Canonical terminal ID. Always set. |
| `timestamp` | UTC timestamp of event occurrence. |
| `sequence` | Monotonically increasing counter within a session. Enables ordering even with clock skew. |
| `payload` | Event-type-specific data (see Section 3.3). |

### 3.3 Event Types

#### 3.3.1 Session Lifecycle Events

| Event Type | When | Payload |
|------------|------|---------|
| `session.created` | Session record created | `{ "provider_type": "...", "task_class": "...", "adapter_type": "..." }` |
| `session.closed` | Session finalized | `{ "final_state": "succeeded\|failed", "total_attempts": N, "total_duration_seconds": F, "evidence_complete": bool }` |

#### 3.3.2 Run Lifecycle Events

| Event Type | When | Payload |
|------------|------|---------|
| `run.started` | Subprocess spawned | `{ "pid": N, "pgid": N, "command": "...", "timeout_seconds": N }` |
| `run.progress` | Periodic progress signal | `{ "elapsed_seconds": F, "output_bytes": N, "last_output_age_seconds": F, "heartbeat_ok": bool, "confidence": "high\|medium\|low\|none" }` |
| `run.output_fragment` | Meaningful output detected | `{ "fragment": "<last 200 chars>", "stream": "stdout\|stderr", "cumulative_bytes": N }` |
| `run.timeout` | Subprocess exceeded timeout | `{ "timeout_seconds": N, "elapsed_seconds": F, "output_bytes_at_timeout": N }` |
| `run.completed` | Subprocess exited | `{ "exit_code": N, "duration_seconds": F, "failure_class": "...", "classification_reason": "..." }` |

#### 3.3.3 Evidence Events

| Event Type | When | Payload |
|------------|------|---------|
| `evidence.artifact_created` | Log or output artifact written | `{ "artifact_type": "log\|output\|report", "path": "...", "size_bytes": N }` |
| `evidence.receipt_emitted` | Receipt written to NDJSON pipeline | `{ "receipt_id": "...", "receipt_type": "task_complete\|task_failed\|task_timeout" }` |
| `evidence.correlation_linked` | Correlation metadata finalized | `{ "links": { "log_artifact": "...", "output_artifact": "...", "receipt_id": "...", "report_path": "..." } }` |

#### 3.3.4 Attachability Signal

| Event Type | When | Payload |
|------------|------|---------|
| `session.attachability` | Periodic (same cadence as progress) | `{ "attachable": bool, "reason": "...", "surface_type": "none\|log_tail\|structured_stream" }` |

**Attachability** indicates whether an operator or downstream system can meaningfully observe the session in real time. For headless sessions, `attachable` is typically `false` with `surface_type: "none"` (no terminal to connect to). When structured event streaming is available, `surface_type: "structured_stream"` indicates the event stream can be consumed live. When only log tailing is possible, `surface_type: "log_tail"`.

### 3.4 Event Storage

Structured events are stored as NDJSON in a session-scoped file:

```
$VNX_DATA_DIR/headless_sessions/<session_id>/events.ndjson
```

Each line is one JSON event. The file is append-only during the session and read-only after closure.

Additionally, terminal states from the event stream are projected into the `headless_runs` table (existing) and coordination_events (existing) for backward compatibility.

---

## 4. Evidence Classes

### 4.1 Evidence Taxonomy

Every headless session produces evidence. Evidence is classified by type, durability,
and richness. The evidence model defines what must exist for a session to be
considered **evidence-complete** (required for `CLOSING` -> `CLOSED` transition).

| Class | Description | Durability | Richness |
|-------|-------------|------------|----------|
| **Raw Output** | Captured stdout/stderr from subprocess | Persisted to disk | Low — unstructured text, provider-specific format |
| **Structured Event Stream** | NDJSON event stream (Section 3) | Persisted to disk | High — typed, sequenced, machine-readable |
| **Report Artifact** | Normalized markdown in unified_reports/ | Persisted to disk | Medium — human-readable, receipt-linked |
| **Runtime Correlation** | Links between session/run/attempt/dispatch/receipt | In DB + event stream | High — enables full trace chain |

### 4.2 Evidence Completeness Requirements

A session is **evidence-complete** when all applicable evidence classes are present:

| Evidence Class | Required? | Completeness Check |
|----------------|-----------|-------------------|
| Raw Output | Yes (always) | `artifacts/log_artifact.txt` (the combined log artifact at `log_artifact_path`) exists and is non-empty. The separate `raw_output/stdout.log` and `raw_output/stderr.log` are supplementary capture files; the completeness check uses the combined artifact only. |
| Structured Event Stream | Yes (always) | `events.ndjson` exists, has `session.created` and `session.closed` events |
| Report Artifact | Conditional | Required for review-gate sessions. Must exist at `report_path` in unified_reports/. |
| Runtime Correlation | Yes (always) | `evidence.correlation_linked` event exists with all applicable links populated |

### 4.3 Evidence Inventory Record

At session closure, an **evidence inventory** is written to the session directory:

```json
{
  "session_id": "...",
  "dispatch_id": "...",
  "evidence_complete": true,
  "inventory": {
    "raw_output": { "path": "...", "size_bytes": N, "present": true },
    "event_stream": { "path": "...", "event_count": N, "present": true },
    "report_artifact": { "path": "...", "present": true },
    "correlation": {
      "run_id": "...",
      "attempt_id": "...",
      "receipt_id": "...",
      "log_artifact_path": "...",
      "output_artifact_path": "...",
      "report_path": "..."
    }
  },
  "closed_at": "ISO8601"
}
```

Path: `$VNX_DATA_DIR/headless_sessions/<session_id>/evidence_inventory.json`

---

## 5. Provider Capability Matrix

### 5.1 Problem Statement

Not all CLI providers expose the same internal detail. Claude Code CLI can expose
MCP tool calls. Codex and Gemini CLIs emit text output only. Pretending all providers
offer identical observability creates false confidence in headless evidence quality.

### 5.2 Capability Definitions

| Capability | Description |
|------------|-------------|
| `TOOL_CALL_VISIBILITY` | Provider output includes structured tool-call events (tool name, arguments, result) |
| `STRUCTURED_PROGRESS` | Provider emits machine-parseable progress signals during execution |
| `OUTPUT_STREAMING` | Provider writes output incrementally (not buffered until exit) |
| `EXIT_CODE_SEMANTIC` | Provider exit code reliably distinguishes success/failure/timeout |
| `STDERR_DIAGNOSTIC` | Provider stderr contains actionable diagnostic information on failure |

### 5.3 Provider Capability Matrix

| Capability | Claude Code CLI | Codex CLI | Gemini CLI | Unknown/Custom |
|------------|----------------|-----------|------------|----------------|
| `TOOL_CALL_VISIBILITY` | Yes (with `--output-format json`) | No | No | Assumed No |
| `STRUCTURED_PROGRESS` | No | No | No | Assumed No |
| `OUTPUT_STREAMING` | Yes | Partial (may buffer) | Yes | Assumed No |
| `EXIT_CODE_SEMANTIC` | Yes | Yes | Yes | Unknown |
| `STDERR_DIAGNOSTIC` | Yes | Yes | Partial | Unknown |

### 5.4 Visibility Levels

Based on the capability matrix, each provider is classified into a **visibility level**:

| Level | Name | Criteria | Providers |
|-------|------|----------|-----------|
| `L3` | **Tool-Aware** | `TOOL_CALL_VISIBILITY` = Yes | Claude Code CLI (with json output) |
| `L2` | **Output-Rich** | `OUTPUT_STREAMING` = Yes, `EXIT_CODE_SEMANTIC` = Yes | Claude Code CLI (text), Gemini CLI |
| `L1` | **Output-Only** | `EXIT_CODE_SEMANTIC` = Yes, rest No or Partial | Codex CLI |
| `L0` | **Opaque** | No reliable signals beyond process liveness | Unknown/Custom providers |

### 5.5 Observability Rules By Level

| Rule | L3 (Tool-Aware) | L2 (Output-Rich) | L1 (Output-Only) | L0 (Opaque) |
|------|-----------------|-------------------|-------------------|-------------|
| **Progress confidence** | High — tool calls indicate active work | Medium — output indicates activity | Low — buffered output may delay signals | None — only heartbeat |
| **Stall detection quality** | High — lack of tool calls = likely stalled | Medium — output gap = possible stall | Low — output gap may be buffering | Minimal — heartbeat only |
| **Failure root cause** | Rich — tool errors visible | Medium — stderr diagnostics | Basic — exit code + stderr patterns | Minimal — exit code only |
| **run.output_fragment events** | Emit with tool-call context | Emit raw text fragments | Emit when available (may be delayed) | Do not emit (unreliable) |
| **run.progress confidence field** | `"high"` | `"medium"` | `"low"` | `"none"` |

### 5.6 Provider Capability Declaration

Each provider type must declare its capabilities at registration:

```python
PROVIDER_CAPABILITIES = {
    "headless_claude_cli": {
        "visibility_level": "L2",  # L3 when --output-format json
        "tool_call_visibility": False,  # True with json output
        "structured_progress": False,
        "output_streaming": True,
        "exit_code_semantic": True,
        "stderr_diagnostic": True,
    },
    "headless_codex_cli": {
        "visibility_level": "L1",
        "tool_call_visibility": False,
        "structured_progress": False,
        "output_streaming": False,  # May buffer
        "exit_code_semantic": True,
        "stderr_diagnostic": True,
    },
    "headless_gemini_cli": {
        "visibility_level": "L2",
        "tool_call_visibility": False,
        "structured_progress": False,
        "output_streaming": True,
        "exit_code_semantic": True,
        "stderr_diagnostic": False,  # Partial
    },
}
```

Unknown providers default to `L0` (Opaque) until explicitly registered.

### 5.7 Tool-Call Visibility (L3) Detail

When a provider supports `TOOL_CALL_VISIBILITY`, the structured event stream
includes tool-call events:

| Event Type | When | Payload |
|------------|------|---------|
| `run.tool_call` | Tool invocation detected in output | `{ "tool_name": "...", "arguments_summary": "...", "result_summary": "...", "duration_ms": N }` |

**Important**: Tool-call events are **best-effort**. They depend on the provider
emitting parseable tool-call markers in its output. VNX does not inject or
intercept MCP tool calls — it parses the provider's output format.

Tool-call events are:
- Available only at visibility level L3
- Derived from provider output parsing, not from runtime instrumentation
- Subject to provider output format changes (not a stable ABI)
- Never required for session evidence completeness (they are enrichment, not mandatory evidence)

---

## 6. Session Directory Layout

Each headless session creates a directory under `$VNX_DATA_DIR/headless_sessions/`:

```
$VNX_DATA_DIR/headless_sessions/<session_id>/
  events.ndjson              # Structured event stream (Section 3)
  evidence_inventory.json    # Evidence completeness record (Section 4.3)
  raw_output/
    stdout.log               # Raw captured stdout
    stderr.log               # Raw captured stderr
  artifacts/
    log_artifact.txt         # Combined log artifact (existing format)
    output_artifact.txt      # Structured output (if applicable)
```

The `log_artifact_path` in the run record points to `artifacts/log_artifact.txt`.
The existing log artifact format (from HEADLESS_RUN_CONTRACT Section 5.3) is preserved.

---

## 7. Backward Compatibility

| Constraint | Rule |
|------------|------|
| Existing `headless_runs` table | Session fields are additive columns. No breaking schema changes. |
| Existing `coordination_events` | Continue to emit. Structured events are a parallel stream, not a replacement. |
| Existing log artifacts | Format preserved. Session directory adds structure around them. |
| Existing receipt pipeline | Unchanged. `evidence.receipt_emitted` event is informational, not a new receipt path. |
| Existing failure taxonomy | Unchanged. Session-level evidence builds on per-run classification. |
| `VNX_HEADLESS_ENABLED=0` | Disables headless sessions entirely. No session directories created. |

---

## 8. Testing Contract

### 8.1 Session Lifecycle Tests

1. Session creation assigns unique `session_id`
2. Session state derives from attempt state (not independent)
3. Session closes only after evidence finalization
4. New attempt on same (terminal, dispatch) creates new session (S-1)
5. Session never reopens after CLOSED

### 8.2 Event Stream Tests

1. Every session has `session.created` as first event and `session.closed` as last
2. `sequence` is monotonically increasing within a session
3. `run.started` always precedes `run.completed` or `run.timeout`
4. `run.progress` events have consistent elapsed/output tracking
5. `evidence.correlation_linked` contains all applicable links

### 8.3 Evidence Completeness Tests

1. Evidence-incomplete session cannot transition to CLOSED
2. Missing raw output blocks closure
3. Missing event stream blocks closure
4. Missing report artifact blocks closure for review-gate sessions
5. Evidence inventory accurately reflects actual artifact presence

### 8.4 Provider Capability Tests

1. Each known provider declares correct visibility level
2. Unknown providers default to L0
3. `run.tool_call` events only emitted at L3
4. `run.progress` confidence field matches visibility level
5. `run.output_fragment` events respect visibility-level emission rules

---

## 9. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes

### Phase 2: LocalSessionAdapter Lifecycle (PR-1)
- Implement session creation, attempt tracking, state derivation
- Persist session records in DB
- Add session directory structure

### Phase 3: Structured Event Stream (PR-2)
- Implement event envelope and NDJSON writer
- Emit lifecycle events from adapter
- Add artifact correlation

### Phase 4: Provider-Aware Visibility (PR-3)
- Implement provider capability registration
- Add visibility-level-aware progress and fragment emission
- Expose observability quality projections

### Phase 5: Certification (PR-4)
- Prove session lifecycle correctness
- Prove event stream and evidence integrity
- Prove provider visibility claims

---

## 10. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should sessions own their own state machine? | No. Session state is derived from attempt/run state. This avoids dual-state coordination bugs. |
| Should structured events replace coordination_events? | No. Structured events are a parallel, richer stream. Coordination events remain the backward-compatible audit trail. |
| Should tool-call events be required evidence? | No. They are enrichment at L3 only. Requiring them would make non-Claude providers structurally incomplete. |
| Should the session directory be inside `$VNX_DATA_DIR/`? | Yes. It follows the existing pattern (dispatches, unified_reports) and is excluded from git via `.vnx-data/` gitignore. |
| Should provider capabilities be configurable at runtime? | No. They are declared per provider type in code. Runtime changes would create evidence integrity ambiguity. |
| Should `run.output_fragment` include full output? | No. Fragments are capped at 200 chars. Full output is in raw_output/. Fragments are for real-time observability, not archival. |
