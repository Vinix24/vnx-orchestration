# VNX Subsystem Status Ledger

This document is the single source of truth for which VNX subsystems are live, parked, cut, or scoped. **Generated (PR-3):** the five deterministic columns (subsystem/what/flag/status/doc) are generated from `scripts/lib/config_registry.py` via `vnx subsystems --md`; `.github/workflows/subsystems-drift.yml` fails CI if those columns drift from that output. The `health` column remains manual in this ledger and is excluded from drift checks; CLI output uses a live health beacon when a probe has run (PR-5–7), else falls back to the last-committed value here. Hand-edits to the deterministic columns must match the generated output; hand-edits to `health` remain manual.

**Not the daemon-process register.** These rows describe flag-backed and flagless subsystems (`config_registry`), not OS processes. Which processes VNX's supervisor actually starts, and whether each one is currently running, lives in a separate generated file: `docs/core/DAEMON_LIVENESS.md` (source: `scripts/lib/daemon_register.py`, regenerate with `python3 scripts/generate_daemon_liveness_md.py`). The daemon table remains in its own file and has five columns; this subsystem ledger has six, of which `check_subsystems_drift.py` compares only the five deterministic columns. Overlap between the two registers is exactly two.

Each registry row carries a `doc` path under `docs/modules/`, or `geschrapt`, `geen-module` or `niet-gebouwd`. The generated table has six columns, including relative Markdown links in `doc`. `scripts/check_subsystems_drift.py` exits 1 for a missing documentation page, an unknown or disallowed exemption reason, an orphan page under `docs/modules/`, or a changed generated link cell; other deterministic-column drift also exits 1. M3/M4 found no merged candidates, so their thirteen new rows are distinct and flagless.

<!-- BEGIN generated: vnx subsystems --md; columns subsystem/what/flag/status/doc; health remains manual -->
| subsystem | what | flag | status | health | doc |
|-----------|------|------|--------|--------|-----|
| provider-routing | Model/provider selection, constraint solving, fallback order. | — | LIVE | works — dispatch outcomes routed correctly | [docs/modules/provider-routing.md](../modules/provider-routing.md) |
| git-grounded-reconcile | Per-project canonical stores, git-provenance linking, no shared-state fork. | — | LIVE | works — `vnx fabric-audit` passes | [docs/modules/git-grounded-reconcile.md](../modules/git-grounded-reconcile.md) |
| phantom_guard | Receipt deduplication and replay protection. | — | LIVE | works — zero duplicate dispatches in test suite | [docs/modules/phantom_guard.md](../modules/phantom_guard.md) |
| tmux-operational-scar | Terminal/session lifecycle, session handover, F1.1 safe linkage. | — | LIVE | works — `vnx doctor` tmux checks pass | [docs/modules/tmux-operational-scar.md](../modules/tmux-operational-scar.md) |
| zero-llm-injection | No prompt injection via environment or receipts; strict input boundaries. | — | LIVE | works — red-team tests pass | [docs/modules/zero-llm-injection.md](../modules/zero-llm-injection.md) |
| dispatch-plan | Single-entry dispatch door, dispatch-plan reconciliation. | — | LIVE | works — dispatch tests pass | [docs/modules/dispatch-plan.md](../modules/dispatch-plan.md) |
| test-suite | Pytest + integration coverage for kernel and cockpit. | — | LIVE | works — CI green | geen-module |
| migration-mechanisms | Schema-evolution surfaces (42 SQL files + 6 appliers). Consolidation PARKed pending inventory-lock. | `VNX_MIGRATION_SYSTEM` | PARK-with-trigger | degraded — consolidation PARKed pending inventory-lock (`scripts/lib/migration_inventory.py`) | [docs/modules/migration-mechanisms.md](../modules/migration-mechanisms.md) |
| within-db-tenancy | Composite (project_id, id) keys inside per-project DBs. Removal PARKed pending per-table central-DB safety proof. | — | PARK-with-trigger | degraded — keys present; drop deferred (central-store/dual-write/ADR-026 interaction) | [docs/modules/within-db-tenancy.md](../modules/within-db-tenancy.md) |
| docs-bloat | Comparisons, stale archive, marketing docs inflating docs/ count. | — | CUT | degraded — large `_archive/` | geschrapt |
| governance-enforcement-stack | Receipt hash-chain + signed attestation + evidence-bound merge gate. SURFACED here; enforcement wiring deferred. | `VNX_GOVERNANCE_ENFORCED` | PARK-with-trigger | produces-crap — receipts are unchained (no `prev_hash`) | [docs/modules/governance-enforcement-stack.md](../modules/governance-enforcement-stack.md) |
| receipt-hash-chain | Tamper-evident NDJSON hash-chain (ADR-029). | `VNX_HASH_CHAIN_REQUIRED` | PARK-with-trigger | produces-crap — unchained receipts | [docs/modules/receipt-hash-chain.md](../modules/receipt-hash-chain.md) |
| signed-attestation | SSH-signed PR attestation manifests (ADR-027). | `VNX_ATTESTATION_REQUIRED` | PARK-with-trigger | produces-crap — the mechanism works, no signed attestation is in active use | [docs/modules/signed-attestation.md](../modules/signed-attestation.md) |
| evidence-bound-gate | D3 evidence-bound merge gate. | `VNX_EVIDENCE_BOUND_GATE` | PARK-with-trigger | produces-crap — advisory only, enforces nothing | [docs/modules/evidence-bound-gate.md](../modules/evidence-bound-gate.md) |
| morning-pause | Morning pause (ADR-095): firing and merging stop from the start time until the morning points are decided, at the latest at the deadline. On by default (D4b); no snapshot for today inside the window means not started, not paused. | `VNX_MORNING_PAUSE_ENABLED` | PARK-with-trigger | unknown — no probe yet | [docs/modules/morning-pause.md](../modules/morning-pause.md) |
| deliverable-predecessor | Deliverable predecessors (ADR-096): the door refuses (refuse), reports (warn) or skips (off) a fire whose hard predecessor has no pr_merged receipt. | `VNX_DELIVERABLE_PREDECESSOR_MODE` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/deliverable-predecessor.md](../modules/deliverable-predecessor.md) |
| blast-radius | Blast radius (ADR-096): overlap of a fire's paths with live work or an open PR warns (warn), refuses unless --override-overlap REASON is given (refuse), or is not read (off); an unknown value reads as refuse. | `VNX_BLAST_RADIUS_MODE` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/blast-radius.md](../modules/blast-radius.md) |
| intelligence-self-learning-loop | Daily pattern learning, skill refinements, confidence updates. | `VNX_LEARNING_LOOP_ENABLED` | ACTIVATE-and-measure | produces-crap — last committed reading, not re-measured | [docs/modules/intelligence-self-learning-loop.md](../modules/intelligence-self-learning-loop.md) |
| dream-consolidation | Nightly memory consolidation + pending review dispatch. | `VNX_DREAM_SCHEDULER_ENABLED` | ACTIVATE-and-measure | unknown — no cycles run | [docs/modules/dream-consolidation.md](../modules/dream-consolidation.md) |
| injection-effectiveness-eval-loop | Instrument WHY patterns are ignored before tuning generation. | `VNX_INJECTION_WHY_ENABLED` | ACTIVATE-and-measure | probe built — `scripts/intel_injection_join.py` `cohort` (per-arm counts and controls, never a verdict) and `effect` (stratified difference, 95% interval, verdict class; exit 0 or 3 with a reason, floor 200 per arm); no live reading yet — the A/B window is not open | [docs/modules/injection-effectiveness-eval-loop.md](../modules/injection-effectiveness-eval-loop.md) |
| cross-project-federation | Cross-project intelligence federation (not yet implemented). | `VNX_USE_FEDERATION` | ACTIVATE-and-measure | unknown — no probe yet | niet-gebouwd |
| cheap-recon-scout | Cheap-model scout recon pre-pass in the dispatch door (fail-open). | `VNX_SCOUT_PREPASS` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/cheap-recon-scout.md](../modules/cheap-recon-scout.md) |
| horizon-planning | Autonomous roadmap auto-next loading (starts work unattended). | `VNX_ROADMAP_AUTOPILOT` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/horizon-planning.md](../modules/horizon-planning.md) |
| headless-dispatch-routing | Headless dispatch routing mode selector. | `VNX_HEADLESS_ROUTING` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/headless-dispatch-routing.md](../modules/headless-dispatch-routing.md) |
| central-db-routing | Central-DB read mode for the runtime coordination store (per-project vs central vs shadow). | `VNX_USE_CENTRAL_DB` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/central-db-routing.md](../modules/central-db-routing.md) |
| smart-router-staging | Phased smart-router AUTO routing rollout (per-tier enable + deterministic canary fraction). | `VNX_SMART_ROUTER_CANARY_PCT` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/smart-router-staging.md](../modules/smart-router-staging.md) |
| claude-tmux-serialization | N-slot serial lock protecting the shared Claude subscription session cap for claude dispatches (the lock class keeps its historical claude-tmux name). | `VNX_TMUX_MAX_CONCURRENT` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/claude-tmux-serialization.md](../modules/claude-tmux-serialization.md) |
| central-install-cutover | Cutover behind-guard: every central-install flip measures and names how far the target lags main; over the threshold the flip needs an explicit operator reason (OI-1718). | `VNX_CUTOVER_MAX_BEHIND_COMMITS` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/central-install-cutover.md](../modules/central-install-cutover.md) |
| t0-context-rotation | T0 must rotate at 500K context: measured from the transcript, the guard hook stops new work and blocks the turn end until the rotate flow starts a successor. | `VNX_T0_ROTATE_FORCE_TOKENS` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/t0-context-rotation.md](../modules/t0-context-rotation.md) |
| harness-lane-review-gates | Diff-char cap for the harness-lane review gates (glm_gate/kimi_gate/deepseek_gate): one shared default of 800000, bounded by the argv ceiling (OI-1961), configurable per gate, read at runtime by gate_lane_contract.max_diff_chars() (OI-1874). | `VNX_KIMI_GATE_MAX_DIFF_CHARS` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/harness-lane-review-gates.md](../modules/harness-lane-review-gates.md) |
| deterministic-test-verification | Receipt test counts from the builder's JUnit XML beside the report (VNX_JUNIT_COUNTS); off = the Verification section's prose counts. | `VNX_JUNIT_COUNTS` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/deterministic-test-verification.md](../modules/deterministic-test-verification.md) |
| fleet-pin-audit | Fleet pin audit: a project's .vnx-version pin more than N releases behind the store's current is a finding (exit 1) instead of a WARN. | `VNX_FLEET_PIN_MAX_LAG_RELEASES` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/fleet-pin-audit.md](../modules/fleet-pin-audit.md) |
| ledger-lifecycle | Dispatch lifecycle in the receipt ledger (ADR-045): one start line, one end line per attempt, and a report that is never a second outcome. | `VNX_LEDGER_LIFECYCLE` | LIVE | works — K-act acceptance: one door run with no override books one start, one end and one report_ingested | [docs/modules/ledger-lifecycle.md](../modules/ledger-lifecycle.md) |
| headless-builder-context | Settings levels a claude_headless builder loads (--setting-sources); default project,local, user,project,local restores the old argv; carries `cleanupPeriodDays` and `claudeMdExcludes` (walk-up, claude-code#87590), never `effortLevel`. | `VNX_BUILDER_SETTING_SOURCES` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/headless-builder-context.md](../modules/headless-builder-context.md) |
| docs-style-file | The Markdown file with writing rules that staging appends to a docs dispatch; empty is off, an unreadable set path refuses staging. | `VNX_DOC_STYLE_FILE` | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/docs-style-file.md](../modules/docs-style-file.md) |
| pool | Project-scoped worker pool membership, reaping and scaling. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/pool.md](../modules/pool.md) |
| merge-door | Merge preflights, door permit, head pinning and confirmed-merge bookkeeping. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/merge-door.md](../modules/merge-door.md) |
| provider-switch | Provider-family availability policy and refusal before invocation. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/provider-switch.md](../modules/provider-switch.md) |
| cost-ledger | Provider-run pricing, ledger/spool recording and cost aggregation. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/cost-ledger.md](../modules/cost-ledger.md) |
| daily-metrics | Per-project daily source collection, catch-up and metric documents. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/daily-metrics.md](../modules/daily-metrics.md) |
| worker-permissions | Worker permission profiles, isolation policy and tool/MCP CLI scoping. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/worker-permissions.md](../modules/worker-permissions.md) |
| receipt-pipeline | Report conversion and validated, enriched, idempotent receipt append. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/receipt-pipeline.md](../modules/receipt-pipeline.md) |
| event-streams | Canonical event validation, persistence and stream rotation. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/event-streams.md](../modules/event-streams.md) |
| control-centre | Registered-project lease/heartbeat display and audited queue forwarding. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/control-centre.md](../modules/control-centre.md) |
| unified-supervisor | Periodic runtime checks, lease expiry and anomaly recording. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/unified-supervisor.md](../modules/unified-supervisor.md) |
| state-aggregator | Event-backed per-project snapshots and central activity counts. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/state-aggregator.md](../modules/state-aggregator.md) |
| producer-freshness-monitor | Per-key producer cadence checks, job-exit harvesting and freshness reports. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/producer-freshness-monitor.md](../modules/producer-freshness-monitor.md) |
| wiring-gate | Diff and committed-tree caller analysis with advisory or required findings. | — | ACTIVATE-and-measure | unknown — no probe yet | [docs/modules/wiring-gate.md](../modules/wiring-gate.md) |
| plan-gate-panel | Configurable plan-review panel: one default seat, BLOCK escalation, path/profile floors. | `VNX_PLAN_GATE_ENFORCE` | SCOPE | works — panel runs, verdicts recorded | [docs/modules/plan-gate-panel.md](../modules/plan-gate-panel.md) |
| agent-layout | Agent-skill materialisation: off, shadow copy or on at the provider's native discovery location. | `VNX_AGENT_LAYOUT` | SCOPE | unknown — no probe yet | [docs/modules/agent-layout.md](../modules/agent-layout.md) |
| subsystem-cockpit | SUBSYSTEMS.md + config_registry + vnx subsystems + dashboard tile. | — | COCKPIT | degraded — SSOT exists, probes partial | [docs/modules/subsystem-cockpit.md](../modules/subsystem-cockpit.md) |
| effectiveness-probe-framework | Generic "does it produce crap?" probes per subsystem. | — | COCKPIT | unknown — framework not built yet | niet-gebouwd |
<!-- END generated: vnx subsystems --md -->

**Legend:**
- `LIVE` — running and expected to stay on.
- `PARK-with-trigger` — built + tested, currently off, with a documented un-park trigger.
- `CUT` — being removed; no future need.
- `ACTIVATE-and-measure` — dormant, gated on a probe/loop, then turned on with measurement.
- `SCOPE` — stays on, but restricted to a narrower class of work.
- `COCKPIT` — meta-subsystems that implement this very audit surface.

## CI guards that are not flags

The rows above include flag-backed and flagless subsystems. Two additional CI guards are described here
because they are subsystems an operator has to know the state of. Both are on `main`,
and were released in 1.9.0. This is code behaviour; no ADR records them.

**Profile A and pytest-xdist.** Since D4, Profile A (the core test job) runs a
two-phase shape: the entries of `scripts/ci/serial_tests.txt` in one process with xdist
off, then the rest under pytest-xdist, followed by a required completeness step
(`scripts/ci/check_xdist_parity.py completeness`) that fails on a test not run, a test
run more than once, a broken partition, a failed collection, or a skip not recorded in
`scripts/ci/xdist_expected_skips.txt`. The per-PR `profile-a-xdist-shadow` and
`xdist-parity` jobs are gone; the serial truth runs nightly in
`.github/workflows/vnx-ci-serial-truth.yml` and opens or updates one `xdist-parity`
issue on a difference. `vars.VNX_CI_XDIST=0` returns Profile A to the serial sweep
without changing the selection. The full description is `docs/core/CI_PROFILE_A.md`.

**Function-size ratchet.** `scripts/ci/check_function_size_ratchet.py` (the
`Function size ratchet` step of the `Lint Patterns` job) fails a pull request that
makes a function cross or grow past `function_size_gate.FUNCTION_SIZE_LIMIT`
executable lines. It looks at `scripts/`, `vnx_cli/` and `dashboard/`, compares the
head with the merge base, and exits 0 clean, 1 on violations, 2 on a usage or git
error. The size rule is no longer in the reviewer prompt: CI carries it.
