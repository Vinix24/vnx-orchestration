# Backend Developer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-{title}.md`

Example: `20260202-143000-A-authentication-implementation.md`

## Report Structure

### Summary
Brief overview of what was accomplished (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Implementation/Investigation
- What was built/analyzed
- Technical approach taken
- Key decisions made

### Code Changes
- Files modified/created
- Key functions/classes added
- Database changes (if any)

### Testing
- Tests added/modified
- Test coverage metrics
- Validation results

### Evidence
- Working code examples
- Test output
- Performance metrics (if applicable)

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Example: - [ ] [blocker] Missing error handling in auth module -->
<!-- Leave empty if all work completed -->

### Recommendations
- Next steps
- Potential improvements
- Technical debt items

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Include actual code snippets
- Provide test evidence
- Document API changes
- List all dependencies added
