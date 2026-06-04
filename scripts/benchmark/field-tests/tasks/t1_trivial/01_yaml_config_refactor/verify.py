"""verify.py for task 01 — YAML config refactor.

Called by scorer.score_cell() after the target lane has executed in workdir.
Runs the seed's pytest contract against the modified files. Returns:
    {"pass": bool, "evidence": str, "details": {...}}
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t1_trivial/01_yaml_config_refactor/seed"


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    """Run the pytest contract against the worker's modified worker_runner.py.

    Strategy: copy the worker's modified files (worker_runner.py + optional
    config/worker_queues.yaml) into a fresh tmp dir alongside the seed's
    tests/, then `pytest -x -v` and parse the result.
    """
    seed_dir = workdir / SEED_REL
    if not seed_dir.exists():
        return {
            "pass": False,
            "evidence": f"seed missing at {seed_dir}",
            "details": {"pass_count": 0, "expected": 5},
        }

    target_module = seed_dir / "worker_runner.py"
    if not target_module.exists():
        return {
            "pass": False,
            "evidence": "worker_runner.py not found — worker did not write target file",
            "details": {"pass_count": 0, "expected": 5, "files_written": []},
        }

    files_written = ["worker_runner.py"]
    if (seed_dir / "config" / "worker_queues.yaml").exists():
        files_written.append("config/worker_queues.yaml")

    test_file = seed_dir / "tests" / "test_worker_runner.py"
    if not test_file.exists():
        return {
            "pass": False,
            "evidence": "test file vanished (contract violation)",
            "details": {"pass_count": 0, "expected": 5, "files_written": files_written},
        }

    cmd = [
        sys.executable, "-m", "pytest", str(test_file),
        "-v", "--tb=short", "--no-header", "-q",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=seed_dir, capture_output=True, text=True,
            timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "pass": False,
            "evidence": "pytest timeout >120s",
            "details": {"pass_count": 0, "expected": 5, "files_written": files_written},
        }

    out = proc.stdout + "\n" + proc.stderr
    # Parse the pytest summary line: "X passed" / "X failed" / "X errors"
    import re
    summary_match = re.search(r"(?:=+ )?(\d+) passed", out)
    pass_count = int(summary_match.group(1)) if summary_match else 0
    fail_match = re.search(r"(\d+) failed", out)
    fail_count = int(fail_match.group(1)) if fail_match else 0
    err_match = re.search(r"(\d+) error", out)
    fail_count += int(err_match.group(1)) if err_match else 0

    return {
        "pass": pass_count == 5 and fail_count == 0,
        "evidence": f"pytest: {pass_count} passed, {fail_count} failed/errored",
        "details": {
            "pass_count": pass_count,
            "expected": 5,
            "files_written": files_written,
            "pytest_exit": proc.returncode,
            "pytest_tail": out[-1500:],
        },
    }


if __name__ == "__main__":
    import json
    result = verify(Path.cwd(), {"tier": "t1_trivial"})
    print(json.dumps(result, indent=2))
