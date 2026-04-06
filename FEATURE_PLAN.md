# Feature 31: Headless Worker Resilience

**Feature-ID**: F31
**Status**: In Progress
**Priority**: P1
**Branch**: `feature/f31-headless-worker-resilience`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: codex_gate,claude_github_optional

Primary objective:
Harden the headless subprocess pipeline with timeout protection, lease heartbeat renewal, health monitoring, and LLM-based failure diagnosis so headless workers can run autonomously without dispatcher lockup or zombie leases.

Execution context:
- builds on merged PRs #179-#184: SubprocessAdapter, EventStore, SSE endpoint, agent stream page, event type normalization, Playwright E2E
- SubprocessAdapter burn-in validated 12/12 scenarios including parallel dispatch, long-running tasks, model variations, and session resume
- 101 unit tests cover SubprocessAdapter, StreamEvent parsing, event type normalization, and EventStore
- billing safety invariant preserved: CLI-only, no Anthropic SDK, no API calls

Review gate policy:
- Codex gate required on every PR
- every PR must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged on main

## Problem Statement

The headless subprocess pipeline can hang indefinitely:
- `read_events()` blocks forever if the subprocess hangs (deadlock, infinite loop, waiting for input)
- leases expire (600s TTL) during long-running deliveries because no heartbeat renewal occurs
- no health monitoring detects stuck subprocesses — dispatcher remains blocked
- failures produce no structured diagnosis for automated recovery

## Design Goal

Make headless delivery self-healing: timeout protection kills stuck subprocesses, heartbeat threads keep leases alive, health monitors detect degradation, and LLM diagnosis classifies failure modes for future auto-recovery.

## Non-Goals

- no tmux removal (tmux adapter retained for backward compatibility)
- no Anthropic SDK usage (CLI-only invariant)
- no auto-retry or auto-recovery (diagnosis only in PR-2; recovery is future work)

## Delivery Discipline

- each PR is 150-300 lines and independently deployable
- each PR must have a GitHub PR before merge
- conventional commits: `feat|fix|test|refactor(<scope>): <description>`
- every change verified by existing test suite plus new tests per PR
- billing safety audit on every PR (no Anthropic SDK imports, no API calls)

## Dependency Flow

```text
F31-PR-0 (no dependencies)
F31-PR-0 -> F31-PR-1
F31-PR-1 -> F31-PR-2
```

## F31-PR-0: Timeout Protection + Lease Heartbeat

**Track**: A
**Priority**: P1
**Complexity**: Small
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: []

### Description
Add `read_events_with_timeout()` to SubprocessAdapter with chunk-level and total-deadline timeouts. Add a background heartbeat thread to `subprocess_dispatch.py` that renews the lease during blocking delivery.

### Scope
- `subprocess_adapter.py`: new `read_events_with_timeout()` method using `select.select()` on stdout fd
- `subprocess_dispatch.py`: heartbeat thread via `threading.Event` + `_heartbeat_loop()`
- `subprocess_dispatch.py`: switch `deliver_via_subprocess()` to use `read_events_with_timeout()`
- existing `read_events()` unchanged for backward compatibility

### Deliverables
- `read_events_with_timeout()` in `subprocess_adapter.py`
- `_heartbeat_loop()` + heartbeat thread in `subprocess_dispatch.py`
- `tests/test_subprocess_timeout.py` — 8 tests covering timeout, deadline, heartbeat

### Success Criteria
- chunk timeout kills subprocess after silence period
- total deadline kills subprocess regardless of output
- heartbeat thread renews lease at configured interval
- heartbeat thread stops cleanly on delivery completion
- existing `read_events()` unchanged (backward compatible)
- all new + existing tests pass

### Quality Gate
`gate_f31_pr0_timeout_heartbeat`:
- [ ] `read_events_with_timeout()` kills process on chunk timeout
- [ ] `read_events_with_timeout()` kills process on total deadline
- [ ] Heartbeat thread renews lease during delivery
- [ ] Heartbeat thread stops on completion
- [ ] Existing `read_events()` unchanged
- [ ] All tests pass
- [ ] GitHub PR exists

---

## F31-PR-1: Health Monitor + Subprocess Receipts

**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [F31-PR-0]

### Description
Add periodic health monitoring for running subprocesses and automatic receipt generation when subprocess delivery completes. This gives T0 a completion signal and enables stuck-subprocess detection.

### Scope
- health monitor thread: periodic check of subprocess alive status
- receipt generation after `read_events_with_timeout()` completes
- receipt format: dispatch_id, terminal_id, status, event_count, session_id, exit_code, source
- receipt written to `$VNX_DATA_DIR/receipts/t0_receipts.ndjson`
- tests for health monitoring and receipt generation

### Deliverables
- health monitor integration in `subprocess_dispatch.py`
- receipt generation after delivery completes
- tests for health check + receipt success/failure/edge cases

### Quality Gate
`gate_f31_pr1_health_receipts`:
- [ ] Health monitor detects stuck subprocess
- [ ] Receipt written after successful delivery
- [ ] Receipt written after failed delivery
- [ ] Receipt parseable by existing receipt processor
- [ ] All tests pass
- [ ] GitHub PR exists

---

## F31-PR-2: LLM Failure Diagnosis

**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [F31-PR-1]

### Description
When a subprocess times out or exits with an error, classify the failure mode using structured analysis of stderr, exit code, and last events. Produces a diagnosis record for future auto-recovery decisions.

### Scope
- failure classification: timeout, crash, rate-limit, permission-error, unknown
- stderr capture and truncation for diagnosis context
- structured diagnosis record written alongside receipt
- tests for each failure classification path

### Deliverables
- failure classifier in `subprocess_adapter.py` or `subprocess_dispatch.py`
- diagnosis record format and persistence
- tests for each classification path

### Quality Gate
`gate_f31_pr2_llm_diagnosis`:
- [ ] Timeout failures classified correctly
- [ ] Crash failures classified correctly
- [ ] Diagnosis record written with receipt
- [ ] All tests pass
- [ ] GitHub PR exists

---

# Feature 32: Headless T1 Backend Developer

**Feature-ID**: F32
**Status**: Planned
**Priority**: P1
**Dependencies**: [F31]

Convert T1 from a tmux-era interactive terminal to a pure headless backend-developer agent. Remove skill loading commands, tmux references, and interactive assumptions from T1's CLAUDE.md. Update T0 dispatch instructions for headless T1 delivery.

---

# Feature 33: Dashboard Domain Filter

**Feature-ID**: F33
**Status**: Planned
**Priority**: P1
**Dependencies**: [F32]

Add domain awareness to the dashboard. Kanban board gets domain filter tabs (Coding, Content, All). Agent Stream page replaces terminal selectors with agent name selectors grouped by domain.

---

# Feature 34: Skill Context Inlining

**Feature-ID**: F34
**Status**: Planned
**Priority**: P1
**Dependencies**: [F33]

Enable headless workers to receive skill-specific context by reading the agent directory's CLAUDE.md and prepending it to the dispatch instruction.

---

# Feature 35: End-to-End Validation + Certification

**Feature-ID**: F35
**Status**: Planned
**Priority**: P1
**Dependencies**: [F34]

Full pipeline certification: headless dispatch -> event stream -> archive -> receipt -> gate -> dashboard. Verifies every evidence surface. Closes all open items. Produces a certification report.
