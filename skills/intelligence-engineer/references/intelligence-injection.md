# Intelligence Injection

How VNX feeds learned patterns back into T0 sessions. The "memory" of the governance system.

## The injection pipeline

```
SessionStart hook
   │
   ▼
build_t0_state.py
   │
   ├─→ Read central QI: success_patterns, antipatterns, prevention_rules
   │   filtered by (project_id, valid_until IS NULL, confidence >= 0.7)
   │
   ├─→ Read central RC: recent dispatches, open incidents, escalations
   │   filtered by (project_id, recency window)
   │
   ├─→ Score relevance via scope_match():
   │   - exact project_id match (required)
   │   - tag overlap (Jaccard or substring)
   │   - track / risk_class match (boost)
   │   - recency decay (half-life ~30 days for patterns)
   │
   ├─→ Top-N by score → write into t0_state.json:
   │   {
   │     "strategic_state": {...},
   │     "intelligence": {
   │       "success_patterns": [{ pattern_id, title, body, confidence, why_relevant }, ...],
   │       "antipatterns": [...],
   │       "open_incidents": [...],
   │       "recent_dispatches": [...]
   │     }
   │   }
   │
   └─→ Record injection in rc.intelligence_injections
       (session_id, pattern_id, scope_match JSON, injected_at)
```

T0 then reads `t0_state.json` automatically (CLAUDE.md → SessionStart hook).

## Scope match logic

The match decides "is this past pattern relevant to the current session?". Code in `scripts/lib/intelligence_injection.py:scope_match()`.

### Hard filters (required)

- `project_id == current_project_id` — patterns from other projects don't apply (cross-project federation is a separate read-only aggregator, P1)
- `valid_until IS NULL` (or `> now()`) — invalidated patterns excluded
- `confidence >= threshold` — default 0.7, tunable per project

### Soft filters (scored)

- **Tag overlap**: Jaccard coefficient of pattern tags vs current session tags. Default weight: 0.4
- **Track match**: pattern's source track == current track. Boost: 0.2
- **Risk class match**: pattern's risk_class == current dispatch risk_class. Boost: 0.1
- **Recency decay**: `score *= exp(-age_days / half_life)`, half_life=30 by default

Final score = weighted sum. Top-N by score are injected.

## Common bugs in scope match

### Tag overlap with stale tags
If a pattern was tagged `crawler-v1` but the project moved to `crawler-v2`, no overlap. Two fixes:
- Manually re-tag patterns when a project's vocabulary changes
- Use semantic similarity (embeddings) instead of literal tag overlap (advanced, P2 work)

### Project_id slug drift
Pattern stored under `vnx-roadmap-autopilot`; registry renames to `vnx-orchestration`. Patterns no longer match. Fix: rename script that updates QI tables when registry changes (operator-triggered).

### Confidence inflation
Pattern starts at 0.5 confidence. If it's used 100x with success → bumped to 0.99. But if usage is biased (always for one type of dispatch), it shouldn't generalize. The `confidence_events` table records every change for audit; periodically review.

### Cross-project leakage (architectural risk)
If you forget `WHERE project_id = ?` in a query, you'll inject mc's patterns into seocrawler's T0. This is exactly the kind of bug `database-engineer` skill exists to prevent.

## Federation (cross-project insights, P1)

For higher-level analysis ("which patterns are universally good across projects?"), VNX has a separate read-only aggregator: `scripts/aggregator/build_central_view.py`. It:

- Queries the central DB across all projects
- Computes cross-project statistics (e.g. "this pattern's success rate across projects")
- Writes to a separate aggregate view (read-only, never injected directly into T0)

Purpose: operator dashboard / analytics, not real-time T0 injection. Real-time injection always project-scoped.

## How to query for injection-eligible patterns

```sql
-- Top 10 success patterns for current project, by confidence
SELECT pattern_id, title, body, confidence, tags
FROM success_patterns
WHERE project_id = ?
  AND (valid_until IS NULL OR valid_until > datetime('now'))
  AND confidence >= 0.7
ORDER BY confidence DESC, last_updated DESC
LIMIT 10;

-- With tag-overlap scoring
SELECT pattern_id, title, body,
       confidence * (
         (LENGTH(tags) - LENGTH(REPLACE(tags, ?, ''))) / LENGTH(?)
       ) AS score
FROM success_patterns
WHERE project_id = ?
  AND valid_until IS NULL
ORDER BY score DESC
LIMIT 10;
```

## How to record an injection

```python
def record_injection(con, session_id, pattern_id, dispatch_id, project_id, scope_match):
    con.execute("""
        INSERT INTO intelligence_injections
          (session_id, pattern_id, dispatch_id, scope_match, project_id)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, pattern_id, dispatch_id, json.dumps(scope_match), project_id))
    con.commit()
```

## Common cookbook queries

### Did pattern X get injected for current session?
```sql
SELECT injected_at, scope_match
FROM intelligence_injections
WHERE session_id = ? AND pattern_id = ?;
```

### How often does pattern X get injected?
```sql
SELECT COUNT(*) AS injections, MAX(injected_at) AS last_injection
FROM intelligence_injections
WHERE pattern_id = ?
GROUP BY project_id;
```

### Patterns never injected (dead weight?)
```sql
SELECT sp.pattern_id, sp.title, sp.confidence, sp.created_at
FROM success_patterns sp
LEFT JOIN intelligence_injections ii ON sp.pattern_id = ii.pattern_id
WHERE ii.id IS NULL
  AND sp.project_id = ?
  AND sp.created_at < datetime('now', '-30 days');
```

### Confidence trend per pattern
```sql
SELECT event_at, confidence_before, confidence_after, reason
FROM confidence_events
WHERE pattern_id = ?
ORDER BY event_at DESC;
```

## Tuning knobs

- `INTELLIGENCE_CONFIDENCE_THRESHOLD` — env var, default 0.7
- `INTELLIGENCE_TOP_N` — number of patterns injected per session, default 5
- `INTELLIGENCE_RECENCY_HALF_LIFE_DAYS` — default 30
- `INTELLIGENCE_TAG_OVERLAP_WEIGHT` — default 0.4

These are typically left at defaults; tune only if injection precision/recall is observably wrong.

## Failure mode: T0 sees wrong intelligence

Symptoms: T0's `t0_state.json` includes patterns that don't apply to current work.

Diagnosis:
1. Check `intelligence_injections` for the latest session — did it pick wrong patterns?
2. Look at the `scope_match` JSON — what was the score breakdown?
3. Most common cause: tag overlap is too greedy (treating substring matches as full matches)
4. Second-most common: confidence_threshold too low → noisy patterns leak through

Fix: tighten scope_match logic (use exact tag match instead of substring), bump threshold to 0.85, OR add operator review of injection log periodically.
