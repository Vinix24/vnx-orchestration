# Feature: [Feature Name]

<!-- DOMAIN PLAN SCAFFOLDING — Template Version 1.0
     This template extends FEATURE_PLAN_TEMPLATE.md with substrate boundary
     and capability profile requirements from AGENT_OS_LIFT_IN_CONTRACT.md.
     
     Every non-coding domain plan MUST complete the Domain Onboarding section
     below before any PR work begins. Coding-domain plans may omit the
     Domain Onboarding section (coding_strict is the authoritative default).
-->

**Feature-ID**: Feature [N]
**Status**: Planned
**Priority**: P1
**Branch**: `feature/[branch-name]`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: [gate-list — must match capability profile gate_types]
**Domain**: [coding | business | regulated | research]
**Governance-Profile**: [coding_strict | business_light | regulated_strict]

---

## Domain Onboarding (Required For Non-Coding Domains)

<!-- This section ensures the plan author has explicitly engaged with the
     substrate boundary contract before writing any PRs. -->

### Capability Profile Declaration

**Domain ID**: [e.g., "business", "regulated", "research"]
**Governance Profile**: [coding_strict | business_light | regulated_strict]
**Maturity Level**: [CODING_AUTHORITATIVE | EXPERIMENTAL]

<!-- Fill in the capability expectations below. Values must be consistent
     with the governance profile declared above. See AGENT_OS_LIFT_IN_CONTRACT.md
     Section 5.3 for known profile values. -->

| Capability | Value | Justification |
|------------|-------|---------------|
| `manager_persistence` | [True/False] | [Why this domain needs/doesn't need persistent manager state] |
| `worker_headless_default` | [True/False] | [Why workers are/aren't headless by default] |
| `worker_scope_model` | [worktree/folder/sandbox/none] | [What isolation model this domain uses] |
| `session_evidence_required` | [True/False] | [Whether evidence completeness is required for closure] |
| `gate_required` | [True/False] | [Whether quality gates are mandatory] |
| `gate_types` | [list] | [Which gates apply — must be implemented, not aspirational] |
| `closure_requires_human` | [True/False] | [Whether human must approve closure] |
| `policy_mutation_blocked` | True | [MUST be True — see contract G-5] |
| `audit_retention_days` | [N] | [How long audit trail is retained] |
| `runtime_adapter_type` | [tmux/headless/local_session] | [Which adapter this domain uses] |

### Substrate Boundary Acknowledgment

<!-- Check each item to confirm you have read and accept the constraint. -->

- [ ] This plan does NOT add domain-specific logic to substrate modules (contract B-2)
- [ ] This plan does NOT require the substrate to import from this domain layer (contract B-4)
- [ ] This plan does NOT imply production rollout of this domain (contract Section 7 anti-goals)
- [ ] All gates referenced in `gate_types` above are currently implemented and operational
- [ ] If `governance_profile` is not `coding_strict`, this domain is EXPERIMENTAL maturity only

### Activation Prerequisites (Contract Section 7.2)

<!-- Check each prerequisite. Any unchecked item blocks domain activation. -->

- [ ] Capability profile is fully defined and validated above
- [ ] Governance profile has passing conformance tests
- [ ] Scope isolation model (`worker_scope_model`) is implemented and tested
- [ ] Domain-specific gates (if any) are operational
- [ ] Operator has explicitly approved domain enablement
- [ ] Activation decision is recorded in audit trail

---

## Problem Statement
[1-3 paragraphs. What breaks if this is not done? What user/business impact?]

## Design Goal
[1-2 paragraphs. What does success look like?]

## Non-Goals
- [What this feature explicitly does NOT do]

## Substrate Dependencies

<!-- List which substrate capabilities this feature depends on. Reference
     AGENT_OS_LIFT_IN_CONTRACT.md Section 3.1 for the full list. -->

| Substrate Capability | Required? | Notes |
|---------------------|-----------|-------|
| Dispatch lifecycle | [Yes/No] | |
| Lease management | [Yes/No] | |
| Receipt pipeline | [Yes/No] | |
| Session lifecycle | [Yes/No] | |
| Open items | [Yes/No] | |
| Intelligence & signals | [Yes/No] | |
| Governance enforcement | [Yes/No] | |
| Runtime adapter | [Yes/No] | |
| Capability profiles | [Yes/No] | |
| Read model | [Yes/No] | |

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- final certification must update the private BUSINESS planning progress docs

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
...
```

---

## PR-0: [Title] (150-300 lines)

**Track**: [A | B | C]
**Priority**: P1
**Complexity**: [Low | Medium | High]
**Risk**: [Low | Medium | High]
**Skill**: @[skill-name]
**Requires-Model**: [sonnet | opus]
**Estimated Time**: [e.g., 2-3 hours]
**Dependencies**: []

### Description
[What this PR does]

### Scope
- [Specific change 1]

### Success Criteria
- [Concrete pass/fail item]

### Quality Gate
`gate_pr0_descriptive_name`:
- [ ] [Measurable criterion]

---

<!-- Add more PRs following the same structure -->

## Final Checklist

- [ ] Domain Onboarding section is fully completed (non-coding domains only)
- [ ] Capability profile matches governance profile expectations
- [ ] All substrate boundary acknowledgments are checked
- [ ] All activation prerequisites are addressed (checked or explicitly deferred)
- [ ] All PRs within 150-300 line constraint
- [ ] Dependency graph is acyclic
- [ ] All quality gates have measurable criteria
- [ ] Final certification PR updates the private BUSINESS planning changelog
- [ ] Final certification PR updates the private BUSINESS project status doc

---

**Template Version**: 1.0
**Extends**: FEATURE_PLAN_TEMPLATE.md v1.2
**Contract Reference**: AGENT_OS_LIFT_IN_CONTRACT.md v1
**Last Updated**: 2026-04-03
