"""Advisory pre-migration integrity check for a central store.

Runs ``PRAGMA foreign_key_check`` + ``PRAGMA integrity_check`` on a store's
``runtime_coordination.db`` BEFORE a migration mutates it, so dangling FK edges
(the mission-control 22-dangling ``track_dependencies`` class, 2026-07-10) surface as a
clear report instead of a cryptic mid-migration constraint failure.

**Advisory by default** — reports violations and continues; the migration runner already
prunes/repairs many of them. Set ``VNX_MIGRATE_STRICT_FK=1`` to abort a store's migration
before it mutates (the fleet sweep then skips that one store and continues). Read-only:
opens the DB in ``mode=ro`` and never writes.

This is the low-risk half of Tier F migration hardening (central-store review 2026-07-10,
task #30). The W1 tenant-stamp re-enable is the operator-gated half and lives elsewhere.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional


class StoreIntegrityError(RuntimeError):
    """Raised by ``preflight_or_report`` only when VNX_MIGRATE_STRICT_FK=1 and the store
    has FK/integrity violations."""


class IntegrityReport(NamedTuple):
    db_path: Path
    fk_violations: List[tuple]      # rows from PRAGMA foreign_key_check
    integrity_errors: List[str]     # PRAGMA integrity_check rows other than "ok"
    ok: bool


def check_store_integrity(db_path: "str | Path") -> IntegrityReport:
    """Read-only FK + integrity check. A missing/unreadable DB reports ``ok`` (nothing to
    check) rather than raising — the migration itself handles a truly broken file."""
    path = Path(db_path)
    if not path.is_file():
        return IntegrityReport(path, [], [], True)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return IntegrityReport(path, [], [], True)
    try:
        fk = list(conn.execute("PRAGMA foreign_key_check").fetchall())
        integ = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.Error:
        return IntegrityReport(path, [], [], True)
    finally:
        conn.close()
    integ_errors = [r for r in integ if r != "ok"]
    return IntegrityReport(path, fk, integ_errors, not fk and not integ_errors)


def strict_fk_enabled(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return env.get("VNX_MIGRATE_STRICT_FK") == "1"


def preflight_or_report(
    db_path: "str | Path",
    *,
    label: str = "",
    out=None,
) -> IntegrityReport:
    """Run the advisory pre-flight. Prints a report on violations. Raises
    ``StoreIntegrityError`` ONLY when ``VNX_MIGRATE_STRICT_FK=1``; otherwise advisory."""
    stream = out or sys.stderr
    report = check_store_integrity(db_path)
    if report.ok:
        return report

    tag = f"[integrity]{(' ' + label) if label else ''}"
    if report.fk_violations:
        print(
            f"  {tag} {len(report.fk_violations)} dangling FK row(s) in {report.db_path.name}:",
            file=stream,
        )
        for row in report.fk_violations[:20]:
            tbl = row[0] if len(row) > 0 else "?"
            rowid = row[1] if len(row) > 1 else "?"
            parent = row[2] if len(row) > 2 else "?"
            fkid = row[3] if len(row) > 3 else "?"
            print(f"    {tbl} rowid={rowid} -> missing parent {parent} (fk#{fkid})", file=stream)
        if len(report.fk_violations) > 20:
            print(f"    … +{len(report.fk_violations) - 20} more", file=stream)
    for err in report.integrity_errors[:10]:
        print(f"  {tag} integrity: {err}", file=stream)

    if strict_fk_enabled():
        raise StoreIntegrityError(
            f"{report.db_path}: {len(report.fk_violations)} FK + "
            f"{len(report.integrity_errors)} integrity violation(s) "
            "(VNX_MIGRATE_STRICT_FK=1 — aborting this store before it mutates)"
        )
    print(
        f"  {tag} advisory only — continuing; set VNX_MIGRATE_STRICT_FK=1 to abort a "
        "violating store before it mutates.",
        file=stream,
    )
    return report


__all__ = [
    "IntegrityReport",
    "StoreIntegrityError",
    "check_store_integrity",
    "preflight_or_report",
    "strict_fk_enabled",
]
