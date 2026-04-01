# Feature: Gate Evidence Accuracy And PR-Scoped Lookup

## PR-0: PR-Scoped Gate Evidence Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical contract for how gate evidence is scoped, ordered, and validated so provenance is deterministic and gate results are always attributable to the correct PR.

### Scope
- Define deterministic ordering for dispatch provenance when multiple dispatches exist per PR (fix iterdir() nondeterminism in `scripts/lib/queue_reconciler.py:219`)
- Define PR-scoped lookup semantics for `_find_gate_result` in `scripts/closure_verifier.py:197` (replace OR logic with AND logic requiring both PR and gate match)
- Define `report_path` enforcement rules for verdict-only gate results in `scripts/closure_verifier.py:341-363`
- Lock non-goals so this does not become a full closure verifier rewrite

### Success Criteria
- Provenance ordering is deterministic regardless of filesystem iteration order
- Gate evidence lookup requires PR scope match, not just gate name match
- Verdict-only gate results without report_path are caught and flagged
- Contract is directly testable without manual inspection

### Quality Gate
`gate_pr0_gate_evidence_contract`:
- [ ] Contract defines deterministic dispatch ordering when multiple dispatches exist per PR
- [ ] Contract requires PR-scoped gate lookup (AND logic, not OR)
- [ ] Contract defines report_path enforcement rules for verdict-only results
- [ ] Contract blocks silent cross-PR evidence attribution

---

## PR-1: Deterministic Provenance And PR-Scoped Gate Lookup
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Fix the three gate evidence accuracy bugs: nondeterministic iterdir(), OR-based gate lookup, and missing report_path enforcement for verdict-only results.

### Scope
- Replace unsorted `iterdir()` with `sorted()` by name in `scripts/lib/queue_reconciler.py:219` for deterministic provenance
- Change `_find_gate_result` in `scripts/closure_verifier.py:197` from OR-based matching to AND-based matching requiring both PR number and gate name
- Add report_path enforcement check for verdict-only gate results in `scripts/closure_verifier.py:341-363` so status-only results without report_path are flagged
- Add tests for:
  - Multiple dispatches per PR producing deterministic ordering
  - Gate evidence lookup returning only PR-scoped matches
  - Verdict-only results without report_path being rejected

### Success Criteria
- iterdir() ordering cannot produce different provenance across runs
- Gate evidence lookup never returns results from a different PR
- Verdict-only gate results without report_path are caught before closure
- All three fixes are independently testable

### Quality Gate
`gate_pr1_gate_evidence_accuracy`:
- [ ] All gate evidence accuracy tests pass
- [ ] Provenance ordering is deterministic under test with multiple dispatches per PR
- [ ] Gate lookup returns only PR-scoped matches under test
- [ ] Verdict-only results without report_path fail validation under test

---

## PR-2: Gate Evidence Certification
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1]

### Description
Certify the gate evidence fixes by exercising multi-dispatch PRs, cross-PR gate scenarios, and verdict-only results to prove the closure verifier now produces deterministic, correctly-scoped evidence.

### Scope
- Reproduce multi-dispatch PR scenario and verify deterministic ordering
- Reproduce cross-PR gate evidence scenario and verify scoped lookup
- Reproduce verdict-only result scenario and verify report_path enforcement
- Require Gemini review and Codex final gate on certification

### Success Criteria
- Multi-dispatch PR produces identical provenance across repeated runs
- Cross-PR gate evidence is never attributed to the wrong PR
- Verdict-only results without report_path fail validation deterministically
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists and passes

### Quality Gate
`gate_pr2_gate_evidence_certification`:
- [ ] All gate evidence certification tests pass
- [ ] Multi-dispatch PR provenance is deterministic in certification evidence
- [ ] Cross-PR gate evidence attribution is correctly scoped in certification evidence
- [ ] Verdict-only report_path enforcement is verified in certification evidence
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
