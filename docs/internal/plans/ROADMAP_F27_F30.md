# Roadmap: F27-F30 — Subprocess Migration & Refactor

**Author**: T3 (Architect)
**Date**: 2026-04-04
**Dispatch**: 20260404-195603-roadmap-f27-f30-plan-C
**Status**: Active

---

## 1. Background: Anthropic OAuth Restrictions (April 2026)

### The Policy Change

Anthropic's April 2026 billing update draws a clear line between first-party and third-party usage of Claude:

- **Pro/Max subscriptions** cover only Anthropic's own products: Claude Code (CLI + IDE extensions), claude.ai, and Cowork.
- **Third-party tools** that authenticate via OAuth and route requests through `api.anthropic.com` are classified as "extra usage" and billed separately.
- The distinction hinges on *how* the tool accesses Claude — direct API calls using subscription-backed OAuth tokens trigger extra-usage billing.

### VNX's Position

VNX is **not a third-party harness**. This was verified by codebase audit (ref: `vnx_anthropic_billing_audit.pdf`, April 2026):

| Audit Question | Finding |
|----------------|---------|
| Does any code call Anthropic OAuth endpoints? | **NO** |
| Does any code call `api.anthropic.com` using subscription credentials? | **NO** |
| Does it only launch `claude` CLI processes? | **YES** |
| Are there HTTP clients targeting Anthropic endpoints? | **NO** |

VNX exclusively spawns official `claude` CLI processes via subprocess and tmux. The codebase contains:
- Zero direct API calls to Anthropic
- Zero Anthropic SDK imports (`anthropic`, `@anthropic-ai/sdk`)
- Zero OAuth token handling or credential management

### The Hard Constraint

This billing policy creates an architectural invariant that must hold across all future features:

> **VNX must NEVER import the Anthropic SDK, call `api.anthropic.com` directly, or handle Anthropic OAuth tokens. All interaction with Claude must go through the official `claude` CLI binary.**

Violating this constraint would reclassify VNX as a third-party harness, triggering extra-usage billing for every operator using it. This is a non-negotiable business constraint, not a technical preference.

---

## 2. Architectural Decisions Driven by This Constraint

The subprocess migration was evaluated against four alternatives. Each was rejected for specific, documented reasons.

### Rejected: ACP/ACPX (Agent Communication Protocol)

- **What it is**: Anthropic's protocol for agent-to-agent communication.
- **Why rejected**: Unverified whether ACP implementations route through the Anthropic API directly. Until Anthropic publishes clear billing guidance for ACP-based orchestration, the billing risk is unacceptable.
- **Reassess when**: Anthropic publishes ACP billing classification or provides a compliance audit path.

### Rejected: Claude Agent SDK (Python)

- **What it is**: `claude_agent_sdk` — Python library for building agents that use Claude.
- **Why rejected**: Imports `anthropic` SDK internally. Makes direct API calls to `api.anthropic.com`. Using it would make VNX a third-party harness by definition.
- **Verdict**: Hard no. This is exactly what the billing policy targets.

### Rejected: MCP-based T0 Orchestration

- **What it is**: Running T0 as an MCP server that workers connect to for dispatch coordination.
- **Why rejected**: Adds an abstraction layer that solves no real problem. T0 already calls Python CLI scripts via the Bash tool. MCP would add protocol overhead, a server process, and connection management — all for the same result.
- **Verdict**: Over-engineering. T0's current Bash-based orchestration is simple and effective.

### Chosen: SubprocessAdapter

- **What it is**: Replace tmux `send-keys` with `subprocess.Popen(["claude", "-p", instruction, "--model", model])`.
- **Why chosen**: Same CLI binary, same billing profile, no new dependencies. Workers run as isolated OS processes instead of tmux panes. Process lifecycle is managed by the OS, not by tmux session state.
- **Billing impact**: None. Still launching `claude` CLI — the same binary the operator uses manually.

### Chosen: Claude Code SDK for Event Streaming

- **What it is**: `@anthropic-ai/claude-code-sdk` (Node.js) — programmatic interface to spawn and interact with `claude` CLI processes.
- **Why chosen**: Provides structured event streaming (thinking, tool calls, results) from CLI processes. No Anthropic SDK import — it wraps the CLI binary, not the API.
- **Use case**: Dashboard agent stream (F29). Real-time visibility into worker output without tmux capture-pane scraping.
- **Billing impact**: None. The SDK spawns `claude` CLI processes. It does not call the API directly.

---

## 3. What We Gain (Improvements Over tmux)

Moving from tmux-based terminal management to subprocess execution resolves a class of operational failures that have plagued VNX since inception.

### Eliminated Failure Modes

| tmux Problem | Subprocess Solution |
|-------------|---------------------|
| Stale pane IDs / `panes.json` discovery failures | No pane IDs. Process PIDs from `Popen`. |
| Input-mode probing / `/clear` failures (ref: T3 feedback memory) | No input mode. Instruction passed via `-p` flag at process start. |
| `tmux capture-pane` scraping for output | `stdout` pipe from subprocess. Structured events via SDK. |
| Cross-pane contamination | Process isolation. Each worker is a separate OS process. |
| Tmux session state corruption | No session state. Each dispatch starts a fresh process. |
| Pane-still-running detection heuristics | `process.poll()` / `process.returncode`. Deterministic. |

### New Capabilities

- **Process isolation per terminal**: Each worker runs in its own process with its own environment. No shared tmux session state.
- **Clean context per dispatch**: New process = fresh Claude Code context. No carry-over from previous dispatches. No need for `/clear`.
- **Structured event streaming**: Claude Code SDK emits typed events (thinking, tool_use, result). No regex parsing of captured pane output.
- **Dashboard as primary surface**: With subprocess stdout piped to SSE, the dashboard can show real-time agent output. Operators no longer need to `tmux attach` to see what workers are doing.
- **Deterministic lifecycle**: `SIGTERM` → `SIGKILL` escalation for hung processes. No more "is this pane still alive?" heuristics.

---

## 4. What Remains Limited (Honest Constraints)

CLI-only execution comes with real limitations. These are accepted trade-offs, not problems to solve.

### Context Injection

- Context can only be injected via `CLAUDE.md` files and the `-p` (prompt) flag.
- No custom system prompts via API (not needed — terminal CLAUDE.md files serve this purpose).
- No mid-session context modification (not needed — each dispatch is a fresh process).

### Model Configuration

- Model is set at process start via `--model` flag. Cannot switch mid-session.
- This is fine — VNX has never needed mid-session model switching.
- No custom API parameters (temperature, top_p, etc.). CLI defaults are sufficient for all current use cases.

### Process Lifecycle

- Crashes and hangs require external monitoring (the supervisor must poll `process.poll()`).
- This is strictly better than tmux pane lifecycle management but still requires active supervision.
- The `workflow_supervisor.py` (already 390 lines) will need subprocess-aware stall detection.

### CLI Interface Stability

- VNX depends on `claude` CLI flags: `-p`, `--model`, `--output-format`, `--max-turns`.
- If Anthropic changes CLI flags in a breaking way, subprocess code breaks.
- Mitigation: Pin to known-good CLI versions. Test flag compatibility in CI.

### Streaming Granularity

- No token-level streaming in VNX's own code.
- Claude Code SDK provides event-level streaming (thinking blocks, tool calls, results), which is sufficient for dashboard visibility.
- Token-level rendering is handled by the dashboard's SSE consumer if needed.

---

## 5. Feature Plan Overview

### F27: Batch Refactor (Resolve Blocker Open Items)

**Goal**: Resolve all 57 blocker open items — file-level and function-level size violations that block further feature work.

**Scope**:

| File | Current Lines | Target | Action |
|------|--------------|--------|--------|
| `scripts/dispatcher_v8_minimal.sh` | 2,140 | < 500 per module | Decompose into dispatch, delivery, lifecycle, logging modules |
| `scripts/lib/runtime_coordination.py` | 1,164 | < 400 per module | Extract lease manager, state machine, coordination DB modules |
| `scripts/review_gate_manager.py` | 1,017 | < 400 per module | Extract gate execution, result parsing, report generation modules |
| Function-level violations | ~30 functions > 80 lines | < 80 lines each | Split into focused subfunctions |

**Approach**: Decompose oversized files into focused modules. Each module gets a clear responsibility boundary. Existing imports are updated. No behavioral changes — pure structural refactor.

**Complexity**: L (Large)
**PRs**: 5 (one per major file + function-level sweep)
**Dependencies**: None
**Risk**: Merge conflicts if other features touch these files concurrently. Mitigated by doing F27 first.

---

### F28: SubprocessAdapter (tmux to subprocess)

**Goal**: Replace tmux-based terminal management with subprocess-based execution.

**Architecture**:

```
Dispatcher
    │
    ▼
SubprocessAdapter (implements RuntimeAdapter protocol)
    │
    ├── spawn(terminal, instruction, model) → Process
    ├── poll(process) → ProcessState
    ├── read_output(process) → str
    ├── terminate(process) → None
    └── kill(process) → None
    │
    ▼
subprocess.Popen(["claude", "-p", instruction, "--model", model])
```

**Key Constraint**: CLI-ONLY. The SubprocessAdapter must never import the Anthropic SDK, make HTTP requests to Anthropic endpoints, or handle OAuth tokens. It wraps `subprocess.Popen` around the `claude` binary — nothing more.

**Key Files**:

| File | Action |
|------|--------|
| `scripts/lib/adapter_types.py` | NEW — Extract shared types from `tmux_adapter.py` |
| `scripts/lib/subprocess_adapter.py` | NEW — `SubprocessAdapter` implementing `RuntimeAdapter` |
| `scripts/lib/runtime_facade.py` (184L) | MODIFY — Add subprocess adapter selection |
| `scripts/dispatcher_v8_minimal.sh` (post-F27) | MODIFY — Route dispatch delivery through subprocess |
| `scripts/lib/tmux_adapter.py` (798L) | RETAIN — Backward compatibility, not deleted |

**Prerequisite**: Extract shared types (`RuntimeAdapter` protocol, `ProcessState`, `TerminalInfo`) from `tmux_adapter.py` into `adapter_types.py`. This decouples the protocol definition from the tmux implementation, allowing `SubprocessAdapter` to implement it independently.

**Complexity**: XL (Extra Large)
**PRs**: 5
**Dependencies**: F27 (dispatcher must be decomposed before modifying dispatch delivery)
**Risk**: Behavioral divergence between tmux and subprocess paths during transition. Mitigated by running both in parallel during burn-in.

---

### F29: Dashboard Agent Stream

**Goal**: Real-time agent output visibility in the dashboard via Server-Sent Events (SSE).

**Data Flow**:

```
SubprocessAdapter
    │ stdout pipe
    ▼
Event Store (NDJSON files per terminal)
    │ tail -f
    ▼
SSE Endpoint (dashboard/api_operator.py)
    │ EventSource
    ▼
Browser (app/agent-stream/page.tsx)
    │ render
    ▼
Real-time thinking/tool-call/result display
```

**Key Files**:

| File | Action |
|------|--------|
| `dashboard/api_operator.py` | MODIFY — Add `/api/agent-stream/{terminal}` SSE endpoint |
| `dashboard/token-dashboard/app/agent-stream/page.tsx` | NEW — Agent stream viewer page |
| `scripts/lib/subprocess_adapter.py` | MODIFY — Write structured events to NDJSON store |
| `scripts/lib/event_store.py` | NEW — NDJSON event persistence and tailing |

**Complexity**: M (Medium)
**PRs**: 4
**Dependencies**: F28 (subprocess stdout pipe must exist)
**Risk**: SSE connection management (reconnection, backpressure). Standard patterns apply.

---

### F30: Full tmux Elimination — EXPLICITLY NOT PLANNED

**Status**: DEFERRED

**What it would do**:
- Remove all 21 tmux-dependent files from the codebase
- Delete tmux from session startup (`bin/vnx start`)
- Dashboard replaces all terminal observation operations
- Remove tmux from system requirements

**Why deferred**:
1. **F28 provides backward compatibility** — tmux and subprocess adapters coexist behind the `RuntimeAdapter` protocol. Operators can fall back to tmux if subprocess has issues.
2. **Eliminating tmux is irreversible** — once removed, rolling back requires re-implementing the tmux adapter. This is high-risk for no immediate gain.
3. **Requires full dashboard feature parity** — the dashboard must handle everything operators currently do via `tmux attach`: viewing output, sending input, managing sessions. F29 covers viewing but not input or session management.
4. **Risk/reward ratio is unfavorable now** — tmux works. It's ugly, but it works. Removing it provides cleanliness, not capability.

**Prerequisite for future consideration**: F28 + F29 fully stable in production for at least 2 weeks with zero fallbacks to tmux.

---

## 6. Sequencing and Dependencies

```
F27 (batch refactor)        F28 (subprocess adapter)        F29 (agent stream)
━━━━━━━━━━━━━━━━━━━ ──────→ ━━━━━━━━━━━━━━━━━━━━━━━ ──────→ ━━━━━━━━━━━━━━━━━━━
 Resolve 57 blocker OIs      SubprocessAdapter + dispatch     SSE + dashboard view
 5 PRs, complexity L         5 PRs, complexity XL             4 PRs, complexity M
 No dependencies              Depends on F27                   Depends on F28


                             F30 (tmux elimination)
                             ━━━━━━━━━━━━━━━━━━━━━━
                              DEFERRED
                              Depends on F28 + F29
                              + 2 weeks stable production
```

**Critical path**: F27 → F28 → F29 (serial dependency chain)

**Total PRs**: 14 (5 + 5 + 4), with F30 deferred indefinitely.

**Estimated timeline**: Not provided (per project guidelines — focus on what, not when).

---

## 7. Billing Safety Invariant

The following 4-question audit must pass after every feature merge, for every PR, with no exceptions:

### The Audit

| # | Question | Required Answer |
|---|----------|-----------------|
| 1 | Does any code call Anthropic OAuth endpoints? | **NO** |
| 2 | Does any code call `api.anthropic.com` using subscription credentials? | **NO** |
| 3 | Does it only launch `claude` CLI processes? | **YES** |
| 4 | Are there HTTP clients targeting Anthropic endpoints? | **NO** |

### Enforcement

- **Pre-merge**: T3 (code review) runs a grep-based audit for Anthropic SDK imports, API endpoint strings, and OAuth token handling in every PR review.
- **CI**: Add a lint rule that fails on `import anthropic`, `from anthropic`, `api.anthropic.com`, and `oauth.anthropic.com` patterns in Python and JavaScript files.
- **Post-merge**: Periodic full-codebase audit (same 4 questions) as part of release checklist.

### What Triggers a Blocker

Any PR that introduces:
- `import anthropic` or `from anthropic import` in any Python file
- `require('anthropic')` or `import ... from 'anthropic'` in any JS/TS file
- HTTP requests to `*.anthropic.com` endpoints (excluding documentation links)
- OAuth token storage, refresh, or exchange logic for Anthropic credentials

is an **automatic blocker** — no exceptions, no workarounds, no "we'll fix it later."

---

## Appendix: Key File Inventory

### Files to Refactor (F27)

| File | Lines | Violation |
|------|-------|-----------|
| `scripts/dispatcher_v8_minimal.sh` | 2,140 | > 500L file limit |
| `scripts/lib/runtime_coordination.py` | 1,164 | > 400L file limit |
| `scripts/review_gate_manager.py` | 1,017 | > 400L file limit |
| + ~30 functions across codebase | > 80L each | Function size limit |

### Files to Create (F28)

| File | Purpose |
|------|---------|
| `scripts/lib/adapter_types.py` | Shared types extracted from tmux_adapter |
| `scripts/lib/subprocess_adapter.py` | SubprocessAdapter implementation |

### Files to Create (F29)

| File | Purpose |
|------|---------|
| `scripts/lib/event_store.py` | NDJSON event persistence |
| `dashboard/token-dashboard/app/agent-stream/page.tsx` | Dashboard stream viewer |

### tmux-Dependent Files (F30 scope, deferred)

21 files with "tmux" in the filename exist across the codebase, including:
- `scripts/lib/tmux_adapter.py` (798L) — primary adapter
- `scripts/lib/tmux_session_profile.py` — session configuration
- `scripts/tmux_adapter_cli.py` — CLI wrapper
- `tests/test_tmux_adapter.py` — adapter tests
- `tests/test_tmux_adapter_interface.py` — interface tests
- `tests/test_tmux_session_profile.py` — profile tests

These files are retained in F28 for backward compatibility and only considered for removal in F30.
