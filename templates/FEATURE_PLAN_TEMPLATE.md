# Feature: [Feature Name]

**Status**: DRAFT | READY FOR ORCHESTRATION | IN PROGRESS | COMPLETE
**Priority**: P0 | P1 | P2
**Timeline**: [e.g., 3 days / 2 weeks]
**Track**: A | B | C

## Business Problem
[1-3 paragraphs. What breaks if this is not done? What user/business impact?]

## Success Metrics
- [Measurable outcome 1]
- [Measurable outcome 2]

## Requirements
- [Requirement 1: clear + testable]
- [Requirement 2: clear + testable]

---

## PR-1: [Title] (150-300 lines)

**Track**: A | B | C
**Priority**: P0 | P1 | P2
**Skill**: [skill-name]
**Estimated Time**: [e.g., 4 hours]
**Complexity**: Low | Medium | High
**Risk**: Low | Medium | High

### Dependencies
Dependencies: []

### Description
[What changes, in plain language.]

### Scope
- [Specific change 1]
- [Specific change 2]

### Files Modified/Created
- `path/to/file1` (~N lines)
- `path/to/test_file` (~N lines)

### Success Criteria
- [Concrete pass/fail item]

### Quality Gate
`gate_name_here`:
- [ ] Checks that must be true before promotion

---

## PR-2: [Title] (150-300 lines)

**Track**: A | B | C
**Priority**: P0 | P1 | P2
**Skill**: [skill-name]
**Estimated Time**: [e.g., 1 day]
**Complexity**: Low | Medium | High
**Risk**: Low | Medium | High

### Dependencies
Dependencies: [PR-1]

### Description
[What changes, in plain language.]

### Scope
- [Specific change 1]
- [Specific change 2]

### Files Modified/Created
- `path/to/file2` (~N lines)
- `path/to/test_file2` (~N lines)

### Success Criteria
- [Concrete pass/fail item]

### Quality Gate
`gate_name_here`:
- [ ] Checks that must be true before promotion

---

## PR-3: [Title] (150-300 lines)

**Track**: A | B | C
**Priority**: P0 | P1 | P2
**Skill**: [skill-name]
**Estimated Time**: [e.g., 1 day]
**Complexity**: Low | Medium | High
**Risk**: Low | Medium | High

### Dependencies
Dependencies: [PR-1, PR-2]

### Description
[What changes, in plain language.]

### Scope
- [Specific change 1]
- [Specific change 2]

### Files Modified/Created
- `path/to/file3` (~N lines)
- `path/to/test_file3` (~N lines)

### Success Criteria
- [Concrete pass/fail item]

### Quality Gate
`gate_name_here`:
- [ ] Checks that must be true before promotion

---

## Dependency Flow

```
PR-1 (Foundation)
PR-1 -> PR-2
PR-1 -> PR-2 -> PR-3
```

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| [Risk 1] | High/Med/Low | [Mitigation strategy] |

## Final Checklist
- [ ] All requirements met
- [ ] All PRs within 150-300 line constraint
- [ ] Dependency graph is acyclic
- [ ] All quality gates passed
- [ ] Final certification PR updates the private BUSINESS planning changelog
- [ ] Final certification PR updates the private BUSINESS project status doc

---

## Valid Skill Names

backend-developer, api-developer, frontend-developer, test-engineer, architect,
reviewer, debugger, planner, supabase-expert, monitoring-specialist,
performance-profiler, python-optimizer, data-analyst, excel-reporter, vnx-manager

---

**Template Version**: 1.2
**Last Updated**: 2026-02-09
