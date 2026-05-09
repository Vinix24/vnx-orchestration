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

Round-2 fixes (2026-05-07):
    - Read failures (corrupt source DB, malformed table) no longer silently
      degrade to zero rows. They accumulate into ``plan["read_errors"]``
      and force exit code 3. (BLOCKING 4.)
    - Collision detection now inspects every cross-tenant identifier
      carrier flagged by ``migrate_to_central_vnx._is_collision_column``
      and the JSON-array columns enumerated there, plus
      ``coordination_events.entity_id`` when the entity_type is dispatch
      or pattern. (BLOCKING 2.)
"""

from __future__ import annotations

import argparse
import contextlib
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
from scripts.migrate_to_central_vnx import (  # noqa: E402
    COLLISION_ENTITY_ID_COLUMN,
    COLLISION_ENTITY_TABLE,
    COLLISION_ENTITY_TYPE_COLUMN,
    COLLISION_ENTITY_TYPES_PREFIXED,
    COLLISION_JSON_ARRAY_COLUMNS,
    _is_collision_column,
)

LOG = logging.getLogger("vnx.migrate.dryrun")

# Tables planned for import (subset of the central DBs; matches Phase 0+P4 scope).
# Mirror IMPORT_TABLES_QI in scripts/migrate_to_central_vnx.py — keep in lockstep.
PLAN_TABLES_QI: tuple[str, ...] = (
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "pattern_usage",
    "confidence_events",
    "dispatch_metadata",
    "dispatch_experiments",
    "dispatch_pattern_offered",
    "session_analytics",
    "vnx_code_quality",
    "snippet_metadata",
    "code_snippets",
    "quality_trends",
    "quality_alerts",
    "dispatch_quality_context",
    "quality_system_metrics",
    "scan_history",
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


class _ReadError(RuntimeError):
    """Raised by _safe_count when a present table is unreadable.

    Round-2 (Finding 4): we used to swallow ``sqlite3.Error`` and report
    zero rows, which let a corrupted source DB pass dry-run silently.
    Errors now propagate up to ``_plan_for_db`` and accumulate into
    ``plan["read_errors"]`` for operator review.
    """


def _safe_count(con: sqlite3.Connection, alias: str, table: str) -> tuple[int, bool, bool]:
    """Return ``(row_count, has_project_id_column, present)``.

    ``present=False`` means the table simply doesn't exist in this source
    (acceptable schema drift — for example, the source predates a table
    added later). ``present=True`` with a SQLite error during count means
    the table exists but is unreadable; that case raises ``_ReadError``
    instead of silently returning zero rows. (Finding 4 round 2.)
    """
    cur = con.execute(
        f"SELECT 1 FROM {alias}.sqlite_master WHERE type IN ('table','virtual') AND name=?",
        (table,),
    )
    if cur.fetchone() is None:
        return (0, False, False)
    try:
        cols = [row[1] for row in con.execute(f"PRAGMA {alias}.table_info({table})")]
        has_pid = "project_id" in cols
        n = con.execute(f"SELECT COUNT(*) FROM {alias}.{table}").fetchone()[0]
    except sqlite3.Error as exc:
        raise _ReadError(f"alias={alias} table={table} err={exc}") from exc
    return (int(n), bool(has_pid), True)


def _plan_for_db(
    project: ProjectEntry,
    db_filename: str,
    tables: Iterable[str],
    read_errors: list[dict[str, object]],
) -> list[TablePlan]:
    """Build per-table row-count plans. Records read errors instead of swallowing.

    Round-2 fix (Finding 4): an attach failure (corrupt SQLite header,
    truncated file) and a per-table read error both append a structured
    record to ``read_errors`` so the caller can fail the dry-run with
    exit code 3 instead of reporting a clean preflight.
    """
    db_path = project.state_dir / db_filename
    if not db_path.is_file():
        return []
    out: list[TablePlan] = []
    con = sqlite3.connect(":memory:")
    try:
        try:
            attach_readonly(con, "src", db_path)
        except sqlite3.Error as exc:
            read_errors.append(
                {
                    "project_id": project.project_id,
                    "db": db_filename,
                    "phase": "attach",
                    "path": str(db_path),
                    "error": str(exc),
                }
            )
            return []
        for tbl in tables:
            try:
                n, has_pid, present = _safe_count(con, "src", tbl)
            except _ReadError as exc:
                read_errors.append(
                    {
                        "project_id": project.project_id,
                        "db": db_filename,
                        "phase": "count",
                        "table": tbl,
                        "error": str(exc),
                    }
                )
                continue
            if not present:
                # Schema drift: table doesn't exist in this source. Acceptable.
                continue
            out.append(
                TablePlan(
                    project_id=project.project_id,
                    db_name=db_filename,
                    table=tbl,
                    row_count=n,
                    has_project_id_column=has_pid,
                )
            )
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("DETACH DATABASE src")
    finally:
        con.close()
    return out


def _list_user_tables(con: sqlite3.Connection, alias: str) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            f"SELECT name FROM {alias}.sqlite_master "
            f"WHERE type IN ('table','virtual') AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _table_columns_dry(con: sqlite3.Connection, alias: str, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA {alias}.table_info({table})")]


def _classify_dispatch_or_pattern(column: str) -> str:
    """Bucket a collision-eligible column into ``dispatch_id`` / ``pattern_id``.

    Used only by the dry-run reporter for grouping output. Free-form
    ``parent_dispatch`` is treated as a dispatch identifier; ``entity_id``
    falls under ``dispatch_id`` because the entity_type filter resolves
    pattern entities into the same bucket logically.
    """
    if "pattern" in column:
        return "pattern_id"
    return "dispatch_id"


def _scan_collisions_in_db(
    con: sqlite3.Connection,
    alias: str,
    project_id: str,
    dispatch_seen: dict[str, list[str]],
    pattern_seen: dict[str, list[str]],
    read_errors: list[dict[str, object]],
    db_label: str,
) -> None:
    """Walk every user table once and harvest cross-project identifiers.

    Round-2 (Finding 2): the previous implementation only looked at
    ``dispatches.dispatch_id`` and ``pattern_usage.pattern_id``. That left
    ``related_dispatch_id``, ``parent_dispatch``, ``source_dispatch_ids``
    (JSON), and ``coordination_events.entity_id`` blind to collisions.
    The schema-driven walk below mirrors the live migrator's prefixing
    rules so dry-run and apply detect the same collisions.
    """
    try:
        tables = _list_user_tables(con, alias)
    except sqlite3.Error as exc:
        read_errors.append(
            {
                "project_id": project_id,
                "db": db_label,
                "phase": "list_tables",
                "error": str(exc),
            }
        )
        return

    for table in tables:
        try:
            columns = _table_columns_dry(con, alias, table)
        except sqlite3.Error as exc:
            read_errors.append(
                {
                    "project_id": project_id,
                    "db": db_label,
                    "phase": "table_info",
                    "table": table,
                    "error": str(exc),
                }
            )
            continue

        scalar_cols = [c for c in columns if _is_collision_column(c)]
        json_cols = [c for c in columns if c in COLLISION_JSON_ARRAY_COLUMNS]
        is_entity_table = (
            table == COLLISION_ENTITY_TABLE
            and COLLISION_ENTITY_ID_COLUMN in columns
            and COLLISION_ENTITY_TYPE_COLUMN in columns
        )

        select_targets: list[str] = list(scalar_cols)
        select_targets.extend(c for c in json_cols if c not in select_targets)
        if is_entity_table:
            for c in (COLLISION_ENTITY_TYPE_COLUMN, COLLISION_ENTITY_ID_COLUMN):
                if c not in select_targets:
                    select_targets.append(c)
        if not select_targets:
            continue

        select_sql = ", ".join(f'"{c}"' for c in select_targets)
        try:
            rows = list(
                con.execute(f"SELECT {select_sql} FROM {alias}.{table}")
            )
        except sqlite3.Error as exc:
            read_errors.append(
                {
                    "project_id": project_id,
                    "db": db_label,
                    "phase": "select",
                    "table": table,
                    "error": str(exc),
                }
            )
            continue

        target_index = {name: idx for idx, name in enumerate(select_targets)}
        for row in rows:
            for column in scalar_cols:
                value = row[target_index[column]]
                if value is None or value == "":
                    continue
                bucket = (
                    pattern_seen
                    if _classify_dispatch_or_pattern(column) == "pattern_id"
                    else dispatch_seen
                )
                bucket.setdefault(str(value), []).append(project_id)

            for column in json_cols:
                value = row[target_index[column]]
                if not value:
                    continue
                try:
                    parsed = json.loads(value) if isinstance(value, str) else value
                except (TypeError, ValueError):
                    continue
                if not isinstance(parsed, list):
                    continue
                bucket = (
                    pattern_seen
                    if _classify_dispatch_or_pattern(column) == "pattern_id"
                    else dispatch_seen
                )
                for item in parsed:
                    if item is None or item == "":
                        continue
                    bucket.setdefault(str(item), []).append(project_id)

            if is_entity_table:
                etype = row[target_index[COLLISION_ENTITY_TYPE_COLUMN]]
                eid = row[target_index[COLLISION_ENTITY_ID_COLUMN]]
                if (etype or "") and eid:
                    etype_l = str(etype).lower()
                    if etype_l in COLLISION_ENTITY_TYPES_PREFIXED:
                        bucket = (
                            pattern_seen if etype_l == "pattern" else dispatch_seen
                        )
                        bucket.setdefault(str(eid), []).append(project_id)


def _detect_collisions(
    projects: list[ProjectEntry],
    read_errors: list[dict[str, object]] | None = None,
) -> dict:
    """Detect cross-project dispatch_id / pattern_id collisions.

    Round-2 fix (Finding 2): scans every column flagged by
    :func:`migrate_to_central_vnx._is_collision_column`, every JSON array
    column in :data:`migrate_to_central_vnx.COLLISION_JSON_ARRAY_COLUMNS`,
    and the ``coordination_events.entity_id`` field (filtered by
    ``entity_type``). The previous implementation only inspected
    ``dispatches.dispatch_id`` / ``pattern_usage.pattern_id``, leaving
    every cross-tenant FK reference invisible.

    ``read_errors`` (optional): if provided, attach failures and
    per-table read failures append to this list so the caller (dry-run
    reporter) can promote them to fatal exit-3 errors. (Finding 4.)
    """
    dispatch_seen: dict[str, list[str]] = {}
    pattern_seen: dict[str, list[str]] = {}
    errors_target: list[dict[str, object]] = (
        read_errors if read_errors is not None else []
    )

    for project in projects:
        for db_filename in ("runtime_coordination.db", "quality_intelligence.db"):
            db_path = project.state_dir / db_filename
            if not db_path.is_file():
                continue
            con = sqlite3.connect(":memory:")
            try:
                try:
                    attach_readonly(con, "src", db_path)
                except sqlite3.Error as exc:
                    errors_target.append(
                        {
                            "project_id": project.project_id,
                            "db": db_filename,
                            "phase": "attach",
                            "path": str(db_path),
                            "error": str(exc),
                        }
                    )
                    continue
                _scan_collisions_in_db(
                    con,
                    "src",
                    project.project_id,
                    dispatch_seen,
                    pattern_seen,
                    errors_target,
                    db_filename,
                )
                with contextlib.suppress(sqlite3.OperationalError):
                    con.execute("DETACH DATABASE src")
            finally:
                con.close()

    return {
        "dispatch_id": {k: sorted(set(v)) for k, v in dispatch_seen.items() if len(set(v)) > 1},
        "pattern_id": {k: sorted(set(v)) for k, v in pattern_seen.items() if len(set(v)) > 1},
    }


def build_dry_run_report(projects: list[ProjectEntry]) -> dict:
    """Build the full dry-run plan dict. Pure read — no writes anywhere.

    Round-2 (Finding 4): the returned dict now includes a ``read_errors``
    list. The CLI promotes any non-empty list to exit code 3 so the
    operator never sees a clean preflight that masked an unreadable
    source.
    """
    read_errors: list[dict[str, object]] = []
    plan_rows: list[TablePlan] = []
    for project in projects:
        plan_rows.extend(
            _plan_for_db(project, "quality_intelligence.db", PLAN_TABLES_QI, read_errors)
        )
        plan_rows.extend(
            _plan_for_db(project, "runtime_coordination.db", PLAN_TABLES_RC, read_errors)
        )

    collisions = _detect_collisions(projects, read_errors=read_errors)
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
        "read_errors": read_errors,
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

    lines.append("## Read errors (fatal if non-empty)")
    lines.append("")
    read_errors = plan.get("read_errors") or []
    if not read_errors:
        lines.append("_No source DBs or tables produced read errors._")
    else:
        lines.append(
            "**Preflight FAILED.** The following source reads errored — exit code 3 returned. "
            "Operator must repair the source(s) before re-running the dry-run:"
        )
        lines.append("")
        lines.append("| Project | DB | Phase | Table | Error |")
        lines.append("|---------|----|-------|-------|-------|")
        for err in read_errors:
            lines.append(
                f"| `{err.get('project_id','?')}` | {err.get('db','?')} | "
                f"{err.get('phase','?')} | {err.get('table','-')} | "
                f"`{str(err.get('error','?')).replace('|', ' ')}` |"
            )
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

    # Round-2 fix (Finding 4): a non-empty read_errors list means at
    # least one source DB/table was unreadable. Surface as exit code 3
    # so operators cannot mistake a corrupted-source dry-run for a clean
    # preflight.
    if plan.get("read_errors"):
        print(
            f"DRY-RUN FAILED: {len(plan['read_errors'])} source read error(s); "
            f"see report for details",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
