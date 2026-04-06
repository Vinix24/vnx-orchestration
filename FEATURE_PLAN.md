# Feature: Agent OS Headless Worker Pipeline

**Feature-ID**: Feature 31-35
**Status**: Planned
**Priority**: P1
**Branch**: `feature/agent-os-headless-pipeline`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: codex_gate,claude_github_optional

Primary objective:
Transform the proven headless subprocess infrastructure (F28-F29, burn-in 12/12 PASS) into an autonomous worker pipeline where T1 operates as a pure headless backend-developer, dispatches produce receipts, skill context is injected at runtime, and the dashboard provides domain-filtered pipeline visibility.

Execution context:
- builds on merged PRs #179-#184: SubprocessAdapter, EventStore, SSE endpoint, agent stream page, event type normalization, Playwright E2E
- SubprocessAdapter burn-in validated 12/12 scenarios including parallel dispatch, long-running tasks, model variations, and session resume
- 101 unit tests cover SubprocessAdapter, StreamEvent parsing, event type normalization, and EventStore
- billing safety invariant preserved: CLI-only, no Anthropic SDK, no API calls

Execution preconditions:
- all PRs from F28-F29 merged on main (confirmed 2026-04-06)
- `VNX_ADAPTER_T1=subprocess` environment variable available
- dashboard runs on localhost:3100 (Next.js) with Python API on localhost:4173

Review gate policy:
- Codex gate required on every PR
- every PR must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged on main

## Problem Statement

The headless subprocess pipeline captures events and streams them to the dashboard, but the pipeline is incomplete:
- T1 still operates with tmux-era CLAUDE.md instructions
- no receipts are generated from subprocess execution — T0 has no completion signal
- the dashboard shows terminal IDs (T1/T2/T3) instead of agent identities and domain context
- headless workers receive raw instructions without skill-specific context (CLAUDE.md from agent directories)
- no end-to-end certification proves the full pipeline: dispatch → stream → archive → receipt → gate → dashboard

## Design Goal

Create an autonomous headless worker pipeline that can execute overnight with full observability, receipt generation, skill context injection, and domain-aware dashboard visibility — all without human intervention.

## Non-Goals

- no tmux removal (tmux adapter retained for backward compatibility)
- no business_light domain activation (governance profile defined but gated)
- no remote worker execution (local subprocess only)
- no Anthropic SDK usage (CLI-only invariant)
- no multi-provider support (Claude Code CLI only)

## Delivery Discipline

- each PR is 150-300 lines and independently deployable
- each PR must have a GitHub PR before merge
- conventional commits: `feat|fix|test|refactor(<scope>): <description>`
- every change verified by existing 101-test suite plus new tests per PR
- billing safety audit on every PR (no Anthropic SDK imports, no API calls)

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Headless T1 Backend Developer (F31)
**Track**: A
**Priority**: P1
**Complexity**: Small
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: []

### Description
Convert T1 from a tmux-era interactive terminal to a pure headless backend-developer agent. Remove skill loading commands, tmux references, and interactive assumptions from T1's CLAUDE.md. Update T0 dispatch instructions for headless T1 delivery.

### Scope
- rewrite `.claude/terminals/T1/CLAUDE.md` as a pure backend-developer agent identity — no `/skill` commands, no tmux references, no interactive assumptions
- update `subprocess_dispatch.py` to accept and inject skill CLAUDE.md path into the prompt preamble
- set `VNX_ADAPTER_T1=subprocess` as the documented default for T1 in CLAUDE.md root instructions
- update T0 orchestrator instructions in `.claude/terminals/T0/CLAUDE.md` to reference headless T1 dispatch path
- verify: dispatch a task to T1 via subprocess → events streamed to EventStore → archived on completion

### Deliverables
- rewritten `.claude/terminals/T1/CLAUDE.md` (pure backend-developer, no tmux)
- updated T0 dispatch instructions for headless T1
- `VNX_ADAPTER_T1=subprocess` default documented
- test: dispatch via subprocess_dispatch.py produces events + archive
- GitHub PR

### Success Criteria
- T1 CLAUDE.md contains zero tmux references and zero `/skill` commands
- a headless dispatch to T1 produces structured events in EventStore
- events are archived on next dispatch
- existing 101 tests still pass
- T0 instructions reference headless T1 path

### Quality Gate
`gate_pr0_headless_t1_backend_developer`:
- [ ] T1 CLAUDE.md rewritten as pure backend-developer identity
- [ ] Zero tmux references in T1 CLAUDE.md
- [ ] VNX_ADAPTER_T1=subprocess documented as default
- [ ] Dispatch to T1 via subprocess produces events in EventStore
- [ ] Events archived on subsequent dispatch
- [ ] T0 instructions updated for headless T1
- [ ] All existing tests pass
- [ ] GitHub PR exists

---

## PR-1: Subprocess Receipt Integration (F32)
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
After `read_events()` completes (subprocess exits), automatically write a receipt to `t0_receipts.ndjson`. This closes the dispatch lifecycle loop — T0 gets a completion signal from headless workers.

### Scope
- add receipt generation in `subprocess_adapter.py` after `read_events()` loop completes
- receipt format: `dispatch_id`, `terminal_id`, `status` (success/failure based on exit code), `event_count`, `session_id`, `source="subprocess"`, `last_event_type`, `timestamp`
- expose completion status from SubprocessAdapter: exit code, event count, last event type
- add `subprocess_dispatch.py` integration: call receipt generation after read_events completes
- write receipt to the standard receipt pipeline path (`$VNX_DATA_DIR/receipts/t0_receipts.ndjson`)
- tests for success receipt (exit 0, result event), failure receipt (non-zero exit), and edge case (no events)

### Deliverables
- receipt generation in `subprocess_adapter.py` or `subprocess_dispatch.py`
- receipt format spec and implementation
- completion status exposure (exit code, event count)
- tests for success + failure + edge case receipts
- GitHub PR

### Success Criteria
- every completed subprocess dispatch writes exactly one receipt to t0_receipts.ndjson
- receipt contains dispatch_id, terminal_id, status, event_count, session_id, source
- success receipt written when exit code is 0 and last event type is `result`
- failure receipt written when exit code is non-zero or no result event
- receipt is parseable by the existing receipt processor
- all existing tests pass plus new receipt tests

### Quality Gate
`gate_pr1_subprocess_receipt_integration`:
- [ ] Receipt written to t0_receipts.ndjson after subprocess completion
- [ ] Receipt contains all required fields (dispatch_id, terminal_id, status, event_count, session_id, source)
- [ ] Success and failure receipts tested
- [ ] Receipt parseable by existing receipt processor
- [ ] All existing tests pass
- [ ] GitHub PR exists

---

## PR-2: Dashboard Domain Filter (F33)
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-1]

### Description
Add domain awareness to the dashboard. The Kanban board gets domain filter tabs (Coding, Content, All). The Agent Stream page replaces terminal selectors (T1/T2/T3) with agent name selectors grouped by domain. The stream is linked to dispatch_id with pipeline progress.

### Scope
- add `domain` field to KanbanCard TypeScript type in `dashboard/token-dashboard/app/kanban/page.tsx`
- add `domain` field to the Kanban API response in `dashboard/api_kanban.py` (or equivalent)
- add sidebar domain filter tabs to Kanban page: Coding, Content, All (default)
- agent stream page: replace terminal selector (T1/T2/T3) with agent name selector grouped by domain
- agent names derived from dispatch metadata or EventStore events (e.g., `backend-developer`, `test-engineer`)
- stream linked to dispatch_id — display current dispatch_id and pipeline step if available
- agent stream page: show dispatch_id in status bar

### Deliverables
- `domain` field on KanbanCard type and API
- domain filter tabs on Kanban page
- agent name selector on Agent Stream page (grouped by domain)
- dispatch_id display in stream status bar
- GitHub PR

### Success Criteria
- Kanban page shows domain filter tabs that filter cards by domain
- Agent Stream page shows agent names instead of T1/T2/T3
- Agent names are grouped by domain (Coding, Content)
- Current dispatch_id is visible in the stream status bar
- all existing Playwright E2E tests pass or are updated
- no regression in existing dashboard functionality

### Quality Gate
`gate_pr2_dashboard_domain_filter`:
- [ ] Domain filter tabs render on Kanban page
- [ ] Kanban cards filter by domain selection
- [ ] Agent Stream shows agent names grouped by domain
- [ ] Dispatch ID visible in stream status bar
- [ ] No dashboard regressions
- [ ] GitHub PR exists

---

## PR-3: Skill Context Inlining (F34)
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-2]

### Description
Enable headless workers to receive skill-specific context by reading the agent directory's CLAUDE.md and prepending it to the dispatch instruction. This gives each headless worker a focused agent identity without requiring `/skill` commands.

### Scope
- `subprocess_dispatch.py`: read skill CLAUDE.md from agent directory, prepend to instruction as context preamble
- agent directory resolution: role name → `.claude/skills/{role}/CLAUDE.md` or `agents/{role}/CLAUDE.md` (check both, prefer `.claude/skills/`)
- set cwd to agent directory for the `claude -p` subprocess (so CLAUDE.md is auto-loaded by CLI)
- fallback: if no agent directory exists, dispatch proceeds without skill context (backward compatible)
- `SubprocessAdapter.deliver()`: accept optional `cwd` parameter for agent directory
- verify: headless dispatch with agent role produces skill-aware behavior (e.g., backend-developer follows coding conventions)
- tests: agent dir resolution, CLAUDE.md inlining, cwd override, missing agent dir fallback

### Deliverables
- agent directory resolution logic in `subprocess_dispatch.py`
- CLAUDE.md context inlining (prepend to instruction or set cwd)
- `SubprocessAdapter.deliver()` cwd parameter
- tests for resolution, inlining, and fallback
- GitHub PR

### Success Criteria
- headless dispatch with role `backend-developer` resolves to `.claude/skills/backend-developer/CLAUDE.md`
- CLAUDE.md content is either prepended to instruction or loaded via cwd
- missing agent directory does not break dispatch (graceful fallback)
- skill-specific behavior observable in event stream (agent follows skill instructions)
- all existing tests pass plus new resolution/inlining tests

### Quality Gate
`gate_pr3_skill_context_inlining`:
- [ ] Agent directory resolution works for `.claude/skills/{role}/`
- [ ] CLAUDE.md context available to headless worker
- [ ] Missing agent directory falls back gracefully
- [ ] Skill-specific behavior verified in event stream
- [ ] All existing tests pass
- [ ] GitHub PR exists

---

## PR-4: End-to-End Validation + Certification (F35)
**Track**: C
**Priority**: P1
**Complexity**: Large
**Risk**: Medium
**Skill**: @quality-engineer
**Requires-Model**: sonnet
**Dependencies**: [PR-3]

### Description
Full pipeline certification: headless dispatch → event stream → archive → receipt → gate → dashboard. Verifies every evidence surface. Closes all open items from F31-F34. Produces a certification report.

### Scope
- full pipeline test: dispatch task to headless T1 with skill context → verify events in EventStore → verify archive created → verify receipt in t0_receipts.ndjson → verify dashboard displays events
- Playwright E2E: navigate to Agent Stream → verify events render with correct types → verify agent name selector → verify dispatch_id in status bar → verify domain filter on Kanban
- archive integrity: verify all archives from F31-F34 exist and contain valid NDJSON
- receipt pipeline: verify receipts from F32 are parseable and contain correct metadata
- billing safety audit: grep codebase for Anthropic SDK imports, API endpoints, OAuth tokens
- open items sweep: review all open items from F31-F34, close with evidence or escalate
- certification report: document all evidence surfaces, test results, and open items

### Deliverables
- full pipeline integration test script
- Playwright E2E tests for domain filter, agent selector, dispatch_id display
- archive integrity verification
- receipt pipeline verification
- billing safety audit results
- open items closure evidence
- certification report in `docs/internal/plans/AGENT_OS_CERTIFICATION.md`
- GitHub PR

### Success Criteria
- full pipeline runs end-to-end without manual intervention
- all Playwright E2E tests pass
- all archives contain valid NDJSON with correct dispatch_ids
- all receipts contain correct metadata and are parseable
- billing safety audit passes (zero Anthropic SDK imports, zero API calls)
- all open items from F31-F34 closed or escalated with evidence
- certification report documents every evidence surface

### Quality Gate
`gate_pr4_e2e_certification`:
- [ ] Full pipeline test passes: dispatch → stream → archive → receipt → dashboard
- [ ] Playwright E2E tests pass for domain filter, agent selector, dispatch_id
- [ ] Archive integrity verified (valid NDJSON, correct dispatch_ids)
- [ ] Receipt pipeline verified (parseable, correct metadata)
- [ ] Billing safety audit passes
- [ ] All F31-F34 open items closed or escalated
- [ ] Certification report written
- [ ] GitHub PR exists
