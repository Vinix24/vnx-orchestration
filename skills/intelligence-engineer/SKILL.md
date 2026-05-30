---
name: intelligence-engineer
description: VNX-specific domain expertise for the intelligence system, central state databases, and dispatch lifecycle. Use when working on quality_intelligence.db / runtime_coordination.db / dispatch_tracker.db, code_snippets FTS5, snippet_metadata linkage, success_patterns / antipatterns, T0 state projection (t0_state.json), intelligence_injections lifecycle, dispatch → receipt → events → archive flow, central state architecture, project_id propagation across VNX, or any code that reads/writes VNX governance state. Triggers include: editing intelligence injection code, querying central DB for cross-project insights, debugging "intelligence not appearing in T0 state", modifying schema in `schemas/quality_intelligence.sql` or `schemas/runtime_coordination_v*.sql`, working with `scripts/quality_db_init.py` or `scripts/lib/coordination_db.py`.
allowed-tools: [Read, Write, Edit, MultiEdit, Bash, Grep, Glob]
---

# Intelligence Engineer

VNX-specific domain expert for the intelligence system, central state, and dispatch lifecycle.

## Core Mission

VNX has a sprawling but specific data model: dispatches flow through a runtime coordination DB, generate receipts and events, get archived, and feed back into a quality intelligence DB that informs future dispatch decisions. Most "intelligence not working" or "central state weird" bugs are NOT generic database bugs — they're domain bugs in VNX-specific code paths.

This skill exists alongside `database-engineer` (which covers SQLite mechanics generically). Use `database-engineer` for migration/schema/UPSERT work; use this skill when the question requires knowing **which VNX table holds what, and why**.

## When to Use

- Editing or designing schemas in `schemas/quality_intelligence.sql`, `schemas/runtime_coordination_v*.sql`, or any `0010_…` / `0015_…` migration
- Touching `scripts/quality_db_init.py`, `scripts/lib/coordination_db.py`, or any module that initializes / queries central state
- Working with intelligence injection: `scripts/lib/intelligence_*`, `scripts/build_t0_state.py`, `scripts/lib/intelligence_injection.py`
- Debugging "code_snippets show wrong project", "dispatches missing from T0 state", "success_patterns not retrieved", "central DB query returns nothing"
- Querying central DB for cross-project insights (federation, aggregation, analytics)
- Designing a new intelligence-feeding mechanism (e.g. new event type → injection rule)
- Modifying any code that touches the project_id flow (registry → import → query → injection)

## STEP 0 — Foundational Check (Mandatory)

BEFORE proposing any design, fix, or implementation:

1. **Consult relevant ADRs** in `docs/governance/decisions/`. Special attention to:
   - ADR-005 (NDJSON audit ledger as primary observability)
   - ADR-007 (multi-tenant project_id stamping; composite keys for central state DBs)
   - ADR-010 (subprocess adapter as canonical Claude routing)
   List any ADR that applies to the task and how it constrains your solution.

2. **Consult relevant memory** in `~/.claude/projects/<your-project>/memory/MEMORY.md` — particularly entries about past architectural incidents.

3. **Check P4-style incident docs** in `claudedocs/` for analogous failures (e.g., `2026-05-09-p4-migration-architecture-lessons.md` for multi-tenant migration patterns).

4. **State your foundational read aloud** at the start of your response. Example: "ADR-007 applies: new tabel X needs composite PK over project_id. Per P4 §4.2, single-column UNIQUE is a smell. Memory [[adr-007-multitenant-composite-keys]] confirms."

Skipping STEP 0 is a process violation, not a shortcut. The FUT-1 chain (2026-05-28) burned 6 codex rounds because ADR-007 was not consulted at design time.

## Decision Protocol

When asked a "where does X live in VNX?" question, FIRST consult `references/quality-intelligence-schema.md` and `references/runtime-coordination-schema.md` for table-by-table content. Don't guess; the schemas have evolved and the canonical answer is in the schema files + their accompanying `quality_db_init.py` imperative additions.

When asked to write code that produces or consumes intelligence, walk through `references/dispatch-lifecycle.md` first to understand which lifecycle stage produces the data you need.

## 1. The Two Central DBs

VNX has two physical SQLite databases that together hold the central state:

### `~/.vnx-data/state/quality_intelligence.db` (QI)

Holds learned patterns, code snippets, and quality signals. Read primarily by intelligence injection at SessionStart and by analytics queries.

Key tables (full reference: `references/quality-intelligence-schema.md`):
- **`code_snippets`** (FTS5 vtab) — extracted code patterns with `project_id` column. Indexed for full-text search.
- **`snippet_metadata`** — companion table linking `snippet_rowid → code_snippets.rowid`. Holds `pattern_hash`, `quality_score`, `source_commit_hash`, `usage_count`.
- **`success_patterns`**, **`antipatterns`**, **`prevention_rules`** — learned governance rules with provenance.
- **`pattern_usage`** — cross-references of which dispatch used which pattern.
- **`tag_combinations`** — pre-computed tag tuples for fast filtering.
- **`vnx_code_quality`** — quality scoring per code area.

Schema source files: `schemas/quality_intelligence.sql` (base) + imperative additions in `scripts/quality_db_init.py:84-..`.

### `~/.vnx-data/state/runtime_coordination.db` (RC)

Holds dispatch lifecycle state. Read by dispatcher, T0 status, terminal coordination.

Key tables (full reference: `references/runtime-coordination-schema.md`):
- **`dispatches`** — every dispatch ever created. `dispatch_id` is the primary identifier across VNX.
- **`dispatch_attempts`** — retry attempts per dispatch.
- **`terminal_leases`** — which terminal owns which dispatch right now.
- **`coordination_events`** — append-only event log: state transitions, receipts, escalations.
- **`incident_log`** — flagged incidents (cooldowns, errors).
- **`intelligence_injections`** — record of which intelligence was injected where.
- **`retry_budgets`**, **`retry_state`**, **`escalation_log`** — retry/escalation bookkeeping.

Schema source: `schemas/runtime_coordination.sql` (v1 base) + delta migrations `runtime_coordination_v2.sql` through `runtime_coordination_v9.sql`. Apply order matters.

### Why two DBs?

QI is mostly read (intelligence injection); RC is mostly written (dispatch flow). Separating them:
- Reduces lock contention (RC has lots of small writes, QI mostly batch reads)
- Isolates retention (QI snippets last forever, RC events archive after N days)
- Allows separate backup cadence

When you query "the central state", you usually need to ATTACH both:
```python
con = sqlite3.connect(qi_path)
con.execute("ATTACH DATABASE ? AS rc", (f"file:{rc_path}?mode=ro",))
# Now can JOIN dispatches (rc.dispatches) with intelligence_injections
```

## 2. The Dispatch Lifecycle

The canonical flow that VNX governance is built on:

```
1. CREATE   T0 writes a dispatch file → .vnx-data/dispatches/pending/<id>/dispatch.json
2. PROMOTE  operator approval → moves to active/  → row in rc.dispatches (state='queued')
3. DELIVER  dispatcher delivers to terminal pane → state='dispatched', terminal_leases row inserted
4. EXECUTE  worker runs; events streamed to .vnx-data/events/T<n>.ndjson
5. RECEIPT  worker writes receipt → t0_receipts.ndjson (with append_receipt() helper)
6. ARCHIVE  T0 reviews receipt → moves dispatch to archive/, terminal_leases released
7. INJECT   subsequent SessionStart pulls intelligence from rc.dispatches + qi.success_patterns into t0_state.json
```

Each step has an associated table or file. See `references/dispatch-lifecycle.md` for the full diagram + which step writes which row + retention rules.

**Critical invariants:**
- Every persistent record has `project_id` stamped at write time
- `dispatch_id` is unique within a project, NOT globally — use prefix-rewrite (`<project_id>:<dispatch_id>`) for cross-project federation
- Receipts are NDJSON (one row per receipt), atomically appended via `fcntl.flock`
- Events files are ring-buffered: `T<n>.ndjson` is the live file (truncated post-dispatch), durable record in `events/archive/<terminal>/<dispatch_id>.ndjson`

## 3. Intelligence Injection Flow

Intelligence injection is how VNX "learns": insights from past dispatches get pulled into the current T0 session. Without this, T0 starts every conversation cold.

Flow:
```
SessionStart hook → build_t0_state.py
  ├─ Reads central QI (success_patterns, antipatterns) for this project_id
  ├─ Reads central RC (recent dispatches, open items, escalations)
  ├─ Filters by scope match (project_id, recency, relevance)
  └─ Writes t0_state.json with `strategic_state` + `intelligence` blocks
T0 reads t0_state.json on every SessionStart automatically
```

Code paths:
- `scripts/build_t0_state.py` — main projection driver
- `scripts/lib/intelligence_injection.py` — scope match + filter logic
- `scripts/aggregator/build_central_view.py` — cross-project queries (federation aggregator, P1 work)

Key concept: **scope match**. An injected pattern must be relevant to the current dispatch. Scope keys include: `project_id`, `track`, `tags`, `risk_class`. The match logic re-evaluates after canonical project_id remap (per `feedback_dispatcher_role_alias` memory).

Common bug: intelligence injection writes to `runtime_coordination.intelligence_injections` table but the scope match was wrong → injected pattern was irrelevant → operator had to manually filter. Fix: tighten scope match logic, add tests for edge cases.

## 4. Schema Evolution

VNX schemas have evolved through 9+ versions for RC and 16+ migrations for QI. Key checkpoints:

- **`schemas/runtime_coordination.sql`** is v1. Each `runtime_coordination_v<N>.sql` is a DELTA, not a full schema. Apply v1 → v2 → … → v9 in order.
- **`schemas/quality_intelligence.sql`** is the base. `scripts/quality_db_init.py:84-` adds tables imperatively (e.g. `confidence_events`, `dispatch_pattern_offered`). Don't bypass `bootstrap_qi_db()` — it knows the full set.
- **`schemas/migrations/0010_add_project_id.sql`** — adds project_id to "hot" RC tables (dispatches, dispatch_attempts, terminal_leases, etc).
- **`schemas/migrations/0015_complete_project_id.sql`** — extends to remaining tables (vnx_code_quality, snippet_metadata, etc).
- **`schemas/migrations/0016_rebuild_fts5.sql`** — rebuilds `code_snippets` FTS5 vtab to include `project_id` column.

For new migrations: number sequentially, document in the file header what it does and why, include `CREATE INDEX` for any JOIN columns. See `database-engineer/SKILL.md` for general migration patterns.

## 5. The project_id Flow

Where `project_id` comes from and how it propagates:

1. **Registry**: `~/.vnx/projects.json` — operator-curated list of projects. Each entry has `name` and `path`.
2. **Synthesis**: `scripts/aggregator/build_central_view.py:synthesize_project_id(name)` slugifies the name into a `project_id` token (lowercase, alphanumeric+dash, max 32 chars).
3. **Per-project state**: each project's `.vnx-data/state/{quality_intelligence,runtime_coordination}.db` — rows MAY have `project_id` column (legacy `vnx-dev` default for autopilot's pre-existing rows; absent for newer source DBs).
4. **Migration import**: P4 migration script reads source rows, OVERRIDES any source `project_id` value with the project's actual id from registry. Stamping happens in `_import_table` line ~1010.
5. **Central DB**: every row has `project_id` set to the registry-derived value. Cross-project queries always filter or aggregate by this column.
6. **Intelligence injection**: scope match uses project_id. Cross-project federation (P1) uses project_id as discriminator.

Pitfall: if `~/.vnx/projects.json` has a project under name `vnx-roadmap-autopilot` and migration imports it, the central rows are stamped `vnx-roadmap-autopilot`. Renaming the entry to `vnx-orchestration` later doesn't update existing central rows — they keep the old slug. P4 round-5 caught a flavor of this. Test rename scenarios explicitly.

## 6. VNX Intelligence Cookbook

Common queries you'll need:

### List all dispatches per project (last 7 days)
```sql
ATTACH DATABASE ? AS rc;
SELECT project_id, COUNT(*) AS dispatches, MAX(created_at) AS most_recent
FROM rc.dispatches
WHERE created_at > datetime('now', '-7 days')
GROUP BY project_id
ORDER BY dispatches DESC;
```

### Find code_snippets matching a pattern, scoped to project
```sql
SELECT title, file_path, line_range
FROM code_snippets
WHERE project_id = ?
  AND code_snippets MATCH ?  -- FTS5 query
ORDER BY rank
LIMIT 20;
```
Note: FTS5 MATCH is project-naive — must explicitly filter `project_id = ?` or you'll get cross-project results.

### Intelligence injection scope match
```sql
SELECT sp.pattern_id, sp.title, sp.body, sp.confidence
FROM success_patterns sp
WHERE sp.project_id = ?
  AND sp.tags GLOB '*' || ? || '*'  -- substring tag match
  AND sp.valid_until IS NULL
  AND sp.confidence >= 0.7
ORDER BY sp.confidence DESC
LIMIT 5;
```

### Cross-project federation (read-only, P1 work)
```sql
ATTACH DATABASE ? AS qi_central;
SELECT project_id, COUNT(*) AS snippet_count
FROM qi_central.code_snippets
GROUP BY project_id
ORDER BY snippet_count DESC;
```

### Dispatch chain reconstruction (parent → child)
```sql
WITH RECURSIVE chain AS (
  SELECT dispatch_id, parent_dispatch, 0 AS depth
  FROM dispatches WHERE dispatch_id = ?
  UNION ALL
  SELECT d.dispatch_id, d.parent_dispatch, c.depth + 1
  FROM dispatches d JOIN chain c ON d.parent_dispatch = c.dispatch_id
)
SELECT * FROM chain ORDER BY depth;
```

More queries in `references/intelligence-injection.md`.

## Reference Files

- `references/quality-intelligence-schema.md` — table-by-table reference for QI DB
- `references/runtime-coordination-schema.md` — table-by-table reference for RC DB (v1 base + v2-v9 deltas)
- `references/dispatch-lifecycle.md` — lifecycle diagram + which step writes which row
- `references/intelligence-injection.md` — scope match logic, federation queries, cookbook

## Companion Skills

- For SQLite mechanics, multi-tenant patterns, migration safety: `database-engineer` covers the generic side. This skill is the VNX-specific layer on top.
- For dispatcher / receipt processor / smart-tap operational work: `vnx-manager` is the right specialist.
- For T0 orchestration and dispatch routing decisions: `t0-orchestrator` (this skill is consulted by T0 when the work is intelligence/state DB heavy).

## Inheritance from backend-developer

All `backend-developer` patterns apply: atomic writes, fcntl locking on shared NDJSON, null guards, schema version checks. Treat that as baseline. This skill adds VNX-domain knowledge on top.
