# Planner Report Template

## Output Location
Write your report to: `$VNX_DATA_DIR/unified_reports/`

## Filename Format
`{timestamp}-{track}-plan-{title}.md`

Example: `20260202-143000-A-plan-authentication-system.md`

## Report Structure

### Executive Summary
High-level overview of the plan and expected outcomes (3-4 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Strategic Plan
- Project goals and objectives
- Success criteria
- Key deliverables
- Timeline and milestones

### Technical Approach
- Architecture decisions
- Technology choices
- Design patterns to use
- Integration points

### Implementation Phases
1. **Phase 1**: [Description, duration, deliverables]
2. **Phase 2**: [Description, duration, deliverables]
3. **Phase 3**: [Description, duration, deliverables]

### Deliverable Table (required in the plan doc)
| # | id | Deliverable | task_class | Size limit | hangt af van |
|---|---|---|---|---|---|
| B1 | `dlv-...` | B1: [title] | 02_feature | 300 | geen |
| B2 | `dlv-...` | B2: [title] | 02_feature | 300 | B1 |

- The first lines of the plan name the track as `` Track `<track_id>` `` (one track per plan).
- `#` is the label the deliverable title starts with (`B1: ...`); `id` is the `dlv-` id once the deliverable exists.
- `hangt af van` lists labels or `dlv-` ids, comma-separated, `(soft)` after a soft one, `geen` for none. A paths column is optional.
- Order goes into `--depends-on`, paths into `--dispatch-paths` (see SKILL.md).

### Resource Requirements
- Team composition needed
- Technical resources
- External dependencies
- Time estimates

### Risk Assessment
- Technical risks
- Mitigation strategies
- Contingency plans

### Recommendations
- Immediate next steps
- Priority order
- Success metrics
- Review checkpoints

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Include clear phase boundaries
- Define measurable success criteria
- Identify dependencies explicitly
- Provide realistic time estimates
