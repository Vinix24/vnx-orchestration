# Quality Engineer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-quality-{title}.md`

Example: `20260202-143000-B-quality-api-validation.md`

## Report Structure

### Summary
Brief overview of quality validation work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Test Strategy
- Testing approach taken
- Test types executed (unit, integration, E2E)
- Risk areas assessed
- Dutch market compliance checks

### Quality Assessment
- Code quality score and methodology
- Coverage metrics (unit, integration)
- Performance validation against budgets
- Security scan results

### Issues Found
- Critical issues (blockers)
- Major issues (must fix before release)
- Minor issues (improvements)

### Evidence
- Test output and pass/fail statistics
- Coverage reports
- Performance benchmark results
- Security scan output

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Leave empty if all work completed -->

### Recommendations
- Quality gate pass/fail decision
- Additional testing needed
- Technical debt items identified
- Process improvements

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Minimum 80% unit test coverage
- All critical paths validated
- Performance within budget constraints
- No critical/high security issues
