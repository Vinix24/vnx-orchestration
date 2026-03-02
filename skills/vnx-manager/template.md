# VNX Manager Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-vnx-{title}.md`

Example: `20260202-143000-C-vnx-intelligence-upgrade.md`

## Report Structure

### Summary
Brief overview of VNX system work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., dispatch-routing, receipt-processing, quality-gate). Avoid general-only tags.

### Implementation
- Components modified (dispatcher, smart tap, receipt processor, intelligence)
- Configuration changes
- Script updates

### System Impact
- Token efficiency change
- Reliability improvements
- Process flow changes
- Observability additions

### Testing
- Scripts tested and validation results
- CI test results (doctor + pytest suites)
- Integration verification

### Evidence
- Command output showing changes work
- Before/after metrics
- CI test pass confirmation

### Documentation Updates
- Architecture docs updated
- Operations docs updated
- PROJECT_STATUS.md updated

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Leave empty if all work completed -->

### Recommendations
- Follow-up work needed
- System improvements identified
- Technical debt items

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- All CI tests pass (9/9)
- No hardcoded paths
- Documentation updated
- Changes tested end-to-end
