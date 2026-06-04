# Task 04 — Visibility scorer task + migration + tests

Source-inspiratie: Mission Control PR #244 (visibility_comment_scorer). Tier: T2 medium. Deadline: 30 minutes wallclock.

## Context

The seed includes a minimal SQLite project that ingests documents and needs a scoring layer. There are 50 seed documents pre-loaded. You implement:
1. A migration that adds a `document_scores` table
2. A `scorer.py` module that computes per-document scores
3. A CLI entry point that runs the scorer over all documents
4. A test suite (15 tests) covering scoring logic + persistence + edge cases

This is a realistic multi-file Procrastinate-task-style pattern — bounded scope but spans schema, business logic, persistence, and tests.

## Scoring formula

For each document, compute `score = base * status_multiplier * age_decay` where:

- `base` = `min(word_count, 1000) / 100` (clamped 0-10 from word count)
- `status_multiplier` = `{"draft": 0.5, "published": 1.0, "archived": 0.25}[status]`
- `age_decay` = `1.0 / (1.0 + days_since_created * 0.05)`
- Final score: round to 2 decimal places
- Documents with `status` NOT in the three keys: skip + log (no score row written)

## Required deliverables

### 1. `migrations/001_add_document_scores.sql`

Idempotent migration creating `document_scores`:
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `document_id INTEGER NOT NULL`
- `project_id TEXT NOT NULL DEFAULT 'default'`  *(ADR-007)*
- `score REAL NOT NULL`
- `computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`
- UNIQUE index on `(project_id, document_id)`
- INDEX on `(computed_at)` for time-window queries

Must be re-runnable without errors.

### 2. `scorer.py`

Module with these callables:

```python
def compute_score(word_count: int, status: str, days_since_created: int) -> float | None:
    """Returns score or None if status is unrecognized."""

def score_document(conn, document_id: int, project_id: str = "default") -> float | None:
    """Reads document, computes score, UPSERTs into document_scores. Returns score."""

def score_all(conn, project_id: str = "default") -> dict:
    """Iterates all documents, calls score_document. Returns {scored: N, skipped: M}."""
```

UPSERT pattern: `INSERT ... ON CONFLICT(project_id, document_id) DO UPDATE SET score = excluded.score, computed_at = CURRENT_TIMESTAMP`.

### 3. `cli.py`

CLI entry point: `python3 cli.py --db documents.db --project-id default` — runs `score_all` and prints `{"scored": N, "skipped": M}`. Exit 0 on success, 1 if any uncaught exception.

### 4. `tests/test_scorer.py`

15 tests total:
- `test_compute_score_published_zero_days` — 100 words, published, day 0 → score 1.00
- `test_compute_score_published_word_clamp` — 5000 words, published, day 0 → score 10.00 (clamped)
- `test_compute_score_draft_multiplier` — 200 words, draft, day 0 → 1.00 (2.0 base × 0.5)
- `test_compute_score_archived_multiplier` — 200 words, archived, day 0 → 0.50
- `test_compute_score_age_decay_10_days` — 200 words, published, day 10 → ~1.33 (2.0 / 1.5)
- `test_compute_score_unknown_status_returns_none` — status="weird" → None
- `test_compute_score_empty_document` — 0 words, published, day 0 → 0.00
- `test_score_document_writes_row` — score_document() inserts into document_scores
- `test_score_document_upsert` — calling twice updates same row, not duplicate
- `test_score_document_unknown_status_skips_persist` — status="weird" writes nothing
- `test_score_all_counts_correctly` — 50 docs in seed → returns scored + skipped totals
- `test_score_all_project_isolation` — same document_id under two project_ids creates two rows
- `test_migration_idempotent` — running migrate.sql twice does not error
- `test_score_all_handles_db_lock` — concurrent score_all calls don't crash (single retry OK)
- `test_cli_exit_zero_on_success` — `python3 cli.py --db <fresh-db>` exits 0

## Files in seed

- `documents.db` — pre-loaded with 50 documents (~45 published, 3 draft, 1 archived, 1 unknown-status)
- `schema.sql` — original `documents` table definition (for reference)

## Files you may create/modify

- `migrations/001_add_document_scores.sql` (create)
- `scorer.py` (create)
- `cli.py` (create)
- `tests/test_scorer.py` (create)
- `tests/__init__.py` (create, empty)

Do NOT modify `documents.db` directly or `schema.sql`.

## Definition of done

- All 15 tests pass: `pytest tests/test_scorer.py -v`
- Migration runs cleanly (and twice idempotently)
- `python3 cli.py --db documents.db --project-id default` prints valid JSON `{"scored": 49, "skipped": 1}` and exits 0
- No TODO comments, no print-debug
