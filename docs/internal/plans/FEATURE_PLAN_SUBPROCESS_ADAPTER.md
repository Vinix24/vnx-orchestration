# Feature: Subprocess Adapter

**Feature-ID**: Feature 28
**Status**: Planned
**Priority**: P1
**Branch**: `feature/subprocess-adapter`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Replace tmux-based terminal management with subprocess-based execution using `subprocess.Popen(["claude", ...])`. Enable structured event streaming via `--output-format stream-json` and session continuity via `--resume`.

Execution context:
- Follows F27 (batch refactor) — dispatcher must be decomposed before modifying delivery path
- This is the core architectural migration in the F27→F28→F29 critical path
- tmux adapter is retained for backward compatibility (not deleted)
- Per-terminal adapter selection via `VNX_ADAPTER_T{n}=subprocess|tmux` feature flag

Execution preconditions:
- F27 must be merged on main (dispatcher decomposed)
- `scripts/lib/adapter_protocol.py` (63L) defines the existing RuntimeAdapter protocol
- `scripts/lib/tmux_adapter.py` (798L) is the current implementation
- `scripts/lib/runtime_facade.py` (184L) is the adapter selection layer

Review gate policy:
- Gemini headless review required on every PR
- Every PR must be opened as a GitHub PR before merge consideration

## BILLING SAFETY CONSTRAINT

**CLI-ONLY. No exceptions.**

The SubprocessAdapter must:
- Only invoke the `claude` CLI binary via `subprocess.Popen`
- Never import `anthropic`, `claude_agent_sdk`, or `@anthropic-ai/sdk`
- Never make HTTP requests to `*.anthropic.com`
- Never handle Anthropic OAuth tokens

This constraint is verified by the 4-question billing audit on every PR.

## Problem Statement

VNX currently manages worker terminals via tmux panes. This causes a class of operational failures:

| Failure Mode | Frequency | Impact |
|-------------|-----------|--------|
| Stale pane IDs / `panes.json` discovery failures | Weekly | Dispatch delivery fails silently |
| Input-mode probing / `/clear` failures | Per dispatch | Blocks delivery until operator intervenes |
| `tmux capture-pane` scraping misses | Frequent | Lost output, incorrect state detection |
| Cross-pane contamination | Occasional | Wrong dispatch delivered to wrong terminal |
| Tmux session state corruption | Monthly | Full session restart required |

These failures are inherent to the tmux model (shared session, text-based IPC) and cannot be fully fixed within it.

## Design Goal

Implement `SubprocessAdapter` as an alternative `RuntimeAdapter` that spawns isolated `claude` CLI processes. Workers run as OS processes with deterministic lifecycle management. Event streaming uses native `--output-format stream-json`. Adapter selection is per-terminal via environment variables.

## Non-Goals

- Removing tmux support (that's F30, deferred)
- Dashboard integration (that's F29)
- Changing the dispatch model or T0 orchestration
- Custom API parameters or model switching mid-session

## Architecture

```
Dispatcher
    │
    ▼
RuntimeFacade (adapter factory)
    │
    ├── VNX_ADAPTER_T1=tmux  → TmuxAdapter
    ├── VNX_ADAPTER_T2=subprocess → SubprocessAdapter
    └── VNX_ADAPTER_T3=subprocess → SubprocessAdapter
    │
    ▼
SubprocessAdapter
    │
    ├── spawn(terminal, instruction, model, resume_session=None) → Process
    ├── poll(process) → ProcessState
    ├── read_output(process) → Iterator[StreamEvent]
    ├── get_session_id(process) → Optional[str]
    ├── terminate(process) → None
    └── kill(process) → None
    │
    ▼
subprocess.Popen(["claude", "-p", "--output-format", "stream-json", "--model", model, instruction])
    (optionally: ["--resume", session_id])
```

## Delivery Discipline

- Each PR must have a GitHub PR with clear scope before merge
- Dependent PRs must branch from post-merge main
- Each PR must pass all existing tests
- Billing audit (4-question check) required on every PR

## Dependency Flow

```text
PR-0 (no dependencies, but F27 must be merged)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

---

## PR-0: Extract adapter_types.py
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @architect
**Requires-Model**: opus
**Dependencies**: [F27 merged]

### Description
Extract shared types (`RuntimeAdapter` protocol, `ProcessState`, `TerminalInfo`, `AdapterConfig`) from `tmux_adapter.py` and `adapter_protocol.py` into a standalone `adapter_types.py`. This decouples the protocol from the tmux implementation.

### Scope
- Create `scripts/lib/adapter_types.py` with all shared types
- Move `RuntimeAdapter` protocol definition from `adapter_protocol.py`
- Extract `ProcessState`, `TerminalInfo`, and related types from `tmux_adapter.py`
- Update imports in `tmux_adapter.py`, `runtime_facade.py`, and all consumers
- `adapter_protocol.py` becomes a re-export shim or is removed

### Deliverables
- `scripts/lib/adapter_types.py` with shared types
- Updated imports across codebase
- All tests pass
- GitHub PR with type inventory

### Success Criteria
- `adapter_types.py` is self-contained (no circular imports)
- `tmux_adapter.py` imports types from `adapter_types.py`
- All existing tests pass with updated imports
- No behavioral changes

### Quality Gate
`gate_pr0_adapter_types`:
- [ ] `adapter_types.py` created with RuntimeAdapter protocol + shared types
- [ ] No circular imports
- [ ] All tests pass
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes (no Anthropic SDK imports)

---

## PR-1: RuntimeFacade Accepts RuntimeAdapter Protocol
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
Modify `RuntimeFacade` to accept any `RuntimeAdapter` implementation via factory pattern. Add per-terminal adapter selection via `VNX_ADAPTER_T{n}` environment variables.

### Scope
- Modify `scripts/lib/runtime_facade.py` to use adapter factory
- Add `get_adapter(terminal: str) -> RuntimeAdapter` factory function
- Read `VNX_ADAPTER_T{n}` env vars (values: `tmux` | `subprocess`, default: `tmux`)
- Default to `TmuxAdapter` when env var is unset (backward compatibility)
- Add tests for factory selection logic

### Deliverables
- Updated `runtime_facade.py` with adapter factory
- Factory selection tests
- GitHub PR with adapter factory design

### Success Criteria
- `VNX_ADAPTER_T1=tmux` selects TmuxAdapter
- `VNX_ADAPTER_T2=subprocess` selects SubprocessAdapter (once PR-2 exists)
- Unset env var defaults to TmuxAdapter
- All existing tests pass (TmuxAdapter is still default)

### Quality Gate
`gate_pr1_facade_protocol`:
- [ ] RuntimeFacade uses adapter factory
- [ ] Per-terminal env var selection works
- [ ] Default is TmuxAdapter (backward compatible)
- [ ] Factory selection tests pass
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes

---

## PR-2: SubprocessAdapter Core
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-1]

### Description
Implement `SubprocessAdapter` with core lifecycle methods: spawn, poll, read_output, terminate, kill. Uses `subprocess.Popen` with `--output-format stream-json`.

### Scope
- Create `scripts/lib/subprocess_adapter.py` implementing `RuntimeAdapter`
- `spawn()`: `subprocess.Popen(["claude", "-p", "--output-format", "stream-json", "--model", model, instruction])` with `stdout=PIPE, stderr=PIPE`
- `poll()`: `process.poll()` → `ProcessState` mapping
- `read_output()`: Line-by-line NDJSON reading from stdout pipe
- `terminate()`: `SIGTERM` with timeout, escalate to `SIGKILL`
- `kill()`: Immediate `SIGKILL`
- Process group management for clean cleanup (`os.setpgrp`)
- Add unit tests with mock subprocess

### Deliverables
- `scripts/lib/subprocess_adapter.py`
- Unit tests for all lifecycle methods
- GitHub PR with adapter implementation

### Success Criteria
- SubprocessAdapter implements full RuntimeAdapter protocol
- Process spawns with correct CLI flags
- SIGTERM → SIGKILL escalation works
- Process group cleanup prevents orphans
- Unit tests pass

### Quality Gate
`gate_pr2_subprocess_core`:
- [ ] SubprocessAdapter implements RuntimeAdapter protocol
- [ ] spawn() constructs correct CLI command
- [ ] poll() returns correct ProcessState
- [ ] terminate() escalates SIGTERM → SIGKILL
- [ ] Process group cleanup tested
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes (no Anthropic SDK imports in subprocess_adapter.py)

---

## PR-3: stream-json Event Parsing and --resume Support
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-2]

### Description
Add structured NDJSON event parsing for `--output-format stream-json` output. Extract `session_id` from init events. Support `--resume` for session continuity.

### Scope
- Parse NDJSON events from subprocess stdout: `init`, `thinking`, `tool_use`, `tool_result`, `text`, `result`, `error`
- Extract `session_id` from first `init` event via `get_session_id()`
- Add `resume_session` parameter to `spawn()`:
  ```python
  def spawn(self, terminal, instruction, model, resume_session=None) -> Process:
      cmd = ["claude", "-p", "--output-format", "stream-json", "--model", model]
      if resume_session:
          cmd.extend(["--resume", resume_session])
      cmd.append(instruction)
      return subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE)
  ```
- Define `StreamEvent` dataclass for typed event access
- Add tests with sample NDJSON fixtures

### Deliverables
- Event parser module in `subprocess_adapter.py`
- `StreamEvent` dataclass
- `get_session_id()` implementation
- `--resume` support in `spawn()`
- Tests with NDJSON fixtures
- GitHub PR with event parsing design

### Success Criteria
- All stream-json event types are parsed correctly
- `session_id` extracted from init event
- `--resume` flag added when `resume_session` is provided
- Invalid NDJSON lines are handled gracefully (logged, not crashed)
- Tests cover all event types + malformed input

### Quality Gate
`gate_pr3_stream_json`:
- [ ] All event types parsed correctly
- [ ] session_id extraction works
- [ ] --resume flag correctly added to CLI command
- [ ] Malformed NDJSON handled gracefully
- [ ] Tests with NDJSON fixtures pass
- [ ] GitHub PR exists
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes

---

## PR-4: Dispatcher Routing via Subprocess + Feature Flag
**Track**: B
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-3]

### Description
Wire the SubprocessAdapter into the dispatcher delivery path. Enable per-terminal selection via `VNX_ADAPTER_T{n}=subprocess` environment variables. Verify end-to-end dispatch delivery via subprocess.

### Scope
- Modify dispatcher delivery module (post-F27 decomposed) to use RuntimeFacade
- RuntimeFacade selects SubprocessAdapter when `VNX_ADAPTER_T{n}=subprocess`
- Add integration test: dispatch → subprocess → completion → receipt
- Verify receipt processing works with subprocess output
- Document feature flag usage in operator guide
- Update CHANGELOG.md with F28 closeout
- Update PROJECT_STATUS.md

### Deliverables
- Dispatcher integration with SubprocessAdapter
- Integration tests
- Feature flag documentation
- Updated CHANGELOG.md and PROJECT_STATUS.md
- GitHub PR with end-to-end evidence

### Success Criteria
- `VNX_ADAPTER_T2=subprocess` routes T2 dispatches through SubprocessAdapter
- Dispatch delivery → subprocess execution → receipt generation works end-to-end
- TmuxAdapter still works when env var is unset (backward compatibility)
- Receipt format is identical regardless of adapter
- All tests pass

### Quality Gate
`gate_pr4_dispatcher_routing`:
- [ ] End-to-end dispatch via subprocess works
- [ ] Feature flag selects correct adapter per terminal
- [ ] TmuxAdapter still works as default
- [ ] Receipt format unchanged
- [ ] Integration tests pass
- [ ] CHANGELOG.md updated with F28 closeout
- [ ] PROJECT_STATUS.md updated
- [ ] GitHub PR exists with end-to-end evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings
- [ ] Billing audit passes (final full-codebase scan)
