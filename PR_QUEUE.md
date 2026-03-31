# PR Queue - Awaiting Re-Initialization

This queue file was intentionally reset for a fresh T0 restart.

## Active Feature
`FEATURE_PLAN.md` currently points to:
- **Deterministic Queue State Reconciliation**

## Current State
- queue is **not initialized**
- no staged, pending, active, or rejected dispatches should be treated as valid execution truth
- T0 must rebuild queue state from `FEATURE_PLAN.md` using the local queue tooling before dispatching

## Bootstrap Commands

```bash
python3 scripts/validate_skill.py --list
python3 scripts/pr_queue_manager.py init-feature FEATURE_PLAN.md
python3 scripts/pr_queue_manager.py staging-list
python3 scripts/pr_queue_manager.py status
```

## Chain Context
Feature 1 of 4 in the unattended hardening chain:
1. **Deterministic Queue State Reconciliation** (current)
2. Fail-Closed Terminal Dispatch Guard
3. Failed Delivery Lease Cleanup And Runtime State Reconciliation
4. Verified Provider And Model Routing Enforcement

## Notes For T0
- Treat any missing or contradictory queue projection as stale until rebuilt
- Require Gemini review on every non-trivial PR
- Require Codex final gate on every PR in this chain unless an explicit feature contract says otherwise
- Do not ask for routine user confirmation during the chain
