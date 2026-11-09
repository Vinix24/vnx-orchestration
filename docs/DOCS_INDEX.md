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
| Core Architecture | `docs/core/00_VNX_ARCHITECTURE.md` | Complete system architecture and data flow, incl. *Future-State Reconciliation* (open-item → track → dispatch lifecycle, the autopilot loop, and the precise `derived_status` rule) |
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
| Scripts Index | `docs/SCRIPTS_INDEX.md` | Active script surface map |
| Architecture Decisions | `docs/governance/decisions/` | ADRs — incl. ADR-005 (NDJSON audit ledger; D3 at-most-once bridge events) and ADR-007 (multi-tenant `project_id` / composite-key `dispatches`), ADR-044 (the provider switch: one operator file, fail-closed, two layers), ADR-045 (the ledger records every dispatch: one start line, one end line, one sweep), ADR-046 (one gate engine, four outcomes, one writer), ADR-047 (the tracks DB is the only plan source; `FEATURE_PLAN.md`, `PR_QUEUE.md` and `ROADMAP.yaml` retired as inputs), ADR-050 (agent layout phase 1: the `VNX_AGENT_LAYOUT` flag with `off` and `shadow`, the shadow skill copy, the context manifest sidecar), ADR-051 (a scheduled task follows the pin at runtime, and a release proves itself), ADR-052 (no function under `scripts/` over 200 executable lines), ADR-053 (a test may leave Profile A when a blocking CI step already runs it), ADR-054 (the envelope's governance record is not an end line) ADR-056 (autonomy as a measurable concept: the norm per actor class, the tenant-safe key `(project_id, delivery_id)`, the bounded period and denominator, the threshold `N >= 50`, who may promote, and the write-once outcome marker) and ADR-058 (the T0 playbook source, the pointer block, present against loaded, the budget scope and the evidence contract) and ADR-059 (the native materialisation of an agent's skill set, phase 2: the destinations per provider, the ignore rule, the codex ownership marker, the evidence record and the fail-closed precedence in the receipt). ADR-048 and ADR-049 are still Proposed. Numbering skips ADR-033. ADR-033 was withdrawn with PR #1171 and exists only on the unmerged branch `origin/dispatch/20260715-hashchain-anchor`, never in main |
| Key Provisioning | `docs/governance/KEY_PROVISIONING.md` | One-time operator step to provision a VNX signing key |
| System Contracts | `docs/contracts/` | Feature-level and platform contracts: `docs/contracts/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md`, `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md`, `docs/contracts/HEADLESS_RUN_CONTRACT.md`, `docs/contracts/HEADLESS_SESSION_CONTRACT.md`, `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md`, `docs/contracts/OPEN_ITEMS_GATE_TOGGLE_CONTRACT.md`, `docs/contracts/PRODUCTIZATION_CONTRACT.md` |
| Receipt Operations | `docs/operations/RECEIPT_PIPELINE.md` | Receipt generation, processing, and delivery |
| Event Streams | `docs/operations/EVENT_STREAMS.md` | Per-terminal NDJSON ring-buffer lifecycle and archive layout |
| Runtime Rollback | `docs/operations/RUNTIME_CORE_ROLLBACK.md` | Rollback path for runtime-core cutovers |
| Permission Policy Flip | `docs/operations/PERMISSION_POLICY_FLIP.md` | Operator runbook: read the permission posture report, the five flip conditions, and the manual steps to enable and revert enforcement |
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
| Agent-layout measurements | `docs/investigations/2026-11-08-agent-layout-metingen.md` | Probe cells A, C, D, H1, H2, L and B for points 3-5 of track `agents-als-map-fase-1`: the ancestor `CLAUDE.md`, the `projects` memory, the write-scope hook and the `--bare` lanes |
| Examples | `docs/examples/` | Example orchestration flows: `docs/examples/example_coding_orchestration.md`, `docs/examples/example_content_orchestration.md`, `docs/examples/example_headless_research.md` |
| Archive | `docs/_archive/` | Historical docs kept for traceability; see `docs/_archive/ARCHIVED_MANIFEST.md` for the archival rule and per-file provenance |
