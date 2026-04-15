# F46–F50 Feature Plan — Intelligence, Autonomous Loop & Dashboard Completion

**Created**: 2026-04-13
**Status**: Planned
**Goal**: Fix the three dead pipelines (intelligence extraction, T0 state loop, dashboard visibility) and deliver a fully autonomous, self-learning headless orchestration system

---

## Feature Overview

| Feature | Goal | Depends On |
|---------|------|------------|
| F46 | Fix intelligence extraction — learning loop writes to DB, selector injects real data | — |
| F47 | Build T0 state feedback loop — receipt watcher, feature state machine, state refresh | — |
| F48 | Wire headless dispatch routing — close the autonomous loop | F47-PR1 |
| F49 | Dashboard intelligence & session visibility | F46-PR1 |
| F50 | Autonomous self-improvement loop with dashboard UI | F46, F49-PR1 |

---

## F46: Intelligence Extraction Pipeline Fix

**Problem**: `learning_loop.py` writes to `pending_rules.json` (line 387-434) but never INSERTs into DB tables that `intelligence_selector.py` reads. All dispatches have empty `intelligence_payload`.

### F46-PR1: Learning Loop DB Bridge
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~200
**Status**: Planned
**Dependencies**: []

Modify `scripts/learning_loop.py`:
- Add `persist_to_intelligence_db()`: patterns with `used_count > 0` and `confidence > 0.6` → upsert into `success_patterns`; failure patterns with `occurrence >= 2` → upsert into `antipatterns`
- Add `ingest_approved_rules()`: read `pending_rules.json` entries with `status == "approved"` → INSERT into `prevention_rules` (respects G-L1 governance)
- Wire both into `daily_learning_cycle()` as step 5.5
- Reuse existing `intelligence_persist.py::_upsert_success_pattern()` and `_upsert_antipattern()` functions

**Success criteria**:
- [ ] `SELECT COUNT(*) FROM success_patterns WHERE category='learning_loop'` > 0 after run
- [ ] `SELECT COUNT(*) FROM antipatterns WHERE category='learning_loop'` > 0 after run
- [ ] Approved rules in `pending_rules.json` appear in `prevention_rules` table
- [ ] `intelligence_selector.select()` returns non-empty items for matching dispatches

### F46-PR2: Conversation Analyzer DB Bridge
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~180
**Status**: Planned
**Dependencies**: [F46-PR1]

Modify `scripts/conversation_analyzer.py`:
- Add `bridge_session_to_intelligence()`: extract patterns from session heuristics (test-driven workflow → success_pattern, extended debugging → antipattern, error recovery → antipattern)
- Bridge high-priority `improvement_suggestions` rows into `antipatterns`
- Call at end of Phase 2 (after heuristic detection, before Phase 3)

**Success criteria**:
- [ ] `success_patterns` and `antipatterns` contain entries with `category='session_analysis'`
- [ ] `intelligence_selector` returns session-derived items for matching scope tags
- [ ] No regression in existing session_analytics writes

### F46-PR3: Intelligence Pipeline E2E Tests
**Track**: B (T2 test-engineer)
**Estimated LOC**: ~150
**Status**: Planned
**Dependencies**: [F46-PR1, F46-PR2]

New `tests/test_intelligence_pipeline_e2e.py`:
- `test_learning_loop_populates_db()` — seed pattern_usage, run persist, verify DB
- `test_selector_reads_learning_loop_patterns()` — seed via persist, call selector, assert items non-empty
- `test_approved_rules_ingest()` — pending_rules.json → prevention_rules table
- `test_conversation_bridge()` — session metrics → antipatterns/success_patterns

Extend `scripts/check_intelligence_health.py` with `intelligence_pipeline_connected` boolean.

**Success criteria**:
- [ ] All 4 tests pass
- [ ] `check_intelligence_health.py` reports `intelligence_pipeline_connected: true`

---

## F47: T0 State Feedback Loop

**Problem**: `context_assembler.py` expects `t0_state.json` but it's not refreshed after receipts. The autonomous loop never closes.

### F47-PR1: Receipt Watcher + State Refresh
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~250
**Status**: Planned
**Dependencies**: []

Modify `scripts/headless_trigger.py`:
- Add Layer 0: `ReceiptWatcher` class — watches `t0_receipts.ndjson` for new lines (tail-read)
- On new receipt: call `_refresh_t0_state()` → import and run `build_t0_state` → write atomic `t0_state.json`
- Trigger `trigger_headless_t0(reason="receipt", context=receipt)` with debounce (30s)
- Wire into `main()` alongside existing Layer 1/2/3 watchers

**Success criteria**:
- [ ] New receipt appended to `t0_receipts.ndjson` triggers T0 within 30s
- [ ] `t0_state.json` is refreshed before each T0 invocation
- [ ] Existing Layer 1 (report watcher) and Layer 2 (silence watchdog) unchanged

### F47-PR2: Feature State Machine
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~200
**Status**: Planned
**Dependencies**: [F47-PR1]

New `scripts/lib/feature_state_machine.py`:
- `parse_feature_plan(path) → FeatureState` — parse FEATURE_PLAN.md structure, extract PR sections, `[x]` vs `[ ]` status, track assignments
- `get_next_dispatchable(state_dir) → Optional[dict]` — combine feature state + terminal availability → `{terminal, track, task_description, pr_id, role}`
- Integrate into `scripts/build_t0_state.py`: add `feature_state` section
- Enhance `scripts/f39/context_assembler.py` Section 4: structured feature state

**Success criteria**:
- [ ] `parse_feature_plan()` correctly identifies completed vs pending PRs
- [ ] `t0_state.json` contains `feature_state.next_task` field
- [ ] T0 context includes structured feature state, not just raw markdown

### F47-PR3: State Loop Integration Tests
**Track**: B (T2 test-engineer)
**Estimated LOC**: ~150
**Status**: Planned
**Dependencies**: [F47-PR1, F47-PR2]

New `tests/test_state_feedback_loop.py`:
- `test_receipt_triggers_state_refresh()` — write receipt, verify t0_state.json updated
- `test_feature_state_machine_parsing()` — sample FEATURE_PLAN.md → correct next task
- `test_receipt_watcher_debounce()` — rapid receipts → single refresh
- `test_context_assembler_with_feature_state()` — assembled context includes structured state
- `test_full_loop_dry_run()` — receipt → trigger → decision in dry-run

**Success criteria**:
- [ ] All 5 tests pass
- [ ] Dry-run loop completes without errors

---

## F48: Headless Dispatch Routing (Autonomous Loop Closure)

**Problem**: `subprocess_adapter.py` works but requires manual invocation. No daemon watches `dispatches/pending/` for auto-delivery.

### F48-PR1: Headless Dispatch Daemon
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~250
**Status**: Planned
**Dependencies**: [F47-PR1]

New `scripts/lib/headless_dispatch_daemon.py`:
- `DispatchDaemon` — watchdog on `dispatches/pending/`, picks up new dispatch files
- Parse dispatch metadata (target terminal, track, role, instruction)
- Check terminal availability via `t0_state.json`
- Route via `subprocess_dispatch.py` for subprocess targets
- Move dispatch through `pending/` → `active/` → `completed/`
- Acquire/release leases via `LeaseManager`
- Log to `dispatch_audit.jsonl`

**Success criteria**:
- [ ] Dispatch in `pending/` auto-delivered within 10s
- [ ] Dispatch moves through full lifecycle
- [ ] Receipt written to `t0_receipts.ndjson`
- [ ] Terminal lease acquired/released correctly

### F48-PR2: Autonomous Loop Orchestrator
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~200
**Status**: Planned
**Dependencies**: [F48-PR1]

New `scripts/headless_orchestrator.py` — single entry point:
- Start 3 threads: ReceiptWatcher (F47), DispatchDaemon (F48-PR1), Silence Watchdog (existing)
- Startup validation: t0_state.json, quality_intelligence.db, dispatches/pending/, claude CLI
- Write `headless_health.json` with daemon states
- SIGTERM graceful shutdown
- Cycle logging to `events/autonomous_loop.ndjson`
- CLI: `python3 scripts/headless_orchestrator.py [--dry-run] [--log-level DEBUG]`

**Success criteria**:
- [ ] All daemons start without error
- [ ] Health file shows all daemons running
- [ ] SIGTERM cleanly shuts down all daemons
- [ ] Cycle events logged to `autonomous_loop.ndjson`

### F48-PR3: Autonomous Loop Integration Tests
**Track**: B (T2 test-engineer)
**Estimated LOC**: ~180
**Status**: Planned
**Dependencies**: [F48-PR1, F48-PR2]

New `tests/test_autonomous_loop.py`:
- `test_dispatch_daemon_picks_up_pending()` — dispatch auto-moved to active/
- `test_dispatch_daemon_lease_conflict()` — active lease → daemon skips
- `test_full_autonomous_cycle_dry_run()` — pending → delivered → receipt → state → T0 → new dispatch
- `test_headless_orchestrator_startup()` — start, verify health, stop gracefully

**Success criteria**:
- [ ] All 4 tests pass
- [ ] Dry-run cycle completes the full loop

---

## F49: Dashboard Intelligence & Session Visibility

**Problem**: 12 dashboard pages but no intelligence analytics, no session transcripts, no haiku classification visibility.

### F49-PR1: Intelligence & Classification API Endpoints
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~220
**Status**: Planned
**Dependencies**: [F46-PR1]

New `dashboard/api_intelligence.py`:
- `GET /api/intelligence/patterns` — query success_patterns + antipatterns from quality_intelligence.db
- `GET /api/intelligence/injections` — query coordination_events for injection history
- `GET /api/intelligence/classifications` — parse unified reports for haiku fields (quality_score, content_type, complexity, summary)
- `GET /api/intelligence/dispatch-outcomes` — dispatch_metadata outcomes by track/time
- `GET /api/conversations/{session_id}/transcript` — messages from conversation-index.db

Wire routes in `dashboard/serve_dashboard.py`.

**Success criteria**:
- [ ] All 5 endpoints return valid JSON
- [ ] Classifications parsed from real auto-generated reports
- [ ] Transcript endpoint returns message list

### F49-PR2: Intelligence Dashboard Page
**Track**: C (T3 frontend-developer)
**Estimated LOC**: ~270
**Status**: Planned
**Dependencies**: [F49-PR1]

New `dashboard/token-dashboard/app/operator/intelligence/page.tsx`:
- Pattern Overview: success patterns (green) + antipatterns (red) with confidence bars
- Classification Analytics: quality distribution bar chart, complexity per track, content type pie (Recharts)
- Injection History: timeline of recent injections (items vs suppressed)
- Dispatch Outcomes: success rate gauge per track, failure trend line

Add sidebar nav (Brain icon), types in `lib/types.ts`, SWR hooks in `lib/hooks.ts`.

**Success criteria**:
- [ ] Page renders with all 4 sections from real data
- [ ] Charts display correctly (or graceful empty state)
- [ ] Navigation works from sidebar

### F49-PR3: Session Transcript Viewer & Breadcrumbs
**Track**: C (T3 frontend-developer)
**Estimated LOC**: ~230
**Status**: Planned
**Dependencies**: [F49-PR1]

- New `components/transcript-viewer.tsx`: chat-style message display, lazy-loaded, paginated
- Integrate in `app/conversations/page.tsx`: selected session → transcript panel
- New `components/operator/breadcrumb-nav.tsx`: Session → Dispatch → Report navigation
- Enhance `app/operator/reports/page.tsx`: breadcrumb + "View Transcript" link

**Success criteria**:
- [ ] Click session → see transcript
- [ ] Report detail shows breadcrumb trail
- [ ] Navigation between session/dispatch/report works

---

## F50: Autonomous Self-Improvement Loop

**Problem**: `generate_suggested_edits.py` produces 0 suggestions. No dashboard UI for accept/reject. No weekly digest. No feedback from outcomes → confidence.

### F50-PR1: Improvement Proposals API + Weekly Digest
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~230
**Status**: Planned
**Dependencies**: [F46, F49-PR1]

Add to `dashboard/api_intelligence.py`:
- `GET /api/intelligence/proposals` — read pending_edits.json
- `POST /api/intelligence/proposals/{id}/accept` — mark accepted
- `POST /api/intelligence/proposals/{id}/reject` — mark rejected with reason
- `POST /api/intelligence/proposals/apply` — trigger apply_suggested_edits.py
- `GET /api/intelligence/confidence-trends` — time series from pattern confidence
- `GET /api/intelligence/weekly-digest` — latest weekly_digest.json

New `scripts/weekly_digest.py`:
- Aggregate 7 days: patterns learned, confidence changes, outcomes, top suggestions
- Haiku narrative summary (≤500 chars) via CLI subprocess
- Write `state/weekly_digest.json`

**Success criteria**:
- [ ] Proposals endpoint returns real data
- [ ] Accept/reject persists to file
- [ ] Weekly digest generates narrative summary

### F50-PR2: Self-Improvement Dashboard UI
**Track**: C (T3 frontend-developer)
**Estimated LOC**: ~270
**Status**: Planned
**Dependencies**: [F50-PR1]

New `dashboard/token-dashboard/app/operator/improvements/page.tsx`:
- Pending Proposals: card list with category badge, proposed change, evidence, accept/reject buttons
- Confidence Trends: Recharts line chart (30 days, success vs antipattern confidence)
- Weekly Digest: rendered narrative card with "Generate Now" button

Add sidebar nav (Lightbulb icon), types, hooks, API functions.

**Success criteria**:
- [ ] Proposals render with accept/reject buttons
- [ ] Confidence chart renders with data
- [ ] Weekly digest displays narrative

### F50-PR3: Feedback Loop Wiring
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~170
**Status**: Planned
**Dependencies**: [F50-PR1]

- Enhance `scripts/lib/intelligence_persist.py`: dispatch success → boost patterns in `pattern_usage`; failure → decay
- Enhance `scripts/generate_suggested_edits.py`: new "prevention_rules" category from antipatterns with `occurrence_count >= 3`
- New `GET /api/intelligence/learning-summary`: boost/decay counts, net confidence drift
- Wire `weekly_digest.py` + `generate_suggested_edits.py` into nightly pipeline

**Success criteria**:
- [ ] Dispatch outcome visibly changes pattern confidence within one learning cycle
- [ ] Prevention rule suggestions appear after 3+ antipattern occurrences
- [ ] Learning summary endpoint returns meaningful metrics

---

## Execution Waves

### Wave 1: F46-PR1 → F47-PR1 (T1 sequential, no deps)
### Wave 2: F46-PR2 + F47-PR2 (T1) | F46-PR3 + F47-PR3 (T2 parallel)
### Wave 3: F48-PR1 → F49-PR1 (T1 sequential, after F47-PR1)
### Wave 4: F48-PR2 (T1) | F49-PR2 → F49-PR3 (T3) | F48-PR3 (T2)
### Wave 5: F50-PR1 → F50-PR3 (T1) | F50-PR2 (T3)

**Total: 15 PRs, ~3,220 lines**
