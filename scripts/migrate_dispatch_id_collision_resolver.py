#!/usr/bin/env python3
"""Dispatch_id collision resolver for VNX central-DB migration.

Resolves cross-project dispatch_id collisions detected by migrate_dry_run.py
before the central-DB --apply can run safely.

Strategy (deterministic, auditable):
  1. For each colliding dispatch_id: new_id = f"{project_id}-{original_id}"
  2. If the prefixed ID already exists in the source DB (recursive collision):
     fall back to a stable UUID4 derived from (project_id, original_id)
  3. Write full rewrite mapping to claudedocs/dispatch-id-rewrite-YYYY-MM-DD.json
     for audit trail before any --apply writes happen.

Tables updated in --apply mode (all tables that carry dispatch_id):
  runtime_coordination.db:
    - dispatches.dispatch_id
    - dispatch_attempts.dispatch_id
    - coordination_events.entity_id WHERE entity_type = 'dispatch'
    - terminal_leases (no dispatch_id column — skip)
    - intelligence_injections.dispatch_id
  quality_intelligence.db:
    - dispatch_metadata.dispatch_id
    - dispatch_pattern_offered.dispatch_id
    - confidence_events.dispatch_id (if column exists)
    - dispatch_quality_context.dispatch_id (if column exists)
    - dispatch_experiments.dispatch_id (if table exists)

CLI:
    python3 scripts/migrate_dispatch_id_collision_resolver.py \\
      --dry-run | --apply \\
      --source-db <path> \\
      --project-id <id> \\
      --collision-list <json-manifest-from-dry-run>

Exit codes:
    0 — success (dry-run or apply)
    1 — invalid arguments
    2 — source DB not accessible
    3 — collision list parse error
    4 — apply failure (partial write — check audit log)
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

LOG = logging.getLogger("vnx.migrate.collision_resolver")

# Tables in runtime_coordination.db that carry dispatch_id directly
_RC_DISPATCH_ID_TABLES: list[str] = [
    "dispatches",
    "dispatch_attempts",
    "intelligence_injections",
]

# Tables in quality_intelligence.db that carry dispatch_id directly
_QI_DISPATCH_ID_TABLES: list[str] = [
    "dispatch_metadata",
    "dispatch_pattern_offered",
    "confidence_events",
    "dispatch_quality_context",
    "dispatch_experiments",
]

# coordination_events.entity_id when entity_type='dispatch' is a dispatch_id carrier
_COORDINATION_ENTITY_TABLE = "coordination_events"
_COORDINATION_ENTITY_ID_COL = "entity_id"
_COORDINATION_ENTITY_TYPE_COL = "entity_type"
_DISPATCH_ENTITY_TYPE = "dispatch"


@dataclass
class RewriteEntry:
    original_id: str
    new_id: str
    strategy: str  # 'prefix' | 'uuid_fallback'
    project_id: str


@dataclass
class ResolverResult:
    project_id: str
    source_db: str
    total_collisions: int
    rewrites: list[RewriteEntry] = field(default_factory=list)
    tables_updated: list[str] = field(default_factory=list)
    rows_updated: int = 0
    dry_run: bool = True


def _stable_uuid(project_id: str, original_id: str) -> str:
    """UUID4 derived deterministically from project + original via UUID5 namespace."""
    ns = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # DNS namespace
    seed = f"{project_id}:{original_id}"
    return str(uuid.uuid5(ns, seed))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _collect_existing_ids(conn: sqlite3.Connection, table: str, id_col: str) -> set[str]:
    """Return all dispatch_id values from a table, or empty set if table absent."""
    if not _table_exists(conn, table):
        return set()
    rows = conn.execute(f"SELECT {id_col} FROM {table}").fetchall()
    return {r[0] for r in rows if r[0] is not None}


def _collect_all_existing_dispatch_ids(conn: sqlite3.Connection, db_label: str) -> set[str]:
    """Collect every dispatch_id across all relevant tables in a DB."""
    existing: set[str] = set()
    if db_label == "rc":
        for tbl in _RC_DISPATCH_ID_TABLES:
            if _table_exists(conn, tbl) and _column_exists(conn, tbl, "dispatch_id"):
                existing |= _collect_existing_ids(conn, tbl, "dispatch_id")
        if _table_exists(conn, _COORDINATION_ENTITY_TABLE):
            rows = conn.execute(
                f"SELECT {_COORDINATION_ENTITY_ID_COL} FROM {_COORDINATION_ENTITY_TABLE} "
                f"WHERE {_COORDINATION_ENTITY_TYPE_COL} = ?",
                (_DISPATCH_ENTITY_TYPE,),
            ).fetchall()
            existing |= {r[0] for r in rows if r[0] is not None}
    else:
        for tbl in _QI_DISPATCH_ID_TABLES:
            if _table_exists(conn, tbl) and _column_exists(conn, tbl, "dispatch_id"):
                existing |= _collect_existing_ids(conn, tbl, "dispatch_id")
    return existing


def build_rewrite_map(
    colliding_ids: list[str],
    project_id: str,
    all_existing: set[str],
) -> list[RewriteEntry]:
    """Compute deterministic new_id for each colliding dispatch_id.

    Uses project-prefix strategy; falls back to UUID5 if prefix collides.
    The resulting map is idempotent: calling twice with same inputs yields
    identical output.
    """
    rewrites: list[RewriteEntry] = []
    already_assigned: set[str] = set()

    for orig in sorted(colliding_ids):
        candidate = f"{project_id}-{orig}"
        if candidate not in all_existing and candidate not in already_assigned:
            strategy = "prefix"
            new_id = candidate
        else:
            new_id = _stable_uuid(project_id, orig)
            strategy = "uuid_fallback"

        already_assigned.add(new_id)
        rewrites.append(
            RewriteEntry(
                original_id=orig,
                new_id=new_id,
                strategy=strategy,
                project_id=project_id,
            )
        )

    return rewrites


def _update_table_dispatch_id(
    conn: sqlite3.Connection,
    table: str,
    id_col: str,
    rewrite_map: dict[str, str],
    dry_run: bool,
) -> int:
    """Rewrite dispatch_id values in a table. Returns number of rows updated."""
    if not _table_exists(conn, table) or not _column_exists(conn, table, id_col):
        return 0

    present_ids = _collect_existing_ids(conn, table, id_col)
    to_update = {old: new for old, new in rewrite_map.items() if old in present_ids}
    if not to_update:
        return 0

    updated = 0
    for old_id, new_id in to_update.items():
        if dry_run:
            LOG.info("[DRY-RUN] Would UPDATE %s SET %s='%s' WHERE %s='%s'",
                     table, id_col, new_id, id_col, old_id)
        else:
            conn.execute(
                f"UPDATE {table} SET {id_col} = ? WHERE {id_col} = ?",
                (new_id, old_id),
            )
        updated += 1

    return updated


def _update_coordination_events(
    conn: sqlite3.Connection,
    rewrite_map: dict[str, str],
    dry_run: bool,
) -> int:
    """Rewrite entity_id in coordination_events where entity_type='dispatch'."""
    if not _table_exists(conn, _COORDINATION_ENTITY_TABLE):
        return 0

    rows = conn.execute(
        f"SELECT rowid, {_COORDINATION_ENTITY_ID_COL} FROM {_COORDINATION_ENTITY_TABLE} "
        f"WHERE {_COORDINATION_ENTITY_TYPE_COL} = ?",
        (_DISPATCH_ENTITY_TYPE,),
    ).fetchall()

    updated = 0
    for rowid, entity_id in rows:
        if entity_id in rewrite_map:
            new_id = rewrite_map[entity_id]
            if dry_run:
                LOG.info("[DRY-RUN] Would UPDATE coordination_events SET entity_id='%s' "
                         "WHERE rowid=%s", new_id, rowid)
            else:
                conn.execute(
                    f"UPDATE {_COORDINATION_ENTITY_TABLE} "
                    f"SET {_COORDINATION_ENTITY_ID_COL} = ? WHERE rowid = ?",
                    (new_id, rowid),
                )
            updated += 1

    return updated


def resolve_collisions(
    source_db: Path,
    project_id: str,
    colliding_ids: list[str],
    dry_run: bool,
) -> ResolverResult:
    """Main resolution logic: build map, optionally apply to source DB.

    Operates on a single source DB file. For each project, call once for
    runtime_coordination.db and once for quality_intelligence.db, or pass
    the DB that contains the colliding records.

    The function determines which tables to touch based on what tables exist
    in the DB — no assumptions about DB type are made from the path.
    """
    result = ResolverResult(
        project_id=project_id,
        source_db=str(source_db),
        total_collisions=len(colliding_ids),
        dry_run=dry_run,
    )

    if not colliding_ids:
        LOG.info("No collisions for project=%s in %s — nothing to do", project_id, source_db)
        return result

    if not source_db.exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    # Collect all existing IDs across both DB types (we check per-table presence)
    with sqlite3.connect(source_db) as conn:
        all_existing_rc = _collect_all_existing_dispatch_ids(conn, "rc")
        all_existing_qi = _collect_all_existing_dispatch_ids(conn, "qi")
    all_existing = all_existing_rc | all_existing_qi

    rewrites = build_rewrite_map(colliding_ids, project_id, all_existing)
    result.rewrites = rewrites
    rewrite_map = {r.original_id: r.new_id for r in rewrites}

    if not rewrites:
        return result

    if dry_run:
        LOG.info("[DRY-RUN] %d rewrites planned for project=%s in %s",
                 len(rewrites), project_id, source_db)
        for entry in rewrites:
            LOG.info("  %s -> %s (%s)", entry.original_id, entry.new_id, entry.strategy)
        return result

    # --apply path: write inside a single transaction
    with sqlite3.connect(source_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")

            total_updated = 0

            # RC tables
            for tbl in _RC_DISPATCH_ID_TABLES:
                n = _update_table_dispatch_id(conn, tbl, "dispatch_id", rewrite_map, dry_run=False)
                if n > 0:
                    result.tables_updated.append(tbl)
                    total_updated += n

            n = _update_coordination_events(conn, rewrite_map, dry_run=False)
            if n > 0:
                result.tables_updated.append(_COORDINATION_ENTITY_TABLE)
                total_updated += n

            # QI tables
            for tbl in _QI_DISPATCH_ID_TABLES:
                n = _update_table_dispatch_id(conn, tbl, "dispatch_id", rewrite_map, dry_run=False)
                if n > 0:
                    result.tables_updated.append(tbl)
                    total_updated += n

            conn.execute("COMMIT")
            result.rows_updated = total_updated
            LOG.info("Applied %d ID rewrites across %d tables in %s",
                     total_updated, len(result.tables_updated), source_db)

        except Exception:
            conn.execute("ROLLBACK")
            raise

    return result


def write_audit_log(
    results: list[ResolverResult],
    output_path: Path,
) -> None:
    """Write JSON mapping table for audit trail."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": str(date.today()),
        "dry_run": all(r.dry_run for r in results),
        "projects": [
            {
                "project_id": r.project_id,
                "source_db": r.source_db,
                "total_collisions": r.total_collisions,
                "rows_updated": r.rows_updated,
                "tables_updated": r.tables_updated,
                "rewrites": [
                    {
                        "original_id": e.original_id,
                        "new_id": e.new_id,
                        "strategy": e.strategy,
                    }
                    for e in r.rewrites
                ],
            }
            for r in results
        ],
    }

    tmp = output_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(output_path)
    LOG.info("Audit log written: %s", output_path)


def load_collision_list(collision_list_path: Path, project_id: str) -> list[str]:
    """Extract colliding dispatch_ids for a specific project from a dry-run manifest.

    The manifest format (from migrate_dry_run.py) is:
      {
        "collisions": {
          "dispatch_id": {
            "<original_id>": ["project_a", "project_b", ...]
          }
        }
      }

    We return all dispatch_ids where project_id appears in the projects list.
    Also accepts a simpler flat-list format: ["id1", "id2", ...] for direct use.
    """
    if not collision_list_path.exists():
        raise FileNotFoundError(f"Collision list not found: {collision_list_path}")

    raw = json.loads(collision_list_path.read_text(encoding="utf-8"))

    # Simple flat list
    if isinstance(raw, list):
        return raw

    # Full dry-run manifest
    collisions_section = raw.get("collisions", {})
    dispatch_collisions = collisions_section.get("dispatch_id", {})

    result = []
    for dispatch_id, projects in dispatch_collisions.items():
        if project_id in projects:
            result.append(dispatch_id)

    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Resolve dispatch_id collisions in a VNX source DB before central migration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Print planned rewrites; write NO bytes to any DB")
    mode.add_argument("--apply", action="store_true",
                      help="Write rewrites to source DB inside a single transaction")
    p.add_argument("--source-db", type=Path, required=True,
                   help="Path to the source SQLite DB file to resolve")
    p.add_argument("--project-id", required=True,
                   help="Project identifier (e.g. example-project)")
    p.add_argument("--collision-list", type=Path, required=True,
                   help="Path to migrate_dry_run JSON manifest or flat list of IDs")
    p.add_argument(
        "--audit-out", type=Path,
        default=REPO_ROOT / "claudedocs" / f"dispatch-id-rewrite-{date.today()}.json",
        help="Output path for JSON audit log",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        colliding_ids = load_collision_list(args.collision_list, args.project_id)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        LOG.error("Failed to parse collision list: %s", exc)
        return 3

    if not colliding_ids:
        LOG.info("No collisions found for project_id=%s — nothing to resolve", args.project_id)
        print(f"0 collisions for {args.project_id} — no rewrites needed.")
        return 0

    LOG.info("%d collisions loaded for project_id=%s", len(colliding_ids), args.project_id)

    try:
        result = resolve_collisions(
            source_db=args.source_db,
            project_id=args.project_id,
            colliding_ids=colliding_ids,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        LOG.error("%s", exc)
        return 2
    except Exception as exc:
        LOG.error("Resolution failed: %s", exc)
        return 4

    write_audit_log([result], args.audit_out)

    prefix_count = sum(1 for r in result.rewrites if r.strategy == "prefix")
    uuid_count = sum(1 for r in result.rewrites if r.strategy == "uuid_fallback")
    mode_label = "DRY-RUN" if args.dry_run else "APPLIED"

    print(f"[{mode_label}] project={args.project_id}")
    print(f"  Collisions: {result.total_collisions}")
    print(f"  Rewrites: {len(result.rewrites)} (prefix={prefix_count}, uuid_fallback={uuid_count})")
    if not args.dry_run:
        print(f"  Rows updated: {result.rows_updated}")
        print(f"  Tables touched: {', '.join(result.tables_updated) or 'none'}")
    print(f"  Audit log: {args.audit_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
