#!/usr/bin/env python3
"""migration_inventory.py — read-only inventory lock over the VNX migration surface (PR-8).

Evidence artifact for the parked `migration-consolidation-and-tenancy-cut` track
(docs/core/framework-status-audit-and-cockpit_PRD.md §PR-8). This module DELETES
or DEPRECATES nothing — it only enumerates the six migration surfaces (SQL files,
Python appliers, the manifest reconciler, the schema-migration helpers, the ADR-007
tenancy stamper, and the CLI entrypoint), classifies every touched table as
central-DB (shared) vs per-project, and exposes `verify_complete()` as a read-only
fs<->git completeness oracle.

Table classification (ADR-007): every table below lives in one of the three
central VNX state DBs (quality_intelligence.db, runtime_coordination.db,
dispatch_tracker.db) that schemas/migrations/*.sql exclusively evolves — see
docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md, which binds
"every new central-DB table MUST be project_id-stamped at design time". Verified
against the tree 2026-07-12: every CREATE TABLE in this surface carries
`project_id TEXT NOT NULL DEFAULT 'vnx-dev'`. There is currently no per-project-only
table in this surface; a future migration that adds one and is not added to
CENTRAL_DB_TABLES fails verify_complete() loudly instead of silently reading
"unknown".
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from project_root import resolve_project_root
except ImportError:  # pragma: no cover - direct execution without scripts/lib on sys.path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from project_root import resolve_project_root


CENTRAL_DB_TABLES: Dict[str, bool] = {
    name: True
    for name in (
        "antipatterns", "central_install_events", "central_install_pins",
        "code_snippets", "confidence_events", "coordination_events",
        "cross_project_recommendations", "dispatch_attempts", "dispatch_metadata",
        "dispatch_pattern_offered", "dispatch_quality_context", "dispatches",
        "dream_cycles", "dream_pattern_archives", "escalation_log",
        "execution_targets", "global_patterns", "governance_metrics",
        "headless_runs", "improvement_suggestions", "inbound_inbox",
        "incident_log", "intelligence_injections", "nightly_digests",
        "pattern_usage", "pool_config", "prevention_rules", "quality_alerts",
        "quality_system_metrics", "quality_trends", "recommendation_outcomes",
        "recommendations", "report_findings", "retry_budgets", "retry_state",
        "runtime_schema_version", "scan_history", "schema_version",
        "session_analytics", "snippet_metadata", "sqlite_sequence",
        "success_patterns", "tag_combinations", "terminal_leases",
        "track_dependencies", "track_open_items", "track_phase_history",
        "tracks", "vnx_code_quality", "worker_pool_membership",
        "worker_pools", "worker_states",
    )
}

# Files whose only executable statements do not mechanically resolve to a table
# name via the statement parser below (a documentation-only migration whose SQL
# is fully commented out; a DROP-INDEX-only down-migration whose index names
# encode the table by naming convention, not a parseable `ON <table>` clause).
# Hand-verified against the file text 2026-07-12.
_MANUAL_TABLE_OVERRIDES: Dict[str, Tuple[str, ...]] = {
    "0016_rebuild_fts5.sql": ("code_snippets",),
    "2026_05_task_subclass_down.sql": ("success_patterns", "antipatterns"),
}

_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
_STMT_TABLE_RE = re.compile(
    r"^\s*(?:"
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<t1>[A-Za-z_][A-Za-z0-9_]*)"
    r"|ALTER\s+TABLE\s+(?P<t2>[A-Za-z_][A-Za-z0-9_]*)"
    r"|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<t3>[A-Za-z_][A-Za-z0-9_]*)"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_]*\s+ON\s+(?P<t4>[A-Za-z_][A-Za-z0-9_]*)"
    r"|UPDATE\s+(?P<t5>[A-Za-z_][A-Za-z0-9_]*)"
    r"|DELETE\s+FROM\s+(?P<t6>[A-Za-z_][A-Za-z0-9_]*)"
    r"|INSERT\s+(?:OR\s+(?:IGNORE|REPLACE)\s+)?INTO\s+(?P<t7>[A-Za-z_][A-Za-z0-9_]*)"
    r")",
    re.IGNORECASE,
)
# Collapses migration-internal rebuild artifacts (dispatches_v10, tracks_pre_v24,
# dispatch_attempts_new, tracks_pre_0027_down, ...) to the logical table name.
_SUFFIX_RE = re.compile(r"_(?:v\d+|new|pre_v\d+(?:_down)?|pre_\d+_down)$")


def _canonical_table(raw: str) -> str:
    name = raw
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX_RE.sub("", name)
    return name


def tables_touched_in_sql(text: str, filename: str = "") -> Tuple[str, ...]:
    """Canonical table names one migration file touches.

    Parses statement-by-statement (line comments stripped, split on ';') so a
    free-text comment mentioning "CREATE TABLE" never produces a false-positive
    table name — only text at the START of an executable statement matches.
    """
    stripped = _COMMENT_RE.sub("", text)
    found: List[str] = []
    for stmt in stripped.split(";"):
        m = _STMT_TABLE_RE.match(stmt.strip())
        if m:
            raw = next(v for v in m.groupdict().values() if v)
            found.append(_canonical_table(raw))
    if not found and filename in _MANUAL_TABLE_OVERRIDES:
        found = list(_MANUAL_TABLE_OVERRIDES[filename])
    seen: set = set()
    ordered: List[str] = []
    for t in found:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return tuple(ordered)


@dataclass(frozen=True)
class TableTouch:
    table: str
    central_db: Optional[bool]  # None == unclassified ("unknown")


@dataclass(frozen=True)
class MigrationSurface:
    surface_id: int
    name: str
    paths: Tuple[str, ...]
    file_count: int
    tables_touched: Tuple[TableTouch, ...]
    # Surface 1 only: per-file table breakdown, for the completeness oracle.
    per_file_tables: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()


def _sql_files(root: Path) -> List[Path]:
    return sorted((root / "schemas" / "migrations").glob("*.sql"))


def _applier_files(root: Path) -> List[Path]:
    return sorted((root / "scripts" / "lib" / "migrations").glob("apply_*.py"))


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def build_inventory(root: Optional[Path] = None) -> Tuple[MigrationSurface, ...]:
    """Enumerate all six migration surfaces, read-only, rooted at *root*.

    *root* defaults to the repo root (git-resolved) but is a real parameter so
    callers — including the completeness oracle's negative test — can point it
    at an isolated temp directory instead of the real repo.
    """
    root = (root or resolve_project_root(__file__)).resolve()

    sql_files = _sql_files(root)
    per_file_tables: List[Tuple[str, Tuple[str, ...]]] = []
    distinct_tables: Dict[str, Optional[bool]] = {}
    for f in sql_files:
        touched = tables_touched_in_sql(f.read_text(encoding="utf-8"), f.name)
        per_file_tables.append((f.name, touched))
        for t in touched:
            distinct_tables[t] = CENTRAL_DB_TABLES.get(t)

    applier_files = _applier_files(root)

    return (
        MigrationSurface(
            surface_id=1,
            name="sql-migrations",
            paths=tuple(_rel(root, p) for p in sql_files),
            file_count=len(sql_files),
            tables_touched=tuple(
                TableTouch(t, c) for t, c in sorted(distinct_tables.items())
            ),
            per_file_tables=tuple(per_file_tables),
        ),
        MigrationSurface(
            surface_id=2,
            name="python-appliers",
            paths=tuple(_rel(root, p) for p in applier_files),
            file_count=len(applier_files),
            tables_touched=(),
        ),
        MigrationSurface(
            surface_id=3,
            name="schema-manifest",
            paths=(_rel(root, root / "scripts" / "lib" / "schema_manifest.py"),),
            file_count=1,
            tables_touched=(),
        ),
        MigrationSurface(
            surface_id=4,
            name="schema-migration-helpers",
            paths=(
                _rel(root, root / "scripts" / "lib" / "schema_migration.py"),
                _rel(root, root / "scripts" / "lib" / "migrations" / "auto_apply.py"),
            ),
            file_count=2,
            tables_touched=(),
        ),
        MigrationSurface(
            surface_id=5,
            name="project-id-migration",
            paths=(_rel(root, root / "scripts" / "lib" / "project_id_migration.py"),),
            file_count=1,
            tables_touched=(),
        ),
        MigrationSurface(
            surface_id=6,
            name="migrate-cli",
            paths=(_rel(root, root / "vnx_cli" / "commands" / "migrate.py"),),
            file_count=1,
            tables_touched=(),
        ),
    )


@dataclass(frozen=True)
class CompletenessResult:
    ok: bool
    violations: Tuple[str, ...]


def verify_complete(root: Optional[Path] = None) -> CompletenessResult:
    """Read-only completeness oracle over ONE shared migrations root.

    1. The fs-glob enumeration of ``<root>/schemas/migrations/*.sql`` must equal
       ``git ls-files`` for the same glob, over the SAME root: a file on disk but
       untracked, or tracked but missing on disk, is itself a FAIL.
    2. Every ``scripts/lib/migrations/apply_*.py`` on disk must be represented in
       the applier surface (surface 2).
    3. Every SQL file enumerated in surface 1 must resolve to >=1 touched table,
       and every touched table must carry a non-unknown ``central_db`` value.
    """
    root = (root or resolve_project_root(__file__)).resolve()
    violations: List[str] = []

    migrations_dir = root / "schemas" / "migrations"
    fs_sql = {p.name for p in migrations_dir.glob("*.sql")} if migrations_dir.is_dir() else set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "schemas/migrations/*.sql"],
            capture_output=True,
            text=True,
            check=True,
        )
        git_sql = {Path(line).name for line in proc.stdout.splitlines() if line.strip()}
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        violations.append(f"git ls-files failed: {exc}")
        git_sql = set()

    only_on_disk = fs_sql - git_sql
    only_in_git = git_sql - fs_sql
    if only_on_disk:
        violations.append(f"untracked on disk (not in git): {sorted(only_on_disk)}")
    if only_in_git:
        violations.append(f"tracked in git but missing on disk: {sorted(only_in_git)}")

    surfaces = build_inventory(root)
    sql_surface, applier_surface = surfaces[0], surfaces[1]

    applier_names_on_disk = {p.name for p in _applier_files(root)}
    applier_names_in_surface = {Path(p).name for p in applier_surface.paths}
    missing_appliers = applier_names_on_disk - applier_names_in_surface
    if missing_appliers:
        violations.append(f"applier(s) on disk but not in surface 2: {sorted(missing_appliers)}")

    classification = {tt.table: tt.central_db for tt in sql_surface.tables_touched}
    for fname, tables in sql_surface.per_file_tables:
        if not tables:
            violations.append(f"{fname}: resolved to 0 touched tables")
            continue
        for t in tables:
            if classification.get(t) is None:
                violations.append(f"{fname}: table {t!r} has unknown central_db classification")

    return CompletenessResult(ok=not violations, violations=tuple(violations))


if __name__ == "__main__":  # pragma: no cover - manual inspection entrypoint
    for surface in build_inventory():
        print(f"[{surface.surface_id}] {surface.name}: {surface.file_count} file(s)")
        for p in surface.paths[:3]:
            print(f"    {p}")
        if surface.file_count > 3:
            print(f"    ... +{surface.file_count - 3} more")
    result = verify_complete()
    print(f"verify_complete: ok={result.ok}")
    for v in result.violations:
        print(f"  - {v}")
