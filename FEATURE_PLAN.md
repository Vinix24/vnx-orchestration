# Feature: No Active Feature Materialized

**Status**: PRE-INIT PLACEHOLDER
**Purpose**: Root execution files are intentionally idle between autonomous chains.

## Current State

The previous autonomous hardening chain has been closed and merged.

Do not use this placeholder as active execution truth.

## Next Start Protocol

1. Select the next canonical plan from `docs/internal/plans/`.
2. Materialize that plan into the root `FEATURE_PLAN.md`.
3. Generate or refresh the root `PR_QUEUE.md` for that feature.
4. Run `python3 scripts/pr_queue_manager.py init-feature FEATURE_PLAN.md`.

## Notes

- This file exists so the repo remains in a clear pre-init state.
- No PRs are currently defined here.
- Any new run must begin from a newly materialized canonical feature plan.
