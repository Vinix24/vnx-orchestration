# Feature Plan Reference — Example Shape

> This is a GENERIC EXAMPLE of the feature-plan shape the t0-orchestrator skill
> expects. During deployment, map this reference to your project's real
> `FEATURE_PLAN.md` (see `_MAPPING.md`). It is intentionally content-free:
> project plans are project-specific and never ship with the engine.

## Feature: Example Feature Name

**Status**: In progress
**Priority**: P1
**Source of queue truth**: `PR_QUEUE.md` and `.vnx-data/state/pr_queue_state.json`

## PR-1: First Small Slice

**Track**: A
**Priority**: P1
**Skill**: backend-developer
**Complexity**: Medium
**Estimated Time**: 1 day

Dependencies: []

### Description
One- or two-sentence statement of what this PR delivers.

### Scope
- Bullet the concrete changes (keep the PR 150-300 lines, independently deployable).
- Name the files or modules touched.

### Success Criteria
- Observable, checkable outcomes (tests pass, contract holds).

### Quality Gate
`gate_pr1_example`:
- [ ] Tests pass
- [ ] Review gate cleared

---

## PR-2: Next Slice

**Track**: B
**Priority**: P2
**Skill**: quality-engineer
**Complexity**: Low
**Estimated Time**: 0.5 day

Dependencies: [PR-1]

### Description
Each subsequent PR follows the same shape: track, skill, complexity,
dependencies, scope, success criteria, and a named quality gate.
