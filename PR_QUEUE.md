<!-- DEPRECATED: see pr_queue_state.json -->
# PR Queue - Feature: Dependency Chain Test

## Progress Overview
Total: 3 PRs | Complete: 2 | Active: 0 | Queued: 1 | Blocked: 0
Progress: ██████░░░░ 66%

## Status

### ✅ Completed PRs
- PR-1: Base
- PR-2: Middle

### ⏳ Queued PRs
- PR-3: Top (dependencies: PR-2) [risk=unknown, merge=human, review=none]

## Dependency Flow
```
PR-1 (no dependencies)
PR-1 → PR-2
PR-2 → PR-3
```
