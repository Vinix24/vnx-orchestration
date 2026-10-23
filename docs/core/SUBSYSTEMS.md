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
| injection-effectiveness-eval-loop | Instrument WHY patterns are ignored before tuning generation. | `VNX_INJECTION_WHY_ENABLED` | ACTIVATE-and-measure | unknown — probe not built yet |
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
| plan-gate-panel | 5-model deliberation panel for plan-first enforcement. | `VNX_PLAN_GATE_ENFORCE` | SCOPE | works — panel runs, verdicts recorded |
| plan-gate-task-class-scope | Flag with no effect since P1: VNX_PLAN_GATE_COMPLEX_ONLY is read by complex_only_active() but changes no plan-gate run; kept until P4 reworks the effectiveness probe. | `VNX_PLAN_GATE_COMPLEX_ONLY` | SCOPE | no effect — flag read but changes no plan-gate run (P1) |
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
not yet released, and in `.github/workflows/vnx-ci.yml`. This is code behaviour; no ADR records them.

**xdist shadow and parity check.** Profile A (the core test job) still runs
its suite serially. Next to it the job `profile-a-xdist-shadow` runs the same suite in
the shape a switch to pytest-xdist would use: the tests listed in
`scripts/ci/serial_tests.txt` in one process with xdist off, then the rest under
xdist. The shadow is not a required check and must never fail one: every step that can
fail has `continue-on-error`, each step has its own timeout, the record and upload run
`if: always()`, and a docs-only run records `not_applicable`. The job `xdist-parity`
(`scripts/ci/check_xdist_parity.py`) then compares the serial run's JUnit XML and
collection manifest (`scripts/ci/pytest_collection_manifest.py`) with the shadow's. It
exits 1 on a test that was not run, ran extra or ran more than once, on a different
outcome, on a different skip set or collection outcome, and when the two shadow phases
do not partition the serial selection. It exits 2 on invalid or stale evidence. The
verdict is uploaded as the `xdist-parity-verdict` artifact
(`scripts/ci/record_xdist_parity_verdict.py`). `scripts/ci/xdist_parity_history.py`
applies the cutover rule to the recorded verdicts and counts only a verdict that a
failed parity step cannot leave clean. Under option A of that rule (operator
decision, 2026-10-22) a run whose serial reference job failed, or in which either
Profile A job received a runner shutdown signal, is not measurable: it is set aside
from every condition of the window and listed with its reason. Only positive evidence
sets a run aside: the reference job's conclusion, or the verdict's
`reference.job_result`, is exactly `failure`, or its reference result is present with a
whole-number exit other than 0. A `cancelled`, `timed_out`, `skipped`, missing or
unknown conclusion or job result, and a missing or malformed exit, are no cause and
never set a run aside (codex round 3 on #2184); a cancelled reference job that a
runner shutdown cut short is still set aside through the shutdown evidence. A shutdown
is proven by the runner's own line in the job log (`##[error]The runner has received a
shutdown signal`), read per job from `actions/jobs/<id>/logs` when the check-run
annotations do not carry that text themselves; GitHub's `The operation was canceled.`
annotation is posted for a timeout and a manual or concurrency cancel too and proves
nothing. The annotations are read from `check-runs/<check-run id>/annotations`: the
check-runs API is keyed by that id, never by the Actions job id, and the job row
carries it only as the last path segment of its `check_run_url`. A job row without
that URL, or with one that does not end in a whole number, has no readable annotation
list, and a failed annotation read still reads the job log (codex round 4 on #2184). A
failed annotation or log read is never a shutdown (fail-closed), and when both fail
the problem names both. A real parity difference still blocks. So does an xdist shadow job that is not known to have
succeeded — a conclusion or, with none, a verdict job result of exactly `success`, or a
proven shutdown of its own: a failed shadow blocks, and so does an unknown one.
Nothing has switched Profile A to xdist.

**Function-size ratchet.** `scripts/ci/check_function_size_ratchet.py` (the
`Function size ratchet` step of the `Lint Patterns` job) fails a pull request that
makes a function cross or grow past `function_size_gate.FUNCTION_SIZE_LIMIT`
executable lines. It looks at `scripts/`, `vnx_cli/` and `dashboard/`, compares the
head with the merge base, and exits 0 clean, 1 on violations, 2 on a usage or git
error. The size rule is no longer in the reviewer prompt: CI carries it.
