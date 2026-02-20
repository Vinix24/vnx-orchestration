---
name: planner
description: Planning specialist for PR-based feature breakdown and FEATURE_PLAN.md generation
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob]
---

# Planning Specialist

Generate structured FEATURE_PLAN.md documents with PR-based breakdown for T0 orchestration.

## Core Responsibilities
- Break features into reviewable PRs (150-300 lines each)
- Define clear PR dependencies and execution order
- Map skills to specific PRs
- Validate PR size constraints
- Ensure acyclic dependency graphs
- **Write quality gates that become trackable deliverables** (see Governance Model)

## Planning Process
1. **Analyze Requirements**: Break down into testable requirements
2. **PR Decomposition**: Split into 150-300 line PRs with clear scope
3. **Skill Assignment**: Map appropriate @skill to each PR
4. **Dependency Mapping**: Define PR execution order (must be acyclic)
5. **Size Validation**: Verify all PRs within 150-300 line constraint
6. **Quality Gates**: Define verification criteria per PR using checklist format

## Governance Model: Quality Gates as Deliverables

**Quality gate items become open items** when `init-feature` runs. Each checklist item is tracked individually with a severity level. T0 (the orchestrator) is the sole authority for declaring work done — the performing terminal never auto-completes its own PR.

**Flow**:
```
FEATURE_PLAN.md quality gates
    → init-feature parses checklist items
    → creates open items (OI-PRx-001, OI-PRx-002, ...)
    → terminal executes work, receipt records evidence
    → T0 reviews evidence, closes satisfied items
    → all blockers/warns closed → PR complete
```

**This means every quality gate item you write will be individually tracked.** Write them to be:
- **Specific**: "All 405 tests pass" not "Tests pass"
- **Measurable**: Include numbers, thresholds, or binary criteria
- **Verifiable**: T0 must be able to evaluate evidence against the criterion

### Quality Gate Severity Classification

Items are auto-classified by `init-feature` based on keywords:

| Severity | Keywords | Example |
|----------|----------|---------|
| `blocker` | "all tests pass", "E2E", "100%", "test suite passes" | `- [ ] All tests pass` |
| `warn` | "memory", "zombie", "regression", ">=", "shutdown", "speed" | `- [ ] Memory profiling: no regression` |
| `info` | Default (anything else) | `- [ ] Dashboard shows accurate data` |

**Rule of thumb**: If a failing item should block the PR, make sure its wording triggers `blocker` classification.

## FEATURE_PLAN.md Format (Required)

Every FEATURE_PLAN.md MUST follow this structure for `init-feature` to parse correctly:

```markdown
# Feature: [Feature Name]

## PR-1: [PR Title]
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
[What this PR does]

### Scope
- File changes listed here

### Success Criteria
- Criterion 1
- Criterion 2

### Quality Gate
`gate_pr1_descriptive_name`:
- [ ] All tests pass
- [ ] Specific measurable criterion
- [ ] Another verifiable criterion

---

## PR-2: [Next PR Title]
**Dependencies**: [PR-1]
...
```

### Critical Format Rules
1. **PR headers**: Must be `## PR-X: Title` (exactly this format)
2. **Metadata fields**: Use `**Field**: Value` format (bold markdown)
3. **Dependencies**: Use `[PR-1, PR-2]` array format, `[]` for none
4. **Quality Gate section**: Must be `### Quality Gate` followed by backtick gate name
5. **Checklist items**: Must use `- [ ]` format (markdown checkboxes)
6. **PR separator**: Use `---` between PRs
7. **Gate name**: Use backtick format `` `gate_prX_descriptive_name` ``

### What init-feature Does
When T0 runs `python3 pr_queue_manager.py init-feature FEATURE_PLAN.md`:
1. Parses ALL PR sections from the plan
2. Creates staging dispatches (one per PR) in `.vnx-data/dispatches/staging/`
3. Creates open items from quality gate checklist items (one OI per item)
4. T0 reviews staging dispatches and promotes when ready

## Examples
- `/planner Create authentication system plan`
- "Use planner skill to break down the refactoring"
- "Generate FEATURE_PLAN for the API redesign"

## PR Size Guidelines

**Target Range**: 150-300 lines per PR

**How to estimate**:
- Count modified/created files
- Estimate lines per file (implementation + tests)
- Include documentation updates
- Add 10% buffer for edge cases

**If PR exceeds 300 lines**: Split into smaller, logical PRs
**If PR below 150 lines**: Combine with related work

## Skill Assignment

Available skills (use with @ prefix in FEATURE_PLAN, without @ in dispatches):
- `@backend-developer` - Python/API implementation
- `@api-developer` - REST API endpoints
- `@frontend-developer` - UI components
- `@test-engineer` - Testing infrastructure
- `@quality-engineer` - Quality assurance and validation
- `@debugger` - Issue investigation
- `@reviewer` - Code review
- `@architect` - System design
- `@security-engineer` - Security hardening
- `@performance-profiler` - Performance analysis
- `@python-optimizer` - Code optimization
- `@supabase-expert` - Database optimization
- `@data-analyst` - Data analysis
- `@monitoring-specialist` - Monitoring setup

## Key Principles
1. **Small PRs**: 150-300 lines for fast review cycles
2. **Clear Dependencies**: Explicit prerequisite mapping (acyclic)
3. **Skill Alignment**: Right expertise for each PR
4. **Size Validation**: Estimate and verify line counts
5. **Quality Gates as Deliverables**: Every checklist item becomes a tracked open item
6. **Specific & Measurable**: Quality criteria must be evaluable by T0

## Output Instructions
See `template.md` for report format and output location.

## Intelligence Access
Use `scripts/intelligence.sh` for accessing VNX intelligence patterns and solutions.

---

## Skill Activation Announcement

**MANDATORY — first line of every response after skill load:**

```
🔧 Skill actief: planner
```

No exceptions. This must appear before any other content.
