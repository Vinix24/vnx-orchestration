# Supabase Optimization Template

## Optimization Request
- **Date**: {{DATE}}
- **Target**: {{TARGET_TABLE}}
- **Issue**: {{ISSUE_DESCRIPTION}}
- **Priority**: {{PRIORITY}}

## Current Performance
- Query time p95: {{CURRENT_P95}}ms
- Connection pool usage: {{POOL_USAGE}}%
- Table size: {{TABLE_SIZE}}
- Index count: {{INDEX_COUNT}}

## Analysis Steps
- [ ] Analyze slow query log
- [ ] Check missing indexes
- [ ] Review RLS policies
- [ ] Examine table bloat
- [ ] Check connection pooling

## Optimization Plan
1. {{OPTIMIZATION_1}}
2. {{OPTIMIZATION_2}}
3. {{OPTIMIZATION_3}}

## Expected Impact
- Query time reduction: {{EXPECTED_REDUCTION}}%
- Resource usage: {{RESOURCE_IMPACT}}
- Maintenance window: {{MAINTENANCE_TIME}}

## Migration Safety
- [ ] Backup created
- [ ] Rollback plan documented
- [ ] Staging tested
- [ ] Zero-downtime approach

## Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []