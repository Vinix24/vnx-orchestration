# P4 Postmortem — Database-Engineer's Summary

## TL;DR

Five fix-forward rounds, 14+ bugs, ~16h of operator time. Bug taxonomy in `bug-taxonomy.md`. Full lessons document at `claudedocs/2026-05-09-p4-migration-architecture-lessons.md` (gitignored, internal only).

## Round-by-round

| Round | Trigger | New bugs found | Where they came from |
|---|---|---|---|
| 1 | First codex review | 4 | Patch-level: rollback safety, FTS metadata, structured findings |
| 2 | Codex round-2 review | 4 | Surfaced in changed code: WAL snapshot, collision rewriting, run_id scoping, read-failure handling |
| 3 | Codex round-3 review | 4 | Outside the diff: bootstrap missing, 0010 not applied, verifier silent skip, no pre-import assert |
| 4 | --apply v1 perf observation | 1 | Real-data scale only: O(N×M) FTS rebuild |
| 5 | --apply v3 verify failure | 3 | Real-data integration only: INSERT OR IGNORE skip, tag_combinations dedup, verifier aggregate |

**Pattern:** rounds 1-3 caught patch-scope bugs. Rounds 4-5 caught integration bugs that no amount of patch review would have surfaced — they required real-data or full-flow execution.

## Top architectural recommendations from the postmortem

(Pulled from lessons doc Section 4. Read full doc for context.)

1. **Schema-first migrations beat imperative-first** for multi-tenant. See `multi-tenant-patterns.md` Pattern 5.
2. **Composite uniqueness as default** for any table that will hold multi-tenant rows. See `multi-tenant-patterns.md` Pattern 1-2.
3. **UPSERT is the default**, not `INSERT OR IGNORE`. Use IGNORE only with composite UNIQUE that includes tenant scope.
4. **Bootstrap separation** — make `init_central` a separate gated command, not implicit "is this greenfield?" detection.
5. **Verifier independence** — different connection, different code path, ideally different process.

## Key code:line citations

For when you need to reference the actual buggy patterns:

- `migrate_to_central_vnx.py:1052` — `INSERT OR IGNORE` skip pattern (T2)
- `migrate_to_central_vnx.py:1008-1009` — re-stamp that gets lost on conflict
- `0015_complete_project_id.sql:27-61` — DEFAULT 'vnx-dev' sentinel pattern (T1)
- `0016_rebuild_fts5.sql:60-65` — correlated subquery without index (T7), fixed in round-4
- `_compare_counts` (around line 1336-1342) — verifier per-project COUNT bug (T4), fixed in round-5

## Fix-forward chain head

Branch: `feat/phase06-p4-data-migration-DRAFT` at `80a19e6` (round-5 head).

PR: #432.

39 tests passing post-round-5. Migration runs in <30 sec for FTS rebuild on 855k snippets (down from 3-5h).

## Action items still open

From the lessons doc Section 7:

- **P0**: Real-data CI integration test against fixtures sized within 1 OOM of production data. ~2h. Highest leverage.
- **P0**: UPSERT-by-default migration pattern documented and enforced via lint. ~1h.
- **P1**: Verifier independence audit — ensure no shared helpers with importer. ~2h.
- **P1**: Schema-first migration template for next migrator (P5+). ~3h.
- ... (full list in lessons doc)

## How this skill helps prevent recurrence

The Migration Defense Checklist in `SKILL.md` Section 6 is keyed to the bug taxonomy. Following it catches each known pattern at PR time. The checklist is mandatory pre-merge for any code under `schemas/migrations/` or any code that calls `INSERT OR IGNORE` / `INSERT OR REPLACE`.
