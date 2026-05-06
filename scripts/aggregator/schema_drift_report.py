"""Schema-drift report across all registered project DBs.

Preflight tool for w6-p4 (one-shot data import). Attaches every project
DB in `?mode=ro`, walks `sqlite_master`, and prints (or emits JSON) the
per-project table list plus column-level diffs against the reference
project (first entry in the registry).

Read-only: never writes to source DBs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from scripts.aggregator.build_central_view import (
    SOURCE_DBS,
    ProjectEntry,
    _default_registry_path,
    attach_readonly,
    load_registry,
)


def _project_schema(project: ProjectEntry) -> dict[str, dict[str, list[str]]]:
    """Return `{db_name: {table: [columns]}}` for one project (read-only).

    Missing DBs and missing tables are silently skipped (registry-light operation);
    the caller computes drift by diffing the dicts.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for db_name in SOURCE_DBS:
        path = project.state_dir / db_name
        if not path.is_file():
            out[db_name] = {}
            continue
        con = sqlite3.connect(":memory:")
        try:
            try:
                attach_readonly(con, "src", path)
            except sqlite3.Error:
                out[db_name] = {}
                continue
            tables: dict[str, list[str]] = {}
            for (name,) in con.execute(
                "SELECT name FROM src.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall():
                cols = [row[1] for row in con.execute(f"PRAGMA src.table_info({name})")]
                tables[name] = cols
            out[db_name] = tables
            con.execute("DETACH DATABASE src")
        finally:
            con.close()
    return out


def compute_drift(
    schemas: dict[str, dict[str, dict[str, list[str]]]],
) -> dict:
    """Compute drift relative to the first project (the reference).

    Returns:
      {
        "reference": "<project_id>",
        "projects": {
            "<project_id>": {
                "<db>": {
                    "missing_tables": [...],
                    "extra_tables": [...],
                    "column_diffs": {"<table>": {"missing": [...], "extra": [...]}},
                }
            }
        }
      }
    """
    project_ids = list(schemas.keys())
    if not project_ids:
        return {"reference": None, "projects": {}}
    ref = project_ids[0]
    ref_schema = schemas[ref]
    drift: dict = {"reference": ref, "projects": {}}
    for pid in project_ids:
        per_db: dict = {}
        for db_name in SOURCE_DBS:
            ref_tables = ref_schema.get(db_name, {})
            this_tables = schemas[pid].get(db_name, {})
            ref_names = set(ref_tables)
            this_names = set(this_tables)
            missing = sorted(ref_names - this_names)
            extra = sorted(this_names - ref_names)
            col_diffs: dict[str, dict[str, list[str]]] = {}
            for tbl in ref_names & this_names:
                ref_cols = set(ref_tables[tbl])
                this_cols = set(this_tables[tbl])
                if ref_cols != this_cols:
                    col_diffs[tbl] = {
                        "missing": sorted(ref_cols - this_cols),
                        "extra": sorted(this_cols - ref_cols),
                    }
            per_db[db_name] = {
                "missing_tables": missing,
                "extra_tables": extra,
                "column_diffs": col_diffs,
            }
        drift["projects"][pid] = per_db
    return drift


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Emit drift report as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    registry_path = args.registry or _default_registry_path()
    try:
        projects = load_registry(registry_path)
    except FileNotFoundError:
        print(f"ERROR: registry not found at {registry_path}", file=sys.stderr)
        return 2

    schemas = {p.project_id: _project_schema(p) for p in projects}
    drift = compute_drift(schemas)

    if args.json:
        print(json.dumps(drift, indent=2))
        return 0

    print(f"Reference project: {drift['reference']}")
    for pid, per_db in drift["projects"].items():
        print(f"\nProject: {pid}")
        for db_name, diff in per_db.items():
            tag = "OK" if not (diff["missing_tables"] or diff["extra_tables"] or diff["column_diffs"]) else "DRIFT"
            print(f"  {db_name}: {tag}")
            if diff["missing_tables"]:
                print(f"    missing tables: {', '.join(diff['missing_tables'])}")
            if diff["extra_tables"]:
                print(f"    extra tables:   {', '.join(diff['extra_tables'])}")
            for tbl, cdiff in diff["column_diffs"].items():
                print(
                    f"    column drift in {tbl}: "
                    f"missing={cdiff['missing']} extra={cdiff['extra']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
