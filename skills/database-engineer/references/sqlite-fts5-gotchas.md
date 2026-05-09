# SQLite + FTS5 Gotchas

Quick-access reference for the SQLite quirks that bit P4 and similar SQLite-backed systems.

## FTS5 virtual table mechanics

FTS5 vtabs are stored as a set of physical tables: `<name>_data`, `<name>_idx`, `<name>_content`, `<name>_docsize`, `<name>_config`. The vtab is a thin SQL facade. Implications:

- **No `ALTER TABLE … ADD COLUMN`** for FTS5. Adding a column requires DROP + CREATE + repopulate from a temp staging table.
- **Tokenize directives matter**: `tokenize = 'porter unicode61'` is the VNX default for `code_snippets`. Changing tokenizer = full reindex.
- **rowid is significant**: FTS5 rows have `rowid` (auto-INTEGER PRIMARY KEY). Linkage to other tables via rowid (e.g. `snippet_metadata.snippet_rowid → code_snippets.rowid`) requires preserving rowid through rebuilds.

## The FTS5 rebuild pattern

Canonical rebuild (used in P4 migration 0016):

```sql
-- Step 1: stage the existing data
CREATE TABLE IF NOT EXISTS <name>_rebuild_tmp AS
SELECT rowid, col1, col2, ... FROM <name>;

-- Step 2: drop the vtab
DROP TABLE IF EXISTS <name>;

-- Step 3: recreate with new schema (e.g. additional column)
CREATE VIRTUAL TABLE IF NOT EXISTS <name> USING fts5(
  col1, col2, ..., new_col,
  tokenize = 'porter unicode61'
);

-- Step 4: repopulate with rowid preserved
INSERT INTO <name> (rowid, col1, col2, ..., new_col)
SELECT rowid, col1, col2, ..., <derive new_col>
FROM <name>_rebuild_tmp;

-- Step 5: cleanup
DROP TABLE IF EXISTS <name>_rebuild_tmp;
```

**Wrap the whole sequence in `BEGIN; ... COMMIT;`** so a failure between DROP and CREATE rolls back cleanly. Don't use `executescript()` for this — it auto-commits between statements which breaks the atomicity guarantee.

## The R4 perf trap

If the Step 4 SELECT does a correlated subquery against another table to derive `new_col`, that subquery runs ONCE PER ROW. Without an index on the join column, this is N×M. P4 hit this:

```sql
INSERT INTO code_snippets (..., project_id)
SELECT ..., COALESCE(
  (SELECT project_id FROM snippet_metadata m
   WHERE m.snippet_rowid = code_snippets_rebuild_tmp.rowid),  -- ← per-row subquery
  'vnx-dev'
)
FROM code_snippets_rebuild_tmp;  -- ← 727k rows
```

Without an index on `snippet_metadata.snippet_rowid`, this scans 119k metadata rows × 727k snippets = ~87 billion row scans. Empirically observed: 50min in, journal still growing, ETA estimate 3-5 hours.

**Fix:** add the index *before* the rebuild, in the same migration file:

```sql
CREATE INDEX IF NOT EXISTS idx_snippet_metadata_rowid
ON snippet_metadata(snippet_rowid);
```

Now the subquery is O(log M) per row → 727k × log(119k) ≈ 17M ops. Completes in ~30 sec.

**Alternative fix** (cleaner SQL, equivalent perf with index): rewrite as JOIN:

```sql
INSERT INTO code_snippets (..., project_id)
SELECT t.rowid, t.col1, ..., COALESCE(m.project_id, 'vnx-dev')
FROM code_snippets_rebuild_tmp t
LEFT JOIN snippet_metadata m ON m.snippet_rowid = t.rowid;
```

Still needs the index, but the JOIN intent is more visible to readers and to `EXPLAIN QUERY PLAN`.

## Journal modes & file copies

| Mode | Sidecar files | Safe `cp` | Safe `Connection.backup()` |
|---|---|---|---|
| `DELETE` (default) | `<db>-journal` (only during transaction) | Yes if no active writer | Yes |
| `WAL` | `<db>-wal`, `<db>-shm` | NO — may miss committed pages in -wal | Yes |

Detect mode: `sqlite3 <db> "PRAGMA journal_mode;"`.

For WAL DBs, two safe copy patterns:

1. **Online backup** (preferred):
   ```python
   src = sqlite3.connect(src_path)
   dst = sqlite3.connect(dst_path)
   src.backup(dst)
   ```
   Handles in-flight transactions, locks briefly. This is what P4 R2 switched to after the `shutil.copy2` bug.

2. **Checkpoint-then-copy**:
   ```python
   con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
   shutil.copy2(src_path, dst_path)
   ```
   `TRUNCATE` checkpoint mode flushes the WAL and zeroes it. After this, the base file is consistent and safe to copy. Faster than backup() for cold DBs but requires a writer connection.

## Hot journal recovery

When a writer process dies mid-transaction, the journal/WAL persists. The next opener replays/rolls back automatically — but you have to actually OPEN the DB to trigger this:

```bash
# After SIGTERM/SIGKILL of a migrator:
sqlite3 ~/.vnx-data/state/quality_intelligence.db "SELECT 1;"
# Now check the file size and contents — accurate post-recovery
```

If you query immediately without opening, you may see file sizes that include uncommitted pages. This is benign but confusing.

## ATTACH DATABASE for cross-DB work

```python
con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
```

Critical: `?mode=ro`. Without it, sqlite opens the source for write, which:
- Modifies the source's `-shm` file (breaks read-only contract for backup verification)
- Acquires an exclusive lock briefly (can break a live writer in the source)
- Updates the source's atime

After ATTACH, the source tables are referenced as `src.<table_name>`. Always disambiguate with the alias to avoid name collisions with central tables.

## EXPLAIN QUERY PLAN cheat-sheet

```sql
EXPLAIN QUERY PLAN
INSERT INTO target (...) SELECT ... FROM source ...;
```

Reading the output:
- `SCAN TABLE x` → full table scan. OK at the outer-most level for tables <10k. **Bad** if it appears inside a loop (look up the row above it for context).
- `SEARCH TABLE x USING INDEX y` → index lookup. Good.
- `SEARCH TABLE x USING COVERING INDEX y` → index lookup that doesn't need the row data. Best.
- `USE TEMP B-TREE FOR ORDER BY` → sort step. Acceptable but consider an index that matches the ORDER BY.
- `CORRELATED SCALAR SUBQUERY` → check the subquery's plan. If it has `SCAN TABLE`, you have an N×M situation.

## Composite UNIQUE & sqlite oddities

- `UNIQUE(col_a, col_b)` is NOT the same as `PRIMARY KEY (col_a, col_b)`. The latter implies NOT NULL on both columns; the former allows NULL+NULL multiple times (per SQL spec, but inconsistent with most DBs).
- For multi-tenant tables in SQLite, prefer `PRIMARY KEY (project_id, source_rowid)` over `UNIQUE`. It enforces NOT NULL and creates a covering index automatically.
- Adding a UNIQUE constraint to an existing table requires creating an index, not altering the table:
  ```sql
  CREATE UNIQUE INDEX idx_t_pid_srid ON t(project_id, source_rowid);
  ```
  This is the only way; `ALTER TABLE t ADD UNIQUE(...)` doesn't exist in SQLite.

## INSERT … ON CONFLICT (UPSERT) syntax

SQLite 3.24+ supports the postgres-style UPSERT:

```sql
INSERT INTO terminal_leases (project_id, terminal_id, state, ...)
VALUES (?, ?, ?, ...)
ON CONFLICT(project_id, terminal_id) DO UPDATE SET
  state = excluded.state,
  last_heartbeat_at = excluded.last_heartbeat_at;
```

Notes:
- `excluded.<col>` refers to the row that *would have* been inserted
- The DO UPDATE clause can be empty (`DO NOTHING`) — equivalent to `INSERT OR IGNORE` but more explicit
- The conflict target (`(project_id, terminal_id)`) must match an actual UNIQUE or PK constraint
- You can specify `WHERE` conditions on the DO UPDATE: `DO UPDATE SET ... WHERE excluded.last_heartbeat_at > terminal_leases.last_heartbeat_at`

For migrations, the safest pattern is **per-column explicit UPDATE** rather than `DO UPDATE SET *`. Re-stamping `project_id` on conflict is the P4 round-5 pattern; don't blindly overwrite all columns.
