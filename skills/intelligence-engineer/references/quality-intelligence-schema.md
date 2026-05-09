# quality_intelligence.db Schema Reference

Authoritative source: `schemas/quality_intelligence.sql` (base) + imperative additions in `scripts/quality_db_init.py:84-`. Use `bootstrap_qi_db()` to initialize; do not handcraft.

## Core tables

### code_snippets (FTS5 virtual table)

Extracted code patterns indexed for full-text search.

```sql
CREATE VIRTUAL TABLE code_snippets USING fts5(
    title,              -- function/class/pattern name
    description,        -- one-line description
    code,              -- actual snippet text
    file_path,         -- source file location
    line_range,        -- "start-end" line numbers
    tags,              -- comma-separated category tags
    language,          -- python, bash, sql, typescript, ...
    framework,         -- crawl4ai, supabase, fastapi, ...
    dependencies,      -- required imports (one per line)
    quality_score,     -- 0-100 numeric stored as text
    usage_count,       -- references count, also text
    last_updated,      -- ISO timestamp
    project_id,        -- ADDED in migration 0016 rebuild; rows pre-rebuild lack this
    tokenize = 'porter unicode61'
);
```

`rowid` is the primary identifier. Linkage to `snippet_metadata` is by rowid.

**Query patterns:**
- Always include `WHERE project_id = ?` to prevent cross-project leakage
- Use `MATCH` for FTS queries: `code_snippets MATCH 'fastapi auth NEAR/3 jwt'`
- Combine: `WHERE project_id = ? AND code_snippets MATCH ?`

### snippet_metadata

Companion to code_snippets. Holds metadata that doesn't fit in FTS columns.

```sql
CREATE TABLE snippet_metadata (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snippet_rowid      INTEGER NOT NULL,    -- → code_snippets.rowid
    file_path          TEXT NOT NULL,
    line_start         INTEGER,
    line_end           INTEGER,
    quality_score      REAL DEFAULT 0.0,
    usage_count        INTEGER DEFAULT 0,
    source_commit_hash TEXT,
    pattern_hash       TEXT,                -- SHA1(title|file_path|line_range)
    project_id         TEXT,                -- ADDED migration 0015
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    last_updated       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_snippet_metadata_rowid ON snippet_metadata(snippet_rowid);
CREATE INDEX idx_snippet_pattern_hash ON snippet_metadata(pattern_hash);
CREATE INDEX idx_snippet_quality ON snippet_metadata(quality_score);
```

`idx_snippet_metadata_rowid` was ADDED in migration 0016 (round-4 of P4) to fix the O(N×M) FTS rebuild perf bug. Don't drop this index.

### success_patterns

Learned patterns that worked. Used for intelligence injection.

```sql
CREATE TABLE success_patterns (
    pattern_id         TEXT PRIMARY KEY,    -- slug-style id
    title              TEXT NOT NULL,
    body               TEXT NOT NULL,       -- markdown body
    tags               TEXT,                -- comma-separated
    confidence         REAL DEFAULT 0.5,    -- 0-1, learned over time
    source_dispatch_id TEXT,                -- which dispatch produced this
    valid_from         TEXT,                -- ISO timestamp
    valid_until        TEXT,                -- NULL = still valid
    project_id         TEXT NOT NULL DEFAULT 'vnx-dev',
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);
```

For injection: `SELECT … WHERE project_id = ? AND valid_until IS NULL AND confidence >= 0.7`.

### antipatterns

Inverse of success_patterns. Things that didn't work / shouldn't be repeated.

Same shape as success_patterns. Treated symmetrically by intelligence injection.

### prevention_rules

Hard rules derived from antipatterns. Higher confidence threshold.

```sql
CREATE TABLE prevention_rules (
    rule_id            TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    body               TEXT NOT NULL,
    severity           TEXT,                -- 'blocker', 'warn', 'info'
    source             TEXT,
    source_dispatch_id TEXT,
    valid_from         TEXT,
    valid_until        TEXT,
    project_id         TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

### pattern_usage

Cross-references: which dispatch used which pattern.

```sql
CREATE TABLE pattern_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id  TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    used_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
);
```

Used for: confidence updates (a pattern that's used 100x with success → confidence boost), retention prioritization (frequently used patterns survive cleanup).

### tag_combinations

Pre-computed tuples of tags that co-occur. Used for fast filtering.

```sql
CREATE TABLE tag_combinations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_tuple   TEXT NOT NULL,    -- canonical sorted comma-list
    use_count   INTEGER DEFAULT 1,
    last_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
);
CREATE UNIQUE INDEX idx_tag_combinations_tuple ON tag_combinations(project_id, tag_tuple);
```

The composite UNIQUE was added in P4 round-5 to prevent cross-project deduplication (T6 in bug taxonomy).

### vnx_code_quality

Quality scoring per code area / file.

```sql
CREATE TABLE vnx_code_quality (
    file_path        TEXT NOT NULL,
    project_id       TEXT NOT NULL DEFAULT 'vnx-dev',
    quality_score    REAL,
    last_assessed    TEXT,
    issues_count     INTEGER,
    -- ... more columns ...
    PRIMARY KEY (project_id, file_path)
);
```

## Imperative additions (not in base SQL)

`scripts/quality_db_init.py` adds these in `bootstrap_qi_db()`:

- **`confidence_events`** (line ~321) — append-only log of confidence changes. Tracks why a pattern's confidence went up/down.
- **`dispatch_pattern_offered`** (line ~408) — record of which patterns were offered to which dispatch (for impression tracking, A/B analysis).
- **`session_analytics`** — per-T0-session stats (dispatch count, success rate, etc).
- **`quality_trends`** — time-series of project quality over time.
- **`improvement_suggestions`** — operator-facing recommendations.

These are NOT in `schemas/quality_intelligence.sql` directly because they evolved post-base-schema and were added as imperative migrations. Always use `bootstrap_qi_db()` for fresh init; never run only the base SQL.

## Retention

By design, QI is mostly write-once-read-many. Retention rules:

- `code_snippets` and `snippet_metadata`: forever (operator may prune via VACUUM or explicit DELETE)
- `success_patterns`, `antipatterns`, `prevention_rules`: until `valid_until` is set (never auto-expired)
- `pattern_usage`: 90 days, then archive
- `tag_combinations`: forever (cheap to keep)

For multi-project central, consider per-project retention if disk pressure becomes an issue (P4 lessons doc Section 7 has an action item for this).

## Common gotchas

- **Don't query `code_snippets` without `WHERE project_id = ?`** in a multi-tenant central. FTS5 MATCH is project-naive.
- **Don't trust `DEFAULT 'vnx-dev'` semantics** — that default lets unmigrated rows look legitimate. See bug taxonomy T1.
- **Always use `bootstrap_qi_db()` for init**, not raw SQL. Imperative additions matter.
- **FTS5 rebuilds need an index on subquery columns** — see `database-engineer/references/sqlite-fts5-gotchas.md` for the rebuild pattern.
