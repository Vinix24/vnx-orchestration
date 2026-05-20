# Migration Rollback Runbook

Wave 2a safety companion. Use when a schema migration must be reversed during the rc2/rc3 rollout window or after an accidental apply.

All DB restores use atomic pattern (temp file + verify + mv) to prevent corruption on interrupted restore.

---

## When to roll back

- A new migration introduced a UNIQUE or NOT NULL constraint that breaks rc2-era writes.
- A central DB migration was applied out of order (schema_version mismatch detected by `scripts/lib/schema_versioning.py`).
- Data corruption is detected after `apply_migration_0010 / 0015 / 0016` ran against a central DB.
- Operator decision: wave rollback to previous release candidate.

Do not roll back `0010_add_project_id` unless you are also rolling back all migrations that build on it (0011 through 0021 in reverse order). The rollback chain must be applied in reverse numeric order.

---

## Pre-rollback checklist

```bash
# 1. Check current schema_version
python3 - <<'EOF'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
try:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    print(f"schema_version: {row[0] if row else 'schema_meta absent (version=0)'}")
except Exception as e:
    print(f"schema_meta not found: {e}")
conn.close()
EOF ~/.vnx-data/state/quality_intelligence.db
```

```bash
# 2. Check runtime_schema_version (runtime_coordination.db)
sqlite3 ~/.vnx-data/state/runtime_coordination.db \
  "SELECT version, description FROM runtime_schema_version ORDER BY version DESC LIMIT 5;"
```

```bash
# 3. Verify backup exists
ls -lh ~/Documents/vnx-pre-p4-auto-backup-*/manifest.sha256
```

If no backup exists: **create one first**.

```bash
tar -czf ~/Documents/vnx-rollback-manual-$(date +%Y%m%dT%H%M%S).tar.gz \
  ~/.vnx-data/state/
```

---

## Rollback order reference

Migrations must be rolled back in reverse numeric order. Do not skip.

| Rollback order | Migration | Target DB |
|---|---|---|
| 1st | 2026_05_task_subclass_down.sql | quality_intelligence.db |
| 2nd (QI) | 2026_05_intelligence_hygiene_down.sql | quality_intelligence.db |
| 2nd (RC) | 2026_05_intelligence_hygiene_runtime_down.sql | runtime_coordination.db |
| 3rd | 2026_05_intelligence_ab_arm_down.sql | runtime_coordination.db |
| 4th | 0021_central_install_metadata_down.sql | (central install DB or runtime_coordination.db) |
| 5th | 0020_elastic_worker_pool_down.sql | runtime_coordination.db |
| 6th | 0019_t0_lifecycle_tokens_down.sql | runtime_coordination.db |
| 7th | 0018_cross_project_intelligence_down.sql | global_intelligence.db |
| 8th | 0017_multi_tenant_lease_isolation_down.sql | runtime_coordination.db |
| 9th | 0016_rebuild_fts5_down.sql | quality_intelligence.db |
| 10th | 0015_complete_project_id_down.sql | quality_intelligence.db + runtime_coordination.db |
| 11th | 0014_add_report_findings_down.sql | quality_intelligence.db |
| 12th | 0013_normalize_tag_combination_down.sql | quality_intelligence.db |
| 13th | 0012_add_pattern_content_hash_down.sql | quality_intelligence.db |
| 14th | 0011_add_pattern_category_down.sql | quality_intelligence.db |
| 15th | 0010_add_project_id_down.sql | quality_intelligence.db + runtime_coordination.db |

---

## Scenario 1: Rollback last migration

To undo only the most recently applied migration (example: 0021):

```bash
# Set variables
MIGS=schemas/migrations
QI=~/.vnx-data/state/quality_intelligence.db
RC=~/.vnx-data/state/runtime_coordination.db

# Apply down migration
sqlite3 "$RC" < "$MIGS/0021_central_install_metadata_down.sql"
echo "Exit code: $?"

# Verify table is gone
sqlite3 "$RC" "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'central_install%';"
# Expected: no output

# Update schema_meta
sqlite3 "$RC" "UPDATE schema_meta SET value='20' WHERE key='schema_version';"
sqlite3 "$QI" "UPDATE schema_meta SET value='20' WHERE key='schema_version';"

# Record NDJSON audit event for manual schema_meta mutation (ADR-005)
python3 -c "
import json
from datetime import datetime, timezone
from pathlib import Path
event = {
    'event_type': 'manual_schema_meta_mutation',
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'operator': 'manual_rollback',
    'reason': '<vul reden in>',
}
p = Path.home() / '.vnx-data' / 'events' / 'schema_versioning.ndjson'
p.parent.mkdir(parents=True, exist_ok=True)
with open(p, 'a') as f:
    f.write(json.dumps(event) + '\n')
"
```

---

## Scenario 2: Rollback multiple migrations

Apply down files sequentially in reverse order. Stop at the target version.

```bash
MIGS=schemas/migrations
RC=~/.vnx-data/state/runtime_coordination.db
QI=~/.vnx-data/state/quality_intelligence.db

# Example: rollback from v16 to v11 (reverse order)
sqlite3 "$QI" < "$MIGS/0016_rebuild_fts5_down.sql"
sqlite3 "$QI" < "$MIGS/0015_complete_project_id_down.sql"
sqlite3 "$RC" < "$MIGS/0015_complete_project_id_down.sql"
sqlite3 "$QI" < "$MIGS/0014_add_report_findings_down.sql"
sqlite3 "$QI" < "$MIGS/0013_normalize_tag_combination_down.sql"
sqlite3 "$QI" < "$MIGS/0012_add_pattern_content_hash_down.sql"

# Update schema_meta to target version
sqlite3 "$RC" "UPDATE schema_meta SET value='11' WHERE key='schema_version';"
sqlite3 "$QI" "UPDATE schema_meta SET value='11' WHERE key='schema_version';"

# Record NDJSON audit event for manual schema_meta mutation (ADR-005)
python3 -c "
import json
from datetime import datetime, timezone
from pathlib import Path
event = {
    'event_type': 'manual_schema_meta_mutation',
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'operator': 'manual_rollback',
    'reason': '<vul reden in>',
}
p = Path.home() / '.vnx-data' / 'events' / 'schema_versioning.ndjson'
p.parent.mkdir(parents=True, exist_ok=True)
with open(p, 'a') as f:
    f.write(json.dumps(event) + '\n')
"
```

For migrations that target both QI and RC (0010, 0015), the SQL file contains
two sections separated by a comment. Apply each section to the correct database:

```bash
# 0015 QI section — stops at the empty line before @db: runtime_coordination
sqlite3 "$QI" <<'EOF'
-- paste QI section of 0015_complete_project_id_down.sql here
EOF

# 0015 RC section
sqlite3 "$RC" <<'EOF'
-- paste RC section of 0015_complete_project_id_down.sql here
EOF
```

---

## Scenario 3: Force rollback at corruption

If the DB is partially corrupted (e.g. migration failed mid-way), restore from backup:

```bash
# Find latest backup
BACKUP=$(ls -td ~/Documents/vnx-pre-p4-auto-backup-* | head -1)
echo "Using backup: $BACKUP"

# Verify backup integrity
sha256sum --check "$BACKUP/manifest.sha256" && echo "Backup OK"

# Stop all VNX processes first
vnx stop --all 2>/dev/null || true

# Restore — atomic pattern (temp + verify + mv) prevents corruption on interrupted restore
QI_TMP=~/.vnx-data/state/quality_intelligence.db.restore-tmp
cp "$BACKUP/vnx-dev/quality_intelligence.db" "$QI_TMP"
sqlite3 "$QI_TMP" "PRAGMA integrity_check" | head -5
mv "$QI_TMP" ~/.vnx-data/state/quality_intelligence.db

RC_TMP=~/.vnx-data/state/runtime_coordination.db.restore-tmp
cp "$BACKUP/vnx-dev/runtime_coordination.db" "$RC_TMP"
sqlite3 "$RC_TMP" "PRAGMA integrity_check" | head -5
mv "$RC_TMP" ~/.vnx-data/state/runtime_coordination.db

echo "Restore complete"
```

---

## Post-rollback validation

Run these checks after every rollback before restarting VNX:

```bash
QI=~/.vnx-data/state/quality_intelligence.db
RC=~/.vnx-data/state/runtime_coordination.db

# 1. schema_version sanity
echo "=== schema_meta QI ==="
sqlite3 "$QI" "SELECT key, value FROM schema_meta;"

echo "=== schema_meta RC ==="
sqlite3 "$RC" "SELECT key, value FROM schema_meta;"

# 2. runtime_schema_version sequence
echo "=== runtime_schema_version ==="
sqlite3 "$RC" "SELECT version, description FROM runtime_schema_version ORDER BY version;"

# 3. Row counts in key tables (expect > 0)
echo "=== Row counts ==="
sqlite3 "$QI" "SELECT COUNT(*) as patterns FROM success_patterns;"
sqlite3 "$RC" "SELECT COUNT(*) as dispatches FROM dispatches;"
sqlite3 "$RC" "SELECT COUNT(*) as leases FROM terminal_leases;"

# 4. Index integrity
sqlite3 "$RC" "PRAGMA integrity_check;" | head -5
sqlite3 "$QI" "PRAGMA integrity_check;" | head -5

# 5. Foreign key check
sqlite3 "$RC" "PRAGMA foreign_keys=ON; PRAGMA foreign_key_check;" | head -10
```

Expected: `integrity_check` returns `ok`, `foreign_key_check` returns no rows.

---

## schema_version reference table

| schema_version | After migration |
|---|---|
| 0 | Fresh bootstrap (no migrations applied) |
| 10 | 0010_add_project_id applied |
| 15 | 0015_complete_project_id applied |
| 16 | 0016_rebuild_fts5 applied |

The `schema_meta.schema_version` integer is maintained by `scripts/lib/schema_versioning.py`
and is independent of `runtime_schema_version` (integer per-migration rows in RC)
and `schema_version` (text PK audit log in QI).

---

---

## Pre-apply snapshot (mandatory before every --apply)

Before running `migrate_to_central_vnx.py --apply` for any project, snapshot the
central DB to an archive directory. This snapshot is the fast-restore point for
Scenario X below.

```bash
# Set once per apply session
CENTRAL_DB=~/.vnx-data/state/quality_intelligence.db
FAILED_PROJECT_ID="mission-control"   # fill in target project
ARCHIVE_DIR=~/Archive/pre-apply-${FAILED_PROJECT_ID}-$(date +%Y%m%dT%H%M%S)

mkdir -p "$ARCHIVE_DIR"
cp "$CENTRAL_DB" "$ARCHIVE_DIR/quality_intelligence.db"
sqlite3 "$CENTRAL_DB" ".dump" > "$ARCHIVE_DIR/quality_intelligence.sql"
echo "Snapshot written to: $ARCHIVE_DIR"
```

Verify the snapshot is readable before proceeding:

```bash
sqlite3 "$ARCHIVE_DIR/quality_intelligence.db" "PRAGMA integrity_check;" | head -3
```

Expected: `ok`

---

## Scenario X — Partial migration failure (central store cleanup)

Use when project X (e.g. mission-control) fails halfway through `--apply` to the
central store, leaving partial rows that must be removed before retrying or
rolling back the source project.

### Step 1 — Identify the partially migrated project

Check which project has an inconsistent row count in the central DB:

```bash
CENTRAL_DB=~/.vnx-data/state/quality_intelligence.db

sqlite3 "$CENTRAL_DB" "
SELECT project_id, COUNT(*) as rows
FROM success_patterns
GROUP BY project_id
ORDER BY rows DESC;"

sqlite3 "$CENTRAL_DB" "
SELECT project_id, COUNT(*) as rows
FROM antipatterns
GROUP BY project_id
ORDER BY rows DESC;"
```

Compare against the source project DB to confirm the mismatch.

### Step 2 — Pause VNX to prevent concurrent writes

```bash
vnx pause partial_migration_cleanup
```

Verify the PAUSED marker exists:

```bash
cat ~/.vnx-data/state/PAUSED
```

Do NOT skip this step. Concurrent dispatcher writes during cleanup corrupt the
central store.

### Step 3 — Central store cleanup

Replace `$FAILED_PROJECT_ID` with the actual project_id from Step 1.

```bash
CENTRAL_DB=~/.vnx-data/state/quality_intelligence.db
FAILED_PROJECT_ID="mission-control"

sqlite3 "$CENTRAL_DB" "
BEGIN IMMEDIATE;
DELETE FROM success_patterns    WHERE project_id = '${FAILED_PROJECT_ID}';
DELETE FROM antipatterns        WHERE project_id = '${FAILED_PROJECT_ID}';
DELETE FROM code_snippets       WHERE project_id = '${FAILED_PROJECT_ID}';
DELETE FROM snippet_metadata    WHERE project_id = '${FAILED_PROJECT_ID}';
DELETE FROM intelligence_injections WHERE project_id = '${FAILED_PROJECT_ID}';
DELETE FROM session_analytics   WHERE project_id = '${FAILED_PROJECT_ID}';
COMMIT;"
echo "Exit code: $?"
```

If any DELETE fails, restore from the pre-apply snapshot (Step 4b) and
investigate before retrying.

### Step 4a — Validate post-cleanup

Confirm no rows remain for the failed project:

```bash
sqlite3 "$CENTRAL_DB" "
SELECT
  (SELECT COUNT(*) FROM success_patterns    WHERE project_id='${FAILED_PROJECT_ID}') as patterns,
  (SELECT COUNT(*) FROM antipatterns        WHERE project_id='${FAILED_PROJECT_ID}') as antipatterns,
  (SELECT COUNT(*) FROM code_snippets       WHERE project_id='${FAILED_PROJECT_ID}') as snippets,
  (SELECT COUNT(*) FROM snippet_metadata    WHERE project_id='${FAILED_PROJECT_ID}') as snippet_meta,
  (SELECT COUNT(*) FROM intelligence_injections WHERE project_id='${FAILED_PROJECT_ID}') as injections;"
```

Expected: all zeros.

Run integrity check to confirm the DB is consistent:

```bash
sqlite3 "$CENTRAL_DB" "PRAGMA integrity_check;" | head -3
```

Expected: `ok`

### Step 4b — Restore from pre-apply snapshot (if cleanup fails)

If Step 3 produced errors or the integrity check fails:

```bash
ARCHIVE_DIR=~/Archive/pre-apply-${FAILED_PROJECT_ID}-<timestamp>
CENTRAL_DB_TMP=~/.vnx-data/state/quality_intelligence.db.restore-tmp

cp "$ARCHIVE_DIR/quality_intelligence.db" "$CENTRAL_DB_TMP"
sqlite3 "$CENTRAL_DB_TMP" "PRAGMA integrity_check;" | head -3
# Expected: ok
# Use python3 os.replace for guaranteed atomic swap on the same filesystem.
# Plain `mv` is non-atomic across filesystem boundaries and can cause partial reads
# if the tmp and target are on different mounts.
python3 -c "import os, sys; os.replace(sys.argv[1], sys.argv[2])" "$CENTRAL_DB_TMP" "$CENTRAL_DB"
echo "Central DB restored from snapshot."
```

### Step 5 — Re-run dry-run to confirm no traces

```bash
python3 scripts/migrate_to_central_vnx.py --project "$FAILED_PROJECT_ID" --dry-run
```

The dry-run should show no conflicts and report the correct row counts for the
project as if it were unmigrated.

### Step 6 — Resume VNX

```bash
vnx resume
```

Confirm lifecycle events were written:

```bash
tail -2 ~/.vnx-data/events/lifecycle.ndjson | python3 -m json.tool
```

Expected: one `service_paused` event and one `service_resumed` event.

---

## Open Items

- OI-ROLLBACK-1: Wire `--rollback-fts5` flag into `migrate_to_central_vnx.py` for
  Python-driven 0016 rollback (current _down.sql drops FTS5 data without repopulation).
- OI-ROLLBACK-2: Create helper script `scripts/rollback_migration.py` that reads
  `schema_meta.schema_version`, determines which _down.sql to apply, and updates
  schema_meta atomically.
- OI-ROLLBACK-3: Add 0010_down / 0015_down section split to a helper that correctly
  routes QI vs RC SQL sections without manual cut-paste.
