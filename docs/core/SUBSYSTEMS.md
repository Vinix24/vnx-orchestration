# VNX Subsystem Status Ledger

This document is the single source of truth for which VNX subsystems are live, parked, cut, or scoped. **Generated (PR-3):** the deterministic columns (subsystem/what/flag/status) are regenerated from `scripts/lib/config_registry.py` via `vnx subsystems --md`; `.github/workflows/subsystems-drift.yml` fails CI if this file drifts from that output. The `health` column is dynamic — it comes from a live health beacon when a probe has run (PR-5–7), else falls back to the last-committed value here. Hand-edits to the deterministic columns will be reverted by the next `vnx subsystems --md` run; hand-edits to `health` are fine until a probe takes over that row.

**Not the daemon-process register.** These rows are feature flags (`config_registry`), not OS processes. Which processes VNX's supervisor actually starts, and whether each one is currently running, lives in a separate generated file: `docs/core/DAEMON_LIVENESS.md` (source: `scripts/lib/daemon_register.py`, regenerate with `python3 scripts/generate_daemon_liveness_md.py`). It is a dedicated file rather than a section here because its table shares this ledger's 5-column shape, which `check_subsystems_drift.py` would otherwise misread as phantom subsystem rows (measured while building the D2 daemon-liveness dispatch). Overlap between the two registers is exactly two.

| subsystem | what | flag | status | health |
|-----------|------|------|--------|--------|
| provider-routing | Model/provider selection, constraint solving, fallback order. | — | LIVE | works — dispatch outcomes routed correctly |
| git-grounded-reconcile | Per-project canonical stores, git-provenance linking, no shared-state fork. | — | LIVE | works — `vnx fabric-audit` passes |
| phantom_guard | Receipt deduplication and replay protection. | — | LIVE | works — zero duplicate dispatches in test suite |
| tmux-operational-scar | Terminal/session lifecycle, session handover, F1.1 safe linkage. | — | LIVE | works — `vnx doctor` tmux checks pass |
| zero-llm-injection | No prompt injection via environment or receipts; strict input boundaries. | — | LIVE | works — red-team tests pass |
| dispatch-plan | Single-entry dispatch door, dispatch-plan reconciliation. | — | LIVE | works — dispatch tests pass |
| test-suite | Pytest + integration coverage for kernel and cockpit. | — | LIVE | works — CI green |
| migration-mechanisms | Schema-evolution surfaces (42 SQL files + 6 appliers). Consolidation PARKed pending inventory-lock. | `VNX_MIGRATION_SYSTEM` | PARK-with-trigger | degraded — consolidation PARKed pending inventory-lock (`scripts/lib/migration_inventory.py`) |
| within-db-tenancy | Composite (project_id, id) keys inside per-project DBs. Removal PARKed pending per-table central-DB safety proof. | — | PARK-with-trigger | degraded — keys present; drop deferred (central-store/dual-write/ADR-026 interaction) |
| docs-bloat | Comparisons, stale archive, marketing docs inflating docs/ count. | — | CUT | degraded — large `_archive/` |
| governance-enforcement-stack | Receipt hash-chain + signed attestation + evidence-bound merge gate. SURFACED here; enforcement wiring deferred. | `VNX_GOVERNANCE_ENFORCED` | PARK-with-trigger | produces-crap — receipts are unchained (no `prev_hash`) |
| receipt-hash-chain | Tamper-evident NDJSON hash-chain (ADR-029). | `VNX_HASH_CHAIN_REQUIRED` | PARK-with-trigger | produces-crap — unchained receipts |
| signed-attestation | SSH-signed PR attestation manifests (ADR-027). | `VNX_ATTESTATION_REQUIRED` | PARK-with-trigger | produces-crap — the mechanism works, no signed attestation is in active use |
| evidence-bound-gate | D3 evidence-bound merge gate. | `VNX_EVIDENCE_BOUND_GATE` | PARK-with-trigger | produces-crap — advisory only, enforces nothing |
| intelligence-self-learning-loop | Daily pattern learning, skill refinements, confidence updates. | `VNX_LEARNING_LOOP_ENABLED` | ACTIVATE-and-measure | produces-crap — last committed reading, not re-measured |
| dream-consolidation | Nightly memory consolidation + pending review dispatch. | `VNX_DREAM_SCHEDULER_ENABLED` | ACTIVATE-and-measure | unknown — no cycles run |
| injection-effectiveness-eval-loop | Instrument WHY patterns are ignored before tuning generation. | `VNX_INTEL_AB_WINDOW` | ACTIVATE-and-measure | unknown — probe not built yet |
| cross-project-federation | Cross-project intelligence federation (not yet implemented). | `VNX_USE_FEDERATION` | ACTIVATE-and-measure | unknown — no probe yet |
| cheap-recon-scout | Cheap-model scout recon pre-pass in the dispatch door (fail-open). | `VNX_SCOUT_PREPASS` | ACTIVATE-and-measure | unknown — no probe yet |
| horizon-planning | Autonomous roadmap auto-next loading (starts work unattended). | `VNX_ROADMAP_AUTOPILOT` | ACTIVATE-and-measure | unknown — no probe yet |
| headless-dispatch-routing | Headless dispatch routing mode selector. | `VNX_HEADLESS_ROUTING` | ACTIVATE-and-measure | unknown — no probe yet |
| central-db-routing | Central-DB read mode for the runtime coordination store (per-project vs central vs shadow). | `VNX_USE_CENTRAL_DB` | ACTIVATE-and-measure | unknown — no probe yet |
| smart-router-staging | Phased smart-router AUTO routing rollout (per-tier enable + deterministic canary fraction). | `VNX_SMART_ROUTER_CANARY_PCT` | ACTIVATE-and-measure | unknown — no probe yet |
| claude-tmux-serialization | N-slot serial lock protecting the shared Claude subscription session cap for claude dispatches (the lock class keeps its historical claude-tmux name). | `VNX_TMUX_MAX_CONCURRENT` | ACTIVATE-and-measure | unknown — no probe yet |
| central-install-cutover | Cutover behind-guard: every central-install flip measures and names how far the target lags main; over the threshold the flip needs an explicit operator reason (OI-1718). | `VNX_CUTOVER_MAX_BEHIND_COMMITS` | ACTIVATE-and-measure | unknown — no probe yet |
| t0-context-rotation | T0 must rotate at 500K context: measured from the transcript, the guard hook stops new work and blocks the turn end until the rotate flow starts a successor. | `VNX_T0_ROTATE_FORCE_TOKENS` | ACTIVATE-and-measure | unknown — no probe yet |
| harness-lane-review-gates | Diff-char cap for the harness-lane review gates (glm_gate/kimi_gate/deepseek_gate): one shared default of 800000, bounded by the argv ceiling (OI-1961), configurable per gate, read at runtime by gate_lane_contract.max_diff_chars() (OI-1874). | `VNX_KIMI_GATE_MAX_DIFF_CHARS` | ACTIVATE-and-measure | unknown — no probe yet |
| deterministic-test-verification | Receipt test counts from the builder's JUnit XML beside the report (VNX_JUNIT_COUNTS); off = the Verification section's prose counts. | `VNX_JUNIT_COUNTS` | ACTIVATE-and-measure | unknown — no probe yet |
| fleet-pin-audit | Fleet pin audit: a project's .vnx-version pin more than N releases behind the store's current is a finding (exit 1) instead of a WARN. | `VNX_FLEET_PIN_MAX_LAG_RELEASES` | ACTIVATE-and-measure | unknown — no probe yet |
| ledger-lifecycle | Dispatch lifecycle in the receipt ledger (ADR-045): one start line, one end line per attempt, and a report that is never a second outcome. | `VNX_LEDGER_LIFECYCLE` | LIVE | works — K-act acceptance: one door run with no override books one start, one end and one report_ingested |
| plan-gate-panel | 5-model deliberation panel for plan-first enforcement. | `VNX_PLAN_GATE_ENFORCE` | SCOPE | works — panel runs, verdicts recorded |
| plan-gate-task-class-scope | Flag with no effect since P1: VNX_PLAN_GATE_COMPLEX_ONLY is read by complex_only_active() but changes no plan-gate run; kept until P4 reworks the effectiveness probe. | `VNX_PLAN_GATE_COMPLEX_ONLY` | SCOPE | no effect — flag read but changes no plan-gate run (P1) |
| agent-layout | Agent-as-folder skill layout (track agents-als-map-fase-1): off or shadow; `on` is phase 2 and reads as off. | `VNX_AGENT_LAYOUT` | SCOPE | unknown — no probe yet |
| subsystem-cockpit | SUBSYSTEMS.md + config_registry + vnx subsystems + dashboard tile. | — | COCKPIT | degraded — SSOT exists, probes partial |
| effectiveness-probe-framework | Generic "does it produce crap?" probes per subsystem. | — | COCKPIT | unknown — framework not built yet |

**Legend:**
- `LIVE` — running and expected to stay on.
- `PARK-with-trigger` — built + tested, currently off, with a documented un-park trigger.
- `CUT` — being removed; no future need.
- `ACTIVATE-and-measure` — dormant, gated on a probe/loop, then turned on with measurement.
- `SCOPE` — stays on, but restricted to a narrower class of work.
- `COCKPIT` — meta-subsystems that implement this very audit surface.

## CI guards that are not flags

The rows above are feature flags. Two CI guards are not, and are described here
because they are subsystems an operator has to know the state of. Both are on `main`,
not yet released. This is code behaviour; no ADR records them.

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
