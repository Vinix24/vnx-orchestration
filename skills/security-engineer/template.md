# Security Engineer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-security-{title}.md`

Example: `20260202-143000-C-security-api-audit.md`

## Report Structure

### Summary
Brief overview of security assessment completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Scope
- Components audited
- Attack surface assessed
- Methodology used (OWASP, static analysis, dynamic testing)

### Vulnerabilities Found
#### Critical
- CVSS 9.0+ findings with evidence

#### High
- CVSS 7.0-8.9 findings with evidence

#### Medium
- CVSS 4.0-6.9 findings

#### Low/Informational
- CVSS 0.1-3.9 findings and observations

### Remediation
- Fixes applied (with code references)
- Fixes recommended (with priority)
- Mitigations in place

### Evidence
- Scan output and tool results
- Proof-of-concept examples
- Dependency audit results
- Configuration review findings

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Leave empty if all work completed -->

### Recommendations
- Security hardening priorities
- Monitoring and detection improvements
- Dependency update schedule
- Security testing to add to CI

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- All findings include CVSS score
- Evidence for every vulnerability
- Remediation guidance for all critical/high
- No secrets or credentials in report
