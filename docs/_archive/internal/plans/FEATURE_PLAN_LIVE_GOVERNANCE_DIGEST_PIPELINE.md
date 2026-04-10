# Feature: Live Governance Digest Pipeline

**Feature-ID**: Feature 25
**Status**: Planned
**Priority**: P1
**Branch**: `feature/live-governance-digest-pipeline`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Connect the Feature 18 governance signal extraction and retrospective digest components to the live intelligence pipeline so operators receive actionable governance digests instead of the useless conversation analyzer output.

Execution context:
- Feature 18 built governance_signal_extractor.py (476 lines) and retrospective_digest.py (391 lines) but neither is connected to any live pipeline
- intelligence_daemon.py runs continuously but only calls old outcome_signals.py
- the operator currently has no governance digest replacing the conversation analyzer
- the dashboard needs a new surface (S7) to display the digest

Review gate policy:
- Gemini via Vertex AI required on every PR
- Codex disabled (usage expired)

## Problem Statement

The governance signal extraction and retrospective digest components built in Feature 18 are fully tested but not connected to any live data pipeline. The operator still relies on the conversation analyzer which produces generic, valueless summaries instead of actionable governance intelligence.

## Design Goal

Wire the F18 signal pipeline into intelligence_daemon.py and surface the governance digest in the dashboard so operators see recurring failure patterns, evidence-linked recommendations, and actionable insights daily.

## Non-Goals

- no local model integration (deferred)
- no automatic policy mutation from digest
- no new signal extraction sources beyond what F18 defined

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Governance Digest Pipeline Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Dependencies**: []

### Scope
- define digest generation schedule (every 5 minutes via intelligence_daemon.py)
- define digest JSON shape and file location (state/governance_digest.json)
- define dashboard surface S7 (digest panel) with recurrence table and recommendation cards
- define signal-to-recommendation flow and advisory-only enforcement

### Quality Gate
`gate_pr0_governance_digest_contract`:
- [ ] Contract defines digest generation schedule and trigger
- [ ] Contract defines digest JSON shape with recurrence and recommendation fields
- [ ] Contract defines dashboard surface S7
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-1: Intelligence Daemon Digest Runner
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Scope
- add GovernanceDigestRunner class to intelligence_daemon.py
- runner calls collect_governance_signals() from governance_signal_extractor.py
- runner calls build_digest() from retrospective_digest.py
- runner writes output to state/governance_digest.json
- runner runs every 5 minutes (configurable via VNX_DIGEST_INTERVAL)
- add tests for runner lifecycle and output format

### Quality Gate
`gate_pr1_digest_runner`:
- [ ] GovernanceDigestRunner produces governance_digest.json under test
- [ ] Runner calls F18 extractors and digest builder
- [ ] Output matches contracted JSON shape
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-2: Digest API Endpoint And Signal Store
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-1]

### Scope
- add GET /api/operator/governance-digest endpoint to serve_dashboard.py
- endpoint reads state/governance_digest.json and returns with freshness envelope
- add persistent signal store (feedback/signals.ndjson) for durable signal history
- add fetchGovernanceDigest and useGovernanceDigest SWR hook in Next.js
- add tests for endpoint and signal persistence

### Quality Gate
`gate_pr2_digest_api`:
- [ ] GET /api/operator/governance-digest returns digest with freshness envelope under test
- [ ] Signal store persists signals to NDJSON under test
- [ ] SWR hook available in Next.js
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-3: Governance Digest Dashboard Page
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-2]

### Scope
- create app/operator/governance/page.tsx with digest visualization
- recurrence table: failure family, count, impacted features/PRs, severity
- recommendation cards: category badge, content, evidence pointers, advisory-only flag
- signal timeline chart showing signal volume over time
- add governance link to sidebar under Operator section
- add tests for page rendering

### Quality Gate
`gate_pr3_governance_page`:
- [ ] Governance page renders recurrence table under test
- [ ] Recommendation cards show advisory-only badge under test
- [ ] Signal timeline chart renders under test
- [ ] Sidebar shows governance link
- [ ] Gemini review receipt exists with no blocking findings

---

## PR-4: Live Governance Digest Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Dependencies**: [PR-3]

### Scope
- certify daemon produces digest file from real signal sources
- certify API serves digest with freshness tracking
- certify dashboard renders digest with all components
- certify recommendations show advisory-only flag
- update CHANGELOG.md and PROJECT_STATUS.md

### Quality Gate
`gate_pr4_governance_digest_certification`:
- [ ] Certification proves daemon-to-dashboard pipeline works end-to-end
- [ ] Certification proves advisory-only enforcement in recommendations
- [ ] CHANGELOG.md updated with Feature 25 closeout
- [ ] PROJECT_STATUS.md updated
- [ ] Gemini review receipt exists with no blocking findings
