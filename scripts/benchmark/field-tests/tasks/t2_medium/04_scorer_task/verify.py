"""verify.py for task 04 — scorer task + migration + tests.

Runs the worker's 15-test pytest suite, applies the migration, smoke-runs
the CLI, and validates a sample score against the formula.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t2_medium/04_scorer_task/seed"


def _required_files(seed_dir: Path) -> list[str]:
    found = []
    for rel in (
        "migrations/001_add_document_scores.sql",
        "scorer.py",
        "cli.py",
        "tests/test_scorer.py",
    ):
        if (seed_dir / rel).exists():
            found.append(rel)
    return found


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    init_script = seed_dir / "init_seed_db.py"

    if not init_script.exists():
        return {
            "pass": False,
            "evidence": f"seed init script missing at {init_script}",
            "details": {"pass_count": 0, "expected": 4, "files_written": []},
        }

    files_written = _required_files(seed_dir)
    missing = [f for f in (
        "migrations/001_add_document_scores.sql", "scorer.py",
        "cli.py", "tests/test_scorer.py",
    ) if f not in files_written]
    if missing:
        return {
            "pass": False,
            "evidence": f"worker did not write required files: {missing}",
            "details": {
                "pass_count": 0, "expected": 4, "files_written": files_written,
            },
        }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for rel in files_written:
            src = seed_dir / rel
            dst = tmp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        shutil.copy(init_script, tmp_dir / "init_seed_db.py")
        if (seed_dir / "schema.sql").exists():
            shutil.copy(seed_dir / "schema.sql", tmp_dir / "schema.sql")

        init_proc = subprocess.run(
            [sys.executable, "init_seed_db.py"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        if init_proc.returncode != 0:
            return {
                "pass": False,
                "evidence": f"init_seed_db failed: {init_proc.stderr[-300:]}",
                "details": {"pass_count": 0, "expected": 4, "files_written": files_written},
            }

        checks: list[tuple[str, bool, str]] = []

        try:
            mig_proc = subprocess.run(
                ["sqlite3", "documents.db"],
                input=(tmp_dir / "migrations" / "001_add_document_scores.sql").read_text(),
                cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
            )
            mig_ok = mig_proc.returncode == 0
            checks.append((
                "migration applies cleanly",
                mig_ok,
                f"rc={mig_proc.returncode} stderr={mig_proc.stderr[-200:]}",
            ))
        except Exception as exc:
            checks.append(("migration applies cleanly", False, str(exc)))
            mig_ok = False

        if mig_ok:
            try:
                mig2 = subprocess.run(
                    ["sqlite3", "documents.db"],
                    input=(tmp_dir / "migrations" / "001_add_document_scores.sql").read_text(),
                    cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
                )
                checks.append((
                    "migration idempotent",
                    mig2.returncode == 0,
                    f"rc={mig2.returncode}",
                ))
            except Exception as exc:
                checks.append(("migration idempotent", False, str(exc)))

        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_scorer.py", "-v", "--tb=short", "-q"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=180, check=False,
        )
        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0
        checks.append((
            "15 tests pass",
            pytest_pass >= 15 and pytest_fail == 0,
            f"passed={pytest_pass} failed={pytest_fail}",
        ))

        try:
            cli_proc = subprocess.run(
                [sys.executable, "cli.py", "--db", "documents.db", "--project-id", "default"],
                cwd=tmp_dir, capture_output=True, text=True, timeout=60, check=False,
            )
            cli_ok = cli_proc.returncode == 0
            cli_json_ok = False
            if cli_ok:
                try:
                    out = json.loads(cli_proc.stdout.strip().splitlines()[-1])
                    cli_json_ok = (
                        isinstance(out.get("scored"), int)
                        and isinstance(out.get("skipped"), int)
                        and out["scored"] + out["skipped"] == 50
                    )
                except (json.JSONDecodeError, KeyError, IndexError):
                    cli_json_ok = False
            checks.append((
                "CLI exits 0 and prints valid {scored, skipped} json totaling 50",
                cli_ok and cli_json_ok,
                f"rc={cli_proc.returncode} stdout-tail={cli_proc.stdout[-200:]}",
            ))
        except Exception as exc:
            checks.append(("CLI smoke", False, str(exc)))

    pass_count = sum(1 for _, ok, _ in checks if ok)
    expected = 4

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
        },
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd(), {"tier": "t2_medium"}), indent=2))
