# Feature: Dashboard Pipeline And Kanban Board

**Feature-ID**: Feature 23
**Status**: Planned
**Priority**: P1
**Branch**: `feature/dashboard-pipeline-and-kanban-board`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Fix the broken operator dashboard data pipeline and add a kanban board view for dispatch and PR lifecycle tracking so operators can see at a glance what is staging, active, in review, and done.

Execution context:
- immediate follow-on after Feature 22 chain pilot completion
- the Next.js dashboard on port 3100 exists but shows spinners because serve_dashboard.py on port 4173 crashes silently on API requests
- the proxy rewrite in next.config.ts is correctly configured but receives empty responses
- the read model (dashboard_read_model.py) implements 5 views per contract but the Python server fails to serve them
- no kanban view exists despite dispatch stage data being available from the queue system

Execution preconditions:
- Feature 22 must be merged on main (complete)
- serve_dashboard.py must be debuggable from the feature branch
- Vertex AI must remain operational for Gemini gates

Review gate policy:
- Gemini headless review is required on every PR in this feature (via Vertex AI)
- Codex is disabled (usage expired)
- every PR must be opened as a GitHub PR before merge consideration

Pilot override (Features 23-26 chain):
- Gemini via Vertex AI is the sole review gate provider
- Codex headless gate is disabled for this chain
- This exception must be recorded in CHAIN_PILOT_23_26_REPORT.md

## Problem Statement

The operator dashboard has two critical problems:
- serve_dashboard.py crashes silently on API requests, returning empty responses that cause the Next.js frontend to show infinite spinners
- there is no kanban view for dispatch or PR lifecycle despite the data existing in the queue system

## Design Goal

Fix the data pipeline so the dashboard loads reliably and add a kanban board that shows dispatches flowing through staging, pending, active, review, and done columns.

## Non-Goals

- no drag-drop reordering of dispatches from the kanban (read-only view first)
- no new analytics or token tracking work
- no session start/attach actions (Feature 26)
- no gate toggle UI (Feature 24)

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- dependent PRs must branch from post-merge main
- final certification must update CHANGELOG.md and PROJECT_STATUS.md

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Dashboard Kanban Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the kanban surface (S6), health endpoint contract, dispatch stage mapping, and error/degraded state handling for the dashboard data pipeline fix and kanban board.

### Scope
- define kanban surface S6 with columns: staging, pending, active, review, done
- define dispatch card data shape (PR-id, track, terminal, gate, duration, status)
- define health endpoint /api/health contract for pipeline liveness
- define error and degraded state rendering for kanban and existing surfaces
- define how dispatches map to kanban stages from queue state

### Deliverables
- dashboard kanban contract document
- health endpoint specification
- dispatch-to-kanban stage mapping rules
- GitHub PR with contract summary

### Success Criteria
- kanban surface is explicitly defined before implementation
- dispatch stage mapping is deterministic
- health endpoint enables frontend to detect backend availability
- error/degraded states are first-class rendering conditions

### Quality Gate
`gate_pr0_dashboard_kanban_contract`:
- [ ] Contract defines kanban surface S6 with 5 columns and card data shape
- [ ] Contract defines /api/health endpoint response format
- [ ] Contract defines dispatch-to-stage mapping from queue state
- [ ] Contract defines error and degraded state rendering
- [ ] GitHub PR exists with contract summary
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Fix Dashboard API Pipeline
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Debug and fix the silent crash in serve_dashboard.py that causes empty API responses. Add /api/health endpoint. Verify all operator GET endpoints return valid JSON.

### Scope
- diagnose why serve_dashboard.py returns empty responses on API requests
- fix the crash so all /api/operator/* GET endpoints return valid JSON with freshness envelopes
- add /api/health endpoint returning server status, uptime, and data source availability
- add /api/operator/kanban endpoint returning dispatch cards grouped by stage
- add tests for health endpoint and kanban data shape

### Deliverables
- fixed serve_dashboard.py with working API responses
- /api/health endpoint
- /api/operator/kanban endpoint
- tests for new endpoints
- GitHub PR with fix evidence

### Success Criteria
- curl localhost:4173/api/health returns 200 with valid JSON
- curl localhost:4173/api/operator/projects returns valid FreshnessEnvelope
- curl localhost:4173/api/operator/kanban returns dispatch cards grouped by stage
- no empty responses on any /api/* endpoint

### Quality Gate
`gate_pr1_dashboard_api_fix`:
- [ ] /api/health returns 200 with server status
- [ ] All /api/operator/* GET endpoints return valid JSON under test
- [ ] /api/operator/kanban returns dispatches grouped by stage under test
- [ ] Empty response crash is identified and fixed with evidence
- [ ] GitHub PR exists with fix evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-2: Kanban Board Frontend
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Build the kanban board page in the Next.js dashboard showing dispatches as cards in 5 columns.

### Scope
- create app/kanban/page.tsx with 5-column CSS grid layout
- each column: staging, pending, active, review, done
- each card shows: PR-id, track badge, terminal, gate name, duration, status
- use SWR hook to poll /api/operator/kanban every 15 seconds
- handle empty columns, loading state, and API error state
- add tests for kanban page rendering

### Deliverables
- kanban page component
- kanban card component
- SWR data hook for kanban
- tests for rendering states
- GitHub PR with kanban screenshots

### Success Criteria
- kanban page renders 5 columns with dispatch cards from live data
- empty columns show placeholder text
- loading state shows skeleton, not spinner
- API error shows degraded banner per contract
- cards are color-coded by track (A=green, B=yellow, C=purple)

### Quality Gate
`gate_pr2_kanban_frontend`:
- [ ] Kanban page renders 5 columns under test
- [ ] Dispatch cards show PR-id, track, terminal, gate, duration under test
- [ ] Empty, loading, and error states render correctly under test
- [ ] SWR hook polls /api/operator/kanban endpoint
- [ ] GitHub PR exists with kanban evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-3: Kanban Integration And Navigation
**Track**: B
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2]

### Description
Add kanban link to sidebar navigation, project filter dropdown to kanban page, and error/degraded banners across operator pages.

### Scope
- add kanban link to sidebar under Operator section
- add project-filter dropdown to kanban page reusing useProjects hook
- add degraded-state banner component reusable across operator pages
- wire degraded banner to freshness envelope degraded field on all operator pages
- add tests for navigation and filtering

### Deliverables
- updated sidebar with kanban link
- project filter on kanban page
- degraded banner component
- tests for filtering and navigation
- GitHub PR with integration evidence

### Success Criteria
- kanban is accessible from sidebar navigation
- project filter shows only dispatches for selected project
- degraded banner appears when API returns degraded=true
- all operator pages show degraded banner when data is stale

### Quality Gate
`gate_pr3_kanban_integration`:
- [ ] Sidebar shows kanban link under Operator section
- [ ] Project filter on kanban filters dispatches by project under test
- [ ] Degraded banner renders when freshness envelope shows degraded=true under test
- [ ] GitHub PR exists with integration evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-4: Dashboard Pipeline And Kanban Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify that the dashboard data pipeline is fixed, kanban board renders correctly, and all operator API endpoints return valid data.

### Scope
- certify /api/health returns 200 and reports data source availability
- certify all /api/operator/* GET endpoints return valid FreshnessEnvelope JSON
- certify kanban board renders with real dispatch data from .vnx-data
- certify project filter and degraded banners work end-to-end
- certify planning docs are updated

### Deliverables
- dashboard pipeline and kanban certification report
- certification tests
- updated CHANGELOG.md and PROJECT_STATUS.md
- GitHub PR with certification verdict

### Success Criteria
- dashboard loads on port 3100 without spinners
- all operator endpoints respond with valid JSON
- kanban shows real dispatch lifecycle
- planning docs reflect post-Feature-23 baseline

### Quality Gate
`gate_pr4_dashboard_kanban_certification`:
- [ ] Certification proves /api/health returns valid status
- [ ] Certification proves all operator endpoints return valid JSON
- [ ] Certification proves kanban renders with real dispatch data
- [ ] CHANGELOG.md updated with Feature 23 closeout
- [ ] PROJECT_STATUS.md updated with Feature 23 status
- [ ] GitHub PR exists with certification verdict
- [ ] Gemini review receipt exists with no unresolved blocking findings
