# Runtime Adapter Contract

**Feature**: Feature 16 — Runtime Adapter Formalization And Headless Transport Abstraction
**PR**: PR-0
**Status**: Canonical
**Last Updated**: 2026-04-02

---

## 1. Purpose

This document defines the canonical `RuntimeAdapter` interface that all runtime
transport implementations must satisfy. It establishes the boundary between
VNX orchestration logic (dispatches, leases, receipts, governance) and runtime
transport mechanics (how commands reach terminals and how terminal state is
observed).

The contract exists so that:

- `TmuxAdapter` can be formalized as one implementation rather than the implicit architecture
- Future adapters (headless, local-session, remote) have a locked interface to build against
- Capability gaps are surfaced as governed states rather than hidden behavior
- Runtime/session responsibilities are explicit before extraction begins

---

## 2. Definitions

| Term | Meaning |
|------|---------|
| **Terminal** | A canonical worker identity (T0, T1, T2, T3). Immutable. Stored in `runtime_coordination.db`. |
| **Pane** | A transport-specific execution surface (e.g., tmux pane `%3`). Volatile. Derived. |
| **Session** | A transport-specific container grouping panes (e.g., tmux session `vnx-project`). |
| **Adapter** | An implementation of `RuntimeAdapter` that translates canonical operations into transport-specific commands. |
| **Canonical State** | The authoritative terminal/dispatch state in `runtime_coordination.db`. Never owned by the adapter. |
| **Adapter-Visible State** | Transport-specific state the adapter can observe (e.g., tmux pane existence, process running). |
| **Capability** | A named operation an adapter may or may not support. |

---

## 3. RuntimeAdapter Interface

### 3.1 Responsibility Summary

The adapter is responsible for **transport I/O only**. It does not own lease
state, dispatch state, governance decisions, or receipt processing. The adapter
translates canonical terminal identities into transport-specific targets and
executes transport-level operations.

### 3.2 Required Operations

Every `RuntimeAdapter` implementation must define the following operations.
Operations that are not supported by a given adapter must raise
`UnsupportedCapability` (see Section 5).

#### 3.2.1 `spawn(terminal_id, config) -> SpawnResult`

Create a new execution surface for the given terminal.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID (T0-T3). `config` (dict): provider command, work directory, environment. |
| **Output** | `SpawnResult`: success (bool), transport_ref (str, opaque handle), error (optional str). |
| **Idempotency** | If a surface already exists for `terminal_id`, return existing handle without creating a duplicate. |
| **Failure** | Transport failure raises `AdapterTransportError`. Config validation failure raises `AdapterConfigError`. |
| **Side effects** | Must record `adapter_spawn_start` and `adapter_spawn_success` or `adapter_spawn_failure` coordination events. |

#### 3.2.2 `stop(terminal_id) -> StopResult`

Terminate the execution surface for the given terminal.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `StopResult`: success (bool), was_running (bool), error (optional str). |
| **Idempotency** | Stopping an already-stopped terminal returns `success=True, was_running=False`. |
| **Failure** | Transport failure raises `AdapterTransportError`. |
| **Side effects** | Must record `adapter_stop` coordination event. Must NOT release the lease — lease lifecycle is owned by `LeaseManager`. |

#### 3.2.3 `deliver(terminal_id, dispatch_id, payload) -> DeliveryResult`

Send a dispatch payload to the terminal's execution surface.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str), `dispatch_id` (str), `payload` (DeliveryPayload): skill command and/or dispatch reference. |
| **Output** | `DeliveryResult`: success (bool), delivery_method (str), error (optional str). |
| **Precondition** | Terminal must have an active execution surface. Adapter should validate lease soft-check but must NOT enforce lease state — that is the dispatcher's responsibility. |
| **Failure** | Transport failure returns `success=False` with error detail. Must NOT throw for transport-level failures (caller decides retry policy). |
| **Side effects** | Must record `adapter_deliver_start`, `adapter_deliver_success` or `adapter_deliver_failure` coordination events. |

#### 3.2.4 `attach(terminal_id) -> AttachResult`

Switch operator focus to the terminal's execution surface (interactive).

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `AttachResult`: success (bool), error (optional str). |
| **Behavior** | Transport-specific focus switch (e.g., tmux `select-pane`). |
| **Failure** | Returns `success=False` if terminal surface does not exist. |

#### 3.2.5 `observe(terminal_id) -> ObservationResult`

Capture current observable state from the terminal's execution surface without side effects.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `ObservationResult`: exists (bool), responsive (bool), transport_state (dict), last_output_fragment (optional str), error (optional str). |
| **Behavior** | Read-only probe. Must not send commands, modify state, or cause visible effects in the terminal. |
| **transport_state** | Adapter-specific dict. Keys vary by adapter but must include at minimum: `surface_exists` (bool), `process_alive` (bool). |
| **Failure** | Returns `exists=False` if the execution surface is gone. Never raises for expected transport states. |

#### 3.2.6 `inspect(terminal_id) -> InspectionResult`

Deep inspection of the terminal execution surface for diagnostics.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `InspectionResult`: exists (bool), transport_ref (str), transport_details (dict), pane_content (optional str, last N lines), environment (optional dict), error (optional str). |
| **Behavior** | More expensive than `observe`. May capture pane content, environment variables, process tree. Suitable for `vnx doctor` and recovery, not for hot-path polling. |
| **Failure** | Returns `exists=False` if the execution surface is gone. |

#### 3.2.7 `health(terminal_id) -> HealthResult`

Fast health check for a single terminal.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `HealthResult`: healthy (bool), surface_exists (bool), process_alive (bool), details (dict), error (optional str). |
| **Behavior** | Lightweight subset of `observe`. Must complete within 2 seconds. Suitable for supervisor polling loops. |
| **Failure** | Returns `healthy=False` with detail for any degraded state. |

#### 3.2.8 `session_health() -> SessionHealthResult`

Aggregate health check for the entire adapter session.

| Aspect | Detail |
|--------|--------|
| **Input** | None. |
| **Output** | `SessionHealthResult`: session_exists (bool), terminals (dict of terminal_id -> HealthResult), degraded_terminals (list of str), error (optional str). |
| **Behavior** | Returns health for all known terminals. Must complete within 5 seconds. |

#### 3.2.9 `reheal(terminal_id) -> RehealResult`

Attempt to re-establish the mapping between canonical terminal identity and transport-specific surface after drift.

| Aspect | Detail |
|--------|--------|
| **Input** | `terminal_id` (str): canonical ID. |
| **Output** | `RehealResult`: rehealed (bool), old_ref (optional str), new_ref (optional str), strategy (str), error (optional str). |
| **Behavior** | Adapter-specific recovery. For tmux: rediscover pane by `work_dir` anchor. Must record `adapter_pane_remap` event on success. |
| **Failure** | Returns `rehealed=False` if the terminal cannot be rediscovered. Caller decides escalation. |

---

## 4. Adapter Lifecycle

### 4.1 Initialization

```
adapter = AdapterFactory.create(adapter_type, config)
adapter.initialize(state_dir, session_name, project_root)
```

| Method | Purpose |
|--------|---------|
| `initialize(state_dir, session_name, project_root)` | One-time setup. Load or create transport-specific state files. Validate transport availability. |
| `capabilities() -> CapabilitySet` | Return the set of capabilities this adapter supports (see Section 5). |
| `adapter_type() -> str` | Return the adapter type identifier (e.g., `"tmux"`, `"headless"`, `"local_session"`). |

### 4.2 Shutdown

```
adapter.shutdown(graceful=True)
```

| Method | Purpose |
|--------|---------|
| `shutdown(graceful)` | Clean up transport resources. If `graceful=True`, allow in-flight operations to complete. If `graceful=False`, force-terminate. |

---

## 5. Capability Matrix

### 5.1 Capability Model

Each adapter declares its supported capabilities at initialization. Callers
must check capabilities before invoking operations. Invoking an unsupported
operation must raise `UnsupportedCapability` with the operation name and
adapter type.

### 5.2 Capability Definitions

| Capability | Description | Required |
|------------|-------------|----------|
| `SPAWN` | Create execution surfaces | Yes |
| `STOP` | Terminate execution surfaces | Yes |
| `DELIVER` | Send dispatch payload to terminal | Yes |
| `ATTACH` | Interactive operator focus switch | No |
| `OBSERVE` | Read-only state probe | Yes |
| `INSPECT` | Deep diagnostic inspection | No |
| `HEALTH` | Fast health check | Yes |
| `SESSION_HEALTH` | Aggregate session health | Yes |
| `REHEAL` | Transport-level drift recovery | No |
| `CAPTURE_OUTPUT` | Read terminal output content | No |
| `INTERACTIVE_INPUT` | Send arbitrary keystrokes | No |

### 5.3 Adapter Capability Matrix

| Capability | TmuxAdapter | HeadlessAdapter | LocalSessionAdapter |
|------------|-------------|-----------------|---------------------|
| `SPAWN` | Yes | Yes | Yes |
| `STOP` | Yes | Yes | Yes |
| `DELIVER` | Yes | Yes | Yes |
| `ATTACH` | Yes | No | No |
| `OBSERVE` | Yes | Yes (limited) | Yes (limited) |
| `INSPECT` | Yes | Partial | Partial |
| `HEALTH` | Yes | Yes | Yes |
| `SESSION_HEALTH` | Yes | Yes | Yes |
| `REHEAL` | Yes | No | No |
| `CAPTURE_OUTPUT` | Yes | Partial | No |
| `INTERACTIVE_INPUT` | Yes | No | No |

### 5.4 Unsupported Operation Semantics

When an adapter does not support a capability:

1. The adapter's `capabilities()` set must NOT include the capability
2. Calling the operation must raise `UnsupportedCapability(operation, adapter_type, reason)`
3. The caller (facade/dispatcher) must handle `UnsupportedCapability` as a governed state, not an unexpected error
4. `UnsupportedCapability` must never be silently swallowed — it must be logged and surfaced in doctor/status output

```python
class UnsupportedCapability(RuntimeAdapterError):
    """Raised when an operation is invoked on an adapter that does not support it."""
    def __init__(self, operation: str, adapter_type: str, reason: str = ""):
        self.operation = operation
        self.adapter_type = adapter_type
        self.reason = reason or f"{adapter_type} adapter does not support {operation}"
        super().__init__(self.reason)
```

---

## 6. Canonical State Mapping Rules

### 6.1 Authoritative State Ownership

| State Domain | Owner | Storage | Adapter Role |
|--------------|-------|---------|--------------|
| Terminal lease state | `LeaseManager` | `runtime_coordination.db` | None — adapter must not read or write lease state |
| Dispatch state | `DispatchBroker` | `runtime_coordination.db` | None — adapter must not read or write dispatch state |
| Transport-visible state | `RuntimeAdapter` | Adapter-specific (e.g., `panes.json`) | Owner — adapter writes derived transport mappings |
| Canonical terminal projection | `canonical_state_views` | `terminal_state.json` | None — adapter is a data source, not the projector |

### 6.2 Adapter State -> Canonical State Mapping

The adapter provides transport-visible observations. The runtime facade (not the
adapter) maps these to canonical meaning. The adapter must never infer lease or
dispatch state from transport observations.

| Adapter Observation | Canonical Meaning | Mapping Rule |
|---------------------|-------------------|--------------|
| `surface_exists=True, process_alive=True` | Terminal is reachable | Lease state unchanged. May deliver. |
| `surface_exists=True, process_alive=False` | Terminal surface exists but provider exited | Lease state unchanged. Flag for health attention. Do not auto-expire lease. |
| `surface_exists=False` | Terminal execution surface lost | Flag for reheal attempt. If reheal fails, escalate to recovery. Lease expiry is `LeaseManager`'s decision after explicit `expire()` call. |
| `responsive=True` (observe) | Terminal is accepting input | May deliver. No state change. |
| `responsive=False` (observe) | Terminal exists but not accepting input | Flag no-output-hang attention. Do not auto-classify as failed. |

### 6.3 Mapping Invariants

1. **Adapter never writes canonical state.** The adapter records coordination events (`_append_event`). State transitions are owned by `LeaseManager` and `DispatchBroker`.

2. **Adapter-visible state is volatile.** Tmux pane IDs, process PIDs, and surface existence can change between calls. The adapter must not cache transport state across operation boundaries.

3. **Absence of signal is not signal of absence.** If `observe` returns no output, that does not mean the terminal is hung. The runtime facade must combine adapter observation with lease heartbeat timing and dispatch age before classifying a stall.

4. **Adapter state files are projections.** `panes.json` and any adapter-specific files are derived from transport queries. They are not authoritative for lease, dispatch, or terminal identity.

5. **Event recording is mandatory.** Every adapter operation that changes transport state (spawn, stop, deliver, reheal) must append at least one coordination event. Read-only operations (observe, health) should not record events unless they detect anomalies worth auditing.

---

## 7. TmuxAdapter Compatibility Requirements

### 7.1 Preserved Behaviors

The `TmuxAdapter` formalization must preserve these existing behaviors:

| Behavior | Current Implementation | Contract Requirement |
|----------|----------------------|---------------------|
| Session naming | `vnx-$(basename "$PROJECT_ROOT")` | `TmuxAdapter.initialize()` must use this naming convention |
| Pane layout | 2x2 grid in home window (T0-T3) | `TmuxAdapter.spawn()` must create panes matching `session_profile.json` layout |
| Load-dispatch delivery | `tmux send-keys "load-dispatch <id>" Enter` | `TmuxAdapter.deliver()` primary path. Feature flag `VNX_ADAPTER_PRIMARY` preserved. |
| Legacy paste-buffer fallback | `tmux load-buffer` + `paste-buffer` | `TmuxAdapter.deliver()` fallback path. Feature flag `VNX_ADAPTER_PRIMARY=0`. |
| Pane reheal by work_dir | Rediscover panes by `pane_current_path` match | `TmuxAdapter.reheal()` using work directory as identity anchor |
| Profile drift detection | Compare `session_profile.json` vs live `tmux list-panes` | `TmuxAdapter.observe()` and `TmuxAdapter.health()` must detect drift |
| Dynamic window management | ops, recovery, events windows created on-demand | Out of scope for `RuntimeAdapter` contract (operator UX, not dispatch transport) |

### 7.2 Feature Flag Preservation

| Flag | Default | Purpose | Contract Rule |
|------|---------|---------|--------------|
| `VNX_TMUX_ADAPTER_ENABLED` | `"1"` | Enable/disable tmux adapter entirely | Must be checked in `TmuxAdapter.initialize()`. When `"0"`, adapter reports all capabilities as unavailable. |
| `VNX_ADAPTER_PRIMARY` | `"1"` | Toggle load-dispatch vs legacy paste-buffer | Internal to `TmuxAdapter.deliver()`. Not exposed in contract interface. |
| `VNX_RUNTIME_PRIMARY` | `"1"` | Toggle RuntimeCore vs legacy dispatcher | Consumed by `RuntimeCore`, not by adapter. Preserved as-is. |

### 7.3 Transport-Specific Extensions

`TmuxAdapter` may expose transport-specific methods beyond the `RuntimeAdapter`
interface for operator UX (e.g., `select-pane`, `new-window`, `capture-pane`).
These extensions:

- Must NOT be called by the runtime facade or dispatcher
- Must NOT affect canonical state
- May be called by operator commands (`vnx jump`, `vnx status --panes`)
- Must be clearly documented as transport-specific extensions

---

## 8. Future Adapter Boundary Rules

### 8.1 HeadlessAdapter Constraints

A future `HeadlessAdapter` (subprocess-based, no terminal multiplexer):

| Aspect | Rule |
|--------|------|
| **Spawn** | Launch provider as subprocess with stdin/stdout/stderr capture. No tmux session required. |
| **Stop** | Signal subprocess (SIGTERM, then SIGKILL after grace period). |
| **Deliver** | Write dispatch reference to subprocess stdin or control pipe. |
| **Attach** | `UnsupportedCapability` — headless sessions have no interactive surface. |
| **Observe** | Read subprocess stdout/stderr buffer and process liveness. Limited: no pane content capture. |
| **Inspect** | Read process tree, memory, and recent output. No terminal-format content. |
| **Health** | Check subprocess PID liveness and stdout activity recency. |
| **Reheal** | `UnsupportedCapability` — subprocess identity is process-based, not pane-based. If process dies, spawn a new one. |
| **Session** | No tmux session. Session concept maps to process group or cgroup. |

### 8.2 LocalSessionAdapter Constraints

A future `LocalSessionAdapter` (direct shell execution, single-process):

| Aspect | Rule |
|--------|------|
| **Spawn** | Execute provider command in foreground or managed background process. |
| **Stop** | Terminate the process. |
| **Deliver** | Write to process stdin. |
| **Attach** | `UnsupportedCapability`. |
| **Observe** | Check process liveness and recent output. |
| **Inspect** | Partial — process info only, no terminal content. |
| **Health** | Process liveness check. |
| **Reheal** | `UnsupportedCapability`. |

### 8.3 Adapter Registration Rules

1. Only one adapter may be active for a given session at a time
2. Adapter type is set at session initialization and cannot change mid-session
3. Adapter registration must validate that all required capabilities (`SPAWN`, `STOP`, `DELIVER`, `OBSERVE`, `HEALTH`, `SESSION_HEALTH`) are supported
4. Adapters with missing required capabilities must fail initialization with `AdapterConfigError`

---

## 9. Error Hierarchy

```
RuntimeAdapterError
  +-- AdapterConfigError          # Invalid configuration at init
  +-- AdapterTransportError       # Transport-level failure (tmux command failed, process died)
  +-- UnsupportedCapability       # Operation not supported by this adapter
  +-- AdapterStateError           # Adapter internal state inconsistency
```

All errors carry `adapter_type` and `operation` fields for diagnostic context.
Transport errors carry `transport_detail` with the raw transport error (e.g.,
tmux stderr output, subprocess return code).

---

## 10. Testing Contract

### 10.1 Adapter Conformance Tests

Every `RuntimeAdapter` implementation must pass these conformance tests:

1. **Spawn idempotency** — spawning the same terminal twice returns success without duplication
2. **Stop idempotency** — stopping a non-existent terminal returns success
3. **Deliver precondition** — delivering to a non-existent surface returns failure (not crash)
4. **Observe safety** — observe never modifies transport state
5. **Health timing** — health completes within 2 seconds
6. **Session health timing** — session_health completes within 5 seconds
7. **Unsupported operation** — calling unsupported operations raises `UnsupportedCapability`
8. **Event recording** — state-changing operations produce coordination events
9. **Capability declaration** — `capabilities()` matches actual behavior

### 10.2 TmuxAdapter-Specific Tests

1. **Pane resolution** — terminal_id maps to correct pane_id via panes.json
2. **Profile integrity** — spawned session matches session_profile.json
3. **Reheal accuracy** — pane remap by work_dir succeeds after simulated drift
4. **Feature flag respect** — disabled adapter reports no capabilities
5. **Legacy fallback** — `VNX_ADAPTER_PRIMARY=0` uses paste-buffer delivery

---

## 11. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes to existing adapter

### Phase 2: TmuxAdapter Extraction (PR-1)
- Implement `RuntimeAdapter` protocol in `TmuxAdapter`
- Route validated paths through the new interface
- Freeze new direct tmux coupling in protected paths

### Phase 3: Runtime Facade (PR-2)
- Introduce facade that consumes `RuntimeAdapter`
- Dashboard and dispatcher call facade, not adapter directly

### Phase 4: Headless Skeleton (PR-3)
- Implement `HeadlessAdapter` skeleton behind the contract
- Capability gating for unsupported operations
- Tmux remains default active adapter

### Phase 5: Certification (PR-4)
- Prove adapter-backed parity
- Verify no new direct coupling
- Close feature

---

## 12. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should the adapter own process cleanup? | No. Process cleanup (`pkill`) stays in supervisor/stop scripts. Adapter owns transport surface lifecycle only. |
| Should observe record events? | No, unless anomaly detected. Read-only operations should not create audit noise. |
| Should the adapter validate leases? | Soft pre-check only (warn if lease looks stale). Hard enforcement stays in dispatcher/LeaseManager. |
| Should dynamic tmux windows (ops, recovery) go through the adapter? | No. Dynamic windows are operator UX, not dispatch transport. They remain tmux-specific. |
