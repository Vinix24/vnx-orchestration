# Agent OS Project Plan

**Date**: 2026-04-06
**Status**: Active
**Author**: T1 (Architect)
**Dispatch-ID**: 20260406-330002-agent-os-project-plan-A

---

## 1. Executive Summary

VNX is evolving from a coding-first orchestration system into a unified Agent OS — a stateful orchestration and continuity system that supports multiple domains (coding, business, regulated) under one shared substrate with domain-specific governance profiles.

### Current State (2026-04-06)

The headless subprocess pipeline is **operationally proven**:

- **SubprocessAdapter** (`scripts/lib/subprocess_adapter.py`): spawns `claude -p --output-format stream-json` workers, replaces tmux send-keys for headless execution
- **EventStore** (`scripts/lib/event_store.py`): NDJSON persistence per terminal with file locking, archive-on-clear, tail-with-since
- **SSE endpoint** (`dashboard/api_agent_stream.py`): Server-Sent Events streaming from EventStore to browser
- **Dashboard agent stream** (`dashboard/token-dashboard/app/agent-stream/page.tsx`): real-time event viewer with terminal selector, pause/resume, auto-reconnect
- **Event type normalization**: CLI types (`system`, `assistant`, `user`) mapped to dashboard types (`init`, `thinking`, `tool_use`, `tool_result`, `text`, `result`)
- **Burn-in**: 12/12 scenarios PASS, 3 bugs found and fixed, 284 archived events across 11 dispatch archives
- **Playwright E2E**: automated browser tests against live dashboard

All merged on `main` via PRs #179-#184.

### What's Next

Five features (F31-F35) transform this infrastructure into an autonomous headless worker system where T1 operates as a pure backend-developer agent, dispatches produce receipts, and the dashboard shows domain-filtered pipeline progress.

---

## 2. Architecture

### Unified System: One Substrate, Multiple Domains

```
┌─────────────────────────────────────────────────┐
│         Domain Layer (coding, business, ...)     │
│  - Domain-specific skills, gates, scoping        │
│  - Domain governance profile                     │
│  - Domain-specific tooling (PR, worktree, ...)   │
├─────────────────────────────────────────────────┤
│         Substrate Layer (reusable)               │
│  - Manager/worker orchestration                  │
│  - Dispatch, lease, receipt pipeline             │
│  - Session lifecycle, event stream               │
│  - Open items, carry-forward, intelligence       │
│  - Capability profiles, governance enforcement   │
│  - Runtime adapter interface                     │
├─────────────────────────────────────────────────┤
│         Transport Layer                          │
│  - TmuxAdapter, SubprocessAdapter                │
│  - Process lifecycle, pane management            │
│  - Provider CLI integration                      │
└─────────────────────────────────────────────────┘
```

### Contracts

| Contract | Path | Status |
|----------|------|--------|
| 3-Layer Architecture | `docs/AGENT_OS_LIFT_IN_CONTRACT.md` | Canonical |
| Business-Light Governance | `docs/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` | Canonical |
| Runtime Adapter | `docs/RUNTIME_ADAPTER_CONTRACT.md` | Canonical |

---

## 3. What's Built — Working Component Inventory

### Subprocess Pipeline (F28-F29, merged)

| Component | File | Lines | Status |
|-----------|------|-------|--------|
| SubprocessAdapter | `scripts/lib/subprocess_adapter.py` | ~430 | Production — spawn, deliver, read_events, stop, health, session_health, shutdown |
| EventStore | `scripts/lib/event_store.py` | ~200 | Production — append, tail, clear, archive, event_count, last_event |
| Event type normalization | `scripts/lib/subprocess_adapter.py:_normalize_cli_event()` | ~80 | Production — maps CLI types to dashboard types |
| SSE endpoint | `dashboard/api_agent_stream.py` | ~100 | Production — stream + status handlers |
| Agent stream page | `dashboard/token-dashboard/app/agent-stream/page.tsx` | ~405 | Production — real-time viewer |
| Subprocess dispatch CLI | `scripts/lib/subprocess_dispatch.py` | ~100 | Production — CLI wrapper for SubprocessAdapter |
| Adapter types | `scripts/lib/adapter_types.py` | ~150 | Production — shared RuntimeAdapter protocol |

### Runtime Adapter Protocol

| Adapter | File | Capabilities |
|---------|------|-------------|
| TmuxAdapter | `scripts/lib/tmux_adapter.py` | spawn, stop, deliver, observe, health, attach, inspect |
| SubprocessAdapter | `scripts/lib/subprocess_adapter.py` | spawn, stop, deliver, observe, health, session_health |

### Dispatch & Governance

| Component | File | Role |
|-----------|------|------|
| Dispatcher | `scripts/dispatcher_v8_minimal.sh` | Shell dispatch orchestration |
| Dispatch delivery | `scripts/dispatch_deliver.sh` | Routes to tmux or subprocess based on `VNX_ADAPTER_T{n}` |
| Receipt pipeline | `scripts/append_receipt.py` | NDJSON receipt storage |
| Lease manager | `scripts/lib/lease_manager.py` | Terminal lease lifecycle |
| Runtime coordination | `scripts/lib/runtime_coordination.py` | Coordination DB, state management |

### Dashboard

| Page | Path | Function |
|------|------|----------|
| Kanban board | `dashboard/token-dashboard/app/kanban/page.tsx` | PR pipeline visualization |
| Agent stream | `dashboard/token-dashboard/app/agent-stream/page.tsx` | Real-time event viewer |
| Token usage | `dashboard/token-dashboard/app/page.tsx` | Token consumption tracking |

### Test Coverage

| Test file | Count | Scope |
|-----------|-------|-------|
| `tests/test_subprocess_adapter.py` | 34 | SubprocessAdapter core (spawn, deliver, stop, health, observe, session_health, shutdown, capabilities) |
| `tests/test_subprocess_adapter_pr3.py` | 29 | StreamEvent parsing, session_id extraction, --resume flag, malformed line handling |
| `tests/test_stream_type_normalization.py` | 19 | CLI-to-dashboard type mapping, multi-block splitting, data field extraction |
| `tests/test_event_store.py` | 19 | Append, tail, clear, archive, concurrent writes, event_count, last_event |
| Total subprocess-related | 101 | All passing |

---

## 4. What's Missing — Gap Analysis

| Gap | Current State | Target State | Feature |
|-----|--------------|--------------|---------|
| T1 is not headless by default | T1 CLAUDE.md references tmux, skill loading | Pure backend-developer agent, subprocess-only | F31 |
| No receipts from subprocess | Events stream but no receipt written to t0_receipts.ndjson | Receipt auto-generated after read_events() completes | F32 |
| Dashboard shows terminals, not agents | Agent stream page uses T1/T2/T3 selector | Agent name selector grouped by domain | F33 |
| No skill context in headless mode | subprocess_dispatch.py passes raw instruction | Skill CLAUDE.md prepended to instruction from agent directory | F34 |
| No full pipeline certification | Individual components tested, not end-to-end pipeline | Dispatch → stream → archive → receipt → gate → dashboard verified | F35 |

---

## 5. Design Decisions

### D1: Managers = short-lived sessions, NOT long-running --resume

**Decision**: T0 sessions are disposable. Use `--resume` until ~85% context pressure, then write a handoff summary and start a fresh session.

**Rationale**: Long-running `--resume` sessions accumulate context rot. Context fills up, quality degrades, and the session becomes unpredictable. Short-lived sessions with explicit state handoff maintain quality.

### D2: Workers = headless subprocess, disposable, fresh context per task

**Decision**: Workers run via `SubprocessAdapter.deliver()` — each dispatch starts a fresh `claude -p` process. No carry-over, no resume by default.

**Rationale**: Clean context per task prevents cross-dispatch contamination. Workers are bounded executors, not persistent actors. The SubprocessAdapter + EventStore pipeline provides full structured observability without requiring persistence.

### D3: Agent = directory with CLAUDE.md

**Decision**: An agent is a directory. `claude -p` reads CLAUDE.md from its cwd. No `/skill` command needed in headless mode.

**Rationale**: This is the proven pattern from the GCP worker daemon plan (`claude_orchestration_layer.md`). Each agent directory contains a focused CLAUDE.md, optional `.mcp.json`, and reference files. Agent isolation is filesystem-level.

### D4: Governance = YAML config profiles, not hardcoded

**Decision**: `coding_strict`, `business_light`, and `regulated_strict` are capability profiles with configurable gates, closure rules, and audit retention.

**Rationale**: Different domains need different governance intensity. Profiles are validated at initialization and determine which substrate features activate. See `docs/AGENT_OS_LIFT_IN_CONTRACT.md` Section 5.

### D5: Dashboard evolves with domain filter tabs and agent selector

**Decision**: Replace terminal selector (T1/T2/T3) with agent name selector grouped by domain. Add domain filter tabs to Kanban board. Add pipeline progress view.

**Rationale**: Operators think in terms of work and agents, not terminal IDs. When workers are headless and numerous, terminal IDs become meaningless — the agent identity and dispatch progress matter.

### D6: Receipts from subprocess write to t0_receipts.ndjson

**Decision**: After `read_events()` completes (process exit), write a receipt to the standard receipt pipeline. Receipt includes dispatch_id, terminal_id, event_count, exit code, session_id, source="subprocess".

**Rationale**: The receipt pipeline is the substrate's acknowledgment mechanism. Without subprocess receipts, T0 has no signal that headless work completed. This closes the dispatch lifecycle loop.

### D7: T1 becomes headless backend-developer

**Decision**: Rewrite `.claude/terminals/T1/CLAUDE.md` as a pure backend-developer (no skill loading, no tmux references). Set `VNX_ADAPTER_T1=subprocess` as the default.

**Rationale**: T1 is the first terminal to go fully headless. Its current CLAUDE.md contains tmux-era instructions that are irrelevant for subprocess execution. A clean agent identity enables autonomous overnight execution.

---

## 6. Governance Profiles

### coding_strict (current)

| Aspect | Setting |
|--------|---------|
| Scope model | Git worktree |
| Review policy | Every PR reviewed by gate |
| Gate requirement | Codex + Gemini required |
| Closure authority | Human only |
| Evidence retention | 30 days |
| Runtime adapter | tmux (primary), subprocess (T1) |
| Worker model | Transitioning to headless |

### business_light (planned)

| Aspect | Setting |
|--------|---------|
| Scope model | Folder |
| Review policy | Review-by-exception |
| Gate requirement | None (opt-in) |
| Closure authority | Manager may close |
| Evidence retention | 14 days |
| Runtime adapter | Headless |
| Worker model | Headless-by-default |

See `docs/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` for full profile.

---

## 7. Dashboard Evolution

### Current State

| View | Function | Selector |
|------|----------|----------|
| Agent Stream | Real-time event viewer | Terminal (T1/T2/T3) |
| Kanban | PR pipeline board | None (all PRs) |
| Token Usage | Token consumption | None |

### Target State

| View | Function | Selector |
|------|----------|----------|
| Agent Stream | Real-time event viewer | Agent name (grouped by domain) |
| Kanban | PR/task pipeline board | Domain filter tabs (Coding, Content, All) |
| Pipeline | Dispatch progress tracker | Linked to dispatch_id, shows step progress |
| Token Usage | Token consumption | Per-domain breakdown |

### Key Changes

1. **Domain filter tabs** on Kanban: Filter by `coding`/`business`/`all`
2. **Agent selector** on Agent Stream: Replace T1/T2/T3 with agent names (e.g., `backend-developer`, `test-engineer`, `blog-writer`)
3. **Pipeline progress**: Link agent stream to dispatch_id, show which step of a multi-step pipeline is executing
4. **`domain` field** added to KanbanCard type and API response

---

## 8. GCP Worker Daemon

The VNX Digital Agent Team runs a GCP-hosted worker daemon for business content production:

- **Architecture**: Telegram intake → Supabase task queue → Python daemon → headless `claude -p` workers
- **Agent isolation**: directory-based, each agent has own CLAUDE.md and `.mcp.json`
- **Pipeline model**: multi-step pipelines (research → write → review → publish) with quality gates
- **Session continuity**: `--resume <session_id>` for feedback loops

**Key connection to VNX core**: The daemon can use `SubprocessAdapter.deliver()` + `read_events()` instead of raw `subprocess.run()`. This provides stream-json events, NDJSON archive per dispatch, session_id extraction, and normalized event types — all built and validated today.

See full plan: external document `claude_orchestration_layer.md`
See project status: external document `HANDOFF.md`

---

## 9. Feature Roadmap: F31-F35

| Feature | Title | PR | Track | Skill | Depends |
|---------|-------|-----|-------|-------|---------|
| F31 | Headless T1 Backend Developer | PR-0 | A | @backend-developer | None |
| F32 | Subprocess Receipt Integration | PR-1 | A | @backend-developer | F31 |
| F33 | Dashboard Domain Filter | PR-2 | A | @frontend-developer | F32 |
| F34 | Skill Context Inlining | PR-3 | A | @backend-developer | F33 |
| F35 | End-to-End Validation + Certification | PR-4 | C | @quality-engineer | F34 |

```
F31 (PR-0) → F32 (PR-1) → F33 (PR-2) → F34 (PR-3) → F35 (PR-4)
```

Each feature is designed for autonomous overnight execution via headless subprocess workers. See `FEATURE_PLAN.md` for detailed scope, deliverables, and success criteria.

---

## 10. Reference Links

### Architecture & Strategy

| Document | Path |
|----------|------|
| Agent OS Strategy Report | `docs/research/agent-os-coding-control-plane-report.md` |
| 3-Layer Architecture Contract | `docs/AGENT_OS_LIFT_IN_CONTRACT.md` |
| Business-Light Governance | `docs/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` |
| F27-F30 Roadmap | `docs/internal/plans/ROADMAP_F27_F30.md` |

### Headless Subprocess (F28-F29)

| Document | Path |
|----------|------|
| Burn-in Test Plan | `docs/internal/plans/HEADLESS_BURNIN_TEST_PLAN.md` |
| Burn-in Results (12/12 PASS) | `docs/internal/plans/HEADLESS_BURNIN_RESULTS.md` |
| SubprocessAdapter | `scripts/lib/subprocess_adapter.py` |
| EventStore | `scripts/lib/event_store.py` |
| SSE Endpoint | `dashboard/api_agent_stream.py` |
| Agent Stream Page | `dashboard/token-dashboard/app/agent-stream/page.tsx` |

### GCP Worker Daemon (VNX Digital)

| Document | Path |
|----------|------|
| Orchestration Layer Plan | External: `claude_orchestration_layer.md` |
| VNX Digital Status | External: `HANDOFF.md` |

### Billing Safety

| Constraint | Enforcement |
|-----------|------------|
| No Anthropic SDK imports | CI lint rule + T3 review audit |
| No `api.anthropic.com` calls | CI lint rule + T3 review audit |
| CLI-only interaction | `subprocess.Popen(["claude", ...])` — verified in burn-in |

See `docs/internal/plans/ROADMAP_F27_F30.md` Section 7 for full billing safety invariant.
