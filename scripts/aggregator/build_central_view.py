"""Build the unified read-only federation view DB.

Attaches each registered project's `quality_intelligence.db`,
`runtime_coordination.db`, and `dispatch_tracker.db` in `?mode=ro` and
materializes a small set of unified views in `~/.vnx-aggregator/data.db`.

Read-only at the source: writes only happen against the central view DB
under `~/.vnx-aggregator/`. The operator can delete that directory at
any moment without data loss.

CLI:
    python3 scripts/aggregator/build_central_view.py            # build
    python3 scripts/aggregator/build_central_view.py --dry-run  # plan only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from scripts.aggregator import (
    DEFAULT_AGGREGATOR_DB,
    DEFAULT_AGGREGATOR_DIR,
    DEFAULT_REGISTRY_PATH,
)

LOG = logging.getLogger("vnx.aggregator.build")

SOURCE_DBS = ("quality_intelligence.db", "runtime_coordination.db", "dispatch_tracker.db")

# Tables we materialize into the unified view. Each entry: (source_db, table, has_project_id).
# Tables marked has_project_id=True already carry a `project_id` column post-Phase 0; for
# legacy rows where the value is NULL we synthesize from the project's slug.
UNIFIED_TABLES: tuple[tuple[str, str, bool], ...] = (
    ("quality_intelligence.db", "success_patterns", True),
    ("quality_intelligence.db", "antipatterns", True),
    ("quality_intelligence.db", "dispatch_metadata", True),
    ("runtime_coordination.db", "dispatches", True),
    ("runtime_coordination.db", "terminal_leases", True),
)


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: Path
    project_id: str

    @property
    def state_dir(self) -> Path:
        return self.path / ".vnx-data" / "state"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def synthesize_project_id(name: str) -> str:
    """Slugify a project name into a `project_id` token.

    `[^a-z0-9]+` -> `-`, trimmed of leading/trailing dashes, max 32 chars.
    """
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot synthesize project_id from empty name: {name!r}")
    return slug[:32]


def load_registry(path: Path) -> list[ProjectEntry]:
    """Load `~/.vnx/projects.json` and return resolved `ProjectEntry` rows.

    Tolerates the schema_v1 format (no explicit `project_id`) — synthesizes
    from `name`. Raises FileNotFoundError if the registry is missing.
    """
    raw = json.loads(path.read_text())
    out: list[ProjectEntry] = []
    for entry in raw.get("projects", []):
        name = entry["name"]
        proj_path = Path(entry["path"]).expanduser()
        pid = entry.get("project_id") or synthesize_project_id(name)
        out.append(ProjectEntry(name=name, path=proj_path, project_id=pid))
    return out


def attach_readonly(con: sqlite3.Connection, alias: str, db_path: Path) -> None:
    """Attach `db_path` to `con` under `alias` in read-only mode (`?mode=ro`)."""
    uri = f"file:{db_path}?mode=ro"
    con.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))


def _table_exists(con: sqlite3.Connection, alias: str, table: str) -> bool:
    cur = con.execute(
        f"SELECT 1 FROM {alias}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _column_names(con: sqlite3.Connection, alias: str, table: str) -> list[str]:
    cur = con.execute(f"PRAGMA {alias}.table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _create_unified_table(con: sqlite3.Connection, table: str, columns: list[str]) -> None:
    # Drop & recreate so the run is fully idempotent and tolerant of column drift.
    con.execute(f"DROP TABLE IF EXISTS {table}_unified")
    col_defs = ", ".join(f'"{c}"' for c in columns)
    con.execute(f"CREATE TABLE {table}_unified ({col_defs})")


def _copy_table(
    con: sqlite3.Connection,
    alias: str,
    table: str,
    columns: list[str],
    project_id: str,
    has_project_id: bool,
) -> int:
    quoted = ", ".join(f'"{c}"' for c in columns)
    rows = con.execute(f"SELECT {quoted} FROM {alias}.{table}").fetchall()
    if not rows:
        return 0
    if has_project_id:
        idx = columns.index("project_id")
        rows = [
            tuple(project_id if i == idx and v in (None, "") else v for i, v in enumerate(r))
            for r in rows
        ]
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {table}_unified ({quoted}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def materialize_views(
    view_db_path: Path,
    projects: list[ProjectEntry],
    *,
    dry_run: bool = False,
) -> dict:
    """Build the unified view DB. In dry-run mode, only plan and emit a report."""
    plan: dict = {"view_db": str(view_db_path), "projects": []}

    if dry_run:
        for proj in projects:
            attached: list[dict] = []
            for db_name in SOURCE_DBS:
                src = proj.state_dir / db_name
                attached.append({"db": db_name, "path": str(src), "exists": src.is_file()})
            plan["projects"].append(
                {
                    "name": proj.name,
                    "project_id": proj.project_id,
                    "attached": attached,
                }
            )
            print(
                f"DRY-RUN: would attach project={proj.project_id} "
                f"path={proj.path} ({sum(1 for a in attached if a['exists'])}/"
                f"{len(SOURCE_DBS)} dbs present)",
                file=sys.stderr,
            )
        plan["dry_run"] = True
        return plan

    view_db_path.parent.mkdir(parents=True, exist_ok=True)
    if view_db_path.exists():
        view_db_path.unlink()

    con = sqlite3.connect(view_db_path, uri=False)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        # Create per-table aggregates by attaching every project for one source DB,
        # collecting the union of columns, then copying.
        for source_db, table, has_pid in UNIFIED_TABLES:
            per_project_cols: dict[str, list[str]] = {}
            attached_aliases: list[tuple[str, ProjectEntry]] = []
            for idx, proj in enumerate(projects):
                src_path = proj.state_dir / source_db
                if not src_path.is_file():
                    continue
                alias = f"src_{idx}"
                try:
                    attach_readonly(con, alias, src_path)
                except sqlite3.Error as exc:
                    LOG.warning("attach failed project=%s db=%s err=%s", proj.project_id, source_db, exc)
                    continue
                attached_aliases.append((alias, proj))
                if _table_exists(con, alias, table):
                    per_project_cols[alias] = _column_names(con, alias, table)

            if not per_project_cols:
                for alias, _ in attached_aliases:
                    con.execute(f"DETACH DATABASE {alias}")
                continue

            union_cols: list[str] = []
            for cols in per_project_cols.values():
                for c in cols:
                    if c not in union_cols:
                        union_cols.append(c)
            if has_pid and "project_id" not in union_cols:
                union_cols.append("project_id")

            _create_unified_table(con, table, union_cols)

            row_total = 0
            per_proj: dict = {}
            for alias, proj in attached_aliases:
                if alias not in per_project_cols:
                    continue
                source_cols = per_project_cols[alias]
                # Build a column list that matches `union_cols`; missing cols get NULL.
                select_parts: list[str] = []
                for c in union_cols:
                    if c in source_cols:
                        select_parts.append(f'"{c}"')
                    elif c == "project_id":
                        select_parts.append(f"'{proj.project_id}'")
                    else:
                        select_parts.append("NULL")
                select_sql = (
                    f"SELECT {', '.join(select_parts)} FROM {alias}.{table}"
                )
                rows = con.execute(select_sql).fetchall()
                if has_pid:
                    pid_idx = union_cols.index("project_id")
                    rows = [
                        tuple(
                            proj.project_id if i == pid_idx and v in (None, "") else v
                            for i, v in enumerate(r)
                        )
                        for r in rows
                    ]
                if rows:
                    placeholders = ", ".join("?" for _ in union_cols)
                    quoted = ", ".join(f'"{c}"' for c in union_cols)
                    con.executemany(
                        f"INSERT INTO {table}_unified ({quoted}) VALUES ({placeholders})",
                        rows,
                    )
                row_total += len(rows)
                per_proj[proj.project_id] = len(rows)

            # Commit & close any implicit txn before DETACH; otherwise SQLite
            # rejects the DETACH because the attached DB is still locked.
            con.commit()
            for alias, _ in attached_aliases:
                con.execute(f"DETACH DATABASE {alias}")

            plan["projects"].append(
                {"table": f"{table}_unified", "rows": row_total, "per_project": per_proj}
            )
            LOG.info("materialized %s_unified rows=%d", table, row_total)

        con.commit()
    finally:
        con.close()

    plan["dry_run"] = False
    return plan


def _default_view_db_path() -> Path:
    return Path(os.environ.get("VNX_AGGREGATOR_DIR", DEFAULT_AGGREGATOR_DIR)).expanduser() / DEFAULT_AGGREGATOR_DB


def _default_registry_path() -> Path:
    return Path(os.environ.get("VNX_REGISTRY_PATH", DEFAULT_REGISTRY_PATH)).expanduser()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no writes to view DB")
    parser.add_argument("--registry", type=Path, default=None, help="Override path to projects.json")
    parser.add_argument("--view-db", type=Path, default=None, help="Override view DB path")
    parser.add_argument("--json", action="store_true", help="Emit plan/result as JSON to stdout")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    registry_path = args.registry or _default_registry_path()
    view_db_path = args.view_db or _default_view_db_path()

    try:
        projects = load_registry(registry_path)
    except FileNotFoundError:
        print(f"ERROR: registry not found at {registry_path}", file=sys.stderr)
        return 2

    plan = materialize_views(view_db_path, projects, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(plan, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
