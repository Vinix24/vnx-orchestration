# VNX Documentation Index

**Last Updated**: 2026-03-03

---

## Start Here

1. **Architecture Overview**: `core/00_VNX_ARCHITECTURE.md` (V10.0)
2. **Getting Started**: `core/00_GETTING_STARTED.md` (vnx CLI, demo setup)
3. **Limitations & Scope**: `manifesto/LIMITATIONS.md`
4. **Context Rotation** (community doc): `CONTEXT_ROTATION.md`

---

## Core Reference

### System Design
- Architecture (V10.0): `core/00_VNX_ARCHITECTURE.md`
- Getting started: `core/00_GETTING_STARTED.md`
- System boundaries: `core/VNX_SYSTEM_BOUNDARIES.md`
- Exit codes: `EXIT_CODES.md`

### Formats & Contracts
- Dispatch format (JSON): `core/10_JSON_DISPATCH_FORMAT.md`
- Receipt format (NDJSON): `core/11_RECEIPT_FORMAT.md`
- Permission settings: `core/12_PERMISSION_SETTINGS.md`
- Error contract standard: `orchestration/ERROR_CONTRACT_STANDARD.md`

### Technical Deep Dives
- Intelligence system (v3.0 — code + doc ingestion): `core/technical/INTELLIGENCE_SYSTEM.md`
- Dispatcher system (V7 legacy + V8 current): `core/technical/DISPATCHER_SYSTEM.md`
- State management: `core/technical/STATE_MANAGEMENT.md`
- Report lifecycle: `core/technical/REPORT_LIFECYCLE.md`
- **Context rotation system (v2.5)**: `core/technical/CONTEXT_ROTATION_SYSTEM.md`

---

## Operations

- Monitoring guide: `operations/MONITORING_GUIDE.md`
- Multi-model guide (Claude + Codex + Gemini): `operations/MULTI_MODEL_GUIDE.md`
- Receipt pipeline (V8.1 + quality advisory): `operations/RECEIPT_PIPELINE.md`
- Receipt processing flow: `operations/RECEIPT_PROCESSING_FLOW.md`

---

## Orchestration

- Orchestration index: `orchestration/ORCHESTRATION_INDEX.md`
- T0 operations guide: `orchestration/T0_OPERATIONS_GUIDE.md`
- T0 brief schema: `orchestration/T0_BRIEF_SCHEMA.md`
- PR systems guide: `orchestration/PR_SYSTEMS_GUIDE.md`
- PR queue workflow: `orchestration/README_PR_QUEUE.md`

---

## Intelligence

- Intelligence overview: `intelligence/README.md`
- T0 orchestration intelligence: `intelligence/T0_ORCHESTRATION_INTELLIGENCE.md`
- **Session Intelligence & System Tuning**: `intelligence/SESSION_INTELLIGENCE.md`
- Tag taxonomy: `intelligence/TAG_TAXONOMY.md`
- Cost tracking guide: `intelligence/COST_TRACKING_GUIDE.md`
- Hook integration report (context rotation): `intelligence/VNX_HOOK_INTEGRATION_REPORT.md`
- Rotation test report (v2.4): `intelligence/VNX_ROTATION_TEST_REPORT.md`

---

## Testing & Quality

- QA system: `testing/QUALITY_ASSURANCE_SYSTEM.md`
- Quality reviewer workflow: `testing/QUALITY_REVIEWER_WORKFLOW.md`

---

## Manifesto (Public Architecture Story)

- Architecture narrative: `manifesto/ARCHITECTURE.md`
- Architectural decisions: `manifesto/ARCHITECTURAL_DECISIONS.md`
- Evolution timeline: `manifesto/EVOLUTION_TIMELINE.md`
- Open method (how it was built): `manifesto/OPEN_METHOD.md`
- Limitations & scope: `manifesto/LIMITATIONS.md`
- Public roadmap: `manifesto/ROADMAP.md`

---

## Architecture Studies

- **SuperClaude audit**: `architecture/SUPERCLAUDE_AUDIT.md` (framework relevance assessment)
- State simplification (completed): `architecture/VNX_STATE_SIMPLIFICATION_PROPOSAL.md`
- Receipt upgrade plan: `architecture/RECEIPT_UPGRADE_PLAN.md`
- Git provenance study: `architecture/GIT_PROVENANCE_FEASIBILITY_STUDY.md`
- State consolidation: `architecture/STATE_CONSOLIDATION_ANALYSIS.md`

---

## Scripts Reference

See `SCRIPTS_INDEX.md` for a complete inventory of all VNX scripts.

---

## Directory Structure

```
docs/
  DOCS_INDEX.md          # This file
  README.md              # General introduction
  EXIT_CODES.md          # Script exit code reference
  SCRIPTS_INDEX.md       # Script inventory

  core/                  # System fundamentals (architecture, formats)
    technical/           # Deep technical references

  manifesto/             # Public-facing architecture story

  operations/            # Operational guides & monitoring

  intelligence/          # Intelligence system reference

  orchestration/         # PR workflow, T0 guides, contracts

  testing/               # QA and testing methodology

  architecture/          # Architecture studies & proposals

  internal/              # Internal docs (maintainer notes + publication drafts)
```
