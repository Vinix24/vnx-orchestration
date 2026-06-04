"""verify.py for task 06 — flaky mock fix.

Runs the worker's fixed conftest with --timeout=5. If the mock is still
broken, pytest will timeout. If correctly fixed, 3 tests pass under 5s.
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


SEED_REL = "scripts/benchmark/field-tests/tasks/t2_medium/06_flaky_mock_fix/seed"


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    conftest = seed_dir / "tests" / "conftest.py"

    if not conftest.exists():
        return {
            "pass": False,
            "evidence": "tests/conftest.py missing",
            "details": {"pass_count": 0, "expected": 3, "files_written": []},
        }

    files_written = ["tests/conftest.py"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for src_rel in (
            "paginated_query.py",
            "tests/conftest.py",
            "tests/test_paginated_query.py",
        ):
            src = seed_dir / src_rel
            dst = tmp_dir / src_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        (tmp_dir / "tests" / "__init__.py").touch()

        checks: list[tuple[str, bool, str]] = []

        try:
            pytest_proc = subprocess.run(
                [sys.executable, "-m", "pytest",
                 "tests/test_paginated_query.py",
                 "-v", "--tb=short", "-q",
                 "--timeout=5"],
                cwd=tmp_dir, capture_output=True, text=True, timeout=20, check=False,
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            return {
                "pass": False,
                "evidence": "pytest itself timed out (>20s) — mock still hangs even with pytest-timeout",
                "details": {
                    "pass_count": 0, "expected": 3,
                    "files_written": files_written,
                },
            }

        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0
        timeout_failures = pytest_out.count("Timeout")

        checks.append((
            "no pytest-timeout failures",
            timeout_failures == 0,
            f"timeout markers in output: {timeout_failures}",
        ))
        checks.append((
            "3 tests pass",
            pytest_pass >= 3 and pytest_fail == 0,
            f"passed={pytest_pass} failed={pytest_fail}",
        ))
        checks.append((
            "pytest exits 0",
            pytest_proc.returncode == 0,
            f"rc={pytest_proc.returncode}",
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
    print(json.dumps(verify(Path.cwd(), {"tier": "t2_medium"}), indent=2))
