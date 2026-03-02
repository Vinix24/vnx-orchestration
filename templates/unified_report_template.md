# Unified Report Template

## Metadata
- **Terminal**: {terminal} <!-- T1|T2|T3 -->
- **Date**: {date} <!-- YYYY-MM-DD -->
- **Gate**: {gate} <!-- planning|implementation|review|testing|validation -->
- **Status**: {status} <!-- success|blocked|in_progress|fail -->
- **Task ID**: {task-id}
- **Dispatch ID**: {dispatch-id}
- **PR-ID**: {pr-id or none} <!-- Format: PR-1, PR-2, etc. Use "none" if not PR work -->
- **Track**: {track} <!-- A|B|C -->
- **Timestamp**: {ISO timestamp}

## Summary
Brief overview of what was accomplished (2-3 sentences)

## Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

## Work Completed
- Main achievements
- Key deliverables
- Problems solved

## Technical Details
- Implementation approach
- Code changes (files modified/created)
- Tests added/modified
- Performance metrics

## Evidence
- Test results
- Working examples
- Screenshots/logs (if applicable)

## Open Items
<!-- ⚠️ MANDATORY SECTION - DO NOT DELETE -->
<!-- ALWAYS include this section, even if empty -->
<!-- This ensures nothing is forgotten and extraction works properly -->

<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- DO NOT write IDs (OI-xxx) - system auto-generates them -->

<!-- Examples:
- [ ] [blocker] Missing error handling in auth module
- [ ] [warn] Performance issue with large datasets (>10k records)
- [ ] [info] Consider refactoring duplicate code in utils
-->

<!-- If no open items, write: -->
None - all work completed and tested.

## Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Recommendations
### Immediate
- Critical next steps
- Blockers to resolve

### Short-term
- Follow-up tasks
- Improvements needed

### Long-term
- Technical debt
- Future enhancements

## Notes
Any additional context or observations
