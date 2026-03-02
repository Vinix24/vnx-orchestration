# Architect Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-architecture-{title}.md`

Example: `20260202-143000-C-architecture-system-redesign.md`

## Report Structure

### Executive Summary
High-level overview of architectural decisions and rationale (3-4 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Architectural Analysis
- Current system assessment
- Identified limitations
- Improvement opportunities
- Design constraints

### Proposed Architecture
- System components and boundaries
- Data flow and interactions
- Technology stack decisions
- Design patterns applied

### Technical Design
- Component specifications
- Interface definitions
- Data models
- Security architecture

### Implementation Strategy
- Migration path
- Risk mitigation
- Testing approach
- Rollback procedures

### Performance Considerations
- Scalability analysis
- Performance targets
- Bottleneck identification
- Optimization strategies

### Recommendations
- Implementation priorities
- Technology choices
- Team structure suggestions
- Timeline estimates

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Include architecture diagrams (ASCII or Mermaid)
- Define clear system boundaries
- Document all assumptions
- Provide measurable success criteria
