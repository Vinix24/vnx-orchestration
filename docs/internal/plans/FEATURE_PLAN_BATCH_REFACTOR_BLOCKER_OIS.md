# Feature: Batch Refactor Blocker OIs

**Feature-ID**: Feature 27
**Status**: Planned
**Priority**: P1
**Branch**: `feature/batch-refactor-blocker-ois`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Resolve all 57 blocker open items (file-level and function-level size violations) that block further feature work. Pure structural refactor — no behavioral changes.

Execution context:
- 738 total open items, 57 are blockers (verified via `open_items_manager.py digest`)
- 3 files exceed file-size limits: dispatcher_v8_minimal.sh (2140L), runtime_coordination.py (1164L), review_gate_manager.py (1017L)
- ~30 functions exceed the 80-line function-size limit
- F28 (SubprocessAdapter) depends on dispatcher being decomposed first
- This is the first feature in the F27→F28→F29 critical path

Execution preconditions:
- Main branch must be stable (no in-flight features touching target files)
- All existing tests must pass before refactor begins
- Open items digest must be current

Review gate policy:
- Gemini headless review required on every PR
- Every PR must be opened as a GitHub PR before merge consideration

## Problem Statement

Three core files have grown far beyond maintainable size, and ~30 functions exceed the 80-line limit. These violations are tracked as blocker open items that must be resolved before new features can safely modify these files.

| File | Current Lines | Limit | Violation |
|------|--------------|-------|-----------|
| `scripts/dispatcher_v8_minimal.sh` | 2,140 | 500 | 4.3x over limit |
| `scripts/lib/runtime_coordination.py` | 1,164 | 400 | 2.9x over limit |
| `scripts/review_gate_manager.py` | 1,017 | 400 | 2.5x over limit |
| ~30 functions across codebase | > 80 lines each | 80 | Function-level violations |

These oversized files are merge-conflict magnets and cognitively expensive to review. The dispatcher (2140L shell script) is the single largest risk — it handles dispatch creation, delivery, lifecycle, and logging in one monolithic file.

## Design Goal

Decompose each oversized file into focused modules with clear responsibility boundaries. Each resulting module must be under the size limit. All existing imports and call sites must be updated. No behavioral changes — the system must behave identically before and after.

## Non-Goals

- No new features or capabilities
- No behavioral changes to dispatch, coordination, or gate logic
- No changes to external interfaces (CLI commands, API endpoints)
- No test rewriting (tests may need import path updates only)

## Delivery Discipline

- Each PR must have a GitHub PR with clear scope before merge
- Dependent PRs must branch from post-merge main
- Each PR must pass all existing tests after refactor
- Final certification must verify no behavioral regression

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-3
PR-1, PR-2, PR-3 -> PR-4
```

Note: PR-1, PR-2, PR-3 can run in parallel after PR-0 merges (they touch different files).

## Billing Safety Check

This feature is pure refactoring of existing code. No new dependencies, no new external calls, no SDK imports. Billing safety invariant is trivially maintained.

---

## PR-0: Refactor Contract and Module Boundary Design
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @architect
**Requires-Model**: opus
**Dependencies**: []

### Description
Define the module decomposition plan for all 3 oversized files. Specify exact module boundaries, file names, and which functions/sections move where.

### Scope
- Analyze dispatcher_v8_minimal.sh (2140L) and define 4 module boundaries: dispatch creation, delivery, lifecycle, logging
- Analyze runtime_coordination.py (1164L) and define 3 module boundaries: lease management, state machine, coordination DB
- Analyze review_gate_manager.py (1017L) and define 3 module boundaries: gate execution, result parsing, report generation
- Catalog all ~30 functions > 80 lines with their locations and proposed splits
- Define import update rules for each decomposition

### Deliverables
- Refactor contract document with exact module boundaries
- Function decomposition catalog
- Import migration rules
- GitHub PR with contract

### Success Criteria
- Every module target is under its size limit
- No function is left without a designated target module
- Import migration path is deterministic

### Quality Gate
`gate_pr0_refactor_contract`:
- [ ] Contract defines module boundaries for all 3 files
- [ ] Each target module is projected under size limit
- [ ] Function decomposition catalog covers all >80L functions
- [ ] Import migration rules are specified
- [ ] GitHub PR exists with contract
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-1: Dispatcher Decomposition
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
Decompose `scripts/dispatcher_v8_minimal.sh` (2140L) into 4 focused modules per the refactor contract.

### Scope
- Extract dispatch creation logic into `scripts/lib/dispatch_create.sh`
- Extract delivery logic into `scripts/lib/dispatch_deliver.sh`
- Extract lifecycle management into `scripts/lib/dispatch_lifecycle.sh`
- Extract logging utilities into `scripts/lib/dispatch_logging.sh`
- Reduce `dispatcher_v8_minimal.sh` to orchestration shell (< 500L) that sources modules
- Update all `source` and call sites
- Run `bash -n` on every .sh file (mandatory per project rules)

### Deliverables
- 4 new shell modules in `scripts/lib/`
- Reduced `dispatcher_v8_minimal.sh` (< 500L)
- All files pass `bash -n`
- GitHub PR with before/after line counts

### Success Criteria
- `dispatcher_v8_minimal.sh` is under 500 lines
- Each extracted module is under 500 lines
- All existing dispatcher tests pass
- `bash -n` passes on all modified .sh files
- Dispatcher behavior is identical (no functional changes)

### Quality Gate
`gate_pr1_dispatcher_decompose`:
- [ ] dispatcher_v8_minimal.sh < 500 lines
- [ ] Each extracted module < 500 lines
- [ ] `bash -n` passes on all .sh files
- [ ] All dispatcher tests pass
- [ ] GitHub PR exists with line count evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-2: Runtime Coordination Decomposition
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
Decompose `scripts/lib/runtime_coordination.py` (1164L) into 3 focused modules per the refactor contract.

### Scope
- Extract lease management into `scripts/lib/lease_manager.py`
- Extract state machine logic into `scripts/lib/runtime_state_machine.py`
- Extract coordination DB operations into `scripts/lib/coordination_db.py`
- Reduce `runtime_coordination.py` to facade (< 400L) re-exporting public API
- Update all import sites across the codebase

### Deliverables
- 3 new Python modules in `scripts/lib/`
- Reduced `runtime_coordination.py` (< 400L)
- All imports updated
- GitHub PR with before/after line counts

### Success Criteria
- `runtime_coordination.py` is under 400 lines
- Each extracted module is under 400 lines
- All existing tests pass with updated imports
- No circular imports introduced

### Quality Gate
`gate_pr2_runtime_coord_decompose`:
- [ ] runtime_coordination.py < 400 lines
- [ ] Each extracted module < 400 lines
- [ ] All tests pass
- [ ] No circular imports
- [ ] GitHub PR exists with line count evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-3: Review Gate Manager Decomposition
**Track**: A
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Dependencies**: [PR-0]

### Description
Decompose `scripts/review_gate_manager.py` (1017L) into 3 focused modules per the refactor contract.

### Scope
- Extract gate execution logic into `scripts/lib/gate_executor.py`
- Extract result parsing into `scripts/lib/gate_result_parser.py`
- Extract report generation into `scripts/lib/gate_report_generator.py`
- Reduce `review_gate_manager.py` to orchestration facade (< 400L)
- Update all import sites across the codebase

### Deliverables
- 3 new Python modules in `scripts/lib/`
- Reduced `review_gate_manager.py` (< 400L)
- All imports updated
- GitHub PR with before/after line counts

### Success Criteria
- `review_gate_manager.py` is under 400 lines
- Each extracted module is under 400 lines
- All existing gate tests pass with updated imports
- Gate behavior is identical (no functional changes)

### Quality Gate
`gate_pr3_gate_manager_decompose`:
- [ ] review_gate_manager.py < 400 lines
- [ ] Each extracted module < 400 lines
- [ ] All gate tests pass
- [ ] No behavioral changes to gate execution
- [ ] GitHub PR exists with line count evidence
- [ ] Gemini review receipt exists with no unresolved blocking findings

---

## PR-4: Function Sweep and Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @quality-engineer
**Requires-Model**: opus
**Dependencies**: [PR-1, PR-2, PR-3]

### Description
Split all remaining functions > 80 lines. Certify all blocker open items are resolved. Update open items digest.

### Scope
- Split all ~30 functions that exceed the 80-line limit
- Run `open_items_manager.py digest` and verify blocker count drops to 0
- Run full test suite and verify no regressions
- Update CHANGELOG.md with F27 closeout
- Update PROJECT_STATUS.md

### Deliverables
- All oversized functions split
- Updated open items digest showing 0 blockers
- Full test suite passing
- Updated CHANGELOG.md and PROJECT_STATUS.md
- GitHub PR with certification verdict

### Success Criteria
- Zero blocker open items remaining
- All functions under 80-line limit
- All files under their respective size limits
- Full test suite passes
- No behavioral regressions

### Quality Gate
`gate_pr4_f27_certification`:
- [ ] `open_items_manager.py digest` shows 0 blockers
- [ ] All functions under 80-line limit
- [ ] Full test suite passes
- [ ] CHANGELOG.md updated with F27 closeout
- [ ] PROJECT_STATUS.md updated
- [ ] GitHub PR exists with certification verdict
- [ ] Gemini review receipt exists with no unresolved blocking findings
