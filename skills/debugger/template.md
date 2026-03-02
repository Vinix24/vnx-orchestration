# Debugger Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-debug-{title}.md`

Example: `20260202-143000-C-debug-memory-leak.md`

## Report Structure

### Summary
Brief overview of the issue and resolution (3-4 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Problem Statement
- Symptoms observed
- Error messages/stack traces
- Impact on system
- Reproduction steps

### Investigation Process
- Hypothesis formation
- Debugging tools used
- Data collected
- Tests performed

### Root Cause Analysis
- Primary cause identified
- Contributing factors
- Why it wasn't caught earlier
- System design implications

### Solution Implementation
- Fix applied
- Code changes made
- Configuration updates
- Workarounds (if any)

### Verification
- Tests to confirm fix
- Performance impact
- Regression testing
- Edge cases validated

### Evidence
- Before/after comparisons
- Log excerpts
- Performance metrics
- Test results

### Prevention Recommendations
- How to prevent recurrence
- Monitoring additions
- Test improvements
- Documentation needs

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Include complete reproduction steps
- Document all debugging commands used
- Provide evidence for root cause
- Verify fix with multiple test cases
