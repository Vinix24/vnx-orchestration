# Monitoring Specialist Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-monitoring-{title}.md`

Example: `20260202-143000-C-monitoring-health-dashboard.md`

## Report Structure

### Summary
Brief overview of monitoring work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Monitoring Implementation
- Metrics added/modified
- Alert rules configured
- Dashboard components created
- Health checks implemented

### Configuration
- Alert thresholds and conditions
- Metric collection intervals
- Notification channels setup
- Retention policies

### Evidence
- Dashboard screenshots or metric samples
- Alert trigger validation
- Health check responses
- Performance impact of monitoring overhead

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Leave empty if all work completed -->

### Recommendations
- Additional metrics to track
- Alert tuning suggestions
- Dashboard improvements
- Monitoring coverage gaps

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Sub-second metric updates
- <1% false positive alerts
- Dashboard load time <2s
- All critical paths monitored
