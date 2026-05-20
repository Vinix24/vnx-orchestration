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

## Open Items

- OI-ROLLBACK-1: Wire `--rollback-fts5` flag into `migrate_to_central_vnx.py` for
  Python-driven 0016 rollback (current _down.sql drops FTS5 data without repopulation).
- OI-ROLLBACK-2: Create helper script `scripts/rollback_migration.py` that reads
  `schema_meta.schema_version`, determines which _down.sql to apply, and updates
  schema_meta atomically.
- OI-ROLLBACK-3: Add 0010_down / 0015_down section split to a helper that correctly
  routes QI vs RC SQL sections without manual cut-paste.
