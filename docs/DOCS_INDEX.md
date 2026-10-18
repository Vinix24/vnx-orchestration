# VNX Documentation Index

## For Users

| Document | Path | Description |
|----------|------|-------------|
| README | `README.md` | Project overview and quickstart |
| Quickstart | `docs/QUICKSTART.md` | The condensed pip-CLI path: install, init, a first dispatch, read the receipt |
| Onboarding Guide | `docs/onboarding/ONBOARDING_GUIDE.md` | The full walkthrough: pip install, first dispatch, the repo-local operator path (`./bin/vnx`), feature worktrees, troubleshooting |
| Getting Started | `docs/core/00_GETTING_STARTED.md` | Orientation, the CLI quick reference, and the T0 context guard and rotation spawner |
| Architecture | `docs/manifesto/ARCHITECTURE.md` | Glass Box Governance architecture story |
| Dispatch Guide | `docs/DISPATCH_GUIDE.md` | How dispatches work |
| Operations | `docs/operations/README.md` | Entry point for monitoring and runtime operations |
| Migration Guide | `docs/MIGRATION_GUIDE.md` | Upgrading between versions; includes the runtime DB migration runbook (§6, operator-gated) |
| Exit Codes | `docs/EXIT_CODES.md` | Process exit code reference, including the open-item → track bridge codes (3–6) |
| Changelog | `CHANGELOG.md` | Release notes. A PR never edits it: it adds a fragment under `changelog.d/` and the release PR folds the fragments in |
| Changelog Fragments | `changelog.d/README.md` | The fragment format, the check that enforces it, and the release step that assembles `CHANGELOG.md` |
| Context-Rotation Plugin | `packages/claude-code-toolkit/context-rotation/README.md` | Claude Code plugin that hands a long session over to a fresh one before it degrades; on main, not yet released |
| Product Modes | `docs/contracts/PRODUCTIZATION_CONTRACT.md` | Starter, operator, and governed execution modes |
| Contributing | `CONTRIBUTING.md` | How to contribute |
| Applications | `docs/applications/README.md` | Why a code-governance mechanism generalizes to other domains, with `docs/applications/coding-agents.md` and `docs/applications/finance.md` as worked mappings |

## Technical Reference

| Document | Path | Description |
|----------|------|-------------|
| Core Architecture | `docs/core/00_VNX_ARCHITECTURE.md` | Complete system architecture and data flow, incl. *Future-State Reconciliation* (open-item → track → dispatch lifecycle, the autopilot loop, and the precise `derived_status` rule) |
| Dispatch & Intelligence Architecture | `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` | Current end-to-end flow: stage → single door → assembly (skill + intelligence injection + report-contract) → claude-headless / provider-envelope lane delivery → govern (phantom-guard) → intelligence injection + self-learning loop |
| Dispatch Rules | `docs/core/DISPATCH_RULES.md` | Canonical, machine-checkable lane/provider/gate decision rules: `claude_headless` vs `subprocess_dispatch` vs `provider_dispatch`, concurrency, failure modes, the merge door, open outcomes |
| Locks and Releases | `docs/core/LOCKS_AND_RELEASES.md` | Register of every lock between a dispatch and a merge (eight stages), the flags, variables, attests and human steps that open each one, who may use them, the trail they leave and the test that holds the lock shut |
| Provider Lanes | `docs/core/PROVIDER_LANES.md` | How VNX drives AI coding CLIs as subprocess workers, never a vendor SDK; per-provider lane and billing detail |
| State Fabric | `docs/core/STATE_FABRIC.md` | The state layers VNX governs work across, and how they reconcile |
| Horizon Planning | `docs/core/HORIZON_PLANNING.md` | Horizon, VNX's planning surface: objectives (tracks) and the `vnx horizon` command surface |
| Horizon Lifecycle | `docs/core/HORIZON_LIFECYCLE.md` | State-machine reference for how a track moves through its lifecycle; companion to `docs/core/HORIZON_PLANNING.md` |
| Subsystems | `docs/core/SUBSYSTEMS.md` | Which VNX subsystems are live, parked, cut, or scoped; generated from `scripts/lib/config_registry.py` |
| Numbered Execution Contracts | `docs/core/` | Report/session/queue/gate lifecycle contracts, numbered 11–180: `docs/core/11_RECEIPT_FORMAT.md`, `docs/core/12_PERMISSION_SETTINGS.md`, `docs/core/20_INCIDENT_TAXONOMY.md`, `docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`, `docs/core/60_CONVERSATION_RESUME_CONTRACT.md`, `docs/core/70_QUEUE_TRUTH_CONTRACT.md`, `docs/core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md`, `docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md`, `docs/core/110_SMART_ROUTER_DESIGN.md`, `docs/core/120_PROJECTION_CONSISTENCY_CONTRACT.md`, `docs/core/130_PR_SCOPED_GATE_EVIDENCE_CONTRACT.md`, `docs/core/131_RUNTIME_STATE_MACHINE_CONTRACT.md`, `docs/core/140_DASHBOARD_READ_MODEL_CONTRACT.md`, `docs/core/170_FAIL_CLOSED_BOOTSTRAP_CONTRACT.md`, `docs/core/180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md`, `docs/core/DAEMON_LIVENESS.md`, `docs/core/technical/INTELLIGENCE_SYSTEM.md` |
| Scripts Index | `docs/SCRIPTS_INDEX.md` | Active script surface map |
| Architecture Decisions | `docs/governance/decisions/` | ADRs — incl. ADR-005 (NDJSON audit ledger; D3 at-most-once bridge events) and ADR-007 (multi-tenant `project_id` / composite-key `dispatches`). Numbering skips ADR-033: it was withdrawn with PR #1171 and exists only on the unmerged branch `origin/dispatch/20260715-hashchain-anchor`, never in main |
| Attestation Enforcement | `docs/governance/ATTESTATION_ENFORCEMENT.md` | Signed-attestation system gating whether feature code can merge |
| Key Provisioning | `docs/governance/KEY_PROVISIONING.md` | One-time operator step to provision a VNX signing key |
| System Contracts | `docs/contracts/` | Feature-level and platform contracts: `docs/contracts/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md`, `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md`, `docs/contracts/HEADLESS_RUN_CONTRACT.md`, `docs/contracts/HEADLESS_SESSION_CONTRACT.md`, `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md`, `docs/contracts/OPEN_ITEMS_GATE_TOGGLE_CONTRACT.md`, `docs/contracts/PRODUCTIZATION_CONTRACT.md` |
| Receipt Operations | `docs/operations/RECEIPT_PIPELINE.md` | Receipt generation, processing, and delivery |
| Event Streams | `docs/operations/EVENT_STREAMS.md` | Per-terminal NDJSON ring-buffer lifecycle and archive layout |
| Runtime Rollback | `docs/operations/RUNTIME_CORE_ROLLBACK.md` | Rollback path for runtime-core cutovers |
| Forge Gate | `docs/operations/FORGE_GATE.md` | Operator runbook for gate-enforcement-to-forge: GitHub refuses any merge to `main` without a published gate verdict on the PR head |
| T0 Runbooks | `docs/operations/T0_RUNBOOKS.md` | Operational recipes for the `t0-orchestrator` role |
| T0 Context Rotation | `docs/operations/T0_CONTEXT_ROTATION.md` | The 500K rotation contract: the guard hook, `vnx guard install/check`, the handoff, and the rotation spawner |
| Handoff Contract | `docs/operations/CONTEXT_ROTATION.md` | The `handoff.md` a stopping T0 leaves behind and the `vnx handoff` CLI that reads it |
| Project-Root Resolution | `docs/project-root-resolution.md` | How scripts resolve the project root without trusting leaked environment variables |
| Full Operations List | `docs/operations/README.md` | Canonical-docs table there lists every `docs/operations/*.md` runbook (pools, supervisor, worker permissions, state aggregator, provider API keys, install/migration runbooks, and more) |
| Gates | `docs/gates/wiring_gate.md` | Dead-code detection gate that catches unwired public definitions added in a PR |
| Guides | `docs/guides/AGENT_CREATION_GUIDE.md` | How to define a new VNX agent role (skill-scoped worker, `CLAUDE.md` role file, `config.yaml`) |
| Compliance | `docs/compliance/vnx_anthropic_billing_audit.pdf` | Anthropic billing audit evidence |
| Intelligence | `docs/intelligence/` | Public intelligence references: `docs/intelligence/TAG_TAXONOMY.md`, `docs/intelligence/SELF_LEARNING_LOOP.md`, `docs/intelligence/SCOUT_PREPASS.md`, `docs/intelligence/COST_TRACKING_GUIDE.md` |
| Examples | `docs/examples/` | Example orchestration flows: `docs/examples/example_coding_orchestration.md`, `docs/examples/example_content_orchestration.md`, `docs/examples/example_headless_research.md` |
| Archive | `docs/_archive/` | Historical docs kept for traceability; see `docs/_archive/ARCHIVED_MANIFEST.md` for the archival rule and per-file provenance |
