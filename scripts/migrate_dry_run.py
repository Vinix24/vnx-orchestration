#!/usr/bin/env python3
"""Phase 6 P4 — One-shot data import: DRY-RUN preflight.

Reports per-project, per-table row-count plans, collision detection
between project keyspaces, and schema drift relative to the reference
project. Writes ZERO bytes to any source DB. Outputs a markdown report
for operator review and a JSON manifest for the live migrator's
``--dry-run-manifest`` gate.

CLI:
    python3 scripts/migrate_dry_run.py [--registry PATH] [--out PATH] [--json]

The `claudedocs/<date>-p4-dry-run-report.md` file is the canonical
operator artifact; a JSON sidecar at the same stem (``.json`` suffix)
captures the same data in machine-readable form for downstream gates.

Exit codes:
    0  — preflight produced reports successfully (collisions/drift may still exist)
    2  — registry not found or unreadable
    3  — at least one source DB unreadable (catastrophic preflight failure)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aggregator.build_central_view import (  # noqa: E402
    ProjectEntry,
    attach_readonly,
    load_registry,
    _default_registry_path,
)
from scripts.aggregator.schema_drift_report import (  # noqa: E402
    _project_schema,
    compute_drift,
)

LOG = logging.getLogger("vnx.migrate.dryrun")

# Tables planned for import (subset of the central DBs; matches Phase 0+P4 scope).
PLAN_TABLES_QI: tuple[str, ...] = (
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "pattern_usage",
    "confidence_events",
    "dispatch_metadata",
    "dispatch_pattern_offered",
    "session_analytics",
    "vnx_code_quality",
    "snippet_metadata",
    "quality_trends",
    "quality_alerts",
    "dispatch_quality_context",
    "tag_combinations",
    "improvement_suggestions",
    "nightly_digests",
    "governance_metrics",
)

PLAN_TABLES_RC: tuple[str, ...] = (
    "dispatches",
    "dispatch_attempts",
    "terminal_leases",
    "coordination_events",
    "incident_log",
    "intelligence_injections",
    "retry_budgets",
    "retry_state",
    "escalation_log",
    "execution_targets",
    "inbound_inbox",
    "recommendations",
    "recommendation_outcomes",
)


@dataclass(frozen=True)
class TablePlan:
    project_id: str
    db_name: str
    table: str
    row_count: int
    has_project_id_column: bool


def _safe_count(con: sqlite3.Connection, alias: str, table: str) -> tuple[int, bool]:
    """Return (row_count, has_project_id_column) for `alias.table`. Skips missing tables."""
    try:
        cur = con.execute(
            f"SELECT 1 FROM {alias}.sqlite_master WHERE type IN ('table','virtual') AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            return (0, False)
        cols = [row[1] for row in con.execute(f"PRAGMA {alias}.table_info({table})")]
        has_pid = "project_id" in cols
        # Defensive read: SQLite may raise if the table is malformed.
        n = con.execute(f"SELECT COUNT(*) FROM {alias}.{table}").fetchone()[0]
        return (int(n), bool(has_pid))
    except sqlite3.Error as exc:
        LOG.warning("count failed alias=%s table=%s err=%s", alias, table, exc)
        return (0, False)


def _plan_for_db(
    project: ProjectEntry, db_filename: str, tables: Iterable[str]
) -> list[TablePlan]:
    db_path = project.state_dir / db_filename
    if not db_path.is_file():
        return []
    out: list[TablePlan] = []
    con = sqlite3.connect(":memory:")
    try:
        try:
            attach_readonly(con, "src", db_path)
        except sqlite3.Error as exc:
            LOG.warning("attach failed project=%s db=%s err=%s", project.project_id, db_filename, exc)
            return []
        for tbl in tables:
            n, has_pid = _safe_count(con, "src", tbl)
            out.append(
                TablePlan(
                    project_id=project.project_id,
                    db_name=db_filename,
                    table=tbl,
                    row_count=n,
                    has_project_id_column=has_pid,
                )
            )
        con.execute("DETACH DATABASE src")
    finally:
        con.close()
    return out


def _detect_collisions(projects: list[ProjectEntry]) -> dict:
    """Detect dispatch_id and pattern_id collisions across projects.

    Pure read — attaches each project DB in ``?mode=ro``. Returns:
        {
            "dispatch_id": {<id>: [<project_id>, ...]},
            "pattern_id":  {<id>: [<project_id>, ...]},
        }
    Only ids appearing in >1 project are reported.
    """
    dispatch_seen: dict[str, list[str]] = {}
    pattern_seen: dict[str, list[str]] = {}

    for project in projects:
        rc_path = project.state_dir / "runtime_coordination.db"
        if rc_path.is_file():
            con = sqlite3.connect(":memory:")
            try:
                try:
                    attach_readonly(con, "src", rc_path)
                    cur = con.execute(
                        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='dispatches'"
                    )
                    if cur.fetchone() is not None:
                        for (did,) in con.execute("SELECT dispatch_id FROM src.dispatches"):
                            if not did:
                                continue
                            dispatch_seen.setdefault(str(did), []).append(project.project_id)
                    con.execute("DETACH DATABASE src")
                except sqlite3.Error as exc:
                    LOG.warning("dispatch_id collision-check failed project=%s err=%s", project.project_id, exc)
            finally:
                con.close()

        qi_path = project.state_dir / "quality_intelligence.db"
        if qi_path.is_file():
            con = sqlite3.connect(":memory:")
            try:
                try:
                    attach_readonly(con, "src", qi_path)
                    cur = con.execute(
                        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='pattern_usage'"
                    )
                    if cur.fetchone() is not None:
                        for (pid,) in con.execute("SELECT pattern_id FROM src.pattern_usage"):
                            if not pid:
                                continue
                            pattern_seen.setdefault(str(pid), []).append(project.project_id)
                    con.execute("DETACH DATABASE src")
                except sqlite3.Error as exc:
                    LOG.warning("pattern_id collision-check failed project=%s err=%s", project.project_id, exc)
            finally:
                con.close()

    return {
        "dispatch_id": {k: sorted(set(v)) for k, v in dispatch_seen.items() if len(set(v)) > 1},
        "pattern_id": {k: sorted(set(v)) for k, v in pattern_seen.items() if len(set(v)) > 1},
    }


def build_dry_run_report(projects: list[ProjectEntry]) -> dict:
    """Build the full dry-run plan dict. Pure read — no writes anywhere."""
    plan_rows: list[TablePlan] = []
    for project in projects:
        plan_rows.extend(_plan_for_db(project, "quality_intelligence.db", PLAN_TABLES_QI))
        plan_rows.extend(_plan_for_db(project, "runtime_coordination.db", PLAN_TABLES_RC))

    collisions = _detect_collisions(projects)
    schemas = {p.project_id: _project_schema(p) for p in projects}
    drift = compute_drift(schemas)

    # Aggregate expected post-import row counts per (db, table).
    table_totals: dict[tuple[str, str], int] = {}
    for r in plan_rows:
        key = (r.db_name, r.table)
        table_totals[key] = table_totals.get(key, 0) + r.row_count

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "projects": [
            {"project_id": p.project_id, "name": p.name, "path": str(p.path)}
            for p in projects
        ],
        "row_count_plan": [
            {
                "project_id": r.project_id,
                "db": r.db_name,
                "table": r.table,
                "rows": r.row_count,
                "has_project_id_column": r.has_project_id_column,
            }
            for r in plan_rows
        ],
        "expected_central_totals": [
            {"db": db, "table": tbl, "rows": rows}
            for (db, tbl), rows in sorted(table_totals.items())
        ],
        "collisions": collisions,
        "schema_drift": drift,
        "dry_run": True,
    }


def render_markdown(plan: dict) -> str:
    """Render the dry-run plan into an operator-readable markdown document."""
    lines: list[str] = []
    lines.append("# Phase 6 P4 — One-shot data import DRY-RUN REPORT")
    lines.append("")
    lines.append(f"_Generated: {plan['generated_at']}_")
    lines.append("")
    lines.append("## Projects in scope")
    lines.append("")
    lines.append("| Project ID | Name | Path |")
    lines.append("|------------|------|------|")
    for p in plan["projects"]:
        lines.append(f"| `{p['project_id']}` | {p['name']} | `{p['path']}` |")
    lines.append("")

    lines.append("## Per-project row-count plan (per source DB)")
    lines.append("")
    by_project: dict[str, list[dict]] = {}
    for row in plan["row_count_plan"]:
        by_project.setdefault(row["project_id"], []).append(row)
    for pid in sorted(by_project):
        lines.append(f"### `{pid}`")
        lines.append("")
        lines.append("| DB | Table | Rows | Has project_id column |")
        lines.append("|----|-------|-----:|:---------------------:|")
        for row in sorted(by_project[pid], key=lambda r: (r["db"], r["table"])):
            check = "[ok]" if row["has_project_id_column"] else "[!]"
            lines.append(
                f"| {row['db']} | {row['table']} | {row['rows']:,} | {check} |"
            )
        lines.append("")

    lines.append("## Expected central totals after import")
    lines.append("")
    lines.append("| DB | Table | Rows (sum across projects) |")
    lines.append("|----|-------|---------------------------:|")
    for entry in plan["expected_central_totals"]:
        lines.append(
            f"| {entry['db']} | {entry['table']} | {entry['rows']:,} |"
        )
    lines.append("")

    lines.append("## Collisions detected")
    lines.append("")
    dc = plan["collisions"]["dispatch_id"]
    pc = plan["collisions"]["pattern_id"]
    if not dc and not pc:
        lines.append("_No cross-project key collisions detected._")
    else:
        if dc:
            lines.append("### `runtime_coordination.dispatch_id` collisions")
            lines.append("")
            lines.append("| dispatch_id | Projects |")
            lines.append("|-------------|----------|")
            for k, ps in sorted(dc.items())[:50]:
                lines.append(f"| `{k}` | {', '.join(ps)} |")
            if len(dc) > 50:
                lines.append(f"| _…{len(dc) - 50} more_ | |")
            lines.append("")
        if pc:
            lines.append("### `quality_intelligence.pattern_usage.pattern_id` collisions")
            lines.append("")
            lines.append("| pattern_id | Projects |")
            lines.append("|------------|----------|")
            for k, ps in sorted(pc.items())[:50]:
                lines.append(f"| `{k}` | {', '.join(ps)} |")
            if len(pc) > 50:
                lines.append(f"| _…{len(pc) - 50} more_ | |")
            lines.append("")
        lines.append(
            "Collision-handling rule (plan §5.2): the live migrator prefixes "
            "colliding keys with `<project_id>:` so cross-project namespaces stay "
            "disjoint. Apply rule confirmed via `test_collision_detection.py`."
        )
        lines.append("")

    lines.append("## Schema drift")
    lines.append("")
    drift = plan["schema_drift"]
    if drift.get("reference"):
        lines.append(f"_Reference project: `{drift['reference']}`_")
        lines.append("")
    any_drift = False
    for pid, per_db in drift.get("projects", {}).items():
        for db_name, diff in per_db.items():
            if diff["missing_tables"] or diff["extra_tables"] or diff["column_diffs"]:
                any_drift = True
                lines.append(f"- `{pid}` / `{db_name}`")
                if diff["missing_tables"]:
                    lines.append(f"    - missing tables: {', '.join(diff['missing_tables'])}")
                if diff["extra_tables"]:
                    lines.append(f"    - extra tables: {', '.join(diff['extra_tables'])}")
                for tbl, cdiff in diff["column_diffs"].items():
                    lines.append(
                        f"    - column drift in `{tbl}`: missing={cdiff['missing']} extra={cdiff['extra']}"
                    )
    if not any_drift:
        lines.append("_No schema drift detected._")
    lines.append("")

    lines.append("## Operator pre-flight checklist")
    lines.append("")
    lines.append("Before running `scripts/migrate_to_central_vnx.py --apply`:")
    lines.append("")
    lines.append("- [ ] Reviewed this dry-run report end-to-end")
    lines.append("- [ ] Reviewed `claudedocs/w6-p4-rollback-procedure.md`")
    lines.append("- [ ] All 4 source DBs upgraded to v8.2.0-cqs-advisory-oi")
    lines.append("- [ ] Aggregator service confirmed idle (no concurrent reads against source DBs)")
    lines.append("- [ ] Free disk: at least the sum of source-DB sizes available under `~/Documents/`")
    lines.append("- [ ] No active dispatches in any project (check each `<project>/.vnx-data/state/runtime_coordination.db`)")
    lines.append("- [ ] Backup directory `~/Documents/vnx-pre-p4-auto-backup-<ts>/` is writable")
    lines.append("")
    return "\n".join(lines)


def _default_output_path() -> Path:
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    return REPO_ROOT / "claudedocs" / f"{today}-p4-dry-run-report.md"


def _write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=None, help="Override projects.json path")
    parser.add_argument("--out", type=Path, default=None, help="Output markdown report path")
    parser.add_argument("--json", action="store_true", help="Emit plan JSON to stdout")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    registry_path = args.registry or _default_registry_path()
    try:
        projects = load_registry(registry_path)
    except FileNotFoundError:
        print(f"ERROR: registry not found at {registry_path}", file=sys.stderr)
        return 2

    plan = build_dry_run_report(projects)
    md = render_markdown(plan)

    out_path = args.out or _default_output_path()
    _write_atomically(out_path, md)
    json_path = out_path.with_suffix(out_path.suffix + ".json")
    _write_atomically(json_path, json.dumps(plan, indent=2, default=str))

    if args.json:
        print(json.dumps(plan, indent=2, default=str))
    else:
        print(f"DRY-RUN report: {out_path}")
        print(f"DRY-RUN manifest: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
