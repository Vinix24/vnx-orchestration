# VNX Tag Taxonomy v2.0
**Last Updated**: 2026-03-28
**Owner**: T-MANAGER

**Status**: Active
**Date**: 2026-03-28
**Purpose**: Standardized tag vocabulary for tag intelligence system

## Overview

The VNX Tag Taxonomy provides a standardized vocabulary for tagging issues, patterns, and prevention rules. This ensures consistency across the orchestration system and enables intelligent pattern matching.

## Tag Categories

### Phase Tags
Indicate which development phase the issue occurred in.

| Tag | Description | Example Usage |
|-----|-------------|---------------|
| `design-phase` | Planning, architecture, design decisions | Design flaw, missing requirement |
| `implementation-phase` | Coding, development, feature creation | Bug during coding, implementation error |
| `testing-phase` | QA, validation, test execution | Test failure, validation issue |
| `production-phase` | Deployment, release, live operations | Production bug, deployment failure |

**Aliases**: design, planning, architecture → `design-phase`

### Component Tags
Identify which system component is affected.

| Tag | Description | Example Usage |
|-----|-------------|---------------|
| `crawler-component` | Web crawling, scraping, page analysis | Crawler timeout, parsing error |
| `storage-component` | Database, persistence, data storage | Storage failure, query timeout |
| `api-component` | API endpoints, controllers, routing | API error, endpoint failure |

**Aliases**: crawler, scraping, web → `crawler-component`

### Issue Tags
Classify the type of problem encountered.

| Tag | Description | Example Usage |
|-----|-------------|---------------|
| `validation-error` | Data validation, input checks, schema violations | Invalid input, schema mismatch |
| `performance-issue` | Slow operations, optimization needs | Slow query, timeout |
| `memory-problem` | Memory leaks, OOM, resource exhaustion | Memory leak, high RAM usage |
| `race-condition` | Concurrency issues, threading problems | Race condition, deadlock |

**Aliases**: validation-error, invalid-data → `validation-error`

### Severity Tags
Indicate priority and impact level.

| Tag | Description | Example Usage |
|-----|-------------|---------------|
| `critical-blocker` | Blocks progress, must fix immediately | Production down, data loss |
| `high-priority` | Important but not blocking | Major bug, significant impact |
| `medium-impact` | Moderate importance | Minor bug, enhancement needed |

**Aliases**: critical, blocker, urgent → `critical-blocker`

### Action Tags
Suggest required actions or solutions.

| Tag | Description | Example Usage |
|-----|-------------|---------------|
| `needs-refactor` | Code needs restructuring | Technical debt, code smell |
| `needs-validation` | Missing validation layer | Add input checks, schema validation |
| `needs-retry-logic` | Requires resilience patterns | Add retry, circuit breaker |

**Aliases**: refactor, technical-debt → `needs-refactor`

## Tag Normalization Rules

The Tag Intelligence Engine automatically normalizes tags to the standard taxonomy:

```python
# Examples of normalization
"design" → "design-phase"
"memory leak" → "memory-problem"
"API" → "api-component"
"critical" → "critical-blocker"
```

### Normalization Process
1. Convert to lowercase
2. Strip whitespace
3. Map aliases to canonical tags
4. Remove duplicates
5. Sort alphabetically

## Tag Combination Examples

### Common Patterns

**Example 1: Design Phase Validation**
```yaml
tags: [design-phase, api-component, validation-error, needs-validation]
interpretation: API design missing input validation
recommendation: Add input validation design early in planning
```

**Example 2: Implementation Memory Issue**
```yaml
tags: [implementation-phase, crawler-component, memory-problem, high-priority]
interpretation: Crawler memory leak during development
recommendation: Implement memory profiling during development
```

**Example 3: Production Race Condition**
```yaml
tags: [production-phase, storage-component, race-condition, critical-blocker]
interpretation: Critical database concurrency issue in production
recommendation: Add transaction isolation, implement row-level locking
```

## Prevention Rule Generation

When a tag combination appears **2+ times**, the system generates a prevention rule.

### Pairwise & Triple Subsets (v2.0)

Tag combinations are decomposed into **pairwise and triple** subsets before storage and matching. Previously, full n-tuples of 8-12 tags produced nearly unique combinations that never matched. Now:

```
Input: ["implementation-phase", "sse-streaming", "memory-problem", "high-priority"]
Output subsets:
  Pairs:   (implementation-phase, sse-streaming), (implementation-phase, memory-problem), ...
  Triples: (implementation-phase, sse-streaming, memory-problem), ...
```

This enables actual pattern detection — pairs like `(sse-streaming, memory-problem)` recur across multiple dispatches and generate meaningful prevention rules.

**Hierarchical matching**: If a pair matches, the system checks whether any triple containing those tags also matches, providing more specific recommendations when available.

### Rule Components
- **Tag Combination**: Sorted pairwise or triple tuple of normalized tags
- **Rule Type**: Classified based on tag content (critical-prevention, validation-check, etc.)
- **Description**: Human-readable pattern description
- **Recommendation**: Actionable steps to prevent recurrence
- **Confidence**: 0.0-1.0 based on occurrence count (max at 10 occurrences)
- **Status**: Rules are queued in `pending_rules.json` for operator review (G-L1: never auto-activated)

### Rule Types
| Type | Triggered By | Purpose |
|------|--------------|---------|
| `critical-prevention` | Contains `critical-blocker` | Prevent critical failures |
| `validation-check` | Contains `validation-error` | Add validation layers |
| `performance-optimization` | Contains `performance-issue` | Optimize operations |
| `memory-management` | Contains `memory-problem` | Prevent memory issues |
| `concurrency-control` | Contains `race-condition` | Handle concurrency safely |
| `general-prevention` | Other combinations | General pattern prevention |

## Usage Examples

### CLI Analysis
```bash
# Analyze tag combination
python3 tag_intelligence.py analyze validation-error api-component \
  --phase implementation --terminal T1 --outcome failure

# Query prevention rules
python3 tag_intelligence.py rules --min-confidence 0.7

# Get statistics
python3 tag_intelligence.py stats
```

### Python API
```python
from tag_intelligence import TagIntelligenceEngine

engine = TagIntelligenceEngine()

# Analyze tags
result = engine.analyze_multi_tag_patterns(
    tags=["memory", "crawler", "critical"],
    phase="production-phase",
    terminal="T1",
    outcome="failure"
)

# Query prevention rules
rules = engine.query_prevention_rules(
    tags=["validation-error"],
    min_confidence=0.5
)
```

## Integration with Intelligence System

The Tag Intelligence Engine integrates with `gather_intelligence.py`:

```python
# In gather_for_dispatch()
quality_context = {
    "tags_analyzed": True,
    "tag_combination": normalized_tags,
    "prevention_rules_available": len(rules) > 0,
    "prevention_rule_count": len(rules)
}
```

## Database Schema

### tag_combinations Table
```sql
CREATE TABLE tag_combinations (
    id INTEGER PRIMARY KEY,
    tag_tuple TEXT UNIQUE NOT NULL,
    occurrence_count INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    phases TEXT,
    terminals TEXT,
    outcomes TEXT
)
```

### prevention_rules Table
```sql
CREATE TABLE prevention_rules (
    id INTEGER PRIMARY KEY,
    tag_combination TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    description TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    triggered_count INTEGER DEFAULT 0,
    last_triggered TEXT
)
```

## Tag Evolution Guidelines

### Adding New Tags
1. Verify tag doesn't exist (check all aliases)
2. Assign to appropriate category
3. Add normalization rule to `normalize_tags()`
4. Update this documentation
5. Run tests to verify normalization

### Retiring Tags
1. Mark as deprecated in this doc
2. Add migration rule to normalization
3. Update existing database entries
4. Keep normalization for backwards compatibility

## Best Practices

1. **Be Specific**: Use combination of phase + component + issue for clarity
2. **Avoid Redundancy**: Don't use multiple severity tags
3. **Action-Oriented**: Include action tags when clear solution exists
4. **Consistent Phrasing**: Use verb-noun format for action tags
5. **Minimal Set**: Use 2-4 tags per issue for optimal pattern matching

## Recommendation Manager

The `RecommendationManager` (in `tag_intelligence.py`) manages structured recommendations derived from tag patterns:

### Recommendation Schema
```json
{
  "type": "claude_md_patch|prevention_rule|routing_hint",
  "target": "file_path_or_component",
  "symptom": "detected_issue",
  "evidence_ids": ["dispatch_1", "receipt_2", "OI-042"],
  "confidence": 0.75,
  "created_at": "2026-03-28T14:00:00Z",
  "id": "sha1_hash_12chars",
  "status": "pending|superseded|accepted"
}
```

### Governance Rules
- **G-L1**: Prevention rules are never auto-activated — queued in `pending_rules.json`
- **G-L2**: Evidence trail required — `ValueError` if `evidence_ids` is empty
- **G-L8**: Maximum 5 active pending recommendations. Excess supersedes lowest-confidence
- Stale pending edits (>7 days) are automatically flagged for operator review
- Duplicate recommendations for the same `target + symptom` are merged or superseded

## References

- Tag Intelligence Engine: `scripts/tag_intelligence.py`
- Recommendation Manager: `scripts/tag_intelligence.py` (RecommendationManager class)
- Tests: `tests/test_tag_intelligence.py`
- Integration: `scripts/gather_intelligence.py`
