"""verify.py for task 07 — review state-machine.

Smoke-test the worker's state_machine + persistence + audit trail by:
1. Confirming all required files exist
2. Running the 12 pytest contract tests
3. Applying the migration idempotently
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t3_complex/07_state_machine_sse/seed"
REQUIRED = [
    "state_machine.py",
    "persistence.py",
    "tests/test_state_machine.py",
    "migrations/001_state_machine.sql",
]


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    files_written = [f for f in REQUIRED if (seed_dir / f).exists()]
    missing = [f for f in REQUIRED if f not in files_written]
    if missing:
        return {
            "pass": False,
            "evidence": f"worker did not write: {missing}",
            "details": {"pass_count": 0, "expected": 3, "files_written": files_written},
        }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for rel in REQUIRED:
            src = seed_dir / rel
            dst = tmp_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        (tmp_dir / "tests" / "__init__.py").touch()

        checks: list[tuple[str, bool, str]] = []

        mig_proc = subprocess.run(
            ["sqlite3", "test.db"],
            input=(tmp_dir / "migrations" / "001_state_machine.sql").read_text(),
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        mig_ok = mig_proc.returncode == 0
        checks.append((
            "migration applies + idempotent",
            mig_ok and subprocess.run(
                ["sqlite3", "test.db"],
                input=(tmp_dir / "migrations" / "001_state_machine.sql").read_text(),
                cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
            ).returncode == 0,
            f"first rc={mig_proc.returncode} stderr={mig_proc.stderr[-200:]}",
        ))

        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_state_machine.py",
             "-v", "--tb=short", "-q"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=180, check=False,
        )
        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0
        checks.append((
            "12 tests pass",
            pytest_pass >= 12 and pytest_fail == 0,
            f"passed={pytest_pass} failed={pytest_fail}",
        ))

        invalid_smoke = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); "
             "from state_machine import ReviewableDocument, InvalidTransition; "
             "d = ReviewableDocument(1); "
             "import contextlib; raised = False\n"
             "try:\n  d.transition('approve', 'op', 'skip-the-line')\n"
             "except InvalidTransition: raised = True\n"
             "print('OK' if raised else 'FAIL')"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=30, check=False,
        )
        invalid_ok = invalid_smoke.returncode == 0 and invalid_smoke.stdout.strip().endswith("OK")
        checks.append((
            "InvalidTransition raised for non-allowed transition",
            invalid_ok,
            f"stdout={invalid_smoke.stdout[-200:]}",
        ))

    pass_count = sum(1 for _, ok, _ in checks if ok)

    return {
        "pass": pass_count >= 3,
        "evidence": "; ".join(
            f"{'PASS' if ok else 'FAIL'} {name}" for name, ok, _ in checks
        ),
        "details": {
            "pass_count": min(pass_count, 3),
            "expected": 3,
            "files_written": files_written,
            "checks": [{"name": n, "ok": ok, "note": note} for n, ok, note in checks],
        },
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd(), {"tier": "t3_complex"}), indent=2))
