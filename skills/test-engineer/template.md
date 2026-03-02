# Test Engineer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-testing-{title}.md`

Example: `20260202-143000-B-testing-api-integration.md`

## Report Structure

### Summary
Brief overview of testing work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Test Strategy
- Testing approach taken
- Test types implemented (unit, integration, E2E)
- Coverage targets and achievements
- Risk areas identified

### Test Implementation
- Test files created/modified
- Test frameworks used
- Test data/fixtures created
- Mocking strategies employed

### Test Results
- Test execution summary
- Pass/fail statistics
- Coverage metrics
- Performance benchmarks

### Issues Found
- Bugs discovered
- Edge cases identified
- Performance bottlenecks
- Security vulnerabilities

### Evidence
- Test output logs
- Coverage reports
- Failed test details
- Performance profiling results

### Recommendations
- Additional test scenarios needed
- Coverage gaps to address
- Test infrastructure improvements
- CI/CD integration suggestions

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Minimum 80% code coverage
- All critical paths tested
- Edge cases documented
- Performance benchmarks established
