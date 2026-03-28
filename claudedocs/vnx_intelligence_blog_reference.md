# VNX Intelligence System — Blog Reference Map

Working reference document mapping all code paths, data flows, and documentation for the VNX self-learning intelligence pipeline. Line numbers verified via grep on 2026-03-28.

---

## 1. The Problem (Before)

| Symptom | Evidence |
|---------|----------|
| Learning loop inert | `pattern_usage` — 31 rows, all `used_count=0`, `ignored_count=0`, `confidence=1.0` |
| Session analytics empty | `SELECT COUNT(*) FROM session_analytics` → 0 rows |
| Intelligence = noise | `t0_intelligence.ndjson` — 78MB, 99% `terminal_status` events |
| Recommendations empty | `t0_recommendations.json` → `"total_recommendations": 0` |
| Prevention rules empty | `prevention_rules` table → 0 rows |
| Workers blind | T1-T3 had no intelligence injection hook |
| Tag tuples useless | Full 8-12 n-tuples — nearly unique, no pattern matching possible |

**Root cause**: No component ever called `record_pattern_offer()` or `record_pattern_adoption()` — the feedback loop was open-circuited. Data existed in the DB schema but nothing wrote to it.

---

## 2. Architecture Overview — All Intelligence Scripts

### Core Python Scripts

| # | File | Purpose | Key Entry Point |
|---|------|---------|----------------|
| 1 | `scripts/learning_loop.py` | Pattern usage tracking, confidence adjustment, prevention rule queuing | `daily_learning_cycle()` :577 |
| 2 | `scripts/gather_intelligence.py` | Pattern extraction, offer/adoption recording, intelligence serving | `T0IntelligenceGatherer` :84 |
| 3 | `scripts/conversation_analyzer.py` | Session JSONL parsing, analytics extraction, digest generation | `ConversationAnalyzer` :765 |
| 4 | `scripts/tag_intelligence.py` | Tag combination analysis, prevention rule generation, recommendation management | `TagIntelligenceEngine` :38 |
| 5 | `scripts/build_t0_quality_digest.py` | 3-section quality digest, NDJSON append-only output | `_assemble_digest()` :415 |
| 6 | `scripts/check_intelligence_health.py` | Health verification, session count, usage tracking checks | `check_health()` :250 |
| 7 | `scripts/intelligence_daemon.py` | Hourly extraction, daily hygiene, PR auto-discovery | `IntelligenceDaemon` :71 |
| 8 | `scripts/generate_t0_session_brief.py` | Model performance summary from session_analytics | `generate_brief()` :195 |
| 9 | `scripts/cached_intelligence.py` | TTL cache layer for intelligence patterns | `CachedIntelligence` :130 |
| 10 | `scripts/intelligence_daemon_monitor.py` | Schema validation, dashboard state sync | `validate_monitor_schema_compatibility()` :38 |

### Shell Scripts

| # | File | Purpose |
|---|------|---------|
| 11 | `scripts/userpromptsubmit_intelligence_inject_v5.sh` | T0 intelligence injection hook (change-detection based) |
| 12 | `scripts/userpromptsubmit_worker_intelligence_inject.sh` | T1-T3 worker intelligence injection (<400 tokens) |
| 13 | `scripts/nightly_intelligence_pipeline.sh` | Consolidated 12-phase nightly pipeline |
| 14 | `scripts/conversation_analyzer_nightly.sh` | Legacy nightly analyzer (superseded by #13) |
| 15 | `scripts/intelligence_ack.sh` | T0 ACK flag validation for dispatch gating |
| 16 | `scripts/intelligence_refresh.sh` | Hash cache update on T0 intelligence read |
| 17 | `scripts/sessionstart_t0_intelligence.sh` | Minimal session-start intelligence summary |

### Supporting Scripts

| File | Purpose |
|------|---------|
| `scripts/intelligence_export.py` | Export intelligence DB to NDJSON for git sync |
| `scripts/intelligence_import.py` | Import NDJSON back into intelligence DB |
| `scripts/intelligence_queries.py` | Query API for patterns, rules, sessions |
| `scripts/t0_intelligence_aggregator.py` | Cross-terminal intelligence aggregation |
| `scripts/query_quality_intelligence.py` | Terminal success rate analysis |

---

## 3. Data Flow — The Feedback Loop

```
offer → adoption → confidence → injection
  │         │           │           │
  ▼         ▼           ▼           ▼
 ndjson    ndjson    sqlite     shell hook
```

### Step 1: Pattern Offer
- **Where**: `gather_intelligence.py:record_pattern_offer()` :249
- **What**: Logs `{event: "pattern_offer", pattern_id, terminal, dispatch_id}` to `intelligence_usage.ndjson`
- **When**: Called when patterns are served to any terminal (T0-T3)

### Step 2: Pattern Adoption
- **Where**: `gather_intelligence.py:record_pattern_adoption()` :269
- **What**: Correlates receipt file changes with recently-offered patterns
- **When**: Post-receipt hook checks if edited files match offered pattern references
- **Effect**: Increments `pattern_usage.used_count` in `quality_intelligence.db`

### Step 3: Ignored Pattern Detection
- **Where**: `learning_loop.py:extract_ignored_patterns()` :147
- **What**: Patterns offered but not adopted within dispatch lifecycle
- **Effect**: Increments `pattern_usage.ignored_count`

### Step 4: Confidence Update
- **Where**: `learning_loop.py:update_confidence_scores()` :213
- **What**: Adjusts confidence based on used/ignored ratio
- **Audit**: `_log_confidence_change()` :193 — appends `{timestamp, source, old_value, new_value}` to NDJSON (G-L7)

### Step 5: Intelligence Injection
- **T0**: `userpromptsubmit_intelligence_inject_v5.sh` — recommendations + quality hotspots
- **T1-T3**: `userpromptsubmit_worker_intelligence_inject.sh` — task-relevant patterns (max 3), prevention rules

### Step 6: Nightly Cycle
- **Where**: `learning_loop.py:daily_learning_cycle()` :577
- **Orchestrated by**: `nightly_intelligence_pipeline.sh` phase 4 (:166)
- **Phases**: load metrics → extract used → extract ignored → update confidence → generate rules → archive candidates → save → report

---

## 4. Intelligence Injection

### T0 Path (`userpromptsubmit_intelligence_inject_v5.sh`)
- Change-detection based: compares hash of tags digest, quality digest, recommendations
- Only injects when content has changed since last injection
- Focus: recommendations + quality hotspots (not terminal status)

### T1-T3 Path (`userpromptsubmit_worker_intelligence_inject.sh`)
- **Line 7**: Token budget `<400 tokens (≈1600 chars)`
- **Line 19-23**: `safe_exit()` — always allows, never blocks dispatch (A-5)
- **Line 26-32**: Terminal detection from `$VNX_TERMINAL` or `$PWD`
- **Line 108**: Build injection context section
- **Line 151**: Enforce 1600 char ceiling (~400 tokens)
- **Audit**: All injection events logged to `intelligence_usage.ndjson` (G-L7)
- **Graceful degradation**: Missing dispatch or empty intelligence → `{"decision": "allow"}` (A-5)

### Injection Content Structure
```json
{
  "patterns": ["max 3 relevant patterns"],
  "prevention_rules": ["matching rules for dispatch tags"],
  "session_insights": ["prior report findings"]
}
```

---

## 5. Tag Intelligence

### Before: Full N-Tuples (Broken)
- Tags from dispatches stored as full 8-12 element tuples
- Example: `["implementation-phase", "sse-streaming", "api", "testing", "T1", "backend", "error-handling", "refactor"]`
- Nearly unique — no two dispatches share the same full tuple → zero pattern matching

### After: Pairwise + Triple Subsets
- **Where**: `tag_intelligence.py:generate_tag_subsets()` :196
- **Logic** (lines 196-220):
  - `len <= 3`: use as-is; if triple, also generate pair combinations
  - `len > 3`: generate all pairs + all triples (never full n-tuple)
- **Example output**: `["implementation-phase", "sse-streaming"]`, `["api", "testing", "backend"]`

### Hierarchical Matching
- **Where**: `tag_intelligence.py:analyze_multi_tag_patterns()` :310
- If a pair matches a known pattern, check if any triple containing those tags also matches
- Enables progressive specificity: pair → triple → prevention rule

### Prevention Rule Generation
- **Where**: `tag_intelligence.py:_generate_prevention_rule()` :456
- **Classification**: `_classify_rule_type()` :491 — critical/validation/performance/memory
- **Confidence**: Based on occurrence count, capped at 0.95

---

## 6. Governance Rules — Code Enforcement Map

| Rule | Enforcement | File:Line |
|------|-------------|-----------|
| **G-L1**: No auto-activation | `update_terminal_constraints()` writes to `pending_rules.json`, not DB | `learning_loop.py:387-434` |
| **G-L2**: Evidence trail | `add_recommendation()` requires `evidence_ids` parameter | `tag_intelligence.py:749`, test at `test_tag_intelligence.py:505` |
| **G-L3**: Confidence informational | Confidence used for ranking only, not blocking decisions | `tag_intelligence.py:800-810` |
| **G-L4**: Archive confirmation | `archive_unused_patterns()` writes to `pending_archival.json` | `learning_loop.py:436-493` |
| **G-L5**: LLM rules need review | All generated rules go through pending queue → operator | `learning_loop.py:388-391` (docstring) |
| **G-L6**: NDJSON append-only | `_append_ndjson()` opens file in `"a"` (append) mode | `build_t0_quality_digest.py:460-465` |
| **G-L7**: Injection audit | `record_pattern_offer()` + `record_pattern_adoption()` log to `intelligence_usage.ndjson` | `gather_intelligence.py:249-298` |
| **G-L8**: Max 5 pending | `MAX_PENDING_RECOMMENDATIONS = 5`, lowest-confidence superseded when exceeded | `tag_intelligence.py:32, 802-808` |

### Governance Test Coverage

| Rule | Test | File:Line |
|------|------|-----------|
| G-L1 | `test_update_terminal_constraints_writes_pending_rules_json` | `tests/test_learning_feature.py:639` |
| G-L2 | `test_add_recommendation_requires_evidence` | `tests/test_tag_intelligence.py:505` |
| G-L4 | `test_archive_unused_patterns_writes_pending_archival_json` | `tests/test_learning_feature.py:697` |
| G-L6 | `test_ndjson_output_is_append_only` | `tests/test_learning_feature.py:535` |
| G-L8 | `test_cap_at_max_pending` | `tests/test_tag_intelligence.py:541` |

---

## 7. Quality Digest

### 3-Section Format (`build_t0_quality_digest.py`)

| Section | Function | Line | Content |
|---------|----------|------|---------|
| Operational Defects | `build_operational_defects()` | :117 | Code hotspots, critical issues from receipts |
| Prompt/Config Tuning | `build_prompt_config_tuning()` | :175 | Prevention rules, pending edits, config recommendations |
| Governance Health | `build_governance_health()` | :295 | SPC alerts, governance metrics, compliance status |

### Digest Assembly
- **Where**: `_assemble_digest()` :415 — combines all 3 sections
- **Cap**: Top 5 recommendations per section
- **Evidence**: Each recommendation includes receipt IDs, file paths, dispatch IDs
- **Output**: `_append_ndjson()` :460 — append-only NDJSON to `.vnx-data/state/` (G-L6)
- **Compat**: `_write_compat_json()` :468 — writes latest digest as JSON for backward compatibility

### Lookback Window
- Recommendation engine uses 24h lookback (widened from original 60min)
- Receipts loaded via `_load_recent_receipts()` :46

---

## 8. Testing Evidence

### Test Files

| File | Tests | Focus |
|------|-------|-------|
| `tests/test_learning_feature.py` | 26 | Offer/adoption tracking, worker injection, nightly pipeline, digest format, confidence logging, G-L1/G-L4 |
| `tests/test_tag_intelligence.py` | 29 | Tag normalization, subset generation, combination tracking, prevention rules, recommendation manager, G-L2/G-L8 |
| `tests/test_conversation_analyzer.py` | 44 | Session parsing, heuristic detection, analytics storage, idempotency, model normalization, digest generation |
| `tests/test_check_intelligence_health_refactor.py` | 4 | Health status thresholds, receipt coverage, stale detection |
| `tests/test_intelligence_daemon_paths.py` | 4 | Canonical path writes, rollback mode, dashboard sync |
| `tests/test_intelligence_daemon_monitor_as07.py` | 3 | Schema compatibility, monitor queries |
| `tests/test_session_gc.py` | 1 | Session garbage collection dry-run and apply |

**Total test functions across intelligence suite: 131**

### Key Test Names (Governance Verification)

| Test | Verifies |
|------|----------|
| `test_update_terminal_constraints_writes_pending_rules_json` | G-L1: rules → pending file, not DB |
| `test_update_terminal_constraints_deduplicates_rules` | G-L1: dedup by rule ID |
| `test_add_recommendation_requires_evidence` | G-L2: evidence_ids mandatory |
| `test_archive_unused_patterns_writes_pending_archival_json` | G-L4: archival → pending file |
| `test_archive_unused_patterns_skips_recent_patterns` | G-L4: recent patterns preserved |
| `test_ndjson_output_is_append_only` | G-L6: two runs = two lines |
| `test_log_confidence_change_appends_to_ndjson` | G-L7: audit trail for confidence |
| `test_cap_at_max_pending` | G-L8: max 5, supersedes lowest |
| `test_record_pattern_offer_writes_ndjson` | Offer audit trail |
| `test_record_pattern_adoption_increments_used_count` | Adoption → DB update |
| `test_digest_has_three_sections` | 3-section digest format |
| `test_injection_stays_under_token_budget` | <400 token budget |

---

## 9. Key Metrics

| Metric | Value | Source |
|--------|-------|--------|
| Intelligence scripts (Python) | 15 | `scripts/` directory |
| Intelligence scripts (Shell) | 7 | `scripts/` directory |
| Total test functions | 131 | 7 test files in `tests/` |
| Nightly pipeline phases | 12 (0-11) | `nightly_intelligence_pipeline.sh` :132-214 |
| Max patterns per injection | 3 | `userpromptsubmit_worker_intelligence_inject.sh` :7 |
| Token budget per injection | <400 (~1600 chars) | `userpromptsubmit_worker_intelligence_inject.sh` :7, :151 |
| Max pending recommendations | 5 | `tag_intelligence.py` :32 (`MAX_PENDING_RECOMMENDATIONS`) |
| Stale pending edit threshold | 7 days | `tag_intelligence.py:mark_stale_pending_edits()` :816 |
| Confidence cap | 0.95 | `tag_intelligence.py:_generate_prevention_rule()` :456 |
| Digest lookback window | 24h | `build_t0_quality_digest.py:_load_recent_receipts()` :46 |
| Digest sections | 3 (Operational Defects, Prompt/Config Tuning, Governance Health) | `build_t0_quality_digest.py` :117, :175, :295 |
| Max recommendations per digest section | 5 | `build_t0_quality_digest.py:_assemble_digest()` :415 |
| Recommendation types (MVP) | 3 (`claude_md_patch`, `prevention_rule`, `routing_hint`) | `tag_intelligence.py:_generate_recommendation()` :508 |
| PRs in feature | 5 (PR-0 through PR-4) | `FEATURE_PLAN.md` |
| Governance rules | 8 (G-L1 through G-L8) | `FEATURE_PLAN.md` |
| Architecture rules | 10 (A-1 through A-10) | `FEATURE_PLAN.md` |

---

## 10. Data Storage Map

| Data | Location | Format |
|------|----------|--------|
| Pattern usage | `quality_intelligence.db` → `pattern_usage` table | SQLite |
| Session analytics | `quality_intelligence.db` → `session_analytics` table | SQLite |
| Prevention rules | `quality_intelligence.db` → `prevention_rules` table | SQLite |
| Tag combinations | `quality_intelligence.db` → `tag_combinations` table | SQLite |
| Offer/adoption audit | `.vnx-data/state/intelligence_usage.ndjson` | NDJSON (append-only) |
| Confidence changes | `.vnx-data/state/confidence_changes.ndjson` | NDJSON (append-only) |
| Quality digest history | `.vnx-data/state/quality_digest.ndjson` | NDJSON (append-only, G-L6) |
| Recommendations | `.vnx-data/state/t0_recommendations.json` | JSON |
| Pending rules | `.vnx-data/state/pending_rules.json` | JSON (G-L1) |
| Pending archival | `.vnx-data/state/pending_archival.json` | JSON (G-L4) |
| Intelligence health | `.vnx-data/state/intelligence_health.json` | JSON |
| Pipeline run log | `.vnx-data/state/nightly_pipeline.ndjson` | NDJSON |
| Receipts | `.vnx-data/state/t0_receipts.ndjson` | NDJSON |

---

## 11. Nightly Pipeline Phases

Consolidated pipeline: `scripts/nightly_intelligence_pipeline.sh`

| Phase | Line | Command | Purpose |
|-------|------|---------|---------|
| 0 | :132 | `quality_db_init.py` | DB schema migrations |
| 1a | :140 | `code_quality_scanner.py` | Quality scan |
| 1b | :141 | `code_snippet_extractor.py` | Snippet extraction |
| 1c | :142 | `doc_section_extractor.py` | Documentation extraction |
| 2 | :145 | `conversation_analyzer.py` | Session analysis |
| 3 | :150 | `link_sessions_dispatches.py` | Session-dispatch linkage |
| 4 | :166 | `learning_loop.py run` | Learning cycle (confidence updates) |
| 5 | :169 | `tag_intelligence.py stale` | Mark stale pending edits |
| 6 | :172 | `generate_t0_session_brief.py` | T0 session brief |
| 7 | :175 | `governance_aggregator.py --backfill` | Governance metrics |
| 8 | :178 | `generate_suggested_edits.py` | Suggested edits |
| 9 | :181 | `build_t0_quality_digest.py` | Quality digest (NDJSON) |
| 10 | :184 | `generate_t0_recommendations.py` | Recommendations engine |

**Error handling**: `run_phase()` :78 — each phase runs independently; failure logged to `nightly_pipeline.ndjson` but does not block subsequent phases.

---

*Generated 2026-03-28 for VNX Self-Learning Intelligence Pipeline blog post preparation.*
