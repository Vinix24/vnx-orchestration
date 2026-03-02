# Reviewer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-review-{title}.md`

Example: `20260202-143000-C-review-authentication-pr.md`

## Report Structure

### Summary
Brief overview of code review findings (3-4 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Code Quality Assessment
- Overall code quality score
- Adherence to coding standards
- Design pattern usage
- SOLID principles compliance

### Issues Found
#### Critical Issues
- Security vulnerabilities
- Data integrity risks
- Performance bottlenecks

#### Major Issues
- Design flaws
- Missing error handling
- Inadequate testing

#### Minor Issues
- Code style violations
- Documentation gaps
- Naming improvements

### Positive Observations
- Well-implemented patterns
- Good practices followed
- Effective solutions

### Detailed Findings
- File-by-file review comments
- Line-specific feedback
- Code snippets with issues
- Suggested improvements

### Recommendations
- Refactoring priorities
- Testing additions needed
- Documentation updates required
- Architecture improvements

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Check for security vulnerabilities
- Verify test coverage
- Ensure documentation completeness
- Validate performance implications
