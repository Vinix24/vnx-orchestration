# VNX Subsystem Status Ledger

This document is the single source of truth for which VNX subsystems are live, parked, cut, or scoped. **Generated (PR-3):** the deterministic columns (subsystem/what/flag/status) are regenerated from `scripts/lib/config_registry.py` via `vnx subsystems --md`; `.github/workflows/subsystems-drift.yml` fails CI if this file drifts from that output. The `health` column is dynamic — it comes from a live health beacon when a probe has run (PR-5–7), else falls back to the last-committed value here. Hand-edits to the deterministic columns will be reverted by the next `vnx subsystems --md` run; hand-edits to `health` are fine until a probe takes over that row.

| subsystem | what | flag | status | health |
|-----------|------|------|--------|--------|
| provider-routing | Model/provider selection, constraint solving, fallback order. | — | LIVE | works — dispatch outcomes routed correctly |
| git-grounded-reconcile | Per-project canonical stores, git-provenance linking, no shared-state fork. | — | LIVE | works — `vnx fabric-audit` passes |
| phantom_guard | Receipt deduplication and replay protection. | — | LIVE | works — zero duplicate dispatches in test suite |
| tmux-operational-scar | Terminal/session lifecycle, session handover, F1.1 safe linkage. | — | LIVE | works — `vnx doctor` tmux checks pass |
| zero-llm-injection | No prompt injection via environment or receipts; strict input boundaries. | — | LIVE | works — red-team tests pass |
| dispatch-plan | Single-entry dispatch door, dispatch-plan reconciliation. | — | LIVE | works — dispatch tests pass |
| test-suite | Pytest + integration coverage for kernel and cockpit. | — | LIVE | works — CI green |
| cheap-recon-scout | Cheap-model scout recon pre-pass in the dispatch door (fail-open). | `VNX_SCOUT_PREPASS` | LIVE | unknown — no probe yet |
| horizon-planning | Autonomous roadmap auto-next loading (starts work unattended). | `VNX_ROADMAP_AUTOPILOT` | LIVE | unknown — no probe yet |
| headless-dispatch-routing | Headless dispatch routing mode selector. | `VNX_HEADLESS_ROUTING` | LIVE | unknown — no probe yet |
| central-db-routing | Central-DB read mode for the runtime coordination store (per-project vs central vs shadow). | `VNX_USE_CENTRAL_DB` | LIVE | unknown — no probe yet |
| migration-mechanisms | Schema-evolution surfaces (42 SQL files + 6 appliers). Consolidation PARKed pending inventory-lock. | `VNX_MIGRATION_SYSTEM` | PARK-with-trigger | degraded — 42 SQL files + 6 appliers; consolidation PARKed pending inventory-lock (`scripts/lib/migration_inventory.py`, PR-8) |
| within-db-tenancy | Composite (project_id, id) keys inside per-project DBs. Removal PARKed pending per-table central-DB safety proof. | — | PARK-with-trigger | degraded — keys present; drop deferred (central-store/dual-write/ADR-026 interaction) |
| docs-bloat | Comparisons, stale archive, marketing docs inflating docs/ count. | — | CUT | degraded — ~288 markdown files, large `_archive/` |
| governance-enforcement-stack | Receipt hash-chain + signed attestation + evidence-bound merge gate. SURFACED here; enforcement wiring deferred. | `VNX_GOVERNANCE_ENFORCED` | PARK-with-trigger | produces-crap — 15,577 receipts, 0 `prev_hash` |
| receipt-hash-chain | Tamper-evident NDJSON hash-chain (ADR-029). | `VNX_HASH_CHAIN_REQUIRED` | PARK-with-trigger | produces-crap — unchained receipts |
| signed-attestation | SSH-signed PR attestation manifests (ADR-027). | `VNX_ATTESTATION_REQUIRED` | PARK-with-trigger | produces-crap — 0 signed attestations in active use |
| evidence-bound-gate | D3 evidence-bound merge gate. | `VNX_EVIDENCE_BOUND_GATE` | PARK-with-trigger | produces-crap — advisory only, enforces nothing |
| intelligence-self-learning-loop | Daily pattern learning, skill refinements, confidence updates. | `VNX_LEARNING_LOOP_ENABLED` | ACTIVATE-and-measure | produces-crap — 98% injection ignore rate, 0 dream cycles |
| dream-consolidation | Nightly memory consolidation + pending review dispatch. | `VNX_DREAM_SCHEDULER_ENABLED` | ACTIVATE-and-measure | unknown — no cycles run |
| injection-effectiveness-eval-loop | Instrument WHY patterns are ignored before tuning generation. | `VNX_INJECTION_FEEDBACK_ENABLED` | ACTIVATE-and-measure | unknown — probe not built yet |
| cross-project-federation | Cross-project intelligence federation (not yet implemented). | `VNX_USE_FEDERATION` | ACTIVATE-and-measure | unknown — no probe yet |
| plan-gate-panel | 5-model deliberation panel for plan-first enforcement. | `VNX_PLAN_GATE_ENFORCE` | SCOPE | works — panel runs, verdicts recorded |
| plan-gate-task-class-scope | Restrict panel to complex features; skip trivial tracks. Enforcement deferred to review-floor-enforcer. | `VNX_PLAN_GATE_COMPLEX_ONLY` | SCOPE | unknown — read-site deferred |
| subsystem-cockpit | SUBSYSTEMS.md + config_registry + vnx subsystems + dashboard tile. | — | COCKPIT | degraded — SSOT exists, probes partial |
| effectiveness-probe-framework | Generic "does it produce crap?" probes per subsystem. | — | COCKPIT | unknown — framework not built yet |

**Legend:**
- `LIVE` — running and expected to stay on.
- `PARK-with-trigger` — built + tested, currently off, with a documented un-park trigger.
- `CUT` — being removed; no future need.
- `ACTIVATE-and-measure` — dormant, gated on a probe/loop, then turned on with measurement.
- `SCOPE` — stays on, but restricted to a narrower class of work.
- `COCKPIT` — meta-subsystems that implement this very audit surface.
