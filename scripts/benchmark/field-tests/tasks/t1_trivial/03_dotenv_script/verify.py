"""verify.py for task 03 — add load_dotenv() to backfill script.

Strategy: copy seed (with worker's edited backfill_check.py + the .env) to
a fresh tmp dir, run the script with a clean environment (no inherited
SUPABASE_*), and check that the script loads from .env and prints OK.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t1_trivial/03_dotenv_script/seed"


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    script_path = seed_dir / "backfill_check.py"
    env_path = seed_dir / ".env"

    if not script_path.exists():
        return {
            "pass": False,
            "evidence": "backfill_check.py missing from seed",
            "details": {"pass_count": 0, "expected": 3, "files_written": []},
        }

    script_body = script_path.read_text(encoding="utf-8")
    import ast
    try:
        tree = ast.parse(script_body)
    except SyntaxError as exc:
        return {
            "pass": False,
            "evidence": f"backfill_check.py has syntax error: {exc}",
            "details": {"pass_count": 0, "expected": 3, "files_written": ["backfill_check.py"]},
        }
    has_import = any(
        (isinstance(node, ast.ImportFrom) and node.module == "dotenv")
        or (isinstance(node, ast.Import) and any(a.name == "dotenv" for a in node.names))
        for node in ast.walk(tree)
    )
    has_call = any(
        isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "load_dotenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "load_dotenv")
        )
        for node in ast.walk(tree)
    )

    files_written = ["backfill_check.py"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        shutil.copy(script_path, tmp_dir / "backfill_check.py")
        shutil.copy(env_path, tmp_dir / ".env")

        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("SUPABASE_")}

        try:
            proc = subprocess.run(
                [sys.executable, "backfill_check.py"],
                cwd=tmp_dir, env=clean_env,
                capture_output=True, text=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "pass": False,
                "evidence": "script execution timeout >30s",
                "details": {"pass_count": 0, "expected": 3, "files_written": files_written},
            }

        out = proc.stdout
        err = proc.stderr
        exit_zero = proc.returncode == 0
        prints_ok = "OK: connected to" in out
        loaded_url = "benchmark.supabase.local" in out

    checks = [
        ("load_dotenv imported", has_import,
         "missing 'from dotenv import load_dotenv'" if not has_import else "found"),
        ("load_dotenv() called", has_call,
         "load_dotenv() call not present" if not has_call else "found"),
        ("script exits 0 with .env present", exit_zero,
         f"rc={proc.returncode}, stderr tail: {err[-300:]}"),
        ("prints 'OK: connected to'", prints_ok and loaded_url,
         f"stdout: {out[-300:]}"),
    ]

    pass_count = sum(1 for _, ok, _ in checks if ok)
    expected = 3

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
    import json
    print(json.dumps(verify(Path.cwd(), {"tier": "t1_trivial"}), indent=2))
