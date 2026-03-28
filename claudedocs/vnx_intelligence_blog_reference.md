# VNX Intelligence System — Blog Reference Map

> Working reference for blog post. All line numbers verified via grep. Total system: 12 scripts, 8,222 lines of code, 425 tests across 38 files.

---

## 1. The Problem (Before)

The intelligence pipeline existed but was completely inert:

| Metric | Before | Evidence |
|--------|--------|----------|
| `pattern_usage` rows | 31 | All with `used_count=0, ignored_count=0, confidence=1.0` |
| `session_analytics` rows | 0 | `SELECT COUNT(*) FROM session_analytics` → 0 |
| `t0_recommendations.json` | Empty | `"total_recommendations": 0` |
| `prevention_rules` count | 0 | No rules ever generated |
| `t0_intelligence.ndjson` | 78 MB | 99% `terminal_status` events (noise) |
| Worker intelligence (T1-T3) | None | No injection hook existed |

**Root causes:**
- `record_pattern_offer()` / `record_pattern_adoption()` did not exist — no code ever incremented `used_count`
- `conversation_analyzer.py` failed silently on session discovery (path mismatch)
- `tag_intelligence.py` generated full 8-12 element n-tuples (nearly unique, no pattern matching possible)
- `update_terminal_constraints()` auto-activated rules (governance violation)
- `archive_unused_patterns()` auto-archived without confirmation

---

## 2. Architecture Overview

### Intelligence Scripts (12 files, 8,222 LOC)

| # | File | Lines | Role |
|---|------|-------|------|
| 1 | `scripts/learning_loop.py` | 678 | Core learning cycle: extract signals → update confidence → generate rules → archive |
| 2 | `scripts/gather_intelligence.py` | 1,740 | Pattern offering, adoption tracking, task-relevant queries, relevance scoring |
| 3 | `scripts/conversation_analyzer.py` | 1,087 | 4-phase session analysis: parse → heuristic → deep LLM → store |
| 4 | `scripts/tag_intelligence.py` | 942 | Tag combination engine, prevention rules, recommendation manager |
| 5 | `scripts/intelligence_daemon.py` | 933 | Hourly extraction, daily hygiene, health reporting, PR auto-discovery |
| 6 | `scripts/build_t0_quality_digest.py` | 548 | 3-section quality digest with evidence trails, NDJSON output |
| 7 | `scripts/userpromptsubmit_intelligence_inject_v5.sh` | 170 | T0 intelligence injection (hash dedup, quality/tags/recommendations) |
| 8 | `scripts/userpromptsubmit_worker_intelligence_inject.sh` | 168 | T1-T3 dispatch-scoped intelligence injection (400-token budget) |
| 9 | `scripts/check_intelligence_health.py` | 346 | Health status: healthy/degraded/unhealthy based on daemon + coverage |
| 10 | `scripts/receipt_processor_v4.sh` | 1,207 | Receipt processing with flood protection, dedup, rate limiting |
| 11 | `scripts/generate_t0_session_brief.py` | 252 | Model performance aggregation from session_analytics |
| 12 | `scripts/conversation_analyzer_nightly.sh` | 151 | Consolidated 7-phase nightly pipeline orchestrator |

### Key Classes

| Class | File | Line | Purpose |
|-------|------|------|---------|
| `LearningLoop` | `learning_loop.py` | 47 | Confidence scoring, adoption signal processing |
| `IntelligenceGatherer` | `gather_intelligence.py` | 87 | Pattern queries, offer/adoption tracking |
| `TagIntelligenceEngine` | `tag_intelligence.py` | 41 | Tag combination analysis, prevention rules |
| `RecommendationManager` | `tag_intelligence.py` | 714 | Structured recommendations with evidence + caps |
| `ConversationAnalyzer` | `conversation_analyzer.py` | 765 | 4-phase session analysis orchestrator |
| `SessionParser` | `conversation_analyzer.py` | 171 | JSONL parsing, token/tool counting |
| `HeuristicDetector` | `conversation_analyzer.py` | 312 | Pattern detection without LLM |
| `DeepAnalyzer` | `conversation_analyzer.py` | 445 | LLM-powered deep session analysis |
| `DigestGenerator` | `conversation_analyzer.py` | 620 | Session digest markdown generation |
| `IntelligenceDaemon` | `intelligence_daemon.py` | 74 | Daemon loop: hourly + daily + health |

---

## 3. Data Flow — The Feedback Loop

```
offer → adoption → confidence → injection
  │         │           │            │
  │         │           │            └─ userpromptsubmit_worker_intelligence_inject.sh:108-154
  │         │           └─ learning_loop.py:213 (update_confidence_scores)
  │         └─ gather_intelligence.py:269 (record_pattern_adoption)
  └─ gather_intelligence.py:249 (record_pattern_offer)
```

### Step 1: Pattern Offer
- **Function:** `IntelligenceGatherer.record_pattern_offer()` — `gather_intelligence.py:249`
- Logs to `intelligence_usage.ndjson` with `pattern_id`, `terminal`, `dispatch_id`, `timestamp`
- Called by `_register_offered_patterns()` — `gather_intelligence.py:733`
- Updates `pattern_usage.last_offered` timestamp in DB

### Step 2: Pattern Adoption
- **Function:** `IntelligenceGatherer.record_pattern_adoption()` — `gather_intelligence.py:269`
- Increments `used_count` in `pattern_usage` table
- Logs adoption event to `intelligence_usage.ndjson`
- **Function:** `IntelligenceGatherer.record_adoption_from_receipt()` — `gather_intelligence.py:300`
- Correlates receipt file changes with recently-offered patterns
- Called post-receipt to detect implicit adoption (file overlap)

### Step 3: Confidence Update
- **Function:** `LearningLoop.update_confidence_scores()` — `learning_loop.py:213`
- **Boost adopted:** `confidence = min(confidence * 1.10, 2.0)` — `learning_loop.py:233`
- **Decay ignored:** `confidence = max(confidence * 0.95, 0.1)` — `learning_loop.py:254`
- All changes logged via `_log_confidence_change()` — `learning_loop.py:193` (G-L7)

### Step 4: Intelligence Injection
- T0: `userpromptsubmit_intelligence_inject_v5.sh` — quality digest + tags + recommendations
- T1-T3: `userpromptsubmit_worker_intelligence_inject.sh` — dispatch-scoped patterns + prevention rules

### Orchestration
- **Daily cycle:** `LearningLoop.daily_learning_cycle()` — `learning_loop.py:577`
  - Extract used patterns → extract ignored → update confidence → generate prevention rules → archive
- **Daemon:** `IntelligenceDaemon.run()` — `intelligence_daemon.py:862`
  - Hourly extraction (3600s interval), daily hygiene at 18:00

---

## 4. Intelligence Injection

### T0 Path — `userpromptsubmit_intelligence_inject_v5.sh`

| Section | Lines | Source File | Hash Cache |
|---------|-------|-------------|------------|
| Tags digest | 50-64 | `t0_tags_digest.json` | `.last_tags_hash` |
| Quality digest | 67-81 | `t0_quality_digest.json` | `.last_quality_hash` |
| Recommendations | 84-93 | `t0_recommendations.json` | `.last_recommendations_hash` |

**Hash dedup:** SHA256 per source file. Only injects when content hash changes (lines 54, 71, 88).
**Output format:** `{"decision": "allow", "additionalContext": "..."}` (lines 160-171)

### T1-T3 Path — `userpromptsubmit_worker_intelligence_inject.sh`

| Step | Lines | Description |
|------|-------|-------------|
| Terminal detection | 25-34 | Determine `VNX_TERMINAL` from env or PWD |
| Dispatch resolution | 41-49 | Get `dispatch_id` from `terminal_state.json` `claimed_by` |
| Dispatch file lookup | 51-63 | Search active/completed/pending/staging dirs |
| Task extraction | 65-72 | Extract Gate, Role, Task from dispatch metadata |
| Intelligence query | 74-86 | Call `gather_intelligence.py gather` subcommand |
| Hash dedup | 98-106 | `.last_worker_intel_hash_{TERMINAL_ID}` |
| Build output | 108-154 | Max 3 patterns + 2 prevention rules + 2 session insights |
| Token budget | 151-154 | **400 tokens (≈1600 chars)** — hard ceiling, truncates |
| Audit logging | 156-161 | Log to `intelligence_usage.ndjson` (G-L7) |

**Example injection output:**
```
=== VNX [T1] Dispatch: 20260328-feat-api-B | Gate: gate_pr2 ===
Patterns:
• pattern-42: Use structured error responses in API handlers
• pattern-17: SSE connection cleanup on client disconnect
Prevention:
⚠ rule-3: Validate dispatch scope before file creation
Context:
• Prior T1 session showed 23% error recovery rate on API tasks
```

**Safe degradation (A-5):** `safe_exit()` at line 20 — always returns `{"decision": "allow"}` on any failure.

---

## 5. Tag Intelligence

### Before: Full N-Tuples (Broken)
- `tag_intelligence.py` generated full tag combinations (8-12 elements)
- Example: `["implementation-phase", "sse-streaming", "api-handler", "error-handling", "T1", "P1", "gate_pr2", "backend-developer", "refactor"]`
- Nearly unique per dispatch → no pattern matching possible → 0 prevention rules

### After: Pairwise + Triple Subsets

**Function:** `TagIntelligenceEngine.generate_tag_subsets()` — `tag_intelligence.py:196`
- Decomposes any tag set into **pairs** and **triples** only
- Example input: `["implementation-phase", "sse-streaming", "api-handler", "error-handling"]`
- Output pairs: `["api-handler", "error-handling"]`, `["api-handler", "implementation-phase"]`, ...
- Output triples: `["api-handler", "error-handling", "implementation-phase"]`, ...

**Hierarchical matching:** `TagIntelligenceEngine.query_prevention_rules()` — `tag_intelligence.py:589`
- Query all subsets of input tags
- Sort by specificity (longer tuple = more specific = higher priority)
- Triple match supersedes pair match

### Tag Normalization
**Function:** `TagIntelligenceEngine.normalize_tags()` — `tag_intelligence.py:131`
- Standardizes to taxonomy: `design-phase`, `implementation-phase`, `testing-phase`, `review-phase`
- Component normalization, severity levels, action types
- Alphabetical sorting, deduplication

### Prevention Rule Generation
**Function:** `TagIntelligenceEngine.analyze_multi_tag_patterns()` — `tag_intelligence.py:310`
- Triggers at **2+ occurrences** of same tag subset
- Stores in `prevention_rules` table via `_store_prevention_rule()` — `tag_intelligence.py:548`
- Rule types: `critical-prevention`, `validation-check`, `performance-optimization`, `memory-management`

### Recommendation Schema
**Class:** `RecommendationManager` — `tag_intelligence.py:714`
- Schema: `{type, target, symptom, evidence_ids, confidence, created_at}`
- Types: `claude_md_patch`, `prevention_rule`, `routing_hint`
- **Function:** `add_recommendation()` — `tag_intelligence.py:749`

---

## 6. Governance Rules — Code Enforcement Points

| Rule | What | Enforcement Location | Mechanism |
|------|------|---------------------|-----------|
| **G-L1** | No auto-activation of rules | `learning_loop.py:387` (`update_terminal_constraints`) | Writes to `pending_rules.json`, not DB |
| **G-L2** | Evidence trail required | `tag_intelligence.py:766` (`add_recommendation`) | `ValueError` if `evidence_ids` missing/empty |
| **G-L3** | Confidence is informational | Design principle | No threshold-based auto-actions anywhere |
| **G-L4** | Archive requires confirmation | `learning_loop.py:436` (`archive_unused_patterns`) | Writes to `pending_archival.json`, not DB delete |
| **G-L5** | No LLM rules without review | Design principle | LLM outputs → `pending_edits.json` only |
| **G-L6** | NDJSON append-only | `build_t0_quality_digest.py:460` (`_append_ndjson`) | Append mode file write to `quality_digest.ndjson` |
| **G-L7** | Injection audit logging | `learning_loop.py:193` (`_log_confidence_change`) | All changes → `intelligence_usage.ndjson` |
| **G-L7** | (also) | `userpromptsubmit_worker_intelligence_inject.sh:156` | Injection events → `intelligence_usage.ndjson` |
| **G-L8** | Max 5 pending recommendations | `tag_intelligence.py:800-808` (`add_recommendation`) | Supersedes lowest confidence when full |

### Pending Queues (Human-in-the-Loop)

| Queue File | Written By | Purpose |
|-----------|-----------|---------|
| `pending_rules.json` | `learning_loop.py:398` | Prevention rules awaiting operator approval |
| `pending_archival.json` | `learning_loop.py:455` | Low-confidence patterns awaiting archival confirmation |
| `pending_edits.json` | `tag_intelligence.py:720` | Config/CLAUDE.md edits awaiting review |
| `t0_recommendations.json` | `tag_intelligence.py:749` | Structured recommendations with evidence |

### Stale Detection
- `RecommendationManager.mark_stale_pending_edits()` — `tag_intelligence.py:816`
- Threshold: `STALE_DAYS = 7` — `tag_intelligence.py:35`
- Pending edits older than 7 days marked for operator review

---

## 7. Quality Digest

**File:** `scripts/build_t0_quality_digest.py` (548 lines)

### Constants
| Constant | Value | Line |
|----------|-------|------|
| `MAX_PER_SECTION` | 5 | 39 |
| `LOOKBACK_HOURS` | 24 | 40 |
| `SCHEMA_VERSION` | "2.0" | 41 |

### Three Sections

| Section | Function | Line | Content |
|---------|----------|------|---------|
| **Operational Defects** | `build_operational_defects()` | 117 | Code hotspots from `vnx_code_quality` table |
| **Prompt/Config Tuning** | `build_prompt_config_tuning()` | 175 | Prevention rules, low-confidence patterns, pending edits |
| **Governance Health** | `build_governance_health()` | 295 | SPC alerts, failed gates, governance metrics |

### Evidence Trails
- Each recommendation links to `dispatch_ids`, `receipt_ids`, `file_paths`
- Evidence map built from receipts: `_build_evidence_map()` — line 78
- Pending items loaded: `_load_pending_items()` — line 98

### Output
- **NDJSON (G-L6):** `_append_ndjson()` — line 460 → `quality_digest.ndjson` (append-only)
- **JSON (compat):** `_write_compat_json()` — line 468 → `t0_quality_digest.json` (latest only)
- Assembly: `_assemble_digest()` — line 415

---

## 8. Testing Evidence

### Test Suite: 425 tests across 38 files

#### Intelligence-Specific Test Files (7 files, 91+ learning tests)

| File | Tests | Focus |
|------|-------|-------|
| `tests/test_learning_feature.py` | 26 | Offer/adoption tracking, worker injection, nightly pipeline, digest format, confidence logging |
| `tests/test_tag_intelligence.py` | 49 | Normalization, subset generation, combinations, prevention rules, recommendations |
| `tests/test_conversation_analyzer.py` | 44 | Session parsing, heuristics, deep analysis, digest generation, model normalization |
| `tests/test_pattern_matching.py` | 4 | Database connectivity, keyword extraction, pattern queries |
| `tests/test_check_intelligence_health_refactor.py` | 4 | Health status thresholds, receipt coverage |
| `tests/test_intelligence_daemon_paths.py` | 4 | Canonical path behavior (AS-05) |
| `tests/test_intelligence_daemon_monitor_as07.py` | 3 | Schema compatibility (AS-07) |

**Total intelligence tests: 134**

#### Key Test Names (by PR)

**PR-0 — Usage Signal Pipeline:**
- `test_record_pattern_offer_writes_ndjson` (test_learning_feature.py:132)
- `test_record_pattern_adoption_increments_used_count` (test_learning_feature.py:164)
- `test_record_adoption_from_receipt_correlates_file_paths` (test_learning_feature.py:193)
- `test_update_terminal_constraints_writes_pending_rules_json` (test_learning_feature.py:639)
- `test_archive_unused_patterns_writes_pending_archival_json` (test_learning_feature.py:697)
- `test_log_confidence_change_appends_to_ndjson` (test_learning_feature.py:607)

**PR-1 — Session Analytics:**
- `test_store_session_analytics` (test_conversation_analyzer.py:391)
- `test_idempotent_skip` (test_conversation_analyzer.py:426)
- `test_model_performance_aggregation` (test_conversation_analyzer.py:721)
- `test_parse_assistant_message` (test_conversation_analyzer.py:201)
- `test_heuristic_error_recovery` (test_conversation_analyzer.py:276)

**PR-2 — Worker Intelligence Injection:**
- `test_script_passes_bash_syntax_check` (test_learning_feature.py:240)
- `test_outputs_allow_when_vnx_terminal_unset_and_pwd_unknown` (test_learning_feature.py:249)
- `test_outputs_additional_context_with_dispatch` (test_learning_feature.py:291)
- `test_injection_stays_under_token_budget` (test_learning_feature.py:334)

**PR-3 — Tag Intelligence:**
- `test_four_tags_generates_pairs_and_triples` (test_tag_intelligence.py:149)
- `test_large_tuple_no_full_ntuple` (test_tag_intelligence.py:159)
- `test_cap_at_max_pending` (test_tag_intelligence.py:541)
- `test_add_recommendation_requires_evidence` (test_tag_intelligence.py:505)
- `test_mark_stale_pending_edits` (test_tag_intelligence.py:596)
- `test_hierarchical_ordering` (test_tag_intelligence.py:450)

**PR-4 — Digest + Pipeline:**
- `test_digest_has_three_sections` (test_learning_feature.py:479)
- `test_each_section_capped_at_five` (test_learning_feature.py:491)
- `test_ndjson_output_is_append_only` (test_learning_feature.py:535)
- `test_evidence_trail_fields_present` (test_learning_feature.py:559)
- `test_phase_logging_writes_ndjson_entries` (test_learning_feature.py:429)

---

## 9. Key Metrics

| Metric | Value | Source |
|--------|-------|--------|
| Intelligence scripts | 12 | Architecture overview |
| Total LOC (intelligence) | 8,222 | wc -l across 12 files |
| Pattern database | 31 baseline rows | `pattern_usage` table |
| Confidence boost factor | ×1.10 (cap 2.0) | `learning_loop.py:233` |
| Confidence decay factor | ×0.95 (floor 0.1) | `learning_loop.py:254` |
| Token budget (workers) | 400 tokens (≈1600 chars) | `userpromptsubmit_worker_intelligence_inject.sh:151` |
| Max patterns per injection | 3 | `userpromptsubmit_worker_intelligence_inject.sh:108` |
| Max prevention rules per injection | 2 | `userpromptsubmit_worker_intelligence_inject.sh:108` |
| Max session insights per injection | 2 | `userpromptsubmit_worker_intelligence_inject.sh:108` |
| Max pending recommendations | 5 | `tag_intelligence.py:32` (`MAX_PENDING_RECOMMENDATIONS`) |
| Stale threshold | 7 days | `tag_intelligence.py:35` (`STALE_DAYS`) |
| Digest lookback | 24 hours | `build_t0_quality_digest.py:40` (`LOOKBACK_HOURS`) |
| Max items per digest section | 5 | `build_t0_quality_digest.py:39` (`MAX_PER_SECTION`) |
| Digest schema version | 2.0 | `build_t0_quality_digest.py:41` |
| Hourly extraction interval | 3600s | `intelligence_daemon.py:108` |
| Daily hygiene hour | 18:00 | `intelligence_daemon.py:109` |
| Deep analysis token threshold | 100,000 | `conversation_analyzer.py:51` |
| Deep analysis tool threshold | 100 | `conversation_analyzer.py:52` |
| Session brief lookback | 7 days | `generate_t0_session_brief.py:36` |
| Receipt max age | 24 hours | `receipt_processor_v4.sh:24` |
| Receipt rate limit | 10/min | `receipt_processor_v4.sh:25` |
| Flood threshold | 50 | `receipt_processor_v4.sh:26` |
| Nightly pipeline phases | 7 | `conversation_analyzer_nightly.sh` (Phase 0-4 + 1.5 + 2.5) |
| PRs in feature | 5 (PR-0 through PR-4) | FEATURE_PLAN.md |
| Governance rules | 8 (G-L1 through G-L8) | FEATURE_PLAN.md |
| Architecture rules | 10 (A-1 through A-10) | FEATURE_PLAN.md |
| Total tests (project) | 425 | 38 test files |
| Intelligence tests | 134 | 7 test files |
| Recommendation types | 3 | `claude_md_patch`, `prevention_rule`, `routing_hint` |
| Prevention rule types | 4 | `critical-prevention`, `validation-check`, `performance-optimization`, `memory-management` |

---

## Nightly Pipeline Phases — `conversation_analyzer_nightly.sh`

| Phase | Line | Script | Purpose |
|-------|------|--------|---------|
| 0 | 71 | `quality_db_init.py` | DB schema migrations |
| 1 | 97 | `conversation_analyzer.py` | Session parsing + heuristic + deep analysis |
| 1.5 | 107 | `link_sessions_dispatches.py` | Cross-reference sessions ↔ dispatches ↔ receipts |
| 2 | 115 | `generate_t0_session_brief.py` | Model performance summary |
| 2.5 | 123 | `governance_aggregator.py` | Governance metrics + SPC alerts |
| 3 | 131 | `generate_suggested_edits.py` | `pending_edits.json` (human-in-the-loop) |
| 4 | 139 | `send_digest_email.py` | Email digest (requires `VNX_DIGEST_EMAIL`) |

Each phase is non-fatal — failure in one phase does not block subsequent phases.
Singleton enforcement at line 51-60 prevents parallel runs.

---

## Database Tables — `quality_intelligence.db`

| Table | Used By | Purpose |
|-------|---------|---------|
| `pattern_usage` | `learning_loop.py`, `gather_intelligence.py` | Track offer/adoption counts, confidence scores |
| `session_analytics` | `conversation_analyzer.py`, `generate_t0_session_brief.py` | Session metrics, model performance |
| `prevention_rules` | `tag_intelligence.py` | Tag-based prevention rules with confidence |
| `tag_combinations` | `tag_intelligence.py` | Tag subset tracking with occurrence counts |
| `code_snippets` | `intelligence_daemon.py` | Extracted code patterns with quality scores |
| `vnx_code_quality` | `build_t0_quality_digest.py` | Code quality metrics for hotspot detection |

## State Files — `.vnx-data/state/`

| File | Writer | Reader | Format |
|------|--------|--------|--------|
| `intelligence_usage.ndjson` | `gather_intelligence.py`, `learning_loop.py`, worker inject | Audit trail | NDJSON (append) |
| `quality_digest.ndjson` | `build_t0_quality_digest.py` | Trend analysis | NDJSON (append) |
| `t0_quality_digest.json` | `build_t0_quality_digest.py` | T0 injection | JSON (latest) |
| `t0_tags_digest.json` | `tag_intelligence.py` | T0 injection | JSON (latest) |
| `t0_recommendations.json` | `RecommendationManager` | T0 injection | JSON (latest) |
| `pending_rules.json` | `learning_loop.py` | Operator review | JSON (queue) |
| `pending_archival.json` | `learning_loop.py` | Operator review | JSON (queue) |
| `pending_edits.json` | `generate_suggested_edits.py` | Operator review | JSON (queue) |
| `t0_session_brief.json` | `generate_t0_session_brief.py` | T0 dispatch | JSON (latest) |
| `intelligence_health.json` | `intelligence_daemon.py` | Dashboard, health check | JSON (latest) |
| `t0_receipts.ndjson` | `receipt_processor_v4.sh` | Multiple consumers | NDJSON (append) |
