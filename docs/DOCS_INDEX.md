# VNX Documentation Index

## For Users

| Document | Path | Description |
|----------|------|-------------|
| README | `README.md` | Project overview and quickstart |
| Quickstart | `docs/QUICKSTART.md` | The condensed pip-CLI path: install, init, a first dispatch, read the receipt |
| Onboarding Guide | `docs/onboarding/ONBOARDING_GUIDE.md` | Pip install, the repo-local operator path (`./bin/vnx`), feature worktrees, troubleshooting. The first-dispatch walkthrough is `docs/QUICKSTART.md` |
| Getting Started | `docs/core/00_GETTING_STARTED.md` | Orientation, the CLI quick reference, and the T0 context guard and rotation spawner |
| Architecture | `docs/manifesto/ARCHITECTURE.md` | Glass Box Governance architecture story |
| Dispatch Guide | `docs/DISPATCH_GUIDE.md` | How dispatches work |
| Operations | `docs/operations/README.md` | Entry point for monitoring and runtime operations |
| Migration Guide | `docs/MIGRATION_GUIDE.md` | Upgrading between versions; includes the runtime DB migration runbook (§6, operator-gated) |
| Exit Codes | `docs/EXIT_CODES.md` | Process exit code reference, including the open-item → track bridge codes (3–6) |
| Changelog | `CHANGELOG.md` | Release notes. A PR never edits it: it adds a fragment under `changelog.d/` and the release PR folds the fragments in |
| Changelog Fragments | `changelog.d/README.md` | The fragment format, the check that enforces it, and the release step that assembles `CHANGELOG.md` |
| Context-Rotation Plugin | `packages/claude-code-toolkit/context-rotation/README.md` | Claude Code plugin that hands a long session over to a fresh one before it degrades. The source has been in this repository since `v1.7.2`; the README installs the released plugin from the public toolkit repository |
| Product Modes | `docs/contracts/PRODUCTIZATION_CONTRACT.md` | Starter, operator, and governed execution modes |
| Contributing | `CONTRIBUTING.md` | How to contribute |
| Applications | `docs/applications/README.md` | Why a code-governance mechanism generalizes to other domains, with `docs/applications/coding-agents.md` and `docs/applications/finance.md` as worked mappings |

## Technical Reference

| Document | Path | Description |
|----------|------|-------------|
| Core Architecture | `docs/core/00_VNX_ARCHITECTURE.md` | System architecture and data flow, incl. *Future-State Reconciliation* (open-item → track → dispatch lifecycle, the autopilot loop, and the `derived_status` rule), review seats, and short sections on the learning loop, dreaming and harness portability |
| Dispatch & Intelligence Architecture | `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` | Current end-to-end flow: stage → single door → assembly (skill + intelligence injection + report-contract) → claude-headless / provider-envelope lane delivery → govern (phantom-guard) → intelligence injection + self-learning loop |
| Dispatch Rules | `docs/core/DISPATCH_RULES.md` | Canonical, machine-checkable lane/provider/gate decision rules: `claude_headless` vs `subprocess_dispatch` vs `provider_dispatch`, concurrency, failure modes, the merge door, open outcomes |
| Enforcement Chain | `docs/core/ENFORCEMENT_CHAIN.md` | What stops an agent from merging its own unreviewed change, link by link: the dispatch door, review seats per risk class and the takeover chain, the merge door's five preflights, the `vnx-gate/review` check, the 15 required contexts on `main` (quoted from the live protection), `Red on base` as a check that is not required, and which document explains each required check |
| Locks and Releases | `docs/core/LOCKS_AND_RELEASES.md` | Register of every lock between a dispatch and a merge (eight stages), the flags, variables, attests and human steps that open each one, who may use them, the trail they leave and the test that holds the lock shut |
| Provider Lanes | `docs/core/PROVIDER_LANES.md` | How VNX drives AI coding CLIs as subprocess workers, never a vendor SDK; per-provider lane and billing detail |
| Provider Switch | `docs/core/PROVIDER_SWITCH.md` | The operator's switch per model provider family (`vnx providers`): precedence, file and version policy, fail-closed behaviour, what each consumer does on a refusal; decision in ADR-044 |
| State Fabric | `docs/core/STATE_FABRIC.md` | The state layers VNX governs work across, and how they reconcile |
| Horizon Planning | `docs/core/HORIZON_PLANNING.md` | Horizon, VNX's planning surface: objectives (tracks) and the `vnx horizon` command surface |
| Horizon Lifecycle | `docs/core/HORIZON_LIFECYCLE.md` | State-machine reference for how a track moves through its lifecycle; companion to `docs/core/HORIZON_PLANNING.md` |
| Subsystems | `docs/core/SUBSYSTEMS.md` | Which VNX subsystems are live, parked, cut, or scoped; generated from `scripts/lib/config_registry.py` |
| CI Profile A | `docs/core/CI_PROFILE_A.md` | How Profile A runs after D4: the two-phase xdist shape and its exact arguments, the required completeness step and its failure kinds, the serial list and skip set, the triage rule, and the `VNX_CI_XDIST=0` way back to serial |
| Numbered Execution Contracts | `docs/core/` | Report/session/queue/gate lifecycle contracts, numbered 11–180: `docs/core/11_RECEIPT_FORMAT.md`, `docs/core/12_PERMISSION_SETTINGS.md`, `docs/core/20_INCIDENT_TAXONOMY.md`, `docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`, `docs/core/60_CONVERSATION_RESUME_CONTRACT.md`, `docs/core/70_QUEUE_TRUTH_CONTRACT.md`, `docs/core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md`, `docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md`, `docs/core/110_SMART_ROUTER_DESIGN.md`, `docs/core/120_PROJECTION_CONSISTENCY_CONTRACT.md`, `docs/core/130_PR_SCOPED_GATE_EVIDENCE_CONTRACT.md`, `docs/core/131_RUNTIME_STATE_MACHINE_CONTRACT.md`, `docs/core/140_DASHBOARD_READ_MODEL_CONTRACT.md`, `docs/core/170_FAIL_CLOSED_BOOTSTRAP_CONTRACT.md`, `docs/core/180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md`, `docs/core/DAEMON_LIVENESS.md`, `docs/core/technical/INTELLIGENCE_SYSTEM.md` |
| Learning Capture Routing | `docs/core/LEARNING_CAPTURE_ROUTING.md` | Which bucket a lesson belongs in (open item, memory note, build-log nugget, learning loop), the reference grammar, the audit window, the three pair dispositions and the adoption order; working copy of ADR-067 |
| Scripts Index | `docs/SCRIPTS_INDEX.md` | Active script surface map |
| Architecture Decisions | `docs/governance/decisions/` | ADRs in Nygard format, one file per decision. Starting points: ADR-005 (NDJSON audit ledger), ADR-007 (multi-tenant `project_id`), ADR-008 (dual-LLM adversarial review), ADR-019 (auto-dream memory consolidation), ADR-037 (a missing reviewer is absence, not a vote), ADR-042 (the harness contract), ADR-044 (the provider switch), ADR-047 (the tracks DB is the only plan source), ADR-050 and ADR-059 (the agent-as-folder skill layout), ADR-060 (the gate profile per project), ADR-065 (learning only proposes), ADR-066 (the dream liveness watch), ADR-071 (rework per builder model and review seat: the `rework_edges` table, proposals only) and ADR-072 (`ADR-072-operator-bijsturing-leert.md`: learning from operator steering, Proposed) and ADR-073 (`ADR-073-main-venster-in-plaats-van-wachten-per-merge.md`: a window on main instead of a wait per merge, Proposed). ADR-048 and ADR-049 are Proposed. Numbering skips ADR-033 and ADR-070 |
| Attestation Enforcement | `docs/governance/ATTESTATION_ENFORCEMENT.md` | The attestation pipeline D1 to D5, shipped as advisory (non-blocking) |
| Key Provisioning | `docs/governance/KEY_PROVISIONING.md` | One-time operator step to provision a VNX signing key |
| System Contracts | `docs/contracts/` | Feature-level and platform contracts: `docs/contracts/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md`, `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md`, `docs/contracts/HEADLESS_RUN_CONTRACT.md`, `docs/contracts/HEADLESS_SESSION_CONTRACT.md`, `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md`, `docs/contracts/OPEN_ITEMS_GATE_TOGGLE_CONTRACT.md`, `docs/contracts/PRODUCTIZATION_CONTRACT.md` |
| Receipt Operations | `docs/operations/RECEIPT_PIPELINE.md` | Receipt generation, processing, and delivery |
| Event Streams | `docs/operations/EVENT_STREAMS.md` | Per-terminal NDJSON ring-buffer lifecycle and archive layout |
| Runtime Rollback | `docs/operations/RUNTIME_CORE_ROLLBACK.md` | Rollback path for runtime-core cutovers |
| Permission Policy Flip | `docs/operations/PERMISSION_POLICY_FLIP.md` | Operator runbook: read the permission posture report, the five flip conditions, and the manual steps to enable and revert enforcement |
| Forge Gate | `docs/operations/FORGE_GATE.md` | Operator runbook for gate-enforcement-to-forge: GitHub refuses any merge to `main` without a published gate verdict on the PR head |
| CI Cost Measurement | `docs/operations/CI_COST_METING.md` | The measurement behind ADR-073: docs-only CI before and after, the merge-chain wait, and ready-for-merge to merge, each number with its command |
| T0 Runbooks | `docs/operations/T0_RUNBOOKS.md` | Operational recipes for the `t0-orchestrator` role |
| T0 Context Rotation | `docs/operations/T0_CONTEXT_ROTATION.md` | The 500K rotation contract: the guard hook, `vnx guard install/check`, the handoff, and the rotation spawner |
| Handoff Contract | `docs/operations/CONTEXT_ROTATION.md` | The `handoff.md` a stopping T0 leaves behind and the `vnx handoff` CLI that reads it |
| Project-Root Resolution | `docs/project-root-resolution.md` | How scripts resolve the project root without trusting leaked environment variables |
| Full Operations List | `docs/operations/README.md` | Canonical-docs table there lists every `docs/operations/*.md` runbook (pools, supervisor, worker permissions, state aggregator, provider API keys, install/migration runbooks, and more) |
| Gates | `docs/gates/wiring_gate.md` | Dead-code detection gate that catches unwired public definitions added in a PR |
| Guides | `docs/guides/AGENT_CREATION_GUIDE.md` | How to define a new VNX agent role (skill-scoped worker, `CLAUDE.md` role file, `config.yaml`) |
| Compliance | `docs/compliance/vnx_anthropic_billing_audit.pdf` | Anthropic billing audit evidence |
| Intelligence | `docs/intelligence/` | Public intelligence references: `docs/intelligence/TAG_TAXONOMY.md`, `docs/intelligence/SELF_LEARNING_LOOP.md` (the operator-gated learning loop), `docs/intelligence/SCOUT_PREPASS.md`, `docs/intelligence/COST_TRACKING_GUIDE.md`. Dreaming has no page of its own: see ADR-019 and ADR-066 |
| Provider-agnostic role measurement | `docs/investigations/2026-10-27-provider-agnostic-role-nameting.md` | `vnx role sync` writing the canonical orchestrator role into `AGENTS.md` and `GEMINI.md`, and the drift check on both |
| Harness coupling map | `docs/investigations/2026-10-28-oss-harness-orchestrator.md` | Where the T0 role depends on Claude Code today. Internal research; it makes no claim about other harnesses |
| Agent-layout measurements | `docs/investigations/2026-11-08-agent-layout-metingen.md` | Probe cells A, C, D, H1, H2, L and B for points 3-5 of track `agents-als-map-fase-1`: the ancestor `CLAUDE.md`, the `projects` memory, the write-scope hook and the `--bare` lanes |
| Agent-skill-discovery measurements | `docs/investigations/2026-11-09-agent-skill-discovery.md` | Probe cells C1, C2, C3a and C3b for the DONE points of track `agent-skill-discovery-binding` (R8): claude discovery under `on`, the same under `--bare`, and codex placement and discovery |
| Examples | `docs/examples/` | Example orchestration flows: `docs/examples/example_coding_orchestration.md`, `docs/examples/example_content_orchestration.md`, `docs/examples/example_headless_research.md` |
| Archive | `docs/_archive/` | Historical docs kept for traceability; see `docs/_archive/ARCHIVED_MANIFEST.md` for the archival rule and per-file provenance |
