# Feature: One-Click Terminal Startup And Session Control

**Feature-ID**: Feature 26
**Status**: Planned
**Priority**: P1
**Branch**: `feature/one-click-terminal-startup-and-session-control`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Enable operators to start a full development session (2x2 tmux panes T0-T3) or a business folder terminal with one click from the dashboard, and to stop or attach to sessions from the same surface.

Execution context:
- starting a dev session currently requires manual tmux setup and multiple script invocations
- business folders need simpler single-terminal launch
- the dashboard has POST endpoint stubs for session/start and session/stop but they are not implemented
- governance_profile_selector.py (F20) can detect project type (coding vs business)

Review gate policy:
- Gemini via Vertex AI required on every PR
- Codex disabled (usage expired)

## Problem Statement

Starting a VNX development session requires manual tmux window creation, terminal splitting, and script execution. Business folder work needs a simpler single-terminal setup. Both should be one-click from the operator dashboard.

## Design Goal

Implement session lifecycle actions (start, stop, attach) that create the appropriate terminal layout based on the project's governance profile and expose them as buttons on the dashboard project cards.

## Non-Goals

- no automatic dispatch execution after session start
- no remote session management (local tmux only)
- no terminal output streaming in dashboard (future feature)

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Terminal Startup And Session Control Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Dependencies**: []

### Scope
- define startup profiles: dev (2x2 tmux: T0/T1/T2/T3) and business (single terminal)
- define session naming convention (vnx-<project-name>)
- define POST /api/operator/session/start request/response shape
- define POST /api/operator/session/stop and terminal/attach shapes
- define safe-action A8 (terminal launch) with dry-run support
- define session state model (running/stopped/degraded)

### Quality Gate
`gate_pr0_terminal_startup_contract`:
- [ ] Contract defines startup profiles for dev and business
- [ ] Contract defines session naming and tmux layout
- [ ] Contract defines all session action API shapes
- [ ] Contract defines dry-run support
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-1: Session Start Implementation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Scope
- create scripts/lib/dashboard_actions.py with start_session()
- detect project type from governance_profile_selector.py (coding_strict → 2x2, business_light → single)
- invoke tmux new-session and split-window commands for dev layout
- invoke tmux new-session for business layout
- support dry_run=True returning planned actions without executing
- add tests for session creation logic (mock tmux commands)

### Quality Gate
`gate_pr1_session_start`:
- [ ] start_session() creates 2x2 layout for dev projects under test
- [ ] start_session() creates single terminal for business under test
- [ ] dry_run returns plan without side effects under test
- [ ] Profile detection from governance_profile_selector works
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-2: Session Stop And Attach
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-1]

### Scope
- add stop_session() to dashboard_actions.py (kills tmux session by name)
- add attach_terminal() (switches tmux client focus to terminal pane)
- implement POST handlers in serve_dashboard.py for session/start, session/stop, terminal/attach
- wire handlers to dashboard_actions.py functions
- add tests for stop and attach logic

### Quality Gate
`gate_pr2_session_stop_attach`:
- [ ] stop_session() kills tmux session under test
- [ ] attach_terminal() switches focus under test
- [ ] POST handlers return ActionOutcome shape
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-3: Session Control UI
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-2]

### Scope
- add start/stop/attach buttons to project-card.tsx
- show session state indicator on project cards (running=green, stopped=gray)
- wire buttons to POST endpoints via operator-api.ts
- show outcome toasts (success/failure/dry-run result)
- disable buttons during action execution (optimistic UI)
- add tests for button interactions

### Quality Gate
`gate_pr3_session_control_ui`:
- [ ] Start button triggers session creation under test
- [ ] Stop button triggers session teardown under test
- [ ] Session state indicator reflects running/stopped under test
- [ ] Outcome toasts display correctly
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-4: Terminal Startup And Session Control Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Dependencies**: [PR-3]

### Scope
- certify start creates correct tmux layout for dev and business projects
- certify stop tears down session cleanly
- certify dry-run returns plan without side effects
- certify dashboard buttons work end-to-end
- update CHANGELOG.md and PROJECT_STATUS.md

### Quality Gate
`gate_pr4_terminal_startup_certification`:
- [ ] Certification proves dev session creates 2x2 tmux layout
- [ ] Certification proves business session creates single terminal
- [ ] Certification proves dry-run is side-effect-free
- [ ] CHANGELOG.md updated with Feature 26 closeout
- [ ] PROJECT_STATUS.md updated
- [ ] Gemini review receipt exists with no blocking findings
