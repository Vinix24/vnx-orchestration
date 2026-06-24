# VNX Intelligence System - Technical Reference
**Last Updated**: 2026-06-23
**Owner**: T-MANAGER
**Purpose**: Deep technical reference for the VNX Intelligence System — the read path (per-dispatch injection) and the write path (daemon + learning loop).

**Version**: 6.0.0
**Date**: 2026-06-23
**Status**: Active — deep technical reference
**Maintainer**: T-MANAGER

> **See first:** `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` §7 for the overview. That doc is the entry point; this one is the engine detail it links to.

## Table of Contents
1. [Overview](#overview) — read/write split
2. [System Architecture](#system-architecture) — the door → assembly → selector
3. [Selection Engine (Pattern Matching)](#selection-engine-pattern-matching) — class-based bounded selection
4. [Prevention Rules](#prevention-rules) — antipattern evidence + operator-gated rules
5. [Usage Signal Pipeline](#usage-signal-pipeline) — confidence write-back + reconcile
6. [Intelligence Injection](#intelligence-injection) — in-assembly path, rendered format, audit, tenant isolation
7. [Governance Measurement](#governance-measurement) — CQS / SPC
8. [Integration](#integration) — where injection plugs in, provider parity, daemon
9. [Operations](#operations) / [Testing](#testing)
10. [Appendix](#appendix) — files reference + superseded/legacy

> Superseded sections (retained as pointers only): [Agent Validation](#agent-validation-superseded), [Documentation Ingestion](#documentation-ingestion) (daemon-side/legacy), [Tag Intelligence](#tag-intelligence-superseded), [Performance & Caching](#performance--caching-superseded).

---

## Overview

The VNX Intelligence System enriches every dispatch with task-relevant, evidence-backed intelligence and learns from the outcomes. It is two separate flows around one per-project store (`quality_intelligence.db`):

### Read path — injection (per dispatch, in-assembly)
Injection happens **in Python, in-assembly** — there is no shell `UserPromptSubmit` hook. When a dispatch body is composed (`dispatch_prepare.prepare` → `_inject_skill_context` → `_build_intelligence_section`), it calls `intelligence_injection.fetch_intelligence_section`, which runs `IntelligenceSelector.select()`. The selector queries three bounded item classes from `quality_intelligence.db`, applies confidence/evidence gates, diversity, recency suppression, and a payload cap, then renders a markdown section that is spliced into the worker's instruction. Same path for all provider lanes (`claude` via tmux/subprocess, and codex/gemini/litellm/kimi via `provider_dispatch.py`).

### Write path — learning (daemon, gated)
The `intelligence_daemon.py` runs `learning_loop.py` on a daily cycle (18:00). Tier-1 auto-tunes pattern *confidence* from receipt outcomes (boost 1.10 / decay 0.95). Tier-2 *proposes* new prevention rules into an operator-gated `pending_rules.json` — those are never auto-activated; only operator-approved rules are ingested into the live `prevention_rules` table. The system proposes, the human approves.

### Intelligence store
- **Location**: per-project central store `~/.vnx-data/<project_id>/state/quality_intelligence.db` (resolved via `vnx_paths.resolve_central_data_dir`); embedded installs use `$VNX_STATE_DIR/quality_intelligence.db`.
- **Engine**: SQLite (FTS5 still used by the ADR table `adrs_fts` and legacy `code_snippets`).
- **Read tables**: `success_patterns` (proven_pattern), `antipatterns` + `prevention_rules` (failure_prevention), `dispatch_metadata` (recent_comparable), `adrs` (ADR context).
- **Audit / feedback tables**: `intelligence_injections` (coord DB), `pattern_usage`, `dispatch_pattern_offered`, `confidence_events`.
- **Tenant isolation (ADR-007)**: every table is keyed by `project_id`; reads are project-scoped by default (`VNX_PROJECT_FILTER` ON).

> **Superseded.** The previous engine (`gather_intelligence.py`, agent-name validation, FTS5 relevance scoring, `cached_intelligence.py`, the `userpromptsubmit_*_intelligence_inject.sh` hooks) is **not** the injection path. Where those daemon-side helpers still exist they are documented in the *Superseded / legacy (daemon-side)* appendix.

---

## System Architecture

Injection is in-assembly. The dispatch passes through THE DOOR (`vnx dispatch`), which picks a lane; both Claude lanes and the provider lanes assemble the body through `dispatch_prepare`, which reaches the same selector seam.

```
┌─────────────────────────────────────────────────────────┐
│                   THE DOOR  (vnx dispatch)               │
│   decides the lane (tmux / subprocess / provider)        │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│   ASSEMBLY — dispatch_prepare.prepare()                  │
│   scripts/lib/dispatch_prepare.py:51-117                 │
│     1. repo-map → raw instruction                        │
│     2. _inject_skill_context()  ← gathers intelligence   │
│     3. _inject_permission_profile()                      │
│     4. scope guard / worker-rules / report-contract      │
└────────────────────────┬────────────────────────────────┘
                         │  (best-effort; never blocks dispatch)
                         ▼
┌─────────────────────────────────────────────────────────┐
│   _build_intelligence_section()                          │
│   subprocess_dispatch_internals/skill_injection.py:73-111│
│     → intelligence_injection.fetch_intelligence_section()│
│        • ADR context block (INT-2, role/path triggered)  │
│        • IntelligenceSelector.select(...)                │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│   IntelligenceSelector.select()                          │
│   scripts/lib/intelligence_selector.py:143-181           │
│                                                          │
│   standard classes (gated, diversity, recency, cap):     │
│   ┌──────────────┐ ┌─────────────────┐ ┌──────────────┐ │
│   │proven_pattern│ │failure_prevention│ │recent_       │ │
│   │success_      │ │antipatterns +    │ │comparable    │ │
│   │patterns      │ │prevention_rules  │ │dispatch_meta │ │
│   └──────────────┘ └─────────────────┘ └──────────────┘ │
│                                                          │
│   W5 direct sources (no confidence gate):                │
│   prior_round_finding · adr_relevant · code_anchor ·     │
│   operator_memory · schema_section                       │
└────────────────────────┬────────────────────────────────┘
                         │  reads          ▲  records audit
                         ▼                 │
┌─────────────────────────────────────────────────────────┐
│   per-project quality_intelligence.db (ADR-007 scoped)   │
│   ~/.vnx-data/<project_id>/state/                        │
│   reads:  success_patterns · antipatterns ·              │
│           prevention_rules · dispatch_metadata · adrs    │
│   writes: pattern_usage · dispatch_pattern_offered ·     │
│           confidence_events                              │
│   coord DB: intelligence_injections (injection audit)    │
└────────────────────────┬────────────────────────────────┘
                         │ rendered markdown section
                         ▼
┌─────────────────────────────────────────────────────────┐
│   Enriched instruction body (worker prompt)              │
│   ## ADR Context (auto-injected per Wave-5)              │
│   ## Relevant Intelligence (from past dispatches)        │
│     ### Antipatterns to avoid                            │
│     ### Proven success patterns                          │
│     ### Tag warnings                                     │
│   ## PRIOR ROUND REVIEW FINDINGS  (when pr_id maps)      │
└─────────────────────────────────────────────────────────┘

         ── WRITE PATH (separate, daily) ──
┌─────────────────────────────────────────────────────────┐
│   intelligence_daemon.py  (18:00 daily cycle)            │
│     → learning_loop.daily_learning_cycle()               │
│        Tier-1: confidence boost 1.10 / decay 0.95        │
│        Tier-2: propose rules → pending_rules.json (gated) │
│        reconcile → success_patterns.confidence_score     │
└─────────────────────────────────────────────────────────┘
```

---

## Agent Validation (superseded)

Agent-name validation was a `gather_intelligence.py` feature on the old read path. It is **not** part of the current injection path — role resolution now lives in `worker_permissions` / `terminal_assignments` (`skill_injection._resolve_effective_role`), and lane/provider routing is enforced by the dispatch door. See the *Superseded / legacy (daemon-side)* appendix; this section is retained only as a pointer.

---

## Selection Engine (Pattern Matching)

### Purpose
Choose a bounded, evidence-backed set of intelligence items for the dispatch — at most 3 standard items plus the direct W5 sources — under hard confidence, diversity, recency, and payload limits. This replaces the old FTS5 relevance-scoring engine; selection is now class-based, not a single weighted score.

### Implementation

**File**: `scripts/lib/intelligence_selector.py` — `IntelligenceSelector.select()` (lines 143-181)
**Per-source queries**: `scripts/lib/intelligence_sources/` (`proven_pattern.py`, `failure_prevention.py`, `recent_comparable.py`, `adr_relevant.py`, `code_anchor.py`)
**Contract**: FP-C Intelligence Contract — `docs/core/31_FPC_INTELLIGENCE_CONTRACT.md`. Governance gates: G-R5 (max 3 items), G-R6 (confidence + evidence + scope), G-R7 (advisory-only).

### The three standard item classes

Each is queried from a distinct table and ranked, then gated independently (`intelligence_selector.py:131-141`, source modules in `intelligence_sources/`):

| Class | Source table | What it carries | Rendered under |
|-------|--------------|-----------------|----------------|
| `proven_pattern` | `success_patterns` | high-confidence success patterns (ordered by `confidence_score`) | `### Proven success patterns` |
| `failure_prevention` | `antipatterns` + `prevention_rules` | antipattern evidence + operator-approved rules | `### Antipatterns to avoid` |
| `recent_comparable` | `dispatch_metadata` | similar recent dispatches (last 14 days) | `### Tag warnings` |

Selection priority is fixed: `ITEM_CLASS_PRIORITY = ["proven_pattern", "failure_prevention", "recent_comparable"]` (`intelligence_sources/_common.py:57`).

### Confidence + evidence gates (G-R6)

Each class has its own minimum confidence and minimum evidence-count threshold (`intelligence_sources/_common.py:45-55`). A candidate must clear **both** to be eligible:

| Class | Min confidence | Min evidence |
|-------|----------------|--------------|
| `proven_pattern` | 0.6 | 2 |
| `failure_prevention` | 0.5 | 1 |
| `recent_comparable` | 0.4 | 1 |

If no candidate in a class clears the gate, a `SuppressionRecord` is written with the reason (`confidence X below threshold Y`) and that class contributes nothing. Antipattern confidence is derived from severity (critical 0.9 / high 0.75 / medium 0.6 / low 0.5); prevention-rule confidence comes from the row's `confidence` column (`failure_prevention.py:49,151,218`).

### Diversity, governance cap, and the "best per class" rule

Within an eligible class (`_select_standard_classes`, `intelligence_selector.py:240-308`):
- Duplicate items (same `content_hash`) are dropped — `seen_hashes` carries across classes.
- At most **one** governance-category item per batch (`MAX_GOVERNANCE_PER_BATCH = 1`); extra governance items are dropped.
- The single highest-confidence remaining candidate is selected per class (`best = max(diverse, key=confidence)`). So the standard ceiling is one item per class → 3 total (`MAX_ITEMS_PER_INJECTION = 3`).

### Recency suppression + vangnet

Items injected in the last *N* dispatches for the same `task_class` are suppressed so workers do not see the same pattern every round (`_query_recent_injected_ids` + the recency block in `_select_standard_classes`, `intelligence_selector.py:183-308`). *N* is `VNX_INTEL_SUPPRESS_WINDOW` (default **10**, lines 183/196); set to `0` to disable.

**Vangnet (safety net):** if suppression would empty a class entirely (every diverse candidate was recently injected), the best candidate is let through anyway to prevent an empty injection (`intelligence_selector.py:281-290`).

### Payload cap

After selection, `_enforce_payload_limit` (`intelligence_selector.py:311-329`) serializes the result and, while it exceeds `MAX_PAYLOAD_CHARS = 2000`, drops whole classes in reverse-priority order (recent_comparable first), recording a suppression reason each time. Per-item content is also capped at `MAX_CONTENT_CHARS_PER_ITEM = 500` (`_common.py:31-33`).

### W5 direct sources (no confidence gate)

After the standard classes, `select()` appends the Wave-5 direct item classes when their preconditions are met (`intelligence_selector.py:166-179`, `_DIRECT_SOURCES` 65-72). These are precise, source-backed, and bypass the confidence/evidence gates (they carry their own evidence):

- `prior_round_finding` — built from prior review-gate results when `pr_id` is set (see *Prior-Round Findings* in the Injection section).
- `adr_relevant` / `schema_section` — ADR + schema context from the `adrs` table (role/path triggered).
- `code_anchor` — file:line anchors extracted from `dispatch_paths` + instruction.
- `operator_memory` — operator-curated notes for the skill/paths.

Each appended item is re-checked against the payload cap with a cumulative drop order, so a large W5 item can evict lower-priority standard classes rather than blow the budget.

### A/B control arm

`_ab_arm()` (`intelligence_selector.py:40-50`) returns `control` for ~10% of dispatches **only when** `VNX_INTEL_AB_TEST=1`; otherwise always `treatment` (no-op). On the control arm, `select_intelligence` / `build_intelligence_context` skip injection entirely but still write the `intelligence_injections` audit row (with `ab_arm="control"` and zero items), so the effect of injection can be measured against a clean baseline.

### CLI / inspection

The selector has no standalone CLI; it runs in-assembly. To inspect what was selected for a dispatch, read the audit row:

```bash
sqlite3 ~/.vnx-data/$VNX_PROJECT_ID/state/runtime_coordination.db \
  "SELECT dispatch_id, task_class, items_injected, items_suppressed, ab_arm
   FROM intelligence_injections ORDER BY injected_at DESC LIMIT 5"
```

---

## Documentation Ingestion

> **Daemon-side / legacy.** This populates the legacy `code_snippets` FTS5 table, which the **current injection path does not read** (the selector reads `success_patterns` / `antipatterns` / `prevention_rules` / `dispatch_metadata` / `adrs`, not `code_snippets`). ADR context is now served from the structured `adrs` table, not from ingested markdown. This section is retained for the daemon-side extractor only; it does not affect what a worker prompt receives.

### Purpose
Indexes project markdown documentation into the legacy FTS5 `code_snippets` table, making architectural decisions, API specs, deployment procedures, and business logic searchable alongside code patterns. Configured via `VNX_DOCS_DIRS` environment variable — feature is inactive when not set.

### Implementation

**Version**: 1.0.0 (2026-03-02)
**File**: `scripts/doc_section_extractor.py`
**Configuration**: `VNX_DOCS_DIRS` env var (comma-separated paths, relative or absolute)

### How It Works

1. **Directory Resolution**: Reads `VNX_DOCS_DIRS` env var, resolves relative paths against `PROJECT_ROOT`
2. **File Discovery**: Globs `*.md` recursively, skips `archive/` directories
3. **Frontmatter Parsing**: Extracts YAML frontmatter (title, status, summary, owner) via `yaml.safe_load()`
4. **Section Splitting**: Splits on `## ` headings — each heading becomes a separate FTS5 record
5. **Quality Scoring**: Scores 0-100 based on code blocks, tables, word count, cross-references, status
6. **Category Detection**: Derives from filename prefix number (e.g., `10_` → architecture, `20_` → api)
7. **FTS5 Storage**: Inserts into `code_snippets` with `language="markdown"`, `framework=<category>`
8. **Idempotency**: Skips unchanged files (git commit hash check), clears stale sections before re-extraction

### Configuration

```bash
# Enable doc ingestion (relative to PROJECT_ROOT):
export VNX_DOCS_DIRS=SEOCRAWLER_DOCS

# Multiple directories:
export VNX_DOCS_DIRS=SEOCRAWLER_DOCS,docs/extra

# Absolute path:
export VNX_DOCS_DIRS=/path/to/docs

# Feature disabled when empty/unset (default)
```

### Document Categories

Derived from filename number prefix:

| Range | Category |
|-------|----------|
| 0-9 | governance |
| 10-19 | architecture |
| 20-29 | api |
| 30-49 | implementation |
| 50-59 | configuration |
| 60-69 | operations |
| 70-79 | business |
| 80-99 | deployment |

Fallback: subdirectory name (`production/` → operations) or frontmatter owner field.

### Quality Scoring (0-100)

| Factor | Score |
|--------|-------|
| Base | 50 |
| Has code blocks | +10 |
| Multiple code blocks (>=2) | +5 |
| Has tables | +8 |
| Good body length (50-500 words) | +10 |
| Long body (>500 words) | +5 |
| Too short (<20 words) | -15 |
| Cross-references (>=1) | +5 |
| Cross-references (>=3) | +5 |
| Frontmatter with summary | +5 |
| Status archived/deprecated | x0.5 |
| Status draft | x0.75 |

Minimum score to store: 40 (lower than code's 60, docs are inherently useful).

### FTS5 Column Mapping

| `code_snippets` column | Value for doc section |
|-------------------------|----------------------|
| `title` | `##` heading text |
| `description` | Frontmatter summary + first sentence |
| `code` | Full section body (searchable via FTS5) |
| `file_path` | Path to markdown file |
| `line_range` | `"15-45"` |
| `tags` | `"documentation, architecture, api"` |
| `language` | `"markdown"` |
| `framework` | Category (architecture, api, operations, etc.) |
| `dependencies` | Cross-referenced doc filenames |
| `quality_score` | Doc quality score (0-100) |

### Standalone Usage

```bash
# Run extraction:
VNX_DOCS_DIRS=SEOCRAWLER_DOCS python3 scripts/doc_section_extractor.py

# Verify FTS5 entries:
sqlite3 "$VNX_STATE_DIR/quality_intelligence.db" \
  "SELECT COUNT(*) FROM code_snippets WHERE language='markdown'"
```

### Integration with Intelligence Daemon

The daemon calls `doc_section_extractor.py` after `code_snippet_extractor.py` during daily hygiene refresh (`_refresh_quality_intelligence()`). No manual scheduling needed.

### Testing

```bash
python3 -m pytest tests/test_doc_section_extractor.py -v
# 13 tests: frontmatter, splitting, scoring, categorization, tags, env config, E2E pipeline
```

---

## Prevention Rules

### Purpose
Warn workers about known failure modes for their task. There is no longer a keyword-to-hardcoded-rule generator. Prevention content comes from two real, governed sources that both surface through the `failure_prevention` item class at injection time.

### Source 1 — antipattern evidence (live, read at injection)

Antipatterns accumulate in the `antipatterns` table from the receipt/learning pipeline. The selector reads them in `query_failure_prevention` → `_query_antipatterns` (`scripts/lib/intelligence_sources/failure_prevention.py:52-167`):

- Filter: `occurrence_count >= 1` and not expired (`valid_until`), scope-matched to the dispatch tags.
- Ordering: by severity (critical → low) then `occurrence_count`.
- Confidence by severity: critical 0.9 / high 0.75 / medium 0.6 / low 0.5.
- Rendered as `- **<title>**: <why_problematic> Instead: <better_alternative>` under `### Antipatterns to avoid`.

### Source 2 — operator-approved prevention rules (gated write → read at injection)

New rules are **proposed** by the learning loop and only become live after an operator approves them. Two stages:

**Propose (Tier-2, write path).** `learning_loop.generate_prevention_rules` (`scripts/learning_loop.py:355-379`) groups repeated failures (`>= 2` occurrences of the same error+terminal+agent) and `update_terminal_constraints` (lines 405-452) writes them to `pending_rules.json` with `"status": "pending"`. Per **G-L1 they are never auto-activated** — nothing is inserted into the live `prevention_rules` table at this stage. Confidence is `min(occurrences * 0.2, 0.9)`.

**Approve + ingest.** An operator edits `pending_rules.json` and sets a rule's `status` to `approved`. On the next cycle `ingest_approved_rules` (`scripts/learning_loop.py:556-...`) inserts **only** `status == "approved"` rows into the `prevention_rules` table (dedup by description + tag_combination), then marks them ingested. From then on the selector reads them via `_query_prevention_rules` (`failure_prevention.py:170-227`) and renders them under `### Antipatterns to avoid`.

```
failure receipts
  → generate_prevention_rules()           (Tier-2 proposal)
  → pending_rules.json  {status: "pending"}   ← G-L1: never auto-active
  → operator sets status: "approved"          ← HUMAN GATE
  → ingest_approved_rules()                (only approved rows)
  → prevention_rules table  (live)
  → query_failure_prevention()             (read at next injection)
```

### Pending-rule shape

```json
{
  "id": "rule-ab12cd34",
  "created_at": "2026-06-23T14:00:00",
  "source": "learning_loop",
  "rule_type": "failure_prevention",
  "pattern": "Error pattern: <first 50 chars of error>",
  "terminal_constraint": "T1",
  "prevention": "Validate agent exists ... before dispatch",
  "confidence": 0.6,
  "occurrence_count": 3,
  "status": "pending"
}
```

### Inspection

```bash
# pending vs approved counts
jq '[.pending_rules[] | .status] | group_by(.) | map({(.[0]): length}) | add' \
  ~/.vnx-data/$VNX_PROJECT_ID/state/pending_rules.json

# live rules
sqlite3 ~/.vnx-data/$VNX_PROJECT_ID/state/quality_intelligence.db \
  "SELECT description, confidence, triggered_count FROM prevention_rules
   WHERE valid_until IS NULL OR valid_until > datetime('now') ORDER BY confidence DESC LIMIT 10"
```

---

## Tag Intelligence (superseded)

The compound-tag extractor and the pairwise/triple subset generator (`tag_intelligence.py`, `RecommendationManager`) were part of the old `gather_intelligence.py` read path. They are **not** in the current injection path. Scope matching is now done by the selector's lightweight `scope_tags` mechanism: `select()` builds an effective scope from `skill_name`, `Track-<n>`, `gate`, and the resolved `task_class` (`intelligence_selector.py:155-159`), and each source filters candidates with `_scope_matches` (`intelligence_sources/_common.py`). Operator-gated recommendations now live in `pending_rules.json` (see *Prevention Rules*). Retained here only as a pointer; details are in the *Superseded / legacy (daemon-side)* appendix.

---

## Usage Signal Pipeline

### Purpose
Tracks when injected intelligence patterns are offered to terminals and whether they are adopted, closing the feedback loop so confidence scores change based on real-world outcomes.

### Implementation

The injection side and the outcome side write to different places:

- **Injection-time recording** (read path, in-assembly): `record_injection` → `record_injection_audit` writes the `intelligence_injections` audit row (coord DB), `record_pattern_usage` updates `pattern_usage` + `dispatch_pattern_offered`, and `stamp_source_dispatch_ids` records which source dispatches each injected pattern came from (`scripts/lib/intelligence_sources/_recording.py`).
- **Outcome write-back** (on receipt arrival): `intelligence_persist.update_confidence_from_outcome` (`scripts/lib/intelligence_persist.py:268-...`) matches patterns back to the dispatch via `success_patterns.source_dispatch_ids` and recomputes confidence.
- **Daily Tier-1 tuning**: `learning_loop.py` adjusts `pattern_usage.confidence` (boost 1.10 / decay 0.95), then `confidence_reconcile.reconcile_pattern_confidence` syncs those stats back into `success_patterns.confidence_score`.

### Per-dispatch write-back — Beta posterior (not fixed deltas)

`update_confidence_from_outcome(db_path, dispatch_id, terminal, status)` does **not** apply a fixed `+x%`. It increments `success_count` / `failure_count` on the matching `pattern_usage` row and writes back a `Beta(success+1, failure+1)` Laplace-smoothed posterior to `success_patterns.confidence_score`, so the score reflects total usage volume rather than a run of consecutive boosts (`intelligence_persist.py:276-285`). A `confidence_events` row is written for audit. Linkage is `success_patterns.source_dispatch_ids LIKE '%<dispatch_id>%'`, project-scoped (ADR-007). Matching `pattern_usage` rows use the stable `intel_sp_<id>` id so the per-dispatch update and the daily reconcile read/write the same row.

### Daily Tier-1 confidence tuning (learning_loop.py)

The fixed-rate boost/decay still exists, but as the **daily** Tier-1 tuning, not the per-dispatch path. From `PatternUsageMetric` (`scripts/learning_loop.py:40-53`):

```python
decay_rate = 0.95   # 5% daily decay for unused patterns
boost_rate = 1.10   # 10% boost for used patterns
```

`daily_learning_cycle` (`learning_loop.py:799-...`) runs: analyze receipts → update confidence scores → learn from failures (propose rules) → archive stale → persist to intelligence DB + `ingest_approved_rules` → **reconcile** (`confidence_reconcile.reconcile_pattern_confidence`, syncs `pattern_usage` stats into `success_patterns.confidence_score`). A 300-second TTL guard (`RECONCILE_CACHE_TTL_SECONDS`) lets the selector call `maybe_reconcile` at injection time as a safety net without re-running the full reconcile every dispatch (`confidence_reconcile.py:162-...`).

### pattern_usage schema

```sql
CREATE TABLE pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    last_offered TIMESTAMP,
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Governance Enforcement

- **G-L1**: `update_terminal_constraints()` writes to `pending_rules.json`, never directly to the live `prevention_rules` table.
- **G-L4**: stale-pattern archival queues to an operator-confirmation list, never auto-archives.

### Operations

```bash
# Manual learning cycle
python3 scripts/learning_loop.py run

# Check usage stats
sqlite3 ~/.vnx-data/$VNX_PROJECT_ID/state/quality_intelligence.db \
  "SELECT pattern_id, used_count, success_count, failure_count, confidence
   FROM pattern_usage WHERE used_count > 0 ORDER BY confidence DESC LIMIT 10"

# View pending rules queue (G-L1)
jq '.pending_rules | length' ~/.vnx-data/$VNX_PROJECT_ID/state/pending_rules.json
```

---

## Intelligence Injection

### Purpose
Splice task-relevant intelligence into the worker's instruction body. Injection is **in-assembly, in Python** — there is no `UserPromptSubmit` shell hook and no `additionalContext` JSON. The same path serves every provider lane.

### The in-assembly path (read path)

```
vnx dispatch (THE DOOR)
  → dispatch_prepare.prepare()                 scripts/lib/dispatch_prepare.py:51-117
    → _inject_skill_context()                  subprocess_dispatch_internals/skill_injection.py:114-...
      → _build_intelligence_section()          skill_injection.py:73-111
        → intelligence_injection.fetch_intelligence_section()   scripts/lib/intelligence_injection.py:292-369
          → fetch_adr_context_section()        (ADR block, INT-2)
          → IntelligenceSelector.select()      scripts/lib/intelligence_selector.py:143-181
```

`_build_intelligence_section` looks up the per-project state dir, then delegates to `fetch_intelligence_section`, which (a) builds the ADR context block when role/path triggers match, (b) runs the selector, (c) emits the coordination event, records the `intelligence_injections` audit row, and stamps `source_dispatch_ids`, then (d) returns the combined markdown. `build_intelligence_section` (the wrapper) prepends the `## Relevant Intelligence (from past dispatches)` header and splices it above the original instruction (`intelligence_injection.py:284-289`). Provider lanes (codex/gemini/litellm/kimi) reach the identical `fetch_intelligence_section` via `provider_dispatch.py`.

### Exact rendered markdown

`format_intelligence_items` groups selected items by class and renders fixed section names (`intelligence_injection.py:372-393`):

```markdown
## Relevant Intelligence (from past dispatches)

### Antipatterns to avoid
- **<title>**: <content>

### Proven success patterns
- **<title>**: <content>

### Tag warnings
- **<title>**: <content>

---

<original instruction follows>
```

The class → heading mapping is hard-coded: `failure_prevention` → "Antipatterns to avoid", `proven_pattern` → "Proven success patterns", `recent_comparable` → "Tag warnings". When the selected set has no items in a class, that subheading is omitted. When the selector returns nothing, the original instruction is returned unchanged (best-effort).

### ADR context block (INT-2, Wave-5)

When the dispatch role is in `ADR_TRIGGER_ROLES` (`database-engineer`, `architect`, `intelligence-engineer`, `security-engineer`) or `dispatch_paths` hit `schemas/…` / `coordination_db.py` / `quality_db.py`, `fetch_adr_context_section` queries the `adrs` table (status `Accepted`, project-scoped, FTS-matched on path terms, max 3) and prepends a binding block (`intelligence_injection.py:29-247`):

```markdown
## ADR Context (auto-injected per Wave-5)

The following Architectural Decision Records apply to your dispatch scope:

### ADR-007 — <title>
**Decision summary:** <≤200 chars>
**Binding rules:**
- <rule>

These are BINDING — your implementation MUST comply.
```

Per ADR-005 the ADR injection writes an NDJSON audit event **first** and re-raises `OSError` on write failure (it must not be swallowed); other exceptions degrade to no ADR block. When both ADR and intelligence sections exist, ADR comes first (most binding).

### Prior-round findings (Wave-5, highest signal)

When the dispatch carries a `pr_id` that maps to existing review-gate results, `prior_round_injector.py` injects the previous round's findings so a re-dispatch sees what the last round flagged (the fix for round-cascades like PR #432's 9-round chain). It reads `state/review_gates/results/pr-<id>-{codex_gate,gemini_review}.json`, prefers blocking over advisory and newest round first, scope-filters by overlap with `dispatch_paths`, and trims to `MAX_INJECTION_CHARS = 2000` (`prior_round_injector.py:26,89-176`). Findings carry a `contract_hash` so the re-dispatch can tell which contract version produced them. The rendered block (`format_findings_section`, lines 179-210) includes an **anti-anchoring** notice telling the worker to re-read current code before relying on the findings:

```markdown
## PRIOR ROUND REVIEW FINDINGS

> **Anti-anchoring notice:** Re-read current code at touched lines before
> relying on these findings — they may have been addressed in subsequent rounds.

### Blocking
- **[codex_gate]** <message>

### Advisory
- **[gemini_review]** <message>
```

This surfaces through the selector as the `prior_round_finding` direct item class (`build_prior_round_item`).

### Injection audit (`intelligence_injections`)

Every injection — including A/B control arms that inject nothing — writes one row to the `intelligence_injections` table in the coord DB (`intelligence_selector.py:331-365` → `_recording.record_injection_audit`):

| Column | Meaning |
|--------|---------|
| `injection_id`, `dispatch_id` | identity + attribution (dispatch_id is required; empty raises) |
| `injection_point` | `dispatch_create` or `dispatch_resume` |
| `task_class` | resolved task class used for selection + recency window |
| `items_injected`, `items_suppressed` | counts |
| `payload_chars` | serialized payload size |
| `items_json`, `suppressed_json` | full selected items + suppression reasons |
| `project_id` | ADR-007 tenant key (when the column exists) |
| `ab_arm` | `treatment` or `control` (when the column exists) |

The recency-suppression query (`_query_recent_injected_ids`) reads `items_json` from the last *N* rows of this same table for the task class, which is how an item injected last round gets suppressed this round.

### Tenant isolation (ADR-007)

Injection is per-project. The store is resolved from `current_project_id()` (`scripts/lib/project_scope.py:56-67`), which reads `VNX_PROJECT_ID` (default `vnx-dev`) and rejects ids that violate `^[a-z][a-z0-9-]{1,31}$` — bad ids fail loudly rather than bleeding cross-tenant. The central QI store path is `~/.vnx-data/<project_id>/state/quality_intelligence.db` (`intelligence_selector._get_central_qi_conn` 114-129). Reads are project-scoped by default (`VNX_PROJECT_FILTER` ON), and every source applies a `project_id = ?` clause when the table has the column. At init time, the owning `project_id` for a backfill is resolved **fail-closed** by `project_id_migration.resolve_init_project_id` (`scripts/lib/project_id_migration.py:96-151`): it derives the id from the DB path layout, the `.vnx-project-id` marker, and `VNX_PROJECT_ID`, and raises `RuntimeError` if those sources disagree — that conflict guard is what prevents a fresh non-`vnx-dev` store from silently inheriting another tenant's rows.

### Safe degradation

Missing dispatch metadata, an absent database, empty selection, or any selector exception all degrade to a no-op (original instruction returned). Injection never blocks dispatch execution. The one non-swallowed case is ADR-005 `OSError` on the ADR audit-event write, which must propagate.

---
## Governance Measurement

### Purpose

Replaces self-reported terminal status with objective, calculated quality scores. Detects anomalies using Statistical Process Control (SPC) and makes rework visible through First-Pass Yield measurement.

### Implementation

**Version**: 1.1 (2026-03-07)
**Schema**: 8.2.0-cqs-advisory-oi
**Files**: `cqs_calculator.py`, `governance_aggregator.py`, `open_items_manager.py`
**Full Reference**: `docs/intelligence/GOVERNANCE_MEASUREMENT.md`

### Why This Exists

Analysis of dispatch data revealed three fundamental measurement problems:

1. **166 unique self-reported status values** — no standardization, terminals define own success
2. **Rework invisible** — re-dispatched "successful" tasks counted as multiple successes
3. **System timeouts pollute metrics** — 30% of dispatches timeout (receipt processing issue), inflating failure rates

### Composite Quality Score (CQS)

Weighted 0-100 score computed from 7 independent signals:

```
CQS = Status(25%) + Completion(20%) + Effort(15%) + ErrorDensity(10%)
    + Rework(10%) + T0Advisory(10%) + OIDelta(10%)

Status:       166 raw values -> 5 categories (success=100, partial=60, failure=0)
              timeout/unknown -> excluded from all metrics

Completion:   Has report? + PR merged? + Gate passed? -> 0-100

Effort:       Tokens used / median for same role
              <= 0.5x = 100, 1x = 80, 2x = 20, >2x = 0

ErrorDensity: Error messages / total messages ratio
              0% = 100, <5% = 80, <15% = 50, <30% = 25, >30% = 0

Rework:       Same gate+pr_id dispatched before?
              First attempt = 100, rework = 0

T0Advisory:   quality_advisory.t0_recommendation decision + risk_score
              approve=100, followup=60, hold=0 (70/30 blend with inverse risk)

OIDelta:      Open items created vs resolved balance
              +15/resolved (cap 30), -10/created (cap 30), -20/unresolved target (cap 20)
              No OI involvement = 50 (neutral)
```

### SPC Control Charts

Statistical Process Control from manufacturing quality (Toyota/Six Sigma):

```
UCL = X-bar + 3*sigma     Upper Control Limit
CL  = X-bar               Center Line (mean)
LCL = max(0, X-bar - 3*sigma)  Lower Control Limit
```

Recomputed nightly from 30-day rolling baseline. Point beyond limits has 0.27% probability of occurring by chance.

### Western Electric Anomaly Rules

| Rule | Condition | Severity |
|------|-----------|----------|
| Out of control | Point beyond UCL/LCL | Critical |
| Trend | 7+ consecutive increasing/decreasing | Warning |
| Shift | 8+ points on same side of center | Warning |
| Run | 2 of 3 beyond 2-sigma | Info |

### Nightly Aggregation Metrics

Computed per scope (system/terminal/role/gate/model):

| Metric | Formula | Standard |
|--------|---------|----------|
| First-Pass Yield | Unique tasks succeeded first / total unique | Toyota TPS |
| Rework Rate | Total dispatches / unique gate+pr_id | Six Sigma |
| Gate Velocity | Hours from first dispatch to gate pass | DORA lead time |
| Mean CQS | Average composite quality score | Composite index |
| OI Resolution Rate | resolved / (created + resolved) | Quality debt velocity |

### Database

```sql
-- CQS columns on dispatch_metadata
cqs REAL, normalized_status TEXT, cqs_components TEXT
target_open_items TEXT,       -- JSON array ["OI-042"]
open_items_created INTEGER DEFAULT 0,
open_items_resolved INTEGER DEFAULT 0

-- Aggregated metrics
governance_metrics (period, scope, metric_name, value, sample_size)

-- Control limits
spc_control_limits (metric, scope, center_line, ucl, lcl, sigma)

-- Anomaly alerts
spc_alerts (alert_type, metric, scope, observed, limit, severity)
```

### Integration

- **Receipt-time**: CQS computed in `append_receipt.py` and `receipt_processor.sh` (C3b)
- **Nightly**: Phase 2.5 in `conversation_analyzer_nightly.sh`
- **Weekly**: governance reports generated via nightly pipeline

---

## Performance & Caching (superseded)

The `cached_intelligence.py` multi-layer cache (pattern/report/keyword/prevention caches) wrapped the old `gather_intelligence.py` engine and is **not** in the current selector path. The current engine keeps latency bounded structurally instead of with a cache: selection reads a handful of `LIMIT`-bounded queries per class, caps output at `MAX_PAYLOAD_CHARS = 2000`, and uses a 300-second TTL guard around confidence reconcile (`confidence_reconcile.maybe_reconcile`) so the selector never re-runs the full reconcile per dispatch. Prior-round findings use a 1-minute-bucketed `lru_cache` (`prior_round_injector._fetch_cached`). The cache layer is documented in the *Superseded / legacy (daemon-side)* appendix only.

---

## Integration

### Where injection plugs in (the door → assembly)

Intelligence is not called by a dispatcher script. It runs **inside assembly**, behind the single dispatch door. Both Claude lanes (tmux-spawn, subprocess) and the provider lanes assemble through `dispatch_prepare.prepare()`, which calls `_inject_skill_context` → `_build_intelligence_section` (see *Intelligence Injection*). No lane calls the selector directly; the seam is shared so every provider gets the same intelligence.

```python
# scripts/lib/dispatch_prepare.py:80-91  (assembly seam)
body = _inject_skill_context(
    terminal_id or "",
    raw,                         # instruction (+ repo-map)
    role,
    {
        "dispatch_id": dispatch_id,
        "model": model,
        "dispatch_paths": dispatch_paths or [],
        "pr_id": pr_id,
        "pr": pr_id,
    },
)
```

`dispatch_paths`, `instruction_text`, and `pr_id` are forwarded all the way to `IntelligenceSelector.select()` so the W5 direct classes (adr_relevant, code_anchor, operator_memory, schema_section, prior_round_finding) can fire on real dispatches.

### Provider-lane parity

`provider_dispatch.py` (codex/gemini/litellm/kimi) reaches the same `intelligence_injection.fetch_intelligence_section`, so a kimi or glm worker receives the identical `## Relevant Intelligence` / `## ADR Context` blocks a Claude worker would. This is deliberate: raw vs gate-routed dispatch must not produce a different audit trail.

### Intelligence daemon integration (write path)

The daemon owns the write path and the daily cycle:

```python
# scripts/intelligence_daemon.py  (IntelligenceDaemon)
self.learning_loop = LearningLoop()
self.daily_hygiene_hour = 18          # 18:00

# run loop:
if self.should_run_daily_hygiene():   # once/day at 18:00
    self.daily_hygiene()              # extractor + hygiene refresh
self.run_learning_cycle()             # learning_loop.daily_learning_cycle()
```

`run_learning_cycle` calls `learning_loop.daily_learning_cycle()` (Tier-1 confidence tuning, Tier-2 rule proposals, reconcile). The daemon does not touch the read path.

---
## Operations

All state lives in the per-project central store. `$VNX_PROJECT_ID` defaults to `vnx-dev`.

### Inspect what was injected

```bash
# Recent injection decisions (coord DB) — counts, suppression, A/B arm
sqlite3 ~/.vnx-data/$VNX_PROJECT_ID/state/runtime_coordination.db \
  "SELECT dispatch_id, task_class, items_injected, items_suppressed, ab_arm
   FROM intelligence_injections ORDER BY injected_at DESC LIMIT 10"

# Full selected items + suppression reasons for one dispatch
sqlite3 ~/.vnx-data/$VNX_PROJECT_ID/state/runtime_coordination.db \
  "SELECT items_json, suppressed_json FROM intelligence_injections
   WHERE dispatch_id = '<id>'"
```

### Inspect the read tables

```bash
QI=~/.vnx-data/$VNX_PROJECT_ID/state/quality_intelligence.db

# Proven patterns by confidence
sqlite3 "$QI" "SELECT title, confidence_score, usage_count FROM success_patterns
  ORDER BY confidence_score DESC LIMIT 10"

# Antipatterns + live prevention rules
sqlite3 "$QI" "SELECT title, severity, occurrence_count FROM antipatterns
  ORDER BY occurrence_count DESC LIMIT 10"
sqlite3 "$QI" "SELECT description, confidence, triggered_count FROM prevention_rules
  WHERE valid_until IS NULL OR valid_until > datetime('now') ORDER BY confidence DESC LIMIT 10"
```

### Write path

```bash
# Manual daily learning cycle (Tier-1 tune + Tier-2 propose + reconcile)
python3 scripts/learning_loop.py run

# Pending rule proposals awaiting operator approval (G-L1)
jq '.pending_rules | length' ~/.vnx-data/$VNX_PROJECT_ID/state/pending_rules.json
```

### Troubleshooting

| Symptom | Likely cause | Check |
|---------|--------------|-------|
| No `## Relevant Intelligence` block in worker prompt | selector returned 0 items (all below threshold), or A/B control arm | `intelligence_injections.items_injected` + `ab_arm` for the dispatch |
| Same item every round | recency suppression disabled | `echo $VNX_INTEL_SUPPRESS_WINDOW` (default 10; `0` disables) |
| Intelligence missing for a non-`vnx-dev` project | wrong project store / `VNX_PROJECT_FILTER` | confirm `VNX_PROJECT_ID`, that `~/.vnx-data/<pid>/state/quality_intelligence.db` exists |
| New rule never appears | it is still `status: pending` (G-L1 gate) | set `status: "approved"` in `pending_rules.json`, re-run learning cycle |
| Confidence stuck at initial value | reconcile not run | `python3 scripts/learning_loop.py run`, or rely on injection-time `maybe_reconcile` (300s TTL) |

---

## Testing

### Read-path tests

```bash
python3 -m pytest \
  tests/test_intelligence_selector.py \
  tests/test_intelligence_sources/ \
  tests/test_intelligence_injection.py \
  tests/test_intelligence_recency_suppression.py \
  tests/test_intelligence_per_provider.py \
  tests/test_provider_dispatch_intelligence.py \
  tests/test_subprocess_intelligence_injection.py \
  tests/test_skill_injection_wave5.py \
  tests/test_prior_round_injector.py \
  tests/test_adr_injection.py \
  tests/test_intelligence_ab_framework.py -v
```

### Write-path + tenant tests

```bash
python3 -m pytest \
  tests/test_learning_loop_certification.py \
  tests/test_confidence_reconcile.py \
  tests/test_project_scope_filesystem.py \
  tests/test_project_id_migration.py \
  tests/test_intelligence_pipeline_e2e.py -v
```

### What each suite proves

- `test_intelligence_selector.py` / `test_intelligence_sources/` — thresholds, diversity, payload cap, the three classes.
- `test_intelligence_recency_suppression.py` — suppression window + vangnet.
- `test_intelligence_per_provider.py` / `test_provider_dispatch_intelligence.py` — provider-lane parity (same section for codex/gemini/litellm/kimi).
- `test_prior_round_injector.py` — prior-round scope filter, char cap, anti-anchoring notice.
- `test_project_id_migration.py` — ADR-007 fail-closed conflict guard.
- `test_intelligence_pipeline_e2e.py` — inject → receipt → confidence write-back end to end.

---
## Appendix

### Files Reference (current engine)

**Read path (injection)**
- `scripts/lib/dispatch_prepare.py` — shared assembly seam (`prepare()`), where injection plugs in.
- `scripts/lib/subprocess_dispatch_internals/skill_injection.py` — `_inject_skill_context`, `_build_intelligence_section` (`:73-111`).
- `scripts/lib/intelligence_injection.py` — `fetch_intelligence_section`, `format_intelligence_items`, ADR context block.
- `scripts/lib/intelligence_selector.py` — `IntelligenceSelector.select()` (`:143-181`), gates, recency, payload cap, audit.
- `scripts/lib/intelligence_sources/` — per-class queries + constants: `_common.py` (thresholds/limits), `proven_pattern.py`, `failure_prevention.py`, `recent_comparable.py`, `adr_relevant.py`, `code_anchor.py`, `_recording.py`, `_models.py`.
- `scripts/lib/prior_round_injector.py` — Wave-5 prior-round findings (`MAX_INJECTION_CHARS = 2000`).
- `scripts/lib/confidence_reconcile.py` — `maybe_reconcile` / `reconcile_pattern_confidence` (300s TTL guard).
- `scripts/lib/intelligence_persist.py` — `update_confidence_from_outcome` (Beta posterior write-back).

**Write path (daemon / learning)** — note: these two live at top-level `scripts/`, not `scripts/lib/`:
- `scripts/intelligence_daemon.py` — daily cycle orchestration (18:00), runs the learning loop.
- `scripts/learning_loop.py` — Tier-1 confidence tune (boost 1.10 / decay 0.95), Tier-2 rule proposals → `pending_rules.json`, `ingest_approved_rules`, reconcile.

**Tenant isolation (ADR-007)**
- `scripts/lib/project_scope.py` — `current_project_id` (`:56-67`), `project_filter_enabled`.
- `scripts/lib/project_id_migration.py` — `resolve_init_project_id` fail-closed (`:96-151`).

**Stores (per-project, ADR-007)** — `~/.vnx-data/<project_id>/state/`:
- `quality_intelligence.db` — `success_patterns`, `antipatterns`, `prevention_rules`, `dispatch_metadata`, `adrs`/`adrs_fts`, `pattern_usage`, `dispatch_pattern_offered`, `confidence_events`.
- `runtime_coordination.db` — `intelligence_injections` (injection audit).
- `pending_rules.json` — operator-gated Tier-2 rule proposals (G-L1).

**Contract / spec**
- `docs/core/31_FPC_INTELLIGENCE_CONTRACT.md` — FP-C contract (G-R5/R6/R7, limits).
- `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md` — tenant stamping.

### Superseded / legacy (daemon-side)

The following are **not** part of the injection read path. Some files still exist for daemon-side hygiene/extraction or are fully retired; none are consulted when assembling a worker prompt:

- `gather_intelligence.py` — the old `gather/patterns/validate/list-agents` engine (agent-name validation, FTS5 relevance scoring, keyword→hardcoded prevention rules, compound-tag extraction). Superseded by `IntelligenceSelector`.
- `cached_intelligence.py` — the pattern/report/keyword/prevention cache layer that wrapped `gather_intelligence.py`. Not in the selector path.
- `tag_intelligence.py` — compound-tag extraction + pairwise/triple subsets + `RecommendationManager`. Scope matching is now `scope_tags` + `_scope_matches`.
- `code_snippet_extractor.py` / `doc_section_extractor.py` — populate the legacy `code_snippets` FTS5 table; used (if at all) by daemon hygiene, not by injection.
- `scripts/userpromptsubmit_intelligence_inject.sh` / `scripts/userpromptsubmit_worker_intelligence_inject.sh` — the old shell-hook + `additionalContext` injection model. The files still exist on disk but are **not** the injection path; current injection is in-assembly Python and does not register or call these hooks. (verify: confirm they are not wired in any active `settings.json` before relying on this.)

### Version History

- **v6.0.0** (2026-06-23) — Rebased onto the current engine: in-assembly Python injection (`dispatch_prepare` → `_build_intelligence_section` → `fetch_intelligence_section` → `IntelligenceSelector.select()`), class-based bounded selection (FP-C G-R5/R6/R7), W5 direct sources, recency suppression + vangnet, A/B control arm, ADR context, prior-round findings, Beta-posterior confidence write-back, ADR-007 tenant isolation. The `gather_intelligence.py` engine, FTS5 relevance scoring, agent validation, tag intelligence, and the UserPromptSubmit shell hooks are marked superseded.
- **v5.0.0** (2026-03-28) — Self-Learning Pipeline (last version describing the `gather_intelligence.py` engine).
- **v3.0.0** (2026-03-02) — Documentation ingestion, language-aware filtering, `VNX_DOCS_DIRS`.
- **v2.0.0** (2026-01-26) — Enhanced relevance scoring, prevention rules, tag intelligence.
- **v1.1.0** (2026-01-19) — Pattern matching engine, dispatcher integration.
- **v1.0.0** (2026-01-18) — Agent validation.

### Cross-references

- Overview / entry point: `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` §7 (this doc is the deep reference it links to).
- FP-C contract: `docs/core/31_FPC_INTELLIGENCE_CONTRACT.md`.
- Tenant isolation: `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md`.

---

**Document Version**: 6.0.0
**Last Updated**: 2026-06-23
**Maintained by**: T-MANAGER
**Status**: Active — deep technical reference
