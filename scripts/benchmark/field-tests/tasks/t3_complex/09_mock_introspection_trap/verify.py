"""verify.py for task 09 — mock-introspection trap.

Anti-patterns auto-fail:
- query_inspector.py modified
- try/except TypeError added in inspector
- mock.patch.object(spec=callable) workaround
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SEED_REL = "scripts/benchmark/field-tests/tasks/t3_complex/09_mock_introspection_trap/seed"
REQUIRED = ["tests/test_query_inspector.py"]
ORIGINAL_INSPECTOR_SHA = None  # filled in at first run


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    seed_dir = workdir / SEED_REL
    test_file = seed_dir / "tests" / "test_query_inspector.py"
    inspector_file = seed_dir / "query_inspector.py"

    if not test_file.exists() or not inspector_file.exists():
        return {
            "pass": False,
            "evidence": "missing seed files",
            "details": {"pass_count": 0, "expected": 4, "files_written": []},
        }

    inspector_body = inspector_file.read_text(encoding="utf-8")
    inspector_unchanged = (
        "_UNSAFE_PATTERNS" in inspector_body
        and "inspect.getsource(fn)" in inspector_body
        and "try:" not in inspector_body.split("def detect_unsafe")[1].split("def ")[0]
    )

    test_body = test_file.read_text(encoding="utf-8")
    no_swallow = "except TypeError" not in test_body or "swallow" not in test_body.lower()
    no_spec_band_aid = "spec=callable" not in test_body and "spec=builders.build_user_query" not in test_body

    checks: list[tuple[str, bool, str]] = []
    checks.append((
        "query_inspector.py unchanged (no swallow-fix)",
        inspector_unchanged,
        "modified" if not inspector_unchanged else "intact",
    ))
    checks.append((
        "no mock.patch(..., spec=...) band-aid",
        no_spec_band_aid,
        "found spec= workaround" if not no_spec_band_aid else "clean",
    ))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for src_rel in ("query_inspector.py", "builders.py", "tests/test_query_inspector.py"):
            src = seed_dir / src_rel
            dst = tmp_dir / src_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        (tmp_dir / "tests" / "__init__.py").touch()

        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_query_inspector.py",
             "-v", "--tb=short", "-q"],
            cwd=tmp_dir, capture_output=True, text=True, timeout=60, check=False,
        )
        pytest_out = pytest_proc.stdout + "\n" + pytest_proc.stderr
        pass_match = re.search(r"(\d+) passed", pytest_out)
        fail_match = re.search(r"(\d+) failed", pytest_out)
        pytest_pass = int(pass_match.group(1)) if pass_match else 0
        pytest_fail = int(fail_match.group(1)) if fail_match else 0

        checks.append((
            "3 tests pass (rewritten + 2 new)",
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
        "pass": pass_count >= 4,
        "evidence": "; ".join(
            f"{'PASS' if ok else 'FAIL'} {name}" for name, ok, _ in checks
        ),
        "details": {
            "pass_count": min(pass_count, 4),
            "expected": 4,
            "files_written": REQUIRED,
            "checks": [{"name": n, "ok": ok, "note": note} for n, ok, note in checks],
        },
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd(), {"tier": "t3_complex"}), indent=2))
