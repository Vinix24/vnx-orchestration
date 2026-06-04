"""verify.py for task 02 — tenant-scoped schema migration.

Steps:
1. Rebuild fresh seed DB via init_seed_db.sql
2. Apply the worker's migrate.sql against the fresh DB
3. Run 5 verification queries against the migrated schema
4. Re-apply migrate.sql once more to test idempotency
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t1_trivial/02_rls_policy/seed"


def _apply_sql_script(db_path: Path, sql_path: Path) -> tuple[bool, str]:
    """Run sql_path against db_path via sqlite3 CLI. Returns (ok, output)."""
    try:
        proc = subprocess.run(
            ["sqlite3", str(db_path)],
            input=sql_path.read_text(encoding="utf-8"),
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            return False, f"sqlite3 rc={proc.returncode}: {proc.stderr[-500:]}"
        return True, proc.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"sqlite3 invocation failed: {exc}"


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    migrate_sql = seed_dir / "migrate.sql"
    init_sql = seed_dir / "init_seed_db.sql"

    if not migrate_sql.exists():
        return {
            "pass": False,
            "evidence": "migrate.sql not written by worker",
            "details": {"pass_count": 0, "expected": 5, "files_written": []},
        }
    if not init_sql.exists():
        return {
            "pass": False,
            "evidence": "seed init script missing (test bug)",
            "details": {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]},
        }

    files_written = ["migrate.sql"]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "scan_quota.db"

        ok, msg = _apply_sql_script(db_path, init_sql)
        if not ok:
            return {
                "pass": False,
                "evidence": f"seed init failed: {msg}",
                "details": {"pass_count": 0, "expected": 5, "files_written": files_written},
            }

        ok, msg = _apply_sql_script(db_path, migrate_sql)
        if not ok:
            return {
                "pass": False,
                "evidence": f"migration failed: {msg}",
                "details": {"pass_count": 0, "expected": 5, "files_written": files_written},
            }

        checks: list[tuple[str, bool, str]] = []
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()

            cur.execute(
                "SELECT COUNT(*) FROM scan_quota WHERE project_id = 'default'"
            )
            try:
                default_rows = cur.fetchone()[0]
                checks.append(("3 original rows scoped to 'default' tenant",
                              default_rows == 3,
                              f"found {default_rows}, expected 3"))
            except sqlite3.OperationalError as exc:
                checks.append(("project_id column exists", False, str(exc)))

            try:
                cur.execute(
                    "INSERT INTO scan_quota (scan_id, used_count, quota_limit, project_id) "
                    "VALUES ('scan_a', 0, 100, 'tenant_x')"
                )
                conn.commit()
                checks.append(("cross-tenant insert allowed", True, "inserted scan_a/tenant_x"))
            except sqlite3.IntegrityError as exc:
                checks.append(("cross-tenant insert allowed", False, str(exc)))

            try:
                cur.execute(
                    "INSERT INTO scan_quota (scan_id, used_count, quota_limit, project_id) "
                    "VALUES ('scan_a', 0, 100, 'default')"
                )
                conn.commit()
                checks.append(("same-tenant duplicate rejected", False,
                              "duplicate insert was allowed"))
            except sqlite3.IntegrityError:
                checks.append(("same-tenant duplicate rejected", True,
                              "UNIQUE constraint fired as expected"))

            cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='scan_quota'"
            )
            index_defs = [row[0] or "" for row in cur.fetchall()]
            composite_unique = any(
                "project_id" in (sql or "").lower() and "scan_id" in (sql or "").lower()
                for sql in index_defs
            )
            checks.append(("composite UNIQUE on (project_id, scan_id) exists",
                          composite_unique,
                          f"indexes: {[i[:80] for i in index_defs]}"))

            project_id_index = any(
                "project_id" in (sql or "").lower() and
                ("scan_id" not in (sql or "").lower() or "create index" in (sql or "").lower())
                for sql in index_defs
            )
            checks.append(("index on (project_id) exists",
                          project_id_index,
                          f"indexes: {[i[:80] for i in index_defs]}"))

        ok2, msg2 = _apply_sql_script(db_path, migrate_sql)
        idempotent = ok2
        if idempotent:
            checks.append(("migration is idempotent (re-runs cleanly)", True, "re-applied OK"))
        else:
            checks.append(("migration is idempotent (re-runs cleanly)", False, msg2))

    pass_count = sum(1 for _, ok, _ in checks if ok)
    expected = 5

    return {
        "pass": pass_count >= expected,
        "evidence": "; ".join(
            f"{'PASS' if ok else 'FAIL'} {name}" for name, ok, _ in checks
        ),
        "details": {
            "pass_count": min(pass_count, expected),
            "expected": expected,
            "files_written": files_written,
            "checks": [{"name": n, "ok": ok, "note": note} for n, ok, note in checks],
            "idempotent": idempotent,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(verify(Path.cwd(), {"tier": "t1_trivial"}), indent=2))
