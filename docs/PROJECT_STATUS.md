# VNX Project Status

**Status**: Active  
**Last Updated**: 2026-04-04  
**Owner**: VNX Maintainer  
**Purpose**: Commit-backed status after Feature 26 completion.

---

## Current Summary

Features 12 through 26 are complete. Feature 26 certified 2026-04-04.

This baseline now includes all prior hardening (Features 5–11) plus:

6. **Runtime state machine and stall supervision** (Feature 12)
   - 9-state canonical worker lifecycle with deterministic transitions
   - heartbeat and output tracked as independent liveness signals
   - stall detection with automatic open-item escalation (9 anomaly types)
   - deterministic truth hierarchy: Runtime DB > Queue Projection > Terminal Activity
7. **Coding operator dashboard and session control** (Feature 13)
   - 5 operator surfaces through governed read-model layer
   - 6 safe operator actions with structured outcome model
   - cross-project open-item aggregation with attention model
   - degraded state is explicitly visible (never silent)
8. **Headless review gate hardening** (post-Feature 13)
   - both Gemini and Codex gates default-enabled
   - atomic request-and-execute flow prevents silent non-execution
9. **Terminal startup and session control** (Feature 26)
   - profile-aware session startup: 2x2 tmux layout for dev, single terminal for business
   - session stop/attach with structured outcome model
   - dry-run mode for side-effect-free planning
   - dashboard session control buttons with pending states
   - serve_dashboard.py module split into 3 focused modules

The system is now:

- governance-first with explicit runtime truth
- operator-visible through dashboard read-model
- receipt-led with 2800+ tests across runtime and dashboard layers
- headless-review capable with both providers enabled by default
- session-controllable from dashboard with profile-aware layout creation

---

## Representative Recent Chain Merges

- PR #64 — Feature 12 PR-0: Runtime State Machine Contract
- PR #65 — Feature 12 PR-1: Worker State Machine and Heartbeat Persistence
- PR #66 — Feature 12 PR-2: Stall Detection, Exit Classification, Escalation
- PR #67 — Feature 12 PR-3: Unattended Runtime Reliability Certification
- PR #68 — Feature 13 PR-0: Dashboard Read-Model Contract
- PR #69 — Feature 13 PR-1: Read-Model Projection Layer
- PR #70 — Feature 13 PR-2: Safe Operator Control Actions
- PR #71 — Feature 13 PR-3: Operator Dashboard UI
- PR #72 — Feature 13 PR-4: Dashboard Certification

Governance note:

- all 9 PRs merged through GitHub PRs with CI visibility
- Feature 12/13 were interactively reviewed by T0 with code spot-checks
- headless gates (Gemini/Codex) were not executed during the chain due to infrastructure timing gap and orchestration flow gap
- post-chain correction: both providers now enabled by default with atomic request-and-execute enforcement

---

## Current Proven Capabilities

- Input-readiness checks before slash-prefixed dispatch delivery
- Queue/runtime projection reconciliation against canonical state
- PR-scoped gate-result lookup with report existence enforcement
- Deferred requeue classification for retryable dispatch failures
- Substep-level delivery failure annotation and certification evidence

---

## Carried-Forward Open Items

Pre-existing:
- `OI-022` — `rc_register` remains non-fatal while `acquire_lease` depends on its FK side effect
- `OI-078` — `Profile C` CI path configuration remains pre-existing

From Feature 12/13 (all warn, no blockers):
- `OI-373` — `dashboard_actions.py:start_session` 106 lines — highest priority refactor candidate
- `OI-374` — `serve_dashboard.py` 1215 lines — needs route decomposition (pre-existing file)
- `OI-371` — `dashboard_read_model.py:get_aggregate` 84 lines over threshold
- `OI-314` — `worker_state_manager.py:transition` 3 lines over threshold
- `OI-370` — `runtime_supervisor.py:_check_terminal` 3 lines over threshold

Resolved by post-chain correction:
- `OI-048` — headless Gemini/Codex gate execution reliability — **materially improved**: both providers now default-enabled, atomic execution flow enforced

---

## Recommended Next Features

1. Feature 14: multi-feature autonomy hardening
2. Feature 15: intelligence, context injection, and handover quality
3. Feature 16: runtime adapter formalization and early headless transport abstraction

Prerequisite: live smoke test proving both Gemini and Codex gates execute end-to-end.

---

## Maintenance Rule

Update this document when one of these changes:

- a merged feature materially changes what VNX can do today
- a remediation closes a previously recurring governance/runtime failure class
- the recommended next bridge lane changes in a meaningful way
