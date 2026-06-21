#!/usr/bin/env python3
"""migration_linter.py — CI guard: reject new DEFAULT 'vnx-dev' in schemas and DDL.

WHAT IT DOES
------------
Scans two source trees for the literal string  DEFAULT 'vnx-dev'  (case-insensitive):

  1. schemas/migrations/*.sql
  2. Python files under scripts/ (multi-line string DDL, e.g. quality_db_init.py).

Any match in a file NOT on the allowlist below is a CI failure (non-zero exit).

WHY
---
Phase 3 of the W1 tenant-stamping fix (ADR-007) drops all existing
``DEFAULT 'vnx-dev'`` clauses so that a future unqualified INSERT into a
project-scoped table fails loudly rather than contaminating the wrong tenant.

The linter enforces the invariant going forward: new migrations and DDL helpers
MUST NOT re-introduce ``DEFAULT 'vnx-dev'``.  Callers must stamp project_id
explicitly at insert time (the runtime app layer always does; migration runners
must now use ``resolve_init_project_id()`` from ``project_id_migration.py``).

ALLOWLIST APPROACH
------------------
We maintain a static allowlist of files that already contain the pattern at the
time this linter was introduced.  These are already-applied migrations and
historically present Python DDL helpers — changing them is a separate concern
tracked by the W1 3-phase runner (tenant_stamping.py).  The linter only blocks
NEW occurrences, i.e. files not on the allowlist.

The allowlist is expressed as a set of paths relative to the project root.
Keeping it relative makes the linter portable across developer machines.

USAGE
-----
From project root:

    python3 scripts/lib/migration_linter.py
    python3 scripts/lib/migration_linter.py --strict   # fail on any occurrence, ignoring allowlist

Exit codes:
    0 — no new violations found
    1 — one or more new violations found (or --strict: any occurrence)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern: case-insensitive literal  DEFAULT 'vnx-dev'
# Matches SQL column definitions and Python f-string / multi-line DDL strings.
# ---------------------------------------------------------------------------
_PATTERN = re.compile(r"DEFAULT\s+'vnx-dev'", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Allowlist: files that had DEFAULT 'vnx-dev' when this linter was introduced.
#
# These are already-applied migrations and Python DDL helpers that existed
# before the W1 Phase-3 cleanup.  Adding a file here means "we know about
# this occurrence and will handle it separately via the 3-phase migration
# runner".  Do NOT add new files to this list — fix the new code instead.
#
# Spec reference: W1-TENANT-STAMPING-FIX-SPEC.md §Phase 3 + Kimi finding.
# Grandfathered at: 2026-06-21 (W-init delivery).
# ---------------------------------------------------------------------------
ALLOWLISTED_FILES: frozenset[str] = frozenset({
    # SQL migrations — already applied to production DBs; W1 Phase 3 drops
    # the DEFAULT in the live schema via copy-and-rename rebuild.
    "schemas/migrations/0010_add_project_id.sql",
    "schemas/migrations/0015_complete_project_id.sql",
    "schemas/migrations/0017_multi_tenant_lease_isolation.sql",
    "schemas/migrations/0017_multi_tenant_lease_isolation_down.sql",
    "schemas/migrations/0022_track_layer.sql",
    "schemas/migrations/0024_tracks_tenant_scoping.sql",
    "schemas/migrations/0025_dream_consolidation.sql",
    "schemas/migrations/0027_planning_horizon_and_deliverable_view_down.sql",
    "schemas/migrations/0028_tracks_derived_status_down.sql",
    "schemas/migrations/0031_runtime_tenant_fk_repair.sql",
    # Python DDL helpers — W-core owns these; tenant_stamping.py Phase 3
    # removes the DEFAULT from live DBs.  The source strings here are
    # grandfathered until W-core ships the cleanup.
    "scripts/quality_db_init.py",
    "scripts/migrate_future_system.py",
    "scripts/migrate_to_central_vnx.py",
    "scripts/vnx_structural_doctor.py",
    "scripts/lib/migrations/apply_0017.py",
    "scripts/lib/migrations/apply_0026.py",
    # tenant_stamping.py references the pattern only in comments / docstrings
    # (not in live DDL strings), but we grandfather it to be safe.
    "scripts/lib/tenant_stamping.py",
    # The linter itself documents the pattern in docstrings and error messages;
    # it must be on its own allowlist to avoid a self-referential false positive.
    "scripts/lib/migration_linter.py",
    # project_id_migration.py docstring mentions the pattern for documentation.
    "scripts/lib/project_id_migration.py",
})


def _find_project_root(start: Path) -> Path:
    """Walk up from ``start`` looking for pyproject.toml or .git."""
    current = start.resolve()
    for ancestor in [current, *current.parents]:
        if (ancestor / "pyproject.toml").is_file() or (ancestor / ".git").is_dir():
            return ancestor
    return current


def scan(
    project_root: Path,
    *,
    strict: bool = False,
    allowlist: frozenset[str] = ALLOWLISTED_FILES,
) -> list[dict]:
    """Scan migration SQL files and Python scripts for new DEFAULT 'vnx-dev'.

    Returns a list of violation dicts:
        {"file": rel_path_str, "line": int, "text": str}

    In strict mode, every occurrence is a violation (ignores allowlist).
    In normal mode, only files NOT on the allowlist are violations.
    """
    violations: list[dict] = []

    # Scan 1: SQL migrations
    migrations_dir = project_root / "schemas" / "migrations"
    if migrations_dir.is_dir():
        for sql_file in sorted(migrations_dir.glob("*.sql")):
            rel = sql_file.relative_to(project_root).as_posix()
            if not strict and rel in allowlist:
                continue
            _scan_file(sql_file, rel, violations)

    # Scan 2: Python files under scripts/
    scripts_dir = project_root / "scripts"
    if scripts_dir.is_dir():
        for py_file in sorted(scripts_dir.rglob("*.py")):
            rel = py_file.relative_to(project_root).as_posix()
            if not strict and rel in allowlist:
                continue
            _scan_file(py_file, rel, violations)

    return violations


def _scan_file(path: Path, rel: str, violations: list[dict]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _PATTERN.search(line):
            violations.append({"file": rel, "line": lineno, "text": line.rstrip()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CI linter: fail if any new DEFAULT 'vnx-dev' appears in migrations or Python DDL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on ANY occurrence, including allowlisted files.",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root directory. Defaults to auto-detected root (pyproject.toml / .git).",
    )
    args = parser.parse_args(argv)

    root = Path(args.project_root) if args.project_root else _find_project_root(Path(__file__))
    violations = scan(root, strict=args.strict)

    if violations:
        print(
            f"[migration-linter] FAIL — {len(violations)} new DEFAULT 'vnx-dev' occurrence(s) found.",
            file=sys.stderr,
        )
        print(
            "[migration-linter] New migrations and DDL helpers MUST NOT use DEFAULT 'vnx-dev'.",
            file=sys.stderr,
        )
        print(
            "[migration-linter] Use resolve_init_project_id() from project_id_migration.py instead.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v['file']}:{v['line']}: {v['text']}", file=sys.stderr)
        return 1

    mode_label = "strict" if args.strict else "normal (allowlist active)"
    print(f"[migration-linter] OK — no new DEFAULT 'vnx-dev' occurrences found ({mode_label}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
